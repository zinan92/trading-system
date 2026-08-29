"""Password-authenticated, allowlisted proxy for the Cloud Paper Dashboard."""

from __future__ import annotations

import argparse
import gzip
import html
import json
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from services.cloud_password_auth import (
    SESSION_TTL_SECONDS,
    create_session_token,
    password_auth_configured,
    revoke_session_token,
    validate_session_token,
    verify_password,
)

try:
    import jwt
except ImportError:  # pragma: no cover - production stays fail-closed.
    jwt = None


UPSTREAM = os.getenv("GOLDBOT_UPSTREAM", "http://127.0.0.1:8765").rstrip("/")
ACCESS_TEAM_DOMAIN = os.getenv("GOLDBOT_ACCESS_TEAM_DOMAIN", "").rstrip("/")
ACCESS_AUD = os.getenv("GOLDBOT_ACCESS_AUD", "")
ALLOWED_ACCESS_EMAIL = os.getenv("GOLDBOT_ACCESS_EMAIL", "").strip().lower()
ACCESS_SESSION_DURATION = os.getenv("GOLDBOT_ACCESS_SESSION_DURATION", "168h")
PUBLIC_ORIGIN = os.getenv("GOLDBOT_PUBLIC_ORIGIN", "").rstrip("/")
SESSION_COOKIE_NAME = "__Host-gridmind_session"
LOGIN_CSRF_COOKIE_NAME = "__Host-gridmind_login_csrf"
LOGIN_CSRF_TTL_SECONDS = 600
LOGIN_MAX_FAILURES = int(os.getenv("GOLDBOT_LOGIN_MAX_FAILURES", "5"))
LOGIN_GLOBAL_MAX_FAILURES = int(
    os.getenv("GOLDBOT_LOGIN_GLOBAL_MAX_FAILURES", "30")
)
LOGIN_FAILURE_WINDOW_SECONDS = int(
    os.getenv("GOLDBOT_LOGIN_FAILURE_WINDOW_SECONDS", "300")
)
LOGIN_LOCKOUT_SECONDS = int(os.getenv("GOLDBOT_LOGIN_LOCKOUT_SECONDS", "300"))
ACCESS_AUDIT_PATH = Path(
    os.getenv(
        "GOLDBOT_ACCESS_AUDIT_PATH",
        "/var/lib/gridmind/outputs/cloud/access/audit.jsonl",
    )
)
READ_TIMEOUT = float(os.getenv("GOLDBOT_UPSTREAM_READ_TIMEOUT", "20"))
CONTROL_TIMEOUT = float(os.getenv("GOLDBOT_UPSTREAM_CONTROL_TIMEOUT", "90"))
READ_MAX_BYTES = int(os.getenv("GOLDBOT_UPSTREAM_READ_MAX_BYTES", "8000000"))
SUPERVISOR_HISTORY_TIMEOUT = float(
    os.getenv("GOLDBOT_SUPERVISOR_HISTORY_TIMEOUT", "90")
)
SUPERVISOR_HISTORY_MAX_BYTES = int(
    os.getenv("GOLDBOT_SUPERVISOR_HISTORY_MAX_BYTES", str(64 * 1024 * 1024))
)
ROOT_REDIRECT = "/dashboard-v5.html"

