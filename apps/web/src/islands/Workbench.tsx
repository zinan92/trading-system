/* 回测工作台 tab (#1335, grid #1336): pick an asset and a strategy, fill 1–3 parameter sets, run,
 * compare side by side.
 * History only: the page has no "open with these settings" button. Its one POST starts the
 * calculation; results are read with GET. */
import {useEffect, useState} from "react";
import {getJson, startComparison} from "../api";
import {ASSETS, DEFAULT_GRID_VARIANTS, DEFAULT_VARIANTS, GRID_ASSETS, MAX_VARIANTS, check, checkGrid, gridLabel, gridNotes, gridRows, notes, rows, variantLabel,
  type CompareResult, type DcaParams, type GridParams, type Strategy} from "../compare";
import {EvidenceLine} from "./EvidenceLine";

export const POLL_MS = 2000;
// A DCA is arithmetic; a grid replays every window through Nautilus, a few minutes per set.
export const MAX_POLLS: Record<Strategy, number> = {dca: 150, grid: 450};
const WAIT: Record<Strategy, string> = {dca: "5 分钟", grid: "15 分钟"};

function Bars({counts}: {counts: number[]}) {
  const top = Math.max(...counts, 1);
  return <div className="desk-backtest-bars" role="img" aria-label="每段收益的分布">{counts.map((n, i) => <i key={i} style={{height: `${Math.round(n / top * 100)}%`}} />)}</div>;
}

