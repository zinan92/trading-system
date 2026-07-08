from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


ACTIVE_PAGES = [
    "command-center.html",
    "ops-dashboard.html",
    "dashboard-v4.html",
    "dashboard-replay-v4.html",
    "dashboard-dualtrack-v5.html",
    "dashboard-dualtrack-replay.html",
]


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_active_pages_load_global_shell_assets() -> None:
    for page in ACTIVE_PAGES:
        html = _read(page)
        assert 'href="assets/shell.css"' in html, page
        assert 'src="assets/shell.js"' in html, page


def test_ops_dashboard_drops_stale_console_link_and_dead_endpoints() -> None:
    html = _read("ops-dashboard.html")

    assert "dashboard-v3.html" not in html
    assert "/api/dashboard?view=ops" not in html
    assert "/api/public-access-health" not in html


def test_legacy_pages_redirect_to_current_targets() -> None:
    targets = {
        "dashboard-v3.html": "dashboard-v4.html",
        "dashboard.html": "dashboard-v4.html",
        "dashboard-replay.html": "dashboard-replay-v4.html",
    }
    for page, target in targets.items():
        html = _read(page)
        assert f'content="0; url={target}"' in html
        assert f'const next = "{target}" + window.location.search + window.location.hash;' in html
        assert "window.location.replace(next)" in html


def test_shell_js_has_no_external_network_dependency() -> None:
    shell = _read("assets/shell.js")

    assert "https://" not in shell
    assert "http://" not in shell


def test_shell_primary_nav_starts_with_command_center_and_hides_legacy_cockpit() -> None:
    shell = _read("assets/shell.js")

    assert '["command", "指挥台", "command-center.html"]' in shell
    assert '["command", "指挥台", "command-center.html"]' in shell.split("const items = [", 1)[1].split("];", 1)[0]
    assert '["cockpit", "驾驶舱", "dashboard-v4.html"]' not in shell
    assert 'cockpit: "dashboard-v4.html"' in shell
