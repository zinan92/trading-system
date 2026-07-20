import {writeFile} from "node:fs/promises";

const debugBase = process.env.CHROME_DEBUG_URL || "http://127.0.0.1:9229";
const dashboardUrl = process.env.A6_DASHBOARD_URL || "http://127.0.0.1:8876/dashboard-v5.html";
const evidenceRoot = process.env.A6_EVIDENCE_ROOT || "/Users/wendy/park-io/008_codex session insights and decision logs/交易系统/evidence";

const pages = await (await fetch(`${debugBase}/json/list`)).json();
let page = pages.find(item => item.type === "page" && item.url.includes("dashboard-v5.html"));
if (!page) page = await (await fetch(`${debugBase}/json/new?${encodeURIComponent(dashboardUrl)}`, {method: "PUT"})).json();

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

async function screenshot(name) {
  await call("Page.bringToFront");
  await evaluate("new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))");
  const shot = await call("Page.captureScreenshot", {format: "png", captureBeyondViewport: true});
  const path = `${evidenceRoot}/${name}`;
  await writeFile(path, Buffer.from(shot.data, "base64"));
  return path;
}

await call("Page.enable");
await call("Runtime.enable");
await call("Emulation.setDeviceMetricsOverride", {width: 1600, height: 1000, deviceScaleFactor: 1, mobile: false});
await call("Page.navigate", {url: dashboardUrl});
await waitFor("document.readyState==='complete' && typeof state!=='undefined' && state.data?.contract?.schema_version==='trading-system-read-model-v1'", "stable read model");
await waitFor("document.querySelector('#ordersCount')?.textContent==='(25)'", "authoritative tab counts");

const desktop = await evaluate(`(()=>({
  contract:state.data.contract.schema_version,
  snapshotId:state.data.contract.snapshot_id,
  strategy:document.querySelector('#live').textContent,
  positions:document.querySelector('#positionsCount').textContent,
  orders:document.querySelector('#ordersCount').textContent,
  trades:document.querySelector('#tradesCount').textContent,
  totalPnl:state.data.execution.pnl.total,
  returnPct:state.data.execution.pnl.return_pct,
  runtime:state.data.runtime.status,
  provider:state.data.market.provider_label,
  engine:state.data.execution.engine_label,
  orderRows:document.querySelectorAll('#orders tbody tr').length,
  tradeRows:document.querySelectorAll('#fills tbody tr').length,
  bodyOverflow:document.documentElement.scrollWidth>document.documentElement.clientWidth
}))()`);
const desktopScreenshot = await screenshot("2026-07-18-a6-read-model-desktop.png");

await call("Emulation.setDeviceMetricsOverride", {width: 390, height: 844, deviceScaleFactor: 2, mobile: true});
await call("Page.reload", {ignoreCache: true});
await waitFor("document.readyState==='complete' && state.data?.contract?.schema_version==='trading-system-read-model-v1'", "mobile stable read model");
await waitFor("document.querySelector('#tradesCount')?.textContent==='(1)'", "mobile lifecycle count");
const mobile = await evaluate(`(()=>({
  viewport:window.innerWidth,
  bodyOverflow:document.documentElement.scrollWidth>document.documentElement.clientWidth,
  accountColumns:getComputedStyle(document.querySelector('#account')).gridTemplateColumns.split(' ').filter(Boolean).length,
  positions:document.querySelector('#positionsCount').textContent,
  orders:document.querySelector('#ordersCount').textContent,
  trades:document.querySelector('#tradesCount').textContent,
  strategy:document.querySelector('#live').textContent
}))()`);
const mobileScreenshot = await screenshot("2026-07-18-a6-read-model-mobile.png");

const checks = {
  stable_contract_loaded: desktop.contract === "trading-system-read-model-v1" && desktop.snapshotId.startsWith("trading-system-"),
  strategy_summary_visible: desktop.strategy.includes("中性 · 稳健 · 等价差 · 3900–4100 · 50 格 · 每格 2800 USD"),
  authoritative_counts_visible: desktop.positions === "(0)" && desktop.orders === "(25)" && desktop.trades === "(1)",
  lifecycle_rows_match_count: desktop.orderRows === 25 && desktop.tradeRows === 1,
  canonical_pnl_visible: desktop.totalPnl === 2.8 && desktop.returnPct === 0.028,
  normalized_sources_visible: desktop.provider === "Venue A" && desktop.engine === "Engine A",
  runtime_visible: desktop.runtime === "running",
  desktop_no_horizontal_overflow: desktop.bodyOverflow === false,
  mobile_no_horizontal_overflow: mobile.viewport === 390 && mobile.bodyOverflow === false,
  mobile_counts_and_summary_visible: mobile.positions === "(0)" && mobile.orders === "(25)" && mobile.trades === "(1)" && mobile.strategy.includes("每格 2800 USD"),
  mobile_account_grid_is_readable: mobile.accountColumns === 2,
  no_browser_runtime_errors: browserErrors.length === 0,
};
const failed = Object.entries(checks).filter(([, passed]) => !passed).map(([name]) => name);
console.log(JSON.stringify({checks, failed, desktop, mobile, browserErrors, evidence: {desktopScreenshot, mobileScreenshot}}, null, 2));
socket.close();
if (failed.length) process.exitCode = 1;
