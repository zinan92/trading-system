(function () {
  "use strict";

  const POLL_MS = 60000;
  const STATUS_META = {
    RUN: { label: "运行", tone: "run" },
    DEGRADED: { label: "降级", tone: "degraded" },
    BLOCKED: { label: "阻断", tone: "blocked" },
    UNKNOWN: { label: "未连接", tone: "unknown" },
  };
  const ROOM_URLS = {
    command: "command-center.html",
    ops: "ops-dashboard.html",
    trader: "dashboard-v4.html",
    cockpit: "dashboard-v4.html",
    dualtrack: "dashboard-dualtrack-v5.html",
    dualtrackSplit: "dashboard-dualtrack-split.html",
  };

  let timer = null;

  function filename() {
    const name = window.location.pathname.split("/").pop();
    return name || "command-center.html";
  }

  function apiBase() {
    if (window.GOLDBOT_API_BASE) return String(window.GOLDBOT_API_BASE).replace(/\/$/, "");
    if (window.location.protocol === "file:") return ["http:", "//127.0.0.1:8765"].join("");
    return "";
  }

  function sourceRoom() {
    const params = new URLSearchParams(window.location.search);
    const source = `${params.get("from") || ""} ${params.get("source") || ""} ${params.get("layout") || ""}`.toLowerCase();
    if (source.includes("dualtrack")) return "dualtrack";
    const ref = String(document.referrer || "").toLowerCase();
    if (ref.includes("dashboard-dualtrack")) return "dualtrack";
    return "cockpit";
  }

  function navHtml() {
    const page = filename();
    if (page === "dashboard-replay-v4.html" || page === "dashboard-dualtrack-replay.html") {
      const room = page === "dashboard-dualtrack-replay.html" ? "dualtrack" : sourceRoom();
      const label = room === "dualtrack" ? "返回作战台" : "返回驾驶舱";
      return `<a class="gb-shell-return" href="${ROOM_URLS[room]}">${label}</a>`;
    }
    const items = [
      ["command", "指挥台", "command-center.html"],
      ["dualtrack", "作战台", "dashboard-dualtrack-v5.html"],
      ["dualtrackSplit", "双画布", "dashboard-dualtrack-split.html"],
      ["ops", "运维", "ops-dashboard.html"],
    ];
    return `<nav class="gb-shell-nav" aria-label="全局房间导航">${items.map(([id, label, href]) => {
      const active = page === href ? " active" : "";
      return `<a class="gb-shell-nav-item${active}" data-room="${id}" href="${href}">${label}</a>`;
    }).join("")}</nav>`;
  }

  function installShell() {
    if (document.getElementById("goldbot-shell")) return;
    document.body.classList.add("goldbot-shell-active");
    const shell = document.createElement("div");
    shell.id = "goldbot-shell";
    shell.className = "gb-shell";
    shell.innerHTML = `
      <div class="gb-shell-bar">
        <div class="gb-shell-left">${navHtml()}</div>
        <a class="gb-shell-status unknown" id="gbShellStatus" href="ops-dashboard.html" aria-label="全局系统状态">
          <span class="gb-shell-dot"></span><span id="gbShellStatusText">读取状态</span>
        </a>
        <div class="gb-shell-right" id="gbShellFreshness">--</div>
      </div>
      <div class="gb-shell-banner hidden" id="gbShellBanner"></div>`;
    document.body.prepend(shell);
  }

  function roomHref(room) {
    return ROOM_URLS[String(room || "ops").toLowerCase()] || ROOM_URLS.ops;
  }
  window.GoldbotShell = Object.assign({}, window.GoldbotShell || {}, { roomHref });

  function minutesAgo(iso) {
    const ts = Date.parse(iso || "");
    if (!Number.isFinite(ts)) return "--";
    const mins = Math.max(0, Math.floor((Date.now() - ts) / 60000));
    if (mins < 1) return "刚刚";
    if (mins < 60) return `${mins} 分钟前`;
    const hours = Math.floor(mins / 60);
    return `${hours} 小时前`;
  }

  function firstCheck(payload, status) {
    return (payload.checks || []).find(check => check.status === status) || null;
  }

  function render(payload) {
    const overall = STATUS_META[payload.overall] ? payload.overall : "UNKNOWN";
    const meta = STATUS_META[overall];
    const status = document.getElementById("gbShellStatus");
    const text = document.getElementById("gbShellStatusText");
    const freshness = document.getElementById("gbShellFreshness");
    const banner = document.getElementById("gbShellBanner");
    if (!status || !text || !freshness || !banner) return;

    status.className = `gb-shell-status ${meta.tone}`;
    text.textContent = meta.label;
    freshness.textContent = minutesAgo(payload.generated_at);

    const blocked = firstCheck(payload, "BLOCKED");
    const degraded = firstCheck(payload, "DEGRADED");
    const bannerCheck = blocked || (overall === "DEGRADED" ? degraded : null);
    if (!bannerCheck) {
      banner.className = "gb-shell-banner hidden";
      banner.textContent = "";
      return;
    }
    const tone = blocked ? "blocked" : "degraded";
    banner.className = `gb-shell-banner ${tone}`;
    banner.innerHTML = `<span>${escapeHtml(bannerCheck.reason || meta.label)}</span><a href="${roomHref(bannerCheck.room)}">${escapeHtml(bannerCheck.cta || "查看运维")}</a>`;
  }

  function renderUnknown() {
    render({ overall: "UNKNOWN", generated_at: new Date().toISOString(), checks: [] });
  }

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  async function load() {
    try {
      const response = await fetch(`${apiBase()}/api/system-state`, { cache: "no-store" });
      if (!response.ok) {
        renderUnknown();
        return;
      }
      const payload = await response.json();
      render(payload && typeof payload === "object" ? payload : {});
    } catch {
      renderUnknown();
    }
  }

  function start() {
    installShell();
    load();
    if (timer) clearInterval(timer);
    timer = setInterval(load, POLL_MS);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start, { once: true });
  } else {
    start();
  }
})();
