from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from services.config_loader import ROOT, load_dualtrack_config, load_pipeline_config
from services.dualtrack_clock import BJ_TZ
from services.journal_store import load_json


CLOSE_GRACE_MINUTES = 120
DUALTRACK_CYCLE_LABEL = "com.wendy.trading-orchestrator.dualtrack-live-tick"


@dataclass(frozen=True)
class CycleSchedule:
    day_start: int
    night_start: int

    @property
    def hours(self) -> list[int]:
        return sorted([self.day_start, self.night_start])

    def kind_for_start_hour(self, hour: int) -> str:
        if hour == self.day_start:
            return "DAY"
        if hour == self.night_start:
            return "NIGHT"
        raise ValueError(f"unknown cycle start hour: {hour}")

    def start_hour_for_kind(self, kind: str) -> int:
        if kind == "DAY":
            return self.day_start
        if kind == "NIGHT":
            return self.night_start
        raise ValueError("cycle kind must be DAY or NIGHT")


class DualTrackCycleHeartbeat:
    def __init__(
        self,
        output_root: Path | None = None,
        *,
        config: dict[str, Any] | None = None,
        close_grace_minutes: int = CLOSE_GRACE_MINUTES,
    ) -> None:
        pipeline = load_pipeline_config()
        self.output_root = Path(output_root) if output_root else ROOT / str(pipeline.get("output_root", "outputs"))
        self.config = config if config is not None else load_dualtrack_config()
        self.close_grace = timedelta(minutes=int(close_grace_minutes))

    def run(self, *, as_of: str | datetime | None = None) -> dict[str, Any]:
        checked_at = _parse_utc(as_of)
        if not self._dualtrack_cycle_scheduled():
            return self._payload("not_scheduled", checked_at, None, None, [], "dualtrack_cycle_job_not_generated")
        schedule, config_error = self._cycle_schedule()
        if config_error:
            return self._payload("stale", checked_at, None, None, [], config_error)
        assert schedule is not None
        expected = self._latest_required_boundary(checked_at, schedule)
        if expected is None:
            return self._payload("fresh", checked_at, None, None, [], "inside_initial_grace_period")
        coverage = self._coverage(schedule)
        latest = max((item for item in coverage["covered"] if item <= expected), default=None)
        if latest and latest >= expected:
            return self._payload("fresh", checked_at, expected, latest, [], "latest_required_boundary_is_covered", [])
        missed = self._missed_boundaries(schedule, latest, expected, coverage["covered"])
        declared_gaps = [
            record
            for boundary, record in coverage["evidence_gaps"].items()
            if boundary in missed
        ]
        reason = self._stale_reason(coverage, missed)
        return self._payload("stale", checked_at, expected, latest, missed, reason, declared_gaps)

    def _dualtrack_cycle_scheduled(self) -> bool:
        rows = load_json(self.output_root / "schedules" / "current.json")
        schedule = rows[-1] if rows else {}
        jobs = schedule.get("jobs", []) if isinstance(schedule, dict) else []
        return any(str(job.get("label") or "") == DUALTRACK_CYCLE_LABEL for job in jobs if isinstance(job, dict))

    def _cycle_schedule(self) -> tuple[CycleSchedule | None, str]:
        raw = self.config.get("cycle_hours_utc") if isinstance(self.config, dict) else None
        if not isinstance(raw, dict):
            return None, "config_cycle_hours_utc_missing"
        try:
            day_start = int(raw["day_start"])
            night_start = int(raw["night_start"])
        except (KeyError, TypeError, ValueError):
            return None, "config_cycle_hours_utc_invalid"
        if day_start == night_start or not all(0 <= hour <= 23 for hour in (day_start, night_start)):
            return None, "config_cycle_hours_utc_invalid"
        return CycleSchedule(day_start=day_start, night_start=night_start), ""

    def _latest_required_boundary(self, checked_at: datetime, schedule: CycleSchedule) -> datetime | None:
        cutoff = checked_at - self.close_grace
        candidates = self._boundaries_between(cutoff - timedelta(days=2), cutoff, schedule)
        return max(candidates) if candidates else None

    def _coverage(self, schedule: CycleSchedule) -> dict[str, Any]:
        attribution_dir = self.output_root / "dualtrack" / "attribution"
        daily_dir = self.output_root / "dualtrack" / "ledger" / "daily"
        missing_dirs = [str(path) for path in (attribution_dir, daily_dir) if not path.exists()]
        parse_errors: list[str] = []
        attribution_boundaries = self._attribution_boundaries(attribution_dir, schedule, parse_errors)
        daily_boundaries = self._daily_boundaries(daily_dir, schedule, parse_errors)
        evidence_gaps = self._evidence_gap_boundaries(self.output_root / "dualtrack" / "evidence_gaps", schedule, parse_errors)
        return {
            "covered": attribution_boundaries & daily_boundaries,
            "evidence_gaps": evidence_gaps,
            "missing_dirs": missing_dirs,
            "parse_errors": parse_errors,
        }

    def _attribution_boundaries(self, root: Path, schedule: CycleSchedule, parse_errors: list[str]) -> set[datetime]:
        if not root.exists():
            return set()
        boundaries: set[datetime] = set()
        for path in sorted(root.glob("*.json")):
            boundary = self._boundary_from_cycle_id(path.stem, schedule)
            if boundary is None:
                parse_errors.append(str(path))
                continue
            if not self._json_nonempty(path, parse_errors):
                continue
            boundaries.add(boundary)
        return boundaries

    def _daily_boundaries(self, root: Path, schedule: CycleSchedule, parse_errors: list[str]) -> set[datetime]:
        if not root.exists():
            return set()
        boundaries: set[datetime] = set()
        for path in sorted(root.glob("*.json")):
            rows = self._read_json_rows(path, parse_errors)
            if not rows:
                continue
            latest = rows[-1] if isinstance(rows[-1], dict) else {}
            cycles = latest.get("cycles", {}) if isinstance(latest, dict) else {}
            if not isinstance(cycles, dict):
                parse_errors.append(str(path))
                continue
            for cycle_id in cycles:
                boundary = self._boundary_from_cycle_id(str(cycle_id), schedule)
                if boundary is None:
                    parse_errors.append(f"{path}:{cycle_id}")
                    continue
                boundaries.add(boundary)
        return boundaries

    def _boundary_from_cycle_id(self, cycle_id: str, schedule: CycleSchedule) -> datetime | None:
        try:
            date_part, kind = cycle_id.rsplit("_", 1)
            utc_hour = schedule.start_hour_for_kind(kind)
            bj_date = datetime.fromisoformat(date_part).date()
        except (ValueError, TypeError):
            return None
        bj_hour = (utc_hour + 8) % 24
        utc_date = bj_date - timedelta(days=1) if utc_hour + 8 >= 24 else bj_date
        start = datetime.combine(utc_date, datetime.min.time(), tzinfo=timezone.utc).replace(hour=utc_hour)
        return self._next_boundary_after(start, schedule)

    def _cycle_id_for_start(self, start: datetime, schedule: CycleSchedule) -> str:
        kind = schedule.kind_for_start_hour(start.hour)
        date_part = start.astimezone(BJ_TZ).date().isoformat()
        return f"{date_part}_{kind}"

    def _previous_start_before_boundary(self, boundary: datetime, schedule: CycleSchedule) -> datetime:
        starts = self._boundaries_between(boundary - timedelta(days=2), boundary - timedelta(seconds=1), schedule)
        return max(starts)

    def _next_boundary_after(self, start: datetime, schedule: CycleSchedule) -> datetime:
        candidates = self._boundaries_between(start + timedelta(seconds=1), start + timedelta(days=2), schedule)
        return min(candidates)

    def _missed_boundaries(
        self,
        schedule: CycleSchedule,
        latest: datetime | None,
        expected: datetime,
        covered: set[datetime],
    ) -> list[datetime]:
        if latest is None:
            return [expected]
        candidates = self._boundaries_between(latest + timedelta(seconds=1), expected, schedule)
        return [boundary for boundary in candidates if boundary not in covered]

    def _boundaries_between(self, start: datetime, end: datetime, schedule: CycleSchedule) -> list[datetime]:
        start = start.astimezone(timezone.utc).replace(microsecond=0)
        end = end.astimezone(timezone.utc).replace(microsecond=0)
        first_day = (start - timedelta(days=1)).date()
        last_day = (end + timedelta(days=1)).date()
        days = (last_day - first_day).days + 1
        boundaries: list[datetime] = []
        for offset in range(days):
            day = first_day + timedelta(days=offset)
            for hour in schedule.hours:
                boundary = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).replace(hour=hour)
                if start <= boundary <= end:
                    boundaries.append(boundary)
        return sorted(boundaries)

    def _json_nonempty(self, path: Path, parse_errors: list[str]) -> bool:
        return bool(self._read_json_rows(path, parse_errors))

    def _read_json_rows(self, path: Path, parse_errors: list[str]) -> list[Any]:
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            parse_errors.append(str(path))
            return []
        return rows if isinstance(rows, list) else []

    def _evidence_gap_boundaries(
        self,
        root: Path,
        schedule: CycleSchedule,
        parse_errors: list[str],
    ) -> dict[datetime, dict[str, Any]]:
        if not root.exists():
            return {}
        gaps: dict[datetime, dict[str, Any]] = {}
        for path in sorted(root.glob("*.json")):
            boundary = self._boundary_from_cycle_id(path.stem, schedule)
            rows = self._read_json_rows(path, parse_errors)
            if boundary is None or not rows or not isinstance(rows[-1], dict):
                continue
            record = rows[-1]
            if record.get("status") == "evidence_gap":
                gaps[boundary] = record
        return gaps

    def _stale_reason(self, coverage: dict[str, Any], missed: list[datetime]) -> str:
        if coverage["parse_errors"]:
            return "artifact_timestamp_parse_error"
        if coverage["missing_dirs"]:
            return "artifact_dir_missing"
        if missed and all(boundary in coverage["evidence_gaps"] for boundary in missed):
            return "declared_evidence_gap"
        return "latest_required_boundary_missing"

    def _payload(
        self,
        status: str,
        checked_at: datetime,
        expected: datetime | None,
        latest: datetime | None,
        missed: list[datetime],
        reason: str,
        evidence_gaps: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "checked_at": checked_at.isoformat(),
            "expected_boundary": expected.isoformat() if expected else None,
            "latest_artifact_at": latest.isoformat() if latest else None,
            "missed_boundaries": [item.isoformat() for item in missed],
            "reason": reason,
            "evidence_gaps": [
                {
                    "cycle_id": row.get("cycle_id"),
                    "reason": row.get("reason"),
                    "counts_as_closed_loop": bool(row.get("counts_as_closed_loop")),
                }
                for row in (evidence_gaps or [])
            ],
            "close_grace_minutes": int(self.close_grace.total_seconds() // 60),
        }


def _parse_utc(value: str | datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc).replace(microsecond=0)
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0)
