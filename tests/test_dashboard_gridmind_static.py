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
    assert "includeGridRange" not in html
    assert 'timeframe:"30m"' in html
    assert '<option value="30m" selected>30m</option>' in html
    assert "standard-kline-crosshair" in html
    assert "bottom:3px" in html


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
        "gridAdjustToggle",
        "runningStrategySummary",
        "gridReplaceDialog",
        "gridReplaceComparison",
        "recalculateGridRisk",
        "recheckGridReplace",
        "cancelGridReplace",
        "confirmGridReplace",
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

    for action in ("preview", "start", "stop", "extend_range", "replace_grid", "reset_statistics"):
        assert f"control('{action}'" in html or f'control("{action}"' in html

    assert "control('adjust_plan'" not in html
    assert 'control("adjust_plan"' not in html


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
    assert "cycle_packages" in html
    assert "production_history?.daily" in html
    assert "production_rollover" in html
    assert "function rolloverDisplayState(runtime={},rollover={},acceptedCount=0,openCount=0)" in html
    assert "自动续跑失败" in html
    assert 'rolloverRecovered?"blocked（后续启动已恢复）":rollover.status' in html
    assert "仅反事实回放" in html
    assert "不等同 execution_shadow" in html
    assert "生产基准 / 差值" in html


def test_gridmind_shows_one_line_authoritative_running_strategy_summary() -> None:
    html = _html()

    assert "function renderRunningStrategySummary(plan,runtime={})" in html
    assert 'running?"当前运行策略":"当前生产计划（未运行）"' in html
    assert 'direction==="neutral"?"中性双边"' in html
    assert 'direction==="long"?"单边做多"' in html
    assert 'direction==="short"?"单边做空"' in html
    assert 'grid.notional_mode==="auto"?"风险自动定额"' in html
    assert "每格 ${num(grid.notional_per_grid)} USD" in html
    assert "最大风险 ${maxLoss===null?\"--\":num(maxLoss)} USD" in html
    assert "renderRunningStrategySummary(plan,runtime);renderGridSummary(plan,execution)" in html


def test_gridmind_positions_and_fills_show_trade_lifecycles() -> None:
    html = _html()

    assert '当前持仓<span class="tab-count">（0）</span>' in html
    assert '当前委托<span class="tab-count">（0）</span>' in html
    assert '成交记录<span class="tab-count">（0）</span>' in html
    assert "function tradeLifecycleRows(trades)" in html
    assert "function authoritativePositionRows(execution)" in html
    assert 'updateTabCount("positions",openPositions.length)' in html
    assert 'updateTabCount("orders",accepted.length)' in html
    assert 'updateTabCount("fills",trades.length)' in html
    assert "开仓算 1 笔，平仓后仍是同一笔" in html
    assert 'table("#positions",["状态","方向","数量","开仓时间（北京）","平仓时间（北京）"' in html
    assert 'fills=execution.fills||[]' in html
    assert 'fillAction(fill)' in html
    assert 'beijingDateTime(order.ts)' in html
    assert 'table("#fills",["状态","方向","数量","入场时间（北京）","入场价","出场时间（北京）","出场价","结果","已实现"]' in html
    assert "function tradeCloseReason(trade)" in html
    assert "function tradeQuantity(trade,fills)" in html
    assert '"事件","订单 ID","版本"' not in html


def test_gridmind_orders_show_explicit_take_profit_and_stop_loss() -> None:
    html = _html()

    assert "function orderProtection(order,plan)" in html
    assert "lineage=new Set([plan?.strategy_plan_id,...(plan?.inherited_plan_ids||[])].filter(Boolean).map(String))" in html
    assert "samePlan=Boolean(order.strategy_plan_id&&lineage.has(String(order.strategy_plan_id)))" in html
    assert "samePlan=!order.strategy_plan_id" not in html
    assert 'table("#orders",["状态","方向","类型","数量","委托价","止盈","止损","时间（北京）"]' in html
    assert "未设" in html
    assert "约 +" in html
    assert "约 -" in html


