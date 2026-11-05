/* 回测工作台 tab (#1335): pick an asset, fill 1–3 DCA parameter sets, run, compare side by side.
 * History only: the page has no "open with these settings" button. Its one POST starts the
 * calculation; results are read with GET. */
import {useEffect, useState} from "react";
import {getJson, startComparison} from "../api";
import {ASSETS, DEFAULT_VARIANTS, MAX_VARIANTS, check, notes, rows, variantLabel, type CompareResult, type DcaParams} from "../compare";

export const POLL_MS = 2000;
export const MAX_POLLS = 150;  // 5 minutes

function Bars({counts}: {counts: number[]}) {
  const top = Math.max(...counts, 1);
  return <div className="desk-backtest-bars" role="img" aria-label="每段收益的分布">{counts.map((n, i) => <i key={i} style={{height: `${Math.round(n / top * 100)}%`}} />)}</div>;
}

export function Workbench() {
  const [asset, setAsset] = useState("XAU");
  const [variants, setVariants] = useState<DcaParams[]>(DEFAULT_VARIANTS);
  const [key, setKey] = useState("");
  const [result, setResult] = useState<CompareResult | null>(null);
  const [message, setMessage] = useState("");

  useEffect(() => {
    if (!key) return;
    let alive = true, polls = 0, timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      try {
        const next = await getJson<CompareResult>(`/api/backtest/compare/${encodeURIComponent(key)}`);
        if (!alive) return;
        setResult(next);
        if (next.status === "running") {
          if (++polls >= MAX_POLLS) { setResult({status: "unavailable", reason: "等了 5 分钟还没算完"}); return; }
          timer = setTimeout(poll, POLL_MS);
        }
      } catch (e) { if (alive) setResult({status: "unavailable", reason: e instanceof Error ? e.message : String(e)}); }
    };
    void poll();
    return () => { alive = false; if (timer) clearTimeout(timer); };
  }, [key]);

  const running = result?.status === "running";
  const problem = check(variants);
  const set = (i: number, field: keyof DcaParams, value: string) =>
    setVariants(variants.map((v, j) => j === i ? {...v, [field]: Number(value)} : v));

  const start = async () => {
    setMessage("");
    try {
      const {key: next} = await startComparison({asset, strategy: "dca", variants});
      setResult({status: "running"});
      setKey(next);
    } catch (e) { setMessage(e instanceof Error ? e.message : String(e)); }
  };

  const shown = result?.status === "ready" ? result : null;
  return (
    <div className="desk-bench">
      <div className="desk-bench-head">
        <label>品种 <select aria-label="品种" value={asset} onChange={e => setAsset(e.target.value)} disabled={running}>
          {ASSETS.map(([id, name]) => <option key={id} value={id}>{name}</option>)}
        </select></label>
        <label>策略 <select aria-label="策略" value="dca" disabled={running}>
          <option value="dca">定投</option>
          <option value="grid" disabled>网格（即将开通）</option>
        </select></label>
        <small>过去 180 天 OKX 历史，每段 7 天、互不重叠；只是参考，不能用来开仓</small>
      </div>
      <div className="desk-bench-variants">
        {variants.map((v, i) => (
          <fieldset key={i} disabled={running}>
            <legend>第 {i + 1} 组</legend>
            <label>每次金额（USDT）<input type="number" inputMode="decimal" min={10} max={100000} value={v.amount_usdt} onChange={e => set(i, "amount_usdt", e.target.value)} /></label>
            <label>间隔（小时）<input type="number" inputMode="numeric" min={1} max={168} step={1} value={v.interval_hours} onChange={e => set(i, "interval_hours", e.target.value)} /></label>
            {variants.length > 1 ? <button type="button" className="desk-link" onClick={() => setVariants(variants.filter((_, j) => j !== i))}>去掉这组</button> : null}
          </fieldset>
        ))}
        {variants.length < MAX_VARIANTS ? <button type="button" className="desk-link" disabled={running} onClick={() => setVariants([...variants, {amount_usdt: 100, interval_hours: 72}])}>＋ 加一组</button> : null}
      </div>
      <div className="desk-bench-run">
        <button type="button" className="desk-btn" disabled={running || Boolean(problem)} onClick={() => void start()}>{running ? "回测中…" : "开始回测"}</button>
        <span className="note" role="status">{problem || message || (running ? "正在用过去 180 天的行情算，通常不到半分钟" : "")}</span>
      </div>
      {result?.status === "unavailable" ? <p className="note warn">回测不可用：{result.reason || "原因未知"}</p> : null}
      {shown ? (
        <div className="desk-bench-result">
          <table>
            <thead><tr><th scope="col">{ASSETS.find(([id]) => id === shown.asset)?.[1]} · 定投</th>{shown.variants?.map((v, i) => <th key={i} scope="col">{variantLabel(v.params)}</th>)}</tr></thead>
            <tbody>
              {rows(shown).map(row => (
                <tr key={row.label}><th scope="row">{row.label}</th>{row.cells.map((cell, i) => <td key={i} className={row.best === i ? "best" : ""}>{cell}</td>)}</tr>
              ))}
              <tr><th scope="row">每段收益分布</th>{shown.variants?.map((v, i) => <td key={i}><Bars counts={v.summary.histogram.counts} /></td>)}</tr>
            </tbody>
          </table>
          <p className="note">{notes(shown).join(" · ")}</p>
        </div>
      ) : null}
    </div>
  );
}
