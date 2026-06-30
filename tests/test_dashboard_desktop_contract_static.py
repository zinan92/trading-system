from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_html(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def read_text(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_replay_is_desktop_first_trader_workbench_not_mobile_layout():
    html = read_html("dashboard-replay.html")

    assert "data/vendor/lightweight-charts.standalone.production.js" in html
    assert "data/vendor/echarts.min.js" not in html
    assert "body.layout-trader-focus{\n  min-width:1320px;" in html
    assert ".shell{width:min(1720px,100%);margin:0 auto;padding:18px 22px 36px;display:flex;flex-direction:column;position:relative}" in html
    assert "const DEFAULT_CHART_LAYOUT = \"trader\"" in html
    assert "const DEFAULT_PRIMARY_CHART_TF = \"1m\"" in html
    assert "const CHART_LAYOUT_VERSION = \"trader-focus-left-main-v3\"" in html

    assert "@media (max-width:720px)" not in html
    assert "body.layout-trader-focus .chart-grid{\n    grid-template-columns:1fr;" not in html
    assert "body.layout-trader-focus .controls{\n    width:100%;" not in html
    assert "body.layout-trader-focus .controls{\n  gap:6px;" in html
    assert "max-width:min(1040px,calc(100vw - 310px));" in html
    assert "overflow:hidden;" in html
    assert 'id="replaySourceControlsDrawer"' in html
    assert 'class="source-controls-drawer"' in html
    assert "body.layout-trader-focus .source-controls-drawer:not([open]) .source-controls-body{\n  display:none;" in html
    assert "body.layout-trader-focus .chart-grid{\n  grid-template-columns:minmax(0,2.15fr) repeat(2,minmax(210px,.62fr));" in html
    assert "grid-template-areas:\n    \"primary ctx-a ctx-b\"\n    \"primary ctx-c ctx-d\";" in html
    assert "height:clamp(660px, calc(100vh - 350px), 820px)" in html
    assert "body.layout-trader-focus .chart-grid .chart-card.primary-chart{\n  order:-1;\n  grid-area:primary;" in html
    assert "body.layout-trader-focus .chart-grid .chart-card.context-slot-0{grid-area:ctx-a}" in html
    assert "body.layout-trader-focus .chart-grid .chart-card.context-slot-1{grid-area:ctx-b}" in html
    assert "body.layout-trader-focus .chart-grid .chart-card.context-slot-2{grid-area:ctx-c}" in html
    assert "body.layout-trader-focus .chart-grid .chart-card.context-slot-3{grid-area:ctx-d}" in html
    assert "handleScroll:{mouseWheel:true,pressedMouseMove:true" in html
    assert "axisPressedMouseMove:{time:true,price:true}" in html
    assert "axisDoubleClickReset:{time:true,price:true}" in html
    assert "mouseWheel:true,\n      pinch:true" in html
    assert "function applyDesktopWheelZoom(tf, item, event)" in html
    assert "window.__lastDesktopWheelZoom" in html


def test_replay_hides_ops_and_redundant_details_from_desktop_first_paint():
    html = read_html("dashboard-replay.html")

    assert "body.layout-trader-focus .timeline .rail,\nbody.layout-trader-focus #geometryBadge,\nbody.layout-trader-focus #historyReplayBadge,\nbody.layout-trader-focus #boundaryStatus,\nbody.layout-trader-focus #hoverBadge,\nbody.layout-trader-focus #stepHint,\nbody.layout-trader-focus .timeline > .chip.info{\n  display:none;" in html
    assert "body.layout-trader-focus .asof-strip,\nbody.layout-trader-focus .shortcut-row,\nbody.layout-trader-focus .replay-contract,\nbody.layout-trader-focus .decision-clock-deck,\nbody.layout-trader-focus .pm-evidence-top,\nbody.layout-trader-focus .strategy-replay-brief,\nbody.layout-trader-focus .pm-evidence-drilldown,\nbody.layout-trader-focus .pm-evidence-focus,\nbody.layout-trader-focus .decision-gate-top,\nbody.layout-trader-focus .replay-hover-preview,\nbody.layout-trader-focus .selection-receipt,\nbody.layout-trader-focus .replay-event-rail,\nbody.layout-trader-focus .trade-lifecycle-strip,\nbody.layout-trader-focus .decision-pipeline{" in html
    assert "body.layout-trader-focus .replay-review-header{\n  display:none;\n}" in html
    assert "body.layout-trader-focus .replay-review-workspace{\n  position:relative;\n  grid-template-columns:minmax(0,1.52fr) minmax(360px,.82fr);" not in html
    assert "body.layout-trader-focus .trade-review-focus-grid{\n  grid-template-columns:minmax(0,1fr);" in html
    assert "body.layout-trader-focus .replay-review-workspace .replay-evidence-drawer > summary{\n  display:flex;" in html
    assert "body.layout-trader-focus .replay-review-workspace .replay-evidence-drawer:not([open])" in html
    assert 'id="replayTradeEvidenceDrawer" class="replay-secondary-drawer replay-evidence-drawer" open' not in html
    assert 'id="replayDecisionWorkbench" class="replay-decision-workbench empty"' in html
    assert "function renderReplayDecisionWorkbench" in html
    assert "no-trade-takeaway" in html
    assert "body.layout-trader-focus .no-trade-compact-grid{\n  display:none;" in html
    assert "grid-template-columns:96px 156px minmax(180px,.9fr) minmax(0,1.45fr);" in html
    assert 'class="trade-record-strip no-go-record"' not in html
    assert "<span>${esc(marketStatusText)}</span>" in html
    assert "<span>${esc(executionStatusText)}</span>" in html
    assert "<span>价格</span><b>${esc(priceText)}</b>" not in html
    assert "<span>方向</span><b>${esc(directionText)}</b>" not in html
    assert "body.layout-trader-focus .replay-review-workspace .replay-ops-drawer{\n  display:none;\n}" in html
    assert "body.layout-trader-focus .trade-review-head{\n  display:none;" in html
    assert "body.layout-trader-focus .trade-review-record .panel-head{\n  display:none;" in html
    assert "body.layout-trader-focus .no-trade-compact-head{\n  display:none;" in html
    assert "Entry / TP / SL · PM归因" not in html
    assert "PM / OPS / 同步细节" not in html


def test_trader_dashboard_keeps_pm_surface_separate_from_ops_noise():
    html = read_html("dashboard-v3.html")

    assert "GoldBot Trader Console / 黄金交易驾驶舱" in html
    assert 'href="ops-dashboard.html"' in html
    assert "Open OPS board" in html
    assert '>${esc(textPair("Open OPS","Open OPS"))}<' not in html
    assert 'class="overview-stage"' in html
    assert ".overview-stage .support-copy{display:none}" in html
    assert ".overview-stage .main-grid{grid-template-columns:minmax(0,1.66fr) minmax(330px,.56fr);align-items:start}" in html
    assert 'id="portfolioBook" class="card portfolio-book"' in html
    assert 'id="overviewStrategyBoard" class="card overview-strategy-board"' in html
    assert "renderPortfolioBook(pb);" in html
    assert "renderOverviewStrategyBoard();" in html
    assert ".overview-stage .action-board{display:none}" in html
    assert ".overview-stage .action-lane-head{display:none}" in html
    assert ".overview-stage .action-lane-title p{display:none}" in html
    assert ".overview-stage .action-primary-copy p{display:none}" in html
    assert ".overview-brief-strip{display:none;padding:0;overflow:hidden}" in html
    assert "#view-overview > #todayReviewQueueDrawer{display:none}" in html
    assert "#view-overview > #dashboardEvidenceDrawer{display:none}" in html

    assert 'id="strategyDiagnosticsDrawer"' in html
    assert ".strategy-diagnostics-drawer:not([open]) .strategy-diagnostics-body{display:none}" in html
    assert html.index('id="strategyPmPrescription"') < html.index('id="strategyGrid"') < html.index('id="strategyDiagnosticsDrawer"')
    assert html.index('id="strategyDiagnosticsDrawer"') < html.index('id="strategyQuickFilters"') < html.index('id="m4FrequencyAttributionBoard"')
    assert "loop-workbench-details pm-compact" in html
    assert ".loop-workbench-details:not([open]) .loop-story-grid{display:none}" in html


def test_replay_desktop_interaction_uat_script_covers_tradingview_like_controls():
    script = read_text("tools/verify_replay_desktop_interactions.mjs")

    assert "Mouse wheel zoom-out did not reach 500 visible candles" in script
    assert "Mouse wheel zoom-in did not reach about 50 visible candles" in script
    assert "Replay API prewarm failed before browser UAT" in script
    assert "bars.length > 0 && Boolean(range)" in script
    assert "lastDesktopWheelZoom" in script
    assert "native_wheel" in script
    assert "Hover source should be the 1m primary chart" in script
    assert "Not every timeframe card entered hover-synced state" in script
    assert "Crosshair sync did not map every timeframe to a concrete candle" in script
    assert "async function waitForReplayCursor" in script
    assert "state.loading === false && state.pendingLoad === false" in script
    assert "Next replay control did not advance exactly one 1m candle" in script
    assert "Clicking a 1m candle did not move replay cursor" in script
    assert "Future bars are not hidden after candle selection" in script
    assert "Primary 1m chart did not stop at the selected candle close" in script
    assert "A context timeframe displayed a future candle after 1m selection" in script
    assert "Context charts advanced as if every 1m tick closed every higher timeframe" in script
    assert "one_third_future_space" in script
    assert "Time axis drag did not change visible candle range" in script
    assert "lastTimeAxisDragZoom" in script
    assert "Price axis drag was not recorded" in script
    assert "lastPriceAxisDragZoom" in script
    assert "layout-trader-focus" in script
    assert "Expected left-primary + right 2x2 grid areas" in script
    assert "OPS drawer should be hidden on trader first paint" in script
    assert "Replay evidence details should stay collapsed on trader first paint" in script
    assert "Replay evidence detail body is crowding trader first paint" in script
    assert "Replay attribution checklist should not compete with the current candle card on first paint" in script
    assert "Replay first paint leaked OPS/diagnostic language" in script
    assert "Replay source controls should not be visible on trader first paint" in script
    assert "Replay source controls body is crowding the topbar" in script
    assert "Replay settings trigger should be icon-only on first paint" in script
    assert "Replay dashboard utility control should be icon-only on first paint" in script
    assert "Replay refresh utility control should be icon-only on first paint" in script
    assert "Replay dashboard utility control lost its accessible label" in script
    assert "Replay refresh utility control lost its accessible label" in script
    assert "Replay select-candle transport control should be icon-only on first paint" in script
    assert "Replay latest-signal transport control should be icon-only on first paint" in script
    assert "Replay latest-trade transport control should be icon-only on first paint" in script
    assert "Replay first paint leaked hidden strategy option text" in script
    assert '"市场上下文",' in script
    assert '"数据/策略",' in script
    assert "Replay first paint has too many tracked panels visible" in script
    assert "FIRST_PAINT_BANNED_TERMS" in script
    assert "visible_tracked_count" in script
    assert "Replay price charts are not using TradingView Lightweight Charts" in script
    assert "Replay price page should not load ECharts" in script
    assert "One or more Replay price charts rendered blank or without OHLC bars" in script
    assert "Replay expanded evidence still uses OPS-style English role labels" in script
    assert "Replay expanded evidence leaked backend English copy" in script
    assert "Replay expanded evidence is missing the current-candle decision group" in script
    assert "Replay expanded evidence is missing the signal/filter group" in script
    assert "Replay expanded evidence should not auto-open current-candle internals" in script
    assert "Replay expanded evidence should not auto-open raw trade tables" in script
    assert "Replay expanded evidence should not auto-open signal cross-section tables" in script
    assert "Replay closed raw evidence tables still occupy layout space" in script
    assert "nonblank_canvas_count" in script


def test_dashboard_v3_desktop_uat_script_covers_pm_first_paint_and_replay_cta():
    script = read_text("tools/verify_dashboard_v3_desktop.mjs")

    assert "Dashboard API prewarm failed before V3 browser UAT" in script
    assert "navChartContract" in script
    assert "NAV chart did not render enough real series" in script
    assert "NAV chart is missing the Gold benchmark" in script
    assert "Strategy details drawer should be collapsed on first paint" in script
    assert "System diagnostics drawer should not be visible on overview first paint" in script
    assert "Review queue drawer should not be visible on overview first paint" in script
    assert "Trader command strip should not duplicate the header replay CTA" in script
    assert "Trader command strip still shows a duplicate replay CTA" in script
    assert "Trader command strip leaked tab-duplicate actions" in script
    assert "Dashboard V3 refresh control should be icon-only on first paint" in script
    assert "Dashboard V3 refresh control lost its accessible label" in script
    assert "Today action board duplicates the command strip and should be hidden on overview first paint" in script
    assert "Today action board should not expose CTA buttons on first paint" in script
    assert "Today action board secondary cards should be status-only" in script
    assert "NAV edge summary should not show duplicate replay CTA buttons" in script
    assert "Dashboard V3 first paint leaked OPS/diagnostic language" in script
    assert "Dashboard V3 first paint has too many tracked panels visible" in script
    assert "Compare preset filters should stay inside the collapsed compare panel on first paint" in script
    assert "Compare preset filters are not available inside the compare panel" in script
    assert "Compare panel does not expose enough preset filters" in script
    assert "FIRST_PAINT_BANNED_TERMS" in script
    assert "visible_tracked_count" in script
    assert "Dashboard V3 NAV chart is not using ECharts runtime" in script
    assert "Dashboard V3 NAV chart did not create an ECharts instance" in script
    assert "Dashboard V3 NAV chart canvas is blank" in script
    assert "nonblank_canvas_count" in script
    assert "#headerReplayBtn" in script
    assert "Header replay CTA did not route to dashboard-replay.html" in script
    assert "Header replay CTA opened a 404/not-found page" in script
