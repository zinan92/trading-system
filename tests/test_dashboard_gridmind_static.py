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

    load_body = html.split("async function load({", 1)[1].split("async function control", 1)[0]
    assert "state.market=reconcileReadModelMarket(state.market,data.market);render();" in load_body
    assert load_body.index("reconcileReadModelMarket") < load_body.index("await refreshMarket()")


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
    assert 'dashboardMarketBarsUrl({symbol,timeframe:"1d",limit:2})' in html
    assert "sameVenueMarket(baseMarket,daily)" in html
    assert "交易所当日开盘价至今" in html
    assert 'class="pill run-state" id="marketBadge"' in html
    assert "acceptedOrderCount>0" in html
    assert "function hasProtectedDcaTarget(data)" in html
    assert "lifecycleProtected=acceptedOrderCount>0||hasProtectedDcaTarget(data)" in html
    assert 'String(target?.status||"").toLowerCase()==="accepted"' in html
    assert 'data?.completeness?.status!=="complete"' in html
    assert 'risk.outcome!=="allow"' in html
    assert '(risk.blockers||[]).length>0' in html
    assert 'identity!==state.headerMarketIdentity' in html
    assert "gridmind-run-pulse" in html
    assert "prefers-reduced-motion:reduce" in html
    assert "healthButton" not in html
    assert ">自检<" not in html
    assert "dashboardMarketBinding" in html
    assert "dashboardMarketBarsUrl({symbol,timeframe,limit:240})" in html
    assert "headerMarket=state.market||data?.market||{}" in html
    assert "function syncSelectedMarketLabels(market,execution)" in html
    assert "syncSelectedMarketLabels(market,execution)" in html
    assert "build_strategy_timeframes" not in html


def test_gridmind_labels_only_auditable_grid_lifecycle_loops() -> None:
    html = _html()

    assert "function renderGridLifecycle(execution)" in html
    assert "已证实 ${num(completed)} 格：开仓 → TP → 原价重挂" in html
    assert "生命周期证据不完整，未计作完成循环" in html
    assert "renderGridLifecycle(execution)" in html.split("function render(){", 1)[1].split("function renderRobotControls", 1)[0]


def test_gridmind_projects_external_dca_lifecycle_as_read_only_status_card() -> None:
    html = _html()

    assert 'id="externalDcaCard"' in html
    assert "function renderExternalDcaLifecycle(lifecycle)" in html
    assert "data?.external_dca||execution?.external_dca" in html
    assert "source_strategy_plan_digest" in html
    assert "保护 ${protection.status||\"unknown\"}" in html


def test_gridmind_trade_activity_toasts_are_read_only_and_fail_silent() -> None:
    html = _html()

    assert 'id="tradeToasts"' in html
    assert 'aria-live="polite"' in html
    assert "function tradeActivitySnapshotTrusted(data)" in html
    assert "function tradeActivityFactsTrusted(accounting)" in html
    assert 'accounting?.completeness?.status==="complete"' in html
    assert "tradeActivityFactsTrusted(execution.accounting)" in html
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


def test_gridmind_invalid_manual_draft_is_visible_and_cannot_start() -> None:
    html = _html()

    assert "draftProblem:null" in html
    assert "function invalidStrategyDraft()" in html
    assert 'title:"每格名义缺失"' in html
    assert 'button.textContent=locked?"手动":"AUTO"' in html
    assert "function renderParameterValidity()" in html
    assert 'input.classList.toggle("is-invalid",invalid)' in html
    assert 'box.className="preview-summary invalid"' in html
    assert 'start.textContent=!controlled?"登录后可启动机器人":invalidDraft?"请先修复参数"' in html
    assert "!invalidDraft&&fresh" in html
    assert "state.preview=null;const inputProblem=invalidStrategyDraft()" in html


