from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.config_loader import ROOT
from services.journal_store import write_json
from services.paper_release_receipt import PaperReleaseReceiptGate


SOURCE_SHA = "a" * 40


def _receipt(root: Path, *, checked_at: datetime, source_sha: str = SOURCE_SHA, status: str = "pass"):
    write_json(
        root / "release_gates" / "paper_predeploy_current.json",
        [{
            "schema_version": "paper-predeploy-gate-v2",
            "checked_at": checked_at.isoformat(),
            "status": status,
            "source_sha": source_sha,
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


def test_legacy_start_command_runs_gate_before_any_listener_or_runner_change():
    text = (ROOT / "start.command").read_text(encoding="utf-8")
    gate_index = text.index("python3 -m pipelines.paper_predeploy_gate --json")

    assert gate_index < text.index("lsof -ti")
    assert gate_index < text.index("kill \"$EXISTING_PID\"")
    assert gate_index < text.index("python3 -m pipelines.runner")
