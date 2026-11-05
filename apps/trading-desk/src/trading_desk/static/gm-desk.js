// Desk layer injected into the GridMind console (served at /trade): nav + asset switch,
// one status line, rolling news rail, and today's judgment -> plan -> 执行 panel.
// GridMind's own cards stay in the page (collapsed); only presentation changes here.

// Pure status model, read from the view GridMind last rendered (window.gridmindView), so the
// banner can never disagree with the chart or the account strip. Tested in tests/test_gm_desk_status.py.
const DeskStatus = (() => {
  const TERMINAL = {lower_boundary_invalidated: "价格跌破区间下沿", upper_boundary_invalidated: "价格涨破区间上沿", hard_stop: "触发硬止损", operator_stop: "你手动停止", session_expired: "会话到期"};
  const LIFECYCLE = {armed: "挂单中", running: "运行中", active: "运行中", paused_above_range: "价格高于区间，暂停补单", paused_below_range: "价格低于区间，暂停补单", hard_stop_triggered: "已触发硬止损，正在平仓", flattening: "正在平仓"};
  const ENDED = new Set(["terminal", "sealed", ""]);
  // With no plan these records are what "no plan" means, not a fault. Everything else counts as needing a look.
  const IDLE_EXPECTED = new Set(["production_plan_missing", "risk_decision_missing", "active_strategy_missing", "grid_geometry_invalid", "authoritative_snapshot_missing", "reconciliation_blocked:local_execution_authority_unavailable"]);
  const finite = v => v !== null && v !== "" && Number.isFinite(Number(v));
  const num = v => finite(v) ? Number(v).toLocaleString("en-US", {maximumFractionDigits: 2}) : "--";
  const beijing = stamp => { const t = new Date(stamp); return isNaN(t) ? null : new Date(t.getTime() + 8 * 3600000); };
  const bjDay = stamp => { const t = beijing(stamp); return t ? `${t.getUTCMonth() + 1}/${t.getUTCDate()}` : ""; };
  const bjTime = stamp => { const t = beijing(stamp); return t ? `${bjDay(stamp)} ${String(t.getUTCHours()).padStart(2, "0")}:${String(t.getUTCMinutes()).padStart(2, "0")}` : "--"; };

  function isActive(view) {
    const data = view?.data || {}, plan = view?.livePlan;
    const lifecycle = plan?.source === "testnet_grid_lifecycle" ? plan : null;
    return data.current_strategy?.active === true || Boolean(lifecycle && !ENDED.has(String(lifecycle.status || "")));
  }

  function banner(view, {asset = "黄金"} = {}) {
    if (!view?.data) return {tone: "wait", title: "正在读取交易状态", body: "", action: null};
    const data = view.data, market = view.market || data.market || {}, current = data.current_strategy || {};
    if (view.marketTrusted !== true) {
      const last = market.latest_timestamp ? `最后可信数据 ${bjTime(market.latest_timestamp)}` : "还没有可信数据";
      return {tone: "bad", title: `${asset}行情中断，已停止开新仓`,
        body: `${market.provider_label || "行情源"}的数据没有通过可信检查（${last}）。恢复之前不会开新仓，也不会改用别的数据源顶替。`,
        action: {kind: "details", label: "查看原因"}};
    }
    if (isActive(view)) {
      const plan = view.livePlan || {}, spec = current.specification || {}, ex = current.execution || {}, counts = data.execution?.counts || {};
      const lifecycle = plan.source === "testnet_grid_lifecycle" ? String(plan.status || "") : "";
      const low = spec.lower_price_boundary ?? plan.range?.low, high = spec.upper_price_boundary ?? plan.range?.high;
      const rungs = spec.grid_rung_count ?? plan.grid?.count, fills = ex.fill_count ?? counts.fill_count, held = ex.open_position_count ?? counts.open_position_count;
      const type = String(spec.strategy_type || plan.strategy_type || "grid").toLowerCase() === "dca" ? "定投" : "网格";
      const direction = {long: "做多", short: "做空", neutral: "中性"}[spec.direction || plan.direction] || null;
      const parts = [finite(low) && finite(high) ? `区间 ${num(low)} – ${num(high)}` : null, finite(rungs) && Number(rungs) > 0 ? `${num(rungs)} 格` : null, direction,
        finite(fills) ? `已成交 ${num(fills)} 笔` : null, finite(held) ? `持仓 ${num(held)} 笔` : null].filter(Boolean);
      const bad = /hard_stop|flatten/.test(lifecycle), paused = /paused/.test(lifecycle);
      return {tone: bad ? "bad" : paused ? "warn" : "run", title: `${asset}${type}${LIFECYCLE[lifecycle] || "运行中"}`, body: parts.join(" · "), action: {kind: "stop", label: "去停止"}};
    }
    const terminal = current.terminal || {};
    let body = "还没有上一轮策略的记录。";
    if (terminal.reason) {
      const {closed_count: closed, ended_at: ended, ended_flat: flat, open_positions: open, sealed_open_positions: sealed} = terminal;
      const rec = terminal.reconciliation?.status, late = finite(sealed) && Number(sealed) > 0;
      const reconciled = rec === "ok" ? "，对账一致" : rec ? "，对账未完成" : "";
      const state = flat === true ? (late ? `，现已无持仓（停止时还有 ${num(sealed)} 笔未平，之后才平完）` : `，已无持仓${reconciled}`)
        : flat === false ? `，仍有 ${num(open)} 笔持仓未平` : "，持仓情况未知";
      body = `上一轮${ended ? `在 ${bjDay(ended)} ` : ""}结束：${TERMINAL[terminal.reason] || "已结束（原因见系统详情）"}${finite(closed) && Number(closed) > 0 ? `，平仓 ${num(closed)} 笔` : ""}${state}。`;
      if (flat === false) return {tone: "warn", title: "现在没有在跑的策略", body, action: {kind: "details", label: "查看原因"}};
    }
    return {tone: "idle", title: "现在没有在跑的策略", body, action: {kind: "start", label: "开始今天的计划"}};
  }

  // Only claims a field backs. Never "all good": the calm state is grey, not green.
  function health(view) {
    if (!view?.data) return {tone: "wait", text: "读取中", other: [], expected: []};
    const data = view.data, active = isActive(view), issues = [...(data.completeness?.issues || [])];
    const rec = data.execution?.reconciliation;
    if (rec?.status && !["ok", "pass", "reconciled"].includes(rec.status)) issues.push(`reconciliation_${rec.status}${rec.reason ? `:${rec.reason}` : ""}`);
    const expected = active ? [] : issues.filter(code => IDLE_EXPECTED.has(code)), other = issues.filter(code => !expected.includes(code));
    const parts = [view.marketTrusted === true ? "行情可信" : "行情没有通过可信检查"];
    if (!active) parts.push("没有运行中的计划");
    if (other.length) parts.push(`${other.length} 项需要看一下`);
    return {tone: view.marketTrusted !== true ? "bad" : other.length ? "warn" : "calm", text: parts.join(" · "), other, expected};
  }

  function retired(selection, catalog) {
    const old = selection?.retired_selection;
    if (!old || selection?.venue_profile_id) return null;
    const [venue, env] = String(old.venue_profile_id || "").split(".");
    const label = venue ? `${venue.charAt(0).toUpperCase()}${venue.slice(1)}${env === "paper" ? " Paper" : ""}` : "之前的交易场所";
    const paper = (catalog?.venue_profiles || []).find(p => p.environment === "paper");
    return {old: label, next: paper?.label || null};
  }

  return {banner, health, retired, isActive};
})();
if (typeof module !== "undefined") module.exports = DeskStatus;

