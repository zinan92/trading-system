from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from trading_desk import plans, review
from trading_desk.app import create_app


def test_news_ranks_asset_news_within_bucket_and_drops_reddit(sources):
    news = sources.news("BTC")
    assert news["ok"] is True
    assert [i["id"] for i in news["items"]] == [2, 1]
    assert news["items"][1]["topic"] == "BTC"


def test_news_serves_last_good_list_when_intel_fails(sources, fetch):
    assert sources.news("XAU")["ok"]
    sources._news_cache["XAU"] = (0.0, sources._news_cache["XAU"][1])  # expire
    fetch.routes["/api/articles/search"] = ConnectionError("down")
    stale = sources.news("XAU")
    assert stale["ok"] is True and stale["stale"] is True and stale["items"]


def test_news_down_without_cache_is_explicit(sources, fetch):
    fetch.routes["/api/articles/search"] = ConnectionError("down")
    result = sources.news("BTC")
    assert result["ok"] is False and "8001" in result["reason"]


def test_bars_use_the_grid_venue_not_a_research_feed(sources, fetch):
    sources.bars("BTC", "4h")
    sources.bars("XAU", "1d")
    urls = [u for u, _ in fetch.calls]
    assert any("dashboard-control/market-bars" in u and "hyperliquid.testnet" in u for u in urls)
    assert any("dualtrack/market/bars" in u and "XAUUSDT" in u for u in urls)
    assert not any("8100" in u or "GC%3DF" in u for u in urls)


def test_two_accounts_stay_separate(sources):
    btc = sources.btc_account()
    xau = sources.xau_paper()["account"]
    assert btc["equity"] == 995.31 and btc["realized"] == 0.0342  # PAXG fills excluded
    assert xau["realized"] == 64.39 and xau["unrealized"] == -142.61 and xau["reconciliation"] == "ok"


def test_btc_grid_reads_latest_lifecycle(sources):
    grid = sources.btc_grid()
    assert grid["status_label"].startswith("价格高于区间") and grid["open_orders"] == 5 and grid["notional_per_rung"] == 18.5


def test_plan_keeps_running_grid_in_same_direction(sources):
    grid = sources.btc_grid()
    plan = plans.build_plan("BTC", "long", 77862.5, grid)
    assert plan["kind"] == "keep" and plan["rungs"][0] == 74000.0 and plan["hard_stop"] == 72000.0


def test_opposite_plan_is_new_and_stop_is_beyond_range(sources):
    plan = plans.build_plan("BTC", "short", 77862.5, sources.btc_grid())
    assert plan["kind"] == "new"
    assert all(77862.5 <= r <= plan["range"][1] for r in plan["rungs"]) and plan["hard_stop"] > plan["range"][1]
    assert plan["max_loss"] > 0


def test_plan_without_price_cannot_be_approved(config, sources, store, fetch):
    fetch.routes["/api/park-paper/read-model"] = ConnectionError("down")
    client = TestClient(create_app(config, sources, store))
    response = client.post("/api/judgments", json={"asset": "XAU", "direction": "long", "confidence": 3, "action": "approved"})
    assert response.status_code == 409


def test_review_outcomes():
    assert review.outcome("long", 1.2) == "hit"
    assert review.outcome("long", -0.8) == "miss"
    assert review.outcome("short", -0.6) == "hit"
    assert review.outcome("long", 0.2) == "even"
    assert review.outcome("flat", 1.5) == "hit" and review.outcome("flat", -2.5) == "miss"


def test_judgment_flow_and_review_resolution(config, sources, store):
    client = TestClient(create_app(config, sources, store))
    saved = client.post("/api/judgments", json={"asset": "BTC", "direction": "long", "confidence": 4, "reason": "回踩支撑",
                                                "cited": [{"id": 1, "title": "x"}], "action": "approved"}).json()
    assert saved["action"] == "approved" and saved["plan"]["kind"] == "keep" and "没有下单" in saved["handoff"]
    desk = client.get("/api/desk/BTC").json()
    assert desk["steps"]["judge"] and desk["steps"]["approve"]
    old = "2026-09-05T00:00:00+00:00"
    store.add_judgment(asset="BTC", direction="short", confidence=2, reason="old", cited=[], price_at=102.0, plan=None, action="recorded", created_at=old)
    body = client.get("/api/review").json()
    resolved = [i for i in body["items"] if i["reason"] == "old"][0]
    assert resolved["outcome"] == "miss" and resolved["price_after"] > 102.0


def test_notes_and_health(config, sources, store):
    client = TestClient(create_app(config, sources, store))
    assert client.get("/api/health").json()["submits_orders"] is False
    assert client.post("/api/notes", json={"asset": "XAU", "body": "  金价守 4300  "}).json()["body"] == "金价守 4300"
    assert client.get("/api/desk/XAU").json()["notes"][0]["body"] == "金价守 4300"
    assert client.get("/").status_code == 200
