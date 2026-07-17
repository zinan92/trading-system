from __future__ import annotations

from copy import deepcopy

import pytest

from services.dualtrack_nautilus_parity_contract import platform_parity_code_hash
from services.execution_conformance import (
    CANDIDATE_RECEIPT_SCHEMA,
    EXECUTION_SCENARIO_SCHEMA,
    build_candidate_execution_receipt,
    build_execution_scenario,
    candidate_receipt_blockers,
)


CYCLE_ID = "2026-07-18_DAY"


def _config() -> dict:
    return {
        "capital_per_track_usd": 10_000.0,
        "max_leverage": 10.0,
        "execution_contract": {
            "schema_version": "dualtrack-execution-contract-v1",
            "execution_instrument_id": "XAUUSDT-PERP.BINANCE",
            "price_increment": "0.01",
            "quantity_increment": "0.001",
        },
        "paper_fee_model": {
            "maker_fee_rate": "0.0002",
            "taker_fee_rate": "0.0004",
            "funding_rate": "0.0001",
        },
    }


def _plan() -> dict:
    return {
        "schema_version": "strategy-plan-v1",
        "strategy_plan_id": "strategy-plan-a3",
        "cycle_id": CYCLE_ID,
        "version": 3,
        "locked_at": "2026-07-18T00:00:00+00:00",
        "status": "active",
        "direction": "long",
        "range": {"low": 99.0, "high": 101.0},
        "grid": {"count": 1, "notional_per_grid": 100.0},
    }


def _commands() -> list[dict]:
    return [{
        "cycle_id": CYCLE_ID,
        "ts": "2026-07-18T00:00:10+00:00",
        "symbol": "GOLD",
        "side": "buy",
        "event": "entry",
        "order_type": "limit",
        "price": 100.0,
        "market_price": 101.0,
        "quantity": 1.0,
        "notional": 100.0,
        "sl": 98.0,
        "tp": 101.0,
        "source": "strategy_production_console",
        "source_fill_id": "strategy-grid:strategy-plan-a3:grid-1",
        "strategy_plan_id": "strategy-plan-a3",
        "strategy_plan_version": 3,
    }]


def _events() -> list[dict]:
    return [
        {
            "cycle_id": CYCLE_ID,
            "event_id": "a3-event-1",
            "ts_event": "2026-07-18T00:01:00+00:00",
            "event_started_at": "2026-07-18T00:00:10+00:00",
            "price": 100.5,
            "open": 101.0,
            "high": 101.2,
            "low": 99.9,
            "fresh": True,
            "is_synthetic": False,
            "source": "market_db:binance_usdm_futures",
            "provider": "binance_usdm_futures",
            "instrument_id": "XAUUSDT",
        },
        {
            "cycle_id": CYCLE_ID,
            "event_id": "a3-event-2",
            "ts_event": "2026-07-18T00:02:00+00:00",
            "event_started_at": "2026-07-18T00:01:00+00:00",
            "price": 101.0,
            "open": 100.5,
            "high": 101.1,
            "low": 100.4,
            "fresh": True,
            "is_synthetic": False,
            "source": "market_db:binance_usdm_futures",
            "provider": "binance_usdm_futures",
            "instrument_id": "XAUUSDT",
        },
    ]


def _scenario() -> dict:
    return build_execution_scenario(
        candidate_id="grid-candidate-a3",
        plan=_plan(),
        commands=_commands(),
        market_events=_events(),
        config=_config(),
    )


def _snapshot(scenario: dict) -> dict:
    command = scenario["commands"][0]
    return {
        "schema_version": "dualtrack-execution-v1",
        "engine": "nautilus_paper",
        "cycle_id": CYCLE_ID,
        "orders": [{
            "order_id": "nautilus-command-a3",
            "state": "accepted",
            "side": command["side"],
            "event": command["event"],
            "order_type": command["order_type"],
            "price": command["price"],
            "quantity": command["quantity"],
            "ts": command["ts"],
            "strategy_plan_id": command["strategy_plan_id"],
            "strategy_plan_version": command["strategy_plan_version"],
        }],
        "fills": [],
        "positions": [],
        "account": {
            "starting_cash": 10_000.0,
            "realized_pnl": 0.0,
            "ending_cash": 10_000.0,
            "equity": 10_000.0,
            "margin": 0.0,
            "exposure": 0.0,
            "slippage": 0.0,
            "fees": 0.0,
            "funding": 0.0,
        },
        "pnl": {"realized": 0.0, "unrealized": 0.0},
        "mark": {"price": 101.0, "fresh": True, "source": "test"},
        "capabilities": {
            "native_order_lifecycle": True,
            "replay_version": "dualtrack-nautilus-replay-v6",
            "nautilus_version": "1.230.0",
            "platform_code_hash": platform_parity_code_hash(),
        },
    }


def _reconciliation(*, status: str = "ok") -> dict:
    return {
        "schema_version": "dualtrack-execution-reconciliation-v1",
        "engine": "nautilus_paper",
        "cycle_id": CYCLE_ID,
        "status": status,
        "issues": [] if status == "ok" else [{"code": "test_drift"}],
    }


def test_scenario_is_content_addressed_and_binds_exact_execution_inputs() -> None:
    first = _scenario()
    second = _scenario()

    assert first == second
    assert first["schema_version"] == EXECUTION_SCENARIO_SCHEMA
    assert first["scenario_id"].startswith("execution-scenario-")
    assert first["plan_identity"] == {
        "strategy_plan_id": "strategy-plan-a3",
        "strategy_plan_version": 3,
        "available_at": "2026-07-18T00:00:00+00:00",
    }
    assert first["evaluation_window"]["event_count"] == 2
    assert first["contracts"]["execution_contract_hash"].startswith("sha256:")
    assert first["contracts"]["fee_contract_hash"].startswith("sha256:")
    assert all(row["schema_version"] == "dualtrack-market-event-v1" for row in first["market_events"])


