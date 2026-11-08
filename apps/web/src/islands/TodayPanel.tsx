/* 「今天」 steps 1–2 (#1327): your judgment → the plan → approve or record. Moved from gm-desk.js
 * with the same requests, wording and button rules.
 *
 * The legacy page still owns which asset is selected (its top-bar buttons are the one picker, #1433; it drives
 * GridMind's selects) and all of
 * step 3: 执行 with its confirm, 停止 and 暂停补单. This panel talks to it through window events:
 *   out  desk:desk   {asset, desk}                       every /api/desk read (step 3 needs control/meta)
 *   out  desk:exec   {asset, judgmentId, preview, done}  hand an approved preview to step 3
 *        desk:exec   {asset, clear: true}                 a new plan clears step 3
 *   out  desk:steps  {asset, direction, planKind, handed} so step 3 shows the right placeholder
 *   in   desk:reload                                     step 3 acted; read /api/desk again
 */
import {useCallback, useEffect, useRef, useState} from "react";
import {getJson, postJson} from "../api";
import {
  AI_CALL, PLAN_TAG, READING_LABEL, execRestore, firstSentence, fmtD, judgmentBody, planBody, readingDirection,
  type Asset, type Desk, type Direction, type Plan,
} from "../today";
import {citeStore, useCites} from "../todayStore";
import {AiSources} from "./AiSources";

export const REFRESH_MS = 60000;
export const ADVICE_POLL_MS = 5000;
const DIRECTIONS: Array<[Direction, string]> = [["long", "做多"], ["flat", "观望"], ["short", "做空"]];

const emit = (name: string, detail: unknown) => window.dispatchEvent(new CustomEvent(name, {detail}));
const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

export interface TodayProps {asset?: string; assets?: Asset[]; gridmind?: string[]}

/** Keyed by asset: a different asset starts a fresh panel, as the legacy focusAsset() reset it. */
export function TodayPanel({asset, assets = [], gridmind = []}: TodayProps) {
  if (!asset) return <p className="desk-meta" style={{padding: 0}}>正在读取品种…</p>;
  return <Today key={asset} asset={asset} assets={assets} gridmind={gridmind} />;
}

/** The asset is chosen in the top bar only (#1433). For one GridMind does not chart, say what this panel can do. */
export function AssetNote({asset, assets, gridmind}: {asset: string; assets: Asset[]; gridmind: string[]}) {
  if (gridmind.includes(asset)) return null;
  const a = assets.find(x => x.key === asset);
  if (!a) return null;
  // The plan decides what can be done (record_only when the venue cannot plan a grid); this only says the chart
  // and the tables below stay on GridMind's asset.
  const text = !(a.capabilities ?? []).includes("grid_plan")
    ? `${a.label}只记判断和复盘。图表和下方表格仍是 GridMind 上的品种。`
    : `${a.label}的判断和计划在这里；图表和下方表格仍是 GridMind 上的品种。`;
  return <p className="desk-asset-note" id="deskAssetNote">{text}</p>;
}

function Reading({desk, error}: {desk: Desk | null; error: string}) {
  if (error) return <div className="desk-reading" id="deskReading">{error}</div>;
  const k = desk?.kline;
  if (!desk) return <div className="desk-reading" id="deskReading" />;
  const note = desk.meta?.reading_note ? <small className="desk-reading-note" style={{display: "block", opacity: 0.7}}>{desk.meta.reading_note}</small> : null;
  if (!k?.ok) return <div className="desk-reading" id="deskReading">K 线日报：{k?.reason || "暂无"}{note}</div>;
  const dir = readingDirection(k.structure);
  return (
    <div className="desk-reading" id="deskReading">
      <b className={`d-${dir}`}>K 线日报 {READING_LABEL[dir]}</b>{firstSentence(k.synthesis)}{" "}
      <a href="/desk#news-kline" style={{color: "inherit", opacity: 0.7}}>看卡片</a>
      {note}
    </div>
  );
}

