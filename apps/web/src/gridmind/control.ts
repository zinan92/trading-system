/* GridMind's control layer (#1418): the request bodies, the gates in front of them, what an interrupted
 * request turned out to do, and the preview, start, stop and range-replacement flows. Ported from
 * gridmind.js at 3e116a7; legacy-control-layer.json holds what the old functions returned for a table of
 * inputs, compared as JSON strings so key order counts, and control.test.ts holds these to it.
 *
 * ADR 0008: apps/web never sends a control request. This module builds the body and decides; the one
 * call that sends it stays in gridmind.js (`control()`, handed in as `host.control`). The flows run on
 * the host's `state` object, which gridmind.js still owns, and change it exactly as gridmind.js did.
 * No island imports this module (islands.test.tsx checks). */
import {isDcaType} from "./chart";
import {num} from "./format";
import {apiFailure, OPERATOR_FAILURE_COPY, tradingFailure, type ApiFailure} from "./read";

// Plans, previews, read models and runtime events are open-ended JSON shapes; the functions read them defensively.
/* eslint-disable @typescript-eslint/no-explicit-any */
type Json = any;

/** The start form's inputs, as their `.value` strings. */
export interface StrategyForm {
  rangeLow: string; rangeHigh: string; gridCount: string; gridProfitTarget: string; gridNotional: string;
  dcaTarget: string; dcaStop: string; leverage: string;
}

export interface Problem { fields: string[]; title: string; detail: string }

// --- the start form ------------------------------------------------------------------------------

/** An old long- or short-only grid read as the neutral grid it migrates to; null for any other plan. */
export function legacySingleSidePlanMigration(plan: Json): Json | null {
  const range = plan?.range || {}, grid = plan?.grid || {}, direction = String(plan?.direction || "").toLowerCase();
  const low = Number(range.low), high = Number(range.high), count = Number(grid.count), planPrice = Number(plan?.execution_context?.market?.price);
  const splitPrice = Number.isFinite(Number(range.split_price)) ? Number(range.split_price) : Number.isFinite(planPrice) ? planPrice : (low + high) / 2;
  if (!["long", "short"].includes(direction) || String(range.scope || "") || !Number.isFinite(low) || !Number.isFinite(high) || high <= low
    || !Number.isInteger(count) || count < 2) return null;
  let executableLow = low, executableHigh = high;
  if (Number.isFinite(splitPrice) && low < splitPrice && splitPrice < high) {
    if (direction === "long") executableHigh = splitPrice; else executableLow = splitPrice;
  }
  return {schema_version: "legacy-single-side-grid-migration-v1", direction, legacy_grid_count: count, executable_grid_count: Math.ceil(count / 2),
    split_price: splitPrice, legacy_range: {low, high}, executable_range: {low: executableLow, high: executableHigh}};
}

export interface StrategyContext {
  /** gridmind.js's isDca(): the strategy type picked in the form. */
  dca: boolean;
  direction: Json; style: Json; gridMode: Json;
  parameterLocks: Set<string>;
  /** The production plan (`state.data?.strategy?.plan || {}`). */
  currentPlan: Json;
}

/** The strategy part of a preview, prepare_start or start body. Only locked parameters are sent; the
 * solver fills the rest. Key order is part of the wire (gridmind_control_wire.json). */
export function strategyPayload(form: StrategyForm, ctx: StrategyContext): Json {
  if (ctx.dca) {
    const low = Number(form.rangeLow), high = Number(form.rangeHigh), count = Number(form.gridCount), notional = Number(form.gridNotional);
    const target = Number(form.dcaTarget), stop = Number(form.dcaStop), leverage = Number(form.leverage);
    if (!["long", "short"].includes(ctx.direction)) throw Error("DCA 只能选择做多或做空。");
    if (!Number.isFinite(low) || !Number.isFinite(high) || high <= low) throw Error("DCA 加仓下边界必须小于上边界。");
    if (!Number.isInteger(count) || count < 1) throw Error("DCA 加仓次数必须是正整数。");
    if (!(notional > 0)) throw Error("每次加仓名义必须大于 0。");
    if (!(target > 0) || !(stop > 0)) throw Error("DCA 止盈价和止损价必须大于 0。");
    if (!(leverage > 0)) throw Error("杠杆必须大于 0。");
    const step = count === 1 ? 0 : (high - low) / (count - 1);
    const entryLevels = Array.from({length: count}, (_, index) => ctx.direction === "long" ? high - index * step : low + index * step);
    return {strategy_type: "dca", direction: ctx.direction, dca: {entry_levels: entryLevels, target_price: target, stop_price: stop,
      notional_per_addition: notional, max_additions: count, loop_enabled: false}, risk_budget: {leverage}};
  }
  const locks = ctx.parameterLocks;
  const payload: Json = {direction: ctx.direction, style: ctx.style, out_of_range: "pause_keep_orders"};
  const locked = [...locks].sort(), grid: Json = {mode: ctx.gridMode, notional_mode: "auto", out_of_range: payload.out_of_range, trailing_up: false};
  if (locks.has("range")) {
    const low = Number(form.rangeLow), high = Number(form.rangeHigh);
    if (!Number.isFinite(low) || !Number.isFinite(high) || high <= low) throw Error("下边界必须小于上边界。");
    payload.range = {low, high};
  }
  if (locks.has("grid_count")) { const value = Number(form.gridCount); if (!Number.isInteger(value)) throw Error("网格数量必须是整数。"); grid.count = value; }
  if (locks.has("profit_target")) { const value = Number(form.gridProfitTarget); if (!(value > 0)) throw Error("每格净利目标必须大于 0。"); grid.target_net_profit_per_grid_usd = value; }
  if (locks.has("notional_per_grid")) { const value = Number(form.gridNotional); if (!(value > 0)) throw Error("每格名义必须大于 0。"); grid.notional_per_grid = value; grid.notional_mode = "manual"; }
  if (locks.has("leverage")) { const value = Number(form.leverage); if (!(value > 0)) throw Error("杠杆必须大于 0。"); payload.risk_budget = {leverage: value}; }
  const currentPlan = ctx.currentPlan, legacyMigration = legacySingleSidePlanMigration(currentPlan);
  payload.grid = grid;
  payload.solver = {mode: "manual_adaptive", locked, current_grid_count: Number(currentPlan?.grid?.count) || null,
    current_direction: legacyMigration ? "neutral" : String(currentPlan?.direction || ctx.direction)};
  return payload;
}

export function parameterProblem({fields = [], title, detail}: {fields?: string[]; title: string; detail: string}): Problem {
  return {fields, title, detail};
}

