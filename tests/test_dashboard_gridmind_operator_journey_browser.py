"""One Paper-only operator journey across the Dashboard's primary controls.

The existing focused browser tests own detailed chart/drag/history assertions.
This test keeps the operator-facing control flow together and verifies that
every click produces the expected Paper API request and visible result.
"""

import json

import pytest

from tests.test_dashboard_gridmind_header_browser import _header_model
from tests.test_dashboard_gridmind_order_lifecycle_browser import _static_server
from tests.test_dashboard_gridmind_profit_controls_browser import _preview


def _recommendation() -> dict:
    return {
        "proposal_id": "ai-operator-journey-1",
        "source": "ai",
        "created_at": "2026-07-23T03:00:00+00:00",
        "direction": "long",
        "style": "steady",
        "rationale": "可信行情显示上行结构。",
        "signal": {"calibration_status": "uncalibrated", "rule_score": 76},
        "analysis": {
            "contexts": {"1d": {"atr14": 42}, "15m": {"indicators": {"ema20": 4_100}}},
            "framework": {
                "position": {"lookback_bars": 200, "rank": 0.2, "label": "low", "directional_prior": "long"},
                "trend": {"stage": "forming", "direction": "up", "d1_recent_efficiency": 0.3, "h4_recent_efficiency": 0.28},
                "strategy": {"recommended_strategy_type": "grid", "reason": "trend_not_established"},
            },
        },
    }


def test_gridmind_paper_operator_journey_uses_visible_results_and_control_requests() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    model = _header_model(4_000.0)
    model["runtime"].update({
        "actual_state": "stopped",
        "desired_state": "stopped",
        "status": "stopped",
        "can_start_when_authorized": True,
        "can_stop_when_authorized": False,
    })
    model["execution"]["counts"].update({
        "open_order_count": 0,
        "accepted_order_count": 0,
        "open_position_count": 0,
    })
    model["strategy"]["proposals"] = []
    requests: list[dict] = []
    browser_errors: list[str] = []

    def fulfill_control(route) -> None:
        payload = route.request.post_data_json
        requests.append(payload)
        action = payload["action"]
        if action == "refresh_recommendation":
            model["strategy"]["proposals"] = [_recommendation()]
            response = {"action": action, "proposal": _recommendation()}
        elif action == "preview":
            response = _preview()
        elif action == "prepare_start":
            response = {**_preview(), "action": action, "prepared_start_id": "operator-journey-prepared"}
        elif action == "start":
            model["runtime"].update({
                "actual_state": "running",
                "desired_state": "running",
                "status": "running",
                "can_stop_when_authorized": True,
            })
            model["execution"]["counts"].update({"open_order_count": 30, "accepted_order_count": 30})
            response = {
                "action": action,
                "plan": {"version": 8},
                "preview": _preview()["preview"],
                "created_orders": 30,
                "accepted_orders": 30,
                "filled_orders": 0,
            }
        elif action == "stop":
            model["runtime"].update({
                "actual_state": "stopped",
                "desired_state": "stopped",
                "status": "stopped",
                "can_stop_when_authorized": False,
            })
            model["execution"]["counts"].update({"open_order_count": 0, "accepted_order_count": 0})
            response = {"action": action, "cancelled_orders": 30, "flattened_positions": 0}
        else:  # pragma: no cover - assertion makes new controls intentional
            raise AssertionError(f"unexpected control action: {action}")
        route.fulfill(status=200, content_type="application/json", body=json.dumps(response, ensure_ascii=False))

    with _static_server() as origin, playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True, channel="chrome")
        except Exception as exc:  # pragma: no cover - local browser availability
            pytest.skip(f"Playwright Chromium unavailable: {exc}")
        page = browser.new_page(viewport={"width": 1680, "height": 1050})
        page.set_default_timeout(5_000)
        page.on("console", lambda message: browser_errors.append(message.text) if message.type == "error" else None)
        page.on("pageerror", lambda error: browser_errors.append(str(error)))
        page.add_init_script("window.setInterval = () => 0")
        page.route(
            "**/api/trading-system/read-model",
            lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(model, ensure_ascii=False)),
        )
        page.route(
            "**/api/dualtrack/market/bars?*",
            lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps({**model["market"], "bars": []})),
        )
        page.route("**/api/strategy-console/control", fulfill_control)
        page.goto(f"{origin}/dashboard-gridmind.html", wait_until="load")

        page.locator("#refreshTrend").click()
        page.locator("#trendBox").filter(has_text="做多 · 稳健").wait_for()
        assert "① 长期位置" in page.locator("#trendBox").inner_text()
        assert "② 趋势阶段" in page.locator("#trendBox").inner_text()
        assert "③ 策略与参数" in page.locator("#trendBox").inner_text()
        assert requests[-1] == {"action": "refresh_recommendation", "cycle_id": "2026-07-18_DAY"}
        assert "生产计划未改变" in page.locator("#actionStatus").inner_text()

        page.locator("#smartFill").click()
        page.locator("#previewSummary").wait_for(state="visible")
        assert requests[-1]["action"] == "preview"
        assert requests[-1]["solver"]["locked"] == []

        page.locator("#gridAdjustToggle").click()
        page.locator(".grid-adjust-overlay.on").wait_for(state="visible")
        page.locator("#gridAdjustToggle").click()
        assert page.locator(".grid-adjust-overlay.on").count() == 0

        page.locator("#startRobot").click()
        page.locator(".trade-toast").filter(has_text="机器人已启动").wait_for()
        assert [row["action"] for row in requests[-2:]] == ["prepare_start", "start"]
        assert requests[-1]["prepared_start_id"] == "operator-journey-prepared"
        assert "当前接受 30 笔委托" in page.locator(".trade-toast").inner_text()

        page.locator("#stopRobot").click()
        page.locator("#actionStatus").filter(has_text="机器人已停止").wait_for()
        assert requests[-1] == {"action": "stop", "cycle_id": "2026-07-18_DAY"}
        assert "撤销 30 笔挂单" in page.locator("#actionStatus").inner_text()

        for tab, panel in (("review", "#review"), ("shadows", "#shadows"), ("history", "#history")):
            page.locator(f'[data-tab="{tab}"]').click()
            assert page.locator(panel).evaluate("element => element.parentElement.classList.contains('on') || element.classList.contains('on')")

        assert browser_errors == []
        browser.close()
