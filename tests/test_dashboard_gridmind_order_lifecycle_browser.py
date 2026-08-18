from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Thread

import pytest

from services.trading_system_read_model import project_trading_system_read_model
from tests.test_trading_system_read_model import _broker, _risk, _source


ROOT = Path(__file__).resolve().parents[1]


class _QuietStaticHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return


@contextmanager
def _static_server():
    handler = partial(_QuietStaticHandler, directory=str(ROOT))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _transitions(states: list[str]) -> list[dict[str, str]]:
    return [
        {"from": states[index - 1] if index else "", "to": state}
        for index, state in enumerate(states)
    ]


def _read_model(
    state: str | None,
    *,
    cycle_id: str = "2026-07-18_DAY",
    history: list[str] | None = None,
    full_orders: bool = False,
    open_trade: bool = False,
    park: dict | None = None,
) -> dict:
    source = deepcopy(_source(open_trade=open_trade))
    source["cycle"]["cycle_id"] = cycle_id
    source["production_execution"]["cycle_id"] = cycle_id
    source["runtime"]["cycle_id"] = cycle_id
    if full_orders:
        orders = source["production_execution"]["orders"]
        orders[0].update({
            "quantity": 1.25,
            "updated_at": "2026-07-18T01:02:00+00:00",
        })
    else:
        orders = [{
            "order_id": "hostile-order",
            "state": '<img src=x onerror="globalThis.pwned=true">',
            "side": "sell",
            "order_type": "limit",
        }]
        if state is not None:
            states = history or ["entry", "submitting", state]
            orders.append({
                "order_id": "order-lifecycle-browser-1",
                "state": state,
                "transitions": _transitions(states),
                "side": "buy",
                "order_type": "limit",
                "quantity": 1.0,
                "price": 3999.0,
            })
    source["production_execution"]["orders"] = orders
    if open_trade:
        position = source["production_execution"]["positions"][0]
        position.update({
            "entry_ts": "2026-07-18T01:00:00+00:00",
            "remaining_units": 1.25,
            "tp": 4010.0,
            "sl": 3980.0,
            "unrealized_pnl": 12.34,
        })
        source["production_execution"]["accounting_snapshot"]["positions"][0].update(position)
    model = project_trading_system_read_model(
        source,
        risk_decision=_risk(),
        broker=_broker(),
        park=park,
        generated_at="2026-07-18T01:02:04+00:00",
    ).to_dict()
    hostile = next(
        (row for row in model["execution"]["orders"] if row["order_id"] == "hostile-order"),
        None,
    )
    if hostile is not None:
        hostile["state_label"] = '<img src=x onerror="globalThis.pwned=true">'
    return model


