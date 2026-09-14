from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from trading_desk import plans, review
from trading_desk.app import create_app
from trading_desk.executor import ExecutionRefused, Executor


class FakeExecutor(Executor):
    def __init__(self, config, fetch):
        super().__init__(config, fetch, run=self._run)
        self.driver_calls = []
        self.close_calls = []

    def _run(self, args, **kwargs):
        if "-c" in args:  # close_terminal
            self.close_calls.append(args)
            (self.config.paper_output / "testnet_automation" / "current.json").write_text(json.dumps({"status": "idle"}))
            return type("Done", (), {"returncode": 0, "stderr": ""})()
        self.driver_calls.append(args)
        receipt = args[args.index("--receipt") + 1]
        open(receipt, "w").write(json.dumps({"status": "grid_running"}))

    def _driver_env(self):
        return {"HYPERLIQUID_TESTNET_SECRET_FILE": "/s", "HYPERLIQUID_TESTNET_ACCOUNT_ADDRESS": "0xA", "HYPERLIQUID_TESTNET_RUNTIME_ID": "r", "TRADING_ORCHESTRATOR_RELEASE_SHA": "x"}


def client_for(config, sources, store, fetch):
    ex = FakeExecutor(config, fetch)
    return TestClient(create_app(config, sources, store, ex)), ex


def test_news_ranks_asset_news_within_bucket_and_drops_reddit(sources, btc):
    news = sources.news(btc)
    assert news["ok"] is True
    assert [i["id"] for i in news["items"]] == [2, 1]
    assert news["items"][1]["topic"] == "BTC"


def test_news_serves_last_good_list_when_intel_fails(sources, fetch, xau):
    assert sources.news(xau)["ok"]
    key = next(iter(sources._news_cache))
    sources._news_cache[key] = (0.0, sources._news_cache[key][1])
    fetch.routes["/api/articles/search"] = ConnectionError("down")
    stale = sources.news(xau)
    assert stale["ok"] is True and stale["stale"] is True and stale["items"]


def test_news_down_without_cache_is_explicit(sources, fetch, btc):
    fetch.routes["/api/articles/search"] = ConnectionError("down")
    result = sources.news(btc)
    assert result["ok"] is False and "8001" in result["reason"]


def test_bars_use_the_grid_venue_not_a_research_feed(sources, fetch, btc, xau):
    assert sources.bars(btc, "4h")["ok"]
    assert sources.bars(xau, "1d")["ok"]
    bodies = [b for _, b in fetch.calls if b]
    urls = [u for u, _ in fetch.calls]
    assert any(b.get("type") == "candleSnapshot" and b["req"]["coin"] == "BTC" for b in bodies)
    assert any("dualtrack/market/bars" in u and "XAUUSDT" in u for u in urls)
    assert not any("8100" in u or "GC%3DF" in u for u in urls)


def test_two_accounts_stay_separate(sources):
    hl = sources.hl_account()
    xau = sources.xau_paper()["account"]
    assert hl["equity"] == 995.31 and hl["realized"] == pytest.approx(-1.9658, abs=1e-3)
    assert xau["realized"] == 64.39 and xau["unrealized"] == -142.61 and xau["reconciliation"] == "ok"


def test_hl_grid_reads_matching_instrument(sources, btc, store):
    grid = sources.hl_grid(btc)
    assert grid["status_label"].startswith("价格高于区间") and grid["open_orders"] == 5 and grid["notional_per_rung"] == 18.5
    eth = store.add_hl_asset(coin="ETH", instrument_id="ETH-USD-PERP")
    none = sources.hl_grid(eth)
    assert none["ok"] is False and "还没有网格" in none["reason"]


def test_btc_grid_prefers_newest_updated_at_over_mtime(config, sources, btc):
    import os, time
    folder = config.paper_output / "dualtrack" / "grid_testnet_lifecycle"
    old = folder / "dashboard-plan:old.json"
    old.write_text(json.dumps([{"instrument_id": "BTC-USD-PERP", "status": "terminal", "updated_at": "2026-09-01T00:00:00+00:00", "rungs": [], "orders": []}]))
    os.utime(old, (time.time() + 60, time.time() + 60))
    assert sources.hl_grid(btc)["status"] == "paused_above_range"


