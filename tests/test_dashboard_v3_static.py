from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_v3_exposes_trader_facing_information_architecture():
    html = (ROOT / "dashboard-v3.html").read_text(encoding="utf-8")

    assert "GoldBot Trader Console / 黄金交易驾驶舱" in html
    assert 'id="langToggle"' in html
    assert 'data-lang="zh"' in html
    assert 'data-lang="en"' in html
    assert 'data-lang="bi"' in html
    assert "function textPair" in html
    assert "const REQUEST_TIMEOUT_MS = 12000" in html
    assert "async function fetchWithTimeout" in html
    assert 'timeout.code = "dashboard_timeout"' in html
    assert "Dashboard 服务没有响应" in html
    assert "交易机器人可能仍在跑，但驾驶舱暂时读不到数据" in html
    assert "window.goldbotCharts.lastDashboardError" in html
    assert "fetchWithTimeout(`${API_BASE}/api/dashboard?${query.toString()}`" in html
    assert "function renderStaticLabels" in html
    assert "function renderLanguageControls" in html
    assert "function renderTabs" in html
    assert "renderLanguageControls();\nrenderTabs();\nloadDashboard();" in html
    assert "goldbot_trader_lang" in html
    assert "goldbot_trader_lang_version" in html
    assert "zh-default-v2" in html
    assert 'const productDefault = "zh"' in html
    assert 'params.get("lang") || params.get("trader_lang")' in html
    assert 'if(["zh","en","bi"].includes(urlLang)) return urlLang' in html
    assert 'if(version === langVersion && ["zh","en","bi"].includes(saved)) return saved' in html
    assert 'localStorage.setItem("goldbot_trader_lang", productDefault)' in html
    assert "function initialUrlParam" in html
    assert "function initialView" in html
    assert "function initialStrategy" in html
    assert "function initialFamily" in html
    assert "function initialStrategyFilter" in html
    assert "function marketViewStatus" in html
    assert "function marketViewTitle" in html
    assert "function marketViewFacts" in html
    assert "function marketViewActionLabel" in html
    assert "function marketViewPriceExpiryLabel" in html
    assert "function marketViewAllowedDirectionsLabel" in html
    assert "market_view_status" in html
    assert "Park方向过滤" in html
    assert "filter active" in html
    assert "filter expired" in html
    assert "过滤器生效中" in html
    assert "过滤器已解除" in html
    assert "不过滤新信号" in html
    assert "价格过期" in html
    assert "目标位未设置" in html
    assert "No target" in html
    assert "target_price" in html
    assert "允许方向" in html
    assert 'initialUrlParam("strategy") || initialUrlParam("strategy_id")' in html
    assert 'view:initialView()' in html
    assert "family:initialFamily()" in html
    assert "strategyFilter:initialStrategyFilter()" in html
    assert 'selectedStrategy:initialStrategy()' in html
    assert "function renderApiChip" in html
    assert 'chip.dataset.labelKey = tone === "ok" ? "connected"' in html
    assert 'connected: textPair("已连接","Live")' in html
    assert 'data-zh="总览" data-en="Overview"' in html
    assert 'data-zh="NAV versus 黄金" data-en="NAV versus Gold"' in html
    assert 'id="view-replay" class="panel replay-index-surface"' in html
    assert "Trade Index / 交易索引" in html
    assert 'id="multiTimeframeReplayBtn"' in html
    assert 'id="headerReplayBtn"' in html
    assert 'id="refreshBtn" class="btn primary utility-icon-btn"' in html
    assert 'aria-label="刷新驾驶舱"' in html
    assert 'id="refreshBtn" class="btn primary" data-zh="刷新" data-en="Refresh"' not in html
    assert ".btn.utility-icon-btn" in html
    assert "function headerReplayStrategyId" in html
    assert "function headerReplayContext" in html
    assert "function renderHeaderReplayCta" in html
    assert "function openHeaderReplayCta" in html
    assert '$("headerReplayBtn").onclick = () => openHeaderReplayCta();' in html
    assert "trader_header_replay_cta" in html
    assert 'class="btn ops trader-ops-only"' in html
    assert "驾驶舱首屏多周期复盘" in html
    assert "直接进入 TradingView 式多周期 K 线回放" in html
    assert "function multiTimeframeReplayUrl" in html
    assert 'url.searchParams.set("layout", options.layout || "trader")' in html
    assert "MTF Replay / 多周期回放" in html
    assert "multiTimeframeReplayUrl(detail, {preferReplayCursor:true})" in html
    assert "function defaultReplayReviewContextForTarget" in html
    assert "function mergeReplayReviewContext" in html
    assert "交易病历 + 多周期回放" in html
    assert "这笔交易是否符合计划、入场理由、TP/SL 和风险边界？" in html
    assert "reviewSource:\"trader_command_strip\"" in html
    assert "const preserveRunDate = Boolean(cursorOverride && mergedReviewContext && Object.keys(mergedReviewContext).length)" in html
    assert "options.explicitDashboardDateOnly ? explicitDashboardDate" in html
    assert "options.explicitDashboardDateOnly ? \"\" : replayDateFromCursor(cursor)" in html
    assert "function latestDecisionReplayCursor" in html
    assert "latest_go_decision_snapshot" in html
    assert "latest_decision_snapshot" in html
    assert "latestDecisionReplayCursor(detail || {})" in html
    assert "dashboard-replay.html" in html
    assert "Open multi-timeframe candle replay" in html
    assert "multi-timeframe candle replay" in html
    assert 'data-aria-zh="选择策略" data-aria-en="Select strategy"' in html
    assert "document.title = textPair" in html
    assert 'shortZh:"突破"' in html
    assert 'shortEn:"Breakout"' in html
    assert 'shortZh:"5分趋势"' in html
    assert 'shortEn:"5m Trend"' in html
    assert 'gold_5m_adx_ema_pullback:{zh:"5分钟ADX回踩"' in html
    assert 'shortZh:"5m ADX"' in html
    assert 'gold_5m_london_ny_breakout:{zh:"5分钟伦敦/纽约突破"' in html
    assert 'shortZh:"伦敦/纽约突破"' in html
    assert 'gold_1m_bollinger_reclaim_filter:{zh:"1分钟布林收复"' in html
    assert 'shortZh:"布林收复"' in html
    assert 'trend_pullback:["趋势回踩","Trend pullback"]' in html
    assert 'session_breakout:["时段突破","Session breakout"]' in html
    assert 'bollinger_reclaim_filter:["布林收复过滤","Bollinger reclaim filter"]' in html
    assert 'london_ny_compression_breakout:["伦敦/纽约重叠压缩突破","London/NY overlap compression breakout"]' in html
    assert '.replace(/5m london ny breakout/gi,"5分钟伦敦/纽约突破 / 5m London/NY Breakout")' in html
    assert '.replace(/1m bollinger reclaim filter/gi,"1分钟布林收复 / 1m Bollinger Reclaim")' in html
    assert 'textPair("出票","Ticket")' in html
    assert 'textPair("查K线","Replay")' in html
    assert "候选已经触发，但回测样本太薄" in html
    assert "样本型策略没有触发" in html
    assert "缠论结构策略本来就低频" in html
    assert "localizeMixed(translateCopy(frequency.recommendation?.reason || \"\"))" in html
    assert "function shortName" in html
    assert "textPair(m.shortZh" in html
    assert "Overview / 总览" in html
    assert "Strategies / 策略" in html
    assert "Trade Index / 交易索引" in html
    assert "Trade Replay / 交易回放" not in html
    assert "Daily Loop / 每日闭环" in html
    assert 'id="productTrustBanner"' in html
    assert "function renderProductTrust" in html
    assert "function maturityCheckLabel" in html
    assert "Product trust needs attention" in html
    assert "产品可信度有注意项" in html
    assert "Trade records" in html
    assert "Strategy books" in html
    assert "Strategy edge" in html
    assert "Frequency attribution" in html
    assert "Execution safety" in html
    assert "Promotion locked" in html
    assert "No proven winner; allocation and auto-tuning stay locked." in html
    assert ".product-trust-banner" in html
    assert ".product-trust-steps" in html
    assert ".product-trust-review-lane" in html
    assert ".pm-review-lane-grid" in html
    assert "PM 今日复盘入口" in html
    assert "PM review lane" in html
    assert "Start here: review losses/stops first" in html
    assert "data-trust-review-replay" in html
    assert "data-trust-review-cursor" in html
    assert "data-trust-review-question" in html
    assert "data-trust-review-action" in html
    assert "data-trust-review-label" in html
    assert "data-trust-review-summary" in html
    assert "data-trust-review-trade-id" in html
    assert "data-trust-review-exit-reason" in html
    assert "data-trust-review-realized-pnl" in html
    assert "回放这一根K线" in html
    assert "Replay this candle" in html
    assert "function productTrustReviewLaneHtml" in html
    assert "function productTrustReviewCardHtml" in html
    assert "function reviewTradeContext" in html
    assert "reviewQuestion: target.dataset.trustReviewQuestion" in html
    assert "reviewTradeId: target.dataset.trustReviewTradeId" in html
    assert "reviewSource: \"pm_review_lane\"" in html
    assert "review_question" in html
    assert "review_action" in html
    assert "review_label" in html
    assert "review_summary" in html
    assert "review_trade_id" in html
    assert "review_exit_reason" in html
    assert "review_realized_pnl" in html
    assert "function setOptionalReplayParam" in html
    assert ".product-trust-journey" in html
    assert ".journey-card" in html
    assert "function productTrustJourneyHtml" in html
    assert "function productTrustJourneyCardHtml" in html
    assert "function statusToneForJourney" in html
    assert "Trade record cards" in html
    assert "Strategy books" in html
    assert "Edge judgment" in html
    assert "Frequency attribution" in html
    assert "Execution safety" in html
    assert "PM allocation lock" in html
    assert "Safety truth is anchored to the active demo strategy" in html
    assert "stale global reconciliation no longer masquerades as current" in html
    assert "Waiting for at least one proven strategy" in html
    assert "Review trades" in html
    assert "Open OPS board" in html
    assert "Ops details" in html
    assert '>${esc(textPair("Open OPS","Open OPS"))}<' not in html
    assert 'id="executionSafetyBoard"' in html
    assert "function renderExecutionSafetyBoard" in html
    assert "执行安全红线" in html
    assert "TP/SL 保护单" in html
    assert "活跃模拟策略对账" in html
    assert "真钱网络单" in html
    assert "飞书告警" in html
    assert "行情可信闸" in html
    assert "network_call_attempted" in html
    assert "blocked_by_activation_gate" in html
    assert "window.goldbotCharts.executionSafetyBoard" in html
    assert "const traderEnvAlreadyVisible = Boolean(" in html
    assert "window.goldbotCharts.executionSafetySuppressed" in html
    assert "顶部交易状态已覆盖暂停开仓结论；执行红线明细留在 OPS。" in html
    assert ".execution-safety-board" in html
    assert ".execution-safety-board.compact{gap:0;margin:0 0 8px;padding:6px 9px;min-height:38px}" in html
    assert ".execution-safety-board.compact .execution-safety-grid" in html
    assert ".execution-safety-board.compact .execution-safety-title span{display:none}" in html
    assert ".execution-safety-board.compact .btn.small{height:26px;padding:0 8px;font-size:11px}" in html
    assert 'if(boardTone !== "bad")' in html
    assert 'el.className = `execution-safety-board compact ${boardTone}`' in html
    assert ".execution-safety-grid" in html
    assert "Fix execution safety before discussing strategy returns." in html
    assert ".product-trust-banner.compact .product-trust-pulse" in html
    assert 'if(platformTone !== "bad")' in html
    assert "productTrustSuppressed" in html
    assert "非关键平台成熟度信息不打断 Trader 首屏" in html
    assert "const compact = false" in html
    assert '$("productTrustBanner")?.classList.add("compact")' in html
    assert "function overviewEdgeMetric" in html
    assert "全部跑输黄金；显示最小落后" in html
    assert "No strategy beating Gold; least lag shown" in html
    assert '"观察超额"' in html
    assert '"Watchlist edge"' in html
    assert "not a validated win" in html
    assert '"领先黄金"' in html
    assert '"Beating Gold"' in html
    assert 'id="strategyDecisionSummary"' in html
    assert 'id="strategyScopeDrawer" class="strategy-scope-drawer"' in html
    assert '<span data-zh="筛选" data-en="Filters">Filters / 筛选</span>' in html
    assert "策略筛选与概览" not in html
    assert "Strategy filters and overview" not in html
    assert 'id="strategyWorkflowGuide"' in html
    assert 'id="strategyPmPrescription"' in html
    assert 'id="strategyDeepDiveDrawer" class="strategy-diagnostics-drawer strategy-deep-dive-drawer"' in html
    assert "策略深挖" in html
    assert "Strategy deep dive" in html
    assert 'id="strategyDiagnosticsDrawer"' in html
    assert 'id="strategyExecutionFunnel"' in html
    assert 'id="strategyFunnelMatrix"' in html
    assert 'id="strategyTriageDeck"' in html
    assert 'id="strategyQuickFilters"' in html
    assert 'id="strategyBookDrawer"' in html
    assert 'id="strategyPerformanceDetails"' in html
    assert 'id="strategyPerformanceSummary"' in html
    assert 'id="strategyPerformanceTable"' in html
    assert "策略监控台" in html
    assert "Strategy Monitor" in html
    assert "先看 PM 处方和行动队列" in html
    assert "Start with the PM prescription and action queue" in html
    assert "策略诊断" in html
    assert "Strategy diagnostics" in html
    assert "策略账本核对" in html
    assert "Strategy book audit" in html
    assert '<details id="strategyFunnelMatrix" class="strategy-funnel-matrix"></details>' in html
    assert ".strategy-diagnostics-drawer:not([open]) .strategy-diagnostics-body{display:none}" in html
    assert ".strategy-deep-dive-drawer:not([open]) .strategy-deep-dive-body{display:none}" in html
    assert "#strategyDeepDiveDrawer > summary .strategy-diagnostics-title span{display:none}" in html
    assert html.index('id="strategyScopeDrawer"') < html.index('id="strategyPmPrescription"') < html.index('id="strategyGrid"') < html.index('id="strategyDeepDiveDrawer"')
    assert html.index('id="strategyScopeDrawer"') < html.index('id="familyFilters"') < html.index('id="strategyDecisionSummary"') < html.index('id="strategyPmPrescription"')
    assert ".strategy-scope-drawer:not([open]) .strategy-scope-body{display:none}" in html
    assert "#view-strategies > .card > .section-title{margin-bottom:8px}" in html
    assert "#view-strategies > .card > .section-title .support-copy{display:none}" in html
    assert "#strategyGrid .strategy-action-title span{display:none}" in html
    assert html.index('id="strategyDeepDiveDrawer"') < html.index('id="strategyDiagnosticsDrawer"') < html.index('id="strategyBookDrawer"') < html.index('id="strategyPerformanceDetails"')
    assert html.index('id="strategyDiagnosticsDrawer"') < html.index('id="strategyQuickFilters"') < html.index('id="m4FrequencyAttributionBoard"')
    assert html.index('id="strategyBookDrawer"') < html.index('id="strategyPerformanceDetails"') < html.index('id="strategyPerformanceTable"')
    assert "复核成交" in html
    assert "Review trade" in html
    assert "只观察" in html
    assert "Watch only" in html
    assert "等样本" in html
    assert "Need sample" in html
    assert "暂停观察" in html
    assert "Pause watch" in html
    assert "补交易记录" in html
    assert "Complete trade records" in html
    assert "下一步证据" in html
    assert "Next evidence" in html
    assert "暂停/淘汰触发" in html
    assert "Pause trigger" in html
    assert "交易记录待补齐" in html
    assert "trade records need completion" in html
    assert "function strategyDecision" in html
    assert "function strategyNextEvidence" in html
    assert "function strategyRetireTrigger" in html
    assert "function renderStrategyDecisionSummary" in html
    assert "function dailyTradeSampleSummary" in html
    assert "function renderStrategyExecutionFunnel" in html
    assert "今日交易漏斗" in html
    assert "Today’s Trade Funnel" in html
    assert "逐根评估" in html
    assert "Per-bar decisions" in html
    assert "方向候选" in html
    assert "Directional candidates" in html
    assert "Ticket" in html
    assert "真实执行" in html
    assert "Executed" in html
    assert "观察/不交易不计样本；PM 先看候选在哪一层漏掉。" in html
    assert "function funnelBlockerCopy" in html
    assert "function topFunnelReviewItem" in html
    assert "window.goldbotCharts.strategyExecutionFunnel" in html
    assert "data-funnel-replay" in html
    assert "打开漏斗复盘K线" in html
    assert ".strategy-execution-funnel" in html
    assert ".funnel-steps" in html
    assert "function renderStrategyFunnelMatrix" in html
    assert "function funnelMatrixRows" in html
    assert "function funnelMatrixLeakCopy" in html
    assert "function funnelMatrixActionCopy" in html
    assert "function funnelMatrixReviewEvidence" in html
    assert "function funnelMatrixReviewCursor" in html
    assert "详细策略漏斗矩阵" in html
    assert "Detailed Strategy Funnel Matrix" in html
    assert "用于排查候选、出票和执行断点。" in html
    assert "Use this to diagnose candidate, ticket, and execution breaks." in html
    assert "data-funnel-matrix-replay" in html
    assert "data-funnel-matrix-cursor" in html
    assert "forceDateOnly:!cursor" in html
    assert "cursor," in html
    assert "review_cursor: funnelMatrixReviewCursor(row)" in html
    assert "Inspect funnel blocker in Replay" in html
    assert "查看候选K线" in html
    assert "Inspect candidate candle" in html
    assert "window.goldbotCharts.strategyFunnelMatrix" in html
    assert ".strategy-funnel-matrix" in html
    assert ".strategy-funnel-matrix:not([open]) .funnel-matrix-scroll" in html
    assert ".funnel-matrix-row" in html
    assert "查K线" in html
    assert "function renderStrategyTriageDeck" in html
    assert "function strategyTriageCard" in html
    assert "function strategyTriageToneLabel" in html
    assert "window.goldbotCharts.strategyTriageDeck" in html
    assert "data-triage-replay" in html
    assert "data-triage-filter" in html
    assert "function renderStrategyWorkflowGuide" in html
    assert "Trader 使用路径" in html
    assert "Trader workflow" in html
    assert ".strategy-workflow-lead{display:grid;grid-template-columns:auto minmax(0,1fr);gap:10px;align-items:center;min-width:0}" in html
    assert "strategy-workflow-steps-details" in html
    assert "完整复盘路径：漏斗 -> K线 -> 病历 -> 记录 -> 晋级锁" in html
    assert "Full review path: funnel -> OHLC -> trade card -> records -> promotion lock" in html
    assert ".strategy-workflow-steps-details:not([open]) .strategy-workflow-steps{display:none}" in html
    assert "打开优先回放" in html
    assert "Open priority replay" in html
    assert "data-workflow-replay" in html
    assert "data-workflow-cursor" in html
    assert "strategy_workflow_guide" in html
    assert "window.goldbotCharts.strategyWorkflowGuide" in html
    assert ".strategy-workflow-guide" in html
    assert ".workflow-step" in html
    assert "function renderStrategyPmPrescription" in html
    assert "function strategyPmPrescription" in html
    assert "function prescriptionCopyForReason" in html
    assert "function prescriptionMetricLabel" in html
    assert "function prescriptionFocusCopy" in html
    assert "PM 处方" in html
    assert "PM prescription" in html
    assert "function pmFacingBlockerLabel" in html
    assert "回测样本薄：只影响晋升，不挡 paper" in html
    assert "上下文" in html
    assert "不挡paper；继续看出票漏斗" in html
    assert "Thin backtest: blocks promotion, not paper" in html
    assert "do not treat unticketed candidates" in html
    assert "data-prescription-replay" in html
    assert "strategy_pm_prescription" in html
    assert "window.goldbotCharts.strategyPmPrescription" in html
    assert ".strategy-pm-prescription" in html
    assert ".prescription-inner" in html
    assert ".prescription-lead>.prescription-copy{display:none}" in html
    assert ".prescription-actions>.prescription-copy{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}" in html
    assert ".prescription-redline{border-top:1px solid rgba(255,255,255,.075);padding-top:7px;min-width:0}" in html
    assert ".prescription-redline:not([open]) .prescription-redline-body{display:none}" in html
    assert "prescription-redline-body" in html
    assert "红线" in html
    assert "Red line" in html
    assert ".prescription-focus-list{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:6px}" in html
    assert ".prescription-focus-row>div{display:grid;gap:2px;min-width:0}" in html
    assert 'id="m4FrequencyAttributionBoard"' in html
    assert "function renderM4FrequencyAttributionBoard" in html
    assert "function m4FrequencyGroups" in html
    assert "function m4ReasonCopy" in html
    assert "频率与风控归因" in html
    assert "Frequency and Risk Attribution" in html
    assert "今天为什么只开这些单" in html
    assert "Frequency is diagnostic, not a hard SLA" in html
    assert "市场没有给合格信号" in html
    assert "回测样本薄，但不挡 paper" in html
    assert "薄回测只说明不能晋升或加真钱" in html
    assert "风控预算挡住新单" in html
    assert "data-m4-frequency-replay" in html
    assert "reviewSource:\"m4_frequency_attribution\"" in html
    assert "window.goldbotCharts.m4FrequencyAttribution" in html
    assert ".m4-frequency-board" in html
    assert ".m4-frequency-card" in html
    assert "Open MTF replay" in html
    assert "Switch table filter" in html
    assert "Do not promote low-execution strategies from return rank alone." in html
    assert ".strategy-triage-deck" in html
    assert ".strategy-triage-card" in html
    assert "function strategyDecisionRank" in html
    assert "function renderStrategyPerformanceTable" in html
    assert "function strategyPerformanceRow" in html
    assert "function renderStrategyQuickFilters" in html
    assert 'id="strategyBookBoard"' in html
    assert "function renderStrategyBookBoard" in html
    assert "function strategyBookCardHtml" in html
    assert "function humanizeBackendCopy" in html
    assert "function traderDisplayLabel" in html
    assert "function portfolioDisplayLabel" in html
    assert "由历史成交补齐的信号记录" in html
    assert "这笔记录由历史成交修复，原始下单记录缺失。" in html
    assert "identity.trader_id || row.classification?.family_label" not in html
    assert "function strategyBookReviewContext" in html
    assert "策略账户账本" in html
    assert "Strategy Account Books" in html
    assert "NAV ${fmtMoney(b.current)} = ${fmtMoney(b.start)} + ${fmtMoney(b.realized)} + ${fmtMoney(b.unrealized)}" in html
    assert "data-strategy-book-open" in html
    assert "策略账本对账" in html
    assert "Does ${name}'s book reconcile?" in html
    assert ".strategy-book-board" in html
    assert ".strategy-book-card" in html
    assert "function chartLibraryReady" in html
    assert "function renderNavChartUnavailable" in html
    assert "Chart library did not load; strategy table, review queue, and multi-timeframe replay still work." in html
    assert "window.goldbotCharts.navChartUnavailable" in html
    assert ".trader-env-banner{display:none;margin:0 0 8px;padding:6px 9px" in html
    assert 'const showTraderBanner = overallDown || anyWarn;' in html
    assert 'if(!showTraderBanner){' in html
    assert ".trader-env-sub{display:none;" in html
    assert ".trader-env-summary{display:flex;align-items:center;gap:6px;flex-wrap:nowrap;" in html
    assert ".trader-env-auditline" in html
    assert ".trader-env-details:not([open]) .trader-env-detail-list{display:none}" in html
    assert "const auditLabel = [" in html
    assert 'class="trader-env-pill ok trader-ops-only"' in html
    assert 'class="trader-env-pill warn trader-ops-only"' in html
    assert 'class="trader-env-pill bad trader-ops-only"' in html
    assert '<summary title="${esc(auditLabel)}">${esc(textPair("原因","Reason"))}</summary>' in html
    assert "暂停新仓 · 只复盘，不加仓" in html
    assert "谨慎模式 · 先确认再开仓" in html
    assert "交易环境异常 · 不开新仓" not in html
    assert "环境明细" not in html
    assert "查看原因" not in html
    assert '<div class="trader-env-auditline">${esc(auditLabel)}</div>' in html
    assert '<a class="btn small trader-env-ops-link" href="ops-dashboard.html">${esc(textPair("打开运维面板","Open OPS board"))}</a>' in html
    assert '<a class="btn small" href="ops-dashboard.html">${esc(textPair("处理","Resolve"))}</a>' not in html
    assert "function strategyFilterPredicate" in html
    assert "strategyFilter:initialStrategyFilter()" in html
    assert 'data-strategy-filter' in html
    assert "今日成交" in html
    assert "Traded today" in html
    assert "低样本" in html
    assert "Low sample" in html
    assert "只靠浮盈" in html
    assert "Open PnL only" in html
    assert "已兑现亏损" in html
    assert "Realized loss" in html
    assert "低频失效" in html
    assert "Low frequency" in html
    assert "strategy-filter-chip" in html
    assert "function strategyActionQueueItems" in html
    assert "function renderStrategyActionQueue" in html
    assert "function strategyActionItemHtml" in html
    assert 'class="strategy-action-queue"' in html
    assert "strategy-action-panel" in html
    assert "strategy-action-list" in html
    assert "Priority Action Queue" in html
    assert "先处理最紧急策略；其余行动项在队列里展开。" in html
    assert "Start with the most urgent strategy; expand the queue for the rest." in html
    assert "primaryItems = items.slice(0,1)" in html
    assert "secondaryItems = items.slice(1)" in html
    assert "strategy-secondary-actions" in html
    assert "展开其余策略行动" in html
    assert "Expand remaining strategy actions" in html
    assert "data-strategy-action" in html
    assert ".strategy-action-item{border:1px solid rgba(255,255,255,.1);background:rgba(255,255,255,.026);border-radius:8px;padding:9px;display:grid;grid-template-rows:auto auto auto;gap:7px" in html
    assert ".strategy-action-reason{display:none}" in html
    assert ".strategy-action-facts{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:6px}" in html
    assert 'class="strategy-action-reason"' in html
    action_template = html[html.index("function strategyActionItemHtml"):html.index("function performanceReviewContext")]
    assert 'textPair("7日","7d")' not in action_template
    assert "strategy-performance-board" in html
    assert "strategy-performance-details" in html
    assert ".strategy-performance-details:not([open]) .strategy-performance-board{display:none}" in html
    assert "performance-table-scroll" in html
    assert "performance-row header" in html
    assert "策略表现总表" in html
    assert "Strategy Performance Table" in html
    assert "Scan samples, edge, realized PnL, and low-frequency status first" in html
    assert "data-performance-row" in html
    assert "data-performance-open" in html
    assert 'textPair("样本可信度","Sample trust")' in html
    assert 'textPair("今日","Today")' in html
    assert 'textPair("7日","7d")' in html
    assert 'textPair("回撤","DD")' in html
    assert "strategy-action-copy" in html
    assert "grid-template-columns:minmax(220px,.9fr)" in html
    assert ".decision-evidence,.strategy-action-copy{display:none}" in html
    assert "Real trades today; inspect Gold OHLC entries, exits, TP, and SL first." in html
    assert "Not enough real executions yet; watch/no-trade does not count as sample." in html
    assert "strategyDecisionRank(a)" in html
    assert "const STYLE_META" in html
    assert "function styleLabel" in html
    assert "range_breakout" in html
    assert "Range breakout" in html
    assert "grid_mean_reversion" in html
    assert "Grid mean reversion" in html
    assert "macd_golden_cross" in html
    assert "MACD 金叉" in html
    assert "macd_death_cross" in html
    assert "MACD 死叉" in html
    assert "fibonacci_rejection" in html
    assert "Fibonacci resistance rejection" in html
    assert "pullback_long" in html
    assert "Pullback long" in html
    assert 'data-strategy-open' in html
    assert "openMultiTimeframeReplayForStrategy(card.dataset.strategyAction)" in html
    assert "openMultiTimeframeReplayForStrategy(open.dataset.strategyOpen)" in html
    assert "看K线+病历" in html
    assert 'id="loopVerdict"' in html
    assert 'id="dailyLoopWorkbench"' in html
    assert 'id="loopClosureLedger"' in html
    assert html.index('id="loopClosureLedger"') < html.index('id="dailyLoopWorkbench"')
    assert 'id="loopSourceDetails"' in html
    assert "Sources / 来源" in html
    assert "Detailed source snapshot" not in html
    assert "详细来源摘要" not in html
    assert ".loop-source-details" in html
    assert ".loop-source-body" in html
    assert "今日闭环判定" in html
    assert "Daily Loop Verdict" in html
    assert ".loop-verdict-facts{grid-column:1 / -1;display:flex;align-items:center;gap:7px;flex-wrap:nowrap;overflow:hidden;min-width:0}" in html
    assert ".loop-verdict-fact{min-width:0;max-width:260px" in html
    assert ".loop-verdict-summary{display:none}" in html
    assert "const facts = [" in html
    assert "textPair(\"盘中\",\"Run\")" in html
    assert "textPair(`${executed} 笔真实成交`, `${executed} real trades`)" in html
    assert "loop-verdict-primary" in html
    assert "loopExceptionCallout(planException)" not in html
    assert "闭环明细" in html
    assert "Loop detail" in html
    assert "展开计划、执行、复盘、明日假设" in html
    assert "Open plan, execution, review, and tomorrow hypothesis" in html
    assert "今天有 ${executedCount} 笔真实成交，先复盘成交策略" not in html
    assert "早盘是不交易计划，但盘中 ${planException.tradedNames} 有 ${executedCount} 笔真实成交" not in html
    assert "闭环四步详情" not in html
    assert "Four-step loop detail" not in html
    assert "默认收起，PM 行动队列优先。" not in html
    assert "Collapsed by default; PM action queue comes first." not in html
    assert "loop-workbench-details" in html
    assert ".loop-workbench{margin:0 0 12px;padding:0;border:0;background:transparent;box-shadow:none;display:block}" in html
    assert ".loop-workbench-details.pm-compact" in html
    assert ".loop-workbench-details.pm-compact > summary b" in html
    assert ".loop-workbench-details.pm-compact > summary small" in html
    assert ".loop-workbench-details:not([open]) .loop-story-grid{display:none}" in html
    assert "Review misses traded strategy" in html
    assert "watch / no-trade does not count as a real trade sample" in html
    assert "data-loop-workbench-replay" in html
    assert "data-loop-ledger-scroll" in html
    assert "data-loop-event-replay" in html
    assert "data-loop-exception-replay" in html
    assert "data-loop-exception-ledger" in html
    assert "function loopClosureDiagnosis" in html
    assert "PM diagnosis" in html
    assert "Plan/execution exception" in html
    assert "Signal-to-ticket funnel" in html
    assert "Risk block" in html
    assert "Plan did not trigger" in html
    assert "Ticket did not execute" in html
    assert "Market no-signal" in html
    assert ".loop-ledger-diagnosis" in html
    assert "window.goldbotCharts.loopExceptionActions" in html
    assert "function openLatestReplayForStrategy" in html
    assert "function renderDailyLoopWorkbench" in html
    assert "function loopStoryStep" in html
    assert "function replaceStrategyIds" in html
    assert "闭环账本" in html
    assert "Closure Ledger" in html
    assert "PM 行动队列" in html
    assert "PM Action Queue" in html
    assert "默认只看最需要处理的闭环断点；逐策略完整账本只在排查原因时展开。" in html
    assert "Default to the loop gaps that need action; expand the full per-strategy ledger only for diagnosis." in html
    assert ".loop-ledger .loop-ledger-copy{display:none}" in html
    assert ".loop-action-card > p{display:none}" in html
    assert "grid-template-columns:minmax(0,1.15fr) minmax(0,.9fr) auto" in html
    assert ".loop-action-next{border-top:0;border-left:1px solid rgba(255,255,255,.075);padding-top:0;padding-left:10px" in html
    assert "loop-action-grid" in html
    assert "const prioritizedRows = openIssues ? rows.filter(row => row.tone !== \"ok\") : rows" in html
    assert "const actionRows = prioritizedRows.slice(0,1)" in html
    assert "loop-secondary-details" in html
    assert "展开其余行动项" in html
    assert "function loopActionCard" in html
    assert "loop-ledger-details" in html
    assert "展开逐策略闭环账本" in html
    assert "Expand full per-strategy closure ledger" in html
    assert ".loop-ledger-details:not([open]) .loop-ledger-grid{display:none}" in html
    assert "function renderLoopClosureLedger" in html
    assert "function loopClosureRows" in html
    assert "function loopClosureRowCard" in html
    assert "function loopClosureReviewContext" in html
    assert "function openLoopClosureReplay" in html
    assert "function loopClosureNextAction" in html
    assert "data-loop-replay" in html
    assert "data-loop-review-replay" in html
    assert "openLoopClosureReplay(row.dataset.loopReplay" in html
    assert "await openLoopClosureReplay(id, rowById.get(id) || {})" in html
    assert 'reviewSource:daily.strategy_id ? "strategy_daily_reviews" : "closure_ledger"' in html
    assert "Open review replay" in html
    assert "Add evening review for executed strategy" in html
    assert "计划和执行分叉，需要复盘解释" in html
    assert "Plan and execution split; review must explain it" in html
    assert "function renderLoopVerdict" in html
    assert "function eveningReviewState" in html
    assert "function strategyDailyReviewCoverage" in html
    assert "function strategyDailyReviewCoverageText" in html
    assert "function strategyDailyReviewCoversId" in html
    assert 'reviewSource:dailyReviewed ? "strategy_daily_reviews"' in html
    assert "Covered by strategy review" in html
    assert "Explain plan/execution exception" in html
    assert "By-strategy review covers traded strategies" in html
    assert "Today's traded strategies are covered by the by-strategy review" in html
    assert "outputs/strategy_daily_reviews/current.json" in html
    assert "逐策略晚盘复盘记录" in html
    assert "function loopExecutionEventCard" in html
    assert "Review file exists but summary is empty" in html
    assert "Review misses traded strategy" in html
    assert "Evening review misses today's real trade" in html
    assert "Review summary missing" in html
    assert "function eveningCoverageText" in html
    assert "function executionReasonText" in html
    assert "watch/no-trade does not count as sample" in html
    assert "Strategy guardrails blocked new paper exposure." in html
    assert "Directional signal did not become an order request." in html
    assert "function levelNameText" in html
    assert "EMA50均线" in html
    assert "EMA50 average" in html
    assert 'real ${executedCount === 1 ? "trade" : "trades"}' in html
    assert "NAV versus Gold / NAV versus 黄金" in html
    assert "Start with the calibration strategy NAV versus Gold from launch to now" in html
    assert "review entries, TP, and SL in multi-timeframe replay" in html
    assert "Default view prioritizes strategies that really traded today" not in html
    assert "NAV versus Gold / NAV versus 黄金" in html
    assert 'id="navMtmBtn"' in html
    assert 'navMode:"indexed"' in html
    assert "function focusedDashboardPayload" in html
    assert "function focusStrategyPayload" in html
    assert "single-strategy-focus" in html
    assert "有信号，但没达到下单标准" in html
    assert "comparable path checkpoints" in html
    assert "function mtmExcessData" in html
    assert "function mtmComparison" in html
    assert "function navDisplayComparison" in html
    assert "gold_ohlc_cadence_mtm_excess" in html
    assert "Gold-cadence comparison" in html
    assert "comparable window" in html
    assert "function mtmGoldCadenceSamples" in html
    assert "function navChartTitle" in html
    assert "Gold=0% line" in html
    assert "Common-window trade path" in html
    assert "样本窗口" in html
    assert "Strategy line = common-window replayed equity index" in html
    assert "5m_mark_to_market" in html
    assert "Gold = thin solid 0% baseline" in html
    assert "Choose up to 3 strategies; click a line, end label, or selected strategy to open the trade index." in html
    assert "Vs / 对比" in html
    assert "Raw audit / 原始(排查)" in html
    assert "function navTailLabel" in html
    assert "function navLegendLabel" in html
    assert "function selectedNavPillTitle" in html
    assert "function compactNavTailLabel" in html
    assert "function navTailLabelOffset" in html
    assert "5m ADX" in html
    assert 'name:navLegendLabel(r.strategy_id, textPair("路径","Path"))' in html
    assert "name:navLegendLabel(r.strategy_id," in html
    assert 'const seriesId = String(p.seriesId || "")' in html
    assert "const fullName = rows().some(row => row.strategy_id === seriesId) ? shortName(seriesId) : \"\"" in html
    assert 'markPoint:navEndLabel(data, navTailLabel(r.strategy_id), color, {index:i,total:selected.length,fullLabel:shortName(r.strategy_id)})' in html
    assert 'title="${esc(selectedNavPillTitle(r.strategy_id, summaryText))}"' in html
    assert "<b>${esc(navTailLabel(r.strategy_id))}</b>" in html
    assert "offset:labelOffset" in html
    assert 'id="navIndexedBtn" class="btn small trader-ops-only"' in html
    assert ".trader-ops-only{display:none!important}" in html
    assert '$("navMeta").title = `${modeLabel} · ${cadence}`' in html
    assert '$("navMeta").innerHTML = `<span class="dot"></span>${esc(textPair(`${selected.length} 条策略`, `${selected.length} strategies`))}`' in html
    assert '${esc(traderModeLabel)}' not in html
    assert 'id="selectedSummary"' in html
    assert 'id="comparePresetBar"' in html
    assert 'id="navInsightDetails" class="nav-insight-details"' in html
    assert '<summary><span data-zh="策略明细" data-en="Strategy details">策略明细</span><em id="navInsightSummary"' in html
    assert ".nav-insight-details:not([open]) .nav-insight-body{display:none}" in html
    assert ".nav-insight-body .compare-rationale{margin:0 0 8px}" in html
    assert 'id="compareRationale"' in html
    assert '<div id="navDecisionBoard" class="nav-decision-board"></div>\n            <details id="navMethodDetails" class="nav-method-details">' in html
    assert "function updateNavInsightSummary" in html
    assert "updateNavInsightSummary(selected)" in html
    assert "choose strategies" in html
    assert 'const CALIBRATION_STRATEGY_ID = "gold_1m_macd"' in html
    assert 'comparePreset:"calibration"' in html
    assert 'return initialUrlParam("strategy") || initialUrlParam("strategy_id") || CALIBRATION_STRATEGY_ID' in html
    assert "function calibrationRow" in html
    assert "function traderScopeRows" in html
    assert "function calibrationModeActive" in html
    assert 'id:"calibration"' in html
    assert 'label:textPair("校正策略","Calibration")' in html
    assert "过去开仓最多的 MACD 动量策略" in html
    assert 'item.row.strategy_id === CALIBRATION_STRATEGY_ID' in html
    assert "new Set([CALIBRATION_STRATEGY_ID])" in html
    assert "function traderPriorityItems" in html
    assert "function defaultTraderFocusId" in html
    assert "window.goldbotCharts.traderDefaultFocus" in html
    assert "function comparePresetOptions" in html
    assert "function comparePresetRows" in html
    assert "function renderComparePresets" in html
    assert "function applyComparePreset" in html
    assert "window.goldbotCharts.comparePreset" in html
    assert "Screens who is making money" in html
    assert "Only strategies with real executions today" in html
    assert "Prioritizes strategies with closed outcomes" in html
    assert "watch/no-trade is not counted" in html
    assert 'id="traderFocus"' in html
    assert 'data-layout="pm-first-overview"' in html
    assert 'data-priority="chart-before-controls"' in html
    assert 'id="traderCommandStrip"' in html
    assert 'id="traderDecisionDeck"' in html
    assert 'id="dashboardEvidenceDrawer" class="dashboard-secondary-drawer"' in html
    assert 'data-zh="系统诊断" data-en="System diagnostics">系统诊断' in html
    assert "更多证据 / 平台诊断" not in html
    assert ".dashboard-secondary-drawer" in html
    assert ".dashboard-drawer-body" in html
    assert "function renderTraderDecisionDeck" in html
    assert "function promotionDeckCopy" in html
    assert "function deckCard" in html
    assert "function deckToneLabel" in html
    assert "window.goldbotCharts.traderDecisionDeck" in html
    assert "data-deck-replay" in html
    assert "data-deck-view" in html
    assert "Trade permission" in html
    assert "Real execution" in html
    assert "Replay priority" in html
    assert "Promotion gate" in html
    assert "Judge trade markers on Gold OHLC, not on NAV." in html
    assert "Promotion needs samples, execution-grade OHLC, and loop coverage." in html
    assert ".trader-decision-deck" in html
    assert ".deck-card" in html
    assert "function renderTodayBrief" in html
    assert "function briefCard" in html
    assert "function briefCompactSummary" in html
    assert "function evidenceGapTitle" in html
    assert "function evidenceGapBody" in html
    assert "decision-brief" in html
    assert "primary-brief" in html
    assert "compact-brief" in html
    assert ".brief-compact" in html
    assert "el.innerHTML = briefCompactSummary(primaryCards)" in html
    assert "extra.innerHTML = cards.map(briefCard).join(\"\")" in html
    assert "今日重点" in html
    assert "Today focus" in html
    assert "闸门状态" in html
    assert "Gate status" in html
    assert "function compactStrategyNames" in html
    assert "compactStrategyNames(traded, 2)" in html
    assert '`${head} 等 ${names.length} 个`' in html
    assert "先复盘今日真实成交" in html
    assert "解释今天为什么没成交" in html
    assert "先处理执行/风险阻断" in html
    assert "交易闸门可用" in html
    assert 'id="todayDetailDrawer" class="today-detail-drawer"' in html
    assert 'id="todayBriefExtra" class="brief-list today-brief-extra"' in html
    assert 'id="todayDetailCount"' in html
    assert 'data-zh="今日指令细节" data-en="Command details">今日指令细节' in html
    assert ".today-detail-drawer" in html
    assert ".today-detail-body" in html
    assert "const primaryCards = cards.slice(0,2)" in html
    assert "const detailCards = cards.slice(2)" in html
    assert "brief-card" in html
    assert "Today stance" in html
    assert "Pause new exposure" in html
    assert "Trading allowed, not promotion" in html
    assert "Overview defaults to performance vs Gold; judge entries/exits in multi-timeframe replay." in html
    assert "Repair signal/order trace" in html
    assert "Verify signal-to-order handling" in html
    assert "complete records before promotion" in html
    assert 'id="navTruthStrip"' in html
    assert 'class="overview-stage"' in html
    assert 'class="grid hero-grid overview-metrics"' in html
    assert ".overview-metrics .metric" in html
    assert ".system-health-strip{margin-bottom:10px" in html
    assert ".system-health-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px}" in html
    assert "系统健康" in html
    assert "系统正常：数据在进，策略在跑" in html
    assert "data_feed" in html
    assert "runner_liveness" in html
    assert "strategy_evaluation" in html
    assert "tp_sl_coverage" in html
    assert "execution_blocker" in html
    assert '<details class="command-brief">' not in html
    assert "Do first / avoid / why paused" not in html
    assert ".overview-stage .support-copy{display:none}" in html
    assert ".overview-stage .main-grid{grid-template-columns:minmax(0,1.66fr) minmax(330px,.56fr);align-items:start}" in html
    assert ".overview-stage #navChart{height:300px}" in html
    assert ".overview-stage .action-board{display:none}" in html
    assert 'id="portfolioBook" class="card portfolio-book"' in html
    assert 'id="overviewStrategyBoard" class="card overview-strategy-board"' in html
    assert ".portfolio-book{display:grid;gap:10px;align-content:start;min-height:100%}" in html
    assert ".overview-strategy-row{display:grid;grid-template-columns:minmax(210px,1.2fr)" in html
    assert "function renderPortfolioBook" in html
    assert "function renderOverviewStrategyBoard" in html
    assert "window.goldbotCharts.portfolioBook" in html
    assert "window.goldbotCharts.overviewStrategyBoard" in html
    assert "当前持仓" in html
    assert "策略运行概况" in html
    assert "运行时间" in html
    assert "总交易" in html
    assert "年化回报" in html
    assert "阶段性表现" in html
    assert "3天" in html
    assert "7天" in html
    assert "30天" in html
    assert ".overview-stage .action-lane-head{display:none}" in html
    assert ".overview-stage .action-lane-title p{display:none}" in html
    assert ".overview-stage .action-primary-copy p{display:none}" in html
    assert ".overview-stage .action-lane-body{grid-template-columns:minmax(260px,1.15fr) minmax(0,1.85fr);gap:6px}" in html
    assert ".overview-stage .action-primary-copy b{font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}" in html
    assert 'aside class="card overview-brief-strip"' in html
    assert ".overview-brief-strip{display:none;padding:0;overflow:hidden}" in html
    assert 'id="todaySummaryDrawer" class="today-summary-drawer"' in html
    assert ".today-summary-drawer > summary{list-style:none;cursor:pointer;display:grid;grid-template-columns:auto minmax(0,1fr) auto;align-items:center;gap:8px;padding:7px 10px;min-height:34px}" in html
    assert ".today-summary-drawer > summary::after{content:\"展开\"" in html
    assert ".today-summary-drawer[open] > summary::after{content:\"收起\"}" in html
    assert ".today-summary-drawer > summary #decisionChip{display:none}" in html
    assert "#view-overview > #todayReviewQueueDrawer{display:none}" in html
    assert "#view-overview > #dashboardEvidenceDrawer{display:none}" in html
    assert ".summary-oneline{font-style:normal;color:var(--muted);font-size:11px;line-height:1.25;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}" in html
    assert "todaySummaryOneLine" in html
    assert "`${pmTodoTitle} · ${executed} 笔真实成交 · ${gateBlocked ? \"暂停新增\" : \"可评估新机会\"}`" in html
    assert ".overview-brief-strip .brief-compact{grid-template-columns:minmax(170px,.36fr) minmax(0,.58fr) minmax(220px,.42fr) minmax(170px,.32fr);align-items:center;gap:8px 10px;padding:8px 10px}" in html
    assert ".chart{height:300px}" in html
    assert html.index('data-layout="pm-first-overview"') < html.index('id="traderCommandStrip"') < html.index('class="overview-stage"') < html.index('id="portfolioBook"') < html.index('id="overviewStrategyBoard"') < html.index('id="todayReviewQueueDrawer"') < html.index('id="todayReviewQueue"') < html.index('id="dashboardEvidenceDrawer"')
    assert html.index('data-priority="chart-before-controls"') < html.index('id="portfolioBook"') < html.index('id="todayActionBoard"') < html.index('aside class="card overview-brief-strip"')
    assert html.index('id="dashboardEvidenceDrawer"') < html.index('id="traderDecisionDeck"') < html.index('id="heroMetrics"') < html.index('id="promotionDossier"') < html.index('id="realizedEvidenceBoard"')
    assert html.index('data-priority="chart-before-controls"') < html.index('class="compare-toolbar"')
    assert "function renderTraderCommandStrip" in html
    assert "function systemHealthModel" in html
    assert "function vitalCardModel" in html
    assert "function systemVitalCard" in html
    assert "每轮 runner 读取最新黄金 K 线后评估当前策略" in html
    assert "K线新鲜" in html
    assert "Runner 正常" in html
    assert "策略已评估" in html
    assert "无硬阻断" in html
    assert "function commandBlockers" in html
    assert "function marketBlockerLabels" in html
    assert "function marketBlockerDetail" in html
    assert "function commandVisibleBlockers" in html
    assert "function commandPauseDetail" in html
    assert "function todayTradedRows" in html
    assert "function commandReviewScope" in html
    assert "function commandFirstAction" in html
    assert "function commandDoNot" in html
    assert "function commandLoopSummary" in html
    assert 'data-command-mtf' not in html
    assert "打开多周期+病历" not in html
    assert "function openMultiTimeframeReplayForStrategy" in html
    assert "multiTimeframeReplayUrl(detail, {strategy:id, cursor, preserveRunDate, ...replayOptions, ...mergedReviewContext})" in html
    assert "const strategy = options.strategy || app.selectedStrategy" in html
    assert 'id="todayReviewQueueDrawer" class="overview-secondary-drawer"' in html
    assert '<summary><span data-zh="待复盘事项" data-en="Review queue">待复盘事项</span><em id="todayReviewQueueSummary"' in html
    assert 'id="todayReviewQueue" class="review-queue"' in html
    assert ".overview-secondary-drawer" in html
    assert ".overview-secondary-drawer:not([open]) .overview-secondary-body{display:none}" in html
    assert ".overview-secondary-body .review-queue{margin-bottom:0;border:0;background:transparent;box-shadow:none;padding:0}" in html
    assert ".dashboard-secondary-drawer:not([open]) .dashboard-drawer-body" in html
    assert ".nav-method-details:not([open]) .nav-method-body{display:none}" in html
    assert ".today-detail-drawer:not([open]) .today-detail-body{display:none}" in html
    assert "function updateTodayReviewQueueSummary" in html
    assert "updateTodayReviewQueueSummary(items, urgent)" in html
    assert "waiting for real trades" in html
    assert 'data-command-replay' not in html
    assert 'data-command-view="strategies"' not in html
    assert 'data-command-view="loop"' not in html
    assert "function traderEnvNoExposureVisible" in html
    assert "function traderCommandHeadline" in html
    assert "function commandVisiblePauseReason" in html
    assert "function traderCommandModeChip" in html
    assert "function traderCommandCopy" in html
    assert "const model = systemHealthModel(pb, gate)" in html
    assert "el.className = `card system-health-strip ${toneClass(model.tone)}`" in html
    assert "function strategyFrequencySummary" in html
    assert "function commandFrequencyLimiter" in html
    assert "function commandNoTradeFallbackReason" in html
    assert "风控预算/仓位上限" in html
    assert "当前没有可执行的新仓票" in html
    assert "不开新仓：${reason.zh}" in html
    assert "No new exposure: ${reason.en}" in html
    assert "vitalCardModel(\"data_feed\")" in html
    assert "vitalCardModel(\"runner_liveness\")" in html
    assert "vitalCardModel(\"strategy_evaluation\")" in html
    assert "vitalCardModel(\"tp_sl_coverage\")" in html
    assert "vitalCardModel(\"execution_blocker\")" in html
    assert "复盘模式" in html
    assert "Review mode" in html
    assert "真实成交优先看；观察/不交易不计样本。先打开多周期和交易病历。" in html
    assert "Review real executions first; watch/no-trade is not a sample. Open MTF and trade cards first." in html
    assert "systemVitalCard(item)" in html
    assert "不开新仓，先看真实成交" in html
    assert "No new exposure; inspect real executions first" in html
    assert "Review ${reviewScope.en} first" not in html
    assert "function commandStrategyLabel" in html
    assert "const primaryName = commandStrategyLabel(primaryRow?.strategy_id || \"\")" in html
    assert "const names = traded.slice(0,2).map(row => commandStrategyLabel(row.strategy_id))" in html
    assert "const name = commandStrategyLabel(strategyId)" in html
    assert "title:commandStrategyLabel(row.strategy_id)" in html
    assert "const tradedZh = traded.slice(0,3).map(row => navTailLabel(row.strategy_id)).join(\" · \")" in html
    assert "new exposure paused" in html
    assert "根异常长K线" not in html
    assert "异常K线" not in html
    assert "abnormal candles" not in html
    assert "wide_ohlc_bars" not in html
    assert "Binance USDM 行情已接入；暂停新增不是因为缺行情" in html
    assert "K线质量待复核" not in html
    assert "先打开 ${name} 最新成交，再检查其余成交策略" in html
    assert "then inspect the other traded strategies" in html
    assert "不要新增仓位或放松执行规则" in html
    assert "Do not add exposure or loosen execution rules" in html
    assert 'id="todayActionBoard"' in html
    assert 'id="promotionDossier"' in html
    assert 'id="todayReviewQueue"' in html
    assert 'id="realizedEvidenceBoard"' in html
    assert "晋级复核包" in html
    assert "Promotion Review" in html
    assert "function renderPromotionDossier" in html
    assert "function promotionGateCards" in html
    assert "function promotionCandidateItems" in html
    assert "window.goldbotCharts.promotionDossier" in html
    assert "Answers one question: can this strategy enter manual promotion review" in html
    assert "this review packet does not change demo/live gates" in html
    assert "OHLC quality pending" not in html
    assert "rank includes open PnL" in html
    assert "trade records are incomplete" in html
    assert "data-promotion-replay" in html
    assert 'data-promotion-view="strategies"' in html
    assert 'data-promotion-view="loop"' in html
    assert "已平仓表现" in html
    assert "Closed Outcomes" in html
    assert "function renderRealizedEvidenceBoard" in html
    assert "function realizedEvidence" in html
    assert "function realizedActionCopy" in html
    assert "Separate closed outcomes from open mark-to-market" in html
    assert "Open PnL only" in html
    assert "Closed outcomes / 已平仓表现" in html
    assert "待复盘事项" in html
    assert "Review queue" in html
    assert "Start here: review real trades first" in html
    assert "function renderTodayReviewQueue" in html
    assert "function todayReviewQueueItems" in html
    assert "function reviewQueueCard" in html
    assert "data-review-replay" in html
    assert "await openMultiTimeframeReplayForStrategy(target.dataset.reviewReplay, target.dataset.reviewCursor || \"\", {" in html
    assert "reviewQuestion: target.dataset.reviewQuestion" in html
    assert "reviewSource: \"today_review_queue\"" in html
    assert "function strategyDailyReviewById" in html
    assert "dailyReview.review_priority" in html
    assert "dailyReview.pm_summary" in html
    assert "data-review-cursor" in html
    assert "function replayDateFromCursor" in html
    assert "const date = options.date || (options.preserveRunDate ? fallbackDate : (options.explicitDashboardDateOnly ? \"\" : replayDateFromCursor(cursor))) || fallbackDate" in html
    assert 'data-review-replay="${esc(strategyId)}"${cursorAttr}' in html
    assert "data-review-loop>${esc(textPair(\"看闭环\",\"Daily loop\"))}" in html
    assert "data-review-strategy=\"${esc(strategyId)}\"" in html
    assert "打开K线+病历" in html
    assert "data-review-loop" in html
    assert "data-review-strategy" in html
    assert "openMultiTimeframeReplayForStrategy(event.currentTarget.dataset.realizedReplay)" in html
    assert "openMultiTimeframeReplayForStrategy(btn.dataset.shortlistReplay)" in html
    assert "复盘成交" in html
    assert "Review trade" in html
    assert "信号未下单" in html
    assert "Signal no order" in html
    assert "风控拦截" in html
    assert "Risk blocked" in html
    assert "低频失效" in html
    assert "Low-frequency" in html
    assert 'textPair("下一步","Next")' in html
    assert "先复盘最新真实成交" in html
    assert "Review the latest real trade first" in html
    assert ".action-lane{display:grid;gap:10px}" in html
    assert ".action-lane-title p{margin:0;color:var(--muted);font-size:11px;line-height:1.32;overflow:hidden;display:-webkit-box;-webkit-line-clamp:1" in html
    assert ".action-lane-body{display:grid;grid-template-columns:minmax(0,.9fr) minmax(420px,1.1fr)" in html
    assert ".action-next-list{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:7px}" in html
    assert ".action-next-list{grid-template-columns:1fr}" in html
    assert ".action-primary-card" in html
    assert ".action-primary-card.status-only" in html
    assert ".action-mini-card" in html
    assert ".action-mini-status" in html
    assert "function renderTodayActionBoard" in html
    assert "function actionLaneItems" in html
    assert "function actionLaneTitle" in html
    assert "function actionPrimaryCard" in html
    assert "function actionMiniCard" in html
    assert "function actionMiniStatus" in html
    assert "function tradePermissionAction" in html
    assert "function sampleAction" in html
    assert "function actionLimitLabel" in html
    assert "function replayAction" in html
    assert "function promotionAction" in html
    assert "key:\"sample\"" in html
    assert "key:\"replay\"" in html
    assert "key:\"promotion\"" in html
    assert "function actionCard" not in html
    assert "action-step-index" not in html
    assert "今日样本" in html
    assert "Today sample" in html
    assert "inactive / low sample" in html
    assert 'data-overview-replay' not in html
    assert "function actionButtonAttrs" not in html
    assert "action-primary-card status-only" in html
    assert 'data-overview-view' not in html
    assert "Open latest replay" in html
    assert "Do not promote today" in html
    assert "交易者焦点" in html
    assert "Trader Focus" in html
    assert 'id="todayShortlist"' in html
    assert "今日优先看" in html
    assert "Today Shortlist" in html
    assert "为什么看" in html
    assert "Why watch" in html
    assert "为什么不能晋级" in html
    assert "Why not promote" in html
    assert "function traderShortlist" in html
    assert "function promotionBlockers" in html
    assert "function watchReason" in html
    assert "const shortlistIds = traderShortlist()" in html
    assert 'data-shortlist-replay' in html
    assert "聚焦最新成交" in html
    assert "Focus latest trade" in html
    assert "打开交易索引" in html
    assert "Open trade index" in html
    assert "候选，不是已验证赢家" in html
    assert "Candidate, not a winner yet" in html
    assert "function traderFocusVerdict" in html
    assert "function latestTrade" in html
    assert 'id="evidenceSummary"' in html
    assert 'id="replayEvidenceDetails"' in html
    assert "Review packet / 复盘材料" in html
    assert "Review evidence / 复盘证据" not in html
    assert "Evidence and audit / 证据与审计" not in html
    assert '<details id="replayEvidenceDetails"' in html
    assert "function renderEvidenceSummary" in html
    evidence_summary_body = html[html.index("function renderEvidenceSummary"):html.index("function evidenceSummaryChip")]
    assert "Record completeness" in evidence_summary_body
    assert "Evidence summary" not in evidence_summary_body
    assert "Technical details" in evidence_summary_body
    assert "Technical trace (collapsed)" not in evidence_summary_body
    assert "Replayable price path; records incomplete" in evidence_summary_body
    assert "Price is replayable; evidence is not promotion-ready" not in evidence_summary_body
    assert "rawGapsHidden:true" in html
    assert "Signal/order trace missing" in html
    assert "repair same-day signal, ticket, and order records" in html
    assert 'data-focus-latest' in html
    assert 'id="pickerToggle"' in html
    assert 'id="strategyPickerPanel"' in html
    assert 'id="comparePresetBar" class="compare-presets" aria-label="对比预设"' in html
    assert html.index('id="strategyPickerPanel"') < html.index('id="comparePresetBar"') < html.index('class="picker-help"')
    assert html.index('class="picker-help"') < html.index('id="selectedSummary"') < html.index('id="strategyPicker"')
    assert "Edit compare / 调整对比" in html
    assert "Choose up to 3 strategies; click a line, end label, or selected strategy to open the trade index." in html
    assert "Default view uses today’s actually traded strategies and Edge vs Gold" not in html
    assert "Switch to Trade Path for position-risk review" not in html
    assert "MTM pts" in html
    assert "selected strategy to open the trade index" in html
    assert "Gold OHLC Trade Replay / 黄金OHLC交易回放" not in html
    assert "Answer one question: did this trade's Entry / TP / SL match the price path? Extra evidence stays collapsed." not in html
    assert "只回答一件事：这笔交易的 Entry / TP / SL 和实际走势是否对得上。其他证据默认收起。" not in html
    assert "By default, only the current strategy is marked" not in html
    assert "manually overlay up to two strategies when comparing" not in html
    assert "The latest/focused trade draws an entry-to-exit/now path" not in html
    assert "TP/SL use short near-entry ticks, not full-width lines" not in html
    assert "#priceChart{height:100%;width:100%;overflow:hidden;contain:paint}" in html
    assert "#priceChart .tv-lightweight-charts{max-width:100%;overflow:hidden!important}" in html
    assert 'id="view-replay" class="panel replay-index-surface"' in html
    assert "Trade Index / 交易索引" in html
    assert "这里只负责选交易、看病历入口和跳转；完整 K 线复盘请打开多周期回放。" in html
    assert "Use this page only to pick a trade, open the trade-card entry point, and jump; open multi-timeframe replay for full OHLC review." in html
    assert ".replay-index-surface .section-title .support-copy{display:none}" in html
    assert 'id="tradeIndexTools" class="trade-tape-details trade-index-tools"' in html
    assert 'data-zh="筛选" data-en="Filters"' in html
    assert "Filters, compare, and jumps / 筛选、对比和跳转" not in html
    assert "filter strategy / 筛选策略" not in html
    trade_tools = html.index('id="tradeIndexTools"')
    strategy_select = html.index('id="strategySelect"', trade_tools)
    replay_shortcuts = html.index('id="replayShortcutDetails"', trade_tools)
    trade_grid = html.index('class="grid main-grid"', trade_tools)
    assert trade_tools < strategy_select < replay_shortcuts < trade_grid
    assert ".replay-index-surface .main-grid{grid-template-columns:minmax(0,1.18fr) minmax(420px,.42fr);align-items:start}" in html
    assert ".replay-index-surface .trade-index-tools summary{\n  min-height:30px;" in html
    assert ".replay-index-surface .trade-index-tools{" in html
    assert ".replay-index-surface .trade-index-toolbar{margin:0;align-items:center}" in html
    assert ".replay-index-surface .price-chart-wrap{\n  height:420px;" in html
    assert "pointer-events:auto;" in html
    assert ".replay-index-surface #priceOverlay{display:none}" in html
    assert "replay-index-card" in html
    assert "replay-workbench-card" in html
    assert ".replay-legend-item div{min-width:0}" in html
    assert "Entry, exit, take-profit, and stop-loss are marked on Gold OHLC, not on NAV." not in html
    assert "Clean display / 净化显示" in html
    assert "Raw OHLC / 原始K线" in html
    assert 'id="priceModeBanner"' in html
    assert "function renderPriceModeBanner" in html
    assert "window.goldbotCharts.priceModeBanner" in html
    assert "Gold OHLC clean display" in html
    assert "raw high/low is unchanged" in html
    assert "not a TradingView Lightweight Charts rendering error" in html
    assert "function replayTrades" in html
    assert "open_trades" in html
    assert "closed_trades" in html
    assert "function displayWideThresholdPct" in html
    assert ">0.5%" in html
    assert "displayWideBars" in html
    assert "raw data is unchanged" in html
    assert "function displayCandlesForReplay" in html
    assert "function aggregateCandles" in html
    assert "raw OHLC ->" in html
    assert "Current replay scope keeps the source timeframe by default" in html
    assert "displayScope" in html
    assert "displayCandlesForReplay(raw, scope)" in html
    assert "not a chart-rendering artifact" in html
    assert "function candleDisplayData" in html
    assert "displayClipped" in html
    assert "detailErrors" in html
    assert "Detail API error / 单策略接口错误" in html
    assert "Overlay compare / 叠加对比" in html
    assert "replayCompare:false" in html
    assert "function replayOverlayIds" in html
    assert "function ensureReplayOverlayDetails" in html
    assert "function latestReplayTradeKey" in html
    assert 'app.focusedTradeId = latestReplayTradeKey(detail)' in html
    assert 'app.replayScope = "trades"' in html
    assert 'app.replayScope = "focus"' in html
    assert "Click a trade to isolate its entry / exit / TP / SL" in html
    assert 'const markAllTrades = app.replayScope === "trades" || app.replayCompare' in html
    assert "const shouldShowMarker = markAllTrades || !app.focusedTradeId || isFocusedTrade" in html
    assert "当前只显示所选策略" in html
    assert "回到单策略清洁回放" in html
    assert 'id="navDecisionBoard"' in html
    assert 'id="navEdgeTape"' in html
    assert 'id="navInsightDetails"' in html
    assert 'id="navMethodDetails"' in html
    assert 'data-zh="图表说明" data-en="Chart guide">图表说明' in html
    assert "function renderNavEdgeTape" in html
    assert "nav-edge-summary" in html
    assert "nav-edge-summary-top" not in html[html.index("function renderNavEdgeTape"):html.index("function renderNavDecisionBoard")]
    assert "nav-edge-focus" not in html[html.index("function renderNavEdgeTape"):html.index("function renderNavDecisionBoard")]
    assert "nav-edge-detail-toggle" not in html
    assert "nav-edge-kpis" not in html
    assert "nav-edge-kpi" not in html
    assert ".nav-edge-tape{display:grid;grid-template-columns:1fr;gap:6px;margin:6px 0 6px}" in html
    assert ".nav-edge-summary{border:1px solid rgba(216,170,63,.22);background:linear-gradient(135deg,rgba(216,170,63,.075),rgba(122,162,255,.035)),rgba(255,255,255,.018);border-radius:8px;padding:6px 8px;display:grid;grid-template-columns:minmax(0,1fr);gap:4px;align-items:center;min-width:0}" in html
    assert ".nav-edge-summary-main p{margin:0;color:var(--muted);font-size:10px;line-height:1.22;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}" in html
    assert ".compare-toolbar{display:flex;align-items:center;justify-content:flex-end;gap:8px;margin:6px 0 6px;min-height:30px}" in html
    assert ".compare-toolbar-left{display:contents}" in html
    assert ".compare-preset span{display:none}" in html
    assert ".selected-pill span{display:none}" in html
    assert ".compare-presets,.selected-summary{flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none;padding-bottom:2px}" in html
    assert ".picker-panel .compare-presets{justify-content:flex-start;overflow-x:auto;margin:0 0 8px;padding-bottom:2px}" in html
    assert "读图" in html
    assert "Read" in html
    nav_edge_body = html[html.index("function renderNavEdgeTape"):html.index("function renderNavDecisionBoard")]
    assert "领先黄金" not in nav_edge_body
    assert "样本可信度" not in nav_edge_body
    assert "读图细节" not in html
    assert "Chart details" not in html
    assert "图表读法" not in nav_edge_body
    assert "Chart read" not in nav_edge_body
    assert "先复盘" not in nav_edge_body
    assert "Replay first" not in nav_edge_body
    assert "不要从点数多少判断策略" in nav_edge_body
    assert "do not judge by point counts" in nav_edge_body
    assert "window.goldbotCharts.navEdgeTape" in html
    assert "const leaderName = leader ? navTailLabel(leader.row.strategy_id) : textPair(\"暂无\",\"None\")" in html
    assert "const replayName = replay ? navTailLabel(replay.row.strategy_id) : textPair(\"无今日成交\",\"No trade today\")" in html
    assert "const trustName = weak ? navTailLabel(weak.row.strategy_id) : textPair(\"可读\",\"Readable\")" in html
    assert "黄金=0%基准" in nav_edge_body
    assert "Gold=0% baseline" in nav_edge_body
    assert "这里先看谁值得继续复盘" in html
    assert "data-edge-open" not in nav_edge_body
    assert 'role="button" tabindex="0"' not in nav_edge_body
    assert "replayButton || leaderButton || trustButton" not in html
    assert 'id="navQualityBoard"' in html
    assert html.index('id="navChart"') < html.index('id="navEdgeTape"') < html.index('class="compare-toolbar"') < html.index('id="todayActionBoard"') < html.index('id="navInsightDetails"') < html.index('id="compareRationale"') < html.index('id="navDecisionBoard"') < html.index('id="navMethodDetails"') < html.index('id="navReadout"') < html.index('id="navQualityBoard"')
    assert "nav-decision-board" in html
    assert "nav-quality-board" in html
    assert "nav-compare-ledger" in html
    assert "nav-compare-row" in html
    assert "NAV comparison ledger" in html
    assert 'textPair("未实现 PnL","Open PnL")' in html
    assert 'textPair("覆盖","Cover")' not in html[html.index("function renderNavDecisionBoard"):html.index("function updateNavInsightSummary")]
    assert 'textPair("下一步","Next")' in html
    assert "function renderNavDecisionBoard" in html
    assert "function navDecisionTone" in html
    assert "function navDecisionTrust" in html
    assert "function navDecisionAction" in html
    assert "function renderNavQualityBoard" in html
    assert "function navQualityFallback" in html
    assert "function navQualityCadenceLabel" in html
    assert "function mtmCurveFor" in html
    assert "function cleanMtmPoints" in html
    assert "function cleanMtmOverlapPoints" in html
    assert "function mtmCommonWindow" in html
    assert "function mtmDrawdownFor" in html
    assert "function mtmIndexData" in html
    assert "function mtmQualityFor" in html
    assert "function goldIndexBetween" in html
    assert 'data-nav-open' in html
    assert "跑赢黄金" in html
    assert "performance vs Gold" in html
    assert "重叠检查点不足" in html
    assert "Too few overlap checkpoints" in html
    assert "Equity checkpoints" in html
    assert "not a minute curve" in html
    assert "Performance vs Gold" in html
    assert "Trade path mode" in html
    assert "common overlap window" in html
    assert "comparable path checkpoints" in html
    assert "Gold and strategies index from that start" in html
    assert "Lines are trend-readable only with at least 4 overlap points" in html
    assert "Raw NAV is for data audit, not ranking." in html
    assert "Use replay first" in html
    assert 'id="navDiagnostic"' in html
    assert 'id="navVisualKey"' in html
    assert 'id="navReadout"' in html
    assert "function renderNavVisualKey" in html
    assert "function navTooltip" in html
    assert "function nearestValueInsideTimeline" in html
    assert "const close = nearestValueInsideTimeline(gold, t)" in html
    assert "Gold line=0% baseline" in html
    assert "Strategy checkpoints" in html
    assert "not trend lines" in html
    assert "Checkpoint comparison" in html
    assert 'symbol:sparse ? "emptyCircle" : "none"' in html
    assert 'textPair("图表口径","Chart basis")' in html
    assert 'textPair("数据间隔","Data cadence")' in html
    assert "navOverlapWindow" in html
    assert "goldIndexedAtStrategyCheckpoints" in html
    assert "overlap checkpoints" in html
    assert "Start/latest only; do not read as a trend" in html
    assert 'textPair("风险回报","R/R")' in html
    assert 'textPair("出场计划","Exit plan")' in html
    assert 'textPair("信号记录","Signal record")' in html
    assert 'textPair("图表聚焦","Chart focus")' in html
    assert '<details id="tradeFocusReadout" class="trade-tape-details trade-focus-readout"></details>' in html
    assert "trade-focus-body" in html
    assert ".trade-focus-readout:not([open]) .trade-focus-body{display:none}" in html
    assert 'textPair("标注","Markers")' in html
    assert "图上标注说明" not in html
    assert "Chart marker notes" not in html
    assert 'id="tradeReviewVerdict"' in html
    assert 'id="tradeWorkbench"' in html
    assert "Focused Trade Workbench / 单笔交易工作台" in html
    assert "data-workbench-strategies" in html
    assert "Open Strategy Board" in html
    assert "window.goldbotCharts.tradeWorkbenchActions" in html
    assert "function renderTradeWorkbench" in html
    assert "function workbenchStep" in html
    assert "trade-scorecard" in html
    assert "scorecard-grid" in html
    assert "trade-empty-state" in html
    assert ".trade-empty-state{min-height:220px;" in html
    assert "没有成交样本" in html
    assert "No trade sample" in html
    assert "当前策略今天没有可回放成交" in html
    assert "No replayable trade for this strategy today" in html
    assert "这页只服务逐笔复盘。有成交时才显示 K 线标注" not in html
    assert "This page is for trade-by-trade review. Once a trade exists" not in html
    assert "等待选择真实成交。" in html
    assert "Waiting for a real trade." in html
    assert "先选择一笔真实成交。右侧只显示入场、止盈、止损、盈亏和复盘动作" not in html
    assert "data-empty-open-strategies" in html
    assert 'activate("strategies")' in html
    assert "data-empty-open-mtf" in html
    assert "forceDateOnly:true" in html
    assert ".replay-index-card.no-trades #priceChartWrap" in html
    assert "function currentReplayTrades" in html
    assert "const trade = latestTrade(currentReplayTrades(detail || {}))" in html
    assert 'const cursor = cursorOverride || target.cursor || (target.kind === "empty" ? "" : latestDecisionReplayCursor(detail));' in html
    assert "const runDate = currentRunDate();" in html
    assert "function tradeScorecard" in html
    assert "function tradeScorecardHtml" in html
    assert "function tradeJournalSnapshot" in html
    assert "function tradeJournalSnapshotHtml" in html
    assert "function tradeCurrentOrExitPrice" in html
    assert "function priceDistancePct" in html
    assert "function compactTradeWorkbenchHtml" in html
    assert "function compactWorkbenchCard" in html
    assert "function tradeWorkbenchItemByLabel" in html
    assert "window.goldbotCharts.tradeJournalSnapshot" in html
    assert "Trade journal snapshot" in html
    assert "Price plan" in html
    assert "Records / loop" in html
    assert "Monitor TP/SL" in html
    assert "Trader next action" in html
    assert "交易员下一步" in html
    assert "Show review records" in html
    assert "展开复盘记录" in html
    assert "Show trade evidence details" not in html
    assert "展开交易证据细节" not in html
    assert "data-trade-action-panel" in html
    assert "data-trade-price-strip" in html
    assert "data-trader-action-grid" in html
    assert html.index('class="workbench-details"') < html.index('data-trader-action-grid')
    assert html.index("Show review records") < html.index('data-trader-action-grid')
    assert ".workbench-priority" in html
    assert ".workbench-action-grid" in html
    assert ".workbench-details" in html
    assert ".workbench-actions.primary" in html
    assert ".workbench-more-actions" in html
    assert ".workbench-more-actions:not([open]) .workbench-more-list{display:none}" in html
    assert "更多操作" in html
    assert "More actions" in html
    assert "function tradeRiskDollars" in html
    assert "function tradeRMultiple" in html
    assert "function tradeEvidenceProvenance" in html
    assert "window.goldbotCharts.tradeScorecard" in html
    assert "Plan complete" in html
    assert "R multiple" in html
    assert "Reviewable" in html
    assert "Complete records first" in html
    assert "Backfilled records" in html
    assert "Entry thesis" in html
    assert "Strategy signal" in html
    assert "Risk check" in html
    assert "Exit / position state" in html
    assert "data-workbench-focus" in html
    assert "data-workbench-4tf" in html
    assert "data-workbench-all" in html
    assert "data-workbench-loop" in html
    assert "Focus this trade" in html
    assert "Open OHLC + trade card" in html
    assert 'primary:"open_4tf", secondary:["focus","all","loop","strategies"]' in html
    assert "An open sample does not validate the strategy; wait for close." in html
    assert "trade-verdict" in html
    assert "Trade review" in html
    assert "交易复盘" in html
    assert "Trade review needs a trade" in html
    assert "复盘依据来自黄金K线、当前交易和每日闭环。" in html
    assert "Replay audit" not in html
    assert "回放审计" not in html
    assert "Gold OHLC chart and trade card" in html
    assert ".trade-verdict-details" in html
    assert ".trade-verdict-summary-copy" in html
    assert "function renderTradeReviewVerdict" in html
    assert "function tradeReplayVerdict" in html
    assert "function tradeLoopContextFor" in html
    assert "function tradeVerdictItem" in html
    assert "Complete review records first" in html
    assert "晚盘未覆盖" in html
    assert "Open Daily Loop" in html
    assert "data-open-daily-loop" in html
    assert "Watch/no-trade does not count as a trade sample" in html
    assert 'id="tradeTapeDetails"' in html
    assert 'id="tradeTapeCount"' in html
    assert "Other trades" in html
    assert "其他交易" in html
    assert ".trade-tape-details" in html
    assert "function forceExpandedDetails" in html
    assert "window.goldbotCharts.forceExpandedDetails" in html
    assert 'mode:"all_details_open"' in html
    assert "expandedDetailsObserver.observe(document.body" in html
    assert 'tapeDetails.dataset.attentionDefault = "expanded"' in html
    assert "tapeDetails.open = true" in html
    assert 'el.dataset.attentionDefault = "expanded"' in html
    assert 'el.open = true' in html
    assert 'tapeDetails.dataset.attentionDefault = "collapsed"' not in html
    assert "tapeDetails.open = false" not in html
    assert "tapeDetails.open = visibleTrades.length > 0" not in html
    assert "Review record note: use this only to switch trades" in html
    assert 'id="tradeMap"' in html
    assert "交易磁带" in html
    assert "Replay tape" in html
    assert 'id="replayScopeBar"' in html
    assert "function replayScopeOptions" in html
    assert "function replayScopeRange" in html
    assert "function renderReplayScopeBar" in html
    assert "function applyReplayVisibleRange" in html
    assert "window.goldbotCharts.replayVisibleRange" in html
    assert 'replayScope:"focus"' in html
    assert "selectedCount === 2" in html
    assert "app.replayCompare = true" in html
    assert "normalSeriesUseDots:false" in html
    assert "sparseSeriesUseDots:true" in html
    assert "window.goldbotCharts.navChartContract" in html
    assert "window.goldbotCharts.navDetailPreloadIds" in html
    assert "function strategyDisplayComparison" in html
    assert "Focus trade" in html
    assert "聚焦交易" in html
    assert "Trade day" in html
    assert "成交日" in html
    assert "All trades" in html
    assert "全部交易" in html
    assert "Full range" in html
    assert "全部K线" in html
    assert "function chronologicalTrades" in html
    assert "function tradeLabelMap" in html
    assert "function tradeToken" in html
    assert "function renderTradeMap" in html
    assert 'data-trade-map-focus' in html
    assert 'textPair("默认聚焦最新交易","Latest trade in focus")' in html
    assert "only this trade is marked on the chart" not in html
    assert "当前只显示这笔的图上标注" not in html
    assert "Click a trade to isolate its entry / exit / TP / SL" in html
    assert "trade-token" in html
    assert "window.goldbotCharts.priceMarkerText" in html
    assert "window.goldbotCharts.priceBadgeText" in html
    assert "window.goldbotCharts.pricePathText" in html
    assert "window.goldbotCharts.pricePathCount" in html
    assert "window.goldbotCharts.priceOverlayStrategyIds" in html
    assert "window.goldbotCharts.priceOverlayTradeCount" in html
    assert "window.goldbotCharts.priceOverlayMarkerCount" in html
    assert "window.goldbotCharts.replayOverlayRequestedIds" in html
    assert 'id="replayLegend"' in html
    assert "function renderReplayLegend" in html
    assert "window.goldbotCharts.replayLegend" in html
    assert "near-entry ticks" in html
    assert 'markerSurface:"Gold OHLC"' in html
    assert "markerLibrary:\"TradingView Lightweight Charts series markers plus near-entry HTML overlays\"" in html
    assert "entryMarkers:Number(counts.entries || 0)" in html
    assert "exitMarkers:Number(counts.exits || 0)" in html
    assert "takeProfitTicks:Number(counts.takeProfits || 0)" in html
    assert "stopLossTicks:Number(counts.stopLosses || 0)" in html
    assert "Entry, exit, TP, and SL markers are rendered on Gold OHLC, never on NAV." in html
    assert ".legend-marker.entry" in html
    assert "pricePathAnnotations" in html
    assert "function tradePathAnnotation" in html
    assert "function renderPricePathAnnotations" in html
    assert "price-path-layer" in html
    assert "trade-path-label" in html
    assert "price-path-guide" in html
    assert 'drawGuide(annotation.target, "tp")' in html
    assert 'drawGuide(annotation.stopLoss, "sl")' in html
    assert "trade-focus-readout" in html
    assert "function focusedTradeFrom" in html
    assert "function renderTradeFocusReadout" in html
    assert 'textPair("当前聚焦交易","Focused trade")' in html
    assert 'textPair("最新交易","Latest trade")' in html
    assert 'textPair("显示全部标注","Show all markers")' in html
    assert 'textPair("全部标注","All markers")' in html
    assert 'data-clear-trade-focus' in html
    assert "复盘记录提示：这里仅用于切换交易" in html
    assert "Review record note: use this only to switch trades" in html
    assert "Replay Verdict" not in html
    assert "晋级提示：交易回放可读" not in html
    assert "Promotion note: this replay is readable" not in html
    assert "Evidence note: evidence needs follow-up" not in html
    assert 'data-trade-focus' in html
    assert "function tradeKey" in html
    assert "focusedTradeId" in html
    assert 'textPair("排名可信度","Rank trust")' in html
    assert "观察收益" in html
    assert "Watch only" in html
    assert "Closed outcomes / 已平仓表现" in html
    assert 'textPair("同期黄金","Gold same window")' in html
    assert "function rankedStrategyItems" in html
    assert 'textPair("超额","Edge")' in html
    assert 'textPair("K线可信度","OHLC trust")' in html
    assert "function marketGateLabel" in html
    assert "function marketGateAction" in html
    assert "function marketBlockerText" in html
    assert "缺少可执行行情K线" in html
    assert "Execution-grade OHLC missing" in html
    assert "function providerCopy" in html
    assert "function truthLevelCopy" in html
    assert "function qualityFlagCopy" in html
    assert "function sourceSummaryText" in html
    assert "Binance USDM 行情" in html
    assert "公共代理行情" in html
    assert 'id="priceTrustDetails"' in html
    assert 'id="priceTrustPanel"' in html
    assert "OHLC quality / 行情质量" in html
    assert 'textPair("K线可信度","OHLC trust")' in html
    assert 'textPair("显示模式","Display mode")' in html
    assert 'textPair("K线形态","Candle shape")' in html
    assert "function candleShapeStats" in html
    assert "thin-body long wicks" in html
    assert 'textPair("交易者动作","Trader action")' in html
    assert "Loading strategy OHLC / 正在加载策略K线" in html
    assert "Replay only / 仅用于回放" in html
    assert "Paper/demo can continue; confirm account-level readiness before live promotion." in html
    assert 'href="ops-dashboard.html"' in html
    assert "navMarkers" not in html
    assert "Fewer than 4 overlap points are shown as dots, not a trend line" in html
    assert "excessVsGoldFromStrategyStart" in html
    assert "navComparison" in html
    assert "raw NAV audit; not for ranking" in html
    assert "Strategy points are not minute candles" in html
    assert "function renderNavTruthStrip" in html
    assert "function navTruthItem" in html
    assert "nav-truth-strip" in html
    assert 'id="navRuler"' in html
    assert "function renderNavRuler" in html
    assert "function medianGapMsForPoints" in html
    assert "window.goldbotCharts.navRuler" in html
    assert "window.goldbotCharts.navPointContract" in html
    assert "window.goldbotCharts.navTruth" in html
    assert "function navPointContract" in html
    assert "real trade path" in html
    assert "not broker account NAV" in html
    assert "common-window replayed equity index" in html
    assert "Gold=0% straight line" in html
    assert "Strategy value = strategy return minus same-window Gold return" in html
    assert "read dots, not trend" in html
    assert "function goldBaselineSeries" in html
    assert "solid_series" in html
    assert "window.goldbotCharts.navGoldBaselineData = data" in html
    assert "series.push(goldBaselineSeries(axisStart, axisEnd, goldPoints))" in html
    assert "lineStyle:{width:NAV_BASELINE_WIDTH,type:\"solid\",color:GOLD_COLOR,opacity:.78}" in html
    assert "function navEndLabel" in html
    assert "markPoint:navEndLabel" in html
    assert "function bindNavChartClick" in html
    assert "function handleNavChartClick" in html
    assert "strategy_series_to_gold_ohlc_replay" in html
    assert "navChart.off(\"click\", handleNavChartClick)" in html
    assert "openLatestReplayForStrategy(id)" in html
    assert "id:\"gold-baseline\"" in html
    assert "id:\"gold-index\"" in html
    assert "markLine" not in html
    assert "NAV_LINE_WIDTH" in html
    assert "NAV_BASELINE_WIDTH" in html
    assert "width:sparse ? 0 : NAV_LINE_WIDTH" in html
    assert "lineStyle:{width:NAV_MTM_LINE_WIDTH,type:\"solid\",color,opacity:.84}" in html
    assert html.count('query.set("view", "trader")') >= 2
    assert "await ensureDetailsForSelection();\n    renderOverview();" not in html
    assert "const GOLD_COLOR" in html
    assert "barQuality(bars, detail.ohlc_quality" in html
    assert "data/vendor/lightweight-charts.standalone.production.js" in html
    assert "LightweightCharts.CandlestickSeries" in html
    assert "LightweightCharts.createSeriesMarkers" in html
    assert "window.goldbotCharts.priceDisplayStats" in html
    assert "window.goldbotCharts.priceCandleContract" in html
    assert "function replaySourceSummaryText" in html
    assert "function replayCoverageText" in html
    assert "Market DB replay window" in html
    assert "covers ${Number(replayOhlc.trade_count)} trades" in html
    assert "replayOhlc:replayOhlc || {}" in html
    assert "trades have markers in this OHLC window" in html
    assert "priceOutOfRangeTradeCount" in html
    assert "priceOutOfRangeEventCount" in html
    assert "currentEntryVisible" in html
    assert "fallbackFocus" in html
    assert "target < candleMs[0] || target > candleMs[candleMs.length - 1]" in html
    assert "TradingView Lightweight Charts" in html
    assert "Only long wicks are clipped in the display layer; raw high/low is unchanged." in html
    assert "abnormal wicks" not in html
    assert "raw candles ->" in html
    assert "distributionClipped" in html
    assert "thresholdClipped" in html
    assert "distributionWideCount" in html
    assert "thinBodyWideCount" in html
    assert "Source vs display" in html
    assert "not a chart-rendering artifact" in html
    assert "thin-body long wicks come from source high/low" in html
    assert "barSpacing:7.2" in html
    assert "minBarSpacing:4" in html
    assert "rgba(45,212,191,.10)" in html
    assert "replayRangeFromIndices(candles, focusWindow, 55" in html
    assert "replayRangeFromIndices(candles, dayTradeIndices, 120" in html
    assert "replayRangeFromIndices(candles, allTradeIndices, 60" in html
    assert "Math.max(0, candles.length - 140)" in html
    assert "function markerEntryText" in html
    assert "function markerExitText" in html
    assert 'textPair("买入","Buy")' in html
    assert 'textPair("亏损出场","Exit loss")' in html
    assert "Buy 买" not in html
    assert "Short 空" not in html
    assert "Exit 盈" not in html
    assert 'id="priceOverlay"' in html
    assert "price-badge" in html
    assert "timeToCoordinate" in html
    assert "priceToCoordinate" in html
    assert "const clamp = (value,min,max)" in html
    assert "replayCompare" in html
    assert "Overlay ${overlayCount}" in html
    assert "replayOverlayIds().length" in html
    assert "selectedDetails.length === 1 || id === app.selectedStrategy" in html
    assert "compact-trade-card" in html
    assert "data-trade-open-4tf" in html
    assert "data-trade-focus-inline" in html
    assert "tapeDetails.open = visibleTrades.length > 0" not in html
    assert "Open OHLC + trade card" in html
    assert "Focus trade" in html
    assert "trade-tape-copy" in html
    assert "trade-tape-line" in html
    assert "trade-tape-footer" in html
    assert "trade-tape-actions" in html
    assert "function tradeDecisionSummary" in html
    assert "tradeDecisionStrip(decision)" in html
    assert "Trader decision" in html
    assert "Sample use" in html
    assert "Open trade: monitor only, do not validate yet" in html
    assert "Complete trade records" in html
    assert "trade-decision-grid" in html
    assert ".compact-trade-card .trade-meta{grid-template-columns:repeat(3,minmax(0,1fr))}" in html
    assert "function loopPlanException" in html
    assert "function loopExceptionCallout" in html
    assert "noTradeExecution = noTradePlan && executedCount > 0" in html
    assert "No-trade plan vs real execution; review the exception first" in html
    assert "Morning was a no-trade plan, but real trades occurred intraday" in html
    assert "Plan/execution exception" in html
    assert ".loop-exception" in html
    assert ".loop-exception-actions" in html
    assert "Replay ${displayName(id)}" in html
    assert "Open closure ledger" in html


