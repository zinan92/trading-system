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
    assert "autoscaleInfoProvider:original=>original()" in html
    assert "includeGridRange" not in html


def test_gridmind_defaults_to_30m_without_changing_strategy_input_timeframes() -> None:
    html = _html()

    assert 'timeframe:"30m"' in html


def test_gridmind_renders_last_trusted_read_model_before_optional_30m_refresh() -> None:
    html = _html()

    load_body = html.split("async function load", 1)[1].split("async function control", 1)[0]
    assert "if(!state.market||!isFresh(state.market))state.market=data.market;render();" in load_body
    assert load_body.index("state.market=data.market;render();") < load_body.index("await refreshMarket()")


def test_gridmind_header_is_a_live_xau_market_tape_without_self_check() -> None:
    html = _html()

    assert 'id="headerPair">XAU / USDT<' in html
    assert 'id="headerChange"' in html
    assert "function displayTradingPair(market)" in html
    assert "market?.provider_symbol" in html
    assert 'function isTrustedHeaderMarket(m){return isFresh(m)&&m?.timeframe==="1m"}' in html
    assert "function nextHeaderPriceDirection" in html
    assert 'current>previous?"up":"down"' in html
    assert "function marketTodayChangePct(market,dailyMarket=state.dailyMarket)" in html
    assert "timeframe=1d&limit=2" in html
    assert "sameVenueMarket(baseMarket,daily)" in html
    assert "交易所当日开盘价至今" in html
    assert 'class="pill run-state" id="marketBadge"' in html
    assert "acceptedOrderCount>0" in html
    assert 'data?.completeness?.status!=="complete"' in html
    assert 'risk.outcome!=="allow"' in html
    assert '(risk.blockers||[]).length>0' in html
    assert 'identity!==state.headerMarketIdentity' in html
    assert "gridmind-run-pulse" in html
    assert "prefers-reduced-motion:reduce" in html
    assert "healthButton" not in html
    assert ">自检<" not in html
    assert 'timeframe=${encodeURIComponent(state.timeframe)}' in html
    assert 'state.timeframe==="1m"' in html
    assert "build_strategy_timeframes" not in html


def test_gridmind_trade_activity_toasts_are_read_only_and_fail_silent() -> None:
    html = _html()

    assert 'id="tradeToasts"' in html
    assert 'aria-live="polite"' in html
    assert "function tradeActivitySnapshotTrusted(data)" in html
    assert 'accounting?.completeness?.status==="complete"' in html
    assert "accountingSnapshotTrusted(execution.current_accounting)" in html
    assert 'const CLOSE_FILL_EVENTS=new Set(["exit","stop","target"' in html
    assert "function fillStableKey(fill,trade=null)" in html
    assert "function observeTradeActivity(data)" in html
    assert "observeTradeActivity(data)" in html.split("function applyReadModel", 1)[1].split("function renderTrend", 1)[0]
    assert "observeTradeActivity" not in html.split("function render(){", 1)[1].split("function renderRobotControls", 1)[0]
    assert "function armTradeAudio()" in html
    assert "window.AudioContext||window.webkitAudioContext" in html
    assert 'window.addEventListener("pointerdown",armTradeAudio' in html
    assert 'window.addEventListener("keydown",armTradeAudio' in html
    assert "new Notification" not in html


def test_gridmind_draws_only_visible_price_overlays_with_one_axis_label() -> None:
    html = _html()

    assert "visibleOhlcPriceWindow(market)" in html
    assert "state.chart?.getVisibleOhlcRange?.(100)" in html
    assert "visibleWindowOrders=visibleOrders.filter" in html
    assert "const labelledOrder=" in html
    assert "showLabel=order===labelledOrder" in html
    assert 'axisLabelVisible:showLabel' in html
    assert 'rgba(148,163,184,.16)' in html
    assert 'rgba(40,199,111,.38)' in html

    install = html.index("state.chart.setAdaptedData(StandardKline.adaptBarPayload(market)")
    overlay = html.index("state.chart.setPriceLines(buildVisibleChartPriceLines", install)
    assert install < overlay


