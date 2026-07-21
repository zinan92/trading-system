from __future__ import annotations

from copy import deepcopy

import pytest

from services.grid_range_adjustment import build_range_extension


CONFIG = {
    "max_leverage": 10.0,
    "strategy_grid": {
        "capital_utilization_cap": 1.0,
        "max_plan_loss_pct": 1.0,
    },
}


def _market(price: float = 110.0) -> dict:
    return {
        "status": "ready",
        "fresh": True,
        "is_synthetic": False,
        "provider": "test",
        "latest_close": price,
        "bars": [{}] * 20,
    }


def _arithmetic_plan() -> dict:
    levels = [100.0, 105.0, 110.0, 115.0, 120.0]
    return {
        "cycle_id": "2026-07-05_DAY",
        "strategy_plan_id": "plan-arithmetic",
        "direction": "neutral",
        "style": "steady",
        "range": {"low": 100.0, "high": 120.0},
        "grid": {
            "mode": "arithmetic",
            "count": 4,
            "levels": levels,
            "spacing": 5.0,
            "spacing_ratio": None,
            "notional_per_grid": 50.0,
            "notional_mode": "auto",
            "leverage": 2.0,
            "orders": [
                {
                    "preview_order_id": f"old-{index}",
                    "side": "buy" if price < 110.0 else "sell",
                    "event": "entry",
                    "order_type": "limit",
                    "price": price,
                    "quantity": round(50.0 / price, 8),
                    "notional": 50.0,
                    "sl": 95.0 if price < 110.0 else 125.0,
                    "tp": 110.0,
                }
                for index, price in enumerate((100.0, 105.0, 115.0, 120.0))
            ],
        },
    }


def _geometric_plan() -> dict:
    levels = [100.0, 110.0, 121.0, 133.1, 146.41]
    return {
        "cycle_id": "2026-07-05_DAY",
        "strategy_plan_id": "plan-geometric",
        "direction": "neutral",
        "style": "steady",
        "range": {"low": 100.0, "high": 146.41},
        "grid": {
            "mode": "geometric",
            "count": 4,
            "levels": levels,
            "spacing": 11.6025,
            "spacing_ratio": 1.1,
            "notional_per_grid": 50.0,
            "notional_mode": "auto",
            "leverage": 2.0,
            "orders": [
                {
                    "preview_order_id": f"old-g-{index}",
                    "side": "buy" if price < 121.0 else "sell",
                    "event": "entry",
                    "order_type": "limit",
                    "price": price,
                    "quantity": round(50.0 / price, 8),
                    "notional": 50.0,
                    "sl": 90.90909091 if price < 121.0 else 161.051,
                    "tp": 121.0,
                }
                for index, price in enumerate((100.0, 110.0, 133.1, 146.41))
            ],
        },
    }


def test_arithmetic_range_snaps_both_edges_and_preserves_internal_geometry() -> None:
    plan = _arithmetic_plan()
    original_levels = deepcopy(plan["grid"]["levels"])
    original_orders = deepcopy(plan["grid"]["orders"])

    expanded = build_range_extension(
        plan["cycle_id"],
        plan,
        {"low": 92.0, "high": 127.0},
        market=_market(),
        account={"equity": 10_000.0},
        config=CONFIG,
        accepted_entries=[{
            "order_id": "pending-105",
            "state": "accepted",
            "event": "entry",
            "side": "buy",
            "price": 105.0,
            "quantity": round(50.0 / 105.0, 8),
        }],
        positions=[{
            "position_id": "filled-100",
            "status": "open",
            "side": "long",
            "entry_price": 100.0,
            "remaining_units": 0.5,
            "sl": 95.0,
        }],
    )

    assert expanded["steps"] == {"low": 2, "high": 1}
    assert expanded["effective_range"] == {"low": 90.0, "high": 125.0}
    assert expanded["grid"]["levels"][2:7] == original_levels
    assert expanded["grid"]["orders"][:4] == original_orders
    assert expanded["grid"]["notional_per_grid"] == 50.0
    assert expanded["grid"]["notional_mode"] == "manual"
    assert expanded["risk"]["pending_entry_count"] == 4
    assert expanded["risk"]["open_position_count"] == 1


