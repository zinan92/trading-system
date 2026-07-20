import {writeFile} from "node:fs/promises";

const debugBase = process.env.CHROME_DEBUG_URL || "http://127.0.0.1:9229";
const dashboardUrl = process.env.STRATEGY_CONSOLE_URL || "http://127.0.0.1:8765/dashboard-dualtrack-split.html";
const evidenceRoot = process.env.STRATEGY_CONSOLE_EVIDENCE || "/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence";

const pages = await (await fetch(`${debugBase}/json/list`)).json();
let page = pages.find(item => item.type === "page" && item.url.includes("dashboard-dualtrack-split.html"));
if (!page) {
  page = await (await fetch(`${debugBase}/json/new?${encodeURIComponent(dashboardUrl)}`, {method: "PUT"})).json();
}

const socket = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
  socket.addEventListener("open", resolve, {once: true});
  socket.addEventListener("error", reject, {once: true});
});

let sequence = 0;
const pending = new Map();
const browserErrors = [];
socket.addEventListener("message", event => {
  const message = JSON.parse(event.data);
  if (message.id && pending.has(message.id)) {
    const {resolve, reject, timeout} = pending.get(message.id);
    pending.delete(message.id);
    clearTimeout(timeout);
    if (message.error) reject(new Error(JSON.stringify(message.error)));
    else resolve(message.result);
    return;
  }
  if (message.method === "Runtime.exceptionThrown") browserErrors.push(message.params.exceptionDetails?.text || "runtime exception");
  if (message.method === "Runtime.consoleAPICalled" && message.params.type === "error") browserErrors.push("console.error");
});

function call(method, params = {}) {
  return new Promise((resolve, reject) => {
    const id = ++sequence;
    const timeout = setTimeout(() => {
      pending.delete(id);
      reject(new Error(`${method} timed out`));
    }, 20_000);
    pending.set(id, {resolve, reject, timeout});
    socket.send(JSON.stringify({id, method, params}));
  });
}

async function evaluate(expression) {
  const response = await call("Runtime.evaluate", {expression, awaitPromise: true, returnByValue: true});
  if (response.exceptionDetails) throw new Error(response.exceptionDetails.exception?.description || response.exceptionDetails.text);
  return response.result?.value;
}

async function waitFor(expression, label, timeoutMs = 20_000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    if (await evaluate(`Boolean(${expression})`)) return;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error(`Timed out waiting for ${label}`);
}

async function click(selector) {
  const clicked = await evaluate(`(()=>{const element=document.querySelector(${JSON.stringify(selector)});if(!element)return false;element.click();return true})()`);
  if (!clicked) throw new Error(`Missing clickable element: ${selector}`);
}

async function screenshot(name) {
  await call("Page.bringToFront");
  await evaluate("new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))");
  await new Promise(resolve => setTimeout(resolve, 250));
  const shot = await call("Page.captureScreenshot", {format: "png", captureBeyondViewport: true});
  const path = `${evidenceRoot}/${name}`;
  await writeFile(path, Buffer.from(shot.data, "base64"));
  return path;
}

await call("Page.enable");
await call("Runtime.enable");
await call("Emulation.setDeviceMetricsOverride", {width: 1600, height: 1000, deviceScaleFactor: 1, mobile: false});
await call("Page.navigate", {url: dashboardUrl});
await waitFor("document.readyState==='complete' && typeof state!=='undefined' && state.data && !state.busy", "initial dashboard state");
await waitFor("state.data.runtime.actual_state==='stopped'", "an initially stopped paper robot");

const initial = await evaluate(`(()=>({
  actual:state.data.runtime.actual_state,
  accepted:(state.data.production_execution.orders||[]).filter(order=>order.state==='accepted').length,
  marketFresh:isFresh(state.market||state.data.market),
  startDisabled:document.querySelector('#startRobot').disabled,
  stopDisabled:document.querySelector('#stopRobot').disabled
}))()`);

