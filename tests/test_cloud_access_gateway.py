from __future__ import annotations

from io import BytesIO
import json
import socket
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from services import cloud_access_gateway as gateway


def test_gateway_allowlist_exposes_only_dashboard_contracts() -> None:
    assert gateway.ROOT_REDIRECT == "/dashboard-v5.html"
    assert gateway._is_allowed("/dashboard-v5.html")
    assert gateway._is_allowed("/assets/tokens.css")
    assert gateway._is_allowed("/api/auth/session")
    assert gateway._is_allowed("/api/trading-system/read-model")
    assert gateway._is_allowed("/api/trading-system/cloud-health")
    assert gateway._is_allowed("/api/trading-system/supervisor-history")
    assert gateway._is_allowed("/api/park-paper/ai-chat")
    assert gateway._is_allowed("/api/dashboard-control/catalog")
    assert gateway._is_allowed("/api/dashboard-control/runtime-status")
    assert gateway._is_allowed("/api/dashboard-control/market-bars")
    assert gateway.MUTATION_EXACT == {
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
    assert not gateway._is_allowed("/outputs/dualtrack/strategy_control/runtime.json")
    assert not gateway._is_allowed("/api/trading-system/read-model/internal")
    assert not gateway._is_allowed("/api/trading-system/read-model/")
    assert not gateway._is_allowed("/../configs/paper.env")



def test_supervisor_history_get_has_an_exact_dedicated_upstream_envelope(
    monkeypatch,
) -> None:
    monkeypatch.setattr(gateway, "READ_TIMEOUT", 20.0)
    monkeypatch.setattr(gateway, "READ_MAX_BYTES", 8_000_000)
    monkeypatch.setattr(gateway, "SUPERVISOR_HISTORY_TIMEOUT", 90.0)
    monkeypatch.setattr(
        gateway,
        "SUPERVISOR_HISTORY_MAX_BYTES",
        64 * 1024 * 1024,
    )

    assert gateway._upstream_envelope(
        "GET",
        "/api/trading-system/supervisor-history",
    ) == (90.0, 64 * 1024 * 1024)
    assert gateway._upstream_envelope(
        "GET",
        "/api/trading-system/read-model",
    ) == (20.0, 8_000_000)
    assert gateway._upstream_envelope(
        "GET",
        "/api/trading-system/supervisor-history/internal",
    ) == (20.0, 8_000_000)


class _FakeUpstreamResponse(BytesIO):
    status = 200

    def __init__(self, body: bytes) -> None:
        super().__init__(body)
        self.headers = {"Content-Type": "application/json"}
        self.requested_bytes: int | None = None

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def read(self, size: int = -1) -> bytes:
        self.requested_bytes = size
        return super().read(size)


def test_gateway_fails_closed_instead_of_forwarding_truncated_json(
    monkeypatch,
) -> None:
    response = _FakeUpstreamResponse(b"123456789")
    monkeypatch.setattr(gateway, "READ_MAX_BYTES", 8)
    monkeypatch.setattr(
        gateway,
        "urlopen",
        lambda *_args, **_kwargs: response,
    )
    sent: list[tuple[int, dict]] = []
    forwarded: list[tuple[int, bytes, dict]] = []
    handler = object.__new__(gateway.CloudAccessGatewayHandler)
    handler.headers = {}
    handler._send_json = lambda status, payload: sent.append(
        (status, payload)
    )
    handler._write_response = lambda status, body, headers: forwarded.append(
        (status, body, headers)
    )

    handler._proxy(
        "http://127.0.0.1:8765/api/trading-system/read-model"
    )

    assert response.requested_bytes == 9
    assert forwarded == []
    assert sent == [
        (
            502,
            {
                "error": "upstream_response_too_large",
                "message": (
                    "后台只读响应超过安全上限，未转发不完整数据。"
                ),
            },
        )
    ]


def test_supervisor_history_forwards_complete_body_above_normal_read_cap(
    monkeypatch,
) -> None:
    body = b"123456789"
    response = _FakeUpstreamResponse(body)
    monkeypatch.setattr(gateway, "READ_MAX_BYTES", 8)
    monkeypatch.setattr(gateway, "SUPERVISOR_HISTORY_TIMEOUT", 90.0)
    monkeypatch.setattr(gateway, "SUPERVISOR_HISTORY_MAX_BYTES", 16)
    timeouts: list[float] = []

    def fake_urlopen(_request, *, timeout):
        timeouts.append(timeout)
        return response

    monkeypatch.setattr(gateway, "urlopen", fake_urlopen)
    forwarded: list[tuple[int, bytes, dict]] = []
    handler = object.__new__(gateway.CloudAccessGatewayHandler)
    handler.headers = {}
    handler._write_response = lambda status, payload, headers: forwarded.append(
        (status, payload, headers)
    )

    handler._proxy(
        "http://127.0.0.1:8765/api/trading-system/supervisor-history"
        "?cycle_id=2026-08-04_DAY"
    )

    assert timeouts == [90.0]
    assert response.requested_bytes == 17
    assert forwarded == [
        (200, body, {"Content-Type": "application/json"})
    ]


def test_verified_identity_grants_only_the_allowlisted_dashboard_control(
    monkeypatch,
) -> None:
    monkeypatch.setattr(gateway, "ALLOWED_ACCESS_EMAIL", "operator@example.com")
    monkeypatch.setattr(
        gateway,
        "_validated_access_claims",
        lambda _headers: {"email": "operator@example.com", "exp": 1},
    )

    headers = {"Cf-Access-Jwt-Assertion": "signed-access-jwt"}
    identity = gateway.authenticated_access_identity(headers)

    assert identity == {
        "email": "operator@example.com",
        "issued_at": None,
        "expires_at": 1,
        "issuer": None,
        "subject": None,
    }
    session = gateway._session_payload(headers)
    assert session["authenticated"] is True
    assert session["can_control"] is True


class _UnreadableBody:
    def read(self, _size: int = -1) -> bytes:
        raise AssertionError("public gateway must refuse before reading POST body")


def test_anonymous_gateway_refuses_mutation_before_body_parse_or_upstream(
    monkeypatch,
) -> None:
    audits: list[dict] = []
    refused: list[bool] = []
    sent: list[tuple[int, dict]] = []
    monkeypatch.setattr(gateway, "authenticated_access_identity", lambda _headers: None)
    monkeypatch.setattr(
        gateway,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("public gateway must not call upstream for POST")
        ),
    )

    paths = [
        "/api/strategy-console/control",
        "/api/dualtrack/orders",
        "/api/park-paper/ai-chat",
        "/api/unlisted-neighbor",
    ]
    for path in paths:
        handler = object.__new__(gateway.CloudAccessGatewayHandler)
        handler.path = path
        handler.headers = {
            "Content-Length": "20",
        }
        handler.rfile = _UnreadableBody()
        handler._refuse = lambda: refused.append(True)
        handler._audit = lambda payload, selected_path, identity, result, status: audits.append(
            {
                "payload": payload,
                "path": selected_path,
                "identity": identity,
                "result": result,
                "status": status,
            }
        )
        handler._send_json = lambda status, payload: sent.append((status, payload))

        handler.do_POST()

    assert refused == [True]
    assert [row["path"] for row in audits] == paths[:3]
    assert all(row["result"] == "denied" and row["status"] == 401 for row in audits)
    assert [status for status, _payload in sent] == [401, 401, 401]


