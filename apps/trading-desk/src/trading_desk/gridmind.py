"""The trading page is the GridMind console itself, served same-origin by the desk.

The desk renders /trade from its own template (trade.html, #1414) and serves the page's static
files from this checkout. GridMind (trading-system, port 8765) stays the one product for data,
grids, orders and controls: the desk forwards only the API paths that page uses, behind the desk's
passcode gate, and adds its own news rail and judgment panel on top (static/gm-desk.*).
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request as UrlRequest, urlopen

from services.structured_log import request_id

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from . import remote

ROOT = Path(__file__).resolve().parents[4]  # the trading-system checkout the desk runs from
TEMPLATE = Path(__file__).resolve().parent / "trade.html"
STATIC_DIR = Path(__file__).resolve().parent / "static"  # the desk's own files, under /static
CSS, JS = "text/css; charset=utf-8", "text/javascript; charset=utf-8"
# The page's static files, served from this checkout (#1414). Each path maps to exactly one file;
# nothing else under the checkout is reachable.
STATIC_FILES = {
    "/assets/tokens.css": CSS,
    "/packages/standard-kline/standard-kline.js": JS,
    "/data/vendor/echarts.min.js": JS,
    "/data/vendor/lightweight-charts.standalone.production.js": JS,
    "/assets/gridmind/base.css": CSS,
    "/assets/gridmind/skin.css": CSS,
    "/assets/gridmind/control-intents.js": JS,
    "/assets/gridmind/read.js": JS,  # #1416: built from apps/web/src/gridmind/read.ts
    "/assets/gridmind/chart.js": JS,  # #1417: built from apps/web/src/gridmind/chart.ts
    "/assets/gridmind/control.js": JS,  # #1418: built from apps/web/src/gridmind/control.ts
    "/assets/gridmind/gridmind.js": JS,
    "/assets/gridmind/park-ai-chat.js": JS,
    "/assets/gridmind/dashboard-control.js": JS,
}
# API paths forwarded to 8765; same allowlist as trading-system's public gateway (services/cloud_access_gateway.py).
READ_PATHS = frozenset({
    "/api/trading-system/read-model",
    "/api/trading-system/cloud-health",
    "/api/trading-system/daily-self-review",
    "/api/trading-system/supervisor-history",
    "/api/trading-system/ai-evaluation-receipt",
    "/api/dualtrack/market/bars",
    "/api/park-paper/ai-chat",
    "/api/dashboard-control/catalog",
    "/api/dashboard-control/market-bars",
    "/api/dashboard-control/selection",
    "/api/dashboard-control/runtime-status",
})
WRITE_PATHS = frozenset({
    "/api/strategy-console/control",
    "/api/dualtrack/orders",
    "/api/park-paper/ai-chat",
    "/api/dashboard-control/selection",
    "/api/dashboard-control/preview",
    "/api/dashboard-control/account-admission",
    "/api/dashboard-control/confirm",
    "/api/dashboard-control/control",
    "/api/dashboard-control/acceptance",
})
SHELL = ('<link rel="stylesheet" href="/static/gm-desk.css?v={v}">'
         '<script src="/static/gm-desk.js?v={v}" defer></script>'
         '<script src="/static/web/web.js?v={v}" defer></script>')  # React islands, built from apps/web (#1325)
ASSET_FILES = ("gm-desk.js", "gm-desk.css", "web/web.js")


def _asset_version() -> str:
    """Content hash of the desk's static files: a changed file always busts the browser cache.

    A hand-bumped number was forgotten once (2026-11-05): browsers kept running the old
    gm-desk.js and selected a Paper venue that no longer existed.
    """

    import hashlib

    digest = hashlib.sha256()
    for name in ASSET_FILES:
        digest.update((STATIC_DIR / name).read_bytes())
    return digest.hexdigest()[:12]


ASSET_VERSION = _asset_version()
MAX_BODY = 64_000

Fetch = Callable[[str, str, bytes | None, str, float], tuple[int, bytes, str]]


def urllib_fetch(url: str, method: str, body: bytes | None, content_type: str, timeout: float) -> tuple[int, bytes, str]:
    request = UrlRequest(url, data=body, method=method, headers={**({"Content-Type": content_type} if body is not None else {}), **({"X-Request-Id": request_id()} if request_id() else {})})
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, response.read(), response.headers.get("Content-Type") or "application/octet-stream"
    except HTTPError as exc:
        return exc.code, exc.read(), exc.headers.get("Content-Type") or "application/json"


def render_trade_page() -> str:
    """The desk's /trade page: a mount point for one React tree (#1415), read from disk on every request.

    The asset version is hashed from the files on disk on every request too. A deploy fast-forwards the
    checkout and restarts the desk only after the gate passes; a version fixed at startup would serve the
    new page with the old ?v= in between, and a browser could then reuse a cached old web.js.
    """
    return TEMPLATE.read_text(encoding="utf-8").replace("__DESK_ASSET_VERSION__", _asset_version())


def inject_shell(html: str) -> str:
    """The shell the desk added to 8765's dashboard-gridmind.html before #1415: that page plus this shell is
    the reference the React tree is checked against (tests/test_trade_tree_browser.py) until #1419."""
    tag = SHELL.format(v=ASSET_VERSION)
    head = html.replace("<title>Extended 网格交易机器人 · GridMind 视觉版</title>", "<title>Park 交易台 · 交易</title>", 1)
    if "</body>" not in head:
        return head + tag
    return head.replace("</body>", tag + "</body>", 1)


