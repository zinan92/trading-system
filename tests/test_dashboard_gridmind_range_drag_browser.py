from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

import pytest

from tests.test_dashboard_gridmind_header_browser import _header_model
from tests.test_dashboard_gridmind_order_lifecycle_browser import _static_server


def _bars(timeframe: str) -> dict:
    if timeframe == "1d":
        rows = [{
            "timestamp": "2026-07-18T00:00:00+00:00",
            "open": 3980.0,
            "high": 4020.0,
            "low": 3970.0,
            "close": 4000.0,
        }]
    else:
        start = datetime(2026, 7, 17, tzinfo=timezone.utc)
        rows = []
        for index in range(48):
            close = 4000.0 + ((index % 9) - 4) * 8.0
            rows.append({
                "timestamp": (start + timedelta(minutes=30 * index)).isoformat(),
                "open": close - 1.0,
                "high": max(close + 10.0, 4110.0 if index == 10 else close + 10.0),
                "low": min(close - 10.0, 3890.0 if index == 11 else close - 10.0),
                "close": close,
                "volume": 10 + index,
            })
    return {
        "symbol": "GOLD",
        "timeframe": timeframe,
        "provider": "binance_usdm_futures",
        "provider_symbol": "XAUUSDT",
        "trusted": True,
        "latest_close": rows[-1]["close"],
        "latest_timestamp": rows[-1]["timestamp"],
        "bars": rows,
    }


def _preview(request: dict) -> dict:
    low = float(request["range"]["low"])
    high = float(request["range"]["high"])
    old = {
        "range_low": 3900.0,
        "range_high": 4100.0,
        "range_width": 200.0,
        "mode": "arithmetic",
        "grid_count": 50,
        "spacing": 4.0,
        "spacing_ratio": None,
        "notional_per_grid": 2800.0,
        "total_grid_notional": 140000.0,
        "estimated_margin": 14000.0,
        "actual_leverage": 1.4,
        "max_loss": 1000.0,
        "min_net_profit_per_grid_usd": 10.35,
        "target_net_profit_per_grid_usd": 10.0,
    }
    new = deepcopy(old)
    new.update({
        "range_low": low,
        "range_high": high,
        "range_width": high - low,
        "spacing": (high - low) / 50,
    })
    return {
        "action": "preview_range",
        "preview": {
            "schema_version": "grid-range-drag-preview-v1",
            "expected_strategy_plan_id": "plan-7",
            "expected_strategy_plan_version": 7,
            "preview_id": "range-preview-browser-1",
            "geometry": {
                "handle": request["handle"],
                "new_range": {"low": low, "high": high},
            },
            "old": old,
            "new": new,
            "can_apply": True,
            "can_apply_with_acknowledgements": True,
            "confirm_disabled_reasons": [],
            "manual_confirmation": {
                "schema_version": "grid-range-risk-ack-v1",
                "preview_id": "range-preview-browser-1",
                "scope": "paper_only",
                "required": True,
                "available": True,
                "facts_digest": "browser-safe-facts",
                "risk_snapshot_digest": "browser-safe-risk",
                "required_acknowledgements": [
                    {
                        "code": "specification_change",
                        "severity": "warning",
                        "title": "我确认网格规格变化",
                        "summary": "Range、间距和每格计划净利将按新规格变化。",
                    },
                    {
                        "code": "maximum_loss_scenario",
                        "severity": "critical",
                        "title": "我确认预计最大损失",
                        "summary": "预计最大损失 1000 → 1000 USD。",
                    },
                ],
                "overridable_blocker_codes": [],
                "non_overridable_blocker_codes": [],
            },
            "risk_recalculation": {
                "available": False,
                "applied_to_preview": False,
            },
            "order_delta": {
                "cancel_pending_entries": 25,
                "submit_new_entries": 50,
            },
            "positions": {"open_count": 0, "preview_effect": "none"},
            "tp_sl": {
                "existing_orders_affected_by_preview": False,
                "candidate_orders_recomputed": True,
            },
            "side_effects": {
                "orders_created": 0,
                "orders_cancelled": 0,
                "positions_changed": 0,
                "strategy_plan_written": False,
                "risk_decision_persisted": False,
            },
        },
    }


