const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmt = (n, d) => n == null || isNaN(n) ? "—" : Number(n).toLocaleString("en-US", {minimumFractionDigits: d ?? (Math.abs(n) >= 1000 ? 0 : 2), maximumFractionDigits: d ?? (Math.abs(n) >= 1000 ? 1 : 2)});
const S = {page: "news", nl: "morning"};

async function api(path, body, method) {
  const opts = method ? {method} : body ? {method: "POST"} : {};
  if (body) { opts.headers = {"content-type": "application/json"}; opts.body = JSON.stringify(body); }
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : "请求失败，请稍后再试");
  return data;
}

// ---- assets ---------------------------------------------------------------------------
async function loadAssets() { return (await api("/api/assets")).assets; }
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
  try { await api("/api/assets", {coin: picked, news_queries: news}); $("#add-dialog").close(); $("#add-news").value = ""; $("#add-search").value = ""; loadSystem(); }
  catch (e) { $("#add-toast").textContent = e.message; }
};

// ---- pages -----------------------------------------------------------------------------
function showPage(page, nl) {
  S.page = page;
  document.querySelectorAll("[data-page]").forEach(b => b.setAttribute("aria-selected", b.dataset.page === page));
  $("#page-news").hidden = page !== "news"; $("#page-system").hidden = page !== "system";
  if (page === "news") { if (nl) pickNl(nl); else loadNewsletters(); }
  if (page === "system") { loadSystem(); loadMainnet(); }
}
document.querySelectorAll("[data-page]").forEach(b => b.onclick = () => { history.replaceState(null, "", `#${b.dataset.page}`); showPage(b.dataset.page); });

async function loadNewsletters() {
  try {
    const n = await api("/api/newsletters");
    $("#nl-archive").innerHTML = `<option value="">选择日期</option>` + n.morning.archive.map(d => `<option value="${d}">${d}</option>`).join("");
    const meta = {morning: n.morning.latest, kline: n.kline.latest, weekly: n.weekly.latest}[S.nl];
    $("#nl-meta").textContent = meta ? `更新于 ${meta}` : "还没有生成";
  } catch (e) { $("#nl-meta").textContent = e.message; }
  if ($("#nl-frame").getAttribute("src") === "about:blank") $("#nl-frame").src = `/newsletter/${S.nl}`;
}
function pickNl(nl) { S.nl = nl; document.querySelectorAll("[data-nl]").forEach(x => x.setAttribute("aria-pressed", x.dataset.nl === nl)); $("#nl-archive").value = ""; $("#nl-frame").src = `/newsletter/${nl}`; loadNewsletters(); }
document.querySelectorAll("[data-nl]").forEach(b => b.onclick = () => { history.replaceState(null, "", b.dataset.nl === "morning" ? "#news" : `#news-${b.dataset.nl}`); pickNl(b.dataset.nl); });
$("#nl-archive").onchange = () => { if ($("#nl-archive").value) $("#nl-frame").src = `/newsletter/${$("#nl-archive").value}`; };

const STAGE = {preview: "预览", execute_started: "按下执行", executed: "已执行", execute_failed: "执行未完成", refused: "被拒绝", stop_requested: "按下停止", stop_refused: "停止被拒绝", rearm_hold: "暂停补单", rearm_release: "恢复补单", mainnet_setup: "保存正式盘设置", mainnet_unlock: "正式盘解锁"};
async function loadSystem() {
  let s;
  try { s = await api("/api/system"); } catch (e) { $("#checks").innerHTML = `<li class="degraded">${esc(e.message)}</li>`; return; }
  $("#sys-time").textContent = `检查于 ${new Date().toLocaleTimeString("zh-CN", {hour: "2-digit", minute: "2-digit"})}`;
  $("#checks").innerHTML = s.checks.map(c => `<li><span><span class="pill ${c.ok ? "ok" : "bad"}">${c.ok ? "正常" : "异常"}</span> ${esc(c.name)}</span><span>${esc(c.detail ?? "")}</span></li>`).join("");
  $("#paused").innerHTML = s.paused.map(p => `<tr><td class="num">${esc(p.label)}</td><td>${esc(p.what)}</td><td>${esc(p.why)}</td></tr>`).join("") || `<tr><td colspan="3" class="empty">没有暂停的服务</td></tr>`;
  $("#restore").textContent = `恢复方法：告诉执行员要恢复哪一项，或在终端运行 ${s.restore}`;
  $("#exec-log").innerHTML = s.executions.map(e => `<tr><td class="num">${new Date(e.created_at).toLocaleString("zh-CN", {month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit"})}</td><td>${esc(e.asset)}</td><td>${STAGE[e.stage] || esc(e.stage)}</td><td>${esc(e.detail?.reason || e.detail?.message || (e.detail?.blockers || []).join("；") || (e.detail?.execution_ready ? `预览通过，最多亏 ${fmt(e.detail.max_loss)}` : e.detail?.status || ""))}</td></tr>`).join("") || `<tr><td colspan="4" class="empty">还没有执行记录</td></tr>`;
  const assets = await loadAssets();
  $("#asset-admin").innerHTML = assets.map(a => `<div class="admin-row" data-key="${esc(a.key)}"><b>${esc(a.label)} <span class="meta" style="font-weight:400;color:var(--ink-3)">${a.kind === "xau_paper" ? "纸面盘" : "测试盘"}</span></b>
    <input value="${esc(a.news_queries.join(", "))}" aria-label="${esc(a.label)} 新闻关键词">
    <span class="acts"><button type="button" data-act="save">保存关键词</button><button type="button" class="danger" data-act="del">移除</button></span></div>`).join("");
  $("#asset-admin").querySelectorAll(".admin-row").forEach(row => {
    const key = row.dataset.key;
    row.querySelector('[data-act="save"]').onclick = async () => { try { await api(`/api/assets/${key}/news`, {news_queries: row.querySelector("input").value.split(/[,，]/)}, "PUT"); row.querySelector('[data-act="save"]').textContent = "已保存"; } catch (e) { alert(e.message); } };
    row.querySelector('[data-act="del"]').onclick = async () => { if (!confirm(`从交易台移除 ${key}？交易所上的挂单和持仓不受影响。`)) return; try { await api(`/api/assets/${key}`, null, "DELETE"); loadSystem(); } catch (e) { alert(e.message); } };
  });
}