/** The first thing wrong with the form, or null. A problem keeps the form from being sent at all. */
export function invalidStrategyDraft(form: StrategyForm, {dca, parameterLocks}: {dca: boolean; parameterLocks: Set<string>}): Problem | null {
  const locked = (name: string) => parameterLocks.has(name), rangeFields = ["#rangeLow", "#rangeHigh"];
  const positive = (value: string) => Number(value) > 0;
  if (dca) {
    const low = Number(form.rangeLow), high = Number(form.rangeHigh);
    if (!Number.isFinite(low) || !Number.isFinite(high) || high <= low) return parameterProblem({fields: rangeFields, title: "加仓区间无效", detail: "下边界必须小于上边界。请填写有效价格，或点击“根据当前行情智能填充 DCA”。"});
    if (!Number.isInteger(Number(form.gridCount)) || Number(form.gridCount) < 1) return parameterProblem({fields: ["#gridCount"], title: "加仓次数无效", detail: "最多加仓次数必须是正整数。"});
    if (!positive(form.gridNotional)) return parameterProblem({fields: ["#gridNotional"], title: "每次加仓名义缺失", detail: "每次加仓名义不能为空且必须大于 0 USD。"});
    if (!positive(form.dcaTarget) || !positive(form.dcaStop)) return parameterProblem({fields: ["#dcaTarget", "#dcaStop"], title: "整轮止盈或止损价无效", detail: "止盈价和止损价都必须大于 0。"});
    if (!positive(form.leverage)) return parameterProblem({fields: ["#leverage"], title: "杠杆无效", detail: "杠杆必须大于 0。"});
    return null;
  }
  if (locked("range")) {
    const low = Number(form.rangeLow), high = Number(form.rangeHigh);
    if (!Number.isFinite(low) || !Number.isFinite(high) || high <= low) return parameterProblem({fields: rangeFields, title: "Range 无效", detail: "下边界必须小于上边界。请填写有效价格，或点击“根据当前行情智能填充 Range”恢复自动求解。"});
  }
  if (locked("grid_count") && (!Number.isInteger(Number(form.gridCount)) || Number(form.gridCount) < 1)) return parameterProblem({fields: ["#gridCount"], title: "网格数量无效", detail: "网格数量必须是正整数；点击“手动”可恢复 AUTO 求解。"});
  if (locked("profit_target") && !positive(form.gridProfitTarget)) return parameterProblem({fields: ["#gridProfitTarget"], title: "每格净利目标无效", detail: "每格净利目标不能为空且必须大于 0 USD；点击“手动”可恢复 AUTO 求解。"});
  if (locked("notional_per_grid") && !positive(form.gridNotional)) return parameterProblem({fields: ["#gridNotional"], title: "每格名义缺失", detail: "每格名义不能为空且必须大于 0 USD。请填写金额，或点击“手动”恢复 AUTO 求解。"});
  if (locked("leverage") && !positive(form.leverage)) return parameterProblem({fields: ["#leverage"], title: "杠杆无效", detail: "杠杆必须大于 0；点击“手动”可恢复 AUTO 求解。"});
  return null;
}

/** Which input a failed preview points at. */
export function previewProblemFromError(error: Json): Problem {
  const message = String(error?.message || error || "参数预览未完成。");
  if (/notional_per_grid|每格名义/.test(message)) return parameterProblem({fields: ["#gridNotional"], title: "每格名义缺失", detail: "每格名义不能为空且必须大于 0 USD。请填写金额，或点击“手动”恢复 AUTO 求解。"});
  if (/grid.?count|网格数量/.test(message)) return parameterProblem({fields: ["#gridCount"], title: "网格数量无效", detail: "请输入正整数，或点击“手动”恢复 AUTO 求解。"});
  if (/leverage|杠杆/.test(message)) return parameterProblem({fields: ["#leverage"], title: "杠杆无效", detail: "杠杆必须大于 0。"});
  return parameterProblem({title: "参数预览未完成", detail: message});
}

// --- the control request ---------------------------------------------------------------------------

/** The body of a control request, or the reason it may not be sent. Only a preview may go out without control access. */
export function controlRequestBody(action: string, payload: Json, {accessControlled, cycleId}: {accessControlled: boolean; cycleId: Json}): string {
  if (!accessControlled && action !== "preview") throw Error("需要先使用获授权的 Cloudflare 账号登录。");
  if (!cycleId) throw Error("当前策略周期尚未读取完成。");
  return JSON.stringify({cycle_id: cycleId, action, ...payload});
}

/** A dropped connection or a 5xx leaves the outcome unknown: the runtime's audit receipt decides it. */
export function isUncertainControlError(error: Json): boolean {
  const status = Number(error?.status);
  return !Number.isFinite(status) || status === 0 || status >= 500;
}

export function controlEventFingerprint(event: Json): string {
  return event && typeof event === "object" ? [event.ts, event.action, event.result, event.error].map(value => String(value ?? "")).join("|") : "";
}

export function newerControlEvent(runtime: Json, action: string, afterEvent = ""): Json | null {
  const event = runtime?.last_control_event;
  if (!event || String(event.action || "") !== String(action) || controlEventFingerprint(event) === String(afterEvent || "")) return null;
  return event;
}

export function auditedControlAcceptance(runtime: Json, action: string, afterEvent = ""): Json | null {
  const event = newerControlEvent(runtime, action, afterEvent);
  return event && String(event.result || "") === "accepted" ? event : null;
}

export function auditedControlRejection(runtime: Json, action: string, afterEvent = ""): ApiFailure | null {
  const event = newerControlEvent(runtime, action, afterEvent);
  if (!event || String(event.result || "") !== "rejected") return null;
  const reason = String(event.error || "").trim();
  return reason ? apiFailure(reason, 409, "audited_control_rejection") : null;
}

export type ControlAttempt =
  | {settled: "done"; result: Json; reset: boolean}
  | {settled: "failed"; error: ApiFailure};

/** What one fresh read model says about an interrupted request: done (and whether the start form resets),
 * failed, or null to read again. */
export function controlAttempt(action: string, latest: Json, afterEvent = ""): ControlAttempt | null {
  const runtime = latest?.runtime || {}, actual = runtime.actual_state || runtime.desired_state || "stopped", counts = latest?.execution?.counts || {};
  const accepted = Number(counts.accepted_order_count), openOrders = Number(counts.open_order_count), open = Number(counts.open_position_count);
  const plan = latest?.strategy?.plan || {};
  const samePlan = String(runtime.strategy_plan_id || "") === String(plan.strategy_plan_id || "") && Number(runtime.strategy_plan_version) === Number(plan.version);
  const receipt = auditedControlAcceptance(runtime, action, afterEvent);
  const rejection = auditedControlRejection(runtime, action, afterEvent);
  if (rejection) return {settled: "failed", error: rejection};
  if (action === "start" && receipt && actual === "running" && runtime.last_action === "start" && runtime.accepted_order_count_known === true && samePlan)
    return {settled: "done", reset: true, result: {reconciled: true, plan, accepted_orders: accepted, receipt}};
  if (action === "replace_grid" && receipt && actual === "running" && runtime.last_action === "replace_grid" && runtime.accepted_order_count_known === true && samePlan)
    return {settled: "done", reset: false, result: {reconciled: true, plan, accepted_orders: accepted, receipt}};
  if (action === "stop" && actual === "stopped" && openOrders === 0 && open === 0)
    return {settled: "done", reset: true, result: {reconciled: true, cancelled_orders: 0, flattened_positions: 0, receipt}};
  if (action === "prepare_start" && receipt) return {settled: "failed", error: apiFailure("后台已生成启动规格，但响应中断；请重新点击启动机器人。", 502, "prepare_start_response_lost")};
  if (actual === "error") return {settled: "failed", error: apiFailure(runtime.last_error || "后台执行失败，机器人已进入异常状态。", 500, "runtime_error")};
  return null;
}

