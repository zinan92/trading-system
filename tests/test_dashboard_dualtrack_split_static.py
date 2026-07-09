import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPLIT_PAGE = "dashboard-dualtrack-split.html"


WIDGETS = {
    "direction": "方向",
    "levels": "关键位",
    "signal": "信号",
    "kline": "K线图",
    "context": "大级别图",
    "order": "下单",
    "risk": "风险",
    "position": "持仓",
    "fills": "成交",
    "pnl": "收益",
    "data": "数据",
    "status": "状态",
    "review": "复盘",
}


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def read_split_html() -> str:
    path = ROOT / SPLIT_PAGE
    assert path.exists(), f"{SPLIT_PAGE} must exist as the parallel split-canvas page"
    return path.read_text(encoding="utf-8")


def canvas(html: str, track: str) -> str:
    pattern = (
        rf'<section class="canvas {track}"[^>]*data-track="{track}"[^>]*>'
        rf'(?P<body>.*?)</section>\s*<!-- /{track} canvas -->'
    )
    match = re.search(pattern, html, re.S)
    assert match, f"{track} canvas must be present and explicitly closed"
    return match.group("body")


def test_split_page_declares_two_canvases_and_exact_widget_registry():
    html = read_split_html()

    assert '<link rel="stylesheet" href="assets/tokens.css">' in html
    assert '<section class="canvas human" data-track="human"' in html
    assert '<section class="canvas machine" data-track="machine"' in html
    assert html.count('data-widget="phase"') == 2
    assert "Context图" not in html
    assert "balance" not in html

    for track in ("human", "machine"):
        body = canvas(html, track)
        for slug, label in WIDGETS.items():
            assert f'<span class="nm">{label}</span><span class="slug">{slug}</span>' in body
        assert body.count('<span class="nm">大级别图</span><span class="slug">context</span>') == 2
        assert len(re.findall(r'class="[^"]*\brv-sec\b[^"]*"', body)) == 3


def test_split_page_uses_standard_kline_and_split_read_contracts():
    html = read_split_html()

    assert "data/vendor/lightweight-charts.standalone.production.js" in html
    assert "packages/standard-kline/standard-kline.js" in html
    assert "StandardKline.StandardKlineChart" in html
    assert "data-standard-kline-host" in html
    assert "seedCandles(" not in html
    assert "<svg" not in html

    assert 'api("/api/dualtrack/cycle/current")' in html
    assert 'optionalApi(`/api/dualtrack/plan/${cycleId}`)' in html
    assert 'optionalApi(`/api/dualtrack/machine/${cycleId}`)' in html
    assert 'optionalApi(`/api/dualtrack/human/${cycleId}`)' in html
    assert 'optionalApi("/api/dualtrack/ledger")' in html
    assert 'optionalApi("/api/dualtrack/runtime/status")' in html
    assert 'BINANCE_MARKET_SYMBOL = "XAUUSDT"' in html
    assert 'BINANCE_REST_BASE = "https://fapi.binance.com"' in html
    assert 'BINANCE_WS_BASE = "wss://fstream.binance.com/stream?streams="' in html
    assert "async function fetchLiveBars" in html
    assert "function startMarketStream" in html
    assert "new WebSocket" in html
    assert "/api/dualtrack/market/bars?symbol=GOLD" not in html
    assert "binance_usdm_fallback" not in html
    assert "synthetic_fallback" not in html
    assert 'api("/api/dualtrack/plan"' in html
    assert 'api("/api/dualtrack/orders"' in html
    assert 'api("/api/dualtrack/verdict"' in html
    assert 'optionalApi("/api/dualtrack/config")' in html
    assert 'optionalApi(`/api/dualtrack/trades/${cycleId}?track=human${mark}`)' in html
    assert 'optionalApi(`/api/dualtrack/trades/${cycleId}?track=${track}${mark}`)' in html
    assert 'loadTradeData(cycleId, "machine")' in html
    assert "loadTradeData" in html
    assert "loadMachineTradesAfterClose" not in html
    assert 'source:"split_canvas"' in html


