from __future__ import annotations

import json

import pytest

from services.dca_plan import build_deterministic_dca_candidate_payload_v1
from tests.test_dashboard_gridmind_header_browser import _header_model
from tests.test_dashboard_gridmind_order_lifecycle_browser import _static_server


def _dca_preview(payload: dict) -> dict:
    dca = payload["dca"]
    levels = [round(float(value), 2) for value in dca["entry_levels"]]
    count = int(dca["max_additions"])
    notional = float(dca["notional_per_addition"])
    total = count * notional
    target = float(dca["target_price"])
    stop = float(dca["stop_price"])
    direction = payload["direction"]
    return {
        "schema_version": "strategy-dca-preview-v1",
        "strategy_type": "dca",
        "preview_id": "dca-ui-preview-1",
        "cycle_id": "2026-07-22_DAY",
        "direction": direction,
        "market": {"price": 4_000, "symbol": "GOLD", "timeframe": "1m"},
        "dca": {
            "entry_levels": levels,
            "max_additions": count,
            "entry_count": count,
            "notional_per_addition": notional,
            "total_possible_notional": total,
            "target_price": target,
            "stop_price": stop,
            "loop_enabled": False,
        },
        "entries": [
            {
                "preview_entry_id": f"dca-entry-{index:02d}",
                "side": "buy" if direction == "long" else "sell",
                "price": price,
                "notional": notional,
            }
            for index, price in enumerate(levels, start=1)
        ],
        "depth_economics": [
            {
                "depth": depth,
                "target_net_pnl_usd": round(20.0 * depth, 2),
                "stop_net_pnl_usd": round(-24.0 * depth, 2),
            }
            for depth in range(1, count + 1)
        ],
        "risk": {
            "selected_leverage": float(payload["risk_budget"]["leverage"]),
            "leverage_limit": 10,
            "actual_leverage_at_full_depth": total / 10_000,
            "estimated_margin_at_full_depth": total / float(payload["risk_budget"]["leverage"]),
            "maximum_loss_at_full_depth": 24.0 * count,
            "capacity_exceeded": total / 10_000 > 10,
            "risk_flags": [],
        },
        "manual_confirmation": {
            "schema_version": "dca-risk-ack-v1",
            "scope": "paper_only",
            "required": True,
            "available": True,
            "preview_id": "dca-ui-preview-1",
            "facts_digest": "dca-facts-1",
            "required_acknowledgements": [
                {
                    "code": "dca_accumulation_specification",
                    "severity": "warning",
                    "title": "我确认 DCA 补仓与整轮止盈规格",
                    "summary": f"最多 {count} 次，全部筹码在 {target} 统一退出。",
                },
                {
                    "code": "dca_maximum_loss",
                    "severity": "critical",
                    "title": "我确认满仓与最大损失情景",
                    "summary": f"预计最大损失 {24.0 * count} USD。",
                },
            ],
        },
    }


