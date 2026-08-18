from __future__ import annotations

from pathlib import Path

import pytest

from services.park_strategy_plan import ParkStrategyPlanError, build_deterministic_risk_plan, normalize_park_input


MARKET = {
    "price": 4300.0,
    "trusted": True,
    "fresh": True,
    "source": "trusted-paper-feed",
    "observed_at": "2026-08-14T10:00:00+08:00",
}


def test_normalizes_parks_chinese_short_dca_input() -> None:
    normalized = normalize_park_input("做空 DCA，最多 10 倍杠杆，价格区间是 4444~4200")
    assert normalized["direction"] == "short"
    assert normalized["strategy_type"] == "dca"
    assert normalized["upper_price_boundary"] == 4444.0
    assert normalized["lower_price_boundary"] == 4200.0
    assert normalized["maximum_leverage"] == 10.0
    assert normalized["maximum_acceptable_loss"] is None


def test_normalizes_english_grid_input_with_deterministic_boundary_hard_stop() -> None:
    normalized = normalize_park_input({
        "direction": "long", "strategy_type": "grid",
        "upper_price_boundary": 4444, "lower_price_boundary": 4200,
        "maximum_acceptable_loss": 100,
    })
    assert normalized["strategy_type"] == "grid"
    assert normalized["stop_price"] == 4200.0
    assert normalized["take_profit_price"] is None


def test_normalizes_neutral_grid_as_a_first_class_bilateral_direction() -> None:
    normalized = normalize_park_input("中性网格策略 4450 4100 最大20x杠杆")
    assert normalized["direction"] == "neutral"
    assert normalized["strategy_type"] == "grid"
    assert normalized["order_count"] == 30
    assert normalized["stop_price"] is None
    assert normalized["take_profit_price"] is None


def test_neutral_grid_risk_plan_is_bilateral_and_conservative() -> None:
    normalized = normalize_park_input("中性网格策略 4450 4100 最大20x杠杆")
    plan = build_deterministic_risk_plan(normalized, market=MARKET, account_equity=1000)
    risk = plan["risk"]
    assert risk["risk_boundary"] == {"lower": 4100.0, "upper": 4450.0}
    assert set(risk["legs"]) == {"long", "short"}
    assert risk["legs"]["long"]["boundary"] == 4100.0
    assert risk["legs"]["short"]["boundary"] == 4450.0
    assert risk["effective_leverage"] <= 20.0
    assert risk["theoretical_max_loss"] > 0


def test_grid_explicit_spacing_builds_inset_rungs_and_full_depth_loss() -> None:
    normalized = normalize_park_input({
        "direction": "neutral",
        "strategy_type": "grid",
        "upper_price_boundary": 4000,
        "lower_price_boundary": 3800,
        "grid_spacing": 10,
        "maximum_leverage": 10,
        "maximum_acceptable_loss": 100,
    })
    assert normalized["order_count"] == 19
    plan = build_deterministic_risk_plan(normalized, market={**MARKET, "price": 3900.0}, account_equity=1000)
    risk = plan["risk"]
    assert risk["grid_entry_range"] == {"lower": 3810.0, "upper": 3990.0}
    assert risk["grid_spacing"] == 10.0
    assert risk["grid_rung_prices"] == [3810.0 + 10.0 * index for index in range(19)]
    assert len(risk["grid_rungs"]) == 19
    assert all(3800 < rung["price"] < 4000 for rung in risk["grid_rungs"])
    assert {rung["side"] for rung in risk["grid_rungs"]} == {"buy", "sell"}
    assert risk["grid_loss_model"] == "full_depth_all_rungs_to_hard_stop"
    assert risk["theoretical_max_loss"] <= 100.0 + 1e-8
    assert risk["hard_stop"] == {"long": 3800.0, "short": 4000.0}


def test_grid_explicit_hard_stop_overrides_boundary_default() -> None:
    normalized = normalize_park_input({
        "direction": "long",
        "strategy_type": "grid",
        "upper_price_boundary": 4000,
        "lower_price_boundary": 3800,
        "grid_spacing": 10,
        "maximum_leverage": 10,
        "stop_price": 3850,
    })
    plan = build_deterministic_risk_plan(normalized, market={**MARKET, "price": 3900.0}, account_equity=1000)
    assert plan["risk"]["hard_stop"] == 3850.0
    assert all(rung["hard_stop"] == 3850.0 for rung in plan["risk"]["grid_rungs"])
    assert plan["risk"]["hard_stop_source"] == "explicit_stop_price"


def test_grid_default_hard_stop_provenance_is_boundary_not_explicit() -> None:
    normalized = normalize_park_input({
        "direction": "long",
        "strategy_type": "grid",
        "upper_price_boundary": 4000,
        "lower_price_boundary": 3800,
        "grid_spacing": 10,
        "maximum_leverage": 10,
    })
    plan = build_deterministic_risk_plan(normalized, market={**MARKET, "price": 3900.0}, account_equity=1000)
    assert plan["risk"]["hard_stop_source"] == "authorized_price_boundary"