def test_arithmetic_range_contracts_each_edge_on_nearest_steps() -> None:
    plan = _arithmetic_plan()

    contracted = build_range_extension(
        plan["cycle_id"],
        plan,
        {"low": 104.0, "high": 116.0},
        market=_market(),
        account={"equity": 10_000.0},
        config=CONFIG,
        accepted_entries=[],
        positions=[],
    )

    assert contracted["steps"] == {"low": -1, "high": -1}
    assert contracted["grid"]["levels"] == [105.0, 110.0, 115.0]
    assert contracted["grid"]["count"] == 2
    assert contracted["edge_orders"] == []
    assert [row["preview_order_id"] for row in contracted["grid"]["orders"]] == ["old-1", "old-2"]


def test_geometric_range_uses_ratio_for_outward_and_inward_snap() -> None:
    plan = _geometric_plan()
    old_levels = deepcopy(plan["grid"]["levels"])

    expanded = build_range_extension(
        plan["cycle_id"],
        plan,
        {"low": 82.0, "high": 177.0},
        market=_market(121.0),
        account={"equity": 10_000.0},
        config=CONFIG,
        accepted_entries=[],
        positions=[],
    )
    contracted = build_range_extension(
        plan["cycle_id"],
        plan,
        {"low": 109.0, "high": 135.0},
        market=_market(121.0),
        account={"equity": 10_000.0},
        config=CONFIG,
        accepted_entries=[],
        positions=[],
    )

    assert expanded["steps"] == {"low": 2, "high": 2}
    assert expanded["grid"]["levels"][2:7] == old_levels
    assert expanded["effective_range"] == {"low": 82.6446281, "high": 177.1561}
    assert contracted["steps"] == {"low": -1, "high": -1}
    assert contracted["grid"]["levels"] == [110.0, 121.0, 133.1]


def test_range_adjustment_does_not_rehang_a_filled_edge_entry() -> None:
    plan = _arithmetic_plan()

    result = build_range_extension(
        plan["cycle_id"],
        plan,
        {"low": 95.0, "high": 120.0},
        market=_market(),
        account={"equity": 10_000.0},
        config=CONFIG,
        accepted_entries=[],
        positions=[],
        filled_entries=[{"event": "entry", "side": "buy", "price": 95.0}],
    )

    assert result["steps"] == {"low": 1, "high": 0}
    assert result["edge_orders"] == []
    assert not [order for order in result["grid"]["orders"] if order["price"] == 95.0]


def test_range_adjustment_skips_marketable_and_already_pending_edge_entries() -> None:
    plan = _arithmetic_plan()
    at_market = build_range_extension(
        plan["cycle_id"],
        plan,
        {"low": 95.0, "high": 120.0},
        market=_market(95.0),
        account={"equity": 10_000.0},
        config=CONFIG,
        accepted_entries=[],
        positions=[],
    )
    existing = {
        "order_id": "already-pending-95",
        "state": "accepted",
        "event": "entry",
        "side": "buy",
        "price": 95.0,
        "quantity": round(50.0 / 95.0, 8),
        "notional": 50.0,
        "sl": 90.0,
    }
    already_pending = build_range_extension(
        plan["cycle_id"],
        plan,
        {"low": 95.0, "high": 120.0},
        market=_market(),
        account={"equity": 10_000.0},
        config=CONFIG,
        accepted_entries=[existing],
        positions=[],
    )

    assert at_market["edge_orders"] == []
    assert already_pending["edge_orders"] == []
    assert already_pending["risk"]["pending_entry_count"] == 1


