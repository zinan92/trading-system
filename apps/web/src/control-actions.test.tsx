import {act} from "@testing-library/react";
import type {ComponentType} from "react";
import {createRoot} from "react-dom/client";
import {ControlActions, RobotActions, StartRisk} from "./islands/ControlActions";
import gridmind from "../../../dashboard-gridmind.html?raw";

function draw(Component: ComponentType<any>, props: Record<string, unknown> = {}): HTMLElement {
  const host = document.createElement("div");
  const root = createRoot(host);
  act(() => root.render(<Component {...props} />));
  return host;
}

const page = new DOMParser().parseFromString(gridmind, "text/html");
const same = (host: HTMLElement, id: string) => {
  const legacy = page.querySelector(id)!;
  expect(host.children).toHaveLength(legacy.children.length);
  [...legacy.children].forEach((node, i) => expect(host.children[i]!.isEqualNode(node)).toBe(true));
};
const button = (host: HTMLElement, id: string) => host.querySelector<HTMLButtonElement>(id)!;

describe("the action controls are the legacy markup before anything is published", () => {
  it("preview and confirm", () => same(draw(ControlActions), "#dashboardActions"));
  it("start and stop", () => same(draw(RobotActions), "#robotActions"));
  it("the risk review", () => same(draw(StartRisk), "#startRiskReview"));
});

describe("they draw what GridMind publishes", () => {
  it("enables nothing on its own", () => {
    expect(button(draw(ControlActions), "#dashboardConfirmButton").disabled).toBe(true);
    expect(button(draw(StartRisk), "#confirmStartRisk").disabled).toBe(true);
  });

  it("preview and confirm follow their own flags", () => {
    const host = draw(ControlActions, {previewDisabled: false, confirmDisabled: true});
    expect(button(host, "#dashboardPreviewButton").disabled).toBe(false);
    expect(button(host, "#dashboardConfirmButton").disabled).toBe(true);
  });

  it("start and stop show GridMind's class, text, title and state", () => {
    const host = draw(RobotActions, {
      start: {className: "robot start is-inactive", disabled: true, text: "请先修复参数", title: "缺少区间：填写上下边界"},
      stop: {className: "robot stop is-active", disabled: false, text: "正在撤单并平仓…"},
    });
    const start = button(host, "#startRobot"), stop = button(host, "#stopRobot");
    expect([start.className, start.disabled, start.textContent, start.title]).toEqual(["robot start is-inactive", true, "请先修复参数", "缺少区间：填写上下边界"]);
    expect([stop.className, stop.disabled, stop.textContent]).toEqual(["robot stop is-active", false, "正在撤单并平仓…"]);
  });

  it("the risk review shows the comparison, the published ticks, the blockers and the gate", () => {
    const host = draw(StartRisk, {
      rows: [["预计最大损失", "12 USD", "<34> USD"]],
      acks: [{code: "a", title: "杠杆 & 风险", summary: "偏高", critical: true, checked: true}, {code: "b", title: "规格", summary: "变了", checked: false}],
      blockers: [{code: "x", title: "行情过期", explanation: "没有新价格", action: "等行情恢复", lines: []}],
      gate: {ok: false, text: "请逐项确认"},
      confirmText: "确认风险并启动 DCA",
      confirmDisabled: true,
    });
    expect(host.querySelector(".grid-compare-row")!.textContent).toBe("预计最大损失12 USD→<34> USD");
    const boxes = [...host.querySelectorAll<HTMLInputElement>("[data-start-risk-ack]")];
    expect(boxes.map(box => [box.dataset.startRiskAck, box.dataset.gmIntent, box.checked])).toEqual([["a", "startRisk.ack", true], ["b", "startRisk.ack", false]]);
    expect(host.querySelector(".grid-risk-ack.critical b")!.textContent).toBe("杠杆 & 风险");
    expect(host.querySelector("#startRiskBlockers")!.textContent).toContain("行情过期");
    expect(host.querySelector("#startRiskGate")!.className).toBe("grid-review-gate");
    expect(button(host, "#confirmStartRisk").textContent).toBe("确认风险并启动 DCA");
  });

  it("marks a complete gate", () => {
    expect(draw(StartRisk, {gate: {ok: true, text: "确认完整"}, confirmDisabled: false}).querySelector("#startRiskGate")!.className).toBe("grid-review-gate ok");
  });
});
