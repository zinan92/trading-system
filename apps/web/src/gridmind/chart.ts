/* GridMind's chart (#1417): the price lines drawn on the main K-line, the grid-range overlay and its
 * drag, and the history-gesture tests. Ported from gridmind.js at c63a066; legacy-chart.json holds
 * what the old functions returned for a table of inputs and chart.test.ts holds these to it.
 *
 * Nothing here keeps state. gridmind.js owns `state` (the draft, the drag, the chart object) and
 * passes in what each function used to read from it. A drag only edits the draft on the page: the
 * release still goes out as the `grid.dragEnd` intent, and the replacement order flow stays in
 * gridmind.js (#1418). */

// Plans, previews, markets and drafts are open-ended JSON shapes; the functions read them defensively.
/* eslint-disable @typescript-eslint/no-explicit-any */
type Json = any;

export interface PriceLine {
  price: number;
  color: string;
  lineWidth: number;
  lineStyle?: "solid" | "dashed";
  title: string;
  axisLabelVisible: boolean;
}

export interface PriceWindow { min: number; max: number }

export interface GridDrag {
  pointerId: number;
  mode: string;
  startPrice: number;
  startLow: number;
  startHigh: number;
}

/** The chart the page creates once (StandardKline.StandardKlineChart options). */
export const CHART_OPTIONS = Object.freeze({
  height: 450, minHeight: 320, showVolume: true, timeZone: "Asia/Shanghai", locale: "zh-CN", gridColor: "rgba(255,255,255,.018)",
});

/** gridmind.js's isDca: an absent type means the type picked in the form (its default parameter). */
export function isDcaType(value: unknown, formStrategyType: unknown): boolean {
  return String((value === undefined ? formStrategyType : value) || "").toLowerCase() === "dca";
}

// --- the grid draft -------------------------------------------------------------------------------

export function draftGridLevels(draft: Json): number[] {
  const count = draft?.original?.grid?.count, low = Number(draft?.low), high = Number(draft?.high), mode = draft?.original?.grid?.mode;
  if (!Number.isInteger(count) || count < 1 || !Number.isFinite(low) || !Number.isFinite(high) || high <= low) return [];
  if (mode === "geometric" && low > 0) {
    const ratio = Math.pow(high / low, 1 / count);
    return Array.from({length: count + 1}, (_, index) => low * Math.pow(ratio, index));
  }
  return Array.from({length: count + 1}, (_, index) => low + (high - low) * index / count);
}

export interface DraftLine { price: number; side: "buy" | "sell"; split: boolean }

/** Which side each draft level would trade. A neutral grid splits at the level nearest `splitPrice` (the latest close). */
export function draftGridLinePresentation(draft: Json, splitPrice: number): DraftLine[] {
  const levels = draftGridLevels(draft), direction = String(draft?.direction || "neutral");
  const splitIndex = direction === "neutral" && Number.isFinite(splitPrice) && levels.length
    ? levels.reduce((best, price, index) => Math.abs(price - splitPrice) < Math.abs(levels[best]! - splitPrice) ? index : best, 0)
    : -1;
  return levels.map((price, index) => ({
    price,
    side: direction === "long" ? "buy" : direction === "short" ? "sell"
      : price < splitPrice ? "buy" : price > splitPrice ? "sell" : index < splitIndex ? "buy" : "sell",
    split: index === splitIndex,
  }));
}

// --- the drag -------------------------------------------------------------------------------------

/** The price under a pointer `y` pixels down an overlay `height` tall. Past an edge it extends the edge's slope. */
export function dragPrice(y: number, height: number, yToPrice: (y: number) => unknown): number {
  if (!Number.isFinite(y) || height <= 0) return NaN;
  const clamped = Math.max(0, Math.min(height, y)), direct = Number(yToPrice(clamped));
  if (!Number.isFinite(direct)) return NaN;
  if (y >= 0 && y <= height) return direct;
  const edge = y < 0 ? 0 : height, near = y < 0 ? Math.min(height, edge + 1) : Math.max(0, edge - 1);
  const edgePrice = Number(yToPrice(edge)), nearPrice = Number(yToPrice(near));
  if (!Number.isFinite(edgePrice) || !Number.isFinite(nearPrice) || near === edge) return direct;
  const slope = (nearPrice - edgePrice) / (near - edge);
  return Number.isFinite(slope) ? edgePrice + slope * (y - edge) : direct;
}

