import json
from pathlib import Path

from services.binance_futures_feed import (
    BinanceFuturesFeedClient,
    run_binance_usdm_1m_backfill,
    run_binance_usdm_1m_feed_import,
    run_binance_usdm_feed_import,
)
from services.journal_store import load_json
from services.market_store import MarketStore


class _FakeResponse:
    def __init__(self, payload) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def _exchange_info() -> dict:
    return {
        "symbols": [
            {
                "symbol": "XAUUSDT",
                "status": "TRADING",
                "contractType": "TRADIFI_PERPETUAL",
                "underlyingType": "COMMODITY",
                "marginAsset": "USDT",
            }
        ]
    }


def test_binance_usdm_feed_imports_xauusdt_5m_bars(tmp_path: Path):
    seen = []

    def opener(request, timeout):
        seen.append(request.full_url)
        if "/exchangeInfo" in request.full_url:
            return _FakeResponse(_exchange_info())
        return _FakeResponse(
            [
                [
                    1779793200000,
                    "4050.10",
                    "4051.20",
                    "4049.80",
                    "4050.70",
                    "12.5",
                    1779793499999,
                    "0",
                    10,
                    "0",
                    "0",
                    "0",
                ]
            ]
        )

    store = MarketStore(tmp_path / "market.db")
    result = BinanceFuturesFeedClient(store, opener=opener).fetch_and_store(limit=1)
    bars = store.load_bars("GOLD", "5m", 10)
    quote = store.load_latest_quote("GOLD")

    assert result["status"] == "pass"
    assert result["imported_rows"] == 1
    assert any("symbol=XAUUSDT" in url for url in seen)
    assert bars[0].provider == "binance_usdm"
    assert bars[0].close == 4050.7
    assert "public_proxy_feed" in bars[0].quality_flags
    assert "crypto_perpetual" in bars[0].quality_flags
    # Binance USDM is a public proxy feed, not an official broker feed.
    assert "official_broker_feed" not in bars[0].quality_flags
    assert quote["close"] == 4050.7


def test_binance_usdm_feed_pipeline_writes_artifacts(monkeypatch, tmp_path: Path):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market.db"
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(root))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_MARKET_DB", str(db_path))

    def opener(request, timeout):
        if "/exchangeInfo" in request.full_url:
            return _FakeResponse(_exchange_info())
        return _FakeResponse([])

    result = run_binance_usdm_feed_import("2026-05-26", opener=opener)

    assert result["run_date"] == "2026-05-26"
    assert result["market_db"] == str(db_path)
    assert load_json(root / "binance_usdm_feed" / "current.json")[0]["run_date"] == "2026-05-26"


_STEP_MS = 300_000  # 5m


def _kline(open_ms: int, price: float) -> list:
    return [open_ms, f"{price:.2f}", f"{price + 1:.2f}", f"{price - 1:.2f}", f"{price + 0.5:.2f}", "10", open_ms + _STEP_MS - 1, "0", 5, "0", "0", "0"]