def test_gridmind_order_lifecycle_is_monotonic_in_the_real_dom() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    accepted = ["entry", "submitting", "accepted"]
    partial = [*accepted, "partially_filled"]
    cancelled = [*partial, "cancelled"]
    protection_failed_6 = [*accepted, "filled", "protective_attached", "protective_failed"]
    protection_attached_7 = [*protection_failed_6, "protective_attached"]
    protection_failed_8 = [*protection_attached_7, "protective_failed"]
    protection_attached_9 = [*protection_failed_8, "protective_attached"]
    responses = [
        _read_model("accepted", history=accepted),
        _read_model("partially_filled", history=partial),
        _read_model("cancelled", history=cancelled),
        _read_model("accepted", history=accepted),
        _read_model(None),
        _read_model("accepted", cycle_id="2026-07-18_NIGHT", history=accepted),
        _read_model(None, cycle_id="2026-07-18_NIGHT"),
        _read_model("protective_failed", cycle_id="2026-07-19_DAY", history=protection_failed_8),
        _read_model("protective_failed", cycle_id="2026-07-19_DAY", history=protection_failed_6),
        _read_model("protective_attached", cycle_id="2026-07-19_DAY", history=protection_attached_7),
        _read_model("protective_attached", cycle_id="2026-07-19_DAY", history=protection_attached_9),
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

    def fulfill_market(route) -> None:
        timeframe = route.request.url.split("timeframe=", 1)[1].split("&", 1)[0]
        payload = {
            **responses[0]["market"],
            "timeframe": timeframe,
            "bars": [],
        }
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payload, ensure_ascii=False),
        )

    with _static_server() as origin, playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True, channel="chrome")
        except Exception as exc:  # pragma: no cover - depends on local browser install
            pytest.skip(f"Playwright Chromium unavailable: {exc}")
        page = browser.new_page()
        page.on("console", lambda message: browser_errors.append(message.text) if message.type == "error" else None)
        page.on("pageerror", lambda error: browser_errors.append(str(error)))
        page.add_init_script("window.setInterval = () => 0")
        page.route("**/api/trading-system/read-model", fulfill_read_model)
        page.route("**/api/dualtrack/market/bars?*", fulfill_market)
        page.goto(f"{origin}/dashboard-gridmind.html", wait_until="load")
        page.locator("#orders tbody tr").first.wait_for(state="attached")

        labels: list[str] = []
        open_counts: list[int] = []
        table_counts: list[str] = []
        for index in range(5):
            if index:
                page.evaluate("() => load({withMarket:false})")
            labels.append(page.locator("#orders tbody tr").first.locator("td").first.inner_text())
            open_counts.append(page.evaluate("() => state.data.execution.open_orders.length"))
            table_counts.append(page.locator("#ordersCount").inner_text())

        assert labels == ["已接受", "部分成交", "暂无记录", "暂无记录", "暂无记录"]
        assert open_counts == [1, 1, 0, 1, 0]
        assert table_counts == ["(1)", "(1)", "(0)", "(0)", "(0)"]
        assert page.locator("#orders img").count() == 0
        assert page.evaluate("() => globalThis.pwned === true") is False
        assert "ok" not in (page.locator("#runBadge").get_attribute("class") or "").split()
        assert page.locator("#runBadge").inner_text() == "运行异常"

        page.evaluate("() => load({withMarket:false})")
        assert page.locator("#orders tbody tr").first.locator("td").first.inner_text() == "已接受"
        assert page.locator("#ordersCount").inner_text() == "(1)"
        assert page.evaluate("() => state.orderLifecycle.cycleId") == "2026-07-18_NIGHT"

        page.evaluate("() => load({withMarket:false})")
        assert page.locator("#orders tbody tr").first.locator("td").first.inner_text() == "暂无记录"
        assert page.locator("#ordersCount").inner_text() == "(0)"

        protection_states: list[tuple[str, int]] = []
        for _index in range(4):
            page.evaluate("() => load({withMarket:false})")
            protection_states.append(page.evaluate(
                """() => {
                    const row=state.orderLifecycle.byId.get('order-lifecycle-browser-1');
                    return [row.state_label,row.state_revision];
                }"""
            ))
        assert protection_states == [
            ["保护异常", 8],
            ["保护异常", 8],
            ["保护异常", 8],
            ["保护已挂", 9],
        ]
        assert browser_errors == []
        browser.close()


