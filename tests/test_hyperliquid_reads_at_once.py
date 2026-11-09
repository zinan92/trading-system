"""Independent Hyperliquid testnet reads run at the same time (#1469).

Each public call takes ~2 s from this host. The catalog made four in a row (~8 s) and Preview seven. The reads
stay live and their results and blockers stay the same; only the waiting overlaps.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

import services.hyperliquid_testnet_market_reader as hl
from services.dashboard_control_plane import DashboardControlPlane, public_catalog_loader

DELAY = 0.2


class Response:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


PAYLOADS = {
    "allMids": {"BTC": "79665.5"},
    "l2Book": {"coin": "BTC", "levels": [[{"px": "79660.0", "sz": "0.25", "n": 1}], [{"px": "79670.0", "sz": "0.20", "n": 1}]]},
    "metaAndAssetCtxs": [{"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40}]}, [{"oraclePx": "79665.0", "markPx": "79665.5"}]],
}


class Overlap:
    """Counts how many calls are in flight at once."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.now = self.peak = 0

    def __enter__(self):
        with self.lock:
            self.now += 1
            self.peak = max(self.peak, self.now)

    def __exit__(self, *_args):
        with self.lock:
            self.now -= 1


def test_run_together_returns_results_in_order_and_overlaps() -> None:
    seen = Overlap()

    def call(value: int):
        def run() -> int:
            with seen:
                time.sleep(DELAY)
            return value
        return run

    began = time.monotonic()
    assert hl.run_together(call(1), call(2), call(3)) == [1, 2, 3]
    assert time.monotonic() - began < 2 * DELAY and seen.peak == 3


def test_run_together_raises_the_first_failure_in_argument_order() -> None:
    def slow_failure() -> None:
        time.sleep(DELAY)
        raise ValueError("first")

    def fast_failure() -> None:
        raise KeyError("second")

    with pytest.raises(ValueError, match="first"):  # the later call failed sooner, but the old order wins
        hl.run_together(slow_failure, fast_failure)


def test_the_market_read_takes_one_call_not_three_and_reads_the_same() -> None:
    seen = Overlap()

    def opener(request, timeout):
        with seen:
            time.sleep(DELAY)
        return Response(PAYLOADS[json.loads(request.data.decode())["type"]])

    reference = hl.HyperliquidTestnetMarketReader(
        opener=lambda request, timeout: Response(PAYLOADS[json.loads(request.data.decode())["type"]]),
        clock=lambda: 1787827000.0,
    ).read("BTC-USD-PERP")
    began = time.monotonic()
    result = hl.HyperliquidTestnetMarketReader(opener=opener, clock=lambda: 1787827000.0).read("BTC-USD-PERP")
    assert time.monotonic() - began < 2 * DELAY, "three calls one after another take at least 3 x DELAY"
    assert seen.peak == 3
    assert result == reference


def test_a_failed_market_call_is_still_typed() -> None:
    def opener(request, timeout):
        if json.loads(request.data.decode())["type"] == "l2Book":
            raise OSError("connection reset")
        return Response(PAYLOADS[json.loads(request.data.decode())["type"]])

    with pytest.raises(hl.HyperliquidTestnetMarketError, match="testnet_market_unavailable"):
        hl.HyperliquidTestnetMarketReader(opener=opener).read("BTC-USD-PERP")


class FakeReader:
    """Stands in for HyperliquidTestnetMarketReader; every read takes DELAY."""

    fail: set[str] = set()
    seen = Overlap()

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def _wait(self, name: str) -> None:
        with FakeReader.seen:
            time.sleep(DELAY)
        if name in FakeReader.fail:
            raise hl.HyperliquidTestnetMarketError(f"{name}_failed")

    def read_catalog(self) -> dict:
        self._wait("catalog")
        return {"instruments": [{"instrument_id": "BTC-USD-PERP", "asset": "BTC", "eligibility": "unknown"},
                                {"instrument_id": "ETH-USD-PERP", "asset": "ETH", "eligibility": "unknown"}]}

    def read(self, instrument_id: str) -> dict:
        self._wait("market")
        return {"bid": 1.0, "ask": 2.0, "mid": 1.5, "price": 1.5, "fresh": True, "observed_at": "2026-11-09T00:00:00+00:00"}

    def read_bars(self, instrument_id: str, *, timeframe: str, limit: int, end) -> dict:
        self._wait(f"bars_{timeframe}")
        return {"provider": "hyperliquid", "is_synthetic": False, "fresh": True, "bars": [{"timeframe": timeframe, "limit": limit}]}


@pytest.fixture
def fake_reader(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(hl, "HyperliquidTestnetMarketReader", FakeReader)
    monkeypatch.setattr(FakeReader, "fail", set())
    monkeypatch.setattr(FakeReader, "seen", Overlap())
    return FakeReader


def test_the_catalog_reads_inventory_and_btc_market_together(fake_reader) -> None:
    began = time.monotonic()
    rows = {row["instrument_id"]: row for row in public_catalog_loader("hyperliquid.testnet")}
    assert time.monotonic() - began < 2 * DELAY and fake_reader.seen.peak == 2
    assert rows["BTC-USD-PERP"]["eligibility"] == "eligible" and rows["BTC-USD-PERP"]["market_fresh"] is True
    assert rows["BTC-USD-PERP"]["market_quality"] == {"bid": 1.0, "ask": 2.0, "mid": 1.5}
    assert rows["ETH-USD-PERP"]["eligibility"] == "unknown"


def test_a_market_failure_still_blocks_btc_and_a_catalog_failure_still_raises(fake_reader) -> None:
    fake_reader.fail = {"market"}
    rows = {row["instrument_id"]: row for row in public_catalog_loader("hyperliquid.testnet")}
    assert rows["BTC-USD-PERP"]["eligibility"] == "blocked"
    assert rows["BTC-USD-PERP"]["blockers"] == ["market_facts_unavailable"]
    fake_reader.fail = {"catalog"}
    with pytest.raises(hl.HyperliquidTestnetMarketError, match="catalog_failed"):
        public_catalog_loader("hyperliquid.testnet")


def test_preview_reads_market_and_three_bar_series_together(fake_reader, tmp_path: Path) -> None:
    plane = DashboardControlPlane(tmp_path)
    began = time.monotonic()
    market, _account = plane.resolve_runtime_facts(venue_profile_id="hyperliquid.testnet", instrument_id="BTC-USD-PERP")
    assert time.monotonic() - began < 2 * DELAY and fake_reader.seen.peak == 4
    assert market is not None and market["timeframe"] == "1m"
    assert market["bars"] == [{"timeframe": "1m", "limit": 240}]
    assert {tf: ctx["bars"][0]["limit"] for tf, ctx in market["strategy_timeframes"].items()} == {"1m": 240, "1d": 32, "4h": 32}
    assert market["latest_close"] == 1.5


@pytest.mark.parametrize("failing", ["market", "bars_1m", "bars_1d", "bars_4h"])
def test_any_failed_preview_read_still_leaves_no_market(fake_reader, tmp_path: Path, failing: str) -> None:
    fake_reader.fail = {failing}
    market, _account = DashboardControlPlane(tmp_path).resolve_runtime_facts(
        venue_profile_id="hyperliquid.testnet", instrument_id="BTC-USD-PERP")
    assert market is None
