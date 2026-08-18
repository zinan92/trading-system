"""Side-effect-free public projection of persisted Park Paper facts."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from services.journal_store import load_json
from services.park_paper_runtime import park_paper_namespace
from services.park_strategy_session import ParkStrategyIdentityJournal


PARK_PUBLIC_READ_MODEL_SCHEMA = "park-paper-public-read-model-v1"
PARK_PUBLIC_MAX_AGE_SECONDS = 300


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path.name} row is not an object")
        rows.append(value)
    return rows


def _latest_array_row(path: Path) -> dict[str, Any]:
    rows = load_json(path)
    row = rows[-1] if rows else {}
    if row and not isinstance(row, dict):
        raise ValueError(f"{path.name} row is not an object")
    return dict(row)


def _matching_latest(
    rows: list[dict[str, Any]],
    *,
    session_id: str,
    revision_id: str,
) -> dict[str, Any]:
    return next(
        (
            dict(row)
            for row in reversed(rows)
            if str(row.get("strategy_session_id") or "") == session_id
            and str(row.get("strategy_revision_id") or "") == revision_id
        ),
        {},
    )


def _timestamp_age_seconds(value: Any, now: datetime) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (now - parsed.astimezone(timezone.utc)).total_seconds())


def _compact_rows(rows: Any, fields: tuple[str, ...]) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    return [
        {key: row.get(key) for key in fields if key in row}
        for row in rows
        if isinstance(row, Mapping)
    ]


def _recording_projection(
    root: Path,
    *,
    active_pair: tuple[str, str] = ("", ""),
) -> dict[str, Any]:
    try:
        packages = _read_jsonl(root / "park_strategy" / "recording" / "packages.jsonl")
        events = _read_jsonl(root / "park_strategy" / "recording" / "events.jsonl")
        blockers = _read_jsonl(root / "park_strategy" / "runtime_blockers.jsonl")
    except (OSError, ValueError, json.JSONDecodeError):
        return {
            "status": "blocked",
            "record_window_id": None,
            "strategy_session_id": None,
            "strategy_revision_id": None,
            "package_status": None,
            "missing_categories": [],
            "strategy_open": None,
            "positions_open": None,
            "execution_mutations": [],
            "next_action": "notify_park_and_wait",
            "blocker_code": "recording_journal_invalid",
        }
    def matches(row: Mapping[str, Any]) -> bool:
        session, revision = active_pair
        if not session or not revision:
            return False
        pairs = set(
            zip(
                row.get("strategy_session_ids") or [],
                row.get("strategy_revision_ids") or [],
            )
        )
        scalar = (
            str(row.get("strategy_session_id") or ""),
            str(row.get("strategy_revision_id") or ""),
        )
        return (session, revision) in pairs or scalar == (session, revision)

    matching_packages = [row for row in packages if matches(row)]
    matching_manifests = [
        row
        for row in events
        if row.get("event") == "manifest_started" and matches(row)
    ]
    latest_manifest = dict(matching_manifests[-1]) if matching_manifests else {}
    latest_window_id = str(latest_manifest.get("record_window_id") or "")
    window_packages = [
        row for row in matching_packages
        if not latest_window_id or str(row.get("record_window_id") or "") == latest_window_id
    ]
    package = dict(window_packages[-1]) if window_packages else {}
    blocker = next(
        (
            row
            for row in reversed(blockers)
            if str(row.get("code") or "").startswith("recording_")
            and str(row.get("code") or "") != "recording_facts_recovered"
            and matches(row)
        ),
        {},
    )
    package_review_status = str(package.get("review_status") or "complete") if package else None
    blocker_windows = {
        str(item.get("record_window_id") or "")
        for item in blocker.get("recording_windows") or []
        if isinstance(item, Mapping)
    }
    latest_package_by_window: dict[str, dict[str, Any]] = {}
    for row in matching_packages:
        latest_package_by_window[str(row.get("record_window_id") or "")] = row
    resolved_windows = {
        window_id
        for window_id, row in latest_package_by_window.items()
        if row.get("status") == "complete"
        and str(row.get("review_status") or "complete") == "complete"
    }
    if blocker and blocker_windows:
        recovered_windows = {
            window_id
            for window_id in blocker_windows
            if any(
                str(row.get("record_window_id") or "") == window_id
                and matches(row)
                and str(row.get("recorded_at") or "") >= str(blocker.get("recorded_at") or "")
                for row in blockers
                if row.get("code") == "recording_facts_recovered"
            )
        }
        if blocker.get("code") == "recording_facts_blocked" and blocker_windows.issubset(resolved_windows | recovered_windows):
            blocker = {}
        elif blocker.get("code") == "recording_package_blocked" and blocker_windows.issubset(resolved_windows):
            blocker = {}
    if package.get("status") == "complete" and package_review_status == "complete" and blocker:
        blocked_windows = {
            str(row.get("record_window_id") or "")
            for row in blocker.get("recording_windows") or []
            if isinstance(row, Mapping)
        }
        if str(package.get("record_window_id") or "") in blocked_windows:
            blocker = {}
    if active_pair != ("", "") and packages and not package and not latest_manifest:
        return {
            "status": "blocked",
            "record_window_id": None,
            "strategy_session_id": active_pair[0],
            "strategy_revision_id": active_pair[1],
            "package_status": None,
            "missing_categories": [],
            "strategy_open": None,
            "positions_open": None,
            "execution_mutations": [],
            "next_action": "notify_park_and_wait",
            "blocker_code": "recording_identity_missing",
        }
    package_status = str(package.get("status") or "") or None
    blocker_code = str(blocker.get("code") or "") or None
    execution_mutations = list(package.get("execution_mutations") or [])
    if execution_mutations:
        blocker_code = blocker_code or "recording_execution_mutation_detected"
    if package_status == "complete" and package_review_status != "complete":
        blocker_code = blocker_code or "recording_review_pending"
    if blocker_code or package_status == "blocked_incomplete":
        status = "blocked"
    elif package_status == "complete":
        status = "complete"
    elif latest_manifest:
        # A live Recording Window exists before its 12h package is closed.
        # It is evidence in progress, never a strategy transition.
        status = "in_progress"
    else:
        status = "none"
    active_recording = latest_manifest or package
    return {
        "status": status,
        "record_window_id": active_recording.get("record_window_id"),
        "strategy_session_id": active_recording.get("strategy_session_id") or (active_pair[0] if latest_manifest else None),
        "strategy_revision_id": active_recording.get("strategy_revision_id") or (active_pair[1] if latest_manifest else None),
        "package_status": package_status,
        "missing_categories": list(package.get("missing_categories") or []),
        "strategy_open": package.get("strategy_open") if package else True if latest_manifest else None,
        "positions_open": package.get("positions_open"),
        "execution_mutations": execution_mutations,
        "next_action": "notify_park_and_wait" if blocker_code else package.get("next_action") or "continue_recording_window" if latest_manifest else None,
        "blocker_code": blocker_code,
    }


def _empty_execution() -> dict[str, Any]:
    return {
        "engine": "unavailable",
        "counts": {
            "accepted_orders": 0,
            "filled_orders": 0,
            "fills": 0,
            "open_positions": 0,
            "closed_positions": 0,
        },
        "orders": [],
        "fills": [],
        "positions": [],
        "account": {},
        "pnl": {},
        "reconciliation": {"status": "missing", "issues": ["snapshot_missing"]},
    }


def build_park_public_read_model(
    output_root: Path,
    *,
    now: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Read the last persisted Park snapshot without refreshing any source."""

    root = Path(output_root)
    checked_at = (now or (lambda: datetime.now(timezone.utc)))()
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    checked_at = checked_at.astimezone(timezone.utc)
    blockers: list[str] = []
    active: dict[str, Any] = {}
    plan: dict[str, Any] = {}
    lifecycle: dict[str, Any] = {}
    snapshot: dict[str, Any] = {}
    safety: dict[str, Any] = {}

    try:
        active = dict(ParkStrategyIdentityJournal(root).active_session() or {})
    except (OSError, ValueError, json.JSONDecodeError):
        blockers.append("identity_journal_invalid")

    session_id = str(active.get("strategy_session_id") or "")
    revision_id = str(active.get("strategy_revision_id") or "")
    if not session_id or not revision_id:
        blockers.append("active_strategy_missing")
    else:
        try:
            lifecycle = _matching_latest(
                _read_jsonl(root / "park_strategy" / "lifecycle.jsonl"),
                session_id=session_id,
                revision_id=revision_id,
            )
            plan = _matching_latest(
                _read_jsonl(root / "park_strategy" / "plans.jsonl"),
                session_id=session_id,
                revision_id=revision_id,
            )
            namespace = park_paper_namespace(session_id)
            snapshot = _latest_array_row(
                root
                / "dualtrack"
                / "nautilus_authoritative"
                / "snapshots"
                / f"{namespace}.json"
            )
        except (OSError, ValueError, json.JSONDecodeError):
            blockers.append("park_snapshot_invalid")

    try:
        safety = _latest_array_row(
            root / "park_strategy" / "safety_evidence.json"
        )
    except (OSError, ValueError, json.JSONDecodeError):
        blockers.append("safety_evidence_invalid")

    if session_id and not lifecycle:
        blockers.append("active_lifecycle_missing")
    if session_id and not plan:
        blockers.append("active_plan_missing")
    if session_id and not snapshot:
        blockers.append("authoritative_snapshot_missing")

    expected_digest = str(active.get("plan_digest") or "")
    observed_digests = {
        str(row.get("plan_digest") or "")
        for row in (lifecycle, plan)
        if row
    }
    if expected_digest and any(
        digest and digest != expected_digest for digest in observed_digests
    ):
        blockers.append("strategy_ownership_mismatch")

    capabilities = dict(snapshot.get("capabilities") or {})
    reconciliation = dict(snapshot.get("reconciliation") or {})
    mark = dict(snapshot.get("mark") or {})
    if snapshot and snapshot.get("engine") != "nautilus_paper":
        blockers.append("authoritative_engine_mismatch")
    if snapshot and capabilities.get("paper_only") is not True:
        blockers.append("paper_only_evidence_missing")
    if snapshot and capabilities.get("immutable_fill_guard") is not True:
        blockers.append("immutable_fill_guard_missing")
    if snapshot and reconciliation.get("status") != "ok":
        blockers.append("reconciliation_not_ok")
    if snapshot and mark.get("fresh") is not True:
        blockers.append("market_mark_not_fresh")
    if safety.get("status") != "pass":
        blockers.append("safety_evidence_not_passing")
    if safety.get("paper_only") is not True:
        blockers.append("safety_paper_only_missing")
    if safety.get("boot_verified") is not True:
        blockers.append("boot_not_verified")

    safety_age = _timestamp_age_seconds(safety.get("checked_at"), checked_at)
    if safety_age is None:
        blockers.append("safety_timestamp_invalid")
    elif safety_age > PARK_PUBLIC_MAX_AGE_SECONDS:
        blockers.append("persisted_snapshot_stale")

    recording = _recording_projection(
        root,
        active_pair=(session_id, revision_id),
    )
    if recording["status"] == "blocked" and recording.get("blocker_code"):
        blockers.append(str(recording["blocker_code"]))

    normalized = dict(plan.get("normalized_input") or {})
    risk = dict(plan.get("risk") or {})
    normalized_strategy_type = str(normalized.get("strategy_type") or "").lower()
    lifecycle_strategy_type = str(lifecycle.get("strategy_type") or "").lower()
    strategy_type = lifecycle_strategy_type or normalized_strategy_type
    if (
        lifecycle_strategy_type
        and normalized_strategy_type
        and lifecycle_strategy_type != normalized_strategy_type
    ):
        blockers.append("strategy_type_mismatch")
    normalized_lower_boundary = normalized.get("lower_price_boundary")
    normalized_upper_boundary = normalized.get("upper_price_boundary")
    lower_boundary = lifecycle.get("lower_price_boundary")
    upper_boundary = lifecycle.get("upper_price_boundary")
    if lower_boundary is None:
        lower_boundary = normalized_lower_boundary
    if upper_boundary is None:
        upper_boundary = normalized_upper_boundary
    try:
        boundary_mismatch = (
            lifecycle.get("lower_price_boundary") is not None
            and normalized_lower_boundary is not None
            and float(lifecycle["lower_price_boundary"]) != float(normalized_lower_boundary)
        ) or (
            lifecycle.get("upper_price_boundary") is not None
            and normalized_upper_boundary is not None
            and float(lifecycle["upper_price_boundary"]) != float(normalized_upper_boundary)
        )
    except (TypeError, ValueError, OverflowError):
        boundary_mismatch = True
    if boundary_mismatch:
        blockers.append("strategy_boundary_mismatch")
    raw_grid_count = risk.get("order_count") or normalized.get("order_count") or 0
    try:
        if isinstance(raw_grid_count, bool):
            raise ValueError("boolean is not a grid count")
        numeric_grid_count = float(raw_grid_count)
        if not math.isfinite(numeric_grid_count) or numeric_grid_count <= 0 or not numeric_grid_count.is_integer():
            raise ValueError("grid count must be a positive integer")
        grid_count = int(numeric_grid_count)
    except (TypeError, ValueError, OverflowError):
        grid_count = 0
        blockers.append("grid_geometry_invalid")
    grid_entry_range: dict[str, float] | None = None
    grid_spacing: float | None = None
    grid_rung_prices: list[float] = []
    if strategy_type == "grid":
        try:
            lower = float(lower_boundary)
            upper = float(upper_boundary)
            valid_geometry = (
                math.isfinite(lower)
                and math.isfinite(upper)
                and upper > lower
                and grid_count > 0
            )
        except (TypeError, ValueError, OverflowError):
            valid_geometry = False
            lower = upper = 0.0
        if not valid_geometry:
            blockers.append("grid_geometry_invalid")
        else:
            grid_spacing = round((upper - lower) / (grid_count + 1), 8)
            grid_entry_range = {
                "lower": round(lower + grid_spacing, 8),
                "upper": round(upper - grid_spacing, 8),
            }
            grid_rung_prices = [
                round(lower + grid_spacing * (index + 1), 8)
                for index in range(grid_count)
            ]
    strategy = {
        "active": bool(session_id and lifecycle),
        "state": lifecycle.get("state") or "IDLE_CLEAN",
        "strategy_session_id": session_id or None,
        "strategy_revision_id": revision_id or None,
        "plan_digest": expected_digest or lifecycle.get("plan_digest") or None,
        "strategy_type": lifecycle.get("strategy_type") or normalized.get("strategy_type"),
        "direction": lifecycle.get("direction") or normalized.get("direction"),
        "lower_price_boundary": lifecycle.get("lower_price_boundary") or normalized.get("lower_price_boundary"),
        "upper_price_boundary": lifecycle.get("upper_price_boundary") or normalized.get("upper_price_boundary"),
        "stop_price": lifecycle.get("stop_price") or normalized.get("stop_price"),
        "take_profit_price": lifecycle.get("take_profit_price") or normalized.get("take_profit_price"),
        "maximum_leverage": lifecycle.get("maximum_leverage") or normalized.get("maximum_leverage"),
        "maximum_acceptable_loss": lifecycle.get("maximum_acceptable_loss") or normalized.get("maximum_acceptable_loss"),
        "maximum_notional": risk.get("maximum_notional"),
        "theoretical_max_loss": risk.get("theoretical_max_loss"),
        "order_count": risk.get("order_count"),
        "selected_constraint": risk.get("selected_constraint"),
        "grid_entry_range": grid_entry_range,
        "grid_spacing": grid_spacing,
        "grid_rung_count": grid_count if strategy_type == "grid" else None,
        "grid_rung_prices": grid_rung_prices,
    }

    orders = _compact_rows(
        snapshot.get("orders"),
        (
            "order_id", "state", "side", "event", "order_type", "quantity",
            "price", "requested_price", "notional", "sl", "tp", "ts",
            "strategy_plan_id", "strategy_plan_version",
        ),
    )
    fills = _compact_rows(
        snapshot.get("fills"),
        (
            "fill_id", "order_id", "trade_id", "event", "side", "order_type",
            "quantity", "price", "cost", "realized_pnl", "ts",
            "strategy_plan_id", "strategy_plan_version",
        ),
    )
    positions = _compact_rows(
        snapshot.get("positions"),
        (
            "position_id", "trade_id", "status", "side", "remaining_units",
            "entry_price", "exit_price", "realized_pnl", "sl", "tp",
            "entry_ts", "exit_ts", "strategy_plan_id", "strategy_plan_version",
        ),
    )
    execution = _empty_execution()
    if snapshot:
        execution = {
            "engine": snapshot.get("engine"),
            "counts": {
                "accepted_orders": sum(1 for row in orders if row.get("state") == "accepted"),
                "filled_orders": sum(1 for row in orders if row.get("state") == "filled"),
                "fills": len(fills),
                "open_positions": sum(1 for row in positions if row.get("status") == "open"),
                "closed_positions": sum(1 for row in positions if row.get("status") == "closed"),
            },
            "orders": orders,
            "fills": fills,
            "positions": positions,
            "account": dict(snapshot.get("account") or {}),
            "pnl": dict(snapshot.get("pnl") or {}),
            "reconciliation": reconciliation,
        }

    unique_blockers = list(dict.fromkeys(blockers))
    return {
        "schema_version": PARK_PUBLIC_READ_MODEL_SCHEMA,
        "generated_at": checked_at.replace(microsecond=0).isoformat(),
        "status": "ok" if not unique_blockers else "blocked",
        "blockers": unique_blockers,
        "viewer": {
            "mode": "public_read_only",
            "paper_only": True,
            "control_plane": "telegram_only",
            "mutations_allowed": False,
        },
        "strategy": strategy,
        "recording": recording,
        "market": mark,
        "execution": execution,
        "safety": {
            "status": safety.get("status") or "missing",
            "checked_at": safety.get("checked_at"),
            "age_seconds": safety_age,
            "max_age_seconds": PARK_PUBLIC_MAX_AGE_SECONDS,
            "release_sha": safety.get("release_sha"),
            "tracked_tree_clean": safety.get("tracked_tree_clean"),
            "boot_verified": safety.get("boot_verified"),
            "paper_only": safety.get("paper_only"),
            "immutable_fill": safety.get("immutable_fill"),
            "supervisor_fail_closed": safety.get("supervisor_fail_closed"),
        },
    }
