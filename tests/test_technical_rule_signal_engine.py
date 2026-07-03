from schemas.asset import Asset
from schemas.market_data import Bar
from services.strategy_registry import StrategyRegistry
from services.technical_rule_signal_engine import TechnicalRuleSignalEngine


GOLD = Asset("GOLD", "Gold", "commodity", "high", "UTC")


def _bars(closes: list[float], timeframe: str = "1m") -> list[Bar]:
    out = []
    for index, close in enumerate(closes):
        prev = closes[index - 1] if index else close
        high = max(close, prev) + 0.5
        low = min(close, prev) - 0.5
        out.append(
            Bar(
                "GOLD",
                timeframe,
                f"2026-05-26T00:{index % 60:02d}:00+00:00",
                prev,
                high,
                low,
                close,
                10,
                "test",
                [],
            )
        )
    return out


def test_registry_selects_technical_rule_engines():
    for engine in (
        "grid",
        "adr_exhaustion_reversion",
        "bollinger_reversion",
        "bollinger_reclaim_filter",
        "breakout",
        "fibonacci",
        "ema50_position",
        "adx_ema_pullback",
        "vwap_extension_reversion",
        "london_ny_compression_breakout",
        "ny_opening_range_breakout",
        "breakout_retest_continuation",
        "false_breakout_reversal",
        "psych_level_rejection",
        "macd_trend_volatility_filter",
    ):
        strategy = StrategyRegistry({f"gold_1m_{engine}": {"engine": engine, "timeframe": "1m", "signal": {"min_bars": 5}}}).get(f"gold_1m_{engine}")
        assert isinstance(strategy.signal_engine(), TechnicalRuleSignalEngine)


def test_grid_engine_generates_reversion_signal():
    engine = TechnicalRuleSignalEngine({"engine": "grid", "signal": {"min_bars": 10, "lookback_bars": 10, "grid_step_pct": 0.05}})
    signal = engine.generate(GOLD, _bars([100] * 9 + [99.7]), run_date="2026-05-26")
    assert signal.direction == "long"
    assert signal.regime == "grid_mean_reversion"
    assert signal.approved_candidate(60, 55)


def test_adr_exhaustion_reversion_requires_range_exhaustion_reclaim_and_session():
    bars = [
        Bar("GOLD", "1m", f"2026-05-26T14:{index % 60:02d}:00+00:00", 100.0, 100.08, 99.92, 100.00 + (0.02 if index % 2 else -0.02), 10, "test", [])
        for index in range(118)
    ]
    bars.append(Bar("GOLD", "1m", "2026-05-26T14:58:00+00:00", 99.40, 99.45, 98.95, 99.00, 10, "test", []))
    bars.append(Bar("GOLD", "1m", "2026-05-26T14:59:00+00:00", 99.00, 99.20, 98.98, 99.15, 10, "test", []))
    engine = TechnicalRuleSignalEngine(
        {
            "engine": "adr_exhaustion_reversion",
            "signal": {
                "min_bars": 120,
                "session_lookback_bars": 120,
                "vwap_lookback_bars": 60,
                "atr_lookback_bars": 14,
                "adx_lookback_bars": 14,
                "min_range_atr_multiple": 1.2,
                "max_adx": 100,
                "extreme_zone_pct": 25,
                "min_vwap_gap_pct": 0.10,
                "min_close_reclaim_pct": 0.01,
                "session_start_utc": 7,
                "session_end_utc": 20,
            },
        }
    )
    signal = engine.generate(GOLD, bars, run_date="2026-05-26")
    assert signal.direction == "long"
    assert signal.regime == "adr_exhaustion_reversion"

    off_session = list(bars)
    off_session[-1] = Bar("GOLD", "1m", "2026-05-26T22:00:00+00:00", bars[-1].open, bars[-1].high, bars[-1].low, bars[-1].close, 10, "test", [])
    assert engine.generate(GOLD, off_session, run_date="2026-05-26").direction == "watch"


