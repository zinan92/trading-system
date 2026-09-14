const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmt = (n, d) => n == null || isNaN(n) ? "—" : Number(n).toLocaleString("en-US", {minimumFractionDigits: d ?? (Math.abs(n) >= 1000 ? 0 : 2), maximumFractionDigits: d ?? (Math.abs(n) >= 1000 ? 1 : 2)});
const signed = (n, d = 2) => n == null ? "—" : `<span class="${n > 0 ? "pos" : n < 0 ? "neg" : ""}">${n > 0 ? "+" : ""}${fmt(n, d)}</span>`;
const DIR = {long: "做多", short: "做空", flat: "观望"};
const STEPS = [["look","看"],["judge","判断"],["plan","计划"],["approve","批准"],["watch","盯"],["review","复盘"]];
const S = {asset: "BTC", tf: "4h", filter: "key", d: null, cites: [], desk: null, news: [], bars: null, plan: null, assets: [], page: "trade", nl: "morning", pending: null};
try { const saved = JSON.parse(localStorage.getItem("desk-ui") || "{}"); if (saved.asset) S.asset = saved.asset; if (saved.tf) S.tf = saved.tf; } catch (e) {}
const qsAsset = new URLSearchParams(location.search).get("asset"); if (qsAsset) S.asset = qsAsset.toUpperCase();
const persistUi = () => { try { localStorage.setItem("desk-ui", JSON.stringify({asset: S.asset, tf: S.tf})); } catch (e) {} };

async function api(path, body, method) {
  const opts = method ? {method} : body ? {method: "POST"} : {};
  if (body) { opts.headers = {"content-type": "application/json"}; opts.body = JSON.stringify(body); }
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : "请求失败，请稍后再试");
  return data;
}

// ---- accounts ------------------------------------------------------------
async function loadAccounts() {
  let data;
  try { data = await api("/api/accounts"); } catch (e) { $("#accts").innerHTML = `<div class="panel degraded">持仓盈亏读不到：${esc(e.message)}</div>`; return; }
  $("#accts").innerHTML = data.accounts.map(a => {
    if (!a.ok) return `<div class="panel acct"><div class="name">${esc(a.venue)}</div><div class="degraded" style="grid-column:2/-1;padding:0">${esc(a.reason)}</div></div>`;
    if (a.note) return `<div class="panel acct"><div class="name">${esc(a.venue)}<small>${esc(a.money)}</small></div><div class="empty" style="grid-column:2/-1;padding:0">${esc(a.note)}</div></div>`;
    const rec = a.reconciliation === "ok" ? "" : `<span class="pill bad">对账异常，数字可能不准</span>`;
    const pos = a.positions.length ? `${a.positions.length} 笔持仓` : "无持仓";
    return `<div class="panel acct">
      <div class="name">${esc(a.venue)}<small>${esc(a.money)} · ${pos} ${rec}</small></div>
      <div><div class="k">权益</div><div class="v num">${fmt(a.equity, 2)}</div></div>
      <div><div class="k">浮动盈亏</div><div class="v num">${signed(a.unrealized)}</div></div>
      <div><div class="k">已实现盈亏（累计）</div><div class="v num">${signed(a.realized)}</div></div>
      <div><div class="k">${a.starting_cash ? "起始资金" : "成交笔数"}</div><div class="v num">${a.starting_cash ? fmt(a.starting_cash, 0) : fmt(a.trades, 0)}</div></div>
    </div>`;
  }).join("");
}

