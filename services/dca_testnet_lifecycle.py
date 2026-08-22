"""Sequential DCA lifecycle over the canonical approved Testnet adapter."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from services.broker_port import BrokerCancelRequest, BrokerOrderRequest
from services.journal_store import load_json, write_json
from services.standard_broker_testnet import (
    StandardBrokerTestnetExecutionAdapter,
    StandardBrokerTestnetHostError,
)


class DcaTestnetLifecycleError(RuntimeError):
    """DCA Testnet lifecycle blocker; callers must not continue adding risk."""


class DcaTestnetLifecycle:
    """Run one sequential DCA deal against an approved local Testnet fixture."""

    schema_version = "testnet-dca-lifecycle-v1"

    def __init__(
        self,
        output_root: Path,
        broker: StandardBrokerTestnetExecutionAdapter,
    ) -> None:
        self.output_root = Path(output_root)
        self.broker = broker
        self._states: dict[str, dict[str, Any]] = {}

    def start(self, plan: dict[str, Any], *, timestamp: str) -> dict[str, Any]:
        identity = self._identity(plan)
        self._validate_risk_inputs(plan)
        existing = self._load(identity["plan_id"])
        if existing is not None and (
            int(existing.get("strategy_plan_version") or 0) != identity["version"]
            or str(existing.get("direction") or "") != identity["direction"]
            or [float(value) for value in existing.get("entry_levels") or []] != identity["entry_levels"]
            or str(existing.get("plan_digest") or "") != identity["plan_digest"]
            or str(existing.get("instrument_id") or "") != identity["instrument_id"]
            or str(existing.get("cycle_id") or "") != identity["cycle_id"]
            or float(existing.get("target_price") or 0) != identity["target_price"]
            or float(existing.get("stop_price") or 0) != identity["stop_price"]
            or existing.get("risk_budget") != identity["risk_budget"]
            or str(existing.get("strategy_session_id") or "") != identity["strategy_session_id"]
            or str(existing.get("strategy_revision_id") or "") != identity["strategy_revision_id"]
        ):
            self._block(existing, "strategy_revision_mismatch", timestamp=timestamp)
            self._save(existing)
            raise DcaTestnetLifecycleError(existing["blocker"])
        if existing is not None:
            self._states[identity["plan_id"]] = existing
            if existing.get("status") in {
                "terminal",
                "blocked_protection",
                "blocked_reconciliation",
                "blocked_risk",
                "stopping",
                "protection_blocked_flattening",
                "target_triggered",
            }:
                return self.snapshot(plan)
            return self.snapshot(plan)
        if self.broker.protection_adapter is None or self.broker.account_adapter is None:
            blocker = (
                "capability_gap:protection_order.submit"
                if self.broker.protection_adapter is None
                else "capability_gap:account.read"
            )
            state = {
                "schema_version": self.schema_version,
                "strategy_plan_id": identity["plan_id"],
                "strategy_plan_version": identity["version"],
                "cycle_id": identity["cycle_id"],
                "direction": identity["direction"],
                "entry_levels": identity["entry_levels"],
                "plan_digest": identity["plan_digest"],
                "instrument_id": identity["instrument_id"],
                "target_price": identity["target_price"],
                "stop_price": identity["stop_price"],
                "risk_budget": identity["risk_budget"],
                "strategy_session_id": identity["strategy_session_id"],
                "strategy_revision_id": identity["strategy_revision_id"],
                "orders": [],
                "fills": [],
                "positions": [],
                "protection": None,
                "events": [],
                "status": "blocked_protection" if self.broker.protection_adapter is None else "blocked_reconciliation",
                "blocker": blocker,
                "created_at": timestamp,
            }
            self._record_event(state, "lifecycle_blocked", timestamp=timestamp, reason=state["blocker"])
            self._states[identity["plan_id"]] = state
            self._save(state)
            return self.snapshot(plan)
        state = {
            "schema_version": self.schema_version,
            "strategy_plan_id": identity["plan_id"],
            "strategy_plan_version": identity["version"],
            "cycle_id": identity["cycle_id"],
            "cycle_id": identity["cycle_id"],
            "direction": identity["direction"],
            "entry_levels": identity["entry_levels"],
            "plan_digest": identity["plan_digest"],
            "instrument_id": identity["instrument_id"],
            "target_price": identity["target_price"],
            "stop_price": identity["stop_price"],
            "risk_budget": identity["risk_budget"],
            "strategy_session_id": identity["strategy_session_id"],
            "strategy_revision_id": identity["strategy_revision_id"],
            "next_entry_index": 0,
            "orders": [],
            "fills": [],
            "positions": [],
            "protection": None,
            "events": [],
            "status": "starting",
            "created_at": timestamp,
        }
        self._states[identity["plan_id"]] = state
        self._record_event(state, "lifecycle_started", timestamp=timestamp)
        if not state["orders"]:
            self._submit_next_entry(plan, state, timestamp=timestamp)
            self._record_event(state, "entry_submitted", timestamp=timestamp)
        if state["status"] == "starting":
            state["status"] = "waiting_entry"
        state["updated_at"] = timestamp
        self._save(state)
        return self.snapshot(plan)

    def snapshot(self, plan: dict[str, Any]) -> dict[str, Any]:
        identity = self._identity(plan)
        state = self._states.get(identity["plan_id"]) or self._load(identity["plan_id"])
        if state is None:
            raise DcaTestnetLifecycleError("DCA Testnet lifecycle has not started")
        self._assert_state_identity(state, identity)
        return json.loads(json.dumps(state, sort_keys=True))

    def on_fill(
        self,
        plan: dict[str, Any],
        raw_fill: dict[str, Any],
        *,
        timestamp: str,
    ) -> dict[str, Any]:
        identity = self._identity(plan)
        state = self._state(plan)
        order_id = str(raw_fill.get("order_id") or "").strip()
        if not order_id:
            client_id = str(raw_fill.get("cloid") or raw_fill.get("client_order_id") or "")
            try:
                order_id = self._order_id_for_client(state, client_id)
            except DcaTestnetLifecycleError as exc:
                self._block(state, "unknown_fill_client_identity", timestamp=timestamp)
                self._save(state)
                raise DcaTestnetLifecycleError(state["blocker"]) from exc
        order = self._find_order(state, order_id)
        if order is None:
            self._block(state, "unknown_fill_order", timestamp=timestamp)
            self._save(state)
            raise DcaTestnetLifecycleError(state["blocker"])
        if state["status"] in {
            "terminal",
            "blocked_reconciliation",
            "blocked_risk",
        } or (
            state["status"] in {"stopping", "blocked_protection", "blocked_risk_flattening"}
            and self._is_entry_event(order["event"])
        ) or (
            state["status"] in {"target_triggered", "protection_blocked_flattening"}
            and self._is_entry_event(order["event"])
        ):
            self._block(state, "late_fill_after_block_or_terminal", timestamp=timestamp)
            self._save(state)
            raise DcaTestnetLifecycleError(state["blocker"])
        try:
            receipt = self.broker.canonical_order_adapter.apply_fill(raw_fill)
        except Exception as exc:  # noqa: BLE001 - freeze at the lifecycle seam.
            self._block(state, f"fill_rejected:{type(exc).__name__}:{exc}", timestamp=timestamp)
            self._save(state)
            raise DcaTestnetLifecycleError(state["blocker"]) from exc
        fill_identities = [
            str(raw_fill.get(key) or "")
            for key in ("tid", "hash")
            if str(raw_fill.get(key) or "")
        ]
        fill_id = fill_identities[0] if fill_identities else ""
        if any(
            set(fill_identities).intersection(
                set(row.get("fill_identities") or [str(row.get("fill_id") or "")])
            )
            for row in state["fills"]
        ):
            return self.snapshot(plan)
        receipt_state = str(getattr(receipt.state, "value", receipt.state) or "").lower()
        if receipt_state in {"unknown", "rejected"}:
            self._block(state, f"fill_receipt_{receipt_state}", timestamp=timestamp)
            self._save(state)
            raise DcaTestnetLifecycleError(state["blocker"])
        quantity = float(raw_fill.get("sz") or raw_fill.get("quantity") or 0.0)
        price = float(receipt.average_fill_price or raw_fill.get("px") or 0)
        planned_price = float(order.get("planned_price") or order.get("price") or 0.0)
        max_slippage = plan.get("risk_budget", {}).get("max_slippage")
        slippage = abs(price - planned_price)
        fill = {
            "fill_id": fill_id,
            "fill_identities": fill_identities,
            "order_id": order_id,
            "event": order["event"],
            "quantity": quantity,
            "price": price,
            "planned_price": planned_price,
            "slippage": slippage,
            "strategy_plan_id": identity["plan_id"],
            "environment": "testnet",
            "account_id": self.broker.broker_config["account_id"],
            "release_sha": self.broker.broker_config["release_sha"],
            "timestamp": timestamp,
        }
        state["fills"].append(fill)
        self._record_event(state, "fill_received", timestamp=timestamp, fill_id=fill_id, order_id=order_id)
        order["state"] = "partial" if receipt_state == "partially_filled" else receipt_state
        order["filled_quantity"] = float(receipt.filled_quantity)
        order["average_fill_price"] = price
        slippage_exceeded = max_slippage not in (None, "") and slippage > float(max_slippage)
        is_entry_event = self._is_entry_event(order["event"])
        if receipt_state == "partially_filled" and not is_entry_event:
            group_before_exit = self._protection_group(plan, state, state["positions"][0]) if state["positions"] else None
            self._decrease_position(state, quantity)
            if sum(float(row["quantity"]) for row in state["positions"]) <= 1e-9:
                self._complete_terminal_exit(plan, state, order=order, timestamp=timestamp, group_before_exit=group_before_exit)
            else:
                self._confirm_or_update_protection(plan, state, timestamp=timestamp)
                if state["status"] not in {"blocked_protection", "blocked_reconciliation"}:
                    state["status"] = "stopping" if order["event"] == "stop" else "target_triggered"
            state["updated_at"] = timestamp
            self._save(state)
            return self.snapshot(plan)
        if receipt_state == "partially_filled":
            self._increase_position(state, order, quantity, price)
            if slippage_exceeded:
                self._block(state, "fill_slippage_exceeded", timestamp=timestamp)
                self._record_event(state, "slippage_budget_breached", timestamp=timestamp, planned_price=planned_price, actual_price=price, slippage=slippage)
                self._flatten_after_block(plan, state, timestamp=timestamp, reason="slippage")
            elif self._risk_within_budget(plan, state):
                self._confirm_or_update_protection(plan, state, timestamp=timestamp)
            else:
                self._block(state, "maximum_loss_budget_exceeded", timestamp=timestamp)
                self._flatten_after_block(plan, state, timestamp=timestamp, reason="risk_budget")
            if state["status"] not in {"blocked_protection", "blocked_risk", "blocked_risk_flattening"}:
                state["status"] = "partial_entry"
            elif state["status"] == "blocked_protection":
                self._flatten_after_protection_failure(plan, state, timestamp=timestamp)
            state["updated_at"] = timestamp
            self._save(state)
            return self.snapshot(plan)
        if is_entry_event:
            self._increase_position(state, order, quantity, price)
            if slippage_exceeded:
                self._block(state, "fill_slippage_exceeded", timestamp=timestamp)
                self._record_event(state, "slippage_budget_breached", timestamp=timestamp, planned_price=planned_price, actual_price=price, slippage=slippage)
                self._flatten_after_block(plan, state, timestamp=timestamp, reason="slippage")
            elif not self._risk_within_budget(plan, state):
                self._block(state, "maximum_loss_budget_exceeded", timestamp=timestamp)
                self._flatten_after_block(plan, state, timestamp=timestamp, reason="risk_budget")
            else:
                self._confirm_or_update_protection(plan, state, timestamp=timestamp)
            if state["status"] not in {"blocked_protection", "blocked_risk", "blocked_risk_flattening"}:
                self._submit_next_entry(plan, state, timestamp=timestamp)
                if state["status"] not in {"budget_exhausted", "blocked_protection", "blocked_risk", "blocked_risk_flattening"}:
                    state["status"] = "open"
            elif state["status"] == "blocked_protection":
                self._flatten_after_protection_failure(plan, state, timestamp=timestamp)
        else:
            group_before_exit = self._protection_group(plan, state, state["positions"][0]) if state["positions"] else None
            self._decrease_position(state, quantity)
            if sum(float(row["quantity"]) for row in state["positions"]) <= 1e-9:
                self._complete_terminal_exit(plan, state, order=order, timestamp=timestamp, group_before_exit=group_before_exit)
        state["updated_at"] = timestamp
        self._save(state)
        return self.snapshot(plan)

    def on_market_event(
        self,
        plan: dict[str, Any],
        *,
        price: float,
        timestamp: str,
    ) -> dict[str, Any]:
        identity = self._identity(plan)
        state = self._state(plan)
        if state["status"] in {
            "terminal",
            "blocked_protection",
            "blocked_reconciliation",
            "blocked_risk",
            "blocked_risk_flattening",
            "target_triggered",
            "protection_blocked_flattening",
            "stopping",
            "stopped",
        }:
            return self.snapshot(plan)
        target = float(plan["dca"]["target_price"])
        stop = float(plan["dca"]["stop_price"])
        target_hit = price >= target if identity["direction"] == "long" else price <= target
        stop_hit = price <= stop if identity["direction"] == "long" else price >= stop
        if stop_hit:
            if state["positions"]:
                return self.stop(plan, timestamp=timestamp, reason="strategy_stop", price=stop)
            if not self._cancel_entries(state, timestamp=timestamp, reason="strategy_stop_before_entry"):
                state["updated_at"] = timestamp
                self._save(state)
                return self.snapshot(plan)
            state["status"] = "stopped"
            state["sealed"] = True
            state["terminal_reason"] = "strategy_stop_before_entry"
            self._queue_park_notification(state, timestamp=timestamp, reason="strategy_stop_before_entry")
            state["next_action"] = "notify_park_and_wait"
            self._record_event(state, "revision_sealed", timestamp=timestamp, reason="strategy_stop_before_entry")
            state["updated_at"] = timestamp
            self._save(state)
            return self.snapshot(plan)
        if target_hit and state["positions"]:
            if not self._cancel_entries(state, timestamp=timestamp, reason="take_profit"):
                state["updated_at"] = timestamp
                self._save(state)
                return self.snapshot(plan)
            position = state["positions"][0]
            self._submit_exit(plan, state, timestamp=timestamp, price=target, quantity=float(position["quantity"]), event="target")
            state["status"] = "target_triggered"
            state["updated_at"] = timestamp
            self._save(state)
        self._catch_up_crossed_entry(plan, state, price=price, timestamp=timestamp)
        state["updated_at"] = timestamp
        self._save(state)
        return self.snapshot(plan)

    def stop(
        self,
        plan: dict[str, Any],
        *,
        timestamp: str,
        reason: str,
        price: float,
    ) -> dict[str, Any]:
        state = self._state(plan)
        if state["status"] in {
            "terminal",
            "sealed",
            "blocked_reconciliation",
            "blocked_risk_flattening",
            "target_triggered",
            "protection_blocked_flattening",
            "stopped",
        }:
            return self.snapshot(plan)
        if not self._cancel_entries(state, timestamp=timestamp, reason=reason):
            state["updated_at"] = timestamp
            self._save(state)
            return self.snapshot(plan)
        quantity = sum(float(row["quantity"]) for row in state["positions"])
        if quantity > 1e-9:
            self._submit_exit(plan, state, timestamp=timestamp, price=price, quantity=quantity, event="stop")
        state["status"] = "stopping"
        state["updated_at"] = timestamp
        self._save(state)
        return self.snapshot(plan)

    def _confirm_or_update_protection(
        self,
        plan: dict[str, Any],
        state: dict[str, Any],
        *,
        timestamp: str,
    ) -> None:
        if self.broker.protection_adapter is None:
            self._block(state, "capability_gap:protection_order.submit", timestamp=timestamp)
            return
        position = state["positions"][0]
        group = self._protection_group(plan, state, position)
        try:
            if state.get("protection") is None:
                submitted = self.broker.request("protection_order", "submit", group)
                confirmed = self.broker.request("protection_order", "query", group)
            else:
                submitted = self.broker.request("protection_order", "replace", group)
                confirmed = self.broker.request("protection_order", "query", group)
            state["protection"] = {
                "protection_id": group.protection_id,
                "status": "active",
                "quantity": float(group.quantity),
                "tp": float(group.take_profit.trigger_price),
                "sl": float(group.stop_loss.trigger_price),
                "reduce_only": True,
                "submitted_operation": submitted.operation,
                "confirmed_operation": confirmed.operation,
                "confirmed_at": timestamp,
                "submitted_provenance": self._provenance_dict(submitted),
                "confirmed_provenance": self._provenance_dict(confirmed),
                "environment": self.broker.broker_config["environment"],
                "account_id": self.broker.broker_config["account_id"],
                "release_sha": self.broker.broker_config["release_sha"],
                "ledger_namespace": self.broker.broker_config["ledger_namespace"],
            }
            self._record_event(state, "protection_confirmed", timestamp=timestamp, protection_id=group.protection_id)
        except Exception as exc:  # noqa: BLE001 - no new entry after coverage failure.
            self._block(
                state,
                f"protection_update_failed:{type(exc).__name__}:{exc}",
                timestamp=timestamp,
            )

    def _flatten_after_protection_failure(
        self,
        plan: dict[str, Any],
        state: dict[str, Any],
        *,
        timestamp: str,
    ) -> None:
        if not self._cancel_entries(state, timestamp=timestamp, reason="protection_failure"):
            return
        quantity = sum(float(row["quantity"]) for row in state["positions"])
        if quantity <= 1e-9:
            return
        self._submit_exit(
            plan,
            state,
            timestamp=timestamp,
            price=float(plan["dca"]["stop_price"]),
            quantity=quantity,
            event="stop",
        )
        state["status"] = "protection_blocked_flattening"
        self._record_event(state, "protection_failure_flatten_submitted", timestamp=timestamp)

    def _complete_terminal_exit(
        self,
        plan: dict[str, Any],
        state: dict[str, Any],
        *,
        order: dict[str, Any],
        timestamp: str,
        group_before_exit: Any | None = None,
    ) -> None:
        try:
            if group_before_exit is not None and state.get("protection") is not None:
                self.broker.request("protection_order", "cancel", group_before_exit)
            state["positions"] = []
            reconciliation = self._terminal_reconciliation(state, timestamp)
            state["reconciliation"] = reconciliation
            if reconciliation["status"] != "ok":
                self._block(state, "terminal_reconciliation_blocked", timestamp=timestamp)
            else:
                state["status"] = "terminal"
                state["terminal_reason"] = order["event"]
                state["protection"] = None
                state["sealed"] = True
                self._queue_park_notification(state, timestamp=timestamp, reason=order["event"])
                state["next_action"] = "notify_park_and_wait"
                self._record_event(state, "revision_sealed", timestamp=timestamp, reason=order["event"])
                self._record_event(state, "terminal_closed", timestamp=timestamp, reason=order["event"])
        except Exception as exc:  # noqa: BLE001 - terminal protection close remains blocked.
            self._block(
                state,
                f"terminal_protection_cancel_failed:{type(exc).__name__}:{exc}",
                timestamp=timestamp,
            )

    def _flatten_after_block(
        self,
        plan: dict[str, Any],
        state: dict[str, Any],
        *,
        timestamp: str,
        reason: str,
    ) -> None:
        """Record the actual fill, then submit a reduce-only recovery leg."""

        if not self._cancel_entries(state, timestamp=timestamp, reason=f"{reason}_budget"):
            state["status"] = "blocked_reconciliation"
            return
        quantity = sum(float(row["quantity"]) for row in state["positions"])
        if quantity <= 1e-9:
            return
        try:
            self._submit_exit(
                plan,
                state,
                timestamp=timestamp,
                price=float(plan["dca"]["stop_price"]),
                quantity=quantity,
                event="risk_recovery",
            )
            state["status"] = "blocked_risk_flattening"
            self._record_event(
                state,
                "risk_recovery_submitted",
                timestamp=timestamp,
                reason=reason,
                quantity=quantity,
            )
        except DcaTestnetLifecycleError:
            state["status"] = "blocked_reconciliation"

    def _queue_park_notification(self, state: dict[str, Any], *, timestamp: str, reason: str) -> None:
        """Persist one idempotent Telegram notification intent for Park."""

        notification_id = f"dca-terminal:{state['strategy_plan_id']}:{state['plan_digest']}"
        state["park_notification_required"] = True
        state["park_notification"] = {
            "notification_id": notification_id,
            "channel": "telegram",
            "status": "queued",
            "reason": reason,
            "strategy_session_id": state.get("strategy_session_id", ""),
            "strategy_revision_id": state.get("strategy_revision_id", ""),
            "plan_digest": state.get("plan_digest", ""),
            "queued_at": timestamp,
            "next_action": "notify_park_and_wait",
        }
        self._record_event(state, "park_notification_queued", timestamp=timestamp, notification_id=notification_id, reason=reason)

    def _terminal_reconciliation(self, state: dict[str, Any], timestamp: str) -> dict[str, Any]:
        try:
            broker_open_orders = self.broker.request(
                "order_execution",
                "open_orders",
                str(state["orders"][0].get("instrument_id") or ""),
            )
            broker_open_count = len(tuple(broker_open_orders or ()))
        except Exception as exc:  # noqa: BLE001 - venue truth is required before sealing.
            return {
                "status": "blocked",
                "at": timestamp,
                "reason": f"broker_open_order_query_failed:{type(exc).__name__}:{exc}",
                **self._identity_metadata(),
            }
        try:
            broker_account = self.broker.request(
                "account",
                "read",
                self.broker.broker_config["account_id"],
            )
            broker_positions = tuple(
                position
                for position in getattr(broker_account, "positions", ())
                if abs(float(getattr(position, "signed_quantity", 0) or 0)) > 1e-9
            )
        except Exception as exc:  # noqa: BLE001 - remote position truth is required before sealing.
            return {
                "status": "blocked",
                "at": timestamp,
                "reason": f"broker_position_query_failed:{type(exc).__name__}:{exc}",
                **self._identity_metadata(),
            }
        open_orders = [row for row in state["orders"] if row.get("state") == "accepted"]
        status = "ok" if not open_orders and broker_open_count == 0 and not state["positions"] and not broker_positions else "blocked"
        return {
            "status": status,
            "at": timestamp,
            "open_order_count": len(open_orders),
            "broker_open_order_count": broker_open_count,
            "open_position_count": len(state["positions"]),
            "broker_position_count": len(broker_positions),
            **self._identity_metadata(),
        }

    def _record_event(self, state: dict[str, Any], event: str, *, timestamp: str, **payload: Any) -> None:
        state.setdefault("events", []).append(
            {
                "event": event,
                "timestamp": timestamp,
                "strategy_session_id": state.get("strategy_session_id", ""),
                "strategy_revision_id": state.get("strategy_revision_id", ""),
                "plan_digest": state.get("plan_digest", ""),
                **payload,
                **self._identity_metadata(),
            }
        )

    def _block(self, state: dict[str, Any], reason: str, *, timestamp: str) -> None:
        state["status"] = (
            "blocked_reconciliation"
            if any(token in reason for token in ("cancel", "fill", "reconciliation", "position_query"))
            else "blocked_protection"
            if "protect" in reason or "capability" in reason
            else "blocked_risk"
        )
        state["blocker"] = reason
        self._record_event(state, "lifecycle_blocked", timestamp=timestamp, reason=reason)

    def _identity_metadata(self) -> dict[str, Any]:
        return {
            "broker_id": self.broker.broker_config["broker_id"],
            "environment": self.broker.broker_config["environment"],
            "account_id": self.broker.broker_config["account_id"],
            "release_sha": self.broker.broker_config["release_sha"],
            "ledger_namespace": self.broker.broker_config["ledger_namespace"],
            "source": "standard-broker.testnet",
            "mapping_revision": self.broker.broker_config["release_sha"],
        }

    @staticmethod
    def _provenance_dict(receipt: Any) -> dict[str, Any]:
        provenance = getattr(receipt, "provenance", None)
        if provenance is None:
            return {}
        return {
            "source": provenance.source,
            "execution_scope": provenance.execution_scope,
            "transport_state": provenance.transport_state,
            "mapping_revision": provenance.mapping_revision,
        }

    @staticmethod
    def _validate_risk_inputs(plan: dict[str, Any]) -> None:
        risk = plan.get("risk_budget") if isinstance(plan.get("risk_budget"), dict) else {}
        required_positive = (
            "maximum_loss_at_full_depth",
            "equity",
            "leverage_limit",
            "max_notional",
            "max_open_orders",
            "max_open_positions",
            "max_slippage",
        )
        for key in required_positive:
            value = risk.get(key)
            if value in (None, "") or float(value) <= 0:
                raise DcaTestnetLifecycleError(
                    f"{key} is required before Testnet writes"
                )

    @staticmethod
    def _risk_within_budget(plan: dict[str, Any], state: dict[str, Any]) -> bool:
        risk = plan.get("risk_budget") if isinstance(plan.get("risk_budget"), dict) else {}
        max_loss = float(risk.get("maximum_loss_at_full_depth") or 0.0)
        stop = float(plan["dca"]["stop_price"])
        loss = sum(
            (float(position["entry_price"]) - stop) * float(position["quantity"])
            if state["direction"] == "long"
            else (stop - float(position["entry_price"])) * float(position["quantity"])
            for position in state["positions"]
        )
        max_notional = float(risk.get("max_notional") or 0.0)
        leverage_limit = float(risk.get("leverage_limit") or 0.0)
        exposure = sum(
            float(position["entry_price"]) * float(position["quantity"])
            for position in state["positions"]
        )
        equity = float(risk.get("equity") or 0.0)
        actual_leverage = exposure / equity if equity > 0 else float("inf")
        return (
            loss <= max_loss + 1e-9
            and exposure <= max_notional + 1e-9
            and actual_leverage <= leverage_limit + 1e-9
        )

    def _submit_next_entry(self, plan: dict[str, Any], state: dict[str, Any], *, timestamp: str) -> None:
        index = int(state["next_entry_index"])
        if index >= len(state["entry_levels"]):
            return
        risk = plan.get("risk_budget") if isinstance(plan.get("risk_budget"), dict) else {}
        if len([row for row in state["orders"] if row.get("state") == "accepted"]) >= int(risk["max_open_orders"]):
            self._block(state, "max_open_orders_exceeded", timestamp=timestamp)
            self._save(state)
            raise DcaTestnetLifecycleError(state["blocker"])
        if len(state["positions"]) >= int(risk["max_open_positions"]):
            # A DCA deal owns one aggregate position identity; additions are
            # allowed only inside that one position, never as a second owner.
            if state["positions"] and state["next_entry_index"] > 0:
                pass
            else:
                self._block(state, "max_open_positions_exceeded", timestamp=timestamp)
                self._save(state)
                raise DcaTestnetLifecycleError(state["blocker"])
        price = float(state["entry_levels"][index])
        quantity = self._entry_quantity(plan, price)
        if not self._projected_entry_within_budget(plan, state, price=price, quantity=quantity):
            state["status"] = "budget_exhausted"
            state["blocker"] = "maximum_loss_budget_exhausted_before_next_entry"
            self._record_event(state, "entry_budget_exhausted", timestamp=timestamp, price=price, quantity=quantity)
            self._save(state)
            return
        command = self._command(plan, state, price=price, quantity=quantity, event="entry", index=index, timestamp=timestamp)
        try:
            receipt = self.broker.submit_order(
                BrokerOrderRequest(
                    run_date=state["cycle_id"],
                    ticket=command,
                    latest_price=price,
                    actual_size=command["quantity"],
                )
            )
        except Exception as exc:  # noqa: BLE001 - ambiguous submit must persist a blocker.
            self._block(state, f"entry_submit_failed:{type(exc).__name__}:{exc}", timestamp=timestamp)
            self._save(state)
            raise DcaTestnetLifecycleError(state["blocker"]) from exc
        try:
            order = self._order_row(command, receipt)
        except DcaTestnetLifecycleError as exc:
            self._block(state, str(exc), timestamp=timestamp)
            self._save(state)
            raise
        state["orders"].append(order)
        state["next_entry_index"] = index + 1

    def _submit_exit(self, plan: dict[str, Any], state: dict[str, Any], *, timestamp: str, price: float, quantity: float, event: str) -> None:
        command = self._command(plan, state, price=price, quantity=quantity, event=event, index=len(state["orders"]), timestamp=timestamp, reduce_only=True)
        try:
            receipt = self.broker.submit_order(
                BrokerOrderRequest(
                    run_date=state["cycle_id"],
                    ticket=command,
                    latest_price=price,
                    actual_size=quantity,
                )
            )
        except Exception as exc:  # noqa: BLE001 - ambiguous submit must persist a blocker.
            self._block(state, f"exit_submit_failed:{type(exc).__name__}:{exc}", timestamp=timestamp)
            self._save(state)
            raise DcaTestnetLifecycleError(state["blocker"]) from exc
        try:
            order = self._order_row(command, receipt)
        except DcaTestnetLifecycleError as exc:
            self._block(state, str(exc), timestamp=timestamp)
            self._save(state)
            raise
        state["orders"].append(order)

    def _cancel_entries(self, state: dict[str, Any], *, timestamp: str, reason: str) -> bool:
        for row in state["orders"]:
            if not self._is_entry_event(row["event"]) or row["state"] != "accepted":
                continue
            try:
                cancel_receipt = self.broker.cancel_order(
                    BrokerCancelRequest(
                        run_date=state["cycle_id"],
                        asset=row["instrument_id"],
                        client_order_id=row["client_order_id"],
                        broker_order_id=row.get("broker_order_id") or "",
                    )
                )
                cancel_state = str(
                    getattr(cancel_receipt.state, "value", cancel_receipt.state) or ""
                ).lower()
                if cancel_state not in {"cancel_pending", "canceled"}:
                    raise DcaTestnetLifecycleError(
                        f"cancel_receipt_{cancel_state or 'unknown'}"
                    )
            except Exception as exc:  # noqa: BLE001 - cancellation uncertainty blocks completion.
                state["status"] = "blocked_reconciliation"
                state["blocker"] = f"entry_cancel_failed:{type(exc).__name__}:{exc}"
                return False
            row["state"] = "cancelled"
            row["cancelled_at"] = timestamp
            row["cancel_reason"] = reason
            self._record_event(
                state,
                "entry_cancelled",
                timestamp=timestamp,
                order_id=row["order_id"],
                reason=reason,
            )
        return True

    def _increase_position(self, state: dict[str, Any], order: dict[str, Any], quantity: float, price: float) -> None:
        position = state["positions"][0] if state["positions"] else None
        if position is None:
            state["positions"] = [{
                "position_id": f"{state['cycle_id']}:dca-position",
                "side": state["direction"],
                "quantity": quantity,
                "entry_price": price,
                "status": "open",
            }]
            return
        total = float(position["quantity"]) + quantity
        position["entry_price"] = (float(position["entry_price"]) * float(position["quantity"]) + price * quantity) / total
        position["quantity"] = total

    @staticmethod
    def _decrease_position(state: dict[str, Any], quantity: float) -> None:
        remaining = max(0.0, sum(float(row["quantity"]) for row in state["positions"]) - quantity)
        if state["positions"]:
            state["positions"][0]["quantity"] = remaining

    @staticmethod
    def _entry_quantity(plan: dict[str, Any], price: float) -> float:
        notional = float(plan["dca"].get("notional_per_addition") or 0.0)
        return round(notional / price, 5)

    def _projected_entry_within_budget(
        self,
        plan: dict[str, Any],
        state: dict[str, Any],
        *,
        price: float,
        quantity: float,
    ) -> bool:
        stop = float(plan["dca"]["stop_price"])
        direction = state["direction"]
        current_loss = sum(
            (float(position["entry_price"]) - stop) * float(position["quantity"])
            if direction == "long"
            else (stop - float(position["entry_price"])) * float(position["quantity"])
            for position in state["positions"]
        )
        added_loss = (price - stop) * quantity if direction == "long" else (stop - price) * quantity
        risk = plan.get("risk_budget") if isinstance(plan.get("risk_budget"), dict) else {}
        current_exposure = sum(
            float(position["entry_price"]) * float(position["quantity"])
            for position in state["positions"]
        )
        projected_exposure = current_exposure + price * quantity
        equity = float(risk.get("equity") or 0.0)
        projected_leverage = projected_exposure / equity if equity > 0 else float("inf")
        return (
            current_loss + added_loss <= float(risk.get("maximum_loss_at_full_depth") or 0.0) + 1e-9
            and projected_exposure <= float(risk.get("max_notional") or 0.0) + 1e-9
            and projected_leverage <= float(risk.get("leverage_limit") or 0.0) + 1e-9
        )

    def _catch_up_crossed_entry(
        self,
        plan: dict[str, Any],
        state: dict[str, Any],
        *,
        price: float,
        timestamp: str,
    ) -> None:
        """Replace a crossed resting entry with a bounded market catch-up order."""

        pending = next(
            (
                row
                for row in reversed(state["orders"])
                if self._is_entry_event(row.get("event")) and row.get("state") == "accepted"
            ),
            None,
        )
        if pending is None:
            return
        planned = float(pending.get("planned_price") or pending.get("price") or 0)
        crossed = price < planned if state["direction"] == "long" else price > planned
        if not crossed:
            return
        max_slippage = float((plan.get("risk_budget") or {}).get("max_slippage") or 0)
        if max_slippage <= 0 or abs(price - planned) > max_slippage:
            self._block(state, "catch_up_slippage_exceeded", timestamp=timestamp)
            self._record_event(state, "catch_up_skipped", timestamp=timestamp, planned_price=planned, market_price=price)
            self._save(state)
            return
        if not self._cancel_entries(state, timestamp=timestamp, reason="catch_up_market"):
            self._save(state)
            return
        index = int(pending.get("strategy_plan_entry_index") or 0)
        attempt = int(state.get("catch_up_attempts") or 0) + 1
        state["catch_up_attempts"] = attempt
        quantity = self._entry_quantity(plan, planned)
        if not self._projected_entry_within_budget(plan, state, price=price, quantity=quantity):
            state["status"] = "budget_exhausted"
            state["blocker"] = "maximum_loss_budget_exhausted_before_catch_up"
            self._record_event(state, "catch_up_budget_exhausted", timestamp=timestamp, market_price=price, quantity=quantity)
            self._save(state)
            return
        command = self._command(
            plan,
            state,
            price=price,
            quantity=quantity,
            event="entry_catch_up",
            index=index,
            timestamp=timestamp,
            order_type="market",
            time_in_force="ioc",
            planned_price=planned,
            attempt=attempt,
        )
        try:
            receipt = self.broker.submit_order(
                BrokerOrderRequest(run_date=state["cycle_id"], ticket=command, latest_price=price, actual_size=quantity)
            )
            state["orders"].append(self._order_row(command, receipt))
            self._record_event(state, "catch_up_submitted", timestamp=timestamp, planned_price=planned, market_price=price)
        except Exception as exc:  # noqa: BLE001 - ambiguous catch-up stops the ladder.
            self._block(state, f"catch_up_submit_failed:{type(exc).__name__}:{exc}", timestamp=timestamp)
        self._save(state)

    def _protection_group(self, plan: dict[str, Any], state: dict[str, Any], position: dict[str, Any]) -> Any:
        from standard_broker import (
            OrderSide,
            ProtectionExecution,
            ProtectionGroup,
            ProtectionLeg,
            ProtectionQuantityPolicy,
            ProtectionType,
        )
        long = state["direction"] == "long"
        return ProtectionGroup(
            protection_id=f"dca-protection:{state['strategy_plan_id']}",
            parent_order_id=str(state["orders"][0]["order_id"]),
            instrument_id=str(state["orders"][0]["instrument_id"]),
            entry_side=OrderSide.BUY if long else OrderSide.SELL,
            entry_price=Decimal(str(position["entry_price"])),
            quantity=Decimal(str(position["quantity"])),
            quantity_policy=ProtectionQuantityPolicy.POSITION_FOLLOWING,
            take_profit=ProtectionLeg(
                protection_type=ProtectionType.TAKE_PROFIT,
                execution=ProtectionExecution.MARKET,
                trigger_price=Decimal(str(plan["dca"]["target_price"])),
            ),
            stop_loss=ProtectionLeg(
                protection_type=ProtectionType.STOP_LOSS,
                execution=ProtectionExecution.MARKET,
                trigger_price=Decimal(str(plan["dca"]["stop_price"])),
            ),
        )

    @staticmethod
    def _command(plan: dict[str, Any], state: dict[str, Any], *, price: float, quantity: float, event: str, index: int, timestamp: str, reduce_only: bool = False, order_type: str = "limit", time_in_force: str = "gtc", planned_price: float | None = None, attempt: int | None = None) -> dict[str, Any]:
        suffix = f":attempt:{attempt}" if attempt is not None else ""
        ticket_id = f"{state['strategy_plan_id']}:{event}:{index}{suffix}"
        return {
            "ticket_id": ticket_id,
            "instrument_id": str(plan.get("instrument_id") or plan.get("execution_context", {}).get("instrument_id") or "BTC-USD-PERP"),
            "side": ("buy" if state["direction"] == "long" else "sell") if DcaTestnetLifecycle._is_entry_event(event) else ("sell" if state["direction"] == "long" else "buy"),
            "order_type": "limit" if order_type == "market" else order_type,
            "limit_price": price if order_type in {"limit", "market"} else None,
            "time_in_force": time_in_force,
            "price": price,
            "execution_semantics": "aggressive_ioc_market" if order_type == "market" else "resting_limit",
            "planned_price": planned_price if planned_price is not None else price,
            "strategy_plan_entry_index": index,
            "quantity": quantity,
            "idempotency_key": ticket_id,
            "reduce_only": reduce_only,
            "close_position": reduce_only,
            "event": event,
            "strategy_plan_id": state["strategy_plan_id"],
            "strategy_plan_version": state["strategy_plan_version"],
            "cycle_id": state["cycle_id"],
            "timestamp": timestamp,
        }

    def _order_row(self, command: dict[str, Any], receipt: Any) -> dict[str, Any]:
        receipt_state = str(getattr(receipt.state, "value", receipt.state) or "").lower()
        if receipt_state in {"unknown", "rejected"}:
            raise DcaTestnetLifecycleError(
                f"order_submit_{receipt_state}:{receipt.order_id}"
            )
        return {
            **command,
            "order_id": str(receipt.order_id),
            "client_order_id": str(receipt.client_order_id),
            "broker_order_id": str(receipt.broker_order_id or ""),
            "environment": "testnet",
            "account_id": receipt.account_address,
            "release_sha": receipt.release_sha,
            "receipt_provenance": self._provenance_dict(receipt),
            "state": (
                "accepted"
                if receipt_state in {"submitting", "resting", "waiting_for_fill", "waiting_for_trigger"}
                else receipt_state
            ),
        }

    def _state(self, plan: dict[str, Any]) -> dict[str, Any]:
        identity = self._identity(plan)
        state = self._states.get(identity["plan_id"]) or self._load(identity["plan_id"])
        if state is None:
            raise DcaTestnetLifecycleError("DCA Testnet lifecycle has not started")
        self._assert_state_identity(state, identity)
        self._states[identity["plan_id"]] = state
        return state

    @staticmethod
    def _assert_state_identity(state: dict[str, Any], identity: dict[str, Any]) -> None:
        expected = {
            "strategy_plan_id": identity["plan_id"],
            "strategy_plan_version": identity["version"],
            "direction": identity["direction"],
            "entry_levels": identity["entry_levels"],
            "plan_digest": identity["plan_digest"],
            "instrument_id": identity["instrument_id"],
            "target_price": identity["target_price"],
            "stop_price": identity["stop_price"],
            "risk_budget": identity["risk_budget"],
            "strategy_session_id": identity["strategy_session_id"],
            "strategy_revision_id": identity["strategy_revision_id"],
        }
        if any(state.get(key) != value for key, value in expected.items()):
            raise DcaTestnetLifecycleError("strategy_revision_mismatch")

    @staticmethod
    def _find_order(state: dict[str, Any], order_id: str) -> dict[str, Any] | None:
        return next((row for row in state["orders"] if str(row.get("order_id")) == order_id), None)

    @staticmethod
    def _is_entry_event(event: object) -> bool:
        return str(event or "") == "entry" or str(event or "").startswith("entry_catch_up")

    @staticmethod
    def _order_id_for_client(state: dict[str, Any], client_id: str) -> str:
        row = next((row for row in state["orders"] if row.get("client_order_id") == client_id), None)
        if row is None:
            raise DcaTestnetLifecycleError("unknown client order identity")
        return str(row["order_id"])

    @staticmethod
    def _identity(plan: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(plan, dict) or plan.get("schema_version") != "strategy-plan-v1" or plan.get("strategy_type") != "dca":
            raise ValueError("DCA Testnet lifecycle requires a versioned DCA StrategyPlan")
        dca = plan.get("dca") if isinstance(plan.get("dca"), dict) else {}
        direction = str(plan.get("direction") or "").lower()
        levels = [float(value) for value in dca.get("entry_levels") or []]
        if (
            direction not in {"long", "short"}
            or not levels
            or not plan.get("strategy_plan_id")
            or not plan.get("cycle_id")
            or not plan.get("plan_digest")
            or not str(plan.get("strategy_session_id") or "").strip()
            or not str(plan.get("strategy_revision_id") or "").strip()
        ):
            raise ValueError("DCA Testnet StrategyPlan identity is incomplete")
        return {
            "plan_id": str(plan["strategy_plan_id"]),
            "cycle_id": str(plan["cycle_id"]),
            "version": int(plan.get("version") or 0),
            "direction": direction,
            "entry_levels": levels,
            "plan_digest": str(plan["plan_digest"]),
            "instrument_id": str(plan.get("instrument_id") or plan.get("execution_context", {}).get("instrument_id") or "BTC-USD-PERP"),
            "target_price": float(dca["target_price"]),
            "stop_price": float(dca["stop_price"]),
            "risk_budget": dict(plan.get("risk_budget") or {}),
            "strategy_session_id": str(plan.get("strategy_session_id") or ""),
            "strategy_revision_id": str(plan.get("strategy_revision_id") or ""),
        }

    def _path(self, plan_id: str) -> Path:
        return self.output_root / "dualtrack" / "dca_testnet_lifecycle" / f"{plan_id}.json"

    def _load(self, plan_id: str) -> dict[str, Any] | None:
        rows = load_json(self._path(plan_id))
        return dict(rows[-1]) if rows and isinstance(rows[-1], dict) else None

    def _save(self, state: dict[str, Any]) -> None:
        write_json(self._path(str(state["strategy_plan_id"])), [state])
