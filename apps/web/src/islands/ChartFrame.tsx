/* The frame around the main K-line (#1398, ADR 0008). The canvas, its listeners and the range drag stay
 * with GridMind until the page moves (#1330), so this takes GridMind's own node and moves it in. It
 * never makes a chart node of its own: a new one would have none of GridMind's bindings. */
function adopt(slot: HTMLDivElement | null) {
  const chart = (window as Window & {gmChartHost?: HTMLElement}).gmChartHost;
  if (slot && chart && chart.parentElement !== slot) slot.appendChild(chart);
}

export function ChartFrame() {
  return <div className="chart-slot" ref={adopt} />;
}
