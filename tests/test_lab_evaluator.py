from __future__ import annotations

from datetime import datetime, timedelta, timezone

from schemas.market_data import Bar
from services.lab_evaluator import (
    LabSimulationConfig,
    evaluate_signals,
    random_direction_signals,
    regenerate_macd_signals,
    validate_1m_bars,
)


def _bars(closes: list[float]) -> list[Bar]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    for index, close in enumerate(closes):
        ts = (start + timedelta(minutes=index)).isoformat()
        rows.append(Bar("GOLD", "1m", ts, close, close + 0.05, close - 0.05, close, 100, "test", []))
    return rows


def _flat_bars(count: int = 900) -> list[Bar]:
    return _bars([100.0] * count)


def test_random_direction_zero_cost_mean_pnl_is_near_zero():
    bars = _flat_bars()
    signals = random_direction_signals(bars, every_n=7, seed=42)
    result = evaluate_signals(bars, signals, LabSimulationConfig(cost_bp_per_side=0, max_hold_bars=3))

    assert result["status"] == "valid"
    assert abs(result["metrics"]["expectancy_per_trade"]) < 0.000001


def test_random_direction_with_costs_matches_expected_drag():
    bars = _flat_bars()
    signals = random_direction_signals(bars, every_n=7, seed=42)
    result = evaluate_signals(bars, signals, LabSimulationConfig(cost_bp_per_side=2, max_hold_bars=3, fixed_notional=1000))

    assert result["status"] == "valid"
    assert abs(result["metrics"]["expectancy_per_trade"] - -0.4) < 0.000001


def test_planted_trend_strategy_captures_target_without_lookahead():
    bars = _bars([100.0 + index * 0.03 for index in range(240)])
    signal = [{"index": 10, "direction": "long", "timestamp": bars[10].timestamp, "close": bars[10].close}]
    result = evaluate_signals(bars, signal, LabSimulationConfig(stop_pct=0.002, target_pct=0.004, max_hold_bars=30))

    trade = result["trades"][0]
    assert result["status"] == "valid"
    assert trade["entry_index"] == 11
    assert trade["entry_price"] == bars[11].open
    assert trade["exit_reason"] == "target"
    assert trade["net_pnl"] > 0


def test_determinism_identical_config_returns_identical_result():
    bars = _bars([100 + index * 0.02 for index in range(120)] + [102.4 - index * 0.02 for index in range(120)])
    signals = regenerate_macd_signals(bars)
    cfg = LabSimulationConfig(cost_bp_per_side=1)

    assert evaluate_signals(bars, signals, cfg) == evaluate_signals(bars, signals, cfg)


def test_missing_bar_fails_closed():
    bars = _flat_bars(20)
    broken = bars[:10] + bars[11:]
    signals = [{"index": 5, "direction": "long"}]

    assert validate_1m_bars(broken)["valid"] is False
    assert evaluate_signals(broken, signals)["status"] == "invalid"
    assert evaluate_signals(broken, signals)["reason"] == "missing_bars"


def test_zero_trade_window_is_invalid():
    bars = _flat_bars(20)

    result = evaluate_signals(bars, [])

    assert result["status"] == "invalid"
    assert result["reason"] == "zero_trades"
