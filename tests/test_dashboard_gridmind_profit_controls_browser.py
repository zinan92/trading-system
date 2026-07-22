from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from services.dualtrack_execution_adapter import LegacyPaperExecutionAdapter
from services.journal_store import load_json, write_json
from tests.test_dashboard_gridmind_header_browser import _header_model
from tests.test_dualtrack_dt2_machine_runner import TEST_CONFIG
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


def _risky_preview() -> dict:
    response = _preview()
    preview = response["preview"]
    preview["grid"]["count"] = 24
    preview["solver"] = {
        "mode": "manual_adaptive",
        "preferred": {"recommended_leverage": 10},
        "risk_flags": [{
            "code": "grid_count_outside_preferred_band",
            "severity": "warning",
            "message": "24 格不在建议的 30–70 格内。",
        }],
        "alternatives": [
            {"id": "preserve_grid_count", "label": "保格数", "grid_count": 40, "planned_profit_per_grid": 4.0, "actual_leverage": 10.0, "selected": False},
            {"id": "preserve_profit_target", "label": "保收益", "grid_count": 24, "planned_profit_per_grid": 10.18, "actual_leverage": 9.99, "selected": True},
            {"id": "preserve_recommended_leverage", "label": "保杠杆", "grid_count": 24, "planned_profit_per_grid": 10.18, "actual_leverage": 9.99, "selected": True},
        ],
    }
    preview["manual_confirmation"] = {
        "schema_version": "grid-range-risk-ack-v1",
        "preview_id": preview["preview_id"],
        "scope": "paper_only",
        "required": True,
        "available": True,
        "facts_digest": "facts-1",
        "risk_snapshot_digest": "risk-1",
        "overridable_blocker_codes": ["candidate_grid_count_out_of_bounds"],
        "non_overridable_blocker_codes": [],
        "old": {
            "range_low": 3_800,
            "range_high": 4_300,
            "grid_count": 40,
            "notional_per_grid": 5_000,
            "min_net_profit_per_grid_usd": 10,
            "actual_leverage": 10,
            "estimated_margin": 10_000,
            "max_loss": 2_240,
        },
        "new": {
            "range_low": 3_900,
            "range_high": 4_100,
            "grid_count": 24,
            "notional_per_grid": 6_666.67,
            "min_net_profit_per_grid_usd": 10.18,
            "actual_leverage": 9.99,
            "estimated_margin": 9_997.2,
            "max_loss": 1_000,
        },
        "required_acknowledgements": [
            {"code": "specification_change", "severity": "warning", "title": "我确认网格规格变化", "summary": "Range 与格数发生变化。"},
            {"code": "maximum_loss_scenario", "severity": "critical", "title": "我确认预计最大损失", "summary": "预计最大损失为 1,000 USD。"},
            {"code": "grid_count_outside_preferred_band", "severity": "warning", "title": "我确认网格数量超出建议区间", "summary": "24 格低于建议的 30 格。"},
        ],
    }
    return response


def _reconciliation_blocked_preview(extra_issues: list[dict] | None = None) -> dict:
    response = _risky_preview()
    manual = response["preview"]["manual_confirmation"]
    manual.update({
        "available": False,
        "non_overridable_blocker_codes": ["execution_reconciliation_drift"],
        "non_overridable_blockers": [{
            "code": "execution_reconciliation_drift",
            "source": "canonical_execution.reconciliation",
            "message": "execution reconciliation is not clean",
            "evidence": {
                "accounting_status": "drift",
                "engine_status": "ok",
                "issues": [
                    {
                        "code": "closed_trade_exit_fill_missing",
                        "trade_id": "entry-trade-1",
                    },
                    {
                        "code": "orphan_exit_fill",
                        "fill_id": "flatten-fill-1",
                        "trade_id": "flatten-command-1",
                    },
                    *(extra_issues or []),
                ],
            },
        }],
    })
    return response