/** The error once every read has left the outcome open. */
export function unresolvedControlError(action: string, latest: Json, runtimeLabels: Record<string, string>): ApiFailure {
  const runtime = latest?.runtime || {}, actual = runtime.actual_state || runtime.desired_state || "unknown";
  if (action === "prepare_start") return apiFailure("启动规格请求结果仍未确认；未下单，请重新点击启动机器人。", 502, "prepare_start_outcome_unknown");
  if (action === "start" && actual === "stopped") return apiFailure("后台已确认机器人仍为停止状态，未创建挂单；请检查行情后再试。", 502, "start_not_applied");
  return apiFailure(`操作结果仍未确认（当前状态：${runtimeLabels[actual] || actual}），请勿重复点击。`, 502, "control_outcome_unknown");
}

const CONTROL_ERROR_TEXT: Readonly<Record<string, string>> = Object.freeze({
  strategy_preview_changed: "行情已改变网格规格，请重新点击启动。",
  prepared_start_changed: "启动规格已失效，请重新点击启动。",
  prepared_start_expired: "启动确认已超过 5 分钟，请重新核对。",
  prepared_start_market_moved: "行情已跨越网格线，请重新核对后启动。",
  "market data is stale": "启动瞬间实时行情暂时过期，未下单；等待行情恢复后重新启动。",
  "server market data is stale": "启动瞬间实时行情暂时过期，未下单；等待行情恢复后重新启动。",
});

export function controlErrorMessage(error: Json): string {
  const raw = String(error?.message || error || "未知错误");
  return Object.prototype.hasOwnProperty.call(CONTROL_ERROR_TEXT, raw) ? CONTROL_ERROR_TEXT[raw]! : tradingFailure(error).text;
}

// --- the range draft and its replacement -----------------------------------------------------------

/** The running grid matches the production plan: same id and version, and running. Only then can its range be replaced. */
export function runtimeMatchesProductionPlan(data: Json): boolean {
  const runtime = data?.runtime || {}, plan = data?.strategy?.plan || {}, actual = runtime.actual_state || runtime.desired_state || "stopped";
  const planId = String(plan.strategy_plan_id || ""), runtimePlanId = String(runtime.strategy_plan_id || "");
  return actual === "running" && Boolean(planId) && runtimePlanId === planId && Number(runtime.strategy_plan_version) === Number(plan.version);
}

export function createGridRangeDraft(plan: Json): Json {
  const range = plan?.range || {}, grid = plan?.grid || {}, migration = legacySingleSidePlanMigration(plan);
  const low = Number(migration?.executable_range?.low ?? range.low), high = Number(migration?.executable_range?.high ?? range.high);
  const count = Number(migration?.executable_grid_count ?? grid.count ?? grid.orders?.length), notional = Number(grid.notional_per_grid);
  const strategyPlanId = String(plan?.strategy_plan_id || ""), strategyPlanVersion = Number(plan?.version);
  if (!strategyPlanId || !Number.isInteger(strategyPlanVersion) || strategyPlanVersion <= 0 || !Number.isFinite(low) || !Number.isFinite(high) || high <= low)
    throw Error("当前生产网格没有可调整的有效区间或版本。");
  if (!Number.isInteger(count) || count < 2 || !Number.isFinite(notional) || notional <= 0) throw Error("当前生产网格缺少固定格数或每格名义。");
  return {scope: "running", expectedPlanId: strategyPlanId, expectedPlanVersion: strategyPlanVersion, low, high, direction: String(plan?.direction || "neutral"),
    dirty: false, original: {range: {low, high}, grid: {count, mode: grid.mode || "arithmetic", notionalPerGrid: notional}},
    legacy: migration ? {range: {...migration.legacy_range}, grid: {count: migration.legacy_grid_count, mode: grid.mode || "arithmetic", notionalPerGrid: notional}} : null};
}

/** The range of a grid not yet started: the form's values where filled, else the preview's. */
export function createStartupGridRangeDraft(form: StrategyForm, {preview, gridMode, direction}: {preview: Json; gridMode: Json; direction: Json}): Json {
  const p = preview || {}, range = p.range || {}, grid = p.grid || {};
  const inputNumber = (value: unknown) => { const raw = String(value ?? "").trim(); return raw === "" ? NaN : Number(raw); };
  const inputLow = inputNumber(form.rangeLow), inputHigh = inputNumber(form.rangeHigh);
  const low = Number.isFinite(inputLow) ? inputLow : Number(range.low), high = Number.isFinite(inputHigh) ? inputHigh : Number(range.high);
  const inputCount = inputNumber(form.gridCount), count = Number.isInteger(inputCount) ? inputCount : Number(grid.count);
  const inputNotional = inputNumber(form.gridNotional), notional = inputNotional > 0 ? inputNotional : Number(grid.notional_per_grid);
  const mode = gridMode || grid.mode || "arithmetic";
  if (!(low > 0) || !Number.isFinite(high) || high <= low) throw Error("请先生成或填写有效的启动 Range。");
  if (!Number.isInteger(count) || count < 2 || !Number.isFinite(notional) || notional <= 0) throw Error("请先生成有效的网格数量与每格名义。");
  return {scope: "startup", low, high, direction: direction || p.direction || "neutral", dirty: false, original: {range: {low, high}, grid: {count, mode, notionalPerGrid: notional}}};
}

/** Which edge the drag moved: "upper", "lower", "range" (both, same width) or "draft". */
export function gridDraftHandle(draft: Json): string {
  const original = draft?.original?.range || {}, width = Number(original.high) - Number(original.low), newWidth = Number(draft?.high) - Number(draft?.low);
  const tolerance = Math.max(1e-8, Math.abs(width) * 1e-8), near = (left: unknown, right: unknown) => Math.abs(Number(left) - Number(right)) <= tolerance;
  if (near(draft?.low, original.low) && !near(draft?.high, original.high)) return "upper";
  if (near(draft?.high, original.high) && !near(draft?.low, original.low)) return "lower";
  if (near(newWidth, width)) return "range";
  return "draft";
}

export function gridRangePreviewPayload(draft: Json, {recalculate = false}: {recalculate?: boolean} = {}): Json {
  if (!draft?.dirty) throw Error("请先拖动网格区间。");
  return {expected_strategy_plan_id: draft.expectedPlanId, expected_strategy_plan_version: draft.expectedPlanVersion, handle: gridDraftHandle(draft),
    range: {low: draft.low, high: draft.high}, recalculate_notional_by_risk_budget: recalculate};
}

/** Why a range preview cannot be trusted, or "" when it can: same plan, same old grid, the dragged range,
 * no implicit change of count, mode or size, and a proof that previewing changed nothing. */
