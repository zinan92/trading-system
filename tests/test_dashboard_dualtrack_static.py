from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_html() -> str:
    return (ROOT / "dashboard-dualtrack-v5.html").read_text(encoding="utf-8")


def test_dualtrack_v5_matches_locked_visual_contract_sections():
    html = read_html()

    assert "<title>人机双轨作战台 - Trading Orchestrator</title>" in html
    assert "--bg:#08090b" in html
    assert "--gold:#d8aa3f" in html
    assert "周期开始前 · 盲答协议" in html
    assert "盘中 · 双轨执行" in html
    assert "周期结束 · 对账复盘" in html
    assert "你的作战单 · 盲答中" in html
    assert "AI 作战单 · Obsidian 每日观点" in html
    assert "机器轨 MACHINE" in html
    assert "月均日现金流 · 双轨合计" in html
    assert "系统地板（方向不可知网格）" in html
    assert "神谕天花板" in html


def test_dualtrack_v5_uses_dualtrack_api_contracts_and_backend_market_bars():
    html = read_html()

    assert 'api("/api/dualtrack/cycle/current")' in html
    assert 'api(`/api/dualtrack/plan/${cycleId}`)' in html
    assert 'api(`/api/dualtrack/machine/${cycleId}`)' in html
    assert 'api("/api/dualtrack/orders"' in html
    assert 'cycleClosed(state.cycle) ? await api(`/api/dualtrack/attribution/${cycleId}`)' in html
    assert "function cycleClosed(cycle)" in html
    assert 'api("/api/dualtrack/ledger")' in html
    assert 'api("/api/dualtrack/verdict"' in html
    assert 'api("/api/dualtrack/market/bars?limit=96")' in html
    assert "BACKEND · 只读 K 线" in html
    assert "后端只读接口 `/api/dualtrack/market/bars`" in html
    assert "marketBarsToCandles" in html
    assert 'id="chartSyntheticWarning"' in html
    assert "data-synthetic-market-warning" in html
    assert "模拟数据 · 非真实价格" in html
    assert "function isSyntheticMarket(payload)" in html
    assert "payload.is_synthetic === true" in html
    assert 'provider.includes("synthetic_seed")' in html
    assert 'mode.includes("synthetic")' in html
    assert "function renderSyntheticWarning()" in html
    assert 'warning.classList.toggle("hidden", !synthetic)' in html
    assert "xauusdt@kline_1m" not in html
    assert "WebSocket(" not in html
    assert "venue-divergence" not in html


def test_dualtrack_v5_removes_ops_connector_panels_from_decision_page():
    html = read_html()

    forbidden = [
        "venueCard",
        "connectorCard",
        "/api/dualtrack/venue/tiger",
        "老虎",
        "TIGER",
        "CONNECTOR",
        "接入 CONNECTOR",
        "接入目录",
        "验证接入",
        "预览激活",
        "写入预检",
    ]
    for item in forbidden:
        assert item not in html


def test_dualtrack_v5_p2_uses_readable_context_charts_and_grouped_plan_cards():
    html = read_html()

    assert ".context-body{height:150px;display:block}" in html
    assert 'id="ctx15" class="context-body" viewBox="0 0 460 150"' in html
    assert 'id="ctx1h" class="context-body" viewBox="0 0 460 150"' in html
    assert "const w = 460, h = 150" in html
    assert 'stroke-width="2.4"' in html
    assert 'font-size="12">H ${fmt(baseHigh)}' in html
    assert 'font-size="12">floor ${fmt(floor)}' in html
    assert 'font-size="14">${change >= 0 ? "+" : ""}${change.toFixed(2)}%' in html
    assert "plan-grid" in html
    assert "plan-metric" in html
    assert '<span class="lab">方向 / RANGE</span>' in html
    assert '<span class="lab">信心 / 来源</span>' in html
    assert '<span class="lab">关键位</span>' in html
    assert '<span class="lab">失效条件</span>' in html
    assert '<div class="row"><span class="kv"><span class="lab">方向</span>' not in html


def test_dualtrack_v5_keeps_machine_track_blind_and_without_intervention_surface():
    html = read_html()

    assert "盲测中 · 仅显示 PnL" in html
    assert "进出场点位、库存、网格状态收盘后揭示" in html
    assert "本界面不提供机器轨干预操作" in html
    assert "/api/dualtrack/pause" not in html
    assert "/api/dualtrack/flatten" not in html
    assert "/api/dualtrack/override" not in html
    assert "/api/dualtrack/halt" not in html
    assert "network_order_created" not in html
    assert "network_cancel_created" not in html


def test_dualtrack_v5_does_not_fetch_attribution_before_cycle_close():
    html = read_html()

    assert "state.attribution = cycleClosed(state.cycle) ?" in html
    assert 'api(`/api/dualtrack/attribution/${cycleId}`).catch(() => null) : null' in html


def test_dashboard_server_disables_cache_for_dualtrack_v5():
    from pipelines import dashboard_server

    handler = object.__new__(dashboard_server.DashboardHandler)
    handler.path = "/dashboard-dualtrack-v5.html"

    assert handler._should_disable_static_cache() is True


def test_dashboard_server_exposes_read_only_tiger_venue_endpoint(monkeypatch):
    from pipelines import dashboard_server

    class FakeTigerVenueStatus:
        def __init__(self, output_root=None):
            self.output_root = output_root

        def snapshot(self):
            return {"status": "ready", "safety": {"dashboard_read_only": True}}

    monkeypatch.setattr(dashboard_server, "TigerVenueStatus", FakeTigerVenueStatus)

    response = dashboard_server.build_dualtrack_tiger_venue_response(output_root=ROOT / "outputs")

    assert response == {"status": "ready", "safety": {"dashboard_read_only": True}}


def test_dashboard_server_exposes_read_only_market_bars_endpoint(tmp_path):
    from pipelines import dashboard_server

    response = dashboard_server.build_dualtrack_market_bars_response(
        market_db=tmp_path / "missing.db",
        config={},
        as_of="2026-07-05T01:03:59+00:00",
        limit=2,
    )

    assert response["schema_version"] == "dualtrack-market-bars-v1"
    assert response["status"] == "seeded"
    assert response["is_synthetic"] is True
    assert "synthetic_seed" in response["quality_flags"]
    assert response["safety"]["read_only"] is True
    assert response["safety"]["opens_order_clients"] is False
    assert "/api/dualtrack/market/bars" not in dashboard_server._DUALTRACK_POST_ENDPOINTS
