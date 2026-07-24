import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import test from "node:test";
import {fileURLToPath} from "node:url";
import {dirname, resolve} from "node:path";
import vm from "node:vm";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const html = readFileSync(resolve(root, "dashboard-gridmind.html"), "utf8");

function reviewHarness() {
  const context = vm.createContext({
    String,
    Number,
    Set,
    DIR: {long: "做多", short: "做空", neutral: "中性"},
    finiteNumber: (value) =>
      value !== null && value !== "" && Number.isFinite(Number(value))
        ? Number(value)
        : null,
    num: (value) => String(value),
    signed: (value) => `${Number(value) > 0 ? "+" : ""}${Number(value)}`,
  });
  const start = html.indexOf("function reviewNumber");
  const end = html.indexOf("\nfunction renderReview", start);
  assert.ok(start >= 0 && end > start);
  vm.runInContext(html.slice(start, end), context);
  return context;
}

function plan(overrides = {}) {
  return {
    strategy_plan_id: "plan-1",
    version: 4,
    direction: "neutral",
    style: "steady",
    range: {low: 3900, high: 4100},
    key_levels: [3950, 4050],
    tp_sl: {take_profit: "one-grid", stop_loss: "range-boundary"},
    grid: {
      mode: "arithmetic",
      count: 50,
      spacing: 4,
      notional_per_grid: 2000,
      leverage: 3,
      out_of_range: "stop",
    },
    ...overrides,
  };
}

function shadow(variantId, netPnl, overrides = {}) {
  return {
    variant_id: variantId,
    status: "pass",
    metrics: {net_pnl: netPnl},
    scenario: {
      plan_identity: {
        strategy_plan_id: "plan-1",
        strategy_plan_version: 4,
      },
      evaluation_window: {
        started_at: "2026-07-20T00:00:00Z",
        ended_at: "2026-07-20T12:00:00Z",
        event_count: 720,
      },
      contracts: {
        execution_contract_hash: "sha256:execution",
        fee_contract_hash: "sha256:fees",
      },
      hashes: {market_event_hash: "sha256:events"},
    },
    ...overrides,
  };
}

test("missing review numbers remain unknown while real zero stays zero", () => {
  const context = reviewHarness();
  assert.equal(context.reviewNumber(null), "未知");
  assert.equal(context.reviewSigned(undefined), "未知");
  assert.equal(context.reviewNumber(0), "0");
  assert.equal(context.reviewSigned(0), "0");
});

test("a plan links to exactly one source proposal and its matching review track", () => {
  const context = reviewHarness();
  const locked = {...plan(), source_proposal_ids: ["proposal-1"]};
  const packageRow = {
    proposals: [{proposal_id: "proposal-1", source: "human", ...plan()}],
  };
  const ledger = {human_review: {direction_review: {decision: "neutral"}}, machine_review: {}};

  assert.equal(context.reviewProposalForPlan(packageRow, locked).proposal_id, "proposal-1");
  assert.equal(context.linkedReviewForPlan(packageRow, ledger, locked).track, "human");
  assert.equal(
    context.reviewProposalForPlan(
      {proposals: [...packageRow.proposals, {...packageRow.proposals[0]}]},
      locked,
    ),
    null,
  );
  assert.equal(context.linkedReviewForPlan({proposals: []}, ledger, locked).track, "unknown");
});

test("spec comparison fails closed on missing or changed exit policy", () => {
  const context = reviewHarness();
  assert.equal(context.reviewPlanSpecComplete(plan()), true);
  assert.equal(
    context.reviewPlanSpecComplete(plan({grid: {...plan().grid, out_of_range: ""}})),
    false,
  );
  assert.equal(context.sameReviewPlanSpec(plan(), plan()), true);
  assert.equal(
    context.sameReviewPlanSpec(
      plan(),
      plan({grid: {...plan().grid, out_of_range: "close"}}),
    ),
    false,
  );
});

test("direction judgment never grades a different strategy direction", () => {
  const context = reviewHarness();
  const mismatch = context.reviewDirectionAssessment(plan(), {decision: "long", hit: true});
  const missing = context.reviewDirectionAssessment(plan(), {});
  const hit = context.reviewDirectionAssessment(plan(), {decision: "neutral", hit: true});

  assert.equal(mismatch.verdict, "口径不一致");
  assert.equal(mismatch.kind, "info");
  assert.equal(missing.verdict, "证据不足");
  assert.equal(hit.verdict, "命中");
  assert.equal(hit.kind, "good");
});

