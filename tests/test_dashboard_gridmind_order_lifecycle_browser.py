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
) -> dict:
    source = deepcopy(_source())
    source["cycle"]["cycle_id"] = cycle_id
    source["production_execution"]["cycle_id"] = cycle_id
    source["runtime"]["cycle_id"] = cycle_id
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
    model = project_trading_system_read_model(
        source,
        risk_decision=_risk(),
        broker=_broker(),
        generated_at="2026-07-18T01:02:04+00:00",
    ).to_dict()
    hostile = next(row for row in model["execution"]["orders"] if row["order_id"] == "hostile-order")
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

        assert labels == ["已接受", "部分成交", "已撤单", "已撤单", "已撤单"]
        assert open_counts == [1, 1, 0, 1, 0]
        assert table_counts == ["(2)"] * 5
        assert page.locator("#orders img").count() == 0
        assert page.evaluate("() => globalThis.pwned === true") is False
        assert "ok" not in (page.locator("#runBadge").get_attribute("class") or "").split()
        assert page.locator("#runBadge").inner_text() == "异常"

        page.evaluate("() => load({withMarket:false})")
        assert page.locator("#orders tbody tr").first.locator("td").first.inner_text() == "已接受"
        assert page.locator("#ordersCount").inner_text() == "(2)"
        assert page.evaluate("() => state.orderLifecycle.cycleId") == "2026-07-18_NIGHT"

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