interface DragGate { gridAdjustMode?: unknown; gridDraft?: Json; gridRangePreviewPending?: unknown }
interface PointerLike { isPrimary?: boolean; button: number; pointerId: number }

/** A new drag, or null when adjust mode is off, a preview is being checked, it is not the main button,
 * or there is no price under the pointer. `price` is only asked for once the gate passes. */
export function gridDragStart(gate: DragGate, event: PointerLike, mode: string, price: () => unknown): GridDrag | null {
  if (!gate.gridAdjustMode || !gate.gridDraft || gate.gridRangePreviewPending || event.isPrimary === false || event.button !== 0) return null;
  const startPrice = Number(price());
  if (!Number.isFinite(startPrice)) return null;
  return {pointerId: event.pointerId, mode, startPrice, startLow: Number(gate.gridDraft.low), startHigh: Number(gate.gridDraft.high)};
}

/** The range after dragging to `price`: "move" keeps the width, "upper"/"lower" keep the other edge and a 2% gap. */
export function calculateGridDragBounds(drag: Pick<GridDrag, "mode" | "startPrice" | "startLow" | "startHigh">, price: number): {low: number; high: number} | null {
  const width = drag.startHigh - drag.startLow, minGap = Math.max(width * .02, .0001), delta = price - drag.startPrice;
  let low = drag.startLow, high = drag.startHigh;
  if (drag.mode === "move") { low = Math.max(.0001, drag.startLow + delta); high = low + width; }
  if (drag.mode === "upper") high = Math.max(drag.startHigh + delta, low + minGap);
  if (drag.mode === "lower") low = Math.max(.0001, Math.min(drag.startLow + delta, high - minGap));
  low = Number(low.toFixed(4));
  high = Number(high.toFixed(4));
  return Number.isFinite(low) && Number.isFinite(high) && high > low ? {low, high} : null;
}

/** The draft after the pointer reaches `price`, marked dirty; null when nothing moved. */
export function draggedDraft(draft: Json, drag: GridDrag, price: unknown): Json | null {
  const at = Number(price);
  if (!Number.isFinite(at)) return null;
  const bounds = calculateGridDragBounds(drag, at);
  if (!bounds) return null;
  const changed = Math.abs(bounds.low - draft.low) > .00001 || Math.abs(bounds.high - draft.high) > .00001;
  return changed ? {...draft, ...bounds, dirty: true} : null;
}

// --- the overlay ----------------------------------------------------------------------------------

export const GRID_OVERLAY_HTML = '<div class="grid-adjust-shade"></div><div class="grid-ghosts"></div><div class="grid-hit upper" data-grid-drag="upper" title="拖动上边界"></div><div class="grid-hit interior" data-grid-drag="move" title="整体平移区间"></div><div class="grid-hit lower" data-grid-drag="lower" title="拖动下边界"></div><div class="grid-draft-actions hidden"><button class="confirm" data-grid-action="confirm" type="button">确认</button><button data-grid-action="cancel" type="button">取消</button></div>';

export interface OverlayHandlers {
  dragStart(event: PointerEvent, mode: string, overlay: HTMLElement): void;
  confirm(): void;
  cancel(): void;
  dragMove(event: PointerEvent): void;
  /** The release; gridmind.js sends it as the grid.dragEnd intent. */
  dragEnd(event: PointerEvent, commit: boolean): void;
  wheel(): void;
}

/** Adds the overlay to the chart. Each new overlay adds its own window listeners, as gridmind.js did. */
export function mountGridOverlay(chartEl: HTMLElement, on: OverlayHandlers): HTMLElement {
  const overlay = document.createElement("div");
  overlay.className = "grid-adjust-overlay";
  overlay.setAttribute("aria-hidden", "true");
  overlay.innerHTML = GRID_OVERLAY_HTML;
  overlay.addEventListener("pointerdown", event => {
    const hit = (event.target as Element).closest<HTMLElement>("[data-grid-drag]");
    if (hit) on.dragStart(event, String(hit.dataset.gridDrag), overlay);
  });
  overlay.querySelector('[data-grid-action="confirm"]')!.addEventListener("click", event => { event.stopPropagation(); on.confirm(); });
  overlay.querySelector('[data-grid-action="cancel"]')!.addEventListener("click", event => { event.stopPropagation(); on.cancel(); });
  window.addEventListener("pointermove", event => on.dragMove(event), {capture: true});
  window.addEventListener("pointerup", event => on.dragEnd(event, true), {capture: true});
  window.addEventListener("pointercancel", event => on.dragEnd(event, false), {capture: true});
  chartEl.addEventListener("wheel", () => on.wheel(), {passive: true});
  chartEl.appendChild(overlay);
  return overlay;
}