def test_scenario_rejects_preavailability_ohlc_and_nonchronological_events() -> None:
    leaked = _events()
    leaked[0]["event_started_at"] = "2026-07-17T23:59:59+00:00"
    with pytest.raises(ValueError, match="predates plan availability"):
        build_execution_scenario(
            candidate_id="grid-candidate-a3",
            plan=_plan(),
            commands=_commands(),
            market_events=leaked,
            config=_config(),
        )

    with pytest.raises(ValueError, match="chronological"):
        build_execution_scenario(
            candidate_id="grid-candidate-a3",
            plan=_plan(),
            commands=_commands(),
            market_events=list(reversed(_events())),
            config=_config(),
        )


def test_scenario_availability_cannot_predate_the_locked_plan() -> None:
    with pytest.raises(ValueError, match="availability predates StrategyPlan lock"):
        build_execution_scenario(
            candidate_id="grid-candidate-a3",
            plan=_plan(),
            commands=_commands(),
            market_events=_events(),
            config=_config(),
            available_at="2026-07-17T23:59:59+00:00",
        )


def test_scenario_rejects_commands_not_bound_to_the_versioned_plan() -> None:
    commands = _commands()
    commands[0]["strategy_plan_version"] = 2

    with pytest.raises(ValueError, match="plan identity"):
        build_execution_scenario(
            candidate_id="grid-candidate-a3",
            plan=_plan(),
            commands=commands,
            market_events=_events(),
            config=_config(),
        )


def test_candidate_receipt_requires_nautilus_reconciliation_and_is_stable() -> None:
    scenario = _scenario()
    namespace = f"strategy_shadow_{scenario['scenario_id'][-12:]}"
    first = build_candidate_execution_receipt(
        scenario=scenario,
        snapshot=_snapshot(scenario),
        reconciliation=_reconciliation(),
        storage_namespace=namespace,
    )
    second = build_candidate_execution_receipt(
        scenario=scenario,
        snapshot=_snapshot(scenario),
        reconciliation=_reconciliation(),
        storage_namespace=namespace,
    )

    assert first == second
    assert first["schema_version"] == CANDIDATE_RECEIPT_SCHEMA
    assert first["status"] == "pass"
    assert first["blockers"] == []
    assert first["scenario_id"] == scenario["scenario_id"]
    assert first["nautilus_version"] == "1.230.0"
    assert first["platform_code_hash"] == platform_parity_code_hash()
    assert first["accounting_snapshot_id"].startswith("accounting-")
    assert candidate_receipt_blockers(first, expected_scenario_id=scenario["scenario_id"]) == []


def test_candidate_receipt_blocks_drift_reserved_namespaces_and_legacy_schema() -> None:
    scenario = _scenario()
    drifted = build_candidate_execution_receipt(
        scenario=scenario,
        snapshot=_snapshot(scenario),
        reconciliation=_reconciliation(status="drift"),
        storage_namespace="nautilus_paper",
    )

    assert drifted["status"] == "blocked"
    assert "execution_reconciliation_not_ok" in drifted["blockers"]
    assert "unsafe_storage_namespace" in drifted["blockers"]
    assert candidate_receipt_blockers({"schema_version": "strategy-shadow-run-v1"}) == [
        "unsupported_candidate_receipt_schema"
    ]


def test_candidate_receipt_blocks_snapshot_or_replay_identity_mismatch() -> None:
    scenario = _scenario()
    snapshot = deepcopy(_snapshot(scenario))
    snapshot["cycle_id"] = "wrong-cycle"
    snapshot["capabilities"]["replay_version"] = "wrong-replay"
    receipt = build_candidate_execution_receipt(
        scenario=scenario,
        snapshot=snapshot,
        reconciliation=_reconciliation(),
        storage_namespace=f"strategy_shadow_{scenario['scenario_id'][-12:]}",
    )

    assert receipt["status"] == "blocked"
    assert "snapshot_cycle_mismatch" in receipt["blockers"]
    assert "replay_version_mismatch" in receipt["blockers"]


def test_candidate_receipt_blocks_unbound_runtime_or_old_platform_code() -> None:
    scenario = _scenario()
    snapshot = deepcopy(_snapshot(scenario))
    snapshot["capabilities"]["nautilus_version"] = ""
    snapshot["capabilities"]["platform_code_hash"] = "sha256:old-code"

    receipt = build_candidate_execution_receipt(
        scenario=scenario,
        snapshot=snapshot,
        reconciliation=_reconciliation(),
        storage_namespace=f"strategy_shadow_{scenario['scenario_id'][-12:]}",
    )

    assert receipt["status"] == "blocked"
    assert "nautilus_runtime_version_missing" in receipt["blockers"]
    assert "snapshot_platform_code_stale" in receipt["blockers"]

    wrong_runtime = deepcopy(_snapshot(scenario))
    wrong_runtime["capabilities"]["nautilus_version"] = "1.229.0"
    receipt = build_candidate_execution_receipt(
        scenario=scenario,
        snapshot=wrong_runtime,
        reconciliation=_reconciliation(),
        storage_namespace=f"strategy_shadow_{scenario['scenario_id'][-12:]}",
    )
    assert "nautilus_runtime_version_mismatch" in receipt["blockers"]
