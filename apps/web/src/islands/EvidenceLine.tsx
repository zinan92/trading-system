/* 回测证据包 (#1369): one line under a backtest result, with the full record folded below it. */
import {evidenceView, type Evidence} from "../evidence";

export function EvidenceLine({evidence}: {evidence: Evidence | null | undefined}) {
  const v = evidenceView(evidence);
  if (!v) return <p className="note desk-evidence-none">证据：这次回测当时没有记录</p>;
  return (
    <details className="desk-evidence">
      <summary>
        <span>证据 · {v.summary}</span>
        <span className={`desk-evidence-verdict ${v.verdict.state}`}>{v.verdict.text}</span>
      </summary>
      <dl>{v.rows.map(([k, val]) => <div key={k}><dt>{k}</dt><dd>{val}</dd></div>)}</dl>
      {v.checks.length ? <ul>{v.checks.map(c => <li key={c.label}><b>{c.status}</b> {c.label}：{c.detail}</li>)}</ul> : null}
      {v.tested.length ? <ul>{v.tested.map(t => <li key={t}>{t}</li>)}</ul> : null}
      {v.notes.map(n => <p key={n} className="note">{n}</p>)}
    </details>
  );
}
