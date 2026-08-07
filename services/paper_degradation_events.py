"""Immutable evidence for Paper-only degraded-continuation decisions.

The normal safety gate still runs first.  This journal records the explicit
Paper alternative selected afterwards; it is never itself command authority.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.journal_store import load_json
from services.paper_supervisor_evidence import (
    RunningEvidenceError,
    validate_running_evidence,
)
from services.paper_supervisor_store import PaperSupervisorStore
from services.strategy_control_plane import production_mutation_lock

DEGRADATION_EVENT_SCHEMA_VERSION = "paper-degradation-event-v1"
DEGRADATION_CYCLE_EVIDENCE_SCHEMA_VERSION = (
    "paper-degradation-cycle-evidence-v1"
)
CONTINUITY_EVIDENCE_SCHEMA_VERSION = "paper-continuity-evidence-v1"
_CYCLE_ID = re.compile(r"^\d{4}-\d{2}-\d{2}_(DAY|NIGHT)$")
_EVENT_FIELDS = frozenset(
    {
        "schema_version",
        "sequence",
        "event_id",
        "cycle_id",
        "execution_profile",
        "bypassed_gate",
        "original_machine_code",
        "original_reason",
        "alternative_action",
        "occurred_at",
        "previous_event_digest",
        "event_digest",
    }
)
_EVENT_OPTIONAL_FIELDS = frozenset({"exception_receipt_digest"})
_TRANSITION_FIELDS = frozenset(
    {
        "transition_id",
        "cycle_id",
        "stop_observed_at",
        "running_proven_at",
        "gap_seconds",
        "stop_observation_sha256",
        "running_observation_sha256",
        "transition_digest",
    }
)


class PaperDegradationEvidenceError(ValueError):
    """Stable fail-closed signal for malformed degradation evidence."""


class PaperDegradationEventStore:
    """Append and verify one hash-linked event journal per 12h cycle."""

    def __init__(self, output_root: Path) -> None:
        self.output_root = Path(output_root)
        self.root = (
            self.output_root
            / "dualtrack"
            / "supervisor"
            / "degradation_events"
        )

    def record(
        self,
        *,
        event_id: str,
        cycle_id: str,
        execution_profile: str,
        bypassed_gate: str,
        original_machine_code: str,
        original_reason: str,
        alternative_action: str,
        occurred_at: str,
        exception_receipt_digest: str | None = None,
    ) -> dict[str, Any]:
        """Append exactly once; a reused identity must have identical facts."""

        base = {
            "schema_version": DEGRADATION_EVENT_SCHEMA_VERSION,
            "event_id": _required_text(event_id, "event_id"),
            "cycle_id": _cycle_id(cycle_id),
            "execution_profile": _required_text(
                execution_profile,
                "execution_profile",
            ),
            "bypassed_gate": _required_text(
                bypassed_gate,
                "bypassed_gate",
            ),
            "original_machine_code": _required_text(
                original_machine_code,
                "original_machine_code",
            ),
            "original_reason": _required_text(
                original_reason,
                "original_reason",
            ),
            "alternative_action": _required_text(
                alternative_action,
                "alternative_action",
            ),
            "occurred_at": _timestamp(occurred_at).isoformat(),
        }
        if exception_receipt_digest is not None:
            base["exception_receipt_digest"] = _digest_text(
                exception_receipt_digest
            )
        with production_mutation_lock(self.output_root):
            rows = self.events(base["cycle_id"])
            for existing in rows:
                if existing["event_id"] != base["event_id"]:
                    continue
                observed = {
                    key: existing[key]
                    for key in base
                }
                if observed != base:
                    raise PaperDegradationEvidenceError(
                        "paper_degradation_event_identity_conflict"
                    )
                return existing
            event = {
                **base,
                "sequence": len(rows) + 1,
                "previous_event_digest": (
                    rows[-1]["event_digest"] if rows else None
                ),
            }
            event["event_digest"] = _digest(event)
            validate_degradation_event_chain(
                [*rows, event],
                cycle_id=base["cycle_id"],
            )
            _atomic_write_json_fsync(
                self._path(base["cycle_id"]),
                [*rows, event],
            )
            return event

    def events(self, cycle_id: str) -> list[dict[str, Any]]:
        cycle = _cycle_id(cycle_id)
        path = self._path(cycle)
        if self.root.exists() and (
            not self.root.is_dir() or self.root.is_symlink()
        ):
            raise PaperDegradationEvidenceError(
                "paper_degradation_event_store_corrupt"
            )
        if path.exists() and (not path.is_file() or path.is_symlink()):
            raise PaperDegradationEvidenceError(
                "paper_degradation_event_store_corrupt"
            )
        try:
            rows = load_json(path)
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            raise PaperDegradationEvidenceError(
                "paper_degradation_event_store_corrupt"
            ) from exc
        return validate_degradation_event_chain(rows, cycle_id=cycle)

    def cycle_evidence(self, cycle_id: str) -> dict[str, Any]:
        cycle = _cycle_id(cycle_id)
        events = self.events(cycle)
        return {
            "schema_version": (
                DEGRADATION_CYCLE_EVIDENCE_SCHEMA_VERSION
            ),
            "cycle_id": cycle,
            "events": events,
            "event_count": len(events),
            "events_digest": _digest(events),
            "tail_event_digest": (
                events[-1]["event_digest"] if events else None
            ),
        }

    def _path(self, cycle_id: str) -> Path:
        return self.root / f"{_cycle_id(cycle_id)}.json"


def validate_degradation_event_chain(
    rows: Any,
    *,
    cycle_id: str,
) -> list[dict[str, Any]]:
    """Return a JSON copy only when the complete event chain is exact."""

    cycle = _cycle_id(cycle_id)
    if not isinstance(rows, list):
        raise PaperDegradationEvidenceError(
            "paper_degradation_event_store_corrupt"
        )
    validated: list[dict[str, Any]] = []
    previous: str | None = None
    seen: set[str] = set()
    for expected_sequence, value in enumerate(rows, start=1):
        if not isinstance(value, Mapping):
            raise PaperDegradationEvidenceError(
                "paper_degradation_event_store_corrupt"
            )
        row = json.loads(json.dumps(dict(value)))
        supplied = str(row.get("event_digest") or "")
        if (
            not (
                set(row) == _EVENT_FIELDS
                or set(row) == _EVENT_FIELDS | _EVENT_OPTIONAL_FIELDS
            )
            or row.get("schema_version")
            != DEGRADATION_EVENT_SCHEMA_VERSION
            or row.get("sequence") != expected_sequence
            or row.get("cycle_id") != cycle
            or row.get("previous_event_digest") != previous
            or not supplied
            or not hmac.compare_digest(
                supplied,
                _digest(
                    {
                        key: item
                        for key, item in row.items()
                        if key != "event_digest"
                    }
                ),
            )
        ):
            raise PaperDegradationEvidenceError(
                "paper_degradation_event_store_corrupt"
            )
        for field in (
            "event_id",
            "execution_profile",
            "bypassed_gate",
            "original_machine_code",
            "original_reason",
            "alternative_action",
        ):
            _required_text(row.get(field), field)
        _timestamp(row.get("occurred_at"))
        if row.get("exception_receipt_digest") is not None:
            _digest_text(row.get("exception_receipt_digest"))
        if row["event_id"] in seen:
            raise PaperDegradationEvidenceError(
                "paper_degradation_event_store_corrupt"
            )
        seen.add(row["event_id"])
        validated.append(row)
        previous = supplied
    return validated


def build_cycle_continuity_evidence(
    output_root: Path,
    cycle_id: str,
) -> dict[str, Any]:
    """Build stop-to-running transitions from sealed Supervisor evidence."""

    observations = PaperSupervisorStore(
        Path(output_root)
    ).observations(_cycle_id(cycle_id))
    return build_continuity_evidence_from_observations(
        observations,
        cycle_id=cycle_id,
    )


def build_continuity_evidence_from_observations(
    observations: Sequence[Mapping[str, Any]],
    *,
    cycle_id: str,
) -> dict[str, Any]:
    """Project every observed stopped interval that later becomes proven."""

    cycle = _cycle_id(cycle_id)
    stopped: tuple[datetime, str] | None = None
    transitions: list[dict[str, Any]] = []
    for observation in observations:
        payload = (
            dict(observation.get("payload") or {})
            if isinstance(observation, Mapping)
            else {}
        )
        evidence_value = payload.get("running_evidence")
        if not isinstance(evidence_value, Mapping):
            continue
        try:
            evidence = validate_running_evidence(evidence_value)
        except RunningEvidenceError as exc:
            raise PaperDegradationEvidenceError(
                "paper_continuity_evidence_invalid"
            ) from exc
        if evidence["cycle_id"] != cycle:
            raise PaperDegradationEvidenceError(
                "paper_continuity_evidence_invalid"
            )
        at = _timestamp(evidence["evidence_at"])
        observation_hash = _digest_text(
            observation.get("observation_sha256")
        )
        runtime = dict(evidence.get("runtime") or {})
        if (
            evidence["running_proven"] is False
            and runtime.get("actual_state") == "stopped"
        ):
            if stopped is None:
                stopped = (at, observation_hash)
            continue
        if evidence["running_proven"] is not True or stopped is None:
            continue
        stopped_at, stopped_hash = stopped
        gap_seconds = (at - stopped_at).total_seconds()
        if gap_seconds < 0:
            raise PaperDegradationEvidenceError(
                "paper_continuity_evidence_invalid"
            )
        transition = {
            "transition_id": "",
            "cycle_id": cycle,
            "stop_observed_at": stopped_at.isoformat(),
            "running_proven_at": at.isoformat(),
            "gap_seconds": gap_seconds,
            "stop_observation_sha256": stopped_hash,
            "running_observation_sha256": observation_hash,
        }
        transition["transition_id"] = (
            f"paper-continuity-{_digest(transition)[:24]}"
        )
        transition["transition_digest"] = _digest(transition)
        transitions.append(transition)
        stopped = None
    validate_continuity_transitions(transitions, cycle_id=cycle)
    return {
        "schema_version": CONTINUITY_EVIDENCE_SCHEMA_VERSION,
        "cycle_id": cycle,
        "transitions": transitions,
        "transition_count": len(transitions),
        "transitions_digest": _digest(transitions),
    }


def validate_continuity_transitions(
    rows: Any,
    *,
    cycle_id: str,
) -> list[dict[str, Any]]:
    cycle = _cycle_id(cycle_id)
    if not isinstance(rows, list):
        raise PaperDegradationEvidenceError(
            "paper_continuity_evidence_invalid"
        )
    validated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in rows:
        if not isinstance(value, Mapping):
            raise PaperDegradationEvidenceError(
                "paper_continuity_evidence_invalid"
            )
        row = json.loads(json.dumps(dict(value)))
        supplied = str(row.get("transition_digest") or "")
        if (
            set(row) != _TRANSITION_FIELDS
            or row.get("cycle_id") != cycle
            or not supplied
            or not hmac.compare_digest(
                supplied,
                _digest(
                    {
                        key: item
                        for key, item in row.items()
                        if key != "transition_digest"
                    }
                ),
            )
        ):
            raise PaperDegradationEvidenceError(
                "paper_continuity_evidence_invalid"
            )
        transition_id = _required_text(
            row.get("transition_id"),
            "transition_id",
        )
        stopped_at = _timestamp(row.get("stop_observed_at"))
        running_at = _timestamp(row.get("running_proven_at"))
        try:
            gap = float(row.get("gap_seconds"))
        except (TypeError, ValueError) as exc:
            raise PaperDegradationEvidenceError(
                "paper_continuity_evidence_invalid"
            ) from exc
        if gap < 0 or abs(gap - (running_at - stopped_at).total_seconds()) > 1e-6:
            raise PaperDegradationEvidenceError(
                "paper_continuity_evidence_invalid"
            )
        _digest_text(row.get("stop_observation_sha256"))
        _digest_text(row.get("running_observation_sha256"))
        if transition_id in seen:
            raise PaperDegradationEvidenceError(
                "paper_continuity_evidence_invalid"
            )
        seen.add(transition_id)
        validated.append(row)
    return validated


def validate_packaged_degradation_evidence(
    package: Mapping[str, Any],
) -> None:
    """Require complete degradation and continuity evidence in v2 packages."""

    cycle = _cycle_id(str(package.get("cycle_id") or ""))
    events = validate_degradation_event_chain(
        package.get("degradation_events"),
        cycle_id=cycle,
    )
    transitions = validate_continuity_transitions(
        package.get("continuity_transitions"),
        cycle_id=cycle,
    )
    if (
        package.get("degradation_event_count") != len(events)
        or package.get("degradation_events_digest") != _digest(events)
        or package.get("degradation_event_tail_digest")
        != (events[-1]["event_digest"] if events else None)
        or package.get("continuity_transition_count")
        != len(transitions)
        or package.get("continuity_transitions_digest")
        != _digest(transitions)
    ):
        raise PaperDegradationEvidenceError(
            "paper_cycle_degradation_evidence_invalid"
        )


def _cycle_id(value: Any) -> str:
    text = str(value or "").strip()
    if not _CYCLE_ID.fullmatch(text):
        raise PaperDegradationEvidenceError(
            "paper_degradation_cycle_id_invalid"
        )
    return text


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 1024:
        raise PaperDegradationEvidenceError(
            f"paper_degradation_{field}_invalid"
        )
    return text


def _timestamp(value: Any) -> datetime:
    text = str(value or "").strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise PaperDegradationEvidenceError(
            "paper_degradation_timestamp_invalid"
        ) from exc
    if parsed.tzinfo is None:
        raise PaperDegradationEvidenceError(
            "paper_degradation_timestamp_invalid"
        )
    return parsed.astimezone(timezone.utc)


def _digest_text(value: Any) -> str:
    text = str(value or "")
    if (
        len(text) != 64
        or any(character not in "0123456789abcdef" for character in text)
    ):
        raise PaperDegradationEvidenceError(
            "paper_degradation_digest_invalid"
        )
    return text


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _atomic_write_json_fsync(path: Path, rows: list[dict[str, Any]]) -> None:
    """Publish one complete append with file and directory durability."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise PaperDegradationEvidenceError(
            "paper_degradation_event_store_corrupt"
        )
    payload = json.dumps(
        rows,
        indent=2,
        ensure_ascii=False,
        sort_keys=True,
    ) + "\n"
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
