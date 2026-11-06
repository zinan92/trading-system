/* 这一笔为什么 (#1359). Read-only: GET /api/why only.
 * A finished run (交易回看) or a desk judgment whose preview or order failed (「今天」 step 3, #1374). */
import {useEffect, useState} from "react";
import {getJson} from "../api";
import {when} from "../review";
import {STATUS_TEXT, identityRows, verdict, type WhyChain} from "../why";

export const whyUrl = ({run, judgment}: {run?: string; judgment?: number | string}) =>
  run ? `/api/why?run=${encodeURIComponent(run)}` : `/api/why?judgment=${encodeURIComponent(String(judgment ?? ""))}`;

export function WhyPanel({run, judgment}: {run?: string; judgment?: number | string}) {
  const [chain, setChain] = useState<WhyChain | null>(null);
  const [error, setError] = useState("");
  const url = whyUrl({run, judgment});

  useEffect(() => {
    let alive = true;
    setChain(null);
    setError("");
    if (!run && (judgment === undefined || judgment === "")) return;
    getJson<WhyChain>(url)
      .then(data => { if (alive) setChain(data); })
      .catch(e => { if (alive) setError(`链路读不出来：${e instanceof Error ? e.message : e}`); });
    return () => { alive = false; };
  }, [url, run, judgment]);

  if (error) return <div className="desk-review-missing" role="status">{error}</div>;
  if (!chain) return null;
  return (
    <section className="desk-why" aria-label="这一笔为什么">
      <h3>这一笔为什么</h3>
      <p className={`desk-why-verdict ${chain.status}`} role="status">{verdict(chain)}</p>
      <ol>
        {chain.steps.map(step => (
          <li key={step.key} className={step.status}>
            <span className="desk-why-pill">{STATUS_TEXT[step.status]}</span>
            <div>
              <b>{step.label}</b>{step.at ? <small>{when(step.at)}</small> : null}
              <p>{step.summary}</p>
              {step.facts?.length ? <dl>{step.facts.map(fact => <div key={fact.label}><dt>{fact.label}</dt><dd>{fact.value}</dd></div>)}</dl> : null}
              {step.source ? <small className="desk-why-source">{step.source}</small> : null}
            </div>
          </li>
        ))}
      </ol>
      <p className="desk-why-identity">{identityRows(chain.identity).map(row => `${row.label} ${row.value}`).join(" · ")}</p>
      <p className="desk-why-note">{chain.note}</p>
    </section>
  );
}