export interface OverlayInput {
  height: number;
  priceToY: (price: number) => unknown;
  draft: Json;
  /** After a wheel zoom the handles follow the range off-screen instead of sticking to the edge. */
  unclamped: boolean;
  previewPending: boolean;
  splitPrice: number;
}

export interface GhostLine { top: number; side: "buy" | "sell"; boundary: boolean; split: boolean }

export interface OverlayLayout {
  shade: {top: number; height: number};
  upperTop: number;
  lowerTop: number;
  interior: {top: number; height: number};
  /** Only a moved draft shows its levels. */
  ghosts: GhostLine[];
  actions: {hidden: boolean; top: number};
  confirm: {disabled: boolean; text: string};
}

/** Where the overlay's parts go; null when the range has no position on the chart. */
export function gridOverlayLayout({height, priceToY, draft, unclamped, previewPending, splitPrice}: OverlayInput): OverlayLayout | null {
  const rawTop = Number(priceToY(draft.high)), rawBottom = Number(priceToY(draft.low));
  if (!Number.isFinite(rawTop) || !Number.isFinite(rawBottom) || height <= 0) return null;
  const top = Math.max(0, Math.min(height, rawTop)), bottom = Math.max(top, Math.min(height, rawBottom));
  const ghosts: GhostLine[] = [];
  if (draft.dirty) {
    const lines = draftGridLinePresentation(draft, splitPrice);
    lines.forEach((line, index) => {
      const y = Number(priceToY(line.price)), boundary = index === 0 || index === lines.length - 1;
      if (!Number.isFinite(y) || (!boundary && (y < 0 || y > height))) return;
      ghosts.push({top: boundary ? Math.max(0, Math.min(height, y)) : y, side: line.side, boundary, split: line.split});
    });
  }
  return {
    shade: {top, height: Math.max(0, bottom - top)},
    upperTop: unclamped ? rawTop - 8 : Math.max(0, top - 8),
    lowerTop: unclamped ? rawBottom - 8 : Math.max(0, bottom - 8),
    interior: {top: Math.min(height, top + 8), height: Math.max(0, bottom - top - 16)},
    ghosts,
    actions: {hidden: !draft.dirty, top: Math.max(8, Math.min(height - 40, top + 8))},
    confirm: {disabled: previewPending, text: previewPending ? "核对中…" : draft.scope === "startup" ? "应用参数" : "确认"},
  };
}

export function ghostLineHtml(line: GhostLine): string {
  return `<div class="grid-ghost-line ${line.side} ${line.boundary ? "boundary" : ""} ${line.split ? "split" : ""}" data-grid-side="${line.side}" style="top:${line.top}px"></div>`;
}

/** Shows or hides the overlay and, when shown, places its parts. */
export function renderGridOverlay(overlay: HTMLElement, active: boolean, input: () => OverlayInput): void {
  overlay.classList.toggle("on", active);
  overlay.setAttribute("aria-hidden", active ? "false" : "true");
  if (!active) return;
  const layout = gridOverlayLayout(input());
  if (!layout) return;
  const part = (selector: string) => overlay.querySelector<HTMLElement>(selector)!;
  const shade = part(".grid-adjust-shade"), upper = part(".grid-hit.upper"), lower = part(".grid-hit.lower");
  const interior = part(".grid-hit.interior"), actions = part(".grid-draft-actions");
  shade.style.top = `${layout.shade.top}px`;
  shade.style.height = `${layout.shade.height}px`;
  upper.style.top = `${layout.upperTop}px`;
  lower.style.top = `${layout.lowerTop}px`;
  interior.style.top = `${layout.interior.top}px`;
  interior.style.height = `${layout.interior.height}px`;
  part(".grid-ghosts").innerHTML = layout.ghosts.map(ghostLineHtml).join("");
  actions.classList.toggle("hidden", layout.actions.hidden);
  actions.style.left = "12px";
  actions.style.top = `${layout.actions.top}px`;
  const confirm = actions.querySelector<HTMLButtonElement>('[data-grid-action="confirm"]')!;
  confirm.disabled = layout.confirm.disabled;
  confirm.textContent = layout.confirm.text;
}