def test_gridmind_dca_smart_fill_preview_risk_ack_and_start() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    model = _header_model(4_000.0)
    model["runtime"].update({
        "actual_state": "stopped",
        "desired_state": "stopped",
        "status": "stopped",
        "can_start_when_authorized": True,
        "can_stop_when_authorized": False,
    })
    model["execution"]["counts"].update({"open_order_count": 0, "open_position_count": 0})
    browser_errors: list[str] = []
    requests: list[dict] = []

    def fulfill_read_model(route) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(model))

    def fulfill_market(route) -> None:
        timeframe = route.request.url.split("timeframe=", 1)[1].split("&", 1)[0]
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({**model["market"], "timeframe": timeframe, "bars": []}),
        )

    def fulfill_control(route) -> None:
        payload = route.request.post_data_json
        requests.append(payload)
        preview = _dca_preview(payload)
        action = payload["action"]
        if action == "preview":
            body = {"action": action, "preview": preview}
        elif action == "prepare_start":
            body = {"action": action, "preview": preview, "prepared_start_id": "prepared-dca-1"}
        else:
            assert action == "start"
            body = {
                "action": action,
                "preview": preview,
                "plan": {"strategy_type": "dca", "version": 7},
                "created_orders": preview["dca"]["max_additions"],
                "accepted_orders": preview["dca"]["max_additions"],
                "filled_orders": 0,
            }
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    with _static_server() as origin, playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True, channel="chrome")
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"Playwright Chromium unavailable: {exc}")
        page = browser.new_page(viewport={"width": 1680, "height": 1050})
        page.on("console", lambda message: browser_errors.append(message.text) if message.type == "error" else None)
        page.on("pageerror", lambda error: browser_errors.append(str(error)))
        page.add_init_script("window.setInterval = () => 0")
        page.route("**/api/trading-system/read-model", fulfill_read_model)
        page.route("**/api/dualtrack/market/bars?*", fulfill_market)
        page.route("**/api/strategy-console/control", fulfill_control)
        page.goto(f"{origin}/dashboard-gridmind.html", wait_until="load")

        page.locator('[data-strategy-type="dca"]').click()
        page.locator("#previewSummary").wait_for(state="visible")
        assert page.locator('[data-direction="neutral"]').is_disabled()
        assert page.locator('[data-direction="long"]').get_attribute("class").endswith("on")
        assert page.locator("#rangeLow").input_value() == "3920"
        assert page.locator("#rangeHigh").input_value() == "3994"
        assert page.locator("#dcaTarget").input_value() == "4040"
        assert page.locator("#dcaStop").input_value() == "3880"
        assert requests[-1]["strategy_type"] == "dca"
        expected_candidate = build_deterministic_dca_candidate_payload_v1(
            direction="long",
            market_price=4_000,
        )
        assert requests[-1]["dca"] == expected_candidate["dca"]
        assert requests[-1]["risk_budget"] == expected_candidate["risk_budget"]
        assert "满仓目标净利\n120 USD" in page.locator("#previewSummary").inner_text()
        assert "达到目标或止损后停止" in page.locator("#dcaModeNote").inner_text()
        assert page.locator("#gridAdjustToggle").is_disabled()

        page.locator("#gridCount").fill("4")
        page.locator("#gridNotional").fill("2500")
        page.locator("#dcaTarget").fill("4050")
        page.wait_for_timeout(450)
        assert requests[-1]["dca"]["max_additions"] == 4
        assert requests[-1]["dca"]["notional_per_addition"] == 2500
        assert requests[-1]["dca"]["target_price"] == 4050

        page.locator("#startRobot").click()
        page.locator("#startRiskDialog").wait_for(state="visible")
        review = page.locator("#startRiskDialog").inner_text()
        assert "DCA 参数 · 风险确认" in review
        assert "满仓总名义" in review
        assert "整轮止盈 / 止损" in review
        assert "预计最大损失" in review
        assert page.locator("#confirmStartRisk").is_disabled()
        for checkbox in page.locator("[data-start-risk-ack]").all():
            checkbox.check()
        assert page.locator("#confirmStartRisk").is_enabled()
        page.locator("#confirmStartRisk").click()
        page.wait_for_timeout(100)
        start_payload = [row for row in requests if row["action"] == "start"][-1]
        assert start_payload["risk_acknowledgements"] == {
            "schema_version": "dca-risk-ack-v1",
            "preview_id": "dca-ui-preview-1",
            "facts_digest": "dca-facts-1",
            "codes": ["dca_accumulation_specification", "dca_maximum_loss"],
        }
        model["strategy"]["summary"] = {
            "strategy_type": "dca",
            "dca_entry_levels": [4004, 3996, 3988],
            "dca_entry_count": 3,
            "notional_per_addition": 2000,
            "target_price": 4050,
            "stop_price": 3970,
        }
        model["execution"]["dca_lifecycle"] = {
            "status": "open",
            "status_label": "聚合止盈已保护",
            "protection_semantics": "event_driven_aggregate_target_not_entry_order",
            "open_quantity": 1.001,
            "active_target": {"generation": 2, "status": "accepted", "side": "sell", "quantity": 1.001, "price": 4050},
        }
        model["strategy"]["plan"].update({"strategy_type": "dca", "direction": "long"})
        model["runtime"].update({"actual_state": "running", "desired_state": "running", "status": "running"})
        model["risk"].update({"status": "current", "outcome": "approved", "blockers": []})
        model["execution"]["orders"] = []
        model["execution"]["positions"] = [{"status": "open"}, {"status": "open"}]
        model["execution"]["counts"].update({"open_order_count": 0, "accepted_order_count": 0, "open_position_count": 2})
        page.reload(wait_until="load")
        assert "聚合 TP #2：1.001 @ 4,050（行情触发）" in page.locator("#gridSummary").inner_text()
        assert page.locator("#marketBadge").inner_text() == "运行中"

        model["execution"]["dca_lifecycle"]["active_target"] = None
        page.reload(wait_until="load")
        assert page.locator("#marketBadge").inner_text() == "运行异常"
        assert browser_errors == []
        browser.close()


