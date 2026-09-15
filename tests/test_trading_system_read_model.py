from __future__ import annotations

import json
from copy import deepcopy

import pytest

from schemas.accounting import build_accounting_snapshot
from services.accounting_projection_core import OPEN_ORDER_STATES
from services.order_lifecycle import LEGAL_TRANSITIONS, ORDER_STATES, TERMINAL_STATES
from services.trading_system_read_model import (
    TRADING_SYSTEM_READ_MODEL_SCHEMA,
    project_external_dca_lifecycle,
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
                "notional_mode": "auto",
                "leverage": 10.0,
                "min_net_profit_per_grid_usd": 10.35,
                "target_net_profit_per_grid_usd": 10.0,
                "orders": [
                    {
                        "preview_order_id": f"preview-{index}",
                        "side": "buy" if index % 2 else "sell",
                        "price": 3900.0 + index,
                        "tp": 3903.0 + index,
                        "sl": 3823.0 + index,
                        "planned_net_profit_usd": 10.5 + index / 100,
                    }
                    for index in range(25)
                ],
            },
            "risk_budget": {"max_loss": 77.0, "leverage": 10.0, "actual_leverage": 9.8},
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
        "cycle_packages": [],
        "review_cycle_id": None,
        "strategy_shadows": [],
        "execution_shadow": {"status": "observing"},
        "ui_capabilities": {"market_timeframes": ["1m", "5m"]},
        "safety": {"one_production_strategy": True},
    }


def test_read_model_exposes_only_auditable_grid_lifecycle_claims() -> None:
    source = _source()
    source["production_execution"]["grid_lifecycle"] = {
        "schema_version": "grid-line-lifecycle-evidence-v1",
        "status": "verified",
        "completed_rearmed_count": 1,
        "unverified_count": 1,
        "reconciliation_status": "ok",
        "lines": [
            {"line_id": "line-a", "generation": 1, "status": "completed_rearmed"},
            {"line_id": "line-b", "generation": 1, "status": "unverified", "evidence_missing": ["target_fill"]},
        ],
    }

    lifecycle = project_trading_system_read_model(source).to_dict()["execution"]["grid_lifecycle"]

    assert lifecycle["status"] == "verified"
    assert lifecycle["completed_rearmed_count"] == 1
    assert lifecycle["unverified_count"] == 1
    assert lifecycle["synthetic_candle_fill_inference"] is False
    assert lifecycle["lines"][1]["evidence_missing"] == ["target_fill"]


def _external_dca_state(status: str = "PROTECTION_ACTIVE") -> dict:
    return {
        "schema_version": "standard-broker-external-dca-v1",
        "status": status,
        "plan_id": "external-plan-1",
        "plan_digest": "sha256:" + "a" * 64,
        "source_strategy_plan_id": "strategy-plan-paper-1",
        "source_strategy_plan_digest": "sha256:" + "b" * 64,
        "strategy_session_id": "session-1",
        "strategy_revision_id": "revision-1",
        "broker_id": "hyperliquid",
        "environment": "testnet",
        "profile_id": "hyperliquid-testnet-position-protection",
        "capability_revision": "hyperliquid-testnet-position-protection-runtime-v1",
        "instrument_id": "PAXG-USD-PERP",
        "account_fingerprint": "sha256:" + "c" * 64,
        "execution_market_source": {
            "source_id": "hyperliquid.external_testnet",
            "broker_id": "hyperliquid",
            "environment": "testnet",
            "instrument_id": "PAXG-USD-PERP",
            "execution_venue": True,
        },
        "receipts": [
            {
                "order_id": "external-plan-1:entry:0",
                "state": "filled",
                "instrument_id": "PAXG-USD-PERP",
            }
        ],
        "position_quantity": "0.01",
        "average_entry_price": "4000",
        "actual_fee_usd": "0.10",
        "entry_facts": {
            "provenance": {
                "account_fingerprint": "sha256:" + "c" * 64,
                "runtime_id": "runtime-1",
                "release_sha": "d" * 40,
                "capability_revision": "hyperliquid-testnet-position-protection-runtime-v1",
                "transport_state": "external_testnet",
            },
            "fills": [{"fill_id": "fill-1", "quantity": "0.01", "price": "4000"}],
            "fees": [{"fee_id": "fee-1", "amount_usd": "0.10"}],
            "positions": [{"instrument_id": "PAXG-USD-PERP", "signed_quantity": "0.01"}],
            "reconciliation": {
                "coherent": True,
                "freshness": "fresh",
                "cursor": "cursor-1",
                "open_order_ids": [],
                "evidence_digest": "sha256:" + "e" * 64,
            },
        },
        "protection": {
            "covered_quantity": "0.01",
            "confirmed": {
                "state": "active",
                "covered_quantity": "0.01",
                "observation_digest": "sha256:" + "f" * 64,
            },
        },
        "next_action": "submit_next_entry_only_after_attended_price_gate",
        "updated_at": "2026-08-24T01:00:00+00:00",
    }


