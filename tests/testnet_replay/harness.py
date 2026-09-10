from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from services.grid_testnet_lifecycle import GridTestnetLifecycle


def grid_plan(*, plan_id: str = "replay-grid", direction: str = "long") -> dict[str, Any]:
    lower, upper = 63_000.0, 66_000.0
    return {
        "schema_version": "strategy-plan-v1", "strategy_type": "grid",
        "strategy_plan_id": plan_id, "plan_digest": "sha256:" + "a" * 64,
        "version": 1, "cycle_id": "replay-cycle", "strategy_session_id": "replay-session",
        "strategy_revision_id": "replay-revision", "direction": direction,
        "instrument_id": "BTC-USD-PERP", "upper_price_boundary": upper,
        "lower_price_boundary": lower, "locked_at": "2026-09-11T00:00:00+00:00",
        "grid": {"rungs": [
            {"rung": 1, "price": 65_000.0, "side": "buy", "take_profit": 65_500.0, "hard_stop": lower, "quantity": 0.1},
            {"rung": 2, "price": 64_000.0, "side": "buy", "take_profit": 64_500.0, "hard_stop": lower, "quantity": 0.1},
        ], "partial_entry_timeout_seconds": 300},
        "risk_budget": {"equity": 10_000.0, "maximum_loss_at_full_depth": 1_000.0,
                         "leverage_limit": 10.0, "max_notional": 20_000.0,
                         "max_open_orders": 8, "max_open_positions": 2,
                         "max_slippage": 50.0, "max_submit_retries": 3},
    }


class ReplayExchange:
    """A deterministic binding-side observation log; it never reaches a venue."""

    def __init__(self) -> None:
        self.positions: list[dict[str, Any]] = []
        self.public_facts: dict[str, Any] = {"status": "pass", "cursor": "replay-0", "fills": [], "positions": [], "open_orders": []}
        self.submissions: list[dict[str, Any]] = []
        self.cancellations: list[Any] = []

    def observe(self, broker: Any) -> None:
        broker.read_public_facts = lambda **_kwargs: json.loads(json.dumps(self.public_facts))
        original_submit = broker.submit_order
        original_cancel = broker.cancel_order

        def submit(request: Any) -> Any:
            self.submissions.append(dict(request.ticket))
            return original_submit(request)

        def cancel(request: Any) -> Any:
            self.cancellations.append(request)
            return original_cancel(request)

        broker.submit_order = submit
        broker.cancel_order = cancel


def assert_invariants(state: Mapping[str, Any], exchange: ReplayExchange, *, previous: Mapping[str, Any] | None = None) -> None:
    """Assert I1-I3 at every replay step; I4 is a repository-level audit."""
    positions_open = any(abs(float(row.get("szi") or row.get("quantity") or 0)) > 1e-9 for row in exchange.positions)
    protection = state.get("hard_stop_protection") or state.get("protection") or {}
    if positions_open:
        assert protection.get("status") == "active" or (
            state.get("status", "").startswith("blocked")
            and state.get("blocker") == "position_open_unprotected"
            and state.get("park_notification_required") is True
        ), state
    if previous is not None and state.get("updated_at") == previous.get("updated_at"):
        events = state.get("events") or []
        assert state.get("warning") or events, state
    if state.get("status", "").startswith("blocked") and state.get("blocker", "").startswith(("unknown_fill", "grid_entry_fill", "fill_rejected")):
        assert not exchange.cancellations


def new_lifecycle(output_root: Path, broker: Any) -> GridTestnetLifecycle:
    return GridTestnetLifecycle(output_root, broker)


def fill(order: Mapping[str, Any], *, tid: int, price: float | None = None) -> dict[str, Any]:
    return {"coin": "BTC", "px": str(price if price is not None else order.get("price")),
            "sz": str(order["quantity"]), "side": "B" if order["side"] == "buy" else "A",
            "time": 1789123200000 + tid, "oid": int(order["broker_order_id"]),
            "cloid": order["client_order_id"], "tid": tid}
