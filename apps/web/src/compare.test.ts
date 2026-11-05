import {check, notes, rows, variantLabel, type CompareResult} from "./compare";

const summary = (median: number) => ({runs: 25, median_return_pct: median, worst_return_pct: -3.2, best_return_pct: 4.1, median_drawdown_pct: -1.5, worst_drawdown_pct: -6.25,
  mean_buys: 7, invested_per_run: 700, funded_runs: 12, histogram: {low: -3.2, width: 0.6, counts: [1, 2, 3]}});
const RESULT: CompareResult = {status: "ready", asset: "BTC", instrument: "BTC-USDT-SWAP", source: "okx_swap_crypto_public", fee_rate: 0.0004,
  history_start: "2026-05-09T08:26:00+00:00", history_end: "2026-11-05T08:25:00+00:00",
  variants: [{params: {amount_usdt: 100, interval_hours: 24}, summary: summary(0.5)}, {params: {amount_usdt: 100, interval_hours: 168}, summary: summary(1.25)}]};

test("rows put each variant in its own column and mark only the higher median", () => {
  const table = rows(RESULT);
  expect(table.map(r => r.label)).toEqual(["每段投入", "中位收益", "最差一次", "最好一次", "最大浮亏（中位）", "最大浮亏（最差）", "平均买入"]);
  const median = table.find(r => r.label === "中位收益")!;
  expect(median.cells).toEqual(["+0.50%", "+1.25%"]);
  expect(median.best).toBe(1);
  expect(table.filter(r => r.best !== undefined)).toHaveLength(1);
  expect(rows({...RESULT, variants: [RESULT.variants![0]!, RESULT.variants![0]!]}).find(r => r.label === "中位收益")!.best).toBeUndefined();  // a tie marks nothing
});

test("notes say the source, the fee assumption and how much funding is in", () => {
  expect(notes(RESULT)).toEqual([
    "BTC-USDT-SWAP（okx_swap_crypto_public）2026-05-09 到 2026-11-05，每段 7 天，25 段互不重叠",
    "按每分钟收盘价买入，手续费按 OKX Paper 假设费率 0.04%，尚未用账户核实",
    "资金费只计入了 12 / 25 段（OKX 只给最近约 94 天）",
    "只是历史参考，不代表未来",
  ]);
});

test("labels and the same bounds the server checks", () => {
  expect(variantLabel({amount_usdt: 100, interval_hours: 168})).toBe("每 7 天买 100 USDT");
  expect(variantLabel({amount_usdt: 50, interval_hours: 6})).toBe("每 6 小时买 50 USDT");
  expect(check([{amount_usdt: 100, interval_hours: 24}])).toBe("");
  expect(check([{amount_usdt: 5, interval_hours: 24}])).toContain("第 1 组：每次金额");
  expect(check([{amount_usdt: 50, interval_hours: 24}, {amount_usdt: 50, interval_hours: 1.5}])).toContain("第 2 组：间隔");
  expect(check([])).toContain("1–3");
});
