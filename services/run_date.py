from __future__ import annotations

from datetime import datetime, timezone


def utc_run_date(now: datetime | None = None) -> str:
    """Return the canonical UTC trading date for run artifacts."""

    current = now if now is not None else datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).date().isoformat()