def test_gridmind_dca_smart_fill_matches_half_cent_boundary() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    market_price = 3_906.25
    model = _header_model(market_price)
    model["runtime"].update(
        {
            "actual_state": "stopped",
            "desired_state": "stopped",
            "status": "stopped",
        }
    )
    requests: list[dict] = []

    def fulfill_control(route) -> None:
        payload = route.request.post_data_json
        requests.append(payload)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "action": "preview",
                    "preview": _dca_preview(payload),
                }
            ),
        )

    with _static_server() as origin, playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(
                headless=True,
                channel="chrome",
            )
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"Playwright Chromium unavailable: {exc}")
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.add_init_script("window.setInterval = () => 0")
        page.route(
            "**/api/trading-system/read-model",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(model),
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
        page.route(
            "**/api/strategy-console/control",
            fulfill_control,
        )
        page.goto(f"{origin}/dashboard-gridmind.html", wait_until="load")
        page.locator('[data-strategy-type="dca"]').click()
        page.locator("#previewSummary").wait_for(state="visible")
        browser.close()

    expected = build_deterministic_dca_candidate_payload_v1(
        direction="long",
        market_price=market_price,
    )
    assert requests[-1]["dca"] == expected["dca"]
    assert requests[-1]["risk_budget"] == expected["risk_budget"]


def test_python_dca_builder_matches_browser_across_gold_cent_grid() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(
                headless=True,
                channel="chrome",
            )
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"Playwright Chromium unavailable: {exc}")
        page = browser.new_page()
        page.goto("about:blank")
        page.evaluate(
            """
            () => {
              window.checkDcaParity = rows => {
              for (const row of rows) {
                const [cents, direction, ...expected] = row;
                const price = cents / 100;
                const long = direction === "long";
                const low = Number(
                  (price * (long ? .98 : 1.0015)).toFixed(2)
                );
                const high = Number(
                  (price * (long ? .9985 : 1.02)).toFixed(2)
                );
                const target = Number(
                  (price * (long ? 1.01 : .99)).toFixed(2)
                );
                const stop = Number(
                  (price * (long ? .97 : 1.03)).toFixed(2)
                );
                const step = (high - low) / 5;
                const levels = Array.from(
                  {length: 6},
                  (_, index) => long
                    ? high - index * step
                    : low + index * step
                );
                const actual = [...levels, target, stop];
                if (
                  actual.some(
                    (value, index) => value !== expected[index]
                  )
                ) {
                  return {cents, direction, expected, actual};
                }
              }
              return null;
              };
            }
            """
        )
        start_cents = 350_000
        end_cents = 450_000
        batch_size = 5_000
        mismatch = None
        rows: list[list[object]] = []
        for cents in range(start_cents, end_cents + 1):
            for direction in ("long", "short"):
                candidate = (
                    build_deterministic_dca_candidate_payload_v1(
                        direction=direction,
                        market_price=cents / 100,
                    )
                )
                dca = candidate["dca"]
                rows.append(
                    [
                        cents,
                        direction,
                        *dca["entry_levels"],
                        dca["target_price"],
                        dca["stop_price"],
                    ]
                )
                if len(rows) >= batch_size:
                    mismatch = page.evaluate(
                        "rows => window.checkDcaParity(rows)",
                        rows,
                    )
                    rows = []
                    if mismatch is not None:
                        break
            if mismatch is not None:
                break
        if mismatch is None and rows:
            mismatch = page.evaluate(
                "rows => window.checkDcaParity(rows)",
                rows,
            )
        browser.close()

    assert mismatch is None


def test_gridmind_strategy_type_selection_is_explicit_and_exclusive() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    model = _header_model(4_000.0)

    with _static_server() as origin, playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True, channel="chrome")
        page = browser.new_page(viewport={"width": 1680, "height": 1050})
        page.add_init_script("window.setInterval = () => 0")
        page.route(
            "**/api/trading-system/read-model",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(model),
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
        page.route(
            "**/api/strategy-console/control",
            lambda route: route.fulfill(
                status=400,
                content_type="application/json",
                body=json.dumps({"error": "preview_fixture_rejected"}),
            ),
        )
        page.goto(f"{origin}/dashboard-gridmind.html", wait_until="load")
        grid = page.locator('#strategyTypeChoices [data-strategy-type="grid"]')
        dca = page.locator('#strategyTypeChoices [data-strategy-type="dca"]')
        assert grid.count() == 1
        assert dca.count() == 1
        assert grid.get_attribute("aria-pressed") == "true"
        assert dca.get_attribute("aria-pressed") == "false"
        assert "on" in (grid.get_attribute("class") or "")

        dca.click()
        assert dca.get_attribute("aria-pressed") == "true"
        assert grid.get_attribute("aria-pressed") == "false"
        assert "on" in (dca.get_attribute("class") or "")
        assert "on" not in (grid.get_attribute("class") or "")
        assert page.locator("#strategyCard").get_attribute("data-strategy-type") == "dca"

        grid.click()
        assert grid.get_attribute("aria-pressed") == "true"
        assert dca.get_attribute("aria-pressed") == "false"
        assert "on" in (grid.get_attribute("class") or "")
        assert "on" not in (dca.get_attribute("class") or "")
        assert page.locator("#strategyCard").get_attribute("data-strategy-type") == "grid"
        browser.close()
