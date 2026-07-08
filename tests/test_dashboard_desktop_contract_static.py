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
    assert 'new URL("dashboard-replay-v4.html", window.location.href)' in html
    assert 'href="assets/shell.css"' in html
    assert 'src="assets/shell.js"' in html
    assert 'class="overview-stage"' in html
    assert 'id="portfolioBook" class="card portfolio-book"' in html
    assert 'id="overviewStrategyBoard" class="card overview-strategy-board"' in html


def test_replay_v4_is_current_replay_workbench() -> None:
    html = read_html("dashboard-replay-v4.html")

    assert "lightweight-charts.standalone.production.js" in html
    assert 'fetch(`${API_BASE}/api/replay?${query.toString()}`' in html
    assert 'new URL("dashboard-v4.html", window.location.href)' in html
    assert 'href="assets/shell.css"' in html
    assert 'src="assets/shell.js"' in html
    assert 'id="chart-1m"' in html
    assert 'id="chart-1d"' in html


def test_legacy_dashboard_and_replay_routes_are_redirect_stubs() -> None:
    dashboard = read_html("dashboard-v3.html")
    replay = read_html("dashboard-replay.html")

    assert 'content="0; url=dashboard-v4.html"' in dashboard
    assert 'content="0; url=dashboard-replay-v4.html"' in replay
    assert "window.location.replace(next)" in dashboard
    assert "window.location.replace(next)" in replay


def test_replay_desktop_interaction_uat_script_targets_current_pages() -> None:
    script = read_text("tools/verify_replay_desktop_interactions.mjs")
    dashboard_script = read_text("tools/verify_dashboard_v3_desktop.mjs")

    assert "dashboard-replay-v4.html" in script
    assert "dashboard-v4.html" in dashboard_script
    assert "Header replay CTA did not route to dashboard-replay-v4.html" in dashboard_script
    assert "Header replay CTA opened a 404/not-found page" in dashboard_script
