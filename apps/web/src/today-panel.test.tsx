import {act, fireEvent, render, screen} from "@testing-library/react";
import {NewsRail} from "./islands/NewsRail";
import {ADVICE_POLL_MS, AssetNote, REFRESH_MS, TodayPanel, basisLine} from "./islands/TodayPanel";
import {execRestore, judgmentBody, planBody} from "./today";
import {citeStore} from "./todayStore";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {status, headers: {"content-type": "application/json"}});
const ASSETS = [{key: "XAU", label: "黄金", kind: "xau_paper"}, {key: "ETH", label: "ETH", kind: "watch"}];
const PLAN_NEW = {kind: "new", title: "做多网格", lines: [["区间", "4,000 – 4,200"]], note: "先给你看", rungs: [4000, 4100]};
const AI = {id: 7, direction: "short", confidence: 4, reason: "美元走强", created_at: "2026-11-06T00:41:00Z", cited: [], evidence: null};

type Call = {url: string; method: string; body: string | undefined};
let calls: Call[] = [];
let events: Array<{name: string; detail: any}> = [];
const listen = (name: string) => { const fn = (e: Event) => events.push({name, detail: (e as CustomEvent).detail}); window.addEventListener(name, fn); return () => window.removeEventListener(name, fn); };
let unlisten: Array<() => void> = [];

function serve(routes: Record<string, (body?: string) => unknown>) {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input), method = init?.method ?? "GET", body = init?.body as string | undefined;
    calls.push({url, method, body});
    const key = `${method} ${url}`;
    const handler = routes[key] ?? routes[`${method} ${url.split("?")[0]}`];
    if (!handler) return json({detail: `no route ${key}`}, 500);
    const out = handler(body);
    return out instanceof Response ? out : json(out);
  });
}

beforeEach(() => {
  calls = []; events = []; citeStore.reset();
  unlisten = ["desk:desk", "desk:exec", "desk:steps"].map(listen);
});
afterEach(() => { unlisten.forEach(fn => fn()); vi.restoreAllMocks(); vi.useRealTimers(); document.body.innerHTML = ""; });

test("request bodies are byte for byte the legacy ones", () => {
  // gm-desk.js: JSON.stringify({asset: D.asset, direction: D.d}) and
  // JSON.stringify({asset, direction, confidence: +conf, reason, cited: D.cites, action})
  expect(planBody("XAU", "long")).toBe('{"asset":"XAU","direction":"long"}');
  expect(judgmentBody("XAU", "short", "4" as unknown as number, "美元", [{id: "n1", title: "金价", bucket: "watch"}], "approved"))
    .toBe('{"asset":"XAU","direction":"short","confidence":4,"reason":"美元","cited":[{"id":"n1","title":"金价","bucket":"watch"}],"action":"approved"}');
});

test("pick → plan → approve sends the legacy bodies and hands the preview to step 3", async () => {
  const preview = {execution_ready: true, orders: [{price: 4000}], max_loss: 12.5};
  serve({
    "GET /api/desk/XAU": () => ({kline: {ok: true, structure: "日线偏多", synthesis: "金价修复。后面再看"}, today_judgment: null, executions: [], ai_today: null, meta: {kind: "xau_paper"}}),
    "POST /api/plan": () => PLAN_NEW,
    "POST /api/judgments": () => ({id: 99, handoff: "预览通过", preview}),
  });
  render(<TodayPanel asset="XAU" assets={ASSETS} gridmind={["XAU"]} />);
  expect(await screen.findByText("K 线日报 ▲ 偏多")).toBeTruthy();
  citeStore.toggle("XAU", {id: "n1", title: "金价跳水", bucket: "watch"});
  fireEvent.click(screen.getByRole("button", {name: "做多"}));
  fireEvent.change(screen.getByLabelText("一句理由"), {target: {value: "ETF 流入"}});
  fireEvent.change(screen.getByLabelText("信心"), {target: {value: "5"}});
  fireEvent.click(await screen.findByRole("button", {name: "批准这份计划"}));
  expect(await screen.findByText("预览通过")).toBeTruthy();
  const posts = calls.filter(c => c.method === "POST");
  expect(posts.map(c => [c.url, c.body])).toEqual([
    ["/api/plan", '{"asset":"XAU","direction":"long"}'],
    ["/api/judgments", '{"asset":"XAU","direction":"long","confidence":5,"reason":"ETF 流入","cited":[{"id":"n1","title":"金价跳水","bucket":"watch"}],"action":"approved"}'],
  ]);
  const exec = events.filter(e => e.name === "desk:exec");
  expect(exec[0]!.detail).toEqual({asset: "XAU", clear: true});
  expect(exec.at(-1)!.detail).toEqual({asset: "XAU", judgmentId: 99, preview, done: null});
  expect(events.filter(e => e.name === "desk:steps").at(-1)!.detail).toEqual({asset: "XAU", direction: true, planKind: "new", handed: true});
  expect(events.find(e => e.name === "desk:desk")!.detail.desk.meta.kind).toBe("xau_paper");
});