def _risky_preview(request: dict) -> dict:
    response = _preview(request)
    preview = response["preview"]
    preview["can_apply"] = False
    preview["confirm_disabled_reasons"] = [
        "market_price_outside_range",
        "grid_profit_target_not_met",
        "projected_leverage_exceeded",
        "projected_margin_exceeded",
        "preview_profit_or_capital_target_not_met",
    ]
    preview["new"].update(
        {
            "min_net_profit_per_grid_usd": 2.0,
            "estimated_margin": 20002.71,
            "actual_leverage": 19.99,
            "max_loss": 2890.4,
        }
    )
    preview["manual_confirmation"] = {
        "schema_version": "grid-range-risk-ack-v1",
        "preview_id": preview["preview_id"],
        "scope": "paper_only",
        "required": True,
        "available": True,
        "facts_digest": "browser-risky-facts",
        "risk_snapshot_digest": "browser-risky-risk",
        "required_acknowledgements": [
            {
                "code": "specification_change",
                "severity": "warning",
                "title": "我确认网格规格变化",
                "summary": "Range 宽度 200 → 115 USD；单格间距与计划净利会变化。",
            },
            {
                "code": "maximum_loss_scenario",
                "severity": "critical",
                "title": "我确认预计最大损失",
                "summary": "预计最大损失 1000 → 2890.4 USD；不包含极端滑点和资金费。",
            },
            {
                "code": "profit_target_shortfall",
                "severity": "critical",
                "title": "我确认每格计划净利低于自动目标",
                "summary": "每格计划净利 10.35 → 2 USD；自动目标至少 10 USD。",
            },
            {
                "code": "leverage_and_margin_risk",
                "severity": "critical",
                "title": "我确认杠杆与保证金风险",
                "summary": "实际杠杆 1.4x → 19.99x；预计保证金升至 20002.71 USD。",
            },
            {
                "code": "market_outside_range",
                "severity": "critical",
                "title": "我确认当前价位于新 Range 外",
                "summary": "新网格可能立即形成单边暴露或长期没有预期成交。",
            },
        ],
        "overridable_blocker_codes": [
            "grid_profit_target_not_met",
            "market_price_outside_range",
            "projected_leverage_exceeded",
            "projected_margin_exceeded",
        ],
        "non_overridable_blocker_codes": [],
    }
    return response


