from __future__ import annotations

from pathlib import Path

from services.execution_conformance import build_execution_scenario
from services.journal_store import load_json, write_json
from services.strategy_shadow_nautilus import NautilusStrategyShadowReplay


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


def _scenario() -> dict:
    plan = {
        "schema_version": "strategy-plan-v1",
        "strategy_plan_id": "plan-nautilus-shadow",
        "cycle_id": CYCLE_ID,
        "version": 1,
        "locked_at": "2026-07-18T01:00:00+00:00",
    }
    commands = [{
        "cycle_id": CYCLE_ID,
        "ts": "2026-07-18T01:00:00+00:00",
        "side": "buy",
        "event": "entry",
        "order_type": "limit",
        "price": 100.0,
        "market_price": 101.0,
        "quantity": 1.0,
        "notional": 100.0,
        "sl": 98.0,
        "tp": 101.0,
        "source_fill_id": "strategy-grid:plan-nautilus-shadow:preview-01-buy",
        "strategy_plan_id": "plan-nautilus-shadow",
        "strategy_plan_version": 1,
    }]
    events = [{
        "cycle_id": CYCLE_ID,
        "event_id": "nautilus-shadow-event-1",
        "ts_event": "2026-07-18T01:01:00+00:00",
        "event_started_at": "2026-07-18T01:00:00+00:00",
        "price": 101.0,
        "open": 101.0,
        "high": 101.1,
        "low": 100.9,
        "fresh": True,
        "is_synthetic": False,
        "source": "market_db:binance_usdm_futures",
        "provider": "binance_usdm_futures",
        "instrument_id": "XAUUSDT",
    }]
    return build_execution_scenario(
        candidate_id="candidate-nautilus-shadow",
        plan=plan,
        commands=commands,
        market_events=events,
        config=_config(),
    )


def _preflight(path: Path) -> None:
    write_json(path, [{
        "status": "ready_for_paper_shadow",
        "fee_model": {
            "mode": "account_observed",
            "maker_fee_rate": "0.0002",
            "taker_fee_rate": "0.0004",
            "funding_rate": "0.0001",
            "funding_time": 1,
            "observed_at": "2026-07-18T00:00:00+00:00",
            "real_money_eligible": False,
        },
    }])


def _fake_replay(_preflight_path: Path, input_path: Path, _output_path: Path) -> dict:
    bundle = load_json(input_path)[-1]
    command_row = bundle["commands"][0]
    command = command_row["command"]
    return {
        "schema_version": "dualtrack-execution-v1",
        "engine": "nautilus_paper",
        "cycle_id": CYCLE_ID,
        "orders": [{
            "order_id": command_row["command_id"],
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
        "capabilities": {"native_order_lifecycle": True},
    }


def test_nautilus_shadow_replay_is_content_namespaced_and_restart_idempotent(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    preflight = output / "dualtrack" / "nautilus" / "instrument_preflight.json"
    _preflight(preflight)
    scenario = _scenario()
    port = NautilusStrategyShadowReplay(
        output,
        nautilus_python=tmp_path / "unused-python",
        preflight_path=preflight,
        replay_executor=_fake_replay,
        config=_config(),
    )

    first = port.replay(scenario)
    second = port.replay(scenario)

    assert first == second
    assert first["receipt"]["status"] == "pass"
    assert first["storage_namespace"].startswith("strategy_shadow_")
    assert first["snapshot"]["engine"] == "nautilus_paper"
    namespace_root = output / "dualtrack" / first["storage_namespace"]
    assert (namespace_root / "snapshots" / f"{CYCLE_ID}.json").exists()
    assert len(load_json(namespace_root / "commands" / f"{CYCLE_ID}.json")) == 1
    assert len(load_json(namespace_root / "events" / f"{CYCLE_ID}.json")) == 1
    for relative in (
        "dualtrack/reconciliation",
        "dualtrack/nautilus/parity",
        "dualtrack/cutover",
        "dualtrack/orders",
        "dualtrack/fills",
        "dualtrack/positions",
    ):
        assert not (output / relative).exists()
