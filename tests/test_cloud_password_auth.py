from __future__ import annotations

import base64
import http.client
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode

from services import cloud_access_gateway as gateway
from services import cloud_password_auth as auth
from services import cycle_risk_envelope
from pipelines import dashboard_server


def _private(path: Path, value: str) -> None:
    path.write_text(value, encoding="ascii")
    path.chmod(0o600)


def _configured_auth(monkeypatch, tmp_path: Path) -> tuple[Path, Path, Path]:
    password_path = tmp_path / "password.scrypt"
    secret_path = tmp_path / "session-secret"
    database_path = tmp_path / "sessions.sqlite3"
    _private(password_path, auth.password_record("test-password") + "\n")
    secret = base64.urlsafe_b64encode(b"s" * 32).decode("ascii").rstrip("=")
    _private(secret_path, secret + "\n")
    monkeypatch.setattr(auth, "PASSWORD_RECORD_PATH", password_path)
    monkeypatch.setattr(auth, "SESSION_SECRET_PATH", secret_path)
    monkeypatch.setattr(auth, "SESSION_DB_PATH", database_path)
    monkeypatch.setattr(gateway, "ALLOWED_ACCESS_EMAIL", "park@example.com")
    monkeypatch.setattr(gateway, "PUBLIC_ORIGIN", "http://goldbot.example")
    gateway._LOGIN_FAILURES.clear()
    gateway._LOGIN_BLOCKED_UNTIL.clear()
    return password_path, secret_path, database_path


def test_password_record_is_non_reversible_and_private_file_is_enforced(
    tmp_path: Path,
) -> None:
    path = tmp_path / "password.scrypt"
    record = auth.password_record("test-password", salt=b"0" * 16)
    assert "test-password" not in record
    _private(path, record + "\n")

    assert auth.verify_password("test-password", path=path) is True
    assert auth.verify_password("wrong-password", path=path) is False

    path.chmod(0o644)
    assert auth.verify_password("test-password", path=path) is False


def test_signed_session_is_actor_bound_tamper_evident_and_revocable(
    tmp_path: Path,
) -> None:
    secret_path = tmp_path / "session-secret"
    database_path = tmp_path / "sessions.sqlite3"
    secret = base64.urlsafe_b64encode(b"k" * 32).decode("ascii").rstrip("=")
    _private(secret_path, secret + "\n")
    token = auth.create_session_token(
        "park@example.com",
        now=1_000,
        ttl_seconds=600,
        secret_path=secret_path,
        database_path=database_path,
    )

    identity = auth.validate_session_token(
        token,
        expected_email="park@example.com",
        now=1_001,
        secret_path=secret_path,
        database_path=database_path,
    )
    assert identity == {
        "email": "park@example.com",
        "iat": 1_000,
        "exp": 1_600,
        "iss": auth.SESSION_ISSUER,
        "sub": "park",
    }
    assert database_path.stat().st_mode & 0o777 == 0o600
    assert (
        auth.validate_session_token(
            token,
            expected_email="intruder@example.com",
            now=1_001,
            secret_path=secret_path,
            database_path=database_path,
        )
        is None
    )
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert (
        auth.validate_session_token(
            tampered,
            expected_email="park@example.com",
            now=1_001,
            secret_path=secret_path,
            database_path=database_path,
        )
        is None
    )

    auth.revoke_session_token(token, database_path=database_path)
    assert (
        auth.validate_session_token(
            token,
            expected_email="park@example.com",
            now=1_001,
            secret_path=secret_path,
            database_path=database_path,
        )
        is None
    )


