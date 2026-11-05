from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_html(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def read_text(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_trader_dashboard_v4_is_current_desktop_console() -> None:
    html = read_html("dashboard-v4.html")

    assert "GoldBot Trader Console V4" in html
    assert 'href="ops-dashboard.html"' in html
    assert "function openInPageReplay(" in html
    assert 'href="assets/shell.css"' in html
    assert 'src="assets/shell.js"' in html
    assert 'class="overview-stage"' in html
    assert 'id="portfolioBook" class="card portfolio-book"' in html
    assert 'id="overviewStrategyBoard" class="card overview-strategy-board"' in html


def test_legacy_dashboard_route_is_a_redirect_stub() -> None:
    dashboard = read_html("dashboard-v3.html")

    assert 'content="0; url=dashboard-v4.html"' in dashboard
    assert "window.location.replace(next)" in dashboard


def test_desktop_uat_script_checks_the_in_page_replay_view() -> None:
    dashboard_script = read_text("tools/verify_dashboard_v3_desktop.mjs")

    assert "dashboard-v4.html" in dashboard_script
    assert "Header replay CTA did not open the in-page replay view" in dashboard_script
    assert "dashboard-replay" not in dashboard_script
