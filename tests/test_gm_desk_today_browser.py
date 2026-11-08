"""「今天」 on the real /trade page (#1327): GridMind + gm-desk.js + the React islands, every API mocked.

Steps 1–2 are React; step 3 (执行, 停止, 暂停补单) is still gm-desk.js. These tests drive the
handoff between them through the page the desk serves (`render_trade_page`), with routed endpoints only:
nothing here reaches a backend, and 执行 is answered by a mock that records the request body.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

from tests.test_dashboard_gridmind_order_lifecycle_browser import _read_model, _static_server
from tests.trade_page import open_manual

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


BTC_SELECTION = "{venue_profile_id: 'hyperliquid.testnet', instrument_id: 'BTC-USD-PERP'}"


def _page(runtime, origin: str, desk: dict, posts: list, dialogs: list, answers: dict | None = None, reads_extra: dict | None = None,
          selection: str | None = BTC_SELECTION, title: str = "今天 · BTC", width: int = 1680, assets: dict | None = None,
          hold_read_model: bool = False):
    from trading_desk.gridmind import render_trade_page

    html = render_trade_page()  # exactly what the desk's /trade sends (#1414)
    model = _read_model("accepted", full_orders=True)
    try:
        browser = runtime.chromium.launch(headless=True, channel="chrome")
    except Exception as exc:  # pragma: no cover - depends on local browser install
        pytest.skip(f"Playwright Chromium unavailable: {exc}")
    page = browser.new_page(viewport={"width": width, "height": 1050 if width > 600 else 812})
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
            "/api/assets": assets or ASSETS,
            "/api/desk/BTC": desk,
            "/api/news/BTC": {"ok": True, "items": [{"id": "n1", "title": "比特币 ETF 净流入", "bucket": "high_impact", "source": "cls",
                                                     "collected_at": "2026-11-06T02:00:00Z"}]},
            "/api/watch": {"items": []},
            "/api/trading-system/read-model": model,
            **(reads_extra or {}),
        }
        if hold_read_model and path == "/api/trading-system/read-model" and not released:
            held.append(route)  # answered after the selection is published (see below)
            return None
        if path in reads:
            return fulfill(route, reads[path])
        if path.startswith("/api/dualtrack/market"):
            return fulfill(route, {**model["market"], "bars": []})
        fulfill(route, {"detail": "not mocked"}, 503)

    held: list = []
    released = False
    page.route("**/api/**", api)
    page.route("**/trade", lambda route: route.fulfill(status=200, content_type="text/html", body=html))
    def static(name: str):
        kind = "text/css" if name.endswith(".css") else "application/javascript"
        return lambda route: route.fulfill(status=200, content_type=kind, body=(STATIC / name).read_text(encoding="utf-8"))

    for name in ("gm-desk.js", "gm-desk.css", "web/web.js"):
        page.route(f"**/static/{name}?*", static(name))
    page.goto(f"{origin}/trade", wait_until="load")
    # GridMind's control script only runs on its own ports, so publish the selection it would:
    # the console is on BTC (Hyperliquid Testnet) unless a test says otherwise.
    if selection is not None:  # None: GridMind's selection has not arrived (8765's catalog is slow)
        page.evaluate(f"() => {{ window.dashboardControlState = {{catalog: {{venue_profiles: []}}, preview: null, selection: {selection}}}; }}")
    if hold_read_model:  # the selection first, GridMind's read after it
        page.wait_for_timeout(500)
        released = True
        for route in held:
            fulfill(route, model)
    page.locator("#deskJudgeTitle", has_text=title).wait_for()
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
        page.locator("#deskBasis summary").click()  # the news is inside 「今天的依据」 (#1435)
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


def test_a_paper_silver_selection_focuses_silver_and_gold_stays_the_fallback() -> None:
    """#1407: a Paper selection names its instrument; silver and crude match by GridMind's catalog id."""
    playwright = pytest.importorskip("playwright.sync_api")
    assets = {"assets": [*ASSETS["assets"],
                         {"key": "SILVER", "label": "白银", "kind": "xag_paper", "instrument_id": "XAG-USDT-SWAP",
                          "venue_profile_id": "okx.paper", "paper_instrument_id": "XAG-USDT-SWAP.OKX"}]}
    silver_desk = _desk(meta={"kind": "xag_paper", "capabilities": ["bars", "bars_health", "execute", "grid_plan", "live_price", "preview"]})
    with _static_server() as origin, playwright.sync_playwright() as runtime:
        browser, page = _page(runtime, origin, _desk(), [], [], reads_extra={
            "/api/assets": assets, "/api/desk/SILVER": silver_desk, "/api/desk/XAU": _desk()})
        page.evaluate("() => { window.dashboardControlState.selection = {venue_profile_id: 'okx.paper', instrument_id: 'XAG-USDT-SWAP.OKX'}; }")
        page.locator("#deskJudgeTitle", has_text="今天 · 白银").wait_for()
        assert not page.evaluate("() => document.body.classList.contains('desk-gold')")  # the gold styling is gold's only
        page.evaluate("() => { window.dashboardControlState.selection = {venue_profile_id: 'okx.paper', instrument_id: 'XAU-USDT-SWAP.OKX'}; }")
        page.locator("#deskJudgeTitle", has_text="今天 · 黄金").wait_for()