def test_password_login_restores_authenticated_dashboard_and_logout_revokes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configured_auth(monkeypatch, tmp_path)
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), gateway.CloudAccessGatewayHandler
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        anonymous = http.client.HTTPConnection(host, port, timeout=2)
        anonymous.request("GET", "/")
        response = anonymous.getresponse()
        assert response.status == 302
        assert response.getheader("Location") == "/login"
        response.read()
        anonymous.close()

        body = urlencode({"password": "test-password"})
        login = http.client.HTTPConnection(host, port, timeout=2)
        login.request(
            "POST",
            "/api/auth/login",
            body=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Host": "goldbot.example",
                "Origin": "http://goldbot.example",
            },
        )
        response = login.getresponse()
        assert response.status == 303
        assert response.getheader("Location") == "/dashboard-v5.html"
        set_cookie = str(response.getheader("Set-Cookie") or "")
        assert set_cookie.startswith(f"{gateway.SESSION_COOKIE_NAME}=")
        assert "; Secure; HttpOnly; SameSite=Strict" in set_cookie
        cookie = set_cookie.split(";", 1)[0]
        response.read()
        login.close()

        session = http.client.HTTPConnection(host, port, timeout=2)
        session.request("GET", "/api/auth/session", headers={"Cookie": cookie})
        response = session.getresponse()
        payload = json.loads(response.read())
        assert payload["authenticated"] is True
        assert payload["can_control"] is True
        assert payload["auth_method"] == "password"
        session.close()

        logout = http.client.HTTPConnection(host, port, timeout=2)
        logout.request(
            "POST",
            "/api/auth/logout",
            headers={
                "Cookie": cookie,
                "Host": "goldbot.example",
                "Origin": "http://goldbot.example",
                "Content-Length": "0",
            },
        )
        response = logout.getresponse()
        assert response.status == 303
        assert "Max-Age=0" in str(response.getheader("Set-Cookie") or "")
        response.read()
        logout.close()

        revoked = http.client.HTTPConnection(host, port, timeout=2)
        revoked.request("GET", "/api/auth/session", headers={"Cookie": cookie})
        response = revoked.getresponse()
        payload = json.loads(response.read())
        assert payload["authenticated"] is False
        assert payload["can_control"] is False
        revoked.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_password_assertion_is_reverified_at_dashboard_and_park_policy_boundaries(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configured_auth(monkeypatch, tmp_path)
    monkeypatch.setenv("GOLDBOT_ACCESS_EMAIL", "park@example.com")
    token = auth.create_session_token("park@example.com")
    handler = object.__new__(dashboard_server.DashboardHandler)
    handler.headers = {
        "X-Goldbot-Actor-Email": "park@example.com",
        "Cf-Access-Jwt-Assertion": token,
    }
    handler.client_address = ("127.0.0.1", 12345)

    actor = handler._control_actor()

    assert actor == {
        "email": "park@example.com",
        "transport": "public_gateway",
        "client": "127.0.0.1",
        "_access_assertion": token,
    }
    park_actor = cycle_risk_envelope._park_actor(actor)
    assert park_actor["email"] == "park@example.com"
    assert park_actor["transport"] == "public_gateway"
    assert park_actor["access_issuer"] == auth.SESSION_ISSUER
    assert len(park_actor["access_assertion_digest"]) == 64


def test_login_failure_rate_limit_is_bounded(monkeypatch) -> None:
    monkeypatch.setattr(gateway, "LOGIN_MAX_FAILURES", 2)
    monkeypatch.setattr(gateway, "LOGIN_GLOBAL_MAX_FAILURES", 3)
    monkeypatch.setattr(gateway, "LOGIN_FAILURE_WINDOW_SECONDS", 60)
    monkeypatch.setattr(gateway, "LOGIN_LOCKOUT_SECONDS", 90)
    gateway._LOGIN_FAILURES.clear()
    gateway._LOGIN_BLOCKED_UNTIL.clear()

    gateway._record_login_failure("client", now=100.0)
    assert gateway._login_retry_after("client", now=101.0) == 0
    gateway._record_login_failure("client", now=102.0)
    assert gateway._login_retry_after("client", now=103.0) == 89
    assert gateway._login_retry_after("other", now=103.0) == 0

    gateway._record_login_failure(
        gateway._GLOBAL_LOGIN_KEY,
        now=100.0,
        max_failures=3,
    )
    gateway._record_login_failure(
        gateway._GLOBAL_LOGIN_KEY,
        now=101.0,
        max_failures=3,
    )
    gateway._record_login_failure(
        gateway._GLOBAL_LOGIN_KEY,
        now=102.0,
        max_failures=3,
    )
    assert (
        gateway._login_retry_after(
            gateway._GLOBAL_LOGIN_KEY,
            now=103.0,
            max_failures=3,
        )
        == 89
    )