def _paginating_opener(start_ms: int, end_ms: int):
    """Fake Binance that returns up to `limit` 5m klines from the requested
    startTime, never past endTime — exercising fetch_range's pagination."""
    from urllib.parse import parse_qs, urlparse

    calls = []

    def opener(request, timeout):
        if "/exchangeInfo" in request.full_url:
            return _FakeResponse(_exchange_info())
        q = parse_qs(urlparse(request.full_url).query)
        calls.append(q)
        req_start = int(q["startTime"][0])
        req_end = int(q["endTime"][0])
        limit = int(q["limit"][0])
        rows = []
        t = max(req_start, start_ms)
        while t <= min(req_end, end_ms) and len(rows) < limit:
            rows.append(_kline(t, 4000 + (t - start_ms) // _STEP_MS))
            t += _STEP_MS
        return _FakeResponse(rows)

    return opener, calls


def test_fetch_range_paginates_and_dedupes(tmp_path: Path):
    start, end = 1_779_793_200_000, 1_779_793_200_000 + _STEP_MS * 4  # 5 bars
    opener, calls = _paginating_opener(start, end)
    client = BinanceFuturesFeedClient(MarketStore(tmp_path / "market.db"), opener=opener)

    bars = client.fetch_range(start, end, page_limit=2)

    assert len(bars) == 5  # deduped, full window
    ts = [b.timestamp for b in bars]
    assert ts == sorted(ts)  # chronological
    assert len(set(ts)) == 5  # no duplicates across pages
    assert all(b.provider == "binance_usdm" for b in bars)
    assert len(calls) >= 3  # 2 + 2 + 1 pagination


def test_backfill_replaces_other_provider(tmp_path: Path):
    from schemas.market_data import Bar

    store = MarketStore(tmp_path / "market.db")
    # An existing COMEX/yahoo bar at the SAME aligned timestamp as a backfill bar.
    start, end = 1_779_793_200_000, 1_779_793_200_000 + _STEP_MS * 2
    from datetime import datetime, timezone

    ts0 = datetime.fromtimestamp(start / 1000, tz=timezone.utc).replace(microsecond=0).isoformat()
    store.upsert_bars([Bar("GOLD", "5m", ts0, 1, 1, 1, 1, 1, "yahoo_chart:GC=F", ["historical_5m"])])

    opener, _ = _paginating_opener(start, end)
    client = BinanceFuturesFeedClient(store, opener=opener)
    result = client.backfill_and_store(start, end, replace_providers=["yahoo_chart:GC=F"])

    coverage = {item["provider"]: item["rows"] for item in store.coverage()}
    assert "yahoo_chart:GC=F" not in coverage  # replaced
    assert coverage["binance_usdm"] == 3
    assert result["backfilled_rows"] == 3
    assert result["deleted_rows"] == 1


_STEP_MS_1M = 60_000  # 1m


def _kline_1m(open_ms: int, price: float) -> list:
    return [open_ms, f"{price:.2f}", f"{price + 1:.2f}", f"{price - 1:.2f}", f"{price + 0.5:.2f}", "7", open_ms + _STEP_MS_1M - 1, "0", 3, "0", "0", "0"]


def _paginating_opener_1m(start_ms: int, end_ms: int):
    from urllib.parse import parse_qs, urlparse

    def opener(request, timeout):
        if "/exchangeInfo" in request.full_url:
            return _FakeResponse(_exchange_info())
        q = parse_qs(urlparse(request.full_url).query)
        # 1m feed must request the 1m interval, not 5m.
        assert q["interval"][0] == "1m"
        req_start = int(q["startTime"][0])
        req_end = int(q["endTime"][0])
        limit = int(q["limit"][0])
        rows = []
        t = max(req_start, start_ms)
        while t <= min(req_end, end_ms) and len(rows) < limit:
            rows.append(_kline_1m(t, 4000 + (t - start_ms) // _STEP_MS_1M))
            t += _STEP_MS_1M
        return _FakeResponse(rows)

    return opener


def test_binance_usdm_1m_feed_import_writes_gold_1m(monkeypatch, tmp_path: Path):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market.db"
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(root))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_MARKET_DB", str(db_path))

    def opener(request, timeout):
        if "/exchangeInfo" in request.full_url:
            return _FakeResponse(_exchange_info())
        from urllib.parse import parse_qs, urlparse

        assert parse_qs(urlparse(request.full_url).query)["interval"][0] == "1m"
        return _FakeResponse([_kline_1m(1_779_793_200_000, 4050.0)])

    result = run_binance_usdm_1m_feed_import("2026-05-26", opener=opener)

    assert result["timeframe"] == "1m"
    assert result["interval"] == "1m"
    assert result["output_symbol"] == "GOLD"
    bars = MarketStore(db_path).load_bars("GOLD", "1m", 10)
    assert len(bars) == 1 and bars[0].provider == "binance_usdm"
    # 1m is a NEW, separate series — it must not touch the existing 5m bars.
    assert MarketStore(db_path).load_bars("GOLD", "5m", 10) == []
    assert load_json(root / "binance_usdm_1m_feed" / "current.json")[0]["run_date"] == "2026-05-26"


def test_binance_usdm_1m_backfill_paginates_into_gold_1m(monkeypatch, tmp_path: Path):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market.db"
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(root))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_MARKET_DB", str(db_path))
    from services.binance_futures_feed import _iso_to_ms

    start_iso, end_iso = "2026-05-19T00:00:00+00:00", "2026-05-19T00:09:00+00:00"  # 10 one-minute bars
    result = run_binance_usdm_1m_backfill(
        start_iso,
        end_iso,
        opener=_paginating_opener_1m(_iso_to_ms(start_iso), _iso_to_ms(end_iso)),
    )

    assert result["timeframe"] == "1m"
    assert result["backfilled_rows"] == 10
    bars = MarketStore(db_path).load_bars("GOLD", "1m", 100)
    assert len(bars) == 10
    ts = [b.timestamp for b in bars]
    assert ts == sorted(ts) and len(set(ts)) == 10  # chronological, deduped
    assert all(b.provider == "binance_usdm" and b.timeframe == "1m" for b in bars)
    assert load_json(root / "binance_usdm_1m_backfill" / "current.json")[0]["backfilled_rows"] == 10


def test_fetch_klines_retries_transient_errors(tmp_path: Path):
    import urllib.error

    attempts = {"n": 0}

    def flaky_opener(request, timeout):
        if "/exchangeInfo" in request.full_url:
            return _FakeResponse(_exchange_info())
        attempts["n"] += 1
        if attempts["n"] < 3:  # fail twice, then succeed
            raise urllib.error.URLError("SSL: UNEXPECTED_EOF_WHILE_READING")
        return _FakeResponse([_kline_1m(1_779_793_200_000, 4050.0)])

    client = BinanceFuturesFeedClient(
        MarketStore(tmp_path / "m.db"),
        {"symbol": "XAUUSDT", "interval": "1m", "output_symbol": "GOLD", "timeframe": "1m", "max_retries": 5, "retry_pause_seconds": 0},
        opener=flaky_opener,
        sleeper=lambda _seconds: None,
    )
    bars = client.fetch(limit=1)

    assert len(bars) == 1 and bars[0].close == 4050.5
    assert attempts["n"] == 3  # retried twice before succeeding


def test_fetch_klines_raises_after_exhausting_retries(tmp_path: Path):
    import urllib.error

    import pytest

    def always_fails(request, timeout):
        if "/exchangeInfo" in request.full_url:
            return _FakeResponse(_exchange_info())
        raise urllib.error.URLError("down")

    client = BinanceFuturesFeedClient(
        MarketStore(tmp_path / "m.db"),
        {"symbol": "XAUUSDT", "interval": "1m", "max_retries": 2, "retry_pause_seconds": 0},
        opener=always_fails,
        sleeper=lambda _seconds: None,
    )
    with pytest.raises(urllib.error.URLError):
        client.fetch(limit=1)


def test_1m_backfill_chunks_a_multi_day_window_and_persists_incrementally(monkeypatch, tmp_path: Path):
    root = tmp_path / "outputs"
    db_path = tmp_path / "market.db"
    monkeypatch.setenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(root))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_MARKET_DB", str(db_path))
    from services.binance_futures_feed import _iso_to_ms

    # A 3-day window of 1m bars, backfilled in 1-day chunks.
    start_iso, end_iso = "2026-05-19T00:00:00+00:00", "2026-05-22T00:00:00+00:00"
    opener = _paginating_opener_1m(_iso_to_ms(start_iso), _iso_to_ms(end_iso))

    seen_windows = []
    base_opener = opener

    def tracking_opener(request, timeout):
        from urllib.parse import parse_qs, urlparse

        if "/exchangeInfo" not in request.full_url:
            q = parse_qs(urlparse(request.full_url).query)
            seen_windows.append((int(q["startTime"][0]), int(q["endTime"][0])))
        return base_opener(request, timeout)

    result = run_binance_usdm_1m_backfill(start_iso, end_iso, opener=tracking_opener, chunk_days=1, sleeper=lambda _s: None)

    bars = MarketStore(db_path).load_bars("GOLD", "1m", 10_000)
    # 3 days * 1440 minutes + the inclusive final boundary bar.
    assert len(bars) == 3 * 1440 + 1
    assert result["backfilled_rows"] == len(bars)
    # Persisted incrementally across multiple day-sized chunks (not one giant call window).
    chunk_starts = {start for start, _end in seen_windows}
    assert len(chunk_starts) >= 3
    ts = [b.timestamp for b in bars]
    assert ts == sorted(ts) and len(set(ts)) == len(ts)  # contiguous, deduped across chunks

    from schemas.market_data import Bar

    store = MarketStore(tmp_path / "market.db")
    store.upsert_bars([
        Bar("GOLD", "5m", "2026-05-22T20:50:00+00:00", 1, 1, 1, 1, 1, "yahoo_chart:GC=F", []),
        Bar("GOLD", "5m", "2026-05-22T20:55:00+00:00", 2, 2, 2, 2, 2, "binance_usdm", []),
    ])

    deleted = store.delete_bars("GOLD", "5m", "yahoo_chart:GC=F")

    assert deleted == 1
    providers = {item["provider"] for item in store.coverage()}
    assert providers == {"binance_usdm"}