ALLOW_EXACT = frozenset(
    {
        "/dashboard-v5.html",
        "/assets/tokens.css",
        "/packages/standard-kline/standard-kline.js",
        "/data/vendor/echarts.min.js",
        "/data/vendor/lightweight-charts.standalone.production.js",
        "/api/auth/session",
        "/api/public-access-health",
        "/api/trading-system/read-model",
        "/api/trading-system/cloud-health",
        "/api/trading-system/daily-self-review",
        "/api/trading-system/supervisor-history",
        "/api/trading-system/ai-evaluation-receipt",
        "/api/dualtrack/market/bars",
        "/api/park-paper/ai-chat",
        "/api/dashboard-control/catalog",
        "/api/dashboard-control/selection",
        "/api/dashboard-control/runtime-status",
    }
)
MUTATION_EXACT = frozenset(
    {
        "/api/strategy-console/control",
        "/api/dualtrack/orders",
        "/api/park-paper/ai-chat",
        "/api/dashboard-control/selection",
        "/api/dashboard-control/preview",
        "/api/dashboard-control/account-admission",
        "/api/dashboard-control/confirm",
        "/api/dashboard-control/control",
        "/api/dashboard-control/acceptance",
    }
)
_DROP_HEADERS = frozenset(
    {
        "transfer-encoding",
        "connection",
        "keep-alive",
        "content-length",
        "content-encoding",
        "access-control-allow-origin",
    }
)
_JWK_CLIENT: Any = None
_JWK_LOCK = threading.Lock()
_AUDIT_LOCK = threading.Lock()
_LOGIN_LOCK = threading.Lock()
_LOGIN_FAILURES: dict[str, deque[float]] = defaultdict(deque)
_LOGIN_BLOCKED_UNTIL: dict[str, float] = {}
_GLOBAL_LOGIN_KEY = "__all_clients__"
_LOGIN_VERIFY_SLOTS = threading.BoundedSemaphore(
    int(os.getenv("GOLDBOT_LOGIN_VERIFY_CONCURRENCY", "2"))
)


def _is_allowed(path: str) -> bool:
    return ".." not in path and path in ALLOW_EXACT


def _upstream_envelope(method: str, path: str) -> tuple[float, int]:
    """Return the exact timeout/body envelope for one upstream request."""

    if method == "GET" and path == "/api/trading-system/supervisor-history":
        return SUPERVISOR_HISTORY_TIMEOUT, SUPERVISOR_HISTORY_MAX_BYTES
    timeout = CONTROL_TIMEOUT if method == "POST" else READ_TIMEOUT
    return timeout, READ_MAX_BYTES


def _session_cookie(headers: Any) -> str:
    try:
        cookie = SimpleCookie()
        cookie.load(str(headers.get("Cookie") or ""))
        morsel = cookie.get(SESSION_COOKIE_NAME)
        return str(morsel.value if morsel else "").strip()
    except (AttributeError, TypeError):
        return ""


def _identity_assertion(headers: Any) -> str:
    return str(headers.get("Cf-Access-Jwt-Assertion") or "").strip() or _session_cookie(
        headers
    )


def _validated_access_claims(headers: Any) -> dict[str, Any] | None:
    token = _identity_assertion(headers)
    if token.startswith("gbp1."):
        return validate_session_token(token, expected_email=ALLOWED_ACCESS_EMAIL)
    if (
        not token
        or not ACCESS_TEAM_DOMAIN
        or not ACCESS_AUD
        or not ALLOWED_ACCESS_EMAIL
        or jwt is None
    ):
        return None
    try:
        global _JWK_CLIENT
        with _JWK_LOCK:
            if _JWK_CLIENT is None:
                _JWK_CLIENT = jwt.PyJWKClient(
                    f"{ACCESS_TEAM_DOMAIN}/cdn-cgi/access/certs"
                )
        key = _JWK_CLIENT.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            audience=ACCESS_AUD,
            issuer=ACCESS_TEAM_DOMAIN,
            options={"require": ["aud", "exp", "iat", "iss"]},
        )
    except Exception:  # noqa: BLE001 - any identity uncertainty denies control.
        return None
    return claims if isinstance(claims, dict) else None


def authenticated_access_identity(headers: Any) -> dict[str, Any] | None:
    """Return a verified Cloudflare or fixed-password Park identity."""

    claims = _validated_access_claims(headers)
    email = str((claims or {}).get("email") or "").strip().lower()
    if not email or email != ALLOWED_ACCESS_EMAIL:
        return None
    return {
        "email": email,
        "issued_at": claims.get("iat"),
        "expires_at": claims.get("exp"),
        "issuer": claims.get("iss"),
        "subject": claims.get("sub"),
    }