def test_dashboard_v3_keeps_ops_debug_surfaces_out_of_visible_sections():
    html = (ROOT / "dashboard-v3.html").read_text(encoding="utf-8")

    forbidden_visible_copy = [
        "Explainability Gaps",
        "Trade Lifecycle",
        "missing_signal_artifact",
        "missing_ticket_artifact",
        "repair_same_day_signal_ticket_order_artifacts",
        "[object Object]",
        "supporting artifacts need OPS review",
        "审计缺口",
    ]
    for copy in forbidden_visible_copy:
        assert copy not in html

    assert "candidateText(hypo.best_candidate" in html
    assert "blockerListText(hypo.promotion_blockers" in html
    assert "String(hypo.promotion_blockers" not in html


def test_ops_dashboard_links_back_to_trader_console():
    html = (ROOT / "ops-dashboard.html").read_text(encoding="utf-8")

    assert 'href="dashboard-v3.html"' in html
    assert "Trader Console / 交易看板" in html
    assert "GoldBot OPS Console" in html
    assert "黄金运维控制台" in html
    assert "OPS Shift Brief" in html
    assert "运维值班简报" in html
    assert 'id="ops-lang-toggle"' in html
    assert 'data-ops-lang="zh"' in html
    assert 'data-ops-lang="en"' in html
    assert 'data-ops-lang="bi"' in html
    assert "goldbot_ops_lang" in html
    assert 'params.get(\'ops_lang\') || params.get(\'lang\')' in html
    assert "function opsText" in html
    assert "function setOpsLang" in html
    assert "function renderOpsStaticLabels" in html
    assert "function opsLocalizeMixed" in html
    assert "function opsLooksTechnicalValue" in html
    assert "function opsRowLabelText" in html
    assert "function opsRowValueText" in html
    assert "opsRowLabelText(left)" in html
    assert "opsRowValueText(mid)" in html
    assert "opsRowValueText(right)" in html
    assert "document.title = opsText('GoldBot OPS Console', '黄金运维控制台')" in html
    assert "function parseOpsBilingualPair" in html
    assert "function renderOpsSectionHeadings" in html
    assert "renderOpsSectionHeadings();" in html
    assert 'data-ops-label="latest_close"' in html
    assert 'id="ops-action-board"' in html
    assert 'id="ops-shift-brief"' in html
    assert 'id="backend-signals"' in html
    assert 'id="ops-runbook-strip"' in html
    assert 'id="ops-incident-queue"' in html
    assert html.count('id="data-source"') == 1
    assert "Backend Signals" in html
    assert "后台信号" in html
    assert "function renderBackendSignals" in html
    assert "function backendSignalsOpsAction" in html
    assert "window.goldbotOps.backendSignals" in html
    assert "alerts" in html
    assert "dataHealth" in html
    assert "liveReconciliation" in html
    assert "collectorRuns" in html
    assert "Can I trust automation?" in html
    assert "现在能信自动化吗？" in html
    assert "Highest blocker" in html
    assert "最高优先级阻断" in html
    assert "Safe next command" in html
    assert "下一条安全命令" in html
    assert "Keep surfaces separate" in html
    assert "保持界面分离" in html
    assert "function renderOpsShiftBrief" in html
    assert "window.goldbotOps.shiftBrief" in html
    assert "OPS runbook summary" in html
    assert "运维处置摘要" in html
    assert "Next operator action" in html
    assert "下一步操作" in html
    assert "function renderOpsRunbookStrip" in html
    assert "function renderOpsIncidentQueue" in html
    assert "function opsCommandBlock" in html
    assert "function copyOpsCommand" in html
    assert "data-copy-command" in html
    assert "Copy command" in html
    assert "复制命令" in html
    assert "Copied" in html
    assert "已复制" in html
    assert "window.goldbotOps.lastCopiedCommand" in html
    assert "window.goldbotOps.lastCopyStatus" in html
    assert ".ops-copy-command" in html
    assert ".ops-copy-status" in html
    assert "OPS Incident Queue" in html
    assert "运维事故队列" in html
    assert "Current blockers" in html
    assert "当前阻断" in html
    assert "Operator action" in html
    assert "运维动作" in html
    assert "window.goldbotOps.incidentQueue" in html
    assert "Runtime Snapshot / 运行快照" in html
    assert html.index('id="ops-action-board-panel"') < html.index("Runtime Snapshot / 运行快照")
    assert "Start here: public access, market feed, execution safety, and evidence repair." in html
    assert "function renderOpsActionBoard" in html
    assert "function publicAccessOpsAction" in html
    assert "function marketFeedOpsAction" in html
    assert "function opsBlockerLabel" in html
    assert "function opsBlockerText" in html
    assert "function opsStatusText" in html
    assert "function opsSignalText" in html
    assert "function opsTruthText" in html
    assert "function opsCheckNameText" in html
    assert "Market database" in html
    assert "市场数据库" in html
    assert "Live requests" in html
    assert "实盘请求" in html
    assert "Config hash" in html
    assert "配置哈希" in html
    assert "Strategy hash" in html
    assert "策略哈希" in html
    assert "Expectancy" in html
    assert "期望值" in html
    assert "Network call" in html
    assert "网络提交" in html
    assert "opsLooksTechnicalValue(text)" in html
    assert "Paper reconciliation" in html
    assert "纸面对账" in html
    assert "Live activation gate" in html
    assert "实盘激活门" in html
    assert "function opsHealthMessageText" in html
    assert "Paper reconciliation has not run" in html
    assert "纸面对账尚未运行" in html
    assert "当前为纸面模式；未使用实盘券商" in html
    assert "row(opsCheckNameText(check.name), opsStatusText(check.status), opsHealthMessageText(check.name, check.message)" in html
    assert "row(opsCheckNameText(check.name), opsStatusText(check.status)" in html
    assert "row(opsCheckNameText(item.name), opsStatusText(item.status)" in html
    assert "Execution venue feed" in html
    assert "执行场所行情" in html
    assert "No trade" in html
    assert "不交易" in html
    assert "$('signal-regime').textContent = opsSignalText" in html
    assert "$('data-source').textContent = opsProviderText" in html
    assert "$('data-truth').textContent = opsTruthText" in html
    assert "$('runner-state').textContent = opsStatusText" in html
    assert "$('health-state').textContent = opsStatusText" in html
    assert "function opsDiagnosisText" in html
    assert "公网访问正常" in html
    assert "function opsTunnelText" in html
    assert "function opsTunnelLogText" in html
    assert "Public probe is OK; latest tunnel log entry is historical noise." in html
    assert "公网探测正常；最新隧道日志属于历史噪声。" in html
    assert "row(opsText('Overall', '总体'), opsStatusText(health.status || 'unknown'), opsDiagnosisText(health.diagnosis || '--'), cls)" in html
    assert "row(opsText('Operator action', '运维动作'), opsActionText(health.operator_action || '--')" in html
    assert "Execution-grade OHLC missing" in html
    assert "缺少可执行行情K线" in html
    assert "Wide OHLC bars need review" not in html
    assert "存在异常长K线" not in html
    assert "OANDA credentials missing" not in html
    assert "OANDA 凭证缺失" not in html
    assert "opsBlockerText(blockers, 3)" in html
    assert "opsBlockerText(blockers, 6)" in html
    assert "blockers.join(', ') || 'none'" not in html
    assert "const traderLabel = opsLocalizeMixed" in html
    assert "const traderAction = opsLocalizeMixed" in html
    assert "function opsAuditSummaryText" in html
    assert "Mock trading UAT failed; local simulation loop evidence is incomplete" in html
    assert "function opsNextActionText" in html
    assert "opsNextActionText(item)" in html
    assert "opsAuditSummaryText(item.name, item.summary)" in html
    assert "function opsReviewItemText" in html
    assert "No trades executed today; review whether filters were too strict" in html
    assert "opsReviewItemText(queue[0]?.item || 'none')" in html
    assert "先修复系统健康检查错误，再运行新的纸面交易决策" in html
    assert html.count("command: opsNextActionText(next)") >= 2
    assert "function executionSafetyOpsAction" in html
    assert "function evidenceOpsAction" in html
    assert "function opsActionCard" in html
    assert "data-scroll-target" in html
    assert 'href="#${escapeHtml(item.target)}"' in html
    assert "target: 'ohlc-quality-gate'" in html
    assert "target: 'risk-monitor'" in html
    assert "target: 'doctor'" in html
    assert "Open public access" in html
    assert "打开公网检查" in html
    assert "Open OHLC gate" in html
    assert "打开K线质量门" in html
    assert "Open risk monitor" in html
    assert "打开风控监控" in html
    assert "Open doctor" in html
    assert "打开系统诊断" in html
    assert "System Health / 系统健康" in html
    assert "Public Access / 公网入口" in html
    assert "domain / Cloudflare tunnel" in html
    assert "api/public-access-health" in html
    assert "Full Diagnostics / 完整诊断" in html
    assert "view=ops" in html or "query.set('view', mode)" in html
    assert "ops_payload" in html
    assert "function renderPublicAccess" in html
    assert "Checking public domain and Cloudflare tunnel" in html
    assert "Deployment version" in html
    assert "部署版本" in html
    assert "function opsDeploymentStatusText" in html
    assert "function opsDeploymentFeatureText" in html
    assert "function opsDeploymentFeatureLabel" in html
    assert "public_deployment_stale" in html
    assert "公网部署不是最新版" in html
    assert "Solid Gold baseline" in html
    assert "黄金直线基准" in html
    assert "OPS command copy panel" in html
    assert "OPS 命令复制区" in html
    assert "deployment_features" in html
    assert "Data Quality Gate / 数据质量闸门" in html
    assert "OHLC Quality Gate / K线质量门" in html
    assert "official feed action / 官方行情动作" in html
    assert "renderOhlcQualityGate(marketDataGate, ohlcQuality)" in html
    assert "initializeRunDate()" in html
    assert 'value="2026-05-26"' not in html
    assert "Trade Lifecycle / 交易生命周期" in html
    assert "Risk Monitor / 风险监控" in html


