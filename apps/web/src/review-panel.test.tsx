import {fireEvent, render, screen, waitFor} from "@testing-library/react";
import {ReviewPanel} from "./islands/ReviewPanel";
import reviewSource from "./islands/ReviewPanel.tsx?raw";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {status, headers: {"content-type": "application/json"}});
const SCORE = {ai: {total: 2, hit: 1, miss: 1, even: 0, unverifiable: 0, pending: 0}, park: {total: 0}};
const RUNS = {assets: ["BTC", "XAU"], score: SCORE, runs: [
  {id: "paper:s1", asset: "XAU", label: "黄金 Paper", started_at: "2026-11-01T00:00:00Z", ended_at: "2026-11-02T00:00:00Z", fill_count: 4},
]};
const TIMELINE = {run: {environment: "paper", reason: "跌破下沿"}, market: {source: "okx", instrument: "XAU-USDT-SWAP", bars: []},
  result: {fill_count: 4, entries: 2, exits: 2, flat: true, net: -1.2, fees: 0.2, ended_flat: true},
  events: [{ts: "2026-11-01T01:00:00Z", title: "开仓 买入", source: "fills/park-s1.json", record: {price: 100}}], fills: [], judgments: [], missing: ["行情记录"]};

function route(handler: (url: string) => unknown) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async input => json(handler(String(input))));
}

afterEach(() => vi.restoreAllMocks());

test("loads nothing until the tab is opened", async () => {
  const fetch = route(() => RUNS);
  const view = render(<ReviewPanel />);
  await new Promise(r => setTimeout(r, 0));
  expect(fetch).not.toHaveBeenCalled();
  view.rerender(<ReviewPanel active />);
  await waitFor(() => expect(fetch).toHaveBeenCalledWith("/api/review/runs", {cache: "no-store"}));
});

test("shows the newest run, its missing parts and a picked raw record", async () => {
  route(url => url.startsWith("/api/review/runs") ? RUNS : TIMELINE);
  render(<ReviewPanel active />);
  expect(await screen.findByText("−1.20 USDT")).toBeTruthy();
  expect(screen.getByRole("status").textContent).toContain("这段记录缺失：行情记录");
  expect(screen.getByText("行情源 okx · XAU-USDT-SWAP · 历史，只读")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", {name: /开仓 买入/}));
  expect(screen.getByText(/fills\/park-s1.json/).textContent).toContain('"price": 100');
  expect(screen.getByText(/判断战绩/).textContent).toContain("AI：命中 1 · 未中 1");
});

test("a run with an id shows why, from one more GET", async () => {
  const why = {status: "broken", first_problem: {key: "end", label: "结束", summary: "对账拦下"}, identity: {}, steps: [], note: "记录"};
  const fetch = route(url => url.startsWith("/api/why") ? why : url.includes("timeline") ? {...TIMELINE, run: {...TIMELINE.run, id: "paper:s1"}} : RUNS);
  render(<ReviewPanel active />);
  expect(await screen.findByText("断在「结束」：对账拦下")).toBeTruthy();
  expect(fetch).toHaveBeenCalledWith("/api/why?run=paper%3As1", {cache: "no-store"});
  expect(fetch.mock.calls.every(call => !call[1] || !("method" in (call[1] as object)))).toBe(true);
});

test("filters ask the server and an empty result names the filter", async () => {
  const fetch = route(url => url.startsWith("/api/review/runs?") ? {...RUNS, runs: [], score: {ai: {total: 0}, park: {total: 0}}} : url.startsWith("/api/review/runs") ? RUNS : TIMELINE);
  render(<ReviewPanel active />);
  await screen.findByText("−1.20 USDT");
  fireEvent.change(screen.getByLabelText("品种"), {target: {value: "BTC"}});
  fireEvent.change(screen.getByLabelText("时间"), {target: {value: "7"}});
  expect(await screen.findByText("BTC · 近 7 天 没有跑过的记录")).toBeTruthy();
  expect(fetch.mock.calls.map(([url]) => url)).toContain("/api/review/runs?asset=BTC&days=7");
  expect(screen.getByText(/判断战绩（BTC · 近 7 天）/)).toBeTruthy();
});

test("the review panel only reads", () => {
  expect(reviewSource).not.toMatch(/method:|\/api\/execute|dashboard-control|\/api\/advice\/refresh/);
});
