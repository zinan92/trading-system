"""Control Intents (ADR 0008, #1395): a control that is replaced by a new element still sends the same requests.

#1329 found the control script bound elements by reference at load, so a React re-render would drop a listener
with no error. Controls GridMind still draws are swapped for a listener-free clone before use. React's controls
(#1396) are remounted instead: the island's host is replaced, so React builds them again from what GridMind published.
"""
from __future__ import annotations

import json

import pytest

from tests.test_dashboard_control_wire_browser import WIRE, _fulfill_control, _open
from tests.test_dashboard_gridmind_control_track_browser import _catalog

def _browser(runtime):
    try:
        return runtime.chromium.launch(headless=True, channel="chrome")
    except Exception as exc:  # pragma: no cover - depends on local browser install
        pytest.skip(f"Playwright Chromium unavailable: {exc}")


SWAP = """() => {
  // A select keeps its value, as a React select renders the value it was given.
  const swap = el => { const copy = el.cloneNode(true); if (el.tagName === 'SELECT') copy.value = el.value; el.replaceWith(copy); return copy; };
  document.querySelectorAll('[data-gm-intent]').forEach(el => { if (!el.closest('[data-island]')) swap(el); });
  const host = document.getElementById('dashboardPicker');
  const fresh = host.cloneNode(true); fresh.removeAttribute('data-island-live'); host.replaceWith(fresh);
  return document.querySelectorAll('[data-gm-intent]').length;
}"""
# The clone's children are copies; only a new root makes a select React's own.
REMOUNTED = "() => Object.keys(document.getElementById('dashboardVenueSelect') || {}).some(key => key.startsWith('__react'))"


def test_replaced_controls_send_the_same_requests() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runtime:
        browser = _browser(runtime)
        page, wire, unexpected, errors = _open(browser, _fulfill_control)
        assert page.evaluate(SWAP) >= 9
        page.wait_for_function(REMOUNTED)
        page.locator('[data-dashboard-strategy="grid"]').click()
        page.locator('[data-dashboard-strategy="grid"].on').wait_for()  # the new root draws what GridMind publishes
        page.evaluate(SWAP)  # again, mid-journey: the preview button must be the live one
        page.wait_for_function(REMOUNTED)
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
        browser = _browser(runtime)
        page, wire, _unexpected, errors = _open(browser, _fulfill_control)
        # A synthetic click on the still-disabled confirm button must not reach its handler.
        page.evaluate("() => { const b = document.getElementById('dashboardConfirmButton'); b.disabled = true; b.dispatchEvent(new MouseEvent('click', {bubbles: true})); }")
        page.wait_for_timeout(300)
        browser.close()
    assert wire == []
    assert errors == []


def _two_venues() -> dict:
    catalog = _catalog()
    catalog["venue_profiles"].append({"id": "hyperliquid.testnet", "label": "Hyperliquid Testnet", "instruments": [
        {"instrument_id": "BTC-USD-PERP", "asset": "BTC", "eligibility": "eligible", "blockers": []},
        {"instrument_id": "SOL-USD-PERP", "asset": "SOL", "eligibility": "blocked", "blockers": ["没有行情"]}]})
    return catalog


SWITCH = """async () => {
  const picker = window.dashboardControlState.picker;
  window.gmIntent.fire('change', 'control.venue', {id: 'hyperliquid.testnet'});
  while (!picker.instruments.some(o => o.value === 'BTC-USD-PERP' && !o.disabled)) await new Promise(r => setTimeout(r, 20));
  window.gmIntent.fire('change', 'control.instrument', {id: 'BTC-USD-PERP'});
}"""


@pytest.mark.parametrize("bundle_blocked", [False, True], ids=["react", "no-bundle"])
def test_the_desk_asset_switch_reaches_the_selection(bundle_blocked: bool) -> None:
    """gm-desk.js switchAsset, step for step: intents with an id, and the selects show the result."""
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runtime:
        browser = _browser(runtime)
        page, wire, _unexpected, errors = _open(browser, _fulfill_control, catalog=_two_venues(), bundle_blocked=bundle_blocked)
        page.evaluate(SWITCH)
        page.locator("#dashboardControlStatus", has_text="BTC-USD-PERP 已选择").wait_for()
        assert page.locator("#dashboardVenueSelect").input_value() == "hyperliquid.testnet"
        assert page.locator("#dashboardInstrumentSelect").input_value() == "BTC-USD-PERP"
        browser.close()
    assert [(method, path, json.loads(body)) for method, path, body in wire if path.endswith("/selection")] == [
        ("POST", "/api/dashboard-control/selection", {"venue_profile_id": "hyperliquid.testnet"}),
        ("POST", "/api/dashboard-control/selection", {"venue_profile_id": "hyperliquid.testnet", "instrument_id": "BTC-USD-PERP"}),
    ]
    assert errors == []


