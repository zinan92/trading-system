/* 交易回看 (#1306): pick a past run, see its plan, fills, judgments and how it ended on one chart.
 * Read-only: this file calls only GET endpoints. */
"use strict";

const DeskReview = (() => {
  const BUY = "#7fb2ff", SELL = "#f0a24b", JUDGE = "#c5a8ff";
  const fmt = (value, digits = 2) => value == null ? "—" : Number(value).toLocaleString("zh-CN", {minimumFractionDigits: digits, maximumFractionDigits: digits});
  const signed = value => value == null ? "—" : `${value > 0 ? "+" : value < 0 ? "−" : ""}${fmt(Math.abs(value))}`;
  const seconds = ts => { const at = Date.parse(ts); return Number.isNaN(at) ? null : Math.floor(at / 1000); };

  function when(ts) {
    const at = new Date(ts);
    if (Number.isNaN(at.getTime())) return "时间缺失";
    const parts = new Intl.DateTimeFormat("zh-CN", {timeZone: "Asia/Shanghai", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false}).formatToParts(at);
    const get = type => parts.find(part => part.type === type)?.value || "";
    return `${get("month")}/${get("day")} ${get("hour")}:${get("minute")}`;
  }

  function runLabel(run) {
    return `${run.label} · ${when(run.started_at).split(" ")[0]} – ${when(run.ended_at).split(" ")[0]} · ${run.fill_count} 笔`;
  }

  function markers(data, snap) {
    const out = [];
    for (const fill of data.fills || []) {
      const time = seconds(fill.shown_ts);
      if (time == null) continue;
      const buy = fill.side === "buy";
      out.push({time: snap(time), position: buy ? "belowBar" : "aboveBar", shape: buy ? "arrowUp" : "arrowDown", color: buy ? BUY : SELL, text: ""});
    }
    for (const judgment of data.judgments || []) {
      const time = seconds(judgment.created_at);
      if (time != null) out.push({time: snap(time), position: "aboveBar", shape: "circle", color: JUDGE, text: judgment.author === "park" ? "Park" : "AI"});
    }
    return out;
  }

  function summary(data) {
    const result = data.result || {}, cells = [];
    if (result.net != null) cells.push({label: "这一轮结果（费后，未含资金费）", value: `${signed(result.net)} USDT`, big: true, bad: result.net < 0});
    else if (!result.flat) cells.push({label: "这一轮结果", value: "成交记录没有轧平，算不出结果", big: true, bad: true});
    else cells.push({label: "这一轮结果（费前，未含资金费）", value: `${signed(result.gross)} USDT`, big: true, bad: result.gross < 0});
    cells.push({label: `${data.run?.environment === "testnet" ? "Testnet" : "Paper"} 成交`, value: `${result.fill_count} 笔（开 ${result.entries} · 平 ${result.exits}）`});
    if (result.fees != null) cells.push({label: "手续费", value: `${fmt(result.fees)} USDT`});
    cells.push({label: "结束原因", value: data.run?.reason || "原因见原始记录"});
    cells.push({label: "结束时", value: result.ended_flat === true ? "已空仓" : result.ended_flat === false ? "未空仓" : "记录缺失", bad: result.ended_flat !== true});
    return cells;
  }

  function score(board) {
    const row = (name, item) => `${name}：${item.total ? `命中 ${item.hit} · 未中 ${item.miss} · 持平 ${item.even} · 无法核对 ${item.unverifiable} · 待定 ${item.pending}` : "0 条"}`;
    return [row("AI", board.ai), row("Park", board.park)];
  }

  return {markers, runLabel, score, signed, summary, when};
})();

if (typeof module !== "undefined") module.exports = DeskReview;

