from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path

import pytest

from tests.test_dashboard_gridmind_order_lifecycle_browser import (
    _read_model,
    _static_server,
)


def test_gridmind_supervisor_tab_shows_complete_audit_history() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    model = deepcopy(_read_model("accepted"))
    model["runtime"]["utilization"] = {
        "schema_version": "strategy-runtime-utilization-v2",
        "windows": {
            "24h": {
                "evidence_status": "complete",
                "percentage": 91.25,
            },
            "7d": {
                "evidence_status": "insufficient",
                "percentage": None,
            },
        },
    }
    model["runtime"]["supervisor"] = {
        "schema_version": "paper-supervisor-read-model-v1",
        "classifier_version": "paper-supervisor-blocker-v5",
        "status": "available",
        "current_cycle": {
            "cycle_id": "2026-07-18_DAY",
            "status": "available",
            "attempt_count": 2,
            "start_intent_count": 1,
            "last_result": {
                "status": "healthy",
                "terminal_status": "adopted_existing",
                "machine_code": None,
            },
            "episode": {
                "mode": "ready",
                "episode_id": "episode-2",
                "next_attempt_at": None,
                "blocker": None,
                "alert_required": False,
            },
            "history": {
                "pre_intent_attempts": [
                    {
                        "attempt_id": "attempt-1",
                        "observed_at": "2026-07-18T01:00:00+00:00",
                        "terminal_result": "transient",
                        "terminal_machine_code": (
                            "prepared_start_market_moved"
                        ),
                        "terminal_classification": "transient",
                    },
                    {
                        "attempt_id": "attempt-2",
                        "observed_at": "2026-07-18T01:05:00+00:00",
                        "terminal_result": "prepare_succeeded",
                        "terminal_machine_code": None,
                        "terminal_classification": None,
                    },
                ],
                "start_attempts": [
                    {
                        "intent_recorded_at": (
                            "2026-07-18T01:05:01+00:00"
                        ),
                        "preview_id": "preview-new",
                        "prepared_start_id": "prepared-new",
                        "terminal_result": "accepted",
                        "terminal_machine_code": None,
                    }
                ],
                "observations": [
                    {
                        "recorded_at": "2026-07-18T01:00:02+00:00",
                        "sequence": 1,
                        "observation_sha256": "a" * 64,
                        "payload": {
                            "status": "backing_off",
                            "machine_code": (
                                "prepared_start_market_moved"
                            ),
                            "preview_id": "preview-old",
                            "prepared_start_id": "prepared-old",
                        },
                    },
                    {
                        "recorded_at": "2026-07-18T01:05:03+00:00",
                        "sequence": 2,
                        "observation_sha256": "b" * 64,
                        "payload": {
                            "status": "healthy",
                            "preview_id": "preview-new",
                            "prepared_start_id": "prepared-new",
                        },
                    },
                ],
            },
        },
    }
    full_supervisor = deepcopy(model["runtime"]["supervisor"])
    model["runtime"]["supervisor"] = {
        **{
            key: value
            for key, value in full_supervisor.items()
            if key != "current_cycle"
        },
        "schema_version": "paper-supervisor-polling-summary-v1",
        "current_cycle": {
            **{
                key: value
                for key, value in full_supervisor["current_cycle"].items()
                if key != "history"
            },
            "history": {
                "status": "available_on_demand",
                "complete": False,
                "endpoint": "/api/trading-system/supervisor-history",
                "cycle_id": "2026-07-18_DAY",
                "event_count": 5,
                "observation_count": 2,
            },
        },
    }
    history_response = {
        "schema_version": "paper-supervisor-history-response-v2",
        "cycle_id": "2026-07-18_DAY",
        "completeness": {"status": "complete"},
        "supervisor": full_supervisor,
        "read_only": True,
        "command_authority": False,
    }
    history_requests: list[str] = []
    browser_errors: list[str] = []

    def fulfill_json(route, payload: dict) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payload, ensure_ascii=False),
        )

    with _static_server() as origin, playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(
                headless=True,
                channel="chrome",
            )
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"Playwright Chromium unavailable: {exc}")
        page = browser.new_page(viewport={"width": 1450, "height": 1000})
        page.on(
            "console",
            lambda message: (
                browser_errors.append(message.text)
                if message.type == "error"
                else None
            ),
        )
        page.on(
            "pageerror",
            lambda error: browser_errors.append(str(error)),
        )
        page.add_init_script("window.setInterval = () => 0")
        page.route(
            "**/api/trading-system/read-model",
            lambda route: fulfill_json(route, model),
        )
        page.route(
            "**/api/trading-system/supervisor-history?*",
            lambda route: (
                history_requests.append(route.request.url),
                fulfill_json(route, history_response),
            )[-1],
        )
        page.route(
            "**/api/dualtrack/market/bars?*",
            lambda route: fulfill_json(
                route,
                {**model["market"], "bars": []},
            ),
        )
        page.goto(
            f"{origin}/dashboard-gridmind.html",
            wait_until="load",
        )
        assert history_requests == []
        page.locator('[data-tab="supervisor"]').click()

        panel = page.locator('[data-panel="supervisor"]')
        panel.get_by_text(
            "Paper Supervisor · 2026-07-18_DAY"
        ).wait_for()
        panel.get_by_text("preview-old", exact=True).wait_for()
        assert len(history_requests) == 1
        assert panel.get_by_text(
            "prepared_start_market_moved",
            exact=True,
        ).count() == 2
        assert panel.get_by_text("preview-old", exact=True).count() == 1
        assert panel.get_by_text("preview-new", exact=True).count() == 2
        assert panel.get_by_text("prepared-old", exact=True).count() == 1
        assert panel.get_by_text("prepared-new", exact=True).count() == 2
        assert "24h 91.25% / 7d 证据不足" in page.locator(
            "#live"
        ).inner_text()
        assert browser_errors == []

        artifact_dir = os.environ.get("GRID_SUPERVISOR_SCREENSHOT_DIR")
        if artifact_dir:
            path = Path(artifact_dir)
            path.mkdir(parents=True, exist_ok=True)
            page.screenshot(
                path=str(path / "issue-467-supervisor-history.png"),
                full_page=True,
            )
        browser.close()