def test_gridmind_mobile_layout_prevents_global_horizontal_overflow() -> None:
    html = _html()

    assert "html,body{max-width:100%;overflow-x:hidden}" in html
    assert ".workspace,.market-column,.control-rail,.chart-card,.data-card,.chart-tools,.timeframes{min-width:0}" in html
    assert ".console-wrap{max-width:100%;padding:7px}" in html


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

    for action in ("preview", "start", "stop", "extend_range", "reset_statistics"):
        assert f"control('{action}'" in html or f'control("{action}"' in html

    assert "expected_strategy_plan_id:plan.strategy_plan_id" in html
    assert "间距、每格金额和已有持仓 TP/SL 保持不变" in html
    assert "await requestPreview({useInputs:true});const result=await control('adjust_plan'" not in html


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


def test_gridmind_always_shows_the_locked_production_strategy_summary() -> None:
    html = _html()

    assert 'id="productionStrategySummary"' in html
    assert "function productionStrategySummaryModel(summary,runtime)" in html
    assert "当前生产计划未运行" in html
    assert "directionScope" in html
    assert "notional_mode_label" in html
    assert "最大风险" in html
    assert "杠杆" in html
    assert "renderProductionStrategySummary(strategySummary,runtime)" in html
    assert html.index('id="productionStrategySummary"') < html.index('id="gridSummary"')
    assert "accepted_buy_order_count" in html
    assert "accepted_sell_order_count" in html
    summary_body = html.split("function productionStrategySummaryModel", 1)[1].split("function renderProductionStrategySummary", 1)[0]
    assert "state.preview" not in summary_body
    assert "formDirty" not in summary_body
    assert "#gridNotional" not in summary_body


def test_gridmind_review_is_a_same_cycle_evidence_ledger() -> None:
    html = _html()

    for label in ("当时计划", "判断", "结果与分析", "方向", "网格规格", "执行", "PnL", "关键位", "TP / SL", "反事实"):
        assert label in html
    assert "data?.review?.cycle_packages" in html
    assert "data?.review?.selected_cycle_id" in html
    assert "reviewProposalForPlan" in html
    assert "口径不一致" in html
    assert 'row?.variant_id==="production"&&row?.status==="pass"' in html
    assert "同周期、同历史、同执行契约且计划身份匹配的 production 基准" in html
    assert "reviewShadowInputsMatch" in html
    assert "reviewShadowPlanMatches" in html
    assert "已实现" in html and "未实现" in html
    assert "仅为建议，未自动应用" in html


def test_gridmind_positions_show_lifecycle_times_and_keeps_immutable_fill_trace() -> None:
    html = _html()

    assert 'table("#positions",["状态","方向","数量","开仓时间（北京）","平仓时间（北京）"' in html
    assert 'fills=execution.fills||[]' in html
    assert 'fillAction(fill)' in html
    assert 'beijingDateTime(order.updated_at??order.ts)' in html
    assert 'trades.slice().reverse().map(trade=>' in html
    assert 'trade.entry_quantity??trade.quantity' in html
    assert 'trade.close_reason_label||"未知"' in html
    assert 'trade.realized_pnl' in html


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
    assert 'renderTabCounts(counts,acceptedOrderCount)' in html
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


def test_gridmind_current_order_table_uses_only_backend_classified_accepted_lifecycles() -> None:
    html = _html()

    assert "当前委托" in html
    assert "reconcileOrderLifecycle(data)" in html
    assert "rank>previousRank" in html
    assert "acceptedLifecycleOrders=displayedAcceptedOrderLifecycle()" in html
    assert "current.has(orderId)" in html
    assert "renderTables(execution,acceptedLifecycleOrders)" in html
    assert 'acceptedOrders.slice().reverse().map(order=>[esc(order.state_label||"未知状态")' in html
    assert "openOrders.map(order=>" not in html
    assert '["挂单中",side(order.side)' not in html


