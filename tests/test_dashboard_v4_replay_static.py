from pathlib import Path


def read_html(name: str) -> str:
    return Path(name).read_text(encoding="utf-8")


def test_v4_dashboard_routes_to_v4_replay():
    html = read_html("dashboard-v4.html")

    assert 'new URL("dashboard-replay-v4.html", window.location.href)' in html
    assert 'new URL("dashboard-replay.html", window.location.href)' not in html


def test_v4_replay_has_trader_facing_sections_and_returns_to_v4():
    html = read_html("dashboard-replay-v4.html")

    assert '<body class="replay-v4">' in html
    assert 'new URL("dashboard-v4.html", window.location.href)' in html
    assert "lightweight-charts.standalone.production.js" in html
    assert 'id="chart-1m"' in html
    assert 'id="chart-1d"' in html
    assert 'fetch(`${API_BASE}/api/replay?${query.toString()}`' in html
    assert "window.__replayV4State" in html
    assert 'class="wrap"' in html
    assert 'class="head"' in html
    assert "这笔交易 · 病历" in html
    assert "这笔的生命周期" in html
    assert "归因 · 这笔问题出在哪" in html
    assert "信号 → 出票 · 五道关口" in html
    assert "市场方向 · 口述观点" in html
    assert "平台可信度" in html
    assert 'class="med"' in html
    assert 'class="life"' in html
    assert 'class="attr"' in html
    assert 'class="gates"' in html
    assert 'class="mv"' in html
    assert 'class="mstrip"' in html


def test_legacy_replay_remains_legacy_route():
    html = read_html("dashboard-replay.html")

    assert '<body class="replay-v4 layout-trader-focus">' not in html
    assert 'new URL("dashboard-v3.html", window.location.href)' in html
