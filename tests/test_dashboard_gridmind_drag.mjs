import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import test from "node:test";
import {fileURLToPath} from "node:url";
import {dirname, resolve} from "node:path";
import vm from "node:vm";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const html = readFileSync(resolve(root, "dashboard-gridmind.html"), "utf8");

function extractedFunction(name, nextName) {
  const start = html.indexOf(`function ${name}`);
  const end = html.indexOf(`\nfunction ${nextName}`, start);
  assert.ok(start >= 0 && end > start, `could not extract ${name}`);
  return vm.runInNewContext(`(${html.slice(start, end)})`);
}

const calculateGridDragBounds = extractedFunction(
  "calculateGridDragBounds",
  "moveGridDrag",
);
const executionIdentity = extractedFunction("executionIdentity", "sameStringSet");
const gridPreviewContractIssue = extractedFunction(
  "gridPreviewContractIssue",
  "gridGateMarket",
);
const tradeLifecycleRows = extractedFunction(
  "tradeLifecycleRows",
  "updateTabCount",
);
const rolloverDisplayState = extractedFunction(
  "rolloverDisplayState",
  "runtimeExecutionState",
);
const runtimeExecutionState = extractedFunction(
  "runtimeExecutionState",
  "marketIssueText",
);
const displayTradingPair = extractedFunction(
  "displayTradingPair",
  "marketSourceLabel",
);
const isFresh = extractedFunction("isFresh", "isTrustedHeaderMarket");
const isTrustedHeaderMarket = vm.runInNewContext(
  `(${html.slice(
    html.indexOf("function isTrustedHeaderMarket"),
    html.indexOf("\nfunction statusText", html.indexOf("function isTrustedHeaderMarket")),
  )})`,
  {isFresh},
);
const sameVenueMarket = extractedFunction("sameVenueMarket", "marketUtcDayKey");
const quantityText = vm.runInNewContext(
  `(${html.slice(
    html.indexOf("function quantityText"),
    html.indexOf("\nfunction runtimeActionText", html.indexOf("function quantityText")),
  )})`,
  {
    finiteNumber: (value) =>
      value !== null && value !== "" && Number.isFinite(Number(value))
        ? Number(value)
        : null,
  },
);
const tradeCloseReason = vm.runInNewContext(
  `(${html.slice(
    html.indexOf("function tradeCloseReason"),
    html.indexOf("\nfunction orderProtection", html.indexOf("function tradeCloseReason")),
  )})`,
  {
    finiteNumber: (value) =>
      value !== null && value !== "" && Number.isFinite(Number(value))
        ? Number(value)
        : null,
  },
);
const fillStableKey = extractedFunction("fillStableKey", "tradeStableKey");
const reviewProposalForPlan = extractedFunction(
  "reviewProposalForPlan",
  "reviewPlanSpecComplete",
);
const reviewPlanChecks = vm.runInNewContext(
  `${html.slice(
    html.indexOf("function reviewPlanSpecComplete"),
    html.indexOf("\nfunction reviewCell", html.indexOf("function reviewPlanSpecComplete")),
  )}; ({reviewPlanSpecComplete,sameReviewPlanSpec})`,
  {
    finiteNumber: (value) =>
      value !== null && value !== "" && Number.isFinite(Number(value))
        ? Number(value)
        : null,
  },
);
const reviewDirectionAssessment = vm.runInNewContext(
  `(${html.slice(
    html.indexOf("function reviewDirectionAssessment"),
    html.indexOf("\nfunction reviewProposalForPlan", html.indexOf("function reviewDirectionAssessment")),
  )})`,
  {
    reviewDirectionLabel: (value) =>
      ({long: "做多", short: "做空", neutral: "中性"})[value] || "缺少证据",
    humanizeReviewText: (value) => String(value ?? ""),
  },
);