function Ai({desk, direction, adopted, onAdopt, onRefreshed}: {desk: Desk; direction: Direction | null; adopted: number | string | null;
  onAdopt: (d: Direction, id: number | string) => void; onRefreshed: () => void}) {
  const [asking, setAsking] = useState(false);
  const [note, setNote] = useState("");
  const poll = useRef<ReturnType<typeof setInterval> | null>(null);
  useEffect(() => () => { if (poll.current) clearInterval(poll.current); }, []);
  const running = Boolean(desk.ai_running) || asking;
  const refresh = async () => {
    setAsking(true); setNote("");
    try { await postJson("/api/advice/refresh", "{}"); } catch (e) { setNote(message(e)); return; }
    poll.current = setInterval(async () => {
      const st = await getJson<{running?: boolean}>("/api/advice").catch(() => null);
      if (st && !st.running) { if (poll.current) clearInterval(poll.current); poll.current = null; setAsking(false); onRefreshed(); }
    }, ADVICE_POLL_MS);
  };
  const button = (
    <button type="button" className="desk-link" id="deskAiRefresh" disabled={running} onClick={() => void refresh()}>
      {note || (running ? "AI 正在看…" : "刷新 AI 建议")}
    </button>
  );
  const ai = desk.ai_today;
  if (!ai) return <div className="desk-ai" id="deskAi"><div className="desk-ai-line"><span className="k">AI 建议</span><span className="muted">每天 08:40 生成</span>{button}</div></div>;
  const at = new Date(ai.created_at).toLocaleTimeString("zh-CN", {hour: "2-digit", minute: "2-digit"});
  return (
    <div className="desk-ai" id="deskAi">
      <div className="desk-ai-line">
        <span className="k">AI 建议</span>
        <b className={`d-${ai.direction === "flat" ? "wait" : ai.direction}`}>{AI_CALL[ai.direction]}</b>
        <span className="muted">信心 {ai.confidence}/5 · {at}</span>
        {button}
      </div>
      <p>{ai.reason}</p>
      <AiSources judgment={{id: ai.id, cited: ai.cited, evidence: ai.evidence as never}} />
      {direction === null ? <button type="button" className="desk-link" id="deskAiAdopt" onClick={() => onAdopt(ai.direction, ai.id)}>照 AI 的方向判断</button>
        : adopted != null && String(adopted) === String(ai.id) ? <span className="muted" id="deskAiAdopted">这次判断照的是这条 AI 建议</span> : null}
    </div>
  );
}

