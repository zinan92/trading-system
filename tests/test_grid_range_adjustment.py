from __future__ import annotations

from copy import deepcopy

import pytest

from services.grid_range_adjustment import build_range_extension


def market(price: float = 110.0) -> dict:
    return {
        "status": "ready",
        "fresh": True,
        "is_synthetic": False,
        "provider": "test",
        "latest_close": price,
        "bars": [{}] * 15,
    }


def arithmetic_plan() -> dict:
    levels = [100.0, 105.0, 110.0, 115.0, 120.0]
    orders = []
    for index, price in enumerate((100.0, 105.0, 115.0, 120.0)):
        side = "buy" if price < 110.0 else "sell"
        orders.append(
            {
                "preview_order_id": f"old-{index}",
                "side": side,
                "event": "entry",
                "order_type": "limit",
                "price": price,
                "quantity": round(50.0 / price, 8),
                "notional": 50.0,
                "sl": 95.0 if side == "buy" else 125.0,
                "tp": 110.0,
            }
        )
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
            "orders": orders,
        },
    }


def geometric_plan() -> dict:
    levels = [100.0, 110.0, 121.0, 133.1, 146.41]
    orders = []
    for index, price in enumerate((100.0, 110.0, 133.1, 146.41)):
        side = "buy" if price < 121.0 else "sell"
        orders.append(
            {
                "preview_order_id": f"old-g-{index}",
                "side": side,
                "event": "entry",
                "order_type": "limit",
                "price": price,
                "quantity": round(50.0 / price, 8),
                "notional": 50.0,
                "sl": 90.90909091 if side == "buy" else 161.051,
                "tp": 121.0,
            }
        )
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
            "notional_mode": "manual",
            "leverage": 2.0,
            "orders": orders,
        },
    }


def test_arithmetic_edges_snap_to_whole_steps_without_changing_internal_orders() -> None:
    plan = arithmetic_plan()
    original_levels = deepcopy(plan["grid"]["levels"])
    original_orders = deepcopy(plan["grid"]["orders"])

    result = build_range_extension(
        plan["cycle_id"],
        plan,
        {"low": 92.0, "high": 127.0},
        market=market(),
        accepted_entries=[],
        positions=[],
    )

    assert result["steps"] == {"low": 2, "high": 1}
    assert result["effective_range"] == {"low": 90.0, "high": 125.0}
    assert result["grid"]["levels"][2:7] == original_levels
    assert result["grid"]["orders"][:4] == original_orders
    assert result["grid"]["spacing"] == 5.0
    assert result["grid"]["notional_per_grid"] == 50.0
    assert result["grid"]["notional_mode"] == "auto"
    assert {row["price"] for row in result["edge_orders"]} == {90.0, 95.0, 125.0}


def test_arithmetic_contraction_removes_only_outer_geometry() -> None:
    result = build_range_extension(
        "2026-07-05_DAY",
        arithmetic_plan(),
        {"low": 104.0, "high": 116.0},
        market=market(),
        accepted_entries=[],
        positions=[],
    )

    assert result["steps"] == {"low": -1, "high": -1}
    assert result["grid"]["levels"] == [105.0, 110.0, 115.0]
    assert result["grid"]["count"] == 2
    assert result["edge_orders"] == []
    assert [row["preview_order_id"] for row in result["grid"]["orders"]] == [
        "old-1",
        "old-2",
    ]


def test_geometric_edges_preserve_ratio_for_expansion_and_contraction() -> None:
    plan = geometric_plan()
    old_levels = deepcopy(plan["grid"]["levels"])
    expanded = build_range_extension(
        plan["cycle_id"],
        plan,
        {"low": 82.0, "high": 177.0},
        market=market(121.0),
        accepted_entries=[],
        positions=[],
    )
    contracted = build_range_extension(
        plan["cycle_id"],
        plan,
        {"low": 109.0, "high": 135.0},
        market=market(121.0),
        accepted_entries=[],
        positions=[],
    )

    assert expanded["steps"] == {"low": 2, "high": 2}
    assert expanded["grid"]["levels"][2:7] == old_levels
    assert expanded["grid"]["spacing_ratio"] == 1.1
    assert expanded["effective_range"] == {"low": 82.6446281, "high": 177.1561}
    assert contracted["grid"]["levels"] == [110.0, 121.0, 133.1]


def test_new_edge_dedupes_live_exposure_but_rearms_after_completed_fill() -> None:
    plan = arithmetic_plan()
    cases = (
        ({"accepted_entries": [{"side": "buy", "price": 95.0}]}, "accepted"),
        ({"positions": [{"side": "long", "entry_price": 95.0}]}, "position"),
    )
    for extra, label in cases:
        result = build_range_extension(
            plan["cycle_id"],
            plan,
            {"low": 95.0, "high": 120.0},
            market=market(),
            accepted_entries=extra.get("accepted_entries", []),
            positions=extra.get("positions", []),
        )
        assert result["edge_orders"] == [], label

    completed = build_range_extension(
        plan["cycle_id"],
        plan,
        {"low": 95.0, "high": 120.0},
        market=market(),
        accepted_entries=[],
        positions=[],
    )
    assert [row["price"] for row in completed["edge_orders"]] == [95.0]


def test_adjustment_rejects_range_that_excludes_current_market() -> None:
    with pytest.raises(ValueError, match="market_outside_requested_range"):
        build_range_extension(
            "2026-07-05_DAY",
            arithmetic_plan(),
            {"low": 100.0, "high": 116.0},
            market=market(119.0),
            accepted_entries=[],
            positions=[],
        )