// ---- desk ----------------------------------------------------------------
async function loadDesk() {
  try { S.desk = await api(`/api/desk/${S.asset}`); } catch (e) { $("#status").innerHTML = `<div class="degraded">${esc(e.message)}</div>`; return; }
  const d = S.desk;
  $("#venue").textContent = `${d.meta.label} 现价 · ${d.meta.venue}`;
  $("#px").textContent = fmt(d.price, S.asset === "BTC" ? 1 : 2);
  $("#steps").innerHTML = STEPS.map(([k, label], i) => `<li class="${d.steps[k] ? "done" : ""}"><b>${i + 1}</b>${label}</li>`).join("");
  $("#today").textContent = new Date().toLocaleDateString("zh-CN", {month: "long", day: "numeric"});
  renderStatus(); renderNotes(); renderKline();
  const tj = d.today_judgment;
  if (tj && S.d === null) {
    S.d = tj.direction; $("#reason").value = tj.reason || ""; $("#conf").value = tj.confidence; $("#confv").textContent = `${tj.confidence} / 5`; S.cites = tj.cited || []; syncDir(); renderCites();
    const runs = (d.executions || []).filter(e => e.judgment_id === tj.id);
    const preview = runs.find(e => e.stage === "preview"), done = runs.find(e => ["executed", "execute_started", "execute_failed", "refused"].includes(e.stage));
    loadPlan().then(() => { if (tj.action === "approved" && preview) renderExec(tj.id, preview.detail, done); });
  }
  renderChart();
}

function renderStatus() {
  const g = S.desk.grid;
  if (!g.ok) { $("#status").innerHTML = `<div class="${g.none ? "empty" : "degraded"}">${esc(g.reason || "网格状态读不到")}</div>`; $("#legend").innerHTML = ""; return; }
  const tone = /卡住|止损/.test(g.status_label) ? "bad" : /暂停|高于|低于/.test(g.status_label) ? "warn" : /结束|停止/.test(g.status_label) ? "idle" : "ok";
  const updated = g.updated_at ? new Date(g.updated_at).toLocaleTimeString("zh-CN", {hour: "2-digit", minute: "2-digit"}) : "—";
  $("#status").innerHTML = `
    <div><div class="k">网格状态</div><div class="v"><span class="pill ${tone}">${esc(g.status_label)}</span></div></div>
    <div><div class="k">方向 · 区间</div><div class="v num">${DIR[g.direction] || "—"} ${fmt(g.lower)}–${fmt(g.upper)}</div></div>
    <div><div class="k">挂单 / 成交</div><div class="v num">${fmt(g.open_orders, 0)} / ${fmt(g.fills, 0)}</div></div>
    <div><div class="k">硬止损</div><div class="v num">${fmt(g.hard_stop)}</div></div>
    <div><div class="k">系统上次检查</div><div class="v num">${updated}</div></div>${stopControl(S.desk.control)}`;
  const stop = $("#stop-btn");
  if (stop) stop.onclick = async () => {
    if (!confirm(`确认停止 ${S.asset} 测试盘网格？\n会撤掉全部挂单；如果有持仓，会按市价平掉。`)) return;
    stop.disabled = true;
    try { const r = await api("/api/grid/stop", {asset: S.asset, confirm_text: "停止"}); $("#stop-toast").textContent = r.message; setTimeout(loadDesk, 1500); }
    catch (e) { $("#stop-toast").textContent = e.message; stop.disabled = false; }
  };
  $("#legend").innerHTML = `<span><i style="border-color:var(--brass);border-top-style:dashed"></i>网格价位（${g.rungs.length} 格）</span>
    <span><i style="border-color:var(--ink-2)"></i>区间 ${fmt(g.lower)} – ${fmt(g.upper)}</span>
    <span><i style="border-color:var(--warn)"></i>硬止损 ${fmt(g.hard_stop)}</span><span>红涨绿跌</span>`;
}

function stopControl(c) {
  if (!c) return "";
  if (c.stopping) return `<div class="stop"><span class="pill warn">正在停止：撤单、平仓中，约 1–2 分钟</span></div>`;
  if (c.ended) return `<div class="stop"><span class="pill idle">网格已结束，可以执行新计划</span></div>`;
  if (!c.can_stop) return "";
  return `<div class="stop"><button type="button" class="btn-stop" id="stop-btn">停止网格</button><div class="toast" id="stop-toast" role="status"></div></div>`;
}

