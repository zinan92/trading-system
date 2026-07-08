from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.journal_store import write_json
from pipelines import dashboard_server


def _now() -> datetime:
    return datetime(2026, 7, 8, 4, 0, tzinfo=timezone.utc)


def _write_latest(path: Path, payload: dict) -> None:
    write_json(path, [payload])


def _healthy_inputs(output: Path, *, as_of: datetime | None = None) -> None:
    now = as_of or _now()
    _write_latest(
        output / "strategies" / "gold_1m_macd" / "live_reconciliation" / "current.json",
        {
            "run_date": "2026-07-08",
            "system_state": "READY",
            "checked_at": (now - timedelta(minutes=3)).isoformat(),
        },
    )
    _write_latest(
        output / "schedules" / "status_current.json",
        {
            "status": "active",
            "checked_at": (now - timedelta(minutes=5)).isoformat(),
            "message": "all launchd jobs match",
            "mismatched_jobs": [],
            "required_count": 9,
            "loaded_count": 9,
        },
    )
    _write_latest(
        output / "schedules" / "current.json",
        {
            "generated_at": (now - timedelta(minutes=10)).isoformat(),
            "jobs": [{"label": "com.wendy.trading-orchestrator.runner"}],
        },
    )
    _write_latest(
        output / "daily_review_runs" / "current.json",
        {
            "status": "pass",
            "finished_at": (now - timedelta(minutes=4)).isoformat(),
        },
    )
    _write_latest(
        output / "data_source_preflight" / "current.json",
        {
            "status": "pass",
            "symbol": "GOLD",
            "timeframe": "1m",
            "latest_timestamp": (now - timedelta(minutes=5)).isoformat(),
            "latest_bar": {"timestamp": (now - timedelta(minutes=5)).isoformat()},
        },
    )


def test_system_state_blocks_on_naked_position_fixture(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    _healthy_inputs(output)
    _write_latest(
        output / "strategies" / "gold_1m_macd" / "live_reconciliation" / "current.json",
        {
            "run_date": "2026-07-08",
            "system_state": "BLOCKED_NAKED_POSITION_SUSPECTED",
            "reason_code": "BLOCKED_NAKED_POSITION_SUSPECTED",
            "checked_at": "2026-07-05T12:00:00+00:00",
            "escalation_action": "处理裸头寸",
        },
    )

    payload = dashboard_server.build_system_state_response(output_root=output, as_of=_now())

    assert payload["overall"] == "BLOCKED"
    naked = next(check for check in payload["checks"] if check["id"] == "naked_position")
    assert naked["status"] == "BLOCKED"
    assert "gold_1m_macd" in naked["reason"]
    assert naked["room"] == "ops"


def test_system_state_empty_artifacts_fail_closed_unknown(tmp_path: Path) -> None:
    payload = dashboard_server.build_system_state_response(output_root=tmp_path / "outputs", as_of=_now())

    assert payload["overall"] == "UNKNOWN"
    statuses = {check["id"]: check["status"] for check in payload["checks"] if check["id"] != "dualtrack_heartbeat"}
    assert statuses["naked_position"] == "UNKNOWN"
    assert statuses["schedule"] == "UNKNOWN"
    assert statuses["daily_review"] == "UNKNOWN"
    assert statuses["data_freshness"] == "UNKNOWN"


def test_system_state_schedule_stale_degrades_when_other_inputs_are_healthy(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    _healthy_inputs(output)
    _write_latest(
        output / "schedules" / "status_current.json",
        {
            "status": "stale_installed",
            "checked_at": (_now() - timedelta(minutes=2)).isoformat(),
            "message": "installed launchd plists differ",
            "mismatched_jobs": ["com.wendy.trading-orchestrator.dualtrack-cycle"],
        },
    )

    payload = dashboard_server.build_system_state_response(output_root=output, as_of=_now())

    assert payload["overall"] == "DEGRADED"
    schedule = next(check for check in payload["checks"] if check["id"] == "schedule")
    assert schedule["status"] == "DEGRADED"
    assert "stale_installed" in schedule["reason"]


def test_system_state_all_healthy_inputs_run(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    _healthy_inputs(output)

    payload = dashboard_server.build_system_state_response(output_root=output, as_of=_now())

    assert payload["overall"] == "RUN"
    assert {check["status"] for check in payload["checks"]} == {"RUN"}


def test_system_state_damaged_json_returns_unknown_payload(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    _healthy_inputs(output)
    broken = output / "strategies" / "gold_1m_macd" / "live_reconciliation" / "current.json"
    broken.write_text("{not-json", encoding="utf-8")

    payload = dashboard_server.build_system_state_response(output_root=output, as_of=_now())

    assert payload["overall"] == "UNKNOWN"
    naked = next(check for check in payload["checks"] if check["id"] == "naked_position")
    assert naked["status"] == "UNKNOWN"


def test_system_state_priority_unknown_beats_degraded_but_blocked_beats_unknown(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    _healthy_inputs(output)
    _write_latest(
        output / "schedules" / "status_current.json",
        {"status": "stale_installed", "checked_at": (_now() - timedelta(minutes=2)).isoformat()},
    )
    (output / "daily_review_runs" / "current.json").unlink()

    unknown_payload = dashboard_server.build_system_state_response(output_root=output, as_of=_now())
    assert unknown_payload["overall"] == "UNKNOWN"

    _write_latest(
        output / "strategies" / "gold_1m_macd" / "live_reconciliation" / "current.json",
        {"system_state": "BLOCKED_NAKED_POSITION_SUSPECTED", "checked_at": "2026-07-05T12:00:00+00:00"},
    )
    blocked_payload = dashboard_server.build_system_state_response(output_root=output, as_of=_now())
    assert blocked_payload["overall"] == "BLOCKED"
