"""「今天」 on the real /trade page (#1327): GridMind + gm-desk.js + the React islands, every API mocked.

Steps 1–2 are React; step 3 (执行, 停止, 暂停补单) is still gm-desk.js. These tests drive the
handoff between them through the page the desk serves (`inject_shell`), with routed endpoints only:
nothing here reaches a backend, and 执行 is answered by a mock that records the request body.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

from tests.test_dashboard_gridmind_order_lifecycle_browser import _read_model, _static_server

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "apps" / "trading-desk" / "src" / "trading_desk" / "static"
sys.path.insert(0, str(ROOT / "apps" / "trading-desk" / "src"))

ASSETS = {"assets": [
    {"key": "BTC", "label": "BTC", "kind": "hl_testnet", "instrument_id": "BTC-USD-PERP"},
    {"key": "XAU", "label": "黄金", "kind": "xau_paper", "instrument_id": "XAU-USDT-SWAP"},
    {"key": "ETH", "label": "ETH", "kind": "watch", "instrument_id": "ETH"},
]}
PLAN = {"kind": "new", "title": "做多网格", "lines": [["区间", "70,000 – 80,000"]], "note": "先给你看", "rungs": [72000, 74000]}
PREVIEW = {"execution_ready": True, "orders": [{"price": 72000}, {"price": 74000}], "max_loss": 12.5, "preview_digest": "sha256:x"}


def _page(runtime, origin: str, desk: dict, posts: list, dialogs: list, answers: dict | None = None, reads_extra: dict | None = None):
    from trading_desk.gridmind import inject_shell

    html = inject_shell((ROOT / "dashboard-gridmind.html").read_text(encoding="utf-8"))
    model = _read_model("accepted", full_orders=True)
    try:
        browser = runtime.chromium.launch(headless=True, channel="chrome")
    except Exception as exc:  # pragma: no cover - depends on local browser install
        pytest.skip(f"Playwright Chromium unavailable: {exc}")
    page = browser.new_page(viewport={"width": 1680, "height": 1050})
    page.on("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.accept()))

    def fulfill(route, body, status=200):
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body, ensure_ascii=False))

    def api(route) -> None:
        request = route.request
        path = request.url.split("://", 1)[1].split("/", 1)[1].split("?", 1)[0]
        path = "/" + path
        if request.method == "POST":
            posts.append((path, request.post_data))
            answered = {
                "/api/plan": (PLAN, 200),
                "/api/judgments": ({"id": 99, "handoff": "预览通过：确认没问题再按「执行」", "preview": PREVIEW}, 200),
                "/api/execute": ({"message": "已提交（mock）"}, 200),
                **(answers or {}),
            }
            body, status = answered.get(path, ({"detail": "not mocked"}, 500))
            return fulfill(route, body, status)
        reads = {
            "/api/assets": ASSETS,
            "/api/desk/BTC": desk,
            "/api/news/BTC": {"ok": True, "items": [{"id": "n1", "title": "比特币 ETF 净流入", "bucket": "high_impact", "source": "cls",
                                                     "collected_at": "2026-11-06T02:00:00Z"}]},
            "/api/watch": {"items": []},
            "/api/trading-system/read-model": model,
            **(reads_extra or {}),
        }
        if path in reads:
            return fulfill(route, reads[path])
        if path.startswith("/api/dualtrack/market"):
            return fulfill(route, {**model["market"], "bars": []})
        fulfill(route, {"detail": "not mocked"}, 503)

    page.route("**/api/**", api)
    page.route("**/trade", lambda route: route.fulfill(status=200, content_type="text/html", body=html))
    def static(name: str):
        kind = "text/css" if name.endswith(".css") else "application/javascript"
        return lambda route: route.fulfill(status=200, content_type=kind, body=(STATIC / name).read_text(encoding="utf-8"))

    for name in ("gm-desk.js", "gm-desk.css", "web/web.js"):
        page.route(f"**/static/{name}?*", static(name))
    page.goto(f"{origin}/trade", wait_until="load")
    # GridMind's control script only runs on its own ports, so publish the selection it would:
    # the console is on BTC (Hyperliquid Testnet).
    page.evaluate("() => { window.dashboardControlState = {catalog: {venue_profiles: []}, preview: null,"
                  " selection: {venue_profile_id: 'hyperliquid.testnet', instrument_id: 'BTC-USD-PERP'}}; }")
    page.locator("#deskJudgeTitle", has_text="今天 · BTC").wait_for()
    return browser, page


def _desk(**overrides) -> dict:
    desk = {"kline": {"ok": True, "structure": "日线偏多", "synthesis": "比特币修复。"}, "today_judgment": None, "executions": [],
            "ai_today": None, "ai_running": False, "control": {"can_stop": False}, "meta": {"kind": "hl_testnet", "capabilities": ["bars", "bars_health", "execute", "grid_plan", "live_price", "preview", "price_alerts", "rearm_hold", "stop"]}}
    desk.update(overrides)
    return desk


def test_today_flow_hands_an_approved_preview_to_the_legacy_execute_step() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    posts: list = []
    dialogs: list = []
    with _static_server() as origin, playwright.sync_playwright() as runtime:
        browser, page = _page(runtime, origin, _desk(), posts, dialogs)
        exec_box = page.locator("#deskExecBox")
        assert exec_box.inner_text() == "先完成前两步"
        page.locator("#deskNews li", has_text="比特币 ETF 净流入").click()
        page.locator(".desk-dir [data-d=long]").click()
        page.locator("#deskReason").fill("ETF 流入")
        page.get_by_role("button", name="批准这份计划").click()
        page.locator("#deskToast", has_text="预览通过").wait_for()
        exec_box.locator("#deskExec").wait_for()
        assert "72,000 / 74,000" in exec_box.inner_text() and "12.50 USDC" in exec_box.inner_text()
        assert page.locator('.desk-step[data-step="3"]').get_attribute("class").split() == ["desk-step", "on"]
        page.locator("#deskExec").click()
        page.locator("#deskExecToast", has_text="已提交（mock）").wait_for()
        assert posts == [
            ("/api/plan", '{"asset":"BTC","direction":"long"}'),
            ("/api/judgments", '{"asset":"BTC","direction":"long","confidence":3,"reason":"ETF 流入",'
                               '"cited":[{"id":"n1","title":"比特币 ETF 净流入","bucket":"high_impact"}],"action":"approved"}'),
            # 执行 is still gm-desk.js, body unchanged; answered by the mock above.
            ("/api/execute", '{"judgment_id":99,"shown_max_loss":12.5,"confirm_text":"执行"}'),
        ]
        assert dialogs and "最多亏 12.50 USDC" in dialogs[0]
        browser.close()


def test_the_rail_notes_sit_after_both_button_rows() -> None:
    """gm-desk.js anchors these on GridMind's markup, which React islands now host (#1397)."""
    playwright = pytest.importorskip("playwright.sync_api")
    with _static_server() as origin, playwright.sync_playwright() as runtime:
        browser, page = _page(runtime, origin, _desk(), [], [])
        assert page.locator(".gm-control-actions + .desk-rail-note").count() == 1
        assert page.locator(".strategy-card .robot-actions + .desk-rail-note").count() == 1
        browser.close()


