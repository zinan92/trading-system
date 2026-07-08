from pathlib import Path

from services.dualtrack_store import DualTrackPlanStore
from services.journal_store import write_json


ROOT = Path(__file__).resolve().parents[1]


def read_html() -> str:
    return (ROOT / "dashboard-dualtrack-v5.html").read_text(encoding="utf-8")


def extract_function(html: str, name: str) -> str:
    marker = f"function {name}"
    start = html.index(marker)
    next_start = html.find("\nfunction ", start + len(marker))
    if next_start == -1:
        next_start = html.find("\nconst ", start + len(marker))
    if next_start == -1:
        next_start = len(html)
    return html[start:next_start]


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
    assert "运行状态 · 样本完整性" in html
    assert "复盘闭环" in html


def test_dualtrack_v5_uses_dualtrack_api_contracts_and_backend_market_bars():
    html = read_html()

    assert 'api("/api/dualtrack/cycle/current")' in html
    assert 'api(`/api/dualtrack/plan/${cycleId}`)' in html
    assert 'api(`/api/dualtrack/machine/${cycleId}`)' in html
    assert 'api("/api/dualtrack/orders"' in html
    assert 'cycleClosed(state.cycle) ? await api(`/api/dualtrack/attribution/${cycleId}`)' in html
    assert "function cycleClosed(cycle)" in html
    assert 'api("/api/dualtrack/ledger")' in html
    assert "`/api/dualtrack/market/bars?timeframe=${encodeURIComponent(state.mainTf)}&limit=96`" in html
    assert 'api("/api/dualtrack/market/bars?symbol=GOLD&timeframe=15m&limit=64")' in html
    assert 'api("/api/dualtrack/market/bars?symbol=GOLD&timeframe=1h&limit=64")' in html
    assert 'api("/api/dualtrack/runtime/status")' in html
    assert 'api("/api/dualtrack/verdict"' in html
    assert "data/vendor/lightweight-charts.standalone.production.js" in html
    assert "packages/standard-kline/standard-kline.js" in html
    assert 'data-standard-kline-host' in html
    assert "function renderMainKline()" in html
    assert "StandardKline.StandardKlineChart" in html
    assert "window.dualtrackStandardKline" in html
    assert "function buildHumanPlanPriceLines(plan)" in html
    assert "function buildTrackFillPriceLines(fills, color)" in html
    assert "function buildTrackFillMarkers(fills, candles, track, color)" in html
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
    assert '<svg id="mainChart"' not in html
    assert "function drawChart" not in html
    assert "contextMeta" in html
    assert "derived_from_1m" in html
    assert "renderRuntimeStatus" in html
    assert "machine_fills_hidden" in html
    assert "previous_closeout" in html
    assert "最近已收盘" in html
    assert "查看最近回放" in html
    assert "Obsidian fallback" in html
    assert "late/draft：显示与记录，不参与盲测评分" in html
    assert "state.candles.filter((_, i) => i %" not in html
    assert 'api("/api/connectors/config/rollback"' not in html
    assert "write:true" not in html
    assert '"write":true' not in html
    assert "accept_warnings" not in html
    assert "acknowledgement" not in html
    assert "xauusdt@kline_1m" not in html
    assert "fstream.binance.com" not in html
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
    assert '<div id="ctx15" class="context-body" data-standard-kline-context="15m"></div>' in html
    assert '<div id="ctx1h" class="context-body" data-standard-kline-context="1h"></div>' in html
    assert "function renderContextKline(" in html
    assert "ensureContextKline(" in html
    assert "height:150" in html
    assert "function drawMini" not in html
    assert "plan-grid" in html
    assert "plan-metric" in html
    assert '<span class="lab">方向 / RANGE</span>' in html
    assert '<span class="lab">信心 / 来源</span>' in html
    assert '<span class="lab">关键位</span>' in html
    assert '<span class="lab">失效条件</span>' in html
    assert '<div class="row"><span class="kv"><span class="lab">方向</span>' not in html


def test_dualtrack_v5_renders_open_ended_plan_ranges_without_zero_bound():
    html = read_html()

    assert "const hasPrice" in html
    assert "const rangeText" in html
    assert "open upside" in html
    assert "fmt(plan.range?.high)" not in html
    assert "plan.range?.high || []" not in html


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
    assert "submit_command" not in html
    assert "api_key" not in html.lower()
    assert "private_key" not in html.lower()


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