export function gridRangePreviewContractIssue(preview: Json, draft: Json): string {
  const near = (left: unknown, right: unknown) => Number.isFinite(Number(left)) && Number.isFinite(Number(right)) && Math.abs(Number(left) - Number(right)) <= .00011;
  const old = preview?.old || {}, next = preview?.new || {}, original = draft?.original || {}, migration = preview?.migration;
  const isLegacy = migration?.schema_version === "legacy-single-side-grid-migration-v1", expectedOld = isLegacy ? draft?.legacy : original;
  const sideEffects = preview?.side_effects || {};
  const requiredEffects: Record<string, unknown> = {orders_created: 0, orders_cancelled: 0, positions_changed: 0, strategy_plan_written: false, risk_decision_persisted: false};
  const manual = preview?.manual_confirmation || {}, ackRows: Json[] = manual.required_acknowledgements || [], ackCodes = ackRows.map(row => String(row?.code || ""));
  if (preview?.schema_version !== "grid-range-drag-preview-v1" || !String(preview?.preview_id || "")) return "预览版本或标识缺失";
  if (!draft || preview.expected_strategy_plan_id !== draft.expectedPlanId || Number(preview.expected_strategy_plan_version) !== Number(draft.expectedPlanVersion)) return "生产计划身份不一致";
  if (isLegacy && (!draft.legacy || migration.applies_on_final_confirmation_only !== true || migration.direction !== draft.direction || !Number.isFinite(Number(migration.split_price))
    || Number(migration.legacy_grid_count) !== Number(draft.legacy.grid?.count) || Number(migration.executable_grid_count) !== Number(original.grid?.count)
    || !near(migration.legacy_range?.low, draft.legacy.range?.low) || !near(migration.legacy_range?.high, draft.legacy.range?.high)
    || !near(migration.executable_range?.low, original.range?.low) || !near(migration.executable_range?.high, original.range?.high))) return "旧单边网格迁移合同不一致";
  if (!expectedOld || !near(old.range_low, expectedOld.range?.low) || !near(old.range_high, expectedOld.range?.high) || Number(old.grid_count) !== Number(expectedOld.grid?.count)
    || !near(old.notional_per_grid, expectedOld.grid?.notionalPerGrid) || old.mode !== expectedOld.grid?.mode) return "原网格规格与生产草稿不一致";
  if (!near(preview?.geometry?.new_range?.low, draft.low) || !near(preview?.geometry?.new_range?.high, draft.high) || !near(next.range_low, draft.low) || !near(next.range_high, draft.high))
    return "预览区间与拖动草稿不一致";
  const expectedNextCount = isLegacy ? Number(original.grid?.count) : Number(old.grid_count);
  if (Number(next.grid_count) !== expectedNextCount || next.mode !== old.mode) return "网格数量或模式发生了隐式变化";
  if (!preview?.risk_recalculation?.applied_to_preview && !near(next.notional_per_grid, old.notional_per_grid)) return "每格名义发生了隐式变化";
  if (Object.entries(requiredEffects).some(([key, value]) => sideEffects[key] !== value)) return "只读预览缺少零副作用证明";
  if (!Number.isFinite(Number(preview?.order_delta?.cancel_pending_entries)) || !Number.isFinite(Number(preview?.order_delta?.submit_new_entries))
    || preview?.positions?.preview_effect !== "none" || preview?.tp_sl?.existing_orders_affected_by_preview !== false || preview?.tp_sl?.candidate_orders_recomputed !== true)
    return "订单、持仓或 TP/SL 预览声明不完整";
  if (typeof preview?.can_apply !== "boolean" || typeof preview?.can_apply_with_acknowledgements !== "boolean" || !Array.isArray(preview?.confirm_disabled_reasons)) return "风险门禁声明不完整";
  if (manual.schema_version !== "grid-range-risk-ack-v1" || manual.scope !== "paper_only" || String(manual.preview_id || "") !== String(preview.preview_id)
    || !String(manual.facts_digest || "") || !String(manual.risk_snapshot_digest || "") || manual.required !== true || typeof manual.available !== "boolean"
    || !Array.isArray(manual.required_acknowledgements) || ackCodes.some(code => !code) || new Set(ackCodes).size !== ackCodes.length) return "人工风险确认合同不完整";
  return "";
}

/** The open orders and positions the replacement expects to find, by stable id. Missing or repeated ids refuse the replacement. */
export function executionIdentity(data: Json): {accepted_order_ids: string[]; open_position_ids: string[]} {
  const execution = data?.execution || {};
  const orders: Json[] = Array.isArray(execution.open_orders) ? execution.open_orders : (execution.orders || []).filter((row: Json) => String(row?.state || "").toLowerCase() === "accepted");
  const positions: Json[] = Array.isArray(execution.open_positions) ? execution.open_positions : (execution.positions || []).filter((row: Json) => String(row?.status || "").toLowerCase() === "open");
  const orderIds = orders.map(row => String(row?.order_id || row?.command_id || "").trim()), positionIds = positions.map(row => String(row?.position_id || row?.trade_id || "").trim());
  if (orderIds.some(id => !id) || positionIds.some(id => !id) || new Set(orderIds).size !== orderIds.length || new Set(positionIds).size !== positionIds.length)
    throw Error("当前挂单或持仓缺少唯一身份，禁止替换网格。 ");
  return {accepted_order_ids: orderIds.sort(), open_position_ids: positionIds.sort()};
}

export function requiredGridRiskAcknowledgementCodes(preview: Json): string[] {
  return (preview?.manual_confirmation?.required_acknowledgements || []).map((row: Json) => String(row?.code || "")).filter(Boolean);
}

export function gridRiskAcknowledgementsComplete(preview: Json, acks: Set<string>): boolean {
  const required = requiredGridRiskAcknowledgementCodes(preview);
  return required.length > 0 && required.every(code => acks.has(code));
}

export function gridRiskAcknowledgementPayload(preview: Json, acks: Set<string>): Json {
  return {schema_version: "grid-range-risk-ack-v1", preview_id: preview?.preview_id, facts_digest: preview?.manual_confirmation?.facts_digest,
    risk_snapshot_digest: preview?.manual_confirmation?.risk_snapshot_digest, codes: requiredGridRiskAcknowledgementCodes(preview).filter(code => acks.has(code)).sort()};
}

/** The replace_grid body, after every check runs again on a fresh read model. The checks use `draft`, captured when
 * the operator pressed confirm; the range and handle in the body come from `bodyDraft`, the page's draft as it is
 * once the read model is back. Both are the same object unless the draft changed during that read. Kept for
 * byte-for-byte parity with gridmind.js at 3e116a7 (#1418). */