def test_read_model_projects_external_dca_identity_facts_protection_and_reconciliation() -> None:
    source = _source()
    source["external_dca_lifecycle"] = _external_dca_state()

    model = project_trading_system_read_model(source, broker=_broker()).to_dict()
    external = model["external_dca"]

    assert external["status"] == "PROTECTION_ACTIVE"
    assert external["identity"]["broker_id"] == "hyperliquid"
    assert external["identity"]["environment"] == "testnet"
    assert external["identity"]["instrument_id"] == "PAXG-USD-PERP"
    assert external["identity"]["source_strategy_plan_id"] == "strategy-plan-paper-1"
    assert external["counts"] == {
        "order_count": 1,
        "open_order_count": 0,
        "fill_count": 1,
        "fee_count": 1,
        "position_count": 1,
    }
    assert external["facts"]["cursor"] == "cursor-1"
    assert external["facts"]["freshness"] == "fresh"
    assert external["protection"]["status"] == "active"
    assert external["reconciliation"]["status"] == "pass"
    assert external["market_compatibility"]["status"] == "blocked"
    assert "external_instrument_does_not_match_dashboard_market" in external["blockers"]
    assert model["execution"]["external_dca"]["status"] == "PROTECTION_ACTIVE"
    assert model["execution"]["broker"]["provider"] == "hyperliquid"
    assert model["execution"]["broker"]["environment"] == "testnet"
    assert model["execution"]["broker"]["armed"] is False
    assert model["execution"]["broker"]["ready"] is False


@pytest.mark.parametrize(
    "status",
    [
        "WAITING_ENTRY",
        "ENTRY_FILLED_PENDING_FACTS",
        "PROTECTION_ACTIVE",
        "BLOCKED",
        "FLATTEN_SUBMIT_INTENT_RESERVED",
        "FLAT_RECONCILED",
    ],
)
def test_external_dca_statuses_are_not_collapsed_in_read_model(status: str) -> None:
    projected = project_external_dca_lifecycle(_external_dca_state(status))
    assert projected["status"] == status
    assert projected["status_label"]


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