def test_gridmind_running_summary_and_execution_tables_show_authoritative_fields() -> None:
    """A stopped/partial page must not hide the operator's live execution facts."""

    playwright = pytest.importorskip("playwright.sync_api")
    model = _read_model("accepted", full_orders=True, open_trade=True)
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
            body=json.dumps({**model["market"], "timeframe": timeframe, "bars": []}),
        )

    with _static_server() as origin, playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True, channel="chrome")
        except Exception as exc:  # pragma: no cover - depends on local browser install
            pytest.skip(f"Playwright Chromium unavailable: {exc}")
        page = browser.new_page(viewport={"width": 1680, "height": 1050})
        page.on("console", lambda message: browser_errors.append(message.text) if message.type == "error" else None)
        page.on("pageerror", lambda error: browser_errors.append(str(error)))
        page.add_init_script("window.setInterval = () => 0")
        page.route("**/api/trading-system/read-model", fulfill_read_model)
        page.route("**/api/dualtrack/market/bars?*", fulfill_market)
        page.goto(f"{origin}/dashboard-gridmind.html", wait_until="load")
        page.locator("#positions tbody tr").first.wait_for(state="attached")

        summary = page.locator("#productionStrategySummary").inner_text()
        assert "运行中" in summary
        assert "中性（双边）" in summary
        assert "稳健" in summary
        assert "等价差 · 50 格" in summary
        assert "每格 2,800 USD" in summary
        assert "计划净利 ≥ 10.35 USD / 格" in summary
        assert "实际杠杆 9.8x（上限 10x）" in summary

        assert page.locator("#positionsCount").inner_text() == "(1)"
        assert page.locator("#ordersCount").inner_text() == "(25)"
        assert page.locator("#tradesCount").inner_text() == "(1)"
        positions = page.locator("#positions").inner_text()
        assert "数量" in positions and "1.25" in positions
        assert "止盈" in positions and "4,010" in positions
        assert "止损" in positions and "3,980" in positions
        assert "未实现" in positions and "+12.34" in positions
        orders = page.locator("#orders").inner_text()
        assert "数量" in orders and "止盈" in orders and "止损" in orders
        assert "计划净利" in orders and "10.5 USD" in orders
        fills = page.locator("#fills").inner_text()
        assert "入场时间（北京）" in fills and "出场时间（北京）" in fills
        assert "结果" in fills and "已实现" in fills and "持仓中" in fills
        assert browser_errors == []
        browser.close()


def test_gridmind_shows_the_active_park_strategy_at_the_top() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    park = {
        "generated_at": "2026-08-18T01:02:04+00:00",
        "status": "ok",
        "blockers": [],
        "strategy": {
            "active": True,
            "state": "RUNNING",
            "strategy_session_id": "session-browser-park",
            "strategy_revision_id": "revision-browser-4",
            "plan_digest": "sha256:browser-plan",
            "strategy_type": "grid",
            "direction": "short",
            "lower_price_boundary": 3800.0,
            "upper_price_boundary": 4000.0,
            "stop_price": 4000.0,
            "take_profit_price": None,
            "maximum_leverage": 10.0,
            "maximum_acceptable_loss": 500.0,
            "maximum_notional": 40_000.0,
            "theoretical_max_loss": 480.0,
            "order_count": 19,
            "selected_constraint": "maximum_acceptable_loss",
            "grid_entry_range": {"lower": 3810.0, "upper": 3990.0},
            "grid_spacing": 10.0,
            "grid_rung_count": 19,
            "grid_rung_prices": [3810.0, 3820.0, 3830.0],
        },
        "execution": {
            "counts": {
                "accepted_orders": 12,
                "filled_orders": 3,
                "fills": 7,
                "open_positions": 2,
                "closed_positions": 1,
            },
            "reconciliation": {"status": "ok", "issues": []},
        },
        "recording": {
            "status": "complete",
            "record_window_id": "2026-08-18_DAY",
            "package_status": "complete",
            "next_action": "review_recorded_evidence",
            "blocker_code": None,
        },
        "market": {"price": 3910.0, "fresh": True},
        "safety": {"status": "pass", "age_seconds": 4.0},
    }
    model = _read_model("accepted", full_orders=True, park=park)
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
            body=json.dumps({**model["market"], "timeframe": timeframe, "bars": []}),
        )

    with _static_server() as origin, playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True, channel="chrome")
        except Exception as exc:  # pragma: no cover - depends on local browser install
            pytest.skip(f"Playwright Chromium unavailable: {exc}")
        page = browser.new_page(viewport={"width": 1680, "height": 1050})
        page.on("console", lambda message: browser_errors.append(message.text) if message.type == "error" else None)
        page.on("pageerror", lambda error: browser_errors.append(str(error)))
        page.add_init_script("window.setInterval = () => 0")
        page.route("**/api/trading-system/read-model", fulfill_read_model)
        page.route("**/api/dualtrack/market/bars?*", fulfill_market)
        page.goto(f"{origin}/dashboard-gridmind.html", wait_until="load")
        page.locator("#currentStrategyCard").wait_for(state="visible")

        card = page.locator("#currentStrategyCard")
        copy = card.inner_text()
        assert page.evaluate(
            "() => document.querySelector('#currentStrategyCard').compareDocumentPosition(document.querySelector('#parkAiChatCard')) & Node.DOCUMENT_POSITION_FOLLOWING"
        )
        assert "当前运行策略" in copy
        assert "运行中" in copy
        assert "Short · Grid" in copy
        assert "3,800 – 4,000" in copy
        assert "Grid Entry Range" in copy and "3,810 – 3,990" in copy
        assert "Grid Spacing" in copy and "10 · 19 Rungs" in copy
        assert "Hard Stop 4,000" in copy
        assert "10x" in copy
        assert "最大理论亏损" in copy and "480 USD" in copy
        assert "12 挂单" in copy and "7 成交" in copy and "2 持仓" in copy
        assert "session-browser-park" in copy
        assert "revision-browser-4" in copy
        assert "行情可信" in copy and "对账 ok" in copy and "4 秒前" in copy
        assert "Recording 2026-08-18_DAY · complete" in copy
        assert "Draft" not in copy and "推荐" not in copy
        assert browser_errors == []
        browser.close()


