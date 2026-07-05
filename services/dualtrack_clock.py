from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

BJ_TZ = timezone(timedelta(hours=8))


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
