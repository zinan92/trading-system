from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path

from services import cloud_access_gateway as gateway


def test_gateway_allowlist_exposes_only_dashboard_contracts() -> None:
    assert gateway._is_allowed("/dashboard-v5.html")
    assert gateway._is_allowed("/api/trading-system/cloud-health")
    assert gateway._is_allowed("/api/trading-system/daily-self-review")
    assert gateway._is_allowed("/api/trading-system/supervisor-history")
    assert not gateway._is_allowed("/outputs/dualtrack/strategy_control/runtime.json")
    assert not gateway._is_allowed("/api/trading-system/cloud-health/internal")
    assert not gateway._is_allowed("/api/trading-system/supervisor-history/internal")
    assert not gateway._is_allowed("/api/trading-system/supervisor-history/")
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


def test_access_identity_never_grants_dashboard_control(monkeypatch) -> None:
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
    assert session["can_control"] is False


class _UnreadableBody:
    def read(self, _size: int = -1) -> bytes:
        raise AssertionError("public gateway must refuse before reading POST body")


def test_public_gateway_refuses_post_before_body_parse_or_upstream(monkeypatch) -> None:
    audits: list[dict] = []
    refused: list[bool] = []
    monkeypatch.setattr(gateway, "_write_audit", audits.append)
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
        "/api/unlisted-neighbor",
    ]
    for path in paths:
        handler = object.__new__(gateway.CloudAccessGatewayHandler)
        handler.path = path
        handler.headers = {
            "Content-Length": "20",
            "Cf-Access-Jwt-Assertion": "signed-access-jwt",
        }
        handler.rfile = _UnreadableBody()
        handler._refuse = lambda: refused.append(True)

        handler.do_POST()

    assert refused == [True, True, True]
    assert [row["path"] for row in audits] == paths
    assert all(
        row
        == {
            "email": None,
            "path": row["path"],
            "action": None,
            "result": "dashboard_read_only",
            "status": 405,
        }
        for row in audits
    )


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
