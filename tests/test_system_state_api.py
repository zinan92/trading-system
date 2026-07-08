from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.journal_store import write_json
from pipelines import dashboard_server


def _now() -> datetime:
    return datetime(2026, 7, 8, 4, 0, tzinfo=timezone.utc)


def _write_latest(path: Path, payload: dict) -> None:
    write_json(path, [payload])


def _active_demo_config() -> dict:
    return {
        "output_root": "outputs",
        "schedule": {"profile": "full"},
        "demo_trading": {"enabled": True, "active_strategy_id": "gold_1m_macd"},
    }


def _healthy_inputs(output: Path, *, as_of: datetime | None = None, schedule_profile: str = "full") -> None:
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
            "profile": schedule_profile,
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
            "checked_at": (_now() - timedelta(minutes=5)).isoformat(),
            "escalation_action": "处理裸头寸",
        },
    )

    payload = dashboard_server.build_system_state_response(output_root=output, as_of=_now())

    assert payload["overall"] == "BLOCKED"
    naked = next(check for check in payload["checks"] if check["id"] == "naked_position")
    assert naked["status"] == "BLOCKED"
    assert "gold_1m_macd" in naked["reason"]
    assert naked["room"] == "ops"


def test_system_state_stale_active_demo_naked_position_is_unknown(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("services.system_state.load_pipeline_config", _active_demo_config)
    output = tmp_path / "outputs"
    _healthy_inputs(output)
    _write_latest(
        output / "strategies" / "gold_1m_macd" / "live_reconciliation" / "current.json",
        {
            "run_date": "2026-07-08",
            "system_state": "BLOCKED_NAKED_POSITION_SUSPECTED",
            "reason_code": "naked_position_suspected",
            "checked_at": (_now() - timedelta(hours=2)).isoformat(),
        },
    )

    payload = dashboard_server.build_system_state_response(output_root=output, as_of=_now())

    naked = next(check for check in payload["checks"] if check["id"] == "naked_position")
    assert naked["status"] == "UNKNOWN"
    assert "stale" in naked["reason"]


def test_system_state_stale_inactive_demo_reconciliation_is_skipped(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    _healthy_inputs(output)
    _write_latest(
        output / "strategies" / "gold_1m_macd" / "live_reconciliation" / "current.json",
        {
            "run_date": "2026-07-08",
            "system_state": "BLOCKED_RECONCILIATION_UNKNOWN",
            "reason_code": "venue_state_unknown",
            "checked_at": (_now() - timedelta(hours=2)).isoformat(),
        },
    )

    payload = dashboard_server.build_system_state_response(output_root=output, as_of=_now())

    naked = next(check for check in payload["checks"] if check["id"] == "naked_position")
    assert naked["status"] == "RUN"
    assert naked["skipped"] is True
    assert "demo reconciliation not scheduled" in naked["reason"]


def test_system_state_missing_naked_position_checked_at_is_unknown(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("services.system_state.load_pipeline_config", _active_demo_config)
    output = tmp_path / "outputs"
    _healthy_inputs(output)
    _write_latest(
        output / "strategies" / "gold_1m_macd" / "live_reconciliation" / "current.json",
        {
            "run_date": "2026-07-08",
            "system_state": "BLOCKED_RECONCILIATION_UNKNOWN",
            "reason_code": "venue_state_unknown",
        },
    )

    payload = dashboard_server.build_system_state_response(output_root=output, as_of=_now())

    naked = next(check for check in payload["checks"] if check["id"] == "naked_position")
    assert naked["status"] == "UNKNOWN"
    assert "timestamp_missing" in naked["reason"]


def test_system_state_empty_artifacts_fail_closed_unknown(tmp_path: Path) -> None:
    payload = dashboard_server.build_system_state_response(output_root=tmp_path / "outputs", as_of=_now())

    assert payload["overall"] == "UNKNOWN"
    statuses = {check["id"]: check["status"] for check in payload["checks"] if check["id"] != "dualtrack_heartbeat"}
    assert statuses["naked_position"] == "UNKNOWN"
    assert statuses["schedule"] == "UNKNOWN"
    assert "daily_review" not in statuses
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


def test_system_state_uses_fresh_local_market_db_when_preflight_artifact_is_stale(monkeypatch, tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    db = tmp_path / "market.db"
    conn = sqlite3.connect(db)
    conn.execute("create table bars(symbol text, timeframe text, timestamp text)")
    conn.execute("insert into bars values(?,?,?)", ("GOLD", "1m", (_now() - timedelta(minutes=2)).isoformat()))
    conn.commit()
    conn.close()
    _healthy_inputs(output)
    _write_latest(
        output / "data_source_preflight" / "current.json",
        {
            "status": "pass",
            "symbol": "GOLD",
            "timeframe": "1m",
            "latest_timestamp": (_now() - timedelta(hours=2)).isoformat(),
        },
    )
    monkeypatch.setattr("services.system_state.load_pipeline_config", lambda: {"local_market_db": str(db), "schedule": {"profile": "full"}})

    payload = dashboard_server.build_system_state_response(output_root=output, as_of=_now())

    data = next(check for check in payload["checks"] if check["id"] == "data_freshness")
    assert data["status"] == "RUN"
    assert data["evidence"]["source"] == "local_market_db"


def test_system_state_focus_profile_omits_daily_review_check(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    _healthy_inputs(output, schedule_profile="dualtrack_focus")
    (output / "daily_review_runs" / "current.json").unlink()

    payload = dashboard_server.build_system_state_response(output_root=output, as_of=_now())

    ids = {check["id"] for check in payload["checks"]}
    assert payload["overall"] == "RUN"
    assert "daily_review" not in ids
    assert "dualtrack_heartbeat" in ids


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
        {"system_state": "BLOCKED_NAKED_POSITION_SUSPECTED", "checked_at": (_now() - timedelta(minutes=5)).isoformat()},
    )
    blocked_payload = dashboard_server.build_system_state_response(output_root=output, as_of=_now())
    assert blocked_payload["overall"] == "BLOCKED"
