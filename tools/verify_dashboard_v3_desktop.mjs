import fs from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const DEFAULT_URL = "http://127.0.0.1:8765/dashboard-v3.html?date=2026-06-27";
const FIRST_PAINT_BANNED_TERMS = [
  "系统证据",
  "已兑现证据",
  "M1-M5",
  "执行安全红线",
  "PM 证据",
  "原始净值(排查)",
  "策略诊断",
  "策略账本核对",
  "完整策略矩阵",
  "交易环境异常",
  "环境明细",
  "查看原因",
  "Trading environment down",
  "Environment details",
  "View reasons",
];

function argValue(name, fallback = "") {
  const index = process.argv.indexOf(name);
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback;
}

function assertPass(condition, message, evidence = {}) {
  if (!condition) {
    const error = new Error(message);
    error.evidence = evidence;
    throw error;
  }
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

async function prewarmDashboardApi(pageUrl) {
  const parsed = new URL(pageUrl);
  const apiUrl = new URL("/api/dashboard", parsed.origin);
  apiUrl.searchParams.set("view", "trader");
  const date = parsed.searchParams.get("date");
  if (date) apiUrl.searchParams.set("date", date);
  const startedAt = Date.now();
  const controller = new AbortController();
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

function directViewUrl(pageUrl, view) {
  const parsed = new URL(pageUrl);
  parsed.searchParams.set("view", view);
  return parsed.toString();
}

async function verifyDirectViewRoute(browser, pageUrl, view, chromeViewport) {
  const routePage = await browser.newPage(chromeViewport);
  const routeErrors = [];
  routePage.on("console", msg => {
    if (msg.type() === "error") routeErrors.push(msg.text());
  });
  routePage.on("pageerror", err => routeErrors.push(err.message));
  const routeUrl = directViewUrl(pageUrl, view);
  await routePage.goto(routeUrl, { waitUntil: "domcontentloaded", timeout: 15000 });
  await routePage.waitForFunction(
    targetView => {
      const panelText = (document.querySelector(`#view-${targetView}`)?.innerText || "").replace(/\s+/g, " ").trim();
      const apiText = document.querySelector("#apiChip")?.innerText?.trim() || "";
      if (!apiText || /加载中|Loading/i.test(apiText)) return false;
      if (targetView === "overview") return Boolean(window.goldbotCharts?.navChartContract?.selected?.length);
      if (targetView === "strategies") return panelText.includes("主瓶颈") && panelText.includes("行动队列") && panelText.includes("策略深挖");
      if (targetView === "replay") return panelText.includes("交易索引") && (panelText.includes("交易员下一步") || panelText.includes("当前策略今天没有可回放成交"));
      if (targetView === "loop") return panelText.includes("今日闭环判定") && panelText.includes("PM 行动队列");
      return panelText.length > 0;
    },
    view,
    { timeout: 30000 }
  );
  await routePage.waitForTimeout(300);
  const routeState = await routePage.evaluate(targetView => {
    const visible = selector => {
      const el = document.querySelector(selector);
      if (!el) return false;
      const rect = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
    };
    const panelText = (document.querySelector(`#view-${targetView}`)?.innerText || "").replace(/\s+/g, " ").trim();
    return {
      view: targetView,
      url: window.location.href,
      active_tab: document.querySelector("button.tab.active")?.dataset?.view || "",
      panel_visible: visible(`#view-${targetView}`),
      api_chip: document.querySelector("#apiChip")?.innerText?.trim() || "",
      api_loaded: !/加载中|Loading/i.test(document.querySelector("#apiChip")?.innerText?.trim() || ""),
      loading: panelText.includes("加载中"),
      text_sample: panelText.slice(0, 700),
      overview_has_nav: Boolean(window.goldbotCharts?.navChartContract?.selected?.length),
      strategies_has_pm: panelText.includes("主瓶颈") && panelText.includes("行动队列") && panelText.includes("策略深挖"),
      replay_has_workbench: panelText.includes("交易索引") && (panelText.includes("交易员下一步") || panelText.includes("当前策略今天没有可回放成交")),
      loop_has_queue: panelText.includes("今日闭环判定") && panelText.includes("PM 行动队列"),
    };
  }, view);
  await routePage.close();
  return { ...routeState, console_errors: routeErrors };
}

async function run() {
  const url = argValue("--url", process.env.DASHBOARD_V3_UAT_URL || DEFAULT_URL);
  const output = argValue("--output", "");
  const screenshot = argValue("--screenshot", "");
  const chromeExecutable =
    process.env.CHROME_EXECUTABLE || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";

  const prewarm = await prewarmDashboardApi(url);
  assertPass(prewarm.ok, "Dashboard API prewarm failed before V3 browser UAT", { prewarm });

  const browser = await chromium.launch({
    headless: true,
    executablePath: chromeExecutable,
  });
  const viewportConfig = { viewport: { width: 1728, height: 1050 }, deviceScaleFactor: 1 };
  const page = await browser.newPage(viewportConfig);
  const errors = [];
  page.on("console", msg => {
    if (msg.type() === "error") errors.push(msg.text());
  });
  page.on("pageerror", err => errors.push(err.message));

  await page.goto(url, { waitUntil: "domcontentloaded", timeout: 15000 });
  await page.waitForFunction(
    () => {
      const contract = window.goldbotCharts?.navChartContract;
      const option = window.goldbotCharts?.navChart?.getOption?.();
      const series = option?.series || [];
      return Boolean(contract?.selected?.length) && series.some(item => (item.data || []).length >= 20);
    },
    null,
    { timeout: 30000 }
  );
  await page.waitForTimeout(500);

  const firstPaint = await page.evaluate(({ bannedTerms, sampleFactorySource }) => {
    const sampleCanvas = eval(`(${sampleFactorySource})`);
    const option = window.goldbotCharts?.navChart?.getOption?.() || {};
    const series = (option.series || []).map(item => ({
      name: item.name || "",
      type: item.type || "",
      point_count: (item.data || []).length,
      first: (item.data || [])[0] || null,
      last: (item.data || []).slice(-1)[0] || null,
    }));
    const visible = selector => {
      const el = document.querySelector(selector);
      if (!el) return false;
      const rect = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
    };
    const text = selector => (document.querySelector(selector)?.innerText || "").replace(/\s+/g, " ").trim();
    const rectOf = selector => {
      const rect = document.querySelector(selector)?.getBoundingClientRect?.();
      return rect ? { x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height) } : null;
    };
    const trackedSelectors = [
      "#view-overview",
      "#traderCommandStrip",
      "#navChart",
      "#portfolioBook",
      "#overviewStrategyBoard",
      "#todayActionBoard",
      "#navInsightDetails",
      "#todayReviewQueueDrawer",
      "#dashboardEvidenceDrawer",
      "#executionSafetyBoard",
      "#productTrustBanner",
      "#strategyDiagnosticsDrawer",
      "#strategyBookDrawer",
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
    const visibleText = document.body.innerText.replace(/\s+/g, " ").trim().slice(0, 1800);
    const navCanvases = Array.from(document.querySelectorAll("#navChart canvas"));
    const navCanvasSamples = navCanvases.map(sampleCanvas);
    return {
      title: document.title,
      api_chip: text("#apiChip"),
      nav_contract: window.goldbotCharts?.navChartContract || null,
      nav_series: series,
      nav_chart_box: (() => {
        const rect = document.querySelector("#navChart")?.getBoundingClientRect();
        return rect ? { width: Math.round(rect.width), height: Math.round(rect.height) } : null;
      })(),
      nav_runtime: {
        has_echarts: Boolean(window.echarts),
        has_echarts_instance: Boolean(window.goldbotCharts?.navChart?.getOption),
        canvas_count: navCanvases.length,
        nonblank_canvas_count: navCanvasSamples.filter(item => Number(item.alpha_sample || 0) > 10).length,
        canvas_samples: navCanvasSamples.slice(0, 3),
      },
      overview_visible: visible("#view-overview"),
      ops_button_visible: visible("a.btn.ops"),
      evidence_drawer_visible: visible("#dashboardEvidenceDrawer"),
      review_queue_visible: visible("#todayReviewQueueDrawer"),
      nav_details_open: Boolean(document.querySelector("#navInsightDetails")?.open),
      strategy_details_summary: text("#navInsightDetails > summary"),
      command_action_count: document.querySelectorAll("#traderCommandStrip .command-actions .btn").length,
      command_action_text: text("#traderCommandStrip .command-actions"),
      command_headline_text: text("#traderCommandStrip .command-headline"),
      command_evidence_count: document.querySelectorAll("#traderCommandStrip .command-evidence .mini").length,
      command_summary_count: document.querySelectorAll("#traderCommandStrip .command-summary").length,
      command_evidence_text: text("#traderCommandStrip .command-evidence"),
      command_visible_text: text("#traderCommandStrip"),
      command_strip_rect: rectOf("#traderCommandStrip"),
      command_review_word_count: (text("#traderCommandStrip").match(/复盘/g) || []).length,
      refresh_button_text: text("#refreshBtn"),
      refresh_button_label: document.querySelector("#refreshBtn")?.getAttribute("aria-label") || "",
      action_board_visible: visible("#todayActionBoard"),
      portfolio_book_visible: visible("#portfolioBook"),
      portfolio_book_text: text("#portfolioBook"),
      portfolio_book_rect: rectOf("#portfolioBook"),
      overview_strategy_board_visible: visible("#overviewStrategyBoard"),
      overview_strategy_board_text: text("#overviewStrategyBoard").slice(0, 900),
      overview_strategy_row_count: document.querySelectorAll("#overviewStrategyBoard .overview-strategy-row:not(.header)").length,
      overview_strategy_rect: rectOf("#overviewStrategyBoard"),
      compare_presets_visible: visible("#comparePresetBar"),
      selected_summary_visible: visible("#selectedSummary"),
      action_board_button_count: document.querySelectorAll("#todayActionBoard .btn").length,
      action_board_secondary_button_count: document.querySelectorAll("#todayActionBoard .action-mini-card .btn").length,
      action_board_text: text("#todayActionBoard").slice(0, 500),
      nav_header_text: text(".overview-stage .section-title").slice(0, 500),
      nav_edge_rect: rectOf("#navEdgeTape"),
      nav_edge_button_count: document.querySelectorAll("#navEdgeTape .btn").length,
      nav_edge_text: text("#navEdgeTape").slice(0, 500),
      nav_edge_explanatory_copy_visible: visible("#navEdgeTape .nav-edge-summary-main p"),
      nav_edge_chart_read_count: (text("#navEdgeTape").match(/读图|读图细节|Read|Chart details/g) || []).length,
      visible_vs_gold_count: (visibleText.match(/相对黄金/g) || []).length,
      nav_edge_detail_open: Boolean(document.querySelector("#navEdgeTape .nav-edge-detail-toggle")?.open),
      nav_edge_kpi_visible_count: Array.from(document.querySelectorAll("#navEdgeTape .nav-edge-kpi")).filter(el => {
        const rect = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0 && rect.top < window.innerHeight;
      }).length,
      visible_text: visibleText,
      banned_terms_visible: bannedTerms.filter(term => visibleText.includes(term)),
      env_reason_visible: visibleText.includes("原因") || visibleText.includes("Reason"),
      visible_tracked_count: tracked.reduce((sum, item) => sum + item.visible_count, 0),
      tracked,
    };
  }, { bannedTerms: FIRST_PAINT_BANNED_TERMS, sampleFactorySource: canvasSampleScript() });

  assertPass(errors.length === 0, "Dashboard V3 has console/page errors", { errors });
  assertPass(firstPaint.title.includes("GoldBot") || firstPaint.title.includes("黄金"), "Unexpected Dashboard V3 title", firstPaint);
  assertPass(firstPaint.overview_visible, "Dashboard V3 overview is not visible", firstPaint);
  assertPass(firstPaint.nav_chart_box?.width >= 900 && firstPaint.nav_chart_box?.height >= 280, "NAV chart is not desktop-sized inside the PM overview layout", firstPaint);
  assertPass(firstPaint.nav_runtime?.has_echarts === true, "Dashboard V3 NAV chart is not using ECharts runtime", firstPaint);
  assertPass(firstPaint.nav_runtime?.has_echarts_instance === true, "Dashboard V3 NAV chart did not create an ECharts instance", firstPaint);
  assertPass(firstPaint.nav_runtime?.canvas_count >= 1 && firstPaint.nav_runtime?.nonblank_canvas_count >= 1, "Dashboard V3 NAV chart canvas is blank", firstPaint);
  assertPass((firstPaint.nav_contract?.selected || []).length >= 1, "NAV chart did not select any strategy", firstPaint);
  assertPass(firstPaint.nav_series.filter(item => item.point_count >= 20).length >= 2, "NAV chart did not render enough real series", firstPaint);
  assertPass(firstPaint.nav_series.some(item => /黄金|Gold/.test(item.name)), "NAV chart is missing the Gold benchmark", firstPaint);
  assertPass(firstPaint.nav_details_open === false, "Strategy details drawer should be collapsed on first paint", firstPaint);
  assertPass(firstPaint.evidence_drawer_visible === false, "System diagnostics drawer should not be visible on overview first paint", firstPaint);
  assertPass(firstPaint.review_queue_visible === false, "Review queue drawer should not be visible on overview first paint", firstPaint);
  assertPass(firstPaint.ops_button_visible === false, "OPS shortcut should not be visible on trader overview first paint", firstPaint);
  assertPass(firstPaint.command_action_count === 0, "Trader command strip should not duplicate the header replay CTA", firstPaint);
  assertPass(firstPaint.command_evidence_count === 0, "Trader command strip should not use metric cards on first paint", firstPaint);
  assertPass(firstPaint.command_summary_count === 1, "Trader command strip should show exactly one PM summary line", firstPaint);
  assertPass((firstPaint.command_strip_rect?.height || 999) <= 62, "Trader command strip should render as a compact one-line status bar", firstPaint);
  assertPass(!/等\s*\d+\s*个策略|and \d+ more|\d+\s*traded strategies/i.test(firstPaint.command_headline_text || ""), "Trader command headline duplicated review scope details", firstPaint);
  assertPass(/真实成交|real trades/i.test(firstPaint.command_evidence_text || ""), "Trader command strip summary should keep the real-trade count visible", firstPaint);
  assertPass(!/复盘|review/i.test(firstPaint.command_evidence_text || ""), "Trader command summary should not repeat the review action", firstPaint);
  assertPass(!/复盘|review/i.test(firstPaint.command_headline_text || ""), "Trader command headline should avoid duplicating the review-mode chip", firstPaint);
  assertPass(Number(firstPaint.command_review_word_count || 0) <= 1, "Trader command strip repeats 复盘 too often on first paint", firstPaint);
  assertPass(!/等待样本|Need samples|闭环断点|Loop gaps/.test(firstPaint.command_evidence_text || ""), "Trader command strip leaked secondary diagnostic metrics on first paint", firstPaint);
  assertPass(!/打开多周期\+病历|Open MTF \+ trade card/.test(firstPaint.command_action_text || ""), "Trader command strip still shows a duplicate replay CTA", firstPaint);
  assertPass(!/看交易索引|看策略分桶|看每日闭环/.test(firstPaint.command_action_text || ""), "Trader command strip leaked tab-duplicate actions", firstPaint);
  assertPass(!firstPaint.refresh_button_text.includes("刷新") && !firstPaint.refresh_button_text.includes("Refresh"), "Dashboard V3 refresh control should be icon-only on first paint", firstPaint);
  assertPass(firstPaint.refresh_button_label.includes("刷新"), "Dashboard V3 refresh control lost its accessible label", firstPaint);
  assertPass(firstPaint.action_board_visible === false, "Today action board duplicates the command strip and should be hidden on overview first paint", firstPaint);
  assertPass(firstPaint.portfolio_book_visible === true, "Portfolio book should be visible on PM overview first paint", firstPaint);
  assertPass(/今日账本|Today book/.test(firstPaint.portfolio_book_text || ""), "Portfolio book title missing", firstPaint);
  assertPass(/已实现|Realized/.test(firstPaint.portfolio_book_text || ""), "Portfolio book should show realized PnL", firstPaint);
  assertPass(/未实现|Open PnL/.test(firstPaint.portfolio_book_text || ""), "Portfolio book should show open PnL", firstPaint);
  assertPass(/真实成交|Trades/.test(firstPaint.portfolio_book_text || ""), "Portfolio book should show trade count", firstPaint);
  assertPass(firstPaint.overview_strategy_board_visible === true, "Strategy leaderboard should be visible on PM overview first paint", firstPaint);
  assertPass(Number(firstPaint.overview_strategy_row_count || 0) >= 3, "Strategy leaderboard should show multiple strategy rows", firstPaint);
  assertPass(/策略排行榜|Strategy leaderboard/.test(firstPaint.overview_strategy_board_text || ""), "Strategy leaderboard title missing", firstPaint);
  assertPass(/已实现|Realized/.test(firstPaint.overview_strategy_board_text || ""), "Strategy leaderboard should expose realized PnL", firstPaint);
  assertPass(/未实现|Open/.test(firstPaint.overview_strategy_board_text || ""), "Strategy leaderboard should expose open PnL", firstPaint);
  assertPass(firstPaint.compare_presets_visible === false, "Compare preset filters should stay inside the collapsed compare panel on first paint", firstPaint);
  assertPass(firstPaint.selected_summary_visible === false, "Selected strategy chips should stay inside the collapsed compare panel on first paint", firstPaint);
  assertPass(firstPaint.action_board_button_count === 0, "Today action board should not expose CTA buttons on first paint", firstPaint);
  assertPass(firstPaint.action_board_secondary_button_count === 0, "Today action board secondary cards should be status-only", firstPaint);
  assertPass(!/策略相对黄金|Strategy Edge vs Gold/.test(firstPaint.nav_header_text || ""), "NAV header should not repeat the chart mode as its section title", firstPaint);
  assertPass(!/\s相对黄金\s/.test(firstPaint.nav_header_text || ""), "NAV mode controls should use compact labels instead of repeating 相对黄金", firstPaint);
  assertPass(Number(firstPaint.visible_vs_gold_count || 0) <= 3, "Dashboard V3 first paint repeats 相对黄金 too often", firstPaint);
  assertPass((firstPaint.nav_edge_rect?.height || 999) <= 64, "NAV chart readout should stay compact", firstPaint);
  assertPass(firstPaint.nav_edge_button_count === 0, "NAV edge summary should not show duplicate replay CTA buttons", firstPaint);
  assertPass(firstPaint.nav_edge_detail_open === false, "NAV readout details should be collapsed on trader first paint", firstPaint);
  assertPass(firstPaint.nav_edge_kpi_visible_count === 0, "NAV readout detail KPI cards should not compete with the chart on first paint", firstPaint);
  assertPass(!/\b\d+-\d+\b/.test(firstPaint.nav_edge_text || ""), "NAV edge summary leaked machine point ranges to Trader first paint", firstPaint);
  assertPass(!/超额点|edge points|持仓活动点|active-position points/.test(firstPaint.nav_edge_text || ""), "NAV edge summary leaked point-count pseudo metrics", firstPaint);
  assertPass(!/领先黄金|成交样本|样本可信度|Leading Gold|Trade sample|Sample trust/.test(firstPaint.nav_edge_text || ""), "NAV edge summary leaked mixed KPI cards", firstPaint);
  assertPass(!/先复盘|Replay first/i.test(firstPaint.nav_edge_text || ""), "NAV chart readout duplicated the top replay instruction", firstPaint);
  assertPass(!/再点策略|open Gold OHLC/i.test(firstPaint.nav_edge_text || ""), "NAV chart readout should not issue a second navigation instruction", firstPaint);
  assertPass(/读图|Read/.test(firstPaint.nav_edge_text || ""), "NAV chart readout should explain the chart, not issue a second command", firstPaint);
  assertPass(!/图表读法|Chart read/.test(firstPaint.nav_edge_text || ""), "NAV chart readout should use compact labels on first paint", firstPaint);
  assertPass(Number(firstPaint.nav_edge_chart_read_count || 0) <= 1, "NAV edge summary repeats chart-reading labels", firstPaint);
  assertPass(firstPaint.banned_terms_visible.length === 0, "Dashboard V3 first paint leaked OPS/diagnostic language", firstPaint);
  assertPass(firstPaint.env_reason_visible === true, "Trader safety banner should expose a reason entry point", firstPaint);
  assertPass(!/状态\s*处理|State\s*Resolve/.test(firstPaint.visible_text || ""), "Trader safety banner leaked OPS-style action copy", firstPaint);
  assertPass(firstPaint.visible_tracked_count <= 7, "Dashboard V3 first paint has too many tracked panels visible", firstPaint);

  if (screenshot) {
    await page.screenshot({ path: screenshot, fullPage: false });
  }

  await page.locator("#pickerToggle").click();
  const comparePanel = await page.evaluate(() => {
    const visible = selector => {
      const el = document.querySelector(selector);
      if (!el) return false;
      const rect = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
    };
    return {
      picker_open: !document.querySelector("#strategyPickerPanel")?.classList.contains("closed"),
      compare_presets_visible: visible("#comparePresetBar"),
      compare_preset_count: document.querySelectorAll("#comparePresetBar .compare-preset").length,
      selected_summary_visible: visible("#selectedSummary"),
      selected_summary_count: document.querySelectorAll("#selectedSummary .selected-pill").length,
      strategy_picker_visible: visible("#strategyPicker"),
    };
  });
  assertPass(comparePanel.picker_open === true, "Compare panel did not open", comparePanel);
  assertPass(comparePanel.compare_presets_visible === true, "Compare preset filters are not available inside the compare panel", comparePanel);
  assertPass(comparePanel.compare_preset_count >= 4, "Compare panel does not expose enough preset filters", comparePanel);
  assertPass(comparePanel.selected_summary_visible === true, "Selected strategy chips should be available after opening compare panel", comparePanel);
  assertPass(comparePanel.selected_summary_count >= 1, "Compare panel did not render selected strategy chips", comparePanel);
  assertPass(comparePanel.strategy_picker_visible === true, "Strategy picker is not visible after opening compare panel", comparePanel);

  await page.locator('button.tab[data-view="replay"]').click();
  await page.waitForTimeout(700);
  const tradeIndex = await page.evaluate(() => {
    const visible = selector => {
      const el = document.querySelector(selector);
      if (!el) return false;
      const rect = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0 && rect.top < window.innerHeight;
    };
    const text = selector => (document.querySelector(selector)?.innerText || "").replace(/\s+/g, " ").trim();
    const evidence = document.querySelector("#replayEvidenceDetails");
    if (evidence) evidence.open = true;
    const visibleSupport = Array.from(document.querySelectorAll("#view-replay .support-copy")).filter(el => visibleSelectorElement(el)).map(el => el.innerText.replace(/\s+/g, " ").trim());
    function visibleSelectorElement(el) {
      if (!el) return false;
      const rect = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0 && rect.top < window.innerHeight;
    }
    return {
      view_visible: visible("#view-replay"),
      view_text: text("#view-replay"),
      visible_support_copy: visibleSupport,
      no_trades: Boolean(document.querySelector(".replay-index-card.no-trades")),
      empty_state_visible: visible(".trade-empty-state"),
      price_chart_visible: visible("#priceChartWrap"),
      workbench_visible: visible("#tradeWorkbench"),
      focus_readout_visible: visible("#tradeFocusReadout"),
      focus_readout_open: Boolean(document.querySelector("#tradeFocusReadout")?.open),
      focus_readout_body_visible: visible("#tradeFocusReadout .trade-focus-body"),
      workbench_visible_button_count: Array.from(document.querySelectorAll("#tradeWorkbench button")).filter(el => visibleSelectorElement(el)).length,
      workbench_more_open: Boolean(document.querySelector("#tradeWorkbench .workbench-more-actions")?.open),
      workbench_more_visible: visible("#tradeWorkbench .workbench-more-actions"),
      evidence_summary_text: text("#replayEvidenceDetails > summary"),
      evidence_open_text: text("#replayEvidenceDetails"),
      empty_text: text(".trade-empty-state"),
      workbench_text: text("#tradeWorkbench"),
      old_empty_copy_visible: text("#tradeMap").includes("暂无成交可映射到K线"),
    };
  });
  assertPass(tradeIndex.view_visible === true, "Trade index tab did not open", tradeIndex);
  assertPass(tradeIndex.visible_support_copy.length === 0, "Trade index first paint still shows instructional support copy", tradeIndex);
  assertPass(!/当前只显示这笔的图上标注|only this trade is marked on the chart/.test(tradeIndex.view_text || ""), "Trade tape leaked duplicate chart-marker explanation", tradeIndex);
  assertPass(!/图上标注说明|Chart marker notes/.test(tradeIndex.view_text || ""), "Trade index leaked explanatory marker-notes title", tradeIndex);
  assertPass(/标注|Markers/.test(tradeIndex.view_text || ""), "Trade index should keep a concise chart-marker entry", tradeIndex);
  assertPass(!/筛选、对比和跳转|Filters, compare, and jumps|筛选策略|filter strategy/.test(tradeIndex.view_text || ""), "Trade index filter drawer leaked duplicate tool labels", tradeIndex);
  assertPass(/筛选|Filters/.test(tradeIndex.view_text || ""), "Trade index should keep a concise filter drawer entry", tradeIndex);
  assertPass(tradeIndex.evidence_summary_text.includes("复盘材料"), "Trade index review drawer should use trader-facing review-packet copy", tradeIndex);
  assertPass(!tradeIndex.evidence_summary_text.includes("复盘证据"), "Trade index should not label the review drawer as evidence on first paint", tradeIndex);
  assertPass(!tradeIndex.evidence_summary_text.includes("证据与审计"), "Trade index leaked backend audit copy on first paint", tradeIndex);
  assertPass(tradeIndex.evidence_open_text.includes("交易复盘"), "Expanded trade evidence should use trade-review language", tradeIndex);
  assertPass(tradeIndex.evidence_open_text.includes("记录完整性"), "Expanded trade review packet should explain record completeness, not backend evidence", tradeIndex);
  assertPass(!/证据摘要|证据链|Evidence summary|Evidence chain/.test(tradeIndex.evidence_open_text), "Expanded trade review packet leaked evidence-chain wording", tradeIndex);
  assertPass(!/回放审计|Replay audit|审计来自|Audit uses/.test(tradeIndex.evidence_open_text), "Expanded trade evidence leaked backend audit language", tradeIndex);
  assertPass(!/展开交易证据细节|Show trade evidence details|证据\/闭环|Evidence \/ loop|Trade evidence/.test(tradeIndex.workbench_text || ""), "Trade workbench leaked backend evidence copy", tradeIndex);
  if (tradeIndex.no_trades) {
    assertPass(tradeIndex.empty_state_visible === true, "No-trade trade index should show a clear trader empty state", tradeIndex);
    assertPass(tradeIndex.price_chart_visible === false, "No-trade trade index should not show an empty OHLC chart", tradeIndex);
    assertPass(tradeIndex.empty_text.includes("当前策略今天没有可回放成交"), "Trade index empty state does not tell the trader what happened", tradeIndex);
    assertPass(tradeIndex.empty_text.includes("没有成交样本"), "Trade index empty state should lead with the sample state, not repeat the page title", tradeIndex);
    assertPass(!/这页只服务逐笔复盘|This page is for trade-by-trade review/.test(tradeIndex.empty_text), "Trade index empty state still shows a long instructional paragraph", tradeIndex);
    assertPass(/等待选择真实成交|Waiting for a real trade/.test(tradeIndex.workbench_text), "Trade workbench empty state should stay as a short waiting state", tradeIndex);
    assertPass(!/先选择一笔真实成交。右侧只显示|Select a real trade first. This panel only shows/.test(tradeIndex.workbench_text), "Trade workbench empty state still shows long instructional copy", tradeIndex);
    assertPass(!tradeIndex.empty_text.startsWith("交易索引 当前策略"), "Trade index empty state repeats the page title", tradeIndex);
    assertPass(tradeIndex.old_empty_copy_visible === false, "Trade index still shows the old low-value empty tape copy", tradeIndex);
  } else {
    assertPass(tradeIndex.workbench_visible === true, "Trade index with trades should show the focused trade workbench", tradeIndex);
    assertPass(Number(tradeIndex.workbench_visible_button_count || 0) <= 1, "Trade workbench first paint exposes too many peer action buttons", tradeIndex);
    assertPass(tradeIndex.workbench_more_visible === true && tradeIndex.workbench_more_open === false, "Trade workbench secondary actions should be collapsed behind More actions", tradeIndex);
    assertPass(/打开K线\+病历|Open OHLC \+ trade card/.test(tradeIndex.workbench_text || ""), "Trade workbench lost the primary OHLC + trade card action", tradeIndex);
    assertPass(tradeIndex.focus_readout_visible === true, "Trade index should keep marker details available as a collapsed drawer", tradeIndex);
    assertPass(tradeIndex.focus_readout_open === false, "Trade marker details should be collapsed by default", tradeIndex);
    assertPass(tradeIndex.focus_readout_body_visible === false, "Trade marker note details should not compete with the workbench on first paint", tradeIndex);
  }
  const noTradeIndex = await page.evaluate(async () => {
    const candidates = (typeof rows === "function" ? rows() : []).filter(row =>
      Number(row.today_trade_count || 0) === 0 && Number(row.trade_count_7d || 0) === 0
    );
    const target = candidates[0] || null;
    if (!target) return { skipped: true, reason: "no zero-trade strategy in current payload" };
    await setSelectedStrategy(target.strategy_id, "replay", "");
    await new Promise(resolve => setTimeout(resolve, 700));
    const visible = selector => {
      const el = document.querySelector(selector);
      if (!el) return false;
      const rect = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0 && rect.top < window.innerHeight;
    };
    const text = selector => (document.querySelector(selector)?.innerText || "").replace(/\s+/g, " ").trim();
    const visibleSupport = Array.from(document.querySelectorAll("#view-replay .support-copy")).filter(el => {
      const rect = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0 && rect.top < window.innerHeight;
    }).map(el => el.innerText.replace(/\s+/g, " ").trim());
    return {
      skipped: false,
      strategy_id: target.strategy_id,
      visible_support_copy: visibleSupport,
      no_trades: Boolean(document.querySelector(".replay-index-card.no-trades")),
      empty_state_visible: visible(".trade-empty-state"),
      price_chart_visible: visible("#priceChartWrap"),
      empty_text: text(".trade-empty-state"),
      workbench_text: text("#tradeWorkbench"),
      old_empty_copy_visible: text("#tradeMap").includes("暂无成交可映射到K线"),
    };
  });
  if (!noTradeIndex.skipped) {
    assertPass(noTradeIndex.visible_support_copy.length === 0, "Zero-trade trade index still shows instructional support copy", noTradeIndex);
    assertPass(noTradeIndex.no_trades === true, "Zero-trade strategy did not activate the no-trade trade-index state", noTradeIndex);
    assertPass(noTradeIndex.empty_state_visible === true, "Zero-trade strategy should show the trader empty state", noTradeIndex);
    assertPass(noTradeIndex.price_chart_visible === false, "Zero-trade strategy should hide the empty OHLC chart", noTradeIndex);
    assertPass(noTradeIndex.empty_text.includes("当前策略今天没有可回放成交"), "Zero-trade empty state does not explain the state", noTradeIndex);
    assertPass(noTradeIndex.empty_text.includes("没有成交样本"), "Zero-trade empty state should lead with the sample state", noTradeIndex);
    assertPass(!/这页只服务逐笔复盘|This page is for trade-by-trade review/.test(noTradeIndex.empty_text), "Zero-trade empty state still shows long instructional copy", noTradeIndex);
    assertPass(/等待选择真实成交|Waiting for a real trade/.test(noTradeIndex.workbench_text), "Zero-trade workbench should stay as a short waiting state", noTradeIndex);
    assertPass(!noTradeIndex.empty_text.startsWith("交易索引 当前策略"), "Zero-trade empty state repeats the page title", noTradeIndex);
    assertPass(noTradeIndex.old_empty_copy_visible === false, "Zero-trade state still shows the old low-value tape copy", noTradeIndex);
  }
  await page.locator('button.tab[data-view="loop"]').click();
  await page.waitForTimeout(500);
  const loopPage = await page.evaluate(() => {
    const visible = selector => {
      const el = document.querySelector(selector);
      if (!el) return false;
      const rect = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0 && rect.top < window.innerHeight;
    };
    const visibleActionCards = Array.from(document.querySelectorAll("#loopClosureLedger > .loop-action-grid > .loop-action-card")).filter(el => {
      const rect = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0 && rect.top < window.innerHeight;
    });
    const visibleActionParagraphs = Array.from(document.querySelectorAll("#loopClosureLedger > .loop-action-grid > .loop-action-card > p")).filter(el => {
      const rect = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0 && rect.top < window.innerHeight;
    });
    const firstActionRect = document.querySelector("#loopClosureLedger > .loop-action-grid > .loop-action-card")?.getBoundingClientRect?.();
    return {
      view_visible: visible("#view-loop"),
      visible_primary_action_cards: visibleActionCards.length,
      visible_action_paragraphs: visibleActionParagraphs.length,
      primary_action_card_rect: firstActionRect ? { x: Math.round(firstActionRect.x), y: Math.round(firstActionRect.y), width: Math.round(firstActionRect.width), height: Math.round(firstActionRect.height) } : null,
      ledger_copy_visible: visible("#loopClosureLedger .loop-ledger-copy"),
      verdict_height: document.querySelector("#loopVerdict")?.getBoundingClientRect().height || 0,
      verdict_buttons: document.querySelectorAll("#loopVerdict button").length,
      ledger_top: document.querySelector("#loopClosureLedger")?.getBoundingClientRect().top || 0,
      workbench_top: document.querySelector("#dailyLoopWorkbench")?.getBoundingClientRect().top || 0,
      secondary_details_open: Boolean(document.querySelector(".loop-secondary-details")?.open),
      secondary_details_visible: visible(".loop-secondary-details"),
      full_ledger_open: Boolean(document.querySelector("#loopClosureLedger .loop-ledger-details:not(.loop-secondary-details)")?.open),
      text: (document.querySelector("#loopClosureLedger")?.innerText || "").replace(/\s+/g, " ").trim().slice(0, 800),
      view_text: (document.querySelector("#view-loop")?.innerText || "").replace(/\s+/g, " ").trim().slice(0, 1400),
    };
  });
  assertPass(loopPage.view_visible === true, "Daily Loop tab did not open", loopPage);
  assertPass(loopPage.verdict_height > 0 && loopPage.verdict_height < 95, "Daily Loop verdict should be a compact status strip", loopPage);
  assertPass(loopPage.verdict_buttons === 0, "Daily Loop verdict should not duplicate PM action buttons", loopPage);
  assertPass(loopPage.ledger_top > 0 && loopPage.ledger_top < loopPage.workbench_top, "PM Action Queue should come before the four-step diagnostic details", loopPage);
  assertPass(loopPage.visible_primary_action_cards <= 1, "Daily Loop first paint should show at most one primary PM action card", loopPage);
  assertPass((loopPage.primary_action_card_rect?.height || 999) <= 92, "Daily Loop primary action should render as a compact action strip", loopPage);
  assertPass(loopPage.ledger_copy_visible === false, "Daily Loop PM queue still shows instructional support copy", loopPage);
  assertPass(loopPage.visible_action_paragraphs === 0, "Daily Loop primary action still shows long diagnostic prose on first paint", loopPage);
  assertPass(loopPage.secondary_details_open === false, "Daily Loop secondary action items should be collapsed by default", loopPage);
  assertPass(loopPage.full_ledger_open === false, "Daily Loop full strategy ledger should be collapsed by default", loopPage);
  assertPass(!/默认只看最需要处理的闭环断点|Default to the loop gaps/.test(loopPage.text || ""), "Daily Loop first paint leaked PM queue instructional copy", loopPage);
  assertPass(!/早盘没有把这个策略列为计划对象|Morning plan did not include this strategy/.test(loopPage.text || ""), "Daily Loop first paint leaked long plan-execution diagnosis copy", loopPage);
  assertPass(!/盘中\s+\d+\s+笔真实成交：|real trades: .*5m|real trades: .*gold_/i.test(loopPage.view_text || ""), "Daily Loop verdict leaked the traded-strategy long list on first paint", loopPage);
  assertPass(!/早盘是不交易计划，但盘中|Morning was a no-trade plan, but/.test(loopPage.view_text || ""), "Daily Loop loop-detail drawer leaked duplicate plan/execution headline", loopPage);
  assertPass(!/闭环四步详情|Four-step loop detail|默认收起，PM 行动队列优先|Collapsed by default; PM action queue comes first|详细来源摘要|Detailed source snapshot/.test(loopPage.view_text || ""), "Daily Loop first paint leaked diagnostic/instructional drawer copy", loopPage);
  assertPass(/闭环明细|Loop detail/.test(loopPage.view_text || ""), "Daily Loop should keep a concise loop-detail drawer entry", loopPage);
  assertPass(/展开计划、执行、复盘、明日假设|Open plan, execution, review, and tomorrow hypothesis/.test(loopPage.view_text || ""), "Daily Loop loop-detail drawer should be a short navigation hint", loopPage);

  await page.locator('button.tab[data-view="strategies"]').click();
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.waitForTimeout(500);
  const strategyPage = await page.evaluate(() => {
    const rectOf = selector => {
      const rect = document.querySelector(selector)?.getBoundingClientRect?.();
      return rect ? { x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height) } : null;
    };
    const visible = selector => {
      const el = document.querySelector(selector);
      if (!el) return false;
      const rect = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0 && rect.top < window.innerHeight;
    };
    const visiblePrimaryActions = Array.from(document.querySelectorAll("#strategyGrid > .strategy-action-panel > .strategy-primary-action-list > .strategy-action-item")).filter(el => {
      const rect = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0 && rect.top < window.innerHeight;
    });
    return {
      view_visible: visible("#view-strategies"),
      strategy_title_support_visible: visible("#view-strategies > .card > .section-title .support-copy"),
      scope_drawer_open: Boolean(document.querySelector("#strategyScopeDrawer")?.open),
      family_filters_visible: visible("#familyFilters"),
      decision_summary_visible: visible("#strategyDecisionSummary"),
      pm_prescription_visible: visible("#strategyPmPrescription"),
      pm_prescription_rect: rectOf("#strategyPmPrescription"),
      pm_prescription_lead_copy_visible: visible("#strategyPmPrescription .prescription-lead > .prescription-copy"),
      pm_prescription_redline_open: Boolean(document.querySelector("#strategyPmPrescription .prescription-redline")?.open),
      pm_prescription_redline_body_visible: visible("#strategyPmPrescription .prescription-redline-body"),
      strategy_action_title_copy_visible: visible("#strategyGrid .strategy-action-title span"),
      visible_primary_action_cards: visiblePrimaryActions.length,
      secondary_actions_open: Boolean(document.querySelector(".strategy-secondary-actions")?.open),
      secondary_actions_visible: visible(".strategy-secondary-actions"),
      deep_dive_open: Boolean(document.querySelector("#strategyDeepDiveDrawer")?.open),
      deep_dive_visible: visible("#strategyDeepDiveDrawer"),
      deep_dive_summary_copy_visible: visible("#strategyDeepDiveDrawer > summary .strategy-diagnostics-title span"),
      diagnostics_open: Boolean(document.querySelector("#strategyDiagnosticsDrawer")?.open),
      diagnostics_visible: visible("#strategyDiagnosticsDrawer"),
      book_open: Boolean(document.querySelector("#strategyBookDrawer")?.open),
      book_visible: visible("#strategyBookDrawer"),
      performance_open: Boolean(document.querySelector("#strategyPerformanceDetails")?.open),
      performance_visible: visible("#strategyPerformanceDetails"),
      performance_table_visible: visible("#strategyPerformanceTable"),
      text: (document.querySelector("#view-strategies")?.innerText || "").replace(/\s+/g, " ").trim().slice(0, 1000),
    };
  });
  assertPass(strategyPage.view_visible === true, "Strategy tab did not open", strategyPage);
  assertPass(strategyPage.strategy_title_support_visible === false, "Strategy page title should not show instructional support copy on first paint", strategyPage);
  assertPass(strategyPage.scope_drawer_open === false, "Strategy filters and overview should be collapsed by default", strategyPage);
  assertPass(strategyPage.family_filters_visible === false, "Strategy family filters should not compete with PM prescription on first paint", strategyPage);
  assertPass(!/策略筛选与概览|Strategy filters and overview|filters\s+\/\s+筛选/.test(strategyPage.text || ""), "Strategy scope drawer leaked duplicate filter labels", strategyPage);
  assertPass(/筛选|Filters/.test(strategyPage.text || ""), "Strategy page should keep one concise filter drawer entry", strategyPage);
  assertPass(strategyPage.decision_summary_visible === false, "Strategy decision summary tiles should stay inside the collapsed scope drawer", strategyPage);
  assertPass(strategyPage.pm_prescription_visible === true, "Strategy page should keep PM prescription visible", strategyPage);
  assertPass((strategyPage.pm_prescription_rect?.height || 999) <= 180, "Strategy PM prescription should stay compact enough for the action queue", strategyPage);
  assertPass(strategyPage.pm_prescription_lead_copy_visible === false, "Strategy PM prescription should not show explanatory prose on first paint", strategyPage);
  assertPass(strategyPage.pm_prescription_redline_open === false, "Strategy PM prescription red line should be collapsed by default", strategyPage);
  assertPass(strategyPage.pm_prescription_redline_body_visible === false, "Strategy PM prescription red line details should not compete with the primary next action", strategyPage);
  assertPass(strategyPage.strategy_action_title_copy_visible === false, "Strategy action queue should not show explanatory prose on first paint", strategyPage);
  assertPass(strategyPage.visible_primary_action_cards <= 1, "Strategy first paint should show at most one primary strategy action", strategyPage);
  assertPass(strategyPage.secondary_actions_open === false, "Strategy secondary actions should be collapsed by default", strategyPage);
  assertPass(strategyPage.deep_dive_open === false, "Strategy deep-dive drawer should be collapsed by default", strategyPage);
  assertPass(strategyPage.deep_dive_visible === true, "Strategy page should expose one compact deep-dive entry point", strategyPage);
  assertPass(strategyPage.deep_dive_summary_copy_visible === false, "Strategy deep-dive entry should stay label-only on first paint", strategyPage);
  assertPass(strategyPage.diagnostics_open === false, "Strategy diagnostics should be collapsed by default", strategyPage);
  assertPass(strategyPage.diagnostics_visible === false, "Strategy diagnostics should stay inside the collapsed deep-dive drawer on first paint", strategyPage);
  assertPass(strategyPage.book_open === false, "Strategy book audit should be collapsed by default", strategyPage);
  assertPass(strategyPage.book_visible === false, "Strategy book audit should stay inside the collapsed deep-dive drawer on first paint", strategyPage);
  assertPass(strategyPage.performance_open === false, "Strategy performance table should be collapsed by default", strategyPage);
  assertPass(strategyPage.performance_visible === false, "Strategy performance drawer should stay inside the collapsed deep-dive drawer on first paint", strategyPage);
  assertPass(strategyPage.performance_table_visible === false, "Strategy performance table should not compete with the PM first screen", strategyPage);
  assertPass(!/薄样本回测门|thin backtest gate/i.test(strategyPage.text || ""), "Strategy first paint leaked internal backtest-gate jargon", strategyPage);
  assertPass(/候选太薄|candidate too thin/i.test(strategyPage.text || ""), "Strategy first paint should explain the bottleneck in trader-facing PM language", strategyPage);
  assertPass((strategyPage.text.match(/候选太薄/g) || []).length <= 1, "Strategy first paint repeats the same PM bottleneck label too often", strategyPage);
  assertPass(!/先看 PM 处方和行动队列|Start with the PM prescription and action queue/i.test(strategyPage.text || ""), "Strategy first paint leaked instructional page copy", strategyPage);
  assertPass(!/首屏只显示最需要马上处理的策略|First screen shows only the most urgent strategy/i.test(strategyPage.text || ""), "Strategy first paint leaked action-queue instructional copy", strategyPage);
  assertPass(!/诊断、账本和完整排名只在需要解释异常或核对数字时展开|Open diagnostics, books, and full ranking/i.test(strategyPage.text || ""), "Strategy first paint leaked deep-dive instructional copy", strategyPage);
  assertPass(!/不要做|Do not|unticketed candidates|open MTM|thin-sample returns/.test(strategyPage.text || ""), "Strategy first paint leaked red-line detail copy", strategyPage);
  assertPass(/卡点数/.test(strategyPage.text || ""), "Strategy PM prescription should use an action/quantity metric instead of repeating the bottleneck label", strategyPage);
  assertPass(/需看代表 K 线/.test(strategyPage.text || ""), "Strategy PM focus rows should describe the next review action", strategyPage);
  assertPass(!/adx ema pullback|trend pullback/i.test(strategyPage.text || ""), "Strategy first paint leaked raw strategy slug or raw family label", strategyPage);
  assertPass(/5分钟ADX回踩|5m ADX/i.test(strategyPage.text || ""), "Strategy first paint should show a readable ADX pullback strategy name", strategyPage);

  const directRoutes = [];
  for (const directView of ["overview", "strategies", "replay", "loop"]) {
    const route = await verifyDirectViewRoute(browser, url, directView, viewportConfig);
    directRoutes.push(route);
    assertPass(route.console_errors.length === 0, `Dashboard V3 direct ${directView} route has console/page errors`, route);
    assertPass(route.api_loaded === true, `Dashboard V3 direct ${directView} route did not finish loading API state`, route);
    assertPass(route.active_tab === directView, `Dashboard V3 direct ${directView} route did not activate the matching tab`, route);
    assertPass(route.panel_visible === true, `Dashboard V3 direct ${directView} route did not show the matching panel`, route);
    assertPass(route.loading === false, `Dashboard V3 direct ${directView} route stayed in loading state`, route);
    if (directView === "overview") assertPass(route.overview_has_nav === true, "Dashboard V3 direct overview route did not render NAV chart data", route);
    if (directView === "strategies") {
      assertPass(route.strategies_has_pm === true, "Dashboard V3 direct strategies route did not render PM prescription/action queue", route);
      assertPass(!/薄样本回测门|thin backtest gate/i.test(route.text_sample || ""), "Dashboard V3 direct strategies route leaked internal backtest-gate jargon", route);
      assertPass(/候选太薄|candidate too thin/i.test(route.text_sample || ""), "Dashboard V3 direct strategies route should explain the bottleneck in trader-facing PM language", route);
      assertPass((route.text_sample.match(/候选太薄/g) || []).length <= 1, "Dashboard V3 direct strategies route repeats the same PM bottleneck label too often", route);
      assertPass(/卡点数/.test(route.text_sample || ""), "Dashboard V3 direct strategies route should show bottleneck quantity without repeating the label", route);
      assertPass(/需看代表 K 线/.test(route.text_sample || ""), "Dashboard V3 direct strategies route should show a concrete PM review action", route);
      assertPass(!/adx ema pullback|trend pullback/i.test(route.text_sample || ""), "Dashboard V3 direct strategies route leaked raw strategy slug or family label", route);
      assertPass(/5分钟ADX回踩|5m ADX/i.test(route.text_sample || ""), "Dashboard V3 direct strategies route should show a readable ADX pullback strategy name", route);
    }
    if (directView === "replay") assertPass(route.replay_has_workbench === true, "Dashboard V3 direct trade-index route did not render the trade workbench or empty state", route);
    if (directView === "loop") {
      assertPass(route.loop_has_queue === true, "Dashboard V3 direct daily-loop route did not render the PM action queue", route);
      assertPass(!/5m london ny breakout|1m bollinger reclaim filter/i.test(route.text_sample || ""), "Dashboard V3 direct daily-loop route leaked raw strategy names", route);
      assertPass(/盘中\s+\d+\s+笔真实成交|\d+\s+real trades/i.test(route.text_sample || ""), "Dashboard V3 direct daily-loop route should show the intraday real-trade count", route);
      assertPass(!/盘中\s+\d+\s+笔真实成交：|real trades: .*5m|real trades: .*gold_/i.test(route.text_sample || ""), "Dashboard V3 direct daily-loop route should not show the full traded-strategy list on first paint", route);
    }
  }

  await page.locator("#headerReplayBtn").click();
  await page.waitForURL(/dashboard-replay\.html/, { timeout: 10000 });
  await page.waitForLoadState("domcontentloaded");
  const replayRoute = await page.evaluate(() => ({
    url: window.location.href,
    title: document.title,
    body_text: document.body.innerText.replace(/\s+/g, " ").slice(0, 300),
    is_404: /404|not found/i.test(document.body.innerText),
  }));
  assertPass(replayRoute.url.includes("dashboard-replay.html"), "Header replay CTA did not route to dashboard-replay.html", replayRoute);
  assertPass(!replayRoute.is_404, "Header replay CTA opened a 404/not-found page", replayRoute);
  if (!noTradeIndex.skipped) {
    const expectedDate = new URL(url).searchParams.get("date") || "";
    const replayDate = new URL(replayRoute.url).searchParams.get("date") || "";
    assertPass(replayDate === expectedDate, "Header replay CTA drifted from the current dashboard date after selecting a zero-trade strategy", {
      expected_date: expectedDate,
      replay_date: replayDate,
      replay_url: replayRoute.url,
      zero_trade_index: noTradeIndex,
    });
  }

  const result = {
    status: "pass",
    url,
    prewarm,
    first_paint: firstPaint,
    compare_panel: comparePanel,
    trade_index: tradeIndex,
    zero_trade_index: noTradeIndex,
    loop_page: loopPage,
    strategy_page: strategyPage,
    direct_routes: directRoutes,
    replay_cta: replayRoute,
    console_errors: errors,
    generated_at: new Date().toISOString(),
  };

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
