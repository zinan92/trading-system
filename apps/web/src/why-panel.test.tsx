import {act, render, screen} from "@testing-library/react";
import {WhyPanel} from "./islands/WhyPanel";
import {identityRows, verdict, type WhyChain} from "./why";

const chain = (over: Partial<WhyChain> = {}): WhyChain => ({
  status: "clean", first_problem: null, note: "这些是记录，不是交易所动作的证明。", identity: {plan_digest: "sha256:abcdef0123456789", release_sha: null},
  steps: [{key: "confirm", label: "确认", status: "ok", summary: "park 已确认", at: "2026-09-14T01:13:46+00:00", source: "confirmations.json", facts: [{label: "品种", value: "BTC"}]},
          {key: "end", label: "结束", status: "not_reached", summary: "这一轮还没结束"}], ...over});

function mock(body: unknown, status = 200) {
  const fetchMock = vi.fn(async () => new Response(JSON.stringify(body), {status}));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => vi.unstubAllGlobals());

test("the verdict names the first break", () => {
  expect(verdict(chain({status: "broken", first_problem: {key: "preview", label: "预览", summary: "预览没通过"}}))).toBe("断在「预览」：预览没通过");
  expect(verdict(chain())).toBe("每一步都有记录，没有断点");
  expect(verdict(chain({status: "attention"}))).toBe("链路是通的，有几处要留意");
});

test("an unrecorded commit is said to be unrecorded", () => {
  expect(identityRows({plan_digest: "sha256:abcdef0123456789"})).toEqual([{label: "计划指纹", value: "abcdef012345"}, {label: "代码版本", value: "当时没有记录"}]);
  expect(identityRows({release_sha: "f80fa54b1e03a5d5", market_source: "okx"})).toEqual([{label: "计划指纹", value: "没有记录"}, {label: "代码版本", value: "f80fa54b1e03"}, {label: "行情来源", value: "okx"}]);
});

test("reads one GET and shows every step, its source and the record note", async () => {
  const fetchMock = mock(chain());
  await act(async () => { render(<WhyPanel run="testnet:dashboard-plan:abc" />); });
  expect(fetchMock).toHaveBeenCalledTimes(1);
  expect(fetchMock.mock.calls[0]?.[0 as never]).toBe("/api/why?run=testnet%3Adashboard-plan%3Aabc");
  expect(screen.getByRole("status").textContent).toBe("每一步都有记录，没有断点");
  expect(screen.getByText("park 已确认")).toBeTruthy();
  expect(screen.getByText("没走到")).toBeTruthy();
  expect(screen.getByText("confirmations.json")).toBeTruthy();
  expect(screen.getByText("这些是记录，不是交易所动作的证明。")).toBeTruthy();
});

test("a failed read says so instead of showing a clean chain", async () => {
  mock({detail: "没有这一轮的记录"}, 404);
  await act(async () => { render(<WhyPanel run="paper:x" />); });
  expect(screen.getByRole("status").textContent).toBe("链路读不出来：没有这一轮的记录");
});
