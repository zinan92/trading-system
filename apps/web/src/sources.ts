/* AI 透明度 (#1317): what an AI line was given, what it says it used, and which of its numbers
 * have no source. Pure view logic over data the server already stored; it calls nothing. */

export type PriceSource = "venue" | "bars_1h" | "none" | "unknown";

export interface Evidence {
  price?: {id?: string; value?: number | null; source?: string; at?: string};
  kline?: {id?: string; date?: string; cutoff_at?: string; synthesis?: string; missing?: string};
  news?: Array<{id: string; title: string; source?: string; url?: string; collected_at?: string}>;
  news_stale?: boolean;
  news_missing?: string;
  three?: Array<{id: string; title: string}>;
  unverified?: string[];
  model?: {channel?: string; asked_at?: string};
}

export interface Judgment {
  id?: number | string;
  cited?: string[];
  evidence?: Evidence | null;
}

export interface SourceItem {
  label: string;
  text: string;
  url?: string;
  cited: boolean;
}

export type AdviceView =
  | {empty: string}
  | {summary: string; flags: string[]; unverified: string[]; items: SourceItem[]; model: string};

const PRICE_SOURCE: Record<string, string> = {venue: "交易场所现价", bars_1h: "1 小时 K 线收盘价", none: "读不到", unknown: "来源未记录"};
const priceSource = (source?: string) => PRICE_SOURCE[source ?? ""] ?? "来源未记录";
const num = (value: number) => Number(value).toLocaleString("zh-CN", {maximumFractionDigits: 2});

export function when(ts?: string | null): string {
  const at = new Date(ts ?? "");
  if (!ts || Number.isNaN(at.getTime())) return "时间缺失";
  const parts = new Intl.DateTimeFormat("zh-CN", {timeZone: "Asia/Shanghai", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false}).formatToParts(at);
  const get = (type: string) => parts.find(part => part.type === type)?.value ?? "";
  return `${get("month")}/${get("day")} ${get("hour")}:${get("minute")}`;
}

export function advice(judgment: Judgment | null | undefined): AdviceView {
  const ev = judgment?.evidence ?? {};
  if (!ev.price && !ev.news) return {empty: "这条建议生成时还没有记录依据"};
  const cited = new Set(judgment?.cited ?? []);
  const price = ev.price ?? {}, kline = ev.kline ?? {}, news = ev.news ?? [], three = ev.three ?? [];
  const priceText = price.value == null ? "现价读不到" : `现价 ${num(price.value)}（${priceSource(price.source)}）`;
  const klineText = kline.missing ? "没有 K 线日报" : `K 线日报 ${String(kline.date ?? "").slice(5).replace("-", "/") || "日期缺失"}`;
  const usedNews = news.filter(n => cited.has(n.id)).length;
  const summary = [priceText, klineText, `新闻 ${news.length} 条${news.length ? `，引用 ${usedNews} 条` : ""}`, `三件事 ${three.length} 件`].join(" · ");
  const flags: string[] = [];
  if (price.value == null) flags.push("模型拿到的现价是空的");
  if (ev.news_stale) flags.push("新闻是旧列表（Intel 暂时读不到）");
  if (ev.news_missing) flags.push(`没有新闻：${ev.news_missing}`);
  if (!cited.size) flags.push("AI 没说依据哪几条");
  const items: SourceItem[] = [
    {label: "现价", text: price.value == null ? `读不到 · ${when(price.at)}` : `${num(price.value)} · ${priceSource(price.source)} · ${when(price.at)}`, cited: cited.has("price")},
    {label: "K 线日报", text: kline.missing ? kline.missing : `${kline.date || "日期缺失"} · 截至 ${when(kline.cutoff_at)}${kline.synthesis ? ` · ${kline.synthesis}` : ""}`, cited: cited.has("kline")},
    ...news.map(n => ({label: n.id, text: `${n.title} · ${n.source || "来源缺失"} · ${when(n.collected_at)}`, url: n.url || "", cited: cited.has(n.id)})),
    ...three.map(w => ({label: w.id, text: `三件事：${w.title}`, cited: cited.has(w.id)})),
  ];
  return {summary, flags, unverified: ev.unverified ?? [], items, model: ev.model ? `${ev.model.channel || "模型"} · ${when(ev.model.asked_at)}` : ""};
}

export interface WatchSourceInput {
  why?: string;
  source_title?: string;
  detail?: string;
  unverified?: string[];
}

/** #1319: where a 三件事 line comes from; nothing to add when there is no model text. */
export function watchSource(item: WatchSourceInput | null | undefined): {source: string; unverified: string[]} {
  if (!item?.why) return {source: "", unverified: []};
  return {source: [item.source_title, item.detail].filter(Boolean).join(" · ") || "来源缺失", unverified: item.unverified ?? []};
}
