"""Login gate for requests that arrive through the phone tunnel.

Local requests (the Mac itself) pass. Anything proxied by cloudflared carries
Cf-Connecting-Ip and must hold the session cookie derived from Park's passcode,
which lives only in a 0600 file on this Mac.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

COOKIE = "desk_session"


def _passcode(path: Path) -> str | None:
    try:
        if path.stat().st_mode & 0o077:
            return None  # refuse a passcode file others can read
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _session(passcode: str) -> str:
    return hmac.new(passcode.encode(), b"park-trading-desk-session-v1", hashlib.sha256).hexdigest()


def is_remote(request: Request) -> bool:
    # The server binds 127.0.0.1, so off-Mac traffic can only arrive through a proxy, which adds these headers.
    return any(request.headers.get(name) for name in ("cf-connecting-ip", "x-forwarded-for", "cf-ray"))


def _write_passcode(path: Path, value: str) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.touch(mode=0o600, exist_ok=True)
    tmp.chmod(0o600)
    tmp.write_text(value + "\n", encoding="utf-8")
    tmp.replace(path)


async def _change_passcode(request: Request, passcode_file: Path, passcode: str | None) -> Response:
    """Park types the new passcode himself; it goes straight to the 0600 file and is never logged."""
    if request.method != "POST":
        return HTMLResponse(CHANGE.format(error="", done=""))
    form = parse_qs((await request.body()).decode("utf-8", "replace"))
    current = (form.get("current") or [""])[0].strip()
    new, again = (form.get("new") or [""])[0].strip(), (form.get("again") or [""])[0].strip()
    if passcode is None or not hmac.compare_digest(current, passcode):
        time.sleep(1.5)
        return HTMLResponse(CHANGE.format(error="当前口令不对", done=""), status_code=401)
    if len(new) < 6:
        return HTMLResponse(CHANGE.format(error="新口令至少 6 个字符", done=""), status_code=400)
    if new != again:
        return HTMLResponse(CHANGE.format(error="两次输入的新口令不一样", done=""), status_code=400)
    _write_passcode(passcode_file, new)
    response: Response = HTMLResponse(CHANGE.format(error="", done="已改好。其他设备需要用新口令重新登录。"))
    response.set_cookie(COOKIE, _session(new), max_age=30 * 86400, httponly=True, secure=True, samesite="strict")
    return response


def install(app, passcode_file: Path) -> None:
    @app.middleware("http")
    async def gate(request: Request, call_next):
        if not is_remote(request):
            if request.url.path == "/passcode":
                return await _change_passcode(request, passcode_file, _passcode(passcode_file))
            return await call_next(request)
        passcode = _passcode(passcode_file)
        if passcode is None:
            return HTMLResponse("<p style='font-family:sans-serif;padding:24px'>手机访问还没有开通。</p>", status_code=403)
        if request.url.path == "/login":
            if request.method == "POST":
                form = parse_qs((await request.body()).decode("utf-8", "replace"))
                given = (form.get("passcode") or [""])[0].strip()
                if hmac.compare_digest(given, passcode):
                    response: Response = RedirectResponse("/", status_code=303)
                    response.set_cookie(COOKIE, _session(passcode), max_age=30 * 86400, httponly=True, secure=True, samesite="strict")
                    return response
                time.sleep(1.5)
                return HTMLResponse(LOGIN.format(error="口令不对"), status_code=401)
            return HTMLResponse(LOGIN.format(error=""))
        if hmac.compare_digest(request.cookies.get(COOKIE, ""), _session(passcode)):
            if request.url.path == "/passcode":
                return await _change_passcode(request, passcode_file, passcode)
            return await call_next(request)
        if request.url.path.startswith("/api/"):
            return HTMLResponse("需要登录", status_code=401)
        return RedirectResponse("/login", status_code=303)


LOGIN = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>交易台登录</title><style>body{{margin:0;font-family:"PingFang SC",sans-serif;background:#0F1419;color:#EEF2F5;display:grid;place-items:center;min-height:100vh;padding-inline:16px}}
form{{display:grid;gap:12px;width:min(320px,100%)}}input,button{{font:inherit;font-size:17px;padding:12px;border-radius:8px;border:1px solid #27313A}}
input{{background:#171E25;color:#EEF2F5}}button{{background:#D5A94E;color:#0F1419;font-weight:700;border:0}}p{{color:#F0786A;margin:0;min-height:1.2em}}</style></head>
<body><form method="post" action="/login"><h1 style="margin:0;font-size:22px">Park 交易台</h1>
<label for="passcode">口令</label><input id="passcode" name="passcode" type="password" autocomplete="current-password" required autofocus>
<button type="submit">进入</button><p>{error}</p></form></body></html>"""


CHANGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>修改口令</title><style>body{{margin:0;font-family:"PingFang SC",sans-serif;background:#0F1419;color:#EEF2F5;display:grid;place-items:center;min-height:100vh;padding-inline:16px}}
form{{display:grid;gap:10px;width:min(320px,100%)}}input,button{{font:inherit;font-size:17px;padding:12px;border-radius:8px;border:1px solid #27313A}}
input{{background:#171E25;color:#EEF2F5}}button{{background:#D5A94E;color:#0F1419;font-weight:700;border:0}}p{{margin:0;min-height:1.2em}}.e{{color:#F0786A}}.ok{{color:#5CC593}}a{{color:#D5A94E}}</style></head>
<body><form method="post" action="/passcode"><h1 style="margin:0;font-size:22px">修改交易台口令</h1>
<label for="current">当前口令</label><input id="current" name="current" type="password" autocomplete="current-password" required>
<label for="new">新口令（至少 6 个字符，自己记得住就行）</label><input id="new" name="new" type="password" autocomplete="new-password" required minlength="6">
<label for="again">再输一遍新口令</label><input id="again" name="again" type="password" autocomplete="new-password" required minlength="6">
<button type="submit">保存</button><p class="e">{error}</p><p class="ok">{done}</p><a href="/">返回交易台</a></form></body></html>"""