def test_dashboard_v2_forwards_to_trader_console_v3():
    html = (ROOT / "dashboard-v2.html").read_text(encoding="utf-8")

    assert "dashboard-v3.html" in html
    assert "window.location.replace(next)" in html
    assert "Dashboard v2 now forwards to the trader-facing v3 console" in html
    assert "Explainability Gaps" not in html


def test_dashboard_v3_preserves_4tf_replay_cursor_context():
    html = (ROOT / "dashboard-v3.html").read_text(encoding="utf-8")

    assert 'id="replayCursorContext"' in html
    assert ".replay-cursor-context" in html
    assert "function initialReplayCursor" in html
    assert 'initialUrlParam("cursor") || initialUrlParam("replay_cursor")' in html
    assert "function initialReplaySource" in html
    assert 'replayCursor:initialReplayCursor()' in html
    assert 'replayReturnSource:initialReplaySource()' in html
    assert "function renderReplayCursorContext" in html
    assert "function clearReplayCursorContext" in html
    assert '["mtf_replay","4tf_replay"].includes(app.replayReturnSource)' in html
    assert "Current candle-decision point" in html
    assert "Continue MTF opens this decision candle" in html
    assert "data-replay-continue-4tf" in html
    assert "Continue MTF" in html
    assert "data-replay-open-loop" in html
    assert "Open loop ledger" in html
    assert 'activate("loop")' in html
    assert '$("loopClosureLedger")?.scrollIntoView' in html
    assert "data-replay-clear-cursor" in html
    assert "window.goldbotCharts.replayCursorContext" in html
    assert "function multiTimeframeReplayUrl(detail={}, options={})" in html
    assert 'url.searchParams.set("return_view", options.returnView || app.view || "overview")' in html
    assert 'url.searchParams.set("return_date", options.returnDate || fallbackDate || date || "")' in html
    assert 'url.searchParams.set("return_family", options.returnFamily || app.family || "all")' in html
    assert 'url.searchParams.set("return_strategy_filter", options.returnStrategyFilter || app.strategyFilter || "all")' in html
    assert "options.preferReplayCursor && app.replayCursor" in html
    assert "multiTimeframeReplayUrl(detail, {preferReplayCursor:true})" in html
    assert "options.cursor\n    || (options.preferReplayCursor && app.replayCursor)" in html
    assert "|| trade?.opened_at\n    || trade?.entry_at\n    || trade?.timestamp" in html