def test_gridmind_surfaces_legacy_exposure_as_a_visible_migration_blocker() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    model = _read_model("accepted", full_orders=True, open_trade=True)
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
            body=json.dumps({**model["market"], "timeframe": timeframe, "bars": []}),
        )

    with _static_server() as origin, playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True, channel="chrome")
        except Exception as exc:  # pragma: no cover - depends on local browser install
            pytest.skip(f"Playwright Chromium unavailable: {exc}")
        page = browser.new_page(viewport={"width": 1680, "height": 1050})
        page.on("console", lambda message: browser_errors.append(message.text) if message.type == "error" else None)
        page.on("pageerror", lambda error: browser_errors.append(str(error)))
        page.add_init_script("window.setInterval = () => 0")
        page.route("**/api/trading-system/read-model", fulfill_read_model)
        page.route("**/api/dualtrack/market/bars?*", fulfill_market)
        page.goto(f"{origin}/dashboard-gridmind.html", wait_until="load")
        page.locator("#currentStrategyCard").wait_for(state="visible")

        copy = page.locator("#currentStrategyCard").inner_text()
        assert "现有执行暴露" in copy
        assert "迁移阻塞" in copy
        assert "25 挂单" in copy and "1 持仓" in copy
        assert "Park 身份缺失" in copy
        assert "暂无 Park Strategy" not in copy
        assert browser_errors == []
        browser.close()


