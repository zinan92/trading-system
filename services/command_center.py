from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from services.config_loader import ROOT, load_pipeline_config
from services.dualtrack_clock import cycle_window, parse_utc, seconds_until_end
from services.dualtrack_config import dualtrack_config
from services.dualtrack_cycle_heartbeat import DualTrackCycleHeartbeat
from services.dualtrack_scoring import DualTrackScorer
from services.dualtrack_store import DualTrackPlanStore
from services.system_state import build_system_state


def build_command_center_state(output_root: Path | None = None, *, as_of: str | datetime | None = None) -> dict[str, Any]:
    root = Path(output_root) if output_root else _default_output_root()
    now = parse_utc(as_of)
    system = _build_system(root, now)
    cycle_liveness = _build_cycle_liveness(root, now)
    cycle = _build_cycle_payload(root, now)
    if cycle is not None:
        cycle["phase"] = _phase(cycle, now, cycle_liveness)
    scoreboard = _scoreboard(_ledger_payload(root))
    calibration = _calibration(root)
    return {
        "generated_at": now.isoformat(),
        "system": system,
        "cycle": cycle,
        "scoreboard": scoreboard,
        "calibration": calibration,
        "cycle_liveness": cycle_liveness,
        "next_action": _next_action(system, cycle),
    }


def _default_output_root() -> Path:
    config = load_pipeline_config()
    return ROOT / str(config.get("output_root", "outputs"))


def _build_system(root: Path, now: datetime) -> dict[str, Any]:
    try:
        payload = build_system_state(output_root=root, as_of=now)
        if isinstance(payload, dict):
            return payload
    except Exception as exc:  # noqa: BLE001 - command center must fail closed, not 500.
        return _unknown_system(now, f"system_state_builder_failed: {exc.__class__.__name__}")
    return _unknown_system(now, "system_state_payload_invalid")


def _unknown_system(now: datetime, reason: str) -> dict[str, Any]:
    return {
        "generated_at": now.isoformat(),
        "overall": "UNKNOWN",
        "checks": [{"id": "system_state", "status": "UNKNOWN", "reason": reason, "room": "ops", "cta": "查看运维"}],
    }


def _build_cycle_payload(root: Path, now: datetime) -> dict[str, Any] | None:
    try:
        cfg = dualtrack_config()
        deadline = int(cfg.get("plan_lock_deadline_min_before_cycle", 0))
        window = cycle_window(now, lock_deadline_min_before_cycle=deadline)
        store = DualTrackPlanStore(root, config=cfg)
        reveal_allowed = store.reveal_allowed(window.cycle_id, as_of=now)
        effective = store.effective_plan(window.cycle_id, as_of=now) if reveal_allowed else None
        effective_status: dict[str, Any] = {
            "has_effective_plan": effective is not None,
            "machine_stands_down": effective is None,
        }
        if reveal_allowed and effective is not None:
            effective_status["author"] = str(effective.get("effective_author") or "")
        return {
            **window.to_dict(),
            "countdown_seconds": seconds_until_end(now, lock_deadline_min_before_cycle=deadline),
            "effective_plan_status": effective_status,
        }
    except Exception:  # noqa: BLE001 - missing cycle inputs should render unknown, not crash.
        return None


def _build_cycle_liveness(root: Path, now: datetime) -> dict[str, Any]:
    try:
        payload = DualTrackCycleHeartbeat(output_root=root).run(as_of=now)
        status = str(payload.get("status") or "unknown")
        if status not in {"fresh", "stale", "not_scheduled"}:
            status = "stale"
        return {
            "status": status,
            "reason": str(payload.get("reason") or status),
            "expected_boundary": payload.get("expected_boundary"),
            "latest_artifact_at": payload.get("latest_artifact_at"),
            "missed_boundaries": payload.get("missed_boundaries", []),
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": "stale", "reason": f"heartbeat_failed: {exc.__class__.__name__}"}


def _phase(cycle: dict[str, Any], now: datetime, liveness: dict[str, Any]) -> str:
    lock_deadline = _parse_time(cycle.get("lock_deadline"))
    end = _parse_time(cycle.get("end"))
    latest = _parse_time(liveness.get("latest_artifact_at"))
    if end and latest and latest >= end:
        return "revealed"
    if lock_deadline and now < lock_deadline:
        return "blind"
    if end and now >= end:
        return "revealed"
    return "intraday"


def _ledger_payload(root: Path) -> dict[str, Any]:
    try:
        payload = DualTrackScorer(root).ledger_payload()
        return payload if isinstance(payload, dict) else {"daily": []}
    except Exception:  # noqa: BLE001
        return {"daily": []}


def _scoreboard(ledger: dict[str, Any]) -> dict[str, Any]:
    daily = [row for row in ledger.get("daily", []) if isinstance(row, dict)]
    machine = round(sum(_track_pnl(row, "machine") for row in daily), 8)
    human = round(sum(_track_pnl(row, "human") for row in daily), 8)
    cycles: list[dict[str, Any]] = []
    for row in daily:
        for cycle_id, item in (row.get("cycles") or {}).items():
            if isinstance(item, dict):
                cycles.append({"cycle_id": str(cycle_id), **item})
    return {
        "total_machine_pnl": machine,
        "total_human_pnl": human,
        "delta": round(machine - human, 8),
        "cycles_scored": len(cycles),
        "last_cycle": cycles[-1] if cycles else None,
    }


def _track_pnl(row: dict[str, Any], track: str) -> float:
    try:
        return float(((row.get("tracks") or {}).get(track) or {}).get("realized_pnl") or 0)
    except (TypeError, ValueError):
        return 0.0


def _calibration(root: Path) -> dict[str, Any]:
    path = root / "bias_ledger" / "summary.json"
    if not path.exists():
        return {"available": False, "total": 0, "hit_rate": None, "mean_brier": None, "last_10": []}
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"available": False, "total": 0, "hit_rate": None, "mean_brier": None, "last_10": []}
    total = int(row.get("total") or 0)
    return {
        "available": total > 0,
        "total": total,
        "adjudicated": int(row.get("adjudicated") or 0),
        "correct": int(row.get("correct") or 0),
        "wrong": int(row.get("wrong") or 0),
        "undecidable": int(row.get("undecidable") or 0),
        "hit_rate": row.get("hit_rate"),
        "mean_brier": row.get("mean_brier"),
        "last_10": row.get("last_10") if isinstance(row.get("last_10"), list) else [],
    }


def _next_action(system: dict[str, Any], cycle: dict[str, Any] | None) -> dict[str, str]:
    checks = [item for item in system.get("checks", []) if isinstance(item, dict)]
    if system.get("overall") == "BLOCKED":
        blocked = next((check for check in checks if check.get("status") == "BLOCKED"), None)
        if blocked:
            return {"kind": "fix", "label": str(blocked.get("reason") or "系统阻断"), "target": str(blocked.get("room") or "ops")}
    status = (cycle or {}).get("effective_plan_status") or {}
    if cycle and cycle.get("phase") == "blind" and status.get("has_effective_plan") is False:
        return {"kind": "blind_answer", "label": "提交本周期盲答作战单", "target": "dualtrack"}
    return {"kind": "none", "label": "系统运行正常,等待下一周期", "target": ""}


def _parse_time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return parse_utc(value)
    except (TypeError, ValueError):
        return None
