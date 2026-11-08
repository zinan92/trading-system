from __future__ import annotations

from copy import deepcopy
import json

import pytest

from tests.test_dashboard_gridmind_order_lifecycle_browser import (
    _read_model,
    _static_server,
)
from tests.trade_page import open_manual, press, text


def _header_model(
    price: float,
    *,
    trusted: bool = True,
    provider: str = "binance_usdm_futures",
    risk_blocked: bool = False,
    risk_acknowledged: bool = False,
    execution_tick_health: dict | None = None,
) -> dict:
    model = deepcopy(_read_model("accepted"))
    model["market"].update({
        "provider": provider,
        "provider_symbol": "XAUUSDT",
        "timeframe": "1m",
        "trusted": trusted,
        "latest_close": price,
        "latest_timestamp": "2026-07-18T01:02:00+00:00",
    })
    model["runtime"].update({
        "status": "running",
        "status_label": "运行中",
        "actual_state": "running",
        "desired_state": "running",
        "stale_cycle": False,
    })
    model["completeness"] = {"status": "complete", "issues": []}
    if execution_tick_health is not None:
        model["runtime"]["execution_tick_health"] = execution_tick_health
    if risk_blocked:
        model["risk"]["outcome"] = "block"
        model["risk"]["blockers"] = ["max_plan_loss_exceeded"]
    if risk_acknowledged:
        decision_id = "risk-decision-acknowledged-running"
        preview_id = "grid-preview-acknowledged-running"
        model["risk"].update({
            "displayed_decision_id": decision_id,
            "expected_decision_id": decision_id,
        })
        model["runtime"].update({
            "preview_id": preview_id,
            "risk_decision_id": decision_id,
            "last_control_event": {
                "action": "start",
                "result": "accepted",
                "request": {
                    "risk_acknowledgements": {
                        "schema_version": "grid-range-risk-ack-v1",
                        "preview_id": preview_id,
                        "codes": [
                            "leverage_and_margin_risk",
                            "maximum_loss_scenario",
                            "specification_change",
                        ],
                    },
                },
                "runtime_after": {"actual_state": "running"},
            },
        })
    return model


def _minute_feed(price: float, step: int, *, provider: str = "binance_usdm_futures", trusted: bool = True) -> dict:
    """One 1m K-line snapshot: since 0b93ab8 the header follows the selected chart feed (#1288)."""

    timestamp = f"2026-07-18T01:{step:02d}:00+00:00"
    return {
        "symbol": "GOLD",
        "timeframe": "1m",
        "provider": provider,
        "provider_symbol": "XAUUSDT",
        "trusted": trusted,
        "status": "ok" if trusted else "untrusted",
        "latest_close": price,
        "latest_timestamp": timestamp,
        "bars": [{"timestamp": timestamp, "open": price, "high": price, "low": price, "close": price}],
    }


