import * as control from "./control";
import legacy from "./legacy-control-layer.json";
import {apiFailure} from "./read";

/* legacy-control-layer.json was captured once (#1418) by running the control layer as it stood in gridmind.js at
 * 3e116a7 in a VM: its pure functions over an input table, and its flows (preview, prepare, start, stop, the start
 * and stop buttons, the range preview and the range replacement, lost-response reconciliation) over scripted
 * scenarios with every page hook stubbed to a log. Request bodies are compared as JSON strings, so key order counts.
 * Each case here feeds the TypeScript port the same input and expects the same output, log and final state. */

/* eslint-disable @typescript-eslint/no-explicit-any */
const L = legacy as any, C = L.cases;
const plain = (v: unknown) => JSON.parse(JSON.stringify(v, (_k, x) => x instanceof Set ? {set: [...x].sort()} : (typeof x === "number" && !Number.isFinite(x)) ? {num: String(x)} : x));
const value = (v: unknown) => v === undefined ? "<undefined>" : plain(v);
const errorShape = (e: any) => ({message: e?.message, status: e?.status ?? null, code: e?.code ?? null});
const run = (f: () => unknown) => { let v; try { v = f(); } catch (e) { return {error: errorShape(e)}; } return {ok: value(v)}; };
const runAsync = async (f: () => Promise<unknown>) => { let v; try { v = await f(); } catch (e) { return {error: errorShape(e)}; } return {ok: value(v)}; };
const jsonOrError = (f: () => unknown) => { try { return {json: JSON.stringify(f())}; } catch (e) { return {error: errorShape(e)}; } };
const form = (name: string): control.StrategyForm => L.forms[name];

describe("the start form", () => {
  it.each<[any, any]>(C.legacySingleSidePlanMigration)("migration of %j", (plan, expected) => {
    expect(run(() => control.legacySingleSidePlanMigration(plan))).toEqual(expected);
  });

  it.each<any>(C.strategyPayload.map((row: any) => [row.form, row.dca, row.direction, row.locks.join("+"), row.plan, row]))(
    "payload: form %s, dca %s, %s, locks %s, plan %d", (_f, _d, _dir, _l, _p, row) => {
      const ctx = {dca: row.dca, direction: row.direction, style: "steady", gridMode: row.direction === "short" ? "geometric" : "arithmetic",
        parameterLocks: new Set<string>(row.locks), currentPlan: L.plans[row.plan] || {}};
      expect(jsonOrError(() => control.strategyPayload(form(row.form), ctx))).toEqual(row.result);  // a string: key order counts
    });

  it.each<any>(C.invalidStrategyDraft.map((row: any) => [row.form, row.dca, row.locks.join("+"), row]))("draft problem: form %s, dca %s, locks %s", (_f, _d, _l, row) => {
    expect(run(() => control.invalidStrategyDraft(form(row.form), {dca: row.dca, parameterLocks: new Set(row.locks)}))).toEqual(row.result);
  });

  it.each<[any, any]>(C.previewProblemFromError)("problem from %j", (error, expected) => {
    expect(run(() => control.previewProblemFromError(error))).toEqual(expected);
  });

  it.each<any>(C.createStartupGridRangeDraft.map((row: any) => [row.form, Boolean(row.preview), row.gridMode, row]))("startup draft: form %s, preview %s, mode %j", (_f, _p, _m, row) => {
    expect(run(() => control.createStartupGridRangeDraft(form(row.form), {preview: row.preview, gridMode: row.gridMode, direction: row.direction}))).toEqual(row.result);
  });
});

describe("the control request", () => {
  it.each<any>(C.controlBody.map((row: any) => [row.action, row.access, row.cycleId, row]))("%s, access %s, cycle %j", (_a, _b, _c, row) => {
    const result = (() => { try { return {body: control.controlRequestBody(row.action, row.payload, {accessControlled: row.access, cycleId: row.cycleId ?? undefined})}; }
      catch (e) { return {error: errorShape(e)}; } })();
    expect(result).toEqual(row.result);
  });

  it.each<[any, boolean]>(C.isUncertainControlError)("uncertain %j", (error, expected) => {
    expect(control.isUncertainControlError(error)).toBe(expected);
  });

  it.each<any>(C.controlEvents.map((row: any) => [JSON.stringify(row.event), row.action, row.after, row]))("event %s for %s after %j", (_e, _a, _f, row) => {
    const runtime = {last_control_event: row.event};
    expect(control.controlEventFingerprint(row.event)).toBe(row.fingerprint);
    expect(plain(control.auditedControlAcceptance(runtime, row.action, row.after))).toEqual(row.acceptance);
    const rejection = control.auditedControlRejection(runtime, row.action, row.after);
    expect(rejection ? errorShape(rejection) : null).toEqual(row.rejection);
  });

  it.each<[any, any]>(C.controlErrorMessage)("error text for %j", (error, expected) => {
    expect(run(() => control.controlErrorMessage(error))).toEqual(expected);
  });
});

