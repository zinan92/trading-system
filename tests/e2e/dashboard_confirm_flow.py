"""Attended browser acceptance for the Dashboard V5 Testnet Grid flow.

This is intentionally a real-browser check: it talks to the Dashboard's
source-bound control endpoints and never mocks market, account, or control
responses.  The server must be started separately with an isolated output
root.  No credential value is read or included in the evidence.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REDACTED_KEYS = {"private_key", "secret", "api_key", "api_secret", "signer", "signature"}


def _public(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _public(item)
            for key, item in value.items()
            if str(key).lower() not in REDACTED_KEYS
            and "password" not in str(key).lower()
        }
    if isinstance(value, list):
        return [_public(item) for item in value]
    return value


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_public(payload), ensure_ascii=False, indent=2) + "\n")


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def run(base_url: str, evidence_dir: Path) -> dict[str, Any]:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - environment diagnostic
        raise RuntimeError("Playwright is required: python3 -m pip install playwright") from exc

    evidence_dir.mkdir(parents=True, exist_ok=True)
    base_url = base_url.rstrip("/")
    console_errors: list[str] = []
    page_errors: list[str] = []
    api_events: list[dict[str, Any]] = []
    receipt: dict[str, Any] = {
        "schema_version": "dashboard-confirm-flow-evidence-v1",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base_url,
        "environment": "testnet",
        "venue_profile": "hyperliquid.testnet",
        "instrument_id": "BTC-USD-PERP",
        "strategy_family": "grid",
        "screenshots": [],
        "console_errors": console_errors,
        "page_errors": page_errors,
        "api_events": api_events,
    }

    def record_response(response) -> None:
        path = response.url.split("?", 1)[0].replace(base_url, "")
        if not path.startswith("/api/dashboard-control/"):
            return
        event: dict[str, Any] = {"method": response.request.method, "path": path, "status": response.status}
        if response.request.method == "POST":
            try:
                request = response.request.post_data_json or {}
                if path.endswith("/confirm"):
                    preview = request.get("preview") if isinstance(request, dict) else {}
                    event["request"] = {
                        "preview_digest": preview.get("preview_digest"),
                        "venue_profile_id": preview.get("venue_profile_id"),
                        "instrument_id": preview.get("instrument_id"),
                        "strategy_family": preview.get("strategy_family"),
                        "confirmation": _public(request.get("confirmation", {})),
                    }
                else:
                    event["request"] = _public(request)
            except (TypeError, ValueError):
                event["request"] = {"present": bool(response.request.post_data)}
        if path.endswith("/preview") or path.endswith("/confirm"):
            try:
                body = response.json()
                if path.endswith("/preview"):
                    preview = body.get("preview") or {}
                    event["response"] = {
                        "schema_version": body.get("schema_version"),
                        "preview_digest": preview.get("preview_digest"),
                        "execution_ready": preview.get("execution_ready"),
                        "blockers": preview.get("blockers", []),
                        "venue_profile_id": preview.get("venue_profile_id"),
                        "instrument_id": preview.get("instrument_id"),
                        "strategy_family": preview.get("strategy_family"),
                    }
                else:
                    confirmation = body.get("confirmation") or {}
                    event["response"] = {
                        "schema_version": body.get("schema_version"),
                        "status": confirmation.get("status"),
                        "event": confirmation.get("event"),
                        "activation_id": confirmation.get("activation_id"),
                        "preview_digest": confirmation.get("preview_digest"),
                        "execution_mutation": confirmation.get("execution_mutation"),
                        "coordinator_invoked": confirmation.get("coordinator_invoked"),
                        "blockers": confirmation.get("blockers", []),
                        "next_action": confirmation.get("next_action"),
                    }
            except (TypeError, ValueError):
                event["response"] = {"json": False}
        if response.status >= 400 and path.endswith("/confirm"):
            try:
                event["error_body"] = _public(response.json())
            except (TypeError, ValueError):
                event["error_body"] = {"json": False}
        api_events.append(event)

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except PlaywrightError as exc:  # pragma: no cover - machine diagnostic
            raise RuntimeError(f"Chromium unavailable: {exc}") from exc
        page = browser.new_page(viewport={"width": 1680, "height": 1050})
        page.set_default_timeout(15_000)
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on("response", record_response)

        def shot(name: str) -> None:
            path = evidence_dir / f"{name}.png"
            page.screenshot(path=str(path), full_page=True)
            receipt["screenshots"].append(str(path))

        page.goto(f"{base_url}/dashboard-v5.html", wait_until="domcontentloaded")
        page.locator("#dashboardVenueSelect").wait_for(state="attached")
        page.locator("#dashboardVenueSelect option[value='hyperliquid.testnet']").wait_for(state="attached")
        venue = page.locator("#dashboardVenueSelect")
        _assert(venue.locator("option").count() >= 2, "Dashboard did not expose venue profiles")
        labels = [venue.locator("option").nth(i).inner_text() for i in range(venue.locator("option").count())]
        _assert(not any(re.search(r"mainnet|live", label, re.I) for label in labels), "Mainnet/Live appeared in venue choices")
        venue.select_option("hyperliquid.testnet")
        page.locator("#dashboardInstrumentSelect").wait_for(state="attached")
        page.locator("#dashboardInstrumentSelect option[value='BTC-USD-PERP']").wait_for(state="attached")
        page.locator("#dashboardInstrumentSelect").select_option("BTC-USD-PERP")
        page.locator("[data-dashboard-strategy='grid']").click()
        shot("01-venue-instrument-strategy")

        account = page.locator("#dashboardAccountSummary")
        page.wait_for_function(
            "el => /^Account (READY|BLOCKED)/.test(el.textContent || '')",
            arg=account.element_handle(),
        )
        _assert("READY" in account.inner_text(), f"Testnet account admission was not READY: {account.inner_text()}")

        # Keep the plan bounded and derive the range from the selected venue's
        # live market response.  Values are injected into existing form fields;
        # no browser-side synthetic market or fixture is introduced.
        bars_response = page.request.get(
            f"{base_url}/api/dashboard-control/market-bars?venue_profile_id=hyperliquid.testnet&instrument_id=BTC-USD-PERP&timeframe=1m&limit=2"
        )
        _assert(bars_response.ok, f"Selected Testnet market bars failed: HTTP {bars_response.status}")
        bars = bars_response.json()
        _assert(bars.get("trusted") is True and bars.get("is_synthetic") is not True, "Selected market facts were not trusted real data")
        price = float(bars.get("latest_close") or bars.get("bars", [])[-1]["close"])
        _assert(price > 0, "Selected BTC market price was missing")
        # Use user-equivalent input events after the venue refresh has settled;
        # an earlier refresh may otherwise rehydrate the Paper form defaults.
        page.wait_for_timeout(1_000)
        page.locator("#rangeLow").fill(f"{price * 0.99:.8f}")
        page.locator("#rangeHigh").fill(f"{price * 1.01:.8f}")
        page.locator("#gridCount").fill("2")
        page.locator("#gridNotional").fill("10")
        page.locator("#leverage").fill("1")
        _assert(page.locator("#rangeLow").input_value() != "0", "Grid range form values were not retained")
        preview_text = ""
        for attempt in range(3):
            page.locator("#dashboardPreviewButton").click()
            page.locator("#dashboardPreviewSummary").filter(has_text="grid").wait_for()
            preview_text = page.locator("#dashboardPreviewSummary").inner_text()
            if "market_facts_required" not in preview_text and "testnet_account_unavailable" not in preview_text:
                break
            if attempt < 2:
                page.wait_for_timeout(1_000)
        _assert("BLOCKED" not in preview_text, f"Testnet Grid preview was blocked: {preview_text}")
        _assert("READY" in preview_text, f"Testnet Grid preview was not READY: {preview_text}")
        shot("02-preview")

        confirm_button = page.locator("#dashboardConfirmButton")
        _assert(confirm_button.is_enabled(), "Confirm & Run remained disabled after a READY preview")
        with page.expect_response("**/api/dashboard-control/confirm", timeout=15_000):
            confirm_button.click()
        confirmation_events = [event for event in api_events if event["path"].endswith("/confirm")]
        _assert(confirmation_events, f"Dashboard did not send the confirm request; API events: {api_events[-4:]}")
        confirmation = confirmation_events[-1].get("response") or {}
        _assert(confirmation.get("status") == "confirmed", f"Confirmation was not confirmed: {confirmation}; API event: {confirmation_events[-1]}")
        _assert(bool(confirmation.get("activation_id")), "Confirmed response did not contain activation_id")
        _assert(confirmation.get("execution_mutation") is False, "Dashboard confirmation reported execution mutation")
        shot("03-confirmed")

        # Repeating the same exact browser action must return the durable
        # activation instead of creating a second activation or failing.
        with page.expect_response("**/api/dashboard-control/confirm", timeout=15_000):
            confirm_button.click()
        confirmation_events = [event for event in api_events if event["path"].endswith("/confirm")]
        _assert(len(confirmation_events) >= 2, "Idempotency retry did not reach the confirm endpoint")
        repeated = confirmation_events[-1].get("response") or {}
        _assert(repeated.get("status") == "confirmed", f"Idempotent confirmation was not confirmed: {repeated}")
        _assert(repeated.get("activation_id") == confirmation.get("activation_id"), "Idempotent confirmation changed activation_id")
        shot("04-idempotent-confirmed")

        _assert(not console_errors, f"Browser console errors: {console_errors}")
        _assert(not page_errors, f"Browser page errors: {page_errors}")
        browser.close()

    receipt.update({
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "status": "pass",
        "activation_id": confirmation.get("activation_id"),
        "preview_digest": confirmation.get("preview_digest"),
        "market_price": price,
    })
    _write_json(evidence_dir / "dashboard-confirm-flow.json", receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="Running isolated Dashboard base URL")
    parser.add_argument("--evidence-dir", type=Path, default=Path("docs/evidence/issue-1149"))
    args = parser.parse_args()
    try:
        result = run(args.base, args.evidence_dir)
    except Exception as exc:  # noqa: BLE001 - CLI must leave a useful evidence trail.
        print(f"dashboard-confirm-flow: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"status": result["status"], "activation_id": result["activation_id"], "evidence": str(args.evidence_dir)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
