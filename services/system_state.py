from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from services.config_loader import ROOT, load_pipeline_config


STATUS_ORDER = {"RUN": 0, "DEGRADED": 1, "UNKNOWN": 2, "BLOCKED": 3}
SCHEDULE_MAX_AGE = timedelta(hours=24)
DATA_MAX_AGE = timedelta(minutes=30)
_CACHE_TTL_SECONDS = 30
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


@dataclass(frozen=True)
class SystemCheck:
    id: str
    status: str
    reason: str
    room: str
    cta: str
    skipped: bool = False
    evidence: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "status": self.status,
            "reason": self.reason,
            "room": self.room,
            "cta": self.cta,
        }
        if self.skipped:
            payload["skipped"] = True
        if self.evidence:
            payload["evidence"] = self.evidence
        return payload


def build_system_state(output_root: Path | None = None, *, as_of: str | datetime | None = None) -> dict[str, Any]:
    root = Path(output_root) if output_root else _default_output_root()
    now = _parse_utc(as_of)
    cache_key = str(root.resolve())
    if as_of is None:
        cached = _CACHE.get(cache_key)
        if cached and (datetime.now(timezone.utc).timestamp() - cached[0]) < _CACHE_TTL_SECONDS:
            return cached[1]
    try:
        checks = [
            _check_naked_position(root),
            _check_schedule(root, now),
            _check_dualtrack_heartbeat(root, now),
            _check_daily_review(root),
            _check_data_freshness(root, now),
        ]
        payload = {
            "generated_at": now.isoformat(),
            "overall": _overall(checks),
            "checks": [check.to_dict() for check in checks],
        }
    except Exception as exc:  # noqa: BLE001 - the shell must receive a fail-closed payload.
        payload = {
            "generated_at": now.isoformat(),
            "overall": "UNKNOWN",
            "checks": [
                SystemCheck(
                    id="system_state",
                    status="UNKNOWN",
                    reason=f"system_state_builder_failed: {exc.__class__.__name__}",
                    room="ops",
                    cta="查看运维",
                ).to_dict()
            ],
        }
    if as_of is None:
        _CACHE[cache_key] = (datetime.now(timezone.utc).timestamp(), payload)
    return payload


def _default_output_root() -> Path:
    config = load_pipeline_config()
    return ROOT / str(config.get("output_root", "outputs"))


def _overall(checks: list[SystemCheck]) -> str:
    return max((check.status for check in checks), key=lambda status: STATUS_ORDER.get(status, 2))


def _check_naked_position(root: Path) -> SystemCheck:
    paths = sorted((root / "strategies").glob("*/live_reconciliation/current.json"))
    if not paths:
        return _unknown("naked_position", "live_reconciliation_artifact_missing", "处理裸头寸")
    healthy = 0
    for path in paths:
        strategy = path.parents[1].name
        row, error = _latest_row(path)
        if error:
            return _unknown("naked_position", f"{strategy}: {error}", "处理裸头寸")
        state = str(row.get("system_state") or row.get("status") or "")
        if state.startswith("BLOCKED"):
            since = _first_text(row, "blocked_since", "checked_at", "generated_at", "run_date")
            reason_code = _first_text(row, "reason_code", "system_state") or state
            reason = f"{strategy} {reason_code}"
            if since:
                reason = f"{reason} since {str(since)[:10]}"
            return SystemCheck(
                id="naked_position",
                status="BLOCKED",
                reason=reason,
                room="ops",
                cta=str(row.get("escalation_action") or "处理裸头寸"),
                evidence={"artifact": str(path), "system_state": state},
            )
        healthy += 1
    return SystemCheck(
        id="naked_position",
        status="RUN",
        reason=f"no blocked live reconciliation across {healthy} strategy artifact(s)",
        room="ops",
        cta="查看运维",
    )


def _check_schedule(root: Path, now: datetime) -> SystemCheck:
    path = root / "schedules" / "status_current.json"
    row, error = _latest_row(path)
    if error:
        return _unknown("schedule", error, "检查调度")
    checked_at, parse_error = _row_time(row, "checked_at", "generated_at")
    if parse_error or checked_at is None:
        return _unknown("schedule", parse_error or "schedule_timestamp_missing", "检查调度")
    if now - checked_at > SCHEDULE_MAX_AGE:
        return _unknown("schedule", f"schedule_status_stale since {checked_at.isoformat()}", "检查调度")
    status = str(row.get("status") or "unknown")
    if status == "active":
        return SystemCheck(
            id="schedule",
            status="RUN",
            reason="launchd schedule active",
            room="ops",
            cta="查看调度",
            evidence={"checked_at": checked_at.isoformat()},
        )
    degraded_states = {"stale_installed", "installed", "partial_installed", "generated_only", "missing", "fail"}
    return SystemCheck(
        id="schedule",
        status="DEGRADED" if status in degraded_states else "UNKNOWN",
        reason=f"{status}: {row.get('message') or 'schedule not active'}",
        room="ops",
        cta="修复调度",
        evidence={
            "checked_at": checked_at.isoformat(),
            "mismatched_jobs": row.get("mismatched_jobs", []),
            "loaded_count": row.get("loaded_count"),
            "required_count": row.get("required_count"),
        },
    )