def test_split_page_10b_posts_real_payloads_and_visible_errors():
    html = read_split_html()

    assert ".catch(() => null)" not in html
    assert "async function submitOrder" in html
    for field in ("cycle_id:state.cycle?.cycle_id", "ts:new Date().toISOString()", "side:state.order.side", 'event:"entry"', "order_type:state.order.order_type", "price:Number(state.order.price)", "notional:Number(state.order.notional)", "sl:Number(state.order.sl)", "tp:Number(state.order.tp)"):
        assert field in html
    for field in ("direction:draft.direction", "range:{low:parseFloatOrNull(draft.rangeLow), high:parseFloatOrNull(draft.rangeHigh)}", "key_levels:parseKeyLevels(draft.keyLevels)", "invalidation,", "confidence:draft.confidence", 'source:"split_canvas"'):
        assert field in html
    assert "status.textContent = friendlyErrorMessage(error.message)" in html
    assert "setOrderStatus(friendlyErrorMessage(error.message)" in html
    assert "平仓数量超过当前人轨持仓" in html
    assert "裁决已归档" in html


def test_split_page_uses_strict_live_binance_feed_and_reports_blockers():
    html = read_split_html()

    assert "function binanceKlineToBar" in html
    assert "function emptyMarketPayload" in html
    assert "function buildMarketPayload" in html
    assert 'source_mode:"binance_usdm_live"' in html
    assert 'source_mode:"unavailable"' in html
    assert 'access_issues:[marketAccessIssue(issue)]' in html
    assert 'quality_flags:[source === "websocket" ? "binance_public_websocket" : "binance_public_rest", "binance_usdm_live", "xauusdt"]' in html
    assert '"binance_public_rest"' in html
    assert '"binance_public_websocket"' in html
    assert "mergeStreamBarIntoState" in html
    assert "handleMarketStreamMessage" in html
    assert "uses_public_exchange_feed:true" in html
    assert "marketStatusLabel" in html
    assert "marketIssueText" in html
    assert "行情不新鲜 · 禁止下单" in html


def test_split_page_10b_fail_closed_price_confirmbar_and_tpsl_contract():
    html = read_split_html()

    assert 'class="confirmbar hidden" data-confirmbar' in html
    assert "background:color-mix(in srgb,var(--panel) 56%,transparent)" in html
    assert "backdrop-filter:blur(2px)" in html
    assert "function latestHumanPrice" in html
    assert "function humanMarketFresh" in html
    assert "行情不新鲜 · 禁止下单" in html
    assert "function suggestTpSl" in html
    assert "side === \"sell\" ? {tp:low, sl:high} : {tp:high, sl:low}" in html
    assert "state.order.tp = null;" in html
    assert "做空: TP=前低 · SL=前高" in html
    assert "TP/SL到价本地平仓" in html
    assert "TP/SL 仅记录 · 不自动执行" not in html
    assert "data-tpsl-overlay" in html
    assert "data-tpsl-line=\"tp\"" in html
    assert "data-tpsl-line=\"sl\"" in html
    assert "function bindHumanChartOverlaySync" in html
    assert "function scheduleTpslOverlaySync" in html
    assert 'host.dataset.tpslOverlaySync = "bound"' in html
    assert "priceToY" in html
    assert "yToPrice" in html
    assert "scheduleLiveTradeRefresh()" in html
    assert "mark_price" in html


def test_split_page_10c_order_affordance_and_disabled_states():
    html = read_split_html()

    assert ".confirmbar .go{flex-shrink:0;white-space:nowrap" in html
    assert 'class="confirmbar-row confirmbar-main"' not in html
    assert 'class="confirmbar-row confirmbar-actions"' not in html
    assert "function compactUsd" in html
    assert 'preview.textContent = block || `${side} ${compactUsd(stats.notional)} · 保证金 ${compactUsd(stats.margin)} · 后 ${leverageRatioLabel(stats.afterLeverage)}`;' in html
    assert "min-height:34px" in html
    assert ".confirmbar-buttons{margin-left:0;flex-direction:column}" in html
    assert ".seg button[disabled]{opacity:.45;cursor:not-allowed}" in html
    assert 'button.disabled = locked || state.planPending' in html
    assert '$$("[data-order-side]").forEach(button => {' in html
    assert "button.disabled = Boolean(block) || state.order.pending" in html
    assert "const block = orderBlockReason();" in html


