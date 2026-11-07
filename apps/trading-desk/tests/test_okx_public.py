"""#1387: silver and WTI read OKX public swap data through the datafeed; calls recorded before the move stay on futures."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from conftest import okx_candles
from test_desk import client_for
from trading_desk import review
from trading_desk.sources import Sources
from trading_desk.store import Store

SILVER = "/api/candles/commodity/XAG-USDT-SWAP"


def test_bars_come_from_the_okx_source_on_the_datafeed(config, fetch, store) -> None:
    fetch.routes[SILVER] = okx_candles("XAG-USDT-SWAP", [52.1, 52.14])
    got = Sources(config, fetch=fetch).bars(store.asset("SILVER"), "1h", 2)
    assert got["ok"] and got["bars"][-1][4] == 52.14 and "OKX XAG-USDT-SWAP" in got["source"]
    url = fetch.calls[-1][0]
    assert url.startswith(config.datafeed_url) and "source=okx_swap_public" in url and "timeframe=1h" in url
    assert "cache_policy=bypass" in url  # OKX's current candle, never 8100's cached copy (#1390)


def test_a_stale_feed_is_not_a_price(config, fetch, store) -> None:
    fetch.routes[SILVER] = okx_candles("XAG-USDT-SWAP", [52.1, 52.14], hours_old=3)
    got = Sources(config, fetch=fetch).bars(store.asset("SILVER"), "1h", 2)
    assert got["ok"] is False and "过期" in got["reason"] and got["bars"] == []


def test_candles_from_another_source_or_symbol_are_refused(config, fetch, store) -> None:
    sources = Sources(config, fetch=fetch)
    for wrong in ({"selected_source": "yahoo_finance"}, {"provider_symbol": "SI=F"}, {"is_synthetic": True}):
        fetch.routes[SILVER] = {**okx_candles("XAG-USDT-SWAP", [52.14]), **wrong}
        assert sources.bars(store.asset("SILVER"), "1h", 2)["ok"] is False


def test_the_desk_shows_the_okx_price_and_keeps_calls_record_only(config, sources, store, fetch) -> None:
    fetch.routes[SILVER] = okx_candles("XAG-USDT-SWAP", [52.1, 52.14])
    client, ex = client_for(config, sources, store, fetch)
    desk = client.get("/api/desk/SILVER").json()
    assert desk["meta"]["venue"] == "OKX XAG-USDT-SWAP 公开行情" and desk["meta"]["money"] == "只记判断"
    assert client.post("/api/plan", json={"asset": "SILVER", "direction": "long"}).json()["kind"] == "record_only"
    saved = client.post("/api/judgments", json={"asset": "SILVER", "direction": "long", "confidence": 3, "action": "recorded"}).json()
    assert saved["price_at"] == 52.14
    assert ex.driver_calls == []


def _old_database(path) -> None:
    """A desk from before #1387: silver and WTI on futures candles, one open call each."""
    Store(path)
    with sqlite3.connect(path) as conn:
        conn.execute("update assets set kind='watch', instrument_id='SIL' where key='SILVER'")
        conn.execute("update assets set kind='watch', instrument_id='CL' where key='WTI'")
        conn.execute("delete from asset_sources")


def test_an_older_database_moves_silver_and_wti_once_and_remembers_the_futures_feed(tmp_path) -> None:
    path = tmp_path / "old.db"
    _old_database(path)
    store = Store(path)
    assert (store.asset("SILVER")["kind"], store.asset("SILVER")["instrument_id"]) == ("okx_public", "XAG-USDT-SWAP")
    assert (store.asset("WTI")["kind"], store.asset("WTI")["instrument_id"]) == ("okx_public", "CL-USDT-SWAP")
    assert store.source_at("SILVER", "2026-11-01T00:00:00+00:00") == {"kind": "watch", "instrument_id": "SIL"}
    assert store.source_at("SILVER", "2999-01-01T00:00:00+00:00") is None
    Store(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("select count(*) from asset_sources").fetchone()[0] == 2  # not recorded twice


def test_a_row_park_changed_is_not_moved(tmp_path) -> None:
    path = tmp_path / "old.db"
    _old_database(path)
    with sqlite3.connect(path) as conn:
        conn.execute("update assets set instrument_id='SI' where key='SILVER'")
    store = Store(path)
    assert (store.asset("SILVER")["kind"], store.asset("SILVER")["instrument_id"]) == ("watch", "SI")
    assert store.source_at("SILVER", "2026-11-01T00:00:00+00:00") is None


def test_a_call_recorded_on_futures_is_scored_on_futures_and_a_new_one_on_okx(tmp_path, config, fetch) -> None:
    path = tmp_path / "old.db"
    _old_database(path)
    now = datetime.now(timezone.utc)
    store = Store(path)  # silver moves to OKX
    with sqlite3.connect(path) as conn:
        conn.execute("update asset_sources set until=?", ((now - timedelta(hours=75)).isoformat(),))  # the move, 75 hours ago
    for hours_ago, price in ((80, 40.0), (73, 50.0)):  # one call before the move, one after; both due
        store.add_judgment(asset="SILVER", direction="long", confidence=3, reason="", cited=[], price_at=price, plan=None,
                           action="recorded", created_at=(now - timedelta(hours=hours_ago)).isoformat(), author="ai")
    fetch.routes["/api/overview"] = {"assets": [{"asset_key": "silver", "timeframes": [{"timeframe": "thirty_minute", "candles": [
        {"timestamp": (now - timedelta(hours=8, minutes=10)).isoformat(), "open": 41, "high": 41, "low": 41, "close": 41.0}]}]}]}
    fetch.routes[SILVER] = okx_candles("XAG-USDT-SWAP", [52.0] * 100)
    assert review.resolve_due(store, Sources(config, fetch=fetch), 72) == 2
    scored = {j["price_at"]: j for j in store.judgments("SILVER", author="ai")}
    assert scored[40.0]["price_after"] == 41.0  # futures candles: the feed its price was read from
    assert scored[50.0]["price_after"] == 52.0  # OKX candles