describe("the range draft and its replacement", () => {
  const F = L.fixtures;

  it.each<[any, boolean]>(C.runtimeMatchesProductionPlan)("runtime matches plan: %j", (data, expected) => {
    expect(control.runtimeMatchesProductionPlan(data)).toBe(expected);
  });

  it.each<[any, any]>(C.createGridRangeDraft)("running draft of %j", (plan, expected) => {
    expect(run(() => control.createGridRangeDraft(plan))).toEqual(expected);
  });

  it.each<any>(C.gridRangePreviewContractIssue.map((row: any) => [row.preview, row.draft, row.result]))("contract %s with %s draft", (key, draft, expected) => {
    expect(control.gridRangePreviewContractIssue(F.previews[key], F.drafts[draft])).toBe(expected);
  });

  it.each<any>(C.gridReplacementPayload.map((row: any) => [row.preview, row.draft, row.acks.join("+"), row.data, row]))("replace_grid body: %s, %s draft, acks %s, data %s", (_p, _d, _a, _x, row) => {
    expect(jsonOrError(() => control.gridReplacementPayload(F.previews[row.preview], F.drafts[row.draft], F.data[row.data], new Set(row.acks)))).toEqual(row.result);
  });

  it.each<[number, number, string]>(C.gridDraftHandle)("handle for %d–%d", (low, high, expected) => {
    expect(control.gridDraftHandle({low: Number((low as any)?.num ?? low), high, original: {range: {low: 3900, high: 4000}}})).toBe(expected);
  });

  it.each<any>(C.gridRangePreviewPayload.map((row: any) => [row.draft?.dirty, row.recalculate, row]))("preview_range body, dirty %s, recalculate %s", (_d, _r, row) => {
    expect(jsonOrError(() => control.gridRangePreviewPayload(row.draft, {recalculate: row.recalculate}))).toEqual(row.result);
  });

  it.each<[any, any]>(C.executionIdentity)("execution identity of %j", (data, expected) => {
    expect(jsonOrError(() => control.executionIdentity(data))).toEqual(expected);
  });

  it.each<any>(C.gridRiskAcks.map((row: any) => [row.preview?.preview_id ?? null, row.acks.join("+"), row]))("range acks on %s: %s", (_p, _a, row) => {
    const acks = new Set<string>(row.acks);
    expect(control.gridRiskAcknowledgementsComplete(row.preview, acks)).toBe(row.complete);
    expect(JSON.stringify(control.gridRiskAcknowledgementPayload(row.preview, acks))).toBe(row.payload);
  });
});

describe("the start risk review", () => {
  const store = (s: any): control.StartRiskAcks => ({previewId: s.previewId, codes: new Set(s.codes)});

  it.each<any>(C.startRisk.map((row: any) => [row.label, JSON.stringify(row.store), row.busy, row.formType, row]))("%s, acks %s, busy %s, form %s", (_l, _s, _b, _f, row) => {
    const preview = L.fixtures.riskPreviews[row.label];
    expect(control.startRiskConfirmDisabled(preview, store(row.store), row.busy)).toBe(row.disabled);
    expect(JSON.stringify(control.startRiskPayload(preview, store(row.store)))).toBe(row.payload);
    expect(run(() => control.startRiskView(preview, store(row.store), {busy: row.busy, formStrategyType: row.formType}))).toEqual(row.view);
  });

  it.each<any>(C.toggleStartRiskAck.map((row: any) => [row.label, row]))("ticking on %s", (_l, row) => {
    let acks: control.StartRiskAcks = {previewId: "old", codes: new Set(["z"])};
    const steps = ["a", "b", "a", "c"].map(code => { acks = control.toggleStartRiskAck(acks, row.preview, code); return plain(acks); });
    expect(steps).toEqual(row.steps);
  });

  it.each<[string, any]>(C.startBlockerCopy)("blocker copy for %j", (code, expected) => {
    expect(plain(control.startBlockerCopy(code, "raw message"))).toEqual(expected);
  });
});