def test_gridmind_control_cards_expand_while_runtime_status_scrolls() -> None:
    html = _html()

    assert ".control-rail{position:static;height:auto;min-height:0;max-height:none;overflow:visible" in html
    assert "grid-auto-rows:max-content" in html
    assert ".market-card,.strategy-card{height:auto}" in html
    assert ".live-card .card-body{height:295px;max-height:295px;overflow-y:auto" in html
    assert "运行中调整" not in html
    # actionStatus was deliberately restored as the visible control status
    # line by the DCA V5 controls change (#154); only the old adjust card
    # controls must stay retired.
    for retired_id in ("adjustLow", "adjustHigh", "applyAdjustment", "resetStats"):
        assert f'id="{retired_id}"' not in html
    assert 'id="actionStatus"' in html
    assert ".chart{height:450px;min-height:320px}" in html
    assert "StandardKlineChart(host,{height:450,minHeight:320" in html
    assert ".chart{height:420px}" in html
    assert ".chart{height:340px}" in html


def test_gridmind_adds_an_additive_park_ai_chat_without_replacing_existing_controls() -> None:
    html = _html()

    for element_id in (
        "yesterdayPnlCard",
        "yesterdayPnlFacts",
        "yesterdayPnlEvidence",
        "parkAiChatCard",
        "parkAiChatMessages",
        "parkAiChatInput",
        "parkAiSend",
        "parkAiConfirm",
        "parkAiReject",
        "parkAiSnapshot",
    ):
        assert f'id="{element_id}"' in html
    assert "/api/park-paper/ai-chat" in html
    assert 'id="startRobot"' in html
    assert 'id="stopRobot"' in html
    assert "api.deepseek.com" not in html
    assert "DEEPSEEK_API_KEY" not in html
    assert 'body:JSON.stringify({action,...payload})' in html
    assert 'post("confirm",{draft_id:pending.draft_id,plan_digest:pending.plan_digest})' in html
    assert "Grid Entry Range" in html
    assert "Grid Hard Stop" in html
    assert "TP geometry" in html
    assert "Risk Digest" in html
    assert "Local stop" in html


def test_gridmind_keeps_the_chart_above_the_fold_and_park_ai_chat_in_the_control_rail() -> None:
    html = _html()

    ai_card = html.index('id="parkAiChatCard"')
    metrics = html.index('<section class="metrics"')
    workspace = html.index('<main class="workspace"')
    rail = html.index('<aside class="control-rail">')
    control = html.index('id="dashboardControlCard"')
    market_card = html.index('<section class="card market-card">')
    assert metrics < workspace < rail < control < ai_card < market_card
    assert 'class="card park-ai-chat-card top-ai-card rail-ai-card"' in html
    assert ".top-ai-card{border:1px solid rgba(63,208,224,.42)" in html


def test_gridmind_draws_the_testnet_grid_lifecycle_when_no_production_plan_exists() -> None:
    html = _html()

    assert "function lifecycleDisplayPlan(runtime)" in html
    assert "function displayPlan(data)" in html
    assert 'renderChart(market,livePlan,execution)' in html
    assert 'title:"硬止损"' in html
    assert 'row.eligibility==="eligible"||row.instrument_id===control.selection?.instrument_id' in html


def test_gridmind_labels_a_running_strategy_with_a_stale_execution_tick_as_degraded() -> None:
    html = _html()

    assert "execution_tick_health?.status===\"blocked\"" in html
    assert 'tickLost?"运行降级"' in html
    assert "function executionTickText(runtime)" in html
    assert "live tick 心跳已过期" in html
    assert "execution_tick_failure?.status===\"failed\"" in html
    assert "route_datafeed:\"行情/路由\"" in html


def test_gridmind_retains_trusted_candles_and_loads_older_history_safely() -> None:
    html = _html()

    assert 'id="chartHistoryNotice"' in html
    assert "retainLastTrustedMarket" in html
    assert "retained_last_trusted" in html
    assert "loadOlderMarketBars" in html
    assert "page.historical_page!==true||page.trusted_history!==true" in html
    assert 'host.addEventListener("wheel",()=>{' in html
    assert "state.historyInputStartRange=state.chart?.getVisibleLogicalRange?.()||null" in html
    assert "historyInputCanLoad(range,state.historyInputUntil,state.historyInputStartRange)" in html
    assert 'reason==="visible-logical-range"||reason==="set-visible-logical-range"' in html
    assert "pagination?.has_more===false" in html
    assert "getVisibleLogicalRange" in html
    assert "restoreVisibleLogicalRange" in html
    assert "standard-kline:viewchange" in html
    assert "shouldLoadOlderHistory" in html
    assert "alreadyAtOldestEdge" in html
    assert "event=>finish(event,true)" in html
    assert "event=>finish(event,false)" in html
    assert 'status:"error",fresh:false,trusted:false' in html