def _leverage_blocked_preview(*, mixed: bool) -> dict:
    response = _reconciliation_blocked_preview() if mixed else _risky_preview()
    manual = response["preview"]["manual_confirmation"]
    leverage = {
        "code": "manual_leverage_capacity_exceeded",
        "source": "adaptive_grid_solver",
        "message": "实际杠杆 29.99x，超过 Paper 手动容量 20x。",
        "evidence": {
            "actual_leverage": 29.99,
            "selected_leverage": 20.0,
            "manual_paper_leverage_limit": 20.0,
            "estimated_margin": 14_997.0,
            "equity": 10_000.0,
        },
    }
    manual["available"] = False
    manual["non_overridable_blocker_codes"] = [
        *manual.get("non_overridable_blocker_codes", []),
        "manual_leverage_capacity_exceeded",
    ]
    manual["non_overridable_blockers"] = [
        *manual.get("non_overridable_blockers", []),
        leverage,
    ]
    return response


def test_gridmind_explains_reconciliation_start_blocker_in_plain_language(
    tmp_path: Path,
) -> None:
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
    requests: list[dict] = []
    adapter = LegacyPaperExecutionAdapter(tmp_path / "outputs", config=TEST_CONFIG)
    adapter.submit_order({
        "cycle_id": "2026-07-05_DAY",
        "ts": "2026-07-05T01:02:00+00:00",
        "side": "buy",
        "event": "entry",
        "order_type": "market",
        "price": 100.0,
        "notional": 1_000.0,
        "sl": 95.0,
        "tp": 110.0,
        "source": "start_blocker_browser_test",
    })
    fills_path = (
        tmp_path
        / "outputs"
        / "dualtrack"
        / "fills"
        / "2026-07-05_DAY_human.json"
    )
    persisted_fills = load_json(fills_path)
    write_json(fills_path, [*persisted_fills, dict(persisted_fills[0])])
    actual_duplicate_issue = next(
        row
        for row in adapter.reconcile("2026-07-05_DAY")["issues"]
        if row["code"] == "duplicate_fill_id"
    )

    def fulfill_control(route) -> None:
        body = route.request.post_data_json
        requests.append(body)
        response = _risky_preview()
        if body["action"] == "prepare_start":
            response = {
                **_reconciliation_blocked_preview([
                    actual_duplicate_issue,
                    {
                        "code": "persisted_orders_mismatch",
                        "snapshot_count": 40,
                        "persisted_count": 39,
                    },
                ]),
                "action": "prepare_start",
                "prepared_start_id": "prepared-blocked-1",
            }
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(response, ensure_ascii=False),
        )

    with _static_server() as origin, playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True, channel="chrome")
        page = browser.new_page(viewport={"width": 1680, "height": 1050})
        page.add_init_script("window.setInterval = () => 0")
        page.route(
            "**/api/trading-system/read-model",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(model, ensure_ascii=False),
            ),
        )
        page.route(
            "**/api/dualtrack/market/bars?*",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({**model["market"], "bars": []}),
            ),
        )
        page.route("**/api/strategy-console/control", fulfill_control)
        page.goto(f"{origin}/dashboard-gridmind.html", wait_until="load")
        page.locator("#smartFill").click()
        page.locator("#previewSummary").wait_for(state="visible")
        page.locator("#startRobot").click()
        page.locator("#startRiskDialog").wait_for(state="visible")

        text = page.locator("#startRiskDialog").inner_text()
        assert "账本对账未通过" in text
        assert "已关闭交易缺少对应平仓成交" in text
        assert "存在无法归属到原交易的平仓成交" in text
        assert "成交记录 ID 重复" in text
        assert f"成交 ID：{actual_duplicate_issue['fill_ids'][0]}" in text
        assert "快照数量：40" in text
        assert "持久化数量：39" in text
        assert "先修复下方账本差异并重新核对" in text
        assert "机器码：execution_reconciliation_drift" in text
        assert page.locator("#confirmStartRisk").is_disabled()
        assert [row["action"] for row in requests] == ["preview", "prepare_start"]
        artifact_dir = os.environ.get("GRID_PROFIT_SCREENSHOT_DIR")
        if artifact_dir:
            path = Path(artifact_dir)
            path.mkdir(parents=True, exist_ok=True)
            page.locator("#startRiskDialog").screenshot(
                path=str(path / "issue-122-start-blocker-explanation.png")
            )
        browser.close()


