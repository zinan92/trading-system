"""Fail-closed validation for one complete natural Paper live-tick receipt."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from services.dualtrack_clock import parse_utc


MAX_HEARTBEAT_AGE_SECONDS = 180


def validate_complete_tick_heartbeat(
    row: Mapping[str, Any] | None,
    *,
    cycle_id: str,
    observed_at: str | datetime,
) -> dict[str, Any]:
    """Require the exact heartbeat written after all prerequisite phases."""

    checked_at = parse_utc(observed_at)
    heartbeat = dict(row) if isinstance(row, Mapping) else {}
    detail = (
        dict(heartbeat.get("detail") or {})
        if isinstance(heartbeat.get("detail"), Mapping)
        else {}
    )
    try:
        recorded_at = parse_utc(heartbeat.get("ts"))
    except (TypeError, ValueError):
        recorded_at = None
    age_seconds = (
        (checked_at - recorded_at).total_seconds()
        if recorded_at is not None
        else None
    )
    complete = (
        str(heartbeat.get("cycle_id") or "") == cycle_id
        and str(heartbeat.get("event") or "")
        == "live_tick_heartbeat"
        and detail.get("runner") == "dualtrack-live-tick"
        and detail.get("ledger_refreshed") is True
        and age_seconds is not None
        and 0 <= age_seconds <= MAX_HEARTBEAT_AGE_SECONDS
    )
    if complete:
        return {
            "status": "ready",
            "machine_code": None,
            "cycle_id": cycle_id,
            "recorded_at": recorded_at.isoformat(),
            "age_seconds": round(float(age_seconds), 3),
            "complete": True,
        }
    reason = "heartbeat_missing"
    if heartbeat:
        if recorded_at is None:
            reason = "heartbeat_timestamp_invalid"
        elif age_seconds is not None and age_seconds < 0:
            reason = "heartbeat_in_future"
        elif (
            age_seconds is not None
            and age_seconds > MAX_HEARTBEAT_AGE_SECONDS
        ):
            reason = "heartbeat_stale"
        elif str(heartbeat.get("cycle_id") or "") != cycle_id:
            reason = "heartbeat_cycle_mismatch"
        else:
            reason = "heartbeat_incomplete"
    return {
        "status": "blocked",
        "machine_code": "execution_tick_heartbeat_temporarily_missing",
        "cycle_id": cycle_id,
        "recorded_at": (
            recorded_at.isoformat() if recorded_at is not None else None
        ),
        "age_seconds": (
            round(float(age_seconds), 3)
            if age_seconds is not None
            else None
        ),
        "complete": False,
        "reason": reason,
    }
