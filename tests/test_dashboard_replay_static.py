from __future__ import annotations

from pathlib import Path


def test_legacy_replay_route_is_redirect_stub() -> None:
    html = Path("dashboard-replay.html").read_text(encoding="utf-8")

    assert '<meta http-equiv="refresh" content="0; url=dashboard-replay-v4.html" />' in html
    assert 'const next = "dashboard-replay-v4.html" + window.location.search + window.location.hash;' in html
    assert "window.location.replace(next)" in html
    assert "Legacy replay now forwards to replay v4" in html


def test_replay_v4_is_the_active_replay_surface() -> None:
    html = Path("dashboard-replay-v4.html").read_text(encoding="utf-8")

    assert "lightweight-charts.standalone.production.js" in html
    assert 'fetch(`${API_BASE}/api/replay?${query.toString()}`' in html
    assert 'new URL("dashboard-v4.html", window.location.href)' in html
    assert 'id="chart-1m"' in html
    assert 'id="chart-1d"' in html
    assert "这笔交易 · 病历" in html
    assert "信号 → 出票 · 五道关口" in html
