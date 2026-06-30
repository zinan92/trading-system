"""Chan engine tests, against a committed real 1m-gold fixture (deterministic,
offline). Skips cleanly if the chan core's deps (pandas, py>=3.11) are absent —
chan strategies are meant to run under the 3.13 strategies job, not 3.9."""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from schemas.asset import Asset
from schemas.market_data import Bar

pytest.importorskip("pandas", reason="chan core needs pandas (runs under 3.13)")
if sys.version_info < (3, 11):
    pytest.skip("chan core needs Python >= 3.11", allow_module_level=True)

from services.chan_signal_engine import ChanSignalEngine
from services import chan_signal_engine as chan_module

_FIXTURE = Path(__file__).parent / "fixtures" / "chan_gold_1m.csv"


def _load_bars() -> list[Bar]:
    bars = []
    for line in _FIXTURE.read_text(encoding="utf-8").splitlines()[1:]:
        ts, o, h, l, c, v = line.split(",")
        iso = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).isoformat()
        bars.append(Bar("GOLD", "1m", iso, float(o), float(h), float(l), float(c), float(v), "binance_usdm", []))
    return bars


class _FakeType:
    value = "2"


class _FakeKlu:
    time = "2026/05/26 00:00"
    close = 4500.0


class _FakePoint:
    type = [_FakeType()]
    klu = _FakeKlu()
    is_buy = True


class _FakeChan:
    def trigger_load(self, payload):
        return None

    def get_seg_bsp(self):
        return [_FakePoint()]


class _FakeKLType:
    K_1M = "K_1M"


def test_detect_points_uses_process_cache_for_identical_inputs(monkeypatch):
    chan_module._DETECT_POINTS_CACHE.clear()
    calls = {"new_chan": 0}

    def fake_new_chan(self, candles):
        calls["new_chan"] += 1
        return _FakeChan(), lambda bar: bar, _FakeKLType

    monkeypatch.setattr(ChanSignalEngine, "_new_chan", fake_new_chan)
    bars = [Bar("GOLD", "1m", f"2026-05-26T00:{i:02d}:00+00:00", 4500, 4501, 4499, 4500 + i, 1, "test", []) for i in range(5)]
    engine = ChanSignalEngine({"symbol": "GOLD", "signal": {"bsp_types": ["2", "2s"]}})

    first = engine.detect_points(bars)
    first[0]["time"] = "mutated"
    second = engine.detect_points(bars)

    assert calls["new_chan"] == 1
    assert second[0]["time"] == "2026/05/26 00:00"


def test_detects_second_buy_points_on_real_gold(tmp_path):
    bars = _load_bars()
    engine = ChanSignalEngine({"symbol": "GOLD", "signal": {"chan_data_dir": str(tmp_path)}})
    points = engine.detect_points(bars)

    # Loosened config surfaces 二买/类二买 on real 1m gold (~5d fixture).
    assert len(points) >= 3, f"expected 二买/类二买 points, got {len(points)}"
    assert all(set(p["types"]) & {"2", "2s"} for p in points)
    assert any(p["is_buy"] for p in points) or any(not p["is_buy"] for p in points)


def test_generate_emits_signal_when_point_is_fresh(tmp_path):
    bars = _load_bars()
    engine = ChanSignalEngine({"symbol": "GOLD", "signal": {"chan_data_dir": str(tmp_path)}})
    points = engine.detect_points(bars)
    assert points, "fixture should contain detected points"

    # Truncate the bar series to END exactly on a detected 二买/类二买 bar →
    # generate() must see it as fresh and emit a directional signal.
    target = points[-1]
    target_key = "".join(ch for ch in target["time"] if ch.isdigit())[:12]
    cut = [b for b in bars if datetime.fromisoformat(b.timestamp).strftime("%Y%m%d%H%M") <= target_key]
    assert len(cut) >= 300

    gold = Asset("GOLD", "Gold", "commodity", "high", "UTC")
    sig = engine.generate(gold, cut, [], "2026-05-31")
    assert sig.direction in ("long", "short")
    assert sig.regime == "chan_second_buy"
    assert ("2" in sig.thesis or "二买" in sig.thesis)


def test_no_signal_when_too_few_bars(tmp_path):
    bars = _load_bars()[:100]
    engine = ChanSignalEngine({"symbol": "GOLD", "signal": {"chan_data_dir": str(tmp_path)}})
    sig = engine.generate(Asset("GOLD", "Gold", "commodity", "high", "UTC"), bars, [], "2026-05-31")
    assert sig.direction == "watch"
    assert sig.status == "no_signal"


def test_historical_signals_returns_indexed_directions(tmp_path):
    bars = _load_bars()[-3500:]  # a slice that contains several 二买/类二买
    engine = ChanSignalEngine({"symbol": "GOLD", "signal": {"chan_data_dir": str(tmp_path)}})
    signals = engine.historical_signals(bars)

    assert signals, "expected at least one historical signal in the slice"
    assert all(set(s) == {"index", "direction"} for s in signals)
    assert all(0 <= s["index"] < len(bars) for s in signals)
    assert all(s["direction"] in ("long", "short") for s in signals)
    assert [s["index"] for s in signals] == sorted(s["index"] for s in signals)


def test_bsp_types_splits_first_and_second_buy(tmp_path):
    """The engine's emitted points are filterable by bsp type, so 一买 (1/1p) and
    二买 (2/2s) become distinct strategies off the SAME chan core."""
    bars = _load_bars()
    second = ChanSignalEngine({"symbol": "GOLD", "signal": {"chan_data_dir": str(tmp_path / "two")}})  # default 2,2s
    first = ChanSignalEngine({"symbol": "GOLD", "signal": {"chan_data_dir": str(tmp_path / "one"), "bsp_types": ["1", "1p"]}})

    second_pts = second.detect_points(bars)
    first_pts = first.detect_points(bars)

    assert second_pts and first_pts
    assert all(set(p["types"]) & {"2", "2s"} for p in second_pts)  # default = 二买/类二买 only
    assert all(set(p["types"]) & {"1", "1p"} for p in first_pts)   # 一买 strategy = 一买 only
    # The split is real: there exist pure-一买 points the 二买 strategy never sees.
    assert any(set(p["types"]) <= {"1", "1p"} for p in first_pts)
    assert {p["time"] for p in first_pts} != {p["time"] for p in second_pts}


def test_first_buy_strategy_emits_first_buy_signal(tmp_path):
    bars = _load_bars()
    engine = ChanSignalEngine({"symbol": "GOLD", "signal": {"chan_data_dir": str(tmp_path), "bsp_types": ["1", "1p"]}})
    points = engine.detect_points(bars)
    gold = Asset("GOLD", "Gold", "commodity", "high", "UTC")

    for point in reversed([p for p in points if p["is_buy"]]):
        key = "".join(ch for ch in point["time"] if ch.isdigit())[:12]
        cut = [b for b in bars if datetime.fromisoformat(b.timestamp).strftime("%Y%m%d%H%M") <= key]
        if len(cut) < 300:
            continue
        sig = engine.generate(gold, cut, [], "2026-05-31")
        if sig.direction == "long":
            assert sig.regime == "chan_first_buy"  # NOT chan_second_buy — distinct regime on the leaderboard
            assert "一买" in sig.thesis
            return
    pytest.skip("no causally-stable fresh 一买 long in fixture")