def same_origin(request: Request) -> bool:
    origin = (request.headers.get("origin") or "").rstrip("/")
    host = request.headers.get("host") or ""
    return bool(host) and origin in {f"https://{host}", f"http://{host}"}


def install(app: FastAPI, dashboard_url: str, fetch: Fetch | None = None) -> None:
    fetch = fetch or urllib_fetch
    upstream = dashboard_url.rstrip("/")

    def relay(path: str, query: str, method: str, body: bytes | None, content_type: str, timeout: float) -> Response:
        url = f"{upstream}{path}{'?' + query if query else ''}"
        try:
            status, payload, kind = fetch(url, method, body, content_type, timeout)
        except (URLError, OSError, TimeoutError):
            return JSONResponse({"error": "gridmind_unavailable", "message": "交易后台（8765）暂时连不上，稍后自动重试。"}, status_code=503)
        return Response(payload, status_code=status, media_type=kind, headers={"Cache-Control": "no-store"})

    @app.get("/trade")
    def trade() -> Response:
        # Rendered here, not fetched from 8765: when 8765 is down the page still loads and GridMind
        # shows 运行异常, and every control request it sends gets the relay's 503.
        return Response(render_trade_page(), media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-store"})

    def static_file(request: Request) -> Response:
        path = request.url.path
        return Response((ROOT / path.lstrip("/")).read_bytes(), media_type=STATIC_FILES[path], headers={"Cache-Control": "no-store"})

    for path in STATIC_FILES:
        app.add_api_route(path, static_file, methods=["GET"], include_in_schema=False)

    @app.get("/dashboard-v5.html")
    def legacy_link() -> RedirectResponse:
        return RedirectResponse("/trade", status_code=302)

    @app.get("/api/auth/session")
    def session() -> dict:
        # Reaching this route already passed the desk gate: local, or holding Park's passcode cookie.
        return {"authenticated": True, "can_control": True, "email": None, "session_duration": "desk"}

    async def read(request: Request) -> Response:
        if request.url.path not in READ_PATHS:
            raise HTTPException(404, "not found")
        return await run_in_threadpool(relay, request.url.path, request.url.query, "GET", None, "", 30)

    async def write(request: Request) -> Response:
        if request.url.path not in WRITE_PATHS:
            raise HTTPException(404, "not found")
        if remote.is_remote(request) and not same_origin(request):
            return JSONResponse({"error": "origin_denied", "message": "控制请求必须来自交易台页面。"}, status_code=403)
        body = await request.body()
        if not body or len(body) > MAX_BODY:
            return JSONResponse({"error": "invalid_body", "message": "请求内容无效。"}, status_code=400)
        return await run_in_threadpool(relay, request.url.path, "", "POST", body, request.headers.get("content-type") or "application/json", 150)

    for path in sorted(READ_PATHS | WRITE_PATHS):
        methods = (["GET"] if path in READ_PATHS else []) + (["POST"] if path in WRITE_PATHS else [])
        for method in methods:
            app.add_api_route(path, read if method == "GET" else write, methods=[method], include_in_schema=False)