function tradeActivityHarness() {
  const shown = [];
  const context = vm.createContext({
    Set,
    Map,
    String,
    Number,
    Date,
    state: {
      data: {market: {provider_symbol: "XAUUSDT"}},
      tradeActivity: {
        initialized: false,
        seenFillKeys: new Set(),
        tradeStatusByKey: new Map(),
        tradeRemainingByKey: new Map(),
        notifiedCloseTradeKeys: new Set(),
      },
    },
    tradeLifecycleRows,
    finiteNumber: (value) =>
      value !== null && value !== "" && Number.isFinite(Number(value))
        ? Number(value)
        : null,
    displayTradingPair,
    quantityText: (value) => String(value ?? "--"),
    num: (value) => String(value ?? "--"),
    signed: (value) => `${Number(value) > 0 ? "+" : ""}${Number(value)}`,
    beijingDateTime: (value) => String(value ?? "--"),
    tradeCloseReason,
    showTradeToast: (model) => shown.push(model),
  });
  const activityStart = html.indexOf("const CLOSE_FILL_EVENTS=");
  const audioStart = html.indexOf("\nfunction armTradeAudio", activityStart);
  vm.runInContext(html.slice(activityStart, audioStart), context);
  const positionsStart = html.indexOf("function authoritativePositionRows");
  const renderTablesStart = html.indexOf("\nfunction renderTables", positionsStart);
  vm.runInContext(html.slice(positionsStart, renderTablesStart), context);
  const observeStart = html.indexOf("function observeTradeActivity");
  const positionsFunctionStart = html.indexOf(
    "\nfunction authoritativePositionRows",
    observeStart,
  );
  vm.runInContext(html.slice(observeStart, positionsFunctionStart), context);
  return {context, shown};
}

test("off-screen handles move by price delta without snapping to visible prices", () => {
  const upper = calculateGridDragBounds(
    {mode: "upper", startLow: 3800, startHigh: 4190, startPrice: 4020},
    4025,
  );
  const lower = calculateGridDragBounds(
    {mode: "lower", startLow: 3800, startHigh: 4190, startPrice: 3970},
    3965,
  );

  assert.equal(upper.low, 3800);
  assert.equal(upper.high, 4195);
  assert.equal(lower.low, 3795);
  assert.equal(lower.high, 4190);
});

test("successive drag releases accumulate from the prior draft", () => {
  const first = calculateGridDragBounds(
    {mode: "move", startLow: 3800, startHigh: 4190, startPrice: 4000},
    4010,
  );
  const second = calculateGridDragBounds(
    {
      mode: "move",
      startLow: first.low,
      startHigh: first.high,
      startPrice: 4010,
    },
    4005,
  );

  assert.equal(first.low, 3810);
  assert.equal(first.high, 4200);
  assert.equal(second.low, 3805);
  assert.equal(second.high, 4195);
});

function validDraft() {
  return {
    low: 3800,
    high: 4200,
    notionalPerGrid: 100,
    original: {
      direction: "neutral",
      style: "steady",
      grid: {
        count: 4,
        mode: "arithmetic",
        notionalPerGrid: 100,
        leverage: 5,
        outOfRange: "wait",
      },
    },
  };
}

function validPreview() {
  return {
    schema_version: "strategy-grid-preview-v1",
    preview_id: "preview-1",
    direction: "neutral",
    style: "steady",
    range: {low: 3800, high: 4200},
    grid: {
      count: 4,
      mode: "arithmetic",
      notional_per_grid: 100,
      notional_mode: "manual",
      leverage: 5,
      out_of_range: "wait",
    },
    orders: [{side: "buy", price: 3900, notional: 100}],
    risk: {
      risk_budget_exceeded: false,
      estimated_margin: 20,
      actual_leverage: 0.1,
      max_loss: 8,
    },
  };
}

test("replacement preview rejects null risk fields and malformed orders", () => {
  const draft = validDraft();
  const nullRisk = validPreview();
  nullRisk.risk.estimated_margin = null;
  const malformedOrder = validPreview();
  malformedOrder.orders[0].notional = "";

  assert.equal(gridPreviewContractIssue(validPreview(), draft), "");
  assert.equal(
    gridPreviewContractIssue(nullRisk, draft),
    "风险核对字段不完整",
  );
  assert.equal(
    gridPreviewContractIssue(malformedOrder, draft),
    "新网格订单不完整",
  );
});

test("over-budget preview requires the strict positive safe notional cap", () => {
  const draft = validDraft();
  const safe = validPreview();
  safe.risk.risk_budget_exceeded = true;
  safe.risk.safe_notional_cap_per_grid = 72.5;
  const missing = structuredClone(safe);
  delete missing.risk.safe_notional_cap_per_grid;
  const zero = structuredClone(safe);
  zero.risk.safe_notional_cap_per_grid = 0;

  assert.equal(gridPreviewContractIssue(safe, draft), "");
  assert.equal(
    gridPreviewContractIssue(missing, draft),
    "安全名义上限缺失",
  );
  assert.equal(
    gridPreviewContractIssue(zero, draft),
    "安全名义上限缺失",
  );
});