def test_dashboard_v3_uses_smart_replay_targets_for_trader_journey():
    html = (ROOT / "dashboard-v3.html").read_text(encoding="utf-8")

    assert 'id="replayTargetBar"' in html
    assert 'id="replayShortcutDetails"' in html
    assert "Quick jumps / 快速跳转" in html
    assert ".replay-target-bar" in html
    assert ".replay-target-btn" in html
    assert "function smartReplayTarget(detail={})" in html
    assert "function replayTargetOptions(detail={})" in html
    assert "function replayTargetById(detail={}, id=\"\")" in html
    assert "function applyReplayTarget(detail={}, target={}, options={})" in html
    assert "function renderReplayTargetBar(detail={})" in html
    assert "function smartReplayTargetCopy(target={})" in html
    assert "function openSmartReplayForStrategy(id, options={})" in html
    assert "latest_go_decision_snapshot" in html
    assert "latest_decision_snapshot" in html
    assert 'kind:"trade"' in html
    assert 'kind:"planned_go"' in html
    assert 'kind:"no_go"' in html
    assert "await openSmartReplayForStrategy(id)" in html
    assert "window.goldbotCharts.smartReplayTarget" in html
    assert "window.goldbotCharts.replayTargetBar" in html
    assert 'options.mode === "4tf"' in html
    assert "multiTimeframeReplayUrl(detail, {cursor:target.cursor" in html
    assert "renderReplay();" in html
    assert 'textPair("多周期回放","MTF replay")' in html