def test_gridmind_explains_shadow_variants_and_expands_cycle_review() -> None:
    html = _html()

    assert "function shadowPresentation(shadow)" in html
    assert "生产基准" in html
    assert "AI 原始提案" in html
    assert "它改变了什么" in html
    assert 'class="review-ledger"' in html
    assert "当时计划" in html
    assert "复盘与分析" in html
    assert "AI 原提案" in html
    assert "function reviewProposalForPlan(packageRow,plan)" in html
    assert "sourceIds.has(String(row.proposal_id))" in html
    assert "function reviewPlanSpecComplete(plan)" in html
    assert "function reviewDirectionAssessment(plan,direction={})" in html
    assert "口径不一致" in html
    assert "不能据此评价生产方向" in html
    assert "levelEvidence=plannedLevels!==null&&plannedLevels>0&&touchedLevels!==null" in html
    assert "tpEvidence=tpOrders!==null&&tpOrders>0&&validGeometry!==null" in html
    assert 'reconciliationKind=!reconciliationKnown?"info":reconciliationOk?"good":"bad"' in html
    assert 'reconciliationVerdict=!reconciliationKnown?"证据不足"' in html
    assert 'value==="aggressive"?"激进":value==="steady"?"稳健":"缺少证据"' in html
    assert 'value==="geometric"?"等比例网格":value==="arithmetic"?"等价差网格":"缺少证据"' in html
    assert "周期包未找到可匹配的 AI 提案" in html
    assert "方向判断与本周期盈亏分开评价" in html
    assert "历史差值不是未来预测" in html
    assert "下一周期" in html
    assert "仅为复盘建议，未自动应用" in html


def test_gridmind_header_is_a_live_xau_market_tape_without_self_check() -> None:
    html = _html()

    assert 'id="headerPair">XAU / USDT<' in html
    assert 'id="headerChange"' in html
    assert "function displayTradingPair(market)" in html
    assert "market?.provider_symbol" in html
    assert 'function isTrustedHeaderMarket(m){return isFresh(m)&&m?.timeframe==="1m"}' in html
    assert "leftProvider&&rightProvider&&leftProvider===rightProvider" in html
    assert "function updateHeaderMarket(market)" in html
    assert 'price>state.headerLastPrice?"up":"down"' in html
    update_header_body = html.split("function updateHeaderMarket(market){", 1)[1].split(
        "\nasync function refreshDailyMarket", 1
    )[0]
    assert ".at(-2)" not in update_header_body
    assert "function marketTodayChangePct(market,dailyMarket=state.dailyMarket)" in html
    assert "timeframe=1d&limit=2" in html
    assert "交易所当日开盘价至今" in html
    assert 'class="pill run-state" id="marketBadge"' in html
    assert "gridmind-run-pulse" in html
    assert "prefers-reduced-motion:reduce" in html
    assert "healthButton" not in html
    assert ">自检<" not in html


def test_gridmind_notifies_only_new_authoritative_trade_activity() -> None:
    html = _html()

    assert 'id="tradeToasts" aria-live="polite"' in html
    assert "function observeTradeActivity(data)" in html
    assert "function fillStableKey(fill)" in html
    assert "fill?.source_fill_id||fill?.fill_id||fill?.order_id" in html
    assert "if(!tracker.initialized)" in html
    assert "tracker.seenFillKeys.has(key)" in html
    assert "tracker.notifiedCloseTradeKeys.has" in html
    assert "tradeRemainingByKey:new Map()" in html
    assert "currentRemaining<previousRemaining-1e-9" in html
    assert 'new Set(["exit","stop","target","take_profit","stop_loss","flatten"])' in html
    assert "function closeFillState(fill,trade)" in html
    assert "model?.closesTrade" in html
    assert "减仓成交" in html
    assert "notificationTradeRows(execution)" in html
    assert "function notificationTradeRows(execution)" in html
    assert "finalTradeKeys=new Set()" in html
    assert 'previous==="open"||finalTradeKeys.has(key)' in html
    assert "source_order_id||last?.order_id" in html
    assert "function showTradeToast(model)" in html
    assert 'model.outcome||""' in html
    assert 'playTradeChime(model.outcome==="loss"?"stop":model.outcome==="profit"?"target":model.kind)' in html
    assert "function playTradeChime(kind)" in html
    assert "window.AudioContext||window.webkitAudioContext" in html
    assert 'document.addEventListener("pointerdown",armTradeAudio' in html
    load_body = html.split("async function load({withMarket=true}={}){", 1)[1].split(
        "function pollDashboard", 1
    )[0]
    render_body = html.split("function render(){", 1)[1].split(
        "function renderRobotControls", 1
    )[0]
    assert "observeTradeActivity(data)" in load_body
    assert "observeTradeActivity" not in render_body