function Today({asset, assets, gridmind}: {asset: string; assets: Asset[]; gridmind: string[]}) {
  const [desk, setDesk] = useState<Desk | null>(null);
  const [deskError, setDeskError] = useState("");
  const [direction, setDirection] = useState<Direction | null>(null);
  // The AI call Park adopted (#1370): set by 「照 AI 的方向判断」, cleared by picking a direction himself.
  const [adopted, setAdopted] = useState<number | string | null>(null);
  const [reason, setReason] = useState("");
  const [confidence, setConfidence] = useState(3);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [planState, setPlanState] = useState<"idle" | "loading" | "error">("idle");
  const [planError, setPlanError] = useState("");
  const [toast, setToast] = useState("");
  const [busy, setBusy] = useState(false);
  const [handed, setHanded] = useState(false);
  const cites = useCites(asset);
  const live = useRef({direction, alive: true, planSeq: 0});
  live.current.direction = direction;

  useEffect(() => { citeStore.forAsset(asset); live.current.alive = true; return () => { live.current.alive = false; }; }, [asset]);

  const loadPlan = useCallback(async (d: Direction) => {
    const seq = ++live.current.planSeq;
    setPlan(null); setPlanState("loading"); setToast(""); setHanded(false);
    emit("desk:exec", {asset, clear: true});
    try {
      const p = await postJson<Plan>("/api/plan", planBody(asset, d));
      if (!live.current.alive || seq !== live.current.planSeq) return false;
      setPlan(p); setPlanState("idle"); return true;
    } catch (e) {
      if (live.current.alive && seq === live.current.planSeq) { setPlanError(message(e)); setPlanState("error"); }
      return false;
    }
  }, [asset]);

  const handOff = useCallback((judgmentId: number | string, preview: Record<string, unknown>, done: unknown) => {
    setHanded(true);
    emit("desk:exec", {asset, judgmentId, preview, done});
  }, [asset]);

  const loadDesk = useCallback(async () => {
    let d: Desk;
    try { d = await getJson<Desk>(`/api/desk/${asset}`); } catch (e) { if (live.current.alive) setDeskError(message(e)); return; }
    if (!live.current.alive) return;
    setDesk(d); setDeskError("");
    emit("desk:desk", {asset, desk: d});
    const tj = d.today_judgment;
    // Only while Park has not picked a direction: a later refresh never overwrites what he is typing.
    if (tj && live.current.direction === null) {
      live.current.direction = tj.direction;
      setDirection(tj.direction); setAdopted(tj.advice?.id ?? null); setReason(tj.reason || ""); setConfidence(tj.confidence);
      citeStore.set(asset, tj.cited || []);
      const restore = execRestore(d);
      const ok = await loadPlan(tj.direction);
      if (ok && restore && live.current.alive) handOff(restore.judgmentId, restore.preview, restore.done);
    }
  }, [asset, loadPlan, handOff]);

  useEffect(() => {
    void loadDesk();
    const timer = setInterval(() => { if (!document.hidden) void loadDesk(); }, REFRESH_MS);
    const reload = () => { void loadDesk(); };
    window.addEventListener("desk:reload", reload);
    return () => { clearInterval(timer); window.removeEventListener("desk:reload", reload); };
  }, [loadDesk]);

  useEffect(() => { emit("desk:steps", {asset, direction: Boolean(direction), planKind: plan?.kind ?? null, handed}); }, [asset, direction, plan, handed]);

  const pick = (d: Direction, advice: number | string | null = null) => { live.current.direction = d; setDirection(d); setAdopted(advice); void loadPlan(d); };

  const submit = async (action: "approved" | "recorded") => {
    if (!direction) return;
    setBusy(true);
    try {
      const saved = await postJson<{id: number | string; handoff?: string; preview?: Record<string, unknown>}>(
        "/api/judgments", judgmentBody(asset, direction, confidence, reason, citeStore.get().asset === asset ? citeStore.get().cites : [], action, adopted));
      if (!live.current.alive) return;
      setToast(saved.handoff ?? "");
      if (saved.preview) handOff(saved.id, saved.preview, null);
    } catch (e) { if (live.current.alive) setToast(message(e)); }
    finally { if (live.current.alive) setBusy(false); }
  };

  const done = {1: Boolean(direction), 2: handed};
  const next = !done[1] ? 1 : !done[2] ? 2 : 3;
  return (
    <ol className="desk-steps">
      <li className={`desk-step${done[1] ? " done" : ""}${next === 1 ? " on" : ""}`} data-step="1">
        <span className="desk-n" aria-hidden="true">1</span>
        <div className="desk-step-body">
          <h3>你的判断</h3>
          <AssetNote asset={asset} assets={assets} gridmind={gridmind} />
          <Reading desk={desk} error={deskError} />
          {desk ? <Ai desk={desk} direction={direction} adopted={adopted} onAdopt={pick} onRefreshed={() => void loadDesk()} /> : <div className="desk-ai" id="deskAi" />}
          <div className="desk-dir" role="group" aria-label="方向">
            {DIRECTIONS.map(([d, label]) => (
              <button key={d} type="button" data-d={d} aria-pressed={direction === d} onClick={() => pick(d)}>{label}</button>
            ))}
          </div>
          <label htmlFor="deskReason" className="desk-meta" style={{padding: 0}}>一句理由</label>
          <textarea id="deskReason" placeholder="比如：美元走强、ETF 净流出，先不追多" value={reason} onChange={e => setReason(e.target.value)} />
          <div className="desk-conf">
            <label htmlFor="deskConf">信心</label>
            <input id="deskConf" type="range" min="1" max="5" value={confidence} onChange={e => setConfidence(Number(e.target.value))} />
            <span id="deskConfV">{confidence} / 5</span>
          </div>
          <div className="desk-cites" id="deskCites">
            {cites.length ? cites.map(c => <span key={c.title} title={c.title}>{c.title.slice(0, 16)}…</span>) : "还没有引用新闻"}
          </div>
        </div>
      </li>
      <li className={`desk-step${done[2] ? " done" : ""}${next === 2 ? " on" : ""}`} data-step="2">
        <span className="desk-n" aria-hidden="true">2</span>
        <div className="desk-step-body">
          <h3>生成计划</h3>
          <div className="desk-plan" id="deskPlan">
            {planState === "loading" ? <p className="note">正在生成计划…</p>
              : planState === "error" ? <p className="warn">{planError}</p>
              : !plan ? <p className="note">先选方向，系统按它生成计划，先给你看。</p>
              : (
                <>
                  <h4><span>{plan.title}</span><span className="pill">{PLAN_TAG[plan.kind] || plan.kind}</span></h4>
                  <dl>
                    {plan.lines.map(([k, v], i) => <FactRow key={`${k}-${i}`} k={k} v={v} />)}
                    {plan.rungs ? <FactRow k="价位" v={plan.rungs.map(r => fmtD(r)).join(" / ")} /> : null}
                  </dl>
                  <p className="note">{plan.note}</p>
                  <div className="desk-row">
                    <button type="button" className="desk-btn primary" id="deskApprove" disabled={plan.kind === "unavailable" || busy}
                      onClick={() => void submit(plan.kind === "new" ? "approved" : "recorded")}>
                      {plan.kind === "new" ? "批准这份计划" : "确认并记录"}
                    </button>
                    {plan.kind === "record_only" ? null : (
                      <button type="button" className="desk-btn" id="deskRecord" disabled={busy} onClick={() => void submit("recorded")}>只记录判断</button>
                    )}
                  </div>
                  <div className="desk-toast" id="deskToast" role="status">{toast}</div>
                </>
              )}
          </div>
        </div>
      </li>
    </ol>
  );
}

function FactRow({k, v}: {k: string; v: string}) {
  return <><dt>{k}</dt><dd>{v}</dd></>;
}
