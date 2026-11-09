"""#1453: the tables row shows 持仓 / 委托 / 成交 / 交易回看 / 回测; 运行监督 / 影子策略 / 收益曲线 open from 系统详情. Mocks only."""
from __future__ import annotations

import json

import pytest

from tests.test_dashboard_gridmind_order_lifecycle_browser import _read_model, _static_server

ROW = "() => [...document.querySelectorAll('#tabs [data-tab]')].filter(b => getComputedStyle(b).display !== 'none').map(b => b.dataset.tab)"


@pytest.mark.parametrize("width", [1680, 375])
def test_system_tabs_open_from_system_details(width: int) -> None:
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
        posts: list[str] = []
        page.on("request", lambda request: posts.append(request.url) if request.method != "GET" else None)
        page.route("**/api/trading-system/read-model",
                   lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(model, ensure_ascii=False)))
        page.route("**/api/dualtrack/market/bars?*", lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps({**model["market"], "bars": []})))
        page.goto(f"{origin}/trade", wait_until="load")
        page.wait_for_function("() => window.gridmindView && window.gridmindView.data")

        assert page.evaluate(ROW) == ["positions", "orders", "fills", "review", "bench"]
        page.locator("#deskSystem summary").click()
        assert page.locator("#deskSysTabs button").all_inner_texts() == ["运行监督", "影子策略", "收益曲线"]

        page.locator('#deskSysTabs [data-open-tab="history"]').click()
        assert page.locator('#tabs [data-tab="history"]').get_attribute("class").split().count("on") == 1
        assert page.locator('[data-panel="history"]').is_visible()
        assert page.evaluate(ROW) == ["positions", "orders", "fills", "review", "history", "bench"]  # shown while open

        page.locator('#tabs [data-tab="fills"]').click()
        assert page.evaluate(ROW) == ["positions", "orders", "fills", "review", "bench"]
        assert page.evaluate("() => document.documentElement.scrollWidth") <= width
        assert errors == [] and posts == []  # navigation only
        browser.close()
