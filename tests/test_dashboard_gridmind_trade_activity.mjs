import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import test from "node:test";
import {fileURLToPath} from "node:url";
import {dirname, resolve} from "node:path";
import vm from "node:vm";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const html = readFileSync(resolve(root, "dashboard-gridmind.html"), "utf8");

function activityHarness() {
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
    finiteNumber: (value) =>
      value !== null && value !== "" && Number.isFinite(Number(value))
        ? Number(value)
        : null,
    displayTradingPair: () => "XAU / USDT",
    quantityText: (value) => String(value ?? "--"),
    num: (value) => String(value ?? "--"),
    signed: (value) => `${Number(value) > 0 ? "+" : ""}${Number(value)}`,
    beijingDateTime: (value) => String(value ?? "--"),
    showTradeToast: (model) => shown.push(model),
  });
  const activityStart = html.indexOf("const CLOSE_FILL_EVENTS=");
  const audioStart = html.indexOf("\nfunction armTradeAudio", activityStart);
  assert.ok(activityStart >= 0 && audioStart > activityStart);
  vm.runInContext(html.slice(activityStart, audioStart), context);
  const observeStart = html.indexOf("function observeTradeActivity", audioStart);
  const renderTablesStart = html.indexOf("\nfunction renderTables", observeStart);
  assert.ok(observeStart >= 0 && renderTablesStart > observeStart);
  vm.runInContext(html.slice(observeStart, renderTablesStart), context);
  return {context, shown};
}

function snapshot({
  fills = [],
  trades = [],
  positions = [],
  trusted = true,
  currentTrusted = trusted,
} = {}) {
  return {
    execution: {
      fills,
      trades,
      positions,
      accounting: {
        completeness: {status: trusted ? "complete" : "degraded"},
        reconciliation: {status: trusted ? "pass" : "fail"},
      },
      current_accounting: {
        completeness: {status: currentTrusted ? "complete" : "degraded"},
        reconciliation: {status: currentTrusted ? "pass" : "fail"},
      },
    },
  };
}

const entry = {
  cycle_id: "cycle-1",
  strategy_plan_id: "plan-1",
  fill_id: "entry-1",
  order_id: "order-1",
  trade_id: "trade-1",
  event: "entry",
  side: "buy",
  price: 3990,
  quantity: 1,
  ts: "2026-07-21T10:00:00Z",
};
const openTrade = {
  cycle_id: "cycle-1",
  strategy_plan_id: "plan-1",
  trade_id: "trade-1",
  status: "open",
  side: "long",
  entry_price: 3990,
  remaining_units: 1,
};

test("only the first authoritative snapshot establishes the silent baseline", () => {
  const {context, shown} = activityHarness();
  const history = snapshot({fills: [entry], trades: [openTrade]});

  assert.equal(
    context.observeTradeActivity(snapshot({fills: [entry], trades: [openTrade], trusted: false})).length,
    0,
  );
  assert.equal(context.state.tradeActivity.initialized, false);
  assert.equal(context.observeTradeActivity(history).length, 0);
  assert.equal(context.state.tradeActivity.initialized, true);
  assert.equal(context.observeTradeActivity(history).length, 0);
  assert.equal(shown.length, 0);
});

test("cycle plus fill identity dedupes history but preserves partial fills", () => {
  const {context} = activityHarness();
  assert.equal(context.fillStableKey({fill_id: "f-1"}), "");
  assert.equal(
    context.fillStableKey({cycle_id: "cycle-1", fill_id: "f-1"}),
    "cycle-1:f-1",
  );
  assert.notEqual(
    context.fillStableKey({cycle_id: "cycle-1", fill_id: "f-1"}),
    context.fillStableKey({cycle_id: "cycle-2", fill_id: "f-1"}),
  );

  context.observeTradeActivity(snapshot({fills: [entry], trades: [openTrade]}));
  const partials = [
    {...entry, fill_id: "entry-2a", order_id: "shared-order", trade_id: "trade-2"},
    {...entry, fill_id: "entry-2b", order_id: "shared-order", trade_id: "trade-2"},
  ];
  const trade2 = {...openTrade, trade_id: "trade-2"};
  const models = context.observeTradeActivity(
    snapshot({fills: [entry, ...partials], trades: [openTrade, trade2]}),
  );
  assert.equal(models.length, 2);
  assert.ok(models.every((model) => model.title === "网格成交 · 买入"));
  assert.equal(
    context.observeTradeActivity(
      snapshot({fills: [entry, ...partials], trades: [openTrade, trade2]}),
    ).length,
    0,
  );
});

