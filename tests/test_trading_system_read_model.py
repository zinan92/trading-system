from __future__ import annotations

import json
from copy import deepcopy

from schemas.accounting import build_accounting_snapshot
from services.trading_system_read_model import (
    TRADING_SYSTEM_READ_MODEL_SCHEMA,
    project_trading_system_read_model,
)


def _accounting(*, open_trade: bool = False) -> dict:
    trades = [
        {
            "trade_id": "trade-1",
            "status": "open" if open_trade else "closed",
            "side": "long",
            "entry_price": 4000.0,
            "remaining_units": 1.0 if open_trade else 0.0,
            "strategy_plan_id": "plan-7",
            "strategy_plan_version": 7,
        }
    ]
    fills = [
        {"fill_id": "fill-entry", "trade_id": "trade-1", "event": "entry", "price": 4000.0},
    ]
    if not open_trade:
        fills.append(
            {"fill_id": "fill-exit", "trade_id": "trade-1", "event": "target", "price": 4003.0}
        )
    return build_accounting_snapshot(
        source_type="production_history",
        source_name="versioned_strategy_plan_fills_only",
        source_schema_version="dualtrack-execution-v1",
        scope={"strategy_plan_scope": "all_versioned_production_plans"},
        currency="USD",
        orders=[],
        fills=fills,
        positions=trades,
        trades=trades,
        counts={
            "order_count": 0,
            "open_order_count": 0,
            "fill_count": len(fills),
            "entry_fill_count": 1,
            "exit_fill_count": 0 if open_trade else 1,
            "position_count": 1,
            "open_position_count": 1 if open_trade else 0,
            "trade_count": 1,
            "open_trade_count": 1 if open_trade else 0,
            "completed_trade_count": 0 if open_trade else 1,
        },
        pnl={
            "gross_realized_pnl": 3.0 if not open_trade else 0.0,
            "commission": 0.2 if not open_trade else 0.1,
            "funding": 0.0,
            "net_realized_pnl": 2.8 if not open_trade else -0.1,
            "unrealized_pnl": 1.5 if open_trade else 0.0,
            "total_pnl": 1.4 if open_trade else 2.8,
        },
        account={
            "starting_balance": 10_000.0,
            "ending_cash": 9_999.9 if open_trade else 10_002.8,
            "equity": 10_001.4 if open_trade else 10_002.8,
        },
        completeness={"status": "complete", "limitations": []},
        reconciliation={"status": "pass", "issues": []},
    ).to_dict()


def _source(*, open_trade: bool = False) -> dict:
    accounting = _accounting(open_trade=open_trade)
    orders = [
        {
            "order_id": f"order-{index}",
            "state": "accepted",
            "side": "buy" if index % 2 else "sell",
            "price": 3900.0 + index,
            "strategy_plan_id": "plan-7",
            "strategy_plan_version": 7,
        }
        for index in range(25)
    ]
    return {
        "schema_version": "strategy-production-console-v1",
        "cycle": {"cycle_id": "2026-07-18_DAY", "phase": "intraday"},
        "production_plan": {
            "schema_version": "strategy-plan-v1",
            "strategy_plan_id": "plan-7",
            "version": 7,
            "status": "active",
            "direction": "neutral",
            "style": "steady",
            "range": {"low": 3900.0, "high": 4100.0, "method": "d1_atr"},
            "grid": {
                "mode": "arithmetic",
                "count": 50,
                "notional_per_grid": 2800.0,
                "leverage": 3.0,
            },
            "risk_budget": {"max_loss": 77.0, "leverage": 3.0},
        },
        "proposals": [{"proposal_id": "ai-1", "source": "ai"}],
        "runtime": {
            "desired_state": "running",
            "actual_state": "running",
            "cycle_id": "2026-07-18_DAY",
            "strategy_plan_id": "plan-7",
            "strategy_plan_version": 7,
            "risk_decision_id": "risk-7",
            "updated_at": "2026-07-18T01:02:03+00:00",
        },
        "market": {
            "schema_version": "dualtrack-market-bars-v1",
            "symbol": "GOLD",
            "timeframe": "1m",
            "provider": "venue-a",
            "status": "ready",
            "fresh": True,
            "is_synthetic": False,
            "latest_close": 4004.0,
            "latest_timestamp": "2026-07-18T01:02:00+00:00",
            "bars": [],
        },
        "production_execution": {
            "schema_version": "dualtrack-execution-v1",
            "engine": "engine-a",
            "cycle_id": "2026-07-18_DAY",
            "orders": orders,
            "positions": accounting["positions"],
            "trades": accounting["trades"],
            "fills": accounting["fills"],
            "accounting_snapshot": accounting,
            "account": {
                "starting_cash": 10_000.0,
                "ending_cash": accounting["account"]["ending_cash"],
                "equity": accounting["account"]["equity"],
                "exposure": 4000.0 if open_trade else 0.0,
                "margin": 1333.33 if open_trade else 0.0,
            },
            "pnl": {
                "realized": accounting["pnl"]["net_realized_pnl"],
                "unrealized": accounting["pnl"]["unrealized_pnl"],
            },
            "trade_summary": {
                "trade_count": accounting["counts"]["trade_count"],
                "completed_trade_count": accounting["counts"]["completed_trade_count"],
            },
            "reconciliation": {"status": "ok", "issues": []},
        },
        "ledger": {"daily": [], "recent_reviews": []},
        "strategy_shadows": [],
        "execution_shadow": {"status": "observing"},
        "ui_capabilities": {"market_timeframes": ["1m", "5m"]},
        "safety": {"one_production_strategy": True},
    }


