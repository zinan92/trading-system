/* 「今天」 (#1327): the pure parts of judgment → plan, shared by the panel and its tests.
 * Wording, formats and request bodies are the legacy gm-desk.js ones, unchanged. */

export type Direction = "long" | "flat" | "short";
export interface Cite {id?: string | number | null; title: string; bucket?: string}
export interface Asset {key: string; label: string; kind?: string; instrument_id?: string}

export interface Plan {
  kind: "keep" | "new" | "hold" | "unavailable" | "record_only" | string;
  title: string;
  lines: Array<[string, string]>;
  note: string;
  rungs?: number[] | null;
}

export interface Execution {judgment_id?: number | string; stage: string; detail?: Record<string, unknown> | null}
export interface TodayJudgment {
  id: number | string; direction: Direction; reason?: string; confidence: number; cited?: Cite[]; action?: string;
  /** The AI call this judgment was drafted from (#1370), as it was then; null when Park did not adopt one. */
  advice?: {id: number | string; direction: Direction; created_at: string} | null;
}
export interface AiCall {
  id: number | string; direction: Direction; confidence: number; reason: string; created_at: string;
  cited?: string[]; evidence?: Record<string, unknown> | null;
}
export interface Desk {
  kline?: {ok?: boolean; structure?: string; synthesis?: string; reason?: string} | null;
  today_judgment?: TodayJudgment | null;
  executions?: Execution[];
  ai_today?: AiCall | null;
  ai_running?: boolean;
  control?: Record<string, unknown> | null;
  meta?: {kind?: string} | null;
}

export const PLAN_TAG: Record<string, string> = {keep: "沿用当前网格", new: "需要批准", hold: "不开新单", unavailable: "暂不可用", record_only: "只记录"};
export const AI_CALL: Record<Direction, string> = {long: "做多", short: "做空", flat: "观望"};
export const READING_LABEL = {long: "▲ 偏多", short: "▼ 偏空", wait: "– 观望"} as const;

/** gm-desk.js fmtD. */
export function fmtD(n: unknown, d?: number): string {
  if (n == null || isNaN(Number(n))) return "—";
  const v = Number(n);
  return v.toLocaleString("en-US", {minimumFractionDigits: d ?? (Math.abs(v) >= 1000 ? 0 : 2), maximumFractionDigits: d ?? (Math.abs(v) >= 1000 ? 1 : 2)});
}

export const readingDirection = (s?: string): "long" | "short" | "wait" =>
  /分歧|等待确认/.test(s || "") ? "wait" : /偏多/.test(s || "") ? "long" : /偏空/.test(s || "") ? "short" : "wait";

/** First sentence of the K-line synthesis, as the legacy reading line showed it. */
export const firstSentence = (s?: string) => (s || "").split(/(?<=[。；])/)[0] ?? "";

/** The /api/plan body, byte for byte the legacy one. */
export const planBody = (asset: string, direction: Direction) => JSON.stringify({asset, direction});

/** The /api/judgments body, byte for byte the legacy one (key order included). The adopted AI call's id (#1370)
 * goes last and only when Park pressed 「照 AI 的方向判断」 and kept that direction, so every other body is unchanged. */
export const judgmentBody = (asset: string, direction: Direction, confidence: number, reason: string, cited: Cite[], action: "approved" | "recorded",
                             adviceId?: number | string | null) =>
  JSON.stringify({asset, direction, confidence: +confidence, reason, cited, action, ...(adviceId != null ? {advice_id: Number(adviceId)} : {})});

export const DONE_STAGES = ["executed", "execute_started", "execute_failed", "refused"];

/** What the legacy page restored after a reload: today's approved judgment, its preview and whether it already ran. */
export function execRestore(desk: Desk): {judgmentId: number | string; preview: Record<string, unknown>; done: Execution | null} | null {
  const tj = desk.today_judgment;
  if (!tj || tj.action !== "approved") return null;
  const runs = (desk.executions || []).filter(e => e.judgment_id === tj.id);
  const preview = runs.find(e => e.stage === "preview");
  if (!preview) return null;
  const done = runs.find(e => DONE_STAGES.includes(e.stage)) ?? null;
  return {judgmentId: tj.id, preview: (preview.detail ?? {}) as Record<string, unknown>, done};
}

/** gm-desk.js ago(): a news time as the rail showed it. */
export function ago(stamp: string | undefined, now: Date = new Date()): string {
  if (!stamp) return "";
  const at = new Date(stamp + (/[Z+]/.test(stamp.slice(19)) ? "" : "Z")), minutes = Math.round((now.getTime() - at.getTime()) / 60000);
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return `${minutes} 分钟前`;
  return (at.toDateString() === now.toDateString() ? "" : `${at.getMonth() + 1}/${at.getDate()} `) + at.toLocaleTimeString("zh-CN", {hour: "2-digit", minute: "2-digit"});
}
