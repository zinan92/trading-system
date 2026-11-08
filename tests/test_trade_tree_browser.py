"""#1415: /trade is one React tree. Booted with the same mocked APIs, it must be the page it replaced.

The first reference was the pre-#1415 /trade: 8765's dashboard-gridmind.html plus the desk shell, recorded from
1b7b20b into tests/fixtures/trade_tree_shape.json when #1419 retired that file. The redesign re-records it on
purpose, from /trade itself (#1433: 「今天」 lost its asset buttons and its props carry capabilities); the record's
"source" says which. The page is served from a
`.test` host, so GridMind's control and assistant scripts run (they stay off on other localhost ports). Every
endpoint is mocked; no request leaves the page. The control journey here only presses mocked buttons. The redesign
changes the page on purpose: re-record the shape in the same PR that does, and say so in its records.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from tests.test_dashboard_control_wire_browser import CONFIRMED, DASHBOARD_PREVIEW, WIRE, _fulfill_control, _model
from tests.test_dashboard_gridmind_control_track_browser import _catalog

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "apps" / "trading-desk" / "src" / "trading_desk" / "static"
sys.path.insert(0, str(ROOT / "apps" / "trading-desk" / "src"))
ORIGIN = "http://trade.test"
NOW = "2026-11-08T04:00:00Z"
ASSETS = {"assets": [
    {"key": "BTC", "label": "BTC", "kind": "hl_testnet", "instrument_id": "BTC-USD-PERP"},
    {"key": "XAU", "label": "黄金", "kind": "xau_paper", "instrument_id": "XAU-USDT-SWAP"},
]}
DESK = {"kline": {"ok": True, "structure": "日线偏多", "synthesis": "金价修复。"}, "today_judgment": None, "executions": [],
        "ai_today": None, "ai_running": False, "control": {"can_stop": False},
        "meta": {"kind": "xau_paper", "capabilities": ["bars", "execute", "grid_plan", "live_price", "preview"]}}

# Tag, sorted attributes (style as parsed) and text exactly; form controls by their live value; dialogs and
# details by whether they are open.
SHAPE = """root => {
  const skip = new Set(["SCRIPT", "LINK"]);
  const shape = node => {
    if (node.nodeType === 3) return ["#text", node.textContent];
    if (node.nodeType !== 1 || skip.has(node.tagName)) return null;
    const attrs = [...node.attributes].map(a => [a.name, a.name === "style" ? node.style.cssText : a.value]).sort((x, y) => x[0] < y[0] ? -1 : 1);
    const live = "value" in node && ["INPUT", "SELECT", "TEXTAREA"].includes(node.tagName) ? [node.value, !!node.checked] : null;
    const open = node.tagName === "DIALOG" || node.tagName === "DETAILS" ? node.open : null;
    return [node.tagName, attrs, live, open, merge([...node.childNodes].map(shape).filter(x => x !== null))];
  };
  // Adjacent text nodes render as one (gm-desk.js moving a card out leaves two behind); compare them joined.
  const merge = list => list.reduce((out, item) => {
    const last = out[out.length - 1];
    if (item[0] === "#text" && last && last[0] === "#text") out[out.length - 1] = ["#text", last[1] + item[1]]; else out.push(item);
    return out;
  }, []);
  const kids = merge([...root.childNodes].map(shape).filter(x => x !== null));
  // The reference's shell tags sat after its content; the whitespace they leave behind is not the page.
  while (kids.length && kids[kids.length - 1][0] === "#text" && !kids[kids.length - 1][1].trim()) kids.pop();
  return kids;
}"""


def _first_difference(a, b, path="root"):
    """Where two shapes first differ, readably: a DOM diff of thousands of nodes is useless as one assert."""
    if type(a) is not type(b) or not isinstance(a, list):
        return None if a == b else (path, a, b)
    if a and isinstance(a[0], str) and len(a) == 5:  # an element: tag, attrs, live, open, children
        label = f"{path}>{a[0]}" + "".join(f"[{k}={v}]" for k, v in a[1] if k in ("id", "class"))
        for i, part in enumerate(a[:4]):
            if part != b[i]:
                return (label + f" ({['tag', 'attrs', 'value', 'open'][i]})", part, b[i])
        return _first_difference(a[4], b[4], label)
    for i, (x, y) in enumerate(zip(a, b)):
        found = _first_difference(x, y, f"{path}[{i}]")
        if found:
            return found
    return None if len(a) == len(b) else (f"{path} (child count)", [str(x)[:120] for x in a[len(b):]], [str(y)[:120] for y in b[len(a):]])


REFERENCE = json.loads((ROOT / "tests" / "fixtures" / "trade_tree_shape.json").read_text(encoding="utf-8"))


def _tree() -> str:
    from trading_desk.gridmind import render_trade_page

    return render_trade_page()


def _open(browser, html: str, *, width: int = 1680, height: int = 1050):
    model = _model()
    wire: list[list] = []
    reads: list[str] = []
    errors: list[str] = []
    answers = {
        "/api/trading-system/read-model": model,
        "/api/auth/session": {"authenticated": True, "can_control": True, "email": None, "session_duration": "mock"},
        "/api/dashboard-control/catalog": _catalog(),
        "/api/dashboard-control/selection": {"selection": {"venue_profile_id": "binance.paper", "instrument_id": "XAUUSDT.BINANCE", "strategy_family": "grid"}},
        "/api/dashboard-control/runtime-status": {"runtime": {}, "relevance": {}, "notification": None},
        "/api/dashboard-control/preview": DASHBOARD_PREVIEW,
        "/api/dashboard-control/confirm": CONFIRMED,
        "/api/park-paper/ai-chat": {"pending_draft": None},
        "/api/assets": ASSETS,
        "/api/watch": {"items": []},
    }

    def handle(route) -> None:
        request = route.request
        path = request.url.removeprefix(ORIGIN).split("?", 1)[0]
        if path.startswith("/api/"):
            (wire if request.method != "GET" else reads).append([request.method, path, request.post_data] if request.method != "GET" else path)
        if path == "/trade":
            return route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
        if path == "/api/strategy-console/control":
            return _fulfill_control(route, model)
        if path in answers:
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(answers[path], ensure_ascii=False))
        if path.startswith("/api/desk/"):
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(DESK, ensure_ascii=False))
        if path.startswith("/api/news/"):
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "items": []}))
        if path.startswith("/api/dualtrack/market/bars"):
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({**model["market"], "bars": []}))
        if path.startswith("/api/"):
            return route.fulfill(status=404, content_type="application/json", body="{}")
        if path.startswith("/static/"):
            local = STATIC / path.removeprefix("/static/")
        else:
            local = ROOT / path.lstrip("/")
        return route.fulfill(status=200, path=str(local)) if local.is_file() else route.fulfill(status=404, body="")

    # The frozen reference was recorded in Beijing time (desk times such as 「每分钟更新 · 12:00」 are local);
    # CI runs in UTC, so the browser is pinned to the zone the reference was recorded in.
    page = browser.new_page(viewport={"width": width, "height": height}, timezone_id="Asia/Shanghai")
    page.set_default_timeout(10000)
    page.clock.set_fixed_time(NOW)
    page.add_init_script("window.setInterval = () => 0")  # one boot, no refresh loop: both pages settle the same way
    page.on("dialog", lambda dialog: dialog.accept())
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    page.route(f"{ORIGIN}/**", handle)
    page.goto(f"{ORIGIN}/trade", wait_until="load")
    page.wait_for_function("() => window.dashboardControlState && window.dashboardControlState.catalog")
    page.wait_for_function("() => document.querySelectorAll('[data-island]').length > 0 && [...document.querySelectorAll('[data-island]')].every(el => el.hasAttribute('data-island-live'))")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(600)
    return page, wire, reads, errors


def _launch(runtime):
    try:
        return runtime.chromium.launch(headless=True, channel="chrome")
    except Exception as exc:  # pragma: no cover - depends on local browser install
        pytest.skip(f"Playwright Chromium unavailable: {exc}")


@pytest.mark.parametrize("width,height", [(1680, 1050), (375, 812)], ids=["desktop", "phone"])
def test_the_booted_tree_is_the_page_it_replaced(width: int, height: int) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runtime:
        browser = _launch(runtime)
        page, wire, reads, errors = _open(browser, _tree(), width=width, height=height)
        tree = {
            "dom": page.evaluate(f"({SHAPE})(document.getElementById('trade-app'))"),
            "body_class": page.evaluate("() => document.body.className"),
            "wire": wire, "reads": sorted(reads), "errors": errors,
            "scroll_width": page.evaluate("() => document.documentElement.scrollWidth"),
            "islands": page.evaluate("() => [...document.querySelectorAll('[data-island]')].map(el => el.dataset.island)"),
        }
        page.close()
        browser.close()
    reference = REFERENCE["desktop" if width > 600 else "phone"]
    assert tree["errors"] == [] and reference["errors"] == []
    assert tree["islands"] == reference["islands"] and len(tree["islands"]) >= 15
    assert _first_difference(tree["dom"], reference["dom"]) is None
    assert tree["body_class"] == reference["body_class"]
    assert tree["wire"] == reference["wire"] == []  # booting sends no control request
    assert tree["reads"] == reference["reads"]
    assert tree["scroll_width"] == reference["scroll_width"] <= width


def test_the_control_journey_on_the_tree_sends_the_recorded_requests() -> None:
    """ADR 0008's wire, on the page production now serves: the same 7 requests, byte for byte."""
    playwright = pytest.importorskip("playwright.sync_api")
    wires = {}
    with playwright.sync_playwright() as runtime:
        browser = _launch(runtime)
        for name, html in {"tree": _tree()}.items():
            page, wire, _reads, errors = _open(browser, html)
            # On /trade gm-desk.css hides GridMind's own buttons behind the desk flow, on both pages alike. The
            # journey presses them the way the intent router sees a press: a bubbling click on the control.
            def press(selector: str) -> None:  # waits for the control to be enabled, as a real click would
                page.locator(f"{selector}:not([disabled])").wait_for(state="attached")
                page.locator(selector).dispatch_event("click")
            text = lambda selector, words: page.wait_for_function(
                "([s, w]) => [...document.querySelectorAll(s)].some(el => el.textContent.includes(w))", arg=[selector, words])
            press('[data-dashboard-strategy="grid"]')
            press("#dashboardPreviewButton")
            press("#dashboardConfirmButton")
            text("#dashboardPreviewSummary", "mock-1")
            press("#smartFill")
            text("#previewSummary", "预计最大损失")
            press("#startRobot")
            text(".trade-toast", "机器人已启动")
            press("#stopRobot")
            text("#actionStatus", "机器人已停止")
            wires[name] = (wire, errors)
            page.close()
        browser.close()
    assert wires["tree"][0] == WIRE and wires["tree"][1] == []
