"""Recompute a fixed 48-hour Testnet execution soak from persisted receipts.

This module is deliberately evidence-only.  It reads JSON receipts and never
contacts a venue, scheduler, or service.  The public functions accept records
directly so a historical package can be replayed deterministically in tests.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo


WINDOW = timedelta(hours=48)
EXPECTED_TICKS = 2_880
BEIJING = ZoneInfo("Asia/Shanghai")
HEALTHY_EXECUTION_STATUSES = frozenset({"active", "idle_by_design"})


def _time(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _timestamp(row: Mapping[str, Any]) -> datetime | None:
    for key in ("occurred_at", "observed_at", "timestamp", "created_at"):
        value = _time(row.get(key))
        if value is not None:
            return value
    return None


def _execution(row: Mapping[str, Any]) -> Mapping[str, Any]:
    value = row.get("execution")
    return value if isinstance(value, Mapping) else {}


def _execution_status(row: Mapping[str, Any]) -> str:
    execution = _execution(row)
    return str(execution.get("status") or row.get("execution_status") or "unknown").strip().lower()


def _is_tick(row: Mapping[str, Any]) -> bool:
    """Exclude standalone rollover receipts from the 60-second tick count."""
    return bool(row.get("tick_id") or isinstance(row.get("execution"), Mapping) or row.get("execution_status"))


def _receipt_landed(row: Mapping[str, Any]) -> bool:
    """Recognise persisted tick receipts without treating HTTP state as one."""
    if row.get("receipt_landed") is False:
        return False
    if row.get("receipt_landed") is True or row.get("receipt_path"):
        return True
    if isinstance(row.get("receipt"), Mapping):
        return True
    if isinstance(row.get("rollover_receipt"), Mapping):
        return True
    execution = _execution(row)
    if isinstance(execution.get("receipt"), Mapping):
        return True
    # scheduler ticks are themselves durable receipts; heartbeat is their
    # explicit persisted marker in the M5-S3 contract.
    return isinstance(row.get("heartbeat"), Mapping) and bool(row.get("tick_id"))


def _is_rollover(row: Mapping[str, Any]) -> bool:
    values: list[Any] = [row.get("event"), row.get("action"), row.get("type")]
    for key in ("receipt", "rollover_receipt", "execution"):
        nested = row.get(key)
        if isinstance(nested, Mapping):
            values.extend(nested.get(k) for k in ("event", "action", "type", "kind"))
    return _receipt_landed(row) and any("rollover" in str(value or "").lower() for value in values)


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_records(input_path: Path) -> list[dict[str, Any]]:
    """Load a JSON list, or the scheduler ticks file below an output root."""
    path = Path(input_path)
    if path.is_dir():
        candidates = (path / "testnet_automation/scheduler/ticks.json", path / "ticks.json")
        path = next((candidate for candidate in candidates if candidate.is_file()), Path())
        if not path:
            raise FileNotFoundError("ticks.json not found below input root")
    payload = _load_json(path)
    if isinstance(payload, Mapping):
        payload = payload.get("ticks", payload.get("records", []))
    if not isinstance(payload, list) or not all(isinstance(row, Mapping) for row in payload):
        raise ValueError("input must contain a JSON list of tick receipts")
    return [dict(row) for row in payload]


def _boundaries(start: datetime) -> list[datetime]:
    local_start = start.astimezone(BEIJING)
    cursor = local_start.replace(hour=0, minute=0, second=0, microsecond=0)
    result: list[datetime] = []
    while cursor <= local_start:
        cursor += timedelta(hours=1)
    # Walk days instead of assuming a UTC offset; Beijing has no DST, but this
    # keeps the boundary definition explicit and easy to audit.
    day = local_start.date()
    for offset in range(4):
        current_day = day + timedelta(days=offset)
        for hour in (8, 21):
            candidate = datetime(current_day.year, current_day.month, current_day.day, hour, tzinfo=BEIJING).astimezone(timezone.utc)
            if start < candidate < start + WINDOW:
                result.append(candidate)
    return sorted(result)


def build_soak_report(records: Iterable[Mapping[str, Any]], *, start: str | datetime | None = None) -> dict[str, Any]:
    """Return the deterministic 48-hour report for historical tick receipts."""
    rows = [dict(row) for row in records]
    dated = [(row, _timestamp(row)) for row in rows]
    dated = [(row, when) for row, when in dated if when is not None]
    if start is None:
        if not dated:
            raise ValueError("at least one timestamped tick is required")
        window_start = min(when for _row, when in dated)
    else:
        window_start = _time(start) if not isinstance(start, datetime) else _time(start.isoformat())
        if window_start is None:
            raise ValueError("start must be timezone-aware ISO-8601")
    window_end = window_start + WINDOW
    in_window = [row for row, when in dated if window_start <= when < window_end and _is_tick(row)]
    healthy = [row for row in in_window if _execution_status(row) in HEALTHY_EXECUTION_STATUSES and _receipt_landed(row)]
    unknown = [row for row in in_window if _execution_status(row) == "unknown"]
    boundary_rows = []
    for boundary in _boundaries(window_start):
        matches = [row for row, when in dated if when == boundary and _is_rollover(row)]
        boundary_rows.append({
            "boundary": boundary.astimezone(BEIJING).isoformat(),
            "status": "present" if matches else "missing",
            "receipt_count": len(matches),
        })
    coverage = len(healthy) / EXPECTED_TICKS
    boundaries_ok = len(boundary_rows) == 3 and all(item["status"] == "present" for item in boundary_rows)
    return {
        "schema_version": "testnet-soak-report-v1",
        "window": {"start": window_start.isoformat(), "end": window_end.isoformat(), "hours": 48},
        "expected_ticks": EXPECTED_TICKS,
        "observed_ticks": len(in_window),
        "healthy_ticks": len(healthy),
        "missing_ticks": EXPECTED_TICKS - len(in_window),
        "coverage": round(coverage, 6),
        "coverage_percent": round(coverage * 100, 4),
        "unknown_control_results": len(unknown),
        "rollover_boundaries": boundary_rows,
        "acceptance": {
            "coverage_at_least_85_percent": coverage >= 0.85,
            "zero_unknown_control_results": not unknown,
            "three_rollover_boundaries_present": boundaries_ok,
        },
        "status": "pass" if coverage >= 0.85 and not unknown and boundaries_ok else "blocked",
    }


def render_markdown(report: Mapping[str, Any]) -> str:
    window = report["window"]
    acceptance = report["acceptance"]
    lines = [
        "# Testnet 48-hour soak report", "", f"- Status: `{report['status']}`",
        f"- Window (UTC): `{window['start']}` → `{window['end']}`", "- Denominator: `2,880` expected 60-second ticks", "",
        "| Metric | Value |", "| --- | ---: |",
        f"| Observed ticks | {report['observed_ticks']} |", f"| Healthy ticks with receipt | {report['healthy_ticks']} |",
        f"| Missing ticks | {report['missing_ticks']} |", f"| Coverage | {report['coverage_percent']}% |",
        f"| Unknown control results | {report['unknown_control_results']} |", "", "## Rollover boundaries", "",
        "| Beijing boundary | Result | Receipts |", "| --- | --- | ---: |",
    ]
    lines.extend(f"| `{item['boundary']}` | `{item['status']}` | {item['receipt_count']} |" for item in report["rollover_boundaries"])
    lines.extend(["", "## Acceptance", "", f"- Coverage ≥85%: `{acceptance['coverage_at_least_85_percent']}`", f"- Unknown = 0: `{acceptance['zero_unknown_control_results']}`", f"- Three rollover boundaries present: `{acceptance['three_rollover_boundaries_present']}`", ""])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Recompute a read-only 48-hour Testnet soak report")
    parser.add_argument("--input", required=True, type=Path, help="ticks.json or an output root containing it")
    parser.add_argument("--output-dir", default=Path("docs/evidence/issue-1150"), type=Path)
    parser.add_argument("--start", help="timezone-aware ISO-8601 window start")
    args = parser.parse_args(argv)
    try:
        report = build_soak_report(load_records(args.input), start=args.start)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "soak-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (args.output_dir / "soak-report.md").write_text(render_markdown(report), encoding="utf-8")
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "blocked", "code": type(exc).__name__, "reason": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
