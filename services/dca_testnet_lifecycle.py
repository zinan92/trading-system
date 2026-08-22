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
        state = self._load(identity["plan_id"]) or {
            "schema_version": self.schema_version,
            "strategy_plan_id": identity["plan_id"],
            "strategy_plan_version": identity["version"],
            "cycle_id": identity["cycle_id"],
            "direction": identity["direction"],
            "entry_levels": identity["entry_levels"],
            "next_entry_index": 0,
            "orders": [],
            "fills": [],
            "positions": [],
            "protection": None,
            "status": "starting",
            "created_at": timestamp,
        }
        self._states[identity["plan_id"]] = state
        if not state["orders"]:
            self._submit_next_entry(plan, state, timestamp=timestamp)
        state["status"] = "waiting_entry"
        state["updated_at"] = timestamp
        self._save(state)
        return self.snapshot(plan)

    def snapshot(self, plan: dict[str, Any]) -> dict[str, Any]:
        identity = self._identity(plan)
        state = self._states.get(identity["plan_id"]) or self._load(identity["plan_id"])
        if state is None:
            raise DcaTestnetLifecycleError("DCA Testnet lifecycle has not started")
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
            order_id = self._order_id_for_client(state, client_id)
        try:
            receipt = self.broker.canonical_order_adapter.apply_fill(raw_fill)
        except Exception as exc:  # noqa: BLE001 - freeze at the lifecycle seam.
            state["status"] = "blocked_reconciliation"
            state["blocker"] = f"fill_rejected:{type(exc).__name__}:{exc}"
            self._save(state)
            raise DcaTestnetLifecycleError(state["blocker"]) from exc
        order = self._find_order(state, order_id)
        if order is None:
            raise DcaTestnetLifecycleError("fill references unknown DCA order")
        if str(raw_fill.get("tid") or "") in {str(row.get("fill_id") or "") for row in state["fills"]}:
            return self.snapshot(plan)
        quantity = float(receipt.filled_quantity)
        price = float(receipt.average_fill_price or raw_fill.get("px") or 0)
        fill_id = str(raw_fill.get("tid") or raw_fill.get("hash") or "")
        fill = {
            "fill_id": fill_id,
            "order_id": order_id,
            "event": order["event"],
            "quantity": quantity,
            "price": price,
            "strategy_plan_id": identity["plan_id"],
            "environment": "testnet",
            "account_id": self.broker.broker_config["account_id"],
            "release_sha": self.broker.broker_config["release_sha"],
            "timestamp": timestamp,
        }
        state["fills"].append(fill)
        order["state"] = "filled"
        order["filled_quantity"] = quantity
        order["average_fill_price"] = price
        if order["event"] == "entry":
            self._increase_position(state, order, quantity, price)
            self._confirm_or_update_protection(plan, state, timestamp=timestamp)
            if state["status"] != "blocked_protection":
                self._submit_next_entry(plan, state, timestamp=timestamp)
                state["status"] = "open"
        else:
            self._decrease_position(state, quantity)
            if sum(float(row["quantity"]) for row in state["positions"]) <= 1e-9:
                state["positions"] = []
                state["status"] = "terminal"
                state["terminal_reason"] = order["event"]
                state["protection"] = None
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
        if state["status"] in {"terminal", "blocked_protection", "stopped"}:
            return self.snapshot(plan)
        target = float(plan["dca"]["target_price"])
        stop = float(plan["dca"]["stop_price"])
        target_hit = price >= target if identity["direction"] == "long" else price <= target
        stop_hit = price <= stop if identity["direction"] == "long" else price >= stop
        if stop_hit and state["positions"]:
            return self.stop(plan, timestamp=timestamp, reason="strategy_stop", price=stop)
        if target_hit and state["positions"]:
            self._cancel_entries(state, timestamp=timestamp, reason="take_profit")
            position = state["positions"][0]
            self._submit_exit(plan, state, timestamp=timestamp, price=target, quantity=float(position["quantity"]), event="target")
            state["status"] = "target_triggered"
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
        self._cancel_entries(state, timestamp=timestamp, reason=reason)
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
            state["status"] = "blocked_protection"
            state["blocker"] = "capability_gap:protection_order.submit"
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
            }
        except Exception as exc:  # noqa: BLE001 - no new entry after coverage failure.
            state["status"] = "blocked_protection"
            state["blocker"] = f"protection_update_failed:{type(exc).__name__}:{exc}"

    def _submit_next_entry(self, plan: dict[str, Any], state: dict[str, Any], *, timestamp: str) -> None:
        index = int(state["next_entry_index"])
        if index >= len(state["entry_levels"]):
            return
        price = float(state["entry_levels"][index])
        command = self._command(plan, state, price=price, quantity=self._entry_quantity(plan, price), event="entry", index=index, timestamp=timestamp)
        receipt = self.broker.submit_order(
            BrokerOrderRequest(
                run_date=state["cycle_id"],
                ticket=command,
                latest_price=price,
                actual_size=command["quantity"],
            )
        )
        state["orders"].append(self._order_row(command, receipt))
        state["next_entry_index"] = index + 1

    def _submit_exit(self, plan: dict[str, Any], state: dict[str, Any], *, timestamp: str, price: float, quantity: float, event: str) -> None:
        command = self._command(plan, state, price=price, quantity=quantity, event=event, index=len(state["orders"]), timestamp=timestamp, reduce_only=True)
        receipt = self.broker.submit_order(
            BrokerOrderRequest(
                run_date=state["cycle_id"],
                ticket=command,
                latest_price=price,
                actual_size=quantity,
            )
        )
        state["orders"].append(self._order_row(command, receipt))

    def _cancel_entries(self, state: dict[str, Any], *, timestamp: str, reason: str) -> None:
        for row in state["orders"]:
            if row["event"] != "entry" or row["state"] != "accepted":
                continue
            try:
                self.broker.cancel_order(
                    BrokerCancelRequest(
                        run_date=state["cycle_id"],
                        asset=row["instrument_id"],
                        client_order_id=row["client_order_id"],
                        broker_order_id=row.get("broker_order_id") or "",
                    )
                )
            except Exception as exc:  # noqa: BLE001 - cancellation uncertainty blocks completion.
                state["status"] = "blocked_reconciliation"
                state["blocker"] = f"entry_cancel_failed:{type(exc).__name__}:{exc}"
                return
            row["state"] = "cancelled"
            row["cancelled_at"] = timestamp
            row["cancel_reason"] = reason

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
    def _command(plan: dict[str, Any], state: dict[str, Any], *, price: float, quantity: float, event: str, index: int, timestamp: str, reduce_only: bool = False) -> dict[str, Any]:
        return {
            "ticket_id": f"{state['strategy_plan_id']}:{event}:{index}",
            "instrument_id": str(plan.get("instrument_id") or plan.get("execution_context", {}).get("instrument_id") or "BTC-USD-PERP"),
            "side": ("buy" if state["direction"] == "long" else "sell") if event == "entry" else ("sell" if state["direction"] == "long" else "buy"),
            "order_type": "limit",
            "limit_price": price,
            "price": price,
            "quantity": quantity,
            "idempotency_key": f"{state['strategy_plan_id']}:{event}:{index}",
            "reduce_only": reduce_only,
            "close_position": reduce_only,
            "event": event,
            "strategy_plan_id": state["strategy_plan_id"],
            "strategy_plan_version": state["strategy_plan_version"],
            "cycle_id": state["cycle_id"],
            "timestamp": timestamp,
        }

    @staticmethod
    def _order_row(command: dict[str, Any], receipt: Any) -> dict[str, Any]:
        return {
            **command,
            "order_id": str(receipt.order_id),
            "client_order_id": str(receipt.client_order_id),
            "broker_order_id": str(receipt.broker_order_id or ""),
            "state": "accepted",
        }

    def _state(self, plan: dict[str, Any]) -> dict[str, Any]:
        identity = self._identity(plan)
        state = self._states.get(identity["plan_id"]) or self._load(identity["plan_id"])
        if state is None:
            raise DcaTestnetLifecycleError("DCA Testnet lifecycle has not started")
        self._states[identity["plan_id"]] = state
        return state

    @staticmethod
    def _find_order(state: dict[str, Any], order_id: str) -> dict[str, Any] | None:
        return next((row for row in state["orders"] if str(row.get("order_id")) == order_id), None)

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
        if direction not in {"long", "short"} or not levels or not plan.get("strategy_plan_id") or not plan.get("cycle_id"):
            raise ValueError("DCA Testnet StrategyPlan identity is incomplete")
        return {
            "plan_id": str(plan["strategy_plan_id"]),
            "cycle_id": str(plan["cycle_id"]),
            "version": int(plan.get("version") or 0),
            "direction": direction,
            "entry_levels": levels,
        }

    def _path(self, plan_id: str) -> Path:
        return self.output_root / "dualtrack" / "dca_testnet_lifecycle" / f"{plan_id}.json"

    def _load(self, plan_id: str) -> dict[str, Any] | None:
        rows = load_json(self._path(plan_id))
        return dict(rows[-1]) if rows and isinstance(rows[-1], dict) else None

    def _save(self, state: dict[str, Any]) -> None:
        write_json(self._path(str(state["strategy_plan_id"])), [state])