def _risk(decision_id: str = "risk-7") -> dict:
    return {
        "schema_version": "risk-decision-v1",
        "decision_id": decision_id,
        "request_id": "request-7",
        "outcome": "allow",
        "permissions": {
            "increase_exposure": True,
            "reduce_exposure": True,
            "cancel_orders": True,
        },
        "metrics": {
            "projected_max_loss": 77.0,
            "projected_actual_leverage": 2.8,
            "projected_margin": 1400.0,
        },
        "limits": {"max_plan_loss": 1000.0, "max_leverage": 10.0},
        "blockers": [],
        "warnings": [],
    }


def _broker() -> dict:
    return {
        "schema_version": "broker-read-model-v1",
        "adapter_name": "paper",
        "provider": "paper",
        "environment": "paper",
        "display_label": "Paper",
        "capabilities": ["preflight", "submit_order"],
        "armed": False,
        "ready": True,
        "credential_env_names": [],
    }


def test_read_model_copies_canonical_counts_and_projects_running_strategy() -> None:
    source = _source()
    before = deepcopy(source)

    model = project_trading_system_read_model(
        source,
        risk_decision=_risk(),
        broker=_broker(),
        generated_at="2026-07-18T01:02:04+00:00",
    ).to_dict()

    assert model["contract"]["schema_version"] == TRADING_SYSTEM_READ_MODEL_SCHEMA
    assert model["contract"]["snapshot_id"].startswith("trading-system-")
    assert model["contract"]["read_only"] is True
    assert model["strategy"]["summary"] == {
        "strategy_id": "production_grid",
        "plan_id": "plan-7",
        "plan_version": 7,
        "status": "active",
        "direction": "neutral",
        "direction_label": "中性",
        "style": "steady",
        "style_label": "稳健",
        "grid_mode": "arithmetic",
        "grid_mode_label": "等价差",
        "range_low": 3900.0,
        "range_high": 4100.0,
        "range_width": 200.0,
        "grid_count": 50,
        "spacing": 4.0,
        "spacing_ratio": None,
        "notional_per_grid": 2800.0,
        "leverage": 3.0,
        "display_label": "中性 · 稳健 · 等价差 · 3900–4100 · 50 格 · 每格 2800 USD",
    }
    assert model["execution"]["counts"] == {
        "open_order_count": 25,
        "open_position_count": 0,
        "trade_count": 1,
        "open_trade_count": 0,
        "completed_trade_count": 1,
        "completed_round_trip_count": 1,
        "fill_count": 2,
        "entry_fill_count": 1,
        "exit_fill_count": 1,
    }
    assert model["execution"]["pnl"]["total"] == 2.8
    assert model["execution"]["pnl"]["return_pct"] == 0.028
    assert model["execution"]["scopes"]["orders_and_positions"]["kind"] == "current_execution_cycle"
    assert model["execution"]["scopes"]["trades_fills_and_pnl"]["kind"] == "all_versioned_production_plans"
    assert model["risk"]["status"] == "current"
    assert model["risk"]["metrics"]["projected_max_loss"] == 77.0
    assert model["runtime"]["status"] == "running"
    assert model["runtime"]["open_order_count"] == 25
    assert model["runtime"]["can_stop_when_authorized"] is True
    assert source == before


def test_one_open_and_one_open_then_close_are_each_one_trade() -> None:
    open_model = project_trading_system_read_model(
        _source(open_trade=True),
        risk_decision=_risk(),
        broker=_broker(),
        generated_at="2026-07-18T01:02:04+00:00",
    ).to_dict()
    closed_model = project_trading_system_read_model(
        _source(open_trade=False),
        risk_decision=_risk(),
        broker=_broker(),
        generated_at="2026-07-18T01:02:04+00:00",
    ).to_dict()

    assert open_model["execution"]["counts"]["trade_count"] == 1
    assert open_model["execution"]["counts"]["completed_round_trip_count"] == 0
    assert closed_model["execution"]["counts"]["trade_count"] == 1
    assert closed_model["execution"]["counts"]["completed_round_trip_count"] == 1


def test_mismatched_risk_observation_is_never_presented_as_current_permission() -> None:
    model = project_trading_system_read_model(
        _source(),
        risk_decision=_risk("old-risk"),
        broker=_broker(),
        generated_at="2026-07-18T01:02:04+00:00",
    ).to_dict()

    assert model["risk"]["status"] == "historical"
    assert model["risk"]["current_decision"] == {}
    assert model["risk"]["latest_observation"]["decision_id"] == "old-risk"
    assert model["risk"]["authorizes_new_order"] is False
    assert "risk_decision_id_mismatch" in model["completeness"]["issues"]


def test_snapshot_identity_is_deterministic_and_json_safe() -> None:
    first = project_trading_system_read_model(
        _source(),
        risk_decision=_risk(),
        broker=_broker(),
        generated_at="2026-07-18T01:02:04+00:00",
    ).to_dict()
    second = project_trading_system_read_model(
        _source(),
        risk_decision=_risk(),
        broker=_broker(),
        generated_at="2026-07-18T01:02:04+00:00",
    ).to_dict()

    assert first["contract"]["snapshot_id"] == second["contract"]["snapshot_id"]
    json.dumps(first, allow_nan=False)


def test_projector_contains_no_provider_or_engine_selection_branches() -> None:
    from pathlib import Path

    source = Path(__file__).parents[1] / "services" / "trading_system_read_model.py"
    text = source.read_text(encoding="utf-8").lower()

    for token in ("binance", "tiger", "legacy_paper", "nautilus_paper"):
        assert token not in text
