"""#1387: silver and WTI read OKX public swap data through the datafeed; calls recorded before the move stay on futures.
#1407: the same assets then become Paper grids on the same swaps; each earlier era keeps its own feed for scoring."""
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


def test_the_desk_shows_the_okx_price_on_the_paper_silver_row(config, sources, store, fetch) -> None:
    fetch.routes[SILVER] = okx_candles("XAG-USDT-SWAP", [52.1, 52.14])
    client, ex = client_for(config, sources, store, fetch)
    desk = client.get("/api/desk/SILVER").json()
    assert desk["meta"]["venue"] == "OKX XAG-USDT-SWAP · 纸面盘" and desk["meta"]["money"] == "纸面模拟 · 手续费未核实"
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
    assert (store.asset("SILVER")["kind"], store.asset("SILVER")["instrument_id"]) == ("xag_paper", "XAG-USDT-SWAP")
    assert (store.asset("WTI")["kind"], store.asset("WTI")["instrument_id"]) == ("cl_paper", "CL-USDT-SWAP")
    assert store.source_at("SILVER", "2026-11-01T00:00:00+00:00") == {"kind": "watch", "instrument_id": "SIL"}
    with sqlite3.connect(path) as conn:
        stops = conn.execute("select kind, until from asset_sources where asset='SILVER' order by until").fetchall()
    assert [kind for kind, _ in stops] == ["watch", "okx_public"] and stops[0][1] < stops[1][1]  # two stops, never merged
    assert store.source_at("SILVER", stops[0][1]) == {"kind": "okx_public", "instrument_id": "XAG-USDT-SWAP"}
    assert store.source_at("SILVER", "2999-01-01T00:00:00+00:00") is None
    Store(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("select count(*) from asset_sources").fetchone()[0] == 4  # two per asset, not recorded twice


def test_a_row_park_changed_is_not_moved(tmp_path) -> None:
    path = tmp_path / "old.db"
    _old_database(path)
    with sqlite3.connect(path) as conn:
        conn.execute("update assets set instrument_id='SI' where key='SILVER'")
    store = Store(path)
    assert (store.asset("SILVER")["kind"], store.asset("SILVER")["instrument_id"]) == ("watch", "SI")
    assert store.source_at("SILVER", "2026-11-01T00:00:00+00:00") is None


def test_each_era_is_scored_on_its_own_feed(tmp_path, config, fetch) -> None:
    path = tmp_path / "old.db"
    _old_database(path)
    now = datetime.now(timezone.utc)
    store = Store(path)  # silver moves to OKX public, then to Paper
    with sqlite3.connect(path) as conn:  # the futures stop 76 hours ago, the Paper move 74 hours ago
        conn.execute("update asset_sources set until=? where kind='watch'", ((now - timedelta(hours=76)).isoformat(),))
        conn.execute("update asset_sources set until=? where kind='okx_public'", ((now - timedelta(hours=74)).isoformat(),))
    for hours_ago, price in ((80, 40.0), (75, 50.0), (73, 51.0)):  # one call in each era; all due
        store.add_judgment(asset="SILVER", direction="long", confidence=3, reason="", cited=[], price_at=price, plan=None,
                           action="recorded", created_at=(now - timedelta(hours=hours_ago)).isoformat(), author="ai")
    fetch.routes["/api/overview"] = {"assets": [{"asset_key": "silver", "timeframes": [{"timeframe": "thirty_minute", "candles": [
        {"timestamp": (now - timedelta(hours=8, minutes=10)).isoformat(), "open": 41, "high": 41, "low": 41, "close": 41.0}]}]}]}
    fetch.routes[SILVER] = okx_candles("XAG-USDT-SWAP", [52.0] * 100)
    assert review.resolve_due(store, Sources(config, fetch=fetch), 72) == 3
    scored = {j["price_at"]: j for j in store.judgments("SILVER", author="ai")}
    assert scored[40.0]["price_after"] == 41.0  # futures candles: the feed its price was read from
    assert scored[50.0]["price_after"] == 52.0  # OKX candles, read as the judgment-only kind
    assert scored[51.0]["price_after"] == 52.0  # OKX candles, read by the Paper kind
