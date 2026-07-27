from __future__ import annotations

import json
from pathlib import Path

from services import cloud_access_gateway as gateway


def test_gateway_allowlist_exposes_only_dashboard_contracts() -> None:
    assert gateway._is_allowed("/dashboard-v5.html")
    assert gateway._is_allowed("/api/trading-system/cloud-health")
    assert gateway._is_allowed("/api/trading-system/daily-self-review")
    assert not gateway._is_allowed("/outputs/dualtrack/strategy_control/runtime.json")
    assert not gateway._is_allowed("/api/trading-system/cloud-health/internal")
    assert not gateway._is_allowed("/../configs/paper.env")


def test_mutation_requires_validated_allowed_identity(monkeypatch) -> None:
    monkeypatch.setattr(gateway, "ALLOWED_ACCESS_EMAIL", "operator@example.com")
    monkeypatch.setattr(
        gateway,
        "_validated_access_claims",
        lambda _headers: {"email": "operator@example.com", "exp": 1},
    )

    identity = gateway._mutation_identity(
        "/api/strategy-console/control",
        {"action": "start"},
        {"Cf-Access-Jwt-Assertion": "secret"},
    )

    assert identity == {
        "email": "operator@example.com",
        "expires_at": 1,
        "subject": None,
    }


def test_wrong_or_missing_identity_cannot_mutate(monkeypatch) -> None:
    monkeypatch.setattr(gateway, "ALLOWED_ACCESS_EMAIL", "operator@example.com")
    monkeypatch.setattr(
        gateway,
        "_validated_access_claims",
        lambda _headers: {"email": "intruder@example.com"},
    )

    assert gateway._mutation_identity(
        "/api/strategy-console/control",
        {"action": "stop"},
        {"Cf-Access-Jwt-Assertion": "secret"},
    ) is None
    assert gateway._mutation_identity(
        "/api/trading-system/read-model",
        {},
        {},
    ) is None


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
