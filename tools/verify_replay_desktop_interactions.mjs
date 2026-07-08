import fs from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const DEFAULT_URL =
  "http://127.0.0.1:8765/dashboard-replay-v4.html?date=2026-06-27&strategy=gold_1m_chan&cursor=2026-06-27T07%3A25%3A00%2B00%3A00";
const TIMEFRAME_MS = {
  "1m": 60_000,
  "5m": 5 * 60_000,
  "15m": 15 * 60_000,
  "4h": 4 * 60 * 60_000,
  "1d": 24 * 60 * 60_000,
};
const FIRST_PAINT_BANNED_TERMS = [
  "PM 证据检查台",
  "PM 证据详情",
  "M1-M5",
  "当前 K 线决策流水线",
  "回放边界",
  "周期收线同步",
  "关键交易事件导航",
  "交易生命周期导航",
  "系统证据",
  "同步 / 门控 / 原始链路",
  "市场上下文",
  "口述方向 · 多周期",
  "数据/策略",
];

function argValue(name, fallback = "") {
  const index = process.argv.indexOf(name);
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback;
}

function span(range) {
  return range ? Number(range.to) - Number(range.from) : null;
}

function assertPass(condition, message, evidence = {}) {
  if (!condition) {
    const error = new Error(message);
    error.evidence = evidence;
    throw error;
  }
}

function timeMs(value) {
  const ms = new Date(value || "").getTime();
  return Number.isFinite(ms) ? ms : NaN;
}

function candleEndMs(tf, row) {
  const explicit = timeMs(row?.bucket_end);
  if (Number.isFinite(explicit)) return explicit;
  const start = timeMs(row?.timestamp);
  return Number.isFinite(start) ? start + (TIMEFRAME_MS[tf] || 0) : NaN;
}

async function prewarmReplayApi(pageUrl) {
  const parsed = new URL(pageUrl);
  const apiUrl = new URL("/api/replay", parsed.origin);
  for (const key of ["date", "strategy", "cursor", "trade_id"]) {
    const value = parsed.searchParams.get(key);
    if (value) apiUrl.searchParams.set(key, value);
  }
  const controller = new AbortController();
  const startedAt = Date.now();
  const timer = setTimeout(() => controller.abort(), 30000);
  try {
    const response = await fetch(apiUrl, { signal: controller.signal });
    await response.arrayBuffer();
    return {
      url: apiUrl.toString(),
      status: response.status,
      ok: response.ok,
      duration_ms: Date.now() - startedAt,
    };
  } finally {
    clearTimeout(timer);
  }
}

async function visibleRange(page) {
  return await page.evaluate(() => window.replayDebug?.chartOption?.("1m")?.visibleLogicalRange || null);
}

async function priceRange(page) {
  return await page.evaluate(() => window.replayDebug?.chartOption?.("1m")?.priceVisibleRange || null);
}

async function currentCursor(page) {
  return await page.evaluate(() => window.replayDebug?.state?.()?.cursor || "");
}

function canvasSampleScript() {
  return `
    canvas => {
      try {
        const ctx = canvas.getContext("2d");
        if (!ctx) return { ok: false, reason: "no_context", width: canvas.width, height: canvas.height };
        const data = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
        let alpha = 0;
        let colored = 0;
        for (let i = 3; i < data.length; i += 16) {
          if (data[i] > 0) alpha += 1;
          if (data[i] > 0 && (data[i - 3] !== 0 || data[i - 2] !== 0 || data[i - 1] !== 0)) colored += 1;
        }
        return { ok: true, width: canvas.width, height: canvas.height, alpha_sample: alpha, colored_sample: colored };
      } catch (error) {
        return { ok: false, reason: error.message, width: canvas.width, height: canvas.height };
      }
    }
  `;
}

async function waitForReplayCursor(page, expectedCursor, timeout = 15000) {
  await page.waitForFunction(
    expected => {
      const state = window.replayDebug?.loadingState?.() || {};
      const cursor = window.replayDebug?.state?.()?.cursor || "";
      return cursor === expected && state.loading === false && state.pendingLoad === false;
    },
    expectedCursor,
    { timeout }
  );
}

