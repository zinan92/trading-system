/* Builds to assets/gridmind/chart.js (vite.chart.config.ts), a plain script loaded before gridmind.js.
 * gridmind.js is not a module, so the chart layer reaches it as window.gmChart. */
import * as chart from "./chart";

declare global {
  interface Window { gmChart: typeof chart }
}

window.gmChart = chart;
