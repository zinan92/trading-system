from __future__ import annotations

from pathlib import Path

import pytest

from pipelines.strategy_shadow_replay import run_strategy_shadow_replay
from services.backtest_plugin_registry import BacktestPluginRegistry, UnknownBacktestPlugin
from services.backtest_port import STRATEGY_SHADOW_KIND
from services.dualtrack_nautilus_parity_contract import platform_parity_code_hash
from services.execution_conformance import build_candidate_execution_receipt
from services.journal_store import load_json, write_json
from services.strategy_shadow import StrategyShadowRunner, load_strategy_shadow_runs


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
        "strategy_plan_id": "plan-strategy-shadow",
        "cycle_id": CYCLE_ID,
        "version": 2,
        "locked_at": "2026-07-18T01:00:00+00:00",
        "status": "active",
        "direction": "long",
        "range": {"low": 99.0, "high": 101.0},
        "execution_context": {
            "market": {
                "price": 101.0,
                "symbol": "GOLD",
                "provider": "binance_usdm_futures",
            },
        },
        "grid": {
            "count": 1,
            "notional_per_grid": 100.0,
            "orders": [{
                "preview_order_id": "preview-01-buy",
                "side": "buy",
                "price": 100.0,
                "quantity": 1.0,
                "notional": 100.0,
                "sl": 98.0,
                "tp": 101.0,
            }],
        },
    }


def _events() -> list[dict]:
    return [{
        "cycle_id": CYCLE_ID,
        "event_id": "shadow-event-1",
        "ts_event": "2026-07-18T01:01:00+00:00",
        "event_started_at": "2026-07-18T01:00:00+00:00",
        "price": 100.5,
        "open": 101.0,
        "high": 101.2,
        "low": 99.9,
        "fresh": True,
        "is_synthetic": False,
        "source": "market_db:binance_usdm_futures",
        "provider": "binance_usdm_futures",
        "instrument_id": "XAUUSDT",
    }]


def _snapshot(scenario: dict) -> dict:
    identity = scenario["plan_identity"]
    command = scenario["commands"][0]
    return {
        "schema_version": "dualtrack-execution-v1",
        "engine": "nautilus_paper",
        "cycle_id": CYCLE_ID,
        "orders": [{
            "order_id": "shadow-order-1",
            "state": "filled",
            "side": "buy",
            "event": "entry",
            "order_type": "limit",
            "price": 100.0,
            "quantity": 1.0,
            "ts": "2026-07-18T01:01:00+00:00",
            "strategy_plan_id": identity["strategy_plan_id"],
            "strategy_plan_version": identity["strategy_plan_version"],
        }],
        "fills": [{
            "fill_id": "shadow-fill-1",
            "order_id": "shadow-order-1",
            "trade_id": "shadow-trade-1",
            "event": "entry",
            "side": "buy",
            "price": 100.0,
            "quantity": 1.0,
            "cost": 0.02,
            "gross_pnl": 0.0,
            "realized_pnl": -0.02,
            "slippage": 0.0,
            "ts": "2026-07-18T01:01:00+00:00",
            "strategy_plan_id": identity["strategy_plan_id"],
            "strategy_plan_version": identity["strategy_plan_version"],
        }],
        "positions": [{
            "position_id": "shadow-position-1",
            "trade_id": "shadow-trade-1",
            "status": "open",
            "side": "long",
            "quantity": 1.0,
            "remaining_units": 1.0,
            "entry_price": 100.0,
            "entry_ts": "2026-07-18T01:01:00+00:00",
            "realized_pnl": -0.02,
            "unrealized_pnl": 0.5,
            "strategy_plan_id": identity["strategy_plan_id"],
            "strategy_plan_version": identity["strategy_plan_version"],
        }],
        "account": {
            "starting_cash": 10_000.0,
            "realized_pnl": -0.02,
            "ending_cash": 9_999.98,
            "equity": 10_000.48,
            "margin": 10.0,
            "exposure": 100.0,
            "slippage": 0.0,
            "fees": 0.02,
            "funding": 0.0,
        },
        "pnl": {"realized": -0.02, "unrealized": 0.5},
        "mark": {"price": 100.5, "fresh": True, "source": "test"},
        "capabilities": {
            "replay_version": "dualtrack-nautilus-replay-v6",
            "nautilus_version": "1.230.0",
            "platform_code_hash": platform_parity_code_hash(),
        },
        "command_echo": command,
    }


class FakeReplayPort:
    def replay(self, scenario: dict) -> dict:
        snapshot = _snapshot(scenario)
        reconciliation = {
            "schema_version": "dualtrack-execution-reconciliation-v1",
            "engine": "nautilus_paper",
            "cycle_id": CYCLE_ID,
            "status": "ok",
            "issues": [],
        }
        namespace = f"strategy_shadow_{scenario['scenario_id'][-12:]}"
        return {
            "snapshot": snapshot,
            "reconciliation": reconciliation,
            "receipt": build_candidate_execution_receipt(
                scenario=scenario,
                snapshot=snapshot,
                reconciliation=reconciliation,
                storage_namespace=namespace,
            ),
            "storage_namespace": namespace,
        }