export function gridReplacementPayload(preview: Json, draft: Json, data: Json, acks: Set<string>, bodyDraft: Json = draft): Json {
  const issue = gridRangePreviewContractIssue(preview, draft);
  if (issue) throw Error(`最终规格完整性核对失败：${issue}`);
  if (preview?.can_apply_with_acknowledgements !== true || preview?.manual_confirmation?.available !== true)
    throw Error(`存在不可人工覆盖的安全问题：${(preview?.manual_confirmation?.non_overridable_blocker_codes || []).join(" / ") || "风险结果未通过"}`);
  if (!gridRiskAcknowledgementsComplete(preview, acks)) throw Error("请逐项勾选全部规格与风险确认");
  if (!runtimeMatchesProductionPlan(data)) throw Error("运行时与生产计划身份不一致");
  const plan = data?.strategy?.plan || {};
  if (String(plan.strategy_plan_id || "") !== String(draft.expectedPlanId || "") || Number(plan.version) !== Number(draft.expectedPlanVersion)) throw Error("生产计划已变化，请重新拖动并核对");
  return {...gridRangePreviewPayload(bodyDraft, {recalculate: preview?.risk_recalculation?.applied_to_preview === true}), expected_preview_id: preview.preview_id,
    expected_execution: executionIdentity(data), risk_acknowledgements: gridRiskAcknowledgementPayload(preview, acks)};
}

// --- the start risk review -------------------------------------------------------------------------

/** Acknowledgements belong to one preview (ADR 0008): a new preview_id finds an empty set. */
export interface StartRiskAcks { previewId: string | null; codes: Set<string> }

export const emptyStartRiskAcks = (): StartRiskAcks => ({previewId: null, codes: new Set()});

export function requiredStartRiskCodes(preview: Json): string[] {
  return (preview?.manual_confirmation?.required_acknowledgements || []).map((row: Json) => String(row?.code || "")).filter(Boolean);
}

export function startRiskAcks(preview: Json, store: StartRiskAcks): Set<string> {
  const id = preview?.preview_id;
  return id && store.previewId === id ? store.codes : new Set();
}

/** The store after ticking or unticking `code` on the current preview. With no preview it is unchanged. */
export function toggleStartRiskAck(store: StartRiskAcks, preview: Json, code: string): StartRiskAcks {
  const id = preview?.preview_id;
  if (!id) return store;
  const next = store.previewId === id ? store : {previewId: id, codes: new Set<string>()};
  if (next.codes.has(code)) next.codes.delete(code); else next.codes.add(code);
  return next;
}

export function startRiskAcknowledgementsComplete(preview: Json, store: StartRiskAcks): boolean {
  const required = requiredStartRiskCodes(preview), acks = startRiskAcks(preview, store);
  return required.length > 0 && required.every(code => acks.has(code));
}

export function startRiskConfirmDisabled(preview: Json, store: StartRiskAcks, busy: unknown): boolean {
  return preview?.manual_confirmation?.available !== true || !startRiskAcknowledgementsComplete(preview, store) || Boolean(busy);
}

export function startRiskPayload(preview: Json, store: StartRiskAcks): Json {
  const manual = preview?.manual_confirmation || {};
  const payload: Json = {schema_version: manual.schema_version || "grid-range-risk-ack-v1", preview_id: preview?.preview_id, facts_digest: manual.facts_digest,
    codes: requiredStartRiskCodes(preview).filter(code => startRiskAcks(preview, store).has(code)).sort()};
  if (manual.risk_snapshot_digest) payload.risk_snapshot_digest = manual.risk_snapshot_digest;
  return payload;
}

interface BlockerCopy { title: string; explanation: string; action: string }

export const START_BLOCKER_COPY: Readonly<Record<string, BlockerCopy>> = Object.freeze({
  manual_leverage_capacity_exceeded: {title: "杠杆超过 Paper 手动容量", explanation: "当前参数要求的实际杠杆超过 Paper 允许人工确认的上限。", action: "降低每格名义或网格数量，扩大间距，直到实际杠杆回到容量内。"},
  adaptive_grid_count_invalid: {title: "网格数量不是整数", explanation: "手动锁定的网格数量必须是完整格数。", action: "输入 2–200 之间的整数网格数量后重新预览。"},
  adaptive_grid_count_out_of_bounds: {title: "网格数量超出硬边界", explanation: "当前手动网格数量不在 Paper 求解器允许的 2–200 格范围内。", action: "把网格数量调整到 2–200 格后重新预览。"},
  adaptive_manual_leverage_out_of_bounds: {title: "杠杆超出硬边界", explanation: "当前手动杠杆不在 Paper 求解器允许的 1–20x 范围内。", action: "把杠杆调整到 1–20x 后重新预览。"},
  candidate_range_invalid: {title: "网格区间无效", explanation: "当前下边界、上边界或区间宽度不满足可执行网格条件。", action: "确认下边界小于上边界、两者为有限正数，然后重新生成预览。"},
  candidate_kind_invalid: {title: "候选策略参数无效", explanation: "后台无法将当前候选规格识别为可执行的网格类型。", action: "重新选择策略方向和网格模式，或点击智能填充后重新预览。"},
  account_snapshot_unavailable: {title: "账户数据不可用", explanation: "系统没有拿到完整且可信的账户快照。", action: "刷新账户数据并确认余额、权益和对账状态后重试。"},
  execution_state_unavailable: {title: "执行状态不可用", explanation: "系统无法安全确认当前挂单和持仓状态。", action: "恢复执行状态读取并完成核对后重试。"},
  market_untrusted: {title: "行情暂不可信", explanation: "行情过期、缺失、合成或来源不符合执行要求。", action: "等待可信实时行情恢复后重新生成启动预览。"},
});

const own = (table: Readonly<Record<string, unknown>>, key: string) => Object.prototype.hasOwnProperty.call(table, key);

export function startBlockerCopy(code: string, message: unknown = ""): BlockerCopy {
  const canonical = own(OPERATOR_FAILURE_COPY, code) ? OPERATOR_FAILURE_COPY[code] : undefined;
  if (canonical) return {title: canonical.title, explanation: canonical.reason, action: canonical.action};
  return (own(START_BLOCKER_COPY, code) ? START_BLOCKER_COPY[code] : undefined)
    || {title: "启动安全检查未通过", explanation: String(message || "系统发现不可人工覆盖的安全问题。"), action: "请根据机器码排查并修复后重试。"};
}

export const START_RECONCILIATION_ISSUE_COPY: Readonly<Record<string, string>> = Object.freeze({
  closed_trade_exit_before_entry: "历史交易平仓时间早于开仓（已隔离为时间异常，不计入正常已完成交易）", closed_trade_exit_fill_missing: "已关闭交易缺少对应平仓成交",
  orphan_exit_fill: "存在无法归属到原交易的平仓成交", fill_trade_id_missing: "成交缺少交易身份", duplicate_fill_id: "成交记录 ID 重复", negative_position_units: "仓位剩余数量为负",
  account_realized_pnl_mismatch: "账户已实现盈亏与成交汇总不一致", account_ending_cash_mismatch: "期末现金与账本公式不一致", account_equity_mismatch: "账户权益与账本公式不一致",
  fill_realized_pnl_mismatch: "成交已实现盈亏与快照不一致", account_fees_mismatch: "账户手续费与成交汇总不一致", net_realized_pnl_formula_mismatch: "净已实现盈亏公式不一致",
  account_slippage_mismatch: "账户滑点与成交汇总不一致", open_position_identity_missing: "未平仓仓位缺少交易身份", open_position_strategy_plan_missing: "未平仓仓位缺少策略身份",
  persisted_orders_mismatch: "订单快照与持久化记录不一致", persisted_fills_mismatch: "成交快照与持久化记录不一致", persisted_positions_mismatch: "仓位快照与持久化记录不一致",
});

