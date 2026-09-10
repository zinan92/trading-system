from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from services.grid_testnet_lifecycle import GridTestnetLifecycle


def grid_plan(*, plan_id: str = "replay-grid", direction: str = "long") -> dict[str, Any]:
    lower, upper = 72_000.0, 84_000.0
    return {
        "schema_version": "strategy-plan-v1", "strategy_type": "grid",
        "strategy_plan_id": plan_id, "plan_digest": "sha256:" + "a" * 64,
        "version": 1, "cycle_id": "replay-cycle", "strategy_session_id": "replay-session",
        "strategy_revision_id": "replay-revision", "direction": direction,
        "instrument_id": "BTC-USD-PERP", "upper_price_boundary": upper,
        "lower_price_boundary": lower, "locked_at": "2026-09-11T00:00:00+00:00",
        "grid": {"rungs": [
            {"rung": 1, "price": 80_000.0, "side": "buy", "take_profit": 80_500.0, "hard_stop": lower, "quantity": 0.00024},
            {"rung": 2, "price": 78_000.0, "side": "buy", "take_profit": 78_500.0, "hard_stop": lower, "quantity": 0.00024},
        ], "partial_entry_timeout_seconds": 300},
        "risk_budget": {"equity": 10_000.0, "maximum_loss_at_full_depth": 1_000.0,
                         "leverage_limit": 10.0, "max_notional": 20_000.0,
                         "max_open_orders": 8, "max_open_positions": 2,
                         "max_slippage": 50.0, "max_submit_retries": 3},
    }


class ReplayExchange:
    """A persistent Standard Broker binding seam; it never reaches a venue."""

    def __init__(self) -> None:
        self.positions: list[dict[str, Any]] = []
        self.open_orders: list[dict[str, Any]] = []
        self.protection_groups: dict[str, dict[str, Any]] = {}
        self.fills: list[dict[str, Any]] = []
        self.public_facts: dict[str, Any] = {"status": "pass", "cursor": "replay-0"}
        self.submissions: list[dict[str, Any]] = []
        self.cancellations: list[Any] = []
        self.reads: list[str] = []

    def observe(self, broker: Any) -> None:
        def facts(**_kwargs: Any) -> dict[str, Any]:
            self.reads.append("read_public_facts")
            return json.loads(json.dumps({
                **self.public_facts,
                "positions": self.positions,
                "fills": self.fills,
                "open_orders": self.open_orders,
                "protection_groups": list(self.protection_groups.values()),
            }))

        broker.read_public_facts = facts
        # Keep the typed Standard Broker facts seam intact for proof startup;
        # lifecycle reconciliation reads the credential-free public projection.
        broker.market_fact = lambda *, instrument_id, now: {
            **self.market(), "instrument_id": instrument_id, "price": self.market()["mid"],
            "observed_at": now.isoformat(), "freshness": "fresh",
            "transport_state": "local_fixture",
        }
        original_submit = broker.submit_order
        original_cancel = broker.cancel_order
        original_request = broker.request

        def submit(request: Any) -> Any:
            result = original_submit(request)
            ticket = dict(request.ticket)
            oid = ticket.get("broker_order_id") or getattr(result, "broker_order_id", None)
            # The local adapter keeps the venue oid in its receipt/state, not
            # necessarily on the immutable request ticket.  The fixture backend
            # allocates monotonically from 101, so retain that binding identity.
            if oid is None:
                oid = 101 + sum(1 for row in self.submissions if row.get("event") in {"entry", "entry_rearm", "tp", "hard_stop", "hard_stop_recovery"})
            row = {**ticket, "broker_order_id": oid}
            self.submissions.append(row)
            if str(ticket.get("time_in_force") or "gtc").lower() != "ioc":
                self.open_orders.append(row)
            return result

        def cancel(request: Any) -> Any:
            self.cancellations.append(request)
            result = original_cancel(request)
            ticket = getattr(request, "ticket", {}) or {}
            if not isinstance(ticket, Mapping):
                ticket = vars(ticket) if hasattr(ticket, "__dict__") else {}
            if not ticket:
                ticket = vars(request) if hasattr(request, "__dict__") else {}
            oid = str(ticket.get("broker_order_id") or ticket.get("order_id") or getattr(request, "broker_order_id", "") or getattr(request, "order_id", "") or "")
            order_id = str(ticket.get("order_id") or getattr(request, "order_id", "") or "")
            cloid = str(ticket.get("client_order_id") or ticket.get("cloid") or getattr(request, "client_order_id", "") or "")
            self.open_orders[:] = [row for row in self.open_orders
                                   if str(row.get("broker_order_id")) != oid
                                   and str(row.get("order_id") or "") != order_id
                                   and str(row.get("client_order_id") or row.get("cloid") or "") != cloid]
            return result

        def request(port: str, operation: str, payload: Any) -> Any:
            result = original_request(port, operation, payload)
            if port == "protection_order":
                protection_id = str(getattr(payload, "protection_id", None) or getattr(payload, "protectionId", None) or "")
                if not protection_id and isinstance(payload, Mapping):
                    protection_id = str(payload.get("protection_id") or payload.get("protectionId") or "")
                if operation in {"submit", "replace"}:
                    def leg(row: Any, kind: str) -> dict[str, Any]:
                        value = getattr(row, kind, None)
                        return {
                            "type": str(getattr(getattr(value, "protection_type", None), "value", getattr(value, "protection_type", ""))).lower(),
                            "execution": str(getattr(getattr(value, "execution", None), "value", getattr(value, "execution", ""))).lower(),
                            "trigger_price": str(getattr(value, "trigger_price", "")),
                        }
                    self.protection_groups[protection_id] = {
                        "protection_id": protection_id,
                        "state": "active",
                        "take_profit": leg(payload, "take_profit"),
                        "stop_loss": leg(payload, "stop_loss"),
                    }
                elif operation == "cancel":
                    self.protection_groups.setdefault(protection_id, {"protection_id": protection_id})["state"] = "canceled"
            return result

        broker.submit_order = submit
        broker.cancel_order = cancel
        broker.request = request
        # A fresh broker process must recover durable orders before applying a
        # fill.  The local testnet adapter has no venue recovery primitive, so
        # replay the binding's idempotent registration seam.
        try:
            from services.broker_port import BrokerOrderRequest
            for row in self.submissions:
                if row.get("broker_order_id"):
                    original_submit(BrokerOrderRequest(run_date="replay", ticket=dict(row)))
        except (AttributeError, TypeError, ValueError):
            pass

    def inject_fill(self, order: Mapping[str, Any], *, tid: int, price: float | None = None) -> dict[str, Any]:
        event = fill(order, tid=tid, price=price)
        quantity = float(event["sz"])
        signed = quantity if event["side"] == "B" else -quantity
        current = float(self.positions[0]["szi"]) if self.positions else 0.0
        next_quantity = current + signed
        self.positions[:] = ([{"coin": "BTC", "szi": str(next_quantity), "entryPx": event["px"]}]
                             if abs(next_quantity) > 1e-9 else [])
        self.fills.append(event)
        self.open_orders[:] = [row for row in self.open_orders
                               if str(row.get("broker_order_id")) != str(order.get("broker_order_id"))]
        return event

    def market(self, *, observed_at: str = "2026-09-11T01:00:00+00:00", bid: str = "76937", ask: str = "76978") -> dict[str, Any]:
        return {"execution_ready": True, "fresh": True, "is_synthetic": False,
                "fallback_policy": "none", "observed_at": observed_at, "bid": bid,
                "ask": ask, "mid": str((float(bid) + float(ask)) / 2), "mark": "76957.5",
                "oracle": "76957.5", "impact": ask, "depth_notional": "100000",
                "max_slippage": "50", "source": "hyperliquid.external_testnet",
                "cursor": self.public_facts.get("cursor", "replay-0"),
                "broker_id": "hyperliquid", "environment": "testnet",
                "instrument_id": "BTC-USD-PERP", "asset_index": 0,
                "mapping_revision": "fixture-v1", "universe_revision": "fixture-v1",
                "connection_epoch": "replay-epoch"}