test("partial close waits for authoritative remaining units and final close toasts once", () => {
  const {context, shown} = activityHarness();
  const reduction = {
    ...entry,
    fill_id: "reduce-1",
    order_id: "reduce-order",
    event: "target",
    side: "sell",
    price: 4000,
    quantity: 0.4,
  };
  context.observeTradeActivity(snapshot({fills: [entry], trades: [openTrade]}));

  assert.equal(
    context.observeTradeActivity(
      snapshot({fills: [entry, reduction], trades: [openTrade]}),
    ).length,
    0,
  );
  assert.equal(context.state.tradeActivity.seenFillKeys.has("cycle-1:reduce-1"), false);

  const partial = {...openTrade, remaining_units: 0.6};
  const partialModels = context.observeTradeActivity(
    snapshot({fills: [entry, reduction], trades: [partial]}),
  );
  assert.equal(partialModels.length, 1);
  assert.equal(partialModels[0].title, "减仓成交 · 止盈");

  const closed = {
    ...openTrade,
    status: "closed",
    remaining_units: 0,
    exit_price: 4000,
    close_reason: "tp",
    realized_pnl: 10,
  };
  const finalModels = context.observeTradeActivity(
    snapshot({fills: [entry, reduction], trades: [closed]}),
  );
  assert.equal(finalModels.length, 1);
  assert.equal(finalModels[0].title, "交易完成 · 止盈");
  assert.equal(finalModels[0].closesTrade, true);
  assert.equal(shown.length, 2);
  assert.equal(
    context.observeTradeActivity(snapshot({fills: [entry, reduction], trades: [closed]})).length,
    0,
  );
});

test("fill and trade closing in one snapshot produce one completion toast", () => {
  const {context, shown} = activityHarness();
  context.observeTradeActivity(snapshot({fills: [entry], trades: [openTrade]}));
  const target = {
    ...entry,
    fill_id: "target-1",
    order_id: "target-order",
    event: "target",
    side: "sell",
    price: 4003,
    quantity: 1,
  };
  const closed = {
    ...openTrade,
    status: "closed",
    remaining_units: 0,
    exit_price: 4003,
    close_reason: "tp",
    realized_pnl: 3,
  };

  const models = context.observeTradeActivity(
    snapshot({fills: [entry, target], trades: [closed]}),
  );
  assert.equal(models.length, 1);
  assert.equal(models[0].title, "交易完成 · 止盈");
  assert.equal(shown.length, 1);
});

test("degraded current accounting cannot authorize a position reduction toast", () => {
  const {context} = activityHarness();
  const reduction = {
    ...entry,
    fill_id: "reduce-current-1",
    event: "exit",
    side: "sell",
    quantity: 0.5,
  };
  const reducedPosition = {...openTrade, remaining_units: 0.5};
  context.observeTradeActivity(snapshot({fills: [entry], trades: [openTrade]}));

  assert.equal(
    context.observeTradeActivity(
      snapshot({
        fills: [entry, reduction],
        trades: [openTrade],
        positions: [reducedPosition],
        currentTrusted: false,
      }),
    ).length,
    0,
  );
  assert.equal(
    context.state.tradeActivity.seenFillKeys.has("cycle-1:reduce-current-1"),
    false,
  );
  assert.equal(
    context.observeTradeActivity(
      snapshot({fills: [entry, reduction], trades: [openTrade], positions: [reducedPosition]}),
    )[0].title,
    "减仓成交 · 平仓",
  );
});

test("a prior-cycle trade can close after the current cycle changes", () => {
  const {context} = activityHarness();
  const oldEntry = {...entry, cycle_id: "old-cycle", fill_id: "old-entry"};
  const oldOpen = {...openTrade, cycle_id: "old-cycle"};
  context.observeTradeActivity(snapshot({fills: [oldEntry], trades: [oldOpen]}));
  const oldTarget = {
    ...oldEntry,
    fill_id: "old-target",
    event: "target",
    side: "sell",
    price: 4003,
  };
  const oldClosed = {
    ...oldOpen,
    status: "closed",
    remaining_units: 0,
    exit_price: 4003,
    close_reason: "tp",
  };

  const models = context.observeTradeActivity(
    snapshot({fills: [oldEntry, oldTarget], trades: [oldClosed]}),
  );
  assert.equal(models.length, 1);
  assert.equal(models[0].title, "交易完成 · 止盈");
});

test("same trade id in two cycles cannot overlay the prior-cycle close", () => {
  const {context} = activityHarness();
  const oldEntry = {...entry, cycle_id: "old-cycle", fill_id: "old-entry"};
  const newEntry = {...entry, cycle_id: "new-cycle", fill_id: "new-entry"};
  const oldOpen = {...openTrade, cycle_id: "old-cycle"};
  const newOpen = {...openTrade, cycle_id: "new-cycle"};
  context.observeTradeActivity(
    snapshot({fills: [oldEntry, newEntry], trades: [oldOpen, newOpen], positions: [newOpen]}),
  );

  const oldTarget = {
    ...oldEntry,
    fill_id: "old-target",
    event: "target",
    side: "sell",
    price: 4003,
  };
  const oldClosed = {
    ...oldOpen,
    status: "closed",
    remaining_units: 0,
    exit_price: 4003,
    close_reason: "tp",
  };
  const models = context.observeTradeActivity(
    snapshot({
      fills: [oldEntry, oldTarget, newEntry],
      trades: [oldClosed, newOpen],
      positions: [newOpen],
    }),
  );
  assert.equal(models.length, 1);
  assert.equal(models[0].title, "交易完成 · 止盈");
});

test("audio silently degrades when Web Audio is unavailable", () => {
  const context = vm.createContext({
    window: {},
    Date,
    state: {tradeAudio: null, lastTradeSoundAt: 0},
  });
  const start = html.indexOf("function armTradeAudio");
  const end = html.indexOf("\nfunction removeTradeToast", start);
  vm.runInContext(html.slice(start, end), context);
  assert.equal(context.armTradeAudio(), null);
  assert.doesNotThrow(() => context.playTradeChime("target"));
});
