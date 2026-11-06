import {render} from "@testing-library/react";
import {GmHeader, type HeaderProps} from "./islands/GmHeader";
import legacy from "./gridmind/legacy-header.json";

/* legacy-header.json was captured once (#1353) from the last GridMind that wrote the top bar itself
 * (3d3aede): for each read model, the props the new GridMind publishes and each element as it was. */
type El = {tag: string; cls: string | null; title: string | null; html: string};
type Case = {name: string; props: HeaderProps; legacy: Record<string, El>};

describe("顶栏 renders exactly what GridMind wrote", () => {
  for (const c of legacy as unknown as Case[]) {
    it(c.name, () => {
      const {container} = render(<GmHeader {...c.props} />);
      for (const [id, want] of Object.entries(c.legacy)) {
        const el = container.querySelector(`#${id}`)!;
        expect({tag: el.tagName, cls: el.getAttribute("class"), title: el.getAttribute("title"), html: el.innerHTML}).toEqual(want);
      }
    });
  }

  it("covers healthy, ticking and failed states, not just errors", () => {
    const seen = (id: string) => (legacy as unknown as Case[]).map(c => c.legacy[id]!.cls ?? "");
    expect(seen("marketBadge")).toEqual(expect.arrayContaining(["pill run-state running", "pill run-state error"]));
    expect(seen("runBadge")).toEqual(expect.arrayContaining(["top-status ok", "top-status warn"]));
    expect(seen("headerPrice")).toEqual(expect.arrayContaining(["tick-up", "tick-down"]));
    expect(seen("sourceBadge")).toEqual(expect.arrayContaining(["pill ok", "pill "]));
  });

  it("a read-model failure before the first render says 运行异常 and shows no healthy state", () => {
    const failed = (legacy as unknown as Case[]).find(c => c.name === "read_model_fails_first")!;
    expect(failed.legacy.marketBadge!.cls).toContain("error");
    expect(failed.legacy.marketBadge!.html).toContain("运行异常");
    expect(failed.legacy.runBadge!.cls).toBe("top-status");
  });
});

it("with nothing published it shows the loading placeholders, not a state", () => {
  const {container} = render(<GmHeader />);
  expect(container.querySelector("#marketBadge")!.textContent).toBe("状态读取中");
  expect(container.querySelector("#runBadge")!.textContent).toBe("状态读取中");
});