// ---- news ----------------------------------------------------------------
async function loadNews() {
  $("#news").innerHTML = `<li class="empty">正在读取新闻…</li>`;
  let data;
  try { data = await api(`/api/news/${S.asset}`); } catch (e) { data = {ok: false, reason: e.message, items: []}; }
  if (!data.ok) { $("#news").innerHTML = `<li class="degraded">${esc(data.reason)}</li>`; S.news = []; return; }
  S.news = data.items; renderNews();
  if (data.stale) $("#news").insertAdjacentHTML("afterbegin", `<li class="degraded">新闻服务暂时连不上，下面是上一次读到的列表。</li>`);
}
function renderNews() {
  const label = {high_impact: "重要", watch: "关注", noise: "噪音", unrated: "未分级"};
  const cls = {high_impact: "high", watch: "watch", noise: "noise", unrated: "noise"};
  const items = S.news.filter(n => S.filter === "all" || n.bucket === "high_impact" || n.bucket === "watch");
  if (!items.length) { $("#news").innerHTML = `<li class="empty">今天暂时没有${S.filter === "all" ? "" : "重要或关注级"}相关新闻。</li>`; return; }
  $("#news").innerHTML = items.map(n => {
    const cited = S.cites.some(c => c.title === n.title);
    const time = n.collected_at ? new Date(n.collected_at + (/[Z+]/.test(n.collected_at.slice(19)) ? "" : "Z")).toLocaleTimeString("zh-CN", {hour: "2-digit", minute: "2-digit"}) : "";
    return `<li class="${cls[n.bucket]}${cited ? " cited" : ""}" data-title="${esc(n.title)}" tabindex="0" role="button" aria-pressed="${cited}">
      <span class="bar"></span><div><div class="t">${esc(n.title)}</div>
      <div class="m"><span class="tag ${cls[n.bucket]}">${label[n.bucket]}</span><span>${esc(n.topic)}</span><span>${esc(n.source)}</span><span class="num">${time}</span>${cited ? '<span class="cite">已引用</span>' : ""}</div></div></li>`;
  }).join("");
}
function toggleCite(title) {
  const k = S.cites.findIndex(c => c.title === title);
  if (k >= 0) S.cites.splice(k, 1); else { const n = S.news.find(x => x.title === title); if (n) S.cites.push({id: n.id, title: n.title, bucket: n.bucket}); }
  renderNews(); renderCites();
}
function renderCites() { $("#cites").innerHTML = S.cites.length ? S.cites.map(c => `<span>${esc(c.title.slice(0, 18))}…</span>`).join("") : "<em>还没有引用</em>"; }