def test_cloud_health_is_exposed_as_read_only_operator_truth() -> None:
    source = _source()
    source["cloud_health"] = {
        "schema_version": "cloud-paper-health-v1",
        "status": "degraded",
        "incidents": [
            {
                "stage": "backup",
                "code": "backup_missing_or_stale",
                "next_action": "Run the verified backup job.",
            }
        ],
        "control_actions_executed": 0,
    }

    model = project_trading_system_read_model(source).to_dict()

    assert model["operations"]["cloud_health"]["status"] == "degraded"
    assert model["operations"]["cloud_health"]["incidents"][0]["stage"] == "backup"
    assert model["safety"]["read_only"] is True


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
        "strategy_type": "grid",
        "strategy_type_label": "Grid",
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
        "notional_mode": "auto",
        "notional_mode_label": "自动利润目标",
        "max_loss": 77.0,
        "leverage": 10.0,
        "actual_leverage": 9.8,
        "min_net_profit_per_grid_usd": 10.35,
        "target_net_profit_per_grid_usd": 10.0,
        "display_label": "中性 · 稳健 · 等价差 · 3900–4100 · 50 格 · 每格 2800 USD",
    }
    assert model["execution"]["counts"] == {
        "order_count": 25,
        "open_order_count": 25,
        "accepted_order_count": 25,
        "unknown_order_count": 0,
        "open_position_count": 0,
        "trade_count": 1,
        "open_trade_count": 0,
        "completed_trade_count": 1,
        "completed_round_trip_count": 1,
        "chronology_invalid_trade_count": 0,
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
    assert len(model["execution"]["accepted_orders"]) == 25
    assert model["execution"]["trades"][0]["close_reason"] == "tp"
    assert model["execution"]["trades"][0]["close_reason_label"] == "TP"
    assert source == before


def _yesterday_report(*, status: str = "complete", net: float = 12.5, fees: float | None = 0.5, funding: float | None = -0.1) -> dict:
    execution = {
        "realized_pnl": net,
        "trade_count": 3,
        "fill_count": 6,
    }
    if fees is not None:
        execution["fees"] = fees
    if funding is not None:
        execution["funding"] = funding
    return {
        "schema_version": "trading-daily-24h-v1",
        "status": status,
        "report_date": "2026-07-17",
        "execution": execution,
        "provenance": {
            "cycle_packages": [
                {"cycle_id": "2026-07-16_NIGHT", "package_hash": "sha256:package-1"},
                {"cycle_id": "2026-07-17_DAY", "package_hash": "sha256:package-2"},
                {"cycle_id": "2026-07-17_NIGHT", "package_hash": "sha256:package-3"},
            ]
        },
        "report_hash": "sha256:report-1",
    }


@pytest.mark.parametrize("net", [12.5, -4.25, 0.0])
def test_read_model_projects_complete_yesterday_pnl_without_unrealized_or_shadow(net: float) -> None:
    source = _source()
    source["daily_reports"] = {
        "source": "terminal_cycle_packages",
        "reports": [_yesterday_report(net=net)],
    }

    model = project_trading_system_read_model(
        source,
        generated_at="2026-07-18T01:02:04+00:00",
    ).to_dict()

    yesterday = model["yesterday_pnl"]
    assert yesterday["status"] == "complete"
    assert yesterday["report_date"] == "2026-07-17"
    assert yesterday["net_realized_pnl"] == net
    assert yesterday["fees"] == 0.5
    assert yesterday["funding"] == -0.1
    assert yesterday["trade_count"] == 3
    assert yesterday["fill_count"] == 6
    assert yesterday["includes_unrealized"] is False
    assert len(yesterday["supporting_packages"]) == 3


def test_read_model_marks_yesterday_pnl_partial_when_cost_evidence_is_missing() -> None:
    source = _source()
    source["daily_reports"] = {
        "reports": [_yesterday_report(fees=None, funding=None)],
    }

    model = project_trading_system_read_model(
        source,
        generated_at="2026-07-18T01:02:04+00:00",
    ).to_dict()

    yesterday = model["yesterday_pnl"]
    assert yesterday["status"] == "partial"
    assert yesterday["net_realized_pnl"] == 12.5
    assert yesterday["fees"] is None
    assert yesterday["funding"] is None
    assert "yesterday_fees_missing" in yesterday["blockers"]
    assert "yesterday_funding_missing" in yesterday["blockers"]


def test_read_model_never_renders_missing_yesterday_pnl_as_zero() -> None:
    source = _source()
    source["daily_reports"] = {"reports": []}

    model = project_trading_system_read_model(
        source,
        generated_at="2026-07-18T01:02:04+00:00",
    ).to_dict()

    yesterday = model["yesterday_pnl"]
    assert yesterday["status"] == "evidence_insufficient"
    assert yesterday["net_realized_pnl"] is None
    assert yesterday["report_date"] == "2026-07-17"


def test_read_model_quarantines_an_inverted_completed_trade_from_normal_counts() -> None:
    source = _source()
    accounting = source["production_execution"]["accounting_snapshot"]
    accounting["trades"][0].update({
        "entry_ts": "2026-07-18T01:00:00+00:00",
        "exit_ts": "2026-07-18T00:55:00+00:00",
    })
    accounting["reconciliation"] = {
        "status": "drift",
        "issues": [{
            "code": "closed_trade_exit_before_entry",
            "trade_id": "trade-1",
            "entry_ts": "2026-07-18T01:00:00+00:00",
            "exit_ts": "2026-07-18T00:55:00+00:00",
        }],
    }

    model = project_trading_system_read_model(source).to_dict()

    trade = model["execution"]["trades"][0]
    assert trade["status"] == "chronology_invalid"
    assert trade["close_reason_label"] == "时间异常"
    assert model["execution"]["counts"]["completed_trade_count"] == 0
    assert model["execution"]["counts"]["chronology_invalid_trade_count"] == 1


def test_read_model_quarantines_from_the_dedicated_reconciliation_bucket() -> None:
    source = _source()
    accounting = source["production_execution"]["accounting_snapshot"]
    accounting["trades"][0].update({
        "entry_ts": "2026-07-18T01:00:00+00:00",
        "exit_ts": "2026-07-18T00:55:00+00:00",
    })
    accounting["reconciliation"] = {
        "status": "pass",
        "issues": [],
        "quarantined": [{
            "code": "closed_trade_exit_before_entry",
            "trade_id": "trade-1",
            "entry_ts": "2026-07-18T01:00:00+00:00",
            "exit_ts": "2026-07-18T00:55:00+00:00",
        }],
    }

    model = project_trading_system_read_model(source).to_dict()

    trade = model["execution"]["trades"][0]
    assert trade["status"] == "chronology_invalid"
    assert trade["close_reason_label"] == "时间异常"
    assert model["execution"]["counts"]["completed_trade_count"] == 0
    assert model["execution"]["counts"]["chronology_invalid_trade_count"] == 1


def test_read_model_projects_dca_round_summary_without_grid_geometry_warning() -> None:
    source = _source()
    source["production_plan"] = {
        "schema_version": "strategy-plan-v1",
        "strategy_type": "dca",
        "strategy_plan_id": "plan-dca-1",
        "cycle_id": "2026-07-18_DAY",
        "version": 8,
        "status": "active",
        "direction": "long",
        "dca": {
            "max_additions": 3,
            "notional_per_addition": 2_000.0,
            "target_price": 4_050.0,
            "stop_price": 3_970.0,
            "loop_enabled": False,
            "total_possible_notional": 6_000.0,
            "entries": [
                {"preview_entry_id": "dca-entry-01", "price": 4_004.0},
                {"preview_entry_id": "dca-entry-02", "price": 3_996.0},
                {"preview_entry_id": "dca-entry-03", "price": 3_988.0},
            ],
        },
        "risk_budget": {
            "selected_leverage": 10.0,
            "actual_leverage_at_full_depth": 0.6,
            "maximum_loss_at_full_depth": 47.0,
        },
    }
    source["runtime"].update({
        "strategy_plan_id": "plan-dca-1",
        "strategy_plan_version": 8,
        "strategy_type": "dca",
    })

    model = project_trading_system_read_model(
        source,
        broker=_broker(),
        generated_at="2026-07-18T01:02:04+00:00",
    ).to_dict()

    summary = model["strategy"]["summary"]
    assert summary["strategy_type"] == "dca"
    assert summary["display_label"] == (
        "做多 · DCA · 最多 3 次 · 每次 2000 USD · 目标 4050"
    )
    assert summary["dca_entry_levels"] == [4_004.0, 3_996.0, 3_988.0]
    assert summary["max_loss"] == 47.0
    assert "strategy_geometry_incomplete" not in model["completeness"]["issues"]


def test_read_model_projects_dca_aggregate_target_as_event_driven_protection() -> None:
    source = _source()
    source["production_plan"] = {
        "schema_version": "strategy-plan-v1",
        "strategy_type": "dca",
        "strategy_plan_id": "plan-dca-1",
        "version": 8,
        "status": "active",
        "direction": "long",
        "dca": {
            "max_additions": 3,
            "notional_per_addition": 2_000.0,
            "target_price": 4_050.0,
            "stop_price": 3_970.0,
            "entries": [{"price": 4_004.0}],
        },
    }
    source["dca_lifecycle"] = {
        "strategy_plan_id": "plan-dca-1",
        "round_id": "round-dca-1",
        "status": "open",
        "additions_filled": 2,
        "open_quantity": 1.001,
        "average_entry_price": 4_000.0,
        "target_generations": [
            {"target_id": "old", "generation": 1, "status": "cancelled", "price": 4_050.0, "quantity": 0.5},
            {"target_id": "active", "generation": 2, "status": "accepted", "price": 4_050.0, "quantity": 1.001},
        ],
        "active_target": {"target_id": "active", "generation": 2, "status": "accepted", "side": "sell", "price": 4_050.0, "quantity": 1.001},
    }

    model = project_trading_system_read_model(source).to_dict()

    lifecycle = model["execution"]["dca_lifecycle"]
    assert lifecycle["status"] == "open"
    assert lifecycle["protection_semantics"] == "event_driven_aggregate_target_not_entry_order"
    assert lifecycle["active_target"]["generation"] == 2
    assert lifecycle["active_target"]["quantity"] == 1.001
    assert len(lifecycle["target_generations"]) == 2


def test_read_model_projects_only_authoritative_order_rows_without_fill_inference() -> None:
    source = _source()
    source["production_execution"]["orders"] = []
    source["production_execution"]["current_cycle_fills"] = [{
        "fill_id": "full-fill-1",
        "order_id": "order-1",
        "quantity": 1.5,
    }]

    model = project_trading_system_read_model(
        source,
        risk_decision=_risk(),
        broker=_broker(),
        generated_at="2026-07-18T01:02:04+00:00",
    ).to_dict()

    assert model["execution"]["orders"] == []
    assert model["execution"]["counts"]["order_count"] == 0
    assert model["execution"]["counts"]["open_order_count"] == 0
    assert model["execution"]["scopes"]["orders_and_positions"]["order_state_source"] == (
        "current_execution_snapshot"
    )


def test_order_state_aliases_share_canonical_lifecycle_metadata() -> None:
    source = _source()
    source["production_execution"]["orders"] = [
        {"order_id": "order-partial", "state": "partiallyfilled"},
        {"order_id": "order-cancel", "state": "canceled"},
    ]

    model = project_trading_system_read_model(
        source,
        risk_decision=_risk(),
        broker=_broker(),
        generated_at="2026-07-18T01:02:04+00:00",
    ).to_dict()

    assert [(row["state"], row["state_label"]) for row in model["execution"]["orders"]] == [
        ("partially_filled", "部分成交"),
        ("cancelled", "已撤单"),
    ]
    assert model["execution"]["counts"]["open_order_count"] == 1
    assert [row["is_terminal"] for row in model["execution"]["orders"]] == [False, True]


def test_all_canonical_order_states_have_safe_presentation_metadata() -> None:
    source = _source()
    states = sorted(ORDER_STATES | OPEN_ORDER_STATES)
    source["production_execution"]["orders"] = [
        {"order_id": f"order-{state}", "state": state} for state in states
    ]

    model = project_trading_system_read_model(
        source,
        risk_decision=_risk(),
        broker=_broker(),
        generated_at="2026-07-18T01:02:04+00:00",
    ).to_dict()

    projected = model["execution"]["orders"]
    assert [row["state"] for row in projected] == states
    assert all(row["state_known"] is True for row in projected)
    assert all(row["state_rank"] > 0 for row in projected)
    assert all(row["state_revision"] is None for row in projected)
    assert all(row["state_label"] != "未知状态" for row in projected)
    assert {
        row["state"] for row in projected if row["is_open"]
    } == OPEN_ORDER_STATES
    assert {
        row["state"] for row in projected if row["is_terminal"]
    } == TERMINAL_STATES
    ranks = {row["state"]: row["state_rank"] for row in projected}
    assert all(
        ranks[next_state] >= ranks[state]
        for state, next_states in LEGAL_TRANSITIONS.items()
        for next_state in next_states
    )


def test_transition_history_exposes_revision_for_bidirectional_protection_recovery() -> None:
    source = _source()
    source["production_execution"]["orders"] = [{
        "order_id": "protected-order",
        "state": "protective_attached",
        "transitions": [
            {"from": "", "to": "entry"},
            {"from": "entry", "to": "submitting"},
            {"from": "submitting", "to": "accepted"},
            {"from": "accepted", "to": "filled"},
            {"from": "filled", "to": "protective_attached"},
            {"from": "protective_attached", "to": "protective_failed"},
            {"from": "protective_failed", "to": "protective_attached"},
        ],
    }]

    order = project_trading_system_read_model(
        source,
        risk_decision=_risk(),
        broker=_broker(),
        generated_at="2026-07-18T01:02:04+00:00",
    ).to_dict()["execution"]["orders"][0]

    assert order["state_rank"] == 60
    assert order["state_revision"] == 7


def test_state_revision_is_withheld_when_history_does_not_match_current_state() -> None:
    source = _source()
    source["production_execution"]["orders"] = [{
        "order_id": "unproven-order",
        "state": "protective_attached",
        "transitions": [{"from": "protective_attached", "to": "protective_failed"}],
    }]

    order = project_trading_system_read_model(
        source,
        risk_decision=_risk(),
        broker=_broker(),
        generated_at="2026-07-18T01:02:04+00:00",
    ).to_dict()["execution"]["orders"][0]

    assert order["state_revision"] is None


def test_unknown_order_state_uses_text_only_fallback_label() -> None:
    source = _source()
    source["runtime"].update({"desired_state": "stopped", "actual_state": "stopped"})
    source["production_execution"]["orders"] = [{
        "order_id": "hostile-order",
        "state": '<img src=x onerror="globalThis.pwned=true">',
    }]

    model = project_trading_system_read_model(
        source,
        risk_decision=_risk(),
        broker=_broker(),
        generated_at="2026-07-18T01:02:04+00:00",
    ).to_dict()

    order = model["execution"]["orders"][0]
    assert order["state_known"] is False
    assert order["state_label"] == "未知状态"
    assert order["state_rank"] == 0
    assert order["is_open"] is False
    assert model["execution"]["counts"]["unknown_order_count"] == 1
    assert model["completeness"] == {
        "status": "degraded",
        "issues": ["execution_order_state_unknown"],
    }
    assert model["runtime"]["can_start_when_authorized"] is False
    assert model["runtime"]["can_stop_when_authorized"] is True
    assert model["runtime"]["status"] == "degraded"
    assert model["runtime"]["status_label"] == "异常"


def test_running_nautilus_runtime_degrades_when_execution_tick_is_stale() -> None:
    source = _source(open_trade=True)
    source["runtime"].update({
        "desired_state": "running",
        "actual_state": "running",
        "execution_tick_health": {
            "status": "blocked",
            "reason": "heartbeat_stale",
            "age_seconds": 181.0,
            "max_age_seconds": 180,
        },
        "execution_tick_failure": {
            "status": "failed",
            "failure_phase": "ledger_write",
            "next_action": "检查本地账本输出是否可写。",
            "heartbeat_written": False,
        },
    })

    model = project_trading_system_read_model(
        source,
        risk_decision=_risk(),
        broker=_broker(),
        generated_at="2026-07-18T01:02:04+00:00",
    ).to_dict()

    assert model["runtime"]["status"] == "degraded"
    assert model["runtime"]["liveness_degraded"] is True
    assert model["runtime"]["execution_tick_health"]["reason"] == "heartbeat_stale"
    assert model["runtime"]["execution_tick_failure"]["failure_phase"] == "ledger_write"
    assert model["runtime"]["execution_tick_failure"]["next_action"] == "检查本地账本输出是否可写。"
    assert "running_with_execution_tick_unavailable" in model["completeness"]["issues"]


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


def test_order_protection_is_completed_only_from_the_exact_strategy_plan() -> None:
    source = _source()
    same_plan = source["production_execution"]["orders"][0]
    same_plan["source_fill_id"] = "strategy-grid:plan-7:preview-0"
    same_plan.pop("tp", None)
    same_plan.pop("sl", None)
    missing_plan = source["production_execution"]["orders"][1]
    missing_plan.pop("strategy_plan_id")
    missing_plan.update({"tp": 3999.0, "sl": 3800.0})
    mismatched_plan = source["production_execution"]["orders"][2]
    mismatched_plan["strategy_plan_id"] = "plan-old"
    mismatched_plan.update({"tp": 3999.0, "sl": 3800.0})
    ambiguous = source["production_execution"]["orders"][3]
    source["production_plan"]["grid"]["orders"].append(
        dict(source["production_plan"]["grid"]["orders"][3], preview_order_id="preview-duplicate")
    )
    wrong_source_plan = source["production_execution"]["orders"][4]
    wrong_source_plan["source_fill_id"] = "strategy-grid:plan-old:preview-4"
    wrong_source_plan.update({"tp": 3907.0, "sl": 3827.0})
    conflicting_identity = source["production_execution"]["orders"][5]
    conflicting_identity["preview_order_id"] = "preview-5"
    conflicting_identity["source_fill_id"] = "strategy-grid:plan-7:preview-6"
    conflicting_identity.update({"tp": 3908.0, "sl": 3828.0})
    missing_preview = source["production_execution"]["orders"][6]
    missing_preview["preview_order_id"] = "preview-missing"
    missing_preview.update({"tp": 3909.0, "sl": 3829.0})
    duplicate_preview = source["production_execution"]["orders"][7]
    duplicate_preview["preview_order_id"] = "preview-7"
    duplicate_preview.update({"tp": 3910.0, "sl": 3830.0})
    source["production_plan"]["grid"]["orders"].append(
        dict(source["production_plan"]["grid"]["orders"][7])
    )

    model = project_trading_system_read_model(
        source,
        risk_decision=_risk(),
        broker=_broker(),
        generated_at="2026-07-18T01:02:04+00:00",
    ).to_dict()
    orders = {row["order_id"]: row for row in model["execution"]["orders"]}

    assert orders["order-0"]["protection"] == {
        "status": "known",
        "tp": 3903.0,
        "sl": 3823.0,
        "source": "strategy_plan",
        "reason": None,
    }
    assert orders["order-1"]["protection"]["status"] == "unknown"
    assert orders["order-1"]["protection"]["reason"] == "strategy_plan_id_missing"
    assert orders["order-2"]["protection"]["status"] == "unknown"
    assert orders["order-2"]["protection"]["reason"] == "strategy_plan_id_mismatch"
    assert orders[ambiguous["order_id"]]["protection"]["status"] == "unknown"
    assert orders[ambiguous["order_id"]]["protection"]["reason"] == "strategy_plan_protection_incomplete"
    assert orders[wrong_source_plan["order_id"]]["protection"]["status"] == "unknown"
    assert (
        orders[wrong_source_plan["order_id"]]["protection"]["reason"]
        == "strategy_plan_order_identity_mismatch"
    )
    assert orders[conflicting_identity["order_id"]]["protection"]["status"] == "unknown"
    assert orders[missing_preview["order_id"]]["protection"]["status"] == "unknown"
    assert orders[duplicate_preview["order_id"]]["protection"]["status"] == "unknown"


def test_order_profit_and_open_position_fields_are_projected_from_exact_plan() -> None:
    source = _source(open_trade=True)
    source["production_execution"]["accounting_snapshot"]["positions"][0][
        "trade_id"
    ] = "order-0"

    model = project_trading_system_read_model(
        source,
        risk_decision=_risk(),
        broker=_broker(),
        generated_at="2026-07-18T01:02:04+00:00",
    ).to_dict()

    order = model["execution"]["orders"][0]
    position = model["execution"]["open_positions"][0]
    assert order["planned_net_profit_usd"] == 10.5
    assert position["remaining_quantity"] == 1.0
    assert position["protection"] == order["protection"]


def test_inherited_running_order_resolves_protection_from_its_originating_plan() -> None:
    source = _source()
    active = source["production_plan"]
    inherited = deepcopy(active)
    inherited.update(
        {
            "strategy_plan_id": "plan-6",
            "version": 6,
            "status": "superseded",
        }
    )
    active["inherited_plan_ids"] = ["plan-6"]
    source["production_plan_history"] = [inherited, active]
    order = source["production_execution"]["orders"][0]
    order["strategy_plan_id"] = "plan-6"
    order["strategy_plan_version"] = 6
    order["source_fill_id"] = "strategy-grid:plan-6:preview-0"
    order.pop("tp", None)
    order.pop("sl", None)

    model = project_trading_system_read_model(
        source,
        risk_decision=_risk(),
        broker=_broker(),
        generated_at="2026-07-18T01:02:04+00:00",
    ).to_dict()
    projected = next(
        row for row in model["execution"]["orders"] if row["order_id"] == order["order_id"]
    )

    assert projected["protection"] == {
        "status": "known",
        "tp": 3903.0,
        "sl": 3823.0,
        "source": "strategy_plan",
        "reason": None,
    }


def test_mismatched_risk_observation_is_never_presented_as_current_permission() -> None:
    source = _source()
    model = project_trading_system_read_model(
        source,
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


def test_market_order_non_finite_price_does_not_hide_the_operator_read_model() -> None:
    source = _source()
    order = source["production_execution"]["orders"][0]
    order.update({
        "state": "filled",
        "event": "flatten",
        "order_type": "market",
        "price": float("nan"),
        "requested_price": 4121.57,
    })

    model = project_trading_system_read_model(
        source,
        risk_decision=_risk(),
        broker=_broker(),
        generated_at="2026-07-18T01:02:04+00:00",
    ).to_dict()
    projected = next(
        row for row in model["execution"]["orders"] if row["order_id"] == order["order_id"]
    )

    assert projected["price"] is None
    assert projected["requested_price"] == 4121.57
    assert projected["price_status"] == "missing_market_order_execution_price"
    json.dumps(model, allow_nan=False)


def test_review_packages_and_shadows_remain_separate_read_only_evidence() -> None:
    source = _source()
    package = {
        "cycle_id": "2026-07-17_NIGHT",
        "status": "closed",
        "package_hash": "package-hash",
        "strategy_plan": {"strategy_plan_id": "plan-old", "version": 6},
    }
    source["cycle_packages"] = [package]
    source["review_cycle_id"] = "2026-07-17_NIGHT"
    source["strategy_shadows"] = [{
        "cycle_id": "2026-07-17_NIGHT",
        "variant_id": "production",
        "metrics": {"realized_pnl": 3.0, "unrealized_pnl": 0.0},
    }]
    source["strategy_shadow_promotion"] = {
        "status": "collecting_evidence",
        "safety": {"read_only": True, "submits_orders": False},
    }
    source["safe_repair_queue"] = {
        "counts": {"requires_human_confirmation": 1},
        "safety": {"command_authority": False},
    }

    model = project_trading_system_read_model(
        source,
        risk_decision=_risk(),
        broker=_broker(),
        generated_at="2026-07-18T01:02:04+00:00",
    ).to_dict()

    assert model["review"]["cycle_packages"] == [package]
    assert model["review"]["selected_cycle_id"] == "2026-07-17_NIGHT"
    assert model["research"]["strategy_shadows"][0]["variant_id"] == "production"
    assert model["research"]["strategy_shadow_promotion"]["safety"]["submits_orders"] is False
    assert model["operations"]["safe_repair_queue"]["safety"]["command_authority"] is False
    assert model["safety"]["read_only"] is True
    assert model["safety"]["command_authority"] is False


def test_review_projection_does_not_fill_missing_evidence_fields() -> None:
    source = _source()
    source["cycle_packages"] = [{"cycle_id": "2026-07-17_NIGHT", "status": "closed"}]

    model = project_trading_system_read_model(
        source,
        risk_decision=_risk(),
        broker=_broker(),
    ).to_dict()

    package = model["review"]["cycle_packages"][0]
    assert "strategy_plan" not in package
    assert "execution" not in package
    assert "review" not in package


def test_strategy_summary_does_not_invent_notional_provenance() -> None:
    source = _source()
    source["production_plan"]["grid"].pop("notional_mode")

    model = project_trading_system_read_model(
        source,
        risk_decision=_risk(),
        broker=_broker(),
    ).to_dict()

    assert model["strategy"]["summary"]["notional_mode"] is None
    assert model["strategy"]["summary"]["notional_mode_label"] == "来源未知"


def test_projector_contains_no_provider_or_engine_selection_branches() -> None:
    from pathlib import Path

    source = Path(__file__).parents[1] / "services" / "trading_system_read_model.py"
    text = source.read_text(encoding="utf-8").lower()

    for token in ("binance", "tiger", "legacy_paper", "nautilus_paper"):
        assert token not in text


def test_read_model_projects_active_park_strategy_as_current_strategy() -> None:
    park = {
        "schema_version": "park-paper-public-read-model-v1",
        "generated_at": "2026-08-18T01:02:04+00:00",
        "status": "ok",
        "blockers": [],
        "strategy": {
            "active": True,
            "state": "ACTIVE_LOCKED",
            "strategy_session_id": "session-park-1",
            "strategy_revision_id": "revision-park-3",
            "plan_digest": "sha256:park-plan",
            "strategy_type": "grid",
            "direction": "short",
            "lower_price_boundary": 3800.0,
            "upper_price_boundary": 4000.0,
            "stop_price": 4000.0,
            "take_profit_price": None,
            "maximum_leverage": 10.0,
            "maximum_acceptable_loss": 500.0,
            "maximum_notional": 40_000.0,
            "theoretical_max_loss": 480.0,
            "order_count": 19,
            "selected_constraint": "maximum_acceptable_loss",
            "grid_entry_range": {"lower": 3810.0, "upper": 3990.0},
            "grid_spacing": 10.0,
            "grid_rung_count": 19,
            "grid_rung_prices": [3810.0, 3820.0, 3830.0],
        },
        "execution": {
            "counts": {
                "accepted_orders": 12,
                "filled_orders": 3,
                "fills": 7,
                "open_positions": 2,
                "closed_positions": 1,
            },
            "reconciliation": {"status": "ok", "issues": []},
        },
        "market": {"price": 3910.0, "fresh": True, "source": "trusted-paper-market"},
        "safety": {"status": "pass", "age_seconds": 4.0, "paper_only": True},
    }

    model = project_trading_system_read_model(
        _source(),
        risk_decision=_risk(),
        broker=_broker(),
        park=park,
        generated_at="2026-08-18T01:02:04+00:00",
    ).to_dict()

    assert model["current_strategy"] == {
        "schema_version": "park-current-strategy-summary-v1",
        "source": "park_strategy_session",
        "active": True,
        "status": "active_locked",
        "status_label": "运行中",
        "contract_status": "authoritative",
        "blockers": [],
        "identity": {
            "strategy_session_id": "session-park-1",
            "strategy_revision_id": "revision-park-3",
            "plan_digest": "sha256:park-plan",
        },
        "specification": {
            "strategy_type": "grid",
            "direction": "short",
            "lower_price_boundary": 3800.0,
            "upper_price_boundary": 4000.0,
            "stop_price": 4000.0,
            "take_profit_price": None,
            "maximum_leverage": 10.0,
            "maximum_acceptable_loss": 500.0,
            "maximum_notional": 40_000.0,
            "theoretical_max_loss": 480.0,
            "order_count": 19,
            "selected_constraint": "maximum_acceptable_loss",
            "grid_entry_range": {"lower": 3810.0, "upper": 3990.0},
            "grid_spacing": 10.0,
            "grid_rung_count": 19,
            "grid_rung_prices": [3810.0, 3820.0, 3830.0],
        },
        "execution": {
            "accepted_order_count": 12,
            "filled_order_count": 3,
            "fill_count": 7,
            "open_position_count": 2,
            "closed_position_count": 1,
            "reconciliation_status": "ok",
        },
        "recording": {},
        "freshness": {
            "generated_at": "2026-08-18T01:02:04+00:00",
            "market_fresh": True,
            "safety_status": "pass",
            "safety_age_seconds": 4.0,
        },
    }
    assert model["contract"]["source_identities"]["strategy_session_id"] == "session-park-1"
    assert model["contract"]["source_identities"]["strategy_revision_id"] == "revision-park-3"


def test_read_model_exposes_legacy_exposure_as_a_cutover_blocker() -> None:
    park = {
        "schema_version": "park-paper-public-read-model-v1",
        "generated_at": "2026-08-18T01:02:04+00:00",
        "status": "blocked",
        "blockers": ["active_strategy_missing"],
        "strategy": {
            "active": False,
            "state": "IDLE_CLEAN",
            "strategy_session_id": None,
            "strategy_revision_id": None,
            "plan_digest": None,
        },
        "execution": {
            "counts": {
                "accepted_orders": 0,
                "filled_orders": 0,
                "fills": 0,
                "open_positions": 0,
                "closed_positions": 0,
            },
            "reconciliation": {"status": "missing", "issues": ["snapshot_missing"]},
        },
        "market": {},
        "safety": {"status": "missing", "age_seconds": None, "paper_only": None},
    }

    model = project_trading_system_read_model(
        _source(open_trade=True),
        risk_decision=_risk(),
        broker=_broker(),
        park=park,
        generated_at="2026-08-18T01:02:04+00:00",
    ).to_dict()

    current = model["current_strategy"]
    assert current["source"] == "legacy_exposure_blocker"
    assert current["active"] is False
    assert current["status"] == "migration_blocked"
    assert current["status_label"] == "迁移阻塞"
    assert current["contract_status"] == "blocked"
    assert current["blockers"] == [
        "active_strategy_missing",
        "legacy_cycle_exposure_without_park_identity",
    ]
    assert current["identity"] == {
        "strategy_session_id": None,
        "strategy_revision_id": None,
        "plan_digest": None,
    }
    assert set(current["specification"].values()) == {None}
    assert current["execution"]["accepted_order_count"] == 25
    assert current["execution"]["open_position_count"] == 1
    assert "legacy_cycle_exposure_without_park_identity" in model["completeness"]["issues"]


def test_read_model_distinguishes_missing_park_evidence_from_clean_idle() -> None:
    park = {
        "generated_at": "2026-08-18T01:02:04+00:00",
        "status": "blocked",
        "blockers": ["active_strategy_missing", "safety_evidence_not_passing"],
        "strategy": {
            "active": False,
            "state": "IDLE_CLEAN",
            "strategy_session_id": None,
            "strategy_revision_id": None,
            "plan_digest": None,
        },
        "execution": {"counts": {}, "reconciliation": {"status": "missing"}},
        "ledger": {"session": "park-session-x", "active": False, "equity": 9877.86, "starting_cash": 10000.0},
        "market": {},
        "safety": {"status": "missing"},
    }

    source = _source()
    source["production_execution"]["orders"] = []
    source["production_execution"]["accounting_snapshot"]["positions"] = []
    source["production_execution"]["accounting_snapshot"]["counts"]["open_position_count"] = 0
    model = project_trading_system_read_model(
        source,
        risk_decision=_risk(),
        broker=_broker(),
        park=park,
        generated_at="2026-08-18T01:02:04+00:00",
    ).to_dict()

    current = model["current_strategy"]
    assert current["ledger"]["equity"] == 9877.86 and current["ledger"]["active"] is False
    assert current["status"] == "evidence_blocked"
    assert current["status_label"] == "证据阻塞"
    assert current["contract_status"] == "blocked"
    assert current["active"] is False
