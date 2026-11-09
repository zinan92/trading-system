"""#1455: /trade's labels outside 系统详情 read in Chinese; the source badge never says "Unavailable". Mocks only."""
from __future__ import annotations

import json

import pytest

from tests.test_dashboard_gridmind_order_lifecycle_browser import _read_model, _static_server
from tests.trade_page import open_manual

ENGLISH = (
    "production grid console", "Unavailable", "交易 blocked", "fit", " bars", "Dashboard 响应",
    "Trading BOT", "Venue Profile", "Instrument", "Strategy Family", "Strategy Preview", "Confirm", "Grid 网格", "DCA 趋势加仓",
    "智能填充 Range", "AUTO", "KLINE", "OHLCV", "Broker 来源", "fallback", "synthetic", "Snapshot", "snapshot", "Draft",
)
VISIBLE_TEXT = """() => {
  const parts = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) {
    const text = node.textContent.trim(), element = node.parentElement;
    if (!text || !element || element.closest('script,style,svg,#deskSystem')) continue;
    const box = element.getBoundingClientRect(), style = getComputedStyle(element);
    if (box.width > 0 && style.visibility !== 'hidden') parts.push(text);
  }
  return parts.join('\\n');
}"""


def _page(runtime, width: int):
    try:
        browser = runtime.chromium.launch(headless=True, channel="chrome")
    except Exception as exc:  # pragma: no cover - local browser dependency
        pytest.skip(f"Playwright Chromium unavailable: {exc}")
    return browser, browser.new_page(viewport={"width": width, "height": 1000})


def _serve(page, model: dict) -> None:
    page.route("**/api/trading-system/read-model",
               lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(model, ensure_ascii=False)))
    page.route("**/api/dualtrack/market/bars?*", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps({**model["market"]}, ensure_ascii=False)))


@pytest.mark.parametrize("width", [1680, 375])
def test_the_page_outside_system_details_reads_in_chinese(width: int) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    model = _read_model("accepted")
    with _static_server() as origin, playwright.sync_playwright() as runtime:
        browser, page = _page(runtime, width)
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        posts: list[str] = []
        page.on("request", lambda request: posts.append(request.url) if request.method != "GET" else None)
        _serve(page, model)
        page.goto(f"{origin}/trade", wait_until="load")
        page.wait_for_function("() => window.gridmindView && window.gridmindView.data")
        open_manual(page)
        page.wait_for_function("() => document.querySelector('#productionChart [data-action=fit]')")

        text = page.evaluate(VISIBLE_TEXT)
        assert "交易机器人 · 交易所 → 品种 → 策略" in text and "1 · 交易所" in text and "3 · 策略类型" in text
        assert "生成策略预览" in text and "智能填充区间" in text and "自动" in text
        assert page.locator("#productionChart [data-action=fit]").inner_text() == "复位"
        leftovers = [word for word in ENGLISH if word in text]
        assert leftovers == [], leftovers
        assert errors == [] and posts == []
        browser.close()


def test_an_unavailable_source_says_so_in_chinese() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    model = _read_model("accepted")
    model["market"] = {**model["market"], "timeframe": "1m", "provider": "unavailable", "provider_label": "Unavailable",
                       "status": "error", "trusted": False}
    with _static_server() as origin, playwright.sync_playwright() as runtime:
        browser, page = _page(runtime, 1680)
        _serve(page, model)
        page.goto(f"{origin}/trade", wait_until="load")
        page.wait_for_function("() => window.gridmindView && window.gridmindView.data")
        page.wait_for_function("() => document.querySelector('#sourceBadge').textContent !== '来源读取中'")
        assert page.locator("#sourceBadge").inner_text() == "行情源不可用"
        browser.close()
