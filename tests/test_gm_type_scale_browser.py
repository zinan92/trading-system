"""#1457: /trade's words are 14px or 12px; headline figures are 20px in the one monospace font. Mocks only."""
from __future__ import annotations

import json

import pytest

from tests.test_dashboard_gridmind_order_lifecycle_browser import _read_model, _static_server
from tests.trade_page import open_manual, open_system

SIZES = """() => {
  const sizes = {}, monos = new Set(), figures = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) {
    const text = node.textContent.trim(), element = node.parentElement;
    if (!text || !element || element.closest('script,style,svg,canvas,.standard-kline-chart,.tv-lightweight-charts')) continue;
    const box = element.getBoundingClientRect(), style = getComputedStyle(element);
    if (!box.width || style.visibility === 'hidden') continue;
    const key = style.fontSize;
    (sizes[key] = sizes[key] || []).length < 3 && sizes[key].push(text.slice(0, 30));
    if (/mono|Menlo|Consolas/i.test(style.fontFamily)) monos.add(style.fontFamily);
    if (key === '20px') figures.push(element.closest('#account,#headerPrice,#deskStatus,#deskReview')?.id || element.className || element.tagName);
  }
  return {sizes, monos: [...monos], figures};
}"""


@pytest.mark.parametrize("width", [1680, 375])
def test_trade_uses_two_text_sizes_and_one_figure_size(width: int) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    model = _read_model("accepted")
    with _static_server() as origin, playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True, channel="chrome")
        except Exception as exc:  # pragma: no cover - local browser dependency
            pytest.skip(f"Playwright Chromium unavailable: {exc}")
        page = browser.new_page(viewport={"width": width, "height": 1000})
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/api/trading-system/read-model",
                   lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(model, ensure_ascii=False)))
        page.route("**/api/dualtrack/market/bars?*", lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(model["market"], ensure_ascii=False)))
        page.goto(f"{origin}/trade", wait_until="load")
        page.wait_for_function("() => window.gridmindView && window.gridmindView.data")
        open_manual(page)
        open_system(page)

        found = page.evaluate(SIZES)
        assert set(found["sizes"]) <= {"12px", "14px", "20px"}, found["sizes"]
        assert {"12px", "14px"} <= set(found["sizes"])
        assert len(found["monos"]) == 1, found["monos"]
        assert page.evaluate("() => getComputedStyle(document.querySelector('#account .metric strong')).fontSize") == "20px"
        assert page.evaluate("() => getComputedStyle(document.querySelector('#deskStatus h1')).fontSize") == "20px"  # the one headline
        figures = set(found["figures"])
        assert figures and figures <= {"account", "headerPrice", "deskStatus", "deskReview"}, figures
        assert page.evaluate("() => [...document.querySelectorAll('#productionChart .standard-kline-toolbar button')].every(b => b.getBoundingClientRect().height < 30)")
        assert page.evaluate("() => document.documentElement.scrollWidth") <= width
        assert errors == []
        browser.close()