def test_range_adjustment_rejects_fixed_notional_when_current_account_cannot_fund_it() -> None:
    plan = _arithmetic_plan()

    with pytest.raises(ValueError, match="risk_budget_exceeded"):
        build_range_extension(
            plan["cycle_id"],
            plan,
            {"low": 95.0, "high": 125.0},
            market=_market(),
            account={"equity": 10.0},
            config=CONFIG,
            accepted_entries=[{
                "order_id": "pending-105",
                "state": "accepted",
                "event": "entry",
                "side": "buy",
                "price": 105.0,
                "quantity": round(50.0 / 105.0, 8),
                "sl": 95.0,
            }],
            positions=[],
        )

    with pytest.raises(ValueError, match="risk_evidence_missing"):
        build_range_extension(
            plan["cycle_id"],
            plan,
            {"low": 95.0, "high": 125.0},
            market=_market(),
            account={},
            config=CONFIG,
            accepted_entries=[],
            positions=[],
        )

    with pytest.raises(ValueError, match="risk_evidence_missing"):
        build_range_extension(
            plan["cycle_id"],
            plan,
            {"low": 95.0, "high": 125.0},
            market=_market(),
            account={"equity": 0.0, "starting_cash": 1_000_000.0},
            config=CONFIG,
            accepted_entries=[],
            positions=[],
        )


@pytest.mark.parametrize(
    ("accepted_entry", "position"),
    [
        (
            {
                "order_id": "pending-missing-price",
                "state": "accepted",
                "event": "entry",
                "side": "buy",
                "price": 0.0,
                "quantity": 0.5,
                "sl": 95.0,
            },
            None,
        ),
        (
            {
                "order_id": "pending-missing-quantity",
                "state": "accepted",
                "event": "entry",
                "side": "buy",
                "price": 105.0,
                "quantity": 0.0,
                "sl": 95.0,
            },
            None,
        ),
        (
            None,
            {
                "position_id": "position-missing-stop",
                "status": "open",
                "side": "long",
                "entry_price": 105.0,
                "remaining_units": 0.5,
                "strategy_plan_id": "unknown-plan",
            },
        ),
    ],
)
def test_range_adjustment_fails_closed_when_execution_risk_evidence_is_incomplete(
    accepted_entry: dict | None,
    position: dict | None,
) -> None:
    plan = _arithmetic_plan()

    with pytest.raises(ValueError, match="risk_evidence_missing"):
        build_range_extension(
            plan["cycle_id"],
            plan,
            dict(plan["range"]),
            market=_market(),
            account={"equity": 10_000.0},
            config=CONFIG,
            accepted_entries=[accepted_entry] if accepted_entry else [],
            positions=[position] if position else [],
        )


def test_range_adjustment_uses_matching_ancestor_plan_for_duplicate_price_risk() -> None:
    plan = _arithmetic_plan()
    quantity = round(50.0 / 105.0, 8)
    ancestor_order = {
        **next(order for order in plan["grid"]["orders"] if order["price"] == 105.0),
        "sl": 104.0,
        "_strategy_plan_id": "plan-ancestor",
    }
    accepted = {
        "order_id": "pending-ancestor",
        "strategy_plan_id": "plan-ancestor",
        "state": "accepted",
        "event": "entry",
        "side": "buy",
        "price": 105.0,
        "quantity": quantity,
    }

    result = build_range_extension(
        plan["cycle_id"],
        plan,
        dict(plan["range"]),
        market=_market(),
        account={"equity": 10_000.0},
        config=CONFIG,
        accepted_entries=[accepted],
        positions=[],
        historical_plan_orders=[ancestor_order],
    )

    assert result["risk"]["max_loss"] == round(quantity, 2)

    accepted_without_plan = {key: value for key, value in accepted.items() if key != "strategy_plan_id"}
    with pytest.raises(ValueError, match="risk_evidence_missing"):
        build_range_extension(
            plan["cycle_id"],
            plan,
            dict(plan["range"]),
            market=_market(),
            account={"equity": 10_000.0},
            config=CONFIG,
            accepted_entries=[accepted_without_plan],
            positions=[],
            historical_plan_orders=[ancestor_order],
        )