export const START_EVIDENCE_LABELS: Readonly<Record<string, string>> = Object.freeze({
  fill_id: "成交", fill_ids: "成交 ID", trade_id: "交易", trade_ids: "交易 ID", order_id: "订单", order_ids: "订单 ID", snapshot_count: "快照数量", persisted_count: "持久化数量",
  expected: "应为", actual: "实际", account: "账户值", fills: "成交汇总", snapshot: "快照值", formula: "公式结果", remaining_quantity: "剩余数量", accounting_status: "账本状态",
  engine_status: "执行引擎状态", actual_leverage: "实际杠杆", selected_leverage: "设置杠杆", manual_paper_leverage_limit: "Paper 手动上限", estimated_margin: "预计保证金", equity: "账户权益",
});

const evidenceLabel = (key: string) => (own(START_EVIDENCE_LABELS, key) ? START_EVIDENCE_LABELS[key] : undefined) || key;

export function startEvidenceValue(value: unknown): string {
  if (Array.isArray(value)) return value.map(item => String(item)).join("、");
  if (value && typeof value === "object") return JSON.stringify(value);
  return String(value);
}

export function startBlockerIssueText(issue: Json): string {
  const code = String(issue?.code || "unknown_issue"), label = (own(START_RECONCILIATION_ISSUE_COPY, code) ? START_RECONCILIATION_ISSUE_COPY[code] : undefined) || `未识别的对账差异：${code}`;
  const facts = Object.entries(issue || {}).filter(([key, value]) => key !== "code" && value !== null && value !== undefined && value !== "")
    .map(([key, value]) => `${evidenceLabel(key)}：${startEvidenceValue(value)}`);
  return facts.length ? `${label}（${facts.join("；")}）` : label;
}

export function startBlockerRows(manual: Json): Json[] {
  const details: Json[] = (manual?.non_overridable_blockers || []).filter((row: Json) => row && typeof row === "object");
  const codes: string[] = (manual?.non_overridable_blocker_codes || []).map(String);
  const byCode = new Map<string, Json>(details.map(row => [String(row.code || "risk_blocked"), row]));
  codes.forEach(code => { if (!byCode.has(code)) byCode.set(code, {code, evidence: {}}); });
  return [...byCode.values()];
}

export function startBlockerEvidenceLines(row: Json): string[] {
  const evidence = row?.evidence && typeof row.evidence === "object" ? row.evidence : {}, issues: Json[] = Array.isArray(evidence.issues) ? evidence.issues : [];
  const lines = issues.map(startBlockerIssueText);
  Object.entries(evidence).filter(([key, value]) => key !== "issues" && value !== null && value !== undefined && value !== "")
    .forEach(([key, value]) => lines.push(`${evidenceLabel(key)}：${startEvidenceValue(value)}`));
  return lines;
}

export function startBlockerProps(manual: Json): {code: string; title: string; explanation: string; action: string; lines: string[]}[] {
  return startBlockerRows(manual).map(row => {
    const code = String(row.code || "risk_blocked"), copy = startBlockerCopy(code, row.message);
    return {code, title: String(copy.title ?? "--"), explanation: String(copy.explanation ?? "--"), action: String(copy.action ?? "--"),
      lines: startBlockerEvidenceLines(row).map(line => String(line ?? "--"))};
  });
}

/** What the start risk dialog shows: old → new rows, the acknowledgements to tick, blockers, the gate and confirm. */
export function startRiskView(preview: Json, store: StartRiskAcks, {busy, formStrategyType}: {busy: unknown; formStrategyType: unknown}): Json {
  const manual = preview?.manual_confirmation || {}, old = manual.old || {}, next = manual.new || {}, dca = preview?.dca || {}, risk = preview?.risk || {};
  const dcaPrices = ((dca.entry_levels || []) as unknown[]).map(Number).filter(Number.isFinite);
  const rows = isDcaType(preview?.strategy_type, formStrategyType)
    ? [["策略", `当前价 ${num(preview?.market?.price)}`, `${preview.direction === "long" ? "做多" : "做空"} DCA`], ["加仓区间", "—", `${num(Math.min(...dcaPrices))}–${num(Math.max(...dcaPrices))}`],
      ["最多加仓", "—", `${num(dca.max_additions)} 次`], ["每次名义", "—", `${num(dca.notional_per_addition)} USD`], ["满仓总名义", "—", `${num(dca.total_possible_notional)} USD`],
      ["整轮止盈 / 止损", "—", `${num(dca.target_price)} / ${num(dca.stop_price)}`], ["满仓实际杠杆", "—", `${num(risk.actual_leverage_at_full_depth)}x`],
      ["预计保证金", "—", `${num(risk.estimated_margin_at_full_depth)} USD`], ["预计最大损失", "—", `${num(risk.maximum_loss_at_full_depth)} USD`]]
    : [["Range", `${num(old.range_low)}–${num(old.range_high)}`, `${num(next.range_low)}–${num(next.range_high)}`], ["网格数量", `${num(old.grid_count)} 格`, `${num(next.grid_count)} 格`],
      ["每格净利", `${num(old.min_net_profit_per_grid_usd)} USD`, `${num(next.min_net_profit_per_grid_usd)} USD`], ["每格名义", `${num(old.notional_per_grid)} USD`, `${num(next.notional_per_grid)} USD`],
      ["实际杠杆", `${num(old.actual_leverage)}x`, `${num(next.actual_leverage)}x`], ["预计保证金", `${num(old.estimated_margin)} USD`, `${num(next.estimated_margin)} USD`],
      ["预计最大损失", `${num(old.max_loss)} USD`, `${num(next.max_loss)} USD`]];
  const acks = startRiskAcks(preview, store), complete = startRiskAcknowledgementsComplete(preview, store), available = manual.available === true;
  const first = startBlockerRows(manual)[0], copy = startBlockerCopy(String(first?.code || ""), first?.message);
  return {
    rows,
    acks: (manual.required_acknowledgements || []).map((row: Json) => ({code: String(row.code), title: row.title, summary: row.summary, critical: row.severity === "critical", checked: acks.has(String(row.code))})),
    blockers: startBlockerProps(manual),
    gate: {ok: available && complete && !busy, text: !available ? `禁止启动：${copy.title}。请先处理上方具体差异。` : !complete ? "请逐项确认规格、预计最大损失和风险；未确认不会下单。" : "确认完整；启动时后台会重新计算并校验。"},
    confirmText: isDcaType(undefined, formStrategyType) ? "确认风险并启动 DCA" : "确认风险并启动机器人",
    confirmDisabled: startRiskConfirmDisabled(preview, store, busy),
  };
}

// --- the flows -------------------------------------------------------------------------------------

export interface Toast { ok?: boolean; title: string; metric: string; detail: string }

