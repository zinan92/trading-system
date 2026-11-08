/* 滚动新闻 (#1327): /api/news/{asset} every minute while the tab is visible, 「新」 on items that were not there
 * last time, and a click cites the item in today's judgment. Since #1435 it is read by 「今天」 step 1
 * (useNews) and shown inside 「今天的依据」; the reads go on while that line is folded. */
import {useEffect, useRef, useState} from "react";
import {getJson} from "../api";
import {ago, type Cite} from "../today";
import {citeStore, useCites} from "../todayStore";

export const NEWS_MS = 60000;
export const AGO_MS = 30000;
const LABEL: Record<string, string> = {high_impact: "重要", watch: "关注", noise: "噪音", unrated: "未分级"};
const CLS: Record<string, string> = {high_impact: "high", watch: "watch", noise: "noise", unrated: "noise"};

export interface NewsItem {id?: string | number; title: string; bucket?: string; source?: string; collected_at?: string}
interface NewsData {ok?: boolean; reason?: string; items?: NewsItem[]; stale?: boolean}
export interface News {items: NewsItem[]; error: string; stale: boolean; at: Date | null; fresh: Set<string | number>}

/** The list's default filter, 「重要+关注」: what 「N 条新闻」 counts too. */
export const isKey = (n: NewsItem) => n.bucket === "high_impact" || n.bucket === "watch";
const keyOf = (n: NewsItem) => n.id ?? n.title;
/** Important items that were not there on the previous read. */
export const freshImportant = (news: News) => news.items.filter(n => n.bucket === "high_impact" && news.fresh.has(keyOf(n)));

export function useNews(asset: string): News {
  const [news, setNews] = useState<News>({items: [], error: "", stale: false, at: null, fresh: new Set()});
  const [, setTick] = useState(0);
  const seen = useRef<Set<string | number> | null>(null);

  useEffect(() => {
    let alive = true;
    seen.current = null;
    setNews({items: [], error: "", stale: false, at: null, fresh: new Set()});
    const load = async (silent: boolean) => {
      let data: NewsData;
      try { data = await getJson<NewsData>(`/api/news/${asset}`); } catch (e) { data = {ok: false, reason: e instanceof Error ? e.message : String(e), items: []}; }
      if (!alive) return;
      if (!data.ok) {
        // A failed minute refresh keeps the list it had; only a first read shows the reason.
        setNews(current => (!silent || !current.items.length ? {...current, items: [], error: data.reason || "新闻读取失败"} : current));
        return;
      }
      const list = data.items || [];
      const before = seen.current;
      seen.current = new Set(list.map(keyOf));
      setNews({items: list, error: "", stale: Boolean(data.stale), at: new Date(),
        fresh: new Set(before ? list.filter(n => !before.has(keyOf(n))).map(keyOf) : [])});
    };
    void load(false);
    const reload = setInterval(() => { if (!document.hidden) void load(true); }, NEWS_MS);
    const tick = setInterval(() => { if (!document.hidden) setTick(n => n + 1); }, AGO_MS);
    return () => { alive = false; clearInterval(reload); clearInterval(tick); };
  }, [asset]);
  return news;
}

export function NewsRail({asset}: {asset?: string}) {
  if (!asset) return <Shell meta="读取中"><li className="desk-empty">正在读取新闻…</li></Shell>;
  return <Rail key={asset} asset={asset} />;
}

function Rail({asset}: {asset: string}) {
  return <NewsList asset={asset} news={useNews(asset)} />;
}

function Shell({meta, filter, onFilter, children}: {meta: string; filter?: "key" | "all"; onFilter?: (f: "key" | "all") => void; children: React.ReactNode}) {
  return (
    <>
      <div className="card-title">
        <span id="deskNewsTitle">滚动新闻</span>
        <span className="desk-seg" role="group" aria-label="新闻筛选">
          <button type="button" data-nf="key" aria-pressed={(filter ?? "key") === "key"} onClick={() => onFilter?.("key")}>重要+关注</button>
          <button type="button" data-nf="all" aria-pressed={filter === "all"} onClick={() => onFilter?.("all")}>全部</button>
        </span>
      </div>
      <div className="desk-meta"><span>点一条，引用到今日判断</span><span id="deskNewsAt">{meta}</span></div>
      <ul id="deskNews">{children}</ul>
    </>
  );
}

export function NewsList({asset, news}: {asset: string; news: News}) {
  const {items, error, stale, at, fresh} = news;
  const [filter, setFilter] = useState<"key" | "all">("key");
  const cites = useCites(asset);

  const meta = at ? `每分钟更新 · ${at.toLocaleTimeString("zh-CN", {hour: "2-digit", minute: "2-digit"})}` : "";
  if (error) return <Shell meta={meta} filter={filter} onFilter={setFilter}><li className="desk-bad">{error}</li></Shell>;
  if (!at) return <Shell meta="读取中" filter={filter} onFilter={setFilter}><li className="desk-empty">正在读取新闻…</li></Shell>;
  const shown = items.filter(n => filter === "all" || isKey(n));
  const cite = (n: NewsItem) => citeStore.toggle(asset, {id: n.id, title: n.title, bucket: n.bucket} as Cite);
  return (
    <Shell meta={meta} filter={filter} onFilter={setFilter}>
      {stale ? <li className="desk-bad">新闻服务暂时连不上，下面是上一次读到的列表。</li> : null}
      {!shown.length ? <li className="desk-empty">过去 24 小时没有{filter === "all" ? "" : "重要或关注级"}相关新闻。</li> : shown.map(n => {
        const cited = cites.some(c => c.title === n.title);
        return (
          <li key={n.id ?? n.title} className={`${CLS[n.bucket ?? ""] || "noise"}${cited ? " cited" : ""}`} data-title={n.title} tabIndex={0} role="button" aria-pressed={cited}
            onClick={() => cite(n)} onKeyDown={e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); cite(n); } }}>
            <span className="bar" />
            <div>
              <div className="t">{fresh.has(n.id ?? n.title) ? <span className="new">新</span> : null}{n.title}</div>
              <div className="m">
                <span>{ago(n.collected_at)}</span><span className={`tag ${CLS[n.bucket ?? ""] || ""}`}>{LABEL[n.bucket ?? ""] || ""}</span><span>{n.source}</span>
                {cited ? <span className="cite">已引用</span> : null}
              </div>
            </div>
          </li>
        );
      })}
    </Shell>
  );
}
