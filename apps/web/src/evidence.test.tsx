import {render, screen} from "@testing-library/react";
import {evidenceView, type Evidence} from "./evidence";
import {EvidenceLine} from "./islands/EvidenceLine";

const pack = (overrides: Partial<Evidence> = {}): Evidence => ({
  schema_version: "backtest-evidence-v1",
  code: {commit: "0123456789abcdef0123456789abcdef01234567", dirty: false, reason: null},
  data: {source: "okx_swap_public", instrument: "XAU-USDT-SWAP", bars: 259200, expected_bars: 259200, first_bar: "2026-05-10T00:40:00+00:00",
    last_bar: "2026-11-06T00:39:00+00:00", bars_sha256: "sha256:3f9ad01c77aa", funding_rows: 284, funding_sha256: "sha256:beef"},
  inputs: {contract_sha256: "sha256:c0ffee"},
  params: {digest: "sha256:p4r4m5"},
  checks: [
    {name: "history_before_run", label: "数据在回测开始前就已收盘", status: "ok", detail: "…"},
    {name: "fills_inside_window", label: "每笔成交都在自己那段之内", status: "ok", detail: "26 段全部符合"},
  ],
  tested: [{label: "把后面的行情换掉，前面的结果不变", test: "tests/test_backtest_evidence.py"}],
  verdict: "ok",
  notes: ["参数是在这段行情已经看得到之后定的"],
  ...overrides,
});

test("a clean run reads as commit, data fingerprint and a passed check", () => {
  const v = evidenceView(pack())!;
  expect(v.summary).toBe("代码 0123456 · 数据 3f9ad01c（259,200 根）");
  expect(v.verdict).toEqual({state: "ok", text: "未来数据检查全部通过（2 项）"});
  expect(v.rows.map(([k]) => k)).toEqual(["代码提交", "数据", "行情指纹", "资金费指纹", "回测合约指纹", "参数指纹"]);
  expect(v.tested).toEqual(["把后面的行情换掉，前面的结果不变（单元测试覆盖，本次未跑）"]);
});

test("a dirty checkout and an unknown commit are said, not hidden", () => {
  expect(evidenceView(pack({code: {commit: "a".repeat(40), dirty: true}}))!.summary).toContain("aaaaaaa（有未提交改动）");
  expect(evidenceView(pack({code: {commit: null, dirty: null, reason: "git 不可用：FileNotFoundError"}}))!.summary)
    .toContain("代码 未核实（git 不可用：FileNotFoundError）");
});

test("a failed check is named; an unverified one is named too", () => {
  const failed = pack({verdict: "failed", checks: [{name: "x", label: "资金费只算在已经有的仓位上", status: "failed", detail: "1 / 26 段不符合"}]});
  expect(evidenceView(failed)!.verdict).toEqual({state: "failed", text: "未来数据检查没通过：资金费只算在已经有的仓位上"});
  const open = pack({verdict: "未核实", checks: [{name: "y", label: "每笔成交都在自己那段之内", status: "未核实", detail: "3 / 26 段没有记录所需时间"}]});
  expect(evidenceView(open)!.verdict.text).toBe("未来数据检查未核实：每笔成交都在自己那段之内");
  const noData = pack({verdict: "未核实", data: null, checks: [], reason: "读不到历史数据：timeout"});
  expect(evidenceView(noData)!.summary).toContain("数据 未读到");
  expect(evidenceView(noData)!.verdict.text).toBe("未来数据检查未核实：读不到历史数据：timeout");
});

test("a result from before the pack says so instead of guessing", () => {
  expect(evidenceView(undefined)).toBeNull();
  render(<EvidenceLine evidence={undefined} />);
  expect(screen.getByText("证据：这次回测当时没有记录")).toBeTruthy();
});

test("the line folds the full record and sends nothing", () => {
  const fetchSpy = vi.spyOn(globalThis, "fetch");
  const {container} = render(<EvidenceLine evidence={pack()} />);
  expect(container.querySelector("details.desk-evidence summary")?.textContent).toContain("未来数据检查全部通过");
  expect(screen.getByText("0123456789abcdef0123456789abcdef01234567")).toBeTruthy();
  expect(screen.getByText("参数是在这段行情已经看得到之后定的")).toBeTruthy();
  expect(fetchSpy).not.toHaveBeenCalled();
});