await click('[data-tf="5m"]');
await waitFor("state.market?.timeframe==='5m' && isFresh(state.market)", "fresh 5m chart data");
const timeframeFiveMinutes = await evaluate("state.market.timeframe");
await click('[data-tf="1m"]');
await waitFor("state.market?.timeframe==='1m' && isFresh(state.market)", "fresh 1m chart data");

await click('[data-direction="neutral"]');
await waitFor("state.preview?.direction==='neutral'", "neutral preview");
await click('[data-style="steady"]');
await waitFor("state.preview?.style==='steady'", "steady preview");
const steady = await evaluate(`(()=>({
  low:state.preview.range.low,
  high:state.preview.range.high,
  count:state.preview.grid.count,
  buys:state.preview.orders.filter(order=>order.side==='buy').length,
  sells:state.preview.orders.filter(order=>order.side==='sell').length
}))()`);

await click('[data-direction="short"]');
await waitFor("state.preview?.direction==='short' && state.preview.orders.every(order=>order.side==='sell')", "short-only preview orders");
const shortSteady = await evaluate(`(()=>({low:state.preview.range.low,high:state.preview.range.high,count:state.preview.grid.count,orders:state.preview.orders.length}))()`);
await click('[data-style="aggressive"]');
await waitFor("state.preview?.style==='aggressive' && state.preview.grid.count===12", "aggressive grid preview");
const aggressive = await evaluate(`(()=>({
  low:state.preview.range.low,
  high:state.preview.range.high,
  count:state.preview.grid.count,
  orders:state.preview.orders.length,
  sides:[...new Set(state.preview.orders.map(order=>order.side))],
  previewLines:state.chart.priceLines.length
}))()`);