def test_dashboard_v3_performance_table_opens_replay_with_pm_context():
    html = (ROOT / "dashboard-v3.html").read_text(encoding="utf-8")

    assert "function performanceReviewContext(item={})" in html
    assert 'reviewSource:"strategy_performance"' in html
    assert 'reviewLabel:textPair("策略表现复盘","Strategy performance review")' in html
    assert "这个策略的表现是否可信？先核对" in html
    assert "Entry/TP/SL" in html
    assert "const itemById = new Map(items.map(item => [String(item.row?.strategy_id || \"\"), item]));" in html
    assert "performanceReviewContext(itemById.get(id) || {row:{strategy_id:id}})" in html
    assert "openMultiTimeframeReplayForStrategy(id, \"\", performanceReviewContext" in html


def test_dashboard_v3_embedded_replay_uses_standard_candle_unit_and_cursor_range():
    html = (ROOT / "dashboard-v3.html").read_text(encoding="utf-8")

    assert 'upColor:"rgba(0,0,0,0)"' in html
    assert "borderVisible:true" in html
    assert "borderUpColor" in html
    assert "borderDownColor" in html
    assert "window.goldbotCharts.priceCandleStyle" in html
    assert 'upFill:"hollow"' in html
    assert 'downFill:"solid"' in html
    assert '"EMA20","EMA50","EMA100","EMA200","MACD","RSI"' in html
    assert "function replayRangeFromCursor(candles, idxAt, cursor=\"\")" in html
    assert "const cursorRange = replayRangeFromCursor(candles, idxAt, app.replayCursor)" in html
    assert 'if(app.replayScope === "focus" && !app.focusedTradeId && !app.replayCursor)' in html
    assert "Decision cursor is outside current OHLC" in html
    assert "cursorRange.valid ? textPair" in html
    assert "聚焦逐根决策" in html
    assert "Use this candle to judge why the system chose GO / No-Go" in html