def test_split_page_removes_internal_task_labels_and_raw_runtime_keys():
    html = read_split_html()

    assert "09a" not in html
    assert "09b" not in html
    assert "machine_fills_hidden" not in html
    assert "成交明细 · 实时显示" in html
    assert "runtimeStatusMeta" in html
    assert "正常" in html
    assert "注意" in html
    assert "阻塞" in html
    assert "未连接" in html
    assert "未知" not in html
    assert "盲测" not in html


def test_split_page_main_chart_timeframe_and_indicators_are_operator_controls():
    html = read_split_html()

    assert 'data-main-tf="1m"' in html
    assert 'data-main-tf="5m"' in html
    assert "MAIN_TIMEFRAMES = [\"1m\", \"5m\"]" in html
    assert "dualtrack.split.main.tf" in html
    assert "setMainTf(button.dataset.mainTf)" in html
    assert "fetchLiveBars" in html
    assert "startMarketStream()" in html
    assert 'data-indicator="ema"' in html
    assert 'data-indicator="macd"' in html
    assert "DEFAULT_EMA_PERIODS = [20, 50]" in html
    assert "dualtrack.split.indicators.ema" in html
    assert "dualtrack.split.indicators.macd" in html
    assert "emaPeriods().map(period => ({period}))" in html
    assert "macd:{fast:12, slow:26, signal:9}" in html
    assert "indicators:isMainChart ? mainIndicators()" in html
    assert ".stage.macd-on{min-height:520px}" in html
    assert "GOLD ${tf} · ${suffix}" in html


def test_split_page_machine_signal_explains_actual_rule_not_layer_keys():
    html = read_split_html()

    assert "function machineSignalHtml" in html
    assert "function layerSignalText" in html
    assert "没有 MACD 背离、顶底分或小阳线触发器" in html
    assert "到关键位按网格规则补仓" in html
    assert "网格：关键位触发，已有机器成交。" in html
    assert "趋势腿：资格已开，允许顺势加仓。" in html
    assert "grid:traded / trend:armed" not in html


def test_split_page_reflows_charts_to_full_width_main_and_context_row():
    html = read_split_html()

    assert ".chartrow{display:flex;flex-direction:column" in html
    assert ".towers{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))" in html
    assert "grid-template-columns:7fr 3fr" not in html
    assert 'toolbar:options.compact ? {zoom:false} : {}' in html
    assert ".machine[data-real-phase=\"mid\"] [data-widget=\"kline\"] .stage{min-height:120px;height:120px}" not in html


def test_split_page_binds_tabs_and_actions_to_real_cycle_phase():
    html = read_split_html()

    assert "function deriveRealPhase" in html
    assert "function syncRealPhase" in html
    assert "function updateActionGates" in html
    assert "data-real-phase=\"mid\"" in html
    assert "syncRealPhase(true)" in html
    assert "syncRealPhase(false)" in html
    assert "非当前阶段" in html
    assert 'button.classList.toggle("is-real", button.dataset.phaseTarget === state.realPhase)' in html
    assert 'button.disabled = disabled' in html


def test_split_page_has_unclosed_review_and_empty_position_semantics():
    html = read_split_html()

    assert "本周期未收盘 · 复盘数据收盘后生成" in html
    assert '$$("[data-review-pending]")' in html
    assert '$$("[data-review-content]")' in html
    assert "无持仓" in html
    assert "已实现/未实现=本周期" in html
    assert '<button type="button" class="pill on" data-notional="10000">$10k</button><button type="button" class="pill" data-notional="20000">$20k</button><button type="button" class="pill" data-notional="50000">$50k</button><button type="button" class="pill" data-notional="100000">$100k</button>' in html
    assert "按钮=名义本金" in html
    assert "data-order-metrics" in html
    assert "名义本金" in html
    assert "占用保证金" in html
    assert "预估仓位" in html
    assert "本单实际杠杆" in html
    assert "成交后名义敞口" in html
    assert "成交后本轨杠杆" in html
    assert 'data-risk-reward-r' in html
    assert "function orderRiskReward" in html
    assert "reward / risk" in html
    assert 'host.addEventListener("standard-kline:viewchange", () => scheduleTpslOverlaySync(6), {passive:true})' in html
    assert "tpslOverlaySyncFrames" in html