test("execution identity prefers authoritative positions when trades is empty", () => {
  const identity = executionIdentity({
    orders: [{state: "accepted", command_id: "order-2"}],
    trades: [],
    positions: [{status: "open", position_id: "position-7"}],
  });

  assert.equal(identity.complete, true);
  assert.equal(identity.acceptedOrderIds.join(","), "order-2");
  assert.equal(identity.openPositionIds.join(","), "position-7");
});

test("trade records count one lifecycle even after that trade closes", () => {
  const rows = tradeLifecycleRows([
    {
      trade_id: "trade-1",
      status: "open",
      entry_ts: "2026-07-17T08:00:00Z",
    },
    {
      trade_id: "trade-1",
      status: "closed",
      entry_ts: "2026-07-17T08:00:00Z",
      exit_ts: "2026-07-17T08:10:00Z",
    },
    {
      trade_id: "trade-2",
      status: "OPEN",
      entry_ts: "2026-07-17T08:20:00Z",
    },
  ]);

  assert.equal(rows.length, 2);
  assert.equal(rows.find((row) => row.trade_id === "trade-1").status, "closed");
});

test("a newer healthy start marks an earlier same-cycle rollover block as recovered", () => {
  const runtime = {
    cycle_id: "2026-07-17_DAY",
    desired_state: "running",
    actual_state: "running",
    last_error: null,
    updated_at: "2026-07-17T10:24:35Z",
  };
  const rollover = {
    status: "blocked",
    current_cycle_id: "2026-07-17_DAY",
    recorded_at: "2026-07-17T08:09:16Z",
  };

  assert.equal(
    JSON.stringify(rolloverDisplayState(runtime, rollover, 25)),
    JSON.stringify({blocked: false, recovered: true}),
  );
  assert.equal(
    JSON.stringify(rolloverDisplayState(runtime, rollover, 0)),
    JSON.stringify({blocked: true, recovered: false}),
  );
  assert.equal(
    JSON.stringify(rolloverDisplayState(runtime, rollover, 0, 1)),
    JSON.stringify({blocked: false, recovered: true}),
  );
  assert.equal(
    JSON.stringify(
      rolloverDisplayState(
        {...runtime, updated_at: "2026-07-17T08:00:00Z"},
        rollover,
        25,
      ),
    ),
    JSON.stringify({blocked: true, recovered: false}),
  );
});

test("provider ticker is formatted as the real XAU / USDT pair", () => {
  assert.equal(displayTradingPair({provider_symbol: "XAUUSDT"}), "XAU / USDT");
  assert.equal(displayTradingPair({provider_symbol: "BTC/USDT"}), "BTC / USDT");
  assert.equal(displayTradingPair({symbol: "GOLD"}), "GOLD");
});

test("header trust requires an explicit fresh 1m snapshot from the same provider", () => {
  const oneMinute = {
    status: "ready",
    fresh: true,
    is_synthetic: false,
    timeframe: "1m",
    provider: "binance_usdm_futures",
    provider_symbol: "XAUUSDT",
  };
  assert.equal(isTrustedHeaderMarket(oneMinute), true);
  assert.equal(isTrustedHeaderMarket({...oneMinute, timeframe: "30m"}), false);
  assert.equal(
    sameVenueMarket(oneMinute, {...oneMinute, timeframe: "1d"}),
    true,
  );
  assert.equal(
    sameVenueMarket(oneMinute, {...oneMinute, provider: ""}),
    false,
  );
  assert.equal(
    sameVenueMarket(oneMinute, {...oneMinute, provider: "tiger"}),
    false,
  );
});

test("runtime state follows actual_state and treats authoritative positions as activity", () => {
  const runningWithPosition = runtimeExecutionState(
    {actual_state: "running", desired_state: "running"},
    {orders: [], positions: [{position_id: "p-1", status: "open"}]},
  );
  assert.equal(runningWithPosition.running, true);
  assert.equal(runningWithPosition.inconsistent, false);
  assert.equal(runningWithPosition.openCount, 1);

  const stoppedWithResidue = runtimeExecutionState(
    {actual_state: "stopped", desired_state: "stopped"},
    {orders: [], positions: [{position_id: "p-1", status: "open"}]},
  );
  assert.equal(stoppedWithResidue.orphaned, true);

  const emptyRunning = runtimeExecutionState(
    {actual_state: "running", desired_state: "running"},
    {orders: [], positions: []},
  );
  assert.equal(emptyRunning.running, true);
  assert.equal(emptyRunning.inconsistent, true);
});