def test_switching_asset_fires_intents_with_ids() -> None:
    """ADR 0008, #1396: the selects are React's. gm-desk.js asks GridMind, which stands in here as two spies."""
    playwright = pytest.importorskip("playwright.sync_api")
    posts: list = []
    with _static_server() as origin, playwright.sync_playwright() as runtime:
        browser, page = _page(runtime, origin, _desk(), posts, [], reads_extra={"/api/desk/XAU": _desk()})
        page.evaluate("""() => {
          const state = window.dashboardControlState; window.fired = [];
          state.catalog = {venue_profiles: [{id: 'okx.paper', environment: 'paper', instruments: [{asset: 'XAU', instrument_id: 'XAU-USDT-SWAP'}]}]};
          state.picker = {venue: 'hyperliquid.testnet', instruments: []};
          window.gmIntent.on('change', 'control.venue', event => { window.fired.push(['venue', event.args.id]); state.picker.instruments = [{value: 'XAU-USDT-SWAP'}]; });
          window.gmIntent.on('change', 'control.instrument', event => { window.fired.push(['instrument', event.args.id]);
            state.selection = {venue_profile_id: 'okx.paper', instrument_id: event.args.id}; });
        }""")
        page.locator('#deskAssets [data-asset="XAU"]').click()
        page.wait_for_function("() => window.fired.length === 2")
        assert page.evaluate("() => window.fired") == [["venue", "okx.paper"], ["instrument", "XAU-USDT-SWAP"]]
        browser.close()


def test_a_reload_of_an_executed_plan_shows_it_done_and_offers_no_execute_button() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    desk = _desk(
        today_judgment={"id": 5, "direction": "long", "reason": "早上判断", "confidence": 4, "cited": [], "action": "approved"},
        executions=[{"judgment_id": 5, "stage": "preview", "detail": deepcopy(PREVIEW)}, {"judgment_id": 5, "stage": "executed", "detail": {}}],
    )
    posts: list = []
    with _static_server() as origin, playwright.sync_playwright() as runtime:
        browser, page = _page(runtime, origin, desk, posts, [])
        page.locator("#deskExecBox", has_text="已执行：网格已挂上测试盘。").wait_for()
        assert page.locator("#deskExec").count() == 0
        assert page.locator("#deskReason").input_value() == "早上判断"
        assert [path for path, _ in posts] == ["/api/plan"]  # restoring re-plans; it never re-submits or executes
        browser.close()


