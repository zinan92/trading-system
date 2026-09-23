// Desk layer injected into the GridMind console (served at /trade): nav + asset switch,
// rolling news rail, and today's judgment -> plan -> 执行 panel. GridMind itself stays untouched.
(() => {
  const $d = s => document.querySelector(s);
  const escD = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
  const fmtD = (n, d) => n == null || isNaN(n) ? "—" : Number(n).toLocaleString("en-US", {minimumFractionDigits: d ?? (Math.abs(n) >= 1000 ? 0 : 2), maximumFractionDigits: d ?? (Math.abs(n) >= 1000 ? 1 : 2)});
  const VENUE = {xau_paper: ["binance.paper", "XAUUSDT.BINANCE"]};
  const D = {assets: [], asset: null, news: [], filter: "key", seen: null, fresh: new Set(), newsAt: null, d: null, cites: [], plan: null, desk: null, switching: false};

  async function call(path, body) {
    const res = await fetch(path, body ? {method: "POST", headers: {"content-type": "application/json"}, body: JSON.stringify(body)} : {});
    const data = await res.json().catch(() => ({}));
    if (res.status === 401) { location.href = "/login"; throw new Error("需要重新登录"); }
    if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : data.message || "请求失败，请稍后再试");
    return data;
  }

  // ---- shell ------------------------------------------------------------------------
  function mountShell() {
    const top = $d("header.top"), brand = top?.querySelector(".brand");
    if (brand) brand.insertAdjacentHTML("afterend", `<nav class="desk-nav" aria-label="页面"><a href="/trade" aria-current="page">交易</a><a href="/desk#news">日报</a><a href="/desk#system">系统</a></nav><div class="desk-assets" id="deskAssets" role="group" aria-label="品种"></div>`);
    top?.insertAdjacentHTML("beforeend", `<a class="desk-pass" href="/passcode">改口令</a>`);
    const wrap = $d(".console-wrap");
    const layout = document.createElement("div"); layout.className = "desk-layout";
    wrap.parentNode.insertBefore(layout, wrap);
    layout.innerHTML = `<aside class="card desk-news" aria-labelledby="deskNewsTitle">
      <div class="card-title"><span id="deskNewsTitle">滚动新闻</span><span class="desk-seg" role="group" aria-label="新闻筛选"><button type="button" data-nf="key" aria-pressed="true">重要+关注</button><button type="button" data-nf="all" aria-pressed="false">全部</button></span></div>
      <div class="desk-meta"><span>点一条，引用到今日判断</span><span id="deskNewsAt">读取中</span></div>
      <ul id="deskNews"><li class="desk-empty">正在读取新闻…</li></ul></aside>`;
    layout.appendChild(wrap);
    layout.insertAdjacentHTML("afterbegin", `<section class="card desk-watch" aria-labelledby="deskWatchTitle">
      <div class="desk-watch-head"><span id="deskWatchTitle">本周要看的三件事</span><span class="desk-meta" style="padding:0" id="deskWatchMeta">读取中</span></div>
      <div class="desk-watch-row" id="deskWatch"></div></section>`);
    const rail = $d(".control-rail");
    rail?.insertAdjacentHTML("afterbegin", `<section class="card desk-judge" aria-labelledby="deskJudgeTitle">
      <div class="card-title"><span id="deskJudgeTitle">今日判断 · 执行</span><span class="desk-meta" style="padding:0" id="deskJudgeDate"></span></div>
      <div class="card-body">
        <div class="desk-chips" id="deskChips" role="group" aria-label="判断哪个品种"></div>
        <div class="desk-reading" id="deskReading"></div>
        <div class="desk-ai" id="deskAi"></div>
        <div class="desk-dir" role="group" aria-label="方向"><button type="button" data-d="long" aria-pressed="false">做多</button><button type="button" data-d="flat" aria-pressed="false">观望</button><button type="button" data-d="short" aria-pressed="false">做空</button></div>
        <label for="deskReason" class="desk-meta" style="padding:0">一句理由</label>
        <textarea id="deskReason" placeholder="比如：美元走强、ETF 净流出，先不追多"></textarea>
        <div class="desk-conf"><label for="deskConf">信心</label><input id="deskConf" type="range" min="1" max="5" value="3"><span id="deskConfV">3 / 5</span></div>
        <div class="desk-cites" id="deskCites">还没有引用新闻</div>
        <div class="desk-plan" id="deskPlan"><p class="note">先选方向，系统按它生成计划；批准后才会出现「执行」。</p></div>
        <div id="deskStop"></div>
      </div></section>`);
    const note = "启动和停止在上方「今日判断 · 执行」里按：会重新预览、复核最大亏损，再挂单或撤单。";
    $d(".dashboard-control-actions")?.insertAdjacentHTML("afterend", `<p class="desk-rail-note">${note}</p>`);
    $d(".strategy-card .robot-actions")?.insertAdjacentHTML("afterend", `<p class="desk-rail-note">${note}</p>`);
    $d("#deskJudgeDate").textContent = new Date().toLocaleDateString("zh-CN", {month: "long", day: "numeric"});
    document.querySelectorAll("[data-nf]").forEach(b => b.onclick = () => { D.filter = b.dataset.nf; document.querySelectorAll("[data-nf]").forEach(x => x.setAttribute("aria-pressed", x === b)); renderNews(); });
    $d("#deskNews").addEventListener("click", e => { const li = e.target.closest("li[data-title]"); if (li) toggleCite(li.dataset.title); });
    $d("#deskNews").addEventListener("keydown", e => { const li = e.target.closest("li[data-title]"); if (li && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); toggleCite(li.dataset.title); } });
    document.querySelectorAll(".desk-dir [data-d]").forEach(b => b.onclick = () => { D.d = b.dataset.d; syncDir(); loadPlan(); });
    $d("#deskConf").oninput = () => { $d("#deskConfV").textContent = `${$d("#deskConf").value} / 5`; };
  }

  // ---- asset = GridMind's venue selection -------------------------------------------
  function selectionAsset() {
    const sel = window.dashboardControlState?.selection;
    if (!sel || !D.assets.length) return null;
    if (sel.venue_profile_id === "binance.paper") return D.assets.find(a => a.kind === "xau_paper") || null;
    return D.assets.find(a => a.kind === "hl_testnet" && a.instrument_id === sel.instrument_id) || null;
  }
  function renderAssets() {
    const signature = `${D.asset}|${D.switching}|${D.assets.map(a => a.key).join(",")}`;
    if (signature === D.assetSignature) return;
    D.assetSignature = signature;
    $d("#deskAssets").innerHTML = D.assets.filter(onGridMind)
      .map(a => `<button type="button" data-asset="${escD(a.key)}" aria-pressed="${a.key === D.asset}" ${D.switching ? "disabled" : ""}>${escD(a.label)}</button>`).join("");
    document.querySelectorAll("#deskAssets [data-asset]").forEach(b => b.onclick = () => switchAsset(b.dataset.asset));
  }
  const waitFor = (test, ms = 8000) => new Promise(resolve => { const start = Date.now(); const tick = () => test() ? resolve(true) : Date.now() - start > ms ? resolve(false) : setTimeout(tick, 120); tick(); });
  async function switchAsset(key) {
    const asset = D.assets.find(a => a.key === key);
    if (!asset || D.switching) return;
    if (key === D.asset && !D.override) return;
    D.override = null;
    if (asset.key === selectionAsset()?.key) { applyAsset(); return; }
    const [venueId, instrumentId] = VENUE[asset.kind] || ["hyperliquid.testnet", asset.instrument_id];
    const venue = $d("#dashboardVenueSelect"), instrument = $d("#dashboardInstrumentSelect");
    if (!venue || !instrument) return;
    D.switching = true; renderAssets();
    try {
      if (venue.value !== venueId) { venue.value = venueId; venue.dispatchEvent(new Event("change")); }
      await waitFor(() => [...instrument.options].some(o => o.value === instrumentId && !o.disabled));
      instrument.value = instrumentId; instrument.dispatchEvent(new Event("change"));
      await waitFor(() => window.dashboardControlState?.selection?.instrument_id === instrumentId);
    } finally { D.switching = false; }
    applyAsset();
  }
  // BTC and gold follow GridMind; ETH, silver and oil are judged here while GridMind stays on its chart.
  const onGridMind = a => a.kind === "xau_paper" || a.instrument_id === "BTC-USD-PERP";
  function renderChips() {
    const signature = `${D.asset}|${D.assets.map(a => a.key).join(",")}`;
    if (signature === D.chipSignature) return;
    D.chipSignature = signature;
    $d("#deskChips").innerHTML = D.assets.map(a => `<button type="button" data-chip="${escD(a.key)}" aria-pressed="${a.key === D.asset}" title="${onGridMind(a) ? "图表和交易在 GridMind" : a.kind === "watch" ? "只记判断和复盘" : "测试盘，下单尚未开通"}">${escD(a.label)}</button>`).join("");
    document.querySelectorAll("#deskChips [data-chip]").forEach(b => b.onclick = () => {
      const asset = D.assets.find(a => a.key === b.dataset.chip);
      if (!asset || asset.key === D.asset) return;
      if (onGridMind(asset)) { switchAsset(asset.key); return; }
      D.override = asset.key; focusAsset(asset);
    });
  }
  function focusAsset(asset) {
    const changed = asset.key !== D.asset;
    D.asset = asset.key;
    renderAssets(); renderChips();
    if (!changed) return;
    D.d = null; D.cites = []; D.plan = null; D.seen = null;
    $d("#deskReason").value = ""; $d("#deskConf").value = 3; $d("#deskConfV").textContent = "3 / 5"; syncDir(); renderCites();
    $d("#deskPlan").innerHTML = `<p class="note">先选方向，系统按它生成计划；批准后才会出现「执行」。</p>`;
    $d("#deskJudgeTitle").textContent = `今日判断 · ${asset.label}`;
    $d("#deskStop").innerHTML = "";
    loadNews(); loadDesk();
  }
  function applyAsset() {
    if (D.override) return;  // Park is judging an asset GridMind does not chart
    const asset = selectionAsset();
    if (!asset) return;
    document.body.classList.toggle("desk-gold", asset.kind === "xau_paper");
    if (asset.key === D.asset) { renderAssets(); renderChips(); return; }
    focusAsset(asset);  // sets D.asset and resets the panel
  }

  // ---- news -------------------------------------------------------------------------
  const LABEL = {high_impact: "重要", watch: "关注", noise: "噪音", unrated: "未分级"}, CLS = {high_impact: "high", watch: "watch", noise: "noise", unrated: "noise"};
  function ago(stamp) {
    if (!stamp) return "";
    const at = new Date(stamp + (/[Z+]/.test(stamp.slice(19)) ? "" : "Z")), minutes = Math.round((Date.now() - at) / 60000);
    if (minutes < 1) return "刚刚";
    if (minutes < 60) return `${minutes} 分钟前`;
    return (at.toDateString() === new Date().toDateString() ? "" : `${at.getMonth() + 1}/${at.getDate()} `) + at.toLocaleTimeString("zh-CN", {hour: "2-digit", minute: "2-digit"});
  }
  async function loadNews(silent) {
    const key = D.asset; if (!key) return;
    let data;
    try { data = await call(`/api/news/${key}`); } catch (e) { data = {ok: false, reason: e.message, items: []}; }
    if (key !== D.asset) return;
    if (!data.ok) { if (!silent || !D.news.length) { D.news = []; $d("#deskNews").innerHTML = `<li class="desk-bad">${escD(data.reason)}</li>`; } return; }
    const seen = D.seen;
    D.fresh = new Set(seen ? data.items.filter(n => !seen.has(n.id ?? n.title)).map(n => n.id ?? n.title) : []);
    D.seen = new Set(data.items.map(n => n.id ?? n.title));
    D.news = data.items; D.newsAt = new Date(); D.stale = data.stale; renderNews();
  }
  function renderNews() {
    const items = D.news.filter(n => D.filter === "all" || n.bucket === "high_impact" || n.bucket === "watch");
    $d("#deskNewsAt").textContent = D.newsAt ? `每分钟更新 · ${D.newsAt.toLocaleTimeString("zh-CN", {hour: "2-digit", minute: "2-digit"})}` : "";
    const stale = D.stale ? `<li class="desk-bad">新闻服务暂时连不上，下面是上一次读到的列表。</li>` : "";
    if (!items.length) { $d("#deskNews").innerHTML = stale + `<li class="desk-empty">过去 24 小时没有${D.filter === "all" ? "" : "重要或关注级"}相关新闻。</li>`; return; }
    $d("#deskNews").innerHTML = stale + items.map(n => {
      const cited = D.cites.some(c => c.title === n.title), fresh = D.fresh.has(n.id ?? n.title);
      return `<li class="${CLS[n.bucket] || "noise"}${cited ? " cited" : ""}" data-title="${escD(n.title)}" tabindex="0" role="button" aria-pressed="${cited}"><span class="bar"></span><div>
        <div class="t">${fresh ? '<span class="new">新</span>' : ""}${escD(n.title)}</div>
        <div class="m"><span>${ago(n.collected_at)}</span><span class="tag ${CLS[n.bucket] || ""}">${LABEL[n.bucket] || ""}</span><span>${escD(n.source)}</span>${cited ? '<span class="cite">已引用</span>' : ""}</div></div></li>`;
    }).join("");
  }
  function toggleCite(title) {
    const k = D.cites.findIndex(c => c.title === title);
    if (k >= 0) D.cites.splice(k, 1); else { const n = D.news.find(x => x.title === title); if (n) D.cites.push({id: n.id, title: n.title, bucket: n.bucket}); }
    renderNews(); renderCites();
  }
  function renderCites() { $d("#deskCites").innerHTML = D.cites.length ? D.cites.map(c => `<span title="${escD(c.title)}">${escD(c.title.slice(0, 16))}…</span>`).join("") : "还没有引用新闻"; }

  // ---- 本周三件事 --------------------------------------------------------------------
  const WEEKDAY = "一二三四五六日";
  function whenLabel(item) {
    const at = new Date(item.at); if (isNaN(at)) return "";
    const bj = new Date(at.getTime() + (at.getTimezoneOffset() + 480) * 60000), hm = `${String(bj.getHours()).padStart(2, "0")}:${String(bj.getMinutes()).padStart(2, "0")}`;
    if (item.kind === "breaking") return `突发 · ${hm}`;
    const ms = at - Date.now(), stamp = `周${WEEKDAY[(bj.getDay() + 6) % 7]} ${hm}`;
    if (ms <= 0) return `${stamp} · 已公布`;
    const h = Math.floor(ms / 3600000), m = Math.floor(ms / 60000) % 60;
    return `${stamp} · ${h >= 24 ? `还有 ${Math.floor(h / 24)} 天 ${h % 24} 小时` : h ? `还有 ${h} 小时 ${m} 分` : `还有 ${m} 分钟`}`;
  }
  async function loadWatch() {
    let w;
    try { w = await call("/api/watch"); } catch (e) { $d("#deskWatchMeta").textContent = e.message; return; }
    D.watch = w; renderWatch();
  }
  function renderWatch() {
    const w = D.watch || {}, items = w.items || [];
    $d("#deskWatchMeta").textContent = w.generated_at ? `${w.generated_at.slice(11, 16)} 更新 · ${w.provider === "codex" ? "按全市场冲击排序" : "模型暂不可用，按日历重要性排序"}` : "还在生成";
    $d("#deskWatch").innerHTML = items.length ? items.map(item => `<article class="desk-watch-card ${item.volatility === "高" ? "high" : ""}${(w.replaced || []).includes(item.ref) ? " fresh" : ""}">
      <div class="w-when"><span>${escD(whenLabel(item))}</span><span>波动 ${escD(item.volatility || "中")}</span></div>
      <b title="${escD(item.source_title)}">${escD(item.title)}</b>
      ${(item.markets || []).length ? `<div class="w-mk">${item.markets.map(m => `<span>${escD(m)}</span>`).join("")}</div>` : ""}
      <p>${escD(item.why || item.source_title)}</p></article>`).join("") : `<p class="desk-empty">三件事还在生成，稍后自动刷新。</p>`;
  }

  // ---- desk state: reading, today's call, stop -----------------------------------------
  const readingDirection = s => /分歧|等待确认/.test(s || "") ? "wait" : /偏多/.test(s || "") ? "long" : /偏空/.test(s || "") ? "short" : "wait";
  async function loadDesk() {
    const key = D.asset; if (!key) return;
    let d;
    try { d = await call(`/api/desk/${key}`); } catch (e) { $d("#deskReading").textContent = e.message; return; }
    if (key !== D.asset) return;
    D.desk = d;
    const k = d.kline;
    if (k?.ok) {
      const dir = readingDirection(k.structure), label = {long: "▲ 偏多", short: "▼ 偏空", wait: "– 观望"}[dir];
      const one = (k.synthesis || "").split(/(?<=[。；])/)[0];
      $d("#deskReading").innerHTML = `<b class="d-${dir}">K 线日报 ${label}</b>${escD(one)} <a href="/desk#news-kline" style="color:inherit;opacity:.7">看卡片</a>`;
    } else $d("#deskReading").textContent = `K 线日报：${k?.reason || "暂无"}`;
    const tj = d.today_judgment;
    if (tj && D.d === null) {
      D.d = tj.direction; $d("#deskReason").value = tj.reason || ""; $d("#deskConf").value = tj.confidence; $d("#deskConfV").textContent = `${tj.confidence} / 5`;
      D.cites = tj.cited || []; syncDir(); renderCites(); renderNews();
      const runs = (d.executions || []).filter(e => e.judgment_id === tj.id);
      const preview = runs.find(e => e.stage === "preview"), done = runs.find(e => ["executed", "execute_started", "execute_failed", "refused"].includes(e.stage));
      loadPlan().then(() => { if (tj.action === "approved" && preview) renderExec(tj.id, preview.detail, done); });
    }
    renderAi(d);
    renderStop(d.control);
  }
  const AI_CALL = {long: "做多", short: "做空", flat: "观望"};
  function renderAi(d) {
    const ai = d.ai_today, box = $d("#deskAi");
    const refresh = `<button type="button" class="desk-link" id="deskAiRefresh" ${d.ai_running ? "disabled" : ""}>${d.ai_running ? "AI 正在看…" : "刷新 AI 建议"}</button>`;
    if (!ai) { box.innerHTML = `<div class="desk-ai-line"><span class="k">AI 建议</span><span class="muted">每天 08:40 生成</span>${refresh}</div>`; }
    else {
      const at = new Date(ai.created_at).toLocaleTimeString("zh-CN", {hour: "2-digit", minute: "2-digit"});
      box.innerHTML = `<div class="desk-ai-line"><span class="k">AI 建议</span><b class="d-${ai.direction === "flat" ? "wait" : ai.direction}">${AI_CALL[ai.direction]}</b><span class="muted">信心 ${ai.confidence}/5 · ${at}</span>${refresh}</div>
        <p>${escD(ai.reason)}</p>${D.d === null ? `<button type="button" class="desk-link" id="deskAiAdopt">照 AI 的方向判断</button>` : ""}`;
      const adopt = $d("#deskAiAdopt");
      if (adopt) adopt.onclick = () => { D.d = ai.direction; syncDir(); loadPlan(); adopt.remove(); };
    }
    $d("#deskAiRefresh").onclick = async () => {
      $d("#deskAiRefresh").disabled = true; $d("#deskAiRefresh").textContent = "AI 正在看…";
      try { await call("/api/advice/refresh", {}); } catch (e) { $d("#deskAiRefresh").textContent = e.message; return; }
      const poll = setInterval(async () => { const st = await call("/api/advice").catch(() => null); if (st && !st.running) { clearInterval(poll); loadDesk(); } }, 5000);
    };
  }

  function renderStop(c) {
    const box = $d("#deskStop");
    if (!c || D.desk?.meta?.kind !== "hl_testnet") { box.innerHTML = ""; return; }
    if (c.stopping) { box.innerHTML = `<p class="desk-toast" style="color:var(--gm-warn)">正在停止：撤单、平仓中，约 1–2 分钟</p>`; return; }
    if (!c.can_stop) { box.innerHTML = ""; return; }
    const held = c.rearm_paused === true;
    box.innerHTML = `${held ? `<p class="desk-hold">⏸ 补单已暂停：现有挂单、止盈、止损不动，止盈后不补新单。</p>` : ""}
      <div class="desk-row"><button type="button" class="desk-btn hold" id="deskHoldBtn">${held ? "恢复补单" : "暂停补单"}</button>
      <button type="button" class="desk-btn stop" id="deskStopBtn">停止网格（撤单、平仓）</button></div><div class="desk-toast" id="deskStopToast" role="status"></div>`;
    $d("#deskHoldBtn").onclick = async () => {
      const text = held ? `恢复 ${D.asset} 测试盘网格补单？\n只补现价下方（做空则上方）的格子。` : `暂停 ${D.asset} 测试盘网格补单？\n现有挂单、止盈、止损都不动；成交止盈后不再补新单，直到你恢复。`;
      if (!confirm(text)) return;
      $d("#deskHoldBtn").disabled = true;
      try { const r = await call("/api/grid/rearm-hold", {asset: D.asset, paused: !held}); $d("#deskStopToast").textContent = r.message; loadDesk(); }
      catch (e) { $d("#deskStopToast").textContent = e.message; $d("#deskHoldBtn").disabled = false; }
    };
    $d("#deskStopBtn").onclick = async () => {
      if (!confirm(`确认停止 ${D.asset} 测试盘网格？\n会撤掉全部挂单；如果有持仓，会按市价平掉。`)) return;
      $d("#deskStopBtn").disabled = true;
      try { const r = await call("/api/grid/stop", {asset: D.asset, confirm_text: "停止"}); $d("#deskStopToast").textContent = r.message; setTimeout(loadDesk, 1500); }
      catch (e) { $d("#deskStopToast").textContent = e.message; $d("#deskStopBtn").disabled = false; }
    };
  }
  // ---- judgment -> plan -> approve -> 执行 (same flow and endpoints as before) --------
  function syncDir() { document.querySelectorAll(".desk-dir [data-d]").forEach(b => b.setAttribute("aria-pressed", b.dataset.d === D.d)); }
  async function loadPlan() {
    if (!D.d || !D.asset) return;
    const box = $d("#deskPlan"); box.innerHTML = `<p class="note">正在生成计划…</p>`;
    try { D.plan = await call("/api/plan", {asset: D.asset, direction: D.d}); } catch (e) { box.innerHTML = `<p class="warn">${escD(e.message)}</p>`; return; }
    const p = D.plan, tag = {keep: "沿用当前网格", new: "需要批准", hold: "不开新单", unavailable: "暂不可用", record_only: "只记录"}[p.kind] || p.kind;
    const rungs = p.rungs ? `<dt>价位</dt><dd>${p.rungs.map(r => fmtD(r)).join(" / ")}</dd>` : "";
    box.innerHTML = `<h4><span>${escD(p.title)}</span><span class="pill">${tag}</span></h4>
      <dl>${p.lines.map(([k, v]) => `<dt>${escD(k)}</dt><dd>${escD(v)}</dd>`).join("")}${rungs}</dl><p class="note">${escD(p.note)}</p>
      <div class="desk-row"><button type="button" class="desk-btn primary" id="deskApprove" ${p.kind === "unavailable" ? "disabled" : ""}>${p.kind === "new" ? "批准这份计划" : "确认并记录"}</button>${p.kind === "record_only" ? "" : `<button type="button" class="desk-btn" id="deskRecord">只记录判断</button>`}</div>
      <div class="desk-toast" id="deskToast" role="status"></div>`;
    $d("#deskApprove").onclick = () => submit(p.kind === "new" ? "approved" : "recorded");
    if ($d("#deskRecord")) $d("#deskRecord").onclick = () => submit("recorded");
  }
  async function submit(action) {
    if (!D.d) return;
    const buttons = document.querySelectorAll("#deskPlan .desk-row button"); buttons.forEach(b => b.disabled = true);
    try {
      const saved = await call("/api/judgments", {asset: D.asset, direction: D.d, confidence: +$d("#deskConf").value, reason: $d("#deskReason").value, cited: D.cites, action});
      $d("#deskToast").textContent = saved.handoff;
      if (saved.preview) renderExec(saved.id, saved.preview, null);
    } catch (e) { $d("#deskToast").textContent = e.message; }
    finally { buttons.forEach(b => b.disabled = false); }
  }
  function renderExec(judgmentId, preview, done) {
    const plan = $d("#deskPlan"); plan.querySelector(".desk-exec")?.remove();
    const box = document.createElement("div"); box.className = "desk-exec"; box.style.display = "grid"; box.style.gap = "7px";
    const paper = D.desk?.meta?.kind === "xau_paper", unit = paper ? "USDT" : "USDC";
    if (done) {
      const txt = {executed: paper ? "已执行：计划已确认，纸面盘约 1 分钟内挂单。" : "已执行：网格已挂上测试盘。", execute_started: "执行中或上次中断，请到系统页查看记录。", execute_failed: "上次执行没有完成，请到系统页查看记录。", refused: `上次执行被拒绝：${done.detail?.reason || ""}`}[done.stage];
      box.innerHTML = `<p class="note">${escD(txt)}</p>`; plan.appendChild(box); return;
    }
    const BLOCKER = {instrument_not_eligible: "这个品种还没在交易系统里开通下单（测试盘目前只开通了 BTC）"};
    if (!preview.execution_ready) { box.innerHTML = `<p class="warn">预览没通过，不能执行：${escD((preview.blockers || []).map(b => BLOCKER[b] || b).join("；"))}</p>`; plan.appendChild(box); return; }
    box.innerHTML = `<dl><dt>${paper ? "纸面盘预览价位" : "交易所预览价位"}</dt><dd>${(preview.orders || []).map(o => fmtD(o.price)).join(" / ")}</dd><dt>预览最多亏</dt><dd>${fmtD(preview.max_loss)} ${unit}</dd></dl>
      <p class="warn">${paper ? "按下后确认这份黄金纸面盘计划（模拟资金），约 1 分钟内挂单。已有黄金网格在跑时系统会拒绝。" : "按下后会在 Hyperliquid 测试盘真实挂单（假钱）。已有网格在跑时系统会拒绝。"}</p>
      <button type="button" class="desk-btn exec" id="deskExec">执行</button><div class="desk-toast" id="deskExecToast" role="status"></div>`;
    plan.appendChild(box);
    $d("#deskExec").onclick = async () => {
      if (!confirm(`确认在${paper ? "黄金纸面盘" : "测试盘"}执行这份计划？\n最多亏 ${fmtD(preview.max_loss)} ${unit}`)) return;
      $d("#deskExec").disabled = true; $d("#deskExecToast").textContent = "正在复核预览并下单，可能需要一两分钟…";
      try { const r = await call("/api/execute", {judgment_id: judgmentId, shown_max_loss: preview.max_loss, confirm_text: "执行"}); $d("#deskExecToast").textContent = r.message; loadDesk(); }
      catch (e) { $d("#deskExecToast").textContent = e.message; $d("#deskExec").disabled = false; }
    };
  }

  // ---- boot -------------------------------------------------------------------------
  // Phone: chart first, then today's call and the news, then GridMind's tables and controls.
  function placeForViewport() {
    const phone = window.matchMedia("(max-width: 760px)").matches;
    const judge = $d(".desk-judge"), news = $d(".desk-news"), chart = $d(".chart-card");
    if (!judge || !news || !chart) return;
    if (phone && judge.previousElementSibling !== chart) {
      chart.after(judge); judge.after(news);
    } else if (!phone && judge.parentElement !== $d(".control-rail")) {
      $d(".control-rail").prepend(judge); $d(".desk-watch").after(news);
    }
  }

  async function boot() {
    mountShell();
    placeForViewport();
    loadWatch();
    window.matchMedia("(max-width: 760px)").addEventListener("change", placeForViewport);
    try { D.assets = (await call("/api/assets")).assets; } catch (e) { $d("#deskNews").innerHTML = `<li class="desk-bad">${escD(e.message)}</li>`; return; }
    const ready = await waitFor(() => window.dashboardControlState?.selection, 15000);
    if (!ready || !selectionAsset()) {  // GridMind control rail unavailable: fall back to BTC for news and judgment
      D.asset = (D.assets.find(a => a.instrument_id === "BTC-USD-PERP") || D.assets[0]).key; renderAssets(); renderChips(); loadNews(); loadDesk();
    } else applyAsset();
    setInterval(() => { if (!D.switching) applyAsset(); }, 2000);  // follow selection changes made inside GridMind
    setInterval(() => { if (!document.hidden) { loadNews(true); loadDesk(); } }, 60000);
    setInterval(() => { if (!document.hidden && D.news.length) renderNews(); if (D.watch) renderWatch(); }, 30000);
    setInterval(() => { if (!document.hidden) loadWatch(); }, 300000);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot); else boot();
})();
