/* GridMind's own formatters, ported verbatim from dashboard-gridmind.html (#1328) so a table cell
 * reads the same whichever side renders it. Keep them byte-for-byte with the legacy definitions. */

export const num = (v: unknown): string =>
  v === null || v === undefined || v === "" ? "--" : Number.isFinite(Number(v)) ? Number(v).toLocaleString("en-US", {maximumFractionDigits: 2}) : "--";

export const signed = (v: unknown): string => {
  const x = Number(v);
  return Number.isFinite(x) ? `${x > 0 ? "+" : ""}${num(x)}` : "--";
};

export const finiteNumber = (value: unknown): number | null =>
  value !== null && value !== "" && Number.isFinite(Number(value)) ? Number(value) : null;

export const pnlClass = (v: unknown): string => (Number(v) > 0 ? "pos" : Number(v) < 0 ? "neg" : "");

export function timestampDate(timestamp: unknown): Date {
  if (timestamp == null || timestamp === "") return new Date("");
  const text = String(timestamp).trim(), numeric = Number(text);
  if (text !== "" && Number.isFinite(numeric) && /^\d+(?:\.\d+)?$/.test(text)) {
    const magnitude = Math.abs(numeric);
    const milliseconds = magnitude >= 1e17 ? numeric / 1e6 : magnitude >= 1e14 ? numeric / 1e3 : magnitude >= 1e11 ? numeric : numeric * 1e3;
    return new Date(milliseconds);
  }
  return new Date(timestamp as string);
}

export function beijingDateTime(timestamp: unknown): string {
  const date = timestampDate(timestamp);
  if (Number.isNaN(date.getTime())) return "--";
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-CA", {year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZone: "Asia/Shanghai"})
    .formatToParts(date).filter(part => part.type !== "literal").map(part => [part.type, part.value]));
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
}

export function quantityText(value: unknown): string {
  const number = Number(value);
  return Number.isFinite(number) ? number.toLocaleString("en-US", {minimumFractionDigits: 0, maximumFractionDigits: 6, useGrouping: false}) : "--";
}
