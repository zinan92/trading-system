"""Public, read-only allowlist proxy for the Park Paper Dashboard.

Cloudflare may publish this viewer without an interactive Access login.  The
gateway is therefore the final public security boundary: it proxies only the
exact GET allowlist and never forwards Dashboard mutations.  Telegram remains
the sole Park Paper control plane.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

try:
    import jwt
except ImportError:  # pragma: no cover - production stays fail-closed.
    jwt = None


UPSTREAM = os.getenv("GOLDBOT_UPSTREAM", "http://127.0.0.1:8765").rstrip("/")
ACCESS_TEAM_DOMAIN = os.getenv("GOLDBOT_ACCESS_TEAM_DOMAIN", "").rstrip("/")
ACCESS_AUD = os.getenv("GOLDBOT_ACCESS_AUD", "")
ALLOWED_ACCESS_EMAIL = os.getenv("GOLDBOT_ACCESS_EMAIL", "").strip().lower()
ACCESS_SESSION_DURATION = os.getenv("GOLDBOT_ACCESS_SESSION_DURATION", "168h")
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
ROOT_REDIRECT = "/park-paper-dashboard.html"

ALLOW_EXACT = frozenset(
    {
        "/park-paper-dashboard.html",
        "/api/auth/session",
        "/api/park-paper/read-model",
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


def _is_allowed(path: str) -> bool:
    return ".." not in path and path in ALLOW_EXACT


def _upstream_envelope(method: str, path: str) -> tuple[float, int]:
    """Return the exact timeout/body envelope for one upstream request."""

    if method == "GET" and path == "/api/trading-system/supervisor-history":
        return SUPERVISOR_HISTORY_TIMEOUT, SUPERVISOR_HISTORY_MAX_BYTES
    timeout = CONTROL_TIMEOUT if method == "POST" else READ_TIMEOUT
    return timeout, READ_MAX_BYTES


def _validated_access_claims(headers: Any) -> dict[str, Any] | None:
    token = headers.get("Cf-Access-Jwt-Assertion")
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
    """Return the cryptographically verified Cloudflare Access identity."""

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
        # A valid historical Access session may still be attached while the
        # public policy change propagates.  Identity must never re-enable a
        # Dashboard mutation; Telegram is the only control plane.
        "can_control": False,
        "email": identity.get("email") if identity else None,
        "expires_at": identity.get("expires_at") if identity else None,
        "session_duration": ACCESS_SESSION_DURATION,
        "access_configured": bool(
            ACCESS_TEAM_DOMAIN and ACCESS_AUD and ALLOWED_ACCESS_EMAIL
        ),
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


class CloudAccessGatewayHandler(BaseHTTPRequestHandler):
    server_version = "GridMindCloudGateway/1.0"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"", "/"}:
            self._redirect(ROOT_REDIRECT)
            return
        if parsed.path == "/api/auth/session":
            self._send_json(200, _session_payload(self.headers))
            return
        if not _is_allowed(parsed.path):
            self._deny()
            return
        query = f"?{parsed.query}" if parsed.query else ""
        self._proxy(f"{UPSTREAM}{parsed.path}{query}")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        _write_audit(
            {
                "email": None,
                "path": parsed.path,
                "action": None,
                "result": "dashboard_read_only",
                "status": 405,
            }
        )
        # Refuse before reading or parsing the body.  This makes the public
        # boundary independent of Access identity and of any upstream action
        # semantics, including the legacy read-only-looking `preview` action.
        self._refuse()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Allow", "GET, OPTIONS")
        self.end_headers()

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
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
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
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _deny(self) -> None:
        self._write_response(404, b"404", {"Content-Type": "text/plain"})

    def _refuse(self) -> None:
        self.send_response(405)
        self.send_header("Allow", "GET, OPTIONS")
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