def test_neutral_grid_accepts_explicit_per_leg_hard_stop_override() -> None:
    normalized = normalize_park_input({
        "direction": "neutral",
        "strategy_type": "grid",
        "upper_price_boundary": 4000,
        "lower_price_boundary": 3800,
        "grid_spacing": 10,
        "maximum_leverage": 10,
        "stop_price": {"long": 3820, "short": 3980},
    })
    plan = build_deterministic_risk_plan(normalized, market={**MARKET, "price": 3900.0}, account_equity=1000)
    assert plan["risk"]["hard_stop"] == {"long": 3820.0, "short": 3980.0}
    assert plan["risk"]["hard_stop_source"] == "explicit_stop_price"


def test_short_grid_full_depth_notional_stays_within_leverage_cap() -> None:
    normalized = normalize_park_input({
        "direction": "short",
        "strategy_type": "grid",
        "upper_price_boundary": 4000,
        "lower_price_boundary": 3800,
        "grid_spacing": 10,
        "maximum_leverage": 10,
    })
    plan = build_deterministic_risk_plan(normalized, market={**MARKET, "price": 3850.0}, account_equity=1000)
    risk = plan["risk"]
    actual_notional = sum(rung["price"] * risk["per_order_quantity"] for rung in risk["grid_rungs"])
    assert actual_notional <= risk["maximum_notional"] + 1e-8


def test_neutral_direction_cannot_be_reinterpreted_as_dca() -> None:
    with pytest.raises(ParkStrategyPlanError) as error:
        normalize_park_input({
            "direction": "neutral",
            "strategy_type": "dca",
            "upper_price_boundary": 4450,
            "lower_price_boundary": 4100,
            "maximum_leverage": 20,
        })
    assert error.value.code == "neutral_direction_requires_grid"


def test_neutral_grid_uses_the_stricter_loss_cap_and_rejects_ambiguous_global_tp_sl() -> None:
    normalized = normalize_park_input({
        "direction": "neutral",
        "strategy_type": "grid",
        "upper_price_boundary": 4450,
        "lower_price_boundary": 4100,
        "maximum_leverage": 20,
        "maximum_acceptable_loss": 10,
        "stop_price": None,
        "take_profit_price": None,
    })
    plan = build_deterministic_risk_plan(normalized, market=MARKET, account_equity=1000)
    assert plan["risk"]["selected_constraint"] == "maximum_acceptable_loss"
    assert plan["risk"]["theoretical_max_loss"] <= 10
    with pytest.raises(ParkStrategyPlanError) as error:
        build_deterministic_risk_plan(
            {**normalized, "stop_price": 4000},
            market=MARKET,
            account_equity=1000,
        )
    assert error.value.code == "neutral_grid_hard_stop_incomplete"


@pytest.mark.parametrize("payload, code", [
    ({"strategy_type": "dca", "upper_price_boundary": 2, "lower_price_boundary": 1, "maximum_leverage": 2}, "missing_direction"),
    ({"direction": "short", "upper_price_boundary": 2, "lower_price_boundary": 1, "maximum_leverage": 2}, "missing_strategy_type"),
    ({"direction": "short", "strategy_type": "dca", "upper_price_boundary": 2, "maximum_leverage": 2}, "missing_price_boundary"),
    ({"direction": "short", "strategy_type": "dca", "upper_price_boundary": 2, "lower_price_boundary": 1}, "missing_risk_authority"),
])
def test_missing_authority_is_fail_closed(payload: dict, code: str) -> None:
    with pytest.raises(ParkStrategyPlanError) as error:
        normalize_park_input(payload)
    assert error.value.code == code


def test_risk_plan_requires_trusted_fresh_market_and_real_equity() -> None:
    normalized = normalize_park_input("做空 DCA，最多 10 倍杠杆，价格区间是 4444~4200")
    with pytest.raises(ParkStrategyPlanError, match="trusted"):
        build_deterministic_risk_plan(normalized, market={**MARKET, "trusted": False}, account_equity=1000)
    with pytest.raises(ParkStrategyPlanError, match="equity"):
        build_deterministic_risk_plan(normalized, market=MARKET, account_equity=None)


def test_stricter_loss_cap_wins_and_is_recorded() -> None:
    normalized = normalize_park_input({
        "direction": "short", "strategy_type": "dca",
        "upper_price_boundary": 4444, "lower_price_boundary": 4200,
        "maximum_leverage": 10, "maximum_acceptable_loss": 10,
        "order_count": 2,
    })
    plan = build_deterministic_risk_plan(normalized, market=MARKET, account_equity=1000)
    assert plan["risk"]["selected_constraint"] == "maximum_acceptable_loss"
    assert plan["risk"]["maximum_loss_cap_notional"] < plan["risk"]["leverage_cap_notional"]
    assert plan["risk"]["theoretical_max_loss"] <= 10.0
    assert plan["risk"]["order_count"] == 2
    assert plan["risk"]["per_order_quantity"] > 0


def test_plan_digest_is_deterministic_and_market_boundary_is_hard() -> None:
    normalized = normalize_park_input("做空 DCA，最多 10 倍杠杆，价格区间是 4444~4200")
    first = build_deterministic_risk_plan(normalized, market=MARKET, account_equity=1000)
    second = build_deterministic_risk_plan(normalized, market=dict(MARKET), account_equity=1000)
    assert first == second
    with pytest.raises(ParkStrategyPlanError, match="outside"):
        build_deterministic_risk_plan(normalized, market={**MARKET, "price": 4500}, account_equity=1000)
