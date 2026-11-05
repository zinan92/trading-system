import {view} from "./backtest";
import {READY} from "./fixtures";

test("ready shows the numbers and what is assumed", () => {
  const v = view(READY);
  if (v.state !== "ready") throw new Error("expected ready");
  expect(v.title).toBe("这组网格过去 180 天的表现（OKX，费后）");
  expect(v.sub).toBe("跑了 25 段，每段 7 天，互不重叠");
  expect(v.cells).toEqual([["中位收益", "+0.25%"], ["最差一次", "−2.05%"], ["跌破下沿或止损", "40%"], ["平均成交", "30.1 笔"]]);
  expect(v.notes[0]).toContain("假设费率");
  expect(v.notes[1]).toBe("资金费只计入了 12 / 25 段（OKX 只给最近约 94 天）");
});

test("unavailable names the reason and says start is unaffected", () => {
  expect(view({status: "unavailable", reason: "历史数据有缺口：3 分钟"})).toEqual({state: "unavailable", text: "回测不可用：历史数据有缺口：3 分钟。不影响启动。"});
});

test("no result yet is running", () => {
  expect(view(null).state).toBe("running");
  expect(view({status: "running"}).state).toBe("running");
});