test("fill identity prefers fill ids so partial fills on one order are distinct", () => {
  assert.equal(
    fillStableKey({cycle_id: "c-1", order_id: "o-1", fill_id: "f-1"}),
    "c-1:f-1",
  );
  assert.equal(
    fillStableKey({cycle_id: "c-1", order_id: "o-1", fill_id: "f-2"}),
    "c-1:f-2",
  );
  assert.equal(fillStableKey({cycle_id: "c-1", order_id: "o-1"}), "c-1:o-1");
});

test("review compares only the AI proposal explicitly linked to the production plan", () => {
  const proposals = [
    {source: "ai", proposal_id: "p-old", created_at: "2026-07-16T00:00:00Z"},
    {source: "ai", proposal_id: "p-new", created_at: "2026-07-17T00:00:00Z"},
  ];
  assert.equal(
    reviewProposalForPlan({proposals}, {source_proposal_ids: ["p-old"]}).proposal_id,
    "p-old",
  );
  assert.equal(
    reviewProposalForPlan({proposals}, {source_proposal_ids: ["missing"]}),
    null,
  );
  assert.equal(reviewProposalForPlan({proposals}, {}), null);
});

test("review plan equality fails closed and ignores only non-applicable spacing", () => {
  const arithmetic = {
    direction: "short",
    style: "steady",
    range: {low: 3900, high: 4100},
    grid: {
      mode: "arithmetic",
      count: 50,
      spacing: 4,
      notional_per_grid: 2000,
      leverage: 3,
      out_of_range: "wait",
    },
  };
  assert.equal(reviewPlanChecks.reviewPlanSpecComplete(arithmetic), true);
  assert.equal(
    reviewPlanChecks.sameReviewPlanSpec(
      arithmetic,
      {...arithmetic, grid: {...arithmetic.grid, spacing_ratio: 999}},
    ),
    true,
  );
  assert.equal(
    reviewPlanChecks.sameReviewPlanSpec(
      arithmetic,
      {...arithmetic, grid: {...arithmetic.grid, spacing: null}},
    ),
    false,
  );
  assert.equal(
    reviewPlanChecks.sameReviewPlanSpec(
      {...arithmetic, direction: null},
      {...arithmetic, direction: null},
    ),
    false,
  );
});

test("legacy direction grades cannot judge a different locked plan", () => {
  const mismatch = reviewDirectionAssessment(
    {direction: "short"},
    {decision: "long", hit: false, summary: "计划 long，实际 short"},
  );
  assert.equal(mismatch.kind, "info");
  assert.equal(mismatch.verdict, "口径不一致");
  assert.match(mismatch.outcome, /历史评估对象 做多/);
  assert.match(mismatch.outcome, /锁定生产计划 做空/);

  const missing = reviewDirectionAssessment(
    {direction: "short"},
    {decision: "short", hit: null, realized_regime: null},
  );
  assert.equal(missing.verdict, "证据不足");
  assert.equal(missing.outcome, "实际方向证据不足");
});

test("quantity labels preserve real zero while missing values stay unknown", () => {
  assert.equal(quantityText(null), "--");
  assert.equal(quantityText(""), "--");
  assert.equal(quantityText(0), "0");
  assert.equal(quantityText(-0.25), "-0.25");
});

test("a closed trade without exit evidence is not mislabeled as take profit", () => {
  assert.equal(
    tradeCloseReason({
      status: "closed",
      exit_price: null,
      tp: null,
      sl: null,
    }),
    "平仓",
  );
  assert.equal(tradeCloseReason({status: "closed", exit_price: 4000, tp: 4000}), "TP");
  assert.equal(tradeCloseReason({status: "closed", exit_reason: "SL"}), "SL");
});

