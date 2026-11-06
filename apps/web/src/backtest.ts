/* 开仓前回测 (#1309): history for the previewed gold grid, shown above 「你亲手启动」.
 * Reference only. View logic over one GET result; it never touches the start button. */
import type {Evidence} from "./evidence";

export interface BacktestResult {
  status: "running" | "ready" | "unavailable" | string;
  reason?: string;
  history_start?: string;
  history_end?: string;
  evidence?: Evidence;
  summary?: {
    runs: number;
    funded_runs: number;
    median_return_pct: number | null;
    worst_return_pct: number | null;
    best_return_pct: number | null;
    stopped_pct: number;
    mean_fills: number;
    histogram: {low: number | null; width?: number; counts: number[]};
  };
}

export type BacktestView =
  | {state: "running" | "unavailable"; text: string}
  | {state: "ready"; title: string; sub: string; cells: Array<[string, string]>; bars: number[]; range: [string, string]; notes: string[]};

export const pct = (value: number | null | undefined) =>
  value == null ? "—" : `${value > 0 ? "+" : value < 0 ? "−" : ""}${Math.abs(value).toFixed(2)}%`;
const days = (a = "", b = "") => Math.round((Date.parse(b) - Date.parse(a)) / 86400000);

export function view(result: BacktestResult | null | undefined): BacktestView {
  if (!result || result.status === "running") return {state: "running", text: "正在用过去 180 天的 OKX 行情回测这组网格，约 1 分钟…"};
  if (result.status !== "ready" || !result.summary) return {state: "unavailable", text: `回测不可用：${result.reason || "原因未知"}。不影响启动。`};
  const s = result.summary, total = s.runs, funded = s.funded_runs;
  return {
    state: "ready",
    title: `这组网格过去 ${days(result.history_start, result.history_end)} 天的表现（OKX，费后）`,
    sub: `跑了 ${total} 段，每段 7 天，互不重叠`,
    cells: [
      ["中位收益", pct(s.median_return_pct)],
      ["最差一次", pct(s.worst_return_pct)],
      ["跌破下沿或止损", `${s.stopped_pct}%`],
      ["平均成交", `${s.mean_fills} 笔`],
    ],
    bars: s.histogram.counts,
    range: [pct(s.histogram.low), pct(s.best_return_pct)],
    notes: [
      "手续费是 OKX Paper 的假设费率，尚未用账户核实",
      funded === total ? "资金费已计入" : funded ? `资金费只计入了 ${funded} / ${total} 段（OKX 只给最近约 94 天）` : "未含资金费",
    ],
  };
}