def test_dualtrack_v5_task06_main_timeframe_switch_is_wired() -> None:
    html = read_html()

    assert "state.mainTf" in html
    assert "function bindTimeframeSegment()" in html
    assert 'button[data-tf]' in html
    assert "state.mainTf = button.dataset.tf" in html
    assert "renderMainKline()" in html
    assert "loadMainMarket()" in html
    assert "`/api/dualtrack/market/bars?timeframe=${encodeURIComponent(state.mainTf)}&limit=96`" in html
    assert 'api("/api/dualtrack/market/bars?limit=96")' not in html


def test_dualtrack_v5_task06_context_charts_use_standard_kline_not_svg_drawmini() -> None:
    html = read_html()

    assert '<div id="ctx15" class="context-body" data-standard-kline-context="15m"></div>' in html
    assert '<div id="ctx1h" class="context-body" data-standard-kline-context="1h"></div>' in html
    assert "function renderContextKline(" in html
    assert "ensureContextKline(" in html
    assert "state.contextKlines" in html
    assert "new StandardKline.StandardKlineChart" in html
    assert "function drawMini" not in html
    assert "drawMini(" not in html


def test_dualtrack_v5_task06_runtime_status_copy_clarifies_watch_semantics() -> None:
    html = read_html()

    assert "运行状态 · 样本完整性" in html
    assert "盘中 · 正常" in html
    assert "需处理 ·" in html
    assert "已收盘 · 已评分" in html
    assert "运行红灯 · 样本完整性" not in html
    assert "WATCH" not in extract_function(html, "statusText")


def test_dualtrack_v5_task06_layers_are_display_mapped_to_chinese() -> None:
    html = read_html()

    assert "const LAYER_LABELS" in html
    assert '"grid:traded":"网格 · 已成交"' in html
    assert '"trend:armed":"趋势 · 已激活"' in html
    assert "layerLabel(layer)" in html
    assert "${esc(layer)}" not in extract_function(html, "renderMachine")


def test_dualtrack_v5_task06_replay_links_disable_empty_urls() -> None:
    html = read_html()

    assert "replayLink(" in html
    assert "暂无可回放周期" in html
    assert 'href=""' not in html
    assert 'href="${esc(previousReplay)}"' not in html
    assert 'href="${esc(closeout.replay_url ||' not in html


def test_dualtrack_v5_task06_blind_protocol_does_not_feed_machine_points_to_intraday_charts() -> None:
    html = read_html()
    main = extract_function(html, "renderMainKline")
    context = extract_function(html, "renderContextKline")

    assert "state.human?.fills" in main
    assert "state.machine.fills" not in main
    assert "state.machine?.fills" not in main
    assert "state.machine.fills" not in context
    assert "state.machine?.fills" not in context
    assert "markers:" not in context
    assert "priceLines:" not in context


def test_dashboard_server_runtime_status_hides_machine_fills_until_close(tmp_path, monkeypatch):
    from pipelines import dashboard_server

    class FakeMarketFeed:
        def __init__(self, *args, **kwargs):
            pass

        def snapshot(self, **kwargs):
            return {
                "status": "fallback",
                "source_mode": "binance_usdm_fallback",
                "symbol": "GOLD",
                "timeframe": "1m",
                "provider": "binance_usdm",
                "fresh": True,
                "latest_timestamp": "2026-07-05T02:04:00+00:00",
                "age_minutes": 1.0,
            }

    monkeypatch.setattr(dashboard_server, "DualTrackMarketFeed", FakeMarketFeed)
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    DualTrackPlanStore(output).save_ai_plan({
        "cycle_id": cycle_id,
        "author": "ai",
        "direction": "long",
        "range": {"low": 3960.0, "high": None},
        "key_levels": [3992.0],
        "invalidation": [{"side": "below", "price": 3960.0, "confirm": "touch"}],
        "confidence": 7,
        "source": "obsidian",
        "status": "fallback_active",
    }, now="2026-07-05T01:00:00+00:00")
    write_json(output / "dualtrack" / "runner" / f"{cycle_id}.json", [{
        "ts": "2026-07-05T02:04:00+00:00",
        "cycle_id": cycle_id,
        "event": "intraday",
        "detail": {"bar_count": 64, "prev_range": 12.0},
    }])
    write_json(output / "dualtrack" / "cycles" / f"{cycle_id}.json", [{
        "cycle_id": cycle_id,
        "machine_stood_down": False,
        "layers": ["grid:traded", "trend:armed"],
    }])
    write_json(output / "dualtrack" / "fills" / f"{cycle_id}_machine.json", [{
        "fill_id": "m1",
        "ts": "2026-07-05T02:03:00+00:00",
        "realized_pnl": -1.0,
    }])

    response = dashboard_server.build_dualtrack_runtime_status_response(
        output_root=output,
        as_of="2026-07-05T02:05:00+00:00",
    )

    assert response["schema_version"] == "dualtrack-runtime-status-v1"
    assert response["sample"]["valid_now"] is True
    assert response["sample"]["machine_fills_hidden"] is True
    assert response["sample"]["machine_fill_count"] is None
    assert response["runner"]["bar_count"] == 64
    assert response["market"]["provider"] == "binance_usdm"