_authenticated_identity = authenticated_access_identity


def _session_payload(headers: Any) -> dict[str, Any]:
    identity = authenticated_access_identity(headers)
    return {
        "authenticated": identity is not None,
        "can_control": identity is not None,
        "email": identity.get("email") if identity else None,
        "expires_at": identity.get("expires_at") if identity else None,
        "session_duration": ACCESS_SESSION_DURATION,
        "access_configured": bool(ALLOWED_ACCESS_EMAIL)
        and (
            password_auth_configured()
            or bool(ACCESS_TEAM_DOMAIN and ACCESS_AUD)
        ),
        "auth_method": (
            "password"
            if identity and identity.get("issuer") == "gridmind-password-gateway"
            else "cloudflare_access"
            if identity
            else None
        ),
    }


def _mutation_identity(path: str, payload: Any, headers: Any) -> dict[str, Any] | None:
    if path not in MUTATION_EXACT or not isinstance(payload, dict):
        return None
    return authenticated_access_identity(headers)


def _upstream_mutation_headers(
    headers: Any,
    identity: dict[str, Any],
) -> dict[str, str]:
    """Forward a verified assertion for independent loopback re-verification."""

    assertion = _identity_assertion(headers)
    if not assertion:
        raise ValueError("validated Park assertion is missing")
    return {
        "Content-Type": "application/json",
        "X-Goldbot-Actor-Email": str(identity["email"]),
        "Cf-Access-Jwt-Assertion": assertion,
    }