def test_plan_keeps_running_grid_in_same_direction(sources, btc):
    plan = plans.build_plan("hl_testnet", "long", 77862.5, sources.hl_grid(btc))
    assert plan["kind"] == "keep" and plan["rungs"][0] == 74000.0 and plan["hard_stop"] == 72000.0


def test_opposite_plan_is_new_executable_and_stop_beyond_range(sources, btc):
    plan = plans.build_plan("hl_testnet", "short", 77862.5, sources.hl_grid(btc))
    assert plan["kind"] == "new" and plan["executable"] is True and plan["notional_per_rung"] == 19.0
    assert all(77862.5 <= r <= plan["range"][1] for r in plan["rungs"]) and plan["hard_stop"] > plan["range"][1]


def test_small_price_asset_gets_fine_ticks():
    plan = plans.build_plan("hl_testnet", "long", 3.2154, None)
    assert len(set(plan["rungs"])) == 5 and plan["hard_stop"] < plan["range"][0]


def test_review_outcomes():
    assert review.outcome("long", 1.2) == "hit"
    assert review.outcome("long", -0.8) == "miss"
    assert review.outcome("short", -0.6) == "hit"
    assert review.outcome("long", 0.2) == "even"
    assert review.outcome("flat", 1.5) == "hit" and review.outcome("flat", -2.5) == "miss"


def test_price_at_refuses_stale_bar_and_flags_unverifiable():
    bars = [["2026-09-10T00:00:00+00:00", 1, 1, 1, 100.0], ["2026-09-10T01:00:00+00:00", 1, 1, 1, 101.0]]
    assert review.price_at(bars, datetime(2026, 9, 10, 1, 30, tzinfo=timezone.utc)) == 101.0
    assert review.price_at(bars, datetime(2026, 9, 12, tzinfo=timezone.utc)) is None
    assert review.out_of_window(bars, datetime(2026, 9, 1, tzinfo=timezone.utc)) is True


def test_plan_without_price_cannot_be_approved(config, sources, store, fetch):
    fetch.routes["/api/park-paper/read-model"] = ConnectionError("down")
    fetch.routes["/api/dualtrack/market/bars"] = ConnectionError("down")
    client, _ = client_for(config, sources, store, fetch)
    assert client.post("/api/judgments", json={"asset": "XAU", "direction": "long", "confidence": 3, "action": "approved"}).status_code == 409


def test_review_resolution(config, sources, store, fetch):
    client, _ = client_for(config, sources, store, fetch)
    store.add_judgment(asset="XAU", direction="short", confidence=2, reason="old", cited=[], price_at=102.0, plan=None, action="recorded", created_at="2026-09-05T00:00:00+00:00")
    store.add_judgment(asset="XAU", direction="long", confidence=3, reason="ancient", cited=[], price_at=101.0, plan=None, action="recorded", created_at="2026-08-01T00:00:00+00:00")
    items = {i["reason"]: i for i in client.get("/api/review").json()["items"]}
    assert items["old"]["outcome"] == "miss" and items["old"]["price_after"] > 102.0
    assert items["ancient"]["outcome"] == "unverifiable"


# ---- the execution gate -------------------------------------------------------------
PREVIEW_OK = {"preview": {"execution_ready": True, "blockers": [], "preview_digest": "sha256:abc",
                          "preview": {"range": {"low": 1, "high": 2}, "orders": [{"price": 78563, "notional": 19}], "risk": {"max_loss": 4.2}}}}


def _approve_short(client):
    return client.post("/api/judgments", json={"asset": "BTC", "direction": "short", "confidence": 3, "reason": "t", "action": "approved"}).json()


def test_approval_previews_but_never_confirms_or_drives(config, sources, store, fetch):
    fetch.routes["/api/dashboard-control/preview"] = PREVIEW_OK
    fetch.routes["/api/dashboard-control/confirm"] = AssertionError("confirm must not be called on approval")
    client, ex = client_for(config, sources, store, fetch)
    saved = _approve_short(client)
    assert saved["preview"]["execution_ready"] is True and "不按不会下单" in saved["handoff"]
    assert ex.driver_calls == []
    assert not any("confirm" in u for u, _ in fetch.calls)