// ---- chart ---------------------------------------------------------------
async function loadBars() {
  $("#chart").innerHTML = ""; $("#chart-src").textContent = "正在读取 K 线…";
  try { S.bars = await api(`/api/bars/${S.asset}?tf=${S.tf}`); } catch (e) { S.bars = {ok: false, reason: e.message, bars: []}; }
  renderChart();
  if (S.bars.ok && S.bars.bars.length) {
    const daily = S.tf === "1d" ? S.bars.bars : null;
    const px = S.desk?.price, open = daily ? daily[daily.length - 1][1] : null;
    if (px && open) { const c = (px - open) / open * 100; $("#chg").innerHTML = signed(c) + "%"; }
  }
}
async function loadDayChange() {
  try { const r = await api(`/api/bars/${S.asset}?tf=1d`); const px = S.desk?.price; if (r.ok && px) { const o = r.bars[r.bars.length - 1][1]; $("#chg").innerHTML = signed((px - o) / o * 100) + "%"; } } catch (e) {}
}
function renderChart() {
  const svg = $("#chart"), b = S.bars, g = S.desk?.grid;
  if (!b) return;
  if (!b.ok || !b.bars.length) { svg.innerHTML = `<text x="380" y="210" text-anchor="middle" fill="var(--warn)" style="font-family:'Noto Sans SC'">${esc(b.reason || "K 线暂时没有数据")}</text>`; $("#chart-src").textContent = b.source ? `来源：${b.source}` : ""; return; }
  $("#chart-src").textContent = `来源：${b.source}${b.fresh ? "" : " · 数据不是最新"}`;
  const c = b.bars, W = 760, H = 420, L = 8, R = 76, T = 14, B = 26;
  const levels = g && g.ok ? [...g.rungs, g.hard_stop, g.upper, g.lower].filter(v => v != null) : [];
  const price = S.desk?.price;
  let lo = Math.min(...c.map(x => x[3]), ...levels), hi = Math.max(...c.map(x => x[2]), ...levels, price || -Infinity);
  const pad = (hi - lo) * 0.04; lo -= pad; hi += pad;
  const y = v => T + (hi - v) / (hi - lo) * (H - T - B), step = (W - L - R) / c.length, cw = Math.max(2, step * .62);
  const raw = (hi - lo) / 6, mag = Math.pow(10, Math.floor(Math.log10(raw))), tickStep = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw);
  let s = "";
  for (let v = Math.ceil(lo / tickStep) * tickStep; v <= hi; v += tickStep) s += `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="var(--line)"/><text x="${W - R + 6}" y="${y(v) + 4}" fill="var(--ink-3)">${fmt(v, v >= 1000 ? 0 : 1)}</text>`;
  if (g && g.ok) {
    g.rungs.forEach(r => s += `<line x1="${L}" x2="${W - R}" y1="${y(r)}" y2="${y(r)}" stroke="var(--brass)" stroke-dasharray="5 4" stroke-width="1.1"/>`);
    [g.upper, g.lower].forEach(v => v != null && (s += `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="var(--ink-2)" stroke-width="1.2"/>`));
    if (g.hard_stop != null) s += `<line x1="${L}" x2="${W - R}" y1="${y(g.hard_stop)}" y2="${y(g.hard_stop)}" stroke="var(--warn)" stroke-width="1.6"/>`;
  }
  c.forEach((k, i) => { const [, o, h, l, cl] = k, x = L + i * step + step / 2, col = cl >= o ? "var(--up)" : "var(--down)";
    s += `<line x1="${x}" x2="${x}" y1="${y(h)}" y2="${y(l)}" stroke="${col}"/><rect x="${x - cw / 2}" y="${y(Math.max(o, cl))}" width="${cw}" height="${Math.max(1, Math.abs(y(o) - y(cl)))}" fill="${col}"/>`; });
  if (price) { const py = y(price); s += `<line x1="${L}" x2="${W - R}" y1="${py}" y2="${py}" stroke="var(--ink)" stroke-dasharray="2 3"/><rect x="${W - R + 2}" y="${py - 10}" width="${R - 4}" height="20" rx="3" fill="var(--ink)"/><text x="${W - R + 6}" y="${py + 4}" fill="var(--panel)">${fmt(price, price >= 1000 ? 0 : 1)}</text>`; }
  const every = Math.ceil(c.length / 7);
  c.forEach((k, i) => { if (i % every === 0) { const d = new Date(k[0]); const lbl = S.tf === "1d" ? `${d.getMonth() + 1}/${d.getDate()}` : `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, "0")}时`; s += `<text x="${L + i * step + step / 2}" y="${H - 8}" fill="var(--ink-3)" text-anchor="middle">${lbl}</text>`; } });
  svg.innerHTML = s;
}

// ---- judgment + plan -----------------------------------------------------
function syncDir() { document.querySelectorAll("[data-d]").forEach(b => b.setAttribute("aria-pressed", b.dataset.d === S.d)); }
async function loadPlan() {
  if (!S.d) return;
  $("#plan").innerHTML = `<div class="empty" style="padding:0">正在生成计划…</div>`;
  try { S.plan = await api("/api/plan", {asset: S.asset, direction: S.d}); } catch (e) { $("#plan").innerHTML = `<div class="degraded" style="padding:0">${esc(e.message)}</div>`; return; }
  const p = S.plan, tag = {keep: ["high", "沿用"], new: ["watch", "需要批准"], hold: ["noise", "不开新单"], unavailable: ["noise", "暂不可用"]}[p.kind];
  const rungs = p.rungs ? `<dt>价位</dt><dd class="rungs num">${p.rungs.map(r => `<span>${fmt(r)}</span>`).join("")}</dd>` : "";
  $("#plan").innerHTML = `<h3><span>${esc(p.title)}</span><span class="tag ${tag[0]}">${tag[1]}</span></h3>
    <dl class="kv">${p.lines.map(([k, v]) => `<dt>${esc(k)}</dt><dd class="num">${esc(v)}</dd>`).join("")}${rungs}</dl>
    <p class="note">${esc(p.note)}</p>
    <div class="approve"><button type="button" class="btn-primary" id="approve" ${p.kind === "unavailable" ? "disabled" : ""}>${p.kind === "new" ? "批准这份计划" : "确认并记录"}</button><button type="button" class="btn-ghost" id="record">只记录判断</button></div>
    <div class="toast" id="toast" role="status"></div>`;
  $("#approve").onclick = () => submit(p.kind === "new" ? "approved" : "recorded");
  $("#record").onclick = () => submit("recorded");
}
async function submit(action) {
  if (!S.d) return;
  const btns = document.querySelectorAll(".approve button"); btns.forEach(b => b.disabled = true);
  try {
    const saved = await api("/api/judgments", {asset: S.asset, direction: S.d, confidence: +$("#conf").value, reason: $("#reason").value, cited: S.cites, action});
    $("#toast").textContent = saved.handoff;
    if (saved.preview) renderExec(saved.id, saved.preview, null);
    loadReview(); refreshSteps();
  } catch (e) { $("#toast").textContent = e.message; }
  finally { btns.forEach(b => b.disabled = false); }
}
function renderNotes() {
  const notes = S.desk.notes || [];
  $("#notes").innerHTML = notes.map(n => `<li><time>${new Date(n.created_at).toLocaleString("zh-CN", {month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit"})}</time>${esc(n.body)}</li>`).join("") || `<li>还没有记录</li>`;
}

