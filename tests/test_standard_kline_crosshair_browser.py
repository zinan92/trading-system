from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re

import pytest

from tests.test_dashboard_gridmind_header_browser import _header_model
from tests.test_dashboard_gridmind_history_browser import _market_page
from tests.test_dashboard_gridmind_order_lifecycle_browser import _static_server


def test_crosshair_datetime_is_rendered_on_the_bottom_time_axis() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    market = _market_page(
        count=240,
        start=datetime(2026, 7, 17, tzinfo=timezone.utc),
        historical=False,
    )
    model = _header_model(market["latest_close"])
    model["market"] = market
    model.setdefault("ui_capabilities", {})["market_timeframes"] = [
        "1m", "5m", "15m", "30m", "1h", "4h",
    ]

    def fulfill_market(route) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(market, ensure_ascii=False),
        )

    with playwright.sync_playwright() as runtime, _static_server() as base_url:
        browser = runtime.chromium.launch(headless=True, channel="chrome")
        page = browser.new_page(viewport={"width": 1450, "height": 1000})
        page.set_default_timeout(8_000)
        page.route(
            "**/api/trading-system/read-model",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(model, ensure_ascii=False),
            ),
        )
        page.route("**/api/dualtrack/market/bars?*", fulfill_market)
        page.goto(f"{base_url}/dashboard-gridmind.html", wait_until="networkidle")

        chart = page.locator(".standard-kline-canvas")
        bounds = chart.bounding_box()
        assert bounds is not None
        crosshair_x = bounds["x"] + bounds["width"] * 0.52
        page.mouse.move(crosshair_x, bounds["y"] + bounds["height"] * 0.45)

        label = page.locator("[data-crosshair-time-axis]")
        label.wait_for(state="visible")
        label_text = label.inner_text()
        label_bounds = label.bounding_box()
        assert re.search(r"2026.*07.*\d{2}.*\d{2}:\d{2}", label_text)
        assert label_bounds is not None
        assert label_bounds["y"] >= bounds["y"] + bounds["height"] - 30
        assert abs((label_bounds["x"] + label_bounds["width"] / 2) - crosshair_x) < 90
        assert page.locator(".standard-kline-toolbar [data-crosshair-time]").count() == 0

        # Changing the displayed strategy geometry must request a fresh price
        # autoscale. The revised grid boundary remains in the chart coordinate
        # system rather than retaining a stale, fixed price scale.
        scale = page.evaluate(
            """() => {
              const plan = state.data.strategy.plan;
              const revisedHigh = Number(state.market.latest_close) - 5;
              state.data.strategy.plan = {
                ...plan,
                range: {...plan.range, high: revisedHigh},
              };
              redrawChart();
              return {
                autoScale: state.chart.candleSeries.priceScale().options().autoScale,
                revisedBoundaryY: Number(state.chart.priceToY(revisedHigh)),
                visualKey: state.chartVisualKey,
              };
            }"""
        )
        assert scale["autoScale"] is True
        assert scale["visualKey"].endswith(":production")
        assert 0 <= scale["revisedBoundaryY"] <= bounds["height"]

        artifact_dir = os.environ.get("KLINE_TIME_AXIS_SCREENSHOT_DIR")
        if artifact_dir:
            path = Path(artifact_dir)
            path.mkdir(parents=True, exist_ok=True)
            page.screenshot(
                path=str(path / "issue-100-crosshair-time-axis.png"),
                full_page=True,
            )

        page.mouse.move(2, 2)
        label.wait_for(state="hidden")
        browser.close()