def test_gridmind_drag_release_keeps_draft_until_explicit_confirm() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    model = _header_model(4000.0)
    control_requests: list[dict] = []
    browser_errors: list[str] = []

    def fulfill_read_model(route) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(model, ensure_ascii=False),
        )

    def fulfill_bars(route) -> None:
        timeframe = route.request.url.split("timeframe=", 1)[1].split("&", 1)[0]
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_bars(timeframe)),
        )

    def fulfill_control(route) -> None:
        body = route.request.post_data_json
        control_requests.append(body)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_preview(body)),
        )

    with _static_server() as origin, playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True, channel="chrome")
        except Exception as exc:  # pragma: no cover - local browser dependency
            pytest.skip(f"Playwright Chromium unavailable: {exc}")
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on(
            "console",
            lambda message: browser_errors.append(message.text)
            if message.type == "error"
            else None,
        )
        page.on("pageerror", lambda error: browser_errors.append(str(error)))
        page.add_init_script("window.setInterval = () => 0")
        page.route("**/api/trading-system/read-model", fulfill_read_model)
        page.route("**/api/dualtrack/market/bars?*", fulfill_bars)
        page.route("**/api/strategy-console/control", fulfill_control)
        page.goto(f"{origin}/dashboard-gridmind.html", wait_until="load")
        page.locator("#gridAdjustToggle").wait_for(state="visible")
        page.evaluate("() => refreshMarket()")
        assert page.evaluate("() => [state.market?.timeframe,state.market?.bars?.length]") == [
            "1m",
            48,
        ]
        page.locator("#gridAdjustToggle").click()
        overlay = page.locator(".grid-adjust-overlay.on")
        overlay.wait_for(state="visible")

        interior = page.locator(".grid-hit.interior")
        box = interior.bounding_box()
        assert box is not None and box["height"] > 20
        x = box["x"] + min(160, box["width"] / 2)
        y = box["y"] + box["height"] / 2
        page.mouse.move(x, y)
        page.mouse.down()
        page.mouse.move(x, y - 26, steps=5)
        page.mouse.up()

        assert control_requests == []
        assert page.evaluate("() => state.gridDraft.dirty") is True
        first = page.evaluate("() => ({low:state.gridDraft.low,high:state.gridDraft.high})")
        refreshed_interior = page.locator(".grid-hit.interior").bounding_box()
        assert refreshed_interior is not None
        page.mouse.move(
            refreshed_interior["x"] + min(160, refreshed_interior["width"] / 2),
            refreshed_interior["y"] + refreshed_interior["height"] / 2,
        )
        page.mouse.down()
        page.evaluate(
            """() => {
                const pointerId=state.gridDrag?.pointerId;
                window.dispatchEvent(new PointerEvent('pointercancel',{pointerId}));
            }"""
        )
        page.mouse.up()
        assert page.evaluate("() => state.gridDrag") is None
        assert page.evaluate("() => ({low:state.gridDraft.low,high:state.gridDraft.high})") == first

        upper_box = page.locator(".grid-hit.upper").bounding_box()
        assert upper_box is not None and upper_box["height"] == 16
        upper_x = upper_box["x"] + min(160, upper_box["width"] / 2)
        upper_y = upper_box["y"] + upper_box["height"] / 2
        page.mouse.move(upper_x, upper_y)
        page.mouse.down()
        page.mouse.move(upper_x + upper_box["width"] + 80, upper_y - 12, steps=4)
        page.mouse.up()
        second = page.evaluate("() => ({low:state.gridDraft.low,high:state.gridDraft.high})")
        assert second["low"] == first["low"]
        assert second["high"] != first["high"]

        lower_box = page.locator(".grid-hit.lower").bounding_box()
        assert lower_box is not None and lower_box["height"] == 16
        lower_x = lower_box["x"] + min(160, lower_box["width"] / 2)
        lower_y = lower_box["y"] + lower_box["height"] / 2
        page.mouse.move(lower_x, lower_y)
        page.mouse.down()
        page.mouse.move(lower_x, lower_y + 12, steps=4)
        page.mouse.up()
        third = page.evaluate("() => ({low:state.gridDraft.low,high:state.gridDraft.high})")
        assert third["low"] != second["low"]
        assert third["high"] == second["high"]
        assert page.evaluate("() => state.gridDrag") is None
        assert control_requests == []
        assert set(
            page.locator(".grid-ghost-line").evaluate_all(
                "lines => lines.map(line => line.dataset.gridSide)"
            )
        ) == {"buy", "sell"}
        assert page.locator(".grid-ghost-line.split").count() == 1
        page.evaluate(
            """() => {
              state.gridDraft.direction = "long";
              renderGridAdjustOverlay();
            }"""
        )
        assert set(
            page.locator(".grid-ghost-line").evaluate_all(
                "lines => lines.map(line => line.dataset.gridSide)"
            )
        ) == {"buy"}
        page.evaluate(
            """() => {
              state.gridDraft.direction = "short";
              renderGridAdjustOverlay();
            }"""
        )
        assert set(
            page.locator(".grid-ghost-line").evaluate_all(
                "lines => lines.map(line => line.dataset.gridSide)"
            )
        ) == {"sell"}
        page.evaluate(
            """() => {
              state.gridDraft.direction = "neutral";
              renderGridAdjustOverlay();
            }"""
        )
        assert page.locator(".grid-draft-actions").is_visible()
        assert page.locator("#gridRangeReviewDialog").evaluate("dialog => dialog.open") is False

        artifact_dir = os.environ.get("GRID_RANGE_SCREENSHOT_DIR")
        if artifact_dir:
            path = Path(artifact_dir)
            path.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(path / "issue-66-range-draft.png"), full_page=True)

        page.locator('[data-grid-action="confirm"]').click()
        page.locator("#gridRangeReviewDialog[open]").wait_for(state="visible")
        assert len(control_requests) == 1
        assert control_requests[0]["action"] == "preview_range"
        assert page.locator("#gridRangeGate").inner_text().startswith("请逐项勾选")
        assert page.locator("#gridRangeComparison").inner_text().count("→") >= 10
        assert page.locator("[data-grid-risk-ack]").count() == 2
        assert page.locator("#executeGridRangeReplacement").is_disabled()
        page.locator("[data-grid-risk-ack]").all()[0].check()
        page.locator("[data-grid-risk-ack]").all()[1].check()
        assert page.locator("#gridRangeGate").inner_text().startswith("全部规格已确认")
        if artifact_dir:
            page.screenshot(
                path=str(Path(artifact_dir) / "issue-66-replacement-card.png"),
                full_page=True,
            )
        assert page.locator("#executeGridRangeReplacement").inner_text() == (
            "停止+平仓+撤单+交易新网格"
        )
        page.locator("#executeGridRangeReplacement").click()
        page.wait_for_function("() => state.gridAdjustMode === false")
        assert len(control_requests) == 2
        assert control_requests[1]["action"] == "replace_grid"
        assert control_requests[1]["expected_preview_id"] == (
            "range-preview-browser-1"
        )
        assert control_requests[1]["expected_strategy_plan_id"] == "plan-7"
        assert control_requests[1]["expected_strategy_plan_version"] == 7
        assert control_requests[1]["expected_execution"] == {
            "accepted_order_ids": ["order-lifecycle-browser-1"],
            "open_position_ids": [],
        }
        assert control_requests[1]["risk_acknowledgements"] == {
            "schema_version": "grid-range-risk-ack-v1",
            "preview_id": "range-preview-browser-1",
            "facts_digest": "browser-safe-facts",
            "risk_snapshot_digest": "browser-safe-risk",
            "codes": ["maximum_loss_scenario", "specification_change"],
        }
        assert page.evaluate("() => [state.gridAdjustMode,state.gridDraft]") == [False, None]
        assert page.locator(".grid-adjust-overlay.on").count() == 0
        before_pan_requests = len(control_requests)
        chart_box = page.locator(".standard-kline-canvas").bounding_box()
        assert chart_box is not None
        page.mouse.move(chart_box["x"] + 300, chart_box["y"] + 200)
        page.mouse.down()
        page.mouse.move(chart_box["x"] + 360, chart_box["y"] + 200, steps=4)
        page.mouse.up()
        assert page.evaluate("() => state.gridDraft") is None
        assert len(control_requests) == before_pan_requests
        assert browser_errors == []
        browser.close()


