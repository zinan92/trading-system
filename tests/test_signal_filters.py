from datetime import datetime, timezone

from schemas.asset import Asset
from schemas.market_data import Bar
from schemas.signal import Signal
from services.signal_filters import FilteredSignalEngine, MacdFilter, build_filter, build_filtered_engine

_GOLD = Asset("GOLD", "Gold", "commodity", "high", "UTC")


def _bars(closes: list[float]) -> list[Bar]:
    return [Bar("GOLD", "1m", f"2026-05-01T00:{i % 60:02d}:00+00:00", c, c + 1, c - 1, c, 10, "binance_usdm", []) for i, c in enumerate(closes)]


def _long_signal() -> Signal:
    return Signal("sig_x", "GOLD", "commodity", "long", 70, 65, "chan_1m", "chan 二买", regime="chan_second_buy", status="new")


class _StubEngine:
    def __init__(self, signal):
        self._signal = signal

    def generate(self, asset, candles, events=None, run_date="", factor_context=None):
        return self._signal


def test_macd_filter_passes_long_when_macd_above_zero():
    rising = [100 + i for i in range(60)]  # MACD line firmly > 0
    ok, _reason = MacdFilter().accepts(_long_signal(), _bars(rising))
    assert ok is True


def test_macd_filter_vetoes_long_when_macd_below_zero():
    falling = [200 - i for i in range(60)]  # MACD line < 0 -> no bullish confirmation
    ok, reason = MacdFilter().accepts(_long_signal(), _bars(falling))
    assert ok is False and "0" in reason


def test_filtered_engine_downgrades_to_watch_when_filter_rejects():
    falling = [200 - i for i in range(60)]
    engine = FilteredSignalEngine(_StubEngine(_long_signal()), [MacdFilter()])
    out = engine.generate(_GOLD, _bars(falling), [], "2026-05-02")
    assert out.direction == "watch"
    assert out.status == "no_signal"
    assert "filter 否决" in out.thesis


def test_filtered_engine_passes_signal_through_when_filter_accepts():
    rising = [100 + i for i in range(60)]
    engine = FilteredSignalEngine(_StubEngine(_long_signal()), [MacdFilter()])
    out = engine.generate(_GOLD, _bars(rising), [], "2026-05-02")
    assert out.direction == "long"  # untouched
    assert out.status == "new"


def test_build_filter_and_filtered_engine_from_specs():
    engine = build_filtered_engine(_StubEngine(_long_signal()), [{"type": "macd", "mode": "zero_line"}])
    assert isinstance(engine, FilteredSignalEngine)
    assert isinstance(engine.filters[0], MacdFilter)
    assert isinstance(build_filter("macd"), MacdFilter)


class _HistEngine:
    def historical_signals(self, candles):
        return [{"index": 30, "direction": "long"}, {"index": 59, "direction": "long"}]


def test_filtered_engine_historical_signals_keeps_only_confirmed():
    rising = [100 + i for i in range(60)]  # MACD line > 0 at both indices -> both kept
    engine = FilteredSignalEngine(_HistEngine(), [MacdFilter()])
    kept = engine.historical_signals(_bars(rising))
    assert [s["index"] for s in kept] == [30, 59]

    falling = [200 - i for i in range(60)]  # MACD line < 0 -> long vetoed -> none kept
    engine2 = FilteredSignalEngine(_HistEngine(), [MacdFilter()])
    assert engine2.historical_signals(_bars(falling)) == []


def test_build_filter_rejects_unknown():
    try:
        build_filter({"type": "nonsense"})
        assert False, "expected ValueError"
    except ValueError:
        pass
