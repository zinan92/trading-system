from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _html() -> str:
    return (ROOT / "dashboard-gridmind.html").read_text(encoding="utf-8")


def test_gridmind_keeps_the_compact_production_console_layout() -> None:
    html = _html()

    assert "GRID<em>MIND</em>" in html
    assert 'class="metrics"' in html
    assert 'class="workspace"' in html
    assert 'class="control-rail"' in html
    assert 'data-standard-kline-host' in html
    assert 'packages/standard-kline/standard-kline.js' in html
    assert '<canvas id="chart"' not in html
    assert "autoscaleInfoProvider" in html
    assert "includeGridRange" in html


def test_gridmind_restores_all_production_controls() -> None:
    html = _html()

    required_ids = {
        "directionChoices",
        "styleChoices",
        "gridModeChoices",
        "smartFill",
        "rangeLow",
        "rangeHigh",
        "gridCount",
        "gridNotional",
        "leverage",
        "outOfRange",
        "previewSummary",
        "startRobot",
        "stopRobot",
        "adjustLow",
        "adjustHigh",
        "applyAdjustment",
        "resetStats",
        "showEma",
        "showMacd",
        "aiReceiptDialog",
        "live",
        "positions",
        "orders",
        "fills",
        "review",
        "shadows",
        "history",
    }
    for element_id in required_ids:
        assert f'id="{element_id}"' in html

    for action in ("preview", "start", "stop", "adjust_plan", "reset_statistics"):
        assert f"control('{action}'" in html or f'control("{action}"' in html


def test_gridmind_preserves_traceability_and_safe_control_copy() -> None:
    html = _html()

    assert "Nautilus 迁移门禁" in html
    assert "操作者" in html
    assert "动作时间（北京）" in html
    assert "手动平仓" in html
    assert "计划版本" in html
    assert "登录会话已过期" in html
    assert "行情" in html and "禁止新开仓" in html
    assert "Strategy Shadows" in html
    assert "12小时复盘" in html


def test_gridmind_positions_show_lifecycle_times_and_keeps_immutable_fill_trace() -> None:
    html = _html()

    assert 'table("#positions",["状态","方向","数量","开仓时间（北京）","平仓时间（北京）"' in html
    assert 'fills=execution.fills||[]' in html
    assert 'fillAction(fill)' in html
    assert 'beijingDateTime(order.ts)' in html
    assert 'trades.slice().reverse().map(trade=>' in html


def test_gridmind_consumes_stable_read_model_without_recalculating_trading_truth() -> None:
    html = _html()

    assert "api('/api/trading-system/read-model')" in html
    assert "data?.strategy||{}" in html
    assert "data?.execution||{}" in html
    assert "execution.counts||{}" in html
    assert "pnl.total" in html
    assert "pnl.return_pct" in html
    assert "productionStrategyLabel(strategySummary)" in html

    forbidden = (
        "api('/api/strategy-console/current')",
        "data?.production_plan",
        "data?.production_execution",
        "(Number(pnl.realized)||0)+(Number(pnl.unrealized)||0)",
        "total/Number(account.starting_cash)*100",
        "(high-low)/count",
        '.filter(order=>order.state==="accepted")',
        '.filter(trade=>trade.status==="open")',
        'engine==="nautilus_paper"',
        'engine==="legacy_paper"',
        'm?.provider==="binance_usdm"',
    )
    for expression in forbidden:
        assert expression not in html


def test_gridmind_tabs_show_authoritative_lifecycle_counts() -> None:
    html = _html()

    for element_id in ("positionsCount", "ordersCount", "tradesCount"):
        assert f'id="{element_id}"' in html
    assert 'counts?.open_position_count' in html
    assert 'counts?.open_order_count' in html
    assert 'counts?.trade_count' in html
    assert 'counts.completed_round_trip_count' in html


def test_v5_route_serves_gridmind_without_removing_legacy_console() -> None:
    server = (ROOT / "pipelines" / "dashboard_server.py").read_text(encoding="utf-8")

    assert 'if parsed.path == "/dashboard-v5.html":' in server
    assert 'self._serve_static_alias("/dashboard-gridmind.html")' in server
    assert (ROOT / "dashboard-dualtrack-split.html").exists()