def test_strategy_shadow_is_a_replay_read_model_and_append_idempotent(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    runner = StrategyShadowRunner(output, replay_port=FakeReplayPort(), config=_config())
    first = runner.run(cycle_id=CYCLE_ID, variant_id="candidate", plan=_plan(), market_events=_events())
    second = runner.run(cycle_id=CYCLE_ID, variant_id="candidate", plan=_plan(), market_events=_events())

    assert first == second
    assert first["schema_version"] == "strategy-shadow-run-v2"
    assert first["status"] == "pass"
    assert first["metrics"]["cost"] == 0.02
    assert first["metrics"]["net_pnl"] == 0.48
    assert first["metrics"]["trade_count"] == 0
    assert first["accounting_snapshot"]["counts"]["trade_count"] == 1
    assert (
        first["accounting_snapshot"]["snapshot_id"]
        == first["execution_receipt"]["accounting_snapshot_id"]
    )
    assert first["review"]["future_function"] is False
    assert first["safety"]["writes_production_ledger"] is False
    rows = load_json(output / "dualtrack" / "strategy_shadows" / f"{CYCLE_ID}_candidate.json")
    assert rows == [first]
    receipt_rows = load_json(
        output
        / "dualtrack"
        / "strategy_shadows"
        / "receipts"
        / f"{first['scenario_id']}.json"
    )
    assert receipt_rows == [first["execution_receipt"]]
    for relative in (
        "dualtrack/ledger",
        "dualtrack/reconciliation",
        "dualtrack/nautilus/parity",
        "dualtrack/cutover",
    ):
        assert not (output / relative).exists()


def test_strategy_shadow_preserves_v1_trace_but_never_reuses_it_as_v2(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    path = output / "dualtrack" / "strategy_shadows" / f"{CYCLE_ID}_candidate.json"
    legacy = {"schema_version": "strategy-shadow-run-v1", "input_hash": "legacy"}
    write_json(path, [legacy])

    current = StrategyShadowRunner(
        output,
        replay_port=FakeReplayPort(),
        config=_config(),
    ).run(cycle_id=CYCLE_ID, variant_id="candidate", plan=_plan(), market_events=_events())

    rows = load_json(path)
    assert rows[0] == legacy
    assert rows[-1] == current
    assert rows[-1]["schema_version"] == "strategy-shadow-run-v2"


def test_strategy_shadow_rejects_market_bars_that_started_before_plan_lock(tmp_path: Path) -> None:
    leaked = _events()
    leaked[0]["event_started_at"] = "2026-07-18T00:59:59+00:00"
    runner = StrategyShadowRunner(
        tmp_path / "outputs",
        replay_port=FakeReplayPort(),
        config=_config(),
    )

    with pytest.raises(ValueError, match="predates plan availability"):
        runner.run(cycle_id=CYCLE_ID, variant_id="candidate", plan=_plan(), market_events=leaked)


def test_dashboard_shadow_loader_reads_last_v1_or_v2_row_per_variant(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    folder = output / "dualtrack" / "strategy_shadows"
    write_json(folder / f"{CYCLE_ID}_a.json", [
        {"schema_version": "strategy-shadow-run-v1", "variant_id": "a"},
        {"schema_version": "strategy-shadow-run-v2", "variant_id": "a"},
    ])
    write_json(folder / f"{CYCLE_ID}_b.json", [
        {"schema_version": "strategy-shadow-run-v1", "variant_id": "b"},
    ])

    assert load_strategy_shadow_runs(output, CYCLE_ID) == [
        {"schema_version": "strategy-shadow-run-v2", "variant_id": "a"},
        {"schema_version": "strategy-shadow-run-v1", "variant_id": "b"},
    ]


def test_strategy_shadow_pipeline_uses_custom_replay_plugin_and_persists_audit(
    tmp_path: Path,
) -> None:
    registry = BacktestPluginRegistry()
    registry.register(
        "fake_shadow",
        lambda _context: FakeReplayPort(),
        kind=STRATEGY_SHADOW_KIND,
        evidence_tier="execution_replay_test",
        promotion_evidence_capable=True,
    )
    config = {
        **_config(),
        "backtest_plugins": {"strategy_shadow": "fake_shadow"},
    }

    result = run_strategy_shadow_replay(
        output_root=tmp_path / "outputs",
        cycle_id=CYCLE_ID,
        variant_id="custom",
        plan=_plan(),
        market_events=_events(),
        nautilus_python=tmp_path / "unused-python",
        preflight_path=tmp_path / "unused-preflight.json",
        config=config,
        backtest_plugin_registry=registry,
    )

    assert result["status"] == "pass"
    assert result["backtest_plugin"]["plugin"]["name"] == "fake_shadow"
    assert result["backtest_plugin"]["plugin"]["kind"] == STRATEGY_SHADOW_KIND
    assert result["backtest_plugin"]["registry_fingerprint"] == registry.fingerprint
    rows = load_json(
        tmp_path
        / "outputs"
        / "dualtrack"
        / "strategy_shadows"
        / f"{CYCLE_ID}_custom.json"
    )
    assert rows[-1]["backtest_plugin"] == result["backtest_plugin"]


def test_unknown_strategy_shadow_plugin_fails_before_artifact_creation(
    tmp_path: Path,
) -> None:
    output = tmp_path / "outputs"
    config = {
        **_config(),
        "backtest_plugins": {"strategy_shadow": "typo"},
    }

    with pytest.raises(UnknownBacktestPlugin, match="typo"):
        run_strategy_shadow_replay(
            output_root=output,
            cycle_id=CYCLE_ID,
            variant_id="custom",
            plan=_plan(),
            market_events=_events(),
            nautilus_python=tmp_path / "unused-python",
            preflight_path=tmp_path / "unused-preflight.json",
            config=config,
        )

    assert not output.exists()