@pytest.mark.parametrize(("mixed", "expected_cards"), [(False, 1), (True, 2)])
def test_gridmind_keeps_leverage_capacity_details_alone_and_with_ledger_blocker(
    mixed: bool,
    expected_cards: int,
) -> None:
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

    def fulfill_control(route) -> None:
        body = route.request.post_data_json
        response = _risky_preview()
        if body["action"] == "prepare_start":
            response = {
                **_leverage_blocked_preview(mixed=mixed),
                "action": "prepare_start",
                "prepared_start_id": "prepared-leverage-blocked",
            }
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(response, ensure_ascii=False),
        )

    with _static_server() as origin, playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True, channel="chrome")
        page = browser.new_page(viewport={"width": 1680, "height": 1050})
        page.add_init_script("window.setInterval = () => 0")
        page.route(
            "**/api/trading-system/read-model",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(model, ensure_ascii=False),
            ),
        )
        page.route(
            "**/api/dualtrack/market/bars?*",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({**model["market"], "bars": []}),
            ),
        )
        page.route("**/api/strategy-console/control", fulfill_control)
        page.goto(f"{origin}/dashboard-gridmind.html", wait_until="load")
        page.locator("#smartFill").click()
        page.locator("#previewSummary").wait_for(state="visible")
        page.locator("#startRobot").click()
        page.locator("#startRiskDialog").wait_for(state="visible")

        text = page.locator("#startRiskDialog").inner_text()
        assert page.locator(".start-blocker").count() == expected_cards
        assert "杠杆超过 Paper 手动容量" in text
        assert "实际杠杆：29.99" in text
        assert "Paper 手动上限：20" in text
        assert "机器码：manual_leverage_capacity_exceeded" in text
        assert ("账本对账未通过" in text) is mixed
        assert page.locator("#confirmStartRisk").is_disabled()
        browser.close()


def test_gridmind_sizing_controls_lock_manual_input_and_keep_other_values_auto() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    model = _header_model(4_000.0)
    model["runtime"].update({"actual_state": "stopped", "desired_state": "stopped"})
    browser_errors: list[str] = []
    preview_payloads: list[dict] = []

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
        preview_payloads.append(route.request.post_data_json)
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

        for selector in ("#gridCount", "#gridProfitTarget", "#gridNotional", "#leverage"):
            assert page.locator(selector).count() == 1
        assert page.locator("#outOfRange").count() == 0
        assert page.locator("#rangeLow").input_value() == "3900"
        assert page.locator("#rangeHigh").input_value() == "4100"
        assert page.locator("#gridCount").input_value() == "30"
        assert page.locator("#gridProfitTarget").input_value() == "10"
        assert page.locator("#gridNotional").input_value() == "6666.67"
        assert page.locator("#leverage").input_value() == "10"
        assert preview_payloads[-1]["solver"] == {
            "mode": "manual_adaptive",
            "locked": [],
            "current_grid_count": 50,
            "current_direction": "neutral",
        }

        request_count = len(preview_payloads)
        page.locator("#gridCount").fill("30.4")
        page.wait_for_timeout(450)
        assert len(preview_payloads) == request_count
        assert "网格数量必须是整数" in page.locator("#paramHint").inner_text()
        page.locator("#gridCount").fill("31")
        page.wait_for_timeout(450)
        assert "grid_count" in preview_payloads[-1]["solver"]["locked"]
        assert preview_payloads[-1]["grid"]["count"] == 31
        page.locator('[data-param-lock="grid_count"]').first.click()
        page.wait_for_timeout(100)
        assert "grid_count" not in preview_payloads[-1]["solver"]["locked"]
        summary = page.locator("#previewSummary").inner_text()
        assert "求解后网格\n30 格" in summary
        assert "最低计划净利\n10.18 USD" in summary
        assert "计划净利目标\n≥ 10 USD" in summary
        assert "实际杠杆\n10x / 建议 10x" in summary
        assert "预计最大损失\n1,000 USD" in summary
        missing_metrics = page.evaluate(
            """productionStrategySummaryModel({
                plan_id: "legacy-plan",
                plan_version: 1,
                direction: "neutral",
                direction_label: "中性",
                style_label: "稳健",
                grid_mode_label: "等价差",
                grid_count: 30,
                notional_per_grid: 5000,
                min_net_profit_per_grid_usd: null,
                actual_leverage: null,
                leverage: 10
            }, {actual_state: "running"}).parts"""
        )
        assert "计划净利 ≥ -- USD / 格" in missing_metrics
        assert "实际杠杆 --x（上限 10x）" in missing_metrics
        assert "实际杠杆 10x（上限 10x）" not in missing_metrics
        assert browser_errors == []

        artifact_dir = os.environ.get("GRID_PROFIT_SCREENSHOT_DIR")
        if artifact_dir:
            path = Path(artifact_dir)
            path.mkdir(parents=True, exist_ok=True)
            page.screenshot(
                path=str(path / "issue-97-parameter-controls.png"),
                full_page=True,
            )
        browser.close()


