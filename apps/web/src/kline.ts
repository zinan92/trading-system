/* The page's shared K-line widget (packages/standard-kline, loaded by the GridMind page as a global). */

export interface KlinePayload {bars: unknown[]; symbol: string; timeframe: string; provider: string}

export interface KlineChart {
  candleSeries?: {applyOptions(options: Record<string, unknown>): void};
  setPayload(payload: KlinePayload, extras: {markers: unknown[]; priceLines: unknown[]}): void;
  destroy(): void;
}

export interface StandardKlineGlobal {
  StandardKlineChart: new (host: HTMLElement, options: Record<string, unknown>) => KlineChart;
  adaptBarPayload(payload: KlinePayload): {candles: Array<{time: number}>};
  nearestTime(times: number[], time: number): number | null | undefined;
}

export const standardKline = (): StandardKlineGlobal | undefined =>
  (window as unknown as {StandardKline?: StandardKlineGlobal}).StandardKline;
