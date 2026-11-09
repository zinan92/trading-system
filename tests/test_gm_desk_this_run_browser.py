"""#1449: while a plan runs, 「今天」's steps give way to 「这一轮」; step 3's stop area and 「自己调」 stay. Mocks only."""
from __future__ import annotations

import json

import pytest

from tests.test_dashboard_gridmind_order_lifecycle_browser import _read_model, _static_server

PARK_RUNNING = {
    "generated_at": "2026-08-18T01:02:04+00:00",
    "status": "ok",
    "blockers": [],
    "strategy": {
        "active": True, "state": "RUNNING", "strategy_session_id": "session-browser-park",
        "strategy_revision_id": "revision-browser-4", "plan_digest": "sha256:browser-plan", "strategy_type": "grid",
        "direction": "short", "lower_price_boundary": 3800.0, "upper_price_boundary": 4000.0, "stop_price": 4000.0,
        "take_profit_price": None, "maximum_leverage": 10.0, "maximum_acceptable_loss": 500.0, "maximum_notional": 40_000.0,
        "theoretical_max_loss": 480.0, "order_count": 19, "selected_constraint": "maximum_acceptable_loss",
        "grid_entry_range": {"lower": 3810.0, "upper": 3990.0}, "grid_spacing": 10.0, "grid_rung_count": 19,
        "grid_rung_prices": [3810.0, 3820.0, 3830.0],
    },
    "execution": {"counts": {"accepted_orders": 12, "filled_orders": 3, "fills": 7, "open_positions": 2, "closed_positions": 1},
                  "reconciliation": {"status": "ok", "issues": []}},
    "recording": {"status": "complete", "record_window_id": "2026-08-18_DAY", "package_status": "complete",
                  "next_action": "review_recorded_evidence", "blocker_code": None},
    "market": {"price": 3910.0, "fresh": True},
    "safety": {"status": "pass", "age_seconds": 4.0},
}


def _open(runtime, origin, model, width):
    page = runtime.new_page(viewport={"width": width, "height": 1000})
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/api/trading-system/read-model",
               lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(model, ensure_ascii=False)))
    page.route("**/api/dualtrack/market/bars?*", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({**model["market"], "timeframe": route.request.url.split("timeframe=", 1)[1].split("&", 1)[0], "bars": []})))
    page.goto(f"{origin}/trade", wait_until="load")
    page.wait_for_function("() => window.gridmindView && window.gridmindView.data")
    return page, errors


@pytest.mark.parametrize("width", [1680, 375])
def test_a_running_plan_shows_this_run_instead_of_today_s_steps(width: int) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    running = _read_model("accepted", full_orders=True, park=PARK_RUNNING)
    idle = _read_model("accepted")
    with _static_server() as origin, playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True, channel="chrome")
        except Exception as exc:  # pragma: no cover - local browser dependency
            pytest.skip(f"Playwright Chromium unavailable: {exc}")

        page, errors = _open(browser, origin, running, width)
        panel = page.locator("#deskThisRun")
        panel.locator("h3").wait_for(state="visible")
        assert panel.locator("h3").inner_text() == "这一轮 · 网格运行中"
        text = panel.inner_text()
        assert "3,800 – 4,000" in text and "做空" in text and "19 格 · 间距 10" in text and "计划最坏" in text
        assert "已实现" not in text and "已用" not in text
        assert page.evaluate("() => document.body.classList.contains('desk-running')") is True
        assert page.locator("#deskToday .desk-today").is_visible() is False  # steps 1–2
        assert page.locator("#deskExecBox").is_hidden()
        assert page.locator("#deskToday #deskStop").count() == 1  # step 3's stop area stays
        assert page.locator("#deskToday #deskManual").is_visible()  # 「自己调」 with GridMind's stop button stays
        assert panel.locator("button").count() == 0  # nothing to press in the panel
        assert "开始" not in " ".join(page.locator("#deskStatus button").all_inner_texts())
        assert page.evaluate("() => document.documentElement.scrollWidth") <= width
        assert errors == []
        page.close()

        page, errors = _open(browser, origin, idle, width)
        page.wait_for_function("() => window.gridmindView.data && !document.body.classList.contains('desk-running')")
        page.wait_for_timeout(1500)  # a strip tick or more
        assert page.locator("#deskThisRun").inner_text() == ""
        assert page.locator("#deskThisRun").is_hidden()
        assert page.locator("#deskToday .desk-today").is_visible()
        assert errors == []
        browser.close()