def test_execute_refuses_while_a_grid_is_live(config, sources, store, fetch, paper_output):
    (paper_output / "testnet_automation").mkdir(parents=True)
    (paper_output / "testnet_automation" / "current.json").write_text(json.dumps({"status": "grid_paused_range"}))
    fetch.routes["/api/dashboard-control/preview"] = PREVIEW_OK
    client, ex = client_for(config, sources, store, fetch)
    saved = _approve_short(client)
    response = client.post("/api/execute", json={"judgment_id": saved["id"], "shown_max_loss": 4.2, "confirm_text": "执行"})
    assert response.status_code == 409 and "已经有网格在跑" in response.json()["detail"]
    assert ex.driver_calls == []


def test_execute_confirms_as_park_and_drives_once_when_idle(config, sources, store, fetch, paper_output):
    (paper_output / "testnet_automation").mkdir(parents=True)
    (paper_output / "testnet_automation" / "current.json").write_text(json.dumps({"status": "idle"}))
    fetch.routes["/api/dashboard-control/preview"] = PREVIEW_OK
    fetch.routes["/api/dashboard-control/confirm"] = {"confirmation": {"status": "confirmed", "activation_id": "sha256:act"}}
    client, ex = client_for(config, sources, store, fetch)
    saved = _approve_short(client)
    ok = client.post("/api/execute", json={"judgment_id": saved["id"], "shown_max_loss": 4.2, "confirm_text": "执行"}).json()
    assert ok["started"] is True and len(ex.driver_calls) == 1
    confirm_body = next(b for u, b in fetch.calls if "confirm" in u)
    assert confirm_body["confirmation"]["operator_id"] == "park" and "亲自按下" in confirm_body["confirmation"]["statement"]
    again = client.post("/api/execute", json={"judgment_id": saved["id"], "shown_max_loss": 4.2, "confirm_text": "执行"})
    assert again.status_code == 409 and len(ex.driver_calls) == 1


def test_execute_refuses_when_loss_grew_since_shown(config, sources, store, fetch, paper_output):
    (paper_output / "testnet_automation").mkdir(parents=True)
    (paper_output / "testnet_automation" / "current.json").write_text(json.dumps({"status": "idle"}))
    fetch.routes["/api/dashboard-control/preview"] = PREVIEW_OK
    client, ex = client_for(config, sources, store, fetch)
    saved = _approve_short(client)
    response = client.post("/api/execute", json={"judgment_id": saved["id"], "shown_max_loss": 3.0, "confirm_text": "执行"})
    assert response.status_code == 409 and ex.driver_calls == []


def test_execute_requires_explicit_confirm_text_and_approved_plan(config, sources, store, fetch):
    client, ex = client_for(config, sources, store, fetch)
    recorded = client.post("/api/judgments", json={"asset": "BTC", "direction": "short", "confidence": 3, "action": "recorded"}).json()
    assert client.post("/api/execute", json={"judgment_id": recorded["id"], "confirm_text": "执行"}).status_code == 409
    assert client.post("/api/execute", json={"judgment_id": recorded["id"], "confirm_text": "yes"}).status_code == 422
    assert ex.driver_calls == []


def test_gold_is_never_executable(config, sources, store, fetch):
    client, ex = client_for(config, sources, store, fetch)
    saved = client.post("/api/judgments", json={"asset": "XAU", "direction": "short", "confidence": 3, "action": "approved"}).json()
    assert saved["plan"]["executable"] is False and "preview" not in saved
    with pytest.raises(ExecutionRefused):
        ex.preview(store.asset("XAU"), saved["plan"])


# ---- assets, newsletters, system ---------------------------------------------------------
def test_add_and_remove_asset_from_catalog(config, sources, store, fetch):
    client, _ = client_for(config, sources, store, fetch)
    catalog = client.get("/api/catalog").json()
    assert {i["coin"]: i["added"] for i in catalog["instruments"]} == {"BTC": True, "ETH": False}
    added = client.post("/api/assets", json={"coin": "eth", "news_queries": ["以太坊", "ETH"]}).json()
    assert added["key"] == "ETH" and added["kline_key"] == "ethereum"
    assert client.post("/api/assets", json={"coin": "DOGEX"}).status_code == 422
    assert client.put("/api/assets/ETH/news", json={"news_queries": ["以太坊"]}).json()["news_queries"] == ["以太坊"]
    desk = client.get("/api/desk/ETH").json()
    assert desk["grid"]["ok"] is False and desk["price"] == 114.0
    assert client.delete("/api/assets/ETH").json()["ok"] is True
    assert client.get("/api/desk/ETH").status_code == 404