def test_bollinger_engine_generates_short_at_upper_band():
    closes = [100, 100.1, 99.9, 100.0, 100.1, 99.9, 100.0, 100.1, 99.9, 102.0]
    engine = TechnicalRuleSignalEngine({"engine": "bollinger_reversion", "signal": {"min_bars": 10, "lookback_bars": 10, "std_mult": 1.2}})
    signal = engine.generate(GOLD, _bars(closes), run_date="2026-05-26")
    assert signal.direction == "short"
    assert signal.regime == "bollinger_reversion"


def test_bollinger_reclaim_filter_requires_reclaim_volatility_and_session():
    closes = [100.0] * 20 + [97.0, 99.0]
    bars = [
        Bar("GOLD", "1m", f"2026-05-26T14:{index % 60:02d}:00+00:00", bar.open, bar.high, bar.low, bar.close, bar.volume, bar.provider, bar.quality_flags)
        for index, bar in enumerate(_bars(closes))
    ]
    engine = TechnicalRuleSignalEngine(
        {
            "engine": "bollinger_reclaim_filter",
            "signal": {
                "min_bars": 22,
                "lookback_bars": 20,
                "std_mult": 1.6,
                "atr_lookback_bars": 14,
                "min_atr_pct": 0.01,
                "max_atr_pct": 2.0,
                "min_bandwidth_pct": 0.05,
                "session_start_utc": 7,
                "session_end_utc": 20,
            },
        }
    )
    signal = engine.generate(GOLD, bars, run_date="2026-05-26")
    assert signal.direction == "long"
    assert signal.regime == "bollinger_reclaim_filter"

    off_session = list(bars)
    off_session[-1] = Bar("GOLD", "1m", "2026-05-26T22:00:00+00:00", bars[-1].open, bars[-1].high, bars[-1].low, bars[-1].close, 10, "test", [])
    assert engine.generate(GOLD, off_session, run_date="2026-05-26").direction == "watch"


def test_breakout_engine_generates_range_breakout():
    closes = [100 + i * 0.01 for i in range(20)] + [103.0]
    engine = TechnicalRuleSignalEngine({"engine": "breakout", "signal": {"min_bars": 21, "lookback_bars": 20, "breakout_buffer_pct": 0.0}})
    signal = engine.generate(GOLD, _bars(closes), run_date="2026-05-26")
    assert signal.direction == "long"
    assert signal.regime == "range_breakout"


def test_london_ny_compression_breakout_requires_session_and_compression():
    bars = [
        Bar("GOLD", "5m", f"2026-05-26T13:{index % 60:02d}:00+00:00", 100.0, 100.05, 99.95, 100.00 + (0.01 if index % 2 else -0.01), 10, "test", [])
        for index in range(80)
    ]
    bars.append(Bar("GOLD", "5m", "2026-05-26T14:00:00+00:00", 100.0, 100.5, 99.95, 100.45, 10, "test", []))
    engine = TechnicalRuleSignalEngine(
        {
            "engine": "london_ny_compression_breakout",
            "signal": {
                "min_bars": 80,
                "compression_lookback_bars": 24,
                "breakout_lookback_bars": 18,
                "atr_lookback_bars": 14,
                "max_compression_range_pct": 0.2,
                "min_atr_pct": 0.01,
                "max_atr_pct": 0.6,
                "breakout_buffer_pct": 0.0,
                "session_start_utc": 13,
                "session_end_utc": 17,
            },
        }
    )
    signal = engine.generate(GOLD, bars, run_date="2026-05-26")
    assert signal.direction == "long"
    assert signal.regime == "london_ny_compression_breakout"

    off_session = list(bars)
    off_session[-1] = Bar("GOLD", "5m", "2026-05-26T22:00:00+00:00", 100.0, 100.5, 99.95, 100.45, 10, "test", [])
    assert engine.generate(GOLD, off_session, run_date="2026-05-26").direction == "watch"


