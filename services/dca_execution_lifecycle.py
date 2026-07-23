"""Paper-only aggregate take-profit lifecycle for DCA rounds."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from services.dca_plan import build_dca_entry_commands
from services.execution_engine_port import ExecutionEngineAdapter
from services.journal_store import load_json, write_json


DCA_LIFECYCLE_SCHEMA = "paper-dca-lifecycle-v1"
_EPSILON = 1e-9


class DcaPaperLifecycle:
    """Coordinate DCA entries and one aggregate exit above an engine port."""

    def __init__(self, output_root: Path, adapter: ExecutionEngineAdapter) -> None:
        self.output_root = Path(output_root)
        self.adapter = adapter

    def start(self, plan: dict[str, Any], *, timestamp: str) -> dict[str, Any]:
        identity = _plan_identity(plan)
        existing = self._load(identity["cycle_id"], identity["plan_id"])
        commands = build_dca_entry_commands(plan, timestamp=timestamp)
        state = existing or {
            "schema_version": DCA_LIFECYCLE_SCHEMA,
            "cycle_id": identity["cycle_id"],
            "strategy_plan_id": identity["plan_id"],
            "strategy_plan_version": identity["version"],
            "direction": identity["direction"],
            "round_id": identity["round_id"],
            "status": "starting",
            "entry_order_ids": [],
            "observed_entry_fill_ids": [],
            "target_generations": [],
            "active_target": None,
            "created_at": timestamp,
        }
        self._save(state)
        receipts = []
        for command in commands:
            receipt = self.adapter.submit_order(command)
            receipts.append(receipt)
            order_id = str(receipt.get("order_id") or receipt.get("fill_id") or "")
            if order_id and order_id not in state["entry_order_ids"]:
                state["entry_order_ids"].append(order_id)
                self._save(state)
        state["status"] = "waiting_entry"
        state["entry_submission_count"] = len(commands)
        state["updated_at"] = timestamp
        self._save(state)
        reconciled = self.reconcile(plan, timestamp=timestamp)
        return {"state": reconciled, "receipts": receipts}

    def read_state(self, plan: dict[str, Any]) -> dict[str, Any] | None:
        """Read the persisted Paper lifecycle without reconciling or mutating it."""

        identity = _plan_identity(plan)
        return self._load(identity["cycle_id"], identity["plan_id"])

    def reconcile(self, plan: dict[str, Any], *, timestamp: str) -> dict[str, Any]:
        identity = _plan_identity(plan)
        state = self._load(identity["cycle_id"], identity["plan_id"])
        if state is None:
            raise ValueError("DCA lifecycle has not been started")
        snapshot = self.adapter.snapshot(identity["cycle_id"])
        fills = [
            dict(row)
            for row in snapshot.get("fills") or []
            if str(row.get("strategy_plan_id") or "") == identity["plan_id"]
        ]
        entry_fills = [row for row in fills if str(row.get("event") or "entry") == "entry"]
        entry_fill_ids = sorted({str(row.get("fill_id") or "") for row in entry_fills if row.get("fill_id")})
        filled_entry_order_keys = sorted({_entry_fill_key(row) for row in entry_fills})
        positions = [
            dict(row)
            for row in snapshot.get("positions") or []
            if str(row.get("strategy_plan_id") or "") == identity["plan_id"]
            and str(row.get("status") or "") == "open"
            and str(row.get("side") or "") == identity["direction"]
        ]
        open_quantity = sum(_position_quantity(row) for row in positions)
        average_entry = (
            sum(_position_quantity(row) * _positive(row.get("entry_price"), "DCA position entry price") for row in positions)
            / open_quantity
            if open_quantity > _EPSILON
            else None
        )
        close_fills = [
            row
            for row in fills
            if str(row.get("event") or "") in {"target", "stop", "flatten", "exit"}
        ]
        terminal = close_fills[-1] if close_fills and open_quantity <= _EPSILON else None

        state["observed_entry_fill_ids"] = entry_fill_ids
        state["filled_entry_order_keys"] = filled_entry_order_keys
        state["additions_filled"] = len(filled_entry_order_keys)
        state["open_quantity"] = round(open_quantity, 10)
        state["average_entry_price"] = round(average_entry, 10) if average_entry else None
        state["reconciliation"] = {
            "adapter": str(getattr(self.adapter, "name", "")),
            "at": timestamp,
            "snapshot_order_count": len(snapshot.get("orders") or []),
            "snapshot_fill_count": len(snapshot.get("fills") or []),
            "snapshot_open_position_count": len(positions),
        }
        if terminal:
            event = str(terminal.get("event") or "exit")
            state["status"] = {
                "target": "target_closed",
                "stop": "stop_closed",
                "flatten": "flattened",
                "exit": "closed",
            }[event]
            self._cancel_triggered_target(
                state,
                snapshot=snapshot,
                timestamp=timestamp,
            )
            self._retire_active_target(
                state,
                status="filled" if event == "target" else "cancelled",
                timestamp=timestamp,
                reason=f"round_{event}_closed",
            )
            self._cancel_remaining_entries(state, snapshot=snapshot, timestamp=timestamp)
        elif open_quantity > _EPSILON:
            active = state.get("active_target") if isinstance(state.get("active_target"), dict) else None
            if active and str(active.get("status") or "") == "triggered":
                state["status"] = "target_triggered"
            elif active is None or abs(float(active.get("quantity") or 0.0) - open_quantity) > _EPSILON:
                self._replace_target(
                    state,
                    plan=plan,
                    quantity=open_quantity,
                    average_entry=average_entry,
                    timestamp=timestamp,
                    reason="entry_fill_reconciled",
                )
                state["status"] = "open"
            else:
                state["status"] = "open"
        else:
            self._retire_active_target(
                state,
                status="cancelled",
                timestamp=timestamp,
                reason="no_open_quantity",
            )
            state["status"] = "waiting_entry"
        state["updated_at"] = timestamp
        self._save(state)
        return state

    def process_market_event(
        self,
        plan: dict[str, Any],
        event: dict[str, Any],
    ) -> dict[str, Any]:
        identity = _plan_identity(plan)
        timestamp = str(event.get("ts_event") or "").strip()
        if not timestamp:
            raise ValueError("DCA market event ts_event is required")
        mark = _positive(event.get("price"), "DCA market event price")
        before = self.reconcile(plan, timestamp=timestamp)
        target_price = _positive(plan["dca"].get("target_price"), "DCA target price")
        stop_price = _positive(plan["dca"].get("stop_price"), "DCA stop price")
        target_hit = mark >= target_price if identity["direction"] == "long" else mark <= target_price
        stop_hit = mark <= stop_price if identity["direction"] == "long" else mark >= stop_price
        if before.get("open_quantity", 0) > _EPSILON and (target_hit or stop_hit):
            snapshot = self.adapter.snapshot(identity["cycle_id"])
            self._cancel_remaining_entries(before, snapshot=snapshot, timestamp=timestamp)
            if stop_hit:
                self._cancel_triggered_target(
                    before,
                    snapshot=snapshot,
                    timestamp=timestamp,
                )
            self._save(before)

        engine_result = self.adapter.process_market_event(event)
        after = self.reconcile(plan, timestamp=timestamp)
        submitted_target = None
        active = after.get("active_target") if isinstance(after.get("active_target"), dict) else None
        if (
            target_hit
            and float(after.get("open_quantity") or 0.0) > _EPSILON
            and active
            and str(active.get("status") or "") == "accepted"
        ):
            command = self._target_command(plan, state=after, timestamp=timestamp, mark=mark)
            submitted_target = self.adapter.submit_order(command)
            active["status"] = "triggered"
            active["triggered_at"] = timestamp
            active["execution_order_id"] = str(
                submitted_target.get("order_id") or submitted_target.get("fill_id") or ""
            )
            self._sync_target_generation(after, active)
            after["status"] = "target_triggered"
            after["updated_at"] = timestamp
            self._save(after)
            after = self.reconcile(plan, timestamp=timestamp)
        return {
            "state": after,
            "engine_result": engine_result,
            "target_submission": submitted_target,
            "target_hit": target_hit,
            "stop_hit": stop_hit,
        }

    def _replace_target(
        self,
        state: dict[str, Any],
        *,
        plan: dict[str, Any],
        quantity: float,
        average_entry: float | None,
        timestamp: str,
        reason: str,
    ) -> None:
        self._retire_active_target(
            state,
            status="cancelled",
            timestamp=timestamp,
            reason="replaced_after_quantity_change",
        )
        generation = len(state.get("target_generations") or []) + 1
        target_price = _positive(plan["dca"].get("target_price"), "DCA target price")
        payload = {
            "strategy_plan_id": state["strategy_plan_id"],
            "generation": generation,
            "quantity": round(quantity, 10),
            "price": target_price,
        }
        target = {
            "target_id": f"dca-target-{hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:12]}",
            "generation": generation,
            "status": "accepted",
            "side": "sell" if state["direction"] == "long" else "buy",
            "event": "target",
            "order_type": "limit",
            "reduce_only": True,
            "price": target_price,
            "quantity": round(quantity, 10),
            "average_entry_price": round(float(average_entry), 10) if average_entry else None,
            "created_at": timestamp,
            "reason": reason,
        }
        state.setdefault("target_generations", []).append(target)
        state["active_target"] = target

    @staticmethod
    def _retire_active_target(
        state: dict[str, Any],
        *,
        status: str,
        timestamp: str,
        reason: str,
    ) -> None:
        active = state.get("active_target") if isinstance(state.get("active_target"), dict) else None
        if not active:
            return
        if str(active.get("status") or "") not in {"filled", "cancelled"}:
            active["status"] = status
            active["retired_at"] = timestamp
            active["retire_reason"] = reason
            DcaPaperLifecycle._sync_target_generation(state, active)
        state["active_target"] = None

    @staticmethod
    def _sync_target_generation(
        state: dict[str, Any],
        target: dict[str, Any],
    ) -> None:
        target_id = str(target.get("target_id") or "")
        generations = state.get("target_generations") or []
        for index, row in enumerate(generations):
            if str(row.get("target_id") or "") == target_id:
                generations[index] = dict(target)
                return

    def _cancel_remaining_entries(
        self,
        state: dict[str, Any],
        *,
        snapshot: dict[str, Any],
        timestamp: str,
    ) -> None:
        pending = [
            str(row.get("order_id") or "")
            for row in snapshot.get("orders") or []
            if str(row.get("state") or "").lower() == "accepted"
            and str(row.get("event") or "entry").lower() == "entry"
            and str(row.get("strategy_plan_id") or "") == state["strategy_plan_id"]
            and row.get("order_id")
        ]
        if not pending:
            return
        receipt = self.adapter.cancel_orders(
            state["cycle_id"],
            order_ids=pending,
            ts=timestamp,
            reason="dca_round_exit",
        )
        state["cancelled_entry_order_ids"] = sorted(set(
            [*state.get("cancelled_entry_order_ids", []), *receipt.get("cancelled_order_ids", [])]
        ))

    def _cancel_triggered_target(
        self,
        state: dict[str, Any],
        *,
        snapshot: dict[str, Any],
        timestamp: str,
    ) -> None:
        active = state.get("active_target") if isinstance(state.get("active_target"), dict) else None
        execution_order_id = str((active or {}).get("execution_order_id") or "")
        if not execution_order_id:
            return
        accepted_ids = {
            str(row.get("order_id") or "")
            for row in snapshot.get("orders") or []
            if str(row.get("state") or "").lower() == "accepted"
        }
        if execution_order_id not in accepted_ids:
            return
        receipt = self.adapter.cancel_orders(
            state["cycle_id"],
            order_ids=[execution_order_id],
            ts=timestamp,
            reason="dca_round_closed_before_target_fill",
        )
        state["cancelled_target_order_ids"] = sorted(set(
            [*state.get("cancelled_target_order_ids", []), *receipt.get("cancelled_order_ids", [])]
        ))

    @staticmethod
    def _target_command(
        plan: dict[str, Any],
        *,
        state: dict[str, Any],
        timestamp: str,
        mark: float,
    ) -> dict[str, Any]:
        active = dict(state["active_target"])
        context = plan.get("execution_context") if isinstance(plan.get("execution_context"), dict) else {}
        market = context.get("market") if isinstance(context.get("market"), dict) else {}
        quantity = _positive(active.get("quantity"), "DCA target quantity")
        price = _positive(active.get("price"), "DCA target price")
        return {
            "cycle_id": state["cycle_id"],
            "ts": timestamp,
            "symbol": str(market.get("symbol") or "GOLD"),
            "side": active["side"],
            "event": "target",
            "order_type": "limit",
            "liquidity": "maker",
            "price": price,
            "market_price": mark,
            "quantity": quantity,
            "notional": round(price * quantity, 8),
            "reduce_only": True,
            "trade_id": state["round_id"],
            "position_id": state["round_id"],
            "target_position_side": state["direction"],
            "strategy_type": "dca",
            "strategy_plan_id": state["strategy_plan_id"],
            "strategy_plan_version": state["strategy_plan_version"],
            "source": "strategy_dca_paper",
            "source_fill_id": f"strategy-dca-target:{active['target_id']}",
        }

    def _path(self, cycle_id: str) -> Path:
        return self.output_root / "dualtrack" / "dca_lifecycle" / f"{cycle_id}.json"

    def _load(self, cycle_id: str, plan_id: str) -> dict[str, Any] | None:
        rows = load_json(self._path(cycle_id))
        row = next(
            (
                dict(candidate)
                for candidate in reversed(rows)
                if str(candidate.get("strategy_plan_id") or "") == plan_id
            ),
            None,
        )
        return row

    def _save(self, state: dict[str, Any]) -> None:
        path = self._path(str(state["cycle_id"]))
        rows = load_json(path)
        rows = [
            row
            for row in rows
            if str(row.get("strategy_plan_id") or "") != str(state["strategy_plan_id"])
        ]
        rows.append(state)
        write_json(path, rows)


def _plan_identity(plan: dict[str, Any]) -> dict[str, Any]:
    if (
        not isinstance(plan, dict)
        or plan.get("schema_version") != "strategy-plan-v1"
        or plan.get("strategy_type") != "dca"
    ):
        raise ValueError("DCA lifecycle requires a versioned DCA StrategyPlan")
    plan_id = str(plan.get("strategy_plan_id") or "").strip()
    cycle_id = str(plan.get("cycle_id") or "").strip()
    direction = str(plan.get("direction") or "").lower()
    version = int(plan.get("version") or 0)
    if not plan_id or not cycle_id or direction not in {"long", "short"} or version <= 0:
        raise ValueError("DCA StrategyPlan identity is incomplete")
    return {
        "plan_id": plan_id,
        "cycle_id": cycle_id,
        "version": version,
        "direction": direction,
        "round_id": f"dca-round:{plan_id}",
    }


def _position_quantity(position: dict[str, Any]) -> float:
    for key in ("remaining_units", "units", "quantity"):
        value = position.get(key)
        if value not in (None, ""):
            parsed = float(value)
            return parsed if math.isfinite(parsed) and parsed > 0 else 0.0
    return 0.0


def _entry_fill_key(fill: dict[str, Any]) -> str:
    for key in ("order_id", "source_fill_id", "external_order_id", "fill_id"):
        rendered = str(fill.get(key) or "").strip()
        if rendered:
            return rendered
    raise ValueError("DCA entry fill identity is missing")


def _positive(value: Any, label: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be positive") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{label} must be positive")
    return parsed
