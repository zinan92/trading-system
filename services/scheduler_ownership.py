"""Persistent single-owner contract for the Paper scheduler."""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from services.journal_store import load_json, write_json


LOCAL_OWNER_ID = "local-mac"


def read_local_owner(output_root: Path) -> dict[str, Any]:
    """Return a pure local-owner observation for read-only projections."""

    current = SchedulerOwnershipStore(output_root).current()
    if current:
        active = (
            current.get("status") == "active"
            and current.get("active_owner_id") == LOCAL_OWNER_ID
            and isinstance(current.get("epoch"), int)
            and current["epoch"] >= 1
        )
        return {
            "ok": active,
            "status": "pass" if active else "blocked",
            "owner_status": "local" if active else None,
            "owner_id": current.get("active_owner_id"),
            "epoch": current.get("epoch"),
            **({} if active else {"blocker": "scheduler_owner_mismatch"}),
            "durable": True,
        }
    return {
        "ok": True,
        "status": "pass",
        "owner_status": "local",
        "owner_id": LOCAL_OWNER_ID,
        "epoch": 1,
        "durable": False,
        "next_action": "Materialize the local owner record at the next Park Paper control pass.",
    }


class SchedulerOwnershipStore:
    def __init__(
        self,
        output_root: Path,
        *,
        now: Callable[[], datetime] | None = None,
        scope: str = "paper_only",
    ) -> None:
        self.output_root = Path(output_root)
        self.now = now or (lambda: datetime.now(timezone.utc))
        normalized_scope = str(scope or "").strip().lower()
        if normalized_scope not in {"paper_only", "testnet_only"}:
            raise ValueError("scheduler ownership scope invalid")
        self.scope = normalized_scope
        base = "cloud/scheduler_ownership" if normalized_scope == "paper_only" else "testnet_automation/scheduler_ownership"
        self.path = self.output_root / base / "current.json"

    def current(self) -> dict[str, Any]:
        rows = load_json(self.path)
        return rows[-1] if rows and isinstance(rows[-1], dict) else {}

    def initialize_local(self, *, owner_id: str = LOCAL_OWNER_ID) -> dict[str, Any]:
        with self._lock():
            if self.current():
                raise ValueError("scheduler_ownership_already_initialized")
            return self._write(
                status="active",
                active_owner_id=self._owner(owner_id),
                previous_owner_id=None,
                epoch=1,
                action="initialize_local",
            )

    def pause(self, *, expected_owner_id: str, expected_epoch: int) -> dict[str, Any]:
        with self._lock():
            current = self._require_current(expected_epoch)
            if current.get("status") != "active":
                raise ValueError("scheduler_ownership_not_active")
            if current.get("active_owner_id") != self._owner(expected_owner_id):
                raise ValueError("scheduler_ownership_owner_mismatch")
            return self._write(
                status="paused",
                active_owner_id=None,
                previous_owner_id=current["active_owner_id"],
                epoch=int(current["epoch"]) + 1,
                action="pause",
            )

    def activate(
        self,
        *,
        new_owner_id: str,
        expected_epoch: int,
    ) -> dict[str, Any]:
        with self._lock():
            current = self._require_current(expected_epoch)
            if current.get("status") != "paused":
                raise ValueError("scheduler_ownership_must_be_paused_before_activation")
            return self._write(
                status="active",
                active_owner_id=self._owner(new_owner_id),
                previous_owner_id=current.get("previous_owner_id"),
                epoch=int(current["epoch"]) + 1,
                action="activate",
            )

    def _require_current(self, expected_epoch: int) -> dict[str, Any]:
        current = self.current()
        if not current:
            raise ValueError("scheduler_ownership_not_initialized")
        if int(current.get("epoch") or 0) != int(expected_epoch):
            raise ValueError("scheduler_ownership_epoch_mismatch")
        return current

    def _write(
        self,
        *,
        status: str,
        active_owner_id: str | None,
        previous_owner_id: str | None,
        epoch: int,
        action: str,
    ) -> dict[str, Any]:
        payload = {
            "schema_version": (
                "paper-scheduler-ownership-v1"
                if self.scope == "paper_only"
                else "testnet-scheduler-ownership-v1"
            ),
            "scope": self.scope,
            "status": status,
            "active_owner_id": active_owner_id,
            "previous_owner_id": previous_owner_id,
            "epoch": epoch,
            "action": action,
            "updated_at": self.now().astimezone(timezone.utc).replace(microsecond=0).isoformat(),
            "dual_owner_allowed": False,
        }
        rows = load_json(self.path)
        rows.append(payload)
        write_json(self.path, rows)
        return payload

    @staticmethod
    def _owner(value: str) -> str:
        owner = str(value or "").strip()
        if not owner or len(owner) > 100 or not all(
            char.isalnum() or char in "._-" for char in owner
        ):
            raise ValueError("scheduler_owner_id_invalid")
        return owner

    @contextmanager
    def _lock(self) -> Iterator[None]:
        path = self.path.with_suffix(".lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class SchedulerOwnershipGuard:
    def __init__(
        self,
        output_root: Path,
        *,
        owner_id: str | None = None,
        runtime_mode: str | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.runtime_mode = str(
            runtime_mode or os.getenv("GRIDMIND_RUNTIME_MODE") or "local"
        )
        configured = str(
            owner_id
            if owner_id is not None
            else os.getenv("GRIDMIND_SCHEDULER_OWNER_ID") or ""
        ).strip()
        self.owner_id = configured or (
            LOCAL_OWNER_ID if self.runtime_mode != "cloud" else ""
        )

    def verify(self) -> dict[str, Any]:
        current = SchedulerOwnershipStore(self.output_root).current()
        if not current:
            if self.runtime_mode == "cloud":
                return self._blocked("scheduler_ownership_missing", current)
            # Local Park Paper is the default and sole owner.  Materialize the
            # owner record instead of treating an absent record as legacy
            # compatibility; the epoch is then available to every observer.
            initialized = SchedulerOwnershipStore(self.output_root).initialize_local(
                owner_id=self.owner_id,
            )
            return self._receipt(
                {
                    "ok": True,
                    "status": "pass",
                    "owner_id": self.owner_id,
                    "epoch": initialized["epoch"],
                    "owner_status": "local",
                }
            )
        if current.get("status") != "active":
            return self._blocked("scheduler_ownership_paused", current)
        if not self.owner_id:
            return self._blocked("scheduler_owner_id_not_configured", current)
        if current.get("active_owner_id") != self.owner_id:
            return self._blocked("scheduler_owner_id_mismatch", current)
        return self._receipt(
            {
                "ok": True,
                "status": "pass",
                "owner_id": self.owner_id,
                "epoch": current.get("epoch"),
                "owner_status": "local" if self.runtime_mode != "cloud" else "cloud",
            }
        )

    def _blocked(self, blocker: str, current: dict[str, Any]) -> dict[str, Any]:
        return self._receipt(
            {
                "ok": False,
                "status": "blocked",
                "blocker": blocker,
                "owner_id": self.owner_id or None,
                "active_owner_id": current.get("active_owner_id"),
                "epoch": current.get("epoch"),
                "next_action": (
                    "Keep every Paper scheduler disabled until the explicit "
                    "single-owner transition is completed."
                ),
            }
        )

    def _receipt(self, payload: dict[str, Any]) -> dict[str, Any]:
        receipt = {
            "schema_version": "paper-scheduler-owner-guard-v1",
            "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "runtime_mode": self.runtime_mode,
            "dual_owner_allowed": False,
            **payload,
        }
        write_json(
            self.output_root
            / "cloud"
            / "scheduler_ownership"
            / "guard_current.json",
            [receipt],
        )
        return receipt
