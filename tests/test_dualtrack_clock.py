from __future__ import annotations

from services.dualtrack_clock import (
    comex_futures_session_status,
    cycle_window,
    cycle_window_from_id,
    is_comex_futures_open,
    next_comex_futures_open,
)


def test_comex_futures_session_mask_handles_dst_weekly_open_and_daily_break() -> None:
    assert is_comex_futures_open("2026-07-05T21:59:59+00:00") is False
    assert is_comex_futures_open("2026-07-05T22:00:00+00:00") is True
    assert is_comex_futures_open("2026-07-06T20:59:59+00:00") is True
    assert is_comex_futures_open("2026-07-06T21:00:00+00:00") is False
    assert is_comex_futures_open("2026-07-06T22:00:00+00:00") is True
    assert is_comex_futures_open("2026-07-11T12:00:00+00:00") is False


def test_comex_futures_session_mask_handles_standard_time_open() -> None:
    assert is_comex_futures_open("2026-01-04T22:59:59+00:00") is False
    assert is_comex_futures_open("2026-01-04T23:00:00+00:00") is True
    assert next_comex_futures_open("2026-01-04T22:00:00+00:00").isoformat() == "2026-01-04T23:00:00+00:00"


def test_comex_futures_session_status_reports_next_open_and_reason() -> None:
    daily_break = comex_futures_session_status("2026-07-06T21:30:00+00:00")
    weekend = comex_futures_session_status("2026-07-11T12:00:00+00:00")

    assert daily_break["is_open"] is False
    assert daily_break["reason"] == "daily_break"
    assert daily_break["next_open"] == "2026-07-06T22:00:00+00:00"
    assert weekend["is_open"] is False
    assert weekend["reason"] == "weekend_closed"
    assert weekend["next_open"] == "2026-07-12T22:00:00+00:00"


def test_dualtrack_uses_one_transition_window_then_restores_twelve_hour_cycles() -> None:
    legacy_night = cycle_window("2026-07-13T15:59:59+00:00")
    transition = cycle_window("2026-07-13T16:00:00+00:00")
    same_transition = cycle_window("2026-07-14T08:30:00+00:00")
    restored_night = cycle_window("2026-07-14T13:00:00+00:00")
    restored_day = cycle_window("2026-07-15T01:00:00+00:00")

    assert legacy_night.cycle_id == "2026-07-13_NIGHT"
    assert legacy_night.start.isoformat() == "2026-07-13T13:00:00+00:00"
    assert legacy_night.end.isoformat() == "2026-07-13T16:00:00+00:00"
    assert transition.cycle_id == "2026-07-14_DAY"
    assert transition.start.isoformat() == "2026-07-13T16:00:00+00:00"
    assert transition.end.isoformat() == "2026-07-14T13:00:00+00:00"
    assert same_transition == transition
    assert transition.to_dict()["duration_hours"] == 21
    assert restored_night.cycle_id == "2026-07-14_NIGHT"
    assert restored_night.start.isoformat() == "2026-07-14T13:00:00+00:00"
    assert restored_night.end.isoformat() == "2026-07-15T01:00:00+00:00"
    assert restored_night.to_dict()["duration_hours"] == 12
    assert restored_day.cycle_id == "2026-07-15_DAY"
    assert restored_day.start.isoformat() == "2026-07-15T01:00:00+00:00"
    assert restored_day.end.isoformat() == "2026-07-15T13:00:00+00:00"


def test_cycle_id_parser_preserves_legacy_history_and_transition_window() -> None:
    legacy = cycle_window_from_id("2026-07-13_DAY")
    transition = cycle_window_from_id("2026-07-14_DAY")
    restored_night = cycle_window_from_id("2026-07-14_NIGHT")
    restored_day = cycle_window_from_id("2026-07-15_DAY")

    assert legacy.start.isoformat() == "2026-07-13T01:00:00+00:00"
    assert legacy.end.isoformat() == "2026-07-13T13:00:00+00:00"
    assert transition.start.isoformat() == "2026-07-13T16:00:00+00:00"
    assert transition.end.isoformat() == "2026-07-14T13:00:00+00:00"
    assert restored_night.start.isoformat() == "2026-07-14T13:00:00+00:00"
    assert restored_night.end.isoformat() == "2026-07-15T01:00:00+00:00"
    assert restored_day.start.isoformat() == "2026-07-15T01:00:00+00:00"
    assert restored_day.end.isoformat() == "2026-07-15T13:00:00+00:00"