def test_ny_opening_range_breakout_requires_opening_range_and_confirmation():
    bars = [
        Bar("GOLD", "5m", f"2026-05-26T13:{index * 5:02d}:00+00:00", 100.0, 100.08, 99.92, 100.00 + (0.01 if index % 2 else -0.01), 10, "test", [])
        for index in range(12)
    ]
    bars.extend(
        [
            Bar("GOLD", "5m", "2026-05-26T14:00:00+00:00", 100.02, 100.10, 99.96, 100.04, 12, "test", []),
            Bar("GOLD", "5m", "2026-05-26T14:05:00+00:00", 100.04, 100.38, 100.02, 100.34, 24, "test", []),
        ]
    )
    bars = [
        Bar("GOLD", "5m", f"2026-05-26T12:{index * 5:02d}:00+00:00", 99.6 + index * 0.01, 99.68 + index * 0.01, 99.50 + index * 0.01, 99.62 + index * 0.01, 9, "test", [])
        for index in range(12)
    ] + bars
    engine = TechnicalRuleSignalEngine(
        {
            "engine": "ny_opening_range_breakout",
            "signal": {
                "min_bars": 26,
                "opening_range_start_utc": 13,
                "opening_range_end_utc": 14,
                "session_start_utc": 14,
                "session_end_utc": 17,
                "atr_lookback_bars": 14,
                "min_atr_pct": 0.01,
                "max_atr_pct": 1.0,
                "adx_lookback_bars": 6,
                "min_adx": 5,
                "min_opening_range_pct": 0.05,
                "max_opening_range_pct": 0.6,
                "breakout_buffer_pct": 0.0,
                "min_volume_ratio": 0.8,
            },
        }
    )
    signal = engine.generate(GOLD, bars, run_date="2026-05-26")
    assert signal.direction == "long"
    assert signal.regime == "ny_opening_range_breakout"

    before_range_end = list(bars)
    before_range_end[-1] = Bar("GOLD", "5m", "2026-05-26T13:55:00+00:00", 100.04, 100.38, 100.02, 100.34, 24, "test", [])
    assert engine.generate(GOLD, before_range_end, run_date="2026-05-26").direction == "watch"


def test_false_breakout_reversal_requires_probe_reclaim_volatility_and_session():
    bars = [
        Bar("GOLD", "5m", f"2026-05-26T14:{index % 60:02d}:00+00:00", 100.0, 100.08, 99.92, 100.00 + (0.02 if index % 2 else -0.02), 10, "test", [])
        for index in range(94)
    ]
    bars.append(Bar("GOLD", "5m", "2026-05-26T14:34:00+00:00", 100.0, 100.32, 99.95, 100.28, 10, "test", []))
    bars.append(Bar("GOLD", "5m", "2026-05-26T14:39:00+00:00", 100.28, 100.30, 99.98, 100.02, 10, "test", []))
    engine = TechnicalRuleSignalEngine(
        {
            "engine": "false_breakout_reversal",
            "signal": {
                "min_bars": 96,
                "lookback_bars": 36,
                "atr_lookback_bars": 14,
                "min_atr_pct": 0.01,
                "max_atr_pct": 1.0,
                "adx_lookback_bars": 14,
                "max_adx": 60,
                "breakout_buffer_pct": 0.0,
                "reclaim_buffer_pct": 0.0,
                "min_range_pct": 0.05,
                "max_range_pct": 0.6,
                "session_start_utc": 7,
                "session_end_utc": 20,
            },
        }
    )
    signal = engine.generate(GOLD, bars, run_date="2026-05-26")
    assert signal.direction == "short"
    assert signal.regime == "false_breakout_reversal"

    off_session = list(bars)
    off_session[-1] = Bar("GOLD", "5m", "2026-05-26T22:00:00+00:00", bars[-1].open, bars[-1].high, bars[-1].low, bars[-1].close, 10, "test", [])
    assert engine.generate(GOLD, off_session, run_date="2026-05-26").direction == "watch"