test("trade notifications seed history, dedupe fills, and do not double-toast a close", () => {
  const {context, shown} = tradeActivityHarness();
  const entry = {
    cycle_id: "cycle-1",
    order_id: "order-1",
    trade_id: "trade-1",
    event: "entry",
    side: "buy",
    price: 3990,
    quantity: 1,
    ts: "2026-07-17T10:00:00Z",
  };
  const openTrade = {
    cycle_id: "cycle-1",
    trade_id: "trade-1",
    status: "open",
    side: "long",
    entry_price: 3990,
  };
  const baseline = {
    production_execution: {fills: [entry], trades: [openTrade]},
  };

  assert.equal(context.observeTradeActivity(baseline).length, 0);
  assert.equal(shown.length, 0);
  assert.equal(context.observeTradeActivity(baseline).length, 0);

  const secondEntry = {
    ...entry,
    order_id: "order-2",
    trade_id: "trade-2",
    price: 3980,
  };
  const withEntry = {
    production_execution: {
      fills: [entry, secondEntry],
      trades: [openTrade, {...openTrade, trade_id: "trade-2"}],
    },
  };
  assert.equal(context.observeTradeActivity(withEntry).length, 1);
  assert.equal(shown.at(-1).title, "网格成交 · 买入");
  assert.equal(context.observeTradeActivity(withEntry).length, 0);

  const close = {
    ...secondEntry,
    order_id: "order-2-TP",
    event: "target",
    side: "sell",
    price: 3990,
  };
  const closedTrade = {
    ...openTrade,
    trade_id: "trade-2",
    status: "closed",
    exit_reason: "TP",
    exit_price: 3990,
    realized_pnl: 10,
  };
  const beforeCloseCount = shown.length;
  const closeModels = context.observeTradeActivity({
    production_execution: {
      fills: [entry, secondEntry, close],
      trades: [openTrade, closedTrade],
    },
  });
  assert.equal(closeModels.length, 1);
  assert.equal(shown.length, beforeCloseCount + 1);
  assert.equal(shown.at(-1).title, "交易完成 · 止盈");
  assert.equal(shown.at(-1).outcome, "profit");
});

test("trade notifications separate a partial reduction from the later final close", () => {
  const entry = {
    cycle_id: "cycle-1",
    fill_id: "entry-1",
    order_id: "order-1",
    trade_id: "trade-1",
    event: "entry",
    side: "buy",
    price: 3990,
    quantity: 1,
    ts: "2026-07-17T10:00:00Z",
  };
  const openTrade = {
    cycle_id: "cycle-1",
    trade_id: "trade-1",
    status: "open",
    side: "long",
    entry_price: 3990,
    remaining_units: 1,
  };
  const close = {
    ...entry,
    fill_id: "close-1",
    order_id: "order-1-TP",
    event: "target",
    side: "sell",
    price: 4000,
    quantity: 0.4,
    realized_pnl: -2,
  };
  const closedTrade = {
    ...openTrade,
    status: "closed",
    exit_reason: "TP",
    exit_price: 4000,
    realized_pnl: -2,
  };

  const fillFirst = tradeActivityHarness();
  fillFirst.context.observeTradeActivity({
    production_execution: {fills: [entry], trades: [openTrade]},
  });
  const fillModels = fillFirst.context.observeTradeActivity({
    production_execution: {
      fills: [entry, close],
      trades: [{...openTrade, remaining_units: 0.6}],
    },
  });
  assert.equal(fillModels.length, 1);
  assert.equal(fillModels[0].title, "减仓成交 · 止盈");
  assert.equal(fillModels[0].closesTrade, false);
  assert.equal(fillModels[0].outcome, "loss");
  assert.equal(
    fillFirst.context.observeTradeActivity({
      production_execution: {
        fills: [entry, close],
        trades: [{...openTrade, remaining_units: 0.6}],
      },
    }).length,
    0,
  );
  const finalModels = fillFirst.context.observeTradeActivity({
    production_execution: {fills: [entry, close], trades: [closedTrade]},
  });
  assert.equal(finalModels.length, 1);
  assert.equal(finalModels[0].title, "交易完成 · 止盈");
  assert.equal(finalModels[0].closesTrade, true);
  assert.match(finalModels[0].metric, /^0\.6 XAU @ 4000$/);
  assert.equal(
    fillFirst.context.observeTradeActivity({
      production_execution: {fills: [entry, close], trades: [closedTrade]},
    }).length,
    0,
  );

  const tradeFirst = tradeActivityHarness();
  tradeFirst.context.observeTradeActivity({
    production_execution: {fills: [entry], trades: [openTrade]},
  });
  assert.equal(
    tradeFirst.context.observeTradeActivity({
      production_execution: {fills: [entry], trades: [closedTrade]},
    }).length,
    1,
  );
  assert.equal(
    tradeFirst.context.observeTradeActivity({
      production_execution: {fills: [entry, close], trades: [closedTrade]},
    }).length,
    0,
  );
  assert.equal(
    tradeFirst.context.closingQuantityForTrade(
      {...closedTrade, trade_id: "trade-full", remaining_units: 0},
      [
        {...entry, fill_id: "entry-full", trade_id: "trade-full", quantity: 1},
        {...close, fill_id: "close-full", trade_id: "trade-full", quantity: 1},
      ],
    ),
    1,
  );
});

