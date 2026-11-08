"""The control journey's requests, byte for byte (#1329). Every endpoint is mocked; nothing leaves the page.

The control script only boots on production ports or a non-local host, so the page is served from a
`.test` host. Otherwise this journey would pass with the control track never running.
gridmind_control_wire.json was captured from 8ef4b3a, the last GridMind before these panels moved. Since #1419 the
journey runs on /trade, the only page left; without web.js /trade draws nothing but its one-line fallback, so the
old page's no-bundle variants are gone with it.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_dashboard_gridmind_control_track_browser import _catalog
from tests.test_dashboard_gridmind_header_browser import _header_model
from tests.test_dashboard_gridmind_operator_journey_browser import _control_response
from tests.trade_page import desk_answer, open_manual, press, static_file, text, trade_html

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "http://gridmind.test"
WIRE = json.loads((ROOT / "tests" / "fixtures" / "gridmind_control_wire.json").read_text(encoding="utf-8"))
CONFIRMED = {"confirmation": {"status": "confirmed", "activation_id": "mock-1", "blockers": []}}
DASHBOARD_PREVIEW = {"preview": {
    "preview_digest": "sha256:abc", "strategy_family": "grid", "instrument_id": "XAUUSDT.BINANCE", "requested": {"x": 1},
    "execution_ready": True, "blockers": [], "preview": {"risk": {"maximum_notional": 1000, "maximum_loss_at_full_depth": 12.5}},
}}


def _model() -> dict:
    model = _header_model(4000.0)
    model["runtime"].update({
        "actual_state": "stopped", "desired_state": "stopped", "status": "stopped",
        "can_start_when_authorized": True, "can_stop_when_authorized": False,
    })
    model["execution"]["counts"].update({"open_order_count": 0, "accepted_order_count": 0, "open_position_count": 0})
    model["strategy"]["proposals"] = []
    return model


def _open(browser, fulfill_control, *, catalog: dict | None = None, selection: dict | None = None, hold_selection=None, bundle_without: str | None = None):
    model = _model()
    wire: list[list] = []
    unexpected: list[tuple[str, str]] = []
    errors: list[str] = []
    html = trade_html()
    answers = {
        "/api/trading-system/read-model": model,
        "/api/auth/session": {"authenticated": True, "can_control": True, "email": None, "session_duration": "mock"},
        "/api/dashboard-control/catalog": catalog or _catalog(),
        "/api/dashboard-control/selection": {"selection": selection or {"venue_profile_id": "binance.paper", "instrument_id": "XAUUSDT.BINANCE", "strategy_family": "grid"}},
        "/api/dashboard-control/runtime-status": {"runtime": {}, "relevance": {}, "notification": None},
        "/api/dashboard-control/preview": DASHBOARD_PREVIEW,
        "/api/dashboard-control/confirm": CONFIRMED,
    }

    def handle(route) -> None:
        request = route.request
        path = request.url.removeprefix(ORIGIN).split("?", 1)[0]
        if request.method != "GET":
            wire.append([request.method, path, request.post_data])
        if path == "/trade":
            return route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
        if path == "/api/strategy-console/control":
            return fulfill_control(route, model)
        if hold_selection is not None and request.method == "POST" and path.endswith("/selection"):
            return hold_selection(route)
        if desk_answer(path) is not None:
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(desk_answer(path), ensure_ascii=False))
        if path in answers:
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(answers[path], ensure_ascii=False))
        if path.startswith("/api/dualtrack/market/bars"):
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({**model["market"], "bars": []}))
        if path.startswith("/api/"):
            unexpected.append((request.method, path))
            return route.fulfill(status=404, content_type="application/json", body="{}")
        if bundle_without and path.endswith("web.js"):
            source = static_file(path).read_text(encoding="utf-8")
            assert f'"{bundle_without}"' in source
            return route.fulfill(status=200, content_type="text/javascript", body=source.replace(f'"{bundle_without}"', '"retired-island"'))
        local = static_file(path) or ROOT / path.lstrip("/")
        return route.fulfill(status=200, path=str(local)) if local.is_file() else route.fulfill(status=404, body="")

    page = browser.new_page(viewport={"width": 1680, "height": 1050})
    page.set_default_timeout(8000)
    page.add_init_script("window.setInterval = () => 0")
    page.on("dialog", lambda dialog: dialog.accept())
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route(f"{ORIGIN}/**", handle)
    page.goto(f"{ORIGIN}/trade", wait_until="load")
    page.wait_for_function("() => window.dashboardControlState && window.dashboardControlState.catalog")
    open_manual(page)  # /trade keeps GridMind's form inside 「手动配置与 AI 助手」
    return page, wire, unexpected, errors


def _fulfill_control(route, model: dict) -> None:
    body = _control_response(route.request.post_data_json, model)
    route.fulfill(status=200, content_type="application/json", body=json.dumps(body, ensure_ascii=False))


def test_the_control_journey_sends_the_same_requests_as_before() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True, channel="chrome")
        except Exception as exc:  # pragma: no cover - depends on local browser install
            pytest.skip(f"Playwright Chromium unavailable: {exc}")
        page, wire, unexpected, errors = _open(browser, _fulfill_control)
        press(page, '[data-dashboard-strategy="grid"]')
        press(page, "#dashboardPreviewButton")
        press(page, "#dashboardConfirmButton")
        text(page, "#dashboardPreviewSummary", "mock-1")
        press(page, "#smartFill")
        text(page, "#previewSummary", "预计最大损失")
        press(page, "#startRobot")
        text(page, ".trade-toast", "机器人已启动")
        press(page, "#stopRobot")
        text(page, "#actionStatus", "机器人已停止")
        browser.close()

    assert wire == WIRE
    assert len(WIRE) == 7  # selection, preview, confirm, control: preview, prepare_start, start, stop
    assert unexpected == [("GET", "/api/park-paper/ai-chat")]  # the assistant's history, not part of this journey
    assert errors == []
