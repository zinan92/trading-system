from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.test_dashboard_gridmind_header_browser import _header_model
from tests.test_dashboard_gridmind_order_lifecycle_browser import _static_server


def _preview() -> dict:
    orders = [
        {
            "preview_order_id": f"preview-{index}",
            "side": "buy" if index < 15 else "sell",
            "price": 3_900.0 + index * (200.0 / 30.0),
            "tp": 3_900.0 + (index + 1) * (200.0 / 30.0),
            "sl": 3_893.33,
            "quantity": 1.66,
            "notional": 6_666.0,
            "planned_net_profit_usd": 10.18,
        }
        for index in range(30)
    ]
    return {
        "action": "preview",
        "preview": {
            "schema_version": "strategy-grid-preview-v1",
            "preview_id": "profit-preview-browser-1",
            "cycle_id": "2026-07-18_DAY",
            "direction": "neutral",
            "style": "steady",
            "range": {"low": 3_900.0, "high": 4_100.0},
            "grid": {
                "count": 30,
                "mode": "arithmetic",
                "spacing": 6.6667,
                "notional_per_grid": 6_666.67,
                "notional_mode": "auto",
                "leverage": 10.0,
                "min_net_profit_per_grid_usd": 10.18,
                "target_net_profit_per_grid_usd": 10.0,
                "profit_target_met": True,
            },
            "orders": orders,
            "risk": {
                "estimated_margin": 9_997.2,
                "actual_leverage": 9.9972,
                "capital_budget": 100_000.0,
                "max_simultaneous_same_side_levels": 15,
                "max_loss": 1_000.0,
                "max_loss_role": "advisory_only",
            },
        },
    }


def test_gridmind_profit_target_controls_are_visible_and_not_editable() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    model = _header_model(4_000.0)
    model["runtime"].update({"actual_state": "stopped", "desired_state": "stopped"})
    browser_errors: list[str] = []

    def fulfill_read_model(route) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(model, ensure_ascii=False),
        )

    def fulfill_market(route) -> None:
        timeframe = route.request.url.split("timeframe=", 1)[1].split("&", 1)[0]
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                **model["market"],
                "timeframe": timeframe,
                "bars": [],
            }),
        )

    def fulfill_control(route) -> None:
        assert route.request.post_data_json["action"] == "preview"
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_preview(), ensure_ascii=False),
        )

    with _static_server() as origin, playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True, channel="chrome")
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"Playwright Chromium unavailable: {exc}")
        page = browser.new_page(viewport={"width": 1680, "height": 1050})
        page.on(
            "console",
            lambda message: browser_errors.append(message.text)
            if message.type == "error"
            else None,
        )
        page.on("pageerror", lambda error: browser_errors.append(str(error)))
        page.add_init_script("window.setInterval = () => 0")
        page.route("**/api/trading-system/read-model", fulfill_read_model)
        page.route("**/api/dualtrack/market/bars?*", fulfill_market)
        page.route("**/api/strategy-console/control", fulfill_control)
        page.goto(f"{origin}/dashboard-gridmind.html", wait_until="load")
        page.locator("#smartFill").click()
        page.locator("#previewSummary").wait_for(state="visible")

        for selector in ("#gridCount", "#gridNotional", "#leverage", "#outOfRange"):
            assert page.locator(selector).count() == 0
        assert page.locator("#rangeLow").input_value() == "3900"
        assert page.locator("#rangeHigh").input_value() == "4100"
        summary = page.locator("#previewSummary").inner_text()
        assert "自动网格\n30 格" in summary
        assert "最低计划净利\n10.18 USD" in summary
        assert "计划净利目标\n≥ 10 USD" in summary
        assert "实际杠杆\n10x / 10x" in summary
        assert browser_errors == []

        artifact_dir = os.environ.get("GRID_PROFIT_SCREENSHOT_DIR")
        if artifact_dir:
            path = Path(artifact_dir)
            path.mkdir(parents=True, exist_ok=True)
            page.screenshot(
                path=str(path / "issue-84-profit-target-controls.png"),
                full_page=True,
            )
        browser.close()
