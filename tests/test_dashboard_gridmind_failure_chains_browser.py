from __future__ import annotations

import json

import pytest

from tests.test_dashboard_gridmind_order_lifecycle_browser import _static_server


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            {"status": 530, "content_type": "text/html", "body": "<title>Error 1033</title><p>Cloudflare Tunnel error</p>"},
            "Cloudflare 隧道不可达",
        ),
        (
            {"status": 502, "content_type": "application/json", "body": json.dumps({"error": "gateway_failure"})},
            "Dashboard 服务暂不可用",
        ),
    ],
)
def test_gridmind_read_model_failure_names_the_gateway_chain(
    response: dict,
    expected: str,
) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    with _static_server() as origin, playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True, channel="chrome")
        page = browser.new_page(viewport={"width": 1680, "height": 1050})
        page.add_init_script("window.setInterval = () => 0")
        page.route(
            "**/api/trading-system/read-model",
            lambda route: route.fulfill(**response),
        )
        page.goto(f"{origin}/dashboard-gridmind.html", wait_until="load")
        page.locator("#runBadge").filter(has_text=expected).wait_for()
        text = page.locator("#runBadge").inner_text()
        assert expected in text
        assert "下一步：" in text
        browser.close()


def test_gridmind_names_binance_upstream_and_unreached_backend() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    with _static_server() as origin, playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True, channel="chrome")
        page = browser.new_page(viewport={"width": 1680, "height": 1050})
        page.add_init_script("window.setInterval = () => 0")
        page.goto(f"{origin}/dashboard-gridmind.html", wait_until="load")
        failures = page.evaluate(
            """() => ({
                binance: tradingFailure(marketFailure({
                    status: "blocked",
                    access_issues: ["datafeed HTTP 502: upstream_error: Binance USD-M Futures request failed"]
                })).text,
                network: tradingFailure(apiFailure("network down", 0, "dashboard_response_unconfirmed")).text,
                networkIsUncertain: isUncertainControlError(apiFailure("network down", 0, "dashboard_response_unconfirmed"))
            })"""
        )
        assert "Binance USD-M 行情上游不可用" in failures["binance"]
        assert "禁止新开仓" in failures["binance"]
        assert "Dashboard 响应未收到" in failures["network"]
        assert "当前无法确认" in failures["network"]
        assert "不要重复点击控制按钮" in failures["network"]
        assert failures["networkIsUncertain"] is True
        browser.close()


def test_gridmind_names_complete_grid_rollback_as_execution_failure() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    with _static_server() as origin, playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True, channel="chrome")
        page = browser.new_page()
        page.add_init_script("window.setInterval = () => 0")
        page.goto(f"{origin}/dashboard-gridmind.html", wait_until="load")
        failure = page.evaluate(
            "() => tradingFailure(apiFailure('paper start did not accept the complete grid', 502, 'execution_incomplete')).text"
        )
        assert "网格未被完整接受，已安全回滚" in failure
        assert "刷新当前委托与持仓" in failure
        browser.close()
