const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmt = (n, d) => n == null || isNaN(n) ? "—" : Number(n).toLocaleString("en-US", {minimumFractionDigits: d ?? (Math.abs(n) >= 1000 ? 0 : 2), maximumFractionDigits: d ?? (Math.abs(n) >= 1000 ? 1 : 2)});
const signed = (n, d = 2) => n == null ? "—" : `<span class="${n > 0 ? "pos" : n < 0 ? "neg" : ""}">${n > 0 ? "+" : ""}${fmt(n, d)}</span>`;
const DIR = {long: "做多", short: "做空", flat: "观望"};
const STEPS = [["look","看"],["judge","判断"],["plan","计划"],["approve","批准"],["watch","盯"],["review","复盘"]];
const S = {asset: "BTC", tf: "4h", filter: "key", d: null, cites: [], desk: null, news: [], bars: null, plan: null};
try { const saved = JSON.parse(localStorage.getItem("desk-ui") || "{}"); if (saved.asset) S.asset = saved.asset; if (saved.tf) S.tf = saved.tf; } catch (e) {}
const persistUi = () => { try { localStorage.setItem("desk-ui", JSON.stringify({asset: S.asset, tf: S.tf})); } catch (e) {} };

async function api(path, body) {
  const res = await fetch(path, body ? {method: "POST", headers: {"content-type": "application/json"}, body: JSON.stringify(body)} : {});
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
  renderStatus(); renderNotes();
  const tj = d.today_judgment;
  if (tj && S.d === null) { S.d = tj.direction; $("#reason").value = tj.reason || ""; $("#conf").value = tj.confidence; $("#confv").textContent = `${tj.confidence} / 5`; S.cites = tj.cited || []; syncDir(); renderCites(); loadPlan(); }
  renderChart();
}

function renderStatus() {
  const g = S.desk.grid;
  if (!g.ok) { $("#status").innerHTML = `<div class="degraded">${esc(g.reason || "网格状态读不到")}</div>`; return; }
  const tone = /卡住|止损/.test(g.status_label) ? "bad" : /暂停|高于|低于/.test(g.status_label) ? "warn" : /结束|停止/.test(g.status_label) ? "idle" : "ok";
  const updated = g.updated_at ? new Date(g.updated_at).toLocaleTimeString("zh-CN", {hour: "2-digit", minute: "2-digit"}) : "—";
  $("#status").innerHTML = `
    <div><div class="k">网格状态</div><div class="v"><span class="pill ${tone}">${esc(g.status_label)}</span></div></div>
    <div><div class="k">方向 · 区间</div><div class="v num">${DIR[g.direction] || "—"} ${fmt(g.lower)}–${fmt(g.upper)}</div></div>
    <div><div class="k">挂单 / 成交</div><div class="v num">${fmt(g.open_orders, 0)} / ${fmt(g.fills, 0)}</div></div>
    <div><div class="k">硬止损</div><div class="v num">${fmt(g.hard_stop)}</div></div>
    <div><div class="k">系统上次检查</div><div class="v num">${updated}</div></div>`;
  $("#legend").innerHTML = `<span><i style="border-color:var(--brass);border-top-style:dashed"></i>网格价位（${g.rungs.length} 格）</span>
    <span><i style="border-color:var(--ink-2)"></i>区间 ${fmt(g.lower)} – ${fmt(g.upper)}</span>
    <span><i style="border-color:var(--warn)"></i>硬止损 ${fmt(g.hard_stop)}</span><span>红涨绿跌</span>`;
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
    loadDesk(); loadReview();
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
  const OUT = {hit: ["outcome-hit", "✓ 说中了"], miss: ["outcome-miss", "✗ 没说中"], even: ["outcome-even", "△ 基本没动"]};
  $("#review").innerHTML = data.items.length ? data.items.map(j => {
    const o = j.outcome ? OUT[j.outcome] : ["", `等 ${data.review_hours} 小时`];
    return `<tr><td class="num">${new Date(j.created_at).toLocaleString("zh-CN", {month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit"})}</td>
      <td>${j.asset === "XAU" ? "黄金" : "BTC"}</td><td class="d-${j.direction}">${DIR[j.direction]}</td><td class="num">${j.confidence}/5</td>
      <td>${esc(j.reason) || "—"}${j.cited.length ? `<div class="meta" style="font-size:11.5px;color:var(--ink-3)">引用 ${j.cited.length} 条新闻</div>` : ""}</td>
      <td>${j.action === "approved" ? "已批准" : "只记录"}</td><td class="num">${fmt(j.price_at)}</td><td class="num">${j.price_after ? fmt(j.price_after) + ` (${j.move_pct > 0 ? "+" : ""}${j.move_pct.toFixed(2)}%)` : "—"}</td>
      <td class="${o[0]}">${o[1]}</td></tr>`;
  }).join("") : `<tr><td colspan="9" class="empty">还没有判断记录。在右上方选方向、写理由，记录后会出现在这里。</td></tr>`;
}

// ---- wiring --------------------------------------------------------------
function selectAsset(asset) {
  S.asset = asset; S.d = null; S.cites = []; S.plan = null; persistUi();
  document.querySelectorAll("[data-asset]").forEach(b => b.setAttribute("aria-pressed", b.dataset.asset === asset));
  $("#reason").value = ""; $("#conf").value = 3; $("#confv").textContent = "3 / 5"; syncDir(); renderCites();
  $("#plan").innerHTML = `<div class="empty" style="padding:0">先选一个方向，系统按它生成计划。</div>`;
  loadDesk().then(() => { loadBars(); loadDayChange(); }); loadNews();
}
document.querySelectorAll("[data-asset]").forEach(b => b.onclick = () => selectAsset(b.dataset.asset));
document.querySelectorAll("[data-tf]").forEach(b => b.onclick = () => { S.tf = b.dataset.tf; persistUi(); document.querySelectorAll("[data-tf]").forEach(x => x.setAttribute("aria-pressed", x === b)); loadBars(); });
document.querySelectorAll("[data-f]").forEach(b => b.onclick = () => { S.filter = b.dataset.f; document.querySelectorAll("[data-f]").forEach(x => x.setAttribute("aria-pressed", x === b)); renderNews(); });
document.querySelectorAll("[data-d]").forEach(b => b.onclick = () => { S.d = b.dataset.d; syncDir(); loadPlan(); });
$("#news").addEventListener("click", e => { const li = e.target.closest("li[data-title]"); if (li) toggleCite(li.dataset.title); });
$("#news").addEventListener("keydown", e => { const li = e.target.closest("li[data-title]"); if (li && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); toggleCite(li.dataset.title); } });
$("#conf").oninput = () => { $("#confv").textContent = `${$("#conf").value} / 5`; };
$("#note-form").onsubmit = async e => { e.preventDefault(); const body = $("#note").value.trim(); if (!body) return; try { await api("/api/notes", {asset: S.asset, body}); $("#note").value = ""; loadDesk(); } catch (err) { alert(err.message); } };
document.querySelectorAll("[data-tf]").forEach(x => x.setAttribute("aria-pressed", x.dataset.tf === S.tf));
selectAsset(S.asset); loadAccounts(); loadReview();
setInterval(() => { loadAccounts(); loadDesk(); }, 60000);