/** What a flow needs from gridmind.js. `state` is GridMind's own object; the flows read and write it as gridmind.js did. */
export interface ControlHost {
  state: Json;
  /** gridmind.js's control(): the one place a control request is sent. */
  control(action: string, payload?: Json, options?: {reload?: boolean}): Promise<Json>;
  readModel(): Promise<Json>;
  applyReadModel(data: Json): void;
  load(): Promise<unknown>;
  render(): void;
  renderRobotControls(): void;
  notice(text: string): void;
  hint(text: string): void;
  toast(toast: Toast): void;
  /** Writes a preview into the form (gridmind.js's applyPreview). */
  applyPreview(preview: Json): void;
  form(): StrategyForm;
  runtimeLabels: Record<string, string>;
  wait(milliseconds: number): Promise<unknown>;
  startRiskDialogOpen(): boolean;
  closeStartRiskDialog(): void;
  renderStartRiskReview(): void;
  openStartRiskReview(): void;
  showStartFailure(message: string): void;
  clearGridRangeDraft(): void;
  renderGridRangeReview(): void;
  renderGridAdjustOverlay(): void;
  /** Opens the range review dialog if it is closed. */
  openGridRangeReview(): void;
}

const hostDca = (host: ControlHost) => isDcaType(undefined, host.state.strategyType);

export function hostStrategyPayload(host: ControlHost): Json {
  const s = host.state;
  return strategyPayload(host.form(), {dca: hostDca(host), direction: s.direction, style: s.style, gridMode: s.gridMode, parameterLocks: s.parameterLocks,
    currentPlan: s.data?.strategy?.plan || {}});
}

export function hostInvalidDraft(host: ControlHost): Problem | null {
  return invalidStrategyDraft(host.form(), {dca: hostDca(host), parameterLocks: host.state.parameterLocks});
}

/** After a lost response, read the runtime up to 12 times, 2.5 s apart, until its audit receipt settles the outcome.
 * A definite error (4xx) is not retried: it is thrown as it came. */
export async function reconcileControlOutcome(host: ControlHost, action: string, error: Json, {afterEvent = ""}: {afterEvent?: string} = {}): Promise<Json> {
  if (!isUncertainControlError(error)) throw error;
  host.notice("后台响应中断，正在核对真实运行状态；请勿重复点击。");
  let latest: Json = null;
  for (let attempt = 0; attempt < 12; attempt++) {
    if (attempt > 0) await host.wait(2500);
    try { latest = await host.readModel(); } catch (_readError) { continue; }
    host.applyReadModel(latest);
    const outcome = controlAttempt(action, latest, afterEvent);
    if (!outcome) continue;
    if (outcome.settled === "failed") throw outcome.error;
    if (outcome.reset) { host.state.preview = null; host.state.formDirty = false; }
    host.render();
    return outcome.result;
  }
  throw unresolvedControlError(action, latest, host.runtimeLabels);
}

const previewSchema = (dca: boolean) => dca ? "strategy-dca-preview-v1" : "strategy-grid-preview-v1";

export async function requestPreview(host: ControlHost): Promise<Json> {
  const s = host.state;
  s.preparedStartId = null;
  const requestId = ++s.previewRequest;
  s.formDirty = true;
  s.preview = null;
  const inputProblem = hostInvalidDraft(host);
  if (inputProblem) { s.draftProblem = inputProblem; host.hint(`启动前需补齐参数：${inputProblem.title}。${inputProblem.detail}`); host.render(); return null; }
  s.draftProblem = null;
  host.hint(hostDca(host) ? "正在计算 DCA 加仓、整轮止盈与满仓风险……" : "正在根据可信行情计算网格位置与风险……");
  host.render();
  try {
    const result = await host.control("preview", hostStrategyPayload(host), {reload: false});
    if (requestId !== s.previewRequest) return null;
    if (result.preview?.schema_version !== previewSchema(hostDca(host))) throw Error(`后台未返回标准${hostDca(host) ? " DCA" : "网格"}预览。`);
    host.applyPreview(result.preview);
    host.render();
    return result.preview;
  } catch (error) {
    if (requestId === s.previewRequest) {
      s.preview = null;
      s.draftProblem = previewProblemFromError(error);
      host.hint(`参数预览未完成：${s.draftProblem.title}。${s.draftProblem.detail} 尚未下单。`);
      host.render();
    }
    throw error;
  }
}

/** prepare_start: the backend freezes a start spec and returns its id; nothing is ordered yet. */
export async function prepareStartRobot(host: ControlHost): Promise<Json> {
  const s = host.state;
  const inputProblem = hostInvalidDraft(host);
  if (inputProblem) { s.draftProblem = inputProblem; host.render(); throw Error(`${inputProblem.title}：${inputProblem.detail}`); }
  clearTimeout(s.previewTimer);
  s.previewTimer = null;
  ++s.previewRequest;
  const payload = hostStrategyPayload(host), afterEvent = controlEventFingerprint(s.data?.runtime?.last_control_event);
  let result: Json;
  try { result = await host.control("prepare_start", payload, {reload: false}); } catch (error) { result = await reconcileControlOutcome(host, "prepare_start", error, {afterEvent}); }
  if (result.preview?.schema_version !== previewSchema(hostDca(host)) || !String(result.prepared_start_id || "")) throw Error(`后台未返回可启动的冻结${hostDca(host) ? " DCA" : "网格"}规格。`);
  s.preparedStartId = String(result.prepared_start_id);
  host.applyPreview(result.preview);
  host.render();
  return result.preview;
}

/** start, against the frozen spec; with `acknowledge` it carries the ticked risk acknowledgements. */
export async function performStartRobot(host: ControlHost, {acknowledge = false}: {acknowledge?: boolean} = {}): Promise<void> {
  const s = host.state, preview = s.preview;
  if (!preview || !s.preparedStartId) throw Error("启动规格尚未冻结，请重新点击启动机器人。");
  const payload: Json = {...hostStrategyPayload(host), expected_preview_id: preview.preview_id, prepared_start_id: s.preparedStartId};
  const afterEvent = controlEventFingerprint(s.data?.runtime?.last_control_event);
  if (acknowledge) {
    if (!startRiskAcknowledgementsComplete(preview, s.startRiskAcks)) throw Error("请逐项确认全部风险");
    payload.risk_acknowledgements = startRiskPayload(preview, s.startRiskAcks);
  }
  let result: Json;
  try { result = await host.control("start", payload, {reload: false}); } catch (error) {
    try { result = await reconcileControlOutcome(host, "start", error, {afterEvent}); } catch (resolvedError) {
      const reason = String((resolvedError as Json)?.message || "");
      if (reason.startsWith("prepared_start_") || reason === "strategy_preview_changed") {
        s.preparedStartId = null;
        s.startRiskAcks = emptyStartRiskAcks();
        if (host.startRiskDialogOpen()) host.closeStartRiskDialog();
      }
      throw resolvedError;
    }
  }
  const dcaStarted = isDcaType(result.preview?.strategy_type, s.strategyType);
  const startedNotional = dcaStarted ? result.preview?.dca?.notional_per_addition : result.preview?.grid?.notional_per_grid, strategyName = dcaStarted ? "DCA" : "网格";
  const reconciledOrders = dcaStarted ? `控制面已确认完整 DCA 启动；当前 ${result.accepted_orders || 0} 笔委托仍被接受` : `控制面已确认完整网格启动；当前 ${result.accepted_orders || 0} 笔委托仍被接受`;
  const orders = result.reconciled ? reconciledOrders : `创建 ${result.created_orders || 0} 笔${strategyName}委托；当前接受 ${result.accepted_orders || 0} 笔、已成交 ${result.filled_orders || 0} 笔`;
  s.preview = null;
  s.preparedStartId = null;
  s.formDirty = false;
  s.parameterLocks = new Set();
  if (host.startRiskDialogOpen()) host.closeStartRiskDialog();
  host.notice(`机器人已启动：生产计划 v${result.plan?.version || "--"}，${orders}${Number.isFinite(Number(startedNotional)) ? `，${dcaStarted ? "每次加仓" : "每格"} ${num(startedNotional)} USD` : ""}。`);
  host.toast({ok: true, title: `${strategyName} 机器人已启动`, metric: `当前接受 ${result.accepted_orders || 0} 笔委托`, detail: `生产计划 v${result.plan?.version || "--"} · Paper`});
  await host.load();
}