def test_dashboard_server_runtime_status_exposes_previous_closed_cycle_summary(tmp_path, monkeypatch):
    from pipelines import dashboard_server

    class FakeMarketFeed:
        def __init__(self, *args, **kwargs):
            pass

        def snapshot(self, **kwargs):
            return {
                "status": "fallback",
                "source_mode": "binance_usdm_fallback",
                "symbol": "GOLD",
                "timeframe": "1m",
                "provider": "binance_usdm",
                "fresh": True,
                "latest_timestamp": "2026-07-05T14:04:00+00:00",
                "age_minutes": 1.0,
            }

    monkeypatch.setattr(dashboard_server, "DualTrackMarketFeed", FakeMarketFeed)
    output = tmp_path / "outputs"
    current_cycle = "2026-07-05_NIGHT"
    previous_cycle = "2026-07-05_DAY"
    DualTrackPlanStore(output).save_ai_plan({
        "cycle_id": current_cycle,
        "author": "ai",
        "direction": "long",
        "range": {"low": 3960.0, "high": None},
        "key_levels": [3992.0],
        "invalidation": [{"side": "below", "price": 3960.0, "confirm": "touch"}],
        "confidence": 7,
        "source": "obsidian",
        "status": "fallback_active",
    }, now="2026-07-05T13:00:00+00:00")
    write_json(output / "dualtrack" / "runner" / f"{current_cycle}.json", [{
        "ts": "2026-07-05T14:04:00+00:00",
        "cycle_id": current_cycle,
        "event": "intraday",
        "detail": {"bar_count": 64},
    }])
    write_json(output / "dualtrack" / "cycles" / f"{current_cycle}.json", [{
        "cycle_id": current_cycle,
        "machine_stood_down": False,
        "layers": ["grid:traded"],
    }])
    write_json(output / "dualtrack" / "fills" / f"{current_cycle}_machine.json", [{
        "fill_id": "current-machine",
        "ts": "2026-07-05T14:03:00+00:00",
        "realized_pnl": 2.0,
    }])
    write_json(output / "dualtrack" / "fills" / f"{previous_cycle}_machine.json", [{
        "fill_id": "previous-machine",
        "ts": "2026-07-05T12:50:00+00:00",
        "realized_pnl": 4.0,
    }])
    write_json(output / "dualtrack" / "fills" / f"{previous_cycle}_human.json", [{
        "fill_id": "previous-human",
        "ts": "2026-07-05T12:51:00+00:00",
        "realized_pnl": 1.0,
    }])
    write_json(output / "dualtrack" / "attribution" / f"{previous_cycle}.json", [{
        "cycle_id": previous_cycle,
        "tracks": {},
    }])
    write_json(output / "dualtrack" / "ledger" / "daily" / "2026-07-05.json", [{
        "date": "2026-07-05",
        "total_pnl": 5.0,
    }])

    response = dashboard_server.build_dualtrack_runtime_status_response(
        output_root=output,
        as_of="2026-07-05T14:05:00+00:00",
    )

    assert response["cycle_id"] == current_cycle
    assert response["sample"]["machine_fills_hidden"] is True
    assert response["sample"]["machine_fill_count"] is None
    assert response["previous_closeout"]["cycle_id"] == previous_cycle
    assert response["previous_closeout"]["attribution_available"] is True
    assert response["previous_closeout"]["ledger_available"] is True
    assert response["previous_closeout"]["machine_fills_hidden"] is False
    assert response["previous_closeout"]["machine_fill_count"] == 1
    assert response["previous_closeout"]["human_fill_count"] == 1
    assert response["previous_closeout"]["replay_url"].endswith(f"cycle={previous_cycle}")
