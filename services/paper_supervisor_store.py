"""Durable, append-only state for the Paper Supervisor.

This store deliberately does not know how to start a strategy.  It only makes
the Supervisor's decision boundary recoverable: one lock owner appends facts,
persists a pre-start intent before a control call, and projects a current state
for readers.  Existing control audit and execution records remain untouched.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping


EVENT_SCHEMA = "paper-supervisor-event-v1"
STATE_SCHEMA = "paper-supervisor-cycle-v1"
OBSERVATION_SCHEMA = "paper-supervisor-observation-v1"


class PaperSupervisorStoreError(RuntimeError):
    """The durable attempt state cannot safely be used."""


class PaperSupervisorLeaseHeld(RuntimeError):
    """Another process owns the non-blocking convergence lease."""


def _utc(value: str | datetime | None = None) -> str:
    if isinstance(value, datetime):
        current = value
    elif value:
        current = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    else:
        current = datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _digest(value: Mapping[str, Any]) -> str:
    body = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class PaperSupervisorStore:
    """One-writer, append-only Supervisor evidence store."""

    def __init__(self, output_root: Path) -> None:
        self.output_root = Path(output_root)
        self.root = self.output_root / "dualtrack" / "supervisor" / "convergence"
        # This pathname must never be atomically replaced.  flock follows the
        # inode, so a replaced JSON state file is not a valid flock target.
        self.lock_path = self.root / ".lease.lock"
        self.lease_path = self.root / "lease.json"

    def events_path(self, cycle_id: str) -> Path:
        return self.root / "events" / f"{cycle_id}.jsonl"

    def state_path(self, cycle_id: str) -> Path:
        return self.root / "states" / f"{cycle_id}.json"

    def observations_path(self, day: str) -> Path:
        return self.root / "observations" / f"{day}.jsonl"

    @contextmanager
    def lease(
        self,
        *,
        cycle_id: str,
        owner_id: str | None = None,
        now: str | datetime | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Acquire the durable single-flight lease without waiting."""

        self.root.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise PaperSupervisorLeaseHeld("paper_supervisor_lease_held") from exc
            prior = self._read_optional_json(self.lease_path)
            lease = {
                "schema_version": "paper-supervisor-lease-v1",
                "owner_id": owner_id or f"supervisor-{uuid.uuid4().hex}",
                "cycle_id": str(cycle_id),
                "acquired_at": _utc(now),
                "previous_lease": prior if prior and prior.get("released_at") is None else None,
            }
            self._replace_json(self.lease_path, lease)
            try:
                yield lease
            finally:
                self._replace_json(self.lease_path, {**lease, "released_at": _utc()})
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def events(self, cycle_id: str) -> list[dict[str, Any]]:
        path = self.events_path(cycle_id)
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise PaperSupervisorStoreError("attempt_store_corrupt") from exc
        for expected_sequence, line in enumerate(lines, start=1):
            if not line.strip():
                raise PaperSupervisorStoreError("attempt_store_corrupt")
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise PaperSupervisorStoreError("attempt_store_corrupt") from exc
            if (
                not isinstance(row, dict)
                or row.get("schema_version") != EVENT_SCHEMA
                or row.get("cycle_id") != cycle_id
                or row.get("sequence") != expected_sequence
            ):
                raise PaperSupervisorStoreError("attempt_store_corrupt")
            rows.append(dict(row))
        return rows

    def append_event(
        self,
        cycle_id: str,
        kind: str,
        *,
        now: str | datetime | None = None,
        attempt_id: str | None = None,
        episode_id: str | None = None,
        fields: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append and fsync one immutable Supervisor fact.

        Caller must already own :meth:`lease`; keeping the sequence calculation
        inside that critical section prevents concurrent read-modify-write loss.
        """

        rows = self.events(cycle_id)
        payload = {
            "schema_version": EVENT_SCHEMA,
            "event_id": f"supervisor-event-{uuid.uuid4().hex}",
            "sequence": len(rows) + 1,
            "cycle_id": str(cycle_id),
            "at": _utc(now),
            "kind": str(kind),
            "attempt_id": attempt_id,
            "episode_id": episode_id,
            **dict(fields or {}),
        }
        path = self.events_path(cycle_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._fsync_directory(path.parent)
        return payload

    def read_state(self, cycle_id: str) -> dict[str, Any]:
        current = self._read_optional_json(self.state_path(cycle_id))
        if not current:
            return self.initial_state(cycle_id)
        if current.get("schema_version") != STATE_SCHEMA or current.get("cycle_id") != cycle_id:
            raise PaperSupervisorStoreError("attempt_store_corrupt")
        return current

    def initial_state(self, cycle_id: str) -> dict[str, Any]:
        return {
            "schema_version": STATE_SCHEMA,
            "cycle_id": str(cycle_id),
            "status": "converging",
            "attempt_count": 0,
            "start_call_count": 0,
            "episode": {
                "episode_id": None,
                "consecutive_transient_failures": 0,
                "exhausted_at": None,
                "next_attempt_at": None,
            },
            "structural_blocker": None,
            "last_attempt_id": None,
            "last_observed_at": None,
            "alert_requested_at": None,
        }

    def write_state(self, cycle_id: str, state: Mapping[str, Any]) -> dict[str, Any]:
        payload = {**self.initial_state(cycle_id), **dict(state)}
        payload["schema_version"] = STATE_SCHEMA
        payload["cycle_id"] = str(cycle_id)
        self._replace_json(self.state_path(cycle_id), payload)
        return payload

    def spent_prepared_start_ids(self, cycle_id: str) -> set[str]:
        return {
            str(row.get("prepared_start_id"))
            for row in self.events(cycle_id)
            if row.get("kind") == "start_intent" and row.get("prepared_start_id")
        }

    def unfinished_start_intent(self, cycle_id: str) -> dict[str, Any] | None:
        intents = [row for row in self.events(cycle_id) if row.get("kind") == "start_intent"]
        results = {
            str(row.get("attempt_id"))
            for row in self.events(cycle_id)
            if row.get("kind") == "start_result"
        }
        return next((row for row in reversed(intents) if str(row.get("attempt_id")) not in results), None)

    def append_observation(
        self,
        cycle_id: str,
        *,
        observed_at: str | datetime | None = None,
        healthy_running: bool,
        fields: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        at = _utc(observed_at)
        payload = {
            "schema_version": OBSERVATION_SCHEMA,
            "observation_id": f"supervisor-observation-{uuid.uuid4().hex}",
            "cycle_id": str(cycle_id),
            "observed_at": at,
            "healthy_running": bool(healthy_running),
            **dict(fields or {}),
        }
        path = self.observations_path(at[:10])
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(descriptor, (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._fsync_directory(path.parent)
        return payload

    def read_model(self, cycle_id: str) -> dict[str, Any]:
        state = self.read_state(cycle_id)
        events = self.events(cycle_id)
        attempts = [
            {
                key: row.get(key)
                for key in (
                    "at", "attempt_id", "kind", "mode", "result", "machine_code",
                    "human_reason", "next_action", "preview_id", "prepared_start_id",
                    "classifier_version", "envelope_verification_id",
                )
                if key in row
            }
            for row in events
            if row.get("attempt_id")
        ]
        return {
            **state,
            "attempts": attempts,
            "lease": self._read_optional_json(self.lease_path) or {},
        }

    @staticmethod
    def evidence_hash(value: Mapping[str, Any]) -> str:
        return _digest(value)

    def _read_optional_json(self, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PaperSupervisorStoreError("attempt_store_corrupt") from exc
        if not isinstance(value, dict):
            raise PaperSupervisorStoreError("attempt_store_corrupt")
        return dict(value)

    def _replace_json(self, path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(dict(payload), handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            self._fsync_directory(path.parent)
        except Exception:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