async function run() {
  const url = argValue("--url", process.env.REPLAY_UAT_URL || DEFAULT_URL);
  const output = argValue("--output", "");
  const screenshot = argValue("--screenshot", "");
  const firstPaintScreenshot = argValue("--first-paint-screenshot", "");
  const chromeExecutable =
    process.env.CHROME_EXECUTABLE || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  const browser = await chromium.launch({
    headless: true,
    executablePath: chromeExecutable,
  });
  const page = await browser.newPage({ viewport: { width: 1728, height: 1050 }, deviceScaleFactor: 1 });
  const errors = [];
  page.on("console", msg => {
    if (msg.type() === "error") errors.push(msg.text());
  });
  page.on("pageerror", err => errors.push(err.message));

  const prewarm = await prewarmReplayApi(url);
  assertPass(prewarm.ok, "Replay API prewarm failed before browser UAT", { prewarm });
  await page.goto(url, { waitUntil: "domcontentloaded", timeout: 15000 });
  await page.waitForFunction(
    () => {
      const bars = window.replayDebug?.chartBars?.("1m") || [];
      const range = window.replayDebug?.chartOption?.("1m")?.visibleLogicalRange || null;
      return bars.length > 0 && Boolean(range);
    },
    null,
    { timeout: 30000 }
  );
  await page.waitForTimeout(400);

  const layout = await page.evaluate(() => {
    const grid = document.querySelector("#chartGrid");
    const primary = document.querySelector(".chart-card.primary-chart");
    const contexts = Array.from(document.querySelectorAll(".chart-card.context-chart"));
    const gridRect = grid?.getBoundingClientRect?.();
    const primaryRect = primary?.getBoundingClientRect?.();
    const style = grid ? getComputedStyle(grid) : null;
    return {
      title: document.title,
      bodyClass: document.body.className,
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
      gridAreas: style?.gridTemplateAreas || "",
      gridColumns: style?.gridTemplateColumns || "",
      gridHeight: gridRect ? Math.round(gridRect.height) : 0,
      primaryTimeframe: primary?.dataset?.timeframe || "",
      primaryWidth: primaryRect ? Math.round(primaryRect.width) : 0,
      primaryHeight: primaryRect ? Math.round(primaryRect.height) : 0,
      contextTimeframes: contexts.map(el => el.dataset.timeframe || ""),
      opsDrawerDisplay: getComputedStyle(document.querySelector("#replayOpsEvidenceDrawer")).display,
    };
  });

  const firstPaintSurface = await page.evaluate(bannedTerms => {
    const visible = selector => {
      const el = document.querySelector(selector);
      if (!el) return false;
      for (let node = el; node; node = node.parentElement) {
        if (node.tagName === "DETAILS" && !node.open) {
          const summary = node.querySelector(":scope > summary");
          if (!summary || !summary.contains(el)) return false;
        }
      }
      const rect = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
    };
    const trackedSelectors = [
      "#chartGrid",
      ".chart-card.primary-chart",
      ".chart-card.context-chart",
      "#replayDecisionWorkbench",
      "#replayReviewWorkspace",
      "#replayTradeEvidenceDrawer",
      "#replayMarketEvidenceDrawer",
      "#replayOpsEvidenceDrawer",
      ".replay-contract",
      ".decision-clock-deck",
      ".pm-evidence-top",
      ".strategy-replay-brief",
      ".pm-evidence-drilldown",
      ".pm-evidence-focus",
      ".decision-gate-top",
      ".decision-pipeline",
      ".selection-receipt",
      ".replay-event-rail",
      ".trade-lifecycle-strip",
    ];
    const tracked = trackedSelectors.map(selector => {
      const elements = Array.from(document.querySelectorAll(selector));
      return {
        selector,
        visible_count: elements.filter(el => {
          const rect = el.getBoundingClientRect();
          const style = getComputedStyle(el);
          return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0 && rect.top < window.innerHeight;
        }).length,
      };
    });
    const text = document.body.innerText.replace(/\s+/g, " ").trim();
    const visibleText = text.slice(0, 1600);
    const buttonText = selector => (document.querySelector(selector)?.innerText || "").replace(/\s+/g, " ").trim();
    const rectOf = selector => {
      const rect = document.querySelector(selector)?.getBoundingClientRect?.();
      return rect ? { x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height) } : null;
    };
    return {
      viewport: { width: window.innerWidth, height: window.innerHeight },
      visible_text: visibleText,
      utility_nav_text: buttonText("#backDashboardBtn"),
      utility_refresh_text: buttonText("#loadBtn"),
      utility_nav_label: document.querySelector("#backDashboardBtn")?.getAttribute("aria-label") || "",
      utility_refresh_label: document.querySelector("#loadBtn")?.getAttribute("aria-label") || "",
      select_candle_text: buttonText("#selectCandleBtn"),
      latest_signal_text: buttonText("#jumpLatestGoBtn"),
      latest_trade_text: buttonText("#jumpLatestTradeBtn"),
      chart_lens_toggle_visible: visible("#chartDecisionLensToggle"),
      trader_status_word_count: (visibleText.match(/不下单|拦截|可执行/g) || []).length,
      banned_terms_visible: bannedTerms.filter(term => visibleText.includes(term)),
      chart_grid_visible: visible("#chartGrid"),
      decision_workbench_visible: visible("#replayDecisionWorkbench"),
      decision_workbench_text: buttonText("#replayDecisionWorkbench"),
      trade_record_visible: visible("#tradeCard"),
      trade_record_head_visible: visible(".trade-review-record .panel-head"),
      trade_case_details_open: Boolean(document.querySelector("#tradeCaseDetails")?.open),
      trade_case_summary_text: buttonText("#tradeCaseDetails > summary"),
      trade_case_details_rect: rectOf("#tradeCaseDetails"),
      trade_case_summary_rect: rectOf("#tradeCaseDetails > summary"),
      no_trade_takeaway_rect: rectOf(".no-trade-takeaway"),
      no_go_record_visible: visible(".trade-record-strip.no-go-record"),
      trade_case_detail_body_visible: visible("#tradeCaseDetails .trade-case-detail-body"),
      no_trade_takeaway_visible: visible(".no-trade-takeaway"),
      no_trade_grid_visible: visible(".no-trade-compact-grid"),
      pm_attribution_visible: visible("#pmAttributionWorksheet"),
      pm_attribution_grid_visible: visible("#pmAttributionWorksheet .pm-attribution-grid"),
      market_context_visible: visible("#replayMarketEvidenceDrawer"),
      ops_drawer_visible: visible("#replayOpsEvidenceDrawer"),
      source_controls_visible: visible("#replaySourceControlsDrawer"),
      source_controls_body_visible: visible("#replaySourceControlsDrawer .source-controls-body"),
      visible_tracked_count: tracked.reduce((sum, item) => sum + item.visible_count, 0),
      tracked,
    };
  }, FIRST_PAINT_BANNED_TERMS);
  const chartRuntime = await page.evaluate(sampleFactorySource => {
    const sampleCanvas = eval(`(${sampleFactorySource})`);
    const tfs = ["1d", "4h", "15m", "5m", "1m"];
    const charts = Object.fromEntries(tfs.map(tf => {
      const canvases = Array.from(document.querySelectorAll(`#chart-${tf} canvas`));
      const samples = canvases.map(sampleCanvas);
      return [tf, {
        canvas_count: canvases.length,
        bars: (window.replayDebug?.chartBars?.(tf) || []).length,
        nonblank_canvas_count: samples.filter(item => Number(item.alpha_sample || 0) > 10).length,
        samples: samples.slice(0, 3),
      }];
    }));
    return {
      library: window.__lastReplayChartLibrary || "",
      has_lightweight_charts: Boolean(window.LightweightCharts),
      has_echarts: Boolean(window.echarts),
      charts,
    };
  }, canvasSampleScript());
  if (firstPaintScreenshot) {
    await page.screenshot({ path: firstPaintScreenshot, fullPage: false });
  }
  const expandedEvidence = await page.evaluate(() => {
    const evidenceDrawer = document.querySelector("#replayTradeEvidenceDrawer");
    if (evidenceDrawer) evidenceDrawer.open = true;
    const drawer = document.querySelector("#tradeCaseDetails");
    if (drawer) drawer.open = true;
    const visible = selector => {
      const el = document.querySelector(selector);
      if (!el) return false;
      for (let node = el; node; node = node.parentElement) {
        if (node.tagName === "DETAILS" && !node.open) {
          const summary = node.querySelector(":scope > summary");
          if (!summary || !summary.contains(el)) return false;
        }
      }
      const rect = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
    };
    const text = (document.querySelector("#tradeCaseDetails")?.innerText || "").replace(/\s+/g, " ").trim();
    const summaryText = (document.querySelector("#expandedTradeSummary")?.innerText || "").replace(/\s+/g, " ").trim();
    const closedRawTableLayoutCount = Array.from(document.querySelectorAll("#tradeCaseDetails .raw-evidence-disclosure:not([open]) table"))
      .filter(table => {
        const rect = table.getBoundingClientRect();
        const style = getComputedStyle(table);
        return style.display !== "none" && rect.width > 0 && rect.height > 0;
      }).length;
    const result = {
      parent_open: Boolean(evidenceDrawer?.open),
      open: Boolean(drawer?.open),
      text,
      summary_text: summaryText,
      summary_visible: visible("#expandedTradeSummary"),
      has_expanded_summary: summaryText.includes("完整病历摘要") && summaryText.includes("计划 / 保护单") && summaryText.includes("归因 / 下一步"),
      has_old_role_labels: /\bTRADER\b|\bEVIDENCE\b/.test(text),
      has_backend_english: /constructive gold setup|market view belongs to another run date|Signal-to-ticket triage/.test(text),
      has_current_candle_group: text.includes("信号关口") && text.includes("哪一关放行或拦截"),
      has_signal_filter_group: text.includes("技术过滤") && text.includes("指标、位置、风险"),
      repeated_decision_question: text.includes("本根为什么做/不做"),
      has_raw_table_labels: text.includes("原始成交表") || text.includes("原始指标表"),
      decision_detail_visible: visible("#replayDecisionDock") || visible("#decisionCard"),
      trade_table_visible: visible("#tradeTable"),
      snapshot_summary_visible: visible("#snapshotSummary"),
      snapshot_table_visible: visible("#snapshotTable"),
      closed_raw_table_layout_count: closedRawTableLayoutCount,
    };
    if (drawer) drawer.open = false;
    return result;
  });

  assertPass(errors.length === 0, "Replay page has console/page errors", { errors });
  assertPass(layout.bodyClass.includes("layout-trader-focus"), "Replay did not load desktop trader-focus layout", layout);
  assertPass(layout.primaryTimeframe === "1m", "Primary replay chart is not 1m", layout);
  assertPass(layout.contextTimeframes.length === 4, "Expected four context charts", layout);
  assertPass(layout.gridAreas.includes("primary ctx-a ctx-b"), "Expected left-primary + right 2x2 grid areas", layout);
  assertPass(layout.opsDrawerDisplay === "none", "OPS drawer should be hidden on trader first paint", layout);
  assertPass(firstPaintSurface.chart_grid_visible, "Replay chart grid is not visible on trader first paint", firstPaintSurface);
  assertPass(firstPaintSurface.decision_workbench_visible, "Replay current-candle decision workbench is not visible on trader first paint", firstPaintSurface);
  assertPass(/当前 K 线|执行计划|Entry|TP|SL|展开病历/.test(firstPaintSurface.decision_workbench_text || ""), "Replay decision workbench does not expose the Trader decision essentials", firstPaintSurface);
  assertPass(firstPaintSurface.trade_record_visible === false, "Replay full trade record should be behind the evidence drawer on first paint", firstPaintSurface);
  assertPass(firstPaintSurface.trade_record_head_visible === false, "Replay first paint still shows a duplicate inner review header", firstPaintSurface);
  assertPass(firstPaintSurface.no_trade_takeaway_visible === false, "Replay old current-candle takeaway should not compete with the decision workbench", firstPaintSurface);
  assertPass((firstPaintSurface.no_trade_takeaway_rect?.height || 0) <= 2, "Replay old current-candle takeaway still consumes first-paint layout", firstPaintSurface);
  assertPass(firstPaintSurface.no_go_record_visible === false, "Replay first paint still shows the old three-card No-Go field strip", firstPaintSurface);
  assertPass(firstPaintSurface.no_trade_grid_visible === false, "Replay first paint still shows the old two-card No-Go explanation grid", firstPaintSurface);
  assertPass(firstPaintSurface.trade_case_detail_body_visible === false, "Replay trade record should be collapsed on trader first paint", firstPaintSurface);
  assertPass(firstPaintSurface.trade_case_details_open === false, "Replay evidence details should stay collapsed on trader first paint", firstPaintSurface);
  assertPass(firstPaintSurface.trade_case_detail_body_visible === false, "Replay evidence detail body is crowding trader first paint", firstPaintSurface);
  assertPass((firstPaintSurface.trade_case_details_rect?.height || 0) <= 2, "Replay collapsed evidence entry still consumes a full layout row", firstPaintSurface);
  assertPass((firstPaintSurface.trade_case_summary_rect?.height || 0) <= 34, "Replay collapsed evidence entry should render as a compact pill", firstPaintSurface);
  assertPass(firstPaintSurface.pm_attribution_grid_visible === false, "Replay attribution checklist should not compete with the current candle card on first paint", firstPaintSurface);
  assertPass(firstPaintSurface.market_context_visible === false, "Replay market context handle should not compete on trader first paint", firstPaintSurface);
  assertPass(firstPaintSurface.ops_drawer_visible === false, "Replay OPS drawer is visible on trader first paint", firstPaintSurface);
  assertPass(firstPaintSurface.source_controls_visible === false, "Replay source controls should not be visible on trader first paint", firstPaintSurface);
  assertPass(firstPaintSurface.source_controls_body_visible === false, "Replay source controls body is crowding the topbar", firstPaintSurface);
  assertPass(!firstPaintSurface.visible_text.includes("⚙ 设置"), "Replay settings trigger should be icon-only on first paint", firstPaintSurface);
  assertPass(!firstPaintSurface.utility_nav_text.includes("驾驶舱"), "Replay dashboard utility control should be icon-only on first paint", firstPaintSurface);
  assertPass(!firstPaintSurface.utility_refresh_text.includes("刷新"), "Replay refresh utility control should be icon-only on first paint", firstPaintSurface);
  assertPass(firstPaintSurface.utility_nav_label.includes("返回"), "Replay dashboard utility control lost its accessible label", firstPaintSurface);
  assertPass(firstPaintSurface.utility_refresh_label.includes("刷新"), "Replay refresh utility control lost its accessible label", firstPaintSurface);
  assertPass(!firstPaintSurface.select_candle_text.includes("K 线"), "Replay select-candle transport control should be icon-only on first paint", firstPaintSurface);
  assertPass(!firstPaintSurface.latest_signal_text.includes("信号"), "Replay latest-signal transport control should be icon-only on first paint", firstPaintSurface);
  assertPass(!firstPaintSurface.latest_trade_text.includes("成交"), "Replay latest-trade transport control should be icon-only on first paint", firstPaintSurface);
  assertPass(!firstPaintSurface.visible_text.includes("缠论二买 · 1m 无门控对照"), "Replay first paint leaked hidden strategy option text", firstPaintSurface);
  assertPass(!firstPaintSurface.visible_text.includes("未生成 Entry / TP / SL"), "Replay first paint still uses system-style Entry / TP / SL no-trade copy", firstPaintSurface);
  assertPass(firstPaintSurface.visible_text.includes("未生成入场 / 止盈 / 止损"), "Replay first paint should describe the missing plan in Trader language", firstPaintSurface);
  assertPass(!firstPaintSurface.visible_text.includes("看观望原因"), "Replay first paint duplicated the no-trade reason entry", firstPaintSurface);
  assertPass((firstPaintSurface.visible_text.match(/观望原因/g) || []).length <= 1, "Replay first paint should not duplicate the no-trade reason entry", firstPaintSurface);
  assertPass(firstPaintSurface.decision_workbench_text.includes("为什么"), "Replay decision workbench should explain why this candle did or did not trade", firstPaintSurface);
  assertPass(!/这根 K 线收线后已完成评估|因为没有达到策略触发门槛/.test(firstPaintSurface.visible_text), "Replay first paint leaked the full No-Go explanation into the compact strip", firstPaintSurface);
  assertPass(/不下单|拦截|可执行/.test(firstPaintSurface.visible_text), "Replay first paint should translate decision status into Trader language", firstPaintSurface);
  assertPass(!firstPaintSurface.visible_text.includes("NO-GO"), "Replay first paint still exposes no-go status code", firstPaintSurface);
  assertPass(firstPaintSurface.chart_lens_toggle_visible === false, "Replay first paint should not duplicate the decision status as a chart toggle", firstPaintSurface);
  assertPass(firstPaintSurface.trader_status_word_count <= 1, "Replay first paint repeats the same decision status too often", firstPaintSurface);
  assertPass(firstPaintSurface.visible_text.includes("完整病历与归因"), "Replay first paint should expose a compact complete-record entry", firstPaintSurface);
  assertPass(!/做\/不做/.test(firstPaintSurface.decision_workbench_text || ""), "Replay decision workbench still uses generic do-or-not copy", firstPaintSurface);
  assertPass(!firstPaintSurface.visible_text.includes("继续下一根"), "Replay first paint should keep next-step coaching out of the compact decision strip", firstPaintSurface);
  assertPass(firstPaintSurface.banned_terms_visible.length === 0, "Replay first paint leaked OPS/diagnostic language", firstPaintSurface);
  assertPass(!firstPaintSurface.visible_text.includes("审计"), "Replay first paint leaked audit language into Trader view", firstPaintSurface);
  assertPass(!firstPaintSurface.visible_text.includes("主入口"), "Replay first paint should not expose internal drawer-role labels", firstPaintSurface);
  assertPass(firstPaintSurface.visible_tracked_count <= 9, "Replay first paint has too many tracked panels visible", firstPaintSurface);
  assertPass(chartRuntime.library === "tradingview_lightweight_charts", "Replay price charts are not using TradingView Lightweight Charts", chartRuntime);
  assertPass(chartRuntime.has_lightweight_charts === true, "Replay page did not load Lightweight Charts runtime", chartRuntime);
  assertPass(chartRuntime.has_echarts === false, "Replay price page should not load ECharts", chartRuntime);
  assertPass(
    Object.values(chartRuntime.charts).every(item => item.canvas_count >= 1 && item.bars > 0 && item.nonblank_canvas_count >= 1),
    "One or more Replay price charts rendered blank or without OHLC bars",
    chartRuntime
  );
  assertPass(expandedEvidence.parent_open === true && expandedEvidence.open === true, "Replay evidence drawer did not open for second-layer review", expandedEvidence);
  assertPass(expandedEvidence.summary_visible === true, "Replay expanded evidence summary is not visible after opening the drawer", expandedEvidence);
  assertPass(expandedEvidence.has_expanded_summary === true, "Replay expanded evidence summary is missing Trader-facing record sections", expandedEvidence);
  assertPass(expandedEvidence.has_old_role_labels === false, "Replay expanded evidence still uses OPS-style English role labels", expandedEvidence);
  assertPass(expandedEvidence.has_backend_english === false, "Replay expanded evidence leaked backend English copy", expandedEvidence);
  assertPass(!/Go\s*\/\s*No-Go/.test(expandedEvidence.text || ""), "Replay expanded evidence should use Chinese trader language for the decision gate", expandedEvidence);
  assertPass(expandedEvidence.has_current_candle_group === true, "Replay expanded evidence is missing the current-candle decision group", expandedEvidence);
  assertPass(expandedEvidence.has_signal_filter_group === true, "Replay expanded evidence is missing the signal/filter group", expandedEvidence);
  assertPass(expandedEvidence.repeated_decision_question === false, "Replay expanded evidence repeats the same decision-question label", expandedEvidence);
  assertPass(expandedEvidence.has_raw_table_labels === false, "Replay expanded evidence should not expose raw table labels to Trader view", expandedEvidence);
  assertPass(expandedEvidence.decision_detail_visible === false, "Replay expanded evidence should not auto-open current-candle internals", expandedEvidence);
  assertPass(expandedEvidence.trade_table_visible === false, "Replay expanded evidence should not auto-open raw trade tables", expandedEvidence);
  assertPass(expandedEvidence.snapshot_summary_visible === false && expandedEvidence.snapshot_table_visible === false, "Replay expanded evidence should not auto-open signal cross-section tables", expandedEvidence);
  assertPass(expandedEvidence.closed_raw_table_layout_count === 0, "Replay closed raw evidence tables still occupy layout space", expandedEvidence);

  const chartBox = await page.locator(".chart-card.primary-chart .chart").boundingBox();
  assertPass(Boolean(chartBox), "Primary chart box was not rendered", layout);

  const hoverTarget = await page.evaluate(() => {
    const range = window.replayDebug?.chartOption?.("1m")?.visibleLogicalRange || null;
    const bars = window.replayDebug?.chartBars?.("1m") || [];
    if (!range || !bars.length) return null;
    const rawIndex = Math.round((Number(range.from) + Number(range.to)) / 2);
    const index = Math.max(0, Math.min(bars.length - 1, rawIndex));
    return window.replayDebug?.pixelFor?.("1m", index) || null;
  });
  assertPass(Boolean(hoverTarget?.x && hoverTarget?.y), "Could not compute a 1m hover target", {
    hoverTarget,
  });
  await page.mouse.move(hoverTarget.x, hoverTarget.y);
  await page.waitForTimeout(350);
  const hoverSync = await page.evaluate(() => {
    const lastHover = window.replayDebug?.lastHover?.() || null;
    const syncedCards = Array.from(document.querySelectorAll(".chart-card.hover-synced")).map(
      el => el.dataset.timeframe || ""
    );
    const sourceCards = Array.from(document.querySelectorAll(".chart-card.hover-source")).map(
      el => el.dataset.timeframe || ""
    );
    return { lastHover, syncedCards, sourceCards };
  });
  const mapped = hoverSync.lastHover?.mapped || {};
  const expectedSyncTfs = ["1d", "4h", "15m", "5m", "1m"];
  assertPass(hoverSync.lastHover?.sourceTf === "1m", "Hover source should be the 1m primary chart", hoverSync);
  assertPass(expectedSyncTfs.every(tf => hoverSync.syncedCards.includes(tf)), "Not every timeframe card entered hover-synced state", hoverSync);
  assertPass(hoverSync.sourceCards.includes("1m"), "1m primary chart did not enter hover-source state", hoverSync);
  assertPass(
    expectedSyncTfs.every(tf => Number.isFinite(Number(mapped[tf]?.index)) && mapped[tf]?.row_timestamp),
    "Crosshair sync did not map every timeframe to a concrete candle",
    hoverSync
  );

  const cursorBeforeNext = await currentCursor(page);
  const expectedCursorAfterNext = new Date(new Date(cursorBeforeNext).getTime() + 60_000).toISOString().replace(".000Z", "+00:00");
  await page.locator("#transportNextBtn").click();
  await waitForReplayCursor(page, expectedCursorAfterNext);
  const cursorAfterNext = await currentCursor(page);
  const cursorDeltaMs = new Date(cursorAfterNext).getTime() - new Date(cursorBeforeNext).getTime();
  assertPass(cursorDeltaMs === 60000, "Next replay control did not advance exactly one 1m candle", {
    cursorBeforeNext,
    cursorAfterNext,
    cursorDeltaMs,
  });

  const clickTarget = await page.evaluate(() => {
    const bars = window.replayDebug?.chartBars?.("1m") || [];
    const range = window.replayDebug?.chartOption?.("1m")?.visibleLogicalRange || null;
    const from = Math.max(0, Math.floor(Number(range?.from ?? 0)) + 8);
    const to = Math.min(bars.length - 8, Math.floor(Number(range?.to ?? bars.length - 1)) - 8);
    const index = Math.max(0, Math.min(bars.length - 1, Math.floor((from + to) / 2)));
    const row = bars[index] || {};
    return {
      before_cursor: window.replayDebug?.state?.()?.cursor || "",
      index,
      row_timestamp: row.timestamp || "",
      row_bucket_end: row.bucket_end || "",
      row_close: row.close ?? null,
      pixel: window.replayDebug?.pixelFor?.("1m", index) || null,
      visible_range: range,
    };
  });
  assertPass(Boolean(clickTarget?.pixel?.x && clickTarget?.pixel?.y), "Could not compute a clickable 1m candle target", {
    clickTarget,
  });
  await page.mouse.click(clickTarget.pixel.x, clickTarget.pixel.y);
  await page.waitForFunction(
    () => Boolean(window.replayDebug?.lastClick?.()?.selected_cursor),
    null,
    { timeout: 5000 }
  );
  const expectedCursorAfterClick = await page.evaluate(() => window.replayDebug?.lastClick?.()?.selected_cursor || "");
  await waitForReplayCursor(page, expectedCursorAfterClick);
  const clickSelection = await page.evaluate(() => {
    const tfs = ["1m", "5m", "15m", "4h", "1d"];
    const contract = window.replayDebug?.contractSnapshot?.() || null;
    const tailBars = Object.fromEntries(
      tfs.map(tf => {
        const bars = window.replayDebug?.chartBars?.(tf) || [];
        const row = bars[bars.length - 1] || {};
        return [tf, {
          timestamp: row.timestamp || "",
          bucket_end: row.bucket_end || "",
          close: row.close ?? null,
          visible_count: bars.length,
        }];
      })
    );
    return {
      cursor: window.replayDebug?.state?.()?.cursor || "",
      last_click: window.replayDebug?.lastClick?.() || null,
      future_hidden_all: Boolean(contract?.future_hidden_all),
      one_third_future_space: Boolean(contract?.one_third_future_space),
      chart_units: contract?.chart_units || {},
      tail_bars: tailBars,
    };
  });
  const clickedCursor = clickSelection.cursor;
  const clickedCursorMs = timeMs(clickedCursor);
  assertPass(clickSelection.last_click?.source === "lightweight_charts", "Clicking a candle was not handled by Lightweight Charts", {
    clickTarget,
    clickSelection,
  });
  assertPass(clickSelection.last_click?.tf === "1m", "Clicking a 1m candle did not select the primary timeframe", {
    clickTarget,
    clickSelection,
  });
  assertPass(clickSelection.last_click?.timestamp === clickTarget.row_timestamp, "Clicking a 1m candle selected the wrong candle", {
    clickTarget,
    clickSelection,
  });
  assertPass(clickedCursor && clickedCursor === clickSelection.last_click?.selected_cursor, "Clicking a 1m candle did not move replay cursor", {
    clickTarget,
    clickSelection,
  });
  assertPass(clickedCursor !== clickTarget.before_cursor, "Clicking a 1m candle left replay cursor unchanged", {
    clickTarget,
    clickSelection,
  });
  assertPass(clickSelection.future_hidden_all === true, "Future bars are not hidden after candle selection", {
    clickTarget,
    clickSelection,
  });
  assertPass(clickSelection.one_third_future_space === true, "Replay selection did not keep the right-side future space", {
    clickTarget,
    clickSelection,
  });
  assertPass(
    clickSelection.tail_bars["1m"]?.bucket_end === clickedCursor,
    "Primary 1m chart did not stop at the selected candle close",
    { clickTarget, clickSelection }
  );
  const noFutureTails = Object.entries(clickSelection.tail_bars).every(([tf, row]) => {
    const end = candleEndMs(tf, row);
    return Number.isFinite(end) && end <= clickedCursorMs;
  });
  assertPass(noFutureTails, "A context timeframe displayed a future candle after 1m selection", {
    clickTarget,
    clickSelection,
  });
  const contextWaitsForClose = ["5m", "15m", "4h", "1d"].some(tf => {
    const end = candleEndMs(tf, clickSelection.tail_bars[tf]);
    return Number.isFinite(end) && end < clickedCursorMs;
  });
  assertPass(contextWaitsForClose, "Context charts advanced as if every 1m tick closed every higher timeframe", {
    clickTarget,
    clickSelection,
  });

  await page.mouse.move(chartBox.x + chartBox.width * 0.5, chartBox.y + chartBox.height * 0.45);

  const initialRange = await visibleRange(page);
  for (let i = 0; i < 14; i += 1) {
    await page.mouse.wheel(0, 1400);
    await page.waitForTimeout(80);
  }
  const zoomedOutRange = await visibleRange(page);

  for (let i = 0; i < 28; i += 1) {
    await page.mouse.wheel(0, -1400);
    await page.waitForTimeout(55);
  }
  const zoomedInRange = await visibleRange(page);
  const wheelDebug = await page.evaluate(() => window.replayDebug?.lastDesktopWheelZoom?.() || null);

  const initialSpan = span(initialRange);
  const zoomedOutSpan = span(zoomedOutRange);
  const zoomedInSpan = span(zoomedInRange);
  assertPass(zoomedOutSpan >= 500, "Mouse wheel zoom-out did not reach 500 visible candles", {
    initialSpan,
    zoomedOutSpan,
    zoomedOutRange,
  });
  assertPass(zoomedInSpan <= 55, "Mouse wheel zoom-in did not reach about 50 visible candles", {
    zoomedInSpan,
    zoomedInRange,
  });
  assertPass(wheelDebug?.source === "native_wheel", "Wheel debug did not record native Lightweight Charts zoom", {
    wheelDebug,
  });

  const timeBefore = await visibleRange(page);
  await page.mouse.move(chartBox.x + chartBox.width * 0.52, chartBox.y + chartBox.height - 16);
  await page.mouse.down();
  await page.mouse.move(chartBox.x + chartBox.width * 0.70, chartBox.y + chartBox.height - 16, { steps: 8 });
  await page.mouse.up();
  await page.waitForTimeout(220);
  const timeAfter = await visibleRange(page);
  const timeDrag = await page.evaluate(() => window.replayDebug?.lastTimeAxisDragZoom?.() || null);
  assertPass(span(timeBefore) !== span(timeAfter), "Time axis drag did not change visible candle range", {
    timeBefore,
    timeAfter,
    timeDrag,
  });

  const priceBefore = await priceRange(page);
  await page.mouse.move(chartBox.x + chartBox.width - 28, chartBox.y + chartBox.height * 0.42);
  await page.mouse.down();
  await page.mouse.move(chartBox.x + chartBox.width - 28, chartBox.y + chartBox.height * 0.56, { steps: 8 });
  await page.mouse.up();
  await page.waitForTimeout(220);
  const priceAfter = await priceRange(page);
  const priceDrag = await page.evaluate(() => window.replayDebug?.lastPriceAxisDragZoom?.() || null);
  assertPass(Boolean(priceDrag && priceDrag.source === "price_axis_drag"), "Price axis drag was not recorded", {
    priceBefore,
    priceAfter,
    priceDrag,
  });

  const result = {
    status: "pass",
    url,
    prewarm,
    layout,
    first_paint_surface: firstPaintSurface,
    chart_runtime: chartRuntime,
    expanded_evidence: expandedEvidence,
    hover_sync: {
      source_tf: hoverSync.lastHover?.sourceTf || "",
      iso: hoverSync.lastHover?.iso || "",
      synced_cards: hoverSync.syncedCards,
      mapped_timeframes: Object.fromEntries(
        Object.entries(mapped).map(([tf, row]) => [tf, { index: row.index, row_timestamp: row.row_timestamp }])
      ),
    },
    next_control: {
      before: cursorBeforeNext,
      after: cursorAfterNext,
      delta_ms: cursorDeltaMs,
    },
    click_selection: {
      before_cursor: clickTarget.before_cursor,
      clicked_row_timestamp: clickTarget.row_timestamp,
      selected_cursor: clickedCursor,
      future_hidden_all: clickSelection.future_hidden_all,
      one_third_future_space: clickSelection.one_third_future_space,
      tail_bars: clickSelection.tail_bars,
    },
    wheel: {
      initial_span: initialSpan,
      zoomed_out_span: zoomedOutSpan,
      zoomed_in_span: zoomedInSpan,
      debug_source: wheelDebug?.source || "",
    },
    time_axis_drag: {
      before_span: span(timeBefore),
      after_span: span(timeAfter),
      changed: span(timeBefore) !== span(timeAfter),
    },
    price_axis_drag: {
      before: priceBefore,
      after: priceAfter,
      recorded: Boolean(priceDrag && priceDrag.source === "price_axis_drag"),
    },
    console_errors: errors,
    screenshots: {
      first_paint: firstPaintScreenshot || "",
      final: screenshot || "",
    },
    generated_at: new Date().toISOString(),
  };

  if (screenshot) {
    await page.screenshot({ path: screenshot, fullPage: false });
  }
  await browser.close();
  if (output) fs.writeFileSync(output, `${JSON.stringify(result, null, 2)}\n`);
  console.log(JSON.stringify(result, null, 2));
}

run().catch(error => {
  const payload = {
    status: "fail",
    error: error.message,
    evidence: error.evidence || {},
    generated_at: new Date().toISOString(),
  };
  console.error(JSON.stringify(payload, null, 2));
  process.exit(1);
});
