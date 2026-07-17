from __future__ import annotations

import pytest

from services.strategy_plan_execution import build_plan_grid_entry_commands


def _plan() -> dict:
    return {
        "schema_version": "strategy-plan-v1",
        "strategy_plan_id": "plan-shared-grid",
        "cycle_id": "2026-07-18_DAY",
        "version": 4,
        "locked_at": "2026-07-18T01:00:00+00:00",
        "execution_context": {
            "market": {
                "price": 101.0,
                "symbol": "GOLD",
                "provider": "binance_usdm_futures",
            },
        },
        "grid": {
            "orders": [{
                "preview_order_id": "preview-01-buy",
                "side": "buy",
                "price": 100.0,
                "quantity": 1.25,
                "notional": 125.0,
                "sl": 98.0,
                "tp": 101.0,
            }],
        },
    }


def test_versioned_plan_projects_the_exact_production_grid_command() -> None:
    commands = build_plan_grid_entry_commands(_plan())

    assert commands == [{
        "cycle_id": "2026-07-18_DAY",
        "ts": "2026-07-18T01:00:00+00:00",
        "symbol": "GOLD",
        "side": "buy",
        "event": "entry",
        "order_type": "limit",
        "price": 100.0,
        "market_price": 101.0,
        "quantity": 1.25,
        "notional": 125.0,
        "sl": 98.0,
        "tp": 101.0,
        "source": "strategy_production_console",
        "source_fill_id": "strategy-grid:plan-shared-grid:preview-01-buy",
        "strategy_plan_id": "plan-shared-grid",
        "strategy_plan_version": 4,
    }]


def test_plan_projection_fails_closed_without_start_market_context() -> None:
    plan = _plan()
    plan.pop("execution_context")

    with pytest.raises(ValueError, match="execution market context"):
        build_plan_grid_entry_commands(plan)

