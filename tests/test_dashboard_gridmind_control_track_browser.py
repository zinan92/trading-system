"""Browser regressions for the Dashboard control track (S2, 2026-11-05).

The control track only boots on production ports or a non-local host, so this
test serves the page from a `.test` host through Playwright routing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_dashboard_gridmind_header_browser import _header_model
from tests.trade_page import desk_answer, open_manual, static_file, trade_html

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "http://gridmind.test"


def _json(route, body: dict, status: int = 200) -> None:
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


def _catalog() -> dict:
    return {
        "schema_version": "dashboard-control-plane-v1",
        "venue_profiles": [
            {
                "id": "binance.paper",
                "label": "Binance Paper",
                "instruments": [
                    {
                        "instrument_id": "XAUUSDT.BINANCE",
                        "asset": "XAU",
                        "eligibility": "eligible",
                        "blockers": [],
                    }
                ],
            }
        ],
    }


def _selection() -> dict:
    return {
        "selection": {
            "venue_profile_id": "binance.paper",
            "instrument_id": "XAUUSDT.BINANCE",
            "strategy_family": "dca",
        }
    }


def _old_btc_grid_runtime() -> dict:
    return {
        "schema_version": "dashboard-runtime-status-response-v1",
        "runtime": {
            "status": "grid_terminal",
            "execution_blocker": "position_open_unprotected",
            "environment": "testnet",
            "instrument_id": "BTC-USD-PERP",
            "strategy_family": "grid",
        },
        "relevance": {
            "view": "history",
            "reason": "other_selection",
            "observed_at": "2026-09-15T14:40:59+00:00",
            "age_seconds": 4_367_941,
            "runtime_selection": {
                "venue_profile_id": "hyperliquid.testnet",
                "instrument_id": "BTC-USD-PERP",
                "strategy_family": "grid",
            },
        },
        "notification": None,
    }


def _open_page(runtime, runtime_status: dict):
    model = _header_model(4_000.0)
    model["strategy"]["plan"] = None
    page = runtime.new_page(viewport={"width": 1680, "height": 1050})
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script("window.setInterval = () => 0")
    html = trade_html()  # #1419: the old page is gone; /trade is what runs GridMind

    def handle(route) -> None:
        url = route.request.url
        if url.startswith(f"{ORIGIN}/api/trading-system/read-model"):
            return _json(route, model)
        if url.startswith(f"{ORIGIN}/api/dualtrack/market/bars"):
            return _json(route, {**model["market"], "bars": []})
        if url.startswith(f"{ORIGIN}/api/dashboard-control/catalog"):
            return _json(route, _catalog())
        if url.startswith(f"{ORIGIN}/api/dashboard-control/selection"):
            return _json(route, _selection())
        if url.startswith(f"{ORIGIN}/api/dashboard-control/runtime-status"):
            return _json(route, runtime_status)
        path = url.removeprefix(ORIGIN).split("?")[0]
        if desk_answer(path) is not None:
            return _json(route, desk_answer(path))
        if url.startswith(f"{ORIGIN}/api/"):
            return _json(route, {"error": "not_in_fixture"}, status=404)
        if path == "/trade":
            return route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
        local = static_file(path) or ROOT / path.lstrip("/")
        if local.is_file():
            return route.fulfill(status=200, path=str(local))
        return route.fulfill(status=404, body="")

    page.route(f"{ORIGIN}/**", handle)
    page.goto(f"{ORIGIN}/trade", wait_until="load")
    open_manual(page)  # /trade keeps the control rail inside 「手动配置与 AI 助手」
    return page, errors


def test_dca_selection_without_a_plan_does_not_crash_the_control_track() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True, channel="chrome")
        page, errors = _open_page(browser, _old_btc_grid_runtime())
        status = page.locator("#dashboardControlStatus")
        status.filter(has_text="XAUUSDT.BINANCE").wait_for(timeout=10_000)

        assert "控制轨道暂不可用" not in status.inner_text()
        # The chart overlay builder must accept "DCA selected, nothing on screen".
        assert page.evaluate("Array.isArray(buildVisibleChartPriceLines({latest_close: 4000}, null, {}))")
        assert errors == []
        browser.close()


def test_runtime_record_from_another_selection_is_labelled_as_history() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True, channel="chrome")
        page, _errors = _open_page(browser, _old_btc_grid_runtime())
        summary = page.locator("#dashboardRuntimeStatus")
        summary.filter(has_text="上一次运行").wait_for(timeout=10_000)

        text = summary.inner_text()
        assert summary.get_attribute("data-runtime-view") == "history"
        assert "history" in (summary.get_attribute("class") or "")
        assert "不是当前选择" in text
        assert "BTC-USD-PERP" in text
        # Still visible, just not presented as the current state.
        assert "position_open_unprotected" in text
        browser.close()
