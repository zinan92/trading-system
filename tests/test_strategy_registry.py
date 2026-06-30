from schemas.asset import Asset
from schemas.market_data import Bar, MarketEvent
from services.signal_engine import SignalEngine
from services.strategy_registry import Strategy, StrategyRegistry


def _classification() -> dict:
    return {
        "family": "test",
        "style": "test_style",
        "directionality": "long_short",
        "frequency_bucket": "medium",
        "holding_period": "intraday",
        "expected_trades_per_day_min": 1,
        "expected_trades_per_day_max": 3,
        "return_profile": "test_profile",
        "risk_profile": "medium",
    }


def _bars(symbol: str, start: float, drift: float) -> list[Bar]:
    price = start
    rows = []
    for index in range(1, 31):
        close = round(price * drift, 2)
        rows.append(Bar(symbol, "5m", f"mock-5m-{index:02d}", price, close * 1.01, close * 0.99, close, 1000 + index, "mock"))
        price = close
    return rows


_CONFIG = {
    "gold_5m_v1": {
        "symbol": "GOLD",
        "signal": {"ma_short_bars": 5, "ma_long_bars": 20, "long_strength_min": 60, "long_confidence_min": 55},
    },
    "gold_5m_aggressive": {
        "symbol": "GOLD",
        "enabled": False,
        "signal": {"ma_short_bars": 3, "ma_long_bars": 10, "long_strength_min": 50, "long_confidence_min": 50},
    },
}


def test_registry_lists_and_filters_strategies():
    reg = StrategyRegistry(_CONFIG)
    assert [s.strategy_id for s in reg.strategies()] == ["gold_5m_v1", "gold_5m_aggressive"]
    assert [s.strategy_id for s in reg.enabled()] == ["gold_5m_v1"]
    assert reg.get("gold_5m_aggressive").enabled is False
    assert reg.get("gold_5m_v1").symbol == "GOLD"


def test_strategy_defaults_symbol_and_enabled():
    reg = StrategyRegistry({"x": {"signal": {}}})
    strat = reg.get("x")
    assert strat.symbol == "GOLD"
    assert strat.enabled is True


def test_strategy_engine_matches_legacy_signal_engine():
    """DoD: gold_5m_v1 routed through the registry produces the identical
    signal as the legacy SignalEngine(full_config) path."""
    gold = Asset("GOLD", "Gold", "commodity", "high", "UTC")
    factors = {
        "DXY": {"bars": _bars("DXY", 104, 0.998), "events": []},
        "US10Y_REAL": {"bars": _bars("US10Y_REAL", 2.0, 0.995), "events": []},
        "GLD_FLOW": {"bars": _bars("GLD_FLOW", 1.0, 1.002), "events": []},
    }
    events = [MarketEvent("e1", ["GOLD"], "defensive demand", "mock", "now", "positive", 70)]
    candles = _bars("GOLD", 2350, 1.006)

    legacy = SignalEngine(_CONFIG).generate(gold, candles, events, "2026-05-25", factor_context=factors)
    via_reg = StrategyRegistry(_CONFIG).get("gold_5m_v1").signal_engine().generate(
        gold, candles, events, "2026-05-25", factor_context=factors
    )

    for field in ("direction", "strength", "confidence", "regime", "status", "signal_id"):
        assert getattr(via_reg, field) == getattr(legacy, field)
    assert via_reg.factor_scores == legacy.factor_scores


def test_strategy_live_flag_defaults_false_and_overrides():
    reg = StrategyRegistry(
        {
            "paper_one": {"symbol": "GOLD", "signal": {}},
            "live_one": {"symbol": "GOLD", "live": True, "signal": {}},
        }
    )
    assert reg.get("paper_one").live is False  # default: paper
    assert reg.get("live_one").live is True


def test_strategy_timeframe_field_defaults_and_overrides():
    reg = StrategyRegistry(
        {
            "gold_5m_v1": {"symbol": "GOLD", "signal": {}},
            "gold_1m_chan": {"symbol": "GOLD", "engine": "chan", "timeframe": "1m", "signal": {}},
        }
    )
    assert reg.get("gold_5m_v1").timeframe == "5m"  # default preserves legacy behaviour
    assert reg.get("gold_1m_chan").timeframe == "1m"


def test_engine_type_selects_chan_vs_ma():
    from services.signal_engine import SignalEngine
    ma = StrategyRegistry({"gold_5m_v1": {"signal": {}}}).get("gold_5m_v1").signal_engine()
    assert isinstance(ma, SignalEngine)
    chan = StrategyRegistry({"gold_1m_chan": {"engine": "chan", "signal": {}}}).get("gold_1m_chan").signal_engine()
    assert type(chan).__name__ == "ChanSignalEngine"


def test_engine_type_selects_macd():
    from services.macd_signal_engine import MacdSignalEngine
    macd = StrategyRegistry({"gold_1m_macd": {"engine": "macd", "signal": {}}}).get("gold_1m_macd").signal_engine()
    assert isinstance(macd, MacdSignalEngine)


def test_signal_filters_wrap_the_base_engine():
    from services.macd_signal_engine import MacdSignalEngine
    from services.signal_filters import FilteredSignalEngine, MacdFilter
    strat = StrategyRegistry(
        {"gold_1m_macd_filtered": {"engine": "macd", "signal": {"filters": [{"type": "macd", "mode": "zero_line"}]}}}
    ).get("gold_1m_macd_filtered")
    engine = strat.signal_engine()
    assert isinstance(engine, FilteredSignalEngine)
    assert isinstance(engine.base, MacdSignalEngine)
    assert isinstance(engine.filters[0], MacdFilter)


def test_second_variant_registers_via_config_only():
    reg = StrategyRegistry(_CONFIG)
    aggressive = reg.get("gold_5m_aggressive")
    # Registered purely from config, with its OWN params (not v1's).
    assert aggressive.params["signal"]["ma_short_bars"] == 3
    assert aggressive.signal_engine().signal_config["ma_short_bars"] == 3
    assert reg.get("gold_5m_v1").signal_engine().signal_config["ma_short_bars"] == 5


def test_strategy_classification_audit_requires_pm_fields():
    reg = StrategyRegistry(
        {
            "good": {"classification": _classification(), "signal": {}},
            "bad": {"classification": {"family": "chan"}, "signal": {}},
        }
    )

    audit = reg.classification_audit()

    assert audit["status"] == "fail"
    bad = next(row for row in audit["strategies"] if row["strategy_id"] == "bad")
    assert "expected_trades_per_day_min" in bad["missing_fields"]
    assert "risk_profile" in bad["missing_fields"]
    assert [strategy.strategy_id for strategy in reg.enabled_for_runner()] == ["good"]


def test_production_strategy_config_has_complete_classification():
    reg = StrategyRegistry()
    audit = reg.classification_audit()

    assert audit["status"] == "pass"
    assert audit["strategy_count"] >= 12
    for row in audit["strategies"]:
        cls = row["classification"]
        assert cls["expected_trades_per_day_min"] >= 1
        assert cls["expected_trades_per_day_max"] >= cls["expected_trades_per_day_min"]
        assert cls["holding_period"]
        assert cls["return_profile"]
        assert cls["risk_profile"]
