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
    trusted_account_equity,
)
from services.grid_range_adjustment import (
    build_range_extension,
    range_adjustment_steps,
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
        version = self._next_plan_version(cycle_id)
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

    def latest_plan(self, cycle_id: str) -> dict[str, Any] | None:
        """Return the latest archived plan for a cycle, regardless of active status."""
        plans = [row for row in load_json(self._plans_path(cycle_id)) if isinstance(row, dict)]
        if not plans:
            return None
        return max(plans, key=lambda row: (int(row.get("version") or 0), str(row.get("locked_at") or "")))

    def _next_plan_version(self, cycle_id: str) -> int:
        plans = [row for row in load_json(self._plans_path(cycle_id)) if isinstance(row, dict)]
        return max((int(row.get("version") or 0) for row in plans), default=0) + 1

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

    def persisted_runtime_state(self) -> dict[str, Any]:
        """Return the real stored runtime row without current-cycle masking."""
        rows = load_json(self.root / "runtime.json")
        row = rows[-1] if rows else {}
        return {
            **row,
            "cycle_id": str(row.get("cycle_id") or ""),
            "desired_state": str(row.get("desired_state") or "stopped"),
            "actual_state": str(row.get("actual_state") or row.get("desired_state") or "stopped"),
            "accepted_order_count": int(row.get("accepted_order_count") or 0),
        }

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
            except Exception as exc:
                if str(action or "").lower() != "preview":
                    self._audit_control(cycle_id, action, payload, actor=actor, result="failed", error=str(exc), now=now)
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
        if action == "extend_range":
            return self._extend_range(cycle_id, body, market=market or {}, account=account or {}, now=now)
        if action == "replace_grid":
            return self._replace_grid(cycle_id, body, market=market or {}, account=account or {}, now=now)
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
            adjusted = {
                **current,
                "version": self._next_plan_version(cycle_id),
                "status": "active",
                "locked_at": _timestamp(now),
            }
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
        replacement_request: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = self.active_plan(cycle_id) or self.ensure_compatible_active_plan(cycle_id, as_of=now)
        if not current:
            raise ValueError("cannot start without an active StrategyPlan")
        trusted_account_equity(account)
        preview = self.preview(cycle_id, body, market=market, account=account)
        if (preview.get("risk") or {}).get("risk_budget_exceeded"):
            raise ValueError("risk_budget_exceeded")
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
        if replacement_request:
            adjusted["replacement_request"] = dict(replacement_request)
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
            submitted_ids = {str(row.get("order_id") or "") for row in receipts}
            accepted_before_market = self._accepted_orders(cycle_id, adapter=adapter)
            if {str(row.get("order_id") or "") for row in accepted_before_market} != submitted_ids:
                raise ValueError("paper start did not accept the complete grid")
            execution_event = self._advance_selected_execution(
                adapter,
                cycle_id,
                market=market,
                now=now,
                identity="strategy-start",
            )
            terminal_orders = {
                str(row.get("order_id") or ""): row
                for row in adapter.snapshot(cycle_id).get("orders") or []
            }
            missing_ids = sorted(submitted_ids - set(terminal_orders))
            invalid_states = sorted(
                order_id
                for order_id in submitted_ids & set(terminal_orders)
                if str(terminal_orders[order_id].get("state") or "").lower()
                not in {"accepted", "filled"}
            )
            if missing_ids or invalid_states:
                raise ValueError(
                    "paper start lost submitted grid orders"
                    f"; missing={missing_ids}; invalid_states={invalid_states}"
                )
            accepted = self._accepted_orders(cycle_id, adapter=adapter)
            filled_count = sum(
                1
                for order_id in submitted_ids
                if str(terminal_orders[order_id].get("state") or "").lower() == "filled"
            )
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
                cleanup_snapshot = adapter.snapshot(cycle_id)
                cleanup_price = _positive_number(market.get("latest_close"), "market latest_close")
                for position in cleanup_snapshot.get("positions") or []:
                    if str(position.get("status") or "") != "open":
                        continue
                    if str(position.get("strategy_plan_id") or "") != adjusted["strategy_plan_id"]:
                        continue
                    close_side = "sell" if str(position.get("side") or "") in {"buy", "long"} else "buy"
                    adapter.submit_order({
                        "cycle_id": cycle_id,
                        "ts": _timestamp(now),
                        "side": close_side,
                        "event": "flatten",
                        "order_type": "market",
                        "price": cleanup_price,
                        "market_price": cleanup_price,
                        "trade_id": position.get("trade_id"),
                        "position_id": position.get("position_id"),
                        "target_position_side": position.get("side"),
                        "target_entry_price": position.get("entry_price"),
                        "symbol": position.get("symbol") or str(market.get("symbol") or "GOLD"),
                        "source": "strategy_production_console",
                        "source_fill_id": f"strategy-start-cleanup:{cycle_id}:{position.get('trade_id')}",
                        "strategy_plan_id": adjusted["strategy_plan_id"],
                        "strategy_plan_version": adjusted["version"],
                    })
                self._advance_selected_execution(
                    adapter,
                    cycle_id,
                    market=market,
                    now=now,
                    identity="strategy-start-cleanup",
                )
                cleanup_terminal = adapter.snapshot(cycle_id)
                remaining_orders = [
                    row for row in cleanup_terminal.get("orders") or []
                    if str(row.get("state") or "").lower() == "accepted"
                    and str(row.get("strategy_plan_id") or "") == adjusted["strategy_plan_id"]
                ]
                remaining_positions = [
                    row for row in cleanup_terminal.get("positions") or []
                    if str(row.get("status") or "").lower() == "open"
                    and str(row.get("strategy_plan_id") or "") == adjusted["strategy_plan_id"]
                ]
                if remaining_orders or remaining_positions:
                    raise RuntimeError(
                        "start cleanup left live paper state"
                        f"; accepted_orders={len(remaining_orders)}"
                        f"; open_positions={len(remaining_positions)}"
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
            accepted_after_cleanup = len(self._accepted_orders(cycle_id, adapter=adapter))
            self._write_runtime({
                **starting,
                "desired_state": "stopped",
                "actual_state": "error",
                "updated_at": _timestamp(now),
                "last_error": failure_detail,
                "accepted_order_count": accepted_after_cleanup,
            })
            raise

        running = {
            **starting,
            "actual_state": "running",
            "updated_at": _timestamp(now),
            "accepted_order_count": len(accepted),
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
            "accepted_orders": len(accepted),
            "filled_orders": filled_count,
            "idempotent": False,
        }

    def _extend_range(
        self,
        cycle_id: str,
        body: dict[str, Any],
        *,
        market: dict[str, Any],
        account: dict[str, Any],
        now: str | None,
    ) -> dict[str, Any]:
        expected_plan_id = str(body.get("expected_strategy_plan_id") or "")
        runtime = self.runtime_state(cycle_id)
        current = self.active_plan(cycle_id)
        if runtime.get("desired_state") != "running" or runtime.get("actual_state") != "running":
            raise ValueError("strategy_not_running")
        if not current or str(runtime.get("strategy_plan_id") or "") != str(current.get("strategy_plan_id") or ""):
            raise ValueError("strategy_plan_changed")
        requested_range = body.get("range") if isinstance(body.get("range"), dict) else {}
        if requested_range.get("low") in (None, "") or requested_range.get("high") in (None, ""):
            raise ValueError("requested_range_missing")
        requested_range = {
            "low": _positive_number(requested_range.get("low"), "requested grid low"),
            "high": _positive_number(requested_range.get("high"), "requested grid high"),
        }
        request_fingerprint = _extend_range_request_fingerprint(expected_plan_id, requested_range)
        current_plan_id = str(current.get("strategy_plan_id") or "")
        if not expected_plan_id:
            raise ValueError("strategy_plan_changed")
        if expected_plan_id != current_plan_id:
            prior_adjustment = (
                dict(current.get("range_adjustment") or {})
                if isinstance(current.get("range_adjustment"), dict)
                else {}
            )
            if (
                str(prior_adjustment.get("from_plan_id") or "") == expected_plan_id
                and str(prior_adjustment.get("request_fingerprint") or "") == request_fingerprint
            ):
                return {
                    "action": "extend_range",
                    "runtime": runtime,
                    "plan": current,
                    "effective_range": dict(
                        prior_adjustment.get("effective_range") or current.get("range") or {}
                    ),
                    "steps": dict(prior_adjustment.get("steps") or {"low": 0, "high": 0}),
                    "created_orders": 0,
                    "cancelled_orders": 0,
                    "positions_preserved": True,
                    "tp_sl_affected": False,
                    "idempotent": True,
                }
            raise ValueError("strategy_plan_changed")
        steps = range_adjustment_steps(current, requested_range)
        if not steps["low"] and not steps["high"]:
            return {
                "action": "extend_range",
                "runtime": runtime,
                "plan": current,
                "effective_range": {
                    "low": float((current.get("range") or {}).get("low")),
                    "high": float((current.get("range") or {}).get("high")),
                },
                "steps": steps,
                "created_orders": 0,
                "cancelled_orders": 0,
                "positions_preserved": True,
                "tp_sl_affected": False,
                "idempotent": True,
            }

        adapter = build_configured_execution_engine_adapter(self.output_root, config=self.config)
        before = adapter.snapshot(cycle_id)
        accepted_before = [
            row
            for row in before.get("orders") or []
            if str(row.get("state") or "").lower() == "accepted"
        ]
        accepted_entries = [
            row for row in accepted_before if str(row.get("event") or "entry").lower() == "entry"
        ]
        filled_entries = [
            dict(row)
            for row in before.get("orders") or []
            if str(row.get("state") or "").lower() == "filled"
            and str(row.get("event") or "entry").lower() == "entry"
        ]
        filled_entries.extend(
            dict(row)
            for row in before.get("fills") or []
            if str(row.get("event") or "entry").lower() == "entry"
        )
        protection_before = [
            dict(row) for row in accepted_before if str(row.get("event") or "entry").lower() != "entry"
        ]
        positions_before = [dict(row) for row in before.get("positions") or []]
        protection_before_core = _protection_core(protection_before)
        positions_before_core = _position_core(positions_before)
        ancestor_ids = {str(value) for value in current.get("inherited_plan_ids") or []}
        historical_plan_orders = [
            {**dict(order), "_strategy_plan_id": str(plan.get("strategy_plan_id") or "")}
            for plan in load_json(self._plans_path(cycle_id))
            if str(plan.get("strategy_plan_id") or "") in ancestor_ids
            for order in (plan.get("grid") or {}).get("orders") or []
        ]
        adjustment = build_range_extension(
            cycle_id,
            current,
            requested_range,
            market=market,
            account=account,
            config=self.config,
            accepted_entries=accepted_entries,
            positions=positions_before,
            filled_entries=filled_entries,
            historical_plan_orders=historical_plan_orders,
        )
        if not adjustment["changed"]:
            return {
                "action": "extend_range",
                "runtime": runtime,
                "plan": current,
                "effective_range": adjustment["effective_range"],
                "steps": adjustment["steps"],
                "created_orders": 0,
                "cancelled_orders": 0,
                "positions_preserved": True,
                "tp_sl_affected": False,
                "idempotent": True,
            }

        version = self._next_plan_version(cycle_id)
        adjusted = {
            **current,
            "strategy_plan_id": _plan_id(cycle_id, version, f"extend-range:{adjustment['preview_id']}"),
            "version": version,
            "status": "active",
            "locked_at": _timestamp(now),
            "range": {**dict(current.get("range") or {}), **adjustment["effective_range"]},
            "grid": adjustment["grid"],
            "risk_budget": {
                **dict(current.get("risk_budget") or {}),
                "leverage": adjustment["grid"]["leverage"],
                "max_loss": adjustment["risk"]["max_loss"],
                "estimated_margin": adjustment["risk"]["estimated_margin"],
            },
            "field_sources": {
                **dict(current.get("field_sources") or {}),
                "range": "confirmed",
                "grid": "confirmed",
                "risk_budget": "confirmed",
            },
            "preview_id": adjustment["preview_id"],
            "range_adjustment": {
                "from_plan_id": current_plan_id,
                "requested_range": dict(requested_range),
                "request_fingerprint": request_fingerprint,
                "effective_range": dict(adjustment["effective_range"]),
                "steps": dict(adjustment["steps"]),
            },
            "inherited_plan_ids": list(dict.fromkeys([
                *list(current.get("inherited_plan_ids") or []),
                str(current["strategy_plan_id"]),
            ])),
        }
        effective_low = float(adjustment["effective_range"]["low"])
        effective_high = float(adjustment["effective_range"]["high"])
        internal_entry_ids = {
            str(row.get("order_id") or "")
            for row in accepted_entries
            if effective_low - 1e-8 <= float(row.get("price") or 0.0) <= effective_high + 1e-8
            and row.get("order_id")
        }
        internal_entry_core = _entry_core([
            dict(row)
            for row in accepted_entries
            if str(row.get("order_id") or "") in internal_entry_ids
        ])
        outside_entry_ids = [
            str(row.get("order_id") or "")
            for row in accepted_entries
            if not effective_low - 1e-8 <= float(row.get("price") or 0.0) <= effective_high + 1e-8
            and row.get("order_id")
        ]
        timestamp = _timestamp(now)
        receipts: list[dict[str, Any]] = []
        cancelled = 0
        cancellation_started = False
        cleanup_error = ""
        try:
            receipts = self._submit_preview_orders(
                adapter,
                cycle_id,
                adjusted,
                {
                    "orders": adjustment["edge_orders"],
                    "market": {"price": _positive_number(market.get("latest_close"), "market latest_close")},
                },
                market=market,
                timestamp=timestamp,
            )
            staged_ids = {str(row.get("order_id") or "") for row in receipts if row.get("order_id")}
            staged_accepted = {
                str(row.get("order_id") or "")
                for row in self._accepted_orders(cycle_id, adapter=adapter, kind="entry")
                if str(row.get("strategy_plan_id") or "") == adjusted["strategy_plan_id"]
            }
            if staged_accepted != staged_ids:
                raise ValueError("extend_range_stage_incomplete")

            if outside_entry_ids:
                cancellation_started = True
                cancel_receipt = adapter.cancel_orders(
                    cycle_id,
                    order_ids=outside_entry_ids,
                    ts=timestamp,
                    reason="extend_range_outside",
                )
                cancelled = int(
                    cancel_receipt.get("cancelled_order_count")
                    or len(cancel_receipt.get("cancelled_order_ids") or [])
                )
                if cancelled != len(outside_entry_ids):
                    raise ValueError("extend_range_cancel_incomplete")

            execution_event = self._sync_execution_mutations(
                adapter,
                cycle_id,
                market=market,
                now=now,
                identity="strategy-extend-range",
            )
            terminal = adapter.snapshot(cycle_id)
            terminal_accepted = [
                row
                for row in terminal.get("orders") or []
                if str(row.get("state") or "").lower() == "accepted"
            ]
            terminal_entry_ids = {
                str(row.get("order_id") or "")
                for row in terminal_accepted
                if str(row.get("event") or "entry").lower() == "entry"
            }
            if not internal_entry_ids.issubset(terminal_entry_ids):
                raise RuntimeError("extend_range changed an internal entry order")
            terminal_internal_entry_core = _entry_core([
                dict(row)
                for row in terminal_accepted
                if str(row.get("event") or "entry").lower() == "entry"
                and str(row.get("order_id") or "") in internal_entry_ids
            ])
            if terminal_internal_entry_core != internal_entry_core:
                raise RuntimeError("extend_range changed internal entry price or quantity")
            if set(outside_entry_ids) & terminal_entry_ids:
                raise RuntimeError("extend_range left an out-of-range entry order")
            terminal_orders = {
                str(row.get("order_id") or ""): row for row in terminal.get("orders") or []
            }
            invalid_staged = [
                order_id
                for order_id in staged_ids
                if order_id not in terminal_orders
                or str(terminal_orders[order_id].get("state") or "").lower() not in {"accepted", "filled"}
            ]
            if invalid_staged:
                raise RuntimeError(f"extend_range lost staged orders: {sorted(invalid_staged)}")
            protection_after = [
                dict(row)
                for row in terminal_accepted
                if str(row.get("event") or "entry").lower() != "entry"
            ]
            if _protection_core(protection_after) != protection_before_core:
                raise RuntimeError("extend_range changed protection orders")
            if _position_core([dict(row) for row in terminal.get("positions") or []]) != positions_before_core:
                raise RuntimeError("extend_range changed open positions")
            reconciliation = adapter.reconcile(cycle_id)
            if reconciliation.get("status") != "ok":
                raise ValueError("paper ledger reconciliation failed")
            self._activate_plan(adjusted)
            accepted_after = self._accepted_orders(cycle_id, adapter=adapter)
            running = {
                **runtime,
                "desired_state": "running",
                "actual_state": "running",
                "updated_at": _timestamp(now),
                "last_action": "extend_range",
                "last_error": None,
                "strategy_plan_id": adjusted["strategy_plan_id"],
                "strategy_plan_version": adjusted["version"],
                "preview_id": adjustment["preview_id"],
                "accepted_order_count": len(accepted_after),
            }
            self._write_runtime(running)
        except Exception as exc:
            try:
                adapter.cancel_orders(
                    cycle_id,
                    strategy_plan_id=adjusted["strategy_plan_id"],
                    ts=timestamp,
                    reason="extend_range_stage_failed",
                )
                cleanup_snapshot = adapter.snapshot(cycle_id)
                staged_positions = [
                    row
                    for row in cleanup_snapshot.get("positions") or []
                    if str(row.get("status") or "").lower() == "open"
                    and str(row.get("strategy_plan_id") or "") == adjusted["strategy_plan_id"]
                ]
                cleanup_price = _positive_number(market.get("latest_close"), "market latest_close")
                for position in staged_positions:
                    close_side = (
                        "sell" if str(position.get("side") or "").lower() in {"buy", "long"}
                        else "buy"
                    )
                    adapter.submit_order({
                        "cycle_id": cycle_id,
                        "ts": timestamp,
                        "side": close_side,
                        "event": "flatten",
                        "order_type": "market",
                        "price": cleanup_price,
                        "market_price": cleanup_price,
                        "trade_id": position.get("trade_id"),
                        "position_id": position.get("position_id"),
                        "target_position_side": position.get("side"),
                        "target_entry_price": position.get("entry_price"),
                        "symbol": position.get("symbol") or str(market.get("symbol") or "GOLD"),
                        "source": "strategy_production_console",
                        "source_fill_id": (
                            f"strategy-extend-range-cleanup:{cycle_id}:"
                            f"{position.get('trade_id') or position.get('position_id')}"
                        ),
                        "strategy_plan_id": adjusted["strategy_plan_id"],
                        "strategy_plan_version": adjusted["version"],
                    })
                self._sync_execution_mutations(
                    adapter,
                    cycle_id,
                    market=market,
                    now=now,
                    identity="strategy-extend-range-cleanup",
                )
                cleanup_terminal = adapter.snapshot(cycle_id)
                remaining_staged_orders = [
                    row
                    for row in cleanup_terminal.get("orders") or []
                    if str(row.get("state") or "").lower() == "accepted"
                    if str(row.get("strategy_plan_id") or "") == adjusted["strategy_plan_id"]
                ]
                remaining_staged_positions = [
                    row
                    for row in cleanup_terminal.get("positions") or []
                    if str(row.get("status") or "").lower() == "open"
                    and str(row.get("strategy_plan_id") or "") == adjusted["strategy_plan_id"]
                ]
                if remaining_staged_orders or remaining_staged_positions:
                    raise RuntimeError("extend_range cleanup left staged live state")
                cleanup_protection = [
                    dict(row)
                    for row in cleanup_terminal.get("orders") or []
                    if str(row.get("state") or "").lower() == "accepted"
                    and str(row.get("event") or "entry").lower() != "entry"
                ]
                if _protection_core(cleanup_protection) != protection_before_core:
                    raise RuntimeError("extend_range cleanup changed protection orders")
                if _position_core([
                    dict(row) for row in cleanup_terminal.get("positions") or []
                ]) != positions_before_core:
                    raise RuntimeError("extend_range cleanup changed prior positions")
            except Exception as cleanup_exc:
                cleanup_error = str(cleanup_exc)
            adjusted["status"] = "failed"
            current["status"] = "active"
            self._write_plan(adjusted)
            self._write_plan(current)
            failure_detail = str(exc)
            if cleanup_error:
                failure_detail = f"{failure_detail}; extend_range cleanup failed: {cleanup_error}"
            self._write_runtime({
                **runtime,
                "actual_state": "error" if cancellation_started or cleanup_error else "running",
                "updated_at": _timestamp(now),
                "last_action": "extend_range",
                "last_error": failure_detail,
                "accepted_order_count": len(self._accepted_orders(cycle_id, adapter=adapter)),
            })
            raise
        return {
            "action": "extend_range",
            "runtime": running,
            "plan": adjusted,
            "effective_range": adjustment["effective_range"],
            "steps": adjustment["steps"],
            "created_orders": len(receipts),
            "cancelled_orders": cancelled,
            "positions_preserved": True,
            "tp_sl_affected": False,
            "execution_event": execution_event,
            "reconciliation": reconciliation,
            "idempotent": False,
        }

    def _replace_grid(
        self,
        cycle_id: str,
        body: dict[str, Any],
        *,
        market: dict[str, Any],
        account: dict[str, Any],
        now: str | None,
    ) -> dict[str, Any]:
        expected_plan_id = str(body.get("expected_strategy_plan_id") or "")
        expected_preview_id = str(body.get("expected_preview_id") or "")
        requested_preview = body.get("preview") if isinstance(body.get("preview"), dict) else {}
        if not expected_plan_id:
            raise ValueError("strategy_plan_changed")
        if not expected_preview_id:
            raise ValueError("strategy_preview_changed")
        if str(requested_preview.get("preview_id") or "") != expected_preview_id:
            raise ValueError("strategy_preview_changed")
        runtime = self.runtime_state(cycle_id)
        current = self.active_plan(cycle_id)
        if runtime.get("desired_state") != "running" or runtime.get("actual_state") != "running":
            raise ValueError("strategy_not_running")
        if (
            current
            and str(current.get("preview_id") or "") == expected_preview_id
            and str(runtime.get("preview_id") or "") == expected_preview_id
            and str(runtime.get("strategy_plan_id") or "") == str(current.get("strategy_plan_id") or "")
        ):
            expected_execution_sets = self._expected_execution_sets(body.get("expected_execution"))
            idempotent_payload = self._replacement_payload(
                current,
                requested_preview,
                risk_recalculated=body.get("risk_recalculated") is True,
            )
            current_range = dict(current.get("range") or {})
            if (
                abs(float((idempotent_payload.get("range") or {}).get("low") or 0.0) - float(current_range.get("low") or 0.0)) > 1e-8
                or abs(float((idempotent_payload.get("range") or {}).get("high") or 0.0) - float(current_range.get("high") or 0.0)) > 1e-8
            ):
                raise ValueError("strategy_preview_changed")
            retry_fingerprint = _replace_grid_request_fingerprint(
                expected_plan_id,
                requested_preview,
                risk_recalculated=body.get("risk_recalculated") is True,
                expected_execution=expected_execution_sets,
            )
            prior_replacement = (
                dict(current.get("replacement_request") or {})
                if isinstance(current.get("replacement_request"), dict)
                else {}
            )
            same_current_plan = expected_plan_id == str(current.get("strategy_plan_id") or "")
            direct_retry = (
                str(prior_replacement.get("from_plan_id") or "") == expected_plan_id
                and str(prior_replacement.get("request_fingerprint") or "") == retry_fingerprint
            )
            if not same_current_plan and not direct_retry:
                raise ValueError("strategy_plan_changed")
            if direct_retry and str(runtime.get("last_action") or "") != "replace_grid":
                runtime = {
                    **runtime,
                    "updated_at": _timestamp(now),
                    "last_action": "replace_grid",
                    "last_error": None,
                }
                self._write_runtime(runtime)
            return {
                "action": "replace_grid",
                "runtime": runtime,
                "plan": current,
                "preview": body.get("preview"),
                "stopped": False,
                "cancelled_orders": 0,
                "flattened_positions": 0,
                "created_orders": 0,
                "accepted_orders": len(self._accepted_orders(cycle_id, kind="entry")),
                "idempotent": True,
            }
        if (
            not expected_plan_id
            or not current
            or expected_plan_id != str(current.get("strategy_plan_id") or "")
            or str(runtime.get("strategy_plan_id") or "") != str(current.get("strategy_plan_id") or "")
        ):
            raise ValueError("strategy_plan_changed")

        risk_recalculated = body.get("risk_recalculated") is True
        trusted_account_equity(account)
        replacement_payload = self._replacement_payload(
            current,
            requested_preview,
            risk_recalculated=risk_recalculated,
        )
        try:
            latest_preview = self.preview(cycle_id, replacement_payload, market=market, account=account)
        except ValueError as exc:
            if "notional_per_grid" in str(exc) and "safe cap" in str(exc):
                raise ValueError("risk_budget_exceeded") from exc
            raise
        if latest_preview["preview_id"] != expected_preview_id:
            raise ValueError("strategy_preview_changed")
        requested_notional = float(replacement_payload["grid"]["notional_per_grid"])
        current_notional = float((current.get("grid") or {}).get("notional_per_grid") or 0.0)
        latest_risk = dict(latest_preview.get("risk") or {})
        latest_risk_cap = float(
            latest_risk.get("safe_notional_cap_per_grid")
            or latest_risk.get("risk_notional_cap_per_grid")
            or 0.0
        )
        if latest_risk.get("risk_budget_exceeded"):
            raise ValueError("risk_budget_exceeded")
        if requested_notional < current_notional - 1e-8:
            if not risk_recalculated or latest_risk_cap <= 0 or requested_notional > latest_risk_cap + 1e-8:
                raise ValueError("risk_budget_exceeded")
        latest_price = _positive_number(market.get("latest_close"), "market latest_close")
        if not float(latest_preview["range"]["low"]) <= latest_price <= float(latest_preview["range"]["high"]):
            raise ValueError("market_outside_requested_range")
        self._assert_expected_execution(cycle_id, body.get("expected_execution"))
        expected_execution_sets = self._expected_execution_sets(body.get("expected_execution"))
        request_fingerprint = _replace_grid_request_fingerprint(
            expected_plan_id,
            requested_preview,
            risk_recalculated=risk_recalculated,
            expected_execution=expected_execution_sets,
        )

        try:
            stopped = self._stop(cycle_id, market=market, now=now)
            started = self._start(
                cycle_id,
                replacement_payload,
                market=market,
                account=account,
                now=now,
                replacement_request={
                    "from_plan_id": expected_plan_id,
                    "requested_preview_id": expected_preview_id,
                    "request_fingerprint": request_fingerprint,
                },
            )
        except Exception:
            failed_runtime = self.runtime_state(cycle_id)
            self._write_runtime({
                **failed_runtime,
                "updated_at": _timestamp(now),
                "last_action": "replace_grid",
            })
            raise
        running = {
            **dict(started["runtime"]),
            "updated_at": _timestamp(now),
            "last_action": "replace_grid",
            "last_error": None,
        }
        self._write_runtime(running)
        return {
            "action": "replace_grid",
            "runtime": running,
            "plan": started["plan"],
            "preview": latest_preview,
            "stopped": True,
            "cancelled_orders": int(stopped.get("cancelled_orders") or 0),
            "flattened_positions": int(stopped.get("flattened_positions") or 0),
            "created_orders": int(started.get("created_orders") or 0),
            "accepted_orders": int(started.get("accepted_orders") or 0),
            "idempotent": False,
        }

    def _assert_expected_execution(self, cycle_id: str, expected: Any) -> None:
        expected_orders, expected_positions = self._expected_execution_sets(expected)

        adapter = build_configured_execution_engine_adapter(self.output_root, config=self.config)
        snapshot = adapter.snapshot(cycle_id)
        accepted = [
            row
            for row in snapshot.get("orders") or []
            if str(row.get("state") or "").lower() == "accepted"
        ]
        open_positions = [
            row
            for row in snapshot.get("positions") or []
            if str(row.get("status") or "").lower() == "open"
        ]
        current_orders = {
            str(
                row.get("order_id")
                or row.get("command_id")
                or row.get("authoritative_order_id")
                or ""
            )
            for row in accepted
        }
        current_positions = {
            str(row.get("position_id") or row.get("trade_id") or "")
            for row in open_positions
        }
        if (
            "" in current_orders
            or "" in current_positions
            or current_orders != expected_orders
            or current_positions != expected_positions
        ):
            raise ValueError("execution_state_changed")

    @staticmethod
    def _expected_execution_sets(expected: Any) -> tuple[set[str], set[str]]:
        if not isinstance(expected, dict):
            raise ValueError("execution_state_changed")
        accepted_order_ids = expected.get("accepted_order_ids")
        open_position_ids = expected.get("open_position_ids")
        if not isinstance(accepted_order_ids, list) or not isinstance(open_position_ids, list):
            raise ValueError("execution_state_changed")
        expected_orders = {str(value) for value in accepted_order_ids if str(value)}
        expected_positions = {str(value) for value in open_position_ids if str(value)}
        if len(expected_orders) != len(accepted_order_ids) or len(expected_positions) != len(open_position_ids):
            raise ValueError("execution_state_changed")
        return expected_orders, expected_positions

    def _require_running_plan(
        self,
        cycle_id: str,
        *,
        expected_strategy_plan_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        runtime = self.runtime_state(cycle_id)
        current = self.active_plan(cycle_id)
        if runtime.get("desired_state") != "running" or runtime.get("actual_state") != "running":
            raise ValueError("strategy_not_running")
        if (
            not expected_strategy_plan_id
            or not current
            or expected_strategy_plan_id != str(current.get("strategy_plan_id") or "")
            or str(runtime.get("strategy_plan_id") or "") != str(current.get("strategy_plan_id") or "")
        ):
            raise ValueError("strategy_plan_changed")
        return runtime, current

    @staticmethod
    def _replacement_payload(
        current: dict[str, Any],
        requested_preview: dict[str, Any],
        *,
        risk_recalculated: bool,
    ) -> dict[str, Any]:
        requested_grid = dict(requested_preview.get("grid") or {})
        current_grid = dict(current.get("grid") or {})
        current_notional = _positive_number(current_grid.get("notional_per_grid"), "current notional_per_grid")
        requested_notional = _positive_number(
            requested_grid.get("notional_per_grid"),
            "requested notional_per_grid",
        )
        requested_risk = dict(requested_preview.get("risk") or {})
        requested_risk_cap = float(
            requested_risk.get("safe_notional_cap_per_grid")
            or requested_risk.get("risk_notional_cap_per_grid")
            or 0.0
        )
        immutable_matches = (
            str(requested_preview.get("direction") or "") == str(current.get("direction") or "")
            and str(requested_preview.get("style") or "") == str(current.get("style") or "")
            and str(requested_grid.get("mode") or "") == str(current_grid.get("mode") or "")
            and int(requested_grid.get("count") or 0) == int(current_grid.get("count") or 0)
            and abs(float(requested_grid.get("leverage") or 0.0) - float(current_grid.get("leverage") or 0.0)) <= 1e-8
            and str(requested_grid.get("out_of_range") or "")
            == str(current_grid.get("out_of_range") or "exit_only")
        )
        if not immutable_matches:
            raise ValueError("strategy_preview_changed")
        if requested_notional > current_notional + 1e-8:
            raise ValueError("risk_budget_exceeded")
        if requested_notional < current_notional - 1e-8 and (
            not risk_recalculated
            or requested_risk_cap <= 0
            or requested_notional > requested_risk_cap + 1e-8
        ):
            raise ValueError("risk_budget_exceeded")
        return {
            "direction": str(current.get("direction") or "neutral"),
            "style": str(current.get("style") or "steady"),
            "range": dict(requested_preview.get("range") or {}),
            "out_of_range": str(current_grid.get("out_of_range") or "exit_only"),
            "grid": {
                "mode": str(current_grid.get("mode") or "arithmetic"),
                "count": int(current_grid.get("count") or 0),
                "notional_per_grid": requested_notional,
                "notional_mode": "manual",
            },
            "risk_budget": {
                "leverage": _positive_number(current_grid.get("leverage"), "grid leverage"),
            },
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
        trusted_account_equity(account)
        preview = self.preview(cycle_id, body, market=market, account=account)
        if (preview.get("risk") or {}).get("risk_budget_exceeded"):
            raise ValueError("risk_budget_exceeded")
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
        version = self._next_plan_version(str(current["cycle_id"]))
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

    def _accepted_orders(
        self,
        cycle_id: str,
        *,
        adapter=None,
        kind: str = "all",
    ) -> list[dict[str, Any]]:
        if kind not in {"all", "entry", "protection"}:
            raise ValueError("accepted order kind is unsupported")
        execution = adapter or build_configured_execution_engine_adapter(self.output_root, config=self.config)
        snapshot = execution.snapshot(cycle_id)
        accepted = [
            row
            for row in snapshot.get("orders") or []
            if str(row.get("state") or "").lower() == "accepted"
        ]
        if kind == "entry":
            return [row for row in accepted if str(row.get("event") or "entry").lower() == "entry"]
        if kind == "protection":
            return [row for row in accepted if str(row.get("event") or "entry").lower() != "entry"]
        return accepted

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

    def _sync_execution_mutations(
        self,
        adapter,
        cycle_id: str,
        *,
        market: dict[str, Any],
        now: str | None,
        identity: str,
    ) -> dict[str, Any] | None:
        if str(getattr(adapter, "name", "")) != "nautilus_paper":
            return None
        flush = getattr(adapter, "flush", None)
        if callable(flush):
            return flush(cycle_id)
        return self._advance_selected_execution(
            adapter,
            cycle_id,
            market=market,
            now=now,
            identity=identity,
        )

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


def _protection_core(rows: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    return sorted(
        (
            str(row.get("order_id") or ""),
            str(row.get("event") or ""),
            str(row.get("side") or ""),
            _core_number(row.get("price")),
            _core_number(row.get("quantity")),
            _core_number(row.get("sl")),
            _core_number(row.get("tp")),
            str(row.get("strategy_plan_id") or ""),
        )
        for row in rows
    )


def _entry_core(rows: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    return sorted(
        (
            str(row.get("order_id") or ""),
            str(row.get("side") or ""),
            _core_number(row.get("price")),
            _core_number(row.get("quantity")),
            str(row.get("strategy_plan_id") or ""),
        )
        for row in rows
    )


def _position_core(rows: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    return sorted(
        (
            str(row.get("position_id") or ""),
            str(row.get("trade_id") or ""),
            str(row.get("status") or ""),
            str(row.get("side") or ""),
            _core_number(row.get("remaining_units", row.get("quantity"))),
            _core_number(row.get("entry_price")),
            _core_number(row.get("sl")),
            _core_number(row.get("tp")),
            str(row.get("strategy_plan_id") or ""),
        )
        for row in rows
        if str(row.get("status") or "").lower() == "open"
    )


def _core_number(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        return f"{float(value):.8f}"
    except (TypeError, ValueError):
        return ""


def _extend_range_request_fingerprint(from_plan_id: str, requested_range: dict[str, Any]) -> str:
    raw = json.dumps(
        {
            "from_plan_id": str(from_plan_id),
            "requested_range": {
                "low": float(requested_range["low"]),
                "high": float(requested_range["high"]),
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _replace_grid_request_fingerprint(
    from_plan_id: str,
    requested_preview: dict[str, Any],
    *,
    risk_recalculated: bool,
    expected_execution: tuple[set[str], set[str]],
) -> str:
    raw = json.dumps(
        {
            "from_plan_id": str(from_plan_id),
            "requested_preview": requested_preview,
            "risk_recalculated": bool(risk_recalculated),
            "expected_execution": {
                "accepted_order_ids": sorted(expected_execution[0]),
                "open_position_ids": sorted(expected_execution[1]),
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()


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
