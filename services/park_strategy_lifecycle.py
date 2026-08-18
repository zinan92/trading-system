"""Deterministic, side-effect-free Park strategy lifecycle authority.

The lifecycle emits auditable action plans.  It never calls a broker.  A later
execution adapter may consume a verified plan, but a boundary or blocker cannot
silently become an order mutation merely by being observed here.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping


PARK_LIFECYCLE_SCHEMA = "park-strategy-lifecycle-v1"
PARK_ACTION_PLAN_SCHEMA = "park-terminal-action-plan-v1"
PARK_LIFECYCLE_STATES = (
    "IDLE_CLEAN",
    "PLAN_CALCULATED",
    "AWAITING_CONFIRMATION",
    "ACTIVE_LOCKED",
    "BOUNDARY_TRIGGERED",
    "CLOSING",
    "RECONCILED",
    "PAUSED",
)
_TERMINAL_ACTIONS = (
    "freeze_new_entries",
    "cancel_remaining_strategy_entries",
    "close_all_strategy_owned_positions",
    "reconcile",
    "persist_closure",
    "notify_park",
    "enter_paused",
)


class ParkStrategyLifecycleError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _text(value: Any, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ParkStrategyLifecycleError("missing_identity", f"{field} is required")
    return result


def ownership_ref(strategy_session_id: str, strategy_revision_id: str) -> dict[str, str]:
    return {
        "strategy_session_id": _text(strategy_session_id, "strategy_session_id"),
        "strategy_revision_id": _text(strategy_revision_id, "strategy_revision_id"),
    }


def validate_owned_artifact(
    artifact: Mapping[str, Any],
    *,
    strategy_session_id: str,
    strategy_revision_id: str,
    artifact_type: str = "artifact",
) -> dict[str, Any]:
    expected = ownership_ref(strategy_session_id, strategy_revision_id)
    actual = {
        "strategy_session_id": str(artifact.get("strategy_session_id") or ""),
        "strategy_revision_id": str(artifact.get("strategy_revision_id") or ""),
    }
    if actual != expected:
        raise ParkStrategyLifecycleError(
            "ownership_mismatch",
            f"{artifact_type} ownership does not match the active strategy revision",
        )
    return dict(artifact)


def admit_clean_slate(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Return a typed, non-mutating admission decision."""

    blockers: list[str] = []
    if snapshot.get("reconciliation_healthy") is not True:
        blockers.append("reconciliation_unhealthy")
    for field, code in (
        ("open_positions", "open_positions"),
        ("open_or_accepted_orders", "open_or_accepted_orders"),
        ("unresolved_runtime", "unresolved_runtime"),
        ("pending_terminal_actions", "pending_terminal_actions"),
    ):
        value = snapshot.get(field, 0 if field.endswith("positions") or field.endswith("orders") else False)
        if (isinstance(value, bool) and value) or (not isinstance(value, bool) and int(value or 0) != 0):
            blockers.append(code)
    return {
        "admitted": not blockers,
        "code": "clean_slate" if not blockers else "clean_slate_blocked",
        "blockers": blockers,
        "mutations": [],
    }


def assert_immutable_revision(
    active_plan: Mapping[str, Any], proposed_plan: Mapping[str, Any]
) -> None:
    expected_session = str(active_plan.get("strategy_session_id") or "")
    expected_revision = str(active_plan.get("strategy_revision_id") or "")
    if (
        str(proposed_plan.get("strategy_session_id") or "") != expected_session
        or str(proposed_plan.get("strategy_revision_id") or "") != expected_revision
    ):
        raise ParkStrategyLifecycleError("strategy_locked", "active strategy identity is immutable")
    fields = (
        "plan_digest",
        "direction",
        "strategy_type",
        "upper_price_boundary",
        "lower_price_boundary",
        "stop_price",
        "take_profit_price",
        "maximum_leverage",
        "maximum_acceptable_loss",
    )
    changed = [field for field in fields if active_plan.get(field) != proposed_plan.get(field)]
    if changed:
        raise ParkStrategyLifecycleError(
            "strategy_locked",
            f"active strategy cannot change: {','.join(changed)}",
        )