// ---- review --------------------------------------------------------------
async function loadReview() {
  let data;
  try { data = await api("/api/review"); } catch (e) { $("#review").innerHTML = `<tr><td colspan="9" class="degraded">${esc(e.message)}</td></tr>`; return; }
  const sm = data.summary;
  $("#review-summary").textContent = sm.resolved ? `已到期 ${sm.resolved} 条，说中 ${sm.hits} 条 · 待验证 ${sm.pending} 条` : `判断记录 ${sm.pending} 条，${data.review_hours} 小时后自动对照价格`;
  const OUT = {hit: ["outcome-hit", "✓ 说中了"], miss: ["outcome-miss", "✗ 没说中"], even: ["outcome-even", "△ 基本没动"], unverifiable: ["outcome-miss", "无法验证：到期时的行情已查不到"]};
  $("#review").innerHTML = data.items.length ? data.items.map(j => {
    const o = j.outcome ? OUT[j.outcome] : ["", `等 ${data.review_hours} 小时`];
    return `<tr><td class="num">${new Date(j.created_at).toLocaleString("zh-CN", {month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit"})}</td>
      <td>${esc((S.assets.find(a => a.key === j.asset) || {}).label || j.asset)}</td><td class="d-${j.direction}">${DIR[j.direction]}</td><td class="num">${j.confidence}/5</td>
      <td>${esc(j.reason) || "—"}${j.cited.length ? `<div class="meta" style="font-size:11.5px;color:var(--ink-3)">引用 ${j.cited.length} 条新闻</div>` : ""}</td>
      <td>${j.action === "approved" ? "已批准" : "只记录"}</td><td class="num">${fmt(j.price_at)}</td><td class="num">${j.price_after ? fmt(j.price_after) + ` (${j.move_pct > 0 ? "+" : ""}${j.move_pct.toFixed(2)}%)` : "—"}</td>
      <td class="${o[0]}">${o[1]}</td></tr>`;
  }).join("") : `<tr><td colspan="9" class="empty">还没有判断记录。在右上方选方向、写理由，记录后会出现在这里。</td></tr>`;
}

