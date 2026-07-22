from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from tests.test_dashboard_gridmind_header_browser import _header_model
from tests.test_dashboard_gridmind_order_lifecycle_browser import _static_server


def _market_page(*, count: int, start: datetime, historical: bool) -> dict:
    bars = []
    for index in range(count):
        close = 4000.0 + index * 0.1
        bars.append({
            "timestamp": (start + timedelta(minutes=30 * index)).isoformat(),
            "open": close - 0.2,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": 10 + index,
        })
    return {
        "schema_version": "dualtrack-market-bars-v1",
        "status": "ready",
        "source_mode": "binance_usdm_futures",
        "symbol": "GOLD",
        "provider_symbol": "XAUUSDT",
        "timeframe": "30m",
        "provider": "binance_usdm_futures",
        "quality_flags": ["public_api", "usd_m_futures", "live", "execution_venue"],
        "is_synthetic": False,
        "trusted": not historical,
        "fresh": not historical,
        "trusted_history": historical,
        "historical_page": historical,
        "bar_count": len(bars),
        "latest_close": bars[-1]["close"],
        "latest_timestamp": bars[-1]["timestamp"],
        "bars": bars,
        "pagination": {"has_more": False},
    }


@pytest.mark.parametrize(
    ("input_name", "newer_delta", "older_delta"),
    [
        ("mouse-wheel", (0, -120), (0, 120)),
        ("horizontal-trackpad", (120, 0), (-120, 0)),
    ],
)
def test_gridmind_wheel_or_trackpad_loads_beyond_initial_240_bars(
    input_name: str,
    newer_delta: tuple[int, int],
    older_delta: tuple[int, int],
) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    live = _market_page(
        count=240,
        start=datetime(2026, 7, 17, tzinfo=timezone.utc),
        historical=False,
    )
    history = _market_page(
        count=320,
        start=datetime(2026, 7, 10, 8, tzinfo=timezone.utc),
        historical=True,
    )
    model = _header_model(live["latest_close"])
    model["market"] = live
    model.setdefault("ui_capabilities", {})["market_timeframes"] = [
        "1m", "5m", "15m", "30m", "1h", "4h",
    ]
    history_requests = 0

    def fulfill_market(route) -> None:
        nonlocal history_requests
        query = parse_qs(urlparse(route.request.url).query)
        timeframe = query.get("timeframe", ["30m"])[0]
        if timeframe == "1d":
            payload = {**live, "timeframe": "1d", "bars": live["bars"][-2:]}
        elif query.get("end"):
            history_requests += 1
            payload = history
        else:
            payload = live
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payload, ensure_ascii=False),
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

        source = page.locator(".standard-kline-source")
        assert "240 bars" in source.inner_text()
        for _ in range(12):
            page.locator('[data-action="pan-left"]').click()

        chart = page.locator("#productionChart")
        box = chart.bounding_box()
        assert box is not None
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)

        # Moving back toward newer candles at the oldest edge must not spend a
        # historical request merely because a wheel event occurred.
        page.mouse.wheel(*newer_delta)
        page.wait_for_timeout(300)
        assert history_requests == 0
        assert "240 bars" in source.inner_text()

        # A real wheel/trackpad movement toward older candles arms exactly one
        # trusted historical page and preserves the fresh live snapshot fields.
        page.mouse.wheel(*older_delta)
        page.locator("#chartHistoryNotice").filter(has_text="已向前加载 320 根").wait_for()

        assert history_requests == 1
        assert "560 bars" in source.inner_text()
        assert "行情 blocked" not in page.locator("#gate").inner_text()
        artifact_dir = os.environ.get("GRID_HISTORY_SCREENSHOT_DIR")
        if artifact_dir and input_name == "mouse-wheel":
            path = Path(artifact_dir)
            path.mkdir(parents=True, exist_ok=True)
            page.screenshot(
                path=str(path / "issue-99-history-beyond-240.png"),
                full_page=True,
            )
        browser.close()
