"""Control Intents (ADR 0008, #1395): a control that is replaced by a new element still sends the same requests.

#1329 found the control script bound elements by reference at load, so a React re-render would drop a listener
with no error. Every control in the journey is swapped for a listener-free clone before it is used.
"""
from __future__ import annotations

import pytest

from tests.test_dashboard_control_wire_browser import WIRE, _fulfill_control, _open

SWAP = """() => {
  // A select keeps its value, as a React select renders the value it was given.
  const swap = el => { const copy = el.cloneNode(true); if (el.tagName === 'SELECT') copy.value = el.value; el.replaceWith(copy); return copy; };
  document.querySelectorAll('[data-gm-intent]').forEach(swap);
  return document.querySelectorAll('[data-gm-intent]').length;
}"""


def test_replaced_controls_send_the_same_requests() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True, channel="chrome")
        except Exception as exc:  # pragma: no cover - depends on local browser install
            pytest.skip(f"Playwright Chromium unavailable: {exc}")
        page, wire, unexpected, errors = _open(browser, _fulfill_control)
        assert page.evaluate(SWAP) >= 9
        page.locator('[data-dashboard-strategy="grid"]').click()
        page.evaluate(SWAP)  # again, mid-journey: the preview button must be the live one
        page.locator("#dashboardPreviewButton").click()
        page.locator("#dashboardConfirmButton:not([disabled])").wait_for()  # enabled on the replacement
        page.evaluate(SWAP)
        page.locator("#dashboardConfirmButton").click()
        page.locator("#dashboardPreviewSummary", has_text="mock-1").wait_for()
        page.locator("#smartFill").click()
        page.locator("#previewSummary", has_text="预计最大损失").wait_for()
        page.evaluate(SWAP)
        page.locator("#startRobot").click()
        page.locator(".trade-toast").filter(has_text="机器人已启动").wait_for()
        page.evaluate(SWAP)
        page.locator("#stopRobot").click()
        page.locator("#actionStatus").filter(has_text="机器人已停止").wait_for()
        browser.close()

    assert wire == WIRE
    assert errors == []


def test_a_disabled_control_runs_nothing() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True, channel="chrome")
        except Exception as exc:  # pragma: no cover - depends on local browser install
            pytest.skip(f"Playwright Chromium unavailable: {exc}")
        page, wire, _unexpected, errors = _open(browser, _fulfill_control)
        # A synthetic click on the still-disabled confirm button must not reach its handler.
        page.evaluate("() => { const b = document.getElementById('dashboardConfirmButton'); b.disabled = true; b.dispatchEvent(new MouseEvent('click', {bubbles: true})); }")
        page.wait_for_timeout(300)
        browser.close()
    assert wire == []
    assert errors == []


def test_the_desk_asset_switch_still_reaches_the_selects() -> None:
    """gm-desk.js switches BTC and gold with a change event that does not bubble (`new Event("change")`)."""
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True, channel="chrome")
        except Exception as exc:  # pragma: no cover - depends on local browser install
            pytest.skip(f"Playwright Chromium unavailable: {exc}")
        page, wire, _unexpected, errors = _open(browser, _fulfill_control)
        page.evaluate("() => document.getElementById('dashboardInstrumentSelect').dispatchEvent(new Event('change'))")
        page.wait_for_function("() => /请选择 Strategy|Instrument 选择未保存/.test(document.getElementById('dashboardControlStatus').textContent)")
        browser.close()
    assert [row[:2] for row in wire] == [["POST", "/api/dashboard-control/selection"]]
    assert "instrument_id" in wire[0][2]
    assert errors == []