def test_gridmind_risky_range_requires_every_human_confirmation() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    model = _header_model(4000.0)
    control_requests: list[dict] = []

    def fulfill_read_model(route) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(model, ensure_ascii=False),
        )

    def fulfill_bars(route) -> None:
        timeframe = route.request.url.split("timeframe=", 1)[1].split("&", 1)[0]
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_bars(timeframe)),
        )

    def fulfill_control(route) -> None:
        body = route.request.post_data_json
        control_requests.append(body)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_risky_preview(body), ensure_ascii=False),
        )

    with _static_server() as origin, playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True, channel="chrome")
        except Exception as exc:  # pragma: no cover - local browser dependency
            pytest.skip(f"Playwright Chromium unavailable: {exc}")
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.add_init_script("window.setInterval = () => 0")
        page.route("**/api/trading-system/read-model", fulfill_read_model)
        page.route("**/api/dualtrack/market/bars?*", fulfill_bars)
        page.route("**/api/strategy-console/control", fulfill_control)
        page.goto(f"{origin}/dashboard-gridmind.html", wait_until="load")
        page.locator("#gridAdjustToggle").click()
        page.evaluate(
            """() => {
              state.gridDraft.low = 3990;
              state.gridDraft.high = 4105;
              state.gridDraft.dirty = true;
              renderGridAdjustOverlay();
            }"""
        )
        page.locator('[data-grid-action="confirm"]').click()
        page.locator("#gridRangeReviewDialog[open]").wait_for(state="visible")
        assert page.locator("[data-grid-risk-ack]").count() == 5
        assert page.locator("#gridRangeGate").inner_text().startswith("请逐项勾选")
        assert page.locator("#executeGridRangeReplacement").is_disabled()
        card_text = page.locator("#gridRangeReviewDialog").inner_text()
        assert "每格计划净利 10.35 → 2 USD" in card_text
        assert "实际杠杆 1.4x → 19.99x" in card_text
        assert "预计最大损失 1000 → 2890.4 USD" in card_text
        assert "market_price_outside_range" not in card_text
        for checkbox in page.locator("[data-grid-risk-ack]").all():
            checkbox.check()
        assert page.locator("#executeGridRangeReplacement").is_enabled()
        assert page.locator("#gridRangeGate").inner_text().startswith(
            "高风险参数已逐项确认"
        )
        artifact_dir = os.environ.get("GRID_RANGE_SCREENSHOT_DIR")
        if artifact_dir:
            path = Path(artifact_dir)
            path.mkdir(parents=True, exist_ok=True)
            page.screenshot(
                path=str(path / "issue-92-risk-confirmation.png"),
                full_page=True,
            )
        page.locator("#executeGridRangeReplacement").click()
        page.wait_for_function("() => state.gridAdjustMode === false")
        assert control_requests[-1]["action"] == "replace_grid"
        assert control_requests[-1]["risk_acknowledgements"]["codes"] == [
            "leverage_and_margin_risk",
            "market_outside_range",
            "maximum_loss_scenario",
            "profit_target_shortfall",
            "specification_change",
        ]
        assert control_requests[-1]["risk_acknowledgements"]["facts_digest"] == (
            "browser-risky-facts"
        )
        browser.close()
