import * as chart from "./chart";
import legacy from "./legacy-chart.json";

/* legacy-chart.json was captured once (#1417) by running the chart functions as they stood in gridmind.js at
 * c63a066 in a VM over an input table, with a fake `state` and a fake overlay node. Each case here feeds the
 * TypeScript port the same input, in the shape gridmind.js's wrapper now passes, and expects the same output.
 * Non-finite numbers were written as {num: "NaN"}; a function that threw was written as {throws: <name>}. */

/* eslint-disable @typescript-eslint/no-explicit-any */
const plain = (v: unknown) => JSON.parse(JSON.stringify(v, (_k, x) => (typeof x === "number" && !Number.isFinite(x)) ? {num: String(x)} : x));
const run = (f: () => unknown) => { try { return {ok: plain(f())}; } catch (e) { return {throws: (e as Error).constructor.name}; } };
const revive = (v: any): any => v && typeof v === "object" && !Array.isArray(v) && Object.keys(v).length === 1 && typeof v.num === "string"
  ? Number(v.num) : v;
const C = legacy.cases as any;

describe("the grid draft", () => {
  it.each<[any, any]>(C.draftGridLevels)("levels of %j", (draft, expected) => {
    expect(run(() => chart.draftGridLevels(draft))).toEqual(expected);
  });

  it.each<[any, any, any]>(C.draftGridLinePresentation)("sides of %j split at %j", (draft, split, expected) => {
    expect(run(() => chart.draftGridLinePresentation(draft, Number(revive(split))))).toEqual(expected);
  });
});

describe("the drag", () => {
  const yToPrices: Record<string, ((y: number) => unknown) | undefined> = {
    linear: y => 4000 - y * 0.5, missing: undefined, flat: () => 4000, nan: () => NaN, string: y => String(4000 - y),
  };

  it.each<[string, number, any, number, any]>(C.gridDragPrice)("price for %s, height %d, clientY %j, top %d", (fn, height, clientY, top, expected) => {
    // gridmind.js: y = clientY - overlay top; height = overlay height || chart height (300 here) || 0.
    const yToPrice = yToPrices[fn];
    expect(run(() => chart.dragPrice(Number(revive(clientY)) - top, height || 300, y => yToPrice?.(y)))).toEqual(expected);
  });

  it.each<[any, any, any]>(C.calculateGridDragBounds)("bounds for %j at %j", (drag, price, expected) => {
    expect(run(() => chart.calculateGridDragBounds(drag, price))).toEqual(expected);
  });

  it("a drag starts only in adjust mode, with a draft, no preview pending, the main button and a price", () => {
    const gate = {gridAdjustMode: true, gridDraft: {low: 3900, high: 4000}, gridRangePreviewPending: false};
    const event = {isPrimary: true, button: 0, pointerId: 7};
    expect(chart.gridDragStart(gate, event, "upper", () => 4010)).toEqual({pointerId: 7, mode: "upper", startPrice: 4010, startLow: 3900, startHigh: 4000});
    const price = vi.fn(() => 4010);
    expect(chart.gridDragStart({...gate, gridAdjustMode: false}, event, "move", price)).toBeNull();
    expect(chart.gridDragStart({...gate, gridDraft: null}, event, "move", price)).toBeNull();
    expect(chart.gridDragStart({...gate, gridRangePreviewPending: true}, event, "move", price)).toBeNull();
    expect(chart.gridDragStart(gate, {...event, isPrimary: false}, "move", price)).toBeNull();
    expect(chart.gridDragStart(gate, {...event, button: 2}, "move", price)).toBeNull();
    expect(price).not.toHaveBeenCalled();  // gridmind.js read the price only after the gate
    expect(chart.gridDragStart(gate, event, "move", () => NaN)).toBeNull();
  });

  it("the draft follows the pointer, marked dirty, and stays put when nothing moved", () => {
    const draft = {scope: "running", low: 3900, high: 4000, dirty: false};
    const drag = {pointerId: 1, mode: "move", startPrice: 3950, startLow: 3900, startHigh: 4000};
    expect(chart.draggedDraft(draft, drag, 3960)).toEqual({scope: "running", low: 3910, high: 4010, dirty: true});
    expect(chart.draggedDraft(draft, drag, 3950.000001)).toBeNull();
    expect(chart.draggedDraft(draft, drag, "x")).toBeNull();
    expect(chart.draggedDraft(draft, drag, "3960")).toEqual({scope: "running", low: 3910, high: 4010, dirty: true});
  });
});