test("key-level and TP/SL judgments require the reviewed and locked plans to match", () => {
  const context = reviewHarness();
  const locked = plan();
  const reviewed = plan();
  assert.equal(
    context.reviewKeyLevelAssessment(locked, reviewed, {planned_count: 2, touched_count: 2}).verdict,
    "全部触及",
  );
  assert.equal(
    context.reviewKeyLevelAssessment(
      locked,
      {...reviewed, key_levels: [3940, 4060]},
      {planned_count: 2, touched_count: 2},
    ).verdict,
    "口径不一致",
  );
  assert.equal(
    context.reviewTpSlAssessment(locked, reviewed, {order_count: 2, valid_geometry_count: 2}).verdict,
    "几何完整",
  );
  assert.equal(
    context.reviewTpSlAssessment(
      locked,
      {...reviewed, tp_sl: {...reviewed.tp_sl, stop_loss: "atr"}},
      {order_count: 2, valid_geometry_count: 2},
    ).verdict,
    "口径不一致",
  );
});

test("same-cycle shadows prefer the latest read-model evidence and dedupe scenarios", () => {
  const context = reviewHarness();
  const packageRow = {
    cycle_id: "cycle-2",
    strategy_shadows: [
      {cycle_id: "cycle-2", scenario_id: "base", marker: "package"},
      {cycle_id: "cycle-1", scenario_id: "old"},
    ],
  };
  const data = {
    research: {
      strategy_shadows: [
        {cycle_id: "cycle-2", scenario_id: "base", marker: "latest"},
        {cycle_id: "cycle-2", scenario_id: "candidate"},
      ],
    },
  };

  const rows = context.uniqueSameCycleShadows(data, packageRow);
  assert.equal(rows.length, 2);
  assert.equal(rows[0].marker, "latest");
  assert.ok(rows.every((row) => row.cycle_id === "cycle-2"));
});

test("the review and shadows share the server-selected closed cycle", () => {
  const context = reviewHarness();
  const data = {
    review: {
      selected_cycle_id: "cycle-reviewed",
      cycle_packages: [
        {cycle_id: "cycle-newer", status: "closed"},
        {cycle_id: "cycle-reviewed", status: "closed"},
      ],
    },
  };

  assert.equal(context.selectedReviewPackage(data).cycle_id, "cycle-reviewed");
  assert.equal(
    context.selectedReviewPackage({...data, review: {...data.review, selected_cycle_id: "cycle-newer"}}).cycle_id,
    "cycle-newer",
  );
  assert.equal(
    context.selectedReviewPackage({...data, review: {...data.review, selected_cycle_id: "missing"}}),
    null,
  );
});

test("counterfactual comparison requires a successful production replay", () => {
  const context = reviewHarness();
  const packageRow = {strategy_plan: plan()};
  const blocked = context.reviewCounterfactualAssessment([
    shadow("production", 10, {status: "blocked"}),
    shadow("ai", 20),
  ], packageRow);
  assert.equal(blocked.verdict, "不可比较");
  assert.equal(blocked.delta, null);

  const comparable = context.reviewCounterfactualAssessment([
    shadow("production", 10),
    shadow("ai", 13),
  ], packageRow);
  assert.equal(comparable.verdict, "候选更优");
  assert.equal(comparable.delta, 3);

  const wrongHistory = shadow("ai", 30);
  wrongHistory.scenario.hashes.market_event_hash = "sha256:other-events";
  const rejected = context.reviewCounterfactualAssessment([
    shadow("production", 10),
    wrongHistory,
  ], packageRow);
  assert.equal(rejected.verdict, "不可比较");
  assert.equal(rejected.delta, null);

  const wrongPlan = shadow("production", 10);
  wrongPlan.scenario.plan_identity.strategy_plan_version = 3;
  assert.equal(
    context.reviewCounterfactualAssessment([wrongPlan, shadow("ai", 13)], packageRow).verdict,
    "不可比较",
  );
  assert.equal(
    context.shadowPresentation(wrongPlan, false).title,
    "production 重放（不可作基准）",
  );
});

test("promotion evidence remains an explicit human-confirmed proposal", () => {
  const context = reviewHarness();
  const ready = context.promotionPresentation({
    candidates: [{
      variant_id: "notional-half",
      status: "proposal_ready",
      comparable_period_count: 2,
      comparable_trade_count: 100,
      metrics: {realized_pnl_delta: 8, max_drawdown_delta: 0, cost_delta: 0},
      evidence: [{cycle_id: "A", evidence_id: "proof", evaluation_window: {}, contracts: {}}],
    }],
  });
  assert.equal(ready.kind, "good");
  assert.match(ready.text, /不会自动改策略或下单/);

  const blocked = context.promotionPresentation({
    candidates: [{variant_id: "notional-half", status: "not_comparable", blockers: ["comparable_trade_sample_insufficient"]}],
  });
  assert.equal(blocked.kind, "info");
  assert.match(blocked.text, /comparable_trade_sample_insufficient/);
});
