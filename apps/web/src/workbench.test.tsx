import {act, fireEvent, render, screen} from "@testing-library/react";
import {Workbench, POLL_MS} from "./islands/Workbench";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {status, headers: {"content-type": "application/json"}});
const summary = {runs: 25, median_return_pct: 0.5, worst_return_pct: -3.2, best_return_pct: 4.1, median_drawdown_pct: -1.5, worst_drawdown_pct: -6.25,
  mean_buys: 7, invested_per_run: 700, funded_runs: 25, histogram: {low: -3.2, width: 0.6, counts: [1, 2, 3]}};

afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); });

test("starts one comparison, follows it with GETs and shows the table", async () => {
  vi.useFakeTimers();
  const calls: Array<[string, RequestInit | undefined]> = [];
  let reads = 0;
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    calls.push([String(input), init]);
    if (init?.method === "POST") return json({key: "abc"});
    return json(++reads < 2 ? {status: "running"} : {status: "ready", asset: "BTC", instrument: "BTC-USDT-SWAP", source: "okx_swap_crypto_public", fee_rate: 0.0004,
      variants: [{params: {amount_usdt: 100, interval_hours: 24}, summary}, {params: {amount_usdt: 100, interval_hours: 168}, summary: {...summary, median_return_pct: 0.9}}]});
  });
  render(<Workbench />);
  fireEvent.change(screen.getByLabelText("品种"), {target: {value: "BTC"}});
  await act(async () => { fireEvent.click(screen.getByRole("button", {name: "开始回测"})); await vi.advanceTimersByTimeAsync(0); });
  expect(JSON.parse(String(calls[0]?.[1]?.body))).toEqual({asset: "BTC", strategy: "dca", variants: [{amount_usdt: 100, interval_hours: 24}, {amount_usdt: 25, interval_hours: 6}]});
  expect(screen.getByRole("button", {name: "回测中…"})).toBeTruthy();
  await act(async () => { await vi.advanceTimersByTimeAsync(POLL_MS); });
  expect(screen.getByText("每 7 天买 100 USDT")).toBeTruthy();
  expect(document.querySelector("td.best")?.textContent).toBe("+0.90%");
  expect(calls.slice(1).every(([url, init]) => url === "/api/backtest/compare/abc" && !init?.method)).toBe(true);
});

test("a busy machine and a bad field are said, and a bad field blocks the button", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(json({detail: "已有一个回测在跑，等它跑完再开始"}, 409));
  render(<Workbench />);
  fireEvent.click(screen.getByRole("button", {name: "开始回测"}));
  expect(await screen.findByText("已有一个回测在跑，等它跑完再开始")).toBeTruthy();
  fireEvent.change(screen.getAllByLabelText("间隔（小时）")[0]!, {target: {value: "0"}});
  expect(screen.getByText(/第 1 组：间隔/)).toBeTruthy();
  expect((screen.getByRole("button", {name: "开始回测"}) as HTMLButtonElement).disabled).toBe(true);
});

test("an unavailable result names the reason", async () => {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (_input, init) => init?.method === "POST" ? json({key: "k"}) : json({status: "unavailable", reason: "历史数据有缺口：3 分钟"}));
  render(<Workbench />);
  fireEvent.click(screen.getByRole("button", {name: "开始回测"}));
  expect(await screen.findByText("回测不可用：历史数据有缺口：3 分钟")).toBeTruthy();
});

test("a gold grid comparison sends grid sets and shows grid rows; BTC cannot pick grid yet", async () => {
  vi.useFakeTimers();
  const calls: Array<[string, RequestInit | undefined]> = [];
  const grid = {runs: 25, median_return_pct: 0.3, worst_return_pct: -2, best_return_pct: 1.5, stopped_pct: 8, mean_fills: 11.5, funded_runs: 25, histogram: {low: -2, width: 0.3, counts: [1]}};
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    calls.push([String(input), init]);
    if (init?.method === "POST") return json({key: "g"});
    return json({status: "ready", asset: "XAU", strategy: "grid", instrument: "XAU-USDT-SWAP", source: "okx_swap_public",
      variants: [{params: JSON.parse(String(calls[0]?.[1]?.body)).variants[0], summary: grid, account_equity: 10000}]});
  });
  render(<Workbench />);
  fireEvent.change(screen.getByLabelText("策略"), {target: {value: "grid"}});
  fireEvent.click(screen.getAllByRole("button", {name: "去掉这组"})[1]!);
  fireEvent.change(screen.getByLabelText("格数"), {target: {value: "6"}});
  fireEvent.change(screen.getByLabelText("方向"), {target: {value: "long"}});
  fireEvent.change(screen.getByLabelText("止损（离现价 %，可空）"), {target: {value: "2"}});
  await act(async () => { fireEvent.click(screen.getByRole("button", {name: "开始回测"})); await vi.advanceTimersByTimeAsync(0); });
  expect(JSON.parse(String(calls[0]?.[1]?.body))).toEqual({asset: "XAU", strategy: "grid",
    variants: [{direction: "long", below_pct: 3, above_pct: 3, levels: 6, notional_per_level: 500, stop_pct: 2}]});
  expect(screen.getByText("做多 −3%/+3% · 6 格 × 500 USDT · 止损 2%")).toBeTruthy();
  expect(screen.getByText("8%")).toBeTruthy();
  expect(screen.getByText("11.5 笔")).toBeTruthy();
  vi.useRealTimers();

  fireEvent.change(screen.getByLabelText("品种"), {target: {value: "BTC"}});
  expect(screen.getByText(/BTC 网格回测还没开通/)).toBeTruthy();
  expect((screen.getByRole("button", {name: "开始回测"}) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByRole("option", {name: "网格（BTC 即将开通）"}) as HTMLOptionElement).disabled).toBe(true);
});
