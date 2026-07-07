from __future__ import annotations

from services.dualtrack_clock import (
    comex_futures_session_status,
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