def test_authenticated_same_origin_mutation_forwards_verified_assertion(
    monkeypatch,
) -> None:
    identity = {"email": "operator@example.com"}
    monkeypatch.setattr(
        gateway,
        "authenticated_access_identity",
        lambda _headers: identity,
    )
    monkeypatch.setattr(gateway, "PUBLIC_ORIGIN", "https://goldbot.example")
    forwarded: list[dict] = []
    body = b'{"action":"preview"}'
    token = "gbp1.payload.signature"
    handler = object.__new__(gateway.CloudAccessGatewayHandler)
    handler.path = "/api/strategy-console/control"
    handler.headers = {
        "Content-Length": str(len(body)),
        "Content-Type": "application/json",
        "Cookie": f"{gateway.SESSION_COOKIE_NAME}={token}",
        "Host": "goldbot.example",
        "Origin": "https://goldbot.example",
    }
    handler.rfile = BytesIO(body)
    handler._proxy = lambda target, **kwargs: forwarded.append(
        {"target": target, **kwargs}
    )

    handler.do_POST()

    assert len(forwarded) == 1
    assert forwarded[0]["method"] == "POST"
    assert forwarded[0]["body"] == body
    assert forwarded[0]["request_headers"] == {
        "Content-Type": "application/json",
        "X-Goldbot-Actor-Email": "operator@example.com",
        "Cf-Access-Jwt-Assertion": token,
        "Host": "127.0.0.1:8765",
        "Origin": "http://127.0.0.1:8765",
    }


