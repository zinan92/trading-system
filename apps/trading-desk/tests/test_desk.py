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


def test_news_rolls_newest_first_for_the_asset_and_drops_reddit(sources, btc):
    news = sources.news(btc)
    assert news["ok"] is True and news["rolling"] is True
    assert [i["id"] for i in news["items"]] == [12, 11]  # newest first; Apple (not BTC) and reddit dropped; duplicate title once
    assert news["items"][0]["topic"] == "BTC" and news["items"][1]["topic"] == "宏观"


def test_news_serves_last_good_list_when_intel_fails(sources, fetch, xau):
    assert sources.news(xau)["ok"]
    for key in list(sources._news_cache):
        sources._news_cache[key] = (0.0, sources._news_cache[key][1])
    fetch.routes["/api/ui/realtime"] = ConnectionError("down")
    fetch.routes["/api/articles/search"] = ConnectionError("down")
    stale = sources.news(xau)
    assert stale["ok"] is True and stale["stale"] is True and stale["items"]


def test_news_down_without_cache_is_explicit(sources, fetch, btc):
    fetch.routes["/api/ui/realtime"] = ConnectionError("down")
    fetch.routes["/api/articles/search"] = ConnectionError("down")
    result = sources.news(btc)
    assert result["ok"] is False and "8001" in result["reason"]


def test_asset_the_feed_does_not_map_falls_back_to_keyword_search(sources, btc):
    unmapped = {**btc, "key": "DOGE", "label": "DOGE", "kline_key": None, "news_queries": ["比特币"], "primary": ["比特币"]}
    news = sources.news(unmapped)
    assert news["ok"] is True and "rolling" not in news and news["items"]


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
    eth = store.asset("ETH")  # seeded with the 09-15 assets
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


GOLD_DRAFT = {"status": "draft", "confirmable": True, "message": "ok",
              "draft": {"draft_id": "draft-1", "plan_digest": "sha256:gold",
                        "plan": {"risk": {"theoretical_max_loss": 270.0, "grid_rung_prices": [4150.0, 4300.0],
                                          "maximum_notional": 1700.0, "grid_entry_range": {"lower": 4150.0, "upper": 4300.0}}}}}


def _gold_chat(fetch, *, pending=None):
    original = fetch.__call__
    chat = []

    def call(url, body):
        if url.endswith("/api/park-paper/ai-chat"):
            fetch.calls.append((url, body))
            chat.append(body)
            if body is None:
                return {"pending_draft": pending}
            return {"message": {**GOLD_DRAFT}, "reject": {"status": "rejected"}, "confirm": {"status": "confirmed", "snapshot": {"snapshot_id": "s1"}}}[body["action"]]
        return original(url, body)

    fetch.__class__ = type("GoldFetch", (fetch.__class__,), {"__call__": lambda self, url, body: call(url, body)})
    return chat


def test_gold_preview_refused_while_a_gold_grid_runs(config, sources, store, fetch):
    client, _ex = client_for(config, sources, store, fetch)
    chat = _gold_chat(fetch)
    saved = client.post("/api/judgments", json={"asset": "XAU", "direction": "short", "confidence": 3, "action": "approved"}).json()
    assert saved["plan"]["executable"] is True
    assert saved["preview"]["execution_ready"] is False and "已经有网格在跑" in saved["preview"]["blockers"][0]
    assert chat == []


