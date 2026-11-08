import * as read from "./read";
import legacy from "./legacy-read.json";

/* legacy-read.json was captured once (#1416) by running the read layer as it stood in gridmind.js at
 * 5a5d380 over the inputs it lists. Every case here feeds the same inputs to the TypeScript module
 * and expects what the old functions returned, so the port changes no judgement. */
const L = legacy.cases as any;
const plain = (v: unknown) => JSON.parse(JSON.stringify(v ?? null, (_k, x) => x instanceof Map ? {map: [...x.entries()]} : x instanceof Set ? {set: [...x]} : x));
const errorShape = (e: any) => plain({message: e.message, status: e.status, code: e.code, details: e.details});
const asError = (e: any) => e && typeof e === "object" ? Object.assign(new Error(e.message), e) : e;

describe("failure kinds read as before", () => {
  it.each<[string, any]>(L.tradingFailure.map((c: any): [string, any] => [JSON.stringify(c.input), c]))("tradingFailure %s", (_name, c: any) => {
    expect(read.tradingFailure(asError(c.input))).toEqual(c.output);
  });
  it("every operator failure kind is covered and the fallback is 操作未完成", () => {
    const codes = L.tradingFailure.map((c: any) => c.input?.code);
    expect(codes).toEqual(expect.arrayContaining(Object.keys(read.OPERATOR_FAILURE_COPY)));
    for (const c of L.operatorFailure) expect(read.operatorFailure(c.input[0], c.input[1])).toEqual(c.output);
  });
  it("apiFailure and marketFailure carry status, code and details", () => {
    for (const c of L.apiFailure) expect(errorShape(read.apiFailure(...(c.input as [string])))).toEqual(c.output);
    for (const c of L.marketFailure) expect(errorShape(read.marketFailure(c.input))).toEqual(c.output);
  });
});

describe("trust stays strict", () => {
  it.each<[string, any]>(L.trust.map((c: any): [string, any] => [JSON.stringify(c.input), c]))("%s", (_name, c: any) => {
    expect({isFresh: read.isFresh(c.input), isTrustedHeaderMarket: read.isTrustedHeaderMarket(c.input), statusText: read.statusText(c.input)})
      .toEqual({isFresh: c.isFresh, isTrustedHeaderMarket: c.isTrustedHeaderMarket, statusText: c.statusText});
  });
  it("only trusted === true counts: \"true\" and 1 do not open anything", () => {
    expect([true, "true", 1, undefined].map(trusted => read.isFresh({trusted}))).toEqual([true, false, false, false]);
    expect(read.isTrustedHeaderMarket({trusted: true, timeframe: "30m"})).toBe(false);
  });
});

describe("names and the bars URL", () => {
  it("displayTradingPair and sameVenueMarket", () => {
    for (const c of L.displayTradingPair) expect(read.displayTradingPair(c.input)).toBe(c.output);
    for (const c of L.sameVenueMarket) expect(read.sameVenueMarket(c.input[0], c.input[1])).toBe(c.output);
  });
  it("dashboardMarketBarsUrl follows the control card's selection", () => {
    for (const c of L.dashboardMarketBarsUrl) {
      const binding = read.dashboardMarketBinding(c.selection);
      expect(plain(binding)).toEqual(c.binding);
      expect(read.dashboardMarketBarsUrl(binding, c.args)).toBe(c.output);
    }
  });
});

describe("the top bar", () => {
  it("today's change needs a trusted 1m and a trusted 1d from the same venue and UTC day", () => {
    for (const c of L.marketTodayChangePct) {
      expect(read.marketTodayChangePct(c.input[0], c.input[1])).toBe(c.output);
      expect(read.marketUtcDayKey(c.input[0])).toBe(c.dayKey);
    }
    for (const c of L.nextHeaderPriceDirection) expect(read.nextHeaderPriceDirection(...(c.input as [number, number, string]))).toBe(c.output);
  });
  it("price, tick and change across a sequence of snapshots", () => {
    let memo: read.HeaderMemo = {identity: "", lastPrice: null, direction: ""};
    for (const step of L.updateHeaderMarket.steps) {
      const result = read.headerMarket(step.input, memo, L.updateHeaderMarket.daily);
      memo = result.memo;
      expect(result.view).toEqual(step.view);
      expect(memo).toEqual(step.memo);
    }
  });
  it("an untrusted snapshot neither moves the memo nor shows a tick", () => {
    const memo = {identity: "binance|XAUUSDT", lastPrice: 4000, direction: "up"};
    const result = read.headerMarket({provider: "binance", provider_symbol: "XAUUSDT", trusted: false, timeframe: "1m", latest_close: 4100}, memo, null);
    expect(result.memo).toEqual(memo);
    expect(result.view.priceClass).toBe("");
    expect(result.view.priceTitle).toBe("最新 1m 行情未通过可信门控");
  });
  it("a daily snapshot is asked for and kept only for a trusted 1m base from the same venue", () => {
    const base = {trusted: true, timeframe: "1m", provider: "binance", provider_symbol: "XAUUSDT", latest_timestamp: "2026-07-21T08:00:00+00:00"};
    expect(read.dailyMarketKey(base)).toBe("GOLD|binance|XAUUSDT|2026-07-21");
    expect(read.dailyMarketKey({...base, timeframe: "30m"})).toBe("");
    expect(read.dailyMarketKey({...base, latest_timestamp: "nope"})).toBe("");
    const daily = {trusted: true, timeframe: "1d", provider: "binance", provider_symbol: "XAUUSDT"};
    expect(read.usableDailyMarket(base, daily)).toBe(true);
    expect([{...daily, trusted: "true"}, {...daily, timeframe: "4h"}, {...daily, provider: "okx"}].map(d => read.usableDailyMarket(base, d))).toEqual([false, false, false]);
  });
});