def test_split_page_reveals_machine_mid_order_rows_and_removes_blind_path():
    html = read_split_html()

    assert "renderMachineFillsBlind" not in html
    assert "renderMachineBlindRisk" not in html
    assert "renderMachineOrderRows" in html
    assert 'loadTradeData(cycleId, "machine")' in html
    assert "data-blind-machine-mid" not in html
    for leaked_mockup_price in ("4,062.1", "4,066.3", "4,098.4"):
        assert leaked_mockup_price not in html


def test_split_page_review_grading_does_not_invent_levels_or_signal_scores():
    html = read_split_html()

    assert "判卷 · direction-only · 自评" in html
    assert 'data-grade-field="direction"' in html
    for field in ("levels", "signal"):
        row = re.search(
            rf'<div class="gr-row" data-grade-field="{field}">(?P<row>.*?)</div>',
            html,
            re.S,
        )
        assert row, f"{field} grading row must exist"
        assert "未建立评分口径" in row.group("row")
        assert "gpass" not in row.group("row")
        assert "gblock" not in row.group("row")


def test_split_page_risk_copy_keeps_simplified_liquidation_and_directional_loss_branch():
    html = read_split_html()

    assert "function renderRiskLossLabel" in html
    assert "function renderRiskWidget" in html
    assert "state.config?.max_leverage" in html
    assert "state.config?.capital_per_track_usd" in html
    assert "function trackNominalExposure" in html
    assert "function leverageRatio" in html
    assert "交易杠杆上限" in html
    assert "实际杠杆率" in html
    assert "名义持仓" in html
    assert "最大可亏(按SL)" in html
    assert "distance * units" in html
    assert "强平" not in html
    assert "renderLiquidationValue" not in html
    assert "loss-side" in html
    assert "neutral-side" in html
    risk_loss = re.search(
        r"function renderRiskLossLabel\(side, entry, stop, qty\)\{(?P<body>.*?)\n\}",
        html,
        re.S,
    )
    assert risk_loss, "risk loss helper must stay inspectable"
    assert "Math.abs" not in risk_loss.group("body")


def test_split_page_position_and_risk_prefer_trade_tpsl_over_plan_fallback():
    html = read_split_html()

    assert "function tradeTakeProfit" in html
    assert "function tradeStopPrice" in html
    assert "function entryFillForTrade" in html
    assert "trade?.tp, trade?.target, trade?.take_profit, entryFill?.tp" in html
    assert "trade?.sl, trade?.stop_loss, trade?.stop, entryFill?.sl" in html
    assert "const tp = tradeTakeProfit(track, trade, plan);" in html
    assert "const stop = Number(tradeStopPrice(track, trade, plan));" in html


def test_split_page_trade_rows_keep_realized_and_unrealized_mutually_exclusive():
    html = read_split_html()

    assert "function renderTradeRows" in html
    assert "function formatTradeTime" in html
    assert "function holdingDuration" in html
    assert "function tradeExitTs" in html
    assert "成交时间" in html
    assert "持仓时长" in html
    assert "开仓价" in html
    assert "平仓时间" in html
    assert "平仓价" in html
    assert "<th>操作</th>" in html
    assert "display_trades" in html
    assert "display_summary" in html
    assert "display_cycle_id" in html
    assert "data-close-trade" in html
    assert "async function closeHumanTrade" in html
    assert 'event:"exit"' in html
    assert 'order_type:"market"' in html
    assert 'source:"split_canvas_manual_close"' in html
    assert 'status === "closed" || hasRealized(trade) ? money(trade.realized_pnl) : "--"' in html
    assert 'status === "open" ? money(trade.unrealized_pnl) : "--"' in html
    assert 'class="fst open"' in html
    assert 'class="fst ${status}"' in html


def test_split_page_is_registered_in_shell_and_static_cache_exemptions():
    html = read("assets/shell.js")
    assert "dashboard-dualtrack-split.html" in html
    assert "双画布" in html

    from pipelines import dashboard_server

    handler = object.__new__(dashboard_server.DashboardHandler)
    handler.path = "/dashboard-dualtrack-split.html"

    assert handler._should_disable_static_cache() is True


def test_dualtrack_v5_files_stay_byte_clean_for_task_09():
    result = subprocess.run(
        [
            "git",
            "diff",
            "--exit-code",
            "--",
            "dashboard-dualtrack-v5.html",
            "tests/test_dashboard_dualtrack_static.py",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