describe("history", () => {
  it.each<[any, any, any]>(C.shouldLoadOlderHistory)("drag %j to %j", (drag, range, expected) => {
    expect(run(() => chart.shouldLoadOlderHistory(drag, range))).toEqual(expected);
  });

  it.each<[any, any, any, any]>(C.historyInputCanLoad)("wheel to %j until %j from %j", (range, until, start, expected) => {
    expect(run(() => chart.historyInputCanLoad(range, until, start, 2000))).toEqual(expected);
  });
});

describe("price lines", () => {
  it.each<[any, any, any]>(C.visibleOhlcPriceWindow)("window with %j over %j", (adapted, market, expected) => {
    expect(run(() => chart.visibleOhlcPriceWindow(market, adapted))).toEqual(expected);
  });

  it.each<[any, boolean, any, any]>(C.priceInsideVisibleOhlc)("%j inside %j", (price, wasUndefined, window, expected) => {
    expect(run(() => chart.priceInsideVisibleOhlc(wasUndefined ? undefined : revive(price), window))).toEqual(expected);
  });

  const fixtures = legacy.lineFixtures as any;
  it.each<any>(C.buildVisibleChartPriceLines.map((row: any) => [row.label, row.market, row.adapted ? "adapted" : "bars", row]))(
    "%s, market %d, %s window", (_label, _market, _window, row) => {
      const {plan, execution, preview} = fixtures.cases[row.label], market = fixtures.markets[row.market];
      const priceWindow = chart.visibleOhlcPriceWindow(market, row.adapted);
      expect(plain(priceWindow)).toEqual(row.priceWindow);
      expect(run(() => chart.buildVisibleChartPriceLines({market, plan, execution, preview: row.adjust ? null : preview, priceWindow,
        formStrategyType: row.formType}))).toEqual(row.result);
    });

  it("a plan with no type reads the type picked in the form, as gridmind.js's isDca did", () => {
    expect(chart.isDcaType(undefined, "DCA")).toBe(true);
    expect(chart.isDcaType(null, "dca")).toBe(false);
    expect(chart.isDcaType("grid", "dca")).toBe(false);
  });
});

