import type {BacktestResult} from "./backtest";

export const READY: BacktestResult = {
  status: "ready", history_start: "2026-05-09T06:38:00+00:00", history_end: "2026-11-05T06:37:00+00:00",
  summary: {runs: 25, median_return_pct: 0.25, worst_return_pct: -2.05, best_return_pct: 0.99, stopped_pct: 40.0, mean_fills: 30.1, funded_runs: 12,
            histogram: {low: -2.05, width: 0.25, counts: [4, 5, 1, 0, 0, 1, 0, 1, 0, 10, 2, 1]}},
};