def test_a_running_grid_keeps_its_stop_button_in_step_three() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    with _static_server() as origin, playwright.sync_playwright() as runtime:
        browser, page = _page(runtime, origin, _desk(control={"can_stop": True, "rearm_paused": False}), [], [])
        page.locator("#deskStopBtn").wait_for()
        assert page.locator("#deskHoldBtn").inner_text() == "暂停补单"
        browser.close()


# ---- #1380: the stop and hold buttons follow the venue's declared capabilities, not its kind ------
def test_without_the_stop_capability_there_is_no_stop_button() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    with _static_server() as origin, playwright.sync_playwright() as runtime:
        desk = _desk(control={"can_stop": True, "rearm_paused": False}, meta={"kind": "hl_testnet", "capabilities": ["bars", "grid_plan", "preview"]})
        browser, page = _page(runtime, origin, desk, [], [])
        page.locator("#deskJudgeTitle", has_text="今天 · BTC").wait_for()
        page.wait_for_timeout(300)
        assert page.locator("#deskStopBtn").count() == 0 and page.locator("#deskHoldBtn").count() == 0
        browser.close()


def test_stop_without_rearm_hold_shows_only_stop() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    with _static_server() as origin, playwright.sync_playwright() as runtime:
        desk = _desk(control={"can_stop": True, "rearm_paused": False}, meta={"kind": "anything", "capabilities": ["stop"]})
        browser, page = _page(runtime, origin, desk, [], [])
        page.locator("#deskStopBtn").wait_for()
        assert page.locator("#deskHoldBtn").count() == 0
        browser.close()


# ---- #1374: a blocked preview or a refused order shows where its judgment broke ----------------
WHY_PREVIEW = {"status": "broken", "first_problem": {"key": "preview", "label": "预览", "summary": "预览没通过：缺少实时行情"},
               "note": "这些是记录，不是交易所动作的证明。", "identity": {},
               "steps": [{"key": "preview", "label": "预览", "status": "failed", "summary": "预览没通过：缺少实时行情"},
                         {"key": "press", "label": "按下执行", "status": "not_reached", "summary": "没走到这一步"}]}
WHY_RESULT = {**WHY_PREVIEW, "first_problem": {"key": "result", "label": "下单结果", "summary": "确认没有通过，没有下单。"},
              "steps": [{"key": "preview", "label": "预览", "status": "ok", "summary": "预览通过"},
                        {"key": "result", "label": "下单结果", "status": "failed", "summary": "确认没有通过，没有下单。"}]}


def _judge(page) -> None:
    page.locator(".desk-dir [data-d=long]").click()
    page.get_by_role("button", name="批准这份计划").click()


def test_a_blocked_preview_shows_its_chain_under_step_three() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    blocked = {"id": 99, "handoff": "预览没有通过，不能执行：缺少实时行情", "preview": {"execution_ready": False, "blockers": ["缺少实时行情"]}}
    posts: list = []
    with _static_server() as origin, playwright.sync_playwright() as runtime:
        browser, page = _page(runtime, origin, _desk(), posts, [], answers={"/api/judgments": (blocked, 200)}, reads_extra={"/api/why": WHY_PREVIEW})
        requests: list = []
        page.on("request", lambda r: requests.append((r.method, r.url.split("/", 3)[3])) if "/api/why" in r.url else None)
        _judge(page)
        box = page.locator("#deskExecBox")
        box.locator(".desk-why-verdict", has_text="断在「预览」：预览没通过：缺少实时行情").wait_for()
        assert box.locator("#deskExec").count() == 0
        assert requests == [("GET", "api/why?judgment=99")]
        assert [path for path, _ in posts] == ["/api/plan", "/api/judgments"]
        browser.close()


def test_a_refused_order_shows_its_chain_after_the_press() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    posts: list = []
    with _static_server() as origin, playwright.sync_playwright() as runtime:
        browser, page = _page(runtime, origin, _desk(), posts, [], answers={"/api/execute": ({"detail": "确认没有通过，没有下单。"}, 409)},
                              reads_extra={"/api/why": WHY_RESULT})
        _judge(page)
        page.locator("#deskExec").click()
        page.locator("#deskExecToast", has_text="确认没有通过，没有下单。").wait_for()
        page.locator("#deskExecBox .desk-why-verdict", has_text="断在「下单结果」：确认没有通过，没有下单。").wait_for()
        assert [path for path, _ in posts] == ["/api/plan", "/api/judgments", "/api/execute"]  # one press, one request
        browser.close()