def test_gridmind_runtime_badge_uses_actual_state_and_authoritative_positions() -> None:
    html = _html()

    assert "function runtimeExecutionState(runtime={},execution={})" in html
    assert 'running=actual==="running"' in html
    assert 'hasAuthoritativePositions=Array.isArray(execution.positions)' in html
    assert 'orphaned=runtime.desired_state==="stopped"&&(accepted.length>0||openRows.length>0)' in html
    assert "accepted===0&&openCount===0" in html
    assert "action==='stop'&&actual==='stopped'&&accepted===0&&openCount===0" in html
    assert "renderRobotControls(data,headerFresh,execution)" in html
    assert "rolloverDisplayState(runtime,rollover,accepted.length,openCount)" in html
    assert "运行异常：无挂单且无持仓" in html


def test_gridmind_missing_financial_values_are_not_rendered_as_zero() -> None:
    html = _html()

    assert 'const num=v=>{const x=finiteNumber(v);return x===null?"--"' in html
    assert 'const signed=v=>{const x=finiteNumber(v);return x===null?"--"' in html
    assert 'function quantityText(value){const number=finiteNumber(value);return number===null?"--"' in html
    assert 'exit=finiteNumber(trade.exit_price),tp=finiteNumber(trade.tp),sl=finiteNumber(trade.sl)' in html
    assert 'near=(a,b)=>a!==null&&b!==null' in html
    assert "total=realizedTotal!==null&&unrealizedTotal!==null?realizedTotal+unrealizedTotal:null" in html
    assert "basePnl=finiteNumber(baseline?.metrics?.net_pnl)" in html
    assert "actualPnl=actualRealized!==null&&actualUnrealized!==null?actualRealized+actualUnrealized:null" in html


def test_gridmind_loads_older_trusted_bars_when_the_chart_reaches_the_left_edge() -> None:
    html = _html()

    assert 'standard-kline:viewchange' in html
    assert 'async function loadOlderMarketBars()' in html
    assert 'exclusiveEnd=Number.isNaN(oldestDate.getTime())?oldest:new Date(oldestDate.getTime()-1).toISOString()' in html
    assert 'end=${encodeURIComponent(exclusiveEnd)}' in html
    assert 'trusted_history!==true' in html
    assert 'mergeMarketBars' in html
    assert 'currentVisible=state.chart?.getVisibleLogicalRange?.()' in html
    assert 'restoreVisibleLogicalRange?.(currentVisible,added)' in html
    assert 'state.historyExhausted=page.pagination?.has_more===false' in html


def test_gridmind_isolates_control_market_and_history_notices() -> None:
    html = _html()

    assert 'id="actionStatus"' in html
    assert 'id="marketStatus"' in html
    assert 'id="historyStatus"' in html
    assert 'NOTICE_TARGETS={control:"#actionStatus",market:"#marketStatus",history:"#historyStatus"}' in html
    assert 'function setMarketNotice(message)' in html
    assert 'function setHistoryNotice(message)' in html
    assert 'renderNotices(controlFallback)' in html


def test_gridmind_history_is_generation_safe_and_requires_a_real_right_drag() -> None:
    html = _html()

    assert 'if(state.historyRequest)return state.historyRequest.promise' in html
    assert 'state.marketGeneration!==generation' in html
    assert 'state.timeframe!==timeframe' in html
    assert 'const latest=state.market' in html
    assert 'mergeMarketBars(page,latest)' in html
    assert 'draggedRight=drag.lastX-drag.startX>=12' in html
    assert 'revealedOlder=Number(range.from)<Number(drag.startRange.from)-.25' in html
    assert 'state.chartProgrammatic' in html
    assert 'historyUserIntent' not in html
    assert 'addEventListener("wheel"' not in html
    assert html.count('state.historyExhausted=false') == 1


def test_gridmind_poll_and_market_refresh_ignore_stale_responses() -> None:
    html = _html()

    assert 'if(state.pollPromise)return state.pollPromise' in html
    assert 'if(sequence!==state.loadSequence)return null' in html
    assert 'requestSequence===state.marketRequestSequence' in html
    assert 'accessIssues.length?accessIssues:[issue]' in html
    assert 'failure.access_issues=Array.isArray(body.access_issues)' in html


