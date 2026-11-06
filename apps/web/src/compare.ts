/* 回测工作台 (#1335): view logic for a comparison result. Pure; it sends nothing. */
import {pct} from "./backtest";

export interface DcaParams {amount_usdt: number; interval_hours: number}

export interface GridParams {direction: "long" | "short" | "neutral"; below_pct: number; above_pct: number; levels: number; notional_per_level: number; stop_pct: number | null}

export interface GridSummary {
  runs: number;
  median_return_pct: number;
  worst_return_pct: number;
  best_return_pct: number;
  stopped_pct: number;
  mean_fills: number;
  funded_runs: number;
  histogram: {low: number; width: number; counts: number[]};
}

export interface DcaSummary {
  runs: number;
  median_return_pct: number;
  worst_return_pct: number;
  best_return_pct: number;
  median_drawdown_pct: number;
  worst_drawdown_pct: number;
  mean_buys: number;
  invested_per_run: number;
  funded_runs: number;
  histogram: {low: number; width: number; counts: number[]};
}

export interface CompareResult {
  status: "running" | "ready" | "unavailable" | string;
  reason?: string;
  asset?: string;
  strategy?: string;
  instrument?: string;
  source?: string;
  fee_rate?: number;
  history_start?: string;
  history_end?: string;
  variants?: Array<{params: DcaParams; summary: DcaSummary} | {params: GridParams; summary: GridSummary; account_equity?: number}>;
  /** Grid only (#1337): the backtest_only contract the replay used. */
  contract?: {status: string; observed_at?: string; price_increment: string; size_increment: string} | null;
}

export type Strategy = "dca" | "grid";
export const GRID_ASSETS = ["XAU", "BTC"];  // each replays against its own backtest contract (#1337)
export const DEFAULT_GRID_VARIANTS: GridParams[] = [
  {direction: "neutral", below_pct: 3, above_pct: 3, levels: 9, notional_per_level: 500, stop_pct: null},
  {direction: "neutral", below_pct: 5, above_pct: 5, levels: 9, notional_per_level: 500, stop_pct: null},
];
const DIRECTION: Record<string, string> = {long: "做多", short: "做空", neutral: "双向"};
export const gridLabel = (p: GridParams) => `${DIRECTION[p.direction]} −${p.below_pct}%/+${p.above_pct}% · ${p.levels} 格 × ${p.notional_per_level} USDT${p.stop_pct ? ` · 止损 ${p.stop_pct}%` : ""}`;

export function checkGrid(variants: GridParams[]): string {
  if (!variants.length || variants.length > MAX_VARIANTS) return `参数要有 1–${MAX_VARIANTS} 组`;
  for (const [i, v] of variants.entries()) {
    const n = `第 ${i + 1} 组：`;
    if (!(v.below_pct >= 0.5 && v.below_pct <= 30 && v.above_pct >= 0.5 && v.above_pct <= 30)) return `${n}上下沿要在现价 0.5%–30% 之内`;
    if (!Number.isInteger(v.levels) || v.levels < 2 || v.levels > 30) return `${n}格数要是 2–30 的整数`;
    if (!(v.notional_per_level >= 10 && v.notional_per_level <= 100000)) return `${n}每格金额要在 10–100000 USDT 之间`;
    if (v.stop_pct) {
      if (v.direction === "neutral") return `${n}双向网格用上下沿止损，不另设止损`;
      const edge = v.direction === "long" ? v.below_pct : v.above_pct;
      if (!(v.stop_pct > 0 && v.stop_pct < edge)) return `${n}止损要在现价和区间边沿之间`;
    }
  }
  return "";
}

export function gridRows(result: CompareResult): Row[] {
  const s = (result.variants ?? []).map(v => v.summary as GridSummary);
  const medians = s.map(x => x.median_return_pct);
  const top = Math.max(...medians);
  const best = medians.filter(m => m === top).length === 1 ? medians.indexOf(top) : undefined;
  return [
    {label: "中位收益", cells: s.map(x => pct(x.median_return_pct)), best},
    {label: "最差一次", cells: s.map(x => pct(x.worst_return_pct))},
    {label: "最好一次", cells: s.map(x => pct(x.best_return_pct))},
    {label: "跌破下沿或止损", cells: s.map(x => `${x.stopped_pct}%`)},
    {label: "平均成交", cells: s.map(x => `${x.mean_fills} 笔`)},
  ];
}

