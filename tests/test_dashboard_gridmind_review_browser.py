from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.test_dashboard_gridmind_header_browser import _header_model
from tests.test_dashboard_gridmind_order_lifecycle_browser import _static_server


def _review_model() -> dict:
    model = _header_model(4_010.0)
    plan = dict(model["strategy"]["plan"])
    plan.update({
        "key_levels": [3900.0, 4000.0, 4100.0],
        "tp_sl": {"take_profit": 4010.0, "stop_loss": 3823.0},
    })
    cycle_id = "2026-07-23_NIGHT"
    package = {
        "cycle_id": cycle_id,
        "status": "closed",
        "package_hash": "sha256:review-browser-evidence",
        "window": {"start": "2026-07-23T12:00:00+00:00", "end": "2026-07-24T00:00:00+00:00"},
        "strategy_plan": plan,
        "execution": {
            "engine": "nautilus_paper",
            "order_count": 25,
            "fill_count": 8,
            "pnl": {"unrealized": 0.0},
            "reconciliation": {"status": "pass", "issues": []},
        },
        "review": {"realized_pnl": 12.5, "reconciliation_status": "pass"},
    }
    model["review"] = {
        "selected_cycle_id": cycle_id,
        "cycle_packages": [package],
        "ledger": {"recent_reviews": []},
    }
    model["research"]["strategy_shadows"] = [{
        "cycle_id": cycle_id,
        "variant_id": "notional-half",
        "status": "blocked",
        "input_hash": "sha256:other-market-history",
        "scenario": {"hashes": {"market_event_hash": "sha256:other-market-history"}},
        "metrics": {"net_pnl": 30.0, "trade_count": 2},
    }]
    return model


def test_gridmind_12_hour_review_is_readable_and_never_invents_a_counterfactual_delta() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    model = _review_model()
    browser_errors: list[str] = []

    with _static_server() as origin, playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True, channel="chrome")
        except Exception as exc:  # pragma: no cover - local browser availability
            pytest.skip(f"Playwright Chromium unavailable: {exc}")
        page = browser.new_page(viewport={"width": 1680, "height": 1050})
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
        page.goto(f"{origin}/dashboard-gridmind.html", wait_until="load")
        page.locator('[data-tab="review"]').click()
        review = page.locator("#review")
        review.wait_for()
        text = review.inner_text()

        for label in ("当时计划", "判断", "结果与分析", "方向", "网格规格", "执行", "PnL", "关键位", "TP / SL", "反事实"):
            assert label in text
        assert "12.5 USD" in text
        assert "对账通过" in text
        assert "不可比较" in text
        assert "30" not in text.split("反事实", 1)[-1].split("下一周期", 1)[0]

        artifact_dir = os.environ.get("GRID_REVIEW_SCREENSHOT_DIR")
        if artifact_dir:
            path = Path(artifact_dir)
            path.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(path / "issue-352-12-hour-review.png"), full_page=True)
        assert browser_errors == []
        browser.close()
