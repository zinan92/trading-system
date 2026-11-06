/* 交易回看 tab (#1306 → island #1326), with 品种 and 时间 filters (#1326).
 * Read-only: GET /api/review/runs, /api/review/timeline and /api/why only. It loads when the tab is first
 * opened (the legacy shim flips `active`), not on page load. */
import {useEffect, useRef, useState} from "react";
import {getJson} from "../api";
import {standardKline, type KlineChart} from "../kline";
import {WhyPanel} from "./WhyPanel";
import {PERIODS, emptyText, markers, runLabel, runsUrl, score, summary, when, type RunsResponse, type Timeline} from "../review";

function Chart({data}: {data: Timeline}) {
  const host = useRef<HTMLDivElement>(null);
  const chart = useRef<KlineChart | null>(null);
  const [missing, setMissing] = useState(false);

  useEffect(() => {
    const K = standardKline();
    if (!K || !host.current) { setMissing(true); return; }
    const market = data.market ?? {};
    chart.current ??= new K.StandardKlineChart(host.current, {height: 400, minHeight: 300, showVolume: false, timeZone: "Asia/Shanghai", locale: "zh-CN"});
    const payload = {bars: market.bars ?? [], symbol: market.instrument ?? "", timeframe: market.timeframe ?? "1h", provider: market.source ?? ""};
    const times = K.adaptBarPayload(payload).candles.map(candle => candle.time);
    const snap = (time: number) => times.length ? K.nearestTime(times, time) ?? time : time;
    const prices = (data.lines ?? []).map(line => Number(line.price)).filter(Number.isFinite);
    chart.current.candleSeries?.applyOptions({autoscaleInfoProvider: (base: () => {priceRange?: {minValue: number; maxValue: number}} | null) => {
      const info = base();  // keep the plan's boundaries and stop on screen
      if (!info?.priceRange || !prices.length) return info;
      return {...info, priceRange: {minValue: Math.min(info.priceRange.minValue, ...prices), maxValue: Math.max(info.priceRange.maxValue, ...prices)}};
    }});
    chart.current.setPayload(payload, {
      markers: markers(data, snap),
      priceLines: (data.lines ?? []).map(line => ({price: Number(line.price), title: line.title, color: line.title === "止损" ? "#f0a24b" : "#8b95a3", lineStyle: 2, lineWidth: 1})),
    });
  }, [data]);

  useEffect(() => () => { chart.current?.destroy(); chart.current = null; }, []);  // a filter with no runs unmounts the chart

  return <div ref={host}>{missing ? "K 线组件没有加载" : null}</div>;
}

function RunView({data}: {data: Timeline}) {
  const [picked, setPicked] = useState<number | null>(null);
  useEffect(() => setPicked(null), [data]);
  const backdated = (data.fills ?? []).filter(fill => fill.backdated).length;
  const judgments = (data.judgments ?? []).length;
  const missing = data.missing ?? [];
  const item = picked == null ? null : data.events?.[picked];
  return (
    <>
      <div className="desk-review-kpis">
        {summary(data).map(cell => <div key={cell.label} className={cell.bad ? "bad" : ""}><small>{cell.label}</small><b className={cell.big ? "big" : ""}>{cell.value}</b></div>)}
      </div>
      {missing.length ? <div className="desk-review-missing" role="status">这段记录缺失：{missing.join("、")}。缺的部分没有补，也没有推算。</div> : null}
      <div className="desk-review-body">
        <div>
          <Chart data={data} />
          <p className="desk-review-legend">
            ▲ 开仓 · ▼ 平仓 · K 线 1 小时 · {judgments ? `${judgments} 条判断` : "这一轮没有判断记录"}
            {backdated ? ` · 有 ${backdated} 笔账本时间是回填的，图上按实际结算时间画` : ""}
          </p>
        </div>
        <div>
          <ol className="desk-review-events">
            {(data.events ?? []).map((event, index) => (
              <li key={index}>
                <button type="button" aria-pressed={picked === index} onClick={() => setPicked(index)}><small>{when(event.ts)}</small>{event.title}</button>
              </li>
            ))}
          </ol>
          <pre className="desk-review-record" aria-live="polite">
            {item ? `${item.source}\n${item.ts || "时间缺失"}\n${JSON.stringify(item.record, null, 1)}` : "点一条，看原始记录"}
          </pre>
        </div>
      </div>
      {data.run?.id ? <WhyPanel run={data.run.id} /> : null}
    </>
  );
}

export function ReviewPanel({active = false}: {active?: boolean}) {
  const [filter, setFilter] = useState({asset: "", days: 0});
  const [list, setList] = useState<RunsResponse | null>(null);
  const [runId, setRunId] = useState("");
  const [run, setRun] = useState<Timeline | null>(null);
  const [error, setError] = useState("");
  const [opened, setOpened] = useState(active);
  if (active && !opened) setOpened(true);  // once opened, it stays loaded when the tab is left

  useEffect(() => {
    if (!opened) return;
    let alive = true;
    setError("");
    getJson<RunsResponse>(runsUrl(filter.asset, filter.days))
      .then(data => { if (!alive) return; setList(data); setRunId(data.runs[0]?.id ?? ""); if (!data.runs.length) setRun(null); })
      .catch(e => { if (alive) setError(`回看读不出来：${e instanceof Error ? e.message : e}`); });
    return () => { alive = false; };
  }, [filter, opened]);

  useEffect(() => {
    if (!runId) return;
    let alive = true;
    getJson<Timeline>(`/api/review/timeline?run=${encodeURIComponent(runId)}`)
      .then(data => { if (alive) { setRun(data); setError(""); } })
      .catch(e => { if (alive) setError(`这一轮读不出来：${e instanceof Error ? e.message : e}`); });
    return () => { alive = false; };
  }, [runId]);

  const market = run?.market ?? {};
  const runs = list?.runs ?? [];
  return (
    <>
      <div className="desk-review-head">
        <div className="desk-review-filters">
          <label>品种 <select aria-label="品种" value={filter.asset} onChange={e => setFilter({...filter, asset: e.target.value})}>
            <option value="">全部</option>
            {(list?.assets ?? []).map(a => <option key={a} value={a}>{a}</option>)}
          </select></label>
          <label>时间 <select aria-label="时间" value={filter.days} onChange={e => setFilter({...filter, days: Number(e.target.value)})}>
            {PERIODS.map(([days, label]) => <option key={days} value={days}>{label}</option>)}
          </select></label>
          <label>选一轮 <select aria-label="选一轮" value={runId} onChange={e => setRunId(e.target.value)} disabled={!runs.length}>
            {runs.map(r => <option key={r.id} value={r.id}>{runLabel(r)}</option>)}
          </select></label>
        </div>
        {run && runs.length ? <span className="desk-review-source">行情源 {market.source || "缺失"}{market.instrument ? ` · ${market.instrument}` : ""} · 历史，只读</span> : null}
      </div>
      {error ? <div className="desk-review-kpis">{error}</div>
        : !list ? <div className="desk-review-kpis">{opened ? "读取中" : ""}</div>
        : !runs.length ? <div className="desk-review-kpis">{emptyText(filter)}</div>
        : run ? <RunView data={run} /> : <div className="desk-review-kpis">读取中</div>}
      {list ? <p className="desk-review-score">判断战绩{filter.asset || filter.days ? `（${[filter.asset, PERIODS.find(([d]) => d === filter.days && d)?.[1]].filter(Boolean).join(" · ")}）` : ""}　{score(list.score).join("　")}</p> : null}
    </>
  );
}