describe("market snapshots", () => {
  it("mergeMarketBars", () => {
    for (const c of L.mergeMarketBars) expect(plain(read.mergeMarketBars(c.input[0], c.input[1]))).toEqual(c.output);
  });
  it("a failed refresh keeps the last trusted candles but never their trust", () => {
    for (const c of L.retainLastTrustedMarket) expect(plain(read.retainLastTrustedMarket(c.input[0], c.input[1]))).toEqual(c.output);
    const retained = L.retainLastTrustedMarket[0].output;
    expect(retained.trusted).toBe(false);
    expect(retained.retained_last_trusted).toBe(true);
  });
  it("acceptMarketSnapshot and reconcileReadModelMarket", () => {
    for (const c of L.acceptMarketSnapshot) expect(plain(read.acceptMarketSnapshot(c.input[0], c.input[1]))).toEqual(c.output);
    for (const c of L.reconcileReadModelMarket) expect(plain(read.reconcileReadModelMarket(c.input[0], c.input[1]))).toEqual(c.output);
  });
});

describe("market snapshots, as tests/test_dashboard_gridmind_market.mjs held them before #1416", () => {
  const bar = (timestamp: string, close: number) => ({timestamp, close});
  it("failed refresh retains candles but removes execution trust", () => {
    const trusted = {status: "ready", trusted: true, fresh: true, symbol: "GOLD", timeframe: "1m", latest_timestamp: "2026-07-21T08:00:00+00:00", bars: [bar("2026-07-21T08:00:00+00:00", 4000)]};
    const retained = read.retainLastTrustedMarket(trusted, {status: "blocked", access_issues: ["upstream timeout"]});
    expect([retained.trusted, retained.fresh, retained.retained_last_trusted]).toEqual([false, false, true]);
    expect(retained.bars).toEqual(trusted.bars);
    expect(retained.access_issues).toEqual(["upstream timeout"]);
  });
  it("polling cannot overwrite retained trusted candles with a blocked read model", () => {
    const trusted = {status: "ready", trusted: true, fresh: true, symbol: "GOLD", timeframe: "30m", latest_timestamp: "2026-07-21T08:00:00+00:00", bars: [bar("2026-07-21T08:00:00+00:00", 4000)]};
    const retained = read.retainLastTrustedMarket(trusted, {status: "blocked", bars: [], access_issues: ["upstream timeout"]});
    const afterPoll = read.reconcileReadModelMarket(retained, {status: "blocked", trusted: false, fresh: false, bars: [], access_issues: ["still blocked"]});
    expect([afterPoll.retained_last_trusted, afterPoll.trusted]).toEqual([true, false]);
    expect(afterPoll.bars).toEqual(trusted.bars);
    expect(afterPoll.access_issues).toEqual(["still blocked"]);
  });
  it("historical prepend deduplicates timestamps and preserves live authority", () => {
    const history = {status: "ready", trusted: false, trusted_history: true, symbol: "GOLD", timeframe: "1m", bars: [bar("2026-07-21T07:58:00+00:00", 3998), bar("2026-07-21T07:59:00+00:00", 3999)]};
    const live = {status: "ready", trusted: true, fresh: true, symbol: "GOLD", timeframe: "1m", latest_timestamp: "2026-07-21T08:00:00+00:00", bars: [bar("2026-07-21T07:59:00+00:00", 3999), bar("2026-07-21T08:00:00+00:00", 4000)]};
    const merged = read.mergeMarketBars(history, live);
    expect(merged.trusted).toBe(true);
    expect(merged.latest_timestamp).toBe("2026-07-21T08:00:00+00:00");
    expect(merged.bars.map((row: {timestamp: string}) => row.timestamp)).toEqual(["2026-07-21T07:58:00+00:00", "2026-07-21T07:59:00+00:00", "2026-07-21T08:00:00+00:00"]);
  });
});