// ---- wiring --------------------------------------------------------------
function selectAsset(asset) {
  S.asset = asset; S.d = null; S.cites = []; S.plan = null; persistUi();
  document.querySelectorAll("[data-asset]").forEach(b => b.setAttribute("aria-pressed", b.dataset.asset === asset));
  $("#kline").innerHTML = "";
  $("#reason").value = ""; $("#conf").value = 3; $("#confv").textContent = "3 / 5"; syncDir(); renderCites();
  $("#plan").innerHTML = `<div class="empty" style="padding:0">先选一个方向，系统按它生成计划。</div>`;
  loadDesk().then(() => { loadBars(); loadDayChange(); }); loadNews();
}
document.querySelectorAll("[data-tf]").forEach(b => b.onclick = () => { S.tf = b.dataset.tf; persistUi(); document.querySelectorAll("[data-tf]").forEach(x => x.setAttribute("aria-pressed", x === b)); loadBars(); });
document.querySelectorAll("[data-f]").forEach(b => b.onclick = () => { S.filter = b.dataset.f; document.querySelectorAll("[data-f]").forEach(x => x.setAttribute("aria-pressed", x === b)); renderNews(); });
document.querySelectorAll("[data-d]").forEach(b => b.onclick = () => { S.d = b.dataset.d; syncDir(); loadPlan(); });
$("#news").addEventListener("click", e => { const li = e.target.closest("li[data-title]"); if (li) toggleCite(li.dataset.title); });
$("#news").addEventListener("keydown", e => { const li = e.target.closest("li[data-title]"); if (li && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); toggleCite(li.dataset.title); } });
$("#conf").oninput = () => { $("#confv").textContent = `${$("#conf").value} / 5`; };
$("#note-form").onsubmit = async e => { e.preventDefault(); const body = $("#note").value.trim(); if (!body) return; try { await api("/api/notes", {asset: S.asset, body}); $("#note").value = ""; loadDesk(); } catch (err) { alert(err.message); } };
document.querySelectorAll("[data-tf]").forEach(x => x.setAttribute("aria-pressed", x.dataset.tf === S.tf));
loadAssets().then(() => selectAsset(S.asset)).catch(e => alert(e.message)); loadAccounts(); loadReview();
setInterval(() => { loadAccounts(); if (S.page === "trade") loadDesk(); if (S.page === "system") loadSystem(); }, 60000);

async function refreshSteps() {
  try { const d = await api(`/api/desk/${S.asset}`); $("#steps").innerHTML = STEPS.map(([k, label], i) => `<li class="${d.steps[k] ? "done" : ""}"><b>${i + 1}</b>${label}</li>`).join(""); S.desk.executions = d.executions; } catch (e) {}
}

// ---- K-line daily reading ---------------------------------------------------------
function renderKline() {
  const k = S.desk.kline;
  if (!k || !k.ok) { $("#kline").innerHTML = `<div class="src">K 线日报解读：${esc(k?.reason || "暂无")}</div>`; return; }
  $("#kline").innerHTML = `<div class="src">K 线日报解读 · ${esc(k.date)}（模型生成，未经人工复核）</div>
    <div class="row">${esc(k.position)}</div><div class="row">${esc(k.structure)}</div><div><b>${esc(k.synthesis)}</b></div>
    ${k.periods.length ? `<details><summary>分周期看</summary>${k.periods.map(p => `<div class="row">${esc(p.label)}：${esc(p.text)}</div>`).join("")}</details>` : ""}`;
}

// ---- execution gate ---------------------------------------------------------------
function renderExec(judgmentId, preview, done) {
  const plan = $("#plan"); plan.querySelector(".exec")?.remove();
  const box = document.createElement("div"); box.className = "exec";
  if (done) {
    const txt = {executed: "已执行：网格已挂上测试盘。", execute_started: "执行中或上次中断，请到系统页查看记录。", execute_failed: "上次执行没有完成，请到系统页查看记录。", refused: `上次执行被拒绝：${done.detail?.reason || ""}`}[done.stage];
    box.innerHTML = `<div class="note">${esc(txt)}</div>`; plan.appendChild(box); return;
  }
  if (!preview.execution_ready) { box.innerHTML = `<div class="warn">预览没通过，不能执行：${esc((preview.blockers || []).join("；"))}</div>`; plan.appendChild(box); return; }
  const orders = (preview.orders || []).map(o => `<span>${fmt(o.price)}</span>`).join("");
  box.innerHTML = `<div class="kv"><dt>交易所预览价位</dt><dd class="rungs num">${orders}</dd><dt>预览最多亏</dt><dd class="num">${fmt(preview.max_loss)} USDC</dd></div>
    <div class="warn">按下后会在 Hyperliquid 测试盘真实挂单（假钱）。已有网格在跑时系统会拒绝。</div>
    <button type="button" class="btn-exec" id="exec-btn">执行</button><div class="toast" id="exec-toast" role="status"></div>`;
  plan.appendChild(box);
  $("#exec-btn").onclick = async () => {
    if (!confirm(`确认在测试盘执行这份计划？\n最多亏 ${fmt(preview.max_loss)} USDC`)) return;
    $("#exec-btn").disabled = true; $("#exec-toast").textContent = "正在复核预览并下单，可能需要一两分钟…";
    try { const r = await api("/api/execute", {judgment_id: judgmentId, shown_max_loss: preview.max_loss, confirm_text: "执行"}); $("#exec-toast").textContent = r.message; loadDesk(); }
    catch (e) { $("#exec-toast").textContent = e.message; }
  };
}

