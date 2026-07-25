from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess

from services.config_loader import ROOT
from services.journal_store import write_json
from services.paper_release_receipt import (
    PaperReleaseReceiptGate,
    PaperServiceBootGate,
    current_source_attestation,
)


SOURCE_SHA = "a" * 40


def _receipt(root: Path, *, checked_at: datetime, source_sha: str = SOURCE_SHA, status: str = "pass"):
    write_json(
        root / "release_gates" / "paper_predeploy_current.json",
        [{
            "schema_version": "paper-predeploy-gate-v3",
            "checked_at": checked_at.isoformat(),
            "status": status,
            "source_sha": source_sha,
            "source_tree_sha": source_sha,
            "tracked_tree_clean": True,
            "compatibility_receipt": {"status": "pass"},
        }],
    )


def test_paper_release_receipt_passes_only_when_fresh_and_source_bound(tmp_path: Path):
    now = datetime(2026, 7, 25, 5, 0, tzinfo=timezone.utc)
    _receipt(tmp_path, checked_at=now - timedelta(seconds=30))

    result = PaperReleaseReceiptGate(
        tmp_path,
        now=lambda: now,
        source_sha_resolver=lambda: SOURCE_SHA,
    ).verify()

    assert result["ok"] is True
    assert result["source_sha"] == SOURCE_SHA
    assert result["age_seconds"] == 30


def test_paper_release_receipt_rejects_missing_stale_and_source_mismatch(tmp_path: Path):
    now = datetime(2026, 7, 25, 5, 0, tzinfo=timezone.utc)
    gate = PaperReleaseReceiptGate(
        tmp_path,
        now=lambda: now,
        source_sha_resolver=lambda: SOURCE_SHA,
        max_age_seconds=900,
    )

    assert gate.verify()["blocker"] == "missing_paper_predeploy_receipt"

    _receipt(tmp_path, checked_at=now - timedelta(seconds=901))
    assert gate.verify()["blocker"] == "paper_predeploy_receipt_stale"

    _receipt(tmp_path, checked_at=now, source_sha="b" * 40)
    mismatch = gate.verify()
    assert mismatch["blocker"] == "paper_predeploy_source_sha_mismatch"
    assert mismatch["receipt_source_sha"] == "b" * 40
    assert mismatch["current_source_sha"] == SOURCE_SHA


def test_paper_release_receipt_blocks_malformed_and_unresolvable_source(tmp_path: Path):
    now = datetime(2026, 7, 25, 5, 0, tzinfo=timezone.utc)
    _receipt(tmp_path, checked_at=now)
    failing = PaperReleaseReceiptGate(
        tmp_path,
        now=lambda: now,
        source_sha_resolver=lambda: (_ for _ in ()).throw(RuntimeError("git unavailable")),
    ).verify()
    assert failing["blocker"] == "current_source_sha_unavailable"

    write_json(
        tmp_path / "release_gates" / "paper_predeploy_current.json",
        [{
            "status": "pass",
            "checked_at": "not-a-time",
            "source_sha": SOURCE_SHA,
            "compatibility_receipt": {"status": "pass"},
        }],
    )
    invalid_time = PaperReleaseReceiptGate(
        tmp_path,
        now=lambda: now,
        source_sha_resolver=lambda: SOURCE_SHA,
    ).verify()
    assert invalid_time["blocker"] == "paper_predeploy_receipt_time_invalid"


def test_service_boot_accepts_stale_same_release_and_writes_receipt(tmp_path: Path):
    now = datetime(2026, 7, 25, 5, 0, tzinfo=timezone.utc)
    _receipt(tmp_path, checked_at=now - timedelta(days=1))
    gate = PaperServiceBootGate(
        tmp_path,
        now=lambda: now,
        source_sha_resolver=lambda: SOURCE_SHA,
        max_age_seconds=900,
    )

    result = gate.verify("dashboard")

    assert result["ok"] is True
    boot = (
        tmp_path / "release_gates" / "paper_service_boot_dashboard_current.json"
    ).read_text()
    assert '"status": "pass"' in boot
    assert PaperReleaseReceiptGate(
        tmp_path,
        now=lambda: now,
        source_sha_resolver=lambda: SOURCE_SHA,
        max_age_seconds=900,
    ).verify()["blocker"] == "paper_predeploy_receipt_stale"


def test_service_boot_blocks_dirty_or_mismatched_source_and_writes_receipt(tmp_path: Path):
    now = datetime(2026, 7, 25, 5, 0, tzinfo=timezone.utc)
    _receipt(tmp_path, checked_at=now)
    dirty = PaperServiceBootGate(
        tmp_path,
        now=lambda: now,
        source_attestation_resolver=lambda: {
            "source_sha": SOURCE_SHA,
            "source_tree_sha": SOURCE_SHA,
            "tracked_tree_clean": False,
        },
    ).verify("dualtrack-live-tick")
    assert dirty["blocker"] == "current_tracked_source_tree_dirty"
    assert dirty["boot_receipt"]["status"] == "blocked"

    mismatch = PaperServiceBootGate(
        tmp_path,
        now=lambda: now,
        source_attestation_resolver=lambda: {
            "source_sha": "b" * 40,
            "source_tree_sha": "b" * 40,
            "tracked_tree_clean": True,
        },
    ).verify("dashboard")
    assert mismatch["blocker"] == "paper_predeploy_source_sha_mismatch"


def test_source_attestation_detects_a_tracked_worktree_edit(tmp_path: Path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    tracked = tmp_path / "tracked.py"
    tracked.write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=tmp_path, check=True)

    clean = current_source_attestation(tmp_path)
    tracked.write_text("value = 2\n", encoding="utf-8")
    dirty = current_source_attestation(tmp_path)

    assert clean["tracked_tree_clean"] is True
    assert dirty["tracked_tree_clean"] is False
    assert dirty["source_sha"] == clean["source_sha"]
    assert dirty["source_tree_sha"] == clean["source_tree_sha"]


def test_legacy_start_command_runs_gate_before_any_listener_or_runner_change():
    text = (ROOT / "start.command").read_text(encoding="utf-8")
    gate_index = text.index("python3 -m pipelines.paper_predeploy_gate --json")

    assert gate_index < text.index("lsof -ti")
    assert gate_index < text.index("kill \"$EXISTING_PID\"")
    assert gate_index < text.index("python3 -m pipelines.runner")