describe("the read model", () => {
  it("each order keeps its furthest state this cycle", () => {
    let lifecycle = read.emptyOrderLifecycle();
    for (const step of L.reconcileOrderLifecycle) {
      lifecycle = read.reconcileOrderLifecycle(lifecycle, step.input);
      expect(read.readModelCycleId(step.input)).toBe(step.cycleId);
      expect(plain(lifecycle)).toEqual(step.lifecycle);
    }
  });
  it("the previous lifecycle is not changed in place", () => {
    const first = read.reconcileOrderLifecycle(read.emptyOrderLifecycle(), {execution: {cycle_id: "c1", orders: [{order_id: "a", state: "new", state_rank: 1}]}});
    const second = read.reconcileOrderLifecycle(first, {execution: {cycle_id: "c1", orders: [{order_id: "a", state: "accepted", state_rank: 2}]}});
    expect(first.byId.get("a").state).toBe("new");
    expect(second.byId.get("a").state).toBe("accepted");
  });
  it("the on-demand supervisor history is kept only for its own cycle", () => {
    for (const c of L.applyReadModel) {
      const data = structuredClone(c.input);
      const next = read.carrySupervisorAudit({cycle: c.before.supervisorAuditCycle, audit: c.before.supervisorAudit, request: c.before.supervisorAuditRequest}, data);
      expect(plain({supervisorAuditCycle: next.cycle, supervisorAudit: next.audit, supervisorAuditRequest: next.request})).toEqual(c.after);
      expect(plain(data)).toEqual(c.data);
    }
  });
});

describe("api() sends and names exactly as before", () => {
  afterEach(() => { vi.restoreAllMocks(); sessionStorage.clear(); });

  it.each<[number, string, any]>(L.api.map((c: any, i: number): [number, string, any] => [i, JSON.stringify(c.response).slice(0, 80), c]))("case %i %s", async (_i, _name, c: any) => {
    if (c.reauth !== null) sessionStorage.setItem(read.ACCESS_REAUTH_KEY, c.reauth);
    const calls: unknown[] = [], timers: unknown[] = [];
    vi.spyOn(globalThis, "setTimeout").mockImplementation(((_fn: unknown, ms: unknown) => { timers.push(ms); return 0; }) as any);
    vi.spyOn(globalThis, "fetch").mockImplementation((async (url: string, init: any) => {
      calls.push({url, method: init.method || "GET", body: init.body ?? null, credentials: init.credentials, headers: [...init.headers.entries()].sort()});
      if (c.response.throw) { const e = new Error(c.response.throw); if (c.response.abort) e.name = "AbortError"; throw e; }
      return new Response(c.response.body ?? "", {status: c.response.status, headers: c.response.contentType ? {"content-type": c.response.contentType} : {}});
    }) as any);
    let outcome: unknown;
    try { outcome = {ok: await read.createApi(c.publicObserver)("/api/x", c.opts)}; } catch (e: any) { outcome = {error: {name: e.name, ...errorShape(e)}}; }
    expect(plain(outcome)).toEqual(c.outcome);
    expect(calls).toEqual(c.calls);
    expect(timers).toEqual(c.timers);
    const storage = sessionStorage.getItem(read.ACCESS_REAUTH_KEY);
    expect(storage === null ? [] : [[read.ACCESS_REAUTH_KEY, storage]]).toEqual(c.storage);
  });
});

describe("the poll", () => {
  afterEach(() => vi.restoreAllMocks());

  it("reads now, every 5 s locally or 10 s off localhost, and skips while busy or hidden", () => {
    for (const [publicObserver, ms] of [[false, 5000], [true, 10000]] as const) {
      let tick: () => void = () => {};
      const interval = vi.spyOn(window, "setInterval").mockImplementation(((fn: () => void) => { tick = fn; return 1; }) as any);
      let busy = false, hidden = false;
      vi.spyOn(document, "hidden", "get").mockImplementation(() => hidden);
      const load = vi.fn();
      read.startReadModelPolling(load, {publicObserver, isBusy: () => busy});
      expect(load).toHaveBeenCalledTimes(1);
      expect(interval.mock.calls[0]![1]).toBe(ms);
      tick(); expect(load).toHaveBeenCalledTimes(2);
      busy = true; tick(); expect(load).toHaveBeenCalledTimes(2);
      busy = false; hidden = true; tick(); expect(load).toHaveBeenCalledTimes(2);
      document.dispatchEvent(new Event("visibilitychange")); expect(load).toHaveBeenCalledTimes(2);
      hidden = false; document.dispatchEvent(new Event("visibilitychange")); expect(load).toHaveBeenCalledTimes(3);
      vi.restoreAllMocks();
    }
  });

  it("the local operator's session is local; off localhost it is read", async () => {
    const api = vi.fn(async () => ({authenticated: true, can_control: false}));
    expect(await read.readSession(api, false)).toEqual({authenticated: true, can_control: true, email: null, session_duration: "local"});
    expect(api).not.toHaveBeenCalled();
    expect(await read.readSession(api, true)).toEqual({authenticated: true, can_control: false});
    expect(api).toHaveBeenCalledWith("/api/auth/session");
  });

  it("only the operator's own machine is local", () => {
    expect(["127.0.0.1", "localhost", "::1", "trade.example.test"].map(read.isPublicObserver)).toEqual([false, false, false, true]);
  });
});