def test_gold_approval_drafts_only_and_execute_confirms_once(config, sources, store, fetch):
    fetch.routes["/api/park-paper/read-model"] = {"strategy": {"active": False}, "market": {"price": 4304.0}, "terminal": {}}
    client, ex = client_for(config, sources, store, fetch)
    chat = _gold_chat(fetch, pending={"draft_id": "draft-old"})
    saved = client.post("/api/judgments", json={"asset": "XAU", "direction": "long", "confidence": 3, "action": "approved"}).json()
    plan = saved["plan"]
    assert plan["range"][0] < 4304.0 < plan["range"][1]  # the paper engine needs the price inside the range
    assert saved["preview"]["execution_ready"] is True and saved["preview"]["max_loss"] == 270.0
    assert [b and b["action"] for b in chat] == [None, "reject", "message"]
    assert "做多网格" in chat[-1]["message"] and "XAUUSDT Paper" in chat[-1]["message"]
    assert not any(b and b.get("action") == "confirm" for b in chat)

    ok = client.post("/api/execute", json={"judgment_id": saved["id"], "shown_max_loss": 270.0, "confirm_text": "执行"}).json()
    confirms = [b for b in chat if b and b.get("action") == "confirm"]
    assert ok["started"] is True and "纸面盘" in ok["message"]
    assert confirms == [{"action": "confirm", "draft_id": "draft-1", "plan_digest": "sha256:gold"}]
    assert ex.driver_calls == []  # never the Testnet driver


# ---- assets, newsletters, system ---------------------------------------------------------
def test_add_and_remove_asset_from_catalog(config, sources, store, fetch):
    client, _ = client_for(config, sources, store, fetch)
    assert client.delete("/api/assets/ETH").json()["ok"] is True  # ETH is seeded; take it out to add it back
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


def test_morning_brief_opens_with_yesterdays_calls_and_todays_buttons(config, sources, store, fetch):
    config.kline_archive.mkdir()
    (config.kline_archive / "2026-09-14-kline-daily-newsletter.article.json").write_text(json.dumps({"blocks": [
        {"asset_key": "bitcoin", "type": "asset_heading", "display_name": "BTC 永续"},
        {"asset_key": "bitcoin", "type": "asset_summary", "structure": "结构：日线趋势延续，方向偏空。", "synthesis": "BTC 偏弱。等反弹。"}]}, ensure_ascii=False))
    config.morning_archive.mkdir()
    (config.morning_archive / "2026-09-14.html").write_text('<html><body><nav class="nav">晨报</nav><div class="digest">摘要</div></body></html>')
    client, _ex = client_for(config, sources, store, fetch)
    client.post("/api/judgments", json={"asset": "BTC", "direction": "short", "confidence": 4, "reason": "跌破7.7万", "action": "recorded"})
    page = client.get("/newsletter/morning").text
    assert page.index('class="nav"') < page.index("昨天判断对了吗") < page.index('class="digest"')
    assert 'data-asset="BTC" data-d="short" aria-pressed="true"' in page  # today's call is shown as pressed
    assert "K 线日报 ▼ 偏空" in page and "还没有判断记录" in page
    assert client.get("/newsletter/card", follow_redirects=False).headers["location"] == "/newsletter/morning"


def test_kline_daily_and_weekly_render_as_direction_cards(config, sources, store, fetch):
    from trading_desk import digest
    assert digest.direction_of("日线趋势延续，方向偏多。") == "long"
    assert digest.direction_of("不同周期存在分歧，需要等待确认。", "赔率不利：做空方向") == "wait"
    assert digest.direction_of("各周期均显示趋势减弱，方向偏空。") == "short"
    config.kline_archive.mkdir()
    img = "b" * 64 + ".png"
    (config.kline_archive / "2026-09-15-kline-daily-newsletter.article.json").write_text(json.dumps({"blocks": [
        {"asset_key": "dxy", "type": "asset_summary", "structure": "结构：方向偏多。", "synthesis": "美元偏强。后面还有很多话。"},
        {"asset_key": "gold", "type": "asset_heading", "display_name": "黄金"},
        {"asset_key": "gold", "type": "image", "label": "日线", "path": f"snapshots/{img}"},
        {"asset_key": "gold", "type": "asset_summary", "structure": "结构：分歧。", "synthesis": "黄金等待。"}]}, ensure_ascii=False))
    config.weekly_latest_html.write_text("<h1>原版周报</h1>")
    config.weekly_latest_html.with_suffix(".md").write_text(
        "# 宏观 K 线周报｜2026-09-04\n\n## 实物资产\n\n### 黄金期货（GC=F）\n\n"
        f"![黄金｜周线 K 线图](snapshots/{img})\n\n**周线**：周线在高位。\n\n**结构**：结构：各周期均显示趋势减弱，方向偏多。\n"
        "**综合结论**：黄金中期仍强。短线等回踩。\n\n## 本周机会清单\n\n- 黄金期货（GC=F）：参与\n", encoding="utf-8")
    client, _ = client_for(config, sources, store, fetch)
    daily = client.get("/newsletter/kline").text
    assert daily.index("黄金") < daily.index("dxy")  # Park's assets first
    assert "▲ 偏多" in daily and "美元偏强。</p>" in daily and f"/newsletter-asset/kline/{img}" in daily and "看原版全文" in daily
    weekly = client.get("/newsletter/weekly").text
    assert "实物资产" in weekly and "▲ 偏多" in weekly and "v-参与" in weekly and f"/newsletter-asset/weekly/{img}" in weekly
    assert "原版周报" in client.get("/newsletter/weekly-full").text
    assert client.get("/newsletter-asset/weekly/..%2Fx.png").status_code == 404