def assert_invariants(state: Mapping[str, Any], exchange: ReplayExchange, *, previous: Mapping[str, Any] | None = None) -> None:
    """Assert I1-I3 at every replay step; I4 is a repository-level audit."""
    positions_open = any(abs(float(row.get("szi") or row.get("quantity") or 0)) > 1e-9 for row in exchange.positions)
    if positions_open:
        active_groups = [
            group for group in exchange.protection_groups.values()
            if group.get("state") == "active"
            and group.get("take_profit")
            and group.get("stop_loss")
        ]
        assert active_groups or (
            state.get("status", "").startswith("blocked")
            and state.get("blocker") == "position_open_unprotected"
            and state.get("park_notification_required") is True
        ), state
    if previous is not None and previous is not state:
        assert state.get("updated_at") > previous.get("updated_at"), state
    if state.get("event") == "grid_lifecycle_observed":
        assert state.get("advance_result") is not None, state
    if state.get("status", "").startswith("blocked") and state.get("blocker", "").startswith(("unknown_fill", "grid_entry_fill", "fill_rejected")):
        assert not exchange.cancellations


def new_lifecycle(output_root: Path, broker: Any) -> GridTestnetLifecycle:
    return GridTestnetLifecycle(output_root, broker)


def fill(order: Mapping[str, Any], *, tid: int, price: float | None = None) -> dict[str, Any]:
    return {"coin": "BTC", "px": str(price if price is not None else order.get("price")),
            "sz": str(order["quantity"]), "side": "B" if order["side"] == "buy" else "A",
            "time": 1789123200000 + tid, "oid": int(order["broker_order_id"]),
            "cloid": order["client_order_id"], "tid": tid}
