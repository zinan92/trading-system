from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.cloud_ai_provider import CloudAIProviderReadiness, _digest, _sha256
from services.journal_store import write_json


NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
SHA = "a" * 40
TREE = "b" * 40


def _receipt(executable: Path, *, checked_at: datetime = NOW) -> dict:
    row = {
        "schema_version": "cloud-ai-provider-readiness-v1",
        "checked_at": checked_at.isoformat(),
        "expires_at": (checked_at + timedelta(hours=24)).isoformat(),
        "status": "pass",
        "source_sha": SHA,
        "source_tree_sha": TREE,
        "provider": {
            "name": "codex_cli_chatgpt",
            "configured_command": "codex",
            "executable": str(executable),
            "executable_sha256": _sha256(executable),
            "version": "codex-cli 0.146.0",
            "auth_status": "logged_in",
        },
        "timeout_seconds": 25,
        "response_contract": {
            "status": "pass",
            "required_keys": ["direction", "style", "rationale", "ai_self_assessment"],
        },
        "operations": {
            "orders_allowed": False,
            "production_mutation_allowed": False,
            "uses_exchange_credentials": False,
        },
        "failure_code": None,
    }
    row["readiness_digest"] = _digest(row)
    return row


def test_provider_readiness_accepts_source_bound_receipt(tmp_path: Path) -> None:
    executable = tmp_path / "codex"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    output = tmp_path / "outputs"
    write_json(output / "cloud" / "provider" / "readiness_current.json", [_receipt(executable)])

    result = CloudAIProviderReadiness(
        output,
        repo_root=tmp_path,
        now=lambda: NOW,
        source_attestation=lambda: {
            "source_sha": SHA,
            "source_tree_sha": TREE,
            "tracked_tree_clean": True,
        },
    ).verify()

    assert result["ok"] is True
    assert result["provider"]["auth_status"] == "logged_in"


def test_provider_readiness_fails_closed_when_executable_changes(tmp_path: Path) -> None:
    executable = tmp_path / "codex"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    output = tmp_path / "outputs"
    write_json(output / "cloud" / "provider" / "readiness_current.json", [_receipt(executable)])
    executable.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")

    result = CloudAIProviderReadiness(
        output,
        repo_root=tmp_path,
        now=lambda: NOW,
        source_attestation=lambda: {
            "source_sha": SHA,
            "source_tree_sha": TREE,
            "tracked_tree_clean": True,
        },
    ).verify()

    assert result["ok"] is False
    assert result["blocker"] == "cloud_ai_provider_executable_changed"
