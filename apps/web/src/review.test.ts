import {emptyText, runsUrl, score, summary, when, type Timeline} from "./review";

function data(result: Record<string, unknown> = {}): Timeline {
  const base = {fill_count: 56, entries: 28, exits: 28, flat: true, gross: -117.63, fees: 4.51, net: -122.14, ended_flat: false};
  return {run: {environment: "paper", reason: "价格跌破区间下沿"}, result: {...base, ...result}};
}

test("net result says what it excludes and an open end is flagged", () => {
  const cells = summary(data());
  expect(cells[0]).toEqual({label: "这一轮结果（费后，未含资金费）", value: "−122.14 USDT", big: true, bad: true});
  expect(cells).toContainEqual({label: "策略结束时", value: "未空仓，之后全部结算", bad: true});
});

test("a book that does not net to flat shows no result", () => {
  expect(summary(data({flat: false, net: null, gross: null}))[0]?.value).toBe("成交记录没有轧平，算不出结果");
});

test("missing end record is not called flat", () => {
  expect(summary(data({ended_flat: null}))).toContainEqual({label: "策略结束时", value: "记录缺失", bad: true});
});

test("times are Beijing and a bad time is named", () => {
  expect(when("2026-09-14T16:21:19+00:00")).toBe("9/15 00:21");
  expect(when("nope")).toBe("时间缺失");
});

test("a zero author shows zero", () => {
  const board = {ai: {total: 3, hit: 1, miss: 1, even: 0, unverifiable: 0, pending: 1}, park: {total: 0}};
  expect(score(board)).toEqual(["AI：命中 1 · 未中 1 · 持平 0 · 无法核对 0 · 待定 1", "Park：0 条"]);
});

test("filters go in the query only when set, and an empty result names the filter", () => {
  expect(runsUrl("", 0)).toBe("/api/review/runs");
  expect(runsUrl("XAU", 30)).toBe("/api/review/runs?asset=XAU&days=30");
  expect(emptyText({asset: "", days: 0})).toBe("还没有跑过的记录");
  expect(emptyText({asset: "BTC", days: 7})).toBe("BTC · 近 7 天 没有跑过的记录");
  expect(emptyText({asset: "", days: 90})).toBe("近 90 天 没有跑过的记录");
});