def test_gridmind_risky_preview_requires_every_ack_before_start() -> None:
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
    requests: list[dict] = []

    def fulfill_control(route) -> None:
        body = route.request.post_data_json
        requests.append(body)
        response = _risky_preview()
        if body["action"] == "prepare_start":
            response = {
                **response,
                "action": "prepare_start",
                "prepared_start_id": "prepared-browser-1",
            }
        elif body["action"] == "start":
            response = {
                "action": "start",
                "plan": {"version": 3},
                "preview": response["preview"],
                "created_orders": 24,
                "accepted_orders": 24,
                "filled_orders": 0,
            }
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(response, ensure_ascii=False),
        )

    with _static_server() as origin, playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True, channel="chrome")
        page = browser.new_page(viewport={"width": 1680, "height": 1050})
        page.add_init_script("window.setInterval = () => 0")
        page.route(
            "**/api/trading-system/read-model",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(model, ensure_ascii=False),
            ),
        )
        page.route(
            "**/api/dualtrack/market/bars?*",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({**model["market"], "bars": []}),
            ),
        )
        page.route("**/api/strategy-console/control", fulfill_control)
        page.goto(f"{origin}/dashboard-gridmind.html", wait_until="load")
        page.locator("#smartFill").click()
        page.locator("#previewSummary").wait_for(state="visible")
        page.locator("#startRobot").click()
        page.locator("#startRiskDialog").wait_for(state="visible")

        assert [row["action"] for row in requests] == [
            "preview",
            "prepare_start",
        ]
        assert page.locator("#confirmStartRisk").is_disabled()
        checks = page.locator("[data-start-risk-ack]")
        assert checks.count() == 3
        for index in range(checks.count()):
            checks.nth(index).check()
        assert page.locator("#confirmStartRisk").is_enabled()
        assert "1,000 USD" in page.locator("#startRiskDialog").inner_text()
        assert "24 格" in page.locator("#startRiskDialog").inner_text()
        assert "可比方案：保格数" in page.locator("#previewSummary").inner_text()
        page.locator("#confirmStartRisk").click()
        page.locator(".trade-toast").filter(has_text="机器人已启动").wait_for()
        assert [row["action"] for row in requests] == [
            "preview",
            "prepare_start",
            "start",
        ]
        assert requests[-1]["prepared_start_id"] == "prepared-browser-1"
        assert requests[-1]["expected_preview_id"] == "profit-preview-browser-1"
        assert requests[-1]["risk_acknowledgements"]["codes"] == [
            "grid_count_outside_preferred_band",
            "maximum_loss_scenario",
            "specification_change",
        ]
        artifact_dir = os.environ.get("GRID_PROFIT_SCREENSHOT_DIR")
        if artifact_dir:
            path = Path(artifact_dir)
            path.mkdir(parents=True, exist_ok=True)
            page.screenshot(
                path=str(path / "issue-97-adaptive-risk-confirmation.png"),
                full_page=True,
            )
        browser.close()