// ---- assets ---------------------------------------------------------------------------
async function loadAssets() {
  S.assets = (await api("/api/assets")).assets;
  if (!S.assets.some(a => a.key === S.asset)) S.asset = S.assets[0].key;
  $("#assets").innerHTML = S.assets.map(a => `<button type="button" data-asset="${esc(a.key)}" aria-pressed="${a.key === S.asset}">${esc(a.label)}</button>`).join("") + `<button type="button" class="add" id="add-asset">＋ 品种</button>`;
  document.querySelectorAll("[data-asset]").forEach(b => b.onclick = () => { showPage("trade"); selectAsset(b.dataset.asset); });
  $("#add-asset").onclick = openAdd;
}
let catalog = [], picked = null;
async function openAdd() {
  $("#add-dialog").showModal(); picked = null; $("#add-submit").disabled = true; $("#add-toast").textContent = "正在读取交易所品种…";
  try { const c = await api("/api/catalog"); if (!c.ok) throw new Error(c.reason); catalog = c.instruments; $("#add-toast").textContent = `共 ${catalog.length} 个品种`; renderPick(); }
  catch (e) { $("#add-toast").textContent = e.message; }
}
function renderPick() {
  const q = $("#add-search").value.trim().toUpperCase();
  const list = catalog.filter(i => !q || i.coin.toUpperCase().includes(q)).slice(0, 60);
  $("#add-list").innerHTML = list.map(i => `<li><button type="button" data-coin="${esc(i.coin)}" aria-pressed="${picked === i.coin}" ${i.added ? "disabled title='已添加'" : ""}>${esc(i.coin)}</button></li>`).join("") || `<li class="empty">没有匹配的品种</li>`;
  $("#add-list").querySelectorAll("button[data-coin]").forEach(b => b.onclick = () => { picked = b.dataset.coin; $("#add-news").value = $("#add-news").value || `${picked}, 加密, 美联储`; $("#add-submit").disabled = false; renderPick(); });
}
$("#add-search").oninput = renderPick;
$("#add-submit").onclick = async () => {
  if (!picked) return;
  const news = $("#add-news").value.split(/[,，]/).map(s => s.trim()).filter(Boolean);
  try { const a = await api("/api/assets", {coin: picked, news_queries: news}); $("#add-dialog").close(); $("#add-news").value = ""; $("#add-search").value = ""; S.asset = a.key; await loadAssets(); selectAsset(a.key); loadSystem(); }
  catch (e) { $("#add-toast").textContent = e.message; }
};

// ---- pages -----------------------------------------------------------------------------
function showPage(page) {
  S.page = page;
  document.querySelectorAll("[data-page]").forEach(b => b.setAttribute("aria-selected", b.dataset.page === page));
  $("#page-trade").hidden = page !== "trade"; $("#page-news").hidden = page !== "news"; $("#page-system").hidden = page !== "system";
  for (const id of ["#assets", "#steps", ".ticker"]) document.querySelector(id).style.visibility = page === "trade" ? "visible" : "hidden";
  if (page === "news") loadNewsletters();
  if (page === "system") loadSystem();
}
document.querySelectorAll("[data-page]").forEach(b => b.onclick = () => showPage(b.dataset.page));

