"""Single-production-plan control plane with a non-destructive DualTrack migration.

This module deliberately owns *selection and attribution*, not execution.  The
legacy human/machine files remain immutable source records and are exposed as
same-schema proposals until a production plan is locked.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.dualtrack_execution_adapter import build_configured_execution_engine_adapter
from services.dualtrack_config import dualtrack_config
from services.dualtrack_store import DualTrackPlanStore
from services.control_audit import append_control_event, build_control_event, read_last_control_event
from services.grid_sizing import (
    GRID_STYLES,
    build_grid_preview,
    validate_market as _validate_market,
    positive_number as _positive_number,
)
from services.journal_store import load_json, write_json


PROPOSAL_SCHEMA = "strategy-plan-proposal-v1"
PLAN_SCHEMA = "strategy-plan-v1"
PLAN_FIELDS = ("direction", "style", "range", "key_levels", "grid", "signal", "tp_sl", "risk_budget", "intraday_rules")
FIELD_SOURCES = {"human", "ai", "confirmed"}
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
                "last_control_event": self._last_control_event(),
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
            "last_control_event": self._last_control_event(),
        }

    def _last_control_event(self) -> dict[str, Any] | None:
        try:
            return read_last_control_event(self.output_root)
        except OSError:
            return None

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
        # Geometry and capital sizing stay pure and shared in grid_sizing;
        # the control plane owns only locking, persistence and runtime state.
        return build_grid_preview(cycle_id, payload, market=market, account=account, config=self.config)

    def control(
        self,
        cycle_id: str,
        action: str,
        payload: dict[str, Any] | None = None,
        *,
        market: dict[str, Any] | None = None,
        account: dict[str, Any] | None = None,
        now: str | None = None,
        actor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with _CONTROL_LOCK:
            try:
                result = self._control_locked(cycle_id, action, payload, market=market, account=account, now=now)
            except ValueError as exc:
                # A rejected mutation is still an operator action and must
                # stay attributable; the original rejection is re-raised.
                if str(action or "").lower() != "preview":
                    self._audit_control(cycle_id, action, payload, actor=actor, result="rejected", error=str(exc), now=now)
                raise
            if str(action or "").lower() != "preview":
                recorded = self._audit_control(cycle_id, action, payload, actor=actor, result="accepted", error=None, now=now)
                if isinstance(result, dict):
                    result = {**result, "audit_recorded": recorded}
            return result

    def _audit_control(
        self,
        cycle_id: str,
        action: str,
        payload: dict[str, Any] | None,
        *,
        actor: dict[str, Any] | None,
        result: str,
        error: str | None,
        now: str | None,
    ) -> bool:
        # An audit failure must never block or alter the control outcome;
        # callers surface it honestly via audit_recorded=false.
        try:
            event = build_control_event(
                cycle_id=cycle_id,
                action=action,
                actor=actor,
                payload=payload,
                result=result,
                error=error,
                runtime=self.runtime_state(cycle_id),
                now=_timestamp(now),
            )
            append_control_event(self.output_root, event)
            return True
        except OSError:
            return False

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
            adapter = build_configured_execution_engine_adapter(self.output_root, config=self.config)
            receipt = adapter.cancel_orders(
                cycle_id,
                ts=_timestamp(now),
                reason="operator_cancel_all",
            )
            execution_event = self._advance_selected_execution(
                adapter,
                cycle_id,
                market=market,
                now=now,
                identity="operator-cancel-all",
            )
            accepted_after = self._accepted_orders(cycle_id, adapter=adapter)
            if accepted_after:
                raise ValueError("paper cancel left accepted orders")
            reconciliation = adapter.reconcile(cycle_id)
            if reconciliation.get("status") != "ok":
                raise ValueError("paper ledger reconciliation failed")
            return {
                "action": action,
                "cancelled_orders": int(receipt.get("cancelled_order_count") or 0),
                "execution_receipt": receipt,
                "execution_event": execution_event,
                "reconciliation": reconciliation,
            }
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
        adapter = build_configured_execution_engine_adapter(self.output_root, config=self.config)
        try:
            receipts = self._submit_preview_orders(adapter, cycle_id, adjusted, preview, market=market, timestamp=timestamp)
            execution_event = self._advance_selected_execution(
                adapter,
                cycle_id,
                market=market,
                now=now,
                identity="strategy-start",
            )
            accepted = self._accepted_orders(cycle_id, adapter=adapter)
            if {str(row.get("order_id") or "") for row in accepted} != {
                str(row.get("order_id") or "") for row in receipts
            }:
                raise ValueError("paper start did not activate the complete grid")
            reconciliation = adapter.reconcile(cycle_id)
            if reconciliation.get("status") != "ok":
                raise ValueError("paper ledger reconciliation failed")
        except Exception as exc:
            cleanup_errors: list[str] = []
            try:
                self._cancel_pending(
                    cycle_id,
                    now=now,
                    strategy_plan_id=adjusted["strategy_plan_id"],
                    adapter=adapter,
                    reason="start_failed",
                )
            except Exception as cleanup_exc:
                cleanup_errors.append(f"cancel: {cleanup_exc}")
            try:
                self._advance_selected_execution(
                    adapter,
                    cycle_id,
                    market=market,
                    now=now,
                    identity="strategy-start-cleanup",
                )
            except Exception as cleanup_exc:
                cleanup_errors.append(f"advance: {cleanup_exc}")
            adjusted["status"] = "failed"
            current["status"] = "active"
            self._write_plan(adjusted)
            self._write_plan(current)
            failure_detail = str(exc)
            if cleanup_errors:
                failure_detail = f"{failure_detail}; start cleanup failed: {'; '.join(cleanup_errors)}"
            self._write_runtime({
                **starting,
                "desired_state": "stopped",
                "actual_state": "error",
                "updated_at": _timestamp(now),
                "last_error": failure_detail,
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
            "execution_event": execution_event,
            "reconciliation": reconciliation,
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

        old_orders = self._accepted_orders(cycle_id)
        old_order_ids = [str(row.get("order_id") or "") for row in old_orders if row.get("order_id")]
        old_accepted = len(old_order_ids)
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
        adapter = build_configured_execution_engine_adapter(self.output_root, config=self.config)
        receipts: list[dict[str, Any]] = []
        try:
            # Two-phase paper replacement: the old grid stays live until every
            # replacement order is accepted. A failed stage is removed without
            # pretending that an engine cancellation can be rolled back.
            receipts = self._submit_preview_orders(
                adapter,
                cycle_id,
                adjusted,
                preview,
                market=market,
                timestamp=timestamp,
            )
            cancel_receipt = adapter.cancel_orders(
                cycle_id,
                order_ids=old_order_ids,
                ts=_timestamp(now),
                reason="regrid",
            )
            cancelled = int(
                cancel_receipt.get("cancelled_order_count")
                or len(cancel_receipt.get("cancelled_order_ids") or [])
            )
            if cancelled != old_accepted:
                raise ValueError("regrid did not cancel every previous grid order")
            execution_event = self._advance_selected_execution(
                adapter,
                cycle_id,
                market=market,
                now=now,
                identity="strategy-regrid",
            )
            accepted_ids = {
                str(row.get("order_id") or "")
                for row in self._accepted_orders(cycle_id, adapter=adapter)
            }
            replacement_ids = {str(row.get("order_id") or "") for row in receipts}
            if accepted_ids != replacement_ids:
                raise ValueError("regrid did not leave exactly the replacement grid active")
            self._activate_plan(adjusted)
        except Exception as exc:
            cleanup_error = ""
            try:
                adapter.cancel_orders(
                    cycle_id,
                    strategy_plan_id=adjusted["strategy_plan_id"],
                    ts=_timestamp(now),
                    reason="regrid_stage_failed",
                )
                self._advance_selected_execution(
                    adapter,
                    cycle_id,
                    market=market,
                    now=now,
                    identity="strategy-regrid-cleanup",
                )
            except Exception as cleanup_exc:
                cleanup_error = str(cleanup_exc)
            adjusted["status"] = "failed"
            current["status"] = "active"
            self._write_plan(adjusted)
            self._write_plan(current)
            accepted_after_failure = len(self._accepted_orders(cycle_id))
            failure_detail = str(exc)
            if cleanup_error:
                failure_detail = f"{failure_detail}; regrid cleanup failed: {cleanup_error}"
            self._write_runtime({
                **runtime,
                "actual_state": "running" if not cleanup_error and accepted_after_failure >= old_accepted else "error",
                "updated_at": _timestamp(now),
                "last_action": "adjust_plan",
                "last_error": failure_detail,
                "accepted_order_count": accepted_after_failure,
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
            "execution_event": execution_event,
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
        adapter = build_configured_execution_engine_adapter(self.output_root, config=self.config)
        cancelled = self._cancel_pending(
            cycle_id,
            now=now,
            adapter=adapter,
            reason="stop",
        )
        snapshot = adapter.snapshot(cycle_id)
        open_positions = [row for row in snapshot.get("positions") or [] if row.get("status") == "open"]
        flattened: list[dict[str, Any]] = []
        execution_event: dict[str, Any] | None = None
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
                        "target_position_side": position.get("side"),
                        "target_entry_price": position.get("entry_price"),
                        "symbol": position.get("symbol") or str((market or {}).get("symbol") or "GOLD"),
                        "source": "strategy_production_console",
                        "source_fill_id": f"strategy-stop:{cycle_id}:{position.get('trade_id')}",
                        "strategy_plan_id": previous.get("strategy_plan_id"),
                        "strategy_plan_version": previous.get("strategy_plan_version"),
                    }))
            if market is not None:
                _validate_market(market)
                price = _positive_number(market.get("latest_close"), "market latest_close")
                timestamp = _timestamp(now)
                execution_event = adapter.process_market_event({
                    "schema_version": "dualtrack-market-event-v1",
                    "event_id": f"strategy-stop:{cycle_id}:{timestamp}",
                    "cycle_id": cycle_id,
                    "ts_event": timestamp,
                    "event_started_at": market.get("latest_timestamp"),
                    "source": str(market.get("provider") or market.get("source_mode") or ""),
                    "provider": str(market.get("provider") or ""),
                    "instrument_id": str(
                        ((self.config.get("execution_shadow") or {}).get("nautilus") or {}).get(
                            "execution_instrument_id"
                        )
                        or market.get("symbol")
                        or ""
                    ),
                    "symbol": str(market.get("symbol") or "GOLD"),
                    "timeframe": str(market.get("timeframe") or "1m"),
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "price": price,
                    "fresh": True,
                    "is_synthetic": False,
                })
                flush_shadow = getattr(adapter, "flush_shadow", None)
                if callable(flush_shadow):
                    flush_shadow(cycle_id)
            elif adapter.name == "nautilus_paper":
                raise ValueError("Nautilus stop requires a fresh trusted market mark")
            terminal = adapter.snapshot(cycle_id)
            if [row for row in terminal.get("orders") or [] if row.get("state") == "accepted"]:
                raise ValueError("paper stop left accepted orders")
            if [row for row in terminal.get("positions") or [] if row.get("status") == "open"]:
                raise ValueError("paper stop left open positions")
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
            "execution_event": execution_event,
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

    def _accepted_orders(self, cycle_id: str, *, adapter=None) -> list[dict[str, Any]]:
        execution = adapter or build_configured_execution_engine_adapter(self.output_root, config=self.config)
        snapshot = execution.snapshot(cycle_id)
        return [
            row
            for row in snapshot.get("orders") or []
            if str(row.get("state") or "").lower() == "accepted"
        ]

    def _advance_selected_execution(
        self,
        adapter,
        cycle_id: str,
        *,
        market: dict[str, Any] | None,
        now: str | None,
        identity: str,
    ) -> dict[str, Any] | None:
        """Make accepted mutations observable before an active Nautilus control returns."""

        if str(getattr(adapter, "name", "")) != "nautilus_paper":
            return None
        _validate_market(market or {})
        price = _positive_number((market or {}).get("latest_close"), "market latest_close")
        timestamp = _timestamp(now)
        return adapter.process_market_event({
            "schema_version": "dualtrack-market-event-v1",
            "event_id": f"{identity}:{cycle_id}:{timestamp}",
            "cycle_id": cycle_id,
            "ts_event": timestamp,
            "event_started_at": (market or {}).get("latest_timestamp"),
            "source": str((market or {}).get("provider") or (market or {}).get("source_mode") or ""),
            "provider": str((market or {}).get("provider") or ""),
            "instrument_id": str(
                ((self.config.get("execution_shadow") or {}).get("nautilus") or {}).get(
                    "execution_instrument_id"
                )
                or (market or {}).get("symbol")
                or ""
            ),
            "symbol": str((market or {}).get("symbol") or "GOLD"),
            "timeframe": str((market or {}).get("timeframe") or "1m"),
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "price": price,
            "fresh": True,
            "is_synthetic": False,
        })

    def _cancel_pending(
        self,
        cycle_id: str,
        *,
        now: str | None,
        strategy_plan_id: str | None = None,
        adapter=None,
        reason: str = "",
    ) -> int:
        execution = adapter or build_configured_execution_engine_adapter(self.output_root, config=self.config)
        receipt = execution.cancel_orders(
            cycle_id,
            strategy_plan_id=strategy_plan_id,
            ts=_timestamp(now),
            reason=reason,
        )
        return int(receipt.get("cancelled_order_count") or len(receipt.get("cancelled_order_ids") or []))

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
