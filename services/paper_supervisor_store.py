"""Crash-safe, Paper-only persistence for Supervisor start attempts.

This module deliberately has no control-plane or execution-adapter import.
Callers may supply one public Paper start operation, but the store first makes
the intent durable and permanently spends its ``prepared_start_id``.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import socket
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence


EVENT_SCHEMA_VERSION = "paper-supervisor-convergence-event-v1"
STATE_SCHEMA_VERSION = "paper-supervisor-cycle-state-v1"
LEASE_SCHEMA_VERSION = "paper-supervisor-lease-v1"
AUTHORITY_SCHEMA_VERSION = "paper-supervisor-start-authority-v1"
_EVENT_TYPES = frozenset({"start_intent", "start_result", "recovery_result"})
_RESULTS = frozenset({"accepted", "rejected", "unknown"})
_RECOVERY_RESULTS = frozenset(
    {"executed", "clean_rejection", "control_outcome_unknown"}
)
_CYCLE_SUFFIXES = ("_DAY", "_NIGHT")


class SupervisorStoreError(RuntimeError):
    """Stable fail-closed store error."""

    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


@dataclass(frozen=True)
class StartAuthoritySnapshot:
    """Exact read-only authority used to resolve one unfinished start."""

    cycle_id: str
    active_plan: Mapping[str, Any]
    runtime: Mapping[str, Any]
    accepted_order_fingerprints: Sequence[str]
    open_position_count: int
    reconciliation: Mapping[str, Any]
    control_events: Sequence[Mapping[str, Any]]
    schema_version: str = AUTHORITY_SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "cycle_id": self.cycle_id,
            "active_plan": dict(self.active_plan),
            "runtime": dict(self.runtime),
            "accepted_order_fingerprints": list(
                self.accepted_order_fingerprints
            ),
            "open_position_count": self.open_position_count,
            "reconciliation": dict(self.reconciliation),
            "control_events": [dict(row) for row in self.control_events],
        }


class PaperSupervisorStore:
    """Append-only event authority plus atomically projected cycle state."""

    def __init__(
        self,
        output_root: Path,
        *,
        now: Callable[[], datetime] | None = None,
        hostname: Callable[[], str] | None = None,
        pid: Callable[[], int] | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.root = (
            self.output_root
            / "dualtrack"
            / "supervisor"
            / "convergence"
        )
        self.lock_path = self.root / ".lease.lock"
        self.lease_path = self.root / "lease.json"
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.hostname = hostname or socket.gethostname
        self.pid = pid or os.getpid

    @contextmanager
    def try_lease(
        self,
        cycle_id: str,
        *,
        holder_id: str,
    ) -> Iterator[SupervisorLease | None]:
        """Yield one non-blocking lease or ``None`` to a contender."""

        cycle = _cycle_id(cycle_id)
        holder = _identity(holder_id, "supervisor_lease_holder_invalid")
        self.root.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            self.lock_path,
            os.O_CREAT | os.O_RDWR,
            0o600,
        )
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(descriptor)
                descriptor = -1
                yield None
                return
            lease = SupervisorLease(
                store=self,
                descriptor=descriptor,
                lease_id=f"supervisor-lease-{uuid.uuid4().hex}",
                cycle_id=cycle,
                holder_id=holder,
                acquired_at=_utc(self.now()).isoformat(),
                lock_inode=int(os.fstat(descriptor).st_ino),
            )
            self._write_lease_projection(lease, status="held")
            try:
                yield lease
            finally:
                lease._active = False
                self._write_lease_projection(lease, status="released")
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
                descriptor = -1
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def events(self, cycle_id: str) -> list[dict[str, Any]]:
        """Read and validate the complete event chain."""

        cycle = _cycle_id(cycle_id)
        path = self._events_path(cycle)
        if not path.exists():
            return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise SupervisorStoreError("attempt_store_corrupt") from exc
        events: list[dict[str, Any]] = []
        previous_hash: str | None = None
        for expected_sequence, line in enumerate(lines, start=1):
            if not line.strip():
                raise SupervisorStoreError("attempt_store_corrupt")
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, TypeError) as exc:
                raise SupervisorStoreError("attempt_store_corrupt") from exc
            _validate_event(
                event,
                cycle_id=cycle,
                expected_sequence=expected_sequence,
                previous_hash=previous_hash,
            )
            events.append(event)
            previous_hash = str(event["event_sha256"])
        _project(events, cycle_id=cycle)
        return events

    def current_state(self, cycle_id: str) -> dict[str, Any]:
        """Return deterministic state; reject a divergent projection."""

        cycle = _cycle_id(cycle_id)
        events = self.events(cycle)
        projected = _project(events, cycle_id=cycle)
        path = self._state_path(cycle)
        if not path.exists():
            return projected
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            raise SupervisorStoreError("attempt_store_corrupt") from exc
        _validate_stored_state(
            stored,
            events=events,
            cycle_id=cycle,
        )
        return projected

    def unfinished_intent(self, cycle_id: str) -> dict[str, Any] | None:
        state = self.current_state(cycle_id)
        pending = state.get("unfinished_intent")
        return dict(pending) if isinstance(pending, dict) else None

    def _append(
        self,
        lease: SupervisorLease,
        *,
        event_type: str,
        attempt_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        lease._require_active(self)
        if event_type not in _EVENT_TYPES:
            raise SupervisorStoreError("supervisor_event_type_invalid")
        attempt = _identity(
            attempt_id,
            "supervisor_attempt_id_invalid",
        )
        projected = self.current_state(lease.cycle_id)
        events = self.events(lease.cycle_id)
        self._validate_transition(
            projected,
            event_type=event_type,
            attempt_id=attempt,
            payload=payload,
        )
        canonical_payload = _json_object(
            payload,
            "supervisor_event_payload_invalid",
        )
        event = {
            "schema_version": EVENT_SCHEMA_VERSION,
            "sequence": len(events) + 1,
            "event_type": event_type,
            "event_id": f"supervisor-event-{uuid.uuid4().hex}",
            "cycle_id": lease.cycle_id,
            "attempt_id": attempt,
            "recorded_at": _utc(self.now()).isoformat(),
            "lease_id": lease.lease_id,
            "previous_event_sha256": (
                events[-1]["event_sha256"] if events else None
            ),
            "payload": canonical_payload,
        }
        event["event_sha256"] = _digest(event)
        self._append_event_line(lease.cycle_id, event)
        updated_events = [*events, event]
        updated_state = _project(
            updated_events,
            cycle_id=lease.cycle_id,
        )
        self._write_state(lease.cycle_id, updated_state)
        return event

    @staticmethod
    def _validate_transition(
        state: Mapping[str, Any],
        *,
        event_type: str,
        attempt_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        attempts = [
            row
            for row in state.get("attempts", [])
            if isinstance(row, dict)
        ]
        existing = next(
            (
                row
                for row in attempts
                if str(row.get("attempt_id") or "") == attempt_id
            ),
            None,
        )
        if event_type == "start_intent":
            if state.get("unfinished_intent"):
                raise SupervisorStoreError(
                    "unfinished_start_intent_requires_recovery"
                )
            if existing is not None:
                raise SupervisorStoreError("supervisor_attempt_id_reused")
            prepared_id = _identity(
                payload.get("prepared_start_id"),
                "prepared_start_id_invalid",
            )
            if prepared_id in set(
                state.get("spent_prepared_start_ids") or []
            ):
                raise SupervisorStoreError(
                    "prepared_start_id_already_spent"
                )
            _validate_intent_payload(payload)
            return
        if existing is None:
            raise SupervisorStoreError("supervisor_attempt_missing")
        if existing.get("terminal_result") is not None:
            raise SupervisorStoreError("supervisor_attempt_already_resolved")
        if event_type == "start_result":
            if str(payload.get("result") or "") not in _RESULTS:
                raise SupervisorStoreError(
                    "supervisor_start_result_invalid"
                )
            _optional_code(payload.get("machine_code"))
            _required_digest(payload.get("response_digest"))
            return
        if event_type == "recovery_result":
            if str(payload.get("resolution") or "") not in _RECOVERY_RESULTS:
                raise SupervisorStoreError(
                    "supervisor_recovery_result_invalid"
                )
            _optional_code(payload.get("machine_code"))
            _required_digest(payload.get("authority_digest"))

    def _append_event_line(
        self,
        cycle_id: str,
        event: Mapping[str, Any],
    ) -> None:
        path = self._events_path(cycle_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = _canonical(event) + "\n"
        try:
            descriptor = os.open(
                path,
                os.O_CREAT | os.O_APPEND | os.O_WRONLY,
                0o600,
            )
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            _fsync_directory(path.parent)
        except OSError as exc:
            raise SupervisorStoreError("attempt_store_corrupt") from exc

    def _write_state(
        self,
        cycle_id: str,
        state: Mapping[str, Any],
    ) -> None:
        _atomic_write_json(self._state_path(cycle_id), state)

    def _write_lease_projection(
        self,
        lease: SupervisorLease,
        *,
        status: str,
    ) -> None:
        payload = {
            "schema_version": LEASE_SCHEMA_VERSION,
            "status": status,
            "lease_id": lease.lease_id,
            "cycle_id": lease.cycle_id,
            "holder_id": lease.holder_id,
            "acquired_at": lease.acquired_at,
            "hostname": str(self.hostname() or ""),
            "pid": int(self.pid()),
            "updated_at": _utc(self.now()).isoformat(),
            "lock_path": str(self.lock_path),
            "lock_inode": lease.lock_inode,
            "lock_inode_is_stable": True,
        }
        _atomic_write_json(self.lease_path, payload)

    def _events_path(self, cycle_id: str) -> Path:
        return self.root / "events" / f"{cycle_id}.jsonl"

    def _state_path(self, cycle_id: str) -> Path:
        return self.root / "states" / f"{cycle_id}.json"


class SupervisorLease:
    """Capability proving the caller owns the process-level writer lock."""

    def __init__(
        self,
        *,
        store: PaperSupervisorStore,
        descriptor: int,
        lease_id: str,
        cycle_id: str,
        holder_id: str,
        acquired_at: str,
        lock_inode: int,
    ) -> None:
        self._store = store
        self._descriptor = descriptor
        self.lease_id = lease_id
        self.cycle_id = cycle_id
        self.holder_id = holder_id
        self.acquired_at = acquired_at
        self.lock_inode = lock_inode
        self._active = True

    def record_start_intent(
        self,
        *,
        attempt_id: str,
        preview_id: str,
        prepared_start_id: str,
        plan_identity: Mapping[str, Any],
        expected_order_fingerprints: Sequence[str],
    ) -> dict[str, Any]:
        fingerprints = _fingerprints(expected_order_fingerprints)
        return self._store._append(
            self,
            event_type="start_intent",
            attempt_id=attempt_id,
            payload={
                "preview_id": _identity(
                    preview_id,
                    "supervisor_preview_id_invalid",
                ),
                "prepared_start_id": _identity(
                    prepared_start_id,
                    "prepared_start_id_invalid",
                ),
                "plan_identity": _plan_identity(plan_identity),
                "expected_order_count": len(fingerprints),
                "expected_order_fingerprints": fingerprints,
            },
        )

    def record_start_result(
        self,
        *,
        attempt_id: str,
        result: str,
        machine_code: str | None = None,
        response_digest: str,
    ) -> dict[str, Any]:
        return self._store._append(
            self,
            event_type="start_result",
            attempt_id=attempt_id,
            payload={
                "result": str(result),
                "machine_code": (
                    str(machine_code) if machine_code is not None else None
                ),
                "response_digest": response_digest,
            },
        )

    def execute_start(
        self,
        *,
        attempt_id: str,
        preview_id: str,
        prepared_start_id: str,
        plan_identity: Mapping[str, Any],
        expected_order_fingerprints: Sequence[str],
        operation: Callable[[], Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        """Persist intent, invoke once, then persist a bounded result."""

        self.record_start_intent(
            attempt_id=attempt_id,
            preview_id=preview_id,
            prepared_start_id=prepared_start_id,
            plan_identity=plan_identity,
            expected_order_fingerprints=expected_order_fingerprints,
        )
        response = operation()
        if not isinstance(response, Mapping):
            raise SupervisorStoreError("supervisor_start_response_invalid")
        result = str(response.get("result") or "")
        machine_code = response.get("machine_code")
        response_digest = _digest(dict(response))
        self.record_start_result(
            attempt_id=attempt_id,
            result=result,
            machine_code=(
                str(machine_code) if machine_code is not None else None
            ),
            response_digest=response_digest,
        )
        return response

    def recover_unfinished_intent(
        self,
        authority: StartAuthoritySnapshot,
    ) -> dict[str, Any] | None:
        """Resolve one unfinished intent without calling start again."""

        self._require_active(self._store)
        intent = self._store.unfinished_intent(self.cycle_id)
        if intent is None:
            return None
        resolution = resolve_start_outcome(intent, authority)
        self._store._append(
            self,
            event_type="recovery_result",
            attempt_id=str(intent["attempt_id"]),
            payload=resolution,
        )
        return resolution

    def _require_active(self, store: PaperSupervisorStore) -> None:
        if (
            not self._active
            or store is not self._store
            or self._descriptor < 0
        ):
            raise SupervisorStoreError("supervisor_lease_invalid")


def resolve_start_outcome(
    intent: Mapping[str, Any],
    authority: StartAuthoritySnapshot,
) -> dict[str, Any]:
    """Return the only safe recovery interpretation of exact authority."""

    snapshot = _json_object(
        authority.as_dict(),
        "supervisor_authority_invalid",
    )
    _validate_authority(snapshot)
    if str(intent.get("cycle_id") or "") != snapshot["cycle_id"]:
        raise SupervisorStoreError("supervisor_authority_cycle_mismatch")
    payload = _json_object(
        intent.get("payload"),
        "supervisor_intent_payload_invalid",
    )
    plan = _plan_identity(payload.get("plan_identity"))
    active_plan = _plan_identity(snapshot.get("active_plan"))
    expected_fingerprints = _fingerprints(
        payload.get("expected_order_fingerprints") or []
    )
    runtime = dict(snapshot["runtime"])
    actual_fingerprints = _fingerprints(
        snapshot["accepted_order_fingerprints"],
        allow_empty=True,
    )
    reconciliation = dict(snapshot["reconciliation"])
    exact_reconciliation = (
        reconciliation.get("execution") == "ok"
        and reconciliation.get("accounting") == "pass"
    )
    runtime_plan_identity = (
        active_plan == plan
        and
        str(runtime.get("strategy_plan_id") or "")
        == plan["strategy_plan_id"]
        and int(runtime.get("strategy_plan_version") or 0)
        == plan["strategy_plan_version"]
    )
    runtime_identity = (
        runtime_plan_identity
        and str(runtime.get("preview_id") or "") == payload["preview_id"]
        and str(runtime.get("prepared_start_id") or "")
        == payload["prepared_start_id"]
    )
    matching_audit = _matching_control_events(
        snapshot["control_events"],
        cycle_id=snapshot["cycle_id"],
        preview_id=str(payload["preview_id"]),
        prepared_start_id=str(payload["prepared_start_id"]),
    )
    authority_digest = _digest(
        {
            "schema_version": snapshot["schema_version"],
            "cycle_id": snapshot["cycle_id"],
            "active_plan": active_plan,
            "runtime": runtime,
            "accepted_order_fingerprints": actual_fingerprints,
            "open_position_count": snapshot["open_position_count"],
            "reconciliation": reconciliation,
            "matching_control_events": matching_audit,
        }
    )
    if (
        exact_reconciliation
        and runtime_identity
        and runtime.get("actual_state") == "running"
        and runtime.get("desired_state") == "running"
        and actual_fingerprints == expected_fingerprints
        and len(actual_fingerprints)
        == int(payload["expected_order_count"])
        and len(matching_audit) == 1
        and matching_audit[0]["result"] == "accepted"
    ):
        return {
            "resolution": "executed",
            "machine_code": None,
            "authority_digest": authority_digest,
            "same_prepared_start_retry_allowed": False,
            "fresh_attempt_classification_required": False,
            "orders_created_by_recovery": 0,
        }
    clean_runtime = (
        runtime_plan_identity
        and runtime.get("actual_state") == "stopped"
        and runtime.get("desired_state") == "stopped"
        and str(runtime.get("preview_id") or "")
        in {"", str(payload["preview_id"])}
        and str(runtime.get("prepared_start_id") or "")
        in {"", str(payload["prepared_start_id"])}
    )
    if (
        exact_reconciliation
        and clean_runtime
        and actual_fingerprints == []
        and int(snapshot["open_position_count"]) == 0
        and len(matching_audit) == 1
        and matching_audit[0]["result"] == "rejected"
    ):
        return {
            "resolution": "clean_rejection",
            "machine_code": matching_audit[0]["machine_code"],
            "authority_digest": authority_digest,
            "same_prepared_start_retry_allowed": False,
            "fresh_attempt_classification_required": True,
            "orders_created_by_recovery": 0,
        }
    return {
        "resolution": "control_outcome_unknown",
        "machine_code": "control_outcome_unknown",
        "authority_digest": authority_digest,
        "same_prepared_start_retry_allowed": False,
        "fresh_attempt_classification_required": False,
        "orders_created_by_recovery": 0,
    }


def _matching_control_events(
    rows: Sequence[Mapping[str, Any]],
    *,
    cycle_id: str,
    preview_id: str,
    prepared_start_id: str,
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        request = raw.get("request")
        if not isinstance(request, Mapping):
            continue
        if (
            raw.get("schema_version") != "strategy-control-event-v1"
            or str(raw.get("cycle_id") or "") != cycle_id
            or str(raw.get("action") or "") != "start"
            or str(request.get("expected_preview_id") or "") != preview_id
            or str(request.get("prepared_start_id") or "")
            != prepared_start_id
        ):
            continue
        result = str(raw.get("result") or "")
        if result not in {"accepted", "rejected"}:
            continue
        code = raw.get("error")
        matches.append(
            {
                "result": result,
                "machine_code": _machine_code_or_unknown(code),
            }
        )
    return matches


def _project(
    events: Sequence[Mapping[str, Any]],
    *,
    cycle_id: str,
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    spent: list[str] = []
    for event in events:
        event_type = str(event.get("event_type") or "")
        attempt_id = str(event.get("attempt_id") or "")
        payload = dict(event.get("payload") or {})
        if event_type == "start_intent":
            if attempt_id in by_id:
                raise SupervisorStoreError("attempt_store_corrupt")
            prepared_id = str(payload.get("prepared_start_id") or "")
            if prepared_id in spent:
                raise SupervisorStoreError("attempt_store_corrupt")
            row = {
                "attempt_id": attempt_id,
                "intent_sequence": event["sequence"],
                "intent_recorded_at": event["recorded_at"],
                "preview_id": payload.get("preview_id"),
                "prepared_start_id": prepared_id,
                "plan_identity": payload.get("plan_identity"),
                "expected_order_count": payload.get(
                    "expected_order_count"
                ),
                "expected_order_fingerprints": payload.get(
                    "expected_order_fingerprints"
                ),
                "terminal_result": None,
                "terminal_sequence": None,
            }
            attempts.append(row)
            by_id[attempt_id] = row
            spent.append(prepared_id)
            continue
        row = by_id.get(attempt_id)
        if row is None or row.get("terminal_result") is not None:
            raise SupervisorStoreError("attempt_store_corrupt")
        row["terminal_result"] = (
            payload.get("result")
            if event_type == "start_result"
            else payload.get("resolution")
        )
        row["terminal_machine_code"] = payload.get("machine_code")
        row["terminal_sequence"] = event["sequence"]
        row["terminal_recorded_at"] = event["recorded_at"]
    unfinished = next(
        (
            {
                "cycle_id": cycle_id,
                "attempt_id": row["attempt_id"],
                "payload": {
                    "preview_id": row["preview_id"],
                    "prepared_start_id": row["prepared_start_id"],
                    "plan_identity": row["plan_identity"],
                    "expected_order_count": row["expected_order_count"],
                    "expected_order_fingerprints": row[
                        "expected_order_fingerprints"
                    ],
                },
            }
            for row in attempts
            if row["terminal_result"] is None
        ),
        None,
    )
    if sum(row["terminal_result"] is None for row in attempts) > 1:
        raise SupervisorStoreError("attempt_store_corrupt")
    last_hash = str(events[-1]["event_sha256"]) if events else None
    state = {
        "schema_version": STATE_SCHEMA_VERSION,
        "cycle_id": cycle_id,
        "last_sequence": len(events),
        "last_event_sha256": last_hash,
        "attempt_count": len(attempts),
        "attempts": attempts,
        "spent_prepared_start_ids": spent,
        "unfinished_intent": unfinished,
        "status": "intent_pending" if unfinished else "idle",
    }
    state["state_digest"] = _digest(state)
    return state


def _validate_event(
    event: Any,
    *,
    cycle_id: str,
    expected_sequence: int,
    previous_hash: str | None,
) -> None:
    if not isinstance(event, dict):
        raise SupervisorStoreError("attempt_store_corrupt")
    if (
        event.get("schema_version") != EVENT_SCHEMA_VERSION
        or event.get("sequence") != expected_sequence
        or event.get("event_type") not in _EVENT_TYPES
        or str(event.get("cycle_id") or "") != cycle_id
        or event.get("previous_event_sha256") != previous_hash
        or not str(event.get("event_id") or "").startswith(
            "supervisor-event-"
        )
        or not str(event.get("lease_id") or "").startswith(
            "supervisor-lease-"
        )
    ):
        raise SupervisorStoreError("attempt_store_corrupt")
    _identity(event.get("attempt_id"), "attempt_store_corrupt")
    _parse_timestamp(event.get("recorded_at"))
    payload = _json_object(event.get("payload"), "attempt_store_corrupt")
    _validate_event_payload(
        str(event["event_type"]),
        payload,
        code="attempt_store_corrupt",
    )
    supplied_hash = str(event.get("event_sha256") or "")
    if supplied_hash != _digest(
        {key: value for key, value in event.items() if key != "event_sha256"}
    ):
        raise SupervisorStoreError("attempt_store_corrupt")


def _validate_stored_state(
    stored: Any,
    *,
    events: Sequence[Mapping[str, Any]],
    cycle_id: str,
) -> None:
    if not isinstance(stored, dict):
        raise SupervisorStoreError("attempt_store_corrupt")
    supplied_digest = str(stored.get("state_digest") or "")
    if supplied_digest != _digest(
        {key: value for key, value in stored.items() if key != "state_digest"}
    ):
        raise SupervisorStoreError("attempt_store_corrupt")
    stored_sequence = stored.get("last_sequence")
    if not isinstance(stored_sequence, int):
        raise SupervisorStoreError("attempt_store_corrupt")
    if stored_sequence < 0 or stored_sequence > len(events):
        raise SupervisorStoreError("attempt_store_corrupt")
    expected = _project(
        events[:stored_sequence],
        cycle_id=cycle_id,
    )
    if stored != expected:
        raise SupervisorStoreError("attempt_store_corrupt")


def _validate_event_payload(
    event_type: str,
    payload: Mapping[str, Any],
    *,
    code: str,
) -> None:
    try:
        if event_type == "start_intent":
            _validate_intent_payload(payload)
            return
        if event_type == "start_result":
            if str(payload.get("result") or "") not in _RESULTS:
                raise SupervisorStoreError(code)
            _optional_code(payload.get("machine_code"))
            _required_digest(payload.get("response_digest"), code=code)
            return
        if event_type == "recovery_result":
            if (
                str(payload.get("resolution") or "")
                not in _RECOVERY_RESULTS
            ):
                raise SupervisorStoreError(code)
            _optional_code(payload.get("machine_code"))
            _required_digest(payload.get("authority_digest"), code=code)
            return
    except SupervisorStoreError as exc:
        raise SupervisorStoreError(code) from exc
    raise SupervisorStoreError(code)


def _validate_intent_payload(payload: Mapping[str, Any]) -> None:
    _identity(payload.get("preview_id"), "supervisor_preview_id_invalid")
    _identity(payload.get("prepared_start_id"), "prepared_start_id_invalid")
    _plan_identity(payload.get("plan_identity"))
    fingerprints = _fingerprints(
        payload.get("expected_order_fingerprints") or []
    )
    if payload.get("expected_order_count") != len(fingerprints):
        raise SupervisorStoreError(
            "supervisor_expected_order_count_invalid"
        )


def _validate_authority(snapshot: Mapping[str, Any]) -> None:
    if (
        snapshot.get("schema_version") != AUTHORITY_SCHEMA_VERSION
        or not isinstance(snapshot.get("active_plan"), dict)
        or not isinstance(snapshot.get("runtime"), dict)
        or not isinstance(snapshot.get("reconciliation"), dict)
        or not isinstance(snapshot.get("control_events"), list)
    ):
        raise SupervisorStoreError("supervisor_authority_invalid")
    _cycle_id(snapshot.get("cycle_id"))
    _fingerprints(
        snapshot.get("accepted_order_fingerprints") or [],
        allow_empty=True,
    )
    count = snapshot.get("open_position_count")
    if not isinstance(count, int) or count < 0:
        raise SupervisorStoreError("supervisor_authority_invalid")


def _plan_identity(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SupervisorStoreError("supervisor_plan_identity_invalid")
    plan_id = _identity(
        value.get("strategy_plan_id"),
        "supervisor_plan_identity_invalid",
    )
    version = value.get("strategy_plan_version")
    strategy_type = str(value.get("strategy_type") or "")
    direction = str(value.get("direction") or "")
    if (
        not isinstance(version, int)
        or version < 1
        or strategy_type not in {"grid", "dca"}
        or direction not in {"neutral", "long", "short"}
    ):
        raise SupervisorStoreError("supervisor_plan_identity_invalid")
    return {
        "strategy_plan_id": plan_id,
        "strategy_plan_version": version,
        "strategy_type": strategy_type,
        "direction": direction,
    }


def _fingerprints(
    values: Sequence[Any],
    *,
    allow_empty: bool = False,
) -> list[str]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise SupervisorStoreError(
            "supervisor_order_fingerprints_invalid"
        )
    result = [
        _required_digest(
            value,
            code="supervisor_order_fingerprints_invalid",
        )
        for value in values
    ]
    if (not result and not allow_empty) or len(result) != len(set(result)):
        raise SupervisorStoreError(
            "supervisor_order_fingerprints_invalid"
        )
    return sorted(result)


def _cycle_id(value: Any) -> str:
    cycle = str(value or "")
    suffix = next(
        (candidate for candidate in _CYCLE_SUFFIXES if cycle.endswith(candidate)),
        "",
    )
    if not suffix:
        raise SupervisorStoreError("supervisor_cycle_id_invalid")
    date_part = cycle[: -len(suffix)]
    try:
        parsed_date = datetime.fromisoformat(date_part)
    except ValueError as exc:
        raise SupervisorStoreError("supervisor_cycle_id_invalid") from exc
    if (
        len(date_part) != 10
        or parsed_date.time().isoformat() != "00:00:00"
        or cycle != f"{parsed_date.date().isoformat()}{suffix}"
    ):
        raise SupervisorStoreError("supervisor_cycle_id_invalid")
    return cycle


def _identity(value: Any, code: str) -> str:
    text = str(value or "").strip()
    if (
        not text
        or len(text) > 256
        or any(character not in _IDENTITY_CHARS for character in text)
    ):
        raise SupervisorStoreError(code)
    return text


_IDENTITY_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    "._:-"
)


def _optional_code(value: Any) -> str | None:
    if value is None:
        return None
    return _identity(value, "supervisor_machine_code_invalid")


def _machine_code_or_unknown(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return _identity(value, "supervisor_machine_code_invalid")
    except SupervisorStoreError:
        return "unknown_blocker"


def _required_digest(
    value: Any,
    *,
    code: str = "supervisor_digest_invalid",
) -> str:
    text = str(value or "").lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise SupervisorStoreError(code)
    return text


def _json_object(value: Any, code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SupervisorStoreError(code)
    try:
        copied = json.loads(_canonical(dict(value)))
    except (TypeError, ValueError) as exc:
        raise SupervisorStoreError(code) from exc
    if not isinstance(copied, dict):
        raise SupervisorStoreError(code)
    return copied


def _parse_timestamp(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise SupervisorStoreError("attempt_store_corrupt") from exc
    if parsed.tzinfo is None:
        raise SupervisorStoreError("attempt_store_corrupt")
    return parsed.astimezone(timezone.utc)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise SupervisorStoreError("supervisor_timestamp_invalid")
    return value.astimezone(timezone.utc)


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


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _canonical(dict(value)) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise SupervisorStoreError("attempt_store_corrupt") from exc


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