def test_breakout_retest_continuation_requires_breakout_retest_strength_and_session():
    bars = [
        Bar("GOLD", "5m", f"2026-05-26T14:{index % 60:02d}:00+00:00", 100.0, 100.09, 99.91, 100.00 + (0.02 if index % 2 else -0.02), 12, "test", [])
        for index in range(94)
    ]
    bars.append(Bar("GOLD", "5m", "2026-05-26T14:34:00+00:00", 100.05, 100.44, 100.02, 100.38, 18, "test", []))
    bars.append(Bar("GOLD", "5m", "2026-05-26T14:39:00+00:00", 100.38, 100.48, 100.08, 100.42, 18, "test", []))
    engine = TechnicalRuleSignalEngine(
        {
            "engine": "breakout_retest_continuation",
            "signal": {
                "min_bars": 96,
                "lookback_bars": 36,
                "atr_lookback_bars": 14,
                "min_atr_pct": 0.01,
                "max_atr_pct": 1.0,
                "adx_lookback_bars": 14,
                "min_adx": 5,
                "breakout_buffer_pct": 0.0,
                "retest_tolerance_pct": 0.15,
                "min_range_pct": 0.05,
                "max_range_pct": 0.8,
                "min_volume_ratio": 0.8,
                "session_start_utc": 7,
                "session_end_utc": 20,
            },
        }
    )
    signal = engine.generate(GOLD, bars, run_date="2026-05-26")
    assert signal.direction == "long"
    assert signal.regime == "breakout_retest_continuation"

    off_session = list(bars)
    off_session[-1] = Bar("GOLD", "5m", "2026-05-26T22:00:00+00:00", bars[-1].open, bars[-1].high, bars[-1].low, bars[-1].close, 18, "test", [])
    assert engine.generate(GOLD, off_session, run_date="2026-05-26").direction == "watch"


def test_psych_level_rejection_requires_sweep_reclaim_volatility_and_session():
    bars = [
        Bar("GOLD", "5m", f"2026-05-26T14:{index % 60:02d}:00+00:00", 100.0, 100.08, 99.92, 100.00 + (0.02 if index % 2 else -0.02), 10, "test", [])
        for index in range(95)
    ]
    bars.append(Bar("GOLD", "5m", "2026-05-26T14:55:00+00:00", 99.92, 100.08, 99.60, 100.04, 10, "test", []))
    engine = TechnicalRuleSignalEngine(
        {
            "engine": "psych_level_rejection",
            "signal": {
                "min_bars": 96,
                "level_step": 25,
                "atr_lookback_bars": 14,
                "min_atr_pct": 0.01,
                "max_atr_pct": 1.0,
                "adx_lookback_bars": 14,
                "max_adx": 60,
                "sweep_tolerance_pct": 0.0,
                "reclaim_buffer_pct": 0.0,
                "min_rejection_wick_pct": 0.02,
                "max_close_distance_pct": 0.20,
                "session_start_utc": 7,
                "session_end_utc": 20,
            },
        }
    )
    signal = engine.generate(GOLD, bars, run_date="2026-05-26")
    assert signal.direction == "long"
    assert signal.regime == "psych_level_rejection"

    off_session = list(bars)
    off_session[-1] = Bar("GOLD", "5m", "2026-05-26T22:00:00+00:00", bars[-1].open, bars[-1].high, bars[-1].low, bars[-1].close, 10, "test", [])
    assert engine.generate(GOLD, off_session, run_date="2026-05-26").direction == "watch"


def test_macd_trend_volatility_filter_requires_trend_volatility_and_session():
    closes = [100 + i * 0.04 for i in range(60)] + [102.4 - i * 0.08 for i in range(20)] + [100.8 + i * 0.15 for i in range(10)]
    bars = [
        Bar("GOLD", "1m", f"2026-05-26T14:{index % 60:02d}:00+00:00", bar.open, bar.high, bar.low, bar.close, bar.volume, bar.provider, bar.quality_flags)
        for index, bar in enumerate(_bars(closes))
    ]
    engine = TechnicalRuleSignalEngine(
        {
            "engine": "macd_trend_volatility_filter",
            "signal": {
                "min_bars": 90,
                "fresh_bars": 8,
                "ema_period": 50,
                "ema_slope_lookback_bars": 10,
                "atr_lookback_bars": 14,
                "min_atr_pct": 0.01,
                "max_atr_pct": 2.0,
                "session_start_utc": 7,
                "session_end_utc": 20,
            },
        }
    )
    signal = engine.generate(GOLD, bars, run_date="2026-05-26")
    assert signal.direction == "long"
    assert signal.regime == "macd_trend_volatility_filter"

    off_session = list(bars)
    off_session[-1] = Bar("GOLD", "1m", "2026-05-26T22:00:00+00:00", bars[-1].open, bars[-1].high, bars[-1].low, bars[-1].close, 10, "test", [])
    assert engine.generate(GOLD, off_session, run_date="2026-05-26").direction == "watch"


