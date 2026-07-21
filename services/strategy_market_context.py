"""Trusted, completed multi-timeframe inputs for strategy grid planning."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from services.dualtrack_clock import parse_utc
from services.dualtrack_market_feed import DualTrackMarketFeed


def build_strategy_timeframes(
    *,
    as_of: str | None = None,
    market_db: Path | None = None,
    config: dict | None = None,
    timeframes: tuple[str, ...] = ("1d", "4h", "1h", "15m"),
    feed_cls=DualTrackMarketFeed,
) -> dict[str, dict[str, Any]]:
    checked_at = parse_utc(as_of)
    feed = feed_cls(market_db=market_db, config=config)
    specs = {"1d": 32, "4h": 64, "1h": 96, "15m": 160}
    seconds = {"1d": 86_400, "4h": 14_400, "1h": 3_600, "15m": 900}
    minimum = {"1d": 15, "4h": 15, "1h": 15, "15m": 50}
    result: dict[str, dict[str, Any]] = {}
    unknown = set(timeframes) - set(specs)
    if unknown:
        raise ValueError(f"unsupported strategy timeframes: {sorted(unknown)}")
    for timeframe in timeframes:
        limit = specs[timeframe]
        snapshot = feed.snapshot(symbol="GOLD", timeframe=timeframe, limit=limit, as_of=as_of)
        trusted = snapshot.get("status") in {"ready", "derived"} and snapshot.get("is_synthetic") is False
        # One bounded retry is intentional for transient same-source transport
        # failures. Synthetic data is rejected immediately and no provider
        # fallback is attempted.
        if not trusted and snapshot.get("is_synthetic") is False:
            snapshot = feed.snapshot(symbol="GOLD", timeframe=timeframe, limit=limit, as_of=as_of)
            trusted = snapshot.get("status") in {"ready", "derived"} and snapshot.get("is_synthetic") is False
        if not trusted:
            issues = snapshot.get("access_issues") or []
            detail = f": {issues[0]}" if issues else ""
            raise ValueError(f"strategy timeframe {timeframe} is unavailable or untrusted{detail}")
        completed: list[dict[str, Any]] = []
        for bar in snapshot.get("bars") or []:
            started = parse_utc(str(bar.get("timestamp") or ""))
            if started + timedelta(seconds=seconds[timeframe]) > checked_at:
                continue
            if timeframe == "1d" and started.weekday() >= 5:
                continue
            completed.append(dict(bar))
        if len(completed) < minimum[timeframe]:
            raise ValueError(f"strategy timeframe {timeframe} has insufficient completed bars")
        result[timeframe] = {
            "timeframe": timeframe,
            "provider": snapshot.get("provider"),
            "is_synthetic": False,
            "status": snapshot.get("status"),
            "fresh": snapshot.get("fresh"),
            "bars": completed,
            "bar_count": len(completed),
            "latest_timestamp": completed[-1].get("timestamp"),
            "completed_only": True,
            "weekends_excluded": timeframe == "1d",
        }
    return result