def test_gridmind_start_click_uses_prepared_candidate_and_shows_success() -> None:
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
    requests: list[dict] = []

    def fulfill_control(route) -> None:
        body = route.request.post_data_json
        requests.append(body)
        preview = _preview()
        if body["action"] == "prepare_start":
            response = {
                **preview,
                "action": "prepare_start",
                "prepared_start_id": "prepared-safe-browser-1",
            }
        else:
            response = {
                "action": "start",
                "plan": {"version": 4},
                "preview": preview["preview"],
                "created_orders": 30,
                "accepted_orders": 30,
                "filled_orders": 0,
            }
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(response, ensure_ascii=False),
        )

    with _static_server() as origin, playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True, channel="chrome")
        page = browser.new_page(viewport={"width": 1680, "height": 1050})
        page.add_init_script("window.setInterval = () => 0")
        page.route(
            "**/api/trading-system/read-model",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(model, ensure_ascii=False),
            ),
        )
        page.route(
            "**/api/dualtrack/market/bars?*",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({**model["market"], "bars": []}),
            ),
        )
        page.route("**/api/strategy-console/control", fulfill_control)
        page.goto(f"{origin}/dashboard-gridmind.html", wait_until="load")

        page.locator("#startRobot").click()
        page.locator(".trade-toast").filter(has_text="机器人已启动").wait_for()

        assert [row["action"] for row in requests] == ["prepare_start", "start"]
        assert requests[-1]["prepared_start_id"] == "prepared-safe-browser-1"
        assert requests[-1]["expected_preview_id"] == "profit-preview-browser-1"
        assert "当前接受 30 笔委托" in page.locator(".trade-toast").inner_text()
        browser.close()


def test_gridmind_start_surfaces_audited_stale_market_after_safe_prepare_retry() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    model = _header_model(4_000.0)
    model["runtime"].update({
        "actual_state": "stopped",
        "desired_state": "stopped",
        "status": "stopped",
        "can_start_when_authorized": True,
        "can_stop_when_authorized": False,
        "last_control_event": {
            "ts": "2026-07-22T10:59:00+00:00",
            "action": "stop",
            "result": "accepted",
            "error": None,
        },
    })
    model["execution"]["counts"].update({
        "open_order_count": 0,
        "accepted_order_count": 0,
        "open_position_count": 0,
    })
    requests: list[dict] = []

    def fulfill_control(route) -> None:
        body = route.request.post_data_json
        requests.append(body)
        assert body["action"] == "prepare_start"
        model["runtime"]["last_control_event"] = {
            "ts": f"2026-07-22T11:00:0{len(requests)}+00:00",
            "action": "prepare_start",
            "result": "rejected",
            "error": "market data is stale",
        }
        route.fulfill(status=502, content_type="application/json", body="{}")

    with _static_server() as origin, playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True, channel="chrome")
        page = browser.new_page(viewport={"width": 1680, "height": 1050})
        page.add_init_script("window.setInterval = () => 0")
        page.route(
            "**/api/trading-system/read-model",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(model, ensure_ascii=False),
            ),
        )
        page.route(
            "**/api/dualtrack/market/bars?*",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({**model["market"], "bars": []}),
            ),
        )
        page.route("**/api/strategy-console/control", fulfill_control)
        page.goto(f"{origin}/dashboard-gridmind.html", wait_until="load")

        page.locator("#startRobot").click()
        dialog = page.locator("#startFailureDialog[open]").filter(
            has_text="启动瞬间实时行情暂时过期"
        )
        dialog.wait_for()

        assert [row["action"] for row in requests] == [
            "prepare_start",
            "prepare_start",
        ]
        assert "HTTP 502" not in dialog.inner_text()
        assert "生产状态没有改变" in dialog.inner_text()
        assert page.locator(".trade-toast").filter(has_text="操作未完成").count() == 0
        assert page.evaluate("state.preparedStartId") is None
        assert model["runtime"]["actual_state"] == "stopped"
        browser.close()


