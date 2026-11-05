/* 交易回看 (#1306, island #1326): pick a past run, see its plan, fills, judgments and how it ended.
 * Pure view logic over /api/review/*; nothing here sends a request. */
import {when} from "./sources";

export {when};

export interface Run {
  id: string;
  asset?: string;
  environment?: string;
  label: string;
  started_at: string;
  ended_at: string;
  fill_count: number;
  status?: string;
}

export interface ScoreRow {total: number; hit?: number; miss?: number; even?: number; unverifiable?: number; pending?: number}
export interface Score {ai: ScoreRow; park: ScoreRow}

export interface RunsResponse {
  runs: Run[];
  score: Score;
  assets?: string[];
  filter?: {asset: string | null; days: number; since: string | null};
}

export interface Fill {shown_ts: string; side: string; backdated?: boolean}
export interface TimelineJudgment {created_at: string; author?: string}
export interface TimelineEvent {ts?: string; title: string; source: string; record: unknown}

export interface Timeline {
  run?: {environment?: string; reason?: string};
  result?: {net?: number | null; gross?: number | null; fees?: number | null; flat?: boolean; fill_count?: number; entries?: number; exits?: number; ended_flat?: boolean | null};
  market?: {source?: string; instrument?: string; timeframe?: string; bars?: unknown[]};
  fills?: Fill[];
  judgments?: TimelineJudgment[];
  events?: TimelineEvent[];
  lines?: Array<{price: number | string; title: string}>;
  missing?: string[];
}

export interface Marker {time: number; position: "belowBar" | "aboveBar"; shape: "arrowUp" | "arrowDown" | "circle"; color: string; text: string}
export interface Cell {label: string; value: string; big?: boolean; bad?: boolean}

const BUY = "#7fb2ff", SELL = "#f0a24b", JUDGE = "#c5a8ff";
const fmt = (value: number | null | undefined, digits = 2) =>
  value == null ? "—" : Number(value).toLocaleString("zh-CN", {minimumFractionDigits: digits, maximumFractionDigits: digits});
export const signed = (value: number | null | undefined) =>
  value == null ? "—" : `${value > 0 ? "+" : value < 0 ? "−" : ""}${fmt(Math.abs(value))}`;
const seconds = (ts: string) => { const at = Date.parse(ts); return Number.isNaN(at) ? null : Math.floor(at / 1000); };

export function runLabel(run: Run): string {
  return `${run.label} · ${when(run.started_at).split(" ")[0]} – ${when(run.ended_at).split(" ")[0]} · ${run.fill_count} 笔`;
}

export function markers(data: Timeline, snap: (time: number) => number): Marker[] {
  const out: Marker[] = [];
  for (const fill of data.fills ?? []) {
    const time = seconds(fill.shown_ts);
    if (time == null) continue;
    const buy = fill.side === "buy";
    out.push({time: snap(time), position: buy ? "belowBar" : "aboveBar", shape: buy ? "arrowUp" : "arrowDown", color: buy ? BUY : SELL, text: ""});
  }
  for (const judgment of data.judgments ?? []) {
    const time = seconds(judgment.created_at);
    if (time != null) out.push({time: snap(time), position: "aboveBar", shape: "circle", color: JUDGE, text: judgment.author === "park" ? "Park" : "AI"});
  }
  return out;
}

export function summary(data: Timeline): Cell[] {
  const result = data.result ?? {}, cells: Cell[] = [];
  if (result.net != null) cells.push({label: "这一轮结果（费后，未含资金费）", value: `${signed(result.net)} USDT`, big: true, bad: result.net < 0});
  else if (!result.flat) cells.push({label: "这一轮结果", value: "成交记录没有轧平，算不出结果", big: true, bad: true});
  else cells.push({label: "这一轮结果（费前，未含资金费）", value: `${signed(result.gross)} USDT`, big: true, bad: (result.gross ?? 0) < 0});
  cells.push({label: `${data.run?.environment === "testnet" ? "Testnet" : "Paper"} 成交`, value: `${result.fill_count} 笔（开 ${result.entries} · 平 ${result.exits}）`});
  if (result.fees != null) cells.push({label: "手续费", value: `${fmt(result.fees)} USDT`});
  cells.push({label: "结束原因", value: data.run?.reason || "原因见原始记录"});
  const open = result.flat ? "未空仓，之后全部结算" : "未空仓";
  cells.push({label: "策略结束时", value: result.ended_flat === true ? "已空仓" : result.ended_flat === false ? open : "记录缺失", bad: result.ended_flat !== true});
  return cells;
}

export function score(board: Score): string[] {
  const row = (name: string, item: ScoreRow) =>
    `${name}：${item.total ? `命中 ${item.hit} · 未中 ${item.miss} · 持平 ${item.even} · 无法核对 ${item.unverifiable} · 待定 ${item.pending}` : "0 条"}`;
  return [row("AI", board.ai), row("Park", board.park)];
}

export const PERIODS: Array<[number, string]> = [[0, "全部时间"], [7, "近 7 天"], [30, "近 30 天"], [90, "近 90 天"]];

export function runsUrl(asset: string, days: number): string {
  const params = new URLSearchParams();
  if (asset) params.set("asset", asset);
  if (days) params.set("days", String(days));
  const query = params.toString();
  return `/api/review/runs${query ? `?${query}` : ""}`;
}

export function emptyText(filter: {asset: string; days: number}): string {
  if (!filter.asset && !filter.days) return "还没有跑过的记录";
  const period = PERIODS.find(([d]) => d === filter.days)?.[1] ?? "";
  return `${[filter.asset, filter.days ? period : ""].filter(Boolean).join(" · ")} 没有跑过的记录`;
}
