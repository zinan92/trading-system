"""Local-owner Testnet scheduler boundary for the Automation Coordinator."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from pathlib import Path
from typing import Any, Callable, Mapping

from services.journal_store import load_json, write_json
from services.scheduler_ownership import SchedulerOwnershipStore
from services.testnet_automation_coordinator import TestnetAutomationCoordinator


TESTNET_SCHEDULER_SCHEMA = "testnet-scheduler-ownership-v1"
_TERMINAL_COORDINATOR_STATES = frozenset({"dca_terminal", "grid_terminal"})
_SAMPLING_RACE_REASONS = frozenset({"market_price_mismatch", "market_bbo_inconsistent"})


def _redacted_exception_message(exc: BaseException) -> str:
    """Return a bounded message safe for the durable scheduler receipt."""
    message = str(exc).replace("\n", " ").replace("\r", " ").strip()
    message = re.sub(
        r"(?i)(secret|private[_-]?key|api[_-]?key|token|password)([=:])[^,; ]+",
        r"\1\2[REDACTED]",
        message,
    )
    return message[:240] or "no_message"


def _redacted_exception_evidence(value: Any, *, depth: int = 0) -> Any:
    """Copy typed exception evidence without persisting credential-shaped data."""
    if depth > 5:
        return "[TRUNCATED]"
    if isinstance(value, Mapping):
        redacted = {}
        for key, item in value.items():
            name = str(key)
            if re.search(r"(?i)(secret|private[_-]?key|api[_-]?key|token|password)", name):
                redacted[name] = "[REDACTED]"
            else:
                redacted[name] = _redacted_exception_evidence(item, depth=depth + 1)
        return redacted
    if isinstance(value, (list, tuple)):
        return [_redacted_exception_evidence(item, depth=depth + 1) for item in value]
    if isinstance(value, str):
        return _redacted_exception_message(ValueError(value))
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redacted_exception_message(ValueError(str(value)))


def _exception_advance_result(exc: BaseException) -> dict[str, Any]:
    """Project typed control errors into the scheduler's durable result shape."""
    evidence = getattr(exc, "evidence", None)
    code = getattr(exc, "code", None)
    if not isinstance(evidence, Mapping) or not code:
        return {}
    return {
        "error": {
            "type": type(exc).__name__,
            "code": str(code),
            "evidence": _redacted_exception_evidence(evidence),
        }
    }