def test_gridmind_header_tracks_trusted_ticks_daily_change_and_run_gate() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    # The header price follows the 1m chart feed; the run badge still follows the read model.
    # Both advance one step per load() after the chart is switched to 1m.
    minutes = [
        _minute_feed(4000.0, 0),
        _minute_feed(4001.0, 1),
        _minute_feed(4001.0, 2),
        _minute_feed(3999.0, 3),
        _minute_feed(5000.0, 4, provider="backup_venue"),
        _minute_feed(5001.0, 5, provider="backup_venue"),
        _minute_feed(5001.0, 6, provider="backup_venue"),
        _minute_feed(5002.0, 7, provider="backup_venue", trusted=False),
        _minute_feed(5003.0, 8),
    ]
    responses = [
        _header_model(4000.0),
        _header_model(4000.0),
        _header_model(4001.0),
        _header_model(4001.0),
        _header_model(3999.0),
        _header_model(5000.0, provider="backup_venue"),
        _header_model(
            5001.0,
            provider="backup_venue",
            risk_blocked=True,
            risk_acknowledged=True,
        ),
        _header_model(5001.0, provider="backup_venue", risk_blocked=True),
        _header_model(5002.0, provider="backup_venue", trusted=False),
        _header_model(
            5003.0,
            execution_tick_health={
                "status": "blocked",
                "reason": "heartbeat_stale",
                "age_seconds": 181,
                "max_age_seconds": 180,
            },
        ),
    ]
    response_index = 0
    browser_errors: list[str] = []

    def fulfill_read_model(route) -> None:
        nonlocal response_index
        payload = responses[min(response_index, len(responses) - 1)]
        response_index += 1
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payload, ensure_ascii=False),
        )

    def fulfill_bars(route) -> None:
        timeframe = route.request.url.split("timeframe=", 1)[1].split("&", 1)[0]
        if timeframe == "1m":
            # A load may read the 1m feed more than once; key it to the load's read-model step
            # (step 1 is the switch to 1m, so it gets minute 0).
            payload = minutes[min(max(response_index - 2, 0), len(minutes) - 1)]
        elif timeframe == "1d":
            payload = {
                "symbol": "GOLD",
                "timeframe": "1d",
                "provider": "binance_usdm_futures",
                "provider_symbol": "XAUUSDT",
                "trusted": True,
                "latest_timestamp": "2026-07-18T00:00:00+00:00",
                "bars": [{
                    "timestamp": "2026-07-18T00:00:00+00:00",
                    "open": 3960.0,
                    "high": 4010.0,
                    "low": 3950.0,
                    "close": 4000.0,
                }],
            }
        else:
            payload = {
                "symbol": "GOLD",
                "timeframe": timeframe,
                "provider": "binance_usdm_futures",
                "provider_symbol": "XAUUSDT",
                "trusted": True,
                "latest_close": 4000.0,
                "latest_timestamp": "2026-07-18T01:00:00+00:00",
                "bars": [],
            }
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payload),
        )

    with _static_server() as origin, playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True, channel="chrome")
        except Exception as exc:  # pragma: no cover - local browser dependency
            pytest.skip(f"Playwright Chromium unavailable: {exc}")
        page = browser.new_page()
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
        page.goto(f"{origin}/trade", wait_until="load")
        open_manual(page)  # /trade keeps GridMind's form inside 「手动配置与 AI 助手」 (#1419)
        page.locator("#marketBadge.running").wait_for(state="attached")

        assert page.locator("#headerPair").inner_text() == "XAU / USDT"
        assert page.locator("#headerPrice").inner_text() == "4,000"
        assert page.locator("#headerChange").inner_text() == "+1.01%"
        assert page.locator("#marketBadge").text_content() == "运行中"
        assert page.locator("#healthButton").count() == 0

        page.evaluate("() => { state.timeframe = '1m'; return load(); }")
        assert page.locator("#headerPrice").inner_text() == "4,000"
        assert page.locator("#headerPrice").get_attribute("title").startswith("最新 1m 行情")

        classes: list[str] = []
        for _ in range(3):
            page.evaluate("() => load()")
            classes.append(page.locator("#headerPrice").get_attribute("class") or "")
        assert classes == ["tick-up", "tick-up", "tick-down"]

        page.evaluate("() => load()")  # another venue: no tick against the old venue's price
        assert page.locator("#headerPrice").inner_text() == "5,000"
        assert page.locator("#headerPrice").get_attribute("class") == ""
        assert page.locator("#marketBadge").text_content() == "运行中"

        page.evaluate("() => load()")
        assert page.locator("#marketBadge").text_content() == "运行中"

        page.evaluate("() => load()")
        assert page.locator("#marketBadge").text_content() == "运行异常"

        page.evaluate("() => load()")
        assert page.locator("#headerPrice").get_attribute("class") == ""  # an untrusted feed never ticks
        assert "running" not in (
            page.locator("#marketBadge").get_attribute("class") or ""
        ).split()
        assert page.locator("#marketBadge").text_content() == "运行异常"

        page.evaluate("() => load()")
        assert page.locator("#marketBadge").text_content() == "运行降级"
        assert browser_errors == []
        browser.close()