if (typeof document !== "undefined") (() => {
  const $d = s => document.querySelector(s);
  const escD = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
  const fmtD = (n, d) => n == null || isNaN(n) ? "—" : Number(n).toLocaleString("en-US", {minimumFractionDigits: d ?? (Math.abs(n) >= 1000 ? 0 : 2), maximumFractionDigits: d ?? (Math.abs(n) >= 1000 ? 1 : 2)});
  // Paper gold's venue comes from GridMind's catalog (configs/dualtrack.yaml paper_gold_venue), never from here.
  const paperGoldIds = () => {
    const profile = (window.dashboardControlState?.catalog?.venue_profiles || []).find(p => p.environment === "paper");
    const instrument = (profile?.instruments || []).find(i => i.asset === "XAU");
    return profile && instrument ? [profile.id, instrument.instrument_id] : null;
  };
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
    rail?.insertAdjacentHTML("afterbegin", `<section class="card desk-judge" id="deskToday" aria-labelledby="deskJudgeTitle">
      <div class="card-title"><span id="deskJudgeTitle">今天</span><span class="desk-meta" style="padding:0" id="deskJudgeDate"></span></div>
      <div class="card-body">
        <ol class="desk-steps">
        <li class="desk-step" data-step="1"><span class="desk-n" aria-hidden="true">1</span><div class="desk-step-body">
          <h3>你的判断</h3>
          <div class="desk-chips" id="deskChips" role="group" aria-label="判断哪个品种"></div>
          <div class="desk-reading" id="deskReading"></div>
          <div class="desk-ai" id="deskAi"></div>
          <div class="desk-dir" role="group" aria-label="方向"><button type="button" data-d="long" aria-pressed="false">做多</button><button type="button" data-d="flat" aria-pressed="false">观望</button><button type="button" data-d="short" aria-pressed="false">做空</button></div>
          <label for="deskReason" class="desk-meta" style="padding:0">一句理由</label>
          <textarea id="deskReason" placeholder="比如：美元走强、ETF 净流出，先不追多"></textarea>
          <div class="desk-conf"><label for="deskConf">信心</label><input id="deskConf" type="range" min="1" max="5" value="3"><span id="deskConfV">3 / 5</span></div>
          <div class="desk-cites" id="deskCites">还没有引用新闻</div>
        </div></li>
        <li class="desk-step" data-step="2"><span class="desk-n" aria-hidden="true">2</span><div class="desk-step-body">
          <h3>生成计划</h3>
          <div class="desk-plan" id="deskPlan"><p class="note">先选方向，系统按它生成计划，先给你看。</p></div>
        </div></li>
        <li class="desk-step" data-step="3"><span class="desk-n" aria-hidden="true">3</span><div class="desk-step-body">
          <h3>你亲手启动</h3>
          <div class="desk-plan" id="deskExecBox"></div>
          <div id="deskStop"></div>
        </div></li>
        </ol>
      </div></section>`);
    const note = "日常从「今天」面板开始：选方向 → 批准计划 → 亲手按执行。这里是手动细调，按钮的作用和以前一样。";
    $d(".dashboard-control-actions")?.insertAdjacentHTML("afterend", `<p class="desk-rail-note">${note}</p>`);
    $d(".strategy-card .robot-actions")?.insertAdjacentHTML("afterend", `<p class="desk-rail-note">${note}</p>`);
    mountStatus(wrap, rail);
    $d("#deskJudgeDate").textContent = new Date().toLocaleDateString("zh-CN", {month: "long", day: "numeric"});
    document.querySelectorAll("[data-nf]").forEach(b => b.onclick = () => { D.filter = b.dataset.nf; document.querySelectorAll("[data-nf]").forEach(x => x.setAttribute("aria-pressed", x === b)); renderNews(); });
    $d("#deskNews").addEventListener("click", e => { const li = e.target.closest("li[data-title]"); if (li) toggleCite(li.dataset.title); });
    $d("#deskNews").addEventListener("keydown", e => { const li = e.target.closest("li[data-title]"); if (li && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); toggleCite(li.dataset.title); } });
    document.querySelectorAll(".desk-dir [data-d]").forEach(b => b.onclick = () => { D.d = b.dataset.d; syncDir(); loadPlan(); });
    $d("#deskConf").oninput = () => { $d("#deskConfV").textContent = `${$d("#deskConf").value} / 5`; };
  }

  // ---- status line, retired selection, collapsed GridMind cards --------------------------
  const ICONS = {
    idle: '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="9"></circle><path d="M10 9v6M14 9v6"></path></svg>',
    run: '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="M4 7h16M4 12h16M4 17h16"></path><path d="M3 20l5-6 4 3 4-7 5 4"></path></svg>',
    bad: '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3l9 16H3z"></path><path d="M12 10v4M12 17v.5"></path></svg>',
    info: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="9"></circle><path d="M12 11v5M12 8v.5"></path></svg>',
  };
  ICONS.warn = ICONS.bad; ICONS.wait = ICONS.idle;
  const TABS = {positions: "持仓", orders: "委托", fills: "成交", review: "复盘", history: "收益曲线", supervisor: "运行监督", shadows: "影子策略"};

  function mountStatus(wrap, rail) {
    // First thing on the page, above 本周三件事: what is happening now and the one next step.
    (wrap.parentNode || wrap).insertAdjacentHTML("afterbegin", `<section class="desk-status wait" id="deskStatus" aria-live="polite"></section><div class="desk-retired hidden" id="deskRetired" role="status"></div>`);
    // Every GridMind card stays in the page with its controls; the ones for fine-tuning are folded away.
    rail?.querySelector(".desk-judge")?.insertAdjacentHTML("afterend", `<details class="card desk-more" id="deskManual"><summary>手动配置与 AI 助手<small>Trading BOT · AI 助手 · 趋势评估 · 策略配置与停止</small></summary><div class="desk-more-body"></div></details>`);
    const manual = $d("#deskManual .desk-more-body");
    ["#dashboardControlCard", "#parkAiChatCard", ".market-card", "#strategyCard"].forEach(sel => { const el = $d(sel); if (el && manual) manual.appendChild(el); });
    wrap.insertAdjacentHTML("beforeend", `<details class="card desk-sys" id="deskSystem"><summary><span class="desk-dot" id="deskHealthDot"></span><span id="deskHealth">读取中</span><span class="desk-sys-link">系统详情：运行状态、策略明细、内部记录</span></summary><div class="desk-sys-body"><div class="desk-records" id="deskRecords"></div></div></details>`);
    const system = $d("#deskSystem .desk-sys-body");
    ["#currentStrategyCard", ".live-card"].forEach(sel => { const el = $d(sel); if (el && system) system.appendChild(el); });
    document.querySelectorAll("#tabs [data-tab]").forEach(button => {
      const label = TABS[button.dataset.tab], text = [...button.childNodes].find(node => node.nodeType === 3);
      if (label && text) text.textContent = `${label} `;
      if (["supervisor", "shadows"].includes(button.dataset.tab)) button.classList.add("desk-tab-quiet");
    });
  }

  function reveal(target, details) {
    if (details) details.open = true;
    if (!target) return;
    target.scrollIntoView({behavior: "smooth", block: "center"});
    setTimeout(() => target.focus?.({preventScroll: true}), 350);
  }

  function statusAsset() {
    const asset = selectionAsset();
    if (asset) return asset.kind === "xau_paper" ? "黄金" : asset.label;
    return String(window.gridmindView?.market?.symbol || "GOLD").toUpperCase() === "GOLD" ? "黄金" : String(window.gridmindView.market.symbol);
  }

  function renderStatus() {
    const view = window.gridmindView, control = window.dashboardControlState;
    const banner = DeskStatus.banner(view, {asset: statusAsset()}), health = DeskStatus.health(view), retired = DeskStatus.retired(control?.selection, control?.catalog);
    const signature = JSON.stringify([banner, health, retired, D.switching]);
    if (signature === D.statusSignature) return;
    D.statusSignature = signature;
    document.body.classList.toggle("desk-idle", banner.tone === "idle");
    const box = $d("#deskStatus");
    box.className = `desk-status ${banner.tone}`;
    box.innerHTML = `<span class="desk-status-ic">${ICONS[banner.tone] || ICONS.idle}</span><div class="desk-status-text"><h1>${escD(banner.title)}</h1>${banner.body ? `<p>${escD(banner.body)}</p>` : ""}</div>`
      + (banner.action ? `<button type="button" class="desk-btn big ${banner.action.kind === "start" ? "primary" : ""}" id="deskStatusAction">${escD(banner.action.label)}</button>` : "");
    const action = $d("#deskStatusAction");
    if (action) action.onclick = () => {
      if (banner.action.kind === "start") reveal($d(".desk-dir [data-d]"));
      else if (banner.action.kind === "details") reveal($d("#deskSystem summary"), $d("#deskSystem"));
      // Stop is never pressed for Park: this only takes him to the existing stop button.
      else if (D.desk?.meta?.kind === "hl_testnet" && $d("#deskStopBtn")) reveal($d("#deskStopBtn"));
      else reveal($d("#stopRobot"), $d("#deskManual"));
    };
    const note = $d("#deskRetired");
    note.classList.toggle("hidden", !retired);
    if (retired) {
      const gold = D.assets.find(a => a.kind === "xau_paper");
      note.innerHTML = `<span class="desk-retired-ic">${ICONS.info}</span><span class="t">你之前选的 <b>${escD(retired.old)}</b> 已停用${retired.next ? `，黄金现在走 <b>${escD(retired.next)}</b>` : ""}。重新选一次就能用，旧选择不会被自动替换。</span>`
        + (gold ? `<button type="button" class="desk-btn" id="deskRetiredPick" ${D.switching ? "disabled" : ""}>改选${retired.next ? ` ${escD(retired.next)}` : ""} 黄金</button>` : "");
      const pick = $d("#deskRetiredPick");
      if (pick) pick.onclick = () => switchAsset(gold.key);
    }
    $d("#deskHealth").textContent = health.text;
    $d("#deskHealthDot").className = `desk-dot ${health.tone}`;
    const code = list => list.map(c => `<code>${escD(c)}</code>`).join(" ");
    $d("#deskRecords").innerHTML = (health.other.length ? `<p><b class="desk-warn-text">需要看一下</b> ${code(health.other)}</p>` : "")
      + (health.expected.length ? `<p><span class="desk-muted-text">没有计划时的正常记录</span> ${code(health.expected)}</p>` : "");
  }

  // ---- asset = GridMind's venue selection -------------------------------------------
  function selectionAsset() {
    const sel = window.dashboardControlState?.selection;
    if (!sel || !D.assets.length) return null;
    if (String(sel.venue_profile_id || "").endsWith(".paper")) return D.assets.find(a => a.kind === "xau_paper") || null;
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
    const ids = asset.kind === "xau_paper" ? paperGoldIds() : ["hyperliquid.testnet", asset.instrument_id];
    // Already there only when the instrument is chosen too (a venue alone is half a selection).
    if (asset.key === selectionAsset()?.key && ids && window.dashboardControlState?.selection?.instrument_id === ids[1]) { applyAsset(); return; }
    const venue = $d("#dashboardVenueSelect"), instrument = $d("#dashboardInstrumentSelect");
    if (!venue || !instrument || !ids) return;
    const [venueId, instrumentId] = ids;
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
    $d("#deskPlan").innerHTML = `<p class="note">先选方向，系统按它生成计划，先给你看。</p>`;
    $d("#deskExecBox").innerHTML = "";
    $d("#deskJudgeTitle").textContent = `今天 · ${asset.label}`;
    $d("#deskStop").innerHTML = "";
    syncSteps();
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
  function syncDir() { document.querySelectorAll(".desk-dir [data-d]").forEach(b => b.setAttribute("aria-pressed", b.dataset.d === D.d)); syncSteps(); }
  // Step marks only: which step is done and which is next. The flow and its endpoints are unchanged.
  function syncSteps() {
    const exec = $d("#deskExecBox");
    if (!exec) return;
    const ready = Boolean(exec.querySelector(".desk-exec"));
    if (!ready) exec.innerHTML = `<button type="button" class="desk-btn wide" disabled>${D.plan?.kind === "new" ? "先批准第 2 步的计划" : D.plan ? "这份计划不需要新启动" : "先完成前两步"}</button>`;
    const done = {1: Boolean(D.d), 2: ready, 3: false};
    const next = [1, 2, 3].find(n => !done[n]);
    document.querySelectorAll(".desk-step").forEach(li => { const n = +li.dataset.step; li.classList.toggle("done", done[n]); li.classList.toggle("on", n === next); });
  }
  async function loadPlan() {
    if (!D.d || !D.asset) return;
    const box = $d("#deskPlan"); box.innerHTML = `<p class="note">正在生成计划…</p>`;
    D.plan = null; $d("#deskExecBox").innerHTML = ""; syncSteps();
    try { D.plan = await call("/api/plan", {asset: D.asset, direction: D.d}); } catch (e) { box.innerHTML = `<p class="warn">${escD(e.message)}</p>`; return; }
    const p = D.plan, tag = {keep: "沿用当前网格", new: "需要批准", hold: "不开新单", unavailable: "暂不可用", record_only: "只记录"}[p.kind] || p.kind;
    const rungs = p.rungs ? `<dt>价位</dt><dd>${p.rungs.map(r => fmtD(r)).join(" / ")}</dd>` : "";
    box.innerHTML = `<h4><span>${escD(p.title)}</span><span class="pill">${tag}</span></h4>
      <dl>${p.lines.map(([k, v]) => `<dt>${escD(k)}</dt><dd>${escD(v)}</dd>`).join("")}${rungs}</dl><p class="note">${escD(p.note)}</p>
      <div class="desk-row"><button type="button" class="desk-btn primary" id="deskApprove" ${p.kind === "unavailable" ? "disabled" : ""}>${p.kind === "new" ? "批准这份计划" : "确认并记录"}</button>${p.kind === "record_only" ? "" : `<button type="button" class="desk-btn" id="deskRecord">只记录判断</button>`}</div>
      <div class="desk-toast" id="deskToast" role="status"></div>`;
    $d("#deskApprove").onclick = () => submit(p.kind === "new" ? "approved" : "recorded");
    if ($d("#deskRecord")) $d("#deskRecord").onclick = () => submit("recorded");
    syncSteps();
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
    const plan = $d("#deskExecBox"); plan.innerHTML = "";
    const box = document.createElement("div"); box.className = "desk-exec"; box.style.display = "grid"; box.style.gap = "7px";
    const paper = D.desk?.meta?.kind === "xau_paper", unit = paper ? "USDT" : "USDC";
    if (done) {
      const txt = {executed: paper ? "已执行：计划已确认，纸面盘约 1 分钟内挂单。" : "已执行：网格已挂上测试盘。", execute_started: "执行中或上次中断，请到系统页查看记录。", execute_failed: "上次执行没有完成，请到系统页查看记录。", refused: `上次执行被拒绝：${done.detail?.reason || ""}`}[done.stage];
      box.innerHTML = `<p class="note">${escD(txt)}</p>`; plan.appendChild(box); syncSteps(); return;
    }
    const BLOCKER = {instrument_not_eligible: "这个品种还没在交易系统里开通下单（测试盘目前只开通了 BTC）"};
    if (!preview.execution_ready) { box.innerHTML = `<p class="warn">预览没通过，不能执行：${escD((preview.blockers || []).map(b => BLOCKER[b] || b).join("；"))}</p>`; plan.appendChild(box); syncSteps(); return; }
    box.innerHTML = `<dl><dt>${paper ? "纸面盘预览价位" : "交易所预览价位"}</dt><dd>${(preview.orders || []).map(o => fmtD(o.price)).join(" / ")}</dd><dt>预览最多亏</dt><dd>${fmtD(preview.max_loss)} ${unit}</dd></dl>
      <p class="warn">${paper ? "按下后确认这份黄金纸面盘计划（模拟资金），约 1 分钟内挂单。已有黄金网格在跑时系统会拒绝。" : "按下后会在 Hyperliquid 测试盘真实挂单（假钱）。已有网格在跑时系统会拒绝。"}</p>
      <button type="button" class="desk-btn exec" id="deskExec">执行</button><div class="desk-toast" id="deskExecToast" role="status"></div>`;
    plan.appendChild(box); syncSteps();
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
    syncSteps();
    renderStatus();
    setInterval(renderStatus, 1000);  // follows whatever GridMind last rendered
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
