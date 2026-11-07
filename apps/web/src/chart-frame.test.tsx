import {act} from "@testing-library/react";
import {createRoot} from "react-dom/client";
import {ChartFrame} from "./islands/ChartFrame";

const shell = window as Window & {gmChartHost?: HTMLElement};

afterEach(() => { delete shell.gmChartHost; document.body.replaceChildren(); });

function page() {
  document.body.innerHTML = '<div id="frame"><div class="chart" id="productionChart"><canvas></canvas></div></div>';
  shell.gmChartHost = document.getElementById("productionChart")!;
  return document.getElementById("frame")!;
}

it("keeps GridMind's node, with its canvas and listeners, when React takes the frame", () => {
  const frame = page(), chart = shell.gmChartHost!, canvas = chart.querySelector("canvas");
  const pressed = vi.fn();
  chart.addEventListener("pointerdown", pressed);
  const root = createRoot(frame);
  act(() => root.render(<ChartFrame />));
  expect(document.getElementById("productionChart")).toBe(chart);
  expect(chart.parentElement!.className).toBe("chart-slot");
  expect(chart.querySelector("canvas")).toBe(canvas);
  chart.dispatchEvent(new Event("pointerdown"));
  expect(pressed).toHaveBeenCalledOnce();
});

it("a re-render leaves the node where it is", () => {
  const frame = page(), chart = shell.gmChartHost!;
  const root = createRoot(frame);
  act(() => root.render(<ChartFrame />));
  const slot = chart.parentElement;
  act(() => root.render(<ChartFrame />));
  expect(chart.parentElement).toBe(slot);
  expect(frame.querySelectorAll("#productionChart")).toHaveLength(1);
});

it("a new frame takes the node from the old one", () => {
  const first = page(), chart = shell.gmChartHost!;
  const old = createRoot(first);
  act(() => old.render(<ChartFrame />));
  const second = document.body.appendChild(document.createElement("div"));
  const next = createRoot(second);
  act(() => next.render(<ChartFrame />));
  act(() => old.unmount());
  expect(second.contains(chart)).toBe(true);
});

it("draws no chart of its own", () => {
  const host = document.createElement("div");
  const root = createRoot(host);
  act(() => root.render(<ChartFrame />));
  expect(host.innerHTML).toBe('<div class="chart-slot"></div>');
});
