/* 开仓前回测 box (#1309) above 「执行」 on a gold Paper plan. Polls one GET until the result is
 * ready, for at most 6 minutes. History only: it has no button and sends nothing. */
import {useEffect, useState} from "react";
import {view, type BacktestResult} from "../backtest";

export const POLL_MS = 3000;
export const MAX_TRIES = 120;

async function read(key: string): Promise<BacktestResult | null> {
  try {
    const response = await fetch(`/api/backtest/${encodeURIComponent(key)}`, {cache: "no-store"});
    if (response.ok) return (await response.json()) as BacktestResult;
    if (response.status === 404) return null;
    return {status: "unavailable", reason: `HTTP ${response.status}`};
  } catch (error) {
    return {status: "unavailable", reason: error instanceof Error ? error.message : String(error)};
  }
}

export function BacktestBox({backtestKey}: {backtestKey: string}) {
  const [result, setResult] = useState<BacktestResult | null>(null);

  useEffect(() => {
    let alive = true, tries = 0, timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      const next = await read(backtestKey);
      if (!alive) return;
      const pending = !next || next.status === "running";
      if (pending && ++tries >= MAX_TRIES) { setResult({status: "unavailable", reason: "等了 6 分钟还没算完"}); return; }
      setResult(next);
      if (pending) timer = setTimeout(poll, POLL_MS);
    };
    void poll();
    return () => { alive = false; if (timer) clearTimeout(timer); };
  }, [backtestKey]);

  const v = view(result);
  if (v.state !== "ready") return <section className="desk-backtest" aria-live="polite"><p className="note">{v.text}</p></section>;
  const top = Math.max(...v.bars, 1);
  return (
    <section className="desk-backtest" aria-live="polite">
      <header><b>{v.title}</b><small>只是历史参考，不代表未来</small></header>
      <p className="note">{v.sub}</p>
      <dl>{v.cells.map(([k, val]) => <div key={k}><dt>{k}</dt><dd>{val}</dd></div>)}</dl>
      <div className="desk-backtest-bars" role="img" aria-label={`每段收益的分布，从 ${v.range[0]} 到 ${v.range[1]}`}>
        {v.bars.map((n, i) => <i key={i} style={{height: `${Math.round(n / top * 100)}%`}} />)}
      </div>
      <div className="desk-backtest-axis"><span>{v.range[0]}</span><span>{v.range[1]}</span></div>
      <p className="note">{v.notes.join(" · ")}</p>
    </section>
  );
}