test("a reload restores today's approved judgment, and an already executed plan stays done", async () => {
  const desk = {
    kline: {ok: false, reason: "还没出"},
    today_judgment: {id: 5, direction: "long", reason: "早上判断", confidence: 4, cited: [{id: "a", title: "旧新闻", bucket: "high_impact"}], action: "approved"},
    executions: [
      {judgment_id: 5, stage: "preview", detail: {execution_ready: true, orders: [], max_loss: 3}},
      {judgment_id: 5, stage: "executed", detail: {}},
      {judgment_id: 4, stage: "refused", detail: {}},
    ],
    ai_today: null, meta: {kind: "xau_paper"},
  };
  expect(execRestore(desk as never)!.done!.stage).toBe("executed");
  serve({"GET /api/desk/XAU": () => desk, "POST /api/plan": () => PLAN_NEW});
  render(<TodayPanel asset="XAU" assets={ASSETS} gridmind={["XAU"]} />);
  expect(await screen.findByDisplayValue("早上判断")).toBeTruthy();
  await screen.findByText("做多网格");
  expect(screen.getByRole("button", {name: "做多"}).getAttribute("aria-pressed")).toBe("true");
  expect(screen.getByText("旧新闻…")).toBeTruthy();
  const handed = events.filter(e => e.name === "desk:exec").at(-1)!.detail;
  expect(handed).toEqual({asset: "XAU", judgmentId: 5, preview: {execution_ready: true, orders: [], max_loss: 3}, done: {judgment_id: 5, stage: "executed", detail: {}}});
  // Restoring re-plans (a POST that saves nothing) but never re-submits the judgment.
  expect(calls.filter(c => c.method === "POST").map(c => c.url)).toEqual(["/api/plan"]);
});

test("the minute refresh never overwrites a direction or reason Park is working on", async () => {
  vi.useFakeTimers({shouldAdvanceTime: true});
  let judgment: unknown = null;
  serve({"GET /api/desk/XAU": () => ({kline: null, today_judgment: judgment, executions: [], ai_today: null}), "POST /api/plan": () => PLAN_NEW});
  render(<TodayPanel asset="XAU" assets={ASSETS} gridmind={["XAU"]} />);
  await screen.findByText("K 线日报：暂无");
  fireEvent.click(screen.getByRole("button", {name: "做空"}));
  fireEvent.change(screen.getByLabelText("一句理由"), {target: {value: "我自己在写"}});
  judgment = {id: 1, direction: "long", reason: "别处记的", confidence: 2, cited: [], action: "recorded"};
  await act(async () => { await vi.advanceTimersByTimeAsync(REFRESH_MS); });
  expect(calls.filter(c => c.url === "/api/desk/XAU")).toHaveLength(2);
  expect((screen.getByLabelText("一句理由") as HTMLTextAreaElement).value).toBe("我自己在写");
  expect(screen.getByRole("button", {name: "做空"}).getAttribute("aria-pressed")).toBe("true");
});

test("照 AI 的方向判断 shows only before a direction is picked; refresh asks once and polls", async () => {
  vi.useFakeTimers({shouldAdvanceTime: true});
  let running = true;
  serve({
    "GET /api/desk/XAU": () => ({kline: null, today_judgment: null, executions: [], ai_today: AI}),
    "POST /api/plan": () => PLAN_NEW,
    "POST /api/advice/refresh": () => ({started: true}),
    "GET /api/advice": () => ({running}),
  });
  render(<TodayPanel asset="XAU" assets={ASSETS} gridmind={["XAU"]} />);
  fireEvent.click(await screen.findByRole("button", {name: "照 AI 的方向判断"}));
  expect(screen.getByRole("button", {name: "做空"}).getAttribute("aria-pressed")).toBe("true");
  expect(screen.queryByRole("button", {name: "照 AI 的方向判断"})).toBeNull();
  fireEvent.click(screen.getByRole("button", {name: "刷新 AI 建议"}));
  expect(await screen.findByRole("button", {name: "AI 正在看…"})).toBeTruthy();
  expect(calls.filter(c => c.url === "/api/advice/refresh").map(c => c.body)).toEqual(["{}"]);
  running = false;
  await act(async () => { await vi.advanceTimersByTimeAsync(ADVICE_POLL_MS); });
  expect(calls.filter(c => c.url === "/api/desk/XAU").length).toBeGreaterThanOrEqual(2);
});