def test_an_id_that_is_not_on_offer_selects_nothing() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runtime:
        browser = _browser(runtime)
        page, wire, _unexpected, errors = _open(browser, _fulfill_control, catalog=_two_venues())
        page.evaluate("async () => { window.gmIntent.fire('change', 'control.venue', {id: 'hyperliquid.testnet'}); }")
        page.locator("#dashboardInstrumentSelect option[value='BTC-USD-PERP']").wait_for(state="attached")
        before = len(wire)
        for instrument in ("SOL-USD-PERP", "NOT-LISTED"):  # blocked, then unknown
            page.evaluate("id => window.gmIntent.fire('change', 'control.instrument', {id})", instrument)
            assert page.evaluate("() => window.dashboardControlState.picker.instrument") == ""
        page.wait_for_timeout(200)
        browser.close()
    assert wire[before:] == []
    assert errors == []


def test_a_select_keeps_the_choice_through_a_re_render_while_the_request_is_out() -> None:
    """The real browser path: React restores a controlled select to its last prop, so GridMind must publish first."""
    playwright = pytest.importorskip("playwright.sync_api")
    held: list = []
    with playwright.sync_playwright() as runtime:
        browser = _browser(runtime)
        page, _wire, _unexpected, errors = _open(browser, _fulfill_control, catalog=_two_venues(), hold_selection=held.append)
        page.locator("#dashboardVenueSelect").select_option("hyperliquid.testnet")
        page.locator("#dashboardInstrumentSelect option[value='BTC-USD-PERP']").wait_for(state="attached")
        assert page.locator("#dashboardVenueSelect").input_value() == "hyperliquid.testnet"
        page.evaluate("""() => { const host = document.getElementById('dashboardPicker');
          host.setAttribute('data-props', JSON.stringify({...JSON.parse(host.getAttribute('data-props')), tick: 1})); }""")
        page.wait_for_timeout(100)  # the selection POST is still unanswered
        assert page.locator("#dashboardVenueSelect").input_value() == "hyperliquid.testnet"
        assert page.evaluate("() => window.dashboardControlState.picker.venue") == "hyperliquid.testnet"
        browser.close()
    assert len(held) == 1
    assert errors == []


@pytest.mark.parametrize("bundle_blocked", [False, True], ids=["react", "no-bundle"])
def test_a_retired_selection_shows_the_venue_and_offers_nothing_to_press(bundle_blocked: bool) -> None:
    """Production's state on d7dded8: the saved instrument was retired, so no instrument, strategy, preview or confirm."""
    playwright = pytest.importorskip("playwright.sync_api")
    selection = {"retired_selection": {"venue_profile_id": "binance.old", "instrument_id": "XAUUSDT.OLD"}}
    with playwright.sync_playwright() as runtime:
        browser = _browser(runtime)
        page, wire, _unexpected, errors = _open(browser, _fulfill_control, selection=selection, bundle_blocked=bundle_blocked)
        page.locator("#dashboardControlStatus", has_text="已停用").wait_for()
        assert page.locator("#dashboardVenueSelect").input_value() == "binance.paper"
        assert page.locator("#dashboardInstrumentSelect").input_value() == ""
        for selector in ('[data-dashboard-strategy="grid"]', '[data-dashboard-strategy="dca"]', "#dashboardPreviewButton", "#dashboardConfirmButton"):
            assert page.locator(selector).is_disabled()
        browser.close()
    assert [row for row in wire if row[1].endswith("/selection")] == []
    assert errors == []


def test_a_cached_bundle_without_the_picker_leaves_the_legacy_selects_working() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runtime:
        browser = _browser(runtime)
        page, wire, _unexpected, errors = _open(browser, _fulfill_control, catalog=_two_venues(), bundle_without="control-picker")
        page.wait_for_function("() => window.DeskWeb")
        page.locator("#dashboardVenueSelect").select_option("hyperliquid.testnet")
        page.locator("#dashboardInstrumentSelect").select_option("BTC-USD-PERP")
        page.locator("#dashboardControlStatus", has_text="BTC-USD-PERP 已选择").wait_for()
        browser.close()
    assert [json.loads(body)["venue_profile_id"] for _method, path, body in wire if path.endswith("/selection")] == ["hyperliquid.testnet", "hyperliquid.testnet"]
    assert errors == []