def test_gridmind_exposes_only_operator_strategy_inputs_and_derives_sizing() -> None:
    html = _html()

    required_ids = {
        "directionChoices",
        "styleChoices",
        "gridModeChoices",
        "smartFill",
        "rangeLow",
        "rangeHigh",
        "gridCount",
        "gridProfitTarget",
        "gridNotional",
        "leverage",
        "previewSummary",
        "startRobot",
        "stopRobot",
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

    assert 'id="outOfRange"' not in html
    assert 'mode:"manual_adaptive"' in html
    assert 'const locked=[...state.parameterLocks].sort()' in html
    assert 'data-param-lock="grid_count"' in html
    assert 'data-param-lock="profit_target"' in html
    assert 'data-param-lock="notional_per_grid"' in html
    assert 'data-param-lock="leverage"' in html
    assert "30–70 格" in html
    assert "10x 杠杆是建议值" in html
    assert 'id="startRiskDialog"' in html
    assert "确认风险并启动机器人" in html

    for action in (
        "preview",
        "prepare_start",
        "start",
        "stop",
    ):
        assert f"control('{action}'" in html or f'control("{action}"' in html

    assert "prepared_start_id:state.preparedStartId" in html
    assert "prepared_start_market_moved" in html
    assert "机器人已启动" in html
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
    assert "计划净利 ≥" in html
    assert "上限 10x" in html
    assert 'v===null||v===undefined||v===""?"--"' in html
    assert "renderProductionStrategySummary(strategySummary,runtime)" in html
    assert html.index('id="productionStrategySummary"') < html.index('id="gridSummary"')
    assert "accepted_buy_order_count" in html
    assert "accepted_sell_order_count" in html
    summary_body = html.split("function productionStrategySummaryModel", 1)[1].split("function renderProductionStrategySummary", 1)[0]
    assert "state.preview" not in summary_body
    assert "formDirty" not in summary_body
    assert "#gridNotional" not in summary_body
    assert "summary.actual_leverage??summary.leverage" not in summary_body
    assert "Plan v${summary.plan_version" not in summary_body


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

    assert 'table("#positions",["状态","方向","数量","开仓时间（北京）","开仓价","止盈","止损","未实现","操作"]' in html
    assert 'fills=execution.fills||[]' in html
    assert 'fillAction(fill)' in html
    assert 'beijingDateTime(order.updated_at??order.ts)' in html
    assert 'trades.slice().reverse().map(trade=>' in html
    assert 'trade.entry_quantity??trade.quantity??trade.units' in html
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
    assert 'protectionText(trade,"tp")' in html
    assert 'protectionText(trade,"sl")' in html
    assert "plannedProfitText(order)" in html
    assert "planned_net_profit_usd" in html
    assert "remaining_quantity??trade.remaining_units??trade.quantity" in html
    table_body = html.split("function renderTables", 1)[1].split("function reviewNumber", 1)[0]
    assert '"计划版本"' not in table_body
    assert '"版本"' not in table_body


def test_gridmind_history_uses_real_machine_ledger_and_paper_nav_fields() -> None:
    html = _html()

    assert "function historyLedgerRows(data)" in html
    assert "row?.tracks?.machine?.realized_pnl" in html
    assert "??row?.total_pnl" not in html
    assert "data?.execution?.account?.starting_balance" in html
    assert "累计生产 P&amp;L" in html
    assert "Paper NAV" in html
    assert "row.total_realized_pnl" not in html
    assert "row.cumulative_realized_pnl" not in html
    assert "recovery replay 不计入 Paper P&amp;L" in html


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
    assert "点击最终按钮前仍是只读草稿" in html
    assert "orders_created: 0" not in html
    assert "Object.entries(requiredEffects).some" in html
    assert "function createStartupGridRangeDraft()" in html
    assert 'scope:"startup"' in html
    assert 'draft.scope==="startup"?"应用参数":"确认"' in html
    assert "async function applyStartupGridRangeDraft()" in html
    assert 'lockParameter("range")' in html


def test_gridmind_start_failures_use_a_centered_dialog_not_trade_toasts() -> None:
    html = _html()

    assert 'id="startFailureDialog"' in html
    assert 'id="startFailureReason"' in html
    assert 'aria-describedby="startFailureReason"' in html
    assert 'id="startFailureReason" role="alert"' in html
    assert "启动未完成" in html
    assert "生产状态没有改变" in html
    assert 'failureSurface="toast"' in html
    assert 'failureSurface==="dialog"' in html
    assert html.count('{failureSurface:"dialog"}') == 2


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
        "每格计划净利",
        "预计最大损失",
        "撤单 / 新单",
        "当前持仓",
        "TP / SL",
    ):
        assert label in html
    assert 'id="gridRiskAcknowledgements"' in html
    assert "grid-range-risk-ack-v1" in html
    assert "请逐项勾选上方全部规格和风险确认" in html
    assert "仅 Paper 可人工覆盖" in html
    assert 'id="recalculateGridRangeRisk"' not in html
    assert "尚未交易新网格" not in html
    assert "停止+平仓+撤单+交易新网格" in html
    assert "再次核对计划版本、行情、风险确认、挂单和持仓" in html


