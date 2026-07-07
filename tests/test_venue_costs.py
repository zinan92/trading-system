import pytest

from services.venue_costs import resolve_venue_cost_model, venue_side_cost


def test_venue_costs_keep_legacy_notional_pct_model() -> None:
    rules = {
        "spread_pct": 0.01,
        "slippage_pct": 0.005,
        "commission_pct_notional": 0.001,
        "commission_per_order": 0.25,
        "min_commission": 0.0,
    }

    cost = venue_side_cost(rules, venue="unknown", price=4000, quantity=2)

    assert cost.model_type == "notional_pct"
    assert cost.notional == 8000
    assert cost.spread_cost == pytest.approx(0.4)
    assert cost.slippage_cost == pytest.approx(0.4)
    assert cost.commission == pytest.approx(0.33)
    assert cost.total_cost == pytest.approx(1.13)


def test_venue_costs_support_fixed_per_contract_tiger_mgc() -> None:
    rules = {
        "spread_pct": 0.0,
        "slippage_pct": 0.0,
        "commission_pct_notional": 0.001,
        "venues": {
            "tiger_mgc": {
                "type": "fixed_per_contract",
                "contract_multiplier": 10,
                "commission_per_contract_side": 2.70,
                "exchange_fee_per_contract_side": 0.0,
            }
        },
    }

    cost = venue_side_cost(rules, venue="tiger_mgc", price=4186.9, quantity=2)

    assert cost.model_type == "fixed_per_contract"
    assert cost.notional == pytest.approx(83738.0)
    assert cost.commission == pytest.approx(5.40)
    assert cost.total_cost == pytest.approx(5.40)
    assert cost.effective_bp == pytest.approx(5.40 / 83738.0 * 10_000)


def test_venue_costs_resolve_aliases() -> None:
    rules = {
        "venues": {
            "tiger_mgc": {"type": "fixed_per_contract", "contract_multiplier": 10, "commission_per_contract_side": 2.70}
        },
        "venue_aliases": {"tiger_openapi:COMEX:MGC": "tiger_mgc"},
    }

    model = resolve_venue_cost_model(rules, "tiger_openapi:COMEX:MGC")

    assert model["type"] == "fixed_per_contract"
    assert model["commission_per_contract_side"] == 2.70
