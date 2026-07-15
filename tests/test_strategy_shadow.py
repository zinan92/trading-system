from pathlib import Path

from services.strategy_shadow import StrategyShadowRunner


def plan() -> dict:
    return {
        "strategy_plan_id": "plan-test",
        "version": 2,
        "direction": "long",
        "range": {"low": 100.0, "high": 120.0},
        "grid": {"count": 2, "notional_per_grid": 100.0},
        "tp_sl": {"tp": 110.0, "sl": 95.0},
    }


def events() -> list[dict]:
    return [
        {"timestamp": "2026-07-05T01:00:00+00:00", "open": 105, "high": 106, "low": 99, "close": 101},
        {"timestamp": "2026-07-05T01:01:00+00:00", "open": 101, "high": 111, "low": 100, "close": 110},
    ]


def test_strategy_shadow_is_reproducible_and_does_not_write_production_ledger(tmp_path: Path) -> None:
    runner = StrategyShadowRunner(tmp_path / "outputs")
    first = runner.run(cycle_id="2026-07-05_DAY", variant_id="human", plan=plan(), market_events=events())
    second = runner.run(cycle_id="2026-07-05_DAY", variant_id="human", plan=plan(), market_events=events())

    assert first["input_hash"] == second["input_hash"]
    assert first["metrics"] == second["metrics"]
    assert first["safety"]["writes_production_ledger"] is False
    assert first["review"]["future_function"] is False
    assert not (tmp_path / "outputs" / "dualtrack" / "ledger").exists()
    assert (tmp_path / "outputs" / "dualtrack" / "strategy_shadows" / "2026-07-05_DAY_human.json").exists()