// --- history: when a drag or wheel to the left edge loads older bars ------------------------------

export function shouldLoadOlderHistory(drag: Json, range: Json): boolean {
  const startFrom = Number(drag?.startRange?.from), from = Number(range?.from);
  const draggedRight = Number(drag?.lastX) - Number(drag?.startX) >= 12, revealedOlder = from < startFrom - .25;
  const alreadyAtOldestEdge = startFrom <= 24 && from <= 24;
  return Boolean(draggedRight && from <= 24 && (revealedOlder || alreadyAtOldestEdge));
}

export function historyInputCanLoad(range: Json, inputUntil: unknown, startRange: Json, now = Date.now()): boolean {
  const from = Number(range?.from), startFrom = Number(startRange?.from);
  return Number.isFinite(from) && Number.isFinite(startFrom) && from <= 24 && from < startFrom - .25 && Number(inputUntil) >= Number(now);
}

// --- price lines ----------------------------------------------------------------------------------

/** The price span of the visible candles: the chart's own answer (`adapted`) when it has one, else the last 100 bars. */
export function visibleOhlcPriceWindow(market: Json, adapted: PriceWindow | null | undefined): PriceWindow | null {
  if (adapted) return adapted;
  const bars: Json[] = Array.isArray(market?.bars) ? market.bars.slice(-100) : [];
  const prices = bars.flatMap(bar => [Number(bar.low), Number(bar.high)]).filter(Number.isFinite);
  if (!prices.length) return null;
  return {min: Math.min(...prices), max: Math.max(...prices)};
}

export function priceInsideVisibleOhlc(price: unknown, window: PriceWindow | null): boolean {
  return Number.isFinite(Number(price)) && (!window || (Number(price) >= window.min && Number(price) <= window.max));
}

export interface PriceLineInput {
  market: Json;
  plan: Json;
  execution: Json;
  /** The startup preview, or null while adjusting the range or with none. */
  preview: Json;
  priceWindow: PriceWindow | null;
  formStrategyType: unknown;
}

const nearest = <T extends {price?: unknown}>(rows: T[], latest: number): T | undefined =>
  rows.slice().sort((a, b) => Math.abs(Number(a.price) - latest) - Math.abs(Number(b.price) - latest)).at(0);

/** The lines drawn over the candles: range edges, grid levels, open (or previewed) orders, the hard stop and
 * position averages, each only when its price is on screen. One order (the nearest) carries an axis label. */