def _append(path: Path, row: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(dict(row), sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        with path.open("ab") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        os.unlink(temp_name)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ParkStrategyLifecycleError("journal_corrupt", "lifecycle journal row is not an object")
            rows.append(value)
    return rows


class ParkStrategyLifecycleLedger:
    def __init__(self, output_root: Path) -> None:
        self.path = Path(output_root) / "park_strategy" / "lifecycle.jsonl"

    def rows(self) -> list[dict[str, Any]]:
        return _rows(self.path)

    def active_plan(self) -> dict[str, Any] | None:
        active: dict[str, Any] | None = None
        for row in self.rows():
            if row.get("event") == "plan_activated":
                active = dict(row)
            elif row.get("event") == "terminal_action_plan" and active:
                active["state"] = "PAUSED"
        return active

    def activate(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        required = ownership_ref(
            plan.get("strategy_session_id"),
            plan.get("strategy_revision_id"),
        )
        digest = _text(plan.get("plan_digest"), "plan_digest")
        existing = self.active_plan()
        if existing and existing.get("state") == "PAUSED":
            # A terminal revision is sealed and may be followed by a new
            # Park-confirmed revision; its immutable identity is not reused.
            if any(existing.get(key) != value for key, value in required.items()):
                existing = None
            else:
                raise ParkStrategyLifecycleError(
                    "strategy_sealed",
                    "a sealed strategy revision cannot be activated again; create a new session and revision",
                )
        if existing:
            assert_immutable_revision(existing, {**existing, **dict(plan), **required})
            return dict(existing)
        row = {
            "schema_version": PARK_LIFECYCLE_SCHEMA,
            "event": "plan_activated",
            **required,
            "plan_digest": digest,
            "state": "ACTIVE_LOCKED",
            **{key: plan.get(key) for key in (
                "direction", "strategy_type", "upper_price_boundary", "lower_price_boundary",
                "stop_price", "take_profit_price", "maximum_leverage", "maximum_acceptable_loss",
            ) if key in plan},
        }
        _append(self.path, row)
        return dict(row)
    def boundary_action_plan(
        self,
        *,
        strategy_session_id: str,
        strategy_revision_id: str,
        boundary: str,
        observed_price: float,
        trusted_market: bool,
        fresh_tick: bool,
    ) -> dict[str, Any]:
        identity = ownership_ref(strategy_session_id, strategy_revision_id)
        active = self.active_plan()
        if not active or any(active.get(key) != value for key, value in identity.items()):
            raise ParkStrategyLifecycleError("stale_strategy", "boundary does not match the active strategy")
        if boundary not in {"upper", "lower"}:
            raise ParkStrategyLifecycleError("invalid_boundary", "boundary must be upper or lower")
        if not trusted_market or not fresh_tick:
            raise ParkStrategyLifecycleError("market_not_authoritative", "boundary requires trusted fresh market")
        existing = next(
            (row for row in self.rows() if row.get("event") == "terminal_action_plan" and all(row.get(k) == v for k, v in identity.items())),
            None,
        )
        if existing:
            return dict(existing)
        seed = f"{identity['strategy_session_id']}|{identity['strategy_revision_id']}|{boundary}|{observed_price}"
        action_plan_id = "park-terminal-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
        ordered_actions = list(_TERMINAL_ACTIONS)
        position_authority = "close_strategy_owned_positions"
        row = {
            "schema_version": PARK_ACTION_PLAN_SCHEMA,
            "event": "terminal_action_plan",
            **identity,
            "action_plan_id": action_plan_id,
            "boundary": boundary,
            "observed_price": float(observed_price),
            "authority": "exact_park_confirmed_boundary",
            "state": "BOUNDARY_TRIGGERED",
            "ordered_actions": ordered_actions,
            "position_authority": position_authority,
            "automatic_reopen": False,
            "direction_inference": False,
        }
        _append(self.path, row)
        return dict(row)

    def terminal_action_plan(
        self,
        *,
        strategy_session_id: str,
        strategy_revision_id: str,
        trigger: str,
        observed_price: float,
        trusted_market: bool,
        fresh_tick: bool,
    ) -> dict[str, Any]:
        """Record a non-boundary terminal trigger such as DCA TP/SL."""

        identity = ownership_ref(strategy_session_id, strategy_revision_id)
        active = self.active_plan()
        if not active or any(active.get(key) != value for key, value in identity.items()):
            raise ParkStrategyLifecycleError("stale_strategy", "terminal trigger does not match the active strategy")
        if not trusted_market or not fresh_tick:
            raise ParkStrategyLifecycleError("market_not_authoritative", "terminal trigger requires trusted fresh market")
        existing = next(
            (row for row in self.rows() if row.get("event") == "terminal_action_plan" and all(row.get(k) == v for k, v in identity.items())),
            None,
        )
        if existing:
            return dict(existing)
        seed = f"{identity['strategy_session_id']}|{identity['strategy_revision_id']}|{trigger}|{observed_price}"
        row = {
            "schema_version": PARK_ACTION_PLAN_SCHEMA,
            "event": "terminal_action_plan",
            **identity,
            "action_plan_id": "park-terminal-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24],
            "trigger": str(trigger),
            "boundary": None,
            "observed_price": float(observed_price),
            "authority": "exact_park_confirmed_terminal",
            "state": "TERMINAL_TRIGGERED",
            "ordered_actions": list(_TERMINAL_ACTIONS),
            "position_authority": "close_strategy_owned_positions",
            "automatic_reopen": False,
            "direction_inference": False,
        }
        _append(self.path, row)
        return dict(row)

    def structural_blocker(
        self,
        *,
        strategy_session_id: str,
        strategy_revision_id: str,
        code: str,
        detail: str = "",
    ) -> dict[str, Any]:
        identity = ownership_ref(strategy_session_id, strategy_revision_id)
        row = {
            "schema_version": PARK_LIFECYCLE_SCHEMA,
            "event": "structural_blocker",
            **identity,
            "code": _text(code, "code"),
            "detail": str(detail),
            "entry_authority": "freeze_new_exposure",
            "position_authority": "preserve_safe_protection_no_automatic_flatten",
            "next_action": "notify_park_and_wait",
        }
        _append(self.path, row)
        return dict(row)