// ---- Hyperliquid Mainnet (read-only dry run) --------------------------------------------
const MN_STATE = {ok: "正常", breach: "碰到亏损线", stale: "读数中断", no_baseline: "等 08:00 基准", not_configured: "未设置", baseline_unreadable: "基准文件读不出", observer_missing: "观察程序未安装", mainnet_config_invalid: "设置文件有误"};
async function loadMainnet() {
  let m;
  try { m = await api("/api/mainnet"); } catch (e) { $("#mn-body").innerHTML = `<p class="note">${esc(e.message)}</p>`; return; }
  $("#mn-mode").textContent = m.configured ? `只读试运行 · 子账户 ${m.subaccount}` : "还没设置";
  $("#mn-form").hidden = !!m.configured;
  if (!m.configured) { $("#mn-body").innerHTML = ""; return; }
  const money = v => v == null ? "—" : `${fmt(Number(v))} 美元`;
  const r = m.report || {};
  const lock = m.lock;
  $("#mn-body").innerHTML = `<dl class="mn-grid">
      <div><dt>状态</dt><dd><span class="pill ${m.state === "ok" ? "ok" : "bad"}">${esc(MN_STATE[m.state] || m.state)}</span></dd></div>
      <div><dt>权益</dt><dd class="num">${money(m.equity)}</dd></div>
      <div><dt>今日亏损</dt><dd class="num">${money(m.daily_loss)} / ${fmt(Number(m.daily_limit || 25), 0)}</dd></div>
      <div><dt>总亏损</dt><dd class="num">${money(m.total_loss)} / ${fmt(Number(m.total_limit || 75), 0)}</dd></div>
      <div><dt>两路读数差</dt><dd class="num">${money(m.diff)}</dd></div>
      <div><dt>试运行</dt><dd>${fmt(r.covered_hours ?? 0, 1)} / ${r.window_hours ?? 48} 小时 · ${r.passed ? "通过" : "未通过"}</dd></div>
    </dl>
    ${lock ? `<div class="mn-lock"><b>已锁住</b>（${lock.kind === "total" ? "总亏损" : lock.kind === "daily" ? "今日亏损" : "锁文件异常"}，${lock.triggered_at ? new Date(lock.triggered_at).toLocaleString("zh-CN") : ""}）<button type="button" class="btn-primary" id="mn-unlock">解锁</button><span class="toast" id="mn-unlock-toast" role="status"></span></div>` : ""}
    <p class="note">试运行只读余额，不会下单。${m.observed_at ? `最近读取 ${new Date(m.observed_at).toLocaleTimeString("zh-CN")}` : ""}</p>`;
  const unlockBtn = $("#mn-unlock");
  if (unlockBtn) unlockBtn.onclick = async () => {
    if (!confirm("确认解锁正式盘？")) return;
    try { await api("/api/mainnet/unlock", {}); loadMainnet(); } catch (e) { $("#mn-unlock-toast").textContent = e.message; }
  };
}
$("#mn-form").onsubmit = async ev => {
  ev.preventDefault();
  const key = $("#mn-key"), sub = $("#mn-sub");
  $("#mn-save").disabled = true; $("#mn-toast").textContent = "正在保存…";
  try { const r = await api("/api/mainnet/setup", {key: key.value, subaccount: sub.value}); key.value = ""; $("#mn-toast").textContent = `已保存 · 子账户 ${r.subaccount}`; loadMainnet(); }
  catch (e) { $("#mn-toast").textContent = e.message; }
  finally { key.value = ""; $("#mn-save").disabled = false; }
};

// ---- boot: /desk#news, /desk#news-kline, /desk#news-weekly, /desk#system -----------------
function route() {
  const hash = location.hash.slice(1);
  if (hash === "system") showPage("system");
  else showPage("news", hash.startsWith("news-") ? hash.slice(5) : "morning");
}
route();
window.addEventListener("hashchange", route);
setInterval(() => { if (S.page === "system") { loadSystem(); loadMainnet(); } }, 60000);
const addBtn = document.getElementById("add-asset"); if (addBtn) addBtn.onclick = openAdd;