describe("the flows", () => {
  const SNAP = ["preview", "preparedStartId", "formDirty", "draftProblem", "previewRequest", "previewTimer", "busy", "parameterLocks", "startRiskAcks", "gridRangePreview",
    "gridRangePreviewPending", "gridRangePreviewRequest", "gridRiskAcknowledgements", "gridReplacementPending"];
  const snapshot = (s: any) => plain(Object.fromEntries(SNAP.map(k => [k, k === "preview" || k === "gridRangePreview" ? (s[k]?.preview_id ?? (s[k] ? "?" : null)) : s[k]])));
  function hydrate(spec: any) {
    const s = structuredClone(spec);
    s.parameterLocks = new Set(s.parameterLocks);
    s.gridRiskAcknowledgements = new Set(s.gridRiskAcknowledgements);
    s.startRiskAcks = {previewId: s.startRiskAcks.previewId, codes: new Set(s.startRiskAcks.codes)};
    return s;
  }
  /* The same stubs the capture used, as a ControlHost. */
  function host(scenario: any, state: any) {
    const log: unknown[][] = [], controls = [...scenario.script], models = [...scenario.reads], dialogs = {...scenario.dialogs};
    const h: control.ControlHost = {
      state,
      control: async (action, payload, options) => {
        log.push(["control", action, JSON.stringify(payload), JSON.stringify(options)]);
        const next = controls.shift();
        if (!next) throw Error("unscripted control");
        if (next.fail) throw apiFailure(next.fail.message, next.fail.status, next.fail.code);
        return structuredClone(next.respond);
      },
      readModel: async () => {
        log.push(["readModel", "/api/trading-system/read-model"]);
        const next = models.shift();
        if (next === undefined || next === "fail") throw apiFailure("read failed", 0, "x");
        return structuredClone(next);
      },
      applyReadModel: data => { log.push(["applyReadModel"]); state.data = data; },
      load: async () => { log.push(["load"]); },
      render: () => { log.push(["render"]); },
      renderRobotControls: () => { log.push(["renderRobotControls"]); },
      notice: text => { log.push(["notice", text]); },
      hint: text => { log.push(["hint", text]); },
      toast: toast => { log.push(["toast", JSON.stringify(toast)]); },
      applyPreview: preview => { log.push(["applyPreview", preview?.preview_id ?? null]); state.preview = preview; },
      form: () => L.forms[scenario.form],
      runtimeLabels: {stopped: "已停止", starting: "启动中", running: "运行中", replanning: "调整网格中", stopping: "停止中", error: "运行异常"},
      wait: async ms => { log.push(["wait", ms]); },
      startRiskDialogOpen: () => Boolean(dialogs.startRisk),
      closeStartRiskDialog: () => { log.push(["closeStartRiskDialog"]); dialogs.startRisk = false; },
      renderStartRiskReview: () => { log.push(["renderStartRiskReview"]); },
      openStartRiskReview: () => { log.push(["openStartRiskReview"]); },
      showStartFailure: message => { log.push(["showStartFailure", message]); },
      clearGridRangeDraft: () => { log.push(["clearGridRangeDraft"]); },
      renderGridRangeReview: () => { log.push(["renderGridRangeReview"]); },
      renderGridAdjustOverlay: () => { log.push(["renderGridAdjustOverlay"]); },
      openGridRangeReview: () => { if (!dialogs.gridReview) { log.push(["openGridRangeReview"]); dialogs.gridReview = true; } },
    };
    return {h, log};
  }
  const FLOWS: Record<string, (h: control.ControlHost, scenario: any) => Promise<unknown>> = {
    reconcile: (h, s) => control.reconcileControlOutcome(h, s.action, apiFailure(s.error.message, s.error.status, s.error.code), {afterEvent: s.afterEvent}),
    requestPreview: h => control.requestPreview(h),
    prepareStartRobot: h => control.prepareStartRobot(h),
    performStartRobot: (h, s) => control.performStartRobot(h, s.args),
    startRobot: h => control.startRobot(h),
    stopRobot: h => control.stopRobot(h),
    "runButton:stop": h => control.runButton(h, "正在停止、撤单并平仓", () => control.stopRobot(h)),
    "runButton:start": h => control.runButton(h, "正在核对启动参数", () => control.startRobot(h), {failureSurface: "dialog"}),
    requestGridRangePreview: (h, s) => control.requestGridRangePreview(h, s.args),
    executeGridRangeReplacement: h => control.executeGridRangeReplacement(h),
  };

  it.each<any>(L.scenarios.map((s: any) => [s.flow, s.name, s]))("%s: %s", async (_flow, _name, scenario) => {
    const state = hydrate(scenario.state);
    const {h, log} = host(scenario, state);
    const outcome = await runAsync(() => FLOWS[scenario.flow]!(h, scenario));
    expect(log).toEqual(scenario.log);  // every request (body as a string), notice, render and dialog, in order
    expect(outcome).toEqual(scenario.outcome);
    expect(snapshot(state)).toEqual(scenario.after);
  });

  it("replace_grid takes its range from the page's draft as it is after the fresh read, as gridmind.js did", async () => {
    const scenario = L.scenarios.find((s: any) => s.name === "replace-ok");
    const state = hydrate(scenario.state);
    const {h, log} = host(scenario, state);
    const moved = {...state.gridDraft, high: state.gridDraft.high + 25};
    const read = h.readModel;
    h.readModel = async () => { const data = await read(); state.gridDraft = moved; return data; };
    await control.executeGridRangeReplacement(h);
    const sent = JSON.parse(log.find(e => e[0] === "control" && e[1] === "replace_grid")![2] as string);
    expect(sent.range).toEqual({low: moved.low, high: moved.high});
  });

  it("reconciliation reads 12 times, 2.5 s apart, before it gives up", async () => {
    const scenario = L.scenarios.find((s: any) => s.name === "start-stays-stopped");
    expect(scenario.log.filter((e: any) => e[0] === "readModel")).toHaveLength(12);
    expect(scenario.log.filter((e: any) => e[0] === "wait").map((e: any) => e[1])).toEqual(Array(11).fill(2500));
  });
});

describe("the module sends nothing", () => {
  it("has no request method, path or fetch of its own (ADR 0008)", async () => {
    const {default: source} = await import("./control.ts?raw");
    expect(source).not.toMatch(/method:|fetch\(|XMLHttpRequest|sendBeacon|\/api\//);
  });
});
