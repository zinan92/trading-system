from __future__ import annotations

import time
from datetime import datetime, timezone

from services.run_date import utc_run_date


def test_utc_run_date_ignores_utc_plus_8_local_calendar_day(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    if hasattr(time, "tzset"):
        time.tzset()
    try:
        # 2026-06-09 16:30 UTC is already 2026-06-10 in UTC+8.
        now = datetime(2026, 6, 9, 16, 30, tzinfo=timezone.utc)
        assert utc_run_date(now) == "2026-06-09"
    finally:
        monkeypatch.delenv("TZ", raising=False)
        if hasattr(time, "tzset"):
            time.tzset()