def test_gridmind_distinguishes_evidence_blocked_from_clean_idle() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    model = _read_model("accepted", full_orders=True)
    model["current_strategy"] = {
        "schema_version": "park-current-strategy-summary-v1",
        "source": "park_strategy_session",
        "active": False,
        "status": "evidence_blocked",
        "status_label": "证据阻塞",
        "contract_status": "blocked",
        "blockers": ["safety_evidence_not_passing", "boot_not_verified"],
        "identity": {"strategy_session_id": None, "strategy_revision_id": None, "plan_digest": None},
        "specification": {},
        "execution": {"accepted_order_count": 0, "fill_count": 0, "open_position_count": 0},
        "recording": {
            "status": "blocked",
            "record_window_id": "2026-08-18_DAY",
            "package_status": "blocked_incomplete",
            "blocker_code": "recording_package_blocked",
        },
        "freshness": {"market_fresh": False, "safety_status": "missing"},
    }
    browser_errors: list[str] = []

    def fulfill_read_model(route) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(model, ensure_ascii=False))

    def fulfill_market(route) -> None:
        timeframe = route.request.url.split("timeframe=", 1)[1].split("&", 1)[0]
        route.fulfill(status=200, content_type="application/json", body=json.dumps({**model["market"], "timeframe": timeframe, "bars": []}))

    with _static_server() as origin, playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True, channel="chrome")
        except Exception as exc:  # pragma: no cover - depends on local browser install
            pytest.skip(f"Playwright Chromium unavailable: {exc}")
        page = browser.new_page(viewport={"width": 1680, "height": 1050})
        page.on("console", lambda message: browser_errors.append(message.text) if message.type == "error" else None)
        page.on("pageerror", lambda error: browser_errors.append(str(error)))
        page.add_init_script("window.setInterval = () => 0")
        page.route("**/api/trading-system/read-model", fulfill_read_model)
        page.route("**/api/dualtrack/market/bars?*", fulfill_market)
        page.goto(f"{origin}/dashboard-gridmind.html", wait_until="load")
        page.locator("#currentStrategyCard").wait_for(state="visible")
        copy = page.locator("#currentStrategyCard").inner_text()
        assert "策略证据阻塞" in copy
        assert "证据阻塞" in copy
        assert "暂无 Park Strategy" not in copy
        assert "阻塞: safety_evidence_not_passing" in copy
        assert "Recording 2026-08-18_DAY · blocked" in copy
        assert "Recording 阻塞: recording_package_blocked" in copy
        assert browser_errors == []
        browser.close()


def test_gridmind_shows_dca_tp_sl_without_grid_only_facts() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    park = {
        "generated_at": "2026-08-18T01:02:04+00:00",
        "status": "ok",
        "blockers": [],
        "strategy": {
            "active": True,
            "state": "ACTIVE_LOCKED",
            "strategy_session_id": "session-dca-browser",
            "strategy_revision_id": "revision-dca-1",
            "plan_digest": "sha256:dca-browser",
            "strategy_type": "dca",
            "direction": "long",
            "lower_price_boundary": 3800.0,
            "upper_price_boundary": 4000.0,
            "stop_price": 3800.0,
            "take_profit_price": 4050.0,
            "maximum_leverage": 5.0,
            "theoretical_max_loss": 240.0,
        },
        "execution": {"counts": {"accepted_orders": 2, "filled_orders": 1, "fills": 1, "open_positions": 1, "closed_positions": 0}, "reconciliation": {"status": "ok"}},
        "market": {"price": 3900.0, "fresh": True},
        "safety": {"status": "pass", "age_seconds": 3.0},
    }
    model = _read_model("accepted", full_orders=True, park=park)
    browser_errors: list[str] = []

    def fulfill_read_model(route) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(model, ensure_ascii=False))

    def fulfill_market(route) -> None:
        timeframe = route.request.url.split("timeframe=", 1)[1].split("&", 1)[0]
        route.fulfill(status=200, content_type="application/json", body=json.dumps({**model["market"], "timeframe": timeframe, "bars": []}))

    with _static_server() as origin, playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True, channel="chrome")
        except Exception as exc:  # pragma: no cover - depends on local browser install
            pytest.skip(f"Playwright Chromium unavailable: {exc}")
        page = browser.new_page(viewport={"width": 1680, "height": 1050})
        page.on("console", lambda message: browser_errors.append(message.text) if message.type == "error" else None)
        page.on("pageerror", lambda error: browser_errors.append(str(error)))
        page.add_init_script("window.setInterval = () => 0")
        page.route("**/api/trading-system/read-model", fulfill_read_model)
        page.route("**/api/dualtrack/market/bars?*", fulfill_market)
        page.goto(f"{origin}/dashboard-gridmind.html", wait_until="load")
        page.locator("#currentStrategyCard").wait_for(state="visible")
        copy = page.locator("#currentStrategyCard").inner_text()
        assert "Long · DCA" in copy
        assert "DCA TP / SL" in copy
        assert "TP 4,050 · SL 3,800" in copy
        assert "Grid Entry Range" not in copy
        assert "Grid Spacing" not in copy
        assert browser_errors == []
        browser.close()