def _write_audit(event: dict[str, Any]) -> None:
    safe = {
        "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "email": event.get("email"),
        "path": event.get("path"),
        "action": event.get("action"),
        "result": event.get("result"),
        "status": event.get("status"),
    }
    try:
        ACCESS_AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _AUDIT_LOCK, ACCESS_AUDIT_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(safe, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        return


def _login_key(headers: Any, client_address: Any) -> str:
    forwarded = str(headers.get("CF-Connecting-IP") or "").strip()
    if forwarded and len(forwarded) <= 64 and all(
        character.isdigit() or character in ".:" for character in forwarded
    ):
        return forwarded
    return str(client_address[0] if client_address else "unknown")


def _login_retry_after(
    key: str,
    *,
    now: float | None = None,
    max_failures: int | None = None,
) -> int:
    selected_now = time.monotonic() if now is None else now
    selected_max = LOGIN_MAX_FAILURES if max_failures is None else max_failures
    with _LOGIN_LOCK:
        blocked_until = _LOGIN_BLOCKED_UNTIL.get(key, 0.0)
        if blocked_until > selected_now:
            return max(1, int(blocked_until - selected_now + 0.999))
        _LOGIN_BLOCKED_UNTIL.pop(key, None)
        failures = _LOGIN_FAILURES[key]
        threshold = selected_now - LOGIN_FAILURE_WINDOW_SECONDS
        while failures and failures[0] <= threshold:
            failures.popleft()
        if len(failures) >= selected_max:
            _LOGIN_BLOCKED_UNTIL[key] = selected_now + LOGIN_LOCKOUT_SECONDS
            return LOGIN_LOCKOUT_SECONDS
    return 0


def _record_login_failure(
    key: str,
    *,
    now: float | None = None,
    max_failures: int | None = None,
) -> None:
    selected_now = time.monotonic() if now is None else now
    selected_max = LOGIN_MAX_FAILURES if max_failures is None else max_failures
    with _LOGIN_LOCK:
        failures = _LOGIN_FAILURES[key]
        threshold = selected_now - LOGIN_FAILURE_WINDOW_SECONDS
        while failures and failures[0] <= threshold:
            failures.popleft()
        failures.append(selected_now)
        if len(failures) >= selected_max:
            _LOGIN_BLOCKED_UNTIL[key] = selected_now + LOGIN_LOCKOUT_SECONDS


def _clear_login_failures(key: str) -> None:
    with _LOGIN_LOCK:
        _LOGIN_FAILURES.pop(key, None)
        _LOGIN_BLOCKED_UNTIL.pop(key, None)


def _same_origin(headers: Any) -> bool:
    origin = str(headers.get("Origin") or "").strip().rstrip("/")
    if not origin:
        return False
    if PUBLIC_ORIGIN:
        return origin == PUBLIC_ORIGIN
    host = str(headers.get("Host") or "").strip()
    return bool(host) and origin in {f"https://{host}", f"http://{host}"}


def _login_csrf_cookie(headers: Any) -> str:
    try:
        cookie = SimpleCookie()
        cookie.load(str(headers.get("Cookie") or ""))
        morsel = cookie.get(LOGIN_CSRF_COOKIE_NAME)
        return str(morsel.value if morsel else "").strip()
    except (AttributeError, TypeError):
        return ""


def _login_origin_allowed(headers: Any, csrf_token: str) -> bool:
    if _same_origin(headers):
        return True
    origin = str(headers.get("Origin") or "").strip().rstrip("/")
    if origin != "null":
        return False
    cookie_token = _login_csrf_cookie(headers)
    return bool(
        cookie_token
        and csrf_token
        and secrets.compare_digest(cookie_token, csrf_token)
    )


def _new_login_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def _login_csrf_set_cookie(token: str) -> str:
    return (
        f"{LOGIN_CSRF_COOKIE_NAME}={token}; Path=/; "
        f"Max-Age={LOGIN_CSRF_TTL_SECONDS}; Secure; HttpOnly; SameSite=Strict"
    )


def _login_page(
    *,
    failed: bool = False,
    unavailable: bool = False,
    csrf_token: str = "",
) -> bytes:
    message = ""
    if failed:
        message = '<p class="error" role="alert">密码不正确，请重试。</p>'
    elif unavailable:
        message = '<p class="error" role="alert">登录暂不可用，请稍后重试。</p>'
    csrf_field = (
        f'<input type="hidden" name="csrf_token" '
        f'value="{html.escape(csrf_token, quote=True)}">'
        if csrf_token
        else ""
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Park Paper 登录</title>
  <style>
    :root {{ color-scheme: dark; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: #07111f; color: #e8eef7; }}
    main {{ width: min(390px, calc(100vw - 32px)); padding: 32px; border: 1px solid #24364d; border-radius: 18px; background: #101d2e; box-shadow: 0 24px 70px #0008; }}
    h1 {{ margin: 0 0 8px; font-size: 24px; }}
    p {{ margin: 0 0 22px; color: #9fb0c5; line-height: 1.55; }}
    label {{ display: block; margin-bottom: 8px; font-size: 14px; color: #cbd7e7; }}
    input {{ width: 100%; height: 48px; border: 1px solid #344b68; border-radius: 10px; padding: 0 14px; background: #081524; color: #fff; font-size: 18px; outline: none; }}
    input:focus {{ border-color: #4ca6ff; box-shadow: 0 0 0 3px #287dcc33; }}
    button {{ width: 100%; height: 48px; margin-top: 16px; border: 0; border-radius: 10px; background: #2f8cff; color: #fff; font-weight: 700; font-size: 16px; cursor: pointer; }}
    .error {{ margin: 0 0 16px; padding: 10px 12px; border-radius: 8px; background: #5b1d2b; color: #ffdce4; font-size: 14px; }}
    small {{ display: block; margin-top: 18px; color: #71839a; }}
  </style>
</head>
<body>
  <main>
    <h1>Park Paper</h1>
    <p>输入密码后进入原 Dashboard。</p>
    {message}
    <form method="post" action="/api/auth/login" autocomplete="on">
      {csrf_field}
      <label for="password">密码</label>
      <input id="password" name="password" type="password" required autofocus autocomplete="current-password" maxlength="1024">
      <button type="submit">登录</button>
    </form>
    <small>Paper only</small>
  </main>
</body>
</html>""".encode("utf-8")


class CloudAccessGatewayHandler(BaseHTTPRequestHandler):
    server_version = "GridMindCloudGateway/1.0"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        identity = authenticated_access_identity(self.headers)
        if parsed.path in {"", "/"}:
            self._redirect(ROOT_REDIRECT if identity else "/login")
            return
        if parsed.path == "/login":
            if identity:
                self._redirect(ROOT_REDIRECT)
            else:
                self._write_login_page()
            return
        if parsed.path == "/api/auth/session":
            self._send_json(200, _session_payload(self.headers))
            return
        if not _is_allowed(parsed.path):
            self._deny()
            return
        if identity is None:
            if parsed.path == ROOT_REDIRECT:
                self._redirect("/login")
            else:
                self._send_json(
                    401,
                    {
                        "error": "authentication_required",
                        "message": "请先输入 Dashboard 密码登录。",
                    },
                )
            return
        query = f"?{parsed.query}" if parsed.query else ""
        self._proxy(f"{UPSTREAM}{parsed.path}{query}")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/auth/login":
            self._handle_login()
            return
        if parsed.path == "/api/auth/logout":
            self._handle_logout()
            return
        if parsed.path not in MUTATION_EXACT:
            self._refuse()
            return
        identity = authenticated_access_identity(self.headers)
        if identity is None:
            self.close_connection = True
            self._audit(None, parsed.path, None, "denied", 401)
            self._send_json(
                401,
                {
                    "error": "authentication_required",
                    "message": "请先输入 Dashboard 密码登录。",
                },
            )
            return
        if not _same_origin(self.headers):
            self.close_connection = True
            self._audit(None, parsed.path, identity, "origin_denied", 403)
            self._send_json(
                403,
                {
                    "error": "origin_denied",
                    "message": "控制请求必须来自当前 Dashboard 页面。",
                },
            )
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self._refuse()
            return
        if length <= 0 or length > 64_000:
            self._refuse()
            return
        body = self.rfile.read(length)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._refuse()
            return
        if _mutation_identity(parsed.path, payload, self.headers) is None:
            self._audit(payload, parsed.path, identity, "denied", 403)
            self._send_json(403, {"error": "access_denied", "message": "Park 会话无效。"})
            return
        try:
            request_headers = _upstream_mutation_headers(self.headers, identity)
        except ValueError:
            self._audit(payload, parsed.path, identity, "denied", 403)
            self._send_json(403, {"error": "access_denied", "message": "Park 会话无效。"})
            return
        self._proxy(
            f"{UPSTREAM}{parsed.path}",
            method="POST",
            body=body,
            request_headers={
                **request_headers,
                "Host": urlparse(UPSTREAM).netloc,
                "Origin": UPSTREAM,
            },
            audit={
                "email": identity["email"],
                "path": parsed.path,
                "action": payload.get("action"),
            },
        )

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Allow", "GET, POST, OPTIONS")
        self.end_headers()

    def _handle_login(self) -> None:
        origin = str(self.headers.get("Origin") or "").strip().rstrip("/")
        if not _same_origin(self.headers) and origin != "null":
            self.close_connection = True
            self._send_json(403, {"error": "origin_denied", "message": "登录请求来源无效。"})
            return
        key = _login_key(self.headers, self.client_address)
        retry_after = max(
            _login_retry_after(key),
            _login_retry_after(
                _GLOBAL_LOGIN_KEY,
                max_failures=LOGIN_GLOBAL_MAX_FAILURES,
            ),
        )
        if retry_after:
            self.close_connection = True
            self._write_login_page(429, failed=True, retry_after=retry_after)
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length <= 0 or length > 2048:
            self._record_failed_login(key)
            return
        content_type = str(self.headers.get("Content-Type") or "").lower()
        if "application/x-www-form-urlencoded" not in content_type:
            self._record_failed_login(key)
            return
        body = self.rfile.read(length)
        try:
            values = parse_qs(
                body.decode("utf-8"),
                strict_parsing=True,
                max_num_fields=3,
            )
            supplied = str((values.get("password") or [""])[0])
            csrf_token = str((values.get("csrf_token") or [""])[0])
        except (UnicodeDecodeError, ValueError):
            supplied = ""
            csrf_token = ""
        if not _login_origin_allowed(self.headers, csrf_token):
            self.close_connection = True
            self._send_json(403, {"error": "origin_denied", "message": "登录请求来源无效。"})
            return
        if not _LOGIN_VERIFY_SLOTS.acquire(blocking=False):
            supplied = ""
            self.close_connection = True
            self._write_login_page(429, failed=True, retry_after=2)
            return
        try:
            verified = verify_password(supplied)
        finally:
            _LOGIN_VERIFY_SLOTS.release()
        if not verified:
            supplied = ""
            self._record_failed_login(key)
            return
        supplied = ""
        try:
            token = create_session_token(ALLOWED_ACCESS_EMAIL)
        except Exception:  # noqa: BLE001 - any auth-state uncertainty fails closed.
            _write_audit(
                {
                    "email": None,
                    "path": "/api/auth/login",
                    "action": None,
                    "result": "unavailable",
                    "status": 503,
                }
            )
            self._write_login_page(503, unavailable=True)
            return
        _clear_login_failures(key)
        _clear_login_failures(_GLOBAL_LOGIN_KEY)
        _write_audit(
            {
                "email": ALLOWED_ACCESS_EMAIL,
                "path": "/api/auth/login",
                "action": None,
                "result": "authenticated",
                "status": 303,
            }
        )
        self._redirect_with_cookie(
            ROOT_REDIRECT,
            f"{SESSION_COOKIE_NAME}={token}; Path=/; Max-Age={SESSION_TTL_SECONDS}; "
            "Secure; HttpOnly; SameSite=Strict",
        )

    def _record_failed_login(self, key: str) -> None:
        self.close_connection = True
        _record_login_failure(key)
        _record_login_failure(
            _GLOBAL_LOGIN_KEY,
            max_failures=LOGIN_GLOBAL_MAX_FAILURES,
        )
        _write_audit(
            {
                "email": None,
                "path": "/api/auth/login",
                "action": None,
                "result": "denied",
                "status": 401,
            }
        )
        self._write_login_page(401, failed=True)

    def _write_login_page(
        self,
        status: int = 200,
        *,
        failed: bool = False,
        unavailable: bool = False,
        retry_after: int | None = None,
    ) -> None:
        token = _new_login_csrf_token()
        headers = {
            "Content-Type": "text/html; charset=utf-8",
            "Set-Cookie": _login_csrf_set_cookie(token),
        }
        if retry_after is not None:
            headers["Retry-After"] = str(retry_after)
        self._write_response(
            status,
            _login_page(
                failed=failed,
                unavailable=unavailable,
                csrf_token=token,
            ),
            headers,
        )

    def _handle_logout(self) -> None:
        if not _same_origin(self.headers):
            self.close_connection = True
            self._send_json(403, {"error": "origin_denied", "message": "退出请求来源无效。"})
            return
        self.close_connection = True
        identity = authenticated_access_identity(self.headers)
        token = _session_cookie(self.headers)
        if token.startswith("gbp1."):
            revoke_session_token(token)
        _write_audit(
            {
                "email": (identity or {}).get("email"),
                "path": "/api/auth/logout",
                "action": None,
                "result": "logged_out",
                "status": 303,
            }
        )
        self._redirect_with_cookie(
            "/login",
            f"{SESSION_COOKIE_NAME}=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Strict",
        )

    def _proxy(
        self,
        target: str,
        *,
        method: str = "GET",
        body: bytes | None = None,
        request_headers: dict[str, str] | None = None,
        audit: dict[str, Any] | None = None,
    ) -> None:
        path = urlparse(target).path
        timeout, max_bytes = _upstream_envelope(method, path)
        try:
            with urlopen(
                Request(
                    target,
                    data=body,
                    method=method,
                    headers=request_headers or {},
                ),
                timeout=timeout,
            ) as response:
                response_body = response.read(max_bytes + 1)
                if len(response_body) > max_bytes:
                    if audit:
                        _write_audit(
                            {
                                **audit,
                                "result": "upstream_response_too_large",
                                "status": 502,
                            }
                        )
                    self._send_json(
                        502,
                        {
                            "error": "upstream_response_too_large",
                            "message": (
                                "后台只读响应超过安全上限，未转发不完整数据。"
                            ),
                        },
                    )
                    return
                status = int(response.status)
                headers = response.headers
        except HTTPError as exc:
            content_type = str(exc.headers.get("Content-Type") or "")
            response_body = exc.read(64_000)
            if "application/json" not in content_type.lower():
                content_type = "application/json; charset=utf-8"
                response_body = json.dumps(
                    {
                        "error": "upstream_error",
                        "message": f"Dashboard 后端拒绝了本次请求（HTTP {exc.code}）。",
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
            if audit:
                _write_audit({**audit, "result": "upstream_error", "status": exc.code})
            self._write_response(exc.code, response_body, {"Content-Type": content_type})
            return
        except (TimeoutError, URLError, OSError):
            if audit:
                _write_audit({**audit, "result": "upstream_unavailable", "status": 502})
            self._send_json(
                502,
                {
                    "error": "upstream_unavailable",
                    "message": "后台响应中断；操作结果需要按权威状态核对，请勿重复点击。",
                },
            )
            return
        forwarded = {
            key: value
            for key, value in headers.items()
            if key.lower() not in _DROP_HEADERS
        }
        if "gzip" in str(self.headers.get("Accept-Encoding") or "").lower() and len(response_body) > 1400:
            response_body = gzip.compress(response_body, 6)
            forwarded["Content-Encoding"] = "gzip"
            forwarded["Vary"] = "Accept-Encoding"
        self._write_response(status, response_body, forwarded)
        if audit:
            _write_audit({**audit, "result": "proxied", "status": status})

    def _write_response(self, status: int, body: bytes, headers: dict[str, str]) -> None:
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        if self.close_connection and not any(
            key.lower() == "connection" for key in headers
        ):
            self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
            "form-action 'self'",
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            return

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        self._write_response(
            status,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            {"Content-Type": "application/json; charset=utf-8"},
        )

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _redirect_with_cookie(self, location: str, cookie: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Set-Cookie", cookie)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _deny(self) -> None:
        self._write_response(404, b"404", {"Content-Type": "text/plain"})

    def _refuse(self) -> None:
        # The body is deliberately left unread.  Close this HTTP/1.1 origin
        # connection so a proxy cannot reuse it and reinterpret the rejected
        # body's bytes as the start of a subsequent request.
        self.close_connection = True
        self.send_response(405)
        self.send_header("Allow", "GET, POST, OPTIONS")
        self.send_header("Connection", "close")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _audit(
        self,
        payload: Any,
        path: str,
        identity: dict[str, Any] | None,
        result: str,
        status: int,
    ) -> None:
        _write_audit(
            {
                "email": (identity or {}).get("email"),
                "path": path,
                "action": payload.get("action") if isinstance(payload, dict) else None,
                "result": result,
                "status": status,
            }
        )

    do_PUT = do_DELETE = do_PATCH = do_HEAD = _refuse

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="GridMind Cloud Access gateway.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    ThreadingHTTPServer(
        (args.host, args.port),
        CloudAccessGatewayHandler,
    ).serve_forever()


if __name__ == "__main__":
    main()