def test_trade_page_is_gridmind_behind_the_desk_gate(config, sources, store, fetch, tmp_path, monkeypatch):
    import dataclasses, os
    from trading_desk import gridmind
    calls = []

    def upstream(url, method, body, content_type, timeout):
        calls.append((url, method, body))
        if url.endswith("/dashboard-v5.html"):
            return 200, "<html><head><title>Extended 网格交易机器人 · GridMind 视觉版</title></head><body>GRID</body></html>".encode(), "text/html"
        return 200, b'{"ok": true}', "application/json"

    monkeypatch.setattr(gridmind, "urllib_fetch", upstream)
    secret = tmp_path / "passcode"
    secret.write_text("orchid-47")
    os.chmod(secret, 0o600)
    cfg = dataclasses.replace(config, remote_passcode=secret)
    client = TestClient(create_app(cfg, sources, store, FakeExecutor(cfg, fetch)), base_url="https://testserver")
    assert client.get("/", follow_redirects=False).headers["location"] == "/trade"
    page = client.get("/trade").text
    assert "GRID" in page and "/static/gm-desk.js" in page and "<title>Park 交易台 · 交易</title>" in page
    assert client.get("/api/trading-system/read-model?x=1").json() == {"ok": True}
    assert calls[-1] == ("http://dash/api/trading-system/read-model?x=1", "GET", None)
    assert client.get("/api/trading-system/secrets").status_code == 404
    assert client.get("/api/auth/session").json()["can_control"] is True
    tunnel = {"cf-connecting-ip": "203.0.113.9"}
    assert client.get("/trade", headers=tunnel, follow_redirects=False).headers["location"] == "/login"
    assert client.post("/api/dashboard-control/control", json={"action": "stop"}, headers=tunnel).status_code == 401
    client.post("/login", data={"passcode": "orchid-47"}, headers=tunnel)
    assert client.post("/api/dashboard-control/control", json={"action": "stop"}, headers=tunnel).status_code == 403  # no Origin
    sent = client.post("/api/dashboard-control/control", json={"action": "stop"}, headers={**tunnel, "origin": "https://testserver"})
    assert sent.status_code == 200 and calls[-1][0] == "http://dash/api/dashboard-control/control" and calls[-1][1] == "POST"
    monkeypatch.setattr(gridmind, "urllib_fetch", lambda *a: (_ for _ in ()).throw(ConnectionError("down")))
    down = TestClient(create_app(cfg, sources, store, FakeExecutor(cfg, fetch)))
    assert down.get("/trade").status_code == 503 and "交易后台暂时连不上" in down.get("/trade").text


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


