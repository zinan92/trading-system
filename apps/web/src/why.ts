/* 这一笔为什么 (#1359): the types and words for /api/why. */
export type WhyStatus = "ok" | "attention" | "failed" | "unknown" | "not_reached";
export interface WhyStep {key: string; label: string; status: WhyStatus; summary: string; at?: string | null; source?: string | null; facts?: {label: string; value: string}[]}
export interface WhyIdentity {run_id?: string | null; plan_digest?: string | null; activation_id?: string | null; release_sha?: string | null; market_source?: string | null}
export interface WhyChain {
  status: "clean" | "attention" | "broken";
  first_problem?: {key: string; label: string; summary: string} | null;
  identity: WhyIdentity;
  steps: WhyStep[];
  note: string;
}

export const STATUS_TEXT: Record<WhyStatus, string> = {ok: "正常", attention: "留意", failed: "失败", unknown: "记录缺失", not_reached: "没走到"};

export function verdict(chain: WhyChain): string {
  if (chain.first_problem) return `断在「${chain.first_problem.label}」：${chain.first_problem.summary}`;
  return chain.status === "attention" ? "链路是通的，有几处要留意" : "每一步都有记录，没有断点";
}

const short = (value: string) => value.replace(/^sha256:/, "").slice(0, 12);

/** What identifies this run. A value nobody recorded says so; it is not looked up from today's code. */
export function identityRows(identity: WhyIdentity): {label: string; value: string}[] {
  return [
    {label: "计划指纹", value: identity.plan_digest ? short(identity.plan_digest) : "没有记录"},
    {label: "代码版本", value: identity.release_sha ? short(identity.release_sha) : "当时没有记录"},
    ...(identity.market_source ? [{label: "行情来源", value: identity.market_source}] : []),
  ];
}