def test_the_environment_badge_stays_visible_on_a_phone() -> None:
    """#1412: at 375 px Park still sees whether the page acts on Paper or Testnet."""
    playwright = pytest.importorskip("playwright.sync_api")
    with _static_server() as origin, playwright.sync_playwright() as runtime:
        browser, page = _page(runtime, origin, _desk(), [], [])
        page.set_viewport_size({"width": 375, "height": 812})
        badge = page.locator("#envBadge")
        badge.wait_for(state="visible")
        box = badge.bounding_box()
        assert badge.inner_text().strip() in {"PAPER", "TESTNET"}
        assert box and box["x"] >= 0 and box["x"] + box["width"] <= 375
        assert not page.locator(".brand small").is_visible()  # the subtitle still makes room
        assert page.evaluate("() => document.documentElement.scrollWidth") <= 375  # no sideways scroll
        browser.close()


def test_with_8765_gone_the_page_says_so_and_sends_nothing() -> None:
    """#1414: /trade no longer needs 8765 to load. When 8765 goes away, every path the desk forwards answers with the
    relay's 503: GridMind's next read shows why under the chart and in the action status, and nothing is posted."""
    playwright = pytest.importorskip("playwright.sync_api")
    from trading_desk.gridmind import READ_PATHS

    relay_down = {"error": "gridmind_unavailable", "message": "交易后台（8765）暂时连不上，稍后自动重试。"}
    posts: list = []
    dialogs: list = []
    with _static_server() as origin, playwright.sync_playwright() as runtime:
        browser, page = _page(runtime, origin, _desk(), posts, dialogs)
        try:
            page.unroute("**/api/**")

            def down(route) -> None:
                path = "/" + route.request.url.split("://", 1)[1].split("/", 1)[1].split("?", 1)[0]
                if route.request.method == "POST":
                    posts.append(path)
                if path in READ_PATHS or path.startswith("/api/dualtrack/") or path.startswith("/api/dashboard-control/"):
                    return route.fulfill(status=503, content_type="application/json", body=json.dumps(relay_down, ensure_ascii=False))
                return route.fulfill(status=200, content_type="application/json", body="{}")

            page.route("**/api/**", down)
            page.evaluate("() => load()")
            notice = page.locator("#chartHistoryNotice", has_text="Dashboard 服务暂不可用")
            notice.wait_for()  # on /trade the desk hides GridMind's header pills; the chart notice is what Park sees
            assert "HTTP 503" in notice.inner_text() and "Dashboard 服务暂不可用" in page.locator("#actionStatus").text_content()
            assert page.locator("#runBadge").text_content().startswith("Dashboard 服务暂不可用")
            assert posts == [] and dialogs == []  # no control request goes out on its own
        finally:
            browser.close()


def test_the_top_bar_is_the_one_asset_picker() -> None:
    """#1433: every asset is chosen in the top bar; 「今天」 has no buttons of its own. An asset GridMind does not
    chart switches 「今天」 only: no intent reaches GridMind's selects."""
    playwright = pytest.importorskip("playwright.sync_api")
    with _static_server() as origin, playwright.sync_playwright() as runtime:
        browser, page = _page(runtime, origin, _desk(), [], [], reads_extra={"/api/desk/ETH": _desk(), "/api/news/ETH": {"ok": True, "items": []}})
        page.evaluate("""() => { window.fired = [];
          window.gmIntent.on('change', 'control.venue', event => window.fired.push(['venue', event.args.id]));
          window.gmIntent.on('change', 'control.instrument', event => window.fired.push(['instrument', event.args.id])); }""")
        buttons = page.locator("#deskAssets [data-asset]")
        assert buttons.all_inner_texts() == ["BTC", "黄金", "ETH"]
        assert page.locator('#deskAssets [data-asset="BTC"]').get_attribute("aria-pressed") == "true"
        assert page.locator("#deskAssets [data-desk-only]").all_inner_texts() == ["ETH"]
        assert page.locator("#deskChips").count() == 0
        assert page.locator("#deskAssetNote").count() == 0  # BTC is on GridMind

        page.locator('#deskAssets [data-asset="ETH"]').click()
        page.locator("#deskJudgeTitle", has_text="今天 · ETH").wait_for()
        assert page.locator('#deskAssets [data-asset="ETH"]').get_attribute("aria-pressed") == "true"
        assert page.locator("#deskAssetNote").inner_text() == "ETH只记判断和复盘。图表和下方表格仍是 GridMind 上的品种。"
        page.wait_for_timeout(2500)  # past the 2-second follow loop: GridMind's selection does not pull it back
        assert page.locator("#deskJudgeTitle").inner_text().startswith("今天 · ETH")
        assert page.evaluate("() => window.fired") == []
        browser.close()


