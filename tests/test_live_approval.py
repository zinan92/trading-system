from pathlib import Path

import pytest

from services.journal_store import load_json, write_json
from services.live_approval import LiveApprovalStore


def test_live_approval_request_writes_json_and_markdown(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    write_json(root / "live_activation" / f"{run_date}.json", [{"status": "blocked", "dry_run_ready": False, "real_money_ready": False, "real_money_checks": [{"name": "official_5m_data", "status": "fail"}]}])

    status = LiveApprovalStore(root).request(run_date, "review later")

    assert status["status"] == "requested"
    assert status["approved"] is False
    request = load_json(root / "live_approvals" / f"{run_date}.request.json")[0]
    assert request["failed_checks"] == ["official_5m_data"]
    assert (root / "live_approvals" / f"{run_date}.request.md").exists()


def test_live_approval_blocks_approval_until_dry_run_ready(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    write_json(root / "live_activation" / f"{run_date}.json", [{"status": "blocked", "dry_run_ready": False}])

    with pytest.raises(ValueError, match="dry-run gate is not ready"):
        LiveApprovalStore(root).approve(run_date, "wendy")


def test_live_approval_can_approve_and_revoke_when_ready(tmp_path: Path):
    root = tmp_path / "outputs"
    run_date = "2026-05-26"
    write_json(root / "live_activation" / f"{run_date}.json", [{"status": "dry_run_ready", "dry_run_ready": True, "real_money_ready": False}])

    approved = LiveApprovalStore(root).approve(run_date, "wendy", "reviewed")

    assert approved["status"] == "approved"
    assert approved["approved"] is True
    approval = load_json(root / "live_approvals" / f"{run_date}.approved.json")[0]
    assert approval["approver"] == "wendy"

    revoked = LiveApprovalStore(root).revoke(run_date, "stop")

    assert revoked["status"] == "revoked"
    assert revoked["approved"] is False
    assert not (root / "live_approvals" / f"{run_date}.approved.json").exists()
