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
const runtimeMatchesProductionPlan = extractedFunction(
  "runtimeMatchesProductionPlan",
  "createGridRangeDraft",
);
const gridDraftHandle = extractedFunction(
  "gridDraftHandle",
  "gridRangePreviewPayload",
);
const gridRangePreviewContractIssue = extractedFunction(
  "gridRangePreviewContractIssue",
  "executionIdentity",
);
const executionIdentity = extractedFunction(
  "executionIdentity",
  "gridReplacementPayload",
);

test("inline dashboard JavaScript parses", () => {
  const start = html.lastIndexOf("<script>") + "<script>".length;
  const end = html.indexOf("</script>", start);
  assert.ok(start > 0 && end > start);
  assert.doesNotThrow(() => new vm.Script(html.slice(start, end)));
});

test("middle drag preserves width and boundary drags preserve the opposite edge", () => {
  const moved = calculateGridDragBounds(
    {mode: "move", startLow: 3900, startHigh: 4000, startPrice: 3950},
    3960,
  );
  assert.equal(moved.low, 3910);
  assert.equal(moved.high, 4010);
  const upper = calculateGridDragBounds(
    {mode: "upper", startLow: 3900, startHigh: 4000, startPrice: 4000},
    4100,
  );
  assert.equal(upper.low, 3900);
  assert.equal(upper.high, 4100);
  const lower = calculateGridDragBounds(
    {mode: "lower", startLow: 3900, startHigh: 4000, startPrice: 3900},
    3850,
  );
  assert.equal(lower.low, 3850);
  assert.equal(lower.high, 4000);
});

test("adjustment mode requires runtime and active plan id plus version parity", () => {
  const current = {
    runtime: {
      actual_state: "running",
      strategy_plan_id: "plan-1",
      strategy_plan_version: 7,
    },
    strategy: {plan: {strategy_plan_id: "plan-1", version: 7}},
  };
  assert.equal(runtimeMatchesProductionPlan(current), true);
  assert.equal(
    runtimeMatchesProductionPlan({
      ...current,
      runtime: {...current.runtime, strategy_plan_version: 6},
    }),
    false,
  );
  assert.equal(
    runtimeMatchesProductionPlan({
      ...current,
      runtime: {...current.runtime, strategy_plan_id: "old-plan"},
    }),
    false,
  );
});

test("successive releases accumulate and mixed handles become one consolidated draft", () => {
  const original = {
    range: {low: 3900, high: 4000},
    grid: {count: 50, mode: "arithmetic", notionalPerGrid: 2000},
  };
  const first = calculateGridDragBounds(
    {mode: "upper", startLow: 3900, startHigh: 4000, startPrice: 4000},
    4100,
  );
  const second = calculateGridDragBounds(
    {...first, mode: "lower", startLow: first.low, startHigh: first.high, startPrice: 3900},
    3880,
  );
  assert.equal(second.low, 3880);
  assert.equal(second.high, 4100);
  assert.equal(gridDraftHandle({...second, original}), "draft");
});

function validDraft() {
  return {
    expectedPlanId: "plan-1",
    expectedPlanVersion: 7,
    low: 3900,
    high: 4100,
    original: {
      range: {low: 3900, high: 4000},
      grid: {count: 50, mode: "arithmetic", notionalPerGrid: 2000},
    },
  };
}

function validPreview() {
  return {
    schema_version: "grid-range-drag-preview-v1",
    expected_strategy_plan_id: "plan-1",
    expected_strategy_plan_version: 7,
    preview_id: "preview-1",
    geometry: {new_range: {low: 3900, high: 4100}},
    old: {
      range_low: 3900,
      range_high: 4000,
      mode: "arithmetic",
      grid_count: 50,
      notional_per_grid: 2000,
    },
    new: {
      range_low: 3900,
      range_high: 4100,
      mode: "arithmetic",
      grid_count: 50,
      notional_per_grid: 2000,
    },
    can_apply: true,
    confirm_disabled_reasons: [],
    risk_recalculation: {applied_to_preview: false},
    order_delta: {cancel_pending_entries: 25, submit_new_entries: 50},
    positions: {preview_effect: "none"},
    tp_sl: {
      existing_orders_affected_by_preview: false,
      candidate_orders_recomputed: true,
    },
    side_effects: {
      orders_created: 0,
      orders_cancelled: 0,
      positions_changed: 0,
      strategy_plan_written: false,
      risk_decision_persisted: false,
    },
  };
}

test("range preview rejects implicit sizing changes and any side effect", () => {
  assert.equal(gridRangePreviewContractIssue(validPreview(), validDraft()), "");
  const resized = validPreview();
  resized.new.grid_count = 51;
  assert.equal(
    gridRangePreviewContractIssue(resized, validDraft()),
    "网格数量或模式发生了隐式变化",
  );
  const mutated = validPreview();
  mutated.side_effects.orders_cancelled = 1;
  assert.equal(
    gridRangePreviewContractIssue(mutated, validDraft()),
    "只读预览缺少零副作用证明",
  );
});

test("pointer release retains the draft without opening a card or calling the backend", () => {
  const start = html.indexOf("function finishGridDrag");
  const end = html.indexOf("\nfunction ensureGridAdjustOverlay", start);
  const body = html.slice(start, end);
  assert.match(body, /state\.gridDrag=null/);
  assert.doesNotMatch(body, /requestGridRangePreview|showModal|control\(/);
});

test("replacement confirmation has the exact destructive label and sends stable execution ids", () => {
  assert.match(
    html,
    /id="executeGridRangeReplacement"[^>]*>停止\+平仓\+撤单\+交易新网格<\/button>/,
  );
  const identity = executionIdentity({
    execution: {
      open_orders: [{order_id: "order-b"}, {order_id: "order-a"}],
      open_positions: [{position_id: "position-2"}, {trade_id: "trade-1"}],
    },
  });
  assert.deepEqual(JSON.parse(JSON.stringify(identity)), {
    accepted_order_ids: ["order-a", "order-b"],
    open_position_ids: ["position-2", "trade-1"],
  });
  assert.match(html, /expected_preview_id:preview\.preview_id/);
  assert.match(html, /control\("replace_grid",payload/);
});

test("replacement confirmation fails closed on ambiguous execution identity", () => {
  assert.throws(
    () => executionIdentity({execution: {open_orders: [{order_id: "same"}, {order_id: "same"}], open_positions: []}}),
    /缺少唯一身份/,
  );
  assert.match(html, /再次核对计划版本、行情、风险、挂单和持仓/);
  assert.match(html, /确认后全部平仓/);
  assert.match(html, /随旧持仓撤销；新网格重建/);
});
