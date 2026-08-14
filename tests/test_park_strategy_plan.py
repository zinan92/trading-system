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


def test_normalizes_english_grid_input_and_never_invents_stop() -> None:
    normalized = normalize_park_input({
        "direction": "long", "strategy_type": "grid",
        "upper_price_boundary": 4444, "lower_price_boundary": 4200,
        "maximum_acceptable_loss": 100,
    })
    assert normalized["strategy_type"] == "grid"
    assert normalized["stop_price"] is None
    assert normalized["take_profit_price"] is None


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
