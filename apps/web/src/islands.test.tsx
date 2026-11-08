import {act, fireEvent, render, screen} from "@testing-library/react";
import {createBridge} from "./bridge";
import {READY} from "./fixtures";
import {AiSources, resetAiSourcesMemory} from "./islands/AiSources";
import {BacktestBox, MAX_TRIES, POLL_MS} from "./islands/BacktestBox";
import {WatchPanel} from "./islands/WatchPanel";
import apiSource from "./api.ts?raw";
import backtestSource from "./islands/BacktestBox.tsx?raw";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {status, headers: {"content-type": "application/json"}});

afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); document.body.innerHTML = ""; resetAiSourcesMemory(); });

describe("AiSources", () => {
  const judgment = {id: 41, cited: ["n1"], evidence: {price: {value: 4100, source: "venue", at: "2026-11-05T00:40:00Z"},
    news: [{id: "n1", title: "金价跳水", source: "cls", url: "https://x/1", collected_at: "2026-11-05T00:10:00Z"}], unverified: ["4.2%"]}};

  test("lists inputs, marks the cited one and links the news", () => {
    const {container} = render(<AiSources judgment={judgment} />);
    expect(container.querySelector("summary")?.textContent).toContain("输入里没有：4.2%");
    expect(screen.getByRole("link", {name: /金价跳水/}).getAttribute("rel")).toBe("noopener noreferrer");
    expect(container.querySelector("li.cited .k")?.textContent).toBe("n1");
  });

  test("stays open when the minute refresh remounts it", () => {
    const first = render(<AiSources judgment={judgment} />);
    const details = first.container.querySelector("details")!;
    details.open = true;
    fireEvent(details, new Event("toggle"));
    first.unmount();
    const again = render(<AiSources judgment={judgment} />);
    expect(again.container.querySelector("details")?.open).toBe(true);
  });

  test("an old call says it has no evidence", () => {
    render(<AiSources judgment={{id: 1, cited: [], evidence: {}}} />);
    expect(screen.getByText("这条建议生成时还没有记录依据")).toBeTruthy();
  });
});

describe("BacktestBox", () => {
  test("polls while running, then shows the result", async () => {
    vi.useFakeTimers();
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(json({status: "running"})).mockResolvedValueOnce(json(READY));
    render(<BacktestBox backtestKey="k 1" />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByText(/正在用过去 180 天/)).toBeTruthy();
    await act(async () => { await vi.advanceTimersByTimeAsync(POLL_MS); });
    expect(screen.getByText("这组网格过去 180 天的表现（OKX，费后）")).toBeTruthy();
    expect(screen.getByText("证据：这次回测当时没有记录")).toBeTruthy();  // READY predates #1369
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(fetch.mock.calls[0]?.[0]).toBe("/api/backtest/k%201");
    expect(fetch.mock.calls.every(([, init]) => !init?.method)).toBe(true);
  });

  test("gives up after six minutes and says start is unaffected", async () => {
    vi.useFakeTimers();
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => json({status: "running"}));
    render(<BacktestBox backtestKey="k" />);
    await act(async () => { await vi.advanceTimersByTimeAsync(POLL_MS * MAX_TRIES); });
    expect(screen.getByText("回测不可用：等了 6 分钟还没算完。不影响启动。")).toBeTruthy();
  });

  test("a server error is shown, not retried", async () => {
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(json({}, 500));
    render(<BacktestBox backtestKey="k" />);
    expect(await screen.findByText("回测不可用：HTTP 500。不影响启动。")).toBeTruthy();
    expect(screen.queryByText(/证据/)).toBeNull();  // a failed read is not an old result
    expect(fetch).toHaveBeenCalledTimes(1);
  });
});

describe("WatchPanel", () => {
  test("renders the three cards with sources", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(json({generated_at: "2026-11-05T09:12:00+08:00", provider: "codex", replaced: ["b"],
      items: [{ref: "a", title: "非农", at: "2030-01-01T00:00:00Z", volatility: "高", markets: ["黄金"], why: "押注6.5万", source_title: "NFP", detail: "预期 65K", unverified: []},
              {ref: "b", kind: "breaking", title: "突发", at: "2026-11-05T01:00:00Z", source_title: "快讯"}]}));
    const {container} = render(<WatchPanel />);
    expect(await screen.findByText("09:12 更新 · 按全市场冲击排序")).toBeTruthy();
    const cards = container.querySelectorAll("article.desk-watch-card");
    expect(cards).toHaveLength(2);
    expect(cards[0]?.classList.contains("high")).toBe(true);
    expect(cards[1]?.classList.contains("fresh")).toBe(true);
    expect(cards[0]?.querySelector(".w-src")?.textContent).toBe("来源：NFP · 预期 65K");
    expect(cards[1]?.querySelector(".w-src")).toBeNull();
  });

  test("a read error shows in the header", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(json({detail: "Intel 读不到"}, 502));
    render(<WatchPanel />);
    expect(await screen.findByText("Intel 读不到")).toBeTruthy();
  });
});

