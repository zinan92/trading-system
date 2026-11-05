/* 本周要看的三件事: time labels and the header line, computed from /api/watch. */

export interface WatchItem {
  ref?: string;
  kind?: string;
  at?: string;
  title?: string;
  source_title?: string;
  detail?: string;
  why?: string;
  volatility?: string;
  markets?: string[];
  unverified?: string[];
}

export interface WatchData {
  generated_at?: string;
  provider?: string;
  items?: WatchItem[];
  replaced?: string[];
}

const WEEKDAY = "一二三四五六日";
const BEIJING_MS = 8 * 3600000;

/** "周四 20:30 · 还有 2 天 3 小时", in Beijing time whatever the browser's zone. */
export function whenLabel(item: WatchItem, now: number = Date.now()): string {
  const at = Date.parse(item.at ?? "");
  if (Number.isNaN(at)) return "";
  const bj = new Date(at + BEIJING_MS);
  const hm = `${String(bj.getUTCHours()).padStart(2, "0")}:${String(bj.getUTCMinutes()).padStart(2, "0")}`;
  if (item.kind === "breaking") return `突发 · ${hm}`;
  const ms = at - now, stamp = `周${WEEKDAY[(bj.getUTCDay() + 6) % 7]} ${hm}`;
  if (ms <= 0) return `${stamp} · 已公布`;
  const h = Math.floor(ms / 3600000), m = Math.floor(ms / 60000) % 60;
  return `${stamp} · ${h >= 24 ? `还有 ${Math.floor(h / 24)} 天 ${h % 24} 小时` : h ? `还有 ${h} 小时 ${m} 分` : `还有 ${m} 分钟`}`;
}

export function metaLine(w: WatchData | null): string {
  if (!w?.generated_at) return "还在生成";
  return `${w.generated_at.slice(11, 16)} 更新 · ${w.provider === "codex" ? "按全市场冲击排序" : "模型暂不可用，按日历重要性排序"}`;
}