def test_authenticated_same_origin_ai_chat_mutation_is_forwarded_as_control(
    monkeypatch,
) -> None:
    identity = {"email": "operator@example.com"}
    monkeypatch.setattr(gateway, "authenticated_access_identity", lambda _headers: identity)
    monkeypatch.setattr(gateway, "PUBLIC_ORIGIN", "https://goldbot.example")
    forwarded: list[dict] = []
    body = '{"action":"message","message":"中性网格 4450~4100"}'.encode()
    token = "gbp1.ai.payload"
    handler = object.__new__(gateway.CloudAccessGatewayHandler)
    handler.path = "/api/park-paper/ai-chat"
    handler.headers = {
        "Content-Length": str(len(body)),
        "Content-Type": "application/json",
        "Cookie": f"{gateway.SESSION_COOKIE_NAME}={token}",
        "Host": "goldbot.example",
        "Origin": "https://goldbot.example",
    }
    handler.rfile = BytesIO(body)
    handler._proxy = lambda target, **kwargs: forwarded.append({"target": target, **kwargs})

    handler.do_POST()

    assert len(forwarded) == 1
    assert forwarded[0]["method"] == "POST"
    assert forwarded[0]["body"] == body
    assert forwarded[0]["request_headers"]["X-Goldbot-Actor-Email"] == "operator@example.com"


def test_null_origin_login_requires_matching_csrf_challenge(monkeypatch) -> None:
    monkeypatch.setattr(gateway, "PUBLIC_ORIGIN", "https://goldbot.example")
    token = "login-challenge"
    headers = {
        "Origin": "null",
        "Cookie": f"{gateway.LOGIN_CSRF_COOKIE_NAME}={token}",
    }

    assert gateway._login_origin_allowed(headers, token) is True
    assert gateway._login_origin_allowed(headers, "wrong") is False
    assert gateway._login_origin_allowed({"Origin": "null"}, token) is False
    assert gateway._login_origin_allowed(
        {"Origin": "https://goldbot.example"}, ""
    ) is True
    assert gateway._login_origin_allowed(
        {"Origin": "https://evil.example", "Cookie": headers["Cookie"]},
        token,
    ) is False


def test_login_page_embeds_csrf_challenge_and_host_cookie() -> None:
    token = "login-challenge"
    page = gateway._login_page(csrf_token=token).decode("utf-8")
    cookie = gateway._login_csrf_set_cookie(token)

    assert f'name="csrf_token" value="{token}"' in page
    assert gateway.LOGIN_CSRF_COOKIE_NAME in cookie
    assert "Path=/" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie


def test_null_origin_remains_denied_for_authenticated_mutation(monkeypatch) -> None:
    monkeypatch.setattr(
        gateway,
        "authenticated_access_identity",
        lambda _headers: {"email": "operator@example.com"},
    )
    body = b'{"action":"preview"}'
    sent: list[tuple[int, dict]] = []
    audited: list[tuple[object, str, object, str, int]] = []
    forwarded: list[object] = []
    handler = object.__new__(gateway.CloudAccessGatewayHandler)
    handler.path = "/api/strategy-console/control"
    handler.headers = {
        "Content-Length": str(len(body)),
        "Content-Type": "application/json",
        "Cookie": "__Host-gridmind_session=gbp1.payload.signature",
        "Origin": "null",
    }
    handler.rfile = BytesIO(body)
    handler._send_json = lambda status, payload: sent.append((status, payload))
    handler._audit = lambda payload, path, identity, result, status: audited.append(
        (payload, path, identity, result, status)
    )
    handler._proxy = lambda *_args, **_kwargs: forwarded.append(True)

    handler.do_POST()

    assert sent == [
        (
            403,
            {
                "error": "origin_denied",
                "message": "控制请求必须来自当前 Dashboard 页面。",
            },
        )
    ]
    assert audited[-1][3:] == ("origin_denied", 403)
    assert forwarded == []


def test_refused_body_cannot_contaminate_a_reused_origin_connection() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), gateway.CloudAccessGatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    response = bytearray()
    try:
        with socket.create_connection(server.server_address, timeout=2) as client:
            client.settimeout(2)
            client.sendall(
                b"POST /api/strategy-console/control HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: 2\r\n"
                b"Connection: keep-alive\r\n\r\n"
                b"{}"
                b"GET /api/auth/session HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n\r\n"
            )
            while True:
                try:
                    chunk = client.recv(4096)
                except ConnectionResetError:
                    break
                if not chunk:
                    break
                response.extend(chunk)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert response.count(b"HTTP/1.1 ") == 1
    assert b"HTTP/1.1 401 Unauthorized" in response
    assert b"Connection: close" in response
    assert b'"can_control"' not in response


def test_access_audit_never_persists_assertion_or_secret(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(gateway, "ACCESS_AUDIT_PATH", path)

    gateway._write_audit(
        {
            "email": "operator@example.com",
            "path": "/api/strategy-console/control",
            "action": "stop",
            "result": "proxied",
            "status": 200,
            "Cf-Access-Jwt-Assertion": "secret-jwt",
            "url": "https://deadman.invalid/secret-token",
        }
    )

    raw = path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert payload["email"] == "operator@example.com"
    assert "secret-jwt" not in raw
    assert "secret-token" not in raw