def test_gridmind_order_protection_never_guesses_across_plans() -> None:
    html = _html()

    assert 'const protection=order?.protection||{}' in html
    assert 'protection.status==="known"?num(protection[key]):"未知"' in html
    assert 'protectionText(order,"tp")' in html
    assert 'protectionText(order,"sl")' in html


def test_gridmind_lifecycle_retention_cannot_change_controls_or_chart_truth() -> None:
    html = _html()

    assert "orderLifecycle:{cycleId:null,byId:new Map(),anonymous:[],currentIds:new Set()}" in html
    assert "state.orderLifecycle.byId.get(orderId)" in html
    assert "state.orderLifecycle.cycleId!==cycleId" in html
    assert "state.orderLifecycle.currentIds=new Set(incoming.map" in html
    assert "applyReadModel(data)" in html
    assert "applyReadModel(latest)" in html
    assert "renderRobotControls(data,fresh,execution)" in html
    assert "visibleOrders=previewing?startupPreview.orders:(execution.open_orders||[])" in html
    assert "openOrders=counts.open_order_count" in html


def test_gridmind_range_adjustment_is_an_explicit_read_only_draft_mode() -> None:
    html = _html()

    assert 'id="gridAdjustToggle"' in html
    assert 'aria-pressed="false"' in html
    assert "function toggleGridAdjustMode()" in html
    assert "function runtimeMatchesProductionPlan" in html
    assert 'actual==="running"' in html
    assert "runtimePlanId===planId" in html
    assert "Number(runtime.strategy_plan_version)===Number(plan.version)" in html
    assert "expected_strategy_plan_version:draft.expectedPlanVersion" in html
    assert "state.gridAdjustMode&&Boolean(draft)" in html
    assert 'data-grid-drag="upper"' in html
    assert 'data-grid-drag="move"' in html
    assert 'data-grid-drag="lower"' in html
    assert ".grid-hit.upper,.grid-hit.lower{height:16px;cursor:ns-resize}" in html
    assert ".grid-hit.interior{cursor:grab" in html
    assert "grid-ghost-line" in html
    assert 'data-grid-action="confirm"' in html
    assert 'data-grid-action="cancel"' in html
    assert 'control("preview_range"' in html
    assert "grid-range-drag-preview-v1" in html
    assert "只读核对卡" in html
    assert "orders_created: 0" not in html
    assert "Object.entries(requiredEffects).some" in html


def test_gridmind_range_review_card_shows_required_old_to_new_fields() -> None:
    html = _html()

    assert 'id="gridRangeReviewDialog"' in html
    for label in (
        "下边界",
        "上边界",
        "Range 总宽度",
        "网格模式",
        "网格数量",
        "单格间距 / 比例",
        "每格名义",
        "总名义仓位",
        "预计保证金",
        "实际杠杆",
        "最大风险",
        "撤单 / 新单",
        "当前持仓",
        "TP / SL",
    ):
        assert label in html
    assert 'id="recalculateGridRangeRisk"' in html
    assert "recalculate_notional_by_risk_budget:recalculate" in html
    assert "尚未交易新网格" in html
    assert "停止+平仓+撤单+交易新网格" not in html


def test_uncertain_start_requires_persisted_complete_start_evidence() -> None:
    html = _html()

    assert "accepted=Number(counts.accepted_order_count)" in html
    assert "runtime.last_action==='start'" in html
    assert "runtime.accepted_order_count_known===true" in html
    assert "samePlan" in html
    assert "控制面已确认完整网格启动" in html
    assert "openOrders=Number(counts.open_order_count)" in html


def test_gridmind_order_state_is_always_escaped_as_text() -> None:
    html = _html()

    assert 'esc(order.state_label||"未知状态")' in html
    assert "order.state_label||order.state" not in html


def test_v5_route_serves_gridmind_without_removing_legacy_console() -> None:
    server = (ROOT / "pipelines" / "dashboard_server.py").read_text(encoding="utf-8")

    assert 'if parsed.path == "/dashboard-v5.html":' in server
    assert 'self._serve_static_alias("/dashboard-gridmind.html")' in server
    assert (ROOT / "dashboard-dualtrack-split.html").exists()
