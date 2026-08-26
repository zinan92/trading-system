"""Cloud-owned Testnet scheduler boundary for the Automation Coordinator."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from services.journal_store import load_json, write_json
from services.scheduler_ownership import SchedulerOwnershipStore
from services.testnet_automation_coordinator import TestnetAutomationCoordinator


TESTNET_SCHEDULER_SCHEMA = "testnet-scheduler-ownership-v1"
_TERMINAL_COORDINATOR_STATES = frozenset({"dca_terminal", "grid_terminal"})


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
        with self._lock():
            if self.current():
                raise ValueError("testnet_scheduler_ownership_already_initialized")
            return self._write(
                status="active",
                active_owner_id=self._owner(owner_id),
                previous_owner_id=None,
                epoch=1,
                action="initialize_cloud",
            )

    def initialize_local(self, *, owner_id: str = "local-mac") -> dict[str, Any]:
        del owner_id
        raise ValueError("testnet_scheduler_cloud_only")


class TestnetSchedulerGuard:
    """Fail-closed guard that permits only the configured Cloud owner."""

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
        if self.runtime_mode != "cloud":
            return self._blocked(
                "testnet_scheduler_cloud_only",
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
    ) -> None:
        self.output_root = Path(output_root)
        self.coordinator = coordinator
        self.owner_id = str(owner_id or "").strip()
        self.runtime_mode = str(runtime_mode or "").strip().lower()
        self.clock = clock
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
        coordinator_status = self.coordinator.status()
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
                "alerts_authorize_actions": False,
            }
            return self._record_tick(tick_key, result)
        advanced = None
        if event is not None and callable(advance):
            try:
                advanced = dict(advance(dict(event)))
            except Exception:  # noqa: BLE001 - a failed tick never retries itself.
                result = {
                    **current,
                    "event": "scheduler_advance_blocked",
                    "status": "blocked",
                    "occurred_at": now,
                    "tick_id": tick_key,
                    "coordinator_status": coordinator_state,
                    "restart_reconciled": restart_reconciled,
                    "restart_reconcile_required": False,
                    "execution_enabled": False,
                    "next_action": "notify_park_and_wait",
                    "blocker": "scheduler_advance_failed",
                    "alerts_authorize_actions": False,
                }
                return self._record_tick(tick_key, result)
        result = {
            **current,
            "event": "scheduler_tick",
            "status": "active",
            "occurred_at": now,
            "tick_id": tick_key,
            "coordinator_status": coordinator_state,
            "coordinator": self.coordinator.status(),
            "restart_reconciled": restart_reconciled,
            "restart_reconcile_required": False,
            "execution_enabled": self.coordinator.status().get("execution_enabled") is True,
            "next_action": "await_event_or_heartbeat",
            "blocker": None,
            "advance_result": advanced,
            "alerts_authorize_actions": False,
        }
        return self._record_tick(tick_key, result)

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