export function buildVisibleChartPriceLines({market, plan, execution, preview, priceWindow, formStrategyType}: PriceLineInput): PriceLine[] {
  const lines: PriceLine[] = [], visual = preview || plan, low = Number(visual?.range?.low), high = Number(visual?.range?.high);
  const latest = Number(market?.latest_close), previewing = Boolean(preview);
  // A preview without orders throws here, as it did in gridmind.js.
  const visibleOrders: Json[] = previewing ? preview.orders : (execution.open_orders || []);
  const inside = (price: unknown) => priceInsideVisibleOhlc(price, priceWindow);
  if (visual && isDcaType(visual.strategy_type, formStrategyType)) {
    const dca = visual?.dca || {}, entries: Json[] = previewing ? (visual.entries || []) : (dca.entries || []);
    const entryColor = visual.direction === "short" ? "rgba(255,98,95,.46)" : "rgba(40,199,111,.46)";
    const visibleEntries = entries.filter(row => inside(row.price)), labelledEntry = nearest(visibleEntries, latest);
    for (const entry of visibleEntries) lines.push({price: Number(entry.price), color: entryColor, lineWidth: 1, lineStyle: "solid",
      title: entry === labelledEntry ? (previewing ? "DCA 预览加仓" : "DCA 加仓") : "", axisLabelVisible: entry === labelledEntry});
    if (inside(dca.target_price)) lines.push({price: Number(dca.target_price), color: "rgba(47,191,113,.9)", lineWidth: 2, lineStyle: "solid", title: "整轮止盈", axisLabelVisible: true});
    if (inside(dca.stop_price)) lines.push({price: Number(dca.stop_price), color: "rgba(246,70,93,.9)", lineWidth: 2, lineStyle: "dashed", title: "止损", axisLabelVisible: true});
    for (const trade of execution?.open_positions || []) if (inside(trade.entry_price)) lines.push({price: Number(trade.entry_price), color: "rgba(85,162,255,.7)", lineWidth: 2, title: "DCA 持仓均价", axisLabelVisible: true});
    return lines;
  }
  if (inside(low)) lines.push({price: low, color: previewing ? "rgba(140,116,255,.58)" : "rgba(53,189,113,.58)", lineWidth: 2, title: previewing ? "预览下界" : "Range low", axisLabelVisible: false});
  if (inside(high)) lines.push({price: high, color: previewing ? "rgba(140,116,255,.58)" : "rgba(239,95,95,.58)", lineWidth: 2, title: previewing ? "预览上界" : "Range high", axisLabelVisible: false});
  const visibleGridLevels = ((visual?.grid?.levels || []) as unknown[]).map(Number).filter(price => inside(price));
  const visibleWindowOrders = visibleOrders.filter(order => inside(order.price));
  const orderPriceKeys = new Set(visibleWindowOrders.map(order => Number(order.price).toFixed(4)));
  for (const price of visibleGridLevels) {
    if (price <= low || price >= high || orderPriceKeys.has(price.toFixed(4))) continue;
    lines.push({price, color: "rgba(148,163,184,.16)", lineWidth: 1, lineStyle: "solid", title: "", axisLabelVisible: false});
  }
  const labelledOrder = nearest(visibleWindowOrders, latest);
  for (const order of visibleWindowOrders) {
    const buy = order.side === "buy", showLabel = order === labelledOrder;
    lines.push({price: Number(order.price), color: buy ? "rgba(40,199,111,.38)" : "rgba(255,98,95,.38)", lineWidth: 1, lineStyle: "solid",
      title: showLabel ? (previewing ? `预览${buy ? "买" : "卖"}` : `挂${buy ? "买" : "卖"} v${order.strategy_plan_version ?? "--"}`) : "", axisLabelVisible: showLabel});
  }
  if (!previewing && visual?.hard_stop != null && typeof visual.hard_stop !== "object" && inside(visual.hard_stop))
    lines.push({price: Number(visual.hard_stop), color: "rgba(240,165,46,.8)", lineWidth: 2, lineStyle: "dashed", title: "硬止损", axisLabelVisible: true});
  for (const trade of execution?.open_positions || []) if (inside(trade.entry_price)) lines.push({price: Number(trade.entry_price), color: "rgba(85,162,255,.5)", lineWidth: 1, title: "持仓均价", axisLabelVisible: false});
  return lines;
}

// --- the chart's data -----------------------------------------------------------------------------

/** What the chart shows a range for; when it changes the price scale re-fits once. */
export function chartVisualKey(visual: Json, previewing: boolean, formStrategyType: unknown): string {
  const dca = isDcaType(visual?.strategy_type, formStrategyType);
  const dcaPrices: unknown[] = dca ? ((previewing ? visual?.dca?.entry_levels : visual?.dca?.entries?.map((row: Json) => row.price)) || []) : [];
  const low = dca ? Math.min(...dcaPrices.map(Number)) : Number(visual?.range?.low);
  const high = dca ? Math.max(...dcaPrices.map(Number)) : Number(visual?.range?.high);
  return `${visual?.strategy_type || "grid"}:${Number.isFinite(low) ? low : "--"}:${Number.isFinite(high) ? high : "--"}:${previewing ? "preview" : "production"}`;
}

export interface IndicatorChoice { ema?: unknown; macd?: unknown; fast?: unknown; slow?: unknown }

export function chartIndicators(choice: IndicatorChoice): {ema?: {period: unknown}[]; macd?: {fast: number; slow: number; signal: number}} {
  const indicators: {ema?: {period: unknown}[]; macd?: {fast: number; slow: number; signal: number}} = {};
  if (choice.ema) indicators.ema = [{period: choice.fast}, {period: choice.slow}];
  if (choice.macd) indicators.macd = {fast: 12, slow: 26, signal: 9};
  return indicators;
}