/** The start button: freeze the spec, then either open the risk review or start. */
export async function startRobot(host: ControlHost): Promise<void> {
  await prepareStartRobot(host);
  const manual = host.state.preview?.manual_confirmation || {};
  if (manual.required === true) {
    host.openStartRiskReview();
    host.notice(hostDca(host) ? "请逐项确认 DCA 加仓、整轮止盈、预计最大损失和风险。" : "请逐项确认新网格规格、预计最大损失和风险。");
    return;
  }
  await performStartRobot(host);
}

/** The stop button: stop, cancel and flatten. */
export async function stopRobot(host: ControlHost): Promise<void> {
  const s = host.state;
  let result: Json;
  try { result = await host.control("stop", {}, {reload: false}); } catch (error) { result = await reconcileControlOutcome(host, "stop", error); }
  s.preview = null;
  s.preparedStartId = null;
  s.formDirty = false;
  host.notice(`${result.reconciled ? "连接中断后已核对：" : ""}机器人已停止：${result.reconciled ? "挂单与持仓均为 0" : "撤销 " + (result.cancelled_orders || 0) + " 笔挂单，平仓 " + (result.flattened_positions || 0) + " 笔"}，对账通过。`);
  await host.load();
}

/** One control button at a time: busy while it runs; a failure is shown (dialog or toast) and the page reloads. */
export async function runButton(host: ControlHost, pending: string, work: () => Promise<unknown>, {failureSurface = "toast"}: {failureSurface?: string} = {}): Promise<void> {
  const s = host.state;
  if (s.busy || s.gridAdjustMode) return;
  s.busy = true;
  host.notice(pending);
  host.renderRobotControls();
  try { await work(); } catch (error) {
    const message = controlErrorMessage(error);
    host.notice(`${pending.replace("正在", "")}未完成：${message}`);
    if (failureSurface === "dialog") host.showStartFailure(message); else host.toast({title: "操作未完成", metric: message, detail: "生产状态没有改变"});
    await host.load();
  } finally {
    s.busy = false;
    host.renderRobotControls();
    if (host.startRiskDialogOpen()) host.renderStartRiskReview();
  }
}

/** preview_range: a read-only check of the dragged range. Nothing changes until the replacement. */
export async function requestGridRangePreview(host: ControlHost, {openDialog = false, recalculate = false}: {openDialog?: boolean; recalculate?: boolean} = {}): Promise<Json> {
  const s = host.state;
  if (s.gridRangePreviewPending) return null;
  let payload: Json;
  try { payload = gridRangePreviewPayload(s.gridDraft, {recalculate}); } catch (error) { host.notice((error as Error).message); return null; }
  const requestId = ++s.gridRangePreviewRequest;
  s.gridRangePreviewPending = true;
  host.renderGridAdjustOverlay();
  try {
    const result = await host.control("preview_range", payload, {reload: false});
    if (requestId !== s.gridRangePreviewRequest) return null;
    const issue = gridRangePreviewContractIssue(result.preview, s.gridDraft);
    if (issue) throw Error(`后台区间预览未通过完整性核对：${issue}`);
    s.gridRangePreview = result.preview;
    s.gridRiskAcknowledgements = new Set();
    s.gridRangePreviewPending = false;
    host.notice("新网格规格已核对；生产计划、订单和持仓均未改变。");
    if (openDialog) host.openGridRangeReview();
    host.renderGridRangeReview();
    host.renderGridAdjustOverlay();
    return result.preview;
  } catch (error) {
    if (requestId === s.gridRangePreviewRequest) {
      s.gridRangePreviewPending = false;
      host.notice(`新网格规格核对未完成：${(error as Error).message}`);
      host.renderGridAdjustOverlay();
    }
    return null;
  }
}

/** replace_grid: re-read everything, check it all again, then stop, cancel, flatten and start the new grid. */
export async function executeGridRangeReplacement(host: ControlHost): Promise<void> {
  const s = host.state;
  if (s.gridReplacementPending || s.busy) return;
  const preview = s.gridRangePreview, draft = s.gridDraft;
  s.gridReplacementPending = true;
  s.busy = true;
  host.renderGridRangeReview();
  host.notice("正在再次核对行情、风险、挂单和持仓，并安全替换 paper 网格。");
  try {
    const latest = await host.readModel();
    host.applyReadModel(latest);
    const payload = gridReplacementPayload(preview, draft, latest, s.gridRiskAcknowledgements, s.gridDraft), afterEvent = controlEventFingerprint(s.data?.runtime?.last_control_event);
    let result: Json;
    try { result = await host.control("replace_grid", payload, {reload: false}); } catch (error) {
      host.notice("响应中断，正在读取控制审计回执与真实运行状态；请勿重复点击。");
      result = await reconcileControlOutcome(host, "replace_grid", error, {afterEvent});
    }
    host.clearGridRangeDraft();
    const counts = result.reconciled ? `连接中断后已通过控制回执核对；当前接受 ${result.accepted_orders || 0} 笔。`
      : `撤单 ${result.cancelled_orders || 0} 笔，平仓 ${result.flattened_positions || 0} 笔，新挂 ${result.created_orders || 0} 笔，当前接受 ${result.accepted_orders || 0} 笔。`;
    host.notice(`新网格已运行：生产计划 v${result.plan?.version || "--"}；${counts}`);
    host.toast({ok: true, title: "新参数已确认", metric: "机器人正在运行", detail: `生产计划 v${result.plan?.version || "--"} · 当前接受 ${result.accepted_orders || 0} 笔委托`});
    await host.load();
  } catch (error) {
    const message = controlErrorMessage(error);
    host.notice(`替换网格未完成：${message}`);
    host.toast({title: "新网格未应用", metric: message, detail: "原参数与草稿仍保留"});
    await host.load();
  } finally {
    s.gridReplacementPending = false;
    s.busy = false;
    if (s.gridRangePreview) host.renderGridRangeReview();
    host.renderRobotControls();
  }
}