export function Workbench() {
  const [asset, setAsset] = useState("XAU");
  const [strategy, setStrategy] = useState<Strategy>("dca");
  const [variants, setVariants] = useState<DcaParams[]>(DEFAULT_VARIANTS);
  const [grids, setGrids] = useState<GridParams[]>(DEFAULT_GRID_VARIANTS);
  const [polling, setPolling] = useState<Strategy>("dca");
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
          if (++polls >= MAX_POLLS[polling]) { setResult({status: "unavailable", reason: `等了 ${WAIT[polling]}还没算完`}); return; }
          timer = setTimeout(poll, POLL_MS);
        }
      } catch (e) { if (alive) setResult({status: "unavailable", reason: e instanceof Error ? e.message : String(e)}); }
    };
    void poll();
    return () => { alive = false; if (timer) clearTimeout(timer); };
  }, [key, polling]);

  const running = result?.status === "running";
  const gridOk = GRID_ASSETS.includes(asset);
  const problem = strategy === "grid" ? (gridOk ? checkGrid(grids) : `${asset} 网格回测还没开通`) : check(variants);
  const set = (i: number, field: keyof DcaParams, value: string) =>
    setVariants(variants.map((v, j) => j === i ? {...v, [field]: Number(value)} : v));
  const setGrid = (i: number, field: keyof GridParams, value: string) =>
    setGrids(grids.map((v, j) => {
      if (j !== i) return v;
      if (field === "direction") return {...v, direction: value as GridParams["direction"], stop_pct: value === "neutral" ? null : v.stop_pct};
      if (field === "stop_pct") return {...v, stop_pct: value === "" ? null : Number(value)};
      return {...v, [field]: Number(value)};
    }));
  const count = strategy === "grid" ? grids.length : variants.length;

  const start = async () => {
    setMessage("");
    try {
      const {key: next} = await startComparison(strategy === "grid" ? {asset, strategy, variants: grids} : {asset, strategy, variants});
      setPolling(strategy);
      setResult({status: "running"});
      setKey(next);
    } catch (e) { setMessage(e instanceof Error ? e.message : String(e)); }
  };

  const shown = result?.status === "ready" ? result : null;
  const isGrid = shown?.strategy === "grid";
  return (
    <div className="desk-bench">
      <div className="desk-bench-head">
        <label>品种 <select aria-label="品种" value={asset} onChange={e => setAsset(e.target.value)} disabled={running}>
          {ASSETS.map(([id, name]) => <option key={id} value={id}>{name}</option>)}
        </select></label>
        <label>策略 <select aria-label="策略" value={strategy} onChange={e => setStrategy(e.target.value as Strategy)} disabled={running}>
          <option value="dca">定投</option>
          <option value="grid" disabled={!gridOk}>{gridOk ? "网格" : `网格（${asset} 未开通）`}</option>
        </select></label>
        <small>过去 180 天 OKX 历史，每段 7 天、互不重叠；只是参考，不能用来开仓</small>
      </div>
      <div className="desk-bench-variants">
        {strategy === "dca" ? variants.map((v, i) => (
          <fieldset key={i} disabled={running}>
            <legend>第 {i + 1} 组</legend>
            <label>每次金额（USDT）<input type="number" inputMode="decimal" min={10} max={100000} value={v.amount_usdt} onChange={e => set(i, "amount_usdt", e.target.value)} /></label>
            <label>间隔（小时）<input type="number" inputMode="numeric" min={1} max={168} step={1} value={v.interval_hours} onChange={e => set(i, "interval_hours", e.target.value)} /></label>
            {variants.length > 1 ? <button type="button" className="desk-link" onClick={() => setVariants(variants.filter((_, j) => j !== i))}>去掉这组</button> : null}
          </fieldset>
        )) : grids.map((v, i) => (
          <fieldset key={i} disabled={running}>
            <legend>第 {i + 1} 组</legend>
            <label>方向<select value={v.direction} onChange={e => setGrid(i, "direction", e.target.value)}>
              <option value="neutral">双向</option><option value="long">做多</option><option value="short">做空</option>
            </select></label>
            <label>下沿（低于现价 %）<input type="number" inputMode="decimal" min={0.5} max={30} step={0.5} value={v.below_pct} onChange={e => setGrid(i, "below_pct", e.target.value)} /></label>
            <label>上沿（高于现价 %）<input type="number" inputMode="decimal" min={0.5} max={30} step={0.5} value={v.above_pct} onChange={e => setGrid(i, "above_pct", e.target.value)} /></label>
            <label>格数<input type="number" inputMode="numeric" min={2} max={30} step={1} value={v.levels} onChange={e => setGrid(i, "levels", e.target.value)} /></label>
            <label>每格金额（USDT）<input type="number" inputMode="decimal" min={10} max={100000} value={v.notional_per_level} onChange={e => setGrid(i, "notional_per_level", e.target.value)} /></label>
            {v.direction !== "neutral" ? <label>止损（离现价 %，可空）<input type="number" inputMode="decimal" min={0} step={0.5} value={v.stop_pct ?? ""} onChange={e => setGrid(i, "stop_pct", e.target.value)} /></label> : null}
            {grids.length > 1 ? <button type="button" className="desk-link" onClick={() => setGrids(grids.filter((_, j) => j !== i))}>去掉这组</button> : null}
          </fieldset>
        ))}
        {count < MAX_VARIANTS ? <button type="button" className="desk-link" disabled={running} onClick={() => strategy === "grid"
          ? setGrids([...grids, {...(grids.at(-1) ?? DEFAULT_GRID_VARIANTS[0]!)}])
          : setVariants([...variants, {amount_usdt: 100, interval_hours: 72}])}>＋ 加一组</button> : null}
      </div>
      <div className="desk-bench-run">
        <button type="button" className="desk-btn" disabled={running || Boolean(problem)} onClick={() => void start()}>{running ? "回测中…" : "开始回测"}</button>
        <span className="note" role="status">{problem || message || (running ? (polling === "grid" ? "正在逐段回放过去 180 天，每组要几分钟" : "正在用过去 180 天的行情算，通常不到半分钟") : "")}</span>
      </div>
      {result?.status === "unavailable" ? <><p className="note warn">回测不可用：{result.reason || "原因未知"}</p>{result.evidence ? <EvidenceLine evidence={result.evidence} /> : null}</> : null}
      {shown ? (
        <div className="desk-bench-result">
          <table>
            <thead><tr><th scope="col">{ASSETS.find(([id]) => id === shown.asset)?.[1]} · {isGrid ? "网格" : "定投"}</th>{shown.variants?.map((v, i) => <th key={i} scope="col">{isGrid ? gridLabel(v.params as GridParams) : variantLabel(v.params as DcaParams)}</th>)}</tr></thead>
            <tbody>
              {(isGrid ? gridRows(shown) : rows(shown)).map(row => (
                <tr key={row.label}><th scope="row">{row.label}</th>{row.cells.map((cell, i) => <td key={i} className={row.best === i ? "best" : ""}>{cell}</td>)}</tr>
              ))}
              <tr><th scope="row">每段收益分布</th>{shown.variants?.map((v, i) => <td key={i}><Bars counts={v.summary.histogram.counts} /></td>)}</tr>
            </tbody>
          </table>
          <p className="note">{(isGrid ? gridNotes(shown) : notes(shown)).join(" · ")}</p>
          <EvidenceLine evidence={shown.evidence} />
        </div>
      ) : null}
    </div>
  );
}
