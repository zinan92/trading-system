from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess

import pytest

from pipelines import cloud_ai_provider_readiness as readiness_pipeline
from services.cloud_ai_provider import (
    CloudAIProviderReadiness,
    CloudAIProviderReadinessGateError,
    PROVIDER_READINESS_INVALID,
    PROVIDER_READINESS_UNAVAILABLE,
    _digest,
    _sha256,
    require_cloud_ai_provider_readiness,
    validate_provider_readiness_proof,
)
from services.journal_store import load_json, write_json
from pipelines.cloud_ai_provider_readiness import _auth_status_ready


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


def test_provider_auth_accepts_success_on_stderr_but_remains_fail_closed() -> None:
    assert _auth_status_ready(
        subprocess.CompletedProcess(
            args=["codex", "login", "status"],
            returncode=0,
            stdout="",
            stderr="Logged in using ChatGPT\n",
        )
    ) is True
    assert _auth_status_ready(
        subprocess.CompletedProcess(
            args=["codex", "login", "status"],
            returncode=1,
            stdout="Logged in using ChatGPT\n",
            stderr="",
        )
    ) is False


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (
            subprocess.TimeoutExpired(cmd=["codex", "--version"], timeout=10),
            "strategy_recommendation_provider_timeout",
        ),
        (
            OSError("provider executable unavailable"),
            "strategy_recommendation_provider_unavailable",
        ),
    ],
)
def test_readiness_generator_normalizes_subprocess_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    expected_code: str,
) -> None:
    executable = tmp_path / "codex"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setattr(
        readiness_pipeline,
        "current_source_attestation",
        lambda repo_root: {
            "source_sha": SHA,
            "source_tree_sha": TREE,
        },
    )
    monkeypatch.setattr(
        readiness_pipeline,
        "dualtrack_config",
        lambda: {
            "machine_planner": {"command": str(executable)},
            "convergence": {"provider_timeout_seconds": 25},
        },
    )

    def fail_subprocess(*args: object, **kwargs: object) -> object:
        raise failure

    monkeypatch.setattr(readiness_pipeline.subprocess, "run", fail_subprocess)

    result = readiness_pipeline.run(
        output_root=tmp_path / "outputs",
        repo_root=tmp_path,
    )

    assert result["ok"] is False
    assert result["failure_code"] == expected_code
    stored = load_json(Path(result["path"]))[0]
    assert stored["failure_code"] == expected_code


def _verified_result(*, digest: str = "c" * 64) -> dict:
    return {
        "ok": True,
        "readiness_digest": digest,
        "source_sha": SHA,
        "source_tree_sha": TREE,
        "provider": {"executable_sha256": "d" * 64},
        "checked_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(hours=24)).isoformat(),
    }


@pytest.mark.parametrize(
    ("blocker", "failure_code", "expected_code"),
    [
        ("cloud_ai_provider_readiness_missing", "", PROVIDER_READINESS_UNAVAILABLE),
        ("cloud_ai_provider_readiness_stale", "", PROVIDER_READINESS_UNAVAILABLE),
        (
            "cloud_ai_provider_readiness_not_passing",
            "strategy_recommendation_provider_timeout",
            PROVIDER_READINESS_UNAVAILABLE,
        ),
        ("cloud_ai_provider_readiness_json_invalid", "", PROVIDER_READINESS_INVALID),
        ("cloud_ai_provider_readiness_shape_invalid", "", PROVIDER_READINESS_INVALID),
        ("cloud_ai_provider_readiness_schema_invalid", "", PROVIDER_READINESS_INVALID),
        ("cloud_ai_provider_readiness_time_invalid", "", PROVIDER_READINESS_INVALID),
        ("cloud_ai_provider_readiness_time_in_future", "", PROVIDER_READINESS_INVALID),
        ("cloud_ai_provider_source_sha_mismatch", "", PROVIDER_READINESS_INVALID),
        ("cloud_ai_provider_source_tree_sha_mismatch", "", PROVIDER_READINESS_INVALID),
        ("cloud_ai_provider_source_tree_dirty", "", PROVIDER_READINESS_INVALID),
        ("cloud_ai_provider_executable_changed", "", PROVIDER_READINESS_INVALID),
        ("cloud_ai_provider_response_contract_invalid", "", PROVIDER_READINESS_INVALID),
        ("cloud_ai_provider_side_effect_contract_invalid", "", PROVIDER_READINESS_INVALID),
        (
            "cloud_ai_provider_readiness_not_passing",
            "strategy_recommendation_provider_auth_not_ready",
            PROVIDER_READINESS_INVALID,
        ),
    ],
)
def test_readiness_gate_uses_closed_typed_classification(
    blocker: str,
    failure_code: str,
    expected_code: str,
) -> None:
    with pytest.raises(CloudAIProviderReadinessGateError) as raised:
        require_cloud_ai_provider_readiness(
            lambda: {
                "ok": False,
                "blocker": blocker,
                "failure_code": failure_code,
            }
        )

    assert raised.value.code == expected_code
    assert raised.value.evidence["readiness_blocker"] == blocker


def test_readiness_gate_unknown_result_fails_structural_closed() -> None:
    with pytest.raises(CloudAIProviderReadinessGateError) as raised:
        require_cloud_ai_provider_readiness(
            lambda: {"ok": False, "blocker": "brand_new_code"}
        )

    assert raised.value.code == "unknown_blocker"


def test_readiness_gate_binds_and_rechecks_exact_digest() -> None:
    proof = require_cloud_ai_provider_readiness(
        lambda: _verified_result()
    )

    assert validate_provider_readiness_proof(proof) == proof
    with pytest.raises(CloudAIProviderReadinessGateError) as raised:
        require_cloud_ai_provider_readiness(
            lambda: _verified_result(digest="e" * 64),
            expected_digest=proof["readiness_digest"],
        )
    assert raised.value.code == PROVIDER_READINESS_UNAVAILABLE
    assert raised.value.evidence["readiness_blocker"] == (
        "cloud_ai_provider_readiness_changed"
    )


def test_provider_readiness_rejects_malformed_json_before_trusting_fields(
    tmp_path: Path,
) -> None:
    path = (
        tmp_path
        / "outputs"
        / "cloud"
        / "provider"
        / "readiness_current.json"
    )
    path.parent.mkdir(parents=True)
    path.write_text("{not-json", encoding="utf-8")

    result = CloudAIProviderReadiness(
        tmp_path / "outputs",
        now=lambda: NOW,
        source_attestation=lambda: {},
    ).verify()

    assert result == {
        "ok": False,
        "artifact": str(path),
        "blocker": "cloud_ai_provider_readiness_json_invalid",
    }
    assert _auth_status_ready(
        subprocess.CompletedProcess(
            args=["codex", "login", "status"],
            returncode=0,
            stdout="",
            stderr="permission denied\n",
        )
    ) is False