def test_gridmind_chart_adjustment_is_an_explicit_draft_mode() -> None:
    html = _html()
    bounds_body = html.split("function calculateGridDragBounds(drag,price){", 1)[
        1
    ].split("function moveGridDrag", 1)[0]
    drag_body = html.split("function moveGridDrag(event){", 1)[1].split(
        "function finishGridDrag", 1
    )[0]

    assert 'id="gridAdjustToggle"' in html
    assert 'aria-pressed="false"' in html
    assert 'data-grid-drag="upper"' in html
    assert 'data-grid-drag="move"' in html
    assert 'data-grid-drag="lower"' in html
    assert 'data-grid-action="confirm"' in html
    assert 'data-grid-action="cancel"' in html
    assert 'id="confirmGridDraft"' not in html
    assert 'id="cancelGridDraft"' not in html
    assert ".grid-hit.interior{cursor:grab" in html
    assert ".grid-hit.upper,.grid-hit.lower{height:16px;cursor:ns-resize}" in html
    assert "grid-middle" not in html
    assert "state.chart?.priceToY?." in html
    assert "state.chart?.yToPrice?." in html
    assert 'state.gridDraft={...state.gridDraft,low,high,dirty:true' in drag_body
    assert "high=Math.max(drag.startHigh+delta,low+minGap)" in bounds_body
    assert "low=Math.max(.0001,Math.min(drag.startLow+delta,high-minGap))" in bounds_body
    assert "high=Math.max(price,low+minGap)" not in bounds_body
    assert "low=Math.max(.0001,Math.min(price,high-minGap))" not in bounds_body
    assert "control(" not in drag_body
    assert "requestPreview(" not in drag_body
    assert 'actions.classList.toggle("hidden",!draft.dirty)' in html
    assert 'if(state.gridAdjustMode||event.isPrimary===false' in html
    assert 'if(state.gridAdjustMode||event.detail?.reason' in html
    assert 'state.gridAdjustMode?null:state.preview' in html
    assert "(!boundary&&(y<0||y>height))" in html
    assert 'boundary?Math.max(0,Math.min(height,y)):y' in html
    assert 'if(commit)moveGridDrag(event)' in html
    assert 'pointerup",event=>finishGridDrag(event,{commit:true})' in html
    assert 'pointercancel",event=>finishGridDrag(event)' in html
    assert "resetRisk=state.gridDraft.riskRecalculated===true" in drag_body
    assert "riskRecalculated:false" in drag_body


def test_gridmind_replacement_preview_freezes_production_parameters() -> None:
    html = _html()
    payload_body = html.split("function gridDraftPreviewPayload(){", 1)[1].split(
        "function latestGridMarketPrice", 1
    )[0]

    assert "count:original.grid.count" in payload_body
    assert "mode:original.grid.mode" in payload_body
    assert "notional_per_grid:draft.notionalPerGrid" in payload_body
    assert 'notional_mode:"manual"' in payload_body
    assert "direction:original.direction" in payload_body
    assert "style:original.style" in payload_body
    assert "leverage:original.grid.leverage" in payload_body
    assert "$(" not in payload_body
    assert 'control("preview",payload,{reload:false})' in html
    assert 'setNotice("新网格规格已核对，尚未修改生产网格。")' in html
    assert 'if(openDialog&&!$("#gridReplaceDialog").open)$("#gridReplaceDialog").showModal()' in html