def test_a_retired_selection_judges_what_the_chart_shows() -> None:
    """#1433: with no usable GridMind selection, 「今天」 follows the chart (gold), not BTC, and nothing is selected
    for Park: starting still needs their own 「改选 OKX Paper 黄金」."""
    playwright = pytest.importorskip("playwright.sync_api")
    retired = "{venue_profile_id: null, instrument_id: null, retired_selection: {venue_profile_id: 'binance.paper', instrument_id: 'XAUUSDT.BINANCE'}}"
    with _static_server() as origin, playwright.sync_playwright() as runtime:
        browser, page = _page(runtime, origin, _desk(), [], [], selection=retired, title="今天 · 黄金",
                              reads_extra={"/api/desk/XAU": _desk(), "/api/news/XAU": {"ok": True, "items": []}})
        assert page.evaluate("() => window.gridmindView.market.symbol") == "GOLD"
        assert page.locator('#deskAssets [data-asset="XAU"]').get_attribute("aria-pressed") == "true"
        assert page.locator("#deskAssetNote").count() == 0
        assert page.evaluate("() => window.dashboardControlState.selection.venue_profile_id") is None
        browser.close()


def test_five_assets_fit_a_phone_top_bar_without_widening_the_page() -> None:
    """#1433: production lists five assets; on a phone their row scrolls inside the top bar."""
    playwright = pytest.importorskip("playwright.sync_api")
    five = {"assets": [*ASSETS["assets"],
                       {"key": "SILVER", "label": "白银", "kind": "xag_paper", "instrument_id": "XAG-USDT-SWAP", "capabilities": ["grid_plan"]},
                       {"key": "WTI", "label": "原油", "kind": "cl_paper", "instrument_id": "CL-USDT-SWAP", "capabilities": ["grid_plan"]}]}
    with _static_server() as origin, playwright.sync_playwright() as runtime:
        browser, page = _page(runtime, origin, _desk(), [], [], width=375, assets=five)
        assert page.locator("#deskAssets [data-asset]").all_inner_texts() == ["BTC", "黄金", "ETH", "白银", "原油"]
        assert page.evaluate("() => document.documentElement.scrollWidth") <= 375
        box = page.locator("#deskAssets").bounding_box()
        assert box is not None and box["x"] >= 0 and box["x"] + box["width"] <= 375
        browser.close()


WATCH = {"generated_at": "2026-11-08T09:12:00+08:00", "provider": "codex", "items": [
    {"ref": "a", "title": "非农", "at": "2030-01-01T00:00:00Z", "volatility": "高", "why": "就业强弱定利率"},
    {"ref": "b", "title": "CPI", "at": "2030-01-02T00:00:00Z", "why": "通胀"},
    {"ref": "c", "title": "美联储讲话", "at": "2030-01-03T00:00:00Z", "why": "措辞"}]}


@pytest.mark.parametrize("width", [1680, 1100, 375])
def test_the_news_and_the_three_events_are_one_line_in_step_one(width: int) -> None:
    """#1435: no news column and no events row; step 1 says 「今天的依据」 and opens to both. The page is one column
    and never wider than the window."""
    playwright = pytest.importorskip("playwright.sync_api")
    with _static_server() as origin, playwright.sync_playwright() as runtime:
        browser, page = _page(runtime, origin, _desk(), [], [], width=width, reads_extra={"/api/watch": WATCH})
        line = page.locator("#deskBasisLine", has_text="3 件大事")
        line.wait_for()
        assert line.inner_text() == "今天的依据：3 件大事 · 1 条新闻"
        assert page.locator(".desk-news, .desk-watch").evaluate_all(
            "els => els.every(el => el.closest('#deskToday [data-step=\"1\"] #deskBasis'))")
        assert not page.locator("#deskNews").is_visible()
        page.locator("#deskBasis summary").click()
        assert page.locator("#deskBasis .desk-watch-card").count() == 3
        assert page.locator("#deskNews li", has_text="比特币 ETF 净流入").is_visible()
        assert page.evaluate("() => getComputedStyle(document.querySelector('.desk-layout')).gridTemplateColumns.split(' ').length") == 1
        assert page.evaluate("() => document.documentElement.scrollWidth") <= width
        browser.close()


