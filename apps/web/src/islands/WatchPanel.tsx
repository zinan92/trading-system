/* 本周要看的三件事: reads /api/watch every 5 minutes (only while the tab is visible) and
 * re-renders every 30 seconds so 「还有 N 分钟」 counts down. */
import {useEffect, useState} from "react";
import {getJson} from "../api";
import {watchSource} from "../sources";
import {metaLine, whenLabel, type WatchData, type WatchItem} from "../watch";

export const RELOAD_MS = 300000;
export const TICK_MS = 30000;

function Card({item, fresh, now}: {item: WatchItem; fresh: boolean; now: number}) {
  const src = watchSource(item);
  return (
    <article className={`desk-watch-card ${item.volatility === "高" ? "high" : ""}${fresh ? " fresh" : ""}`}>
      <div className="w-when"><span>{whenLabel(item, now)}</span><span>波动 {item.volatility || "中"}</span></div>
      <b title={item.source_title}>{item.title}</b>
      {item.markets?.length ? <div className="w-mk">{item.markets.map(m => <span key={m}>{m}</span>)}</div> : null}
      <p>{item.why || item.source_title}</p>
      {src.source ? (
        <small className="w-src">
          来源：{src.source}
          {src.unverified.length ? <> <span className="warn">输入里没有：{src.unverified.join("、")}</span></> : null}
        </small>
      ) : null}
    </article>
  );
}

export function WatchPanel() {
  const [data, setData] = useState<WatchData | null>(null);
  const [error, setError] = useState("");
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try { const w = await getJson<WatchData>("/api/watch"); if (alive) { setData(w); setError(""); setNow(Date.now()); } }
      catch (e) { if (alive) setError(e instanceof Error ? e.message : String(e)); }
    };
    void load();
    const reload = setInterval(() => { if (!document.hidden) void load(); }, RELOAD_MS);
    const tick = setInterval(() => setNow(Date.now()), TICK_MS);
    return () => { alive = false; clearInterval(reload); clearInterval(tick); };
  }, []);

  const items = data?.items ?? [];
  const replaced = new Set(data?.replaced ?? []);
  return (
    <>
      <div className="desk-watch-head">
        <span id="deskWatchTitle">本周要看的三件事</span>
        <span className="desk-meta" style={{padding: 0}}>{error || (data ? metaLine(data) : "读取中")}</span>
      </div>
      <div className="desk-watch-row">
        {items.length
          ? items.map((item, i) => <Card key={item.ref ?? i} item={item} fresh={Boolean(item.ref && replaced.has(item.ref))} now={now} />)
          : data ? <p className="desk-empty">三件事还在生成，稍后自动刷新。</p> : null}
      </div>
    </>
  );
}
