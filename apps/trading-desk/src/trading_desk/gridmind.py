"""The trading page is the GridMind console itself, served same-origin through the desk.

GridMind (trading-system, port 8765) stays the one product for charts, grids, orders and
controls. The desk forwards only the paths that page uses, behind the desk's passcode gate,
and adds its own news rail and judgment panel on top (static/gm-desk.*).
"""
from __future__ import annotations

from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request as UrlRequest, urlopen

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from . import remote

# Same allowlist as trading-system's public gateway (services/cloud_access_gateway.py).
READ_PATHS = frozenset({
    "/assets/tokens.css",
    "/packages/standard-kline/standard-kline.js",
    "/data/vendor/echarts.min.js",
    "/data/vendor/lightweight-charts.standalone.production.js",
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
         '<script src="/static/gm-desk.js?v={v}" defer></script>')
ASSET_VERSION = "17"
MAX_BODY = 64_000

Fetch = Callable[[str, str, bytes | None, str, float], tuple[int, bytes, str]]


def urllib_fetch(url: str, method: str, body: bytes | None, content_type: str, timeout: float) -> tuple[int, bytes, str]:
    request = UrlRequest(url, data=body, method=method, headers={"Content-Type": content_type} if body is not None else {})
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, response.read(), response.headers.get("Content-Type") or "application/octet-stream"
    except HTTPError as exc:
        return exc.code, exc.read(), exc.headers.get("Content-Type") or "application/json"


def inject_shell(html: str) -> str:
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
        try:
            status, payload, _ = fetch(f"{upstream}/dashboard-v5.html", "GET", None, "", 20)
        except (URLError, OSError, TimeoutError):
            status, payload = 503, b""
        if status != 200:
            return Response(UNAVAILABLE, status_code=503, media_type="text/html; charset=utf-8")
        html = inject_shell(payload.decode("utf-8", "replace"))
        return Response(html, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-store"})

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


UNAVAILABLE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Park 交易台 · 交易</title><meta http-equiv="refresh" content="15"></head>
<body style="margin:0;background:#0b0c0f;color:#eceef2;font-family:'PingFang SC',sans-serif;display:grid;place-items:center;min-height:100vh;padding-inline:16px">
<div style="max-width:420px"><h1 style="font-size:20px">交易后台暂时连不上</h1>
<p style="color:#9aa3ad">GridMind（8765）没有响应。交易所上的挂单和持仓不受影响。页面每 15 秒自动重试。</p>
<p><a style="color:#3fd0e0" href="/desk#system">查看系统状态</a></p></div></body></html>"""