test("a full close waits for authoritative position state before notifying", () => {
  const {context, shown} = tradeActivityHarness();
  const entry = {
    cycle_id: "cycle-1",
    fill_id: "entry-1",
    order_id: "order-1",
    trade_id: "trade-1",
    event: "entry",
    side: "buy",
    price: 3990,
    quantity: 1,
  };
  const close = {
    ...entry,
    fill_id: "close-1",
    order_id: "order-1-TP",
    event: "target",
    side: "sell",
    price: 4000,
    realized_pnl: 10,
  };
  const openTrade = {
    cycle_id: "cycle-1",
    trade_id: "trade-1",
    status: "open",
    side: "long",
    entry_price: 3990,
    remaining_units: 1,
  };
  const closedTrade = {
    ...openTrade,
    status: "closed",
    remaining_units: 0,
    exit_price: 4000,
    exit_reason: "TP",
    realized_pnl: 10,
  };

  context.observeTradeActivity({
    production_execution: {fills: [entry], trades: [openTrade]},
  });
  assert.equal(
    context.observeTradeActivity({
      production_execution: {fills: [entry, close], trades: [openTrade]},
    }).length,
    0,
  );
  assert.equal(context.state.tradeActivity.seenFillKeys.has("cycle-1:close-1"), false);

  const confirmed = context.observeTradeActivity({
    production_execution: {fills: [entry, close], trades: [closedTrade]},
  });
  assert.equal(confirmed.length, 1);
  assert.equal(confirmed[0].title, "交易完成 · 止盈");
  assert.equal(confirmed[0].closesTrade, true);
  assert.equal(shown.length, 1);
  assert.equal(
    context.observeTradeActivity({
      production_execution: {fills: [entry, close], trades: [closedTrade]},
    }).length,
    0,
  );
});

test("batched partial and final fills produce one authoritative completion", () => {
  const {context, shown} = tradeActivityHarness();
  const entry = {
    cycle_id: "cycle-1",
    fill_id: "entry-1",
    order_id: "entry-order",
    trade_id: "trade-1",
    event: "entry",
    side: "buy",
    price: 3990,
    quantity: 1,
    ts: "2026-07-17T10:00:00Z",
  };
  const openTrade = {
    cycle_id: "cycle-1",
    trade_id: "trade-1",
    status: "open",
    side: "long",
    entry_price: 3990,
    remaining_units: 1,
  };
  const partial = {
    ...entry,
    fill_id: "reduce-1",
    order_id: "reduce-order",
    event: "exit",
    side: "sell",
    price: 3995,
    quantity: 0.4,
    ts: "2026-07-17T10:01:00Z",
  };
  const finalA = {
    ...entry,
    fill_id: "target-1",
    order_id: "target-order",
    event: "target",
    side: "sell",
    price: 4000,
    quantity: 0.3,
    ts: "2026-07-17T10:02:00Z",
  };
  const finalB = {
    ...finalA,
    fill_id: "target-2",
    quantity: 0.3,
    ts: "2026-07-17T10:02:01Z",
  };
  const closedTrade = {
    ...openTrade,
    status: "closed",
    remaining_units: 0,
    exit_price: 4000,
    exit_reason: "TP",
    realized_pnl: 6,
  };

  context.observeTradeActivity({
    production_execution: {fills: [entry], positions: [openTrade], trades: [openTrade]},
  });
  const models = context.observeTradeActivity({
    production_execution: {
      fills: [entry, partial, finalA, finalB],
      positions: [closedTrade],
      trades: [closedTrade],
    },
  });
  assert.equal(models.length, 1);
  assert.equal(models[0].title, "交易完成 · 止盈");
  assert.match(models[0].metric, /^0\.6 XAU @ 4000$/);
  assert.equal(shown.length, 1);
  assert.equal(
    context.closingQuantityForTrade(closedTrade, [entry, partial, finalA, finalB]),
    0.6,
  );
});

