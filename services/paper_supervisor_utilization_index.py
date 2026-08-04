"""Source-bound compact indexes for Supervisor utilization evidence.

The immutable event and observation JSONL files remain authoritative.  This
module only avoids re-parsing their large, repeated running-evidence payloads
after one complete store validation.  A cache hit is accepted solely when the
full raw source-file SHA-256 identities and the derived-index digest still
match exactly.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from services.dualtrack_clock import cycle_window_from_id
from services.paper_supervisor_evidence import validate_running_evidence
from services.paper_supervisor_store import (
    MAX_OBSERVATION_CYCLE_BYTES,
    PaperSupervisorStore,
    SupervisorStoreError,
)

UTILIZATION_INDEX_SCHEMA_VERSION = (
    "paper-supervisor-utilization-index-v1"
)
STABLE_SOURCE_READ_ATTEMPTS = 3
MAX_INDEX_BYTES = 32 * 1024 * 1024


class PaperSupervisorUtilizationIndex:
    """Build and reuse compact utilization points bound to exact sources."""

    def __init__(self, output_root: Path) -> None:
        self.output_root = Path(output_root)
        self.store_root = (
            self.output_root
            / "dualtrack"
            / "supervisor"
            / "convergence"
        )
        self.index_root = self.store_root / "utilization_indexes"

    def source_identity(self, cycle_id: str) -> dict[str, Any]:
        """Return stable, full-file identities for both authorities."""

        cycle = _cycle_id(cycle_id)
        return {
            "events": _stable_file_digest(
                self.store_root / "events" / f"{cycle}.jsonl"
            ),
            "observations": _stable_file_digest(
                self.store_root / "observations" / f"{cycle}.jsonl"
            ),
        }

    def read_or_build(
        self,
        store: PaperSupervisorStore,
        cycle_id: str,
        *,
        validated_observations: Sequence[Mapping[str, Any]] | None = None,
        validated_source_identity: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Return exact compact points, rebuilding from authority as needed."""

        cycle = _cycle_id(cycle_id)
        supplied_identity = (
            _json_copy(validated_source_identity)
            if isinstance(validated_source_identity, Mapping)
            else None
        )
        supplied_rows = (
            [_json_copy(row) for row in validated_observations]
            if validated_observations is not None
            else None
        )
        for _attempt in range(STABLE_SOURCE_READ_ATTEMPTS):
            source = self.source_identity(cycle)
            cached = self._read_index(cycle, source=source)
            if cached is not None:
                return cached
            if (
                supplied_rows is not None
                and supplied_identity == source
            ):
                observations = supplied_rows
            else:
                observations = store.read_cycle_observation_snapshot(cycle)
            after = self.source_identity(cycle)
            if after != source:
                supplied_rows = None
                supplied_identity = None
                time.sleep(0.005)
                continue
            points = _compact_points(cycle, observations)
            self._write_index(cycle, source=source, points=points)
            if self.source_identity(cycle) != source:
                supplied_rows = None
                supplied_identity = None
                time.sleep(0.005)
                continue
            return points
        raise SupervisorStoreError("attempt_store_busy")

    def _read_index(
        self,
        cycle_id: str,
        *,
        source: Mapping[str, Any],
    ) -> list[dict[str, Any]] | None:
        path = self.index_root / f"{cycle_id}.json"
        if not path.exists():
            return None
        try:
            raw = _stable_regular_file_bytes(path, max_bytes=MAX_INDEX_BYTES)
            value = json.loads(raw)
            if not isinstance(value, dict):
                return None
            supplied_digest = str(value.get("index_digest") or "")
            unsigned = {
                key: item
                for key, item in value.items()
                if key != "index_digest"
            }
            if (
                set(value)
                != {
                    "schema_version",
                    "cycle_id",
                    "source_identity",
                    "points",
                    "index_digest",
                }
                or value.get("schema_version")
                != UTILIZATION_INDEX_SCHEMA_VERSION
                or value.get("cycle_id") != cycle_id
                or value.get("source_identity") != source
                or supplied_digest != _digest(unsigned)
            ):
                return None
            return _validate_compact_points(cycle_id, value.get("points"))
        except (
            OSError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            return None

    def _write_index(
        self,
        cycle_id: str,
        *,
        source: Mapping[str, Any],
        points: Sequence[Mapping[str, Any]],
    ) -> None:
        payload = {
            "schema_version": UTILIZATION_INDEX_SCHEMA_VERSION,
            "cycle_id": cycle_id,
            "source_identity": _json_copy(source),
            "points": [_json_copy(row) for row in points],
        }
        payload["index_digest"] = _digest(payload)
        _atomic_write_json(
            self.index_root / f"{cycle_id}.json",
            payload,
        )


def _compact_points(
    cycle_id: str,
    observations: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for row in observations:
        payload = row.get("payload")
        if not isinstance(payload, Mapping):
            raise SupervisorStoreError("attempt_store_corrupt")
        evidence = payload.get("running_evidence")
        if evidence is None:
            continue
        try:
            validated = validate_running_evidence(
                _mapping(evidence)
            )
        except (TypeError, ValueError) as exc:
            raise SupervisorStoreError("attempt_store_corrupt") from exc
        if (
            str(row.get("cycle_id") or "") != cycle_id
            or validated["cycle_id"] != cycle_id
        ):
            raise SupervisorStoreError("attempt_store_corrupt")
        identity = {
            "cycle_id": validated["cycle_id"],
            "plan_identity": validated["plan_identity"],
            "expected_slots": validated["expected_slots"],
            "expected_slot_digest": validated["expected_slot_digest"],
        }
        points.append(
            {
                "cycle_id": cycle_id,
                "sequence": row.get("sequence"),
                "payload": {
                    "running_evidence": {
                        "cycle_id": validated["cycle_id"],
                        "evidence_at": validated["evidence_at"],
                        "persisted_at": validated["persisted_at"],
                        "running_proven": validated["running_proven"],
                        "proof_status": validated["proof_status"],
                        "running_identity_digest": _digest(identity),
                    }
                },
            }
        )
    return _validate_compact_points(cycle_id, points)


def _validate_compact_points(
    cycle_id: str,
    value: Any,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise TypeError("utilization_index_invalid")
    result: list[dict[str, Any]] = []
    previous_sequence = 0
    for raw in value:
        if not isinstance(raw, Mapping) or set(raw) != {
            "cycle_id",
            "sequence",
            "payload",
        }:
            raise ValueError("utilization_index_invalid")
        sequence = raw.get("sequence")
        payload = raw.get("payload")
        evidence = (
            payload.get("running_evidence")
            if isinstance(payload, Mapping)
            else None
        )
        if (
            raw.get("cycle_id") != cycle_id
            or not isinstance(sequence, int)
            or sequence <= previous_sequence
            or not isinstance(evidence, Mapping)
            or set(evidence)
            != {
                "cycle_id",
                "evidence_at",
                "persisted_at",
                "running_proven",
                "proof_status",
                "running_identity_digest",
            }
            or evidence.get("cycle_id") != cycle_id
            or not isinstance(evidence.get("running_proven"), bool)
            or evidence.get("proof_status")
            not in {"proven", "not_proven", "unknown"}
            or not _is_digest(evidence.get("running_identity_digest"))
        ):
            raise ValueError("utilization_index_invalid")
        _timestamp(evidence.get("evidence_at"))
        _timestamp(evidence.get("persisted_at"))
        result.append(_json_copy(raw))
        previous_sequence = sequence
    return result


def compact_running_identity_matches(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
) -> bool | None:
    """Compare compact points or return ``None`` for full evidence rows."""

    one = left.get("running_identity_digest")
    two = right.get("running_identity_digest")
    if one is None and two is None:
        return None
    return (
        _is_digest(one)
        and _is_digest(two)
        and left.get("cycle_id") == right.get("cycle_id")
        and one == two
    )


def _stable_file_digest(path: Path) -> dict[str, Any]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    for _attempt in range(STABLE_SOURCE_READ_ATTEMPTS):
        descriptor = -1
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            if not path.exists():
                return {"present": False, "size": 0, "sha256": None}
            time.sleep(0.005)
            continue
        except OSError as exc:
            raise SupervisorStoreError("attempt_store_corrupt") from exc
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_size > MAX_OBSERVATION_CYCLE_BYTES
            ):
                raise SupervisorStoreError("attempt_store_corrupt")
            digest = hashlib.sha256()
            remaining = before.st_size
            while remaining:
                chunk = os.read(descriptor, min(1_048_576, remaining))
                if not chunk:
                    break
                digest.update(chunk)
                remaining -= len(chunk)
            after = os.fstat(descriptor)
        except OSError as exc:
            raise SupervisorStoreError("attempt_store_corrupt") from exc
        finally:
            os.close(descriptor)
        if (
            remaining == 0
            and _stat_identity(before) == _stat_identity(after)
        ):
            return {
                "present": True,
                "size": int(before.st_size),
                "sha256": digest.hexdigest(),
            }
        time.sleep(0.005)
    raise SupervisorStoreError("attempt_store_busy")


def _stable_regular_file_bytes(path: Path, *, max_bytes: int) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > max_bytes
        ):
            raise ValueError("utilization_index_invalid")
        remaining = before.st_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(1_048_576, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if remaining or _stat_identity(before) != _stat_identity(after):
        raise ValueError("utilization_index_invalid")
    return b"".join(chunks)


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = (_canonical(value) + "\n").encode("utf-8")
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise SupervisorStoreError("attempt_store_corrupt") from exc


def _cycle_id(value: str) -> str:
    cycle = str(value or "")
    try:
        if cycle_window_from_id(cycle).cycle_id != cycle:
            raise ValueError
    except ValueError as exc:
        raise SupervisorStoreError("attempt_store_corrupt") from exc
    return cycle


def _mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("utilization_index_invalid")
    return _json_copy(value)


def _json_copy(value: Any) -> Any:
    return json.loads(_canonical(value))


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _is_digest(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _timestamp(value: Any) -> None:
    from datetime import datetime

    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("utilization_index_invalid")


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
    )