def test_kline_view_and_newsletters(config, sources, store, fetch):
    config.kline_archive.mkdir()
    (config.kline_archive / "2026-09-14-kline-daily-newsletter.article.json").write_text(json.dumps({"cutoff_at": "x", "blocks": [
        {"asset_key": "bitcoin", "type": "asset_summary", "position": "位置：中位", "structure": "结构：分歧", "synthesis": "等确认", "odds": "赔率尚未形成。"},
        {"asset_key": "bitcoin", "type": "period_text", "label": "日线", "text": "日线在 EMA50 上方"}]}, ensure_ascii=False))
    config.morning_archive.mkdir()
    config.morning_latest.write_text("<h1>晨报</h1>")
    (config.morning_archive / "2026-09-13.html").write_text("<h1>旧</h1>")
    client, _ = client_for(config, sources, store, fetch)
    view = client.get("/api/desk/BTC").json()["kline"]
    assert view["ok"] and view["structure"] == "结构：分歧" and view["periods"][0]["label"] == "日线"
    assert client.get("/api/desk/XAU").json()["kline"]["ok"] is False
    assert "旧" in client.get("/newsletter/morning").text  # newest dated brief, not the redirect stub
    assert "旧" in client.get("/newsletter/2026-09-13").text
    assert client.get("/newsletter/..%2Fetc").status_code == 404
    assert client.get("/api/newsletters").json()["morning"]["archive"] == ["2026-09-13"]


def test_system_lists_paused_services_and_health(config, sources, store, fetch):
    config.paused_manifest.write_text("| label | a | b |\n|---|---|---|\n| com.park.market-regime.web | 市场状态网页 | DEMO 数据 |\n")
    client, _ = client_for(config, sources, store, fetch)
    body = client.get("/api/system").json()
    assert body["paused"] == [{"label": "com.park.market-regime.web", "what": "市场状态网页", "why": "DEMO 数据"}]
    assert any(c["name"].startswith("新闻") and c["ok"] for c in body["checks"])
    health = client.get("/api/health").json()
    assert health["submits_orders"] is True and health["requires_human_press"] is True


def test_ended_gold_grid_reads_as_ended_not_broken(config, sources, store, fetch):
    fetch.routes["/api/park-paper/read-model"] = {"status": "blocked", "market": {"price": 4298.0}, "strategy": {"active": False, "state": "IDLE_CLEAN"},
        "terminal": {"reason": "lower_boundary_invalidated", "observed_price": 4298.42, "exit_receipts": [{"ts": "2026-09-14T09:19:35+00:00"}] * 10}, "execution": {}}
    client, _ = client_for(config, sources, store, fetch)
    desk = client.get("/api/desk/XAU").json()
    assert desk["grid"]["ok"] is False and "跌破区间下沿" in desk["grid"]["reason"] and desk["price"] == 4298.0
    account = client.get("/api/accounts").json()["accounts"][1]
    assert account["ok"] is True and "网格已结束" in account["note"]
    assert client.post("/api/plan", json={"asset": "XAU", "direction": "long"}).json()["kind"] == "new"


def test_kline_page_renders_archive_markdown_with_local_images(config, sources, store, fetch):
    config.kline_archive.mkdir(exist_ok=True)
    img = "a" * 64 + ".png"
    (config.kline_archive / "2026-09-14-kline-daily-newsletter.md").write_text(f"---\ntitle: x\n---\n# 宏观 K 线日报\n\n![图](snapshots/{img})\n", encoding="utf-8")
    client, _ = client_for(config, sources, store, fetch)
    page = client.get("/newsletter/kline").text
    assert "宏观 K 线日报" in page and f"/newsletter-asset/kline/{img}" in page
    assert client.get("/newsletter-asset/kline/..%2F..%2Fsecret.png").status_code == 404


def _coordinator(paper_output, **state):
    (paper_output / "testnet_automation").mkdir(parents=True, exist_ok=True)
    (paper_output / "testnet_automation" / "current.json").write_text(json.dumps(state))


