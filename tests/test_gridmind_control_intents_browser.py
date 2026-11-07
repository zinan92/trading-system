"""Control Intents (ADR 0008, #1395): a control that is replaced by a new element still sends the same requests.

#1329 found the control script bound elements by reference at load, so a React re-render would drop a listener
with no error. Every control in the journey is swapped for a listener-free clone before it is used.
"""
from __future__ import annotations

import json

import pytest

from tests.test_dashboard_control_wire_browser import WIRE, _fulfill_control, _open
from tests.test_dashboard_gridmind_control_track_browser import _catalog

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


SWITCH = """async () => {
  const venue = document.getElementById('dashboardVenueSelect'), instrument = document.getElementById('dashboardInstrumentSelect');
  venue.value = 'hyperliquid.testnet'; venue.dispatchEvent(new Event('change'));
  while (![...instrument.options].some(o => o.value === 'BTC-USD-PERP' && !o.disabled)) await new Promise(r => setTimeout(r, 20));
  if (DISABLE) instrument.disabled = true;
  instrument.value = 'BTC-USD-PERP'; instrument.dispatchEvent(new Event('change'));
}"""


@pytest.mark.parametrize("disabled", [False, True])
def test_the_desk_asset_switch_still_reaches_the_selects(disabled: bool) -> None:
    """gm-desk.js switchAsset, step for step: a change that does not bubble on each select, enabled or not.

    The old listeners sat on the selects, so both reached them; a disabled select must not swallow the switch."""
    playwright = pytest.importorskip("playwright.sync_api")
    catalog = _catalog()
    catalog["venue_profiles"].append({"id": "hyperliquid.testnet", "label": "Hyperliquid Testnet", "instruments": [
        {"instrument_id": "BTC-USD-PERP", "asset": "BTC", "eligibility": "eligible", "blockers": []}]})
    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True, channel="chrome")
        except Exception as exc:  # pragma: no cover - depends on local browser install
            pytest.skip(f"Playwright Chromium unavailable: {exc}")
        page, wire, _unexpected, errors = _open(browser, _fulfill_control, catalog=catalog)
        page.evaluate(SWITCH.replace("DISABLE", "true" if disabled else "false"))
        page.locator("#dashboardControlStatus", has_text="BTC-USD-PERP 已选择").wait_for()
        browser.close()
    assert [(method, path, json.loads(body)) for method, path, body in wire if path.endswith("/selection")] == [
        ("POST", "/api/dashboard-control/selection", {"venue_profile_id": "hyperliquid.testnet"}),
        ("POST", "/api/dashboard-control/selection", {"venue_profile_id": "hyperliquid.testnet", "instrument_id": "BTC-USD-PERP"}),
    ]
    assert errors == []
