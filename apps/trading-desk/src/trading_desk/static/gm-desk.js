// Desk layer injected into the GridMind console (served at /trade): nav + asset switch, one status
// line, and step 3 of 「今天」 (执行, 停止, 暂停补单). Steps 1–2 (with the news) are a React island
// (apps/web TodayPanel, #1327, #1435); this file keeps which asset is selected, because that
// drives GridMind's own selects, and every order or control request until #1329.
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
  const signed = v => `${Number(v) > 0 ? "+" : Number(v) < 0 ? "−" : ""}${num(Math.abs(Number(v)))}`;
  const bjTime = stamp => { const t = beijing(stamp); return t ? `${bjDay(stamp)} ${String(t.getUTCHours()).padStart(2, "0")}:${String(t.getUTCMinutes()).padStart(2, "0")}` : "--"; };

  function tickText(data) {
    const tick = data?.runtime?.execution_tick_health || {};
    if (!finite(tick.age_seconds)) return "";
    return `执行心跳 ${num(tick.age_seconds)} 秒没更新${finite(tick.max_age_seconds) ? `（上限 ${num(tick.max_age_seconds)} 秒）` : ""}。`;
  }

  function isActive(view) {
    const data = view?.data || {}, plan = view?.livePlan;
    const lifecycle = plan?.source === "testnet_grid_lifecycle" ? plan : null;
    return data.current_strategy?.active === true || Boolean(lifecycle && !ENDED.has(String(lifecycle.status || "")));
  }

  // GridMind's own run verdict (#1429): the header pill it no longer shows on /trade. Absent on an older page.
  const runOf = view => view?.runState || {};
  // Start is offered only here: read ok, data present, feed trusted, nothing running, GridMind says stopped.
  const calmRun = run => !run.tickLost && (run.runClass === undefined || run.runClass === "stopped");

  // 「行情可信」 (#1441): the chart's feed must be trusted, and so must the read model's feed (the configured market the
  // backend's completeness check reads) whenever it is the same instrument as the chart. Never looser than the
  // chart alone. The read model's market stays on the configured symbol, so for another asset it is not consulted:
  // a gold feed never vouches for BTC. `failed` is the feed that did not pass, so the wording comes from one object.
  function feedTrust(view) {
    const chart = view?.execMarket || view?.market || view?.data?.market || {}, gate = view?.gateMarket;  // #1443: execMarket is the selected symbol's 1m feed
    if (view?.marketTrusted !== true) return {trusted: false, failed: chart};
    const symbol = m => String(m?.symbol || "").toUpperCase();
    const same = gate && (!symbol(gate) || !symbol(chart) || symbol(gate) === symbol(chart));
    if (same && gate.trusted !== true) return {trusted: false, failed: gate};
    return {trusted: true, failed: null};
  }

  function banner(view, {asset = "黄金"} = {}) {
    // A failed read outranks everything: what is on the page is no longer known to be true.
    if (view?.readError) {
      const old = view.data && view.renderedAt ? `页面上的数字停在 ${bjTime(view.renderedAt)} 那次读取，不代表现在。` : "";
      return {tone: "bad", title: view.data ? "读不到最新交易状态" : "读不到交易状态", body: [view.readError, old].filter(Boolean).join(" "), action: null};
    }
    if (!view?.data) return {tone: "wait", title: "正在读取交易状态", body: "", action: null};
    const data = view.data, feed = feedTrust(view), market = feed.failed || view.market || data.market || {}, current = data.current_strategy || {};
    if (!feed.trusted) {
      const last = market.latest_timestamp ? `最后可信数据 ${bjTime(market.latest_timestamp)}` : "还没有可信数据";
      return {tone: "bad", title: `${asset}行情中断，已停止开新仓`,
        body: `${String(market.provider || "").toLowerCase() === "unavailable" || !market.provider_label ? "行情源现在没有返回可用数据" : `${market.provider_label}的数据没有通过可信检查`}（${last}）。恢复之前不会开新仓，也不会改用别的数据源顶替。页面会自动重试。`,
        action: {kind: "details", label: "查看原因"}};
    }
    if (isActive(view)) {
      const plan = view.livePlan || {}, spec = current.specification || {}, ex = current.execution || {}, counts = data.execution?.counts || {};
      const lifecycle = plan.source === "testnet_grid_lifecycle" ? String(plan.status || "") : "";
      const low = spec.lower_price_boundary ?? plan.range?.low, high = spec.upper_price_boundary ?? plan.range?.high;
      const rungs = spec.grid_rung_count ?? plan.grid?.count, fills = ex.fill_count ?? counts.fill_count, held = ex.open_position_count ?? counts.open_position_count;
      const type = String(spec.strategy_type || plan.strategy_type || "grid").toLowerCase() === "dca" ? "定投" : "网格";
      const direction = {long: "做多", short: "做空", neutral: "中性"}[spec.direction || plan.direction] || null;
      // #1449: range, grids, fills and positions are said once, in 「这一轮」; the banner keeps what changes by the minute.
      const pnl = data.execution?.pnl || {}, parts = [finite(pnl.unrealized) ? `浮动盈亏 ${signed(pnl.unrealized)} USD` : null].filter(Boolean);
      const bad = /hard_stop|flatten/.test(lifecycle), paused = /paused/.test(lifecycle), run = runOf(view), stop = {kind: "stop", label: "去停止"};
      if (run.tickLost) return {tone: "warn", title: `${asset}${type}运行降级`, body: [tickText(data), parts.join(" · ")].filter(Boolean).join(" "), action: stop};
      if (!lifecycle && run.runClass === "error") return {tone: "bad", title: `${asset}${type}运行异常`, body: ["运行状态没有通过检查（风控、对账或状态记录），原因见系统详情。", parts.join(" · ")].filter(Boolean).join(" "), action: stop};
      return {tone: bad ? "bad" : paused ? "warn" : "run", title: `${asset}${type}${LIFECYCLE[lifecycle] || "运行中"}`, body: parts.join(" · "), action: stop};
    }
    const run = runOf(view), details = {kind: "details", label: "查看原因"};
    if (run.tickLost) return {tone: "warn", title: "执行心跳中断", body: `${tickText(data) || "执行引擎的心跳没有按时更新。"} 恢复之前不要开始新计划。`, action: details};
    if (run.runClass === "error") return {tone: "bad", title: "运行状态异常", body: "交易系统报告运行异常。先查看原因，恢复之前不要开始新计划。", action: details};
    if (run.runClass === "working") return {tone: "wait", title: `${asset}${run.label || "处理中"}`, body: "", action: null};
    if (!calmRun(run)) return {tone: "warn", title: `${asset}${run.label || "状态未知"}`, body: "运行状态和当前策略记录对不上，先查看原因。", action: details};
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
    // The one way in is 「今天」's steps (#1438); the banner says so instead of offering a second start button.
    // `startable` marks the calm idle state, the only one where starting a new plan is right.
    return {tone: "idle", title: "现在没有在跑的策略", body, next: "今天的计划从「今天」第 1 步开始。", action: null, startable: true};
  }

  // 「这一轮」 (#1449): shown instead of 「今天」's steps whenever the banner says a plan runs (same isActive), whatever
  // the feed or the last read says, so Park can always see the grid. Only facts a field backs; nothing to press.
  function thisRun(view) {
    if (!view?.data || !isActive(view)) return null;
    const data = view.data, plan = view.livePlan || {}, current = data.current_strategy || {}, spec = current.specification || {};
    const ex = current.execution || {}, counts = data.execution?.counts || {}, pnl = data.execution?.pnl || {};
    const lifecycle = plan.source === "testnet_grid_lifecycle" ? String(plan.status || "") : "";
    const low = spec.lower_price_boundary ?? plan.range?.low, high = spec.upper_price_boundary ?? plan.range?.high;
    const rungs = spec.grid_rung_count ?? plan.grid?.count, spacing = spec.grid_spacing;
    const fills = ex.fill_count ?? counts.fill_count, held = ex.open_position_count ?? counts.open_position_count;
    const type = String(spec.strategy_type || plan.strategy_type || "grid").toLowerCase() === "dca" ? "定投" : "网格";
    const direction = {long: "做多", short: "做空", neutral: "中性"}[spec.direction || plan.direction] || null;
    const facts = [], add = (label, value) => facts.push({label, value});
    if (finite(low) && finite(high)) add("区间", `${num(low)} – ${num(high)}`);
    if (direction) add("方向", direction);
    if (finite(rungs) && Number(rungs) > 0) add("格数", `${num(rungs)} 格${finite(spacing) && Number(spacing) > 0 ? ` · 间距 ${num(spacing)}` : ""}`);
    if (finite(fills)) add("成交", `${num(fills)} 次`);
    if (finite(held)) add("持仓", `${num(held)} 笔`);
    if (finite(spec.stop_price)) add("止损", num(spec.stop_price));
    if (finite(spec.take_profit_price)) add("止盈", num(spec.take_profit_price));
    // The plan's worst case against its limit, not risk used so far.
    if (finite(spec.theoretical_max_loss)) add("计划最坏", `−${num(Math.abs(spec.theoretical_max_loss))}${finite(spec.maximum_acceptable_loss) ? ` / 上限 −${num(Math.abs(spec.maximum_acceptable_loss))}` : ""} USD`);
    // execution.pnl's realized part spans every plan (scope all_versioned_production_plans), so only the open
    // positions' unrealized figure belongs to this run.
    if (finite(pnl.unrealized)) add("浮动盈亏", `${signed(pnl.unrealized)} USD`);
    return {title: `这一轮 · ${type}${LIFECYCLE[lifecycle] || "运行中"}`, facts,
      stale: view.readError ? "最新状态读不到，下面是上次读到的。" : null};
  }

  // Only claims a field backs. Never "all good": the calm state is grey, not green.
  function health(view) {
    if (view?.readError) return {tone: "bad", text: "读不到最新状态", other: [], expected: []};
    if (!view?.data) return {tone: "wait", text: "读取中", other: [], expected: []};
    const data = view.data, active = isActive(view), issues = [...(data.completeness?.issues || [])];
    const rec = data.execution?.reconciliation;
    if (rec?.status && !["ok", "pass", "reconciled"].includes(rec.status)) issues.push(`reconciliation_${rec.status}${rec.reason ? `:${rec.reason}` : ""}`);
    const expected = active ? [] : issues.filter(code => IDLE_EXPECTED.has(code)), other = issues.filter(code => !expected.includes(code));
    // Whether a plan runs is the banner's to say, once (#1429).
    const trusted = feedTrust(view).trusted, parts = [trusted ? "行情可信" : "行情没有通过可信检查"];
    if (other.length) parts.push(`${other.length} 项需要看一下`);
    return {tone: !trusted ? "bad" : other.length ? "warn" : "calm", text: parts.join(" · "), other, expected};
  }

  function retired(selection, catalog) {
    const old = selection?.retired_selection;
    if (!old || selection?.venue_profile_id) return null;
    const [venue, env] = String(old.venue_profile_id || "").split(".");
    const label = venue ? `${venue.charAt(0).toUpperCase()}${venue.slice(1)}${env === "paper" ? " Paper" : ""}` : "之前的交易场所";
    const paper = (catalog?.venue_profiles || []).find(p => p.environment === "paper");
    return {old: label, next: paper?.label || null};
  }

  return {banner, health, retired, isActive, calmRun, feedTrust, thisRun};
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
  // Paper grids (ADR 0009): gold, silver and crude; one runs at a time. All settle in USDT.
  const PAPER_KINDS = new Set(["xau_paper", "xag_paper", "cl_paper"]);
  const D = {assets: [], asset: null, desk: null, steps: {}, switching: false};
  // The React islands get the asset through data-props; an unchanged value is not rewritten, so the
  // 2-second follow loop never re-renders (or resets) them.
  const setProps = (el, value) => { const text = JSON.stringify(value); if (el && el.dataset.props !== text) el.dataset.props = text; };
  function pushProps() {
    setProps($d('[data-island="today"]'), {asset: D.asset,
      assets: D.assets.map(a => ({key: a.key, label: a.label, kind: a.kind, instrument_id: a.instrument_id, capabilities: a.capabilities || []})),
      gridmind: D.assets.filter(onGridMind).map(a => a.key)});
  }

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
    // The news and the week's three events are inside 「今天」 step 1 (apps/web TodayPanel, #1435).
    layout.appendChild(wrap);
    const rail = $d(".control-rail");
    // Steps 1–2 are a React island (apps/web TodayPanel, #1327); step 3 stays here until #1329.
    rail?.insertAdjacentHTML("afterbegin", `<section class="card desk-judge" id="deskToday" aria-labelledby="deskJudgeTitle">
      <div class="card-title"><span id="deskJudgeTitle">今天</span><span class="desk-meta" style="padding:0" id="deskJudgeDate"></span></div>
      <div class="card-body">
        <div class="desk-today" data-island="today" data-props="{}"></div>
        <ol class="desk-steps desk-steps-exec">
        <li class="desk-step" data-step="3"><span class="desk-n" aria-hidden="true">3</span><div class="desk-step-body">
          <h3>你亲手启动</h3>
          <div class="desk-plan" id="deskExecBox"></div>
          <div id="deskStop"></div>
        </div></li>
        </ol>
      </div></section>`);
    const note = "日常按上面的 1-2-3 走：选方向 → 批准计划 → 亲手按执行。这里是手动细调，按钮的作用和以前一样。";
    $d(".gm-control-actions")?.insertAdjacentHTML("afterend", `<p class="desk-rail-note">${note}</p>`);
    $d(".strategy-card .robot-actions")?.insertAdjacentHTML("afterend", `<p class="desk-rail-note">${note}</p>`);
    mountStatus(wrap, rail);
  }

  // ---- status line, retired selection, collapsed GridMind cards --------------------------
  const ICONS = {
    idle: '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="9"></circle><path d="M10 9v6M14 9v6"></path></svg>',
    run: '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="M4 7h16M4 12h16M4 17h16"></path><path d="M3 20l5-6 4 3 4-7 5 4"></path></svg>',
    bad: '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3l9 16H3z"></path><path d="M12 10v4M12 17v.5"></path></svg>',
    info: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="9"></circle><path d="M12 11v5M12 8v.5"></path></svg>',
  };
  ICONS.warn = ICONS.bad; ICONS.wait = ICONS.idle;
  const TABS = {positions: "持仓", orders: "委托", fills: "成交", review: "交易回看", history: "收益曲线", supervisor: "运行监督", shadows: "影子策略"};

  function mountStatus(wrap, rail) {
    // First thing on the page, above 本周三件事: what is happening now and the one next step.
    (wrap.parentNode || wrap).insertAdjacentHTML("afterbegin", `<section class="desk-status wait" id="deskStatus" aria-live="polite"></section><div class="desk-retired hidden" id="deskRetired" role="status"></div>`);
    // Every GridMind card stays in the page with its controls; the ones for fine-tuning are folded away.
    rail?.querySelector(".desk-judge .desk-steps-exec")?.insertAdjacentHTML("beforebegin", `<details class="card desk-more" id="deskManual"><summary>自己调<small>手动设网格参数 · AI 助手 · 趋势评估 · 策略配置与停止</small></summary><div class="desk-more-body"></div></details>`);
    const manual = $d("#deskManual .desk-more-body");
    ["#dashboardControlCard", "#parkAiChatCard", ".market-card", "#strategyCard"].forEach(sel => { const el = $d(sel); if (el && manual) manual.appendChild(el); });
    wrap.insertAdjacentHTML("beforeend", `<details class="card desk-sys" id="deskSystem"><summary><span class="desk-dot" id="deskHealthDot"></span><span id="deskHealth">读取中</span><span class="desk-sys-link">系统详情：运行状态、策略明细、内部记录</span></summary><div class="desk-sys-body"><div class="desk-records" id="deskRecords"></div><nav class="desk-sys-tabs" id="deskSysTabs" aria-label="更多记录"><span>更多记录：</span><button data-open-tab="supervisor" type="button">运行监督</button><button data-open-tab="shadows" type="button">影子策略</button><button data-open-tab="history" type="button">收益曲线</button></nav></div></details>`);
    const system = $d("#deskSystem .desk-sys-body");
    ["#currentStrategyCard", ".live-card"].forEach(sel => { const el = $d(sel); if (el && system) system.appendChild(el); });
    document.querySelectorAll("#tabs [data-tab]").forEach(button => {
      const label = TABS[button.dataset.tab], text = [...button.childNodes].find(node => node.nodeType === 3);
      if (label && text) text.textContent = `${label} `;
      if (["supervisor", "shadows"].includes(button.dataset.tab)) button.classList.add("desk-tab-quiet");
      if (["supervisor", "shadows", "history"].includes(button.dataset.tab)) button.classList.add("desk-tab-system");
    });
  }

  // #1453: 运行监督 / 影子策略 / 收益曲线 open from 系统详情. Navigation only: it presses GridMind's own (hidden) tab.
  document.addEventListener("click", event => {
    const link = event.target.closest?.("[data-open-tab]");
    if (!link) return;
    const tab = document.querySelector(`#tabs [data-tab="${link.dataset.openTab}"]`);
    if (!tab) return;
    tab.click();
    reveal(document.querySelector(".data-card"));
  });

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

  // 「这一轮」 (#1449) sits at the top of 「今天」's body; while it shows, steps 1–2 and step 3's plan box are hidden by
  // CSS (body.desk-running) and step 3's stop area and 「自己调」 stay.
  function renderThisRun(run) {
    let box = $d("#deskThisRun");
    if (!box) {
      $d("#deskToday .card-body")?.insertAdjacentHTML("afterbegin", '<section class="desk-thisrun" id="deskThisRun" aria-live="polite"></section>');
      box = $d("#deskThisRun");
    }
    document.body.classList.toggle("desk-running", Boolean(run));
    if (!box) return;
    box.innerHTML = run ? `<h3>${escD(run.title)}</h3>${run.stale ? `<p class="warn">${escD(run.stale)}</p>` : ""}<dl>${run.facts.map(f => `<div><dt>${escD(f.label)}</dt><dd>${escD(f.value)}</dd></div>`).join("")}</dl>` : "";
  }

  function renderStatus() {
    const view = window.gridmindView, control = window.dashboardControlState;
    const banner = DeskStatus.banner(view, {asset: statusAsset()}), health = DeskStatus.health(view), retired = DeskStatus.retired(control?.selection, control?.catalog);
    const run = DeskStatus.thisRun(view);
    const signature = JSON.stringify([banner, health, retired, run, D.switching]);
    if (signature === D.statusSignature) return;
    D.statusSignature = signature;
    document.body.classList.toggle("desk-idle", banner.tone === "idle");
    renderThisRun(run);
    const box = $d("#deskStatus");
    box.className = `desk-status ${banner.tone}`;
    box.innerHTML = `<span class="desk-status-ic">${ICONS[banner.tone] || ICONS.idle}</span><div class="desk-status-text"><h1>${escD(banner.title)}</h1>${banner.body ? `<p>${escD(banner.body)}</p>` : ""}${banner.next ? `<p class="desk-status-next">${escD(banner.next)}</p>` : ""}</div>`
      + (banner.action ? `<button type="button" class="desk-btn big" id="deskStatusAction">${escD(banner.action.label)}</button>` : "");
    const action = $d("#deskStatusAction");
    if (action) action.onclick = () => {
      if (banner.action.kind === "details") reveal($d("#deskSystem summary"), $d("#deskSystem"));
      // Stop is never pressed for Park: this only takes him to the existing stop button.
      else if (can("stop") && $d("#deskStopBtn")) reveal($d("#deskStopBtn"));
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
    if (String(sel.venue_profile_id || "").endsWith(".paper")) {
      // A Paper selection names its instrument (#1407): silver or crude by GridMind's catalog id, else gold.
      return D.assets.find(a => a.paper_instrument_id && a.paper_instrument_id === sel.instrument_id)
        || D.assets.find(a => a.kind === "xau_paper") || null;
    }
    return D.assets.find(a => a.kind === "hl_testnet" && a.instrument_id === sel.instrument_id) || null;
  }
  // The one place an asset is chosen (#1433): every desk asset is here. BTC and gold switch GridMind (chart and
  // trading); the others are judged on the desk while GridMind stays on its chart.
  const deskOnlyNote = a => !(a.capabilities || []).includes("grid_plan") ? "只记判断和复盘；图表不切换" : "在交易台判断和计划；图表不切换";
  function renderAssets() {
    const box = $d("#deskAssets");
    if (!box) return;
    const signature = `${D.asset}|${D.switching}|${D.assets.map(a => a.key).join(",")}`;
    if (signature === D.assetSignature) return;
    D.assetSignature = signature;
    box.innerHTML = D.assets.map(a => {
      const gm = onGridMind(a);
      return `<button type="button" data-asset="${escD(a.key)}" aria-pressed="${a.key === D.asset}" ${gm ? "" : `data-desk-only title="${escD(deskOnlyNote(a))}"`} ${D.switching ? "disabled" : ""}>${escD(a.label)}</button>`;
    }).join("");
    box.querySelectorAll("[data-asset]").forEach(b => b.onclick = () => pickAsset(b.dataset.asset));
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
    const picker = window.dashboardControlState?.picker;
    if (!picker || !ids) return;
    const [venueId, instrumentId] = ids;
    D.switching = true; renderAssets();
    try {
      // Intents with an argument (ADR 0008): the selects belong to GridMind's picker, so nothing here writes to them.
      if (picker.venue !== venueId) window.gmIntent.fire("change", "control.venue", {id: venueId});
      await waitFor(() => picker.instruments.some(o => o.value === instrumentId && !o.disabled));
      window.gmIntent.fire("change", "control.instrument", {id: instrumentId});
      await waitFor(() => window.dashboardControlState?.selection?.instrument_id === instrumentId);
    } finally { D.switching = false; }
    applyAsset();
  }
  // BTC and gold follow GridMind; ETH, silver and oil are judged here while GridMind stays on its chart.
  const onGridMind = a => a.kind === "xau_paper" || a.instrument_id === "BTC-USD-PERP";
  // A top-bar button was pressed: BTC and gold switch GridMind; the others are judged here.
  function pickAsset(key) {
    const asset = D.assets.find(a => a.key === key);
    if (!asset || asset.key === D.asset) return;
    if (onGridMind(asset)) { switchAsset(asset.key); return; }
    D.override = asset.key; focusAsset(asset);
  }
  function focusAsset(asset) {
    const changed = asset.key !== D.asset;
    D.asset = asset.key;
    renderAssets(); pushProps();  // the islands reset themselves for a new asset (keyed by it)
    if (!changed) return;
    D.desk = null; D.steps = {};
    $d("#deskExecBox").innerHTML = "";
    $d("#deskJudgeTitle").textContent = `今天 · ${asset.label}`;
    $d("#deskStop").innerHTML = "";
    syncSteps();
  }
  // No usable GridMind selection (none yet, or a retired one): judge what the chart shows, so the chart and 「今天」
  // name the same asset (#1433). Starting still needs Park's own pick; nothing here selects a venue.
  function chartAsset() {
    const symbol = String(window.gridmindView?.market?.symbol || "").toUpperCase();
    const gold = D.assets.find(a => a.kind === "xau_paper"), btc = D.assets.find(a => a.instrument_id === "BTC-USD-PERP");
    return (symbol === "GOLD" && gold) || (symbol === "BTC" && btc) || btc || D.assets[0];
  }
  function applyAsset() {
    if (D.override) return;  // Park is judging an asset GridMind does not chart
    const asset = selectionAsset();
    if (!asset) return;
    document.body.classList.toggle("desk-gold", asset.kind === "xau_paper");
    if (asset.key === D.asset) { renderAssets(); pushProps(); return; }
    focusAsset(asset);  // sets D.asset and resets the panel
  }

  // ---- what the 「今天」 island reports (#1327) ----------------------------------------
  window.addEventListener("desk:desk", event => {
    if (event.detail?.asset !== D.asset) return;
    D.desk = event.detail.desk; renderStop(D.desk?.control);
  });
  window.addEventListener("desk:exec", event => {
    const detail = event.detail || {};
    if (detail.asset !== D.asset) return;
    if (detail.clear) { $d("#deskExecBox").innerHTML = ""; syncSteps(); return; }
    renderExec(detail.judgmentId, detail.preview, detail.done || null);
  });
  window.addEventListener("desk:steps", event => {
    if (event.detail?.asset !== D.asset) return;
    D.steps = event.detail; syncSteps();
  });
  const reloadToday = () => window.dispatchEvent(new Event("desk:reload"));
  // What this asset's venue module declared it can do (ADR 0007); the page never decides by the kind's name.
  function can(capability) { return (D.desk?.meta?.capabilities || []).includes(capability); }

  function renderStop(c) {
    const box = $d("#deskStop");
    if (!c || !can("stop")) { box.innerHTML = ""; return; }
    if (c.stopping) { box.innerHTML = `<p class="desk-toast" style="color:var(--gm-warn)">正在停止：撤单、平仓中，约 1–2 分钟</p>`; return; }
    if (!c.can_stop) { box.innerHTML = ""; return; }
    const held = c.rearm_paused === true;
    box.innerHTML = `${held ? `<p class="desk-hold">⏸ 补单已暂停：现有挂单、止盈、止损不动，止盈后不补新单。</p>` : ""}
      <div class="desk-row">${can("rearm_hold") ? `<button type="button" class="desk-btn hold" id="deskHoldBtn">${held ? "恢复补单" : "暂停补单"}</button>` : ""}
      <button type="button" class="desk-btn stop" id="deskStopBtn">停止网格（撤单、平仓）</button></div><div class="desk-toast" id="deskStopToast" role="status"></div>`;
    if ($d("#deskHoldBtn")) $d("#deskHoldBtn").onclick = async () => {
      const text = held ? `恢复 ${D.asset} 测试盘网格补单？\n只补现价下方（做空则上方）的格子。` : `暂停 ${D.asset} 测试盘网格补单？\n现有挂单、止盈、止损都不动；成交止盈后不再补新单，直到你恢复。`;
      if (!confirm(text)) return;
      $d("#deskHoldBtn").disabled = true;
      try { const r = await call("/api/grid/rearm-hold", {asset: D.asset, paused: !held}); $d("#deskStopToast").textContent = r.message; reloadToday(); }
      catch (e) { $d("#deskStopToast").textContent = e.message; $d("#deskHoldBtn").disabled = false; }
    };
    $d("#deskStopBtn").onclick = async () => {
      if (!confirm(`确认停止 ${D.asset} 测试盘网格？\n会撤掉全部挂单；如果有持仓，会按市价平掉。`)) return;
      $d("#deskStopBtn").disabled = true;
      try { const r = await call("/api/grid/stop", {asset: D.asset, confirm_text: "停止"}); $d("#deskStopToast").textContent = r.message; setTimeout(reloadToday, 1500); }
      catch (e) { $d("#deskStopToast").textContent = e.message; $d("#deskStopBtn").disabled = false; }
    };
  }
  // ---- step 3: 执行 (same flow, endpoint and body as before; moves to React in #1329) ----------
  // Steps 1–2 mark themselves in the island; this marks step 3 and its placeholder from desk:steps.
  function syncSteps() {
    const exec = $d("#deskExecBox");
    if (!exec) return;
    const ready = Boolean(exec.querySelector(".desk-exec")), kind = D.steps.planKind;
    if (!ready) exec.innerHTML = `<button type="button" class="desk-btn wide" disabled>${kind === "new" ? "先批准第 2 步的计划" : kind ? "这份计划不需要新启动" : "先完成前两步"}</button>`;
    $d('.desk-step[data-step="3"]')?.classList.toggle("on", Boolean(D.steps.direction && ready));
  }
  // 这一笔为什么 for this judgment, under a blocked preview or a refused or unfinished order (#1374). GET only.
  const whyIsland = id => `<div class="desk-exec-why" data-island="why" data-props="${escD(JSON.stringify({judgment: id}))}"></div>`;
  function renderExec(judgmentId, preview, done) {
    const plan = $d("#deskExecBox"); plan.innerHTML = "";
    const box = document.createElement("div"); box.className = "desk-exec"; box.style.display = "grid"; box.style.gap = "7px";
    const paper = PAPER_KINDS.has(D.desk?.meta?.kind), unit = paper ? "USDT" : "USDC";
    if (done) {
      const txt = {executed: paper ? "已执行：计划已确认，纸面盘约 1 分钟内挂单。" : "已执行：网格已挂上测试盘。", execute_started: "执行中或上次中断，请到系统页查看记录。", execute_failed: "上次执行没有完成，请到系统页查看记录。", refused: `上次执行被拒绝：${done.detail?.reason || ""}`}[done.stage];
      box.innerHTML = `<p class="note">${escD(txt)}</p>${done.stage === "executed" ? "" : whyIsland(judgmentId)}`; plan.appendChild(box); syncSteps(); return;
    }
    const BLOCKER = {instrument_not_eligible: "这个品种还没在交易系统里开通下单（测试盘目前只开通了 BTC）"};
    if (!preview.execution_ready) { box.innerHTML = `<p class="warn">预览没通过，不能执行：${escD((preview.blockers || []).map(b => BLOCKER[b] || b).join("；"))}</p>${whyIsland(judgmentId)}`; plan.appendChild(box); syncSteps(); return; }
    box.innerHTML = `<dl><dt>${paper ? "纸面盘预览价位" : "交易所预览价位"}</dt><dd>${(preview.orders || []).map(o => fmtD(o.price)).join(" / ")}</dd><dt>预览最多亏</dt><dd>${fmtD(preview.max_loss)} ${unit}</dd></dl>
      <p class="warn">${paper ? "按下后确认这份黄金纸面盘计划（模拟资金），约 1 分钟内挂单。已有黄金网格在跑时系统会拒绝。" : "按下后会在 Hyperliquid 测试盘真实挂单（假钱）。已有网格在跑时系统会拒绝。"}</p>
      <button type="button" class="desk-btn exec" id="deskExec">执行</button><div class="desk-toast" id="deskExecToast" role="status"></div>`;
    plan.appendChild(box); syncSteps();
    if (paper && preview.backtest_key) plan.insertAdjacentHTML("afterbegin", `<div data-island="backtest" data-props="${escD(JSON.stringify({backtestKey: preview.backtest_key}))}"></div>`);  // apps/web BacktestBox
    $d("#deskExec").onclick = async () => {
      if (!confirm(`确认在${paper ? "黄金纸面盘" : "测试盘"}执行这份计划？\n最多亏 ${fmtD(preview.max_loss)} ${unit}`)) return;
      $d("#deskExec").disabled = true; $d("#deskExecToast").textContent = "正在复核预览并下单，可能需要一两分钟…";
      try { const r = await call("/api/execute", {judgment_id: judgmentId, shown_max_loss: preview.max_loss, confirm_text: "执行"}); $d("#deskExecToast").textContent = r.message; reloadToday(); }
      catch (e) {
        $d("#deskExecToast").textContent = e.message; $d("#deskExec").disabled = false;
        if (!box.querySelector('[data-island="why"]')) box.insertAdjacentHTML("beforeend", whyIsland(judgmentId));
      }
    };
  }

  // ---- boot -------------------------------------------------------------------------
  // Phone: chart first, then today's call (with its news), then GridMind's tables and controls.
  function placeForViewport() {
    const phone = window.matchMedia("(max-width: 760px)").matches;
    const judge = $d(".desk-judge"), chart = $d(".chart-card");
    if (!judge || !chart) return;
    if (phone && judge.previousElementSibling !== chart) chart.after(judge);
    else if (!phone && judge.parentElement !== $d(".control-rail")) $d(".control-rail").prepend(judge);
  }

  // 交易回看 is a React island (apps/web ReviewPanel, #1326). GridMind keeps rewriting its own
  // review panel, so that one is renamed and hidden; the island loads when the tab is first opened.
  function mountReview() {
    const old = $d('[data-panel="review"]');
    if (!old || $d("#deskReview")) return;
    old.dataset.panel = "review-legacy";
    old.insertAdjacentHTML("afterend", `<div class="tab card-body desk-review" data-panel="review" id="deskReview" data-island="review" data-props="{}"></div>`);
  }

  function bindReview() {
    if (!$d("#deskReview")) return;
    $d("#tabs")?.addEventListener("click", event => { if (event.target.closest('[data-tab="review"]')) $d("#deskReview").dataset.props = '{"active":true}'; });
  }

  // 回测工作台 (apps/web Workbench, #1335): a tab of its own next to 交易回看. GridMind's tab handler
  // switches any [data-tab] / [data-panel] pair, so the button and panel are all that is needed.
  function mountBench() {
    const tabs = $d("#tabs"), section = tabs?.closest("section");
    if (!tabs || !section || $d('[data-panel="bench"]')) return;
    tabs.insertAdjacentHTML("beforeend", `<button data-tab="bench" type="button">回测</button>`);
    section.insertAdjacentHTML("beforeend", `<div class="tab card-body" data-panel="bench" data-island="workbench" data-props="{}"></div>`);
  }

  async function boot() {
    // /trade is one React tree since #1415: it already drew everything mountReview, mountBench and
    // mountShell would insert or move. Any other page that loads this file still gets them.
    if (!document.getElementById("trade-app")) {
      mountReview();
      mountBench();
      mountShell();
    }
    bindReview();
    const date = $d("#deskJudgeDate");
    if (date) date.textContent = new Date().toLocaleDateString("zh-CN", {month: "long", day: "numeric"});
    syncSteps();
    renderStatus();
    setInterval(renderStatus, 1000);  // follows whatever GridMind last rendered
    placeForViewport();
    window.matchMedia("(max-width: 760px)").addEventListener("change", placeForViewport);
    try { D.assets = (await call("/api/assets")).assets; } catch (e) { setProps($d('[data-island="today"]'), {error: e.message}); return; }
    renderAssets();  // the buttons show as soon as the list is read, before GridMind's selection (#1433)
    // 「今天」 starts on whichever comes first: a usable GridMind selection, or its first read naming the chart's asset.
    // The selection waits on 8765's catalog (8–13 s); the read takes about 1.5 s (#1436). A retired selection is not
    // usable, so it does not end the wait before the chart's asset is known. The follow loop below moves 「今天」 to
    // the selection's asset if it turns out to be another one.
    await waitFor(() => selectionAsset() || window.gridmindView?.market?.symbol, 15000);
    if (selectionAsset()) applyAsset(); else focusAsset(chartAsset());
    setInterval(() => { if (!D.switching) applyAsset(); }, 2000);  // follow selection changes made inside GridMind
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot); else boot();
})();
