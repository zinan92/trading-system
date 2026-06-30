from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.journal_store import load_json, write_json


ORDER_STATES = {
    "entry",
    "submitting",
    "accepted",
    "partially_filled",
    "filled",
    "protective_attached",
    "protective_failed",
    "rejected",
    "cancelled",
    "expired",
    "closed",
    "reconciled",
}

TERMINAL_STATES = {"rejected", "cancelled", "expired", "reconciled"}
WATCHDOG_STATES = {"submitting", "accepted", "partially_filled"}

LEGAL_TRANSITIONS = {
    "entry": {"submitting"},
    "submitting": {"accepted", "rejected", "cancelled", "expired"},
    "accepted": {"partially_filled", "filled", "rejected", "cancelled", "expired"},
    "partially_filled": {"filled", "protective_attached", "protective_failed", "cancelled", "expired", "closed"},
    "filled": {"protective_attached", "protective_failed", "closed"},
    "protective_attached": {"closed"},
    "protective_failed": {"closed"},
    "closed": {"reconciled"},
    "rejected": set(),
    "cancelled": set(),
    "expired": set(),
    "reconciled": set(),
}


class IllegalOrderTransition(ValueError):
    pass


class OrderLifecycleStore:
    def __init__(self, output_root: Path, *, stale_after_cycles: int = 3) -> None:
        self.output_root = Path(output_root)
        self.stale_after_cycles = stale_after_cycles

    def write_intent(
        self,
        run_date: str,
        *,
        order_id: str,
        ticket_id: str,
        idempotency_key: str,
        requested_quantity: float,
        requested_price: float,
        source: str,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[dict, bool]:
        """Write the durable pre-submit intent.

        The tuple returns (record, created). Replays preserve the original
        idempotency key and let the caller decide whether to recover or submit.
        """

        now = self._now()
        rows = self._rows(run_date)
        existing = self._find(rows, order_id)
        if existing:
            existing["last_seen_at"] = now
            existing["intent_replay_count"] = int(existing.get("intent_replay_count", 0) or 0) + 1
            existing.setdefault("idempotency_key", idempotency_key)
            existing.setdefault("metadata", {}).update(metadata or {})
            self._write(run_date, rows)
            return existing, False

        record = {
            "run_date": run_date,
            "order_id": order_id,
            "ticket_id": ticket_id,
            "idempotency_key": idempotency_key,
            "source": source,
            "state": "entry",
            "state_entered_at": now,
            "last_seen_at": now,
            "requested_quantity": round(float(requested_quantity or 0), 12),
            "filled_quantity": 0.0,
            "protective_quantity": 0.0,
            "requested_price": round(float(requested_price or 0), 8),
            "cycles_in_state": 0,
            "blocked": False,
            "blocker": {},
            "metadata": metadata or {},
            "transitions": [
                {
                    "from": "",
                    "to": "entry",
                    "at": now,
                    "reason": "write_ahead_intent",
                    "metadata": metadata or {},
                }
            ],
        }
        rows.append(record)
        self._write(run_date, rows)
        return record, True

    def transition(
        self,
        run_date: str,
        order_id: str,
        to_state: str,
        *,
        reason: str = "",
        metadata: dict[str, Any] | None = None,
        filled_quantity: float | None = None,
        protective_quantity: float | None = None,
    ) -> dict:
        if to_state not in ORDER_STATES:
            raise IllegalOrderTransition(f"unknown order state: {to_state}")
        now = self._now()
        rows = self._rows(run_date)
        record = self._find(rows, order_id)
        if not record:
            raise IllegalOrderTransition(f"order lifecycle intent missing for {order_id}")

        from_state = str(record.get("state") or "")
        if to_state == from_state:
            if metadata:
                record.setdefault("metadata", {}).update(metadata)
            if filled_quantity is not None:
                record["filled_quantity"] = round(float(filled_quantity or 0), 12)
            if protective_quantity is not None:
                record["protective_quantity"] = round(float(protective_quantity or 0), 12)
            record["last_seen_at"] = now
            self._write(run_date, rows)
            return record

        if to_state not in LEGAL_TRANSITIONS.get(from_state, set()):
            raise IllegalOrderTransition(f"illegal order transition: {from_state} -> {to_state}")

        event = {
            "from": from_state,
            "to": to_state,
            "at": now,
            "reason": reason,
            "metadata": metadata or {},
        }
        if record.get("blocked"):
            record.setdefault("resolved_blockers", []).append(
                {
                    "blocker": record.get("blocker", {}),
                    "resolved_at": now,
                    "resolved_by_transition": f"{from_state}->{to_state}",
                    "reason": reason,
                }
            )
            record["blocked"] = False
            record["blocker"] = {}
        record["state"] = to_state
        record["state_entered_at"] = now
        record["last_seen_at"] = now
        record["cycles_in_state"] = 0
        record.setdefault("transitions", []).append(event)
        record["last_transition"] = event
        if metadata:
            record.setdefault("metadata", {}).update(metadata)
        if filled_quantity is not None:
            record["filled_quantity"] = round(float(filled_quantity or 0), 12)
        if protective_quantity is not None:
            record["protective_quantity"] = round(float(protective_quantity or 0), 12)
        self._write(run_date, rows)
        return record

    def record_blocker(
        self,
        run_date: str,
        order_id: str,
        *,
        reason: str,
        source: str,
        action: str,
        details: dict[str, Any] | None = None,
    ) -> dict:
        now = self._now()
        rows = self._rows(run_date)
        record = self._find(rows, order_id)
        blocker = {
            "run_date": run_date,
            "order_id": order_id,
            "ticket_id": record.get("ticket_id", "") if record else "",
            "state": record.get("state", "") if record else "",
            "reason": reason,
            "source": source,
            "action": action,
            "details": details or {},
            "blocked_at": now,
        }
        if record:
            record["blocked"] = True
            record["blocker"] = blocker
            record["last_seen_at"] = now
            self._write(run_date, rows)
        self._append_unique(self.output_root / "order_lifecycle_blocks" / f"{run_date}.json", blocker)
        self._append_unique(self.output_root / "paper_execution_blocks" / f"{run_date}.json", {
            "operation": "order_lifecycle",
            "source": source,
            "reason": reason,
            "action": action,
            "order_id": order_id,
            "details": details or {},
            "run_date": run_date,
            "blocked_at": now,
        })
        return blocker

    def watchdog_tick(self, run_date: str, *, max_cycles: int | None = None) -> list[dict]:
        threshold = self.stale_after_cycles if max_cycles is None else max_cycles
        rows = self._rows(run_date)
        blockers: list[dict] = []
        changed = False
        for record in rows:
            state = str(record.get("state") or "")
            if state not in WATCHDOG_STATES:
                continue
            cycles = int(record.get("cycles_in_state", 0) or 0) + 1
            record["cycles_in_state"] = cycles
            changed = True
            if cycles <= threshold or record.get("blocked"):
                continue
            blockers.append(
                self.record_blocker(
                    run_date,
                    str(record.get("order_id") or ""),
                    reason=f"order stuck in {state} for {cycles} cycle(s)",
                    source="order_lifecycle_watchdog",
                    action="halt_new_orders_until_order_state_reconciled",
                    details={"state": state, "cycles_in_state": cycles, "threshold": threshold},
                )
            )
        if changed:
            # Re-read first because record_blocker may have already written rows.
            latest = self._rows(run_date)
            latest_by_id = {item.get("order_id"): item for item in latest}
            for record in rows:
                order_id = record.get("order_id")
                if order_id in latest_by_id:
                    latest_by_id[order_id]["cycles_in_state"] = record.get("cycles_in_state", 0)
            self._write(run_date, list(latest_by_id.values()))
        return blockers

    def current(self, run_date: str, order_id: str) -> dict:
        return self._find(self._rows(run_date), order_id) or {}

    def mark_closed_orders_reconciled(self, run_date: str, *, reason: str, metadata: dict[str, Any] | None = None) -> list[dict]:
        updated = []
        for record in list(self._rows(run_date)):
            if record.get("state") == "closed":
                updated.append(self.transition(run_date, str(record["order_id"]), "reconciled", reason=reason, metadata=metadata))
        return updated

    def _path(self, run_date: str) -> Path:
        return self.output_root / "order_lifecycle" / f"{run_date}.json"

    def _rows(self, run_date: str) -> list[dict]:
        rows = load_json(self._path(run_date))
        return rows if isinstance(rows, list) else []

    def _write(self, run_date: str, rows: list[dict]) -> None:
        write_json(self._path(run_date), rows)
        write_json(self.output_root / "order_lifecycle" / "current.json", rows[-20:])

    @staticmethod
    def _find(rows: list[dict], order_id: str) -> dict | None:
        return next((item for item in rows if item.get("order_id") == order_id), None)

    def _append_unique(self, path: Path, item: dict) -> None:
        rows = load_json(path)
        if not isinstance(rows, list):
            rows = []
        key = (item.get("operation"), item.get("source"), item.get("reason"), item.get("order_id"))
        if not any((row.get("operation"), row.get("source"), row.get("reason"), row.get("order_id")) == key for row in rows):
            rows.append(item)
            write_json(path, rows)
            if path.name != "current.json":
                write_json(path.parent / "current.json", rows[-20:])

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
