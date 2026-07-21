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


def test_gridmind_retains_trusted_candles_and_loads_older_history_safely() -> None:
    html = _html()

    assert 'id="chartHistoryNotice"' in html
    assert "retainLastTrustedMarket" in html
    assert "retained_last_trusted" in html
    assert "loadOlderMarketBars" in html
    assert "page.historical_page!==true||page.trusted_history!==true" in html
    assert "pagination?.has_more===false" in html
    assert "getVisibleLogicalRange" in html
    assert "restoreVisibleLogicalRange" in html
    assert "standard-kline:viewchange" in html
    assert 'status:"error",fresh:false,trusted:false' in html


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
    assert 'beijingDateTime(order.updated_at??order.cancelled_at??order.ts)' in html
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
    assert 'renderTabCounts(counts,lifecycleOrders.length)' in html
    assert 'openOrderCount=counts.open_order_count' in html
    assert 'counts?.trade_count' in html
    assert 'counts.completed_round_trip_count' in html


def test_gridmind_uses_revision_for_same_phase_lifecycle_recovery() -> None:
    html = _html()

    assert "revision=Number(order?.state_revision)" in html
    assert "previousRevision=Number(previous?.state_revision)" in html
    assert "hasRevision=Number.isInteger(revision)&&revision>0" in html
    assert "hasPreviousRevision=Number.isInteger(previousRevision)&&previousRevision>0" in html
    assert "revision>previousRevision" in html
    assert "revision>=previousRevision" in html


def test_gridmind_order_table_preserves_terminal_lifecycle_rows() -> None:
    html = _html()

    assert "订单状态" in html
    assert "reconcileOrderLifecycle(data)" in html
    assert "rank>previousRank" in html
    assert "renderTables(execution,lifecycleOrders)" in html
    assert 'orders.slice().reverse().map(order=>[esc(order.state_label||"未知状态")' in html
    assert "openOrders.map(order=>" not in html
    assert '["挂单中",side(order.side)' not in html


def test_gridmind_lifecycle_retention_cannot_change_controls_or_chart_truth() -> None:
    html = _html()

    assert "orderLifecycle:{cycleId:null,byId:new Map(),anonymous:[]}" in html
    assert "state.orderLifecycle.byId.get(orderId)" in html
    assert "state.orderLifecycle.cycleId!==cycleId" in html
    assert "applyReadModel(data)" in html
    assert "applyReadModel(latest)" in html
    assert "renderRobotControls(data,fresh,execution)" in html
    assert "visibleOrders=previewing?state.preview.orders:(execution.open_orders||[])" in html
    assert "accepted=counts.open_order_count" in html


def test_gridmind_order_state_is_always_escaped_as_text() -> None:
    html = _html()

    assert 'esc(order.state_label||"未知状态")' in html
    assert "order.state_label||order.state" not in html


def test_v5_route_serves_gridmind_without_removing_legacy_console() -> None:
    server = (ROOT / "pipelines" / "dashboard_server.py").read_text(encoding="utf-8")

    assert 'if parsed.path == "/dashboard-v5.html":' in server
    assert 'self._serve_static_alias("/dashboard-gridmind.html")' in server
    assert (ROOT / "dashboard-dualtrack-split.html").exists()
