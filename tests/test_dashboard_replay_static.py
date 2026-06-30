from __future__ import annotations

from pathlib import Path


def test_dashboard_replay_static_has_multi_timeframe_contract():
    html = Path("dashboard-replay.html").read_text(encoding="utf-8")

    assert "data/vendor/lightweight-charts.standalone.production.js" in html
    assert "data/vendor/echarts.min.js" not in html
    assert "/api/replay" in html
    assert "const REQUEST_TIMEOUT_MS = 12000" in html
    assert "async function fetchWithTimeout" in html
    assert 'timeout.code = "replay_timeout"' in html
    assert "Replay API 超时" in html
    assert "不要把这当成策略没有信号" in html
    assert "window.__lastReplayError" in html
    assert "fetchWithTimeout(`${API_BASE}/api/replay?${query.toString()}`" in html
    assert 'const timeframes = ["1d","4h","15m","5m","1m"]' in html
    assert "no_future_bars" in html
    assert "Entry" in html
    assert "TP" in html
    assert "SL" in html
    assert "上一根" in html
    assert "下一根" in html
    assert 'id="selectCandleBtn"' in html
    assert 'id="transportPrevBtn"' in html
    assert 'id="transportPlayBtn"' in html
    assert 'id="transportNextBtn"' in html
    assert 'id="boundaryStatus"' in html
    assert 'id="replayBoundaryStrip"' in html
    assert 'id="pmEvidenceTopStrip"' in html
    assert 'id="strategyReplayBrief"' in html
    assert 'id="pmEvidenceDrilldown"' in html
    assert 'id="pmEvidenceFocus"' in html
    assert 'id="decisionGateTopStrip"' in html
    assert 'id="replayHoverPreview"' in html
    assert 'id="hoverBadge"' in html
    assert 'id="workflowRail"' in html
    assert 'id="chartDecisionLens"' in html
    assert 'id="chartDecisionLensToggle"' in html
    assert 'id="snapshotSummary"' in html
    assert 'data-workflow-target="chartGrid"' in html
    assert 'data-workflow-target="timeframeLocator"' in html
    assert 'data-workflow-target="decisionPipeline"' in html
    assert 'data-workflow-target="decisionCard"' in html
    assert 'data-workflow-target="pmAttributionWorksheet"' in html
    assert 'data-workflow-fallback="detailsPanel"' in html
    assert 'id="detailsPanel"' in html
    assert "选 K 线" in html
    assert "多周期位置" in html
    assert "Go / No-Go" in html
    assert "Entry / TP / SL" in html
    assert "复盘归因" in html
    assert "function scrollToWorkflowTarget" in html
    assert "function workflowTargetElement" in html
    assert "window.__lastWorkflowJump" in html
    assert ".boundary-status.locked" in html
    assert ".boundary-status.selecting" in html
    assert ".replay-boundary-strip" in html
    assert ".boundary-sync-grid" in html
    assert ".boundary-sync-chip" in html
    assert ".cadence-grid" in html
    assert ".cadence-chip.advanced" in html
    assert ".cadence-chip.held" in html
    assert ".pm-evidence-top" in html
    assert ".pm-evidence-top.active" in html
    assert ".strategy-replay-brief" in html
    assert ".strategy-replay-brief.active" in html
    assert ".strategy-brief-card" in html
    assert ".strategy-brief-actions" in html
    assert ".pm-evidence-drilldown" in html
    assert ".pm-evidence-drilldown.active" in html
    assert ".pm-drill-card" in html
    assert ".pm-drill-action" in html
    assert ".pm-evidence-focus" in html
    assert ".pm-evidence-focus.active" in html
    assert ".pm-focus-card" in html
    assert ".pm-focus-raw" in html
    assert ".pm-evidence-strip.mini" in html
    assert ".decision-gate-top" in html
    assert ".decision-gate-top.active" in html
    assert ".decision-gate-mini-grid" in html
    assert ".decision-gate-mini" in html
    assert ".replay-hover-preview" in html
    assert ".replay-hover-preview.active" in html
    assert ".ops-diagnostic-intro" in html
    assert ".ops-diagnostic-group" in html
    assert ".ops-diagnostic-body" in html
    assert "只在排查时打开" in html
    assert "回放同步和边界" in html
    assert "复盘可信度" in html
    assert "记录完整性和门控" not in html
    assert "事件导航和生命周期" in html
    assert "body.replay-boundary-locked-flash" in html
    assert ".chart-card.hover-synced" in html
    assert ".chart-card.hover-source" in html
    assert ".workflow-rail" in html
    assert ".workflow-step" in html
    assert ".chart-lens" in html
    assert ".chart-lens-grid" in html
    assert ".chart-lens-toggle" in html
    assert ".chart-lens-toggle.collapsed" in html
    assert '.chart-card[data-timeframe="1m"]{grid-template-rows:auto minmax(0,1fr) auto auto}' in html
    assert '.chart-card[data-timeframe="1m"].decision-lens-open{height:clamp(560px,72vh,760px)}' in html
    assert '.chart-grid.layout-tradingview .chart-card[data-timeframe="1m"].decision-lens-open' in html
    assert "min-height:clamp(600px, calc(100vh - 360px), 820px)" in html
    assert ".chart-lens{\n  position:relative" in html
    assert ".chart-lens-toggle{\n  position:relative" in html
    assert "min-width:58px" in html
    assert "body.layout-trader-focus .no-trade-takeaway{\n  display:grid;\n  grid-template-columns:96px 156px minmax(0,1fr);" in html
    assert "padding-right:148px;" in html
    assert "body.layout-trader-focus .chart-lens-toggle.collapsed{\n  display:none;\n}" in html
    assert 'title="显示本根决策详情">决策</button>' in html
    assert 'toggle.textContent = effectiveCollapsed ? status : "收起";' in html
    assert "NO-GO · 显示决策" not in html
    assert "bottom:112px" not in html
    assert "bottom:var(--decision-dock-offset, 0px)" in html
    assert 'card.style.setProperty("--decision-dock-offset"' in html
    assert 'card.classList.toggle("decision-lens-open"' in html
    assert ".chart-lens-dismiss" in html
    assert ".chart-lens-status" in html
    assert ".chart-lens-open-detail" in html
    assert 'data-open-trade-evidence="1"' in html
    assert 'document.getElementById("replayTradeEvidenceDrawer")' in html
    assert "病历/归因" in html
    assert "function decisionChartMarkers" in html
    assert "历史GO" in html
    assert "历史拦截" in html
    assert "历史方向信号" in html
    assert '"SIG"' in html
    assert "decision_timestamp" in html
    assert 'id="decisionMarkerControls"' in html
    assert 'data-decision-marker-mode="candidates"' in html
    assert 'data-decision-marker-mode="actionable"' in html
    assert 'data-decision-marker-mode="hidden"' in html
    assert "goldbot.replay.decisionMarkerMode" in html
    assert "function setDecisionMarkerMode" in html
    assert "function updateDecisionMarkerControls" in html
    assert "function decisionMarkerAllowed" in html
    assert "候选：显示 SIG/GO/BLK" in html
    assert "只显示 GO 和 BLK" in html
    assert 'setCursor(mark.decision_timestamp, "decision_marker", "1m")' in html
    assert "已打开图上历史决策点" in html
    assert "你点击的是主图上的 SIG / GO / BLK 标记" in html
    assert "window.__lastDecisionChartMarkers" in html
    assert "decisionMarkerMode: () => decisionMarkerMode" in html
    assert "setDecisionMarkerMode" in html
    assert "chartDecisionLensCollapsed" in html
    assert "goldbot.replay.decisionLensCollapsed" in html
    assert "const savedChartDecisionLensPreference = localStorage.getItem(CHART_DECISION_LENS_PREF_KEY)" in html
    assert 'let chartDecisionLensCollapsed = savedChartDecisionLensPreference == null ? true : savedChartDecisionLensPreference === "1"' in html
    assert "function setChartDecisionLensCollapsed" in html
    assert "function toggleChartDecisionLens" in html
    assert "function shouldAutoCompactDecisionLens" in html
    assert 'pmReviewContext?.active || category.key === "implicit_no_go"' in html
    assert "category.key === \"implicit_no_go\"" in html
    assert "autoCompacted" in html
    assert 'data-collapse-decision-lens="1"' in html
    assert 'document.getElementById("chartDecisionLensToggle").onclick = () => toggleChartDecisionLens()' in html
    assert ".evidence-summary" in html
    assert ".raw-evidence-disclosure" in html
    assert ".raw-evidence-disclosure:not([open]) > :not(summary)" in html
    assert "function renderChartDecisionLens" in html
    assert "const planValues = {" in html
    assert "const chartLensTitle = category.title || triage.headline || \"--\"" in html
    assert "<b>${esc(chartLensTitle)}</b>" in html
    assert "<span>Entry</span><b>${esc(planValues.entry)}</b>" in html
    assert "<span>TP</span><b>${esc(planValues.take_profit)}</b>" in html
    assert "<span>SL</span><b>${esc(planValues.stop_loss)}</b>" in html
    assert "planValues, hasPlan" in html
    assert ".chart-lens-grid{display:grid;grid-template-columns:1fr .9fr repeat(3,minmax(0,.78fr));gap:6px}" in html
    assert ".chart-lens-metric.missing" in html
    assert "function renderSnapshotSummary" in html
    assert "window.__lastChartDecisionLens" in html
    assert "window.__lastSnapshotSummary" in html
    assert "已锁定 · 点任意 K 线可重新定位" in html
    assert "鼠标定位 --" in html
    assert "选择回放起点" in html
    assert "点击任意 K 线移动回放起点" in html
    assert "点任意 K 线可重新定位" in html
    assert "点击任意 K 线，选择新的 replay 起点" in html
    assert "function renderReplayBoundaryStrip" in html
    assert "回放边界" in html
    assert "周期收线同步" in html
    assert "下一根 1m" in html
    assert "下一步到 ${shortTime(nextIso)}" in html
    assert "下一根/播放每次只推进 1m" in html
    assert "主时间轴：1m。点任意 K 线可重新定位；5m/15m/4H/1D 只在各自收线后推进。" in html
    assert "Replay boundary" not in html
    assert "multi-timeframe sync" not in html
    assert "next step" not in html
    assert "function renderPmEvidenceTopStrip" in html
    assert "window.__lastPmEvidenceTopStrip" in html
    assert "复盘可信度" in html
    assert "function strategyReplayBriefModel" in html
    assert "function renderStrategyReplayBrief" in html
    assert "window.__lastStrategyReplayBrief" in html
    assert "data-strategy-brief-action" in html
    assert "窗口样本" in html
    assert "复盘入口" in html
    assert "当前策略没有可跳转候选" in html
    assert "strategyReplayBrief: () => window.__lastStrategyReplayBrief || null" in html
    assert "function pmEvidenceDrilldownRows" in html
    assert "function renderPmEvidenceDrilldown" in html
    assert "window.__lastPmEvidenceDrilldown" in html
    assert "data-pm-drill-action" in html
    assert "复盘可信度检查" in html
    assert "按病历、账本、样本、频率和安全状态判断这次复盘能不能信" in html
    assert "PM 证据检查台" not in html
    assert "Replay 窗口证据" not in html
    assert "pmEvidenceDrilldown: () => window.__lastPmEvidenceDrilldown || null" in html
    assert "let selectedPmEvidenceKey" in html
    assert "function pmEvidenceFocusModel" in html
    assert "function renderPmEvidenceFocus" in html
    assert "window.__lastPmEvidenceFocus" in html
    assert "pmEvidenceFocus: () => window.__lastPmEvidenceFocus || null" in html
    assert "复盘记录详情" in html
    assert "PM 证据详情" not in html
    assert "验收问题" in html
    assert "当前记录" in html
    assert "function renderDecisionGateTopStrip" in html
    assert "window.__lastDecisionGateTopStrip" in html
    assert 'data-decision-gate-mini="${esc(step.key)}"' in html
    assert 'aria-label="Go No-Go decision gates"' in html
    assert "function renderReplayHoverPreview" in html
    assert "function flashReplayBoundaryLock" in html
    assert "window.__lastReplayBoundaryStrip" in html
    assert "window.__lastReplayHoverPreview" in html
    assert "window.__lastReplayBoundaryLockFlash" in html
    assert "Next/Play 每次只推进 1m" not in html
    assert "5m 需 5 步，15m 需 15 步才换一根" in html
    assert "点击设为回放起点" in html
    assert "点击 K 线选择回放起点" in html
    assert "选择回放起点" in html
    assert "function setSelectCandleMode" in html
    assert "let selectCandleMode = true" in html
    assert 'document.getElementById("transportPrevBtn").onclick = () => shift(-1)' in html
    assert 'document.getElementById("transportNextBtn").onclick = () => shift(1)' in html
    assert 'document.getElementById("transportPlayBtn").onclick = () => togglePlay()' in html
    assert "selectCandleMode: () => selectCandleMode" in html
    assert "if(cursor){" in html
    assert 'document.getElementById("selectCandleBtn").onclick = () => setSelectCandleMode(true)' in html
    assert 'id="transportBack15Btn"' in html
    assert 'id="transportForward15Btn"' in html
    assert 'id="jumpLatestGoBtn"' in html
    assert 'id="jumpLatestTradeBtn"' in html
    assert "最近信号" in html
    assert "跳到最近信号或候选" in html
    assert "最近候选" in html
    assert "最近拦截" in html
    assert "function explicitDecisionCandidates" in html
    assert "function latestActionableDecision" in html
    assert "const scopedNearbyRows" in html
    assert "const focalRows" in html
    assert "jump_latest_candidate" in html
    assert "最近成交" in html
    assert "function latestGoDecision" in html
    assert "function latestTradeCursor" in html
    assert "function entryDecisionForFocusedTrade" in html
    assert "function tradeAndPlanPriceMatch" in html
    assert "function jumpToLatestGo" in html
    assert "function jumpToLatestTrade" in html
    assert "function updateEvidenceJumpButtons" in html
    assert "state?.latest_go_decision_snapshot" in html
    assert "state?.focused_trade_entry_decision_snapshot" in html
    assert 'linked_by:"focused_trade_entry_decision_snapshot"' in html
    assert "data-entry-decision-jump" in html
    assert "入场决策在" in html
    assert "成交已定位 · 入场决策可打开" in html
    assert "成交 + GO 已关联" in html
    assert "latestActionableDecision()" in html
    assert '"jump_latest_go" : "jump_latest_candidate"' in html
    assert 'setCursor(ts, String(row?.final_decision || "").toLowerCase() === "go"' in html
    assert 'setCursor(ts, "jump_latest_trade", "1m"' in html
    assert "function chartClickAlreadySelected" in html
    assert "selectionContext?.source === \"chart_click\"" in html
    assert "function replayCursorForSelectedRow" in html
    assert "const selectedCursor = replayCursorForSelectedRow(tf, row)" in html
    assert 'setCursor(selectedCursor, "chart_click", tf)' in html
    assert "syncPointer(tf, row.timestamp)" in html
    assert "function hoverStatusText" in html
    assert "function updateHoverBadge" in html
    assert "function updateHoverChartClasses" in html
    assert "card.classList.toggle(\"hover-synced\"" in html
    assert "card.classList.toggle(\"hover-source\"" in html
    assert "LightweightCharts.createChart" in html
    assert "lwc.CandlestickSeries" in html
    assert "LightweightCharts.createSeriesMarkers" in html
    assert "attributionLogo:false" in html
    assert "item.chart.subscribeClick(param =>" in html
    assert "item.chart.subscribeCrosshairMove(param =>" in html
    assert "chart.setCrosshairPosition" in html
    assert "clearCrosshairPosition" in html
    assert "if(chartClickAlreadySelected({timestamp:selectedCursor})) return" in html
    assert 'window.__lastReplayClick = {tf, source:"lightweight_charts"' in html
    assert 'if(selectedCursor) setCursor(selectedCursor, "chart_click", tf)' in html
    assert 'setCursor(hit.row.timestamp, "chart_click", tf)' not in html
    assert "syncPointer(tf, row.timestamp)" in html
    assert "回放边界" in html
    assert "回放位置" in html
    assert 'id="geometryBadge"' in html
    assert 'id="historyReplayBadge"' in html
    assert ".history-replay-badge" in html
    assert "function historyReplayState" in html
    assert "function updateHistoryReplayBadge" in html
    assert "window.__lastHistoryReplayBadge" in html
    assert "历史回放 · 报告日" in html
    assert "function replayGeometryText" in html
    assert "function updateGeometryBadge" in html
    assert "function isoFromLwcTime" in html
    assert "function lwcCandleData" in html
    assert "function lwcLineData" in html
    assert "function traderTimeLabel" in html
    assert "const cardTime = traderTimeLabel(snap.bar_timestamp || state?.cursor)" in html
    assert "<small>${esc(cardTime)}</small>" in html
    assert "回放选中 · ${traderTimeLabel(iso)}" in html
    assert "${sourceLabel} · 决策时间 ${traderTimeLabel(timestamp)}" in html
    assert '"insufficient_sample":"样本不足"' in html
    assert '"legacy market_view":"旧版口述观点"' in html
    assert '"no market view available":"没有可用口述观点"' in html
    assert "缩放可调" in html
    assert "K线位置" not in html
    assert "function replayFocusMark" in html
    assert "function cursorPlacement" in html
    assert "right_blank_count" in html
    assert "right_blank_screen_fraction" in html
    assert "cursor_ratio" in html
    assert "鼠标定位" in html
    assert "lastHover" in html
    assert "applyHoverLines" in html
    assert "show:!overlapsReplay" not in html
    assert "鼠标所在K线" not in html
    assert "包含 ${shortTime(hoverIso)} 的K线" not in html
    assert "function syncLightweightCrosshairs" in html
    assert "function clearLightweightCrosshairs" in html
    assert "function clearHoverLines(){\n  clearHoverState();" in html
    assert "row_timestamp:row?.timestamp || \"\"" in html
    assert "row_close:row?.close ?? null" in html
    assert 'id="timeframeLocator"' in html
    assert ".timeframe-locator" in html
    assert "多周期定位" in html
    assert "高周期图只画已收线 K 线，同时标出当前分钟所属的形成中周期" in html
    assert "function locatorRowsForIso" in html
    assert "function multiTimeframeSyncSummary" in html
    assert "function closedChartRowForIso" in html
    assert "function replayCadenceRows" in html
    assert "function replayCadencePreviousIso" in html
    assert "timeframeRangeEnd(tf, row)" in html
    assert "end <= cursorMs" in html
    assert "cadence_previous_iso" in html
    assert "data-cadence-tf" in html
    assert "本次推进节拍" in html
    assert "多周期同步：" in html
    assert "function timeframeBucketForIso" in html
    assert "function formationProgressText" in html
    assert "formation_range" in html
    assert "formation_status" in html
    assert "formation_matches_visible" in html
    assert "当前分钟属于" in html
    assert "尚未收线，图上不画" in html
    assert "高周期图只画已收线 K 线，同时标出当前分钟所属的形成中周期" in html
    assert '["5m","15m","4h","1d"]' in html
    assert "multiTimeframeSyncSummary(timestamp)" in html
    assert "function locatorContainmentCopy" in html
    assert "最近已收线" in html
    assert "function renderTimeframeLocator" in html
    assert "function timeframeProgressText" in html
    assert "function progressDurationLabel" in html
    assert "progressDurationLabel(elapsedMinutes)" in html
    assert "progressDurationLabel(totalMinutes)" in html
    assert "1/1 天" not in html
    assert "1m 主时钟 · 每根收线一次决策" in html
    assert "未满周期前保持同一根K线" in html
    assert "progress: timeframeProgressText(tf, row, iso)" in html
    assert "cursorContainmentLabelForIso" in html
    assert 'data-locator-tf="${esc(row.timeframe)}"' in html
    assert "renderTimeframeLocator();" in html
    assert "timeframeLocator: () => window.__lastTimeframeLocator" in html
    assert "function ensureReplayCursorVisible" in html
    assert "cursor_auto_keep_visible" in html
    assert "后续 K 线已隐藏" in html
    assert "口述过期" in html
    assert "function marketViewGateEffect" in html
    assert "function effectiveMarketViewExpiry" in html
    assert "function marketViewPanelHtml" in html
    assert "function marketViewDirectionPolicy" in html
    assert "window.__lastMarketFilterPanel" in html
    assert "不过滤新信号；仅做复盘记录" in html
    assert "时间过期" in html
    assert "价格过期" in html
    assert "目标位未设置" in html
    assert "const targetText" in html
    assert "允许方向" in html
    assert "const expiry = effectiveMarketViewExpiry(bias, market)" in html
    assert "过滤器已解除" in html
    assert "过滤器生效中" in html
    assert "中性 / 不过滤" in html
    assert "过滤效果" in html
    assert "系统不再按这条观点过滤多空信号" in html
    assert "chart_click" in html
    assert "EMA20" in html
    assert "EMA50" in html
    assert "EMA100" in html
    assert "EMA200" in html
    assert "EMA20/50/100/200" in html
    assert "MACD Hist" in html
    assert "RSI" in html
    assert "RSI / bars" in html
    assert "ATR / bars" not in html
    assert "TRADING_UNIT_SPEC" in html
    assert 'candle_style:"hollow_up_solid_down"' in html
    assert 'visible_future_space:"one_third_blank_right_zone"' in html
    assert 'const VISIBLE_BARS_BY_TIMEFRAME = {"1m":Infinity,"5m":Infinity,"15m":Infinity,"4h":Infinity,"1d":Infinity}' in html
    assert "if(!Number.isFinite(Number(limit)) || Number(limit) <= 0) return safeBars;" in html
    assert 'const DEFAULT_ZOOM_BARS_BY_TIMEFRAME = {"1m":120,"5m":120,"15m":96,"4h":96,"1d":120}' in html
    assert "const RIGHT_BLANK_SCREEN_FRACTION = 1 / 3" in html
    assert "const RIGHT_BLANK_RATIO = RIGHT_BLANK_SCREEN_FRACTION / (1 - RIGHT_BLANK_SCREEN_FRACTION)" in html
    assert 'data-timeframe="1m"' in html
    assert 'data-timeframe="5m"' in html
    assert 'id="chartGrid"' in html
    assert 'id="chartLayoutControls"' in html
    assert 'id="indicatorControls"' in html
    assert 'id="replayChartSettingsDrawer" class="replay-settings-drawer"' in html
    assert '<summary aria-label="图表设置" title="图表设置"><span class="settings-icon" aria-hidden="true">⚙</span></summary>' in html
    assert ".settings-icon" in html
    assert ".replay-settings-drawer" in html
    assert ".replay-settings-body" in html
    assert html.index('id="replayChartSettingsDrawer"') < html.index('aria-label="交易回放控制"') < html.index('id="chartGrid"')
    assert html.index('id="replayChartSettingsDrawer"') < html.index('id="markerScopeControls"') < html.index('id="chartLayoutControls"') < html.index('id="indicatorControls"')
    assert 'data-indicator-toggle="ema"' in html
    assert 'data-indicator-toggle="macd"' in html
    assert 'data-indicator-toggle="rsi"' in html
    assert 'data-chart-layout="primary"' in html
    assert 'data-chart-layout="tradingview"' in html
    assert 'data-chart-layout="trader"' in html
    assert 'id="primaryTimeframeControls"' in html
    assert 'data-primary-timeframe="1m"' in html
    assert 'data-primary-timeframe="5m"' in html
    assert 'data-primary-timeframe="15m"' in html
    assert 'data-primary-timeframe="4h"' in html
    assert 'data-primary-timeframe="1d"' in html
    assert 'id="primaryTimeframeCopy"' in html
    assert "多周期工作台" in html
    assert "放大 1m" in html
    assert "Trader Focus" in html
    assert "多周期工作台：1D / 4H / 15m / 5m 先定方向，1m 逐根复盘。" in html
    assert "Trader Focus：${primaryTimeframeCopy(primaryChartTf)}" in html
    assert "左侧 ${timeframeLabel(primary)} 是主复盘图" in html
    assert "function setChartLayoutMode" in html
    assert "function updateChartLayoutControls" in html
    assert "function setPrimaryChartTimeframe" in html
    assert "function updatePrimaryTimeframeControls" in html
    assert "function orderedContextTimeframes" in html
    assert 'params.get("primary_tf") || params.get("primary_timeframe")' in html
    assert 'const CHART_LAYOUT_PREF_KEY = "goldbot.replay.layout"' in html
    assert 'const CHART_LAYOUT_VERSION = "trader-focus-left-main-v3"' in html
    assert 'const DEFAULT_CHART_LAYOUT = "trader"' in html
    assert 'const PRIMARY_CHART_TF_PREF_KEY = "goldbot.replay.primaryTimeframe"' in html
    assert 'const DEFAULT_PRIMARY_CHART_TF = "1m"' in html
    assert "function initialChartLayoutMode" in html
    assert "function initialPrimaryChartTimeframe" in html
    assert "localStorage.setItem(CHART_LAYOUT_VERSION_KEY, CHART_LAYOUT_VERSION)" in html
    assert "localStorage.setItem(CHART_LAYOUT_PREF_KEY, chartLayoutMode)" in html
    assert "localStorage.setItem(PRIMARY_CHART_TF_PREF_KEY, primaryChartTf)" in html
    assert 'url.searchParams.set("primary_tf", primaryChartTf)' in html
    assert ".chart-grid.layout-tradingview" in html
    assert ".chart-grid.layout-trader-focus" in html
    assert "body.layout-trader-focus{\n  min-width:1320px;" in html
    assert "body.layout-trader-focus .chart-grid" in html
    assert 'body.layout-trader-focus .chart-grid .chart-card.primary-chart' in html
    assert 'grid-template-columns:minmax(0,2.15fr) repeat(2,minmax(210px,.62fr))' in html
    assert "grid-template-rows:repeat(2,minmax(0,1fr))" in html
    assert "height:clamp(660px, calc(100vh - 350px), 820px)" in html
    assert 'grid-template-areas:\n    "primary ctx-a ctx-b"\n    "primary ctx-c ctx-d"' in html
    assert 'body.layout-trader-focus .chart-grid .chart-card.primary-chart{\n  order:-1;\n  grid-area:primary;' in html
    assert 'body.layout-trader-focus .chart-grid .chart-card.context-slot-0{grid-area:ctx-a}' in html
    assert 'body.layout-trader-focus .chart-grid .chart-card.context-slot-1{grid-area:ctx-b}' in html
    assert 'body.layout-trader-focus .chart-grid .chart-card.context-slot-2{grid-area:ctx-c}' in html
    assert 'body.layout-trader-focus .chart-grid .chart-card.context-slot-3{grid-area:ctx-d}' in html
    assert 'card.classList.toggle("primary-chart", isPrimary)' in html
    assert 'card.classList.toggle("context-chart", !isPrimary)' in html
    assert "body.layout-trader-focus .transport" in html
    assert "body.layout-trader-focus .transport{\n  position:absolute;" in html
    assert "transform:translateX(-58%);" in html
    assert "body.layout-trader-focus .transport .select-mode-btn,\nbody.layout-trader-focus .transport .transport-jump{\n  width:29px;" in html
    assert "body.layout-trader-focus .transport .select-mode-btn .transport-copy,\nbody.layout-trader-focus .transport .transport-jump .transport-copy,\nbody.layout-trader-focus .transport .transport-jump .transport-time{\n  display:none;" in html
    assert "body.layout-trader-focus .brand-sub{\n  display:none;" in html
    assert "body.layout-trader-focus .topbar" in html
    assert "body.layout-trader-focus .controls{\n  gap:6px;" in html
    assert "max-width:min(1040px,calc(100vw - 310px));" in html
    assert "flex-wrap:nowrap;" in html
    assert "overflow:hidden;" in html
    assert ".shell{width:min(1720px,100%);margin:0 auto;padding:18px 22px 36px;display:flex;flex-direction:column;position:relative}" in html
    assert "body.layout-trader-focus .replay-settings-drawer{\n  position:absolute;" in html
    assert "top:18px;" in html
    assert "left:246px;" in html
    assert "width:31px;" in html
    assert "justify-content:center;" in html
    assert "body.layout-trader-focus .replay-settings-drawer[open]{\n  width:min(620px,calc(100% - 44px));" in html
    assert "body.layout-trader-focus .replay-settings-drawer:not([open]) summary::after" in html
    assert "body.layout-trader-focus .replay-settings-drawer:not([open]) .replay-settings-body{\n  display:none;" in html
    assert "body.layout-trader-focus .replay-settings-body{\n  max-height:64vh;" in html
    assert "body.layout-trader-focus .replay-settings-drawer summary" in html
    assert "body.layout-trader-focus .replay-settings-drawer summary .settings-icon" in html
    assert "body.layout-trader-focus .marker-row" in html
    assert "body.layout-trader-focus .layout-row" in html
    assert "body.layout-trader-focus .primary-timeframe-row" in html
    assert "body.layout-trader-focus .timeline .rail" in html
    assert "body.layout-trader-focus #boundaryStatus" in html
    assert "body.layout-trader-focus #hoverBadge" in html
    assert "body.layout-trader-focus .replay-decision-dock" in html
    assert "body.layout-trader-focus .workflow-rail" in html
    assert "body.layout-trader-focus .replay-boundary-strip" in html
    assert "body.layout-trader-focus .timeframe-locator,\nbody.layout-trader-focus .workflow-rail{\n  display:none;" in html
    assert "body.layout-trader-focus .chart-card.replay-active::after" in html
    assert "display:none" in html
    assert "@media (max-width:720px)" not in html
    assert "body.layout-trader-focus .chart-grid{\n    grid-template-columns:1fr;" not in html
    assert "@media (max-width:1319px)" in html
    assert "body.layout-trader-focus .chart-grid{\n    grid-template-columns:minmax(0,2.15fr) repeat(2,minmax(210px,.62fr));" in html
    assert "grid-area:auto;" in html
    assert "body.layout-trader-focus .chart-grid .chart-card.context-chart" in html
    assert 'id="chartGrid" class="chart-grid layout-trader-focus"' in html
    assert "primaryChartTimeframe: () => primaryChartTf" in html
    assert "setPrimaryChartTimeframe" in html
    assert "contextTimeframes: () => orderedContextTimeframes(primaryChartTf)" in html
    assert 'id="replayOpsEvidenceDrawer" class="replay-secondary-drawer replay-ops-drawer"' in html
    assert 'id="replayMarketEvidenceDrawer" class="replay-secondary-drawer replay-market-drawer"' in html
    assert 'id="replayTradeEvidenceDrawer" class="replay-secondary-drawer replay-evidence-drawer"' in html
    assert 'id="replayDecisionWorkbench" class="replay-decision-workbench empty"' in html
    assert ".replay-decision-workbench" in html
    assert "function replayDecisionWorkbenchModel" in html
    assert "function renderReplayDecisionWorkbench" in html
    assert "renderReplayDecisionWorkbench(selectedTrade);" in html
    assert "window.__lastReplayDecisionWorkbench" in html
    assert 'id="replayReviewWorkspace" class="replay-review-workspace"' in html
    assert ".replay-review-workspace" in html
    assert ".replay-review-header" in html
    assert "body.layout-trader-focus .replay-review-header" in html
    assert "body.layout-trader-focus .replay-review-workspace" in html
    assert ".replay-review-workspace .replay-evidence-drawer{order:1}" in html
    assert ".replay-review-workspace .replay-market-drawer{order:2}" in html
    assert ".replay-review-workspace .replay-ops-drawer{order:3}" in html
    assert ".replay-review-workspace .replay-evidence-drawer[open]" in html
    assert "grid-template-columns:repeat(2,minmax(0,1fr))" in html
    assert "grid-template-columns:minmax(0,1.52fr) minmax(360px,.82fr)" not in html
    assert "padding-right:54px;" not in html
    assert ".replay-review-workspace .replay-secondary-drawer[open]{\n  grid-column:1 / -1;" in html
    assert "body.layout-trader-focus .replay-review-workspace .replay-evidence-drawer" in html
    assert "body.layout-trader-focus .replay-review-workspace .replay-evidence-drawer > summary{\n  display:flex;" in html
    assert "body.layout-trader-focus .replay-review-workspace .replay-evidence-drawer:not([open])" in html
    assert "body.layout-trader-focus .replay-review-workspace .replay-evidence-drawer:not([open]) > summary .drawer-title small{\n  display:none;" in html
    assert "body.layout-trader-focus .replay-review-workspace .replay-evidence-drawer:not([open]) > summary .drawer-role" in html
    assert "body.layout-trader-focus .replay-review-workspace .replay-evidence-drawer > summary::after{\n  display:inline;" in html
    assert "body.layout-trader-focus .replay-review-workspace .replay-evidence-drawer .replay-drawer-body{\n  padding:5px;" in html
    assert "body.layout-trader-focus .replay-review-workspace .replay-market-drawer" in html
    assert "body.layout-trader-focus .replay-review-workspace .replay-market-drawer{\n  display:none;\n}" in html
    assert "body.layout-trader-focus .replay-review-workspace .replay-market-drawer[open]{\n  display:block;\n  grid-column:1 / -1;" in html
    assert "body.layout-trader-focus .replay-review-workspace .replay-ops-drawer" in html
    assert "body.layout-trader-focus .replay-review-workspace .replay-ops-drawer{\n  display:none;\n}" in html
    assert "复盘焦点" in html
    assert "先看本根 K 线决策" in html
    assert "Trader Focus" in html
    assert 'id="replayTradeEvidenceDrawer" class="replay-secondary-drawer replay-evidence-drawer"' in html
    assert 'id="replayTradeEvidenceDrawer" class="replay-secondary-drawer replay-evidence-drawer" open' not in html
    assert "完整病历与归因" in html
    assert "当前 K 线决策工作台" in html
    assert "展开病历" in html
    assert "观点" in html
    assert "市场上下文" not in html
    assert "系统诊断" in html
    assert "Entry / TP / SL" in html
    assert "Entry / TP / SL · PM归因" not in html
    assert "市场方向 · 多周期" in html
    assert "同步 / 保护 / 原始链路" in html
    assert "同步 / 门控 / 原始链路" not in html
    assert "drawer-role\">主入口" not in html
    assert "drawer-role\">展开" in html
    assert "drawer-role\">辅助" in html
    assert "drawer-role\">排查" in html
    assert ".replay-secondary-drawer > summary .drawer-title" in html
    assert ".replay-secondary-drawer > summary .drawer-role" in html
    assert "当前决策在主图下方；病历是主入口，市场是辅助，系统诊断只在排查时打开。" in html
    assert ".replay-secondary-drawer > summary" in html
    assert ".replay-secondary-drawer[open] > summary::after" in html
    assert ".replay-secondary-drawer:not([open]) .replay-drawer-body{\n  display:none;" in html
    assert 'id="expandedTradeSummary" class="expanded-trade-summary" aria-label="完整病历摘要"' in html
    assert ".expanded-trade-summary" in html
    assert ".expanded-summary-card.primary" in html
    assert "function expandedTradeSummaryModel" in html
    assert "function renderExpandedTradeSummary" in html
    assert "renderExpandedTradeSummary(selectedTrade);" in html
    assert "完整病历摘要" in html
    assert "计划 / 保护单" in html
    assert "归因 / 下一步" in html
    assert "trade-review-workbench" in html
    assert "先核病历，再做归因" in html
    assert "默认只显示本根决策和选中交易" in html
    assert "当前病历 / PM 归因" not in html
    assert "Entry / TP / SL / 归因" not in html
    assert 'id="tradeCaseDetails" class="trade-case-details"' in html
    assert 'id="tradeCaseDetailsSummaryLabel"' in html
    assert "function tradeCaseDetailsSummaryLabel" in html
    assert "function updateTradeCaseDetailsSummary" in html
    assert "body.layout-trader-focus .trade-case-details:not([open])" in html
    assert "height:0;" in html
    assert "margin:-42px 9px 0 0;" in html
    assert "border-radius:999px;" in html
    assert "content:\"›\";" in html
    assert "观望原因" in html
    assert "观望原因 · ${reason}" in html
    assert 'reason = "回测样本薄"' in html
    assert "位置拦截" in html
    assert "方向过滤" in html
    assert "风控拦截" in html
    assert "质量门未过" in html
    assert "未出票原因" in html
    assert "下单依据" in html
    assert "交易原因" in html
    assert "卡在哪一关" not in html
    assert "为什么不下单" not in html
    assert "为什么被拦截" not in html
    assert "为什么可执行" not in html
    assert "为什么这笔交易" not in html
    assert "为什么这根做/不做" not in html
    assert "展开交易逻辑与过滤" not in html
    assert "展开证据" not in html
    assert "展开原始证据 / 信号横截面" not in html
    assert ".trade-case-detail-body" in html
    assert "trade-review-focus-grid" in html
    assert "trade-review-focus-main" in html
    assert "trade-review-focus-pm" in html
    assert "病历与本根决策" in html
    assert "Review</span><b>归因与下一步" in html
    assert 'aside class="trade-review-focus-pm" aria-label="归因与下一步"' in html
    assert 'id="pmAttributionWorksheet" class="pm-attribution-worksheet" aria-label="PM attribution worksheet"' in html
    assert "当前没有交易病历，不做盈亏归因" not in html
    assert 'grid?.classList.toggle("pm-collapsed", !show)' in html
    assert 'aside?.classList.toggle("is-empty", !show)' in html
    assert "signalTicketTriageModel(snap).headline" in html
    assert html.index('id="tradeCard"') < html.index('id="pmAttributionWorksheet"') < html.index('id="tradeCaseDetails"') < html.index('id="replayDecisionDock"')
    assert "trade-case-review-grid" in html
    assert "trade-case-trader-column" in html
    assert "trade-case-pm-column" in html
    assert "信号关口</span><b>哪一关放行或拦截" in html
    assert "技术过滤</span><b>指标、位置、风险" in html
    assert "交易逻辑</span><b>本根为什么做/不做" not in html
    assert "指标与过滤</span><b>信号、位置、风险" not in html
    assert ".trade-case-review-grid{\n  display:grid;" in html
    assert "grid-template-columns:minmax(0,1.18fr) minmax(320px,.82fr)" in html
    assert "body.layout-trader-focus .trade-case-review-grid" in html
    assert "grid-template-columns:minmax(0,1.26fr) minmax(300px,.74fr)" not in html
    assert ".trade-case-pm-column .trade-review-support-drawers{\n  grid-template-columns:minmax(0,1fr);" in html
    assert "trade-case-raw-diagnostic" in html
    assert ".trade-review-diagnostic.trade-case-raw-diagnostic summary" in html
    assert ".raw-evidence-disclosure.trade-case-raw-diagnostic summary" in html
    assert "body.layout-trader-focus .raw-evidence-disclosure.trade-case-raw-diagnostic,\nbody.layout-trader-focus .trade-review-diagnostic .raw-evidence-disclosure{\n  display:none;" in html
    assert "原始成交表（排查）" in html
    assert "原始指标表（排查）" in html
    assert "查看当前标记范围内交易列表" not in html
    assert "查看完整横截面表" not in html
    assert '<details class="market-decision-details" hidden aria-hidden="true">' in html
    assert '<details class="trade-review-diagnostic decision-detail-diagnostic">' in html
    assert "查看放行 / 拦截原因" in html
    assert "查看做/不做关口" not in html
    assert "展开当前 K 线决策" not in html
    assert '<details class="trade-review-diagnostic signal-filter-diagnostic">' in html
    assert "查看技术条件" in html
    assert "查看指标与过滤" not in html
    assert "展开信号与过滤" not in html
    assert ".decision-detail-diagnostic #decisionCard{margin-bottom:0}" in html
    assert "trade-review-main-stack" in html
    assert "trade-review-support-drawers" in html
    assert ".decision-detail-diagnostic .replay-decision-dock" in html
    assert "margin:0 0 8px;" in html
    assert '<details class="trade-review-diagnostic decision-dock-diagnostic">' not in html
    assert "展开本根决策摘要" not in html
    assert "body.layout-trader-focus .trade-review-focus-grid" in html
    assert "body.layout-trader-focus .trade-review-focus-grid.pm-collapsed" in html
    assert "body.layout-trader-focus .trade-review-focus-label{\n  display:none;" in html
    assert "body.layout-trader-focus .trade-review-focus-pm.is-empty" in html
    assert "body.layout-trader-focus .trade-review-focus-pm .pm-attribution-worksheet" in html
    assert "body.layout-trader-focus .trade-review-focus-pm .pm-attribution-worksheet{\n  grid-template-columns:minmax(0,1fr);" in html
    assert "margin:0;\n  align-content:start;" in html
    assert "body.layout-trader-focus .trade-review-focus-pm .pm-attribution-grid" in html
    assert "body.layout-trader-focus .trade-review-focus-pm .pm-attribution-grid{\n  grid-template-columns:repeat(2,minmax(0,1fr));" in html
    assert "body.layout-trader-focus .trade-card.empty .trade-card-disclosure" in html
    assert "body.layout-trader-focus .trade-review-head{\n  display:none;" in html
    assert "body.layout-trader-focus .trade-review-record .panel-head{\n  display:none;" in html
    assert "body.layout-trader-focus .trade-review-main-stack" in html
    assert "grid-template-columns:minmax(0,1fr)" in html
    assert "body.layout-trader-focus .replay-review-workspace" in html
    assert "grid-template-columns:repeat(3,minmax(0,1fr))" in html
    assert "border-color:rgba(255,255,255,.08)" in html
    assert "body.layout-trader-focus .trade-case-details > summary" in html
    assert "min-height:28px" in html
    assert "body.layout-trader-focus .no-trade-compact-head{\n  display:none;" in html
    assert "body.layout-trader-focus .trade-case-details" in html
    assert "grid-column:1 / -1;" in html
    assert "body.layout-trader-focus .decision-dock-cell:nth-child(4)" in html
    assert "body.layout-trader-focus .decision-dock-cell small" in html
    assert "body.layout-trader-focus .no-trade-compact-cell b" in html
    assert "-webkit-line-clamp:1;" in html
    assert "no-trade-takeaway" in html
    assert 'aria-label="本根结论摘要"' in html
    assert "body.layout-trader-focus .no-trade-compact-grid{\n  display:none;" in html
    assert "grid-template-columns:96px 156px minmax(180px,.9fr) minmax(0,1.45fr);" in html
    assert 'class="trade-record-strip no-go-record"' not in html
    assert "<span>${esc(marketStatusText)}</span>" in html
    assert "<span>${esc(executionStatusText)}</span>" in html
    assert "<span>价格</span><b>${esc(priceText)}</b>" not in html
    assert "<span>方向</span><b>${esc(directionText)}</b>" not in html
    assert "展开本根收线摘要" not in html
    assert "market-context-panel" in html
    assert "market-context-card primary" in html
    assert "market-timeframe-card" in html
    assert "市场背景" in html
    assert "market-filter-deck" in html
    assert "口述观点核心读法" in html
    assert "body.layout-trader-focus .replay-market-drawer[open] .market-copy" in html
    assert "max-height:min(52vh,560px)" in html
    assert ".market-filter-deck{\n  display:grid;\n  grid-template-columns:repeat(2,minmax(0,1fr));" in html
    assert "market-view-open-btn" in html
    assert 'data-open-market-view="1"' in html
    assert 'document.getElementById("replayMarketEvidenceDrawer")' in html
    assert "body.layout-trader-focus .replay-review-workspace .replay-market-drawer{\n  display:none;" in html
    assert "body.layout-trader-focus .replay-review-workspace .replay-ops-drawer{\n  display:none;" in html
    assert "方向判断" in html
    assert "过滤动作" in html
    assert "失效条件" in html
    assert "关键位置" in html
    assert "展开本根决策和执行计划" in html
    assert "market-filter-details" in html
    assert "展开口述观点和过期条件" in html
    assert "market-timeframe-details" in html
    assert "展开周期归属表" in html
    assert ".market-timeframe-details:not([open]) .timeframe-locator" in html
    assert ".market-timeframe-card .timeframe-locator" in html
    assert html.index("市场背景") < html.index('id="timeframeLocator"') < html.index('id="replayOpsEvidenceDrawer"')
    assert "查看技术条件" in html
    assert "展开信号与过滤" not in html
    assert "归因与下一步" in html
    assert "trade-review-diagnostic" in html
    assert "PM / OPS / 同步细节" not in html
    assert "后台同步细节" not in html
    assert html.index('id="chartGrid"') < html.index('id="replayReviewWorkspace"') < html.index('id="replayTradeEvidenceDrawer"') < html.index('class="trade-review-workbench"') < html.index('id="pmAttributionWorksheet"') < html.index('id="tradeCaseDetails"') < html.index('id="replayDecisionDock"') < html.index('id="replayMarketEvidenceDrawer"') < html.index('id="timeframeLocator"') < html.index('id="replayOpsEvidenceDrawer"')
    assert "body.layout-trader-focus .timeline" in html
    assert "body.layout-trader-focus .pm-review-brief" in html
    assert "body.layout-trader-focus .replay-secondary-drawer .workflow-rail" in html
    assert "grid-template-columns:repeat(4,minmax(0,1fr))" in html
    assert "grid-template-rows:" in html
    assert "grid-auto-rows:auto" in html
    assert "grid-column:1 / -1" in html
    assert "chartLayoutMode: () => chartLayoutMode" in html
    assert 'const INDICATOR_PREF_KEY = "goldbot.replay.indicators"' in html
    assert "indicatorPrefs: () => indicatorPrefs" in html
    assert "function setIndicatorVisible" in html
    assert "handleScroll:{mouseWheel:true,pressedMouseMove:true" in html
    assert "axisPressedMouseMove:{time:true,price:true}" in html
    assert "axisDoubleClickReset:{time:true,price:true}" in html
    assert "mouseWheel:true,\n      pinch:true" in html
    assert 'el.addEventListener("wheel", containWheel, {passive:true,capture:true})' in html
    assert "applyDesktopWheelZoom(tf, item, event)" in html
    assert "function applyDesktopWheelZoom(tf, item, event)" in html
    assert "function applyDesktopTimeAxisDragZoom(tf, item, startRange, deltaX, width)" in html
    assert "function bindDesktopTimeAxisDrag(tf, item)" in html
    assert "bindDesktopTimeAxisDrag(tf, item)" in html
    assert "function bindDesktopPriceAxisDragProbe(tf, item)" in html
    assert "bindDesktopPriceAxisDragProbe(tf, item)" in html
    assert 'source:"price_axis_drag"' in html
    assert "window.__lastPriceAxisDragZoom" in html
    assert "lastPriceAxisDragZoom: () => window.__lastPriceAxisDragZoom || null" in html
    assert '"time_axis_drag"' in html
    assert "window.__lastTimeAxisDragZoom" in html
    assert "lastTimeAxisDragZoom: () => window.__lastTimeAxisDragZoom || null" in html
    assert "function setVisibleLogicalRangeWithMemory" in html
    assert 'source:"native_wheel_pending"' in html
    assert 'source:"native_wheel"' in html
    assert 'source:previous.source || "visible_range_change"' in html
    assert "span:Number(range.to) - Number(range.from)" in html
    assert "window.__lastDesktopWheelZoom" in html
    assert "lastDesktopWheelZoom: () => window.__lastDesktopWheelZoom || null" in html
    assert 'el.removeEventListener("wheel", containWheel, true)' in html
    assert "body.time-axis-dragging,body.time-axis-dragging .chart{cursor:ew-resize}" in html
    assert "body.price-axis-dragging,body.price-axis-dragging .chart{cursor:ns-resize}" in html
    assert "minBarSpacing:.5" in html
    assert "subscribeVisibleLogicalRangeChange" in html
    assert "item.logicalRange = {from:Number(range.from), to:Number(range.to)}" in html
    assert "关闭 MACD/RSI 会把空间还给价格 K 线" in html
    assert "item.chart.subscribeDblClick(() => resetChartZoom(tf))" in html
    assert "setVisibleLogicalRangeWithMemory(tf, item, range, \"render\")" in html
    assert "rightBarStaysOnScroll:true" in html
    assert "priceVisibleRange:chartState[tf]?.priceSeries?.priceScale?.().getVisibleRange?.() || null" in html
    assert "boundaryStrip: () => window.__lastReplayBoundaryStrip || null" in html
    assert "chartUnitMeta: () => window.__lastChartUnitMeta || {}" in html
    assert 'id="indicator-label-1m">EMA</small>' in html
    assert 'document.getElementById(`indicator-label-${tf}`)' in html
    assert 'upColor:"rgba(0,0,0,0)"' in html
    assert 'const CANDLE_UP_BORDER = "#2dd4bf"' in html
    assert 'const CANDLE_DOWN_FILL = "#ff6b6b"' in html
    assert 'downColor:"rgba(255,107,107,.72)"' in html
    assert 'borderUpColor:"rgba(45,212,191,.94)"' in html
    assert ".replay-contract" in html
    assert "每根 1m 收线 = 一次交易决策" in html
    assert "只看当时已经发生的 K 线" in html
    assert "多周期按同一时间对齐" in html
    assert 'id="decisionClockDeck"' in html
    assert ".decision-clock-deck" in html
    assert "Replay 主时钟" in html
    assert "策略周期" in html
    assert "策略 K 线归属" in html
    assert "本根决策来源" in html
    assert "function strategyClockTimeframe" in html
    assert "function replayMasterClockTimeframe" in html
    assert "function decisionTimingContext" in html
    assert "function replayContractSnapshot" in html
    assert "function refreshReplayContractSnapshot" in html
    assert "window.__lastReplayContractSnapshot" in html
    assert "window.__replayDebug = window.replayDebug" in html
    assert "historical_replay: window.__lastHistoryReplayBadge || historyReplayState()" in html
    assert "future_hidden_all" in html
    assert "one_third_future_space" in html
    assert "selected_candle_not_edge" in html
    assert "candle_style: TRADING_UNIT_SPEC.candle_style" in html
    assert "indicators: TRADING_UNIT_SPEC.indicators" in html
    assert "function renderDecisionClockDeck" in html
    assert "Replay 每步推进" in html
    assert "策略只有在自己的" in html
    assert "window.__lastDecisionClockDeck" in html
    assert "decisionClockDeck: () => window.__lastDecisionClockDeck" in html
    assert "chartDecisionLens: () => window.__lastChartDecisionLens" in html
    assert "chartDecisionLensCollapsed: () => chartDecisionLensCollapsed" in html
    assert "historyReplayBadge: () => window.__lastHistoryReplayBadge || historyReplayState()" in html
    assert "snapshotSummary: () => window.__lastSnapshotSummary" in html
    assert "decisionChartMarkers: () => window.__lastDecisionChartMarkers" in html
    assert "lastWorkflowJump: () => window.__lastWorkflowJump" in html
    assert "口述观点" in html
    assert 'id="pmReviewBrief"' in html
    assert ".pm-review-brief" in html
    assert "PM 复盘任务" in html
    assert 'id="pmAttributionWorksheet"' in html
    assert ".pm-attribution-worksheet" in html
    assert "归因检查" in html
    assert ".pm-attribution-detail" in html
    assert ".pm-attribution-detail:not([open]) .pm-attribution-grid" in html
    assert ".pm-attribution-detail:not([open]) .pm-evidence-strip" in html
    assert "归因维度" in html
    assert "方向 / 位置 / 入场 / SL / TP / 系统" in html
    assert "记录可信度" in html
    assert "M1-M5 平台状态" in html
    assert "function reviewAttributionCards" in html
    assert "function renderPmAttributionWorksheet" in html
    assert "方向" in html
    assert "位置" in html
    assert "入场时机" in html
    assert "SL 距离" in html
    assert "TP 空间" in html
    assert "系统/数据" in html
    assert "这是复盘清单，不是自动结论；最终归因要回到 K 线和成交路径。" in html
    assert "targetEquityReturn < 1" in html
    assert "TP 不满足你要求的 1% 本金目标" in html
    assert "pmAttributionWorksheet: () => window.__lastPmAttributionWorksheet || null" in html
    assert "state: () => state" in html
    assert "getState: () => state" in html
    assert "function reviewContextFromParams" in html
    assert "function returnContextFromParams" in html
    assert 'returnContext = {view:"replay", date:"", family:"all", strategyFilter:"all"}' in html
    assert 'params.get("return_view")' in html
    assert 'params.get("return_date")' in html
    assert 'params.get("return_strategy_filter")' in html
    assert 'url.searchParams.set("view", view)' in html
    assert 'url.searchParams.set("strategy_filter", returnContext.strategyFilter)' in html
    assert "function dashboardReturnLabel" in html
    assert 'if(view === "strategies") return "返回策略队列";' in html
    assert "function updateReturnDashboardButton" in html
    assert "function renderPmReviewBrief" in html
    assert 'params.get("review_question")' in html
    assert 'params.get("review_action")' in html
    assert 'params.get("review_label")' in html
    assert 'params.get("review_summary")' in html
    assert 'params.get("review_trade_id")' in html
    assert 'params.get("layout")' in html
    assert 'params.get("marker_scope")' in html
    assert 'params.get("decision_marker_mode")' in html
    assert 'pmReviewContext.active' in html
    assert 'document.body.classList.toggle("pm-review-mode", Boolean(pmReviewContext.active))' in html
    assert "body.pm-review-mode .shortcut-row" in html
    assert "body.pm-review-mode .replay-contract" in html
    assert "body.pm-review-mode .workflow-rail" in html
    assert "body.pm-review-mode .marker-row" in html
    assert "body.pm-review-mode .layout-row" in html
    assert "body.pm-review-mode .pm-review-brief-copy p.pm-review-outcome-note" in html
    assert "body.pm-review-mode .selection-receipt" in html
    assert "body.pm-review-mode .selection-receipt .entry-decision-callout" in html
    assert "body.pm-review-mode .pm-review-brief-copy p" in html
    assert "body.pm-review-mode .transport small" in html
    assert "body.pm-review-mode .selection-receipt{\n    grid-template-columns:minmax(0,1fr) auto;" in html
    assert ".pm-evidence-strip" in html
    assert 'aria-label="复盘记录"' in html
    assert "function pmEvidenceCards" in html
    assert "function pmEvidenceStripHtml" in html
    assert "state?.pm_evidence?.strategy_frequency" in html
    assert 'key:"M1"' in html
    assert 'key:"M5"' in html
    assert 'chartLayoutMode = "trader"' in html
    assert 'decisionMarkerMode = "actionable"' in html
    assert "function requestedReplayTradeId" in html
    assert 'query.set("review_trade_id", requestedTradeId)' in html
    assert "function replayTradeMatchesId" in html
    assert "trade.ticket_id, trade.order_id, trade.signal_id" in html
    assert 'params.get("review_exit_reason")' in html
    assert 'params.get("review_realized_pnl")' in html
    assert "function reviewTradeIsClosed" in html
    assert "function reviewOpenPositionCopy" in html
    assert "function reviewOutcomeCopy" in html
    assert "future_outcome_hidden_until_exit" not in html
    assert "未来出场结果只作为任务上下文，不画进入场时刻的行情。" in html
    assert "持仓中交易的 PM 复盘任务" in html
    assert "不把建议出场动作当成已平仓结果" in html
    assert "入场复盘" in html
    assert ".pm-review-outcome-note" in html
    assert ".trade-review-context" in html
    assert "当时 ${statusCopy(focusedTrade.status" in html
    assert "reviewContext: () => pmReviewContext" in html
    assert ".chart-card{height:clamp(330px, calc((100vh - 280px) / 2), 420px);display:grid;grid-template-columns:minmax(0,1fr);grid-template-rows:auto minmax(0,1fr);min-width:0;overflow:hidden;position:relative}" in html
    assert ".chart-card.replay-active::after" in html
    assert 'content:"未来 K 线未加载"' not in html
    assert 'content:"Replay 边界"' not in html
    assert ".chart-card.select-enabled .chart{cursor:copy}" in html
    assert ".chart-card.boundary-locked .chart{cursor:crosshair}" in html
    assert "width:var(--replay-blank-pct, 33.333%)" in html
    assert ".chart-card.replay-active::before" in html
    assert 'content:""' in html
    assert "left:calc(var(--replay-cursor-ratio, .6666) * 100%)" in html
    assert ".chart{width:100%;min-width:0;min-height:0;height:100%;cursor:crosshair;overflow:hidden;overscroll-behavior:contain;touch-action:none}" in html
    assert "function applyDesktopWheelZoom" in html
    assert '"native_wheel"' in html
    assert "function bindDesktopTimeAxisDrag" in html
    assert '"time_axis_drag"' in html
    assert "function bindDesktopPriceAxisDragProbe" in html
    assert 'source:"price_axis_drag"' in html
    assert "window.__lastDesktopWheelZoom" in html
    assert "window.__lastTimeAxisDragZoom" in html
    assert "window.__lastPriceAxisDragZoom" in html
    assert ".chart-grid.layout-trader-focus .chart-card .chart-meta .unit-line" in html
    assert "requestAnimationFrame(() => {" in html
    assert "resizeLightweightChart(item)" in html
    assert ".selection-receipt" in html
    assert "order:3;" in html
    assert ".quiet-no-go-summary" in html
    assert "普通 No-Go 快速摘要" in html
    assert "No-Go</span>" in html
    assert "data-replay-shift=\"1\">下一根</button>" in html
    assert ".selection-receipt.compact-no-go .selection-metrics{\n  display:none;" in html
    assert html.index('id="chartGrid"') < html.index('id="decisionPipeline"') < html.index('id="selectionReceipt"')
    assert ".pm-review-brief" in html
    assert "order:2" in html
    assert ".decision-tape-disclosure" in html
    assert "查看最近 Go / No-Go 时间带" in html
    assert ".trade-card-disclosure" in html
    assert "查看本根 No-Go / 出票记录" in html
    assert "查看入场 GO / 出票记录" in html
    assert "const evidenceSnap = entryDecision?.decision || snap" in html
    assert "signalTicketTriageHtml(evidenceSnap)" in html
    assert ".replay-strip{display:grid;grid-template-columns:1.2fr .9fr 1fr;gap:10px;margin:12px 0 12px;order:7}" in html
    assert ".details{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px;order:8}" in html
    assert ".chart-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;order:4}" in html
    assert "@media (min-width:1320px)" in html
    assert "grid-template-columns:2fr 2fr 1.35fr 1.35fr" in html
    assert 'chart-card[data-timeframe="1m"]' in html
    assert "grid-column:1 / span 2" in html
    assert "grid-row:span 2" in html
    assert "min-height:clamp(520px, calc(100vh - 460px), 700px)" in html
    assert "@media (min-width:1680px)" in html
    assert "min-height:clamp(560px, calc(100vh - 460px), 760px)" in html
    assert ".asof-strip" in html
    assert "order:10" in html
    assert "displayCategories" in html
    assert "function applyUrlParams" in html
    assert "function updateAddressBar" in html
    assert "history.replaceState" in html
    assert "function replayTimeLabel" in html
    assert "function replayRangeLabel" in html
    assert "function cursorContainmentLabel" in html
    assert 'function barStatus(row, tf="")' in html
    assert 'tf === "1m" && row.timestamp && minuteKey(row.timestamp) === minuteKey(state?.cursor)' in html
    assert 'bars.filter(row => barStatus(row, tf) === "forming").length' in html
    assert "function chartUnitMeta" in html
    assert "function visibleReplayBars" in html
    assert "function replayBarsThroughCursor" in html
    assert "const safeBars = replayBarsThroughCursor(bars, tf)" in html
    assert "bars.filter(row => {" in html
    assert "start <= cursorMs" in html
    assert "return safeBars.slice(-limit)" in html
    assert "const rawBars = box.bars || []" in html
    assert "const indicatorBars = replayBarsThroughCursor(rawBars, tf)" in html
    assert "const bars = visibleReplayBars(tf, rawBars)" in html
    assert "const sliceStart = Math.max(0, indicatorBars.length - bars.length)" in html
    assert "const rawCloses = indicatorBars.map(row => Number(row.close))" in html
    assert "emaSeries(rawCloses, 200).slice(sliceStart)" in html
    assert "raw_bar_count" in html
    assert "visible_bar_count" in html
    assert "ohlc-line" in html
    assert "context-close" in html
    assert "body.layout-trader-focus .chart-grid .chart-card.context-chart .chart-meta > span" in html
    assert "min-width:76px" in html
    assert "max-width:none" in html
    assert "body.layout-trader-focus .chart-grid .chart-card.context-chart .chart-meta .ohlc-line.context-close" in html
    assert "text-overflow:clip" in html
    assert "unit-line" in html
    assert "unit-line strong" in html
    assert "const isPrimary = tf === primaryChartTf" in html
    assert 'if(!isPrimary){' in html
    assert '<span class="ohlc-line context-close ${direction}">C ${esc(fmt(c))}</span>' in html
    assert "O ${esc(fmt(o))} H ${esc(fmt(h))} L ${esc(fmt(l))} C ${esc(fmt(c))}" in html
    assert '<span class="unit-line">${esc(indicators)}</span>' not in html
    assert "function replayDecisionCloseLabel" in html
    assert "function currentDecisionTimingLabel" in html
    assert "${esc(range)} · ${esc(decisionTime)}决策" in html
    assert "决策时间 ${traderTimeLabel(timestamp)}" in html
    assert "被评估K线：${currentDecisionTimingLabel()}。" in html
    assert "被评估K线 ${ctx.master_bar_range || \"--\"} · ${ctx.master_decision_close || \"--\"}评估" in html
    assert "master_decision_close" in html
    assert "strategy_decision_close" in html
    assert "--replay-cursor-ratio" in html
    assert "--replay-blank-pct" in html
    assert 'cardEl.style.setProperty("--replay-cursor-ratio"' in html
    assert "右侧隐藏 ${esc(blank)} 格" not in html
    assert 'const label = enabledIndicatorLabels().join(" / ") || "Price only"' in html
    assert "window.__lastChartUnitMeta" in html
    assert "item.priceSeries.setData(lwcCandleData(tf, bars))" in html
    assert "被评估K线刚收线" not in html
    assert "未映射" in html
    assert "class=\"containment\"" in html
    assert "replayRangeLabel(tf, row)" in html
    assert "function traderDashboardUrl" in html
    assert 'id="backDashboardBtn"' in html
    assert 'id="backDashboardBtn" class="btn utility-icon-btn"' in html
    assert 'aria-label="返回交易驾驶舱"' in html
    assert 'id="loadBtn" class="btn primary utility-icon-btn"' in html
    assert 'aria-label="刷新回放"' in html
    assert ".btn.utility-icon-btn" in html
    assert "body.layout-trader-focus .topbar .utility-icon-btn" in html
    assert "button.innerHTML" not in html
    assert "dashboardReturnShortLabel" not in html
    assert "btn-full" not in html
    assert "btn-short" not in html
    assert "let desiredCursorIso" in html
    assert "desiredCursor: () => desiredCursorIso" in html
    assert "loadingState: () => ({loading, pendingLoad, desiredCursorIso})" in html
    assert "new URLSearchParams(window.location.search)" in html
    assert 'params.get("strategy")' in html
    assert "function ensureStrategyOption" in html
    assert "const STRATEGY_DISPLAY_NAMES" in html
    assert "function strategyDisplayName" in html
    assert "缠论二买 · 1m Demo" in html
    assert "缠论二买 · 1m 无门控对照" in html
    assert "布林带均值回归 · 1m" in html
    assert "网格捕捉 · 1m" in html
    assert "伦敦/纽约时段突破 · 5m" in html
    assert 'option.dataset.dynamic = "true"' in html
    assert "option.textContent = strategyDisplayName(strategy)" in html
    assert "option.title = strategy" in html
    assert "function localizeText" in html
    assert "function humanizeReplayCopy" in html
    assert "由历史成交补齐的信号记录" in html
    assert "这笔记录由历史成交修复，原始下单记录缺失。" in html
    assert "function sideCopy" in html
    assert "function statusCopy" in html
    assert "function protectionCopy" in html
    assert "function effectiveTradeTp" in html
    assert "trade?.take_profit ?? trade?.target ?? trade?.target_price" in html
    assert "effectiveTradeTp(t)" in html
    assert "effectiveTradeTp(focusedTrade)" in html
    assert "function pnlHandCheckLabel" in html
    assert "function pnlHandCheckSummary" in html
    assert "function pnlHandCheckFormula" in html
    assert "function tradeHeadline" in html
    assert '"This candle closed without a qualifying strategy trigger.":"这根 K 线收线后没有达到策略触发门槛。"' in html
    assert '"market strong_short":"市场强空"' in html
    assert '"signal watch":"信号观望"' in html
    assert '"trade long":"交易做多"' in html
    assert '"strategy guardrails blocked new paper exposure":"策略保护规则拦截新增模拟敞口"' in html
    assert '"protection":"保护单"' in html
    assert '"snapshot":"快照"' in html
    assert '"implicit_per_bar_no_go":"逐根 No-Go"' in html
    assert 'function hasCandidateIntent' in html
    assert '["位置门控", `${candidateLike && positionGate.allow_candidate === false ? "已拦截" : "未作为主因"}' in html
    assert "没有新鲜方向信号，位置门控不是本根主因" in html
    assert '["Thesis", localizeText(signal.thesis || "--")]' in html
    assert 'document.getElementById("decisionSignal").textContent = `${sideCopy(signal.direction || "--")}' in html
    assert "localizeEvidence(item)" in html
    assert 'localizeText(entry.reason || "missing_entry_reason")' in html
    assert 'localizeText(exit.reason || "holding_open_position")' in html
    assert 'protectionCopy(protection.summary || "protection_missing")' in html
    assert "方向 / 数量" in html
    assert "手算 PnL" in html
    assert "pnlHandCheckFormula(handCheck)" in html
    assert "pnlHandCheckSummary(handCheck)" in html
    assert "localizeText(category.reason)" in html
    assert "tradeHeadline(card, trade)" in html
    assert "ensureStrategyOption(strategy)" in html
    assert 'const strategy = ensureStrategyOption(document.getElementById("strategyInput").value || urlStrategy || "gold_1m_chan")' in html
    assert 'value="gold_1m_macd_trend_vol_filter"' in html
    assert 'value="gold_5m_london_ny_breakout"' in html
    assert 'value="gold_1m_chan_macdfilter"' in html
    assert 'params.get("cursor")' in html
    assert 'id="asOfStrip"' in html
    assert 'id="selectionReceipt"' in html
    assert 'id="replayDecisionDock"' in html
    assert 'id="replayEventRail"' in html
    assert 'id="tradeLifecycleStrip"' in html
    assert 'id="decisionPipeline"' in html
    assert ".replay-decision-dock" in html
    assert ".decision-dock-grid" in html
    assert ".replay-event-rail" in html
    assert ".event-rail-track" in html
    assert ".event-pill" in html
    assert ".trade-lifecycle-strip" in html
    assert ".trade-lifecycle-steps" in html
    assert ".trade-stage" in html
    assert ".m1-replay-path" in html
    assert ".m1-replay-path-actions" in html
    assert ".m1-path-action" in html
    assert "function renderReplayDecisionDock" in html
    assert "function replayEventRailItems" in html
    assert "function renderReplayEventRail" in html
    assert "function bindReplayEventRail" in html
    assert "function tradeLifecycleStages" in html
    assert "function renderTradeLifecycleStrip" in html
    assert "function bindTradeLifecycleStrip" in html
    assert "function m1ReplayPathHtml" in html
    assert "function bindM1ReplayPath" in html
    assert "function indicatorDockValues" in html
    assert "function executionPlanDockText" in html
    assert "本根收线决策" in html
    assert "关键事件导航" in html
    assert "GO / BLK / SIG / 成交按时间排列" in html
    assert "event_rail_decision" in html
    assert "event_rail_trade" in html
    assert "交易生命周期导航" in html
    assert "M1 交易复盘路径" in html
    assert "按交易生命周期复盘这笔单" in html
    assert "1 · 先看入场 GO" in html
    assert "2 · 回到成交分钟" in html
    assert "3 · 看保护单" in html
    assert "data-m1-path-cursor" in html
    assert "data-m1-path-source" in html
    assert "data-m1-path-opened-at" in html
    assert "linked_trade_opened_at" in html
    assert "回到成交病历" in html
    assert "data-linked-trade-fill-jump" in html
    assert "bindM1ReplayPath();" in html
    assert "1 · 入场 GO" in html
    assert "2 · 成交" in html
    assert "3 · Take Profit" in html
    assert "4 · Stop Loss" in html
    assert "5 · Exit" in html
    assert "6 · 保护单" in html
    assert "trade_lifecycle_go" in html
    assert "trade_lifecycle_entry" in html
    assert "trade_lifecycle_exit" in html
    assert "tradeLifecycleStrip: () => window.__lastTradeLifecycleStrip" in html
    assert "replayEventRail: () => window.__lastReplayEventRail" in html
    assert "指标快照" in html
    assert "EMA20/100/200" in html
    assert "replayDecisionDock: () => window.__lastReplayDecisionDock" in html
    assert "function decisionPipelineSteps" in html
    assert "function renderDecisionPipeline" in html
    assert "window.__lastDecisionPipeline" in html
    assert "当前 K 线决策流水线" in html
    assert "No-Go 不是空白；它表示这一根 K 线已经经过市场方向、策略信号、位置过滤、风控、下单计划五个关口。" in html
    assert "市场观点" in html
    assert "策略信号" in html
    assert "位置 / 方向过滤" in html
    assert "质量 / 风控" in html
    assert "下单计划" in html
    assert 'data-decision-gate="${esc(step.key)}"' in html
    assert ".decision-pipeline" in html
    assert ".decision-gate.ok" in html
    assert ".decision-gate.warn" in html
    assert ".decision-gate.bad" in html
    assert ".decision-gate.off" in html
    assert ".selection-receipt" in html
    assert ".selection-metrics" in html
    assert "回放决策" in html
    assert "方向 / 置信度" in html
    assert "当时价格" in html
    assert "执行计划" in html
    assert "本根计划" in html
    assert "不下单 · 未生成入场 / 止盈 / 止损" in html
    assert 'const compactReasonText = "看观望原因";' not in html
    assert "function replayFocusedTrade" in html
    assert "focused_trade_marker" in html
    assert "GO 决策链未闭合 · 本根按 No-Go 评估" in html
    assert "这个时间点附近有真实交易记录；价格和交易病历可以复盘" in html
    assert "function decisionTapeRows" in html
    assert "const scoped = rows.filter(row => !state?.strategy_id || row.strategy_id === state.strategy_id)" in html
    assert "function decisionTapeHtml" in html
    assert "function bindDecisionTape" in html
    assert 'class="decision-tape"' in html
    assert 'data-decision-cursor' in html
    assert "已选择历史决策" in html
    assert "回放已跳到更早的 GO / 拦截决策。" in html
    assert "有候选计划但被拦截" in html
    assert "let selectionContext" in html
    assert "function renderSelectionReceipt" in html
    assert 'let planMetricLabel = "本根计划"' in html
    assert "GO 计划 Entry" in html
    assert "候选计划 Entry" in html
    assert "未执行 / 未成交" in html
    assert "不下单 · 未生成入场 / 止盈 / 止损" in html
    assert "__lastReplaySelection" in html
    assert "selectedTradeId: () => selectedTradeId" in html
    assert "已选择K线 · ${timeframeLabel(sourceTf)}" in html
    assert 'setCursor(addMinutes(current, minutes), "step", "1m", {stepMinutes:minutes})' in html
    assert "{stepMinutes:minutes}" in html
    assert "已推进 ${stepMinutes} 分钟" in html
    assert "回放边界沿 1m 主时间轴推进了 ${stepMinutes} 分钟。" in html
    assert "5m、15m、4H、1D 只在各自收线后推进。" in html
    assert "function renderAsOfStrip" in html
    assert "function asOfSummary" in html
    assert "market_view_expiry" in html
    assert "交易员检查" in html
    assert "口述观点" in html
    assert "观点已过期，不能继续过滤新信号" in html
    assert "观点有效，但结构化过期条件不完整" in html
    assert "结构化口述" in html
    assert 'id="marketIntakePanel"' in html
    assert 'id="marketIntakeText"' in html
    assert 'id="marketIntakeDraftBtn"' in html
    assert 'id="marketIntakeSaveBtn"' in html
    assert "/api/market-view/intake" in html
    assert "function submitMarketViewIntake" in html
    assert "window.__lastMarketViewIntake" in html
    assert "保存只更新 market_views，不会下单" in html
    assert 'document.getElementById("marketIntakeDraftBtn").onclick = () => submitMarketViewIntake(true)' in html
    assert 'document.getElementById("marketIntakeSaveBtn").onclick = () => submitMarketViewIntake(false)' in html
    assert "解析预览" in html
    assert "已保存为当前市场观点；正在刷新 Replay。" in html
    assert "生成时间" in html
    assert "const priceMoveExpiry" in html
    assert "threshold_move_pct" in html
    assert "target_price" in html
    assert "priceExpiryReady" in html
    assert "缺参考价，价格过期条件未启用" in html
    assert "expire_above" in html
    assert "expire_below" in html
    assert "function decisionSourceLabel" in html
    assert "function decisionContractText" in html
    assert "one_go_or_no_go_decision_per_closed_candle" in html
    assert "implicit_no_go_when_no_trigger" in html
    assert "每根 ${contract.decision_clock_timeframe" in html
    assert "function decisionStatusClass" in html
    assert ".status.implicit_no_go" in html
    assert 'snap?.decision_source === "implicit_per_bar_no_go"' in html
    assert "function latestExplicitSummary" in html
    assert "function displayDecisionStatus" in html
    assert "function displayTraderDecisionStatus" in html
    assert 'if(value === "no_go"){' in html
    assert 'return "拦截";' in html
    assert 'return "不下单";' in html
    assert "function executionPlanCopy" in html
    assert "function decisionCategory" in html
    assert "function decisionAttributionRows" in html
    assert "function signalTicketTriageModel" in html
    assert "function signalTicketTriageHtml" in html
    assert "function triageEvidenceBlock" in html
    assert "普通 No-Go 已压缩；展开五道关口" in html
    assert "展开出票漏斗：" in html
    assert "展开过滤、指标和出票漏斗" in html
    assert "展开空交易原因" in html
    assert "data-routine-no-go-triage" in html
    assert "function inlineReplayControlsHtml" in html
    assert 'data-inline-replay-controls' in html
    assert 'data-replay-shift="1"' in html
    assert "function handleInlineReplayControl" in html
    assert "function signalGateCopy" in html
    assert "function riskDiagnosticCopy" in html
    assert "信号到出票漏斗" in html
    assert "K线收线 → 策略信号 → 方向/位置过滤 → 质量/RR/风控 → Entry/TP/SL" in html
    assert "data-triage-gate" in html
    assert "window.__lastSignalTicketTriage" in html
    assert ".signal-ticket-triage" in html
    assert ".triage-gates" in html
    assert ".selection-triage-summary" in html
    assert "首屏信号到出票漏斗摘要" in html
    assert "漏斗结论" in html
    assert "当前关口" in html
    assert "候选没有变成 ticket" in html
    assert "候选没过信号强度 / 置信门" in html
    assert "历史记录：回测样本薄（现在不挡 paper）" in html
    assert "信号强度 $1 低于要求 $2" in html
    assert "信号置信度 $1 低于要求 $2" in html
    assert "risk.signal_gate" in html
    assert "risk.backtest_gate" in html
    assert "优先查强度/置信阈值、TP/RR 质量门、位置门、风险门" in html
    assert "function hasDirectionalSignal" in html
    assert "有方向信号，但被风控/门控挡住" in html
    assert "有信号，但被位置门控拦住" in html
    assert "有方向信号，但被人工方向过滤" in html
    assert "有方向信号，但未出票" in html
    assert "风险拦截" in html
    assert "方向过滤" in html
    assert "位置门控" in html
    assert "已过期 · ${localizeText(expiry.reason" in html
    assert "过滤器已解除" in html
    assert "策略有信号，不等于应该下单" in html
    assert "候选计划只用于复盘，不代表应该执行" in html
    assert ".trade-card.blocked" in html
    assert ".decision-attribution-grid" in html
    assert 'id="decisionNote"' in html
    assert "逐根评估 · No-Go" in html
    assert "这根 K 线收线后已完成评估" in html
    assert "最近明确决策" in html
    assert "本根收线结论：不下单。" in html
    assert "入场 / 止盈 / 止损：未生成。只有 GO 后才会生成可执行计划。" in html
    assert "function togglePlay" in html
    assert "function handleReplayKeyboard" in html
    assert "desiredCursorIso = cursor;" in html
    assert "const cursor = desiredCursorIso || inputToIso(document.getElementById(\"cursorInput\").value)" in html
    assert "document.getElementById(\"cursorInput\").onchange = () => {" in html
    assert "function pmReviewBlockerCopy" in html
    assert "漏斗瓶颈" in html
    assert "PM 漏斗瓶颈" not in html
    assert "本根硬拦截" in html
    assert 'event.key === "ArrowRight"' in html
    assert 'event.key === "ArrowLeft"' in html
    assert 'event.code === "Space"' in html
    assert "Shift</span>" in html
    assert "未来隐藏" in html
    assert 'aria-label="交易回放控制"' in html
    assert 'aria-label="选择回放起点"' in html
    assert 'aria-label="播放回放"' in html
    assert 'class="transport-icon">‹</span>' in html
    assert 'class="transport-icon">▶</span>' in html
    assert 'class="transport-label">1m 主时钟</span>' in html
    assert ".transport-play" in html
    assert ".transport-jump" in html
    assert "position:relative" in html
    assert "top:auto" in html
    assert "transform:none" in html
    assert "position:fixed" not in html
    assert "position:sticky" not in html
    assert 'id="transportPrevBtn"' in html
    assert 'id="transportPlayBtn"' in html
    assert 'id="transportNextBtn"' in html
    assert 'id="prevBtn"' not in html
    assert 'id="nextBtn"' not in html
    assert 'id="playBtn"' not in html
    assert 'document.getElementById("transportBack15Btn").onclick = () => shift(-15)' in html
    assert 'document.getElementById("transportForward15Btn").onclick = () => shift(15)' in html
    assert 'document.addEventListener("click", handleInlineReplayControl)' in html
    assert "function updatePlayButtons" in html
    assert '{className:"transport-icon", text:isGo ? "◆" : (tone === "blocked" ? "◇" : "◌")}' in html
    assert '{className:"transport-icon", text:"●"}' in html
    assert "点击任意 K 线，选择新的 replay 起点" in html
    assert "5m/15m/4H/1D 只在对应周期收线后推进" in html
    assert "Math.round(bars.length * RIGHT_BLANK_RATIO)" in html
    assert "function defaultVisibleLogicalRange" in html
    assert "bars.length + blankCount - 1" in html
    assert "if(!param?.time) return" in html
    assert "setVisibleLogicalRangeWithMemory(tf, item, range, \"render\")" in html
    assert "if(row.bucket_end) return row.bucket_end" in html
    assert "未来K线已隐藏" not in html
    assert "if(source === \"chart_click\") setSelectCandleMode(false)" in html
    assert "if(!selectCandleMode) return" not in html
    assert "已锁定 · 点任意 K 线可重新定位" in html
    assert "function plannedEntryPrice" in html
    assert "plan.entry_zone ?? plan.entry_price" in html
    assert r"raw.match(/\d+(?:\.\d+)?/g)" in html
    assert "const entryPrice = plannedEntryPrice(plan, snap.price || bars.at(-1)?.close)" in html
    assert "function buildLightweightMarkers" in html
    assert "position:lightweightMarkerPosition(mark)" in html
    assert 'if(Number.isFinite(price) && marker.position.startsWith("atPrice")) marker.price = price;' in html
    assert "setLightweightMarkers(item, markers)" in html
    assert "const tradeTp = effectiveTradeTp(trade)" in html
    assert "const tradeSl = effectiveTradeSl(trade)" in html
    assert '["Entry","TP","SL","交易入场","Exit"].includes(mark.name)' in html
    assert 'name:"回放位置"' in html
    assert 'id="markerScopeControls"' in html
    assert 'data-marker-scope="current"' in html
    assert 'data-marker-scope="nearby"' in html
    assert 'data-marker-scope="all"' in html
    assert 'let markerScope = "current"' in html
    assert "function visibleTradeMarkers" in html
    assert "function isTradeActiveAtCursor" in html
    assert "opened <= cursor && cursor <= closed" in html
    assert "默认只显示当前 K 线的决策、入场、止盈、止损、出场" in html
    assert "旧持仓请切到附近交易" in html
    assert "(selectedTradeId && trade.trade_id === selectedTradeId)" in html
    assert "function setMarkerScope" in html
    assert "function markerScopeDescription" in html
    assert "附近 90 分钟" in html
    assert "当前标记范围内没有交易" in html
    assert 'snap.final_decision === "go"' in html
    assert "priceSeries" in html
    assert 'chart_library:"tradingview_lightweight_charts"' in html
    assert '{type:"slider"' not in html
    assert 'id="decisionCard"' in html
    assert 'id="tradeCard"' in html
    assert "交易病历" in html
    assert "本根复盘</div><span class=\"chip\">Go / No-Go · Entry / TP / SL" in html
    assert "当前 K 线决策加载中" in html
    assert "renderReplayDecisionCard(document.getElementById(\"decisionCard\"))" in html
    assert "function noTradeReviewCardHtml" in html
    assert "本根没有交易病历" in html
    assert "No-Go 病历" in html
    assert ".no-trade-compact" in html
    assert ".no-trade-compact-grid" in html
    assert ".no-trade-compact-cell.next b{color:#ffe6a6}" in html
    assert "body.layout-trader-focus .no-trade-compact-grid{\n  display:none;" in html
    assert ".trade-record-strip" in html
    assert 'aria-label="交易病历摘要"' in html
    assert 'aria-label="本根结论摘要"' in html
    assert "<b>${esc(decisionText)}</b>" in html
    assert "<span>${esc(marketStatusText)}</span>" in html
    assert "<span>${esc(executionStatusText)}</span>" in html
    assert "<em>${esc(compactReasonText)}</em>" not in html
    assert "当前无成交" in html
    assert "当前标记范围内没有交易；要看成交，切到“附近交易”或“全部交易”。" in html
    assert "当前标记范围内没有交易；需要审计历史成交" not in html
    assert "要复盘历史成交时，切到“附近交易”或“全部交易”" in html
    assert "结论</span><b>${esc(`${decisionText} · ${directionText}`)}</b>" not in html
    assert "主因</span><b>${esc(reasonText)}</b>" in html
    assert "下一步</span><b>${esc(nextText)}</b>" in html
    assert "继续下一根；要看成交，切到附近交易或全部交易。" in html
    assert "需要审计成交时切到附近交易" not in html
    assert "本轮结论应回到策略漏斗" in html
    assert "不要把这张卡当作成交复盘" in html
    assert "未生成入场 / 止盈 / 止损；只有 GO 后才会生成可执行计划。" in html
    assert "data-return-dashboard" in html
    assert "function renderTradeCard" in html
    assert "function setSelectedTrade" in html
    assert "function clearHoverState" in html
    assert "clearHoverState();" in html
    assert "window.__replayState = state" in html
    assert "window.__replay = window.replayDebug" in html
    assert "chartState: () => chartState" in html
    assert "已定位交易病历" in html
    assert "交易记录" in html
    assert "M1 病历卡完整。该收线时刻仍有逐根 No-Go 结论，但触发这笔交易的原始 GO 决策链尚未闭合。" in html
    assert "data-select-trade" in html
    assert "合规通过" in html
    assert "function m1AcceptanceItems" in html
    assert "function m1AcceptanceChecklistHtml" in html
    assert "M1 交易病历验收" in html
    assert "M1 病历完整" in html
    assert "M1 病历缺" in html
    assert ".m1-acceptance-checklist" in html
    assert ".m1-checklist-items" in html
    assert "入场理由" in html
    assert "出场理由" in html
    assert "保护单状态" in html
    assert "合规检查" in html
    assert "trade-alert" in html
    assert "保护单缺失或不完整" in html
    assert "不能被视为安全持仓" in html
    assert ".trade-card.planned" in html
    assert ".trade-card.no-go" in html
    assert "function currentExecutionPlan" in html
    assert "function renderReplayDecisionCard" in html
    assert "GO · 可执行计划" in html
    assert "GO · 已生成计划" in html
    assert 'replace(/^PM 下一步：/, "")' in html
    assert "本根收线结论：GO，允许进入执行计划。" in html
    assert "本根收线结论：不下单。" in html
    assert "入场 / 止盈 / 止损：未生成。只有 GO 后才会生成可执行计划。" in html
    assert "这根 K 线生成了入场 / 止盈 / 止损计划" in html
    assert "成交后这里会切换成完整 M1 交易病历卡" in html
    assert "signalTicketTriageHtml(snap)" in html
    assert "triageEvidenceBlock(snap, compactRoutineNoGo)" in html
    assert "逐根 K 线决策 · No-Go" in html
    assert "这不是缺数据；这是系统在这根 K 线收线后完成评估" in html
    assert "只有 GO 后才会生成入场 / 止盈 / 止损" in html


def test_dashboard_replay_returns_to_trader_dashboard_with_cursor_context():
    html = Path("dashboard-replay.html").read_text(encoding="utf-8")

    assert "function traderDashboardUrl" in html
    assert 'const cursor = state?.cursor || inputToIso(document.getElementById("cursorInput").value)' in html
    assert 'const view = returnContext.view || "replay"' in html
    assert "dashboardReturnLabel()" in html
    assert "updateReturnDashboardButton();" in html
    assert 'url.searchParams.set("view", view)' in html
    assert "if(cursor){" in html
    assert 'url.searchParams.set("cursor", cursor)' in html
    assert 'url.searchParams.set("replay_cursor", cursor)' in html
    assert 'url.searchParams.set("source", "mtf_replay")' in html
    assert 'url.searchParams.set("replay_source", "mtf_replay")' in html