def test_dashboard_v3_v1_health_verdict_has_runtime_consistency_qa():
    html = (ROOT / "dashboard-v3.html").read_text(encoding="utf-8")

    assert "function v1HealthModel()" in html
    assert "function v1RunConsistencyQa()" in html
    assert "const health = v1HealthModel();" in html
    assert "const hardDown = health.hardDownRows;" in html
    assert "health.rows.map(row => v1VitalRow(row.label, row.value, row.tone)).join(\"\")" in html
    assert "visible_health_ok_but_verdict_system_error" in html
    assert "visible_health_down_but_verdict_not_system_error" in html
    assert "visible_vital_rows_do_not_match_health_model" in html
    assert "window.goldbotCharts.v1ConsistencyQa = result" in html
    assert "root.dataset.v1QaStatus = result.status" in html
    assert "页面自检失败：系统健康左右不一致" in html
    assert "v1RunConsistencyQa();" in html


def test_dashboard_v3_v1_trade_log_deduplicates_signal_labels():
    html = (ROOT / "dashboard-v3.html").read_text(encoding="utf-8")

    assert "function v1SignalLabel(value)" in html
    assert "function v1DedupSlashLabel(value)" in html
    assert "function v1SignalConceptKey(value)" in html
    assert 'return textPair("MACD 死叉", "MACD death cross")' in html
    assert 'return textPair("MACD 金叉", "MACD golden cross")' in html
    assert "v1SignalLabel(t.signal_regime || t.entry_reason || \"\")" in html
    assert "localizeMixed(translateCopy(t.signal_regime || t.entry_reason || \"\"))}</span>" not in html
