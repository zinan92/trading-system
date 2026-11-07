import {check, feeNote, notes, rows, variantLabel, type CompareResult} from "./compare";

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
    "按每分钟收盘价买入，手续费沿用黄金的 OKX Paper 假设费率 0.04%；BTC的实际费率未核实",
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

// ---- 网格 (#1336) ----
import {checkGrid, gridLabel, gridNotes, gridRows, DEFAULT_GRID_VARIANTS, GRID_ASSETS, type GridParams} from "./compare";

const GRID: GridParams = {direction: "neutral", below_pct: 3, above_pct: 3, levels: 9, notional_per_level: 500, stop_pct: null};
const gridSummary = (median: number) => ({runs: 25, median_return_pct: median, worst_return_pct: -2.1, best_return_pct: 1.8, stopped_pct: 12.0, mean_fills: 14.2,
  funded_runs: 25, histogram: {low: -2.1, width: 0.3, counts: [1, 2]}});
const GRID_RESULT: CompareResult = {status: "ready", asset: "XAU", strategy: "grid", instrument: "XAU-USDT-SWAP", source: "okx_swap_public", fee_rate: 0.0005,
  history_start: "2026-05-09T08:26:00+00:00", history_end: "2026-11-05T08:25:00+00:00",
  variants: [{params: GRID, summary: gridSummary(0.2), account_equity: 10000}, {params: {...GRID, below_pct: 5, above_pct: 5}, summary: gridSummary(-0.4), account_equity: 10000}]};

test("grid rows use the same rows as the pre-open box and mark only the higher median", () => {
  const table = gridRows(GRID_RESULT);
  expect(table.map(r => r.label)).toEqual(["中位收益", "最差一次", "最好一次", "跌破下沿或止损", "平均成交"]);
  expect(table[0]!.cells).toEqual(["+0.20%", "−0.40%"]);
  expect(table[0]!.best).toBe(0);
  expect(table[3]!.cells).toEqual(["12%", "12%"]);
  expect(table[4]!.cells).toEqual(["14.2 笔", "14.2 笔"]);
});

test("grid notes say the replay, the account size and the fee assumption", () => {
  const lines = gridNotes(GRID_RESULT);
  expect(lines[1]).toBe("用 Paper 自己的 Nautilus 回放和补单规则；收益按 10,000 USDT 账户算，费后");
  expect(lines).toContain("资金费已计入");
  expect(lines.some(line => line.startsWith("手续费"))).toBe(true);
  expect(lines.some((line) => line.startsWith("合约精度"))).toBe(false);  // an older result has no contract
  const btc = gridNotes({...GRID_RESULT, contract: {status: "backtest_only", price_increment: "0.1", size_increment: "0.0001"}});
  expect(btc).toContain("合约精度取自 OKX 的合约说明：价格 0.1，数量 0.0001；只用于回测");
});

test("grid labels and bounds match the server's", () => {
  expect(gridLabel(GRID)).toBe("双向 −3%/+3% · 9 格 × 500 USDT");
  expect(gridLabel({...GRID, direction: "long", stop_pct: 2})).toBe("做多 −3%/+3% · 9 格 × 500 USDT · 止损 2%");
  expect(checkGrid(DEFAULT_GRID_VARIANTS)).toBe("");
  expect(checkGrid([{...GRID, below_pct: 0.1}])).toMatch(/上下沿/);
  expect(checkGrid([{...GRID, levels: 31}])).toMatch(/格数/);
  expect(checkGrid([{...GRID, levels: 2.5}])).toMatch(/格数/);
  expect(checkGrid([{...GRID, notional_per_level: 5}])).toMatch(/每格金额/);
  expect(checkGrid([{...GRID, direction: "long", stop_pct: 3}])).toMatch(/止损/);
  expect(checkGrid([{...GRID, stop_pct: 1}])).toMatch(/双向/);
  expect(checkGrid([GRID, GRID, GRID, GRID])).toMatch(/1–3/);
  expect(GRID_ASSETS).toEqual(["XAU", "BTC", "SILVER", "WTI"]);
});

test("the fee is gold's assumption, and for any other asset it says so and stays unverified (#1388)", () => {
  expect(feeNote({status: "ready", asset: "XAU", fee_rate: 0.0005})).toBe("手续费按 OKX Paper 假设费率 0.05%，尚未用账户核实");
  expect(feeNote({status: "ready", asset: "SILVER", fee_rate: 0.0005})).toBe("手续费沿用黄金的 OKX Paper 假设费率 0.05%；白银的实际费率未核实");
  expect(feeNote({status: "ready", asset: "WTI", fee_rate: 0.0005})).toContain("原油的实际费率未核实");
});