test("the asset is chosen in the top bar only; a plan that cannot be approved stays disabled", async () => {
  serve({"GET /api/desk/XAU": () => ({kline: null, executions: []}), "POST /api/plan": () => ({kind: "unavailable", title: "读不到现价", lines: [], note: "稍后"})});
  render(<TodayPanel asset="XAU" assets={ASSETS} gridmind={["XAU"]} />);
  // #1433: no asset buttons in the panel, and no note for an asset GridMind charts.
  expect(screen.queryByRole("group", {name: "判断哪个品种"})).toBeNull();
  expect(screen.queryByRole("button", {name: "ETH"})).toBeNull();
  expect(document.getElementById("deskAssetNote")).toBeNull();
  fireEvent.click(screen.getByRole("button", {name: "观望"}));
  expect((await screen.findByRole("button", {name: "确认并记录"}) as HTMLButtonElement).disabled).toBe(true);
  expect(screen.getByText("暂不可用")).toBeTruthy();
});

test("a new asset starts a fresh panel", async () => {
  serve({"GET /api/desk/XAU": () => ({executions: []}), "GET /api/desk/ETH": () => ({executions: []}), "POST /api/plan": () => PLAN_NEW});
  const view = render(<TodayPanel asset="XAU" assets={ASSETS} gridmind={["XAU"]} />);
  fireEvent.click(screen.getByRole("button", {name: "做多"}));
  fireEvent.change(screen.getByLabelText("一句理由"), {target: {value: "黄金的理由"}});
  view.rerender(<TodayPanel asset="ETH" assets={ASSETS} gridmind={["XAU"]} />);
  expect((screen.getByLabelText("一句理由") as HTMLTextAreaElement).value).toBe("");
  expect(screen.getByRole("button", {name: "做多"}).getAttribute("aria-pressed")).toBe("false");
});

describe("NewsRail", () => {
  const items = [
    {id: "n1", title: "金价跳水", bucket: "high_impact", source: "cls", collected_at: new Date().toISOString()},
    {id: "n2", title: "无关小事", bucket: "noise", source: "x", collected_at: new Date().toISOString()},
  ];

  test("filters, cites into today's judgment, and marks new items on the next read", async () => {
    vi.useFakeTimers({shouldAdvanceTime: true});
    let list = items;
    serve({"GET /api/news/XAU": () => ({ok: true, items: list})});
    citeStore.forAsset("XAU");
    render(<NewsRail asset="XAU" />);
    expect(await screen.findByText("金价跳水")).toBeTruthy();
    expect(screen.queryByText("无关小事")).toBeNull();
    fireEvent.click(screen.getByRole("button", {name: "全部"}));
    expect(screen.getByText("无关小事")).toBeTruthy();
    fireEvent.click(screen.getByText("金价跳水").closest("li")!);
    expect(citeStore.get().cites).toEqual([{id: "n1", title: "金价跳水", bucket: "high_impact"}]);
    expect(screen.getByText("已引用")).toBeTruthy();
    list = [{id: "n3", title: "美联储讲话", bucket: "watch", source: "cls", collected_at: new Date().toISOString()}, ...items];
    await act(async () => { await vi.advanceTimersByTimeAsync(60000); });
    expect(screen.getByText("美联储讲话").parentElement!.querySelector(".new")?.textContent).toBe("新");
    expect(calls.every(c => c.method === "GET")).toBe(true);
  });

  test("says why when the news cannot be read", async () => {
    serve({"GET /api/news/XAU": () => ({ok: false, reason: "新闻服务没开", items: []})});
    render(<NewsRail asset="XAU" />);
    expect(await screen.findByText("新闻服务没开")).toBeTruthy();
  });
});

// #1370: the adopted AI call's id goes with the judgment, last and only when kept.
test("the advice id goes last, and only when there is one", () => {
  expect(judgmentBody("XAU", "short", 3, "", [], "approved", 7))
    .toBe('{"asset":"XAU","direction":"short","confidence":3,"reason":"","cited":[],"action":"approved","advice_id":7}');
  expect(judgmentBody("XAU", "short", 3, "", [], "approved", null)).toBe(judgmentBody("XAU", "short", 3, "", [], "approved"));
});

const deskWithAi = () => ({kline: {ok: true, structure: "日线偏空", synthesis: "x"}, today_judgment: null, executions: [], ai_today: AI, meta: {kind: "xau_paper"}});

