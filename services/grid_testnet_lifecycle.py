"""Fail-closed Grid Testnet lifecycle over the canonical broker fixture."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any, Mapping

from services.broker_port import BrokerCancelRequest, BrokerOrderRequest
from services.dualtrack_grid_core import GridLineLifecycle
from services.journal_store import load_json, write_json
from services.testnet_continuation_reconciliation import reconcile_before_continuation


class GridTestnetLifecycleError(RuntimeError):
    """A durable Grid Testnet blocker; callers must not add exposure."""


class GridTestnetLifecycle:
    """Run one fixed-geometry Grid deal against an approved local fixture.

    The lifecycle intentionally owns no clock, market feed, or venue primitive.
    The host supplies trusted events and a canonical broker port.  Grid geometry
    is immutable for the revision; only a Park-authored new revision can change
    it after this lifecycle reaches a terminal state.
    """

    schema_version = "testnet-grid-lifecycle-v1"

    def __init__(self, output_root: Path, broker: Any) -> None:
        self.output_root = Path(output_root)
        self.broker = broker
        self._states: dict[str, dict[str, Any]] = {}

    def start(self, plan: dict[str, Any], *, timestamp: str) -> dict[str, Any]:
        identity = self._identity(plan)
        self._validate_risk_inputs(plan)
        rungs = self._rungs(plan, identity)
        self._validate_full_depth_risk(plan, rungs)
        existing = self._load(identity["plan_id"])
        if existing is not None:
            self._assert_state_identity(existing, identity)
            self._states[identity["plan_id"]] = existing
            if existing.get("status") not in {
                "terminal",
                "sealed",
                "blocked_reconciliation",
                "blocked_protection",
                "blocked_risk",
                "hard_stop_triggered",
            }:
                reconciliation = reconcile_before_continuation(
                    self.broker,
                    existing,
                    timestamp=timestamp,
                    strategy_family="grid",
                    local_position_quantity=self._net_quantity(existing),
                )
                existing["continuation_reconciliation"] = reconciliation
                if reconciliation.get("status") != "ok":
                    self._block(
                        existing,
                        f"restart_reconciliation_blocked:{reconciliation.get('reason', 'unknown')}",
                        timestamp=timestamp,
                    )
                    self._save(existing)
            return self.snapshot(plan)
        if self.broker.protection_adapter is None or self.broker.account_adapter is None:
            status = "blocked_protection" if self.broker.protection_adapter is None else "blocked_reconciliation"
            blocker = "capability_gap:protection_order.submit" if self.broker.protection_adapter is None else "capability_gap:account.read"
            state = self._new_state(identity, rungs, timestamp, status=status, blocker=blocker)
            self._record_event(state, "lifecycle_blocked", timestamp=timestamp, reason=blocker)
            self._save(state)
            return self.snapshot(plan)

        state = self._new_state(identity, rungs, timestamp, status="starting")
        self._states[identity["plan_id"]] = state
        self._record_event(state, "lifecycle_started", timestamp=timestamp)
        try:
            self._submit_initial_ladder(plan, state, timestamp=timestamp)
        except GridTestnetLifecycleError:
            self._save(state)
            return self.snapshot(plan)
        state["status"] = "active"
        state["updated_at"] = timestamp
        self._record_event(state, "ladder_activated", timestamp=timestamp, rung_count=len(state["rungs"]))
        self._save(state)
        return self.snapshot(plan)

    def interrupt(
        self,
        plan: dict[str, Any],
        *,
        timestamp: str,
        reason: str = "manual_interrupt",
    ) -> dict[str, Any]:
        """Pause a Grid without flattening its known open rung quantities."""

        state = self._state(plan)
        if state["status"] in {
            "terminal",
            "sealed",
            "blocked_reconciliation",
            "blocked_protection",
            "blocked_risk",
        }:
            return self.snapshot(plan)
        # A manual interrupt is intentionally minimal-disruption: cancel only
        # resting entry legs and keep every protective exit for known exposure.
        self._cancel_all_open_orders(
            state,
            timestamp=timestamp,
            reason=reason,
            entries_only=True,
        )
        if state["status"] in {"blocked_reconciliation", "blocked_protection", "blocked_risk"}:
            self._save(state)
            return self.snapshot(plan)
        state["status"] = "interrupted"
        state["manual_interrupt"] = True
        state["next_action"] = "await_resume"
        self._record_event(
            state,
            "manual_interrupt",
            timestamp=timestamp,
            reason=str(reason or "manual_interrupt"),
        )
        state["updated_at"] = timestamp
        self._save(state)
        return self.snapshot(plan)

    def resume(self, plan: dict[str, Any], *, timestamp: str) -> dict[str, Any]:
        """Resume an interrupted Grid after host-side revalidation."""

        state = self._state(plan)
        if state["status"] != "interrupted":
            return self.snapshot(plan)
        if self.broker.protection_adapter is None or self.broker.account_adapter is None:
            self._block(state, "capability_gap:resume_revalidation", timestamp=timestamp)
            self._save(state)
            return self.snapshot(plan)
        if self._net_quantity(state) != 0 and state.get("hard_stop_protection", {}).get("status") != "active":
            self._block(state, "hard_stop_protection_revalidation_required", timestamp=timestamp)
            self._save(state)
            return self.snapshot(plan)
        reconciliation = reconcile_before_continuation(
            self.broker,
            state,
            timestamp=timestamp,
            strategy_family="grid",
            local_position_quantity=self._net_quantity(state),
        )
        state["continuation_reconciliation"] = reconciliation
        if reconciliation.get("status") != "ok":
            self._block(
                state,
                f"resume_reconciliation_blocked:{reconciliation.get('reason', 'unknown')}",
                timestamp=timestamp,
            )
            self._save(state)
            return self.snapshot(plan)
        try:
            for rung in state["rungs"]:
                line = GridLineLifecycle.from_snapshot(rung["line"])
                if not line.can_enter:
                    continue
                if any(
                    row.get("state") == "accepted"
                    and row.get("order_id") == rung.get("entry_order_id")
                    for row in state["orders"]
                ):
                    continue
                self._submit_rung_entry(plan, state, rung, timestamp=timestamp, event="entry_rearm")
        except Exception as exc:  # noqa: BLE001 - keep the blocker durable.
            self._block(state, f"resume_submit_failed:{type(exc).__name__}:{exc}", timestamp=timestamp)
            self._save(state)
            return self.snapshot(plan)
        if state["status"] not in {"blocked_reconciliation", "blocked_protection", "blocked_risk"}:
            state["status"] = "active"
            state["manual_interrupt"] = False
            state["next_action"] = "await_fill_or_grid_event"
            self._record_event(state, "manual_resume", timestamp=timestamp)
        state["updated_at"] = timestamp
        self._save(state)
        return self.snapshot(plan)

    def snapshot(self, plan: dict[str, Any]) -> dict[str, Any]:
        identity = self._identity(plan)
        state = self._states.get(identity["plan_id"]) or self._load(identity["plan_id"])
        if state is None:
            raise GridTestnetLifecycleError("Grid Testnet lifecycle has not started")
        self._assert_state_identity(state, identity)
        return json.loads(json.dumps(state, sort_keys=True))

    def on_fill(self, plan: dict[str, Any], raw_fill: dict[str, Any], *, timestamp: str) -> dict[str, Any]:
        state = self._state(plan)
        order_id = str(raw_fill.get("order_id") or "").strip()
        if not order_id:
            client_id = str(raw_fill.get("cloid") or raw_fill.get("client_order_id") or "")
            try:
                order_id = self._order_id_for_client(state, client_id)
            except GridTestnetLifecycleError as exc:
                self._block(state, "unknown_grid_client_identity", timestamp=timestamp)
                self._save(state)
                raise GridTestnetLifecycleError(state["blocker"]) from exc
        order = next((row for row in state["orders"] if str(row.get("order_id")) == order_id), None)
        if order is None:
            self._block(state, "unknown_fill_order", timestamp=timestamp)
            self._save(state)
            raise GridTestnetLifecycleError(state["blocker"])
        fill_identities = [str(raw_fill.get(key) or "") for key in ("tid", "hash") if str(raw_fill.get(key) or "")]
        if any(set(fill_identities).intersection(set(row.get("fill_identities") or [str(row.get("fill_id") or "")])) for row in state["fills"]):
            return self.snapshot(plan)
        was_cancelled = order.get("state") == "cancelled"
        late_cancelled_entry_allowed = was_cancelled and order.get("event") in {"entry", "entry_rearm"} and state["status"] in {"active", "terminal", "sealed", "hard_stop_triggered", "blocked_reconciliation", "blocked_protection", "blocked_risk"}
        if (state["status"] in {"terminal", "sealed"} and not late_cancelled_entry_allowed) or (state["status"] in {"blocked_reconciliation", "blocked_protection", "blocked_risk"} and order.get("event") not in {"hard_stop", "hard_stop_recovery"} and not late_cancelled_entry_allowed) or (state["status"] == "hard_stop_triggered" and order.get("event") not in {"hard_stop", "hard_stop_recovery"} and not late_cancelled_entry_allowed):
            self._block(state, "late_fill_after_block_or_terminal", timestamp=timestamp)
            self._save(state)
            raise GridTestnetLifecycleError(state["blocker"])
        if state["status"] in {"terminal", "sealed", "blocked_reconciliation", "blocked_protection", "blocked_risk"} and order.get("event") in {"hard_stop", "hard_stop_recovery"}:
            state["status"] = "hard_stop_triggered"
        if state["status"] in {"terminal", "sealed"} and late_cancelled_entry_allowed:
            state["status"] = "hard_stop_triggered"
            state["sealed"] = False
            state["post_terminal_late_fill"] = True
            state["park_notification_required"] = False
            state.pop("park_notification", None)
            state.pop("next_action", None)
            self._record_event(state, "post_terminal_late_fill", timestamp=timestamp, order_id=order_id)

        try:
            receipt = self._apply_fill_receipt(state, order, raw_fill)
        except Exception as exc:  # noqa: BLE001 - canonical seam is fail-closed.
            self._block(state, f"fill_rejected:{type(exc).__name__}:{exc}", timestamp=timestamp)
            self._save(state)
            raise GridTestnetLifecycleError(state["blocker"]) from exc
        receipt_state = str(getattr(receipt.state, "value", receipt.state) or "").lower()
        if receipt_state in {"unknown", "rejected"}:
            self._block(state, f"fill_receipt_{receipt_state}", timestamp=timestamp)
            self._save(state)
            raise GridTestnetLifecycleError(state["blocker"])
        quantity = float(raw_fill.get("sz") or raw_fill.get("quantity") or 0.0)
        price = float(receipt.average_fill_price or raw_fill.get("px") or 0.0)
        state["last_market_price"] = price
        planned = float(order.get("planned_price") or order.get("price") or 0.0)
        slippage = abs(price - planned)
        fill_id = fill_identities[0] if fill_identities else ""
        fill = {
            "fill_id": fill_id,
            "fill_identities": fill_identities,
            "order_id": order_id,
            "event": order.get("event"),
            "rung_id": order.get("rung_id"),
            "quantity": quantity,
            "price": price,
            "planned_price": planned,
            "slippage": slippage,
            **self._identity_metadata(),
            "timestamp": timestamp,
        }
        state["fills"].append(fill)
        order["state"] = "partial" if receipt_state == "partially_filled" else receipt_state
        order["filled_quantity"] = float(receipt.filled_quantity)
        order["average_fill_price"] = price
        self._record_event(state, "fill_received", timestamp=timestamp, fill_id=fill_id, order_id=order_id, rung_id=order.get("rung_id"))

        max_slippage = float((plan.get("risk_budget") or {}).get("max_slippage") or 0.0)
        slippage_breached = max_slippage <= 0 or slippage > max_slippage

        rung = self._rung(state, str(order.get("rung_id") or ""))
        line = GridLineLifecycle.from_snapshot(rung["line"])
        if order.get("event") in {"entry", "entry_rearm"}:
            if was_cancelled and state["status"] in {"hard_stop_triggered", "blocked_reconciliation", "blocked_protection", "blocked_risk"}:
                try:
                    self._apply_late_entry_fill(line, fill_id=fill_id, quantity=quantity, at=timestamp)
                except Exception as exc:  # noqa: BLE001
                    self._block(state, f"late_entry_fill_reconciliation_failed:{type(exc).__name__}:{exc}", timestamp=timestamp)
                    self._save(state)
                    raise GridTestnetLifecycleError(state["blocker"]) from exc
                rung["line"] = line.snapshot()
                self._hard_stop(plan, state, timestamp=timestamp, reason="late_fill_after_hard_stop")
                state["updated_at"] = timestamp
                self._save(state)
                return self.snapshot(plan)
            if was_cancelled and line.state == "open":
                try:
                    self._apply_late_entry_fill(line, fill_id=fill_id, quantity=quantity, at=timestamp)
                except Exception as exc:  # noqa: BLE001 - late fills must leave a durable blocker.
                    self._block(state, f"late_entry_fill_reconciliation_failed:{type(exc).__name__}:{exc}", timestamp=timestamp)
                    self._save(state)
                    raise GridTestnetLifecycleError(state["blocker"]) from exc
            elif was_cancelled and line.state == "cancelled":
                try:
                    self._apply_late_entry_fill(line, fill_id=fill_id, quantity=quantity, at=timestamp)
                except Exception as exc:  # noqa: BLE001
                    self._block(state, f"late_entry_fill_reconciliation_failed:{type(exc).__name__}:{exc}", timestamp=timestamp)
                    self._save(state)
                    raise GridTestnetLifecycleError(state["blocker"]) from exc
                rung["line"] = line.snapshot()
                self._hard_stop(plan, state, timestamp=timestamp, reason="late_fill_after_cancel", market_price=price)
                state["updated_at"] = timestamp
                self._save(state)
                return self.snapshot(plan)
            else:
                try:
                    line.apply_entry_fill(fill_id=fill_id, quantity=quantity, at=timestamp)
                except Exception as exc:  # noqa: BLE001
                    self._block(state, f"grid_entry_fill_state_error:{type(exc).__name__}:{exc}", timestamp=timestamp)
                    self._save(state)
                    raise GridTestnetLifecycleError(state["blocker"]) from exc
            rung["line"] = line.snapshot()
            if slippage_breached:
                self._block(state, "fill_slippage_exceeded", timestamp=timestamp)
                self._record_event(state, "slippage_budget_breached", timestamp=timestamp, planned_price=planned, actual_price=price, slippage=slippage)
                self._hard_stop(plan, state, timestamp=timestamp, reason="slippage", market_price=price)
                state["updated_at"] = timestamp
                self._save(state)
                return self.snapshot(plan)
            if receipt_state == "partially_filled":
                rung["partial_deadline"] = rung.get("partial_deadline") or self._deadline(timestamp, plan)
                if was_cancelled:
                    try:
                        self._submit_rung_tp(plan, state, rung, timestamp=timestamp)
                    except Exception as exc:  # noqa: BLE001
                        self._submission_failure(plan, state, timestamp=timestamp, reason=f"tp_submit_failed:{type(exc).__name__}:{exc}")
                        self._save(state)
                        return self.snapshot(plan)
            else:
                rung["partial_deadline"] = None
            if not self._risk_within_budget(plan, state):
                self._block(state, "maximum_loss_budget_exceeded", timestamp=timestamp)
                self._hard_stop(plan, state, timestamp=timestamp, reason="risk_budget")
            else:
                self._ensure_hard_stop(plan, state, timestamp=timestamp)
                if receipt_state != "partially_filled":
                    try:
                        self._submit_rung_tp(plan, state, rung, timestamp=timestamp)
                    except Exception as exc:  # noqa: BLE001
                        self._submission_failure(plan, state, timestamp=timestamp, reason=f"tp_submit_failed:{type(exc).__name__}:{exc}")
                        self._save(state)
                        return self.snapshot(plan)
        elif order.get("event") in {"tp", "hard_stop", "hard_stop_recovery"}:
            rearm = order.get("event") == "tp" and not slippage_breached and state.get("status") not in {"stopping", "hard_stop_triggered"}
            try:
                line.apply_close_fill(fill_id=fill_id, quantity=quantity, at=timestamp, rearm=rearm, terminal_state="stopped" if not rearm else "closed")
            except Exception as exc:  # noqa: BLE001 - unresolved partial/late exits must persist a blocker.
                self._block(state, f"grid_exit_fill_state_error:{type(exc).__name__}:{exc}", timestamp=timestamp)
                self._save(state)
                raise GridTestnetLifecycleError(state["blocker"]) from exc
            rung["line"] = line.snapshot()
            if order.get("event") == "tp" and line.state == "rearmed":
                reconciliation = reconcile_before_continuation(
                    self.broker,
                    state,
                    timestamp=timestamp,
                    strategy_family="grid",
                    local_position_quantity=self._net_quantity(state),
                )
                state["continuation_reconciliation"] = reconciliation
                if reconciliation.get("status") != "ok":
                    self._block(
                        state,
                        f"rearm_reconciliation_blocked:{reconciliation.get('reason', 'unknown')}",
                        timestamp=timestamp,
                    )
                    state["updated_at"] = timestamp
                    self._save(state)
                    return self.snapshot(plan)
                try:
                    self._submit_rung_entry(plan, state, rung, timestamp=timestamp, event="entry_rearm")
                except Exception as exc:  # noqa: BLE001
                    self._submission_failure(plan, state, timestamp=timestamp, reason=f"rearm_submit_failed:{type(exc).__name__}:{exc}")
                    self._save(state)
                    return self.snapshot(plan)
            elif order.get("event") in {"hard_stop", "hard_stop_recovery"} and line.open_quantity <= 1e-9:
                rung["hard_stop_closed"] = True
            self._ensure_hard_stop(plan, state, timestamp=timestamp)
            if slippage_breached:
                self._block(state, "exit_fill_slippage_exceeded", timestamp=timestamp)
                self._record_event(state, "slippage_budget_breached", timestamp=timestamp, planned_price=planned, actual_price=price, slippage=slippage)
                if line.open_quantity > 1e-9 or any(GridLineLifecycle.from_snapshot(item["line"]).open_quantity > 1e-9 for item in state["rungs"]):
                    self._hard_stop(plan, state, timestamp=timestamp, reason="exit_slippage", market_price=price)
                else:
                    state["reconciliation"] = self._terminal_reconciliation(state, timestamp)
                    if state["reconciliation"].get("status") == "ok":
                        state["status"] = "terminal"
                        state["sealed"] = True
                        state["terminal_reason"] = "exit_fill_slippage_exceeded"
                        state["closure_blocker"] = "exit_fill_slippage_exceeded"
                        state["park_notification_required"] = True
                        state["park_notification"] = {"notification_id": f"grid-terminal:{state['strategy_plan_id']}:{state['plan_digest']}", "channel": "telegram", "status": "queued", "reason": "exit_fill_slippage_exceeded", "strategy_session_id": state["strategy_session_id"], "strategy_revision_id": state["strategy_revision_id"], "plan_digest": state["plan_digest"], "next_action": "notify_park_and_wait"}
                        state["next_action"] = "notify_park_and_wait"
                        self._record_event(state, "revision_sealed", timestamp=timestamp, reason="exit_fill_slippage_exceeded")
        else:
            self._block(state, "unknown_grid_order_event", timestamp=timestamp)
        self._maybe_finalize_hard_stop(plan, state, timestamp=timestamp)
        state["updated_at"] = timestamp
        self._save(state)
        return self.snapshot(plan)

    def on_market_event(self, plan: dict[str, Any], *, price: float, timestamp: str) -> dict[str, Any]:
        state = self._state(plan)
        if state["status"] in {"terminal", "sealed", "blocked_reconciliation", "blocked_protection", "blocked_risk"}:
            return self.snapshot(plan)
        if not isinstance(price, (int, float)) or float(price) <= 0:
            self._block(state, "market_price_invalid", timestamp=timestamp)
            self._save(state)
            return self.snapshot(plan)
        state["last_market_price"] = float(price)
        upper = float(state["upper_boundary"])
        lower = float(state["lower_boundary"])
        if float(price) >= upper or float(price) <= lower:
            self._hard_stop(plan, state, timestamp=timestamp, reason="grid_boundary", market_price=float(price))
        else:
            self._skip_missed_rungs(plan, state, price=float(price), timestamp=timestamp)
            self._expire_partial_entries(plan, state, timestamp=timestamp)
        state["updated_at"] = timestamp
        self._save(state)
        return self.snapshot(plan)

    def _submit_initial_ladder(self, plan: dict[str, Any], state: dict[str, Any], *, timestamp: str) -> None:
        accepted: list[str] = []
        for rung in state["rungs"]:
            max_open_orders = int((plan.get("risk_budget") or {}).get("max_open_orders") or 0)
            if max_open_orders > 0 and len([row for row in state["orders"] if row.get("state") == "accepted"]) >= max_open_orders:
                raise GridTestnetLifecycleError("max_open_orders_exceeded")
            try:
                self._submit_rung_entry(plan, state, rung, timestamp=timestamp, event="entry")
                accepted.append(str(rung["entry_order_id"]))
            except GridTestnetLifecycleError as exc:
                self._record_event(state, "ladder_incomplete", timestamp=timestamp, reason=str(exc), accepted_order_ids=accepted)
                self._rollback_ladder(state, timestamp=timestamp, reason="initial_ladder_incomplete")
                state["status"] = "blocked_local_validation" if str(exc).startswith("blocked_local_validation:") else "blocked_reconciliation"
                state["blocker"] = f"initial_ladder_incomplete:{exc}"
                raise

    @staticmethod
    def _apply_late_entry_fill(line: GridLineLifecycle, *, fill_id: str, quantity: float, at: str) -> None:
        if fill_id in line.processed_fill_ids:
            return
        requested = float(line.requested_quantity or 0.0)
        if quantity <= 0 or float(line.entry_filled_quantity) + quantity > requested + 1e-9:
            raise GridTestnetLifecycleError("late_entry_fill_exceeds_requested_quantity")
        if line.state in {"cancelled", "armed", "rearmed", "entry_partially_filled"}:
            line.state = "open_cancelled"
            line.active = False
            line.entry_order_open = False
        line.entry_filled_quantity += quantity
        line.processed_fill_ids.add(fill_id)
        line.transitions.append(
            {
                "sequence": len(line.transitions) + 1,
                "line_id": line.line_id,
                "generation": line.generation,
                "event": "late_entry_fill_reconciled",
                "from": line.state,
                "to": line.state,
                "at": at,
                "fill_id": fill_id,
                "fill_quantity": quantity,
                "requested_quantity": requested,
                "entry_filled_quantity": line.entry_filled_quantity,
                "close_filled_quantity": line.close_filled_quantity,
                "open_quantity": line.open_quantity,
                "entry_order_open": line.entry_order_open,
                "active": line.active,
            }
        )

    def _submit_rung_entry(self, plan: dict[str, Any], state: dict[str, Any], rung: dict[str, Any], *, timestamp: str, event: str) -> None:
        index = int(rung.get("generation") or 1)
        price = float(rung["price"])
        quantity = float(rung["quantity"])
        command = self._command(plan, state, rung, price=price, quantity=quantity, event=event, index=index, timestamp=timestamp)
        receipt = self._submit_with_retries(plan, state, command, timestamp=timestamp)
        row = self._order_row(command, receipt)
        state["orders"].append(row)
        rung["entry_order_id"] = row["order_id"]
        rung["entry_order_client_id"] = row["client_order_id"]
        rung["entry_order_open"] = True

    def _submit_rung_tp(self, plan: dict[str, Any], state: dict[str, Any], rung: dict[str, Any], *, timestamp: str) -> None:
        line = GridLineLifecycle.from_snapshot(rung["line"])
        quantity = line.open_quantity
        if quantity <= 1e-9:
            return
        tp_attempt = int(rung.get("tp_attempt") or 0) + 1
        rung["tp_attempt"] = tp_attempt
        existing_tp = next((row for row in state["orders"] if row.get("order_id") == rung.get("tp_order_id") and row.get("state") == "accepted"), None)
        if existing_tp is not None:
            try:
                receipt = self.broker.cancel_order(BrokerCancelRequest(run_date=state["cycle_id"], asset=existing_tp["instrument_id"], client_order_id=existing_tp.get("client_order_id") or "", broker_order_id=existing_tp.get("broker_order_id") or ""))
                self._require_cancel_receipt(receipt)
                existing_tp["state"] = "cancelled"
                existing_tp["cancel_receipt_state"] = str(getattr(receipt.state, "value", receipt.state))
            except Exception as exc:  # noqa: BLE001 - never leave competing TP legs unresolved.
                self._submission_failure(plan, state, timestamp=timestamp, reason=f"tp_replace_cancel_failed:{type(exc).__name__}:{exc}")
                return
        command = self._command(
            plan,
            state,
            rung,
            price=float(rung["tp"]),
            quantity=quantity,
            event="tp",
            index=int(rung.get("generation") or 1),
            timestamp=timestamp,
            reduce_only=True,
            order_type="market",
            time_in_force="ioc",
            planned_price=float(rung["tp"]),
            attempt=tp_attempt,
            trigger_price=float(rung["tp"]),
        )
        receipt = self._submit_with_retries(plan, state, command, timestamp=timestamp)
        row = self._order_row(command, receipt)
        state["orders"].append(row)
        rung["tp_order_id"] = row["order_id"]
        rung["tp_order_open"] = True

    def _submit_with_retries(self, plan: dict[str, Any], state: dict[str, Any], command: dict[str, Any], *, timestamp: str) -> Any:
        retries = int((plan.get("risk_budget") or {}).get("max_submit_retries") or 3)
        last: Exception | None = None
        for attempt in range(1, max(1, retries) + 1):
            try:
                receipt = self.broker.submit_order(BrokerOrderRequest(run_date=state["cycle_id"], ticket=command, latest_price=float(command.get("price") or 0), actual_size=float(command.get("quantity") or 0)))
                state.setdefault("retry_events", []).append({"operation": "submit", "attempt": attempt, "ticket_id": command["ticket_id"], "status": "accepted", "timestamp": timestamp})
                return receipt
            except Exception as exc:  # noqa: BLE001 - bounded retry evidence.
                last = exc
                state.setdefault("retry_events", []).append({"operation": "submit", "attempt": attempt, "ticket_id": command["ticket_id"], "status": "failed", "error": type(exc).__name__, "timestamp": timestamp})
                if self._is_local_validation_error(exc):
                    raise GridTestnetLifecycleError(
                        self._local_validation_reason(command, exc)
                    ) from exc
                # Never blindly repeat an ambiguous side effect.  A retry is
                # allowed only after the public Broker seam proves that the
                # idempotency key is absent; a known/unknown query outcome
                # stops the lifecycle and lets the caller reconcile.
                query = getattr(self.broker, "query_by_idempotency_key", None)
                if not callable(query):
                    raise GridTestnetLifecycleError(
                        f"submit_unknown_query_unavailable:{type(exc).__name__}"
                    ) from exc
                try:
                    observed = query(str(command.get("idempotency_key") or command.get("ticket_id") or ""))
                except Exception as query_exc:  # noqa: BLE001 - query uncertainty is terminal.
                    detail = str(query_exc).strip()
                    if isinstance(query_exc, KeyError):
                        missing_key = query_exc.args[0] if query_exc.args else "unknown"
                        detail = f"missing_key={missing_key}"
                    elif not detail:
                        detail = "query returned no reason"
                    raise GridTestnetLifecycleError(
                        f"submit_unknown_query_failed:{type(query_exc).__name__}:{detail}"
                    ) from query_exc
                observed_state = str(
                    getattr(getattr(observed, "state", None), "value", getattr(observed, "state", ""))
                    or (observed.get("state") if isinstance(observed, Mapping) else "")
                ).lower()
                if observed_state and observed_state not in {"missing", "not_found", "notfound"}:
                    raise GridTestnetLifecycleError(
                        f"submit_unknown_already_present:{observed_state}"
                    ) from exc
                if attempt >= max(1, retries):
                    break
        raise GridTestnetLifecycleError(f"submit_retries_exhausted:{type(last).__name__ if last else 'unknown'}")

    @staticmethod
    def _is_local_validation_error(exc: Exception) -> bool:
        """Identify failures raised before an order can reach the venue."""

        return isinstance(exc, (TypeError, ValueError)) or type(exc).__name__ in {
            "BrokerCapabilityError",
            "InstrumentBindingError",
            "StandardBrokerExternalExecutionError",
            "UnsupportedBrokerCapability",
        }

    @staticmethod
    def _local_validation_reason(command: Mapping[str, Any], exc: Exception) -> str:
        price = command.get("price")
        quantity = command.get("quantity")
        try:
            notional = float(price) * float(quantity)
        except (TypeError, ValueError):
            notional = "unavailable"
        detail = str(exc).strip() or "validation failed"
        return (
            "blocked_local_validation:"
            f"{type(exc).__name__}:price={price}:quantity={quantity}:"
            f"notional={notional}:reason={detail}"
        )

    def _rollback_ladder(self, state: dict[str, Any], *, timestamp: str, reason: str) -> None:
        for row in state["orders"]:
            if row.get("state") != "accepted":
                continue
            try:
                receipt = self.broker.cancel_order(BrokerCancelRequest(run_date=state["cycle_id"], asset=row["instrument_id"], client_order_id=row.get("client_order_id") or "", broker_order_id=row.get("broker_order_id") or ""))
                self._require_cancel_receipt(receipt)
                row["state"] = "cancelled"
                row["cancel_receipt_state"] = str(getattr(receipt.state, "value", receipt.state))
            except Exception as exc:  # noqa: BLE001 - unresolved rollback blocks.
                state["status"] = "blocked_reconciliation"
                state["blocker"] = f"ladder_rollback_failed:{type(exc).__name__}:{exc}"
        self._record_event(state, "ladder_rollback", timestamp=timestamp, reason=reason)

    def _expire_partial_entries(self, plan: dict[str, Any], state: dict[str, Any], *, timestamp: str) -> None:
        for rung in state["rungs"]:
            deadline = str(rung.get("partial_deadline") or "")
            if not deadline or deadline > timestamp:
                continue
            line = GridLineLifecycle.from_snapshot(rung["line"])
            if not line.entry_order_open or line.open_quantity <= 1e-9:
                continue
            row = next((item for item in state["orders"] if item.get("order_id") == rung.get("entry_order_id")), None)
            if row is None:
                self._block(state, "partial_entry_order_missing", timestamp=timestamp)
                continue
            try:
                receipt = self.broker.cancel_order(BrokerCancelRequest(run_date=state["cycle_id"], asset=row["instrument_id"], client_order_id=row.get("client_order_id") or "", broker_order_id=row.get("broker_order_id") or ""))
                self._require_cancel_receipt(receipt)
                row["state"] = "cancelled"
                line.confirm_entry_cancelled(at=timestamp, reason="five_minute_deadline")
                rung["line"] = line.snapshot()
                rung["partial_deadline"] = None
                self._submit_rung_tp(plan, state, rung, timestamp=timestamp)
                self._record_event(state, "partial_entry_deadline", timestamp=timestamp, rung_id=rung["rung_id"], cancel_state=str(getattr(receipt.state, "value", receipt.state)))
            except Exception as exc:  # noqa: BLE001 - cancellation uncertainty blocks.
                self._submission_failure(plan, state, timestamp=timestamp, reason=f"partial_entry_cancel_failed:{type(exc).__name__}:{exc}")

    def _skip_missed_rungs(self, plan: dict[str, Any], state: dict[str, Any], *, price: float, timestamp: str) -> None:
        for rung in state["rungs"]:
            line = GridLineLifecycle.from_snapshot(rung["line"])
            if not line.can_enter:
                continue
            crossed = price < float(rung["price"]) if rung["side"] == "buy" else price > float(rung["price"])
            if not crossed:
                continue
            row = next((item for item in state["orders"] if item.get("order_id") == rung.get("entry_order_id") and item.get("state") == "accepted"), None)
            if row is None:
                continue
            try:
                receipt = self.broker.cancel_order(BrokerCancelRequest(run_date=state["cycle_id"], asset=row["instrument_id"], client_order_id=row.get("client_order_id") or "", broker_order_id=row.get("broker_order_id") or ""))
                self._require_cancel_receipt(receipt)
                row["state"] = "cancelled"
                row["cancel_reason"] = "missed_rung"
                line.cancel(at=timestamp, reason="market_crossed_without_fill")
                rung["line"] = line.snapshot()
                rung["missed"] = True
                self._record_event(state, "rung_missed_skipped", timestamp=timestamp, rung_id=rung["rung_id"], market_price=price, rung_price=rung["price"])
            except Exception as exc:  # noqa: BLE001 - unresolved cancellation cannot be silently chased.
                self._submission_failure(plan, state, timestamp=timestamp, reason=f"missed_rung_cancel_failed:{type(exc).__name__}:{exc}")

    def _hard_stop(self, plan: dict[str, Any], state: dict[str, Any], *, timestamp: str, reason: str, market_price: float | None = None) -> None:
        if state["status"] in {"terminal", "sealed"}:
            return
        market_price = market_price if market_price is not None else state.get("last_market_price")
        state["status"] = "hard_stop_triggered"
        self._cancel_all_open_orders(state, timestamp=timestamp, reason=reason)
        self._cancel_hard_stop_protection(plan, state, timestamp=timestamp)
        for rung in state["rungs"]:
            line = GridLineLifecycle.from_snapshot(rung["line"])
            if line.open_quantity <= 1e-9:
                continue
            command = self._command(plan, state, rung, price=float(rung["hard_stop"]), quantity=line.open_quantity, event="hard_stop", index=int(rung.get("generation") or 1), timestamp=timestamp, reduce_only=True, order_type="market", time_in_force="ioc", planned_price=float(rung["hard_stop"]), market_price=market_price)
            try:
                receipt = self._submit_with_retries(plan, state, command, timestamp=timestamp)
                state["orders"].append(self._order_row(command, receipt))
            except GridTestnetLifecycleError as exc:
                self._block(state, f"hard_stop_submit_failed:{exc}", timestamp=timestamp)
                self._submit_emergency_flatten(plan, state, rung, timestamp=timestamp, reason="hard_stop_submit_failed", market_price=market_price)
        self._record_event(state, "hard_stop_triggered", timestamp=timestamp, reason=reason)
        self._maybe_finalize_hard_stop(plan, state, timestamp=timestamp)

    def _submit_emergency_flatten(self, plan: dict[str, Any], state: dict[str, Any], rung: dict[str, Any], *, timestamp: str, reason: str, market_price: float | None = None) -> None:
        line = GridLineLifecycle.from_snapshot(rung["line"])
        if line.open_quantity <= 1e-9:
            return
        command = self._command(plan, state, rung, price=float(rung["hard_stop"]), quantity=line.open_quantity, event="hard_stop_recovery", index=int(rung.get("generation") or 1), timestamp=timestamp, reduce_only=True, order_type="market", time_in_force="ioc", planned_price=float(rung["hard_stop"]), attempt=int(rung.get("tp_attempt") or 0) + 1, market_price=market_price if market_price is not None else state.get("last_market_price"))
        try:
            receipt = self._submit_with_retries(plan, state, command, timestamp=timestamp)
            state["orders"].append(self._order_row(command, receipt))
            state["status"] = "hard_stop_triggered"
            self._record_event(state, "emergency_flatten_submitted", timestamp=timestamp, rung_id=rung["rung_id"], reason=reason)
        except GridTestnetLifecycleError as exc:
            self._block(state, f"emergency_flatten_failed:{exc}", timestamp=timestamp)

    def _submission_failure(self, plan: dict[str, Any], state: dict[str, Any], *, timestamp: str, reason: str) -> None:
        self._block(state, reason, timestamp=timestamp)
        self._hard_stop(plan, state, timestamp=timestamp, reason="grid_submission_failure")

    def _cancel_all_open_orders(
        self,
        state: dict[str, Any],
        *,
        timestamp: str,
        reason: str,
        entries_only: bool = False,
    ) -> None:
        for row in state["orders"]:
            if row.get("state") != "accepted":
                continue
            if entries_only and row.get("event") not in {"entry", "entry_rearm"}:
                continue
            try:
                receipt = self.broker.cancel_order(BrokerCancelRequest(run_date=state["cycle_id"], asset=row["instrument_id"], client_order_id=row.get("client_order_id") or "", broker_order_id=row.get("broker_order_id") or ""))
                self._require_cancel_receipt(receipt)
                row["state"] = "cancelled"
                row["cancel_reason"] = reason
                row["cancel_receipt_state"] = str(getattr(receipt.state, "value", receipt.state))
            except Exception as exc:  # noqa: BLE001
                self._block(state, f"order_cancel_failed:{type(exc).__name__}:{exc}", timestamp=timestamp)

    def _ensure_hard_stop(self, plan: dict[str, Any], state: dict[str, Any], *, timestamp: str) -> None:
        net_quantity = self._net_quantity(state)
        if abs(net_quantity) <= 1e-9:
            if state.get("hard_stop_protection") is not None:
                self._cancel_hard_stop_protection(plan, state, timestamp=timestamp)
            return
        if self.broker.protection_adapter is None:
            self._block(state, "capability_gap:protection_order.submit", timestamp=timestamp)
            return
        group = self._hard_stop_group(plan, state, net_quantity)
        try:
            if state.get("hard_stop_protection") is None:
                submitted = self.broker.request("protection_order", "submit", group)
            else:
                submitted = self.broker.request("protection_order", "replace", group)
            confirmed = self.broker.request("protection_order", "query", group)
            state["hard_stop_protection"] = {
                "protection_id": group.protection_id,
                "quantity": abs(net_quantity),
                "sl": float(group.stop_loss.trigger_price),
                "reduce_only": True,
                "status": "active",
                "submitted_provenance": self._provenance_dict(submitted),
                "confirmed_provenance": self._provenance_dict(confirmed),
                "confirmed_at": timestamp,
            }
            self._record_event(state, "hard_stop_coverage_confirmed", timestamp=timestamp, quantity=abs(net_quantity))
        except Exception as exc:  # noqa: BLE001
            self._block(state, f"hard_stop_coverage_failed:{type(exc).__name__}:{exc}", timestamp=timestamp)
            self._cancel_all_open_orders(state, timestamp=timestamp, reason="hard_stop_coverage_failure")
            self._hard_stop(plan, state, timestamp=timestamp, reason="hard_stop_coverage_failure")

    def _cancel_hard_stop_protection(self, plan: dict[str, Any], state: dict[str, Any], *, timestamp: str) -> None:
        del plan
        protection = state.get("hard_stop_protection")
        if not protection or self.broker.protection_adapter is None:
            return
        group = self._hard_stop_group_from_state(state, protection)
        try:
            receipt = self.broker.request("protection_order", "cancel", group)
            if getattr(receipt, "accepted", True) is not True:
                raise GridTestnetLifecycleError("hard_stop_protection_cancel_unconfirmed")
            state["hard_stop_protection"] = None
        except Exception as exc:  # noqa: BLE001
            self._block(state, f"hard_stop_protection_cancel_failed:{type(exc).__name__}:{exc}", timestamp=timestamp)

    def _maybe_finalize_hard_stop(self, plan: dict[str, Any], state: dict[str, Any], *, timestamp: str) -> None:
        if state.get("status") != "hard_stop_triggered":
            return
        open_quantity = sum(GridLineLifecycle.from_snapshot(rung["line"]).open_quantity for rung in state["rungs"])
        if open_quantity > 1e-9:
            return
        try:
            reconciliation = self._terminal_reconciliation(state, timestamp)
        except Exception as exc:  # noqa: BLE001
            self._block(state, f"hard_stop_reconciliation_failed:{type(exc).__name__}:{exc}", timestamp=timestamp)
            return
        state["reconciliation"] = reconciliation
        if reconciliation.get("status") != "ok":
            self._block(state, "hard_stop_reconciliation_blocked", timestamp=timestamp)
            return
        state["status"] = "terminal"
        state["sealed"] = True
        state["terminal_reason"] = "hard_stop"
        state["next_action"] = "notify_park_and_wait"
        state["park_notification_required"] = True
        state["park_notification"] = {
            "notification_id": f"grid-terminal:{state['strategy_plan_id']}:{state['plan_digest']}",
            "channel": "telegram",
            "status": "queued",
            "reason": "hard_stop",
            "strategy_session_id": state["strategy_session_id"],
            "strategy_revision_id": state["strategy_revision_id"],
            "plan_digest": state["plan_digest"],
            "next_action": "notify_park_and_wait",
        }
        self._record_event(state, "revision_sealed", timestamp=timestamp, reason="hard_stop")

    def _terminal_reconciliation(self, state: dict[str, Any], timestamp: str) -> dict[str, Any]:
        instrument = str(state.get("instrument_id") or "")
        try:
            open_orders = tuple(self.broker.request("order_execution", "open_orders", instrument) or ())
            account = self.broker.request("account", "read", self.broker.broker_config["account_id"])
            positions = tuple(position for position in getattr(account, "positions", ()) if abs(float(getattr(position, "signed_quantity", 0) or 0)) > 1e-9)
        except Exception as exc:  # noqa: BLE001
            return {"status": "blocked", "reason": f"broker_truth_query_failed:{type(exc).__name__}:{exc}", "at": timestamp}
        local_open = [row for row in state["orders"] if row.get("state") == "accepted"]
        return {
            "status": "ok" if not open_orders and not local_open and not positions else "blocked",
            "broker_open_order_count": len(open_orders),
            "local_open_order_count": len(local_open),
            "broker_position_count": len(positions),
            "at": timestamp,
        }

    def _net_quantity(self, state: dict[str, Any]) -> float:
        total = 0.0
        for rung in state["rungs"]:
            line = GridLineLifecycle.from_snapshot(rung["line"])
            signed = line.open_quantity if rung["side"] == "buy" else -line.open_quantity
            total += signed
        return total

    def _hard_stop_group(self, plan: dict[str, Any], state: dict[str, Any], net_quantity: float) -> Any:
        from decimal import Decimal
        from standard_broker import OrderSide, ProtectionExecution, ProtectionGroup, ProtectionLeg, ProtectionType, ProtectionQuantityPolicy
        long = net_quantity > 0
        stop = float(state["lower_boundary"] if long else state["upper_boundary"])
        return ProtectionGroup(
            protection_id=f"grid-hard-stop:{state['strategy_plan_id']}",
            parent_order_id=str(state["orders"][0]["order_id"] if state["orders"] else state["strategy_plan_id"]),
            instrument_id=state["instrument_id"],
            entry_side=OrderSide.BUY if long else OrderSide.SELL,
            entry_price=Decimal(str(self._weighted_entry(state))),
            quantity=Decimal(str(abs(net_quantity))),
            quantity_policy=ProtectionQuantityPolicy.POSITION_FOLLOWING,
            take_profit=None,
            stop_loss=ProtectionLeg(protection_type=ProtectionType.STOP_LOSS, execution=ProtectionExecution.MARKET, trigger_price=Decimal(str(stop))),
        )

    def _hard_stop_group_from_state(self, state: dict[str, Any], protection: Mapping[str, Any]) -> Any:
        class _Plan:
            pass
        return self._hard_stop_group(_Plan(), state, float(protection.get("quantity") or 0.0))

    def _weighted_entry(self, state: dict[str, Any]) -> float:
        total = 0.0
        quantity = 0.0
        for rung in state["rungs"]:
            line = GridLineLifecycle.from_snapshot(rung["line"])
            total += float(rung["price"]) * line.open_quantity
            quantity += line.open_quantity
        return total / quantity if quantity > 1e-9 else float(state["lower_boundary"])

    def _command(self, plan: dict[str, Any], state: dict[str, Any], rung: dict[str, Any], *, price: float, quantity: float, event: str, index: int, timestamp: str, reduce_only: bool = False, order_type: str = "limit", time_in_force: str = "gtc", planned_price: float | None = None, attempt: int | None = None, market_price: float | None = None, trigger_price: float | None = None) -> dict[str, Any]:
        suffix = f":a{attempt}" if attempt is not None else ""
        ticket_id = f"{state['strategy_plan_id']}:{rung['rung_id']}:{event}:g{index}{suffix}"
        side = rung["side"] if not reduce_only else ("sell" if rung["side"] == "buy" else "buy")
        execution_price = float(market_price if market_price is not None else price)
        if order_type == "market":
            bound = float((plan.get("risk_budget") or {}).get("max_slippage") or 0.0)
            if bound <= 0:
                raise GridTestnetLifecycleError("market_order_slippage_bound_missing")
            execution_price = max(0.00000001, execution_price - bound) if side == "sell" else execution_price + bound
        return {
            "ticket_id": ticket_id,
            "instrument_id": state["instrument_id"],
            "side": side,
            "order_type": "market" if order_type == "market" else "limit",
            "limit_price": execution_price,
            "time_in_force": time_in_force,
            "price": execution_price,
            "planned_price": planned_price if planned_price is not None else price,
            "trigger_price": trigger_price,
            "execution_semantics": "aggressive_ioc_market" if order_type == "market" else "resting_limit",
            "quantity": quantity,
            "idempotency_key": ticket_id,
            "client_order_id": "0x" + hashlib.sha256(ticket_id.encode("utf-8")).hexdigest()[:32],
            "reduce_only": reduce_only,
            "close_position": reduce_only,
            "event": event,
            "rung_id": rung["rung_id"],
            "strategy_plan_id": state["strategy_plan_id"],
            "strategy_plan_version": state["strategy_plan_version"],
            "strategy_session_id": state["strategy_session_id"],
            "strategy_revision_id": state["strategy_revision_id"],
            "plan_digest": state["plan_digest"],
            "cycle_id": state["cycle_id"],
            "timestamp": timestamp,
        }

    def _order_row(self, command: dict[str, Any], receipt: Any) -> dict[str, Any]:
        receipt_state = str(getattr(receipt.state, "value", receipt.state) or "").lower()
        if receipt_state in {"unknown", "rejected"}:
            raise GridTestnetLifecycleError(f"order_submit_{receipt_state}:{receipt.order_id}")
        return {**command, "order_id": str(receipt.order_id), "client_order_id": str(receipt.client_order_id), "broker_order_id": str(receipt.broker_order_id or ""), "environment": "testnet", "account_id": receipt.account_address, "release_sha": receipt.release_sha, "state": "accepted" if receipt_state in {"submitting", "resting", "waiting_for_fill", "waiting_for_trigger"} else receipt_state}

    def _rungs(self, plan: Mapping[str, Any], identity: Mapping[str, Any]) -> list[dict[str, Any]]:
        grid = plan.get("grid") if isinstance(plan.get("grid"), Mapping) else {}
        risk = plan.get("risk_budget") if isinstance(plan.get("risk_budget"), Mapping) else {}
        hard_stop_value = plan.get("hard_stop")
        if hard_stop_value in (None, ""):
            hard_stop_value = grid.get("hard_stop") or grid.get("hard_stop_price")
        explicit_hard_stop = hard_stop_value not in (None, "")
        if isinstance(hard_stop_value, Mapping):
            hard_stop_by_side = {
                "buy": float(hard_stop_value.get("long") or 0),
                "sell": float(hard_stop_value.get("short") or 0),
            }
        else:
            if explicit_hard_stop:
                default_stop = float(hard_stop_value)
                hard_stop_by_side = {"buy": default_stop, "sell": default_stop}
            else:
                hard_stop_by_side = {
                    "buy": identity["lower_boundary"],
                    "sell": identity["upper_boundary"],
                }
        if identity["direction"] == "long" and explicit_hard_stop and not hard_stop_by_side["buy"] < identity["lower_boundary"]:
            raise GridTestnetLifecycleError("grid_hard_stop_boundary_invalid")
        if identity["direction"] == "short" and explicit_hard_stop and not hard_stop_by_side["sell"] > identity["upper_boundary"]:
            raise GridTestnetLifecycleError("grid_hard_stop_boundary_invalid")
        if identity["direction"] == "neutral" and explicit_hard_stop and not (
            hard_stop_by_side["buy"] < identity["lower_boundary"]
            and hard_stop_by_side["sell"] > identity["upper_boundary"]
        ):
            raise GridTestnetLifecycleError("grid_hard_stop_boundary_invalid")
        raw = grid.get("rungs") or risk.get("grid_rungs") or plan.get("grid_rungs")
        if not isinstance(raw, list) or not raw:
            orders = grid.get("orders") if isinstance(grid.get("orders"), list) else []
            raw = [
                {
                    "rung": index,
                    "price": item.get("price"),
                    "side": item.get("side"),
                    "take_profit": item.get("tp") or item.get("take_profit"),
                    "hard_stop": item.get("sl") or item.get("hard_stop"),
                    "quantity": item.get("quantity"),
                }
                for index, item in enumerate(orders, start=1)
                if isinstance(item, Mapping)
            ]
        if not isinstance(raw, list) or not raw:
            raise GridTestnetLifecycleError("grid_geometry_missing")
        rungs: list[dict[str, Any]] = []
        for index, item in enumerate(raw, start=1):
            if not isinstance(item, Mapping):
                raise GridTestnetLifecycleError("grid_geometry_invalid")
            side = str(item.get("side") or "").lower()
            price = float(item.get("price") or 0)
            tp = float(item.get("tp") or item.get("take_profit") or 0)
            hard_stop = float(item.get("sl") or item.get("hard_stop") or 0)
            quantity = float(item.get("quantity") or risk.get("per_order_quantity") or 0)
            if side not in {"buy", "sell"} or price <= 0 or tp <= 0 or hard_stop <= 0 or quantity <= 0:
                raise GridTestnetLifecycleError("grid_geometry_invalid")
            if not identity["lower_boundary"] <= price <= identity["upper_boundary"]:
                raise GridTestnetLifecycleError("grid_rung_outside_boundary")
            canonical_stop = hard_stop_by_side[side]
            if side == "buy" and (
                tp <= price
                or hard_stop > identity["lower_boundary"] + 1e-9
                or hard_stop < canonical_stop - 1e-9
            ):
                raise GridTestnetLifecycleError("grid_buy_geometry_invalid")
            if side == "sell" and (
                tp >= price
                or hard_stop < identity["upper_boundary"] - 1e-9
                or hard_stop > canonical_stop + 1e-9
            ):
                raise GridTestnetLifecycleError("grid_sell_geometry_invalid")
            if identity["direction"] == "long" and side != "buy":
                raise GridTestnetLifecycleError("long_grid_requires_buy_rungs")
            if identity["direction"] == "short" and side != "sell":
                raise GridTestnetLifecycleError("short_grid_requires_sell_rungs")
            if identity["direction"] == "neutral" and ((side == "buy" and price >= identity["midpoint"]) or (side == "sell" and price <= identity["midpoint"])):
                raise GridTestnetLifecycleError("neutral_grid_midpoint_geometry_invalid")
            line_id = str(item.get("grid_line_id") or item.get("level_id") or f"{identity['revision_id']}:grid:{index}")
            line = GridLineLifecycle(line_id=line_id, armed_at=identity["created_at"], requested_quantity=quantity)
            rungs.append({"rung_id": line_id, "rung": int(item.get("rung") or index), "side": side, "price": price, "tp": tp, "hard_stop": hard_stop, "quantity": quantity, "generation": 1, "line": line.snapshot(), "entry_order_id": None, "tp_order_id": None, "partial_deadline": None})
        if len({rung["price"] for rung in rungs}) != len(rungs):
            raise GridTestnetLifecycleError("grid_rung_prices_not_unique")
        if identity["direction"] == "neutral" and {rung["side"] for rung in rungs} != {"buy", "sell"}:
            raise GridTestnetLifecycleError("neutral_grid_requires_two_legs")
        return rungs

    def _new_state(self, identity: dict[str, Any], rungs: list[dict[str, Any]], timestamp: str, *, status: str, blocker: str | None = None) -> dict[str, Any]:
        state = {"schema_version": self.schema_version, "strategy_plan_id": identity["plan_id"], "strategy_plan_version": identity["version"], "cycle_id": identity["cycle_id"], "strategy_session_id": identity["session_id"], "strategy_revision_id": identity["revision_id"], "plan_digest": identity["plan_digest"], "instrument_id": identity["instrument_id"], "direction": identity["direction"], "lower_boundary": identity["lower_boundary"], "upper_boundary": identity["upper_boundary"], "rungs": rungs, "orders": [], "fills": [], "hard_stop_protection": None, "events": [], "retry_events": [], "status": status, "created_at": timestamp}
        state["midpoint"] = identity["midpoint"]
        if blocker:
            state["blocker"] = blocker
        return state

    @staticmethod
    def _identity(plan: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(plan, Mapping) or plan.get("schema_version") != "strategy-plan-v1" or str(plan.get("strategy_type") or "").lower() != "grid":
            raise ValueError("Grid Testnet lifecycle requires a versioned Grid StrategyPlan")
        for key in ("strategy_plan_id", "plan_digest", "cycle_id", "strategy_session_id", "strategy_revision_id"):
            if not str(plan.get(key) or "").strip():
                raise ValueError(f"Grid StrategyPlan identity is incomplete: {key}")
        normalized = plan.get("normalized_input") if isinstance(plan.get("normalized_input"), Mapping) else {}
        upper = float(plan.get("upper_price_boundary") or normalized.get("upper_price_boundary") or (plan.get("grid") or {}).get("upper_boundary") or (plan.get("range") or {}).get("high") or 0)
        lower = float(plan.get("lower_price_boundary") or normalized.get("lower_price_boundary") or (plan.get("grid") or {}).get("lower_boundary") or (plan.get("range") or {}).get("low") or 0)
        if lower <= 0 or upper <= lower:
            raise ValueError("Grid boundaries are invalid")
        direction = str(plan.get("direction") or normalized.get("direction") or "").lower()
        if direction not in {"long", "short", "neutral"}:
            raise ValueError("Grid direction must be long, short, or neutral")
        midpoint = float(plan.get("midpoint") or normalized.get("midpoint") or (plan.get("grid") or {}).get("midpoint") or (lower + upper) / 2.0)
        if not lower < midpoint < upper:
            raise ValueError("Grid midpoint must be strictly inside its boundaries")
        instrument_id = str(plan.get("instrument_id") or (plan.get("execution_context") or {}).get("instrument_id") or "").strip()
        if not instrument_id:
            raise ValueError("Grid instrument identity is required")
        return {"plan_id": str(plan["strategy_plan_id"]), "version": int(plan.get("version") or 0), "cycle_id": str(plan["cycle_id"]), "session_id": str(plan["strategy_session_id"]), "revision_id": str(plan["strategy_revision_id"]), "plan_digest": str(plan["plan_digest"]), "instrument_id": instrument_id, "direction": direction, "lower_boundary": lower, "upper_boundary": upper, "midpoint": midpoint, "created_at": str(plan.get("locked_at") or "")}

    @staticmethod
    def _validate_risk_inputs(plan: Mapping[str, Any]) -> None:
        risk = plan.get("risk_budget") if isinstance(plan.get("risk_budget"), Mapping) else {}
        for key in ("maximum_loss_at_full_depth", "equity", "leverage_limit", "max_notional", "max_open_orders", "max_open_positions", "max_slippage"):
            if risk.get(key) in (None, "") or float(risk[key]) <= 0:
                raise GridTestnetLifecycleError(f"{key} is required before Testnet writes")

    @staticmethod
    def _validate_full_depth_risk(plan: Mapping[str, Any], rungs: list[dict[str, Any]]) -> None:
        risk = plan.get("risk_budget") if isinstance(plan.get("risk_budget"), Mapping) else {}
        maximum_loss = float(risk.get("maximum_loss_at_full_depth") or 0.0)
        modeled_loss = sum(
            (float(rung["price"]) - float(rung["hard_stop"])) * float(rung["quantity"])
            if rung["side"] == "buy"
            else (float(rung["hard_stop"]) - float(rung["price"])) * float(rung["quantity"])
            for rung in rungs
        )
        max_open_orders = int(risk.get("max_open_orders") or 0)
        if max_open_orders < len(rungs):
            raise GridTestnetLifecycleError("max_open_orders_exceeded_at_initial_ladder")
        max_open_positions = int(risk.get("max_open_positions") or 0)
        if max_open_positions < len(rungs):
            raise GridTestnetLifecycleError("max_open_positions_exceeded_at_full_depth")
        total_notional = sum(float(rung["price"]) * float(rung["quantity"]) for rung in rungs)
        if total_notional > float(risk.get("max_notional") or 0.0) + 1e-9:
            raise GridTestnetLifecycleError("max_notional_exceeded_at_full_depth")
        equity = float(risk.get("equity") or 0.0)
        if equity <= 0 or total_notional / equity > float(risk.get("leverage_limit") or 0.0) + 1e-9:
            raise GridTestnetLifecycleError("leverage_exceeded_at_full_depth")
        # Canonical execution rounding may produce adjacent quantity steps
        # (for example .00013 and .00012).  Each rung remains authoritative;
        # aggregate notional and loss checks below are the risk gate.
        grid = plan.get("grid") if isinstance(plan.get("grid"), Mapping) else {}
        spacing = grid.get("spacing")
        mode = str(grid.get("mode") or "arithmetic").lower()
        if spacing not in (None, "") and mode != "geometric":
            ordered = sorted(float(rung["price"]) for rung in rungs)
            if any(abs((right - left) - float(spacing)) > max(1e-9, abs(float(spacing)) * 1e-6) for left, right in zip(ordered, ordered[1:])):
                raise GridTestnetLifecycleError("grid_spacing_inconsistent")
        # Dashboard/canonical Grid publishes monetary risk to cents. Compare
        # at that contract precision after using the exact rounded quantities;
        # all structural risk caps above remain exact and fail closed.
        if round(modeled_loss, 2) > round(maximum_loss, 2):
            raise GridTestnetLifecycleError("maximum_loss_budget_exceeded_at_full_depth")

    @staticmethod
    def _risk_within_budget(plan: Mapping[str, Any], state: Mapping[str, Any]) -> bool:
        risk = plan.get("risk_budget") if isinstance(plan.get("risk_budget"), Mapping) else {}
        stop_loss = sum(
            (
                float(rung["price"]) - float(rung["hard_stop"])
                if rung["side"] == "buy"
                else float(rung["hard_stop"]) - float(rung["price"])
            ) * GridLineLifecycle.from_snapshot(rung["line"]).open_quantity
            for rung in state["rungs"]
        )
        exposure = sum(float(rung["price"]) * GridLineLifecycle.from_snapshot(rung["line"]).open_quantity for rung in state["rungs"])
        equity = float(risk.get("equity") or 0.0)
        open_rung_count = sum(1 for rung in state["rungs"] if GridLineLifecycle.from_snapshot(rung["line"]).open_quantity > 1e-9)
        return open_rung_count <= int(risk.get("max_open_positions") or 0) and stop_loss <= float(risk.get("maximum_loss_at_full_depth") or 0.0) + 1e-9 and exposure <= float(risk.get("max_notional") or 0.0) + 1e-9 and exposure / equity <= float(risk.get("leverage_limit") or 0.0) + 1e-9

    def _state(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        identity = self._identity(plan)
        state = self._states.get(identity["plan_id"]) or self._load(identity["plan_id"])
        if state is None:
            raise GridTestnetLifecycleError("Grid Testnet lifecycle has not started")
        self._assert_state_identity(state, identity)
        self._states[identity["plan_id"]] = state
        return state

    @staticmethod
    def _assert_state_identity(state: Mapping[str, Any], identity: Mapping[str, Any]) -> None:
        expected = {"strategy_plan_id": identity["plan_id"], "strategy_plan_version": identity["version"], "cycle_id": identity["cycle_id"], "strategy_session_id": identity["session_id"], "strategy_revision_id": identity["revision_id"], "plan_digest": identity["plan_digest"], "instrument_id": identity["instrument_id"], "direction": identity["direction"], "midpoint": identity["midpoint"], "lower_boundary": identity["lower_boundary"], "upper_boundary": identity["upper_boundary"]}
        if any(state.get(key) != value for key, value in expected.items()):
            raise GridTestnetLifecycleError("strategy_revision_mismatch")

    def _rung(self, state: dict[str, Any], rung_id: str) -> dict[str, Any]:
        row = next((rung for rung in state["rungs"] if str(rung.get("rung_id")) == rung_id), None)
        if row is None:
            raise GridTestnetLifecycleError("unknown_grid_rung")
        return row

    @staticmethod
    def _order_id_for_client(state: Mapping[str, Any], client_id: str) -> str:
        row = next((item for item in state["orders"] if item.get("client_order_id") == client_id), None)
        if row is None:
            raise GridTestnetLifecycleError("unknown_grid_client_identity")
        return str(row["order_id"])

    @staticmethod
    def _deadline(timestamp: str, plan: Mapping[str, Any]) -> str:
        from datetime import datetime, timedelta
        try:
            value = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00")) + timedelta(seconds=float((plan.get("grid") or {}).get("partial_entry_timeout_seconds") or 300))
            return value.isoformat()
        except ValueError as exc:
            raise GridTestnetLifecycleError("timestamp_invalid") from exc

    def _apply_fill_receipt(
        self,
        state: dict[str, Any],
        order: Mapping[str, Any],
        raw_fill: Mapping[str, Any],
    ) -> Any:
        """Recover a persisted public intent once, then query its fill receipt."""

        try:
            return self.broker.canonical_order_adapter.apply_fill(raw_fill)
        except Exception as first_error:
            recover = getattr(self.broker, "recover", None)
            broker_order_id = str(order.get("broker_order_id") or "").strip()
            if not callable(recover) or not broker_order_id:
                raise first_error
            request = BrokerOrderRequest(
                run_date=state["cycle_id"],
                ticket=dict(order),
                latest_price=float(order.get("price") or order.get("limit_price") or 0),
                actual_size=float(order.get("quantity") or 0),
            )
            recover(
                request,
                broker_order_id=broker_order_id,
                state=str(order.get("state") or "resting"),
            )
            return self.broker.canonical_order_adapter.apply_fill(raw_fill)

    def _identity_metadata(self) -> dict[str, Any]:
        return {"broker_id": self.broker.broker_config["broker_id"], "environment": self.broker.broker_config["environment"], "account_id": self.broker.broker_config["account_id"], "release_sha": self.broker.broker_config["release_sha"], "ledger_namespace": self.broker.broker_config["ledger_namespace"], "source": "standard-broker.testnet", "mapping_revision": self.broker.broker_config["release_sha"]}

    @staticmethod
    def _require_cancel_receipt(receipt: Any) -> None:
        status = str(getattr(receipt, "state", "") or "").lower()
        if hasattr(getattr(receipt, "state", None), "value"):
            status = str(receipt.state.value).lower()
        if status not in {"cancel_pending", "canceled", "cancelled"}:
            raise GridTestnetLifecycleError(f"cancel_receipt_{status or 'unknown'}")

    def _provenance_dict(self, receipt: Any) -> dict[str, Any]:
        provenance = getattr(receipt, "provenance", None)
        return {key: getattr(provenance, key) for key in ("source", "execution_scope", "transport_state", "mapping_revision")} if provenance is not None else {}

    def _record_event(self, state: dict[str, Any], event: str, *, timestamp: str, **payload: Any) -> None:
        state.setdefault("events", []).append({"event": event, "timestamp": timestamp, **self._identity_metadata(), "strategy_session_id": state.get("strategy_session_id"), "strategy_revision_id": state.get("strategy_revision_id"), "plan_digest": state.get("plan_digest"), **payload})

    def _block(self, state: dict[str, Any], reason: str, *, timestamp: str) -> None:
        state["status"] = "blocked_reconciliation" if any(token in reason for token in ("cancel", "fill", "reconciliation", "position")) else "blocked_protection" if any(token in reason for token in ("protect", "capability")) else "blocked_risk"
        state["blocker"] = reason
        self._record_event(state, "lifecycle_blocked", timestamp=timestamp, reason=reason)

    def _path(self, plan_id: str) -> Path:
        return self.output_root / "dualtrack" / "grid_testnet_lifecycle" / f"{plan_id}.json"

    def _load(self, plan_id: str) -> dict[str, Any] | None:
        rows = load_json(self._path(plan_id))
        return dict(rows[-1]) if rows and isinstance(rows[-1], dict) else None

    def _save(self, state: dict[str, Any]) -> None:
        write_json(self._path(str(state["strategy_plan_id"])), [state])
