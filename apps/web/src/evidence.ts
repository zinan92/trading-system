/* 回测证据包 (#1369): what produced a backtest number. View logic only; it sends nothing. */

export type CheckStatus = "ok" | "failed" | "未核实" | string;

export interface EvidenceCheck {name: string; label: string; status: CheckStatus; detail: string}

export interface Evidence {
  schema_version: string;
  code: {commit: string | null; dirty: boolean | null; reason?: string | null};
  data: {
    source: string; instrument: string; bars: number; expected_bars?: number | null;
    first_bar: string | null; last_bar: string | null; bars_sha256: string; funding_rows: number; funding_sha256: string;
  } | null;
  inputs?: Record<string, string | null>;
  params: {digest: string; values?: Record<string, unknown>};
  checks: EvidenceCheck[];
  tested?: Array<{label: string; test: string}>;
  verdict: CheckStatus;
  reason?: string | null;
  notes?: string[];
}

export interface EvidenceView {
  summary: string;
  verdict: {state: "ok" | "failed" | "unverified"; text: string};
  rows: Array<[string, string]>;
  checks: Array<{label: string; status: string; detail: string}>;
  tested: string[];
  notes: string[];
}

const STATUS: Record<string, string> = {ok: "通过", failed: "没通过", "未核实": "未核实"};
const short = (digest: string | null | undefined, n = 8) => (digest || "").replace(/^sha256:/, "").slice(0, n) || "—";
const day = (iso: string | null | undefined) => (iso || "").slice(0, 10) || "—";

export function code(c: Evidence["code"]): string {
  if (!c.commit) return `未核实（${c.reason || "读不到提交号"}）`;
  return `${c.commit.slice(0, 7)}${c.dirty ? "（有未提交改动）" : c.dirty == null ? "（不知道有没有未提交改动）" : ""}`;
}

export function verdict(e: Evidence): EvidenceView["verdict"] {
  if (e.verdict === "ok") return {state: "ok", text: `未来数据检查全部通过（${e.checks.length} 项）`};
  const failed = e.checks.filter(c => c.status === "failed").map(c => c.label);
  if (e.verdict === "failed") return {state: "failed", text: `未来数据检查没通过：${failed.join("；")}`};
  const open = e.checks.filter(c => c.status !== "ok").map(c => c.label);
  return {state: "unverified", text: `未来数据检查未核实：${open.length ? open.join("；") : e.reason || "这次没有拿到数据"}`};
}

/** null when the result predates the pack: the page says so instead of guessing. */
export function evidenceView(e: Evidence | null | undefined): EvidenceView | null {
  if (!e || !e.code || !e.params) return null;
  const d = e.data;
  const v = verdict(e);
  return {
    summary: [`代码 ${code(e.code)}`, d ? `数据 ${short(d.bars_sha256)}（${d.bars.toLocaleString("en-US")} 根）` : "数据 未读到"].join(" · "),
    verdict: v,
    rows: [
      ["代码提交", e.code.commit ? `${e.code.commit}${e.code.dirty ? "（有未提交改动）" : ""}` : code(e.code)],
      ...(d ? [
        ["数据", `${d.source} · ${d.instrument} · ${day(d.first_bar)} 到 ${day(d.last_bar)} · ${d.bars.toLocaleString("en-US")}${d.expected_bars != null ? ` / ${d.expected_bars.toLocaleString("en-US")}` : ""} 根`],
        ["行情指纹", d.bars_sha256],
        ["资金费指纹", `${d.funding_sha256}（${d.funding_rows} 条）`],
      ] as Array<[string, string]> : []),
      ...Object.entries(e.inputs || {}).filter(([, value]) => value).map(([key, value]) => [key === "preflight_sha256" ? "Paper 配置指纹" : key === "contract_sha256" ? "回测合约指纹" : key, String(value)] as [string, string]),
      ["参数指纹", e.params.digest],
    ],
    checks: e.checks.map(c => ({label: c.label, status: STATUS[c.status] ?? c.status, detail: c.detail})),
    tested: (e.tested || []).map(t => `${t.label}（单元测试覆盖，本次未跑）`),
    notes: e.notes || [],
  };
}