async function loadNewsletters() {
  try {
    const n = await api("/api/newsletters");
    $("#nl-archive").innerHTML = `<option value="">选择日期</option>` + n.morning.archive.map(d => `<option value="${d}">${d}</option>`).join("");
    const meta = {morning: n.morning.latest, kline: n.kline.latest, weekly: n.weekly.latest, card: "打开时实时生成"}[S.nl];
    $("#nl-meta").textContent = S.nl === "card" ? meta : meta ? `更新于 ${meta}` : "还没有生成";
  } catch (e) { $("#nl-meta").textContent = e.message; }
  if ($("#nl-frame").getAttribute("src") === "about:blank") $("#nl-frame").src = `/newsletter/${S.nl}`;
}
document.querySelectorAll("[data-nl]").forEach(b => b.onclick = () => { S.nl = b.dataset.nl; document.querySelectorAll("[data-nl]").forEach(x => x.setAttribute("aria-pressed", x === b)); $("#nl-archive").value = ""; $("#nl-frame").src = `/newsletter/${S.nl}`; loadNewsletters(); });
$("#nl-archive").onchange = () => { if ($("#nl-archive").value) $("#nl-frame").src = `/newsletter/${$("#nl-archive").value}`; };

const STAGE = {preview: "预览", execute_started: "按下执行", executed: "已执行", execute_failed: "执行未完成", refused: "被拒绝", stop_requested: "按下停止", stop_refused: "停止被拒绝"};
async function loadSystem() {
  let s;
  try { s = await api("/api/system"); } catch (e) { $("#checks").innerHTML = `<li class="degraded">${esc(e.message)}</li>`; return; }
  $("#sys-time").textContent = `检查于 ${new Date().toLocaleTimeString("zh-CN", {hour: "2-digit", minute: "2-digit"})}`;
  $("#checks").innerHTML = s.checks.map(c => `<li><span><span class="pill ${c.ok ? "ok" : "bad"}">${c.ok ? "正常" : "异常"}</span> ${esc(c.name)}</span><span>${esc(c.detail ?? "")}</span></li>`).join("");
  $("#paused").innerHTML = s.paused.map(p => `<tr><td class="num">${esc(p.label)}</td><td>${esc(p.what)}</td><td>${esc(p.why)}</td></tr>`).join("") || `<tr><td colspan="3" class="empty">没有暂停的服务</td></tr>`;
  $("#restore").textContent = `恢复方法：告诉执行员要恢复哪一项，或在终端运行 ${s.restore}`;
  $("#exec-log").innerHTML = s.executions.map(e => `<tr><td class="num">${new Date(e.created_at).toLocaleString("zh-CN", {month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit"})}</td><td>${esc(e.asset)}</td><td>${STAGE[e.stage] || esc(e.stage)}</td><td>${esc(e.detail?.reason || e.detail?.message || (e.detail?.blockers || []).join("；") || (e.detail?.execution_ready ? `预览通过，最多亏 ${fmt(e.detail.max_loss)}` : e.detail?.status || ""))}</td></tr>`).join("") || `<tr><td colspan="4" class="empty">还没有执行记录</td></tr>`;
  const assets = (await api("/api/assets")).assets;
  $("#asset-admin").innerHTML = assets.map(a => `<div class="admin-row" data-key="${esc(a.key)}"><b>${esc(a.label)} <span class="meta" style="font-weight:400;color:var(--ink-3)">${a.kind === "xau_paper" ? "纸面盘" : "测试盘"}</span></b>
    <input value="${esc(a.news_queries.join(", "))}" aria-label="${esc(a.label)} 新闻关键词">
    <span class="acts"><button type="button" data-act="save">保存关键词</button><button type="button" class="danger" data-act="del">移除</button></span></div>`).join("");
  $("#asset-admin").querySelectorAll(".admin-row").forEach(row => {
    const key = row.dataset.key;
    row.querySelector('[data-act="save"]').onclick = async () => { try { await api(`/api/assets/${key}/news`, {news_queries: row.querySelector("input").value.split(/[,，]/)}, "PUT"); row.querySelector('[data-act="save"]').textContent = "已保存"; } catch (e) { alert(e.message); } };
    row.querySelector('[data-act="del"]').onclick = async () => { if (!confirm(`从交易台移除 ${key}？交易所上的挂单和持仓不受影响。`)) return; try { await api(`/api/assets/${key}`, null, "DELETE"); await loadAssets(); loadSystem(); } catch (e) { alert(e.message); } };
  });
}