def test_gridmind_replacement_card_shows_full_delta_and_fail_closed_gates() -> None:
    html = _html()

    for label in (
        "上下边界",
        "策略方向",
        "策略风格",
        "Range 宽度",
        "网格模式",
        "网格数量",
        "间距 / 比例",
        "每格名义",
        "配置杠杆",
        "冲出区间",
        "最坏单边总名义",
        "预计保证金",
        "实际杠杆",
        "最大风险",
        "撤单 / 新单",
        "持仓处理",
        "TP / SL",
    ):
        assert f'["{label}"' in html

    assert "停止+平仓+撤单+交易新网格" in html
    assert 'control("replace_grid",{preview,expected_strategy_plan_id:draft.expectedPlanId,expected_preview_id:preview.preview_id,risk_recalculated:draft.riskRecalculated===true,expected_execution:{accepted_order_ids:draft.original.acceptedOrderIds,open_position_ids:draft.original.openPositionIds}}' in html
    assert "riskRecalculated:Math.abs(cap-state.gridDraft.original.grid.notionalPerGrid)>.000001" in html
    assert 'reconcileControlOutcome("replace_grid",error,{expectedPreviewId:preview.preview_id})' in html
    assert "runtime.last_action==='replace_grid'" in html
    assert "runtime.preview_id===expectedPreviewId" in html
    assert "actual==='running'" in html
    assert "accepted>0" in html
    assert "连接中断后已核对：新网格已运行" in html
    assert "preview.risk?.risk_budget_exceeded!==false" in html
    assert "!isFresh(market)" in html
    assert "price<=low||price>=high" in html
    assert "currentPlanId!==draft.expectedPlanId" in html
    assert 'reasons.push("上次提交失败，须重新检查")' in html
    assert "safe_notional_cap_per_grid" in html
    assert "risk_notional_cap_per_grid" not in html
    assert 'notionalPerGrid:cap' in html
    assert "旧持仓随平仓结束；新网格按逐格 TP/SL 重建" in html
    assert "function gridPreviewContractIssue(preview,draft=state.gridDraft)" in html
    assert 'typeof risk.risk_budget_exceeded!=="boolean"' in html
    assert 'value!==null&&value!==""&&Number.isFinite(Number(value))' in html
    assert "preview.orders.some" in html
    assert "gridPreflightMarket=latest.market&&typeof latest.market" in html
    assert "state.loadSequence+=1;state.marketRequestSequence+=1" in html
    assert "isRecentGridMarket(market)" in html
    assert "tpSlPolicyText(original.tpSl)" in html
    assert "function executionIdentity(execution)" in html
    assert "sameStringSet(liveIdentity.acceptedOrderIds,draft?.original?.acceptedOrderIds)" in html
    assert "sameStringSet(liveIdentity.openPositionIds,draft?.original?.openPositionIds)" in html
    assert "执行状态已变化，请重新进入调整模式核对" in html
    assert "state.busy||state.gridAdjustMode" in html
    assert '$("#refreshTrend").disabled=!controlled||!headerFresh||state.busy||state.gridAdjustMode' in html
    assert "ACCESS_CONTROLLED()&&!state.busy&&!state.gridAdjustMode" in html


def test_gridmind_runtime_range_change_only_extends_edge_grids() -> None:
    html = _html()
    handler = html.split('$("#applyAdjustment").addEventListener', 1)[1].split(
        '$("#resetStats").addEventListener', 1
    )[0]

    assert "保持单格间距与每格名义，只增减边缘格；保留持仓/TP/SL" in html
    assert 'control("extend_range",{range:{low,high},expected_strategy_plan_id:expectedStrategyPlanId}' in handler
    assert "strategyPayload" not in handler
    assert "requestPreview" not in handler
    assert '$("#rangeLow").value' not in handler
    assert '$("#rangeHigh").value' not in handler
    assert "state.preview=null;state.formDirty=false" in handler


def test_gridmind_nav_history_keeps_meaningful_precision() -> None:
    html = _html()

    assert "const navText=" in html
    assert "minimumFractionDigits:6,maximumFractionDigits:6" in html
    assert "${navText(row.nav)}" in html


def test_gridmind_retains_last_trusted_bars_but_blocks_controls_on_refresh_failure() -> None:
    html = _html()

    assert "function retainLastTrustedMarket(existing,failure)" in html
    assert "function acceptMarketSnapshot(incoming)" in html
    assert 'status:"error",fresh:false,retained_last_trusted:true' in html
    assert 'state.market=retainLastTrustedMarket(state.market' in html
    assert "已保留最后可信 K 线，仅供查看" in html
    assert 'function isFresh(m)' in html


def test_v5_route_serves_gridmind_without_removing_legacy_console() -> None:
    server = (ROOT / "pipelines" / "dashboard_server.py").read_text(encoding="utf-8")

    assert 'if parsed.path == "/dashboard-v5.html":' in server
    assert 'self._serve_static_alias("/dashboard-gridmind.html")' in server
    assert (ROOT / "dashboard-dualtrack-split.html").exists()
