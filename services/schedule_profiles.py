from __future__ import annotations

from dataclasses import dataclass
from typing import Any

PROJECT_LABEL_PREFIX = "com.wendy.trading-orchestrator."
DUALTRACK_FOCUS_PROFILE = "dualtrack_focus"
FULL_PROFILE = "full"

FULL_SCHEDULE_LABELS = [
    "com.wendy.trading-orchestrator.runner",
    "com.wendy.trading-orchestrator.trading-plan",
    "com.wendy.trading-orchestrator.evening-review",
    "com.wendy.trading-orchestrator.daily-review",
    "com.wendy.trading-orchestrator.dashboard",
    "com.wendy.trading-orchestrator.strategies",
    "com.wendy.trading-orchestrator.dualtrack-live-tick",
    "com.wendy.trading-orchestrator.deadman-ping",
]

FOCUS_SCHEDULE_LABELS = [
    "com.wendy.trading-orchestrator.gold-1m-feed",
    "com.wendy.trading-orchestrator.dualtrack-live-tick",
    "com.wendy.trading-orchestrator.dashboard",
    "com.wendy.trading-orchestrator.deadman-ping",
]


@dataclass(frozen=True)
class ScheduleProfile:
    name: str
    labels: list[str]


PROFILES = {
    DUALTRACK_FOCUS_PROFILE: ScheduleProfile(DUALTRACK_FOCUS_PROFILE, FOCUS_SCHEDULE_LABELS),
    FULL_PROFILE: ScheduleProfile(FULL_PROFILE, FULL_SCHEDULE_LABELS),
}


def normalize_schedule_profile(value: str | None) -> str:
    profile = str(value or DUALTRACK_FOCUS_PROFILE).strip() or DUALTRACK_FOCUS_PROFILE
    if profile not in PROFILES:
        raise ValueError(f"unsupported schedule profile: {profile}")
    return profile


def profile_from_config(config: dict[str, Any] | None) -> str:
    schedule = (config or {}).get("schedule", {})
    if not isinstance(schedule, dict):
        schedule = {}
    return normalize_schedule_profile(str(schedule.get("profile") or DUALTRACK_FOCUS_PROFILE))


def profile_from_schedule(schedule: dict[str, Any] | None, config: dict[str, Any] | None = None) -> str:
    profile = str((schedule or {}).get("profile") or "").strip()
    if profile:
        return normalize_schedule_profile(profile)
    return profile_from_config(config)


def labels_for_profile(profile: str | None) -> list[str]:
    return list(PROFILES[normalize_schedule_profile(profile)].labels)


def is_focus_profile(profile: str | None) -> bool:
    return normalize_schedule_profile(profile) == DUALTRACK_FOCUS_PROFILE