def test_fibonacci_engine_generates_level_signal():
    closes = [100 + i for i in range(20)] + [111.8, 112.0]
    engine = TechnicalRuleSignalEngine({"engine": "fibonacci", "signal": {"min_bars": 22, "lookback_bars": 22, "tolerance_pct": 0.5}})
    signal = engine.generate(GOLD, _bars(closes), run_date="2026-05-26")
    assert signal.direction == "long"
    assert signal.regime == "fibonacci_pullback"


def test_ema50_engine_generates_position_signal():
    closes = [100.0] * 70 + [100.02]
    engine = TechnicalRuleSignalEngine({"engine": "ema50_position", "signal": {"min_bars": 71, "ema_period": 50, "tolerance_pct": 0.2}})
    signal = engine.generate(GOLD, _bars(closes), run_date="2026-05-26")
    assert signal.direction == "long"
    assert signal.regime == "ema50_bounce"


def test_adx_ema_pullback_requires_trend_strength_pullback_and_session():
    closes = [100 + index * 0.08 for index in range(90)] + [107.2, 107.05, 106.95, 107.35]
    bars = [
        Bar("GOLD", "5m", f"2026-05-26T14:{index % 60:02d}:00+00:00", bar.open, bar.high, bar.low, bar.close, bar.volume, bar.provider, bar.quality_flags)
        for index, bar in enumerate(_bars(closes, timeframe="5m"))
    ]
    engine = TechnicalRuleSignalEngine(
        {
            "engine": "adx_ema_pullback",
            "signal": {
                "min_bars": 94,
                "ema_fast_period": 20,
                "ema_slow_period": 50,
                "ema_slope_lookback_bars": 8,
                "adx_lookback_bars": 14,
                "min_adx": 18,
                "max_pullback_pct": 0.5,
                "atr_lookback_bars": 14,
                "min_atr_pct": 0.01,
                "max_atr_pct": 1.2,
                "session_start_utc": 7,
                "session_end_utc": 20,
            },
        }
    )
    signal = engine.generate(GOLD, bars, run_date="2026-05-26")
    assert signal.direction == "long"
    assert signal.regime == "adx_ema_pullback"

    off_session = list(bars)
    off_session[-1] = Bar("GOLD", "5m", "2026-05-26T22:00:00+00:00", bars[-1].open, bars[-1].high, bars[-1].low, bars[-1].close, 10, "test", [])
    assert engine.generate(GOLD, off_session, run_date="2026-05-26").direction == "watch"


def test_vwap_extension_reversion_requires_extension_volume_and_session():
    closes = [100.0] * 92 + [99.70, 99.74, 99.78, 99.82]
    bars = [
        Bar("GOLD", "5m", f"2026-05-26T14:{index % 60:02d}:00+00:00", bar.open, bar.high, bar.low, bar.close, 12, bar.provider, bar.quality_flags)
        for index, bar in enumerate(_bars(closes, timeframe="5m"))
    ]
    engine = TechnicalRuleSignalEngine(
        {
            "engine": "vwap_extension_reversion",
            "signal": {
                "min_bars": 96,
                "vwap_lookback_bars": 48,
                "atr_lookback_bars": 14,
                "min_atr_pct": 0.01,
                "max_atr_pct": 1.2,
                "adx_lookback_bars": 14,
                "max_adx": 60,
                "min_extension_pct": 0.12,
                "min_remaining_to_vwap_pct": 0.03,
                "min_volume_ratio": 0.8,
                "session_start_utc": 7,
                "session_end_utc": 20,
            },
        }
    )
    signal = engine.generate(GOLD, bars, run_date="2026-05-26")
    assert signal.direction == "long"
    assert signal.regime == "vwap_extension_reversion"

    off_session = list(bars)
    off_session[-1] = Bar("GOLD", "5m", "2026-05-26T22:00:00+00:00", bars[-1].open, bars[-1].high, bars[-1].low, bars[-1].close, 12, "test", [])
    assert engine.generate(GOLD, off_session, run_date="2026-05-26").direction == "watch"
