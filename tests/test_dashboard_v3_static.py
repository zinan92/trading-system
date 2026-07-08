from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_dashboard_v2_forwards_to_trader_console_v4() -> None:
    html = _read("dashboard-v2.html")

    assert "dashboard-v4.html" in html
    assert "window.location.replace(next)" in html
    assert "Dashboard v2 now forwards to the trader-facing v4 console" in html
    assert "Explainability Gaps" not in html


def test_dashboard_v3_forwards_to_trader_console_v4() -> None:
    html = _read("dashboard-v3.html")

    assert '<meta http-equiv="refresh" content="0; url=dashboard-v4.html" />' in html
    assert 'const next = "dashboard-v4.html" + window.location.search + window.location.hash;' in html
    assert "window.location.replace(next)" in html
    assert "Dashboard v3 now forwards to the trader-facing v4 console" in html


def test_legacy_dashboard_forwards_to_trader_console_v4() -> None:
    html = _read("dashboard.html")

    assert '<meta http-equiv="refresh" content="0; url=dashboard-v4.html" />' in html
    assert 'const next = "dashboard-v4.html" + window.location.search + window.location.hash;' in html
    assert "window.location.replace(next)" in html


def test_legacy_replay_forwards_to_replay_v4() -> None:
    html = _read("dashboard-replay.html")

    assert '<meta http-equiv="refresh" content="0; url=dashboard-replay-v4.html" />' in html
    assert 'const next = "dashboard-replay-v4.html" + window.location.search + window.location.hash;' in html
    assert "window.location.replace(next)" in html


def test_ops_dashboard_links_back_to_current_trader_console() -> None:
    html = _read("ops-dashboard.html")

    assert 'href="dashboard-v4.html"' in html
    assert "dashboard-v3.html" not in html