@pytest.mark.parametrize("case", ["untrusted_market", "read_model_502", "risk_blocked", "heartbeat_stale"])
def test_trade_shows_park_the_failure_the_hidden_header_pills_carry(case: str) -> None:
    """/trade hides GridMind's header pills (#1294); the banner says what they said (#1419, #1429). Timers run, as
    on the real page (the banner refreshes every second). No failure state offers 「开始今天的计划」."""
    playwright = pytest.importorskip("playwright.sync_api")
    model = {
        "untrusted_market": lambda: _header_model(5002.0, provider="backup_venue", trusted=False),
        "read_model_502": lambda: _header_model(4000.0),
        "risk_blocked": lambda: _header_model(5001.0, provider="backup_venue", risk_blocked=True),
        "heartbeat_stale": lambda: _header_model(5003.0, execution_tick_health={
            "status": "blocked", "reason": "heartbeat_stale", "age_seconds": 181, "max_age_seconds": 180}),
    }[case]()

    def fulfill_read_model(route) -> None:
        if case == "read_model_502":
            route.fulfill(status=502, content_type="text/plain", body="bad gateway")
        else:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(model, ensure_ascii=False))

    def fulfill_bars(route) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps({**model["market"], "bars": []}))

    with _static_server() as origin, playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True, channel="chrome")
        except Exception as exc:  # pragma: no cover - local browser dependency
            pytest.skip(f"Playwright Chromium unavailable: {exc}")
        page = browser.new_page(viewport={"width": 1680, "height": 1050})
        page.route("**/api/trading-system/read-model", fulfill_read_model)
        page.route("**/api/dualtrack/market/bars?*", fulfill_bars)
        page.goto(f"{origin}/trade", wait_until="load")
        banner = page.locator("#deskStatus")
        title = {
            "untrusted_market": "黄金行情中断，已停止开新仓",
            "read_model_502": "读不到交易状态",
            "risk_blocked": "运行状态异常",
            "heartbeat_stale": "执行心跳中断",
        }[case]
        banner.locator("h1", has_text=title).wait_for(state="visible")
        page.wait_for_timeout(1500)  # a banner tick or more: the state holds
        assert banner.locator("h1").inner_text() == title
        copy = banner.inner_text()
        if case == "untrusted_market":
            assert "不会改用别的数据源顶替" in copy and "页面会自动重试" in copy
            assert page.locator("#deskHealth").inner_text().startswith("行情没有通过可信检查")
        if case == "read_model_502":
            assert "Dashboard 服务暂不可用" in copy and "HTTP 502" in copy
            assert page.locator("#deskHealth").inner_text() == "读不到最新状态"
            # The chart's notice names the same cause, in the same words.
            assert "HTTP 502" in page.locator("#chartHistoryNotice").inner_text()
        if case == "heartbeat_stale":
            assert "执行心跳 181 秒没更新（上限 180 秒）" in copy
        assert "开始今天的计划" not in copy
        assert page.locator("#deskStatusAction", has_text="开始").count() == 0
        browser.close()


def test_a_failed_read_survives_any_redraw_until_the_next_good_read() -> None:
    """#1429: GridMind keeps the failure in its state, so a redraw from old data (a market refresh, a tab switch)
    never brings back the old running state while reads still fail. Only a good read clears it."""
    playwright = pytest.importorskip("playwright.sync_api")
    model = _header_model(4000.0)
    answers = ["ok", "502", "ok"]

    def fulfill_read_model(route) -> None:
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if answer == "502":
            route.fulfill(status=502, content_type="text/plain", body="bad gateway")
        else:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(model, ensure_ascii=False))

    def fulfill_bars(route) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps({**model["market"], "bars": []}))

    with _static_server() as origin, playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True, channel="chrome")
        except Exception as exc:  # pragma: no cover - local browser dependency
            pytest.skip(f"Playwright Chromium unavailable: {exc}")
        page = browser.new_page(viewport={"width": 1680, "height": 1050})
        page.add_init_script("window.setInterval = () => 0")
        page.route("**/api/trading-system/read-model", fulfill_read_model)
        page.route("**/api/dualtrack/market/bars?*", fulfill_bars)
        page.goto(f"{origin}/trade", wait_until="load")
        page.wait_for_function("() => window.gridmindView?.data && window.gridmindView.readError === null")
        assert page.evaluate("() => window.gridmindView.runState.label") == "运行中"

        page.evaluate("() => load()")  # the 502
        assert "HTTP 502" in page.evaluate("() => window.gridmindView.readError")
        assert page.evaluate("() => Boolean(window.gridmindView.data)")  # old data kept, marked old
        page.evaluate("() => render()")  # a redraw from old data
        assert "HTTP 502" in page.evaluate("() => window.gridmindView.readError")

        page.evaluate("() => load()")  # a good read clears it
        assert page.evaluate("() => window.gridmindView.readError") is None
        browser.close()