test("adopting the AI call sends its id with the judgment", async () => {
  serve({"GET /api/desk/XAU": deskWithAi, "POST /api/plan": () => PLAN_NEW, "POST /api/judgments": () => ({id: 99, handoff: "已记录"})});
  render(<TodayPanel asset="XAU" assets={ASSETS} gridmind={["XAU"]} />);
  fireEvent.click(await screen.findByRole("button", {name: "照 AI 的方向判断"}));
  expect(await screen.findByText("这次判断照的是这条 AI 建议")).toBeTruthy();
  fireEvent.click(await screen.findByRole("button", {name: "批准这份计划"}));
  await screen.findByText("已记录");
  const body = JSON.parse(calls.find(c => c.url === "/api/judgments")!.body!);
  expect(body.direction).toBe("short");
  expect(body.advice_id).toBe(7);
});

test("picking another direction after adopting drops the id", async () => {
  serve({"GET /api/desk/XAU": deskWithAi, "POST /api/plan": () => PLAN_NEW, "POST /api/judgments": () => ({id: 99, handoff: "已记录"})});
  render(<TodayPanel asset="XAU" assets={ASSETS} gridmind={["XAU"]} />);
  fireEvent.click(await screen.findByRole("button", {name: "照 AI 的方向判断"}));
  fireEvent.click(screen.getByRole("button", {name: "做多"}));
  expect(screen.queryByText("这次判断照的是这条 AI 建议")).toBeNull();
  fireEvent.click(await screen.findByRole("button", {name: "批准这份计划"}));
  await screen.findByText("已记录");
  expect(calls.find(c => c.url === "/api/judgments")!.body).not.toContain("advice_id");
});

test("a reload keeps the adopted id of today's judgment", async () => {
  const desk = {...deskWithAi(), today_judgment: {id: 5, direction: "short", reason: "", confidence: 3, cited: [], action: "recorded",
    advice: {id: 7, direction: "short", created_at: AI.created_at}}};
  serve({"GET /api/desk/XAU": () => desk, "POST /api/plan": () => PLAN_NEW});
  render(<TodayPanel asset="XAU" assets={ASSETS} gridmind={["XAU"]} />);
  expect(await screen.findByText("这次判断照的是这条 AI 建议")).toBeTruthy();
});

// #1380: a chip's hint follows the venue's declared capabilities.
test("the asset note reads the capabilities, not the kind", () => {
  const assets = [{key: "NEW", label: "新", kind: "anything", capabilities: ["grid_plan"]}, {key: "OLD", label: "旧", kind: "hl_testnet", capabilities: []}];
  const {rerender} = render(<AssetNote asset="NEW" assets={assets} gridmind={["XAU"]} />);
  expect(document.getElementById("deskAssetNote")!.textContent).toContain("的判断和计划在这里");
  rerender(<AssetNote asset="OLD" assets={assets} gridmind={["XAU"]} />);
  expect(document.getElementById("deskAssetNote")!.textContent).toContain("只记判断和复盘");
});

test("an OKX-priced asset says the K-line daily reads futures (#1387)", async () => {
  const note = "K 线日报看的是期货合约；这里的价格和 K 线是 OKX 永续，两者有价差。";
  serve({"GET /api/desk/XAU": () => ({kline: {ok: true, structure: "日线偏多", synthesis: "银价修复。"}, today_judgment: null, executions: [], ai_today: null,
    meta: {kind: "okx_public", reading_note: note}})});
  render(<TodayPanel asset="XAU" assets={ASSETS} gridmind={[]} />);
  expect(await screen.findByText(note)).toBeTruthy();
});

test("no note when the reading and the price share a feed", async () => {
  serve({"GET /api/desk/XAU": () => ({kline: {ok: true, structure: "日线偏多", synthesis: "x"}, today_judgment: null, executions: [], ai_today: null, meta: {kind: "xau_paper", reading_note: ""}})});
  render(<TodayPanel asset="XAU" assets={ASSETS} gridmind={["XAU"]} />);
  await screen.findByText("K 线日报 ▲ 偏多");
  expect(document.querySelector(".desk-reading-note")).toBeNull();
});

test("an asset GridMind does not chart says where its chart is, by what its venue can do", () => {
  const assets = [{key: "ETH", label: "ETH", capabilities: ["grid_plan", "execute"]}, {key: "SOL", label: "SOL", capabilities: []}];
  const {rerender} = render(<AssetNote asset="ETH" assets={assets} gridmind={["XAU"]} />);
  expect(document.getElementById("deskAssetNote")!.textContent).toBe("ETH的判断和计划在这里；图表和下方表格仍是 GridMind 上的品种。");
  rerender(<AssetNote asset="SOL" assets={assets} gridmind={["XAU"]} />);
  expect(document.getElementById("deskAssetNote")!.textContent).toBe("SOL只记判断和复盘。图表和下方表格仍是 GridMind 上的品种。");
  rerender(<AssetNote asset="XAU" assets={assets} gridmind={["XAU"]} />);
  expect(document.getElementById("deskAssetNote")).toBeNull();
});

