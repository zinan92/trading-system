from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

BJ_TZ = timezone(timedelta(hours=8))
NY_TZ = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class CycleWindow:
    cycle_id: str
    kind: str
    start: datetime
    end: datetime
    lock_deadline: datetime

    def to_dict(self) -> dict:
        return {
            "cycle_id": self.cycle_id,
            "kind": self.kind,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "lock_deadline": self.lock_deadline.isoformat(),
            "start_cst": self.start.astimezone(BJ_TZ).isoformat(),
            "end_cst": self.end.astimezone(BJ_TZ).isoformat(),
        }


def parse_utc(value: str | datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc).replace(microsecond=0)
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def cycle_window(ts: str | datetime | None = None, *, lock_deadline_min_before_cycle: int = 0) -> CycleWindow:
    utc = parse_utc(ts)
    if 1 <= utc.hour < 13:
        start = utc.replace(hour=1, minute=0, second=0, microsecond=0)
        kind = "DAY"
    elif utc.hour >= 13:
        start = utc.replace(hour=13, minute=0, second=0, microsecond=0)
        kind = "NIGHT"
    else:
        previous = utc - timedelta(days=1)
        start = previous.replace(hour=13, minute=0, second=0, microsecond=0)
        kind = "NIGHT"
    end = start + timedelta(hours=12)
    cycle_date = start.astimezone(BJ_TZ).strftime("%Y-%m-%d")
    return CycleWindow(
        cycle_id=f"{cycle_date}_{kind}",
        kind=kind,
        start=start,
        end=end,
        lock_deadline=start - timedelta(minutes=lock_deadline_min_before_cycle),
    )


def market_session_enabled(config: dict[str, Any]) -> bool:
    session = config.get("market_session")
    return isinstance(session, dict) and bool(session.get("enabled"))


def market_session_status(ts: str | datetime | None = None, *, config: dict[str, Any] | None = None) -> dict[str, Any]:
    session = (config or {}).get("market_session") if isinstance((config or {}).get("market_session"), dict) else {}
    venue = str(session.get("venue") or session.get("name") or "").lower()
    if venue not in {"comex", "comex_futures", "cme_globex_comex"}:
        checked_at = parse_utc(ts)
        return {
            "enabled": bool(session.get("enabled", False)),
            "venue": venue or "none",
            "is_open": True,
            "reason": "session_mask_not_configured",
            "checked_at": checked_at.isoformat(),
            "next_open": None,
        }
    return comex_futures_session_status(ts)


def comex_futures_session_status(ts: str | datetime | None = None) -> dict[str, Any]:
    checked_at = parse_utc(ts)
    is_open = is_comex_futures_open(checked_at)
    next_open = None if is_open else next_comex_futures_open(checked_at)
    return {
        "enabled": True,
        "venue": "comex_futures",
        "timezone": "America/New_York",
        "is_open": is_open,
        "reason": "open" if is_open else _comex_closed_reason(checked_at),
        "checked_at": checked_at.isoformat(),
        "next_open": None if next_open is None else next_open.isoformat(),
    }


def is_comex_futures_open(ts: str | datetime | None = None) -> bool:
    ny = parse_utc(ts).astimezone(NY_TZ)
    weekday = ny.weekday()
    local_time = ny.time()
    if weekday == 5:
        return False
    if weekday == 6:
        return local_time >= time(18, 0)
    if weekday == 4:
        return local_time < time(17, 0)
    if time(17, 0) <= local_time < time(18, 0):
        return False
    return True


def next_comex_futures_open(ts: str | datetime | None = None) -> datetime:
    current = parse_utc(ts)
    if is_comex_futures_open(current):
        return current
    ny = current.astimezone(NY_TZ)
    weekday = ny.weekday()
    local_time = ny.time()
    if weekday == 6 and local_time < time(18, 0):
        candidate = ny.replace(hour=18, minute=0, second=0, microsecond=0)
    elif weekday in {0, 1, 2, 3} and time(17, 0) <= local_time < time(18, 0):
        candidate = ny.replace(hour=18, minute=0, second=0, microsecond=0)
    else:
        days_until_sunday = (6 - weekday) % 7
        if days_until_sunday == 0:
            days_until_sunday = 7
        candidate_date = (ny + timedelta(days=days_until_sunday)).date()
        candidate = datetime.combine(candidate_date, time(18, 0), tzinfo=NY_TZ)
    return candidate.astimezone(timezone.utc).replace(microsecond=0)


def filter_bars_for_market_session(bars: list[Any], config: dict[str, Any]) -> list[Any]:
    if not market_session_enabled(config):
        return bars
    session = config.get("market_session") if isinstance(config.get("market_session"), dict) else {}
    venue = str(session.get("venue") or session.get("name") or "").lower()
    if venue not in {"comex", "comex_futures", "cme_globex_comex"}:
        return bars
    return [bar for bar in bars if is_comex_futures_open(getattr(bar, "timestamp", None))]


def cycle_window_from_id(cycle_id: str, *, lock_deadline_min_before_cycle: int = 0) -> CycleWindow:
    try:
        date_part, kind = cycle_id.rsplit("_", 1)
    except ValueError as exc:
        raise ValueError("cycle_id must look like YYYY-MM-DD_DAY or YYYY-MM-DD_NIGHT") from exc
    if kind not in {"DAY", "NIGHT"}:
        raise ValueError("cycle kind must be DAY or NIGHT")
    hour = 9 if kind == "DAY" else 21
    bj_start = datetime.fromisoformat(f"{date_part}T{hour:02d}:00:00+08:00")
    start = bj_start.astimezone(timezone.utc)
    end = start + timedelta(hours=12)
    return CycleWindow(
        cycle_id=cycle_id,
        kind=kind,
        start=start,
        end=end,
        lock_deadline=start - timedelta(minutes=lock_deadline_min_before_cycle),
    )


def cycle_key(ts: str | datetime) -> tuple[str, str]:
    window = cycle_window(ts)
    date_part, kind = window.cycle_id.rsplit("_", 1)
    return date_part, kind


def seconds_until_end(ts: str | datetime | None = None, *, lock_deadline_min_before_cycle: int = 0) -> int:
    now = parse_utc(ts)
    window = cycle_window(now, lock_deadline_min_before_cycle=lock_deadline_min_before_cycle)
    return max(0, int((window.end - now).total_seconds()))


def _comex_closed_reason(ts: datetime) -> str:
    ny = parse_utc(ts).astimezone(NY_TZ)
    weekday = ny.weekday()
    local_time = ny.time()
    if weekday == 5 or (weekday == 4 and local_time >= time(17, 0)) or (weekday == 6 and local_time < time(18, 0)):
        return "weekend_closed"
    if time(17, 0) <= local_time < time(18, 0):
        return "daily_break"
    return "closed"