class TestnetSchedulerOwnershipStore(SchedulerOwnershipStore):
    """Reuse the file-lock/epoch implementation in a Testnet-only namespace."""

    __test__ = False

    def __init__(
        self,
        output_root: Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(output_root, now=clock, scope="testnet_only")

    def initialize_cloud(self, *, owner_id: str) -> dict[str, Any]:
        del owner_id
        raise ValueError("testnet_scheduler_local_only")

    def initialize_local(self, *, owner_id: str = "local-mac") -> dict[str, Any]:
        with self._lock():
            if self.current():
                raise ValueError("testnet_scheduler_ownership_already_initialized")
            return self._write(
                status="active",
                active_owner_id=self._owner(owner_id),
                previous_owner_id=None,
                epoch=1,
                action="initialize_local",
            )


class TestnetSchedulerGuard:
    """Fail-closed guard that permits only the configured local owner."""

    __test__ = False

    def __init__(
        self,
        output_root: Path,
        *,
        runtime_mode: str = "local",
        owner_id: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.runtime_mode = str(runtime_mode or "").strip().lower()
        self.owner_id = str(owner_id or "").strip()
        self.clock = clock
        self.store = TestnetSchedulerOwnershipStore(self.output_root, clock=clock)

    def verify(self) -> dict[str, Any]:
        checked_at = _timestamp(self.clock)
        current = self.store.current()
        if self.runtime_mode != "local":
            return self._blocked(
                "testnet_scheduler_local_only",
                current,
                checked_at,
                self.runtime_mode,
            )
        if not current:
            return self._blocked(
                "testnet_scheduler_ownership_missing",
                current,
                checked_at,
                self.runtime_mode,
            )
        if current.get("scope") != "testnet_only":
            return self._blocked(
                "testnet_scheduler_scope_mismatch",
                current,
                checked_at,
                self.runtime_mode,
            )
        if current.get("status") != "active":
            return self._blocked(
                "testnet_scheduler_ownership_paused",
                current,
                checked_at,
                self.runtime_mode,
            )
        if not self.owner_id:
            return self._blocked(
                "testnet_scheduler_owner_missing",
                current,
                checked_at,
                self.runtime_mode,
            )
        if current.get("active_owner_id") != self.owner_id:
            return self._blocked(
                "testnet_scheduler_owner_mismatch",
                current,
                checked_at,
                self.runtime_mode,
            )
        return {
            "schema_version": TESTNET_SCHEDULER_SCHEMA,
            "checked_at": checked_at,
            "runtime_mode": self.runtime_mode,
            "scope": "testnet_only",
            "ok": True,
            "status": "pass",
            "owner_id": self.owner_id,
            "epoch": current.get("epoch"),
            "active_owner_id": current.get("active_owner_id"),
            "dual_owner_allowed": False,
        }

    @staticmethod
    def _blocked(
        blocker: str,
        current: Mapping[str, Any],
        checked_at: str,
        runtime_mode: str,
    ) -> dict[str, Any]:
        return {
            "schema_version": TESTNET_SCHEDULER_SCHEMA,
            "checked_at": checked_at,
            "runtime_mode": runtime_mode,
            "scope": "testnet_only",
            "ok": False,
            "status": "blocked",
            "blocker": blocker,
            "owner_id": None,
            "epoch": current.get("epoch"),
            "active_owner_id": current.get("active_owner_id"),
            "dual_owner_allowed": False,
            "next_action": "notify_park_and_wait",
        }


class TestnetScheduler:
    """Schedule one approved Coordinator session without hidden side effects."""

    __test__ = False

    def __init__(
        self,
        output_root: Path,
        coordinator: TestnetAutomationCoordinator,
        *,
        owner_id: str,
        runtime_mode: str = "local",
        clock: Callable[[], str | datetime] | None = None,
        advance_failure_threshold: int = 3,
    ) -> None:
        self.output_root = Path(output_root)
        self.coordinator = coordinator
        self.owner_id = str(owner_id or "").strip()
        self.runtime_mode = str(runtime_mode or "").strip().lower()
        self.clock = clock
        self.advance_failure_threshold = max(1, int(advance_failure_threshold))
        root = self.output_root / "testnet_automation" / "scheduler"
        self.current_path = root / "current.json"
        self.ticks_path = root / "ticks.json"
        self.guard = TestnetSchedulerGuard(
            self.output_root,
            runtime_mode=self.runtime_mode,
            owner_id=self.owner_id,
            clock=self._clock_datetime,
        )

    def status(self) -> dict[str, Any]:
        try:
            rows = load_json(self.current_path)
            return dict(rows[-1]) if rows and isinstance(rows[-1], Mapping) else self._idle()
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return {
                "schema_version": TESTNET_SCHEDULER_SCHEMA,
                "status": "blocked",
                "blocker": "testnet_scheduler_state_corrupt",
                "next_action": "notify_park_and_wait",
            }

    def activate(
        self,
        activation: Mapping[str, Any],
        *,
        command_id: str | None = None,
        timestamp: str | datetime | None = None,
    ) -> dict[str, Any]:
        guard = self.guard.verify()
        if not guard.get("ok"):
            return self._blocked(guard.get("blocker", "ownership_blocked"), timestamp)
        current = self.status()
        if current.get("status") == "active":
            existing_id = str(current.get("activation_id") or "")
            requested_id = str(activation.get("activation_id") or "")
            if requested_id and requested_id != existing_id:
                return self._blocked("testnet_scheduler_active_session_conflict", timestamp)
            return dict(current)
        try:
            coordinator_state = self.coordinator.activate(
                activation,
                command_id=command_id,
                now=timestamp,
            )
        except Exception as exc:  # noqa: BLE001 - scheduler must persist a safe blocker.
            return self._blocked(
                f"coordinator_activation_blocked:{type(exc).__name__}",
                timestamp,
            )
        now = self._timestamp(timestamp)
        state = {
            "schema_version": TESTNET_SCHEDULER_SCHEMA,
            "event": "scheduler_activated",
            "status": "active",
            "occurred_at": now,
            "owner_id": self.owner_id,
            "owner_epoch": guard.get("epoch"),
            "activation_id": coordinator_state.get("activation_id"),
            "execution_enabled": coordinator_state.get("execution_enabled") is True,
            "coordinator": coordinator_state,
            "restart_reconcile_required": False,
            "next_action": coordinator_state.get("next_action"),
            "blocker": None,
            "alerts_authorize_actions": False,
        }
        return self._save_state(state)

    def attach(
        self,
        activation_id: str,
        *,
        timestamp: str | datetime | None = None,
    ) -> dict[str, Any]:
        """Attach the local scheduler to an already-running Coordinator session."""
        guard = self.guard.verify()
        if not guard.get("ok"):
            return self._blocked(guard.get("blocker", "ownership_blocked"), timestamp)
        requested = str(activation_id or "").strip()
        if not requested:
            return self._blocked("testnet_scheduler_activation_id_required", timestamp)
        coordinator = self.coordinator.status()
        if str(coordinator.get("activation_id") or "") != requested:
            return self._blocked("testnet_scheduler_activation_not_found", timestamp)
        coordinator_state = str(coordinator.get("status") or "")
        if coordinator_state not in {"grid_running", "grid_paused_range", "dca_running", "grid_blocked", "dca_blocked"}:
            return self._blocked("testnet_scheduler_attach_requires_running_coordinator", timestamp)
        current = self.status()
        existing_id = str(current.get("activation_id") or "")
        if current.get("status") == "active" and existing_id not in {"", requested}:
            return self._blocked("testnet_scheduler_active_session_conflict", timestamp)
        return self._save_state({
            "schema_version": TESTNET_SCHEDULER_SCHEMA,
            "event": "scheduler_attached",
            "status": "active",
            "occurred_at": self._timestamp(timestamp),
            "owner_id": self.owner_id,
            "owner_epoch": guard.get("epoch"),
            "activation_id": requested,
            "execution_enabled": coordinator.get("execution_enabled") is True,
            "coordinator": coordinator,
            "restart_reconcile_required": False,
            "next_action": "await_event_or_heartbeat",
            "blocker": None,
            "alerts_authorize_actions": False,
        })

    def resume(
        self,
        activation_id: str,
        *,
        timestamp: str | datetime | None = None,
    ) -> dict[str, Any]:
        """Explicitly re-attach a running Coordinator after a scheduler block."""
        resumed = self.attach(activation_id, timestamp=timestamp)
        if resumed.get("status") != "active":
            return resumed
        resumed["event"] = "scheduler_resumed"
        return self._save_state(resumed)

    def mark_restart(self, *, timestamp: str | datetime | None = None) -> dict[str, Any]:
        guard = self.guard.verify()
        if not guard.get("ok"):
            return self._blocked(guard.get("blocker", "ownership_blocked"), timestamp)
        current = self.status()
        if current.get("status") != "active":
            return self._blocked("testnet_scheduler_not_active", timestamp)
        state = {
            **current,
            "event": "scheduler_restart_detected",
            "status": "reconcile_required",
            "occurred_at": self._timestamp(timestamp),
            "restart_reconcile_required": True,
            "next_action": "reconcile_before_resume",
        }
        return self._save_state(state)

    def tick(
        self,
        *,
        tick_id: str,
        event: Mapping[str, Any] | None = None,
        advance: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
        reconcile: Callable[[], Mapping[str, Any]] | None = None,
        timestamp: str | datetime | None = None,
    ) -> dict[str, Any]:
        tick_key = str(tick_id or "").strip()
        if not tick_key:
            return self._blocked("testnet_scheduler_tick_id_required", timestamp)
        try:
            existing_tick = next(
                (row for row in reversed(load_json(self.ticks_path)) if row.get("tick_id") == tick_key),
                None,
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return self._blocked("testnet_scheduler_state_corrupt", timestamp)
        if existing_tick is not None:
            return dict(existing_tick)
        guard = self.guard.verify()
        if not guard.get("ok"):
            return self._record_tick(
                tick_key,
                self._blocked(guard.get("blocker", "ownership_blocked"), timestamp),
            )
        current = self.status()
        coordinator_status = self.coordinator.status()
        if current.get("status") == "awaiting_operator" and coordinator_status.get("status") == "idle":
            return self._save_state({
                **current,
                "event": "scheduler_idle",
                "status": "idle",
                "occurred_at": self._timestamp(timestamp),
                "activation_id": None,
                "coordinator_status": "idle",
                "coordinator": coordinator_status,
                "execution_enabled": False,
                "next_action": "await_activation",
                "blocker": None,
            })
        if current.get("status") not in {"active", "reconcile_required"}:
            return self._record_tick(
                tick_key,
                self._blocked("testnet_scheduler_not_active", timestamp),
            )
        now = self._timestamp(timestamp)
        restart_reconciled = not bool(current.get("restart_reconcile_required"))
        if current.get("restart_reconcile_required"):
            if not callable(reconcile):
                return self._record_tick(
                    tick_key,
                    self._blocked("restart_reconciliation_required", timestamp),
                )
            try:
                evidence = reconcile()
            except Exception:  # noqa: BLE001 - no resume after unknown account state.
                return self._record_tick(
                    tick_key,
                    self._blocked("restart_reconciliation_failed", timestamp),
                )
            if not self._valid_reconcile(evidence, current):
                return self._record_tick(
                    tick_key,
                    self._blocked("restart_reconciliation_failed", timestamp),
                )
            restart_reconciled = True
        coordinator_state = str(coordinator_status.get("status") or "")
        if coordinator_state in _TERMINAL_COORDINATOR_STATES:
            result = {
                **current,
                "event": "scheduler_terminal_wait",
                "status": "awaiting_operator",
                "occurred_at": now,
                "tick_id": tick_key,
                "coordinator_status": coordinator_state,
                "coordinator": coordinator_status,
                "restart_reconciled": restart_reconciled,
                "restart_reconcile_required": False,
                "execution_enabled": False,
                "next_action": "notify_park_and_wait",
                "blocker": None,
                "warning": None,
                "alerts_authorize_actions": False,
            }
            return self._record_tick(tick_key, result)
        if coordinator_status.get("global_hold") or coordinator_state in {
            "portfolio_held",
            "candidate_blocked",
            "canary_blocked",
        }:
            result = {
                **current,
                "event": "scheduler_blocked",
                "status": "blocked",
                "occurred_at": now,
                "tick_id": tick_key,
                "coordinator_status": coordinator_state,
                "coordinator": coordinator_status,
                "restart_reconciled": restart_reconciled,
                "restart_reconcile_required": False,
                "execution_enabled": False,
                "next_action": "notify_park_and_wait",
                "blocker": coordinator_status.get("blocker") or "coordinator_blocked",
                "warning": None,
                "alerts_authorize_actions": False,
            }
            return self._record_tick(tick_key, result)
        advanced = None
        if event is not None and callable(advance):
            try:
                advanced = dict(advance(dict(event)))
            except Exception as exc:  # noqa: BLE001 - preserve safe retry policy.
                advance_result = _exception_advance_result(exc)
                return self._record_advance_failure(
                    current, tick_key, now, coordinator_state, restart_reconciled,
                    f"scheduler_advance_failed:{type(exc).__name__}:{_redacted_exception_message(exc)}",
                    advance_result,
                )
            if str(advanced.get("status") or "").lower() in {"blocked", "unknown", "fail", "failed"}:
                if self._is_sampling_race_failure(advanced):
                    return self._record_sampling_race_warning(
                        current, tick_key, now, coordinator_state,
                        restart_reconciled, advanced,
                    )
                return self._record_advance_failure(
                    current, tick_key, now, coordinator_state, restart_reconciled,
                    str(advanced.get("reason") or "scheduler_advance_blocked"), advanced,
                )
        updated_coordinator = self.coordinator.status()
        updated_state = str(updated_coordinator.get("status") or "")
        became_terminal = updated_state in _TERMINAL_COORDINATOR_STATES
        result = {
            **current,
            "event": "scheduler_terminal_wait" if became_terminal else "scheduler_tick",
            "status": "awaiting_operator" if became_terminal else "active",
            "occurred_at": now,
            "tick_id": tick_key,
            "coordinator_status": updated_state,
            "coordinator": updated_coordinator,
            "restart_reconciled": restart_reconciled,
            "restart_reconcile_required": False,
            "execution_enabled": False if became_terminal else updated_coordinator.get("execution_enabled") is True,
            "next_action": "notify_park_and_wait" if became_terminal else "await_event_or_heartbeat",
            "blocker": None,
            "warning": None,
            "advance_result": advanced,
            "alerts_authorize_actions": False,
            "heartbeat": {"status": "fresh", "observed_at": now, "tick_id": tick_key},
            "advance_failure_count": 0,
        }
        return self._record_tick(tick_key, result)

    def _record_advance_failure(
        self, current: Mapping[str, Any], tick_id: str, now: str,
        coordinator_state: str, restart_reconciled: bool, reason: str,
        advance_result: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        count = int(current.get("advance_failure_count") or 0) + 1
        blocked = count >= self.advance_failure_threshold
        result = {
            **current,
            "event": "scheduler_advance_blocked" if blocked else "scheduler_advance_warning",
            "status": "blocked" if blocked else "active",
            "occurred_at": now,
            "tick_id": tick_id,
            "coordinator_status": coordinator_state,
            "restart_reconciled": restart_reconciled,
            "restart_reconcile_required": False,
            "execution_enabled": False if blocked else current.get("execution_enabled", True),
            "next_action": "notify_park_and_wait" if blocked else "await_event_or_heartbeat",
            "blocker": reason if blocked else None,
            "warning": reason if not blocked else None,
            "advance_failure_count": count,
            "advance_failure_threshold": self.advance_failure_threshold,
            "advance_result": dict(advance_result or {}),
            "alerts_authorize_actions": False,
        }
        return self._record_tick(tick_id, result)

    @staticmethod
    def _is_sampling_race_failure(advance_result: Mapping[str, Any]) -> bool:
        failure = advance_result.get("market_failure")
        return (
            isinstance(failure, Mapping)
            and str(failure.get("reason") or "") in _SAMPLING_RACE_REASONS
        )

    def _record_sampling_race_warning(
        self, current: Mapping[str, Any], tick_id: str, now: str,
        coordinator_state: str, restart_reconciled: bool,
        advance_result: Mapping[str, Any],
    ) -> dict[str, Any]:
        reason = str(advance_result.get("reason") or "scheduler_sampling_race")
        result = {
            **current,
            "event": "scheduler_advance_warning",
            "status": "active",
            "occurred_at": now,
            "tick_id": tick_id,
            "coordinator_status": coordinator_state,
            "restart_reconciled": restart_reconciled,
            "restart_reconcile_required": False,
            "execution_enabled": current.get("execution_enabled", True),
            "next_action": "await_event_or_heartbeat",
            "blocker": None,
            "warning": reason,
            "advance_failure_count": int(current.get("advance_failure_count") or 0),
            "advance_failure_threshold": self.advance_failure_threshold,
            "advance_result": dict(advance_result),
            "alerts_authorize_actions": False,
        }
        return self._record_tick(tick_id, result)

    def dead_man(
        self,
        *,
        timestamp: str | datetime | None = None,
        timeout_seconds: int = 300,
    ) -> dict[str, Any]:
        """Persist a stale-heartbeat result without issuing a control action."""
        now = self._clock_datetime() if timestamp is None else (
            datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
            if isinstance(timestamp, str) else timestamp
        )
        if now.tzinfo is None:
            raise ValueError("scheduler timestamp timezone missing")
        rows = load_json(self.ticks_path)
        latest = next((row for row in reversed(rows) if isinstance(row, Mapping)), None)
        observed = latest.get("occurred_at") if isinstance(latest, Mapping) else None
        age = None
        if observed:
            age = max(
                0.0,
                (
                    now.astimezone(timezone.utc)
                    - datetime.fromisoformat(str(observed).replace("Z", "+00:00")).astimezone(timezone.utc)
                ).total_seconds(),
            )
        stale = age is None or age > int(timeout_seconds)
        current = self.status()
        return self._save_state(
            {
                **current,
                "event": "scheduler_dead_man",
                "occurred_at": now.astimezone(timezone.utc).replace(microsecond=0).isoformat(),
                "dead_man": {
                    "status": "execution_tick_scheduler_down" if stale else "fresh",
                    "age_seconds": age,
                    "timeout_seconds": int(timeout_seconds),
                },
                "next_action": "notify_park_and_wait" if stale else current.get("next_action"),
            }
        )

    def _record_tick(self, tick_id: str, value: Mapping[str, Any]) -> dict[str, Any]:
        row = {**dict(value), "tick_id": tick_id}
        ticks = load_json(self.ticks_path)
        ticks.append(row)
        write_json(self.ticks_path, ticks)
        return self._save_state(row)

    def _save_state(self, value: Mapping[str, Any]) -> dict[str, Any]:
        write_json(self.current_path, [dict(value)])
        return dict(value)

    def _blocked(self, blocker: str, timestamp: str | datetime | None) -> dict[str, Any]:
        return self._save_state(
            {
                "schema_version": TESTNET_SCHEDULER_SCHEMA,
                "event": "scheduler_blocked",
                "status": "blocked",
                "occurred_at": self._timestamp(timestamp),
                "owner_id": self.owner_id,
                "execution_enabled": False,
                "restart_reconcile_required": False,
                "next_action": "notify_park_and_wait",
                "blocker": str(blocker),
                "alerts_authorize_actions": False,
            }
        )

    def _clock_datetime(self) -> datetime:
        value = self.clock() if self.clock is not None else datetime.now(timezone.utc)
        if isinstance(value, datetime):
            parsed = value
        else:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _timestamp(self, value: str | datetime | None) -> str:
        if value is None:
            return self._clock_datetime().replace(microsecond=0).isoformat()
        if isinstance(value, datetime):
            parsed = value
        else:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("scheduler timestamp timezone missing")
        return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()

    @staticmethod
    def _valid_reconcile(value: object, current: Mapping[str, Any]) -> bool:
        if not isinstance(value, Mapping) or str(value.get("status") or "").lower() not in {"pass", "ok"}:
            return False
        for key in ("activation_id", "environment", "account_fingerprint", "release_sha"):
            expected = current.get(key)
            if expected and str(value.get(key) or "") != str(expected):
                return False
        return str(value.get("environment") or "").lower() == "testnet"

    @staticmethod
    def _idle() -> dict[str, Any]:
        return {
            "schema_version": TESTNET_SCHEDULER_SCHEMA,
            "status": "idle",
            "execution_enabled": False,
            "restart_reconcile_required": False,
            "next_action": "await_activation",
            "alerts_authorize_actions": False,
        }


def _timestamp(clock: Callable[[], datetime] | None) -> str:
    value = clock() if clock is not None else datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


__all__ = [
    "TESTNET_SCHEDULER_SCHEMA",
    "TestnetScheduler",
    "TestnetSchedulerGuard",
    "TestnetSchedulerOwnershipStore",
]