// #1435: the week's three events and the news are one folded line in step 1.
describe("今天的依据", () => {
  const now = () => new Date().toISOString();
  const WATCH = {generated_at: "2026-11-08T09:12:00+08:00", provider: "codex", items: [
    {ref: "a", title: "非农", at: "2030-01-01T00:00:00Z"}, {ref: "b", title: "CPI", at: "2030-01-02T00:00:00Z"},
    {ref: "c", kind: "breaking", title: "霍尔木兹海峡封锁", at: "2026-11-08T01:00:00Z"}]};
  const desk = () => ({kline: {ok: true, structure: "日线偏空", synthesis: "x"}, today_judgment: null, executions: [], meta: {kind: "xau_paper"}});

  test("the line counts the events and the 重要+关注 news, and says a breaking event", () => {
    const news = {items: [{title: "a", bucket: "high_impact"}, {title: "b", bucket: "watch"}, {title: "c", bucket: "noise"}], error: "", stale: false, at: new Date(), fresh: new Set<string | number>()};
    expect(basisLine({data: WATCH, error: "", now: 0}, news)).toEqual({text: "今天的依据：3 件大事 · 2 条新闻", alert: "突发：霍尔木兹海峡封锁"});
    expect(basisLine({data: null, error: "", now: 0}, {...news, at: null}).text).toBe("今天的依据：三件事读取中 · 新闻读取中");
    expect(basisLine({data: null, error: "Intel 读不到", now: 0}, {...news, error: "新闻服务没开"}).text).toBe("今天的依据：三件事读不到 · 新闻读不到");
    expect(basisLine({data: {items: []}, error: "", now: 0}, {...news, stale: true}).text).toBe("今天的依据：0 件大事 · 2 条新闻（上次读到的）");
    expect(basisLine({data: {items: []}, error: "", now: 0}, {...news, fresh: new Set(["a", "b"])}).alert).toBe("1 条新的重要新闻");
  });

  test("folded, it keeps reading; opened, a click cites the news in today's judgment", async () => {
    vi.useFakeTimers({shouldAdvanceTime: true});
    let list = [{id: "n1", title: "金价跳水", bucket: "high_impact", source: "cls", collected_at: now()},
                {id: "n2", title: "无关小事", bucket: "noise", source: "x", collected_at: now()}];
    serve({"GET /api/desk/XAU": desk, "GET /api/watch": () => WATCH, "GET /api/news/XAU": () => ({ok: true, items: list})});
    const {container} = render(<TodayPanel asset="XAU" assets={ASSETS} gridmind={["XAU"]} />);
    expect(await screen.findByText("今天的依据：3 件大事 · 1 条新闻")).toBeTruthy();
    expect(container.querySelector("#deskBasis")?.hasAttribute("open")).toBe(false);
    expect(container.querySelector("#deskBasisAlert")?.textContent).toBe("突发：霍尔木兹海峡封锁");
    list = [{id: "n3", title: "美联储加息", bucket: "high_impact", source: "cls", collected_at: now()}, ...list];
    await act(async () => { await vi.advanceTimersByTimeAsync(60000); });
    expect(screen.getByText("今天的依据：3 件大事 · 2 条新闻")).toBeTruthy();
    expect(container.querySelector("#deskBasisAlert")?.textContent).toBe("突发：霍尔木兹海峡封锁 · 1 条新的重要新闻");
    fireEvent.click(screen.getByText("今天的依据：3 件大事 · 2 条新闻"));
    expect(container.querySelector("#deskBasis")?.hasAttribute("open")).toBe(true);
    expect(container.querySelectorAll("#deskBasis article.desk-watch-card")).toHaveLength(3);
    fireEvent.click(screen.getByText("金价跳水").closest("li")!);
    expect(container.querySelector("#deskCites")?.textContent).toContain("金价跳水");
    expect(calls.filter(c => c.url === "/api/watch")).toHaveLength(1);
    expect(calls.every(c => c.method === "GET")).toBe(true);
  });

  test("an asset list that cannot be read says why", () => {
    render(<TodayPanel error="请求失败，请稍后再试" />);
    expect(screen.getByText("请求失败，请稍后再试").id).toBe("deskAssetsError");
  });
});