describe("bridge", () => {
  test("mounts placeholders as they appear, re-renders on new props, unmounts on removal", async () => {
    const seen: string[] = [];
    function Echo({text}: {text: string}) { seen.push(text); return <b>{text}</b>; }
    const bridge = createBridge({echo: Echo});
    const host = document.createElement("div");
    document.body.append(host);
    const observer = bridge.start(host);
    await act(async () => { host.innerHTML = `<div data-island="echo" data-props='{"text":"一"}'></div>`; await Promise.resolve(); });
    expect(host.textContent).toBe("一");
    const el = host.querySelector<HTMLElement>("[data-island]")!;
    await act(async () => { el.dataset.props = JSON.stringify({text: "二"}); await Promise.resolve(); });
    expect(host.textContent).toBe("二");
    expect(bridge.mounted()).toBe(1);
    await act(async () => { host.innerHTML = ""; await Promise.resolve(); });
    expect(bridge.mounted()).toBe(0);
    observer.disconnect();
  });

  test("bad props and unknown names are skipped, not thrown", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const bridge = createBridge({});
    document.body.innerHTML = `<div data-island="nope"></div><div data-island="x" data-props="{bad"></div>`;
    bridge.start(document.body).disconnect();
    expect(warn).toHaveBeenCalledTimes(2);
    expect(bridge.mounted()).toBe(0);
  });
});

test("islands POST only to the allow-list, and never reach an order or control endpoint", () => {
  // #1335 allowed one POST (backtest/compare); #1327 adds plan, judgments and advice/refresh
  // (decision-log). All of them go through postJson in api.ts; no other file may send a method.
  expect(apiSource.match(/method:/g)).toHaveLength(1);
  expect(apiSource).toMatch(/POST_PATHS = \[COMPARE_PATH, "\/api\/plan", "\/api\/judgments", "\/api\/advice\/refresh"\] as const/);
  const sources = import.meta.glob("./**/*.{ts,tsx}", {query: "?raw", import: "default", eager: true}) as Record<string, string>;
  const code = Object.entries(sources).filter(([path]) => !/\.test\.tsx?$/.test(path));
  expect(code.length).toBeGreaterThan(10);
  for (const [path, source] of code) {
    if (path === "./trade/Skeleton.tsx") {
      // The page's markup (#1415): it names GridMind's classes (dashboard-control-card, ...) but sends nothing.
      expect([path, /\/api\/|fetch\(|XMLHttpRequest|sendBeacon|method:|postJson|api\(/.test(source)]).toEqual([path, false]);
      continue;
    }
    if (path === "./gridmind/read.ts") {
      // GridMind's read layer (#1416) is not an island: gridmind.js loads it as read.js. It reads market bars and
      // carries GridMind's own calls unchanged (ADR 0008), but it never names a method or a control path itself.
      expect(source.match(/\/api\/[\w/-]+/g)).toEqual(["/api/dualtrack/market/bars", "/api/dashboard-control/market-bars", "/api/auth/session"]);
      expect(/method:/.test(source)).toBe(false);
      continue;
    }
    if (path === "./gridmind/control.ts" || path === "./gridmind/control-entry.ts") {
      // GridMind's control layer (#1418) builds and gates control bodies and runs the start/stop/replace flows, but
      // sends nothing: every request goes through the host gridmind.js passes in, whose control() is the one sender.
      expect([path, /\/api\/|dashboard-control|fetch\(|XMLHttpRequest|sendBeacon|method:|postJson|api\(/.test(source)]).toEqual([path, false]);
      continue;
    }
    // No island may reach the control layer, even to build a body.
    if (!path.startsWith("./gridmind/")) expect([path, /gridmind\/control/.test(source)]).toEqual([path, false]);
    // Until #1329: 执行, 停止, 补单 and GridMind's own controls stay in the legacy page.
    expect([path, /\/api\/execute|\/api\/grid\/|dashboard-control|\/api\/dualtrack\/orders|\/api\/park-paper|\/api\/mainnet/.test(source)]).toEqual([path, false]);
    if (!path.endsWith("/api.ts")) expect([path, /method:/.test(source)]).toEqual([path, false]);
  }
  expect(backtestSource).not.toMatch(/<button/);
  expect(["健康", "退役", "推荐", "评分"].some(word => backtestSource.includes(word))).toBe(false);
});
