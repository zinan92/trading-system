/* 开仓前回测 (#1309): history for the previewed gold grid, shown above 「你亲手启动」.
 * Reference only. It reads one GET endpoint and never touches the start button. */
"use strict";

const DeskBacktest = (() => {
  const pct = value => value == null ? "—" : `${value > 0 ? "+" : value < 0 ? "−" : ""}${Math.abs(value).toFixed(2)}%`;
  const days = (a, b) => Math.round((Date.parse(b) - Date.parse(a)) / 86400000);

  function view(result) {
    if (!result || result.status === "running") return {state: "running", text: "正在用过去 180 天的 OKX 行情回测这组网格，约 1 分钟…"};
    if (result.status !== "ready") return {state: "unavailable", text: `回测不可用：${result.reason || "原因未知"}。不影响启动。`};
    const s = result.summary, total = s.runs, funded = s.funded_runs;
    return {
      state: "ready",
      title: `这组网格过去 ${days(result.history_start, result.history_end)} 天的表现（OKX，费后）`,
      sub: `跑了 ${total} 段，每段 7 天，互不重叠`,
      cells: [
        ["中位收益", pct(s.median_return_pct)],
        ["最差一次", pct(s.worst_return_pct)],
        ["跌破下沿或止损", `${s.stopped_pct}%`],
        ["平均成交", `${s.mean_fills} 笔`],
      ],
      bars: s.histogram.counts,
      range: [pct(s.histogram.low), pct(s.best_return_pct)],
      notes: [
        "手续费是 OKX Paper 的假设费率，尚未用账户核实",
        funded === total ? "资金费已计入" : funded ? `资金费只计入了 ${funded} / ${total} 段（OKX 只给最近约 94 天）` : "未含资金费",
      ],
    };
  }

  return {pct, view};
})();

if (typeof module !== "undefined") module.exports = DeskBacktest;

if (typeof document !== "undefined") window.DeskBacktest = Object.assign(DeskBacktest, {
  mount(host, key) {
    const esc = value => String(value ?? "").replace(/[&<>"']/g, ch => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[ch]));
    const box = document.createElement("section");
    box.className = "desk-backtest";
    box.setAttribute("aria-live", "polite");
    host.prepend(box);
    let tries = 0;
    const paint = result => {
      const v = DeskBacktest.view(result);
      if (v.state !== "ready") { box.innerHTML = `<p class="note">${esc(v.text)}</p>`; return; }
      const top = Math.max(...v.bars, 1);
      box.innerHTML = `<header><b>${esc(v.title)}</b><small>只是历史参考，不代表未来</small></header>
        <p class="note">${esc(v.sub)}</p>
        <dl>${v.cells.map(([k, val]) => `<div><dt>${esc(k)}</dt><dd>${esc(val)}</dd></div>`).join("")}</dl>
        <div class="desk-backtest-bars" role="img" aria-label="每段收益的分布，从 ${esc(v.range[0])} 到 ${esc(v.range[1])}">${v.bars.map(n => `<i style="height:${Math.round(n / top * 100)}%"></i>`).join("")}</div>
        <div class="desk-backtest-axis"><span>${esc(v.range[0])}</span><span>${esc(v.range[1])}</span></div>
        <p class="note">${v.notes.map(esc).join(" · ")}</p>`;
    };
    const poll = async () => {
      let result = null;
      try {
        const response = await fetch(`/api/backtest/${encodeURIComponent(key)}`, {cache: "no-store"});
        if (response.ok) result = await response.json();
        else if (response.status !== 404) result = {status: "unavailable", reason: `HTTP ${response.status}`};
      } catch (error) { result = {status: "unavailable", reason: error.message}; }
      paint(result);
      if ((!result || result.status === "running") && box.isConnected && ++tries < 120) setTimeout(poll, 3000);
      else if (!result || result.status === "running") paint({status: "unavailable", reason: "等了 6 分钟还没算完"});
    };
    poll();
  },
});
