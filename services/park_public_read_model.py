"""Side-effect-free public projection of persisted Park Paper facts."""

from __future__ import annotations

import json
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

    normalized = dict(plan.get("normalized_input") or {})
    risk = dict(plan.get("risk") or {})
    strategy_type = str(normalized.get("strategy_type") or "").lower()
    lower_boundary = normalized.get("lower_price_boundary")
    upper_boundary = normalized.get("upper_price_boundary")
    grid_count = int(risk.get("order_count") or normalized.get("order_count") or 0)
    grid_entry_range: dict[str, float] | None = None
    grid_spacing: float | None = None
    grid_rung_prices: list[float] = []
    if (
        strategy_type == "grid"
        and lower_boundary is not None
        and upper_boundary is not None
        and grid_count > 0
    ):
        grid_spacing = round(
            (float(upper_boundary) - float(lower_boundary)) / (grid_count + 1),
            8,
        )
        grid_entry_range = {
            "lower": round(float(lower_boundary) + grid_spacing, 8),
            "upper": round(float(upper_boundary) - grid_spacing, 8),
        }
        grid_rung_prices = [
            round(float(lower_boundary) + grid_spacing * (index + 1), 8)
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
