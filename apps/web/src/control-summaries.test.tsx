import {act} from "@testing-library/react";
import type {ComponentType} from "react";
import {createRoot} from "react-dom/client";
import {GridSummary, PreviewSummary, StartBlockers} from "./islands/ControlSummaries";
import legacy from "./gridmind/legacy-control.json";

/* legacy-control.json was captured once (#1329) from the last GridMind that wrote these itself (8ef4b3a):
 * the render functions were called with the same state on both pages, including injected <, & and null. */
type Case = {name: string; props: Record<string, unknown>; html: string};
const groups = legacy as unknown as Record<"preview" | "summary" | "blockers", Case[]>;

function html(Component: ComponentType<any>, props: Record<string, unknown>): string {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  act(() => root.render(<Component {...props} />));
  const out = host.innerHTML;
  act(() => root.unmount());
  host.remove();
  return out;
}

describe("text panels render exactly what GridMind wrote", () => {
  for (const c of groups.preview) it(`preview · ${c.name}`, () => expect(html(PreviewSummary, c.props)).toBe(c.html));
  for (const c of groups.summary) it(`grid summary · ${c.name}`, () => expect(html(GridSummary, c.props)).toBe(c.html));
  for (const c of groups.blockers) it(`start blockers · ${c.name}`, () => expect(html(StartBlockers, c.props)).toBe(c.html));

  it("covers the risk lines, an unsafe message and the blocked case", () => {
    const all = Object.values(groups).flat().map(c => c.html).join("");
    for (const text of ["预计最大损失", "preview-risk critical", "&lt;杠杆&gt; &amp; 过高", "可比方案", "启动前需补齐", "硬止损", "聚合 TP #2", "已证实 2 格", "不完整", "机器码：account_blocked"]) expect(all).toContain(text);
  });
});

it("the grid line keeps its placeholder until GridMind publishes", () => {
  expect(html(GridSummary, {})).toBe("网格读取中");
});
