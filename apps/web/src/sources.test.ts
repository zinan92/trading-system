import {advice, watchSource, type Judgment} from "./sources";

function call(evidence: Record<string, unknown> = {}): Judgment {
  const base = {
    price: {id: "price", value: 4132.6, source: "venue", at: "2026-11-05T00:40:00+00:00"},
    kline: {id: "kline", date: "2026-11-05", cutoff_at: "2026-11-04T23:00:00+00:00", synthesis: "日线弱势"},
    news: [{id: "n1", title: "金价跳水", source: "cls", url: "https://x/1", collected_at: "2026-11-05T00:10:00+00:00"},
           {id: "n2", title: "美元走强", source: "cls", url: "", collected_at: "2026-11-05T00:20:00+00:00"}],
    news_stale: false, three: [{id: "w1", title: "非农"}], unverified: [],
    model: {channel: "codex", asked_at: "2026-11-05T00:40:00+00:00"},
  };
  return {id: 7, cited: ["kline", "n2"], evidence: {...base, ...evidence}};
}

test("summary names each input and how many news it used", () => {
  const v = advice(call());
  if ("empty" in v) throw new Error("expected evidence");
  expect(v.summary).toBe("现价 4,132.6（交易场所现价） · K 线日报 11/05 · 新闻 2 条，引用 1 条 · 三件事 1 件");
  expect(v.flags).toEqual([]);
  expect(v.items.map(i => [i.label, i.cited])).toEqual([["现价", false], ["K 线日报", true], ["n1", false], ["n2", true], ["w1", false]]);
  expect(v.items[2]?.url).toBe("https://x/1");
  expect(v.items[2]?.text).toContain("11/5 08:10");
});

test("missing price, old news, no cited and unsourced numbers are said", () => {
  const data = call({price: {id: "price", value: null, source: "none", at: "2026-11-05T00:40:00+00:00"}, news_stale: true, unverified: ["4.2%"]});
  data.cited = [];
  const v = advice(data);
  if ("empty" in v) throw new Error("expected evidence");
  expect(v.summary.startsWith("现价读不到")).toBe(true);
  expect(v.flags).toEqual(["模型拿到的现价是空的", "新闻是旧列表（Intel 暂时读不到）", "AI 没说依据哪几条"]);
  expect(v.unverified).toEqual(["4.2%"]);
});

test("an old call without evidence says so", () => {
  expect(advice({cited: [], evidence: {}})).toEqual({empty: "这条建议生成时还没有记录依据"});
});

test("a watch line names its source and unbacked numbers", () => {
  const item = {why: "市场押注新增就业6.5万、失业率4.2%", source_title: "美国 Non-Farm Employment Change", detail: "预期 65K · 前值 22K", unverified: ["4.2%"]};
  expect(watchSource(item)).toEqual({source: "美国 Non-Farm Employment Change · 预期 65K · 前值 22K", unverified: ["4.2%"]});
  expect(watchSource({why: "", source_title: "x"})).toEqual({source: "", unverified: []});
});
