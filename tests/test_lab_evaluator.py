from __future__ import annotations

from datetime import datetime, timedelta, timezone

from schemas.market_data import Bar
from services.lab_evaluator import (
    LabSimulationConfig,
    evaluate_signals,
    random_direction_signals,
    regenerate_macd_signals,
    regenerate_strategy_signals,
    resample_bars,
    validate_1m_bars,
)
from services.strategy_registry import Strategy


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


def test_resample_bars_uses_utc_epoch_floor_bucket_convention():
    bars = _bars([100, 101, 99, 102, 103, 98, 104])
    shifted = [
        Bar(bar.symbol, bar.timeframe, (datetime.fromisoformat(bar.timestamp) + timedelta(minutes=1)).isoformat(), bar.open, bar.high, bar.low, bar.close, bar.volume, bar.provider, bar.quality_flags)
        for bar in bars
    ]

    five_min = resample_bars(shifted, "5m")

    assert [bar.timestamp for bar in five_min] == ["2026-01-01T00:00:00+00:00", "2026-01-01T00:05:00+00:00"]
    assert five_min[0].open == 100
    assert five_min[0].close == 102
    assert five_min[0].high == 102.05
    assert five_min[0].low == 98.95
    assert five_min[0].timeframe == "5m"
    assert "derived_timeframe" in five_min[0].quality_flags


def test_non_macd_strategy_replay_is_deterministic():
    bars = _bars([100 + ((index % 12) - 6) * 0.08 for index in range(160)])
    strategy = Strategy(
        strategy_id="test_grid",
        symbol="GOLD",
        timeframe="1m",
        params={"engine": "grid", "signal": {"min_bars": 20, "lookback_bars": 12, "grid_step_pct": 0.02}},
    )

    first = regenerate_strategy_signals(strategy, bars)
    second = regenerate_strategy_signals(strategy, bars)

    assert first == second
    assert first["status"] == "valid"
    assert first["engine_chain"] == ["TechnicalRuleSignalEngine"]
    assert first["signals"]


def test_missing_historical_replay_interface_is_not_replayable():
    strategy = Strategy(strategy_id="legacy_ma", symbol="GOLD", timeframe="5m", params={"signal": {}})

    replay = regenerate_strategy_signals(strategy, _flat_bars(40))

    assert replay["status"] == "not_replayable"
    assert replay["reason"] == "missing_historical_signals"
