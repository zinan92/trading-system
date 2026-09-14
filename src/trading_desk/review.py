"""Resolve a judgment once enough time has passed: compare the price then with the price N hours later."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

HIT_MOVE_PCT = 0.5     # a long/short call needs at least this move in its favour
FLAT_BAND_PCT = 2.0    # a "wait" call is right when price stayed inside this band


def outcome(direction: str, move_pct: float) -> str:
    if direction == "flat":
        return "hit" if abs(move_pct) < FLAT_BAND_PCT else "miss"
    signed = move_pct if direction == "long" else -move_pct
    if signed >= HIT_MOVE_PCT:
        return "hit"
    if signed <= -HIT_MOVE_PCT:
        return "miss"
    return "even"


def _at(ts: Any) -> datetime:
    at = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def price_at(bars: list[list[Any]], when: datetime, max_gap: timedelta = timedelta(hours=2)) -> float | None:
    """Close of the last bar that opened at or before `when`, only if that bar is close enough to count."""
    best = None
    for ts, _o, _h, _l, close in bars:
        at = _at(ts)
        if at <= when:
            best = (at, float(close))
    return best[1] if best and when - best[0] <= max_gap else None


def out_of_window(bars: list[list[Any]], when: datetime) -> bool:
    """True when the target is older than the oldest bar we can read: it can never be verified."""
    return bool(bars) and when < _at(bars[0][0])


def due(judgment: dict[str, Any], hours: int, now: datetime | None = None) -> datetime | None:
    if judgment.get("resolved_at") or judgment.get("price_at") in (None, 0):
        return None
    created = datetime.fromisoformat(str(judgment["created_at"]).replace("Z", "+00:00"))
    target = created + timedelta(hours=hours)
    return target if (now or datetime.now(timezone.utc)) >= target else None
