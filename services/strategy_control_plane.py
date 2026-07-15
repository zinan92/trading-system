"""Single-production-plan control plane with a non-destructive DualTrack migration.

This module deliberately owns *selection and attribution*, not execution.  The
legacy human/machine files remain immutable source records and are exposed as
same-schema proposals until a production plan is locked.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.dualtrack_execution_adapter import build_execution_engine_adapter
from services.dualtrack_config import dualtrack_config
from services.dualtrack_store import DualTrackPlanStore
from services.journal_store import load_json, write_json


PROPOSAL_SCHEMA = "strategy-plan-proposal-v1"
PLAN_SCHEMA = "strategy-plan-v1"
PLAN_FIELDS = ("direction", "style", "range", "key_levels", "grid", "signal", "tp_sl", "risk_budget", "intraday_rules")
FIELD_SOURCES = {"human", "ai", "confirmed"}
GRID_DIRECTIONS = {"neutral", "long", "short"}
GRID_STYLES = {"steady", "aggressive"}
_CONTROL_LOCK = threading.RLock()


class StrategyControlPlane:
    def __init__(self, output_root: Path) -> None:
        self.output_root = Path(output_root)
        self.root = self.output_root / "dualtrack" / "strategy_control"
        self.config = dualtrack_config()

    def upsert_proposal(self, payload: dict[str, Any], *, now: str | None = None) -> dict[str, Any]:
        proposal = normalize_proposal(payload, now=now)
        path = self._proposals_path(proposal["cycle_id"])
        rows = load_json(path)
        rows = [row for row in rows if str(row.get("proposal_id")) != proposal["proposal_id"]]
        rows.append(proposal)
        write_json(path, rows)
        return proposal

    def proposals(self, cycle_id: str) -> list[dict[str, Any]]:
        rows = [row for row in load_json(self._proposals_path(cycle_id)) if isinstance(row, dict)]
        if rows:
            return rows
        # Compatibility read: historical records are never moved or rewritten.
        store = DualTrackPlanStore(self.output_root)
        legacy = []
        for source in ("human", "ai"):
            row = store.load_plan(cycle_id, source)
            if row:
                legacy.append(normalize_proposal({**row, "source": source}, legacy=True))
        return legacy

    def proposal_diff(self, cycle_id: str) -> dict[str, Any]:
        proposals = self.proposals(cycle_id)
        by_source = {str(row.get("source")): row for row in proposals}
        human, ai = by_source.get("human"), by_source.get("ai")
        fields: dict[str, dict[str, Any]] = {}
        for key in PLAN_FIELDS:
            left = human.get(key) if human else None
            right = ai.get(key) if ai else None
            fields[key] = {"human": left, "ai": right, "different": human is not None and ai is not None and left != right}
        return {"cycle_id": cycle_id, "available_sources": sorted(by_source), "fields": fields}

    def lock_production_plan(
        self,
        cycle_id: str,
        *,
        selected_proposal_id: str,
        field_sources: dict[str, str] | None = None,
        now: str | None = None,
    ) -> dict[str, Any]:
        with _CONTROL_LOCK:
            return self._lock_production_plan(
                cycle_id,
                selected_proposal_id=selected_proposal_id,
                field_sources=field_sources,
                now=now,
            )

    def _lock_production_plan(
        self,
        cycle_id: str,
        *,
        selected_proposal_id: str,
        field_sources: dict[str, str] | None = None,
        now: str | None = None,
    ) -> dict[str, Any]:
        proposals = self.proposals(cycle_id)
        selected = next((row for row in proposals if row.get("proposal_id") == selected_proposal_id), None)
        if not selected:
            raise ValueError("selected proposal does not exist")
        sources = _field_sources(field_sources, selected_source=str(selected["source"]))
        existing = self.active_plan(cycle_id)
        version = int(existing.get("version") or 0) + 1 if existing else 1
        plan = {
            "schema_version": PLAN_SCHEMA,
            "strategy_plan_id": _plan_id(cycle_id, version, selected["proposal_id"]),
            "cycle_id": cycle_id,
            "version": version,
            "status": "active",
            "locked_at": _timestamp(now),
            "source_proposal_ids": [selected["proposal_id"]],
            "field_sources": sources,
            **{key: selected.get(key) for key in PLAN_FIELDS},
        }
        self._activate_plan(plan)
        return plan

    def active_plan(self, cycle_id: str) -> dict[str, Any] | None:
        plans = [row for row in load_json(self._plans_path(cycle_id)) if isinstance(row, dict)]
        active = [row for row in plans if row.get("status") == "active"]
        return active[-1] if active else None

    def ensure_compatible_active_plan(self, cycle_id: str, *, as_of: str | None = None) -> dict[str, Any] | None:
        with _CONTROL_LOCK:
            return self._ensure_compatible_active_plan(cycle_id, as_of=as_of)

    def _ensure_compatible_active_plan(self, cycle_id: str, *, as_of: str | None = None) -> dict[str, Any] | None:
        active = self.active_plan(cycle_id)
        if active:
            return active
        proposals = self.proposals(cycle_id)
        # Old precedence remains explicit: human locked proposal, then ai; no mixed plan.
        selected = next((row for row in proposals if row.get("source") == "human" and row.get("legacy_status") == "locked"), None)
        selected = selected or next((row for row in proposals if row.get("source") == "ai"), None)
        if not selected:
            return None
        return self._lock_production_plan(
            cycle_id,
            selected_proposal_id=str(selected["proposal_id"]),
            field_sources={key: str(selected["source"]) for key in PLAN_FIELDS},
            now=as_of,
        )

    def read_model(self, cycle_id: str, *, as_of: str | None = None) -> dict[str, Any]:
        proposals = self.proposals(cycle_id)
        plan = self.ensure_compatible_active_plan(cycle_id, as_of=as_of)
        return {
            "schema_version": "strategy-production-console-v1",
            "cycle_id": cycle_id,
            "production_plan": plan,
            "proposals": proposals,
            "proposal_diff": self.proposal_diff(cycle_id),
            "migration": {
                "legacy_compatible": bool(proposals),
                "legacy_records_preserved": True,
                "legacy_execution_shadow_separate": True,
            },
            "runtime": self.runtime_state(cycle_id),
        }

    def runtime_state(self, cycle_id: str) -> dict[str, Any]:
        rows = load_json(self.root / "runtime.json")
        row = rows[-1] if rows else {}
        stored_cycle_id = str(row.get("cycle_id") or "")
        if stored_cycle_id and stored_cycle_id != cycle_id:
            return {
                "desired_state": "stopped",
                "actual_state": "stopped",
                "cycle_id": cycle_id,
                "updated_at": row.get("updated_at"),
                "statistics_baseline_at": None,
                "strategy_plan_id": None,
                "strategy_plan_version": None,
                "preview_id": None,
                "accepted_order_count": 0,
                "last_action": None,
                "last_error": None,
                "stale_cycle": True,
                "previous_cycle_id": stored_cycle_id,
                "previous_actual_state": str(row.get("actual_state") or row.get("desired_state") or "stopped"),
            }
        return {
            "desired_state": str(row.get("desired_state") or "stopped"),
            "actual_state": str(row.get("actual_state") or row.get("desired_state") or "stopped"),
            "cycle_id": str(row.get("cycle_id") or cycle_id),
            "updated_at": row.get("updated_at"),
            "statistics_baseline_at": row.get("statistics_baseline_at"),
            "strategy_plan_id": row.get("strategy_plan_id"),
            "strategy_plan_version": row.get("strategy_plan_version"),
            "preview_id": row.get("preview_id"),
            "accepted_order_count": int(row.get("accepted_order_count") or 0),
            "last_action": row.get("last_action"),
            "last_error": row.get("last_error"),
            "stale_cycle": False,
            "previous_cycle_id": None,
            "previous_actual_state": None,
        }

    def runtime_configured(self) -> bool:
        return (self.root / "runtime.json").exists()

    def preview(
        self,
        cycle_id: str,
        payload: dict[str, Any] | None = None,
        *,
        market: dict[str, Any],
        account: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = dict(payload or {})
        _validate_market(market)
        direction = str(body.get("direction") or "neutral").lower()
        style = str(body.get("style") or "steady").lower()
        if direction not in GRID_DIRECTIONS:
            raise ValueError("direction must be neutral, long, or short")
        if style not in GRID_STYLES:
            raise ValueError("style must be steady or aggressive")

        latest = _positive_number(market.get("latest_close"), "market latest_close")
        strategy_cfg = dict(self.config.get("strategy_grid") or {})
        range_timeframe = str(strategy_cfg.get("range_timeframe") or "1d")
        spacing_timeframe = str(strategy_cfg.get("spacing_timeframe") or "4h")
        execution_timeframe = str(strategy_cfg.get("execution_timeframe") or "1m")
        range_period = int(strategy_cfg.get("range_atr_period") or 14)
        spacing_period = int(strategy_cfg.get("spacing_atr_period") or 14)
        range_bars = _strategy_bars(market, range_timeframe, range_period)
        spacing_bars = _strategy_bars(market, spacing_timeframe, spacing_period)
        range_atr = _average_true_range(range_bars, period=range_period)
        spacing_atr = _average_true_range(spacing_bars, period=spacing_period)
        styles = strategy_cfg.get("styles") if isinstance(strategy_cfg.get("styles"), dict) else {}
        style_cfg = dict(styles.get(style) or {})
        range_multiple = _positive_number(style_cfg.get("range_atr_multiple"), "range ATR multiple")
        spacing_multiple = _positive_number(style_cfg.get("spacing_atr_multiple"), "spacing ATR multiple")
        margin_utilization = _positive_number(style_cfg.get("margin_utilization_cap"), "margin utilization cap")
        max_plan_loss_pct = _positive_number(style_cfg.get("max_plan_loss_pct"), "max plan loss pct")
        if margin_utilization > 1 or max_plan_loss_pct > 1:
            raise ValueError("strategy risk fractions must not exceed 1")

        # Direction controls which side is armed. It does not secretly move the
        # market range; identical market evidence must produce identical geometry.
        half_span = range_atr * range_multiple
        suggested_low, suggested_high = latest - half_span, latest + half_span

        range_input = body.get("range") if isinstance(body.get("range"), dict) else {}
        low = _number_or(range_input.get("low"), suggested_low)
        high = _number_or(range_input.get("high"), suggested_high)
        if low <= 0 or high <= low:
            raise ValueError("grid range must have positive low below high")
        grid_input = body.get("grid") if isinstance(body.get("grid"), dict) else {}
        target_spacing = max(
            spacing_atr * spacing_multiple,
            latest * float(self.config.get("cost_per_side_bp") or 0.0) * 2.0
            * float(strategy_cfg.get("cost_spacing_multiple") or 1.0) / 10_000.0,
            0.0001,
        )
        min_count = max(2, int(strategy_cfg.get("min_grid_count") or 24))
        max_count = max(min_count, int(strategy_cfg.get("max_grid_count") or 80))
        requested_count = _number_or(grid_input.get("count"), 0.0)
        count = int(requested_count) if requested_count > 0 else max(min_count, min(max_count, int((high - low) / target_spacing)))
        if count < 2 or count > 80:
            raise ValueError("grid count must be between 2 and 80")
        spacing = (high - low) / count
        if spacing <= 0:
            raise ValueError("grid spacing must be positive")

        equity = _account_equity(account or {})
        risk_input = body.get("risk_budget") if isinstance(body.get("risk_budget"), dict) else {}
        leverage_limit = float(self.config.get("max_leverage") or 10.0)
        leverage = _number_or(risk_input.get("leverage", body.get("leverage")), leverage_limit)
        if leverage <= 0 or leverage > leverage_limit:
            raise ValueError(f"leverage must be greater than 0 and at most {leverage_limit:g}")

        levels = [low + spacing * index for index in range(count + 1)]
        nearest_index = min(range(len(levels)), key=lambda index: abs(levels[index] - latest))
        provisional_orders: list[dict[str, Any]] = []
        for index, raw_price in enumerate(levels):
            if index == nearest_index:
                continue
            price = round(raw_price, 4)
            side = "buy" if price < latest else "sell"
            if direction == "long" and side != "buy":
                continue
            if direction == "short" and side != "sell":
                continue
            tp = price + spacing if side == "buy" else price - spacing
            sl = low - spacing if side == "buy" else high + spacing
            provisional_orders.append({
                "preview_order_id": f"preview-{index:02d}-{side}",
                "level": index,
                "state": "preview",
                "side": side,
                "event": "entry",
                "order_type": "limit",
                "price": price,
                "tp": round(tp, 4),
                "sl": round(sl, 4),
            })
        if not provisional_orders:
            raise ValueError("selected direction has no executable grid orders in this range")

        side_counts = {
            side: sum(1 for order in provisional_orders if order["side"] == side)
            for side in ("buy", "sell")
        }
        max_simultaneous_levels = max(side_counts.values())
        absolute_notional_ceiling = equity * leverage
        capital_budget = absolute_notional_ceiling * margin_utilization
        capital_notional_cap = capital_budget / max_simultaneous_levels
        side_loss_rates = {
            side: sum(abs(order["price"] - order["sl"]) / order["price"] for order in provisional_orders if order["side"] == side)
            for side in ("buy", "sell")
        }
        max_side_loss_rate = max(side_loss_rates.values())
        max_loss_budget = equity * max_plan_loss_pct
        risk_notional_cap = max_loss_budget / max_side_loss_rate if max_side_loss_rate > 0 else capital_notional_cap
        safe_notional = min(capital_notional_cap, risk_notional_cap)
        requested_notional = _number_or(grid_input.get("notional_per_grid"), 0.0)
        default_notional_mode = "manual" if requested_notional > 0 else "auto"
        notional_mode = str(grid_input.get("notional_mode") or default_notional_mode).lower()
        if notional_mode not in {"auto", "manual"}:
            raise ValueError("grid notional_mode must be auto or manual")
        if notional_mode == "manual":
            if requested_notional <= 0:
                raise ValueError("manual notional_per_grid must be greater than zero")
            if requested_notional > safe_notional + 1e-8:
                raise ValueError(
                    f"notional_per_grid {requested_notional:.2f} exceeds safe cap {safe_notional:.2f} "
                    "for the selected leverage and risk budget"
                )
            notional = requested_notional
        else:
            # Auto sizing is a policy, not a fixed quote. Recalculate it from the
            # same trusted market/account snapshot used by the start transaction.
            notional = safe_notional
        if notional <= 0:
            raise ValueError("safe per-grid notional is zero")
        orders = [
            {
                **order,
                "quantity": round(notional / order["price"], 8),
                "notional": round(notional, 2),
            }
            for order in provisional_orders
        ]
        side_losses = {
            side: sum(abs(order["price"] - order["sl"]) * order["quantity"] for order in orders if order["side"] == side)
            for side in ("buy", "sell")
        }
        max_loss = max(side_losses.values())
        max_side_notional = max(side_counts[side] * notional for side in side_counts)
        estimated_margin = max_side_notional / leverage
        preview = {
            "schema_version": "strategy-grid-preview-v1",
            "cycle_id": cycle_id,
            "direction": direction,
            "style": style,
            "market": {
                "price": latest,
                "timestamp": market.get("latest_timestamp"),
                "provider": market.get("provider"),
                "timeframe": market.get("timeframe"),
                "execution_timeframe": execution_timeframe,
            },
            "range": {
                "low": round(low, 4),
                "high": round(high, 4),
                "method": f"D1 ATR{range_period} × {range_multiple:g}",
                "source_timeframe": range_timeframe,
                "atr_period": range_period,
                "atr": round(range_atr, 4),
                "atr_multiple": range_multiple,
            },
            "grid": {
                "count": count,
                "spacing": round(spacing, 4),
                "target_spacing": round(target_spacing, 4),
                "spacing_source_timeframe": spacing_timeframe,
                "spacing_atr_period": spacing_period,
                "spacing_atr": round(spacing_atr, 4),
                "spacing_atr_multiple": spacing_multiple,
                "notional_per_grid": round(notional, 2),
                "notional_mode": notional_mode,
                "leverage": round(leverage, 2),
                "leverage_limit": round(leverage_limit, 2),
                "margin_utilization_cap": margin_utilization,
                "out_of_range": str(body.get("out_of_range") or grid_input.get("out_of_range") or "exit_only"),
            },
            "orders": orders,
            "risk": {
                "equity": round(equity, 2),
                "estimated_margin": round(estimated_margin, 2),
                "max_loss": round(max_loss, 2),
                "max_loss_budget": round(max_loss_budget, 2),
                "absolute_notional_ceiling": round(absolute_notional_ceiling, 2),
                "capital_budget": round(capital_budget, 2),
                "capital_notional_cap_per_grid": round(capital_notional_cap, 2),
                "risk_notional_cap_per_grid": round(risk_notional_cap, 2),
                "max_simultaneous_same_side_levels": max_simultaneous_levels,
                "actual_leverage": round(max_side_notional / equity, 4) if equity else None,
                "calibration_status": "shadow_candidate",
            },
            "strategy_timeframes": {
                "range": range_timeframe,
                "spacing": spacing_timeframe,
                "execution": execution_timeframe,
            },
        }
        preview["preview_id"] = _preview_id(preview)
        return preview

    def control(
        self,
        cycle_id: str,
        action: str,
        payload: dict[str, Any] | None = None,
        *,
        market: dict[str, Any] | None = None,
        account: dict[str, Any] | None = None,
        now: str | None = None,
    ) -> dict[str, Any]:
        with _CONTROL_LOCK:
            return self._control_locked(cycle_id, action, payload, market=market, account=account, now=now)

    def _control_locked(
        self,
        cycle_id: str,
        action: str,
        payload: dict[str, Any] | None = None,
        *,
        market: dict[str, Any] | None = None,
        account: dict[str, Any] | None = None,
        now: str | None = None,
    ) -> dict[str, Any]:
        body = dict(payload or {})
        action = str(action or "").lower()
        if action == "preview":
            return {"action": action, "preview": self.preview(cycle_id, body, market=market or {}, account=account)}
        if action == "start":
            return self._start(cycle_id, body, market=market or {}, account=account or {}, now=now)
        if action == "stop":
            return self._stop(cycle_id, market=market, now=now)
        if action == "reset_statistics":
            previous = self.runtime_state(cycle_id)
            row = {
                **previous,
                "cycle_id": cycle_id,
                "desired_state": previous["desired_state"],
                "updated_at": _timestamp(now),
                "last_action": action,
            }
            row["statistics_baseline_at"] = _timestamp(now)
            write_json(self.root / "runtime.json", [row])
            return {"action": action, "runtime": row, "historical_records_preserved": True}
        if action == "adjust_plan":
            if market is not None and body.get("style") in GRID_STYLES:
                return self._regrid(cycle_id, body, market=market, account=account or {}, now=now)
            current = self.active_plan(cycle_id) or self.ensure_compatible_active_plan(cycle_id, as_of=now)
            if not current:
                raise ValueError("cannot adjust without an active StrategyPlan")
            adjusted = {**current, "version": int(current["version"]) + 1, "status": "active", "locked_at": _timestamp(now)}
            for key in ("direction", "range", "grid", "tp_sl", "risk_budget", "intraday_rules", "key_levels", "signal"):
                if key in body:
                    adjusted[key] = body[key]
                    adjusted["field_sources"] = {**adjusted["field_sources"], key: "confirmed"}
            adjusted["strategy_plan_id"] = _plan_id(cycle_id, adjusted["version"], f"console-adjust-{_timestamp(now)}")
            self._activate_plan(adjusted)
            return {"action": action, "plan": adjusted}
        if action == "cancel_all":
            path = self.output_root / "dualtrack" / "orders" / f"{cycle_id}_human.json"
            rows = load_json(path)
            cancelled = 0
            for row in rows:
                if row.get("state") == "accepted":
                    row["state"] = "cancelled"
                    row["cancelled_at"] = _timestamp(now)
                    cancelled += 1
            write_json(path, rows)
            return {"action": action, "cancelled_orders": cancelled}
        raise ValueError("unsupported production control action")

    def _start(
        self,
        cycle_id: str,
        body: dict[str, Any],
        *,
        market: dict[str, Any],
        account: dict[str, Any],
        now: str | None,
    ) -> dict[str, Any]:
        current = self.active_plan(cycle_id) or self.ensure_compatible_active_plan(cycle_id, as_of=now)
        if not current:
            raise ValueError("cannot start without an active StrategyPlan")
        preview = self.preview(cycle_id, body, market=market, account=account)
        runtime = self.runtime_state(cycle_id)
        pending = self._accepted_orders(cycle_id)
        same_running_plan = (
            runtime["desired_state"] == "running"
            and runtime.get("preview_id") == preview["preview_id"]
            and runtime.get("strategy_plan_id") == current.get("strategy_plan_id")
            and bool(pending)
        )
        if same_running_plan:
            return {
                "action": "start",
                "runtime": runtime,
                "plan": current,
                "created_orders": 0,
                "accepted_orders": len(pending),
                "idempotent": True,
            }
        if runtime["desired_state"] == "running":
            raise ValueError("robot is already running; stop it before changing the grid")

        adjusted = self._plan_from_preview(current, preview, now=now)
        self._activate_plan(adjusted)
        timestamp = _timestamp(now)
        starting = {
            **runtime,
            "cycle_id": cycle_id,
            "desired_state": "running",
            "actual_state": "starting",
            "updated_at": timestamp,
            "last_action": "start",
            "last_error": None,
            "strategy_plan_id": adjusted["strategy_plan_id"],
            "strategy_plan_version": adjusted["version"],
            "preview_id": preview["preview_id"],
            "accepted_order_count": 0,
        }
        self._write_runtime(starting)
        adapter = build_execution_engine_adapter(self.output_root)
        try:
            receipts = self._submit_preview_orders(adapter, cycle_id, adjusted, preview, market=market, timestamp=timestamp)
        except Exception as exc:
            self._cancel_pending(cycle_id, now=now, strategy_plan_id=adjusted["strategy_plan_id"])
            adjusted["status"] = "failed"
            current["status"] = "active"
            self._write_plan(adjusted)
            self._write_plan(current)
            self._write_runtime({
                **starting,
                "desired_state": "stopped",
                "actual_state": "error",
                "updated_at": _timestamp(now),
                "last_error": str(exc),
                "accepted_order_count": 0,
            })
            raise

        running = {
            **starting,
            "actual_state": "running",
            "updated_at": _timestamp(now),
            "accepted_order_count": len(receipts),
        }
        self._write_runtime(running)
        return {
            "action": "start",
            "runtime": running,
            "plan": adjusted,
            "preview": preview,
            "orders": receipts,
            "created_orders": len(receipts),
            "accepted_orders": len(receipts),
            "idempotent": False,
        }

    def _regrid(
        self,
        cycle_id: str,
        body: dict[str, Any],
        *,
        market: dict[str, Any],
        account: dict[str, Any],
        now: str | None,
    ) -> dict[str, Any]:
        runtime = self.runtime_state(cycle_id)
        if runtime["desired_state"] != "running":
            raise ValueError("running adjustment requires a running robot")
        current = self.active_plan(cycle_id)
        if not current:
            raise ValueError("cannot adjust without an active StrategyPlan")
        preview = self.preview(cycle_id, body, market=market, account=account)
        if runtime.get("preview_id") == preview["preview_id"]:
            return {
                "action": "adjust_plan",
                "runtime": runtime,
                "plan": current,
                "created_orders": 0,
                "cancelled_orders": 0,
                "idempotent": True,
            }

        orders_path = self.output_root / "dualtrack" / "orders" / f"{cycle_id}_human.json"
        orders_before = load_json(orders_path)
        old_accepted = sum(1 for row in orders_before if row.get("state") == "accepted")
        adjusted = self._plan_from_preview(current, preview, now=now)
        timestamp = _timestamp(now)
        replanning = {
            **runtime,
            "actual_state": "replanning",
            "updated_at": timestamp,
            "last_action": "adjust_plan",
            "last_error": None,
            "strategy_plan_id": adjusted["strategy_plan_id"],
            "strategy_plan_version": adjusted["version"],
            "preview_id": preview["preview_id"],
            "accepted_order_count": 0,
        }
        self._write_runtime(replanning)
        self._activate_plan(adjusted)
        adapter = build_execution_engine_adapter(self.output_root)
        try:
            cancelled = self._cancel_pending(cycle_id, now=now)
            receipts = self._submit_preview_orders(adapter, cycle_id, adjusted, preview, market=market, timestamp=timestamp)
        except Exception as exc:
            write_json(orders_path, orders_before)
            adjusted["status"] = "failed"
            current["status"] = "active"
            self._write_plan(adjusted)
            self._write_plan(current)
            self._write_runtime({
                **runtime,
                "actual_state": "error",
                "updated_at": _timestamp(now),
                "last_action": "adjust_plan",
                "last_error": str(exc),
                "accepted_order_count": old_accepted,
            })
            raise

        running = {
            **replanning,
            "actual_state": "running",
            "updated_at": _timestamp(now),
            "accepted_order_count": len(receipts),
        }
        self._write_runtime(running)
        return {
            "action": "adjust_plan",
            "runtime": running,
            "plan": adjusted,
            "preview": preview,
            "orders": receipts,
            "created_orders": len(receipts),
            "cancelled_orders": cancelled,
            "idempotent": False,
        }

    def _submit_preview_orders(
        self,
        adapter,
        cycle_id: str,
        plan: dict[str, Any],
        preview: dict[str, Any],
        *,
        market: dict[str, Any],
        timestamp: str,
    ) -> list[dict[str, Any]]:
        receipts: list[dict[str, Any]] = []
        for order in preview["orders"]:
            receipt = adapter.submit_order({
                "cycle_id": cycle_id,
                "ts": timestamp,
                "symbol": str(market.get("symbol") or "GOLD"),
                "side": order["side"],
                "event": "entry",
                "order_type": "limit",
                "price": order["price"],
                "market_price": preview["market"]["price"],
                "quantity": order["quantity"],
                "notional": order["notional"],
                "sl": order["sl"],
                "tp": order["tp"],
                "source": "strategy_production_console",
                "source_fill_id": f"strategy-grid:{plan['strategy_plan_id']}:{order['preview_order_id']}",
                "strategy_plan_id": plan["strategy_plan_id"],
                "strategy_plan_version": plan["version"],
            })
            if str(receipt.get("state") or receipt.get("status") or "") != "accepted":
                raise ValueError("paper execution did not accept a grid order")
            receipts.append(receipt)
        return receipts

    def _stop(self, cycle_id: str, *, market: dict[str, Any] | None, now: str | None) -> dict[str, Any]:
        previous = self.runtime_state(cycle_id)
        stopping = {
            **previous,
            "cycle_id": cycle_id,
            "desired_state": "stopped",
            "actual_state": "stopping",
            "updated_at": _timestamp(now),
            "last_action": "stop",
            "last_error": None,
        }
        self._write_runtime(stopping)
        cancelled = self._cancel_pending(cycle_id, now=now)
        adapter = build_execution_engine_adapter(self.output_root)
        snapshot = adapter.snapshot(cycle_id)
        open_positions = [row for row in snapshot.get("positions") or [] if row.get("status") == "open"]
        flattened: list[dict[str, Any]] = []
        try:
            if open_positions:
                _validate_market(market or {})
                price = _positive_number((market or {}).get("latest_close"), "market latest_close")
                timestamp = _timestamp(now or (market or {}).get("latest_timestamp"))
                for position in open_positions:
                    side = "sell" if str(position.get("side") or "") in {"buy", "long"} else "buy"
                    flattened.append(adapter.submit_order({
                        "cycle_id": cycle_id,
                        "ts": timestamp,
                        "side": side,
                        "event": "flatten",
                        "order_type": "market",
                        "price": price,
                        "market_price": price,
                        "trade_id": position.get("trade_id"),
                        "position_id": position.get("position_id"),
                        "symbol": position.get("symbol") or str((market or {}).get("symbol") or "GOLD"),
                        "source": "strategy_production_console",
                        "source_fill_id": f"strategy-stop:{cycle_id}:{position.get('trade_id')}",
                        "strategy_plan_id": previous.get("strategy_plan_id"),
                        "strategy_plan_version": previous.get("strategy_plan_version"),
                    }))
            reconciliation = adapter.reconcile(cycle_id)
            if reconciliation.get("status") != "ok":
                raise ValueError("paper ledger reconciliation failed")
        except Exception as exc:
            self._write_runtime({
                **stopping,
                "actual_state": "error",
                "updated_at": _timestamp(now),
                "last_error": str(exc),
                "accepted_order_count": len(self._accepted_orders(cycle_id)),
            })
            raise

        stopped = {
            **stopping,
            "actual_state": "stopped",
            "updated_at": _timestamp(now),
            "accepted_order_count": 0,
        }
        self._write_runtime(stopped)
        return {
            "action": "stop",
            "runtime": stopped,
            "cancelled_orders": cancelled,
            "flattened_positions": len(flattened),
            "reconciliation": reconciliation,
            "historical_records_preserved": True,
        }

    def _plan_from_preview(self, current: dict[str, Any], preview: dict[str, Any], *, now: str | None) -> dict[str, Any]:
        version = int(current.get("version") or 0) + 1
        plan = {
            **current,
            "strategy_plan_id": _plan_id(str(current["cycle_id"]), version, preview["preview_id"]),
            "version": version,
            "status": "active",
            "locked_at": _timestamp(now),
            "direction": preview["direction"],
            "style": preview["style"],
            "range": dict(preview["range"]),
            "grid": {**preview["grid"], "orders": [dict(order) for order in preview["orders"]]},
            "tp_sl": {
                "mode": "per_grid",
                "take_profit": "next_grid_level",
                "stop_loss": "one_grid_beyond_range",
                "r_multiple": 1.0,
            },
            "risk_budget": {
                **dict(current.get("risk_budget") or {}),
                "leverage": preview["grid"]["leverage"],
                "max_loss": preview["risk"]["max_loss"],
                "estimated_margin": preview["risk"]["estimated_margin"],
            },
            "field_sources": {
                **dict(current.get("field_sources") or {}),
                "direction": "confirmed",
                "range": "confirmed",
                "grid": "confirmed",
                "tp_sl": "confirmed",
                "risk_budget": "confirmed",
            },
            "preview_id": preview["preview_id"],
        }
        return plan

    def _accepted_orders(self, cycle_id: str) -> list[dict[str, Any]]:
        path = self.output_root / "dualtrack" / "orders" / f"{cycle_id}_human.json"
        return [row for row in load_json(path) if row.get("state") == "accepted"]

    def _cancel_pending(self, cycle_id: str, *, now: str | None, strategy_plan_id: str | None = None) -> int:
        path = self.output_root / "dualtrack" / "orders" / f"{cycle_id}_human.json"
        rows = load_json(path)
        cancelled = 0
        for row in rows:
            row_plan_id = row.get("strategy_plan_id") or (row.get("command") or {}).get("strategy_plan_id")
            if row.get("state") != "accepted" or (strategy_plan_id and row_plan_id != strategy_plan_id):
                continue
            row["state"] = "cancelled"
            row["cancelled_at"] = _timestamp(now)
            cancelled += 1
        write_json(path, rows)
        return cancelled

    def _write_runtime(self, row: dict[str, Any]) -> None:
        write_json(self.root / "runtime.json", [row])

    def _activate_plan(self, plan: dict[str, Any]) -> None:
        target_id = str(plan.get("strategy_plan_id") or "")
        for active in self._all_active_plans():
            if str(active.get("strategy_plan_id") or "") == target_id:
                continue
            active["status"] = "superseded"
            self._write_plan(active)
        plan["status"] = "active"
        self._write_plan(plan)

    def _proposals_path(self, cycle_id: str) -> Path:
        return self.root / "proposals" / f"{cycle_id}.json"

    def _plans_path(self, cycle_id: str) -> Path:
        return self.root / "plans" / f"{cycle_id}.json"

    def _write_plan(self, plan: dict[str, Any]) -> None:
        path = self._plans_path(str(plan["cycle_id"]))
        rows = [row for row in load_json(path) if str(row.get("strategy_plan_id")) != str(plan.get("strategy_plan_id"))]
        rows.append(plan)
        write_json(path, rows)

    def _all_active_plans(self) -> list[dict[str, Any]]:
        folder = self.root / "plans"
        if not folder.exists():
            return []
        return [row for path in folder.glob("*.json") for row in load_json(path) if row.get("status") == "active"]


def normalize_proposal(payload: dict[str, Any], *, now: str | None = None, legacy: bool = False) -> dict[str, Any]:
    source = str(payload.get("source") or payload.get("author") or "").lower()
    if source not in {"human", "ai"}:
        raise ValueError("proposal source must be human or ai")
    cycle_id = str(payload.get("cycle_id") or "")
    if not cycle_id:
        raise ValueError("proposal cycle_id is required")
    proposal = {
        "schema_version": PROPOSAL_SCHEMA,
        "cycle_id": cycle_id,
        "source": source,
        "created_at": _timestamp(now or payload.get("locked_at")),
        "direction": str(payload.get("direction") or "neutral"),
        "style": str(payload.get("style") or "steady"),
        "range": dict(payload.get("range") or {}),
        "key_levels": list(payload.get("key_levels") or []),
        "grid": dict(payload.get("grid") or {"orders": list(payload.get("grid_orders") or [])}),
        "signal": dict(payload.get("signal") or {"confidence": payload.get("confidence")}),
        "tp_sl": dict(payload.get("tp_sl") or payload.get("bracket") or {}),
        "risk_budget": dict(payload.get("risk_budget") or {}),
        "intraday_rules": list(payload.get("intraday_rules") or payload.get("invalidation") or []),
        "legacy_status": str(payload.get("status") or ("locked" if payload.get("locked_at") else "")),
        "legacy": bool(legacy),
        "rationale": str(payload.get("rationale") or ""),
        "evidence_used": list(payload.get("evidence_used") or []),
        "analysis": dict(payload.get("analysis") or {}),
        "prompt_contract": dict(payload.get("prompt_contract") or {}),
        "evaluation_receipt": dict(payload.get("evaluation_receipt") or {}),
        "preview_id": payload.get("preview_id"),
    }
    proposal["proposal_id"] = str(payload.get("proposal_id") or _proposal_id(proposal))
    return proposal


def _field_sources(value: dict[str, str] | None, *, selected_source: str) -> dict[str, str]:
    sources = {key: selected_source for key in PLAN_FIELDS}
    for key, source in (value or {}).items():
        if key not in PLAN_FIELDS or source not in FIELD_SOURCES:
            raise ValueError("invalid field source")
        sources[key] = source
    return sources


def _proposal_id(proposal: dict[str, Any]) -> str:
    raw = json.dumps({key: proposal.get(key) for key in ("cycle_id", "source", *PLAN_FIELDS)}, sort_keys=True, separators=(",", ":"))
    return f"proposal-{proposal['source']}-{hashlib.sha256(raw.encode()).hexdigest()[:12]}"


def _plan_id(cycle_id: str, version: int, proposal_id: str) -> str:
    return f"strategy-plan-{cycle_id}-{version}-{hashlib.sha256(proposal_id.encode()).hexdigest()[:8]}"


def _timestamp(value: str | None) -> str:
    if value:
        return str(value)
    return datetime.now(timezone.utc).isoformat()


def _validate_market(market: dict[str, Any]) -> None:
    if market.get("status") not in {"ready", "derived"} or market.get("fresh") is not True:
        raise ValueError("market data is stale")
    if market.get("is_synthetic") is not False:
        raise ValueError("synthetic market data is forbidden")
    if not str(market.get("provider") or "").strip():
        raise ValueError("market provider is missing")
    _positive_number(market.get("latest_close"), "market latest_close")
    if len(market.get("bars") or []) < 15:
        raise ValueError("market history is insufficient for ATR grid planning")


def _strategy_bars(market: dict[str, Any], timeframe: str, period: int) -> list[dict[str, Any]]:
    contexts = market.get("strategy_timeframes")
    context = contexts.get(timeframe) if isinstance(contexts, dict) else None
    if not isinstance(context, dict):
        raise ValueError(f"strategy timeframe {timeframe} is unavailable")
    if context.get("is_synthetic") is not False:
        raise ValueError(f"strategy timeframe {timeframe} is synthetic")
    if not str(context.get("provider") or "").strip():
        raise ValueError(f"strategy timeframe {timeframe} provider is missing")
    bars = list(context.get("bars") or [])
    if len(bars) < period + 1:
        raise ValueError(f"strategy timeframe {timeframe} history is insufficient")
    return bars


def _average_true_range(bars: list[dict[str, Any]], period: int = 14) -> float:
    if len(bars) < period + 1:
        raise ValueError("market history is insufficient for ATR grid planning")
    true_ranges: list[float] = []
    previous_close: float | None = None
    for bar in bars[-(period + 1):]:
        high = _positive_number(bar.get("high"), "bar high")
        low = _positive_number(bar.get("low"), "bar low")
        close = _positive_number(bar.get("close"), "bar close")
        if high < low:
            raise ValueError("bar high is below bar low")
        if previous_close is not None:
            true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
        previous_close = close
    return sum(true_ranges[-period:]) / period


def _account_equity(account: dict[str, Any]) -> float:
    for key in ("equity", "ending_cash", "starting_cash"):
        value = _number_or(account.get(key), 0.0)
        if value > 0:
            return value
    return 10_000.0


def _positive_number(value: Any, label: str) -> float:
    parsed = _number_or(value, math.nan)
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{label} must be positive")
    return parsed


def _number_or(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return float(default)
    return parsed if math.isfinite(parsed) else float(default)


def _preview_id(preview: dict[str, Any]) -> str:
    raw = json.dumps({
        "cycle_id": preview.get("cycle_id"),
        "direction": preview.get("direction"),
        "style": preview.get("style"),
        "range": preview.get("range"),
        "grid": preview.get("grid"),
        "orders": preview.get("orders"),
    }, sort_keys=True, separators=(",", ":"))
    return f"grid-preview-{hashlib.sha256(raw.encode()).hexdigest()[:12]}"
