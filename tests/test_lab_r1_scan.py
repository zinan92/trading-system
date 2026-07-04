from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from schemas.market_data import Bar
from services.lab_r1_scan import R1ScanConfig, render_r1_report, run_r1_scan
from services.lab_registry import LabRegistry
from services.lab_walkforward import WalkForwardConfig
from services.strategy_registry import Strategy


def test_r1_scan_registers_every_strategy_without_holdout_consumption(tmp_path: Path):
    root = tmp_path / "outputs"
    registry = LabRegistry(root)
    bars = _bars(days=32)
    strategies = [
        Strategy("test_macd", "GOLD", {"engine": "macd", "signal": {"min_bars": 40}}, timeframe="1m"),
        Strategy("legacy_ma", "GOLD", {"signal": {}}, timeframe="5m"),
    ]

    result = run_r1_scan(root, registry, bars, strategies=strategies, config=_fast_config())

    assert result["total_trials"] == 2
    assert {row["strategy"] for row in result["rows"]} == {"test_macd", "legacy_ma"}
    assert registry.load("R1_test_macd")["holdout_consumed"] is False
    assert registry.load("R1_legacy_ma")["holdout_consumed"] is False
    assert registry.load("R1_legacy_ma")["status"] == "invalid"
    assert registry.load("R1_legacy_ma")["objective"]["r1_triage"]["status"] == "not_replayable"
    assert (root / "lab" / "reports" / "R1_strategy_scan.md").exists()


def test_r1_scan_is_deterministic_modulo_registry_timestamps(tmp_path: Path):
    root = tmp_path / "outputs"
    registry = LabRegistry(root)
    bars = _bars(days=32)
    strategies = [Strategy("test_macd", "GOLD", {"engine": "macd", "signal": {"min_bars": 40}}, timeframe="1m")]

    run_r1_scan(root, registry, bars, strategies=strategies, config=_fast_config())
    first_entry = _strip_timestamps(registry.load("R1_test_macd"))
    first_report = (root / "lab" / "reports" / "R1_strategy_scan.md").read_text(encoding="utf-8")
    run_r1_scan(root, registry, bars, strategies=strategies, config=_fast_config())
    second_entry = _strip_timestamps(registry.load("R1_test_macd"))
    second_report = (root / "lab" / "reports" / "R1_strategy_scan.md").read_text(encoding="utf-8")

    assert first_entry == second_entry
    assert first_report == second_report


def test_r1_report_carries_multiple_testing_caveat_and_verdict():
    report = render_r1_report(
        [
            {
                "strategy": "alpha",
                "timeframe": "1m",
                "oos_trades": 101,
                "win_rate": 0.51,
                "gross_bp_per_trip": 2.5,
                "break_even_bp": 1.2,
                "maker_2bp_expectancy_per_trade": 0.1,
                "taker_5bp_expectancy_per_trade": -0.4,
                "max_drawdown_pct": 1.5,
                "sortino_at_2bp": 0.2,
                "status": "valid",
                "reason": "pass",
            }
        ],
        total_trials=21,
        min_oos_trades=100,
    )

    assert "Total R1 trials: 21" in report
    assert "Multiple-testing caveat" in report
    assert "alpha: gross 2.5 bp/trip over 101 trades" in report


def _fast_config() -> R1ScanConfig:
    return R1ScanConfig(walkforward=WalkForwardConfig(train_days=1, validate_days=1, step_days=1, holdout_days=1), min_oos_trades=1)


def _bars(days: int) -> list[Bar]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    out: list[Bar] = []
    for index in range(days * 24 * 60):
        wave = ((index % 180) - 90) * 0.01
        price = 100.0 + wave
        out.append(Bar("GOLD", "1m", (start + timedelta(minutes=index)).isoformat(), price, price + 0.05, price - 0.05, price, 100, "test", []))
    return out


def _strip_timestamps(value: dict) -> dict:
    if isinstance(value, dict):
        return {key: _strip_timestamps(val) for key, val in value.items() if key not in {"created_at", "updated_at"}}
    if isinstance(value, list):
        return [_strip_timestamps(item) for item in value]
    return value