def test_park_changes_the_passcode_himself_and_old_one_stops_working(config, sources, store, fetch, tmp_path):
    import dataclasses, os
    secret = tmp_path / "passcode"
    secret.write_text("orchid-47")
    os.chmod(secret, 0o600)
    cfg = dataclasses.replace(config, remote_passcode=secret)
    client = TestClient(create_app(cfg, sources, store, FakeExecutor(cfg, fetch)), base_url="https://testserver")
    tunnel = {"cf-connecting-ip": "203.0.113.9"}
    assert client.get("/passcode", headers=tunnel, follow_redirects=False).status_code == 303  # must log in first
    client.post("/login", data={"passcode": "orchid-47"}, headers=tunnel)
    assert client.post("/passcode", data={"current": "nope", "new": "park888", "again": "park888"}, headers=tunnel).status_code == 401
    assert client.post("/passcode", data={"current": "orchid-47", "new": "park888", "again": "park889"}, headers=tunnel).status_code == 400
    done = client.post("/passcode", data={"current": "orchid-47", "new": "park888", "again": "park888"}, headers=tunnel)
    assert done.status_code == 200 and "已改好" in done.text
    assert secret.read_text().strip() == "park888" and (secret.stat().st_mode & 0o077) == 0
    assert client.get("/api/health", headers=tunnel).status_code == 200  # this phone keeps its session
    fresh = TestClient(create_app(cfg, sources, store, FakeExecutor(cfg, fetch)), base_url="https://testserver")
    assert fresh.post("/login", data={"passcode": "orchid-47"}, headers=tunnel).status_code == 401
    assert fresh.post("/login", data={"passcode": "park888"}, headers=tunnel, follow_redirects=False).status_code == 303


def test_kline_rerun_with_hash_suffix_wins_over_the_earlier_issue(config, sources, store, fetch):
    import os
    from trading_desk import digest
    config.kline_archive.mkdir()
    first = config.kline_archive / "2026-09-15-kline-daily-newsletter.article.json"
    rerun = config.kline_archive / "2026-09-15-kline-daily-newsletter-bf2a95935487.article.json"
    placeholder = config.kline_archive / "2026-09-16-kline-daily-newsletter-unavailable.article.json"
    for path, text in ((first, "旧版"), (rerun, "新版"), (placeholder, "占位")):
        path.write_text(json.dumps({"blocks": [{"asset_key": "bitcoin", "type": "asset_summary", "structure": "结构：方向偏多。", "synthesis": text}]}, ensure_ascii=False))
    os.utime(first, (1_800_000_000, 1_800_000_000))
    os.utime(rerun, (1_800_030_000, 1_800_030_000))
    assert digest.latest_kline_file(config.kline_archive, ".article.json") == rerun
    client, _ = client_for(config, sources, store, fetch)
    assert client.get("/api/desk/BTC").json()["kline"]["synthesis"] == "新版"
    assert "新版" in client.get("/newsletter/kline").text


def test_changing_todays_call_revises_it_instead_of_adding_a_second_one(config, sources, store, fetch):
    client, _ = client_for(config, sources, store, fetch)
    first = client.post("/api/judgments", json={"asset": "BTC", "direction": "long", "confidence": 3, "action": "recorded"}).json()
    again = client.post("/api/judgments", json={"asset": "BTC", "direction": "short", "confidence": 4, "reason": "改主意", "action": "recorded"}).json()
    assert again["id"] == first["id"] and again["direction"] == "short" and again["reason"] == "改主意"
    assert len(store.judgments("BTC")) == 1


def test_park_pauses_and_resumes_grid_rearming_from_the_desk(config, sources, store, fetch, paper_output):
    client, _ = client_for(config, sources, store, fetch)
    assert client.get("/api/desk/BTC").json()["control"]["rearm_paused"] is False
    held = client.post("/api/grid/rearm-hold", json={"asset": "BTC", "paused": True})
    assert held.status_code == 200 and "已暂停补单" in held.json()["message"]
    holds = json.loads((paper_output / "testnet_automation" / "operator_holds.json").read_text())
    assert holds["BTC-USD-PERP"]["rearm_paused"] is True and holds["BTC-USD-PERP"]["by"] == "park"
    assert client.get("/api/desk/BTC").json()["control"]["rearm_paused"] is True
    assert client.post("/api/grid/rearm-hold", json={"asset": "BTC", "paused": False}).json()["paused"] is False
    assert client.post("/api/grid/rearm-hold", json={"asset": "XAU", "paused": True}).status_code == 409
    assert [e["stage"] for e in store.executions("BTC")][:2] == ["rearm_release", "rearm_hold"]
    (paper_output / "testnet_automation" / "operator_holds.json").write_text("{broken")
    assert client.get("/api/desk/BTC").json()["control"]["rearm_paused"] is True  # unreadable reads as held, like the lifecycle


