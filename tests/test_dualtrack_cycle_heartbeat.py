from __future__ import annotations

from pathlib import Path

from services.completion_audit import CompletionAudit
from services.dualtrack_cycle_heartbeat import DualTrackCycleHeartbeat
from services.journal_store import write_json


DUALTRACK_LABEL = "com.wendy.trading-orchestrator.dualtrack-cycle"


def _schedule(root: Path, *, include_dualtrack_cycle: bool = True) -> None:
    jobs = [{"label": "com.wendy.trading-orchestrator.runner"}]
    if include_dualtrack_cycle:
        jobs.append({"label": DUALTRACK_LABEL})
    write_json(root / "schedules" / "current.json", [{"status": "generated", "jobs": jobs}])


def _closed_cycle(root: Path, cycle_id: str) -> None:
    date_part = cycle_id.rsplit("_", 1)[0]
    write_json(root / "dualtrack" / "ledger" / "daily" / f"{date_part}.json", [{
        "date": date_part,
        "cycles": {cycle_id: {"machine": 0, "human": 0}},
    }])
    write_json(root / "dualtrack" / "attribution" / f"{cycle_id}.json", [{
        "cycle_id": cycle_id,
        "status": "closed",
    }])


def test_heartbeat_is_fresh_when_recent_required_boundary_has_artifacts(tmp_path: Path) -> None:
    root = tmp_path / "outputs"
    _schedule(root)
    _closed_cycle(root, "2026-07-06_DAY")

    result = DualTrackCycleHeartbeat(root).run(as_of="2026-07-06T16:00:00+00:00")
    audit = CompletionAudit(root, tmp_path / "market.db")._dualtrack_cycle_liveness(
        "2026-07-06",
        as_of="2026-07-06T16:00:00+00:00",
    )

    assert result["status"] == "fresh"
    assert result["expected_boundary"] == "2026-07-06T13:00:00+00:00"
    assert result["latest_artifact_at"] == "2026-07-06T13:00:00+00:00"
    assert result["missed_boundaries"] == []
    assert audit["status"] == "pass"


def test_heartbeat_is_stale_when_recent_boundary_is_missing_after_grace(tmp_path: Path) -> None:
    root = tmp_path / "outputs"
    _schedule(root)
    _closed_cycle(root, "2026-07-05_NIGHT")

    result = DualTrackCycleHeartbeat(root).run(as_of="2026-07-06T16:00:00+00:00")
    audit = CompletionAudit(root, tmp_path / "market.db")._dualtrack_cycle_liveness(
        "2026-07-06",
        as_of="2026-07-06T16:00:00+00:00",
    )

    assert result["status"] == "stale"
    assert result["expected_boundary"] == "2026-07-06T13:00:00+00:00"
    assert result["latest_artifact_at"] == "2026-07-06T01:00:00+00:00"
    assert result["missed_boundaries"] == ["2026-07-06T13:00:00+00:00"]
    assert audit["status"] == "fail"
    assert audit["evidence"]["missed_boundaries"]


def test_heartbeat_does_not_mark_boundary_stale_inside_grace_period(tmp_path: Path) -> None:
    root = tmp_path / "outputs"
    _schedule(root)
    _closed_cycle(root, "2026-07-05_NIGHT")

    result = DualTrackCycleHeartbeat(root).run(as_of="2026-07-06T13:30:00+00:00")

    assert result["status"] == "fresh"
    assert result["expected_boundary"] == "2026-07-06T01:00:00+00:00"
    assert result["missed_boundaries"] == []
    assert result["reason"] == "latest_required_boundary_is_covered"


def test_heartbeat_not_scheduled_when_dualtrack_cycle_job_is_not_generated(tmp_path: Path) -> None:
    root = tmp_path / "outputs"
    _schedule(root, include_dualtrack_cycle=False)

    result = DualTrackCycleHeartbeat(root).run(as_of="2026-07-06T16:00:00+00:00")
    audit = CompletionAudit(root, tmp_path / "market.db")._dualtrack_cycle_liveness(
        "2026-07-06",
        as_of="2026-07-06T16:00:00+00:00",
    )

    assert result["status"] == "not_scheduled"
    assert result["reason"] == "dualtrack_cycle_job_not_generated"
    assert audit["status"] == "pass"


def test_heartbeat_fails_closed_when_artifacts_are_missing_or_malformed(tmp_path: Path) -> None:
    root = tmp_path / "outputs"
    _schedule(root)
    missing = DualTrackCycleHeartbeat(root).run(as_of="2026-07-06T16:00:00+00:00")

    assert missing["status"] == "stale"
    assert missing["reason"] == "artifact_dir_missing"

    bad_root = tmp_path / "bad_outputs"
    _schedule(bad_root)
    attr_dir = bad_root / "dualtrack" / "attribution"
    attr_dir.mkdir(parents=True)
    (attr_dir / "not-a-cycle.json").write_text("[{}]\n", encoding="utf-8")
    bad = DualTrackCycleHeartbeat(bad_root).run(as_of="2026-07-06T16:00:00+00:00")

    assert bad["status"] == "stale"
    assert bad["reason"] == "artifact_timestamp_parse_error"
    assert bad["missed_boundaries"]


def test_heartbeat_derives_boundaries_from_config(tmp_path: Path) -> None:
    root = tmp_path / "outputs"
    _schedule(root)
    config = {
        "cycle_hours_utc": {"day_start": 2, "night_start": 14},
        "plan_lock_deadline_min_before_cycle": 0,
    }

    result = DualTrackCycleHeartbeat(root, config=config).run(as_of="2026-07-06T17:00:00+00:00")

    assert result["status"] == "stale"
    assert result["expected_boundary"] == "2026-07-06T14:00:00+00:00"
    assert result["missed_boundaries"] == ["2026-07-06T14:00:00+00:00"]
