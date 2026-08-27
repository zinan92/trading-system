from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipelines import testnet_automation_proof as cli


ACCOUNT = "0x" + "11" * 20
RELEASE = "a" * 40


def _args(tmp_path: Path, *, action: str = "preflight") -> list[str]:
    return [
        "--action",
        action,
        "--account-address",
        ACCOUNT,
        "--runtime-id",
        "runtime-proof",
        "--release-sha",
        RELEASE,
        "--approval-id",
        "approval-proof",
        "--approved-by",
        "park",
        "--secret-file",
        str(tmp_path / "never-read-secret"),
        "--output-root",
        str(tmp_path / "outputs"),
    ]


def test_preflight_uses_opt_in_profile_without_execute_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cli.StandardBrokerExternalExecutionAdapter,
        "preflight_build_context",
        lambda _context: {
            "status": "PREFLIGHT_READY",
            "transport_profile": cli.PROTECTED_EXTERNAL_PROFILE,
            "capability_revision": cli.PROTECTED_CAPABILITY_REVISION,
            "secret_resolved": False,
        },
    )

    assert cli.main(_args(tmp_path)) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "PREFLIGHT_READY"
    assert result["secret_resolved"] is False
    assert result["transport_profile"] == cli.PROTECTED_EXTERNAL_PROFILE


def test_start_requires_exact_testnet_ack_before_building_broker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = {
        "schema_version": "strategy-plan-v1",
        "strategy_type": "dca",
        "strategy_plan_id": "proof-plan",
        "strategy_session_id": "proof-session",
        "strategy_revision_id": "proof-revision",
        "plan_digest": "sha256:" + "a" * 64,
        "instrument_id": "BTC-USD-PERP",
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    monkeypatch.setattr(cli, "_load_json", lambda _path: plan)

    argv = _args(tmp_path, action="start") + [
        "--strategy-plan",
        str(plan_path),
        "--market",
        str(tmp_path / "market.json"),
        "--confirmation",
        str(tmp_path / "confirmation.json"),
        "--execute-testnet",
    ]

    assert cli.main(argv) == 2

    result = json.loads(capsys.readouterr().out)
    assert result["reason_code"] == "exact_acknowledgement_required"


def test_preflight_rejects_unreviewed_protection_revision_before_building_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cli.StandardBrokerExternalExecutionAdapter,
        "preflight_build_context",
        lambda _context: pytest.fail("unreviewed capability must be rejected first"),
    )

    argv = _args(tmp_path) + ["--capability-revision", "unreviewed"]

    assert cli.main(argv) == 2

    result = json.loads(capsys.readouterr().out)
    assert result["reason_code"] == "protected_capability_revision_required"