def test_today_starts_on_the_chart_before_gridmind_s_selection_arrives() -> None:
    """#1436: the selection waits on 8765's catalog (8–13 s in production). 「今天」 starts on the chart's asset from
    GridMind's first read, and moves when the selection arrives naming another asset."""
    playwright = pytest.importorskip("playwright.sync_api")
    with _static_server() as origin, playwright.sync_playwright() as runtime:
        browser, page = _page(runtime, origin, _desk(), [], [], selection=None, title="今天 · 黄金",
                              reads_extra={"/api/desk/XAU": _desk(), "/api/news/XAU": {"ok": True, "items": []}})
        # Well inside the old 15 s wait for the selection (the read model answers at once here).
        assert page.evaluate("() => performance.now()") < 8000
        assert page.evaluate("() => window.dashboardControlState === undefined")
        assert page.evaluate("() => window.gridmindView.market.symbol") == "GOLD"
        assert page.locator('#deskAssets [data-asset="XAU"]').get_attribute("aria-pressed") == "true"
        page.evaluate(f"() => {{ window.dashboardControlState = {{catalog: {{venue_profiles: []}}, preview: null, selection: {BTC_SELECTION}}}; }}")
        page.locator("#deskJudgeTitle", has_text="今天 · BTC").wait_for(timeout=5000)
        assert page.locator('#deskAssets [data-asset="BTC"]').get_attribute("aria-pressed") == "true"
        browser.close()


def test_a_retired_selection_that_arrives_first_still_waits_for_the_chart() -> None:
    """#1436: a retired selection is not usable, so it does not end the wait; 「今天」 takes the chart's asset when
    GridMind's read arrives, not BTC."""
    playwright = pytest.importorskip("playwright.sync_api")
    retired = "{venue_profile_id: null, instrument_id: null, retired_selection: {venue_profile_id: 'binance.paper', instrument_id: 'XAUUSDT.BINANCE'}}"
    with _static_server() as origin, playwright.sync_playwright() as runtime:
        browser, page = _page(runtime, origin, _desk(), [], [], selection=retired, title="今天 · 黄金", hold_read_model=True,
                              reads_extra={"/api/desk/XAU": _desk(), "/api/news/XAU": {"ok": True, "items": []}})
        assert page.locator('#deskAssets [data-asset="XAU"]').get_attribute("aria-pressed") == "true"


@pytest.mark.parametrize("width", [1680, 375])
def test_one_way_in_manual_tuning_under_step_two_and_one_highlighted_button(width: int) -> None:
    """#1438: 「自己调」 (GridMind's manual cards, ids unchanged) sits in 「今天」 between step 2 and step 3; only the
    current step's primary button is highlighted; the banner offers no start button of its own."""
    playwright = pytest.importorskip("playwright.sync_api")
    with _static_server() as origin, playwright.sync_playwright() as runtime:
        browser, page = _page(runtime, origin, _desk(), [], [], width=width)
        order = page.evaluate("""() => { const card = document.getElementById('deskToday');
          return [...card.querySelectorAll('.desk-today, #deskManual, .desk-steps-exec')].map(el => el.id || el.className); }""")
        assert order == ["desk-today", "deskManual", "desk-steps desk-steps-exec"]
        assert page.locator("#deskManual > summary").inner_text().startswith("自己调")
        for element_id in ("dashboardControlCard", "parkAiChatCard", "strategyCard", "dashboardVenueSelect", "startRobot", "stopRobot"):
            assert page.locator(f"#deskToday #deskManual #{element_id}").count() == 1, element_id
        open_manual(page)
        assert page.locator("#dashboardVenueSelect").is_visible()
        assert page.locator("#deskStatusAction", has_text="开始").count() == 0

        accent = "rgb(63, 208, 224)"
        background = "el => getComputedStyle(el).backgroundColor"
        page.locator(".desk-dir [data-d=long]").click()
        approve = page.get_by_role("button", name="批准这份计划")
        approve.wait_for()
        assert approve.evaluate(background) == accent  # step 2 is the current step
        approve.click()
        page.locator("#deskExec").wait_for()
        assert page.locator('.desk-step[data-step="3"]').get_attribute("class").split() == ["desk-step", "on"]
        assert approve.evaluate(background) == "rgba(0, 0, 0, 0)"  # still there and usable, no longer highlighted
        assert page.evaluate("() => document.documentElement.scrollWidth") <= width
        browser.close()