def test_gridmind_start_does_not_retry_orders_and_surfaces_audited_market_move() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    model = _header_model(4_000.0)
    model["runtime"].update({
        "actual_state": "stopped",
        "desired_state": "stopped",
        "status": "stopped",
        "can_start_when_authorized": True,
        "can_stop_when_authorized": False,
        "last_control_event": {
            "ts": "2026-07-22T10:59:00+00:00",
            "action": "stop",
            "result": "accepted",
            "error": None,
        },
    })
    model["execution"]["counts"].update({
        "open_order_count": 0,
        "accepted_order_count": 0,
        "open_position_count": 0,
    })
    requests: list[dict] = []

    def fulfill_control(route) -> None:
        body = route.request.post_data_json
        requests.append(body)
        if body["action"] == "prepare_start":
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    **_preview(),
                    "action": "prepare_start",
                    "prepared_start_id": "prepared-before-market-move",
                }, ensure_ascii=False),
            )
            return
        assert body["action"] == "start"
        model["runtime"]["last_control_event"] = {
            "ts": "2026-07-22T11:00:01+00:00",
            "action": "start",
            "result": "rejected",
            "error": "prepared_start_market_moved",
        }
        route.fulfill(status=502, content_type="application/json", body="{}")

    with _static_server() as origin, playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True, channel="chrome")
        page = browser.new_page(viewport={"width": 1680, "height": 1050})
        page.add_init_script("window.setInterval = () => 0")
        page.route(
            "**/api/trading-system/read-model",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(model, ensure_ascii=False),
            ),
        )
        page.route(
            "**/api/dualtrack/market/bars?*",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({**model["market"], "bars": []}),
            ),
        )
        page.route("**/api/strategy-console/control", fulfill_control)
        page.goto(f"{origin}/dashboard-gridmind.html", wait_until="load")

        page.locator("#startRobot").click()
        dialog = page.locator("#startFailureDialog[open]").filter(
            has_text="行情已跨越网格线"
        )
        dialog.wait_for()

        assert [row["action"] for row in requests] == ["prepare_start", "start"]
        assert "HTTP 502" not in dialog.inner_text()
        assert "生产状态没有改变" in dialog.inner_text()
        assert page.locator(".trade-toast").filter(has_text="操作未完成").count() == 0
        assert page.evaluate("state.preparedStartId") is None
        assert model["runtime"]["actual_state"] == "stopped"
        browser.close()


def test_gridmind_start_invalidates_a_deferred_parameter_preview() -> None:
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
    requests: list[dict] = []

    def fulfill_control(route) -> None:
        body = route.request.post_data_json
        requests.append(body)
        preview = _preview()
        if body["action"] == "prepare_start":
            response = {
                **preview,
                "action": "prepare_start",
                "prepared_start_id": "prepared-after-deferred-preview",
            }
        else:
            response = {
                "action": "start",
                "plan": {"version": 5},
                "preview": preview["preview"],
                "created_orders": 30,
                "accepted_orders": 30,
                "filled_orders": 0,
            }
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(response, ensure_ascii=False),
        )

    stale = _preview()
    stale["preview"]["preview_id"] = "stale-deferred-preview"
    deferred_script = f"""
        const nativeFetch = window.fetch.bind(window);
        window.fetch = (input, init = {{}}) => {{
          let body = null;
          try {{ body = JSON.parse(init.body || "null"); }} catch (_error) {{}}
          if (String(input).includes('/api/strategy-console/control') && body?.action === 'preview') {{
            window.__deferredPreviewRequested = true;
            return new Promise(resolve => {{
              window.__releaseDeferredPreview = () => resolve(new Response(
                {json.dumps(json.dumps(stale, ensure_ascii=False))},
                {{status: 200, headers: {{'Content-Type': 'application/json'}}}}
              ));
            }});
          }}
          return nativeFetch(input, init);
        }};
    """

    with _static_server() as origin, playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True, channel="chrome")
        page = browser.new_page(viewport={"width": 1680, "height": 1050})
        page.add_init_script("window.setInterval = () => 0")
        page.add_init_script(deferred_script)
        page.route(
            "**/api/trading-system/read-model",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(model, ensure_ascii=False),
            ),
        )
        page.route(
            "**/api/dualtrack/market/bars?*",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({**model["market"], "bars": []}),
            ),
        )
        page.route("**/api/strategy-console/control", fulfill_control)
        page.goto(f"{origin}/dashboard-gridmind.html", wait_until="load")

        page.locator("#rangeLow").fill("3910")
        page.wait_for_function("window.__deferredPreviewRequested === true")
        page.locator("#startRobot").click()
        page.locator(".trade-toast").filter(has_text="机器人已启动").wait_for()
        page.evaluate("window.__releaseDeferredPreview()")
        page.wait_for_timeout(50)

        assert [row["action"] for row in requests] == ["prepare_start", "start"]
        assert requests[-1]["expected_preview_id"] == "profit-preview-browser-1"
        assert page.evaluate("state.preview") is None
        browser.close()
