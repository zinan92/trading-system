/* AI 透明度 (#1317): what an AI line was given, what it says it used, and which of its numbers
 * have no source. Pure view logic over data the server already stored; it calls nothing. */
"use strict";

const DeskSources = (() => {
  const PRICE_SOURCE = {venue: "交易场所现价", bars_1h: "1 小时 K 线收盘价", none: "读不到", unknown: "来源未记录"};
  const num = value => Number(value).toLocaleString("zh-CN", {maximumFractionDigits: 2});

  function when(ts) {
    const at = new Date(ts);
    if (!ts || Number.isNaN(at.getTime())) return "时间缺失";
    const parts = new Intl.DateTimeFormat("zh-CN", {timeZone: "Asia/Shanghai", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false}).formatToParts(at);
    const get = type => parts.find(part => part.type === type)?.value || "";
    return `${get("month")}/${get("day")} ${get("hour")}:${get("minute")}`;
  }

  function advice(judgment) {
    const ev = judgment?.evidence || {};
    if (!ev.price && !ev.news) return {empty: "这条建议生成时还没有记录依据"};
    const cited = new Set(judgment.cited || []);
    const price = ev.price || {}, kline = ev.kline || {}, news = ev.news || [], three = ev.three || [];
    const priceText = price.value == null ? "现价读不到" : `现价 ${num(price.value)}（${PRICE_SOURCE[price.source] || PRICE_SOURCE.unknown}）`;
    const klineText = kline.missing ? "没有 K 线日报" : `K 线日报 ${String(kline.date || "").slice(5).replace("-", "/") || "日期缺失"}`;
    const usedNews = news.filter(n => cited.has(n.id)).length;
    const summary = [priceText, klineText, `新闻 ${news.length} 条${news.length ? `，引用 ${usedNews} 条` : ""}`, `三件事 ${three.length} 件`].join(" · ");
    const flags = [];
    if (price.value == null) flags.push("模型拿到的现价是空的");
    if (ev.news_stale) flags.push("新闻是旧列表（Intel 暂时读不到）");
    if (ev.news_missing) flags.push(`没有新闻：${ev.news_missing}`);
    if (!cited.size) flags.push("AI 没说依据哪几条");
    const unverified = ev.unverified || [];
    const items = [
      {label: "现价", text: price.value == null ? `读不到 · ${when(price.at)}` : `${num(price.value)} · ${PRICE_SOURCE[price.source] || PRICE_SOURCE.unknown} · ${when(price.at)}`, cited: cited.has("price")},
      {label: "K 线日报", text: kline.missing ? kline.missing : `${kline.date || "日期缺失"} · 截至 ${when(kline.cutoff_at)}${kline.synthesis ? ` · ${kline.synthesis}` : ""}`, cited: cited.has("kline")},
      ...news.map(n => ({label: n.id, text: `${n.title} · ${n.source || "来源缺失"} · ${when(n.collected_at)}`, url: n.url || "", cited: cited.has(n.id)})),
      ...three.map(w => ({label: w.id, text: `三件事：${w.title}`, cited: cited.has(w.id)})),
    ];
    return {summary, flags, unverified, items, model: ev.model ? `${ev.model.channel || "模型"} · ${when(ev.model.asked_at)}` : ""};
  }

  function watch(item) {  // #1319: where a 三件事 line comes from; nothing to add when there is no model text
    if (!item?.why) return {source: "", unverified: []};
    return {source: [item.source_title, item.detail].filter(Boolean).join(" · ") || "来源缺失", unverified: item.unverified || []};
  }

  return {advice, watch, when};
})();

if (typeof module !== "undefined") module.exports = DeskSources;
if (typeof window !== "undefined") window.DeskSources = DeskSources;