def _check_dualtrack_heartbeat(root: Path, now: datetime) -> SystemCheck:
    schedule_path = root / "schedules" / "current.json"
    if not schedule_path.exists():
        return _unknown("dualtrack_heartbeat", "generated_schedule_artifact_missing", "检查双轨周期")
    try:
        from services.dualtrack_cycle_heartbeat import DualTrackCycleHeartbeat
    except ImportError:
        return SystemCheck(
            id="dualtrack_heartbeat",
            status="RUN",
            reason="dualtrack heartbeat service skipped",
            room="ops",
            cta="查看双轨",
            skipped=True,
        )
    try:
        result = DualTrackCycleHeartbeat(output_root=root).run(as_of=now)
    except Exception as exc:  # noqa: BLE001 - unreadable heartbeat inputs are unsafe.
        return _unknown("dualtrack_heartbeat", f"heartbeat_failed: {exc.__class__.__name__}", "检查双轨周期")
    status = str(result.get("status") or "unknown")
    if status == "stale":
        return SystemCheck(
            id="dualtrack_heartbeat",
            status="DEGRADED",
            reason=str(result.get("reason") or "dualtrack close-cycle stale"),
            room="ops",
            cta="修复双轨周期",
            evidence={
                "expected_boundary": result.get("expected_boundary"),
                "latest_artifact_at": result.get("latest_artifact_at"),
                "missed_boundaries": result.get("missed_boundaries", []),
            },
        )
    if status in {"fresh", "not_scheduled"}:
        return SystemCheck(
            id="dualtrack_heartbeat",
            status="RUN",
            reason=f"dualtrack cycle heartbeat {status}",
            room="ops",
            cta="查看双轨",
            evidence={"expected_boundary": result.get("expected_boundary")},
        )
    return _unknown("dualtrack_heartbeat", f"unexpected_heartbeat_status: {status}", "检查双轨周期")


def _check_daily_review(root: Path) -> SystemCheck:
    path = root / "daily_review_runs" / "current.json"
    row, error = _latest_row(path)
    if error:
        return _unknown("daily_review", error, "检查日评")
    status = str(row.get("status") or "").lower()
    if not status:
        return _unknown("daily_review", "daily_review_status_missing", "检查日评")
    if status in {"fail", "failed", "error", "blocked"}:
        return SystemCheck(
            id="daily_review",
            status="DEGRADED",
            reason=f"daily review {status}",
            room="ops",
            cta="修复日评",
            evidence={"finished_at": row.get("finished_at") or row.get("generated_at")},
        )
    return SystemCheck(
        id="daily_review",
        status="RUN",
        reason=f"daily review {status}",
        room="ops",
        cta="查看日评",
    )


def _check_data_freshness(root: Path, now: datetime) -> SystemCheck:
    path = root / "data_source_preflight" / "current.json"
    row, error = _latest_row(path)
    if error:
        return _unknown("data_freshness", error, "检查行情")
    raw_ts = _first_text(row, "latest_timestamp", "checked_at")
    latest_bar = row.get("latest_bar")
    if isinstance(latest_bar, dict) and latest_bar.get("timestamp"):
        raw_ts = str(latest_bar["timestamp"])
    if not raw_ts:
        return _unknown("data_freshness", "latest_bar_timestamp_missing", "检查行情")
    try:
        latest = _parse_utc(str(raw_ts))
    except ValueError:
        return _unknown("data_freshness", "latest_bar_timestamp_invalid", "检查行情")
    age = now - latest
    if age > DATA_MAX_AGE:
        return SystemCheck(
            id="data_freshness",
            status="DEGRADED",
            reason=f"GOLD latest bar age {int(age.total_seconds() // 60)} minutes",
            room="ops",
            cta="刷新行情",
            evidence={"latest_timestamp": latest.isoformat()},
        )
    return SystemCheck(
        id="data_freshness",
        status="RUN",
        reason=f"GOLD latest bar age {int(max(age.total_seconds(), 0) // 60)} minutes",
        room="ops",
        cta="查看行情",
        evidence={"latest_timestamp": latest.isoformat()},
    )


def _latest_row(path: Path) -> tuple[dict[str, Any], str]:
    if not path.exists():
        return {}, f"{path.name}_missing"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, f"{path.name}_unreadable"
    rows = payload if isinstance(payload, list) else [payload]
    rows = [row for row in rows if isinstance(row, dict)]
    if not rows:
        return {}, f"{path.name}_empty"
    return rows[-1], ""


def _unknown(check_id: str, reason: str, cta: str) -> SystemCheck:
    return SystemCheck(id=check_id, status="UNKNOWN", reason=reason, room="ops", cta=cta)


def _first_text(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return ""


def _row_time(row: dict[str, Any], *keys: str) -> tuple[datetime | None, str]:
    for key in keys:
        raw = row.get(key)
        if not raw:
            continue
        try:
            return _parse_utc(str(raw)), ""
        except ValueError:
            return None, f"{key}_invalid"
    return None, ""


def _parse_utc(value: str | datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc).replace(microsecond=0)
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"invalid UTC timestamp: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0)