describe("the overlay", () => {
  function fakeOverlay(height: number) {
    const node = () => {
      const n: any = {style: {}, classes: new Set<string>(), innerHTML: "", disabled: undefined, textContent: ""};
      n.classList = {toggle: (c: string, on: boolean) => { if (on) n.classes.add(c); else n.classes.delete(c); }};
      return n;
    };
    const parts: Record<string, any> = {};
    for (const sel of [".grid-adjust-shade", ".grid-hit.upper", ".grid-hit.lower", ".grid-hit.interior", ".grid-draft-actions", ".grid-ghosts", "confirm"]) parts[sel] = node();
    parts[".grid-draft-actions"].querySelector = () => parts.confirm;
    const self = node();
    Object.assign(self, {attrs: {}, setAttribute(k: string, v: string) { self.attrs[k] = v; }, getBoundingClientRect: () => ({height}),
      querySelector: (sel: string) => parts[sel], parts});
    return self;
  }
  const snapshot = (o: any) => ({classes: [...o.classes].sort(), attrs: o.attrs, parts: Object.fromEntries(Object.entries<any>(o.parts).map(([k, p]) =>
    [k, {style: p.style, classes: [...p.classes].sort(), innerHTML: p.innerHTML, disabled: p.disabled ?? null, textContent: p.textContent}]))});

  it.each<any>(C.renderGridAdjustOverlay.map((row: any) => [row.label, row.height, row.clientHeight, row.toY, row]))(
    "%s, overlay %d, chart %d, %s", (_label, _h, _ch, _toY, row) => {
      // The same state renderGridAdjustOverlay reads: adjust mode on unless the case turns it off.
      const state = {gridAdjustMode: true, gridDraft: row.draft, gridOverlayUnclamped: false, gridRangePreviewPending: false, ...row.extra};
      const priceToY = row.toY === "linear" ? (p: number) => (4100 - Number(p)) * 2 : () => NaN;
      const overlay = fakeOverlay(row.height);
      const result = run(() => {
        chart.renderGridOverlay(overlay, state.gridAdjustMode && Boolean(state.gridDraft), () => ({
          height: overlay.getBoundingClientRect().height || row.clientHeight || 0, priceToY, draft: state.gridDraft,
          unclamped: Boolean(state.gridOverlayUnclamped), previewPending: Boolean(state.gridRangePreviewPending), splitPrice: 3955}));
        return snapshot(overlay);
      });
      expect(result).toEqual(row.result);
    });

  it("the overlay is built once with its handles and sends every release as a drag end", () => {
    const chartEl = document.createElement("div");
    document.body.append(chartEl);
    const on = {dragStart: vi.fn(), confirm: vi.fn(), cancel: vi.fn(), dragMove: vi.fn(), dragEnd: vi.fn(), wheel: vi.fn()};
    const overlay = chart.mountGridOverlay(chartEl, on);
    expect(chartEl.querySelector(".grid-adjust-overlay")).toBe(overlay);
    expect(overlay.getAttribute("aria-hidden")).toBe("true");
    expect([...overlay.querySelectorAll("[data-grid-drag]")].map(n => (n as HTMLElement).dataset.gridDrag)).toEqual(["upper", "move", "lower"]);
    overlay.querySelector(".grid-hit.upper")!.dispatchEvent(new Event("pointerdown", {bubbles: true}));
    expect(on.dragStart).toHaveBeenCalledWith(expect.anything(), "upper", overlay);
    overlay.querySelector(".grid-adjust-shade")!.dispatchEvent(new Event("pointerdown", {bubbles: true}));
    expect(on.dragStart).toHaveBeenCalledTimes(1);
    (overlay.querySelector('[data-grid-action="confirm"]') as HTMLButtonElement).click();
    (overlay.querySelector('[data-grid-action="cancel"]') as HTMLButtonElement).click();
    expect([on.confirm.mock.calls.length, on.cancel.mock.calls.length]).toEqual([1, 1]);
    window.dispatchEvent(new Event("pointermove"));
    window.dispatchEvent(new Event("pointerup"));
    window.dispatchEvent(new Event("pointercancel"));
    chartEl.dispatchEvent(new Event("wheel"));
    expect(on.dragMove).toHaveBeenCalledTimes(1);
    expect(on.dragEnd.mock.calls.map(([, commit]) => commit)).toEqual([true, false]);
    expect(on.wheel).toHaveBeenCalledTimes(1);
    chartEl.remove();
  });

  it("ghost lines keep gridmind.js's markup", () => {
    expect(chart.ghostLineHtml({top: 12.5, side: "buy", boundary: true, split: false}))
      .toBe('<div class="grid-ghost-line buy boundary " data-grid-side="buy" style="top:12.5px"></div>');
  });
});

describe("the chart", () => {
  it("re-fits only when what it shows a range for changes", () => {
    expect(chart.chartVisualKey({strategy_type: "grid", range: {low: 3900, high: 4000}}, false, "grid")).toBe("grid:3900:4000:production");
    expect(chart.chartVisualKey({range: {low: "x"}}, true, "grid")).toBe("grid:--:--:preview");
    expect(chart.chartVisualKey({strategy_type: "dca", dca: {entries: [{price: 3950}, {price: 3900}]}}, false, "grid")).toBe("dca:3900:3950:production");
    expect(chart.chartVisualKey({strategy_type: "dca", dca: {entry_levels: [3930, "3955"]}}, true, "grid")).toBe("dca:3930:3955:preview");
    expect(chart.chartVisualKey({strategy_type: "dca", dca: {}}, false, "grid")).toBe("dca:--:--:production");
    expect(chart.chartVisualKey({dca: {entries: [{price: 1}]}}, false, "dca")).toBe("grid:1:1:production");
    expect(chart.chartVisualKey(null, false, "grid")).toBe("grid:--:--:production");
  });

  it("indicators as the chart takes them", () => {
    expect(chart.chartIndicators({ema: true, macd: true, fast: 9, slow: 21})).toEqual({ema: [{period: 9}, {period: 21}], macd: {fast: 12, slow: 26, signal: 9}});
    expect(chart.chartIndicators({ema: false, macd: false})).toEqual({});
  });

  it("the chart options are fixed", () => {
    expect(chart.CHART_OPTIONS).toEqual({height: 450, minHeight: 320, showVolume: true, timeZone: "Asia/Shanghai", locale: "zh-CN", gridColor: "rgba(255,255,255,.018)"});
    expect(Object.isFrozen(chart.CHART_OPTIONS)).toBe(true);
  });
});