await click("#showMacd");
await evaluate(`(()=>{const input=document.querySelector('#emaFast');input.value='13';input.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
await waitFor("state.indicators.macd===true && state.indicators.fast===13", "editable EMA and selected MACD");

await click("#startRobot");
await waitFor("state.data?.runtime?.actual_state==='running' && (state.data.production_execution.orders||[]).some(order=>order.state==='accepted') && !state.busy", "running robot with accepted paper orders", 30_000);
const running = await evaluate(`(()=>{
  const accepted=(state.data.production_execution.orders||[]).filter(order=>order.state==='accepted');
  return {
    actual:state.data.runtime.actual_state,
    planId:state.data.production_plan.strategy_plan_id,
    planVersion:state.data.production_plan.version,
    accepted:accepted.length,
    sides:[...new Set(accepted.map(order=>order.side))],
    allVersioned:accepted.every(order=>order.strategy_plan_id===state.data.production_plan.strategy_plan_id && order.strategy_plan_version===state.data.production_plan.version),
    orderRows:document.querySelectorAll('#orders tbody tr').length,
    priceLines:state.chart.priceLines.length,
    startDisabled:document.querySelector('#startRobot').disabled,
    startClass:document.querySelector('#startRobot').className,
    stopDisabled:document.querySelector('#stopRobot').disabled,
    stopClass:document.querySelector('#stopRobot').className,
    runBadge:document.querySelector('#runBadge').textContent
  }
})()`);
const runningScreenshot = await screenshot("strategy-console-production-grid-running-2026-07-14.png");

await click('[data-direction="neutral"]');
await waitFor("state.preview?.direction==='neutral'", "running neutral adjustment preview");
await click('[data-style="steady"]');
await waitFor("state.preview?.style==='steady' && state.preview.orders.some(order=>order.side==='buy') && state.preview.orders.some(order=>order.side==='sell')", "running bilateral steady adjustment preview");
await click("#applyAdjustment");
await waitFor(`state.data?.runtime?.actual_state==='running' && state.data?.production_plan?.version>${running.planVersion} && !state.busy`, "running regrid completion", 30_000);
const adjusted = await evaluate(`(()=>{
  const accepted=(state.data.production_execution.orders||[]).filter(order=>order.state==='accepted');
  return {
    actual:state.data.runtime.actual_state,
    version:state.data.production_plan.version,
    accepted:accepted.length,
    sides:[...new Set(accepted.map(order=>order.side))].sort(),
    notice:document.querySelector('#actionStatus').textContent,
    allVersioned:accepted.every(order=>order.strategy_plan_id===state.data.production_plan.strategy_plan_id && order.strategy_plan_version===state.data.production_plan.version)
  }
})()`);
await call("Page.reload", {ignoreCache: true});
await new Promise(resolve => setTimeout(resolve, 500));
await waitFor("document.readyState==='complete' && typeof state!=='undefined' && state.data?.runtime?.actual_state==='running' && !state.busy", "running state after refresh", 30_000);
const afterRefresh = await evaluate(`(()=>({
  accepted:(state.data.production_execution.orders||[]).filter(order=>order.state==='accepted').length,
  startDisabled:document.querySelector('#startRobot').disabled,
  stopDisabled:document.querySelector('#stopRobot').disabled,
  runBadge:document.querySelector('#runBadge').textContent
}))()`);
const adjustedScreenshot = await screenshot("strategy-console-production-grid-regridded-2026-07-14.png");

const idempotent = await evaluate(`(async()=>{
  const plan=state.data.production_plan;
  const payload={cycle_id:state.data.cycle.cycle_id,action:'start',direction:plan.direction,style:plan.style,timeframe:state.timeframe,out_of_range:plan.grid.out_of_range,range:{low:plan.range.low,high:plan.range.high},grid:{count:plan.grid.count,notional_per_grid:plan.grid.notional_per_grid,out_of_range:plan.grid.out_of_range},risk_budget:{leverage:plan.grid.leverage||plan.risk_budget.leverage}};
  const response=await fetch('/api/strategy-console/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  const body=await response.json();
  return {ok:response.ok,idempotent:body.idempotent,created:body.created_orders,accepted:body.accepted_orders};
})()`);

await click("#stopRobot");
await waitFor("state.data?.runtime?.actual_state==='stopped' && !(state.data.production_execution.orders||[]).some(order=>order.state==='accepted') && !(state.data.production_execution.trades||[]).some(trade=>trade.status==='open') && !state.busy", "stopped, flat, reconciled robot", 30_000);
const stopped = await evaluate(`(()=>({
  actual:state.data.runtime.actual_state,
  accepted:(state.data.production_execution.orders||[]).filter(order=>order.state==='accepted').length,
  open:(state.data.production_execution.trades||[]).filter(trade=>trade.status==='open').length,
  startDisabled:document.querySelector('#startRobot').disabled,
  startClass:document.querySelector('#startRobot').className,
  stopDisabled:document.querySelector('#stopRobot').disabled,
  stopClass:document.querySelector('#stopRobot').className,
  notice:document.querySelector('#actionStatus').textContent
}))()`);

await click('[data-tab="shadows"]');
const shadowComparison = await evaluate(`(()=>({
  cards:document.querySelectorAll('#shadows article').length,
  variants:[...document.querySelectorAll('#shadows article>b')].map(element=>element.textContent),
  sameHistory:[...document.querySelectorAll('#shadows .banner')].every(element=>element.textContent.includes('同一历史行情')),
  productionSafe:(state.data.strategy_shadows||[]).every(shadow=>shadow.safety?.writes_production_ledger===false && shadow.safety?.real_orders===false && shadow.review?.future_function===false)
}))()`);
const shadowScreenshot = await screenshot("strategy-console-production-shadows-2026-07-14.png");

await call("Emulation.setDeviceMetricsOverride", {width: 390, height: 844, deviceScaleFactor: 2, mobile: true});
await call("Page.reload", {ignoreCache: true});
await new Promise(resolve => setTimeout(resolve, 500));
await waitFor("document.readyState==='complete' && typeof state!=='undefined' && state.data?.runtime?.actual_state==='stopped' && !state.busy", "mobile stopped dashboard");
await click('[data-direction="long"]');
await waitFor("state.preview?.direction==='long' && state.preview.orders.every(order=>order.side==='buy')", "mobile long preview");
const mobile = await evaluate(`(()=>({
  viewport:window.innerWidth,
  bodyOverflow:document.documentElement.scrollWidth>document.documentElement.clientWidth,
  accountColumns:getComputedStyle(document.querySelector('#account')).gridTemplateColumns.split(' ').filter(Boolean).length,
  direction:state.preview.direction,
  orders:state.preview.orders.length,
  sides:[...new Set(state.preview.orders.map(order=>order.side))]
}))()`);
const mobileScreenshot = await screenshot("strategy-console-production-grid-mobile-2026-07-14.png");

const checks = {
  initial_stopped_and_actionable: initial.actual === "stopped" && initial.accepted === 0 && initial.marketFresh && !initial.startDisabled && initial.stopDisabled,
  timeframe_switches_to_fresh_5m: timeframeFiveMinutes === "5m",
  neutral_preview_is_bilateral: steady.buys > 0 && steady.sells > 0,
  direction_keeps_market_geometry: steady.low === shortSteady.low && steady.high === shortSteady.high,
  style_changes_width_and_density: shortSteady.high - shortSteady.low > aggressive.high - aggressive.low && shortSteady.count >= 24 && aggressive.count >= 24,
  short_preview_only_sells: aggressive.sides.length === 1 && aggressive.sides[0] === "sell",
  preview_is_drawn_on_chart: aggressive.previewLines >= aggressive.orders,
  start_creates_versioned_orders: running.actual === "running" && running.accepted > 0 && running.allVersioned && running.orderRows === running.accepted,
  running_buttons_are_unambiguous: running.startDisabled && running.startClass.includes("is-inactive") && !running.stopDisabled && running.stopClass.includes("is-active") && running.runBadge.includes("挂单"),
  running_adjustment_replaces_orders_without_stopping: adjusted.actual === "running" && adjusted.version === running.planVersion + 1 && adjusted.accepted > 0 && adjusted.sides.join(",") === "buy,sell" && adjusted.allVersioned && adjusted.notice.includes("网格已调整"),
  refresh_preserves_runtime_and_orders: afterRefresh.accepted === adjusted.accepted && afterRefresh.startDisabled && !afterRefresh.stopDisabled,
  repeated_start_is_idempotent: idempotent.ok && idempotent.idempotent === true && idempotent.created === 0 && idempotent.accepted === adjusted.accepted,
  stop_cancels_and_flattens: stopped.actual === "stopped" && stopped.accepted === 0 && stopped.open === 0 && !stopped.startDisabled && stopped.stopDisabled && stopped.notice.includes("对账通过"),
  strategy_shadows_are_visible_and_production_safe: shadowComparison.cards >= 2 && shadowComparison.sameHistory && shadowComparison.productionSafe,
  mobile_has_no_horizontal_overflow: mobile.viewport === 390 && mobile.bodyOverflow === false && mobile.accountColumns === 2,
  mobile_direction_preview_is_real: mobile.direction === "long" && mobile.orders > 0 && mobile.sides.length === 1 && mobile.sides[0] === "buy",
  no_browser_runtime_errors: browserErrors.length === 0,
};
const failed = Object.entries(checks).filter(([, passed]) => !passed).map(([name]) => name);
const result = {checks, failed, initial, steady, shortSteady, aggressive, running, adjusted, afterRefresh, idempotent, stopped, shadowComparison, mobile, browserErrors, evidence: {runningScreenshot, adjustedScreenshot, shadowScreenshot, mobileScreenshot}};
console.log(JSON.stringify(result, null, 2));
socket.close();
if (failed.length) process.exitCode = 1;