def test_uncertain_start_requires_persisted_complete_start_evidence() -> None:
    html = _html()

    assert "accepted=Number(counts.accepted_order_count)" in html
    assert "auditedControlAcceptance(runtime,action,afterEvent)" in html
    assert "runtime.last_action==='start'" in html
    assert "runtime.accepted_order_count_known===true" in html
    assert "samePlan" in html
    assert "控制面已确认完整网格启动" in html
    assert "openOrders=Number(counts.open_order_count)" in html


def test_response_recovery_reads_audit_receipt_without_repeating_control_actions() -> None:
    html = _html()

    assert "function controlReceiptText(runtime)" in html
    assert "控制回执" in html
    assert "result=await reconcileControlOutcome('prepare_start',error,{afterEvent})" in html
    assert 'result=await reconcileControlOutcome("replace_grid",error,{afterEvent})' in html
    assert html.count("await control('prepare_start',payload,{reload:false})") == 1
    assert html.count('await control("replace_grid",payload,{reload:false})') == 1
    assert "连接中断后已通过控制回执核对" in html


def test_gridmind_order_state_is_always_escaped_as_text() -> None:
    html = _html()

    assert 'esc(order.state_label||"未知状态")' in html
    assert "order.state_label||order.state" not in html


def test_v5_route_serves_gridmind_without_removing_legacy_console() -> None:
    server = (ROOT / "pipelines" / "dashboard_server.py").read_text(encoding="utf-8")

    assert 'if parsed.path == "/dashboard-v5.html":' in server
    assert 'self._serve_static_alias("/dashboard-gridmind.html")' in server
    assert (ROOT / "dashboard-dualtrack-split.html").exists()


def test_gridmind_surfaces_layered_cloud_health_in_production_status() -> None:
    html = _html()

    assert "function cloudHealthText(data)" in html
    assert "Cloud 7×24 健康 · 全部分层证据通过" in html
    assert '["Cloud 7×24",cloudHealthText(data)]' in html
    assert "function cloudSchedulerOwnerText(data)" in html
    assert '["调度器所有者",cloudSchedulerOwnerText(data)]' in html


def test_gridmind_idle_strategy_card_is_quiet_between_sessions() -> None:
    html = _html()

    assert '"active_strategy_missing","grid_geometry_invalid","authoritative_snapshot_missing"' in html
    assert 'status.textContent=idleOnly?"暂无运行策略"' in html
    assert "上一个策略的记录" in html