test("a cross-cycle closed history row can complete the prior-cycle toast", () => {
  const {context, shown} = tradeActivityHarness();
  const entry = {
    cycle_id: "2026-07-17_DAY",
    fill_id: "day-entry",
    order_id: "day-entry-order",
    trade_id: "day-trade",
    event: "entry",
    side: "buy",
    price: 3990,
    quantity: 1,
  };
  const openDay = {
    cycle_id: "2026-07-17_DAY",
    trade_id: "day-trade",
    status: "open",
    side: "long",
    remaining_units: 1,
    entry_price: 3990,
  };
  const target = {
    ...entry,
    fill_id: "day-target",
    order_id: "day-target-order",
    event: "target",
    side: "sell",
    price: 4000,
  };
  const closedDay = {
    ...openDay,
    status: "closed",
    remaining_units: 0,
    exit_price: 4000,
    exit_reason: "TP",
    realized_pnl: 10,
  };

  context.observeTradeActivity({
    production_execution: {fills: [entry], positions: [openDay], trades: [openDay]},
  });
  const models = context.observeTradeActivity({
    production_execution: {
      fills: [entry, target],
      positions: [],
      trades: [closedDay],
    },
  });
  assert.equal(models.length, 1);
  assert.equal(models[0].title, "交易完成 · 止盈");
  assert.equal(shown.length, 1);
});

test("authoritative positions can close a trade without a trades row or exit fill", () => {
  const {context} = tradeActivityHarness();
  const entryA = {
    cycle_id: "cycle-1",
    fill_id: "entry-a",
    trade_id: "trade-1",
    event: "entry",
    side: "buy",
    price: 3990,
    quantity: 0.4,
  };
  const entryB = {...entryA, fill_id: "entry-b", quantity: 0.6};
  const priorReduction = {
    ...entryA,
    fill_id: "reduce-1",
    event: "exit",
    side: "sell",
    price: 3995,
    quantity: 0.25,
  };
  const openPosition = {
    cycle_id: "cycle-1",
    position_id: "position-1",
    trade_id: "trade-1",
    status: "open",
    side: "long",
    remaining_units: 0.75,
    entry_price: 3990,
  };
  context.observeTradeActivity({
    production_execution: {
      fills: [entryA, entryB, priorReduction],
      trades: [],
      positions: [openPosition],
    },
  });
  const closed = context.observeTradeActivity({
    production_execution: {
      fills: [entryA, entryB, priorReduction],
      trades: [],
      positions: [
        {
          ...openPosition,
          status: "closed",
          remaining_units: 0,
          exit_price: 4000,
          realized_pnl: 5,
          exit_reason: "TP",
        },
      ],
    },
  });
  assert.equal(closed.length, 1);
  assert.equal(closed[0].title, "交易完成 · 止盈");
  assert.match(closed[0].metric, /^0\.75 XAU @ 4000$/);
});

test("two partial fills on the same order each create one entry notification", () => {
  const {context} = tradeActivityHarness();
  const baseline = {
    cycle_id: "cycle-1",
    fill_id: "baseline",
    order_id: "baseline-order",
    trade_id: "baseline-trade",
    event: "entry",
    side: "buy",
    price: 3990,
    quantity: 1,
  };
  context.observeTradeActivity({
    production_execution: {
      fills: [baseline],
      trades: [{cycle_id: "cycle-1", trade_id: "baseline-trade", status: "open"}],
    },
  });
  const partials = [
    {...baseline, fill_id: "partial-1", order_id: "shared-order", trade_id: "trade-2"},
    {...baseline, fill_id: "partial-2", order_id: "shared-order", trade_id: "trade-2"},
  ];
  assert.equal(
    context.observeTradeActivity({
      production_execution: {
        fills: [baseline, ...partials],
        trades: [
          {cycle_id: "cycle-1", trade_id: "baseline-trade", status: "open"},
          {cycle_id: "cycle-1", trade_id: "trade-2", status: "open"},
        ],
      },
    }).length,
    2,
  );
});
