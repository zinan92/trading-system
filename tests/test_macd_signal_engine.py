from datetime import datetime, timezone

from schemas.asset import Asset
from schemas.market_data import Bar
from services.macd_signal_engine import MacdSignalEngine

_GOLD = Asset("GOLD", "Gold", "commodity", "high", "UTC")


def _bars(closes: list[float]) -> list[Bar]:
    bars = []
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    for i, c in enumerate(closes):
        ts = base.replace(minute=i % 60, hour=(i // 60) % 24).isoformat()
        bars.append(Bar("GOLD", "1m", ts, c, c + 1, c - 1, c, 10, "binance_usdm", []))
    return bars


def test_golden_cross_emits_long():
    # Long decline then sustained rise -> a golden cross forms during the rise.
    closes = [100 - i * 0.3 for i in range(60)] + [82 + i * 1.0 for i in range(40)]
    engine = MacdSignalEngine({"signal": {"fresh_bars": 3}})
    direction, index = engine.detect_cross(_bars(closes))
    assert direction == "long"  # a 金叉 exists in the series
    cut = _bars(closes)[: index + 2]  # truncate so the cross is within fresh_bars of the end
    sig = engine.generate(_GOLD, cut, [], "2026-05-02")
    assert sig.direction == "long"
    assert sig.regime == "macd_golden_cross"
    assert "金叉" in sig.thesis
    assert sig.strength >= 70  # clears the thin-backtest ticket gate


def test_death_cross_emits_short():
    closes = [100 + i * 0.3 for i in range(60)] + [118 - i * 1.0 for i in range(40)]
    engine = MacdSignalEngine({"signal": {"fresh_bars": 3}})
    direction, index = engine.detect_cross(_bars(closes))
    assert direction == "short"  # a 死叉 exists
    cut = _bars(closes)[: index + 2]
    sig = engine.generate(_GOLD, cut, [], "2026-05-02")
    assert sig.direction == "short"
    assert sig.regime == "macd_death_cross"
    assert "死叉" in sig.thesis


def test_no_signal_when_cross_is_stale():
    # Cross happens early, then a long flat tail -> not fresh.
    closes = [100 - i for i in range(30)] + [70 + i for i in range(15)] + [85.0] * 40
    engine = MacdSignalEngine({"signal": {"fresh_bars": 3}})
    sig = engine.generate(_GOLD, _bars(closes), [], "2026-05-02")
    assert sig.direction == "watch"
    assert sig.status == "no_signal"


def test_no_signal_when_too_few_bars():
    engine = MacdSignalEngine({"signal": {}})
    sig = engine.generate(_GOLD, _bars([100.0] * 20), [], "2026-05-02")
    assert sig.direction == "watch"
    assert sig.status == "no_signal"


def test_historical_signals_enumerates_all_crosses():
    closes = [100 - i * 0.3 for i in range(60)] + [82 + i for i in range(40)] + [120 - i for i in range(40)]
    engine = MacdSignalEngine({"signal": {}})
    signals = engine.historical_signals(_bars(closes))

    assert len(signals) >= 2  # at least a golden then a death cross
    assert {s["direction"] for s in signals} <= {"long", "short"}
    assert all(0 <= s["index"] < len(closes) for s in signals)
    assert [s["index"] for s in signals] == sorted(s["index"] for s in signals)