if (typeof document !== "undefined") (() => {
  const esc = value => String(value ?? "").replace(/[&<>"']/g, ch => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[ch]));
  const get = async url => { const response = await fetch(url, {cache: "no-store"}); if (!response.ok) throw new Error(`HTTP ${response.status}`); return response.json(); };
  let root, chart, loaded = false, shown = null;

  function mount() {
    const old = document.querySelector('[data-panel="review"]');
    if (!old || document.querySelector("#deskReview")) return;
    old.dataset.panel = "review-legacy";  // GridMind keeps rewriting #review; it just is not shown any more.
    old.insertAdjacentHTML("afterend", `<div class="tab card-body desk-review" data-panel="review" id="deskReview">
      <div class="desk-review-head"><label>选一轮 <select id="deskRunPick" aria-label="选一轮"></select></label><span class="desk-review-source" id="deskRunSource"></span></div>
      <div class="desk-review-kpis" id="deskRunKpis"></div>
      <div class="desk-review-missing hidden" id="deskRunMissing" role="status"></div>
      <div class="desk-review-body"><div><div id="deskRunChart"></div><p class="desk-review-legend" id="deskRunLegend"></p></div>
        <div><ol class="desk-review-events" id="deskRunEvents"></ol><pre class="desk-review-record" id="deskRunRecord" aria-live="polite"></pre></div></div>
      <p class="desk-review-score" id="deskRunScore"></p></div>`);
    root = document.querySelector("#deskReview");
    root.querySelector("#deskRunPick").addEventListener("change", event => show(event.target.value));
    root.querySelector("#deskRunEvents").addEventListener("click", event => {
      const button = event.target.closest("[data-event]");
      if (button) pick(Number(button.dataset.event));
    });
  }

  function pick(index) {
    const item = shown?.events?.[index];
    if (!item) return;
    root.querySelectorAll("[data-event]").forEach(button => button.setAttribute("aria-pressed", String(Number(button.dataset.event) === index)));
    root.querySelector("#deskRunRecord").textContent = `${item.source}\n${item.ts || "时间缺失"}\n${JSON.stringify(item.record, null, 1)}`;
  }

  function draw(data) {
    shown = data;
    const market = data.market || {}, backdated = (data.fills || []).filter(fill => fill.backdated).length;
    root.querySelector("#deskRunSource").textContent = `行情源 ${market.source || "缺失"}${market.instrument ? ` · ${market.instrument}` : ""} · 历史，只读`;
    root.querySelector("#deskRunKpis").innerHTML = DeskReview.summary(data).map(cell => `<div class="${cell.bad ? "bad" : ""}"><small>${esc(cell.label)}</small><b class="${cell.big ? "big" : ""}">${esc(cell.value)}</b></div>`).join("");
    const missing = root.querySelector("#deskRunMissing");
    missing.classList.toggle("hidden", !(data.missing || []).length);
    missing.textContent = (data.missing || []).length ? `这段记录缺失：${data.missing.join("、")}。缺的部分没有补，也没有推算。` : "";
    root.querySelector("#deskRunEvents").innerHTML = (data.events || []).map((item, index) => `<li><button type="button" data-event="${index}" aria-pressed="false"><small>${esc(DeskReview.when(item.ts))}</small>${esc(item.title)}</button></li>`).join("");
    root.querySelector("#deskRunRecord").textContent = "点一条，看原始记录";
    const judgments = (data.judgments || []).length;
    root.querySelector("#deskRunLegend").textContent = `▲ 开仓 · ▼ 平仓 · K 线 1 小时 · ${judgments ? `${judgments} 条判断` : "这一轮没有判断记录"}${backdated ? ` · 有 ${backdated} 笔账本时间是回填的，图上按实际结算时间画` : ""}`;
    const host = root.querySelector("#deskRunChart");
    if (!window.StandardKline) { host.textContent = "K 线组件没有加载"; return; }
    if (!chart) chart = new StandardKline.StandardKlineChart(host, {height: 400, minHeight: 300, showVolume: false, timeZone: "Asia/Shanghai", locale: "zh-CN"});
    const payload = {bars: market.bars || [], symbol: market.instrument || "", timeframe: market.timeframe || "1h", provider: market.source || ""};
    const times = StandardKline.adaptBarPayload(payload).candles.map(candle => candle.time);
    const snap = time => times.length ? StandardKline.nearestTime(times, time) ?? time : time;
    chart.setPayload(payload, {
      markers: DeskReview.markers(data, snap),
      priceLines: (data.lines || []).map(line => ({price: Number(line.price), title: line.title, color: line.title === "止损" ? "#f0a24b" : "#8b95a3", lineStyle: 2, lineWidth: 1})),
    });
  }

  async function show(id) {
    try { draw(await get(`/api/review/timeline?run=${encodeURIComponent(id)}`)); }
    catch (error) { root.querySelector("#deskRunKpis").textContent = `这一轮读不出来：${error.message}`; }
  }

  async function load() {
    if (loaded) return;
    loaded = true;
    try {
      const {runs, score} = await get("/api/review/runs");
      root.querySelector("#deskRunPick").innerHTML = runs.map(run => `<option value="${esc(run.id)}">${esc(DeskReview.runLabel(run))}</option>`).join("");
      root.querySelector("#deskRunScore").textContent = `判断战绩　${DeskReview.score(score).join("　")}`;
      if (runs.length) await show(runs[0].id);
      else root.querySelector("#deskRunKpis").textContent = "还没有跑过的记录";
    } catch (error) {
      loaded = false;
      root.querySelector("#deskRunKpis").textContent = `回看读不出来：${error.message}`;
    }
  }

  function boot() {
    mount();
    document.querySelector("#tabs")?.addEventListener("click", event => { if (event.target.closest('[data-tab="review"]')) load(); });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot); else boot();
})();