def test_v4_assets_are_seeded_once_and_old_databases_accept_judgment_only_assets(tmp_path, config, fetch):
    import sqlite3
    from trading_desk.sources import Sources
    from trading_desk.store import Store
    old = tmp_path / "old.db"
    with sqlite3.connect(old) as conn:  # the pre-09-15 schema: kind limited to two values, no meta table
        conn.executescript("""create table assets (key text primary key, label text not null,
          kind text not null check (kind in ('hl_testnet','xau_paper')), venue_profile_id text not null, instrument_id text not null,
          coin text not null, news_queries text not null default '[]', primary_words text not null default '[]', kline_key text,
          position integer not null default 100, created_at text not null);
          insert into assets values ('BTC','BTC','hl_testnet','hyperliquid.testnet','BTC-USD-PERP','BTC','[]','[]','bitcoin',1,'2026-09-14');""")
    store = Store(old)
    assert [a["key"] for a in store.assets()] == ["BTC", "ETH", "SILVER", "WTI"]
    store.remove_asset("WTI")
    assert "WTI" not in [a["key"] for a in Store(old).assets()]  # not re-added once seeded
    fetch.routes["/api/overview"] = {"assets": [{"asset_key": "silver", "timeframes": [
        {"timeframe": "thirty_minute", "candles": [{"timestamp": "2026-09-15T00:00:00+00:00", "open": 41.0, "high": 41.5, "low": 40.8, "close": 41.2}]},
        {"timeframe": "daily", "candles": [{"timestamp": "2026-09-14", "open": 40.0, "high": 41.0, "low": 39.5, "close": 40.9}]}]}]}
    sources = Sources(config, fetch=fetch)
    silver = store.asset("SILVER")
    assert sources.bars(silver, "1h")["bars"][-1][4] == 41.2
    assert sources.bars(silver, "1d")["bars"][-1][0] == "2026-09-14T00:00:00+00:00"
    from tests.test_desk import client_for
    client, _ = client_for(config, sources, Store(config.db_path), fetch)
    plan = client.post("/api/plan", json={"asset": "SILVER", "direction": "short"}).json()
    assert plan["kind"] == "record_only" and "只记判断" in plan["title"]
    saved = client.post("/api/judgments", json={"asset": "SILVER", "direction": "short", "confidence": 3, "action": "recorded"}).json()
    assert saved["price_at"] == 41.2
    assert client.get("/api/desk/SILVER").json()["grid"]["ok"] is False


def test_stale_futures_candles_are_refreshed_by_the_background_job(config, fetch):
    from datetime import datetime, timezone
    from trading_desk.sources import Sources

    def overview(stamp):
        return {"assets": [{"asset_key": "silver", "timeframes": [{"timeframe": "thirty_minute", "candles": [
            {"timestamp": stamp, "open": 1, "high": 1, "low": 1, "close": 1}]}]}]}
    now = datetime(2026, 9, 15, 7, 0, tzinfo=timezone.utc)
    fetch.routes["/api/overview"] = overview("2026-09-15T06:30:00+00:00")
    sources = Sources(config, fetch=fetch)
    assert sources.refresh_futures(["silver"], now=now) == {"ok": True, "refreshed": False}
    assert not any("refresh=true" in url for url, _ in fetch.calls)
    fetch.routes["/api/overview"] = overview("2026-09-15T00:30:00+00:00")
    assert sources.refresh_futures(["silver"], now=now) == {"ok": True, "refreshed": True}
    assert any("refresh=true" in url for url, _ in fetch.calls)