def test_stop_grid_needs_the_typed_word_and_records_parks_press(config, sources, store, fetch, paper_output):
    _coordinator(paper_output, status="grid_paused_range", selected_instrument_id="BTC-USD-PERP")
    fetch.routes["/api/dashboard-control/control"] = {"control": {"status": "stop_requested", "blockers": []}}
    client, ex = client_for(config, sources, store, fetch)
    assert client.get("/api/desk/BTC").json()["control"]["can_stop"] is True
    assert client.post("/api/grid/stop", json={"asset": "BTC", "confirm_text": "ok"}).status_code == 422
    assert not any("control" in u for u, _ in fetch.calls)

    done = client.post("/api/grid/stop", json={"asset": "BTC", "confirm_text": "停止"}).json()
    body = next(b for u, b in fetch.calls if u.endswith("/api/dashboard-control/control"))
    assert done["status"] == "stop_requested" and body["action"] == "stop" and "亲自按下" in body["reason"]
    assert store.executions("BTC", limit=1)[0]["stage"] == "stop_requested"
    assert ex.driver_calls == []

    _coordinator(paper_output, status="stop_requested", selected_instrument_id="BTC-USD-PERP")
    control = client.get("/api/desk/BTC").json()["control"]
    assert control["stopping"] is True and control["can_stop"] is False


def test_stop_grid_refuses_other_assets_and_idle_account(config, sources, store, fetch, paper_output):
    _coordinator(paper_output, status="idle")
    client, _ex = client_for(config, sources, store, fetch)
    assert client.post("/api/grid/stop", json={"asset": "BTC", "confirm_text": "停止"}).status_code == 409
    _coordinator(paper_output, status="grid_running", selected_instrument_id="BTC-USD-PERP")
    assert client.post("/api/grid/stop", json={"asset": "XAU", "confirm_text": "停止"}).status_code == 409
    assert not any("control" in u for u, _ in fetch.calls)


def test_execute_closes_an_ended_grid_before_starting_a_new_one(config, sources, store, fetch, paper_output):
    _coordinator(paper_output, status="grid_terminal", selected_instrument_id="BTC-USD-PERP")
    fetch.routes["/api/dashboard-control/preview"] = PREVIEW_OK
    fetch.routes["/api/dashboard-control/confirm"] = {"confirmation": {"status": "confirmed", "activation_id": "sha256:act"}}
    client, ex = client_for(config, sources, store, fetch)
    saved = _approve_short(client)
    ok = client.post("/api/execute", json={"judgment_id": saved["id"], "shown_max_loss": 4.2, "confirm_text": "执行"}).json()
    assert len(ex.close_calls) == 1 and ok["started"] is True


def test_review_card_shows_reading_call_and_verdict(config, sources, store, fetch):
    client, _ex = client_for(config, sources, store, fetch)
    client.post("/api/judgments", json={"asset": "BTC", "direction": "short", "confidence": 4, "reason": "跌破7.7万", "action": "recorded"})
    page = client.get("/newsletter/card")
    assert page.status_code == 200
    assert "Park 的交易复盘" in page.text and "看空" in page.text and "跌破7.7万" in page.text
    assert "今天还没下判断" in page.text  # XAU has no call today


def test_tunnel_requests_need_the_passcode_and_local_ones_do_not(config, sources, store, fetch, tmp_path):
    import dataclasses, os
    secret = tmp_path / "passcode"
    secret.write_text("orchid-47")
    os.chmod(secret, 0o600)
    cfg = dataclasses.replace(config, remote_passcode=secret)
    client = TestClient(create_app(cfg, sources, store, FakeExecutor(cfg, fetch)), base_url="https://testserver")
    tunnel = {"cf-connecting-ip": "203.0.113.9"}
    assert client.get("/api/health").status_code == 200  # the Mac itself
    assert client.get("/api/health", headers=tunnel).status_code == 401
    assert client.get("/", headers=tunnel, follow_redirects=False).headers["location"] == "/login"
    assert client.post("/login", data={"passcode": "wrong"}, headers=tunnel).status_code == 401
    ok = client.post("/login", data={"passcode": "orchid-47"}, headers=tunnel, follow_redirects=False)
    assert ok.status_code == 303 and "desk_session" in ok.headers["set-cookie"]
    assert client.get("/api/health", headers=tunnel).status_code == 200
    os.chmod(secret, 0o644)
    assert client.get("/api/health", headers=tunnel).status_code == 403