export function gridNotes(result: CompareResult): string[] {
  const first = result.variants?.[0];
  if (!first) return [];
  const {funded_runs: funded, runs: total} = first.summary;
  const equity = "account_equity" in first && first.account_equity ? first.account_equity : 10000;
  return [
    `${result.instrument}（${result.source}）${day(result.history_start)} 到 ${day(result.history_end)}，每段 7 天，${total} 段互不重叠`,
    `用 Paper 自己的 Nautilus 回放和补单规则；收益按 ${equity.toLocaleString("zh-CN")} USDT 账户算，费后`,
    ...(result.contract ? [`合约精度取自 OKX 的合约说明：价格 ${result.contract.price_increment}，数量 ${result.contract.size_increment}；只用于回测`] : []),
    "手续费是 OKX Paper 的假设费率，尚未用账户核实",
    funded === total ? "资金费已计入" : funded ? `资金费只计入了 ${funded} / ${total} 段（OKX 只给最近约 94 天）` : "未含资金费",
    "只是历史参考，不代表未来",
  ];
}

const day = (ts?: string) => (ts ?? "").slice(0, 10);

export const ASSETS: Array<[string, string]> = [["XAU", "黄金"], ["BTC", "BTC"]];
export const DEFAULT_VARIANTS: DcaParams[] = [{amount_usdt: 100, interval_hours: 24}, {amount_usdt: 25, interval_hours: 6}];  // the same 700 USDT a week, spread differently
export const MAX_VARIANTS = 3;

export const variantLabel = (p: DcaParams) =>
  `每 ${p.interval_hours % 24 === 0 ? `${p.interval_hours / 24} 天` : `${p.interval_hours} 小时`}买 ${p.amount_usdt} USDT`;

export function check(variants: DcaParams[]): string {
  if (!variants.length || variants.length > MAX_VARIANTS) return `参数要有 1–${MAX_VARIANTS} 组`;
  for (const [i, v] of variants.entries()) {
    if (!(v.amount_usdt >= 10 && v.amount_usdt <= 100000)) return `第 ${i + 1} 组：每次金额要在 10–100000 USDT 之间`;
    if (!Number.isInteger(v.interval_hours) || v.interval_hours < 1 || v.interval_hours > 168) return `第 ${i + 1} 组：间隔要是 1–168 之间的整数小时`;
  }
  return "";
}

export interface Row {label: string; cells: string[]; best?: number}

/** One row per measure, one cell per variant. `best` marks the highest median return only; no "推荐". */
export function rows(result: CompareResult): Row[] {
  const s = (result.variants ?? []).map(v => v.summary as DcaSummary);
  const medians = s.map(x => x.median_return_pct);
  const top = Math.max(...medians);
  const best = medians.filter(m => m === top).length === 1 ? medians.indexOf(top) : undefined;
  return [
    {label: "每段投入", cells: s.map(x => `${x.invested_per_run.toLocaleString("zh-CN")} USDT`)},
    {label: "中位收益", cells: s.map(x => pct(x.median_return_pct)), best},
    {label: "最差一次", cells: s.map(x => pct(x.worst_return_pct))},
    {label: "最好一次", cells: s.map(x => pct(x.best_return_pct))},
    {label: "最大浮亏（中位）", cells: s.map(x => pct(x.median_drawdown_pct))},
    {label: "最大浮亏（最差）", cells: s.map(x => pct(x.worst_drawdown_pct))},
    {label: "平均买入", cells: s.map(x => `${x.mean_buys} 次`)},
  ];
}


export function notes(result: CompareResult): string[] {
  const first = result.variants?.[0]?.summary as DcaSummary | undefined;
  if (!first) return [];
  const funded = first.funded_runs, total = first.runs;
  return [
    `${result.instrument}（${result.source}）${day(result.history_start)} 到 ${day(result.history_end)}，每段 7 天，${total} 段互不重叠`,
    `按每分钟收盘价买入，手续费按 OKX Paper 假设费率 ${((result.fee_rate ?? 0) * 100).toFixed(2)}%，尚未用账户核实`,
    funded === total ? "资金费已计入" : funded ? `资金费只计入了 ${funded} / ${total} 段（OKX 只给最近约 94 天）` : "未含资金费",
    "只是历史参考，不代表未来",
  ];
}
