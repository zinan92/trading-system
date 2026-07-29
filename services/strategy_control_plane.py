"""Single-production-plan control plane with a non-destructive DualTrack migration.

This module deliberately owns *selection and attribution*, not execution.  The
legacy human/machine files remain immutable source records and are exposed as
same-schema proposals until a production plan is locked.
"""

from __future__ import annotations

import hashlib
import fcntl
import json
import math
import os
import threading
from bisect import bisect_right
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from services.execution_plugin_composition import build_configured_execution_engine_adapter
from services.dca_execution_lifecycle import DcaPaperLifecycle
from services.dca_plan import (
    build_dca_entry_commands,
    build_dca_preview,
    build_dca_strategy_plan,
    dca_preview_id,
)
from services.dualtrack_clock import parse_utc
from services.dualtrack_config import dualtrack_config
from services.dualtrack_store import DualTrackPlanStore
from services.control_audit import append_control_event, build_control_event, read_last_control_event
from services.grid_sizing import (
    AdaptiveGridInputError,
    GRID_STYLES,
    build_grid_preview,
    number_or as _number_or,
    preview_id as _grid_preview_id,
    validate_market as _validate_market,
    positive_number as _positive_number,
)
from services.grid_marketability import (
    market_is_on_non_entry_side,
    market_outside_range_requires_blocker,
)
from services.grid_range_adjustment import (
    build_dragged_range,
    build_range_extension,
    range_adjustment_steps,
)
from services.journal_store import load_json, write_json
from services.production_accounting import normalize_nautilus_snapshot_for_accounting
from services.risk_policy_composition import (
    build_risk_decision_store,
    compose_grid_risk_policy,
)
from services.risk_port import (
    RiskDecisionPort,
    RiskDecisionStorePort,
    action_class_for_command,
    build_paper_safe_action_market_gate,
    build_grid_risk_request,
    canonical_market_risk_state,
    normalize_manual_order_command,
    require_exposure_permission,
)
from services.strategy_plan_execution import build_plan_grid_entry_commands


PROPOSAL_SCHEMA = "strategy-plan-proposal-v1"
PLAN_SCHEMA = "strategy-plan-v1"
PLAN_FIELDS = ("direction", "style", "range", "key_levels", "grid", "signal", "tp_sl", "risk_budget", "intraday_rules")
FIELD_SOURCES = {"human", "ai", "confirmed"}
_CONTROL_LOCK = threading.RLock()
_PROCESS_LOCK_STATE = threading.local()
MANUAL_RANGE_RISK_ACK_SCHEMA = "grid-range-risk-ack-v1"
PREPARED_START_SCHEMA = "strategy-prepared-start-v1"
PREPARED_START_TTL_SECONDS = 300
DCA_RISK_ACK_SCHEMA = "dca-risk-ack-v1"
PAPER_EXECUTION_TICK_MAX_AGE_SECONDS = 180
MANUAL_RANGE_RISK_OVERRIDABLE_BLOCKERS = {
    "candidate_grid_count_out_of_bounds",
    "grid_profit_target_not_met",
    "leverage_limit_exceeded",
    "market_price_outside_range",
    "projected_leverage_exceeded",
    "projected_margin_exceeded",
    "required_leverage_mismatch",
}


def _market_inside_source_envelope(
    grid_range: dict[str, Any],
    price: float,
) -> bool:
    """Validate ticks against the source envelope behind an executable Range."""

    source = (
        grid_range.get("source_envelope")
        if isinstance(grid_range.get("source_envelope"), dict)
        else grid_range
    )
    low = _positive_number(source.get("low"), "grid envelope low")
    high = _positive_number(source.get("high"), "grid envelope high")
    return low <= price <= high


def paper_safe_action_market_mark_is_trusted(
    market: dict[str, Any] | None,
    *,
    expected_provider: str = "",
) -> bool:
    """Check provenance before a server mark may price a paper safe action."""

    source = market if isinstance(market, dict) else {}
    provider = str(source.get("provider") or "")
    return (
        source.get("is_synthetic") is False
        and bool(provider)
        and source.get("source_mode") in {"requested_symbol", provider}
        and (not expected_provider or provider == expected_provider)
    )


@contextmanager
def production_mutation_lock(output_root: Path):
    """Serialize production mutations across threads and local processes."""

    root = Path(output_root)
    lock_path = root / "dualtrack" / "strategy_control" / ".mutation.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    key = str(lock_path.resolve())
    with _CONTROL_LOCK:
        held = getattr(_PROCESS_LOCK_STATE, "held", None)
        if held is None:
            held = {}
            _PROCESS_LOCK_STATE.held = held
        if key in held:
            held[key][1] += 1
            try:
                yield
            finally:
                held[key][1] -= 1
            return
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            held[key] = [descriptor, 1]
            yield
        finally:
            held.pop(key, None)
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def resolve_paper_safe_action_pricing(
    market: dict[str, Any] | None,
    execution_snapshot: dict[str, Any],
    *,
    requested_at: str | None,
    command: dict[str, Any] | None = None,
    engine_name: str = "legacy_paper",
    last_market_event: dict[str, Any] | None = None,
    allow_market_mark: bool = True,
) -> dict[str, Any]:
    """Resolve paper-only exit pricing without trusting a browser or fake freshness."""

    source = market if isinstance(market, dict) else {}
    if source.get("is_synthetic") is True:
        raise ValueError("paper safe action cannot use synthetic market data")
    requested = parse_utc(requested_at)
    targets = _safe_action_target_positions(execution_snapshot, command)
    if not targets:
        raise ValueError("paper safe action requires an open position")
    latest_entry = _latest_position_entry_time(targets)
    fresh = (
        allow_market_mark
        and source.get("status") in {"ready", "derived"}
        and source.get("fresh") is True
        and source.get("is_synthetic") is False
    )

    if str(engine_name or "") == "nautilus_paper" and not fresh:
        event = last_market_event if isinstance(last_market_event, dict) else {}
        if event.get("is_synthetic") is False:
            candidate = _safe_action_price_candidate(
                price=event.get("price"),
                timestamp=event.get("ts_event"),
                provider=event.get("provider") or event.get("source"),
                requested=requested,
                latest_entry=latest_entry,
            )
            if candidate is not None:
                return {
                    **candidate,
                    "pricing_source": "last_known_execution_event",
                    "command_timestamp": requested.isoformat(),
                }

    market_candidate = None
    if allow_market_mark and source.get("is_synthetic") is False:
        market_candidate = _safe_action_price_candidate(
            price=source.get("latest_close"),
            timestamp=source.get("latest_timestamp"),
            provider=source.get("provider"),
            requested=requested,
            latest_entry=None if fresh else latest_entry,
        )
    if market_candidate is not None:
        return {
            **market_candidate,
            "pricing_source": "fresh_server_mark" if fresh else "last_known_server_mark",
            "command_timestamp": requested.isoformat(),
        }

    ledger_candidate = _latest_execution_fill_candidate(
        execution_snapshot,
        targets=targets,
        requested=requested,
    )
    if ledger_candidate is not None:
        return {
            **ledger_candidate,
            "pricing_source": "last_known_execution_fill",
            "command_timestamp": requested.isoformat(),
        }

    position_candidate = _latest_position_cost_candidate(targets, requested=requested)
    if position_candidate is not None:
        return {
            **position_candidate,
            "pricing_source": "position_cost_basis",
            "command_timestamp": requested.isoformat(),
        }
    raise ValueError("paper safe action has no trusted execution-ledger price")


def _safe_action_target_positions(
    snapshot: dict[str, Any],
    command: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    positions = [
        dict(row)
        for row in snapshot.get("positions") or []
        if isinstance(row, dict) and str(row.get("status") or "") == "open"
    ]
    body = command if isinstance(command, dict) else {}
    trade_ids = {
        str(body.get("trade_id") or ""),
        str(body.get("target_command_id") or ""),
    } - {""}
    position_ids = {
        str(body.get("position_id") or ""),
        str(body.get("target_position_id") or ""),
    } - {""}
    if not trade_ids and not position_ids:
        return positions
    matched = []
    if trade_ids:
        matched = [row for row in positions if str(row.get("trade_id") or "") in trade_ids]
    if not matched and position_ids:
        matched = [row for row in positions if str(row.get("position_id") or "") in position_ids]
    if len(matched) != 1:
        raise ValueError("paper safe action could not resolve exactly one open position")
    return matched


def _latest_position_entry_time(positions: list[dict[str, Any]]) -> datetime | None:
    values = [
        parsed
        for row in positions
        if (parsed := _optional_utc(row.get("entry_ts"))) is not None
    ]
    return max(values) if values else None


def _safe_action_price_candidate(
    *,
    price: Any,
    timestamp: Any,
    provider: Any,
    requested: datetime,
    latest_entry: datetime | None,
) -> dict[str, Any] | None:
    value = _optional_positive(price)
    priced_at = _optional_utc(timestamp)
    provenance = str(provider or "").strip()
    if value is None or priced_at is None or not provenance or priced_at > requested:
        return None
    if latest_entry is not None and priced_at < latest_entry:
        return None
    return {
        "price": value,
        "pricing_timestamp": priced_at.isoformat(),
        "pricing_provider": provenance,
    }


def _latest_execution_fill_candidate(
    snapshot: dict[str, Any],
    *,
    targets: list[dict[str, Any]],
    requested: datetime,
) -> dict[str, Any] | None:
    target_trade_ids = {
        str(row.get("trade_id") or "") for row in targets if row.get("trade_id") not in (None, "")
    }
    target_position_ids = {
        str(row.get("position_id") or "")
        for row in targets
        if row.get("position_id") not in (None, "")
    }
    candidates: list[tuple[datetime, float]] = []
    for row in snapshot.get("fills") or []:
        if not isinstance(row, dict):
            continue
        if target_trade_ids and str(row.get("trade_id") or "") not in target_trade_ids:
            continue
        if (
            not target_trade_ids
            and target_position_ids
            and str(row.get("position_id") or "") not in target_position_ids
        ):
            continue
        priced_at = _optional_utc(row.get("ts"))
        price = _optional_positive(row.get("price"))
        if priced_at is None or price is None or priced_at > requested:
            continue
        candidates.append((priced_at, price))
    if not candidates:
        return None
    priced_at, price = max(candidates, key=lambda item: item[0])
    return {
        "price": price,
        "pricing_timestamp": priced_at.isoformat(),
        "pricing_provider": "paper_execution_ledger",
    }


def _latest_position_cost_candidate(
    positions: list[dict[str, Any]],
    *,
    requested: datetime,
) -> dict[str, Any] | None:
    candidates: list[tuple[datetime, float]] = []
    for row in positions:
        priced_at = _optional_utc(row.get("entry_ts"))
        price = _optional_positive(row.get("entry_price"))
        if priced_at is None or price is None or priced_at > requested:
            continue
        candidates.append((priced_at, price))
    if not candidates:
        return None
    priced_at, price = max(candidates, key=lambda item: item[0])
    return {
        "price": price,
        "pricing_timestamp": priced_at.isoformat(),
        "pricing_provider": "paper_execution_ledger",
    }


def _optional_positive(value: Any) -> float | None:
    try:
        return _positive_number(value, "paper safe action price")
    except (TypeError, ValueError):
        return None


def _optional_utc(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return parse_utc(value)
    except (TypeError, ValueError):
        return None


def last_paper_execution_market_event(adapter, cycle_id: str) -> dict[str, Any] | None:
    authoritative = getattr(adapter, "authoritative", adapter)
    reader = getattr(authoritative, "last_market_event", None)
    if not callable(reader):
        return None
    event = reader(cycle_id)
    return dict(event) if isinstance(event, dict) else None


def settle_paper_safe_action_commands(adapter, cycle_id: str) -> dict[str, Any] | None:
    """Persist safe actions without requiring another market-data event."""

    authoritative_result: dict[str, Any] | None = None
    if str(getattr(adapter, "name", "")) == "nautilus_paper":
        authoritative = getattr(adapter, "authoritative", adapter)
        flush = getattr(authoritative, "flush", None)
        if not callable(flush):
            raise ValueError("Nautilus paper safe action requires replay flush support")
        authoritative_result = flush(cycle_id)

    shadow_result: dict[str, Any] | None = None
    flush_shadow = getattr(adapter, "flush_shadow", None)
    if callable(flush_shadow):
        shadow_result = flush_shadow(cycle_id)

    if authoritative_result is not None:
        return authoritative_result
    if shadow_result is not None:
        return {"status": "shadow_flushed", "shadow": shadow_result}
    return None


class StrategyControlPlane:
    def __init__(
        self,
        output_root: Path,
        *,
        risk_port: RiskDecisionPort | None = None,
        risk_store: RiskDecisionStorePort | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.root = self.output_root / "dualtrack" / "strategy_control"
        self.config = dualtrack_config()
        if risk_port is None:
            runtime = compose_grid_risk_policy(self.config)
            self.risk_port = runtime.port
        else:
            if not isinstance(risk_port, RiskDecisionPort):
                raise TypeError("risk_port does not implement RiskDecisionPort")
            self.risk_port = risk_port
        self.risk_store = risk_store or build_risk_decision_store(self.output_root)
        if not isinstance(self.risk_store, RiskDecisionStorePort):
            raise TypeError("risk_store does not implement RiskDecisionStorePort")

    def upsert_proposal(self, payload: dict[str, Any], *, now: str | None = None) -> dict[str, Any]:
        proposal = normalize_proposal(payload, now=now)
        path = self._proposals_path(proposal["cycle_id"])
        rows = load_json(path)
        rows = [row for row in rows if str(row.get("proposal_id")) != proposal["proposal_id"]]
        rows.append(proposal)
        write_json(path, rows)
        return proposal

    def proposals(self, cycle_id: str) -> list[dict[str, Any]]:
        rows = [row for row in load_json(self._proposals_path(cycle_id)) if isinstance(row, dict)]
        if rows:
            return rows
        # Compatibility read: historical records are never moved or rewritten.
        store = DualTrackPlanStore(self.output_root)
        legacy = []
        for source in ("human", "ai"):
            row = store.load_plan(cycle_id, source)
            if row:
                legacy.append(normalize_proposal({**row, "source": source}, legacy=True))
        return legacy

    def proposal_diff(self, cycle_id: str) -> dict[str, Any]:
        proposals = self.proposals(cycle_id)
        by_source = {str(row.get("source")): row for row in proposals}
        human, ai = by_source.get("human"), by_source.get("ai")
        fields: dict[str, dict[str, Any]] = {}
        for key in PLAN_FIELDS:
            left = human.get(key) if human else None
            right = ai.get(key) if ai else None
            fields[key] = {"human": left, "ai": right, "different": human is not None and ai is not None and left != right}
        return {"cycle_id": cycle_id, "available_sources": sorted(by_source), "fields": fields}

    def lock_production_plan(
        self,
        cycle_id: str,
        *,
        selected_proposal_id: str,
        field_sources: dict[str, str] | None = None,
        now: str | None = None,
    ) -> dict[str, Any]:
        with production_mutation_lock(self.output_root):
            return self._lock_production_plan(
                cycle_id,
                selected_proposal_id=selected_proposal_id,
                field_sources=field_sources,
                now=now,
            )

    def _lock_production_plan(
        self,
        cycle_id: str,
        *,
        selected_proposal_id: str,
        field_sources: dict[str, str] | None = None,
        now: str | None = None,
    ) -> dict[str, Any]:
        proposals = self.proposals(cycle_id)
        selected = next((row for row in proposals if row.get("proposal_id") == selected_proposal_id), None)
        if not selected:
            raise ValueError("selected proposal does not exist")
        sources = _field_sources(field_sources, selected_source=str(selected["source"]))
        existing = self.active_plan(cycle_id)
        version = int(existing.get("version") or 0) + 1 if existing else 1
        plan = {
            "schema_version": PLAN_SCHEMA,
            "strategy_plan_id": _plan_id(cycle_id, version, selected["proposal_id"]),
            "cycle_id": cycle_id,
            "version": version,
            "status": "active",
            "locked_at": _timestamp(now),
            "source_proposal_ids": [selected["proposal_id"]],
            "field_sources": sources,
            **{key: selected.get(key) for key in PLAN_FIELDS},
        }
        self._activate_plan(plan)
        return plan

    def active_plan(self, cycle_id: str) -> dict[str, Any] | None:
        plans = [row for row in load_json(self._plans_path(cycle_id)) if isinstance(row, dict)]
        active = [row for row in plans if row.get("status") == "active"]
        return active[-1] if active else None

    def latest_plan(self, cycle_id: str) -> dict[str, Any] | None:
        """Return the latest archived plan even after the cycle was closed."""

        plans = [row for row in load_json(self._plans_path(cycle_id)) if isinstance(row, dict)]
        if not plans:
            return None
        return max(
            plans,
            key=lambda row: (int(row.get("version") or 0), str(row.get("locked_at") or "")),
        )

    def ensure_compatible_active_plan(self, cycle_id: str, *, as_of: str | None = None) -> dict[str, Any] | None:
        with production_mutation_lock(self.output_root):
            return self._ensure_compatible_active_plan(cycle_id, as_of=as_of)

    def _ensure_compatible_active_plan(self, cycle_id: str, *, as_of: str | None = None) -> dict[str, Any] | None:
        active = self.active_plan(cycle_id)
        if active:
            return active
        proposals = self.proposals(cycle_id)
        # Old precedence remains explicit: human locked proposal, then ai; no mixed plan.
        selected = next((row for row in proposals if row.get("source") == "human" and row.get("legacy_status") == "locked"), None)
        selected = selected or next((row for row in proposals if row.get("source") == "ai"), None)
        if not selected:
            return None
        return self._lock_production_plan(
            cycle_id,
            selected_proposal_id=str(selected["proposal_id"]),
            field_sources={key: str(selected["source"]) for key in PLAN_FIELDS},
            now=as_of,
        )

    def read_model(self, cycle_id: str, *, as_of: str | None = None) -> dict[str, Any]:
        proposals = self.proposals(cycle_id)
        plan = self.active_plan(cycle_id)
        dca_lifecycle = None
        if isinstance(plan, dict) and plan.get("strategy_type") == "dca":
            try:
                dca_lifecycle = DcaPaperLifecycle(self.output_root, None).read_state(plan)
            except ValueError as exc:
                # Historical plan records stay immutable, but a malformed
                # legacy DCA identity must not take the dashboard down.
                dca_lifecycle = {
                    "status": "unavailable",
                    "warning": f"dca_lifecycle_read_skipped:{exc}",
                }
        return {
            "schema_version": "strategy-production-console-v1",
            "cycle_id": cycle_id,
            "production_plan": plan,
            "production_plan_history": [
                dict(row)
                for row in load_json(self._plans_path(cycle_id))
                if isinstance(row, dict)
            ],
            "proposals": proposals,
            "proposal_diff": self.proposal_diff(cycle_id),
            "migration": {
                "legacy_compatible": bool(proposals),
                "legacy_migration_required": bool(proposals) and plan is None,
                "legacy_records_preserved": True,
                "legacy_execution_shadow_separate": True,
            },
            "runtime": self.runtime_state(cycle_id, now=as_of),
            "dca_lifecycle": dca_lifecycle,
        }

    def runtime_state(
        self,
        cycle_id: str,
        *,
        now: str | datetime | None = None,
    ) -> dict[str, Any]:
        rows = load_json(self.root / "runtime.json")
        row = rows[-1] if rows else {}
        stored_cycle_id = str(row.get("cycle_id") or "")
        if stored_cycle_id and stored_cycle_id != cycle_id:
            previous_actual_state = str(row.get("actual_state") or row.get("desired_state") or "stopped")
            previous_accepted_order_count = int(row.get("accepted_order_count") or 0)
            previous_runtime_unresolved = (
                previous_actual_state in {"starting", "running", "replanning", "stopping"}
                or previous_accepted_order_count > 0
            )
            return {
                "desired_state": "stopped",
                "actual_state": "stopped",
                "cycle_id": cycle_id,
                "updated_at": row.get("updated_at"),
                "statistics_baseline_at": None,
                "strategy_plan_id": None,
                "strategy_plan_version": None,
                "strategy_type": None,
                "preview_id": None,
                "risk_decision_id": None,
                "risk_policy_id": None,
                "accepted_order_count": 0,
                "accepted_order_count_known": True,
                "transition_owner": None,
                "last_action": None,
                "last_error": None,
                "stale_cycle": True,
                "previous_cycle_id": stored_cycle_id,
                "previous_actual_state": previous_actual_state,
                "previous_accepted_order_count": previous_accepted_order_count,
                "previous_strategy_plan_id": row.get("strategy_plan_id"),
                "previous_strategy_plan_version": row.get("strategy_plan_version"),
                "previous_runtime_unresolved": previous_runtime_unresolved,
                "last_control_event": self._last_control_event(),
                "execution_tick_health": {"status": "not_applicable"},
            }
        state = {
            "desired_state": str(row.get("desired_state") or "stopped"),
            "actual_state": str(row.get("actual_state") or row.get("desired_state") or "stopped"),
            "cycle_id": str(row.get("cycle_id") or cycle_id),
            "updated_at": row.get("updated_at"),
            "statistics_baseline_at": row.get("statistics_baseline_at"),
            "strategy_plan_id": row.get("strategy_plan_id"),
            "strategy_plan_version": row.get("strategy_plan_version"),
            "strategy_type": row.get("strategy_type"),
            "preview_id": row.get("preview_id"),
            "risk_decision_id": row.get("risk_decision_id"),
            "risk_policy_id": row.get("risk_policy_id"),
            "accepted_order_count": int(row.get("accepted_order_count") or 0),
            "accepted_order_count_known": bool(row.get("accepted_order_count_known", True)),
            "transition_owner": row.get("transition_owner"),
            "last_action": row.get("last_action"),
            "last_error": row.get("last_error"),
            "dca_lifecycle_status": row.get("dca_lifecycle_status"),
            "dca_lifecycle_path": row.get("dca_lifecycle_path"),
            "stale_cycle": False,
            "previous_cycle_id": None,
            "previous_actual_state": None,
            "previous_accepted_order_count": 0,
            "previous_strategy_plan_id": None,
            "previous_strategy_plan_version": None,
            "previous_runtime_unresolved": False,
            "last_control_event": self._last_control_event(),
        }
        execution_engine = self.config.get("execution_engine") or {}
        if (
            state["actual_state"] == "running"
            and str(execution_engine.get("authoritative") or "") == "nautilus_paper"
        ):
            state["execution_tick_health"] = self.paper_execution_tick_health(
                cycle_id,
                now=now,
            )
        else:
            state["execution_tick_health"] = {"status": "not_applicable"}
        state["execution_tick_failure"] = self.paper_execution_tick_failure(cycle_id)
        return state

    def _last_control_event(self) -> dict[str, Any] | None:
        try:
            return read_last_control_event(self.output_root)
        except OSError:
            return None

    def runtime_configured(self) -> bool:
        return (self.root / "runtime.json").exists()

    def paper_execution_tick_health(
        self,
        cycle_id: str,
        *,
        now: str | datetime | None = None,
    ) -> dict[str, Any]:
        """Return the fresh-heartbeat contract for Nautilus Paper execution.

        A submitted Paper order is not executable merely because it was written
        to the ledger.  The live-tick process must have recently observed the
        current cycle before the console can create new exposure.
        """

        checked_at = parse_utc(now)
        rows = load_json(
            self.output_root / "dualtrack" / "runner" / f"{cycle_id}.json"
        )
        latest = next(
            (dict(row) for row in reversed(rows) if isinstance(row, dict)),
            {},
        )
        timestamp = _optional_utc(latest.get("ts"))
        if timestamp is None:
            return {
                "status": "blocked",
                "code": "paper_execution_tick_unavailable",
                "reason": "heartbeat_missing",
                "latest_ts": latest.get("ts"),
                "max_age_seconds": PAPER_EXECUTION_TICK_MAX_AGE_SECONDS,
            }
        age_seconds = (checked_at - timestamp).total_seconds()
        if age_seconds < 0 or age_seconds > PAPER_EXECUTION_TICK_MAX_AGE_SECONDS:
            return {
                "status": "blocked",
                "code": "paper_execution_tick_unavailable",
                "reason": "heartbeat_stale" if age_seconds >= 0 else "heartbeat_in_future",
                "latest_ts": timestamp.isoformat(),
                "age_seconds": round(age_seconds, 3),
                "max_age_seconds": PAPER_EXECUTION_TICK_MAX_AGE_SECONDS,
                "event": latest.get("event"),
            }
        return {
            "status": "ready",
            "code": "",
            "reason": "heartbeat_fresh",
            "latest_ts": timestamp.isoformat(),
            "age_seconds": round(age_seconds, 3),
            "max_age_seconds": PAPER_EXECUTION_TICK_MAX_AGE_SECONDS,
            "event": latest.get("event"),
        }

    def paper_execution_tick_failure(self, cycle_id: str) -> dict[str, Any]:
        """Return the latest unresolved tick failure without promoting it to health."""

        rows = load_json(self.root / "live_tick_failure.json")
        failure = next(
            (dict(row) for row in reversed(rows) if isinstance(row, dict)),
            {},
        )
        if not failure or str(failure.get("status") or "") != "failed":
            return {"status": "none"}
        failure_cycle_id = str(failure.get("cycle_id") or "")
        if failure_cycle_id and failure_cycle_id != cycle_id:
            return {"status": "none"}
        failure_at = _optional_utc(failure.get("recorded_at"))
        heartbeat_rows = load_json(
            self.output_root / "dualtrack" / "runner" / f"{cycle_id}.json"
        )
        latest_heartbeat = next(
            (dict(row) for row in reversed(heartbeat_rows) if isinstance(row, dict)),
            {},
        )
        heartbeat_at = _optional_utc(latest_heartbeat.get("ts"))
        if failure_at and heartbeat_at and heartbeat_at >= failure_at:
            return {
                "status": "resolved",
                "failure_phase": str(failure.get("failure_phase") or "unknown"),
                "recorded_at": failure_at.isoformat(),
                "resolved_at": heartbeat_at.isoformat(),
            }
        return {
            "status": "failed",
            "failure_phase": str(failure.get("failure_phase") or "unknown"),
            "next_action": str(failure.get("next_action") or ""),
            "recorded_at": failure.get("recorded_at"),
            "heartbeat_written": failure.get("heartbeat_written") is True,
            "error": dict(failure.get("error") or {}) if isinstance(failure.get("error"), dict) else {},
        }

    def _require_paper_execution_tick(
        self,
        cycle_id: str,
        *,
        adapter,
        now: str | datetime | None,
    ) -> None:
        execution_engine = self.config.get("execution_engine") or {}
        if (
            str(execution_engine.get("authoritative") or "")
            != "nautilus_paper"
            or str(getattr(adapter, "name", "")) != "nautilus_paper"
        ):
            return
        health = self.paper_execution_tick_health(cycle_id, now=now)
        if health["status"] != "ready":
            raise ValueError(
                "paper_execution_tick_unavailable"
                f":{health.get('reason') or 'unknown'}"
            )

    def persisted_runtime_state(self) -> dict[str, Any]:
        """Return the stored runtime row without current-cycle masking.

        ``runtime_state(cycle_id)`` deliberately presents an old cycle as
        stopped.  Rollover needs the underlying row so it can distinguish an
        operator stop from a strategy that was running when the cycle ended.
        """

        rows = load_json(self.root / "runtime.json")
        row = dict(rows[-1]) if rows and isinstance(rows[-1], dict) else {}
        return {
            **row,
            "cycle_id": str(row.get("cycle_id") or ""),
            "desired_state": str(row.get("desired_state") or "stopped"),
            "actual_state": str(row.get("actual_state") or row.get("desired_state") or "stopped"),
            "accepted_order_count": int(row.get("accepted_order_count") or 0),
            "accepted_order_count_known": bool(row.get("accepted_order_count_known", True)),
            "transition_owner": row.get("transition_owner"),
        }

    def adopt_cycle_handoff(
        self,
        previous_cycle_id: str,
        current_cycle_id: str,
        *,
        expected_runtime_updated_at: str,
        handoff: dict[str, Any],
        now: str,
    ) -> dict[str, Any]:
        """Move Paper runtime ownership after a verified identity-preserving handoff."""

        persisted = self.persisted_runtime_state()
        previous_plan_id = str(persisted.get("strategy_plan_id") or "")
        current_plan = self.active_plan(current_cycle_id)
        if (
            str(persisted.get("cycle_id") or "") != previous_cycle_id
            or str(persisted.get("desired_state") or "") != "running"
            or str(persisted.get("actual_state") or "") != "running"
            or str(persisted.get("updated_at") or "") != str(expected_runtime_updated_at or "")
        ):
            raise RuntimeError("Paper runtime changed before cycle handoff")
        if not isinstance(current_plan, dict):
            raise RuntimeError("current cycle active StrategyPlan is missing")
        if (
            str(current_plan.get("takeover_from_strategy_plan_id") or "")
            != previous_plan_id
        ):
            raise RuntimeError("current cycle StrategyPlan did not declare the running plan takeover")
        if (
            str(handoff.get("status") or "") != "verified"
            or handoff.get("identity_preserved") is not True
            or str(handoff.get("previous_cycle_id") or "") != previous_cycle_id
            or str(handoff.get("current_cycle_id") or "") != current_cycle_id
            or str(handoff.get("current_strategy_plan_id") or "")
            != str(current_plan.get("strategy_plan_id") or "")
        ):
            raise RuntimeError("Paper cycle handoff receipt is not verified")
        accepted = list(handoff.get("target_accepted_order_ids") or [])
        row = {
            **persisted,
            "cycle_id": current_cycle_id,
            "desired_state": "running",
            "actual_state": "running",
            "updated_at": _timestamp(now),
            "statistics_baseline_at": _timestamp(now),
            "strategy_plan_id": current_plan["strategy_plan_id"],
            "strategy_plan_version": current_plan["version"],
            "strategy_type": current_plan.get("strategy_type"),
            "accepted_order_count": len(accepted),
            "accepted_order_count_known": True,
            "transition_owner": None,
            "last_action": "cycle_handoff",
            "last_error": None,
            "rollover_handoff": {
                "previous_cycle_id": previous_cycle_id,
                "previous_strategy_plan_id": previous_plan_id,
                "receipt_status": "verified",
                "identity_preserved": True,
            },
        }
        self._write_runtime(row)
        return row

    def _assert_rollover_start_guard(self, body: dict[str, Any]) -> None:
        guard = body.get("rollover_guard")
        if not isinstance(guard, dict):
            return
        expected_cycle_id = str(guard.get("previous_cycle_id") or "")
        expected_updated_at = str(guard.get("expected_runtime_updated_at") or "")
        persisted = self.persisted_runtime_state()
        if (
            not expected_cycle_id
            or not expected_updated_at
            or persisted.get("cycle_id") != expected_cycle_id
            or persisted.get("desired_state") != "stopped"
            or persisted.get("actual_state") != "stopped"
            or str(persisted.get("updated_at") or "") != expected_updated_at
        ):
            raise ValueError("paper runtime changed before rollover start")

    def _assert_no_unresolved_prior_cycle_runtime(self, cycle_id: str) -> None:
        """Forbid a new start while a prior-cycle Paper runtime remains active."""

        persisted = self.persisted_runtime_state()
        persisted_cycle_id = str(persisted.get("cycle_id") or "")
        if not persisted_cycle_id or persisted_cycle_id == cycle_id:
            return
        actual_state = str(persisted.get("actual_state") or persisted.get("desired_state") or "stopped")
        accepted_order_count = int(persisted.get("accepted_order_count") or 0)
        if actual_state in {"starting", "running", "replanning", "stopping"} or accepted_order_count > 0:
            raise ValueError(
                "previous_cycle_paper_state_unresolved"
                f":{persisted_cycle_id}:{actual_state}:{accepted_order_count}"
            )

    def preview(
        self,
        cycle_id: str,
        payload: dict[str, Any] | None = None,
        *,
        market: dict[str, Any],
        account: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if str((payload or {}).get("strategy_type") or "grid").lower() == "dca":
            preview = build_dca_preview(
                cycle_id,
                payload,
                market=market,
                account=account,
                config=self.config,
            )
            preview["manual_confirmation"] = _dca_confirmation_contract(preview)
            return preview
        # Geometry and capital sizing stay pure and shared in grid_sizing;
        # the control plane owns only locking, persistence and runtime state.
        return build_grid_preview(cycle_id, payload, market=market, account=account, config=self.config)

    def _adaptive_start_preview(
        self,
        cycle_id: str,
        payload: dict[str, Any],
        *,
        market: dict[str, Any],
        account: dict[str, Any],
        now: str | None,
    ) -> dict[str, Any]:
        """Attach exact Paper-only consent facts without writing plan or risk state."""

        try:
            preview = self.preview(cycle_id, payload, market=market, account=account)
        except AdaptiveGridInputError as error:
            return self._adaptive_input_error_preview(
                cycle_id,
                payload,
                market=market,
                account=account,
                error=error,
            )
        if preview.get("strategy_type") == "dca":
            return preview
        solver = dict(preview.get("solver") or {})
        if solver.get("mode") != "manual_adaptive":
            return preview
        current = self.active_plan(cycle_id)
        # A new cycle has no active plan until the operator confirms a start.
        # Previewing must remain read-only, so use an in-memory seed rather than
        # auto-locking a StrategyPlan merely because the operator clicked the
        # smart-fill button.
        preview_seed = current or {
            "schema_version": PLAN_SCHEMA,
            "cycle_id": cycle_id,
            "version": 0,
            "risk_budget": {},
            "field_sources": {},
        }
        candidate_plan = self._plan_from_preview(preview_seed, preview, now=now)
        timestamp = _timestamp(now)
        commands = build_plan_grid_entry_commands(candidate_plan, timestamp=timestamp)
        adapter = build_configured_execution_engine_adapter(
            self.output_root,
            config=self.config,
        )
        decision = self.risk_port.evaluate(
            self._grid_risk_request(
                cycle_id,
                action_class="increase_exposure",
                intent="preview_start_grid",
                plan=candidate_plan,
                commands=commands,
                account=account,
                market=market,
                adapter=adapter,
                timestamp=timestamp,
                replaced_order_ids=None,
                retained_order_ids=None,
            )
        ).to_dict()
        blocker_codes = {
            str(row.get("code") or "risk_blocked")
            for row in decision.get("blockers") or []
            if isinstance(row, dict)
        }
        non_overridable = sorted(
            blocker_codes - MANUAL_RANGE_RISK_OVERRIDABLE_BLOCKERS
        )
        if any(
            str(row.get("code") or "") == "manual_leverage_capacity_exceeded"
            for row in solver.get("risk_flags") or []
            if isinstance(row, dict)
        ):
            non_overridable.append("manual_leverage_capacity_exceeded")
            non_overridable = sorted(set(non_overridable))
        non_overridable_details = [
            dict(row)
            for row in decision.get("blockers") or []
            if isinstance(row, dict)
            and str(row.get("code") or "risk_blocked") in non_overridable
        ]
        detailed_codes = {
            str(row.get("code") or "") for row in non_overridable_details
        }
        for row in solver.get("risk_flags") or []:
            if not isinstance(row, dict):
                continue
            code = str(row.get("code") or "")
            if code not in non_overridable or code in detailed_codes:
                continue
            non_overridable_details.append({
                "code": code,
                "source": "adaptive_grid_solver",
                "message": str(row.get("message") or ""),
                "evidence": {
                    "actual_leverage": preview.get("risk", {}).get(
                        "actual_leverage"
                    ),
                    "selected_leverage": preview.get("grid", {}).get(
                        "leverage"
                    ),
                    "manual_paper_leverage_limit": solver.get(
                        "preferred", {}
                    ).get("manual_paper_leverage_limit"),
                    "estimated_margin": preview.get("risk", {}).get(
                        "estimated_margin"
                    ),
                    "equity": preview.get("risk", {}).get("equity"),
                },
            })
            detailed_codes.add(code)
        previous_strategy_type = str(
            (current or {}).get("strategy_type") or "grid"
        ).lower()
        # A terminal DCA plan has entries/target/stop rather than Grid range
        # geometry.  It is still useful history, but it cannot be fabricated
        # into an "old grid" for the Grid-only risk comparison.  Use the
        # candidate as the neutral Grid baseline and expose the strategy switch
        # explicitly to the caller instead of leaking a raw `grid low` error.
        old_specification = _range_preview_specification(
            current if current and previous_strategy_type == "grid" else preview
        )
        new_specification = _range_preview_specification(
            preview,
            canonical_metrics=decision.get("metrics"),
        )
        acknowledgements = _manual_range_acknowledgement_contract(
            preview_id=str(preview["preview_id"]),
            old=old_specification,
            new=new_specification,
            blocker_codes=blocker_codes,
            local_profit_target_not_met=(
                preview.get("grid", {}).get("profit_target_met") is not True
            ),
            limits=dict(decision.get("limits") or {}),
        )
        facts_digest = _manual_range_acknowledgement_facts_digest(
            old=old_specification,
            new=new_specification,
            limits=dict(decision.get("limits") or {}),
            acknowledgements=acknowledgements,
        )
        preview["manual_confirmation"] = {
            "schema_version": MANUAL_RANGE_RISK_ACK_SCHEMA,
            "preview_id": preview["preview_id"],
            "scope": "paper_only",
            "required": bool(blocker_codes or solver.get("risk_flags")),
            "available": not non_overridable,
            "facts_digest": facts_digest,
            "risk_snapshot_digest": _manual_range_risk_snapshot_digest(decision),
            "required_acknowledgements": acknowledgements,
            "previous_strategy_type": previous_strategy_type if current else None,
            "strategy_switch": bool(current and previous_strategy_type != "grid"),
            "overridable_blocker_codes": sorted(
                blocker_codes & MANUAL_RANGE_RISK_OVERRIDABLE_BLOCKERS
            ),
            "non_overridable_blocker_codes": non_overridable,
            "non_overridable_blockers": non_overridable_details,
            "old": old_specification,
            "new": new_specification,
        }
        return preview

    def _adaptive_input_error_preview(
        self,
        cycle_id: str,
        payload: dict[str, Any],
        *,
        market: dict[str, Any],
        account: dict[str, Any],
        error: AdaptiveGridInputError,
    ) -> dict[str, Any]:
        """Return a display-only fallback preview for a hard solver input error.

        The fallback removes only the invalid lock.  It is not executable:
        ``manual_confirmation.available`` remains false and records the exact
        rejected input, so the browser can render a concrete remedy without
        silently clamping or applying a different grid.
        """

        fallback_payload = dict(payload)
        solver = dict(fallback_payload.get("solver") or {})
        invalid_lock = (
            "grid_count"
            if error.code.startswith("adaptive_grid_count_")
            else "leverage"
        )
        solver["locked"] = [
            str(value)
            for value in solver.get("locked") or []
            if str(value) != invalid_lock
        ]
        fallback_payload["solver"] = solver
        preview = self.preview(
            cycle_id,
            fallback_payload,
            market=market,
            account=account,
        )
        current = self.active_plan(cycle_id)
        old_specification = _range_preview_specification(current or preview)
        new_specification = _range_preview_specification(preview)
        error_row = {
            "code": error.code,
            "source": "adaptive_grid_solver",
            "message": str(error),
            "evidence": dict(error.evidence),
        }
        acknowledgements = _manual_range_acknowledgement_contract(
            preview_id=str(preview["preview_id"]),
            old=old_specification,
            new=new_specification,
            blocker_codes=set(),
            local_profit_target_not_met=False,
            limits={},
        )
        preview["solver"] = {
            **dict(preview.get("solver") or {}),
            "input_error": error_row,
        }
        preview["manual_confirmation"] = {
            "schema_version": MANUAL_RANGE_RISK_ACK_SCHEMA,
            "preview_id": preview["preview_id"],
            "scope": "paper_only",
            "required": True,
            "available": False,
            "facts_digest": _manual_range_acknowledgement_facts_digest(
                old=old_specification,
                new=new_specification,
                limits={},
                acknowledgements=acknowledgements,
            ),
            "risk_snapshot_digest": "adaptive-solver-input-error",
            "required_acknowledgements": acknowledgements,
            "overridable_blocker_codes": [],
            "non_overridable_blocker_codes": [error.code],
            "non_overridable_blockers": [error_row],
            "old": old_specification,
            "new": new_specification,
        }
        return preview

    def _prepare_start(
        self,
        cycle_id: str,
        payload: dict[str, Any],
        *,
        market: dict[str, Any],
        account: dict[str, Any],
        now: str | None,
    ) -> dict[str, Any]:
        """Freeze one server-built Paper candidate without creating a plan/order."""

        current = self.active_plan(cycle_id)
        strategy_type = str(payload.get("strategy_type") or "grid").lower()
        if not current and strategy_type != "dca":
            raise ValueError("cannot prepare start without an active StrategyPlan")
        runtime = self.runtime_state(cycle_id)
        if runtime.get("desired_state") == "running":
            raise ValueError("robot is already running; stop it before changing the grid")
        adapter = build_configured_execution_engine_adapter(
            self.output_root,
            config=self.config,
        )
        self._require_paper_execution_tick(
            cycle_id,
            adapter=adapter,
            now=now,
        )
        snapshot = adapter.snapshot(cycle_id)
        pending = [
            row
            for row in snapshot.get("orders") or []
            if str(row.get("state") or "").lower() == "accepted"
        ]
        open_positions = [
            row
            for row in snapshot.get("positions") or []
            if str(row.get("status") or "").lower() == "open"
        ]
        if pending or open_positions:
            raise ValueError(
                "new grid start requires zero accepted orders and zero open positions"
            )
        preview = self._adaptive_start_preview(
            cycle_id,
            payload,
            market=market,
            account=account,
            now=now,
        )
        prepared_at = _timestamp(now)
        record = {
            "schema_version": PREPARED_START_SCHEMA,
            "cycle_id": cycle_id,
            "prepared_at": prepared_at,
            "expires_at": (
                parse_utc(prepared_at)
                + timedelta(seconds=PREPARED_START_TTL_SECONDS)
            ).isoformat(),
            "expected_strategy_plan_id": str(
                (current or {}).get("strategy_plan_id") or ""
            ),
            "expected_strategy_plan_version": int((current or {}).get("version") or 0),
            "execution_adapter_name": str(getattr(adapter, "name", "")),
            "market_snapshot": canonical_market_risk_state(market),
            "preview": preview,
        }
        prepared_start_id = self._prepared_start_content_id(record)
        record["prepared_start_id"] = prepared_start_id
        self._write_prepared_start(record)
        return {
            "action": "prepare_start",
            "prepared_start_id": prepared_start_id,
            "expires_at": record["expires_at"],
            "preview": preview,
            "side_effects": {
                "strategy_plan_written": False,
                "orders_created": 0,
                "positions_changed": 0,
                "risk_decision_persisted": False,
            },
        }

    def _load_prepared_start(
        self,
        cycle_id: str,
        prepared_start_id: str,
    ) -> dict[str, Any]:
        try:
            rows = load_json(self._prepared_starts_path(cycle_id))
        except (OSError, TypeError, ValueError):
            raise ValueError("prepared_start_changed")
        row = next(
            (
                dict(candidate)
                for candidate in reversed(rows)
                if isinstance(candidate, dict)
                and str(candidate.get("prepared_start_id") or "")
                == prepared_start_id
            ),
            None,
        )
        if not row or row.get("schema_version") != PREPARED_START_SCHEMA:
            raise ValueError("prepared_start_changed")
        return row

    def _validate_prepared_start(
        self,
        cycle_id: str,
        prepared: dict[str, Any],
        *,
        expected_preview_id: str,
        current: dict[str, Any],
        market: dict[str, Any],
        adapter_name: str,
        now: str | None,
    ) -> dict[str, Any]:
        preview = dict(prepared.get("preview") or {})
        if preview.get("strategy_type") == "dca":
            return self._validate_prepared_dca_start(
                cycle_id,
                prepared,
                preview=preview,
                expected_preview_id=expected_preview_id,
                current=current,
                market=market,
                adapter_name=adapter_name,
                now=now,
            )
        try:
            content_id = self._prepared_start_content_id(prepared)
            canonical_preview_id = _grid_preview_id(preview)
            prepared_plan_version = int(
                prepared.get("expected_strategy_plan_version") or 0
            )
        except (TypeError, ValueError, OverflowError):
            raise ValueError("prepared_start_changed")
        if (
            str(prepared.get("prepared_start_id") or "") != content_id
            or str(prepared.get("cycle_id") or "") != cycle_id
            or str(prepared.get("expected_strategy_plan_id") or "")
            != str(current.get("strategy_plan_id") or "")
            or prepared_plan_version != int(current.get("version") or 0)
            or str(prepared.get("execution_adapter_name") or "") != adapter_name
            or not expected_preview_id
            or expected_preview_id != str(preview.get("preview_id") or "")
            or canonical_preview_id != expected_preview_id
        ):
            raise ValueError("prepared_start_changed")
        try:
            checked_at = parse_utc(_timestamp(now))
            prepared_at = parse_utc(str(prepared.get("prepared_at") or ""))
        except (TypeError, ValueError):
            raise ValueError("prepared_start_changed")
        age_seconds = (checked_at - prepared_at).total_seconds()
        if age_seconds < -1 or age_seconds > PREPARED_START_TTL_SECONDS:
            raise ValueError("prepared_start_expired")
        _validate_market(market)
        prepared_market = dict(prepared.get("market_snapshot") or {})
        current_market = canonical_market_risk_state(market)
        for identity_field in ("provider", "source_mode", "symbol", "timeframe"):
            if str(prepared_market.get(identity_field) or "") != str(
                current_market.get(identity_field) or ""
            ):
                raise ValueError("prepared_start_market_moved")
        latest = _positive_number(market.get("latest_close"), "market latest_close")
        candidate_range = dict(preview.get("range") or {})
        low = _positive_number(candidate_range.get("low"), "prepared range low")
        high = _positive_number(candidate_range.get("high"), "prepared range high")
        direction = str(preview.get("direction") or "neutral").lower()
        if market_outside_range_requires_blocker(
            direction=direction,
            market_price=latest,
            range_low=low,
            range_high=high,
        ) or (
            not _market_inside_source_envelope(candidate_range, latest)
            and not market_is_on_non_entry_side(direction, latest, low, high)
        ):
            raise ValueError("prepared_start_market_moved")
        try:
            levels = sorted(
                _positive_number(level, "prepared grid level")
                for level in dict(preview.get("grid") or {}).get("levels") or []
            )
            prepared_price = _positive_number(
                prepared_market.get("price"),
                "prepared market price",
            )
        except (TypeError, ValueError, OverflowError):
            raise ValueError("prepared_start_changed")
        same_grid_cell = bool(levels) and bisect_right(
            levels,
            prepared_price,
        ) == bisect_right(levels, latest)
        remained_on_non_entry_side = (
            direction == "long"
            and prepared_price >= high
            and latest >= high
        ) or (
            direction == "short"
            and prepared_price <= low
            and latest <= low
        )
        if not same_grid_cell and not remained_on_non_entry_side:
            raise ValueError("prepared_start_market_moved")
        orders = [dict(row) for row in preview.get("orders") or [] if isinstance(row, dict)]
        if not orders:
            raise ValueError("prepared_start_changed")
        try:
            marketable = [
                row
                for row in orders
                if (
                    str(row.get("side") or "").lower() == "buy"
                    and _positive_number(row.get("price"), "prepared order price")
                    >= latest
                )
                or (
                    str(row.get("side") or "").lower() == "sell"
                    and _positive_number(row.get("price"), "prepared order price")
                    <= latest
                )
            ]
        except (TypeError, ValueError, OverflowError):
            raise ValueError("prepared_start_changed")
        if marketable:
            raise ValueError("prepared_start_market_moved")
        return preview

    def _validate_prepared_dca_start(
        self,
        cycle_id: str,
        prepared: dict[str, Any],
        *,
        preview: dict[str, Any],
        expected_preview_id: str,
        current: dict[str, Any],
        market: dict[str, Any],
        adapter_name: str,
        now: str | None,
    ) -> dict[str, Any]:
        """Validate frozen DCA facts without imposing Grid range geometry."""

        try:
            content_id = self._prepared_start_content_id(prepared)
            prepared_plan_version = int(
                prepared.get("expected_strategy_plan_version") or 0
            )
            checked_at = parse_utc(_timestamp(now))
            prepared_at = parse_utc(str(prepared.get("prepared_at") or ""))
        except (TypeError, ValueError, OverflowError):
            raise ValueError("prepared_start_changed")
        prepared_plan_matches = (
            str(prepared.get("expected_strategy_plan_id") or "")
            == str(current.get("strategy_plan_id") or "")
            and prepared_plan_version == int(current.get("version") or 0)
        ) or (
            current.get("strategy_type") == "dca"
            and str(current.get("preview_id") or "")
            == str(preview.get("preview_id") or "")
        )
        if (
            str(prepared.get("prepared_start_id") or "") != content_id
            or str(prepared.get("cycle_id") or "") != cycle_id
            or not prepared_plan_matches
            or str(prepared.get("execution_adapter_name") or "") != adapter_name
            or not expected_preview_id
            or expected_preview_id != str(preview.get("preview_id") or "")
            or dca_preview_id(preview) != expected_preview_id
        ):
            raise ValueError("prepared_start_changed")
        age_seconds = (checked_at - prepared_at).total_seconds()
        if age_seconds < -1 or age_seconds > PREPARED_START_TTL_SECONDS:
            raise ValueError("prepared_start_expired")
        _validate_market(market)
        prepared_market = dict(prepared.get("market_snapshot") or {})
        current_market = canonical_market_risk_state(market)
        for identity_field in ("provider", "source_mode", "symbol", "timeframe"):
            if str(prepared_market.get(identity_field) or "") != str(
                current_market.get(identity_field) or ""
            ):
                raise ValueError("prepared_start_market_moved")
        return preview

    @staticmethod
    def _prepared_start_risk_market(prepared: dict[str, Any]) -> dict[str, Any]:
        """Rebuild the exact consented mark after live executability validation."""

        snapshot = dict(prepared.get("market_snapshot") or {})
        return {
            "status": snapshot.get("status"),
            "fresh": snapshot.get("fresh") is True,
            "is_synthetic": snapshot.get("is_synthetic"),
            "provider": snapshot.get("provider"),
            "source_mode": snapshot.get("source_mode"),
            "symbol": snapshot.get("symbol"),
            "timeframe": snapshot.get("timeframe"),
            "latest_close": snapshot.get("price"),
            "latest_timestamp": snapshot.get("latest_timestamp"),
            "batch_id": snapshot.get("batch_id"),
            "trust": {"status": snapshot.get("trust_status")},
        }

    @staticmethod
    def _prepared_start_content_id(record: dict[str, Any]) -> str:
        payload = {
            key: record.get(key)
            for key in (
                "schema_version",
                "cycle_id",
                "prepared_at",
                "expires_at",
                "expected_strategy_plan_id",
                "expected_strategy_plan_version",
                "execution_adapter_name",
                "market_snapshot",
                "preview",
            )
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return f"prepared-start-{hashlib.sha256(raw.encode()).hexdigest()[:16]}"

    def preview_range_adjustment(
        self,
        cycle_id: str,
        payload: dict[str, Any],
        *,
        market: dict[str, Any],
        account: dict[str, Any],
        now: str | None = None,
    ) -> dict[str, Any]:
        """Return geometry, order delta, and current canonical risk without writes."""

        current = self.active_plan(cycle_id)
        runtime = self.runtime_state(cycle_id)
        if not current or runtime.get("actual_state") != "running":
            raise ValueError("range drag preview requires a running StrategyPlan")
        expected_plan_id = str(payload.get("expected_strategy_plan_id") or "")
        expected_plan_version = int(
            payload.get("expected_strategy_plan_version") or 0
        )
        current_plan_id = str(current.get("strategy_plan_id") or "")
        current_plan_version = int(current.get("version") or 0)
        runtime_plan_id = str(runtime.get("strategy_plan_id") or "")
        runtime_plan_version = int(runtime.get("strategy_plan_version") or 0)
        if (
            expected_plan_id != current_plan_id
            or expected_plan_version != current_plan_version
            or runtime_plan_id != current_plan_id
            or runtime_plan_version != current_plan_version
        ):
            raise ValueError("strategy_plan_changed")
        requested = payload.get("range") if isinstance(payload.get("range"), dict) else {}
        legacy_migration = _legacy_single_side_range_migration(current)
        geometry_plan = current
        if legacy_migration:
            geometry_plan = {
                **current,
                "range": dict(legacy_migration["executable_range"]),
                "grid": {
                    **dict(current.get("grid") or {}),
                    "count": legacy_migration["executable_grid_count"],
                },
            }
        geometry = build_dragged_range(
            geometry_plan,
            requested,
            handle=str(payload.get("handle") or ""),
        )
        grid = dict(current.get("grid") or {})
        candidate_grid_count = (
            legacy_migration["executable_grid_count"]
            if legacy_migration
            else grid.get("count")
        )
        current_range = dict(current.get("range") or {})
        current_plan_market = dict(
            (current.get("execution_context") or {}).get("market") or {}
        )
        candidate_split_price = (
            legacy_migration["split_price"]
            if legacy_migration
            else _number_or(
                current_range.get("split_price"),
                _number_or(
                    current_plan_market.get("price"),
                    _positive_number(market.get("latest_close"), "market latest_close"),
                ),
            )
        )
        notional = _positive_number(
            grid.get("notional_per_grid"),
            "current notional_per_grid",
        )
        def build_candidate(fixed_notional: float) -> dict[str, Any]:
            return build_grid_preview(
                cycle_id,
                {
                "direction": current.get("direction"),
                "style": current.get("style"),
                "out_of_range": grid.get("out_of_range"),
                "range": {
                    **geometry["new_range"],
                    "scope": (
                        f"{current.get('direction')}_side"
                        if str(current.get("direction") or "") in {"long", "short"}
                        else "full"
                    ),
                    "split_price": candidate_split_price,
                    "source_envelope": dict(
                        current_range.get("source_envelope")
                        or geometry["new_range"]
                    ),
                },
                "grid": {
                    "count": candidate_grid_count,
                    "mode": grid.get("mode"),
                    "notional_per_grid": fixed_notional,
                    "notional_mode": "manual",
                    "out_of_range": grid.get("out_of_range"),
                },
                "risk_budget": {"leverage": grid.get("leverage")},
                },
                market=market,
                account=account,
                config=self.config,
                allow_unsafe_manual_preview=True,
            )

        candidate = build_candidate(notional)
        adapter = build_configured_execution_engine_adapter(
            self.output_root,
            config=self.config,
        )
        execution = adapter.snapshot(cycle_id)
        accepted_orders = [
            dict(row)
            for row in execution.get("orders") or []
            if str(row.get("state") or "").lower() == "accepted"
        ]
        accepted_entries = [
            row
            for row in accepted_orders
            if str(row.get("event") or "entry").lower() == "entry"
        ]
        replaced_order_ids = _required_order_ids(
            accepted_entries,
            "range preview replaced entry",
        )
        timestamp = _timestamp(now)
        current_risk_commands = _retained_entry_risk_commands(
            accepted_entries,
            plan_history=[
                dict(row)
                for row in load_json(self._plans_path(cycle_id))
                if isinstance(row, dict)
            ],
            candidate_plan=current,
            timestamp=timestamp,
        )
        current_risk_request = self._grid_risk_request(
            cycle_id,
            action_class="replace_pending",
            intent="preview_current_grid_risk",
            plan=current,
            commands=current_risk_commands,
            account=account,
            market=market,
            adapter=adapter,
            timestamp=timestamp,
            replaced_order_ids=[],
            retained_order_ids=replaced_order_ids,
        )
        current_risk_decision = self.risk_port.evaluate(
            current_risk_request
        ).to_dict()
        def evaluate_candidate(
            preview: dict[str, Any],
        ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
            candidate_plan = {
                **current,
                "strategy_plan_id": f"range-preview-plan:{preview['preview_id']}",
                "version": int(current.get("version") or 0) + 1,
                "status": "preview",
                "locked_at": timestamp,
                "range": dict(preview["range"]),
                "grid": {
                    **dict(preview["grid"]),
                    "orders": list(preview["orders"]),
                },
                "risk_budget": dict(preview["risk"]),
                "preview_id": preview["preview_id"],
                "execution_context": {
                    **dict(current.get("execution_context") or {}),
                    "market": {
                        "price": _positive_number(
                            market.get("latest_close"),
                            "market latest_close",
                        ),
                        "symbol": str(market.get("symbol") or "GOLD"),
                        "timestamp": market.get("latest_timestamp"),
                        "provider": market.get("provider"),
                        "timeframe": market.get("timeframe"),
                    },
                },
            }
            commands = build_plan_grid_entry_commands(
                candidate_plan,
                timestamp=timestamp,
            )
            risk_request = self._grid_risk_request(
                cycle_id,
                action_class="replace_pending",
                intent="preview_range_drag",
                plan=candidate_plan,
                commands=commands,
                account=account,
                market=market,
                adapter=adapter,
                timestamp=timestamp,
                replaced_order_ids=replaced_order_ids,
                retained_order_ids=[],
            )
            decision = self.risk_port.evaluate(risk_request).to_dict()
            return commands, decision

        commands, risk_decision = evaluate_candidate(candidate)
        local_risk = dict(candidate.get("risk") or {})
        local_profit_target_not_met = (
            local_risk.get("profit_target_met") is not True
        )
        local_capital_budget_exceeded = bool(
            local_risk.get("capital_budget_exceeded")
        )
        local_budget_blocked = bool(
            local_profit_target_not_met or local_capital_budget_exceeded
        )
        blockers = list(risk_decision.get("blockers") or [])
        budget_blocker_codes = {
            "grid_profit_target_not_met",
            "required_leverage_mismatch",
            "leverage_limit_exceeded",
            "projected_leverage_exceeded",
            "projected_margin_exceeded",
        }
        canonical_budget_blocked = any(
            str(row.get("code") or "") in budget_blocker_codes
            for row in blockers
            if isinstance(row, dict)
        )
        recommendation = dict(risk_decision.get("recommendation") or {})
        metrics = dict(risk_decision.get("metrics") or {})
        limits = dict(risk_decision.get("limits") or {})
        candidate_by_side = dict(metrics.get("candidate_notional_by_side") or {})
        existing_by_side = dict(metrics.get("existing_notional_by_side") or {})
        equity = _number_or(metrics.get("equity"), 0.0)
        max_leverage = _number_or(limits.get("max_leverage"), 0.0)
        margin_budget = _number_or(limits.get("margin_budget"), 0.0)
        requested_leverage = _number_or(grid.get("leverage"), 0.0)
        notional_caps = []
        if recommendation.get("available") is True:
            recommended = _number_or(
                recommendation.get("recommended_notional_per_grid"),
                0.0,
            )
            if recommended > 0:
                notional_caps.append(recommended)
        local_cap = _number_or(local_risk.get("safe_notional_cap_per_grid"), 0.0)
        if local_cap > 0:
            notional_caps.append(local_cap)
        leverage_capacity = equity * max_leverage
        margin_capacity = margin_budget * requested_leverage
        for side in ("buy", "sell"):
            candidate_side = _number_or(candidate_by_side.get(side), 0.0)
            if candidate_side <= 0:
                continue
            existing_side = _number_or(existing_by_side.get(side), 0.0)
            for capacity in (leverage_capacity, margin_capacity):
                remaining = max(0.0, capacity - existing_side)
                notional_caps.append(notional * remaining / candidate_side)
        safe_notional = (
            math.floor(min(notional_caps) * 100.0) / 100.0
            if notional_caps
            else 0.0
        )
        recalculated = bool(payload.get("recalculate_notional_by_risk_budget"))
        trial_candidate: dict[str, Any] | None = None
        trial_commands: list[dict[str, Any]] | None = None
        trial_decision: dict[str, Any] | None = None
        trial_local_blocked = True
        if (
            safe_notional > 0
            and safe_notional < notional - 1e-8
            and (local_budget_blocked or canonical_budget_blocked)
            and recommendation.get("available") is True
        ):
            trial_candidate = build_candidate(safe_notional)
            trial_commands, trial_decision = evaluate_candidate(
                trial_candidate
            )
            trial_local_risk = dict(trial_candidate.get("risk") or {})
            trial_local_blocked = bool(
                trial_local_risk.get("profit_target_met") is not True
                or trial_local_risk.get("capital_budget_exceeded")
            )
        recalculation_available = bool(
            trial_candidate
            and trial_decision
            and trial_decision.get("allow_exposure_increase") is True
            and not trial_local_blocked
        )
        if recalculated:
            if not recalculation_available:
                raise ValueError("risk notional recalculation is unavailable")
            candidate = dict(trial_candidate or {})
            commands = list(trial_commands or [])
            risk_decision = dict(trial_decision or {})
            local_risk = dict(candidate.get("risk") or {})
            local_profit_target_not_met = (
                local_risk.get("profit_target_met") is not True
            )
            local_capital_budget_exceeded = bool(
                local_risk.get("capital_budget_exceeded")
            )
            local_budget_blocked = bool(
                local_profit_target_not_met or local_capital_budget_exceeded
            )
            blockers = list(risk_decision.get("blockers") or [])
        can_apply = bool(risk_decision.get("allow_exposure_increase")) and not local_budget_blocked
        old_specification = _range_preview_specification(
            current,
            canonical_metrics=current_risk_decision.get("metrics"),
        )
        new_specification = _range_preview_specification(
            candidate,
            canonical_metrics=risk_decision.get("metrics"),
            post_flatten_candidate_only=True,
        )
        raw_blocker_codes = {
            str(row.get("code") or "risk_blocked")
            for row in blockers
            if isinstance(row, dict)
        }
        blocker_codes = _manual_range_effective_blocker_codes(risk_decision)
        non_overridable = sorted(
            raw_blocker_codes - MANUAL_RANGE_RISK_OVERRIDABLE_BLOCKERS
        )
        acknowledgement_contract = _manual_range_acknowledgement_contract(
            preview_id=str(candidate["preview_id"]),
            old=old_specification,
            new=new_specification,
            blocker_codes=blocker_codes,
            local_profit_target_not_met=local_profit_target_not_met,
            limits=dict(risk_decision.get("limits") or {}),
        )
        acknowledgement_facts_digest = _manual_range_acknowledgement_facts_digest(
            old=old_specification,
            new=new_specification,
            limits=dict(risk_decision.get("limits") or {}),
            acknowledgements=acknowledgement_contract,
        )
        risk_snapshot_digest = _manual_range_risk_snapshot_digest(risk_decision)
        return {
            "schema_version": "grid-range-drag-preview-v1",
            "cycle_id": cycle_id,
            "expected_strategy_plan_id": expected_plan_id,
            "expected_strategy_plan_version": expected_plan_version,
            "preview_id": candidate["preview_id"],
            "geometry": geometry,
            "old": old_specification,
            "new": new_specification,
            "migration": legacy_migration,
            "candidate": candidate,
            "canonical_risk": {
                "basis": "exact_commands_plus_current_canonical_accounting",
                "old": current_risk_decision,
                "new": risk_decision,
            },
            "risk_decision": risk_decision,
            "can_apply": can_apply,
            "can_apply_with_acknowledgements": not non_overridable,
            "confirm_disabled_reasons": [
                str(row.get("code") or "risk_blocked")
                for row in blockers
                if isinstance(row, dict)
            ]
            + (
                ["preview_profit_or_capital_target_not_met"]
                if local_budget_blocked
                else []
            ),
            "manual_confirmation": {
                "schema_version": MANUAL_RANGE_RISK_ACK_SCHEMA,
                "preview_id": candidate["preview_id"],
                "scope": "paper_only",
                "required": True,
                "available": not non_overridable,
                "facts_digest": acknowledgement_facts_digest,
                "risk_snapshot_digest": risk_snapshot_digest,
                "required_acknowledgements": acknowledgement_contract,
                "overridable_blocker_codes": sorted(
                    blocker_codes & MANUAL_RANGE_RISK_OVERRIDABLE_BLOCKERS
                ),
                "non_overridable_blocker_codes": non_overridable,
            },
            "risk_recalculation": {
                "available": recalculation_available and not recalculated,
                "reason": (
                    None
                    if recalculation_available
                    else "profit_target_requires_grid_geometry_or_capital_change"
                ),
                "applied_automatically": False,
                "applied_to_preview": recalculated,
                "original_notional_per_grid": notional,
                "notional_per_grid": round(safe_notional, 2) if safe_notional > 0 else None,
            },
            "order_delta": {
                "cancel_pending_entries": len(accepted_entries),
                "submit_new_entries": len(commands),
                "accepted_protection_orders_unchanged_by_preview": sum(
                    1
                    for row in accepted_orders
                    if str(row.get("event") or "entry").lower() != "entry"
                ),
            },
            "positions": {
                "open_count": sum(
                    1
                    for row in execution.get("positions") or []
                    if str(row.get("status") or "").lower() == "open"
                ),
                "preview_effect": "none",
            },
            "tp_sl": {
                "existing_orders_affected_by_preview": False,
                "candidate_orders_recomputed": True,
            },
            "side_effects": {
                "orders_created": 0,
                "orders_cancelled": 0,
                "positions_changed": 0,
                "strategy_plan_written": False,
                "risk_decision_persisted": False,
            },
        }

    def control(
        self,
        cycle_id: str,
        action: str,
        payload: dict[str, Any] | None = None,
        *,
        market: dict[str, Any] | None = None,
        account: dict[str, Any] | None = None,
        now: str | None = None,
        actor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with production_mutation_lock(self.output_root):
            try:
                result = self._control_locked(cycle_id, action, payload, market=market, account=account, now=now)
            except ValueError as exc:
                # A rejected mutation is still an operator action and must
                # stay attributable; the original rejection is re-raised.
                if str(action or "").lower() not in {"preview", "preview_range"}:
                    self._audit_control(cycle_id, action, payload, actor=actor, result="rejected", error=str(exc), now=now)
                raise
            if str(action or "").lower() not in {"preview", "preview_range"}:
                evidence = None
                if isinstance(result, dict) and result.get("safe_action_market_gates"):
                    evidence = {"safe_action_market_gates": result["safe_action_market_gates"]}
                recorded = self._audit_control(
                    cycle_id,
                    action,
                    payload,
                    actor=actor,
                    result="accepted",
                    error=None,
                    evidence=evidence,
                    now=now,
                )
                if isinstance(result, dict):
                    result = {**result, "audit_recorded": recorded}
            return result

    def _audit_control(
        self,
        cycle_id: str,
        action: str,
        payload: dict[str, Any] | None,
        *,
        actor: dict[str, Any] | None,
        result: str,
        error: str | None,
        evidence: dict[str, Any] | None = None,
        now: str | None,
    ) -> bool:
        # An audit failure must never block or alter the control outcome;
        # callers surface it honestly via audit_recorded=false.
        try:
            event = build_control_event(
                cycle_id=cycle_id,
                action=action,
                actor=actor,
                payload=payload,
                result=result,
                error=error,
                runtime=self.runtime_state(cycle_id),
                evidence=evidence,
                now=_timestamp(now),
            )
            append_control_event(self.output_root, event)
            return True
        except OSError:
            return False

    def _control_locked(
        self,
        cycle_id: str,
        action: str,
        payload: dict[str, Any] | None = None,
        *,
        market: dict[str, Any] | None = None,
        account: dict[str, Any] | None = None,
        now: str | None = None,
    ) -> dict[str, Any]:
        body = dict(payload or {})
        action = str(action or "").lower()
        if action == "preview":
            return {
                "action": action,
                "preview": self._adaptive_start_preview(
                    cycle_id,
                    body,
                    market=market or {},
                    account=account or {},
                    now=now,
                ),
            }
        if action == "prepare_start":
            return self._prepare_start(
                cycle_id,
                body,
                market=market or {},
                account=account or {},
                now=now,
            )
        if action == "preview_range":
            return {
                "action": action,
                "preview": self.preview_range_adjustment(
                    cycle_id,
                    body,
                    market=market or {},
                    account=account or {},
                    now=now,
                ),
            }
        if action == "start":
            return self._start(cycle_id, body, market=market or {}, account=account or {}, now=now)
        if action == "stop":
            return self._stop(
                cycle_id,
                market=market,
                now=now,
                transition_owner=(
                    str(body["transition_owner"])
                    if body.get("transition_owner") not in (None, "")
                    else None
                ),
                expected_runtime_updated_at=(
                    str(body["expected_runtime_updated_at"])
                    if body.get("expected_runtime_updated_at") not in (None, "")
                    else None
                ),
            )
        if action == "replace_grid":
            return self._replace_grid(
                cycle_id,
                body,
                market=market or {},
                account=account or {},
                now=now,
            )
        if action == "reset_statistics":
            previous = self.runtime_state(cycle_id)
            row = {
                **previous,
                "cycle_id": cycle_id,
                "desired_state": previous["desired_state"],
                "updated_at": _timestamp(now),
                "last_action": action,
            }
            row["statistics_baseline_at"] = _timestamp(now)
            write_json(self.root / "runtime.json", [row])
            return {"action": action, "runtime": row, "historical_records_preserved": True}
        if action == "adjust_plan":
            if market is not None and body.get("style") in GRID_STYLES:
                return self._regrid(cycle_id, body, market=market, account=account or {}, now=now)
            runtime = self.runtime_state(cycle_id)
            if runtime["desired_state"] == "running" and any(key in body for key in ("grid", "risk_budget")):
                raise ValueError("running grid or risk adjustment requires trusted market preview and risk decision")
            current = self.active_plan(cycle_id) or self.ensure_compatible_active_plan(cycle_id, as_of=now)
            if not current:
                raise ValueError("cannot adjust without an active StrategyPlan")
            adjusted = {**current, "version": int(current["version"]) + 1, "status": "active", "locked_at": _timestamp(now)}
            for key in ("direction", "range", "grid", "tp_sl", "risk_budget", "intraday_rules", "key_levels", "signal"):
                if key in body:
                    adjusted[key] = body[key]
                    adjusted["field_sources"] = {**adjusted["field_sources"], key: "confirmed"}
            adjusted["strategy_plan_id"] = _plan_id(cycle_id, adjusted["version"], f"console-adjust-{_timestamp(now)}")
            self._activate_plan(adjusted)
            return {"action": action, "plan": adjusted}
        if action == "extend_range":
            return self._extend_range(
                cycle_id,
                body,
                market=market or {},
                account=account or {},
                now=now,
            )
        if action == "suspend_entries":
            adapter = build_configured_execution_engine_adapter(
                self.output_root,
                config=self.config,
            )
            requested_ids = {
                str(value)
                for value in body.get("order_ids") or []
                if str(value)
            }
            if not requested_ids:
                raise ValueError("suspend_entries requires exact order IDs")
            before = adapter.snapshot(cycle_id)
            accepted_entries = {
                str(row.get("order_id") or ""): dict(row)
                for row in before.get("orders") or []
                if str(row.get("state") or "").lower() == "accepted"
                and str(row.get("event") or "entry").lower() == "entry"
                and str(row.get("order_id") or "")
            }
            if not requested_ids <= set(accepted_entries):
                raise ValueError("suspend_entries may cancel accepted entry orders only")
            receipt = adapter.cancel_orders(
                cycle_id,
                order_ids=sorted(requested_ids),
                ts=_timestamp(now),
                reason="cycle_direction_conflict",
            )
            execution_event = self._settle_safe_action_commands(
                adapter,
                cycle_id,
            )
            after = adapter.snapshot(cycle_id)
            still_accepted = {
                str(row.get("order_id") or "")
                for row in after.get("orders") or []
                if str(row.get("state") or "").lower() == "accepted"
            }
            if requested_ids & still_accepted:
                raise RuntimeError("suspend_entries left accepted entry orders")
            before_protection = {
                str(row.get("order_id") or "")
                for row in before.get("orders") or []
                if str(row.get("state") or "").lower() == "accepted"
                and str(row.get("event") or "entry").lower() != "entry"
            }
            after_protection = {
                str(row.get("order_id") or "")
                for row in after.get("orders") or []
                if str(row.get("state") or "").lower() == "accepted"
                and str(row.get("event") or "entry").lower() != "entry"
            }
            if before_protection != after_protection:
                raise RuntimeError("suspend_entries changed protective orders")
            reconciliation = adapter.reconcile(cycle_id)
            if reconciliation.get("status") != "ok":
                raise ValueError("paper ledger reconciliation failed")
            return {
                "action": action,
                "cancelled_entry_order_ids": sorted(requested_ids),
                "cancelled_orders": int(
                    receipt.get("cancelled_order_count")
                    or len(receipt.get("cancelled_order_ids") or [])
                ),
                "execution_receipt": receipt,
                "execution_event": execution_event,
                "reconciliation": reconciliation,
            }
        if action == "cancel_all":
            adapter = build_configured_execution_engine_adapter(self.output_root, config=self.config)
            receipt = adapter.cancel_orders(
                cycle_id,
                ts=_timestamp(now),
                reason="operator_cancel_all",
            )
            execution_event = self._settle_safe_action_commands(adapter, cycle_id)
            accepted_after = self._accepted_orders(cycle_id, adapter=adapter)
            if accepted_after:
                raise ValueError("paper cancel left accepted orders")
            reconciliation = adapter.reconcile(cycle_id)
            if reconciliation.get("status") != "ok":
                raise ValueError("paper ledger reconciliation failed")
            return {
                "action": action,
                "cancelled_orders": int(receipt.get("cancelled_order_count") or 0),
                "execution_receipt": receipt,
                "execution_event": execution_event,
                "reconciliation": reconciliation,
                "safe_action_market_gates": [
                    build_paper_safe_action_market_gate(
                        action_class_for_command({"event": "cancel"}),
                        market,
                        pricing_source="not_required",
                    )
                ],
            }
        raise ValueError("unsupported production control action")

    def _start(
        self,
        cycle_id: str,
        body: dict[str, Any],
        *,
        market: dict[str, Any],
        account: dict[str, Any],
        now: str | None,
    ) -> dict[str, Any]:
        strategy_type = str(body.get("strategy_type") or "grid").lower()
        if strategy_type == "dca":
            return self._start_dca(
                cycle_id,
                body,
                market=market,
                account=account,
                now=now,
            )
        current = self.active_plan(cycle_id)
        if not current:
            raise ValueError("cannot start without an already selected active StrategyPlan")
        expected_preview_id = str(body.get("expected_preview_id") or "")
        prepared_start_id = str(body.get("prepared_start_id") or "")
        adapter = build_configured_execution_engine_adapter(
            self.output_root,
            config=self.config,
        )
        self._require_paper_execution_tick(
            cycle_id,
            adapter=adapter,
            now=now,
        )
        self._assert_no_unresolved_prior_cycle_runtime(cycle_id)
        prepared: dict[str, Any] | None = None
        if prepared_start_id:
            prepared = self._load_prepared_start(cycle_id, prepared_start_id)
            preview = self._validate_prepared_start(
                cycle_id,
                prepared,
                expected_preview_id=expected_preview_id,
                current=current,
                market=market,
                adapter_name=str(getattr(adapter, "name", "")),
                now=now,
            )
        else:
            preview = self._adaptive_start_preview(
                cycle_id,
                body,
                market=market,
                account=account,
                now=now,
            )
        if (preview.get("solver") or {}).get("mode") == "manual_adaptive":
            if not expected_preview_id or expected_preview_id != str(
                preview.get("preview_id") or ""
            ):
                raise ValueError("strategy_preview_changed")
        runtime = self.runtime_state(cycle_id)
        execution_snapshot = adapter.snapshot(cycle_id)
        pending = [
            row
            for row in execution_snapshot.get("orders") or []
            if str(row.get("state") or "").lower() == "accepted"
        ]
        open_positions = [
            row
            for row in execution_snapshot.get("positions") or []
            if str(row.get("status") or "").lower() == "open"
        ]
        same_running_plan = (
            runtime["desired_state"] == "running"
            and runtime.get("preview_id") == preview["preview_id"]
            and runtime.get("strategy_plan_id") == current.get("strategy_plan_id")
            and bool(pending)
        )
        if same_running_plan:
            return {
                "action": "start",
                "runtime": runtime,
                "plan": current,
                "created_orders": 0,
                "accepted_orders": len(pending),
                "idempotent": True,
            }
        if runtime["desired_state"] == "running":
            raise ValueError("robot is already running; stop it before changing the grid")
        if pending or open_positions:
            raise ValueError("new grid start requires zero accepted orders and zero open positions")

        adjusted = self._plan_from_preview(current, preview, now=now)
        timestamp = _timestamp(now)
        commands = build_plan_grid_entry_commands(adjusted, timestamp=timestamp)
        manual_override = None
        confirmation = dict(preview.get("manual_confirmation") or {})
        risk_market = market
        if confirmation.get("required") is True:
            manual_override = _validated_manual_range_acknowledgement(
                preview,
                body,
                now=now,
            )
            if prepared is not None:
                # The operator confirmed the prepared mark. Current market
                # trust, venue identity and order marketability were checked
                # immediately above; account/execution/policy remain current.
                risk_market = self._prepared_start_risk_market(prepared)
        risk_decision = self._authorize_grid_mutation(
            cycle_id,
            action_class="increase_exposure",
            intent="start_grid",
            plan=adjusted,
            commands=commands,
            account=account,
            market=risk_market,
            adapter=adapter,
            timestamp=timestamp,
            manual_override=manual_override,
        )
        self._assert_rollover_start_guard(body)
        # From this point through the first submit the shared control lock owns
        # every in-process production mutation path. Plan/runtime writes do not
        # alter any economic input bound by the immediately preceding recheck.
        self._activate_plan(adjusted)
        starting = {
            **runtime,
            "cycle_id": cycle_id,
            # Do not inherit a terminal DCA runtime's identity when a Grid
            # preview replaces it in the same cycle.
            "strategy_type": "grid",
            "desired_state": "running",
            "actual_state": "starting",
            "updated_at": timestamp,
            "last_action": "start",
            "last_error": None,
            "strategy_plan_id": adjusted["strategy_plan_id"],
            "strategy_plan_version": adjusted["version"],
            "preview_id": preview["preview_id"],
            "prepared_start_id": prepared_start_id or None,
            "risk_decision_id": risk_decision["decision_id"],
            "risk_policy_id": (risk_decision.get("policy") or {}).get("policy_id"),
            "accepted_order_count": 0,
            "accepted_order_count_known": True,
            "transition_owner": (
                f"rollover:{body['rollover_guard'].get('previous_cycle_id')}->{cycle_id}"
                if isinstance(body.get("rollover_guard"), dict)
                else None
            ),
        }
        self._write_runtime(starting)
        try:
            receipts = self._submit_plan_orders(adapter, adjusted, timestamp=timestamp, commands=commands)
            submitted_order_ids = [str(row.get("order_id") or "") for row in receipts]
            duplicate_receipt_ids = sorted({
                order_id
                for order_id, count in Counter(submitted_order_ids).items()
                if count > 1
            })
            empty_receipt_count = sum(not order_id.strip() for order_id in submitted_order_ids)
            if empty_receipt_count or duplicate_receipt_ids:
                raise ValueError(
                    "paper start receipts require valid unique order IDs"
                    f"; empty_count={empty_receipt_count}"
                    f"; duplicate_ids={duplicate_receipt_ids}"
                )
            submitted_ids = set(submitted_order_ids)
            self._validate_start_grid_snapshot(adapter, cycle_id, submitted_ids=submitted_ids)
            execution_event = self._advance_selected_execution(
                adapter,
                cycle_id,
                market=market,
                now=now,
                identity="strategy-start",
            )
            terminal_orders = self._validate_start_grid_snapshot(
                adapter,
                cycle_id,
                submitted_ids=submitted_ids,
            )
            accepted = [
                terminal_orders[order_id]
                for order_id in submitted_ids
                if str(terminal_orders[order_id].get("state") or "").lower() == "accepted"
            ]
            filled_count = sum(
                1
                for order_id in submitted_ids
                if str(terminal_orders[order_id].get("state") or "").lower() == "filled"
            )
            reconciliation = adapter.reconcile(cycle_id)
            if reconciliation.get("status") != "ok":
                raise ValueError("paper ledger reconciliation failed")
        except Exception as exc:
            cleanup_errors: list[str] = []
            try:
                self._cancel_pending(
                    cycle_id,
                    now=now,
                    strategy_plan_id=adjusted["strategy_plan_id"],
                    adapter=adapter,
                    reason="start_failed",
                )
            except Exception as cleanup_exc:
                cleanup_errors.append(f"cancel: {cleanup_exc}")
            try:
                cleanup_snapshot = adapter.snapshot(cycle_id)
                cleanup_price = _positive_number(market.get("latest_close"), "market latest_close")
                for position in cleanup_snapshot.get("positions") or []:
                    if str(position.get("status") or "") != "open":
                        continue
                    close_side = "sell" if str(position.get("side") or "") in {"buy", "long"} else "buy"
                    adapter.submit_order({
                        "cycle_id": cycle_id,
                        "ts": _timestamp(now),
                        "side": close_side,
                        "event": "flatten",
                        "order_type": "market",
                        "price": cleanup_price,
                        "market_price": cleanup_price,
                        "trade_id": position.get("trade_id"),
                        "position_id": position.get("position_id"),
                        "target_position_side": position.get("side"),
                        "target_entry_price": position.get("entry_price"),
                        "symbol": position.get("symbol") or str(market.get("symbol") or "GOLD"),
                        "source": "strategy_production_console",
                        "source_fill_id": f"strategy-start-cleanup:{cycle_id}:{position.get('trade_id')}",
                        "strategy_plan_id": adjusted["strategy_plan_id"],
                        "strategy_plan_version": adjusted["version"],
                    })
                self._advance_selected_execution(
                    adapter,
                    cycle_id,
                    market=market,
                    now=now,
                    identity="strategy-start-cleanup",
                )
                cleanup_terminal = adapter.snapshot(cycle_id)
                remaining_orders = [
                    row for row in cleanup_terminal.get("orders") or []
                    if str(row.get("state") or "").lower() == "accepted"
                ]
                remaining_positions = [
                    row for row in cleanup_terminal.get("positions") or []
                    if str(row.get("status") or "").lower() == "open"
                ]
                if remaining_orders or remaining_positions:
                    raise RuntimeError(
                        "start cleanup left live paper state"
                        f"; accepted_orders={len(remaining_orders)}"
                        f"; open_positions={len(remaining_positions)}"
                    )
            except Exception as cleanup_exc:
                cleanup_errors.append(f"advance: {cleanup_exc}")
            adjusted["status"] = "failed"
            current["status"] = "active"
            try:
                self._write_plan(adjusted)
                self._write_plan(current)
            except Exception as cleanup_exc:
                cleanup_errors.append(f"plan_restore: {cleanup_exc}")
            failure_detail = str(exc)
            accepted_after_cleanup = 0
            accepted_order_count_known = True
            try:
                accepted_after_cleanup = len(self._accepted_orders(cycle_id, adapter=adapter))
            except Exception as cleanup_exc:
                accepted_order_count_known = False
                cleanup_errors.append(f"final_snapshot: {cleanup_exc}")
            if cleanup_errors:
                failure_detail = f"{failure_detail}; start cleanup failed: {'; '.join(cleanup_errors)}"
            self._write_runtime({
                **starting,
                "desired_state": "stopped",
                "actual_state": "error",
                "updated_at": _timestamp(now),
                "last_error": failure_detail,
                "accepted_order_count": accepted_after_cleanup,
                "accepted_order_count_known": accepted_order_count_known,
            })
            raise

        running = {
            **starting,
            "strategy_type": "grid",
            "actual_state": "running",
            "updated_at": _timestamp(now),
            "accepted_order_count": len(accepted),
            "accepted_order_count_known": True,
        }
        self._write_runtime(running)
        return {
            "action": "start",
            "runtime": running,
            "plan": adjusted,
            "preview": preview,
            "orders": receipts,
            "execution_event": execution_event,
            "reconciliation": reconciliation,
            "created_orders": len(receipts),
            "accepted_orders": len(accepted),
            "filled_orders": filled_count,
            "risk_decision": risk_decision,
            "idempotent": False,
        }

    def _start_dca(
        self,
        cycle_id: str,
        body: dict[str, Any],
        *,
        market: dict[str, Any],
        account: dict[str, Any],
        now: str | None,
    ) -> dict[str, Any]:
        """Start one finite Paper DCA round from an immutable preview."""

        current = self.active_plan(cycle_id) or {}
        expected_preview_id = str(body.get("expected_preview_id") or "")
        prepared_start_id = str(body.get("prepared_start_id") or "")
        adapter = build_configured_execution_engine_adapter(
            self.output_root,
            config=self.config,
        )
        adapter_name = str(getattr(adapter, "name", ""))
        if "paper" not in adapter_name:
            raise ValueError("DCA start is Paper-only")
        self._require_paper_execution_tick(
            cycle_id,
            adapter=adapter,
            now=now,
        )
        self._assert_no_unresolved_prior_cycle_runtime(cycle_id)
        prepared: dict[str, Any] | None = None
        if prepared_start_id:
            prepared = self._load_prepared_start(cycle_id, prepared_start_id)
            preview = self._validate_prepared_start(
                cycle_id,
                prepared,
                expected_preview_id=expected_preview_id,
                current=current,
                market=market,
                adapter_name=adapter_name,
                now=now,
            )
        else:
            preview = self._adaptive_start_preview(
                cycle_id,
                body,
                market=market,
                account=account,
                now=now,
            )
        if preview.get("strategy_type") != "dca":
            raise ValueError("DCA start requires a DCA preview")
        if (
            not expected_preview_id
            or expected_preview_id != str(preview.get("preview_id") or "")
            or dca_preview_id(preview) != expected_preview_id
        ):
            raise ValueError("strategy_preview_changed")
        acknowledgement = _validated_dca_acknowledgement(preview, body, now=now)

        runtime = self.runtime_state(cycle_id)
        if (
            runtime.get("desired_state") == "running"
            and runtime.get("strategy_type") == "dca"
            and runtime.get("preview_id") == preview["preview_id"]
            and current.get("strategy_type") == "dca"
        ):
            lifecycle = DcaPaperLifecycle(self.output_root, adapter)
            state = lifecycle.reconcile(current, timestamp=_timestamp(now))
            snapshot = adapter.snapshot(cycle_id)
            accepted = [
                row
                for row in snapshot.get("orders") or []
                if str(row.get("state") or "").lower() == "accepted"
            ]
            return {
                "action": "start",
                "runtime": runtime,
                "plan": current,
                "preview": preview,
                "dca_lifecycle": state,
                "created_orders": 0,
                "accepted_orders": len(accepted),
                "idempotent": True,
            }
        snapshot = adapter.snapshot(cycle_id)
        pending = [
            row
            for row in snapshot.get("orders") or []
            if str(row.get("state") or "").lower() == "accepted"
        ]
        open_positions = [
            row
            for row in snapshot.get("positions") or []
            if str(row.get("status") or "").lower() == "open"
        ]
        if runtime.get("desired_state") == "running":
            raise ValueError("robot is already running; stop it before changing strategy")
        if pending or open_positions:
            raise ValueError(
                "new DCA start requires zero accepted orders and zero open positions"
            )

        timestamp = _timestamp(now)
        version = self._next_plan_version(cycle_id)
        plan_id = _plan_id(cycle_id, version, preview["preview_id"])
        adjusted = build_dca_strategy_plan(
            preview,
            strategy_plan_id=plan_id,
            version=version,
            locked_at=timestamp,
        )
        risk_payload = {
            "schema_version": "dca-risk-decision-v1",
            "cycle_id": cycle_id,
            "strategy_plan_id": plan_id,
            "preview_id": preview["preview_id"],
            "outcome": "acknowledged" if preview["risk"]["risk_flags"] else "approved",
            "scope": "paper_only",
            "risk": dict(preview["risk"]),
            "acknowledgement": acknowledgement,
            "evaluated_at": timestamp,
        }
        risk_payload["decision_id"] = _content_id("dca-risk", risk_payload)
        adjusted["risk_decision_id"] = risk_payload["decision_id"]
        adjusted["risk_acknowledgement"] = acknowledgement
        self._write_dca_risk_decision(cycle_id, risk_payload)
        self._assert_rollover_start_guard(body)
        self._activate_plan(adjusted)
        starting = {
            **runtime,
            "cycle_id": cycle_id,
            "desired_state": "running",
            "actual_state": "starting",
            "updated_at": timestamp,
            "last_action": "start",
            "last_error": None,
            "strategy_type": "dca",
            "strategy_plan_id": adjusted["strategy_plan_id"],
            "strategy_plan_version": adjusted["version"],
            "preview_id": preview["preview_id"],
            "prepared_start_id": prepared_start_id or None,
            "risk_decision_id": risk_payload["decision_id"],
            "risk_policy_id": "paper-dca-explicit-consent-v1",
            "accepted_order_count": 0,
            "accepted_order_count_known": True,
        }
        self._write_runtime(starting)
        lifecycle = DcaPaperLifecycle(self.output_root, adapter)
        try:
            started = lifecycle.start(adjusted, timestamp=timestamp)
            event = self._market_event(
                cycle_id,
                market=market,
                now=now,
                identity="strategy-dca-start",
            )
            advanced = lifecycle.process_market_event(adjusted, event)
            terminal = adapter.snapshot(cycle_id)
            reconciliation = adapter.reconcile(cycle_id)
            if reconciliation.get("status") != "ok":
                raise ValueError("paper ledger reconciliation failed")
        except Exception as exc:
            cleanup_error = ""
            try:
                cleanup_now = (
                    parse_utc(timestamp) + timedelta(seconds=1)
                ).isoformat()
                self._stop(
                    cycle_id,
                    market=market,
                    now=cleanup_now,
                )
            except Exception as cleanup_exc:
                cleanup_error = str(cleanup_exc)
            adjusted["status"] = "failed"
            self._write_plan(adjusted)
            if current:
                current["status"] = "active"
                self._write_plan(current)
            failure_detail = str(exc)
            if cleanup_error:
                failure_detail += f"; DCA start cleanup failed: {cleanup_error}"
            accepted_after_cleanup = 0
            accepted_order_count_known = True
            try:
                accepted_after_cleanup = len(
                    self._accepted_orders(cycle_id, adapter=adapter)
                )
            except Exception as snapshot_exc:
                accepted_order_count_known = False
                failure_detail += f"; final_snapshot: {snapshot_exc}"
            self._write_runtime({
                **starting,
                "desired_state": "stopped",
                "actual_state": "error",
                "updated_at": _timestamp(now),
                "last_error": failure_detail,
                "accepted_order_count": accepted_after_cleanup,
                "accepted_order_count_known": accepted_order_count_known,
            })
            raise
        accepted = [
            row
            for row in terminal.get("orders") or []
            if str(row.get("state") or "").lower() == "accepted"
        ]
        filled = [
            row
            for row in terminal.get("orders") or []
            if str(row.get("state") or "").lower() == "filled"
            and str(row.get("strategy_plan_id") or "") == plan_id
        ]
        lifecycle_state = dict(advanced.get("state") or {})
        running = {
            **starting,
            "actual_state": "running",
            "updated_at": _timestamp(now),
            "accepted_order_count": len(accepted),
            "dca_lifecycle_status": lifecycle_state.get("status"),
            "dca_lifecycle_path": str(lifecycle._path(cycle_id)),
        }
        self._write_runtime(running)
        return {
            "action": "start",
            "runtime": running,
            "plan": adjusted,
            "preview": preview,
            "orders": started["receipts"],
            "execution_event": advanced.get("engine_result"),
            "dca_lifecycle": lifecycle_state,
            "reconciliation": reconciliation,
            "created_orders": len(started["receipts"]),
            "accepted_orders": len(accepted),
            "filled_orders": len(filled),
            "risk_decision": risk_payload,
            "idempotent": False,
        }

    def _replace_grid(
        self,
        cycle_id: str,
        body: dict[str, Any],
        *,
        market: dict[str, Any],
        account: dict[str, Any],
        now: str | None,
    ) -> dict[str, Any]:
        """Stop the old paper grid and atomically activate one staged replacement."""

        expected_plan_id = str(body.get("expected_strategy_plan_id") or "")
        try:
            expected_plan_version = int(
                body.get("expected_strategy_plan_version") or 0
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("strategy_plan_changed") from exc
        expected_preview_id = str(body.get("expected_preview_id") or "")
        handle = str(body.get("handle") or "")
        requested_range = (
            dict(body.get("range") or {})
            if isinstance(body.get("range"), dict)
            else {}
        )
        recalculate = body.get("recalculate_notional_by_risk_budget") is True
        expected_execution = self._expected_execution_sets(
            body.get("expected_execution")
        )
        if not expected_plan_id or expected_plan_version <= 0:
            raise ValueError("strategy_plan_changed")
        if not expected_preview_id:
            raise ValueError("strategy_preview_changed")
        supplied_acknowledgement = (
            dict(body.get("risk_acknowledgements") or {})
            if isinstance(body.get("risk_acknowledgements"), dict)
            else {}
        )
        supplied_acknowledgement_codes = sorted(
            {
                str(code)
                for code in supplied_acknowledgement.get("codes") or []
                if str(code)
            }
        )
        supplied_facts_digest = str(
            supplied_acknowledgement.get("facts_digest") or ""
        )
        supplied_risk_snapshot_digest = str(
            supplied_acknowledgement.get("risk_snapshot_digest") or ""
        )
        request_fingerprint = _range_replacement_request_fingerprint(
            expected_plan_id,
            expected_plan_version,
            expected_preview_id,
            handle=handle,
            requested_range=requested_range,
            recalculate_notional=recalculate,
            expected_execution=expected_execution,
            acknowledgement_codes=supplied_acknowledgement_codes,
            acknowledgement_facts_digest=supplied_facts_digest,
            risk_snapshot_digest=supplied_risk_snapshot_digest,
        )
        prior = self._replacement_plan(cycle_id, request_fingerprint)
        runtime = self.runtime_state(cycle_id)
        current = self.active_plan(cycle_id)
        if prior and str(prior.get("status") or "") == "active":
            return self._reconcile_completed_replacement(
                cycle_id,
                prior,
                market=market,
                now=now,
            )
        if (
            prior
            and str(prior.get("status") or "") == "staging"
            and current
            and str(current.get("strategy_plan_id") or "") == expected_plan_id
            and int(current.get("version") or 0) == expected_plan_version
            and runtime.get("actual_state") in {"stopped", "replanning", "error"}
        ):
            return self._resume_staged_replacement(
                cycle_id,
                current=current,
                staged=prior,
                market=market,
                account=account,
                now=now,
            )
        if (
            not current
            or str(current.get("strategy_plan_id") or "") != expected_plan_id
            or int(current.get("version") or 0) != expected_plan_version
            or str(runtime.get("strategy_plan_id") or "") != expected_plan_id
            or int(runtime.get("strategy_plan_version") or 0)
            != expected_plan_version
            or runtime.get("desired_state") != "running"
            or runtime.get("actual_state") != "running"
        ):
            raise ValueError("strategy_plan_changed")

        self._assert_expected_execution(
            cycle_id,
            body.get("expected_execution"),
            expected_strategy_plan_id=expected_plan_id,
        )
        preview_request = {
            "expected_strategy_plan_id": expected_plan_id,
            "expected_strategy_plan_version": expected_plan_version,
            "handle": handle,
            "range": requested_range,
            "recalculate_notional_by_risk_budget": recalculate,
        }
        latest = self.preview_range_adjustment(
            cycle_id,
            preview_request,
            market=market,
            account=account,
            now=now,
        )
        if str(latest.get("preview_id") or "") != expected_preview_id:
            raise ValueError("strategy_preview_changed")
        acknowledgement = _validated_manual_range_acknowledgement(
            latest,
            body,
            now=now,
        )
        if (
            latest.get("can_apply") is not True
            and latest.get("can_apply_with_acknowledgements") is not True
        ):
            reasons = ",".join(latest.get("confirm_disabled_reasons") or [])
            raise ValueError(f"range_replacement_blocked:{reasons or 'risk_blocked'}")
        if acknowledgement["acknowledgement_codes"] != supplied_acknowledgement_codes:
            raise ValueError("range_risk_acknowledgements_incomplete")

        staged = prior
        if staged is None:
            replacement_in_progress = [
                row
                for row in load_json(self._plans_path(cycle_id))
                if isinstance(row, dict)
                and row.get("status") == "staging"
                and isinstance(row.get("replacement_request"), dict)
            ]
            if replacement_in_progress:
                raise ValueError("replacement_request_in_progress")
            staged = self._plan_from_preview(current, latest["candidate"], now=now)
            next_version = self._next_plan_version(cycle_id)
            if int(staged.get("version") or 0) != next_version:
                staged["version"] = next_version
                staged["strategy_plan_id"] = _plan_id(
                    cycle_id,
                    next_version,
                    str(latest["candidate"]["preview_id"]),
                )
            staged["status"] = "staging"
            staged["replacement_request"] = {
                "schema_version": "grid-replacement-request-v1",
                "request_fingerprint": request_fingerprint,
                "from_plan_id": expected_plan_id,
                "from_plan_version": expected_plan_version,
                "requested_preview_id": expected_preview_id,
                "handle": handle,
                "range": requested_range,
                "recalculate_notional_by_risk_budget": recalculate,
                "expected_execution": {
                    "accepted_order_ids": sorted(expected_execution[0]),
                    "open_position_ids": sorted(expected_execution[1]),
                },
                "risk_acknowledgement": acknowledgement,
                "phase": "prepared",
                "prepared_at": _timestamp(now),
            }
        elif (
            str(staged.get("status") or "") != "staging"
            or str(staged.get("preview_id") or "") != expected_preview_id
            or str((staged.get("replacement_request") or {}).get("from_plan_id") or "")
            != expected_plan_id
        ):
            raise ValueError("replacement_request_conflict")

        timestamp = _timestamp(now)
        adapter = build_configured_execution_engine_adapter(
            self.output_root,
            config=self.config,
        )
        commands = build_plan_grid_entry_commands(staged, timestamp=timestamp)
        accepted_entries = [
            row
            for row in adapter.snapshot(cycle_id).get("orders") or []
            if str(row.get("state") or "").lower() == "accepted"
            and str(row.get("event") or "entry").lower() == "entry"
        ]
        risk_decision = self._authorize_grid_mutation(
            cycle_id,
            action_class="replace_pending",
            intent="replace_grid",
            plan=staged,
            commands=commands,
            account=account,
            market=market,
            adapter=adapter,
            timestamp=timestamp,
            replaced_order_ids=_required_order_ids(
                accepted_entries,
                "grid replacement entry",
            ),
            retained_order_ids=[],
            manual_override=acknowledgement,
        )
        self._assert_expected_execution(
            cycle_id,
            body.get("expected_execution"),
            expected_strategy_plan_id=expected_plan_id,
        )
        staged["replacement_request"] = {
            **dict(staged.get("replacement_request") or {}),
            "risk_decision_id": risk_decision["decision_id"],
            "risk_policy_id": (risk_decision.get("policy") or {}).get("policy_id"),
        }
        # The durable candidate and request fingerprint exist before any order,
        # position, active-plan, or runtime mutation begins.
        self._write_plan(staged)
        try:
            stopped = self._stop(cycle_id, market=market, now=now)
        except Exception as exc:
            staged["replacement_request"] = {
                **dict(staged.get("replacement_request") or {}),
                "phase": "stop_failed",
                "last_error": str(exc),
                "updated_at": _timestamp(now),
            }
            self._write_plan(staged)
            raise

        staged["replacement_request"] = {
            **dict(staged.get("replacement_request") or {}),
            "phase": "old_grid_stopped",
            "stopped_at": _timestamp(now),
            "cancelled_orders": int(stopped.get("cancelled_orders") or 0),
            "flattened_positions": int(stopped.get("flattened_positions") or 0),
        }
        self._write_plan(staged)
        try:
            return self._launch_staged_replacement(
                cycle_id,
                current=current,
                staged=staged,
                stopped=stopped,
                market=market,
                account=account,
                now=now,
            )
        except Exception as exc:
            # Submission/activation failures are handled inside the launcher.
            # This outer boundary covers the second market/preview/risk checks
            # that can reject after the old grid has already stopped.
            if str(staged.get("status") or "") == "staging":
                adapter = build_configured_execution_engine_adapter(
                    self.output_root,
                    config=self.config,
                )
                cleanup_error = self._cleanup_staged_replacement(
                    cycle_id,
                    staged,
                    adapter=adapter,
                    market=market,
                    now=now,
                )
                staged["status"] = "failed"
                staged["replacement_request"] = {
                    **dict(staged.get("replacement_request") or {}),
                    "phase": "failed",
                    "last_error": str(exc),
                    "cleanup_error": cleanup_error or None,
                    "updated_at": _timestamp(now),
                }
                self._write_plan(staged)
                failure_detail = str(exc)
                if cleanup_error:
                    failure_detail = (
                        f"{failure_detail}; staged cleanup failed: {cleanup_error}"
                    )
                runtime_after = self.runtime_state(cycle_id)
                self._write_runtime({
                    **runtime_after,
                    "desired_state": "stopped",
                    "actual_state": "error",
                    "updated_at": _timestamp(now),
                    "last_action": "replace_grid",
                    "last_error": failure_detail,
                    "accepted_order_count": len(
                        self._accepted_orders(cycle_id, adapter=adapter)
                    ),
                    "accepted_order_count_known": True,
                })
            raise

    def _resume_staged_replacement(
        self,
        cycle_id: str,
        *,
        current: dict[str, Any],
        staged: dict[str, Any],
        market: dict[str, Any],
        account: dict[str, Any],
        now: str | None,
    ) -> dict[str, Any]:
        """Converge a same-fingerprint retry after a process-level interruption."""

        timestamp = _timestamp(now)
        manual_override = _staged_manual_range_override(staged)
        latest_price = _positive_number(market.get("latest_close"), "market latest_close")
        candidate_range = dict(staged.get("range") or {})
        if market_outside_range_requires_blocker(
            direction=str(staged.get("direction") or "neutral"),
            market_price=latest_price,
            range_low=_positive_number(
                candidate_range.get("low"), "replacement range low"
            ),
            range_high=_positive_number(
                candidate_range.get("high"), "replacement range high"
            ),
        ) and not _manual_override_allows(
            manual_override,
            "market_price_outside_range",
        ):
            raise ValueError("market_outside_requested_range")
        rebuilt = (
            build_grid_preview(
                cycle_id,
                _grid_preview_payload_from_plan(staged),
                market=market,
                account=account,
                config=self.config,
                allow_unsafe_manual_preview=True,
            )
            if manual_override and manual_override.get("overridden_blocker_codes")
            else self.preview(
                cycle_id,
                _grid_preview_payload_from_plan(staged),
                market=market,
                account=account,
            )
        )
        if str(rebuilt.get("preview_id") or "") != str(staged.get("preview_id") or ""):
            raise ValueError("strategy_preview_changed")

        adapter = build_configured_execution_engine_adapter(
            self.output_root,
            config=self.config,
        )
        snapshot = adapter.snapshot(cycle_id)
        active_rows = [
            row
            for row in [
                *(snapshot.get("orders") or []),
                *(snapshot.get("positions") or []),
            ]
            if (
                str(row.get("state") or "").lower() == "accepted"
                or str(row.get("status") or "").lower() == "open"
            )
        ]
        staged_plan_id = str(staged.get("strategy_plan_id") or "")
        if any(
            str(row.get("strategy_plan_id") or "") != staged_plan_id
            for row in active_rows
        ):
            raise ValueError("execution_state_changed_during_replacement_recovery")

        commands = build_plan_grid_entry_commands(staged, timestamp=timestamp)
        accepted_entries = [
            row
            for row in snapshot.get("orders") or []
            if str(row.get("state") or "").lower() == "accepted"
            and str(row.get("event") or "entry").lower() == "entry"
        ]
        replaced_ids = _required_order_ids(
            accepted_entries,
            "replacement recovery entry",
        )
        risk_decision = self._authorize_grid_mutation(
            cycle_id,
            action_class=("replace_pending" if active_rows else "increase_exposure"),
            intent="replace_grid",
            plan=staged,
            commands=commands,
            account=account,
            market=market,
            adapter=adapter,
            timestamp=timestamp,
            replaced_order_ids=replaced_ids,
            retained_order_ids=[],
            manual_override=manual_override,
        )
        staged["replacement_request"] = {
            **dict(staged.get("replacement_request") or {}),
            "phase": "launch_authorized",
            "recovery_started_at": timestamp,
            "risk_decision_id": risk_decision["decision_id"],
            "risk_policy_id": (risk_decision.get("policy") or {}).get("policy_id"),
        }
        self._write_plan(staged)
        replanning = {
            **self.runtime_state(cycle_id),
            "cycle_id": cycle_id,
            "desired_state": "running",
            "actual_state": "replanning",
            "updated_at": timestamp,
            "last_action": "replace_grid",
            "last_error": None,
            "strategy_plan_id": staged_plan_id,
            "strategy_plan_version": staged["version"],
            "preview_id": staged["preview_id"],
            "risk_decision_id": risk_decision["decision_id"],
            "risk_policy_id": (risk_decision.get("policy") or {}).get("policy_id"),
            "accepted_order_count": len(accepted_entries),
            "accepted_order_count_known": True,
        }
        self._write_runtime(replanning)
        before_ids = {
            str(row.get("order_id") or "")
            for row in snapshot.get("orders") or []
        }
        try:
            receipts = self._submit_plan_orders(
                adapter,
                staged,
                timestamp=timestamp,
                commands=commands,
                allow_terminal_retry=True,
            )
            submitted_ids = [str(row.get("order_id") or "") for row in receipts]
            if (
                any(not order_id for order_id in submitted_ids)
                or len(set(submitted_ids)) != len(submitted_ids)
            ):
                raise ValueError("replacement recovery requires unique order IDs")
            terminal = self._validate_replacement_recovery_snapshot(
                adapter,
                cycle_id,
                submitted_ids=set(submitted_ids),
                strategy_plan_id=staged_plan_id,
            )
            reconciliation = adapter.reconcile(cycle_id)
            if reconciliation.get("status") != "ok":
                raise ValueError("paper ledger reconciliation failed")
            self._activate_staged_range_plan(
                staged,
                expected_active_plan_id=str(current["strategy_plan_id"]),
            )
        except Exception as exc:
            cleanup_error = self._cleanup_staged_replacement(
                cycle_id,
                staged,
                adapter=adapter,
                market=market,
                now=now,
            )
            staged["status"] = "failed"
            staged["replacement_request"] = {
                **dict(staged.get("replacement_request") or {}),
                "phase": "failed",
                "last_error": str(exc),
                "cleanup_error": cleanup_error or None,
                "updated_at": _timestamp(now),
            }
            self._write_plan(staged)
            failure_detail = str(exc)
            if cleanup_error:
                failure_detail = f"{failure_detail}; staged cleanup failed: {cleanup_error}"
            self._write_runtime({
                **replanning,
                "desired_state": "stopped",
                "actual_state": "error",
                "updated_at": _timestamp(now),
                "last_error": failure_detail,
                "accepted_order_count": len(
                    self._accepted_orders(cycle_id, adapter=adapter)
                ),
            })
            raise

        accepted = sum(
            str(terminal[order_id].get("state") or "").lower() == "accepted"
            for order_id in submitted_ids
        )
        staged["replacement_request"] = {
            **dict(staged.get("replacement_request") or {}),
            "phase": "complete",
            "completed_at": _timestamp(now),
            "recovered": True,
        }
        self._write_plan(staged)
        running = {
            **replanning,
            "actual_state": "running",
            "updated_at": _timestamp(now),
            "accepted_order_count": accepted,
        }
        self._write_runtime(running)
        return {
            "action": "replace_grid",
            "runtime": running,
            "plan": staged,
            "preview": rebuilt,
            "stopped": True,
            "cancelled_orders": int(
                (staged.get("replacement_request") or {}).get("cancelled_orders")
                or 0
            ),
            "flattened_positions": int(
                (staged.get("replacement_request") or {}).get("flattened_positions")
                or 0
            ),
            "created_orders": sum(order_id not in before_ids for order_id in submitted_ids),
            "accepted_orders": accepted,
            "filled_orders": len(submitted_ids) - accepted,
            "reconciliation": reconciliation,
            "risk_decision": risk_decision,
            "idempotent": False,
            "recovered": True,
        }

    def _launch_staged_replacement(
        self,
        cycle_id: str,
        *,
        current: dict[str, Any],
        staged: dict[str, Any],
        stopped: dict[str, Any],
        market: dict[str, Any],
        account: dict[str, Any],
        now: str | None,
    ) -> dict[str, Any]:
        timestamp = _timestamp(now)
        manual_override = _staged_manual_range_override(staged)
        latest_price = _positive_number(market.get("latest_close"), "market latest_close")
        candidate_range = dict(staged.get("range") or {})
        if market_outside_range_requires_blocker(
            direction=str(staged.get("direction") or "neutral"),
            market_price=latest_price,
            range_low=_positive_number(
                candidate_range.get("low"), "replacement range low"
            ),
            range_high=_positive_number(
                candidate_range.get("high"), "replacement range high"
            ),
        ) and not _manual_override_allows(
            manual_override,
            "market_price_outside_range",
        ):
            raise ValueError("market_outside_requested_range")
        rebuilt = (
            build_grid_preview(
                cycle_id,
                _grid_preview_payload_from_plan(staged),
                market=market,
                account=account,
                config=self.config,
                allow_unsafe_manual_preview=True,
            )
            if manual_override and manual_override.get("overridden_blocker_codes")
            else self.preview(
                cycle_id,
                _grid_preview_payload_from_plan(staged),
                market=market,
                account=account,
            )
        )
        if str(rebuilt.get("preview_id") or "") != str(staged.get("preview_id") or ""):
            raise ValueError("strategy_preview_changed")

        adapter = build_configured_execution_engine_adapter(
            self.output_root,
            config=self.config,
        )
        before = adapter.snapshot(cycle_id)
        if [
            row
            for row in before.get("orders") or []
            if str(row.get("state") or "").lower() == "accepted"
        ] or [
            row
            for row in before.get("positions") or []
            if str(row.get("status") or "").lower() == "open"
        ]:
            raise ValueError("execution_state_changed_after_stop")
        commands = build_plan_grid_entry_commands(staged, timestamp=timestamp)
        risk_decision = self._authorize_grid_mutation(
            cycle_id,
            action_class="increase_exposure",
            intent="start_replacement_grid",
            plan=staged,
            commands=commands,
            account=account,
            market=market,
            adapter=adapter,
            timestamp=timestamp,
            manual_override=manual_override,
        )
        staged["replacement_request"] = {
            **dict(staged.get("replacement_request") or {}),
            "phase": "launch_authorized",
            "launch_authorized_at": timestamp,
            "risk_decision_id": risk_decision["decision_id"],
            "risk_policy_id": (risk_decision.get("policy") or {}).get("policy_id"),
        }
        self._write_plan(staged)
        replanning = {
            **self.runtime_state(cycle_id),
            "cycle_id": cycle_id,
            "desired_state": "running",
            "actual_state": "replanning",
            "updated_at": timestamp,
            "last_action": "replace_grid",
            "last_error": None,
            "strategy_plan_id": staged["strategy_plan_id"],
            "strategy_plan_version": staged["version"],
            "preview_id": staged["preview_id"],
            "risk_decision_id": risk_decision["decision_id"],
            "risk_policy_id": (risk_decision.get("policy") or {}).get("policy_id"),
            "accepted_order_count": 0,
            "accepted_order_count_known": True,
        }
        self._write_runtime(replanning)
        receipts: list[dict[str, Any]] = []
        try:
            receipts = self._submit_plan_orders(
                adapter,
                staged,
                timestamp=timestamp,
                commands=commands,
            )
            submitted_ids = {str(row.get("order_id") or "") for row in receipts}
            if "" in submitted_ids or len(submitted_ids) != len(receipts):
                raise ValueError("replacement receipts require valid unique order IDs")
            self._validate_start_grid_snapshot(
                adapter,
                cycle_id,
                submitted_ids=submitted_ids,
            )
            execution_event = self._advance_selected_execution(
                adapter,
                cycle_id,
                market=market,
                now=now,
                identity="strategy-replacement",
            )
            terminal = self._validate_start_grid_snapshot(
                adapter,
                cycle_id,
                submitted_ids=submitted_ids,
            )
            reconciliation = adapter.reconcile(cycle_id)
            if reconciliation.get("status") != "ok":
                raise ValueError("paper ledger reconciliation failed")
            self._activate_staged_range_plan(
                staged,
                expected_active_plan_id=str(current["strategy_plan_id"]),
            )
        except Exception as exc:
            cleanup_error = self._cleanup_staged_replacement(
                cycle_id,
                staged,
                adapter=adapter,
                market=market,
                now=now,
            )
            staged["status"] = "failed"
            staged["replacement_request"] = {
                **dict(staged.get("replacement_request") or {}),
                "phase": "failed",
                "last_error": str(exc),
                "cleanup_error": cleanup_error or None,
                "updated_at": _timestamp(now),
            }
            self._write_plan(staged)
            failure_detail = str(exc)
            if cleanup_error:
                failure_detail = f"{failure_detail}; staged cleanup failed: {cleanup_error}"
            remaining = len(self._accepted_orders(cycle_id, adapter=adapter))
            self._write_runtime({
                **replanning,
                "desired_state": "stopped",
                "actual_state": "error",
                "updated_at": _timestamp(now),
                "last_error": failure_detail,
                "accepted_order_count": remaining,
            })
            raise

        accepted = sum(
            str(terminal[order_id].get("state") or "").lower() == "accepted"
            for order_id in submitted_ids
        )
        filled = len(submitted_ids) - accepted
        staged["replacement_request"] = {
            **dict(staged.get("replacement_request") or {}),
            "phase": "complete",
            "completed_at": _timestamp(now),
        }
        self._write_plan(staged)
        running = {
            **replanning,
            "actual_state": "running",
            "updated_at": _timestamp(now),
            "accepted_order_count": accepted,
        }
        # If this final write fails, the active plan remains a complete durable
        # replacement; an identical retry repairs runtime without resubmission.
        self._write_runtime(running)
        return {
            "action": "replace_grid",
            "runtime": running,
            "plan": staged,
            "preview": rebuilt,
            "stopped": True,
            "cancelled_orders": int(stopped.get("cancelled_orders") or 0),
            "flattened_positions": int(stopped.get("flattened_positions") or 0),
            "created_orders": len(receipts),
            "accepted_orders": accepted,
            "filled_orders": filled,
            "execution_event": execution_event,
            "reconciliation": reconciliation,
            "risk_decision": risk_decision,
            "idempotent": False,
        }

    def _cleanup_staged_replacement(
        self,
        cycle_id: str,
        staged: dict[str, Any],
        *,
        adapter,
        market: dict[str, Any],
        now: str | None,
    ) -> str:
        errors: list[str] = []
        plan_id = str(staged.get("strategy_plan_id") or "")
        try:
            initial = adapter.snapshot(cycle_id)
        except Exception as exc:
            return f"staged cleanup snapshot unavailable: {exc}"
        foreign = [
            row
            for row in [*(initial.get("orders") or []), *(initial.get("positions") or [])]
            if (
                str(row.get("state") or "").lower() == "accepted"
                or str(row.get("status") or "").lower() == "open"
            )
            and str(row.get("strategy_plan_id") or "") != plan_id
        ]
        if foreign:
            try:
                self._cancel_pending(
                    cycle_id,
                    now=now,
                    strategy_plan_id=plan_id,
                    adapter=adapter,
                    reason="replacement_stage_failed_foreign_state",
                )
                terminal = adapter.snapshot(cycle_id)
                staged_left = [
                    row
                    for row in [
                        *(terminal.get("orders") or []),
                        *(terminal.get("positions") or []),
                    ]
                    if (
                        str(row.get("state") or "").lower() == "accepted"
                        or str(row.get("status") or "").lower() == "open"
                    )
                    and str(row.get("strategy_plan_id") or "") == plan_id
                ]
                if not staged_left:
                    return ""
            except Exception as exc:
                return f"foreign paper state prevents staged-only cleanup: {exc}"
            return "foreign paper state prevents complete staged cleanup"
        try:
            self._cancel_pending(
                cycle_id,
                now=now,
                strategy_plan_id=plan_id,
                adapter=adapter,
                reason="replacement_stage_failed",
            )
        except Exception as exc:
            errors.append(f"cancel: {exc}")
        try:
            snapshot = adapter.snapshot(cycle_id)
            price = _positive_number(market.get("latest_close"), "market latest_close")
            for position in snapshot.get("positions") or []:
                if (
                    str(position.get("status") or "").lower() != "open"
                    or str(position.get("strategy_plan_id") or "") != plan_id
                ):
                    continue
                side = "sell" if str(position.get("side") or "").lower() in {"buy", "long"} else "buy"
                adapter.submit_order(normalize_manual_order_command({
                    "cycle_id": cycle_id,
                    "ts": _timestamp(now),
                    "side": side,
                    "event": "flatten",
                    "order_type": "market",
                    "price": price,
                    "market_price": price,
                    "trade_id": position.get("trade_id"),
                    "position_id": position.get("position_id"),
                    "target_position_side": position.get("side"),
                    "target_entry_price": position.get("entry_price"),
                    "symbol": position.get("symbol") or str(market.get("symbol") or "GOLD"),
                    "source": "strategy_production_console",
                    "source_fill_id": f"replacement-cleanup:{cycle_id}:{position.get('trade_id')}",
                    "strategy_plan_id": plan_id,
                    "strategy_plan_version": staged.get("version"),
                }, config=self.config))
            terminal = adapter.snapshot(cycle_id)
            staged_orders = [
                row
                for row in terminal.get("orders") or []
                if str(row.get("state") or "").lower() == "accepted"
                and str(row.get("strategy_plan_id") or "") == plan_id
            ]
            staged_positions = [
                row
                for row in terminal.get("positions") or []
                if str(row.get("status") or "").lower() == "open"
                and str(row.get("strategy_plan_id") or "") == plan_id
            ]
            if staged_orders or staged_positions:
                raise RuntimeError(
                    "staged replacement cleanup left paper state"
                    f"; orders={len(staged_orders)}; positions={len(staged_positions)}"
                )
        except Exception as exc:
            errors.append(f"flatten: {exc}")
        return "; ".join(errors)

    def _assert_expected_execution(
        self,
        cycle_id: str,
        expected: Any,
        *,
        expected_strategy_plan_id: str,
    ) -> None:
        expected_orders, expected_positions = self._expected_execution_sets(expected)
        adapter = build_configured_execution_engine_adapter(
            self.output_root,
            config=self.config,
        )
        snapshot = adapter.snapshot(cycle_id)
        accepted = [
            row
            for row in snapshot.get("orders") or []
            if str(row.get("state") or "").lower() == "accepted"
        ]
        positions = [
            row
            for row in snapshot.get("positions") or []
            if str(row.get("status") or "").lower() == "open"
        ]
        raw_order_ids = [
            row.get("order_id") or row.get("command_id") or ""
            for row in accepted
        ]
        raw_position_ids = [
            row.get("position_id") or row.get("trade_id") or ""
            for row in positions
        ]
        if any(not isinstance(value, str) for value in [*raw_order_ids, *raw_position_ids]):
            raise ValueError("execution_state_changed")
        current_order_ids = [value.strip() for value in raw_order_ids]
        current_position_ids = [value.strip() for value in raw_position_ids]
        current_orders = set(current_order_ids)
        current_positions = set(current_position_ids)
        foreign = [
            row
            for row in [*accepted, *positions]
            if str(row.get("strategy_plan_id") or "") != expected_strategy_plan_id
        ]
        if (
            "" in current_orders
            or "" in current_positions
            or len(current_orders) != len(current_order_ids)
            or len(current_positions) != len(current_position_ids)
            or current_orders != expected_orders
            or current_positions != expected_positions
            or foreign
        ):
            raise ValueError("execution_state_changed")

    @staticmethod
    def _expected_execution_sets(expected: Any) -> tuple[set[str], set[str]]:
        if not isinstance(expected, dict):
            raise ValueError("execution_state_changed")
        order_ids = expected.get("accepted_order_ids")
        position_ids = expected.get("open_position_ids")
        if not isinstance(order_ids, list) or not isinstance(position_ids, list):
            raise ValueError("execution_state_changed")
        if any(not isinstance(value, str) for value in [*order_ids, *position_ids]):
            raise ValueError("execution_state_changed")
        normalized_orders = [value.strip() for value in order_ids]
        normalized_positions = [value.strip() for value in position_ids]
        if any(not value for value in [*normalized_orders, *normalized_positions]):
            raise ValueError("execution_state_changed")
        orders = set(normalized_orders)
        positions = set(normalized_positions)
        if len(orders) != len(order_ids) or len(positions) != len(position_ids):
            raise ValueError("execution_state_changed")
        return orders, positions

    def _replacement_plan(
        self,
        cycle_id: str,
        request_fingerprint: str,
    ) -> dict[str, Any] | None:
        matches = []
        for row in load_json(self._plans_path(cycle_id)):
            if not isinstance(row, dict):
                continue
            replacement = row.get("replacement_request")
            if not isinstance(replacement, dict):
                continue
            if str(replacement.get("request_fingerprint") or "") == request_fingerprint:
                matches.append(dict(row))
        if len(matches) > 1:
            raise RuntimeError("duplicate replacement request fingerprint")
        return matches[0] if matches else None

    def _reconcile_completed_replacement(
        self,
        cycle_id: str,
        plan: dict[str, Any],
        *,
        market: dict[str, Any],
        now: str | None,
    ) -> dict[str, Any]:
        adapter = build_configured_execution_engine_adapter(
            self.output_root,
            config=self.config,
        )
        reconciliation = adapter.reconcile(cycle_id)
        if reconciliation.get("status") != "ok":
            raise ValueError("paper ledger reconciliation failed")
        snapshot = adapter.snapshot(cycle_id)
        foreign = [
            row
            for row in [*(snapshot.get("orders") or []), *(snapshot.get("positions") or [])]
            if (
                str(row.get("state") or "").lower() == "accepted"
                or str(row.get("status") or "").lower() == "open"
            )
            and str(row.get("strategy_plan_id") or "")
            != str(plan.get("strategy_plan_id") or "")
        ]
        if foreign:
            raise ValueError("execution_state_changed")
        replacement = (
            dict(plan.get("replacement_request") or {})
            if isinstance(plan.get("replacement_request"), dict)
            else {}
        )
        if replacement.get("phase") != "complete":
            plan["replacement_request"] = {
                **replacement,
                "phase": "complete",
                "completed_at": _timestamp(now),
                "recovered": True,
            }
            self._write_plan(plan)
        accepted = sum(
            str(row.get("state") or "").lower() == "accepted"
            for row in snapshot.get("orders") or []
        )
        runtime = {
            **self.runtime_state(cycle_id),
            "cycle_id": cycle_id,
            "desired_state": "running",
            "actual_state": "running",
            "updated_at": _timestamp(now),
            "last_action": "replace_grid",
            "last_error": None,
            "strategy_plan_id": plan["strategy_plan_id"],
            "strategy_plan_version": plan["version"],
            "preview_id": plan.get("preview_id"),
            "accepted_order_count": accepted,
            "accepted_order_count_known": True,
        }
        self._write_runtime(runtime)
        return {
            "action": "replace_grid",
            "runtime": runtime,
            "plan": plan,
            "preview": _grid_preview_payload_from_plan(plan),
            "stopped": False,
            "cancelled_orders": 0,
            "flattened_positions": 0,
            "created_orders": 0,
            "accepted_orders": accepted,
            "reconciliation": reconciliation,
            "idempotent": True,
        }

    def _extend_range(
        self,
        cycle_id: str,
        body: dict[str, Any],
        *,
        market: dict[str, Any],
        account: dict[str, Any],
        now: str | None,
    ) -> dict[str, Any]:
        """Move only whole-grid edges while preserving live internal orders."""

        runtime = self.runtime_state(cycle_id)
        current = self.active_plan(cycle_id)
        if runtime.get("desired_state") != "running":
            raise ValueError("strategy_not_running")
        if not current:
            raise ValueError("strategy_plan_changed")

        expected_plan_id = str(body.get("expected_strategy_plan_id") or "")
        requested = body.get("range") if isinstance(body.get("range"), dict) else {}
        if not expected_plan_id:
            raise ValueError("strategy_plan_changed")
        if requested.get("low") in (None, "") or requested.get("high") in (None, ""):
            raise ValueError("requested_range_missing")
        requested_range = {
            "low": _positive_number(requested.get("low"), "requested grid low"),
            "high": _positive_number(requested.get("high"), "requested grid high"),
        }
        request_fingerprint = _extend_range_request_fingerprint(
            expected_plan_id,
            requested_range,
        )
        current_plan_id = str(current.get("strategy_plan_id") or "")
        if expected_plan_id != current_plan_id:
            prior = (
                dict(current.get("range_adjustment") or {})
                if isinstance(current.get("range_adjustment"), dict)
                else {}
            )
            if (
                str(prior.get("from_plan_id") or "") == expected_plan_id
                and str(prior.get("request_fingerprint") or "") == request_fingerprint
            ):
                repaired_runtime = {
                    **runtime,
                    "actual_state": "running",
                    "last_action": "extend_range",
                    "last_error": None,
                    "strategy_plan_id": current_plan_id,
                    "strategy_plan_version": current.get("version"),
                    "preview_id": current.get("preview_id"),
                    "risk_decision_id": current.get("risk_decision_id"),
                    "risk_policy_id": current.get("risk_policy_id"),
                    "updated_at": _timestamp(now),
                }
                if (
                    runtime.get("actual_state") != "running"
                    or str(runtime.get("strategy_plan_id") or "") != current_plan_id
                ):
                    self._write_runtime(repaired_runtime)
                return {
                    "action": "extend_range",
                    "runtime": repaired_runtime,
                    "plan": current,
                    "effective_range": dict(
                        prior.get("effective_range") or current.get("range") or {}
                    ),
                    "steps": dict(prior.get("steps") or {"low": 0, "high": 0}),
                    "created_orders": 0,
                    "cancelled_orders": 0,
                    "positions_preserved": True,
                    "tp_sl_affected": False,
                    "idempotent": True,
                }
            raise ValueError("strategy_plan_changed")
        if (
            runtime.get("actual_state") != "running"
            or str(runtime.get("strategy_plan_id") or "") != current_plan_id
        ):
            raise ValueError("strategy_plan_changed")

        staged_adjustment = _matching_staged_range_adjustment(
            load_json(self._plans_path(cycle_id)),
            from_plan_id=current_plan_id,
            request_fingerprint=request_fingerprint,
        )
        steps = range_adjustment_steps(current, requested_range)
        if not steps["low"] and not steps["high"]:
            return {
                "action": "extend_range",
                "runtime": runtime,
                "plan": current,
                "effective_range": {
                    "low": float((current.get("range") or {}).get("low")),
                    "high": float((current.get("range") or {}).get("high")),
                },
                "steps": steps,
                "created_orders": 0,
                "cancelled_orders": 0,
                "positions_preserved": True,
                "tp_sl_affected": False,
                "idempotent": True,
            }

        adapter = build_configured_execution_engine_adapter(
            self.output_root,
            config=self.config,
        )
        before = adapter.snapshot(cycle_id)
        before_orders = [dict(row) for row in before.get("orders") or []]
        accepted_before = [
            row
            for row in before_orders
            if str(row.get("state") or "").lower() == "accepted"
        ]
        accepted_entries = [
            row
            for row in accepted_before
            if str(row.get("event") or "entry").lower() == "entry"
        ]
        protections_before = [
            row
            for row in accepted_before
            if str(row.get("event") or "entry").lower() != "entry"
        ]
        positions_before = [dict(row) for row in before.get("positions") or []]
        adjustment = build_range_extension(
            cycle_id,
            current,
            requested_range,
            market=market,
            accepted_entries=accepted_entries,
            positions=positions_before,
            cost_per_side_rate=float(self.config.get("cost_per_side_bp") or 0.5)
            / 10_000.0,
            execution_config=self.config,
        )

        effective_low = float(adjustment["effective_range"]["low"])
        effective_high = float(adjustment["effective_range"]["high"])
        retained_entries = [
            row
            for row in accepted_entries
            if effective_low - 1e-8
            <= _entry_geometry_price(row)
            <= effective_high + 1e-8
        ]
        outside_entries = [
            row
            for row in accepted_entries
            if not (
                effective_low - 1e-8
                <= _entry_geometry_price(row)
                <= effective_high + 1e-8
            )
        ]
        retained_ids = _required_order_ids(retained_entries, "retained entry")
        outside_ids = _required_order_ids(outside_entries, "outside entry")

        timestamp = _timestamp(now)
        version = (
            int(staged_adjustment.get("version") or 0)
            if staged_adjustment
            else self._next_plan_version(cycle_id)
        )
        old_context = (
            dict(current.get("execution_context") or {})
            if isinstance(current.get("execution_context"), dict)
            else {}
        )
        old_market_context = (
            dict(old_context.get("market") or {})
            if isinstance(old_context.get("market"), dict)
            else {}
        )
        adjusted = {
            **current,
            **dict(staged_adjustment or {}),
            "strategy_plan_id": (
                str(staged_adjustment.get("strategy_plan_id") or "")
                if staged_adjustment
                else _plan_id(
                    cycle_id,
                    version,
                    f"extend-range:{adjustment['preview_id']}",
                )
            ),
            "version": version,
            "status": "staging",
            "locked_at": (
                staged_adjustment.get("locked_at") if staged_adjustment else timestamp
            ),
            "range": {
                **dict(current.get("range") or {}),
                **adjustment["effective_range"],
            },
            "grid": adjustment["grid"],
            "execution_context": {
                **old_context,
                "market": {
                    **old_market_context,
                    "price": _positive_number(
                        market.get("latest_close"),
                        "market latest_close",
                    ),
                    "symbol": str(
                        market.get("symbol")
                        or old_market_context.get("symbol")
                        or "GOLD"
                    ),
                    "timestamp": market.get("latest_timestamp"),
                    "provider": market.get("provider"),
                    "timeframe": market.get("timeframe"),
                },
            },
            "field_sources": {
                **dict(current.get("field_sources") or {}),
                "range": "confirmed",
                "grid": "confirmed",
                "risk_budget": "confirmed",
            },
            "preview_id": adjustment["preview_id"],
            "range_adjustment": {
                "from_plan_id": current_plan_id,
                "requested_range": dict(requested_range),
                "request_fingerprint": request_fingerprint,
                "effective_range": dict(adjustment["effective_range"]),
                "steps": dict(adjustment["steps"]),
            },
            "inherited_plan_ids": list(
                dict.fromkeys(
                    [
                        *list(current.get("inherited_plan_ids") or []),
                        current_plan_id,
                    ]
                )
            ),
        }
        if staged_adjustment and (
            str(staged_adjustment.get("preview_id") or "") != adjustment["preview_id"]
            or dict(staged_adjustment.get("range") or {}).get("low")
            != adjustment["effective_range"]["low"]
            or dict(staged_adjustment.get("range") or {}).get("high")
            != adjustment["effective_range"]["high"]
            or list((staged_adjustment.get("grid") or {}).get("levels") or [])
            != list(adjustment["grid"].get("levels") or [])
        ):
            raise RuntimeError("staged range adjustment identity changed")
        edge_plan = {
            **adjusted,
            "grid": {
                **dict(adjusted["grid"]),
                "orders": [dict(row) for row in adjustment["edge_orders"]],
            },
        }
        edge_commands = (
            build_plan_grid_entry_commands(edge_plan, timestamp=timestamp)
            if adjustment["edge_orders"]
            else []
        )
        edge_commands = _rearm_completed_edge_commands(edge_commands, before_orders)
        plan_history = [dict(row) for row in load_json(self._plans_path(cycle_id))]
        retained_commands = _retained_entry_risk_commands(
            retained_entries,
            plan_history=plan_history,
            candidate_plan=adjusted,
            timestamp=timestamp,
        )
        risk_decision = self._authorize_grid_mutation(
            cycle_id,
            action_class="replace_pending",
            intent="adjust_grid_edges",
            plan=adjusted,
            commands=[*retained_commands, *edge_commands],
            account=account,
            market=market,
            adapter=adapter,
            timestamp=timestamp,
            replaced_order_ids=outside_ids,
            retained_order_ids=retained_ids,
        )
        risk_metrics = dict(risk_decision.get("metrics") or {})
        actual_leverage = risk_metrics.get("projected_actual_leverage")
        adjusted["grid"] = {
            **dict(adjusted.get("grid") or {}),
            "actual_leverage": actual_leverage,
        }
        adjusted["risk_budget"] = {
            **dict(current.get("risk_budget") or {}),
            "leverage": adjusted["grid"].get("leverage"),
            "max_loss": risk_metrics.get("projected_max_loss"),
            "estimated_margin": risk_metrics.get("projected_margin"),
            "actual_leverage": actual_leverage,
        }
        adjusted["risk_request_id"] = risk_decision.get("request_id")
        adjusted["risk_decision_id"] = risk_decision.get("decision_id")
        adjusted["risk_policy_id"] = (risk_decision.get("policy") or {}).get(
            "policy_id"
        )
        self._write_plan(adjusted)

        retained_before_core = _entry_core(retained_entries)
        protection_before_core = _protection_core(protections_before)
        position_before_core = _position_core(positions_before)
        receipts: list[dict[str, Any]] = []
        staged_ids: set[str] = set()
        cancelled = 0
        cancellation_started = False
        execution_event: dict[str, Any] | None = None
        reconciliation: dict[str, Any] = {}
        try:
            receipts = self._submit_plan_orders(
                adapter,
                edge_plan,
                timestamp=timestamp,
                commands=edge_commands,
            )
            staged_ids = _required_order_ids(receipts, "staged entry")
            staged_snapshot = {
                str(row.get("order_id") or ""): row
                for row in adapter.snapshot(cycle_id).get("orders") or []
                if row.get("order_id")
            }
            incomplete_stage = sorted(
                order_id
                for order_id in staged_ids
                if order_id not in staged_snapshot
                or str(staged_snapshot[order_id].get("state") or "").lower()
                not in {"accepted", "filled"}
            )
            if incomplete_stage:
                raise ValueError(
                    f"extend_range_stage_incomplete:{incomplete_stage}"
                )

            if outside_ids:
                cancellation_started = True
                cancel_receipt = adapter.cancel_orders(
                    cycle_id,
                    order_ids=outside_ids,
                    ts=timestamp,
                    reason="extend_range_outside",
                )
                cancelled = int(
                    cancel_receipt.get("cancelled_order_count")
                    or len(cancel_receipt.get("cancelled_order_ids") or [])
                )
                if cancelled != len(outside_ids):
                    raise ValueError("extend_range_cancel_incomplete")

            execution_event = self._settle_selected_execution_mutations(
                adapter,
                cycle_id,
            )
            terminal = adapter.snapshot(cycle_id)
            terminal_orders = {
                str(row.get("order_id") or ""): dict(row)
                for row in terminal.get("orders") or []
                if row.get("order_id")
            }
            retained_after = [
                terminal_orders[order_id]
                for order_id in retained_ids
                if order_id in terminal_orders
                and str(terminal_orders[order_id].get("state") or "").lower()
                in {"accepted", "filled"}
            ]
            if set(retained_ids) != {
                str(row.get("order_id") or "") for row in retained_after
            } or _entry_core(retained_after) != retained_before_core:
                raise RuntimeError("extend_range changed an internal entry order")
            if any(
                str(terminal_orders.get(order_id, {}).get("state") or "").lower()
                == "accepted"
                for order_id in outside_ids
            ):
                raise RuntimeError("extend_range left an out-of-range entry order")
            invalid_staged = sorted(
                order_id
                for order_id in staged_ids
                if order_id not in terminal_orders
                or str(terminal_orders[order_id].get("state") or "").lower()
                not in {"accepted", "filled"}
            )
            if invalid_staged:
                raise RuntimeError(f"extend_range lost staged orders: {invalid_staged}")
            terminal_accepted = [
                row
                for row in terminal_orders.values()
                if str(row.get("state") or "").lower() == "accepted"
            ]
            if _protection_core(
                [
                    row
                    for row in terminal_accepted
                    if str(row.get("event") or "entry").lower() != "entry"
                ]
            ) != protection_before_core:
                raise RuntimeError("extend_range changed protection orders")
            if _position_core(
                [dict(row) for row in terminal.get("positions") or []]
            ) != position_before_core:
                raise RuntimeError("extend_range changed open positions")
            reconciliation = adapter.reconcile(cycle_id)
            if reconciliation.get("status") != "ok":
                raise ValueError("paper ledger reconciliation failed")
            self._activate_staged_range_plan(
                adjusted,
                expected_active_plan_id=current_plan_id,
            )
        except Exception as exc:
            cleanup_error = ""
            if not cancellation_started:
                try:
                    adapter.cancel_orders(
                        cycle_id,
                        strategy_plan_id=adjusted["strategy_plan_id"],
                        ts=timestamp,
                        reason="extend_range_stage_failed",
                    )
                    self._settle_selected_execution_mutations(adapter, cycle_id)
                    cleanup = adapter.snapshot(cycle_id)
                    remaining_staged = [
                        row
                        for row in cleanup.get("orders") or []
                        if str(row.get("state") or "").lower() == "accepted"
                        and str(row.get("strategy_plan_id") or "")
                        == adjusted["strategy_plan_id"]
                    ]
                    staged_positions = [
                        row
                        for row in cleanup.get("positions") or []
                        if str(row.get("status") or "").lower() == "open"
                        and str(row.get("strategy_plan_id") or "")
                        == adjusted["strategy_plan_id"]
                    ]
                    if remaining_staged or staged_positions:
                        raise RuntimeError("extend_range cleanup left staged live state")
                    cleanup_accepted = [
                        dict(row)
                        for row in cleanup.get("orders") or []
                        if str(row.get("state") or "").lower() == "accepted"
                    ]
                    if _protection_core(
                        [
                            row
                            for row in cleanup_accepted
                            if str(row.get("event") or "entry").lower() != "entry"
                        ]
                    ) != protection_before_core:
                        raise RuntimeError("extend_range cleanup changed protection orders")
                    if _position_core(
                        [dict(row) for row in cleanup.get("positions") or []]
                    ) != position_before_core:
                        raise RuntimeError("extend_range cleanup changed open positions")
                except Exception as cleanup_exc:
                    cleanup_error = str(cleanup_exc)
            adjusted["status"] = "partial" if cancellation_started else "failed"
            current["status"] = "active"
            self._write_plan(adjusted)
            self._write_plan(current)
            failure_detail = str(exc)
            if cleanup_error:
                failure_detail = (
                    f"{failure_detail}; extend_range cleanup failed: {cleanup_error}"
                )
            accepted_after_failure = len(
                self._accepted_orders(cycle_id, adapter=adapter)
            )
            self._write_runtime(
                {
                    **runtime,
                    "actual_state": (
                        "error"
                        if cancellation_started or cleanup_error
                        else "running"
                    ),
                    "updated_at": _timestamp(now),
                    "last_action": "extend_range",
                    "last_error": failure_detail,
                    "accepted_order_count": accepted_after_failure,
                    "accepted_order_count_known": True,
                }
            )
            raise

        accepted_after = self._accepted_orders(cycle_id, adapter=adapter)
        running = {
            **runtime,
            "actual_state": "running",
            "updated_at": _timestamp(now),
            "last_action": "extend_range",
            "last_error": None,
            "strategy_plan_id": adjusted["strategy_plan_id"],
            "strategy_plan_version": adjusted["version"],
            "preview_id": adjusted["preview_id"],
            "risk_decision_id": risk_decision["decision_id"],
            "risk_policy_id": (risk_decision.get("policy") or {}).get("policy_id"),
            "accepted_order_count": len(accepted_after),
            "accepted_order_count_known": True,
        }
        self._write_runtime(running)
        return {
            "action": "extend_range",
            "runtime": running,
            "plan": adjusted,
            "effective_range": adjustment["effective_range"],
            "steps": adjustment["steps"],
            "orders": receipts,
            "created_orders": len(receipts),
            "cancelled_orders": cancelled,
            "positions_preserved": True,
            "tp_sl_affected": False,
            "execution_event": execution_event,
            "reconciliation": reconciliation,
            "risk_decision": risk_decision,
            "idempotent": False,
        }

    def _regrid(
        self,
        cycle_id: str,
        body: dict[str, Any],
        *,
        market: dict[str, Any],
        account: dict[str, Any],
        now: str | None,
    ) -> dict[str, Any]:
        runtime = self.runtime_state(cycle_id)
        if runtime["desired_state"] != "running":
            raise ValueError("running adjustment requires a running robot")
        current = self.active_plan(cycle_id)
        if not current:
            raise ValueError("cannot adjust without an active StrategyPlan")
        preview = self.preview(cycle_id, body, market=market, account=account)
        if runtime.get("preview_id") == preview["preview_id"]:
            return {
                "action": "adjust_plan",
                "runtime": runtime,
                "plan": current,
                "created_orders": 0,
                "cancelled_orders": 0,
                "idempotent": True,
            }

        adjusted = self._plan_from_preview(current, preview, now=now)
        timestamp = _timestamp(now)
        adapter = build_configured_execution_engine_adapter(self.output_root, config=self.config)
        # Re-read from the selected adapter so risk binding and replacement use
        # exactly one engine state, not two independently constructed views.
        old_orders = self._accepted_orders(cycle_id, adapter=adapter)
        old_order_ids = [str(row.get("order_id") or "") for row in old_orders if row.get("order_id")]
        old_accepted = len(old_order_ids)
        commands = build_plan_grid_entry_commands(adjusted, timestamp=timestamp)
        risk_decision = self._authorize_grid_mutation(
            cycle_id,
            action_class="replace_pending",
            intent="replace_grid",
            plan=adjusted,
            commands=commands,
            account=account,
            market=market,
            adapter=adapter,
            timestamp=timestamp,
            replaced_order_ids=old_order_ids,
        )
        replanning = {
            **runtime,
            "actual_state": "replanning",
            "updated_at": timestamp,
            "last_action": "adjust_plan",
            "last_error": None,
            "strategy_plan_id": adjusted["strategy_plan_id"],
            "strategy_plan_version": adjusted["version"],
            "preview_id": preview["preview_id"],
            "risk_decision_id": risk_decision["decision_id"],
            "risk_policy_id": (risk_decision.get("policy") or {}).get("policy_id"),
            "accepted_order_count": 0,
        }
        self._write_runtime(replanning)
        receipts: list[dict[str, Any]] = []
        try:
            # Two-phase paper replacement: the old grid stays live until every
            # replacement order is accepted. A failed stage is removed without
            # pretending that an engine cancellation can be rolled back.
            receipts = self._submit_plan_orders(adapter, adjusted, timestamp=timestamp, commands=commands)
            cancel_receipt = adapter.cancel_orders(
                cycle_id,
                order_ids=old_order_ids,
                ts=_timestamp(now),
                reason="regrid",
            )
            cancelled = int(
                cancel_receipt.get("cancelled_order_count")
                or len(cancel_receipt.get("cancelled_order_ids") or [])
            )
            if cancelled != old_accepted:
                raise ValueError("regrid did not cancel every previous grid order")
            execution_event = self._advance_selected_execution(
                adapter,
                cycle_id,
                market=market,
                now=now,
                identity="strategy-regrid",
            )
            accepted_ids = {
                str(row.get("order_id") or "")
                for row in self._accepted_orders(cycle_id, adapter=adapter)
            }
            replacement_ids = {str(row.get("order_id") or "") for row in receipts}
            if accepted_ids != replacement_ids:
                raise ValueError("regrid did not leave exactly the replacement grid active")
            self._activate_plan(adjusted)
        except Exception as exc:
            cleanup_error = ""
            try:
                adapter.cancel_orders(
                    cycle_id,
                    strategy_plan_id=adjusted["strategy_plan_id"],
                    ts=_timestamp(now),
                    reason="regrid_stage_failed",
                )
                self._advance_selected_execution(
                    adapter,
                    cycle_id,
                    market=market,
                    now=now,
                    identity="strategy-regrid-cleanup",
                )
            except Exception as cleanup_exc:
                cleanup_error = str(cleanup_exc)
            adjusted["status"] = "failed"
            current["status"] = "active"
            self._write_plan(adjusted)
            self._write_plan(current)
            accepted_after_failure = len(self._accepted_orders(cycle_id))
            failure_detail = str(exc)
            if cleanup_error:
                failure_detail = f"{failure_detail}; regrid cleanup failed: {cleanup_error}"
            self._write_runtime({
                **runtime,
                "actual_state": "running" if not cleanup_error and accepted_after_failure >= old_accepted else "error",
                "updated_at": _timestamp(now),
                "last_action": "adjust_plan",
                "last_error": failure_detail,
                "accepted_order_count": accepted_after_failure,
            })
            raise

        running = {
            **replanning,
            "actual_state": "running",
            "updated_at": _timestamp(now),
            "accepted_order_count": len(receipts),
        }
        self._write_runtime(running)
        return {
            "action": "adjust_plan",
            "runtime": running,
            "plan": adjusted,
            "preview": preview,
            "orders": receipts,
            "execution_event": execution_event,
            "created_orders": len(receipts),
            "cancelled_orders": cancelled,
            "risk_decision": risk_decision,
            "idempotent": False,
        }

    def _submit_plan_orders(
        self,
        adapter,
        plan: dict[str, Any],
        *,
        timestamp: str,
        commands: list[dict[str, Any]] | None = None,
        allow_terminal_retry: bool = False,
    ) -> list[dict[str, Any]]:
        receipts: list[dict[str, Any]] = []
        command_rows = commands if commands is not None else build_plan_grid_entry_commands(plan, timestamp=timestamp)
        for command in command_rows:
            receipt = adapter.submit_order(command)
            state = str(receipt.get("state") or receipt.get("status") or "").lower()
            if state != "accepted" and not (
                allow_terminal_retry and state == "filled"
            ):
                raise ValueError("paper execution did not accept a grid order")
            receipts.append(receipt)
        return receipts

    @staticmethod
    def _validate_replacement_recovery_snapshot(
        adapter,
        cycle_id: str,
        *,
        submitted_ids: set[str],
        strategy_plan_id: str,
    ) -> dict[str, dict[str, Any]]:
        snapshot = adapter.snapshot(cycle_id)
        rows = snapshot.get("orders") or []
        positions = snapshot.get("positions") or []
        if not all(isinstance(row, dict) for row in [*rows, *positions]):
            raise ValueError("replacement recovery snapshot contains invalid rows")
        order_ids = [str(row.get("order_id") or "") for row in rows]
        if (
            any(not order_id for order_id in order_ids)
            or len(set(order_ids)) != len(order_ids)
        ):
            raise ValueError("replacement recovery snapshot requires unique order IDs")
        # launchd intentionally uses macOS system Python 3.9.  The row IDs are
        # derived one-for-one above, so ``strict=True`` adds no safety here and
        # is not available until Python 3.10.
        by_id = dict(zip(order_ids, rows))
        missing = sorted(submitted_ids - set(by_id))
        invalid = sorted(
            order_id
            for order_id in submitted_ids & set(by_id)
            if str(by_id[order_id].get("state") or "").lower()
            not in {"accepted", "filled"}
        )
        foreign = [
            row
            for row in [*rows, *positions]
            if (
                str(row.get("state") or "").lower() == "accepted"
                or str(row.get("status") or "").lower() == "open"
            )
            and str(row.get("strategy_plan_id") or "") != strategy_plan_id
        ]
        unexpected_entries = sorted(
            str(row.get("order_id") or "")
            for row in rows
            if str(row.get("state") or "").lower() == "accepted"
            and str(row.get("event") or "entry").lower() == "entry"
            and str(row.get("order_id") or "") not in submitted_ids
        )
        if missing or invalid or foreign or unexpected_entries:
            raise ValueError(
                "replacement recovery did not converge exact staged state"
                f"; missing={missing}; invalid={invalid}"
                f"; foreign={len(foreign)}"
                f"; unexpected_entries={unexpected_entries}"
            )
        return by_id

    @staticmethod
    def _validate_start_grid_snapshot(
        adapter,
        cycle_id: str,
        *,
        submitted_ids: set[str],
    ) -> dict[str, dict[str, Any]]:
        rows = adapter.snapshot(cycle_id).get("orders") or []
        if not all(isinstance(row, dict) for row in rows):
            raise ValueError("paper start snapshot contains an invalid order row")
        order_ids = [str(row.get("order_id") or "") for row in rows]
        duplicate_snapshot_ids = sorted({
            order_id
            for order_id, count in Counter(order_ids).items()
            if count > 1
        })
        empty_snapshot_count = sum(not order_id.strip() for order_id in order_ids)
        if empty_snapshot_count or duplicate_snapshot_ids:
            raise ValueError(
                "paper start snapshot requires valid unique order IDs"
                f"; empty_count={empty_snapshot_count}"
                f"; duplicate_ids={duplicate_snapshot_ids}"
            )
        orders_by_id = dict(zip(order_ids, rows))
        missing_ids = sorted(submitted_ids - set(orders_by_id))
        invalid_states = sorted(
            order_id
            for order_id in submitted_ids & set(orders_by_id)
            if str(orders_by_id[order_id].get("state") or "").lower()
            not in {"accepted", "filled"}
        )
        unexpected_accepted = sorted(
            order_id
            for order_id, row in orders_by_id.items()
            if order_id not in submitted_ids
            and str(row.get("state") or "").lower() == "accepted"
            and str(row.get("event") or "entry").lower() == "entry"
        )
        if missing_ids or invalid_states or unexpected_accepted:
            raise ValueError(
                "paper start did not observe the complete grid"
                f"; missing={missing_ids}"
                f"; invalid_states={invalid_states}"
                f"; unexpected_accepted={unexpected_accepted}"
            )
        return orders_by_id

    def _authorize_grid_mutation(
        self,
        cycle_id: str,
        *,
        action_class: str,
        intent: str,
        plan: dict[str, Any],
        commands: list[dict[str, Any]],
        account: dict[str, Any],
        market: dict[str, Any],
        adapter,
        timestamp: str,
        replaced_order_ids: list[str] | None = None,
        retained_order_ids: list[str] | None = None,
        manual_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request = self._grid_risk_request(
            cycle_id,
            action_class=action_class,
            intent=intent,
            plan=plan,
            commands=commands,
            account=account,
            market=market,
            adapter=adapter,
            timestamp=timestamp,
            replaced_order_ids=replaced_order_ids,
            retained_order_ids=retained_order_ids,
        )
        decision = self.risk_port.evaluate(request)
        self.risk_store.persist(decision)
        if manual_override:
            _require_acknowledged_paper_grid_risk(
                decision.to_dict(),
                manual_override,
                adapter_name=str(getattr(adapter, "name", "")),
            )
        else:
            require_exposure_permission(decision)
        # Re-read the same authoritative adapter and re-evaluate. Any order,
        # position, account, market, policy, or evaluator drift rejects before
        # candidate plan/runtime/order mutation.
        current_request = self._grid_risk_request(
            cycle_id,
            action_class=action_class,
            intent=intent,
            plan=plan,
            commands=commands,
            account=account,
            market=market,
            adapter=adapter,
            timestamp=timestamp,
            replaced_order_ids=replaced_order_ids,
            retained_order_ids=retained_order_ids,
        )
        current_decision = self.risk_port.evaluate(current_request)
        if (
            current_decision.request_id != decision.request_id
            or current_decision.decision_id != decision.decision_id
        ):
            raise ValueError("risk decision stale: current evaluation changed")
        if manual_override:
            _require_acknowledged_paper_grid_risk(
                current_decision.to_dict(),
                manual_override,
                adapter_name=str(getattr(adapter, "name", "")),
            )
            return {
                **current_decision.to_dict(),
                "operator_override": dict(manual_override),
            }
        return require_exposure_permission(current_decision).to_dict()

    def _grid_risk_request(
        self,
        cycle_id: str,
        *,
        action_class: str,
        intent: str,
        plan: dict[str, Any],
        commands: list[dict[str, Any]],
        account: dict[str, Any],
        market: dict[str, Any],
        adapter,
        timestamp: str,
        replaced_order_ids: list[str] | None,
        retained_order_ids: list[str] | None,
    ):
        execution_snapshot = adapter.snapshot(cycle_id)
        if str(getattr(adapter, "name", "")) == "nautilus_paper":
            execution_snapshot = normalize_nautilus_snapshot_for_accounting(
                self.output_root,
                execution_snapshot,
            )
        return build_grid_risk_request(
            checked_at=timestamp,
            action_class=action_class,
            intent=intent,
            plan=plan,
            commands=commands,
            account_context=account,
            market=market,
            execution_snapshot=execution_snapshot,
            execution_reconciliation=adapter.reconcile(cycle_id),
            policy=self.risk_port.resolve_policy(self.config),
            evaluator=self.risk_port.evaluator_metadata(),
            replaced_order_ids=replaced_order_ids,
            retained_order_ids=retained_order_ids,
        )

    def _stop(
        self,
        cycle_id: str,
        *,
        market: dict[str, Any] | None,
        now: str | None,
        transition_owner: str | None = None,
        expected_runtime_updated_at: str | None = None,
    ) -> dict[str, Any]:
        if expected_runtime_updated_at is not None:
            persisted = self.persisted_runtime_state()
            if (
                persisted.get("cycle_id") != cycle_id
                or str(persisted.get("updated_at") or "") != expected_runtime_updated_at
            ):
                raise ValueError("paper runtime changed after rollover intent")
        previous = self.runtime_state(cycle_id)
        stopping = {
            **previous,
            "cycle_id": cycle_id,
            "desired_state": "stopped",
            "actual_state": "stopping",
            "updated_at": _timestamp(now),
            "last_action": "stop",
            "last_error": None,
            "transition_owner": transition_owner,
        }
        self._write_runtime(stopping)
        adapter = build_configured_execution_engine_adapter(self.output_root, config=self.config)
        # Persist the exact pre-stop identities in the control receipt.  Counts
        # alone cannot prove that a rollover retry did not cancel a different
        # set of orders or flatten a different set of positions after a crash.
        pre_stop_snapshot = adapter.snapshot(cycle_id)
        cancelled_order_ids = sorted({
            str(row.get("order_id") or "")
            for row in pre_stop_snapshot.get("orders") or []
            if str(row.get("state") or "").lower() == "accepted"
            and str(row.get("order_id") or "")
        })
        cancelled = self._cancel_pending(
            cycle_id,
            now=now,
            adapter=adapter,
            reason="stop",
        )
        execution_event = self._settle_safe_action_commands(adapter, cycle_id)
        snapshot = adapter.snapshot(cycle_id)
        open_positions = [row for row in snapshot.get("positions") or [] if row.get("status") == "open"]
        flattened: list[dict[str, Any]] = []
        safe_action_market_gates = [
            build_paper_safe_action_market_gate(
                action_class_for_command({"event": "cancel"}),
                market,
                pricing_source="not_required",
            )
        ]
        try:
            flatten_commands: list[dict[str, Any]] = []
            if open_positions:
                requested_at = _timestamp(now)
                market_config = (
                    self.config.get("market_data")
                    if isinstance(self.config.get("market_data"), dict)
                    else {}
                )
                allow_market_mark = paper_safe_action_market_mark_is_trusted(
                    market,
                    expected_provider=str(market_config.get("provider") or ""),
                )
                execution_event_mark = last_paper_execution_market_event(adapter, cycle_id)
                for position in open_positions:
                    side = "sell" if str(position.get("side") or "") in {"buy", "long"} else "buy"
                    pricing = resolve_paper_safe_action_pricing(
                        market,
                        snapshot,
                        requested_at=requested_at,
                        command={
                            "trade_id": position.get("trade_id"),
                            "position_id": position.get("position_id"),
                        },
                        engine_name=str(getattr(adapter, "name", "legacy_paper")),
                        last_market_event=execution_event_mark,
                        allow_market_mark=allow_market_mark,
                    )
                    command = normalize_manual_order_command({
                        "cycle_id": cycle_id,
                        "ts": pricing["command_timestamp"],
                        "side": side,
                        "event": "flatten",
                        "order_type": "market",
                        "price": pricing["price"],
                        "market_price": pricing["price"],
                        "market_timestamp": pricing["pricing_timestamp"],
                        "market_source": pricing["pricing_provider"],
                        "market_fresh": pricing["pricing_source"] == "fresh_server_mark",
                        "requested_at": requested_at,
                        "trade_id": position.get("trade_id"),
                        "position_id": position.get("position_id"),
                        "target_position_side": position.get("side"),
                        "target_entry_price": position.get("entry_price"),
                        "symbol": position.get("symbol") or str((market or {}).get("symbol") or "GOLD"),
                        "source": "strategy_production_console",
                        "source_fill_id": f"strategy-stop:{cycle_id}:{position.get('trade_id')}",
                        "strategy_plan_id": previous.get("strategy_plan_id"),
                        "strategy_plan_version": previous.get("strategy_plan_version"),
                    }, config=self.config)
                    flatten_gate = build_paper_safe_action_market_gate(
                        action_class_for_command(command),
                        market,
                        pricing_source=pricing["pricing_source"],
                        pricing_price=command["price"],
                        pricing_timestamp=pricing["pricing_timestamp"],
                        pricing_provider=pricing["pricing_provider"],
                    )
                    command["safe_action_market_gate"] = flatten_gate
                    flatten_commands.append(command)
                    safe_action_market_gates.append(flatten_gate)
                flattened.extend(adapter.submit_order(command) for command in flatten_commands)
            fresh_flatten = bool(flatten_commands) and all(
                gate.get("pricing_source") == "fresh_server_mark"
                for gate in safe_action_market_gates[1:]
            )
            if fresh_flatten:
                timestamp = _timestamp(now)
                price = float(flatten_commands[0]["price"])
                trusted_market = market or {}
                execution_event = adapter.process_market_event({
                    "schema_version": "dualtrack-market-event-v1",
                    "event_id": f"strategy-stop:{cycle_id}:{timestamp}",
                    "cycle_id": cycle_id,
                    "ts_event": timestamp,
                    "event_started_at": trusted_market.get("latest_timestamp"),
                    "source": str(
                        trusted_market.get("provider") or trusted_market.get("source_mode") or ""
                    ),
                    "provider": str(trusted_market.get("provider") or ""),
                    "instrument_id": str(
                        ((self.config.get("execution_shadow") or {}).get("nautilus") or {}).get(
                            "execution_instrument_id"
                        )
                        or trusted_market.get("symbol")
                        or ""
                    ),
                    "symbol": str(trusted_market.get("symbol") or "GOLD"),
                    "timeframe": str(trusted_market.get("timeframe") or "1m"),
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "price": price,
                    "fresh": True,
                    "is_synthetic": False,
                })
                flush_shadow = getattr(adapter, "flush_shadow", None)
                if callable(flush_shadow):
                    flush_shadow(cycle_id)
            else:
                execution_event = self._settle_safe_action_commands(adapter, cycle_id)
            terminal = adapter.snapshot(cycle_id)
            if [row for row in terminal.get("orders") or [] if row.get("state") == "accepted"]:
                raise ValueError("paper stop left accepted orders")
            if [row for row in terminal.get("positions") or [] if row.get("status") == "open"]:
                raise ValueError("paper stop left open positions")
            reconciliation = adapter.reconcile(cycle_id)
            if reconciliation.get("status") != "ok":
                raise ValueError("paper ledger reconciliation failed")
        except Exception as exc:
            accepted_after_failure = 0
            accepted_order_count_known = True
            failure_detail = str(exc)
            try:
                accepted_after_failure = len(self._accepted_orders(cycle_id))
            except Exception as snapshot_exc:
                accepted_order_count_known = False
                failure_detail = f"{failure_detail}; final_snapshot: {snapshot_exc}"
            self._write_runtime({
                **stopping,
                "actual_state": "error",
                "updated_at": _timestamp(now),
                "last_error": failure_detail,
                "accepted_order_count": accepted_after_failure,
                "accepted_order_count_known": accepted_order_count_known,
            })
            raise

        stopped = {
            **stopping,
            "actual_state": "stopped",
            "updated_at": _timestamp(now),
            "accepted_order_count": 0,
            "accepted_order_count_known": True,
        }
        dca_lifecycle = None
        lifecycle_warning = None
        active = self.active_plan(cycle_id)
        if previous.get("strategy_type") == "dca" and active and active.get("strategy_type") == "dca":
            try:
                dca_lifecycle = DcaPaperLifecycle(
                    self.output_root,
                    adapter,
                ).reconcile(active, timestamp=_timestamp(now))
                stopped["dca_lifecycle_status"] = dca_lifecycle.get("status")
            except ValueError as exc:
                # Stopping Paper exposure must never be held hostage by an
                # invalid legacy strategy identity.  The cancellation/flatten
                # and engine reconciliation above are authoritative; retain a
                # visible warning for later repair instead of leaving runtime
                # forever in `stopping`.
                lifecycle_warning = f"dca_lifecycle_reconciliation_skipped:{exc}"
                stopped["dca_lifecycle_status"] = "unavailable"
                stopped["lifecycle_warning"] = lifecycle_warning
        self._write_runtime(stopped)
        result = {
            "action": "stop",
            "runtime": stopped,
            "cancelled_orders": cancelled,
            "cancelled_order_ids": cancelled_order_ids,
            "flattened_positions": len(flattened),
            "flattened_position_ids": sorted({
                str(row.get("position_id") or "")
                for row in open_positions
                if str(row.get("position_id") or "")
            }),
            "execution_event": execution_event,
            "reconciliation": reconciliation,
            "historical_records_preserved": True,
            "safe_action_market_gates": safe_action_market_gates,
        }
        if dca_lifecycle is not None:
            result["dca_lifecycle"] = dca_lifecycle
        if lifecycle_warning is not None:
            result["lifecycle_warning"] = lifecycle_warning
        return result

    @staticmethod
    def _settle_safe_action_commands(adapter, cycle_id: str) -> dict[str, Any] | None:
        return settle_paper_safe_action_commands(adapter, cycle_id)

    def _plan_from_preview(self, current: dict[str, Any], preview: dict[str, Any], *, now: str | None) -> dict[str, Any]:
        version = int(current.get("version") or 0) + 1
        plan = {
            **current,
            "strategy_plan_id": _plan_id(str(current["cycle_id"]), version, preview["preview_id"]),
            "version": version,
            "status": "active",
            "strategy_type": "grid",
            "locked_at": _timestamp(now),
            "direction": preview["direction"],
            "style": preview["style"],
            "range": dict(preview["range"]),
            "grid": {
                **preview["grid"],
                "actual_leverage": preview["risk"]["actual_leverage"],
                "orders": [dict(order) for order in preview["orders"]],
            },
            "execution_context": {"market": dict(preview["market"])},
            "tp_sl": {
                "mode": "per_grid",
                "take_profit": "next_grid_level",
                "stop_loss": "one_grid_beyond_range",
                "r_multiple": 1.0,
            },
            "risk_budget": {
                **dict(current.get("risk_budget") or {}),
                "leverage": preview["grid"]["leverage"],
                "actual_leverage": preview["risk"]["actual_leverage"],
                "max_loss": preview["risk"]["max_loss"],
                "estimated_margin": preview["risk"]["estimated_margin"],
            },
            "field_sources": {
                **dict(current.get("field_sources") or {}),
                "direction": "confirmed",
                "range": "confirmed",
                "grid": "confirmed",
                "tp_sl": "confirmed",
                "risk_budget": "confirmed",
            },
            "preview_id": preview["preview_id"],
        }
        # A Grid plan is a distinct contract from DCA.  In particular, a
        # terminal DCA plan may be the current version used to seed a Grid
        # preview, but its aggregate-entry geometry must never leak into the
        # new Grid's execution routing or stop path.
        plan.pop("dca", None)
        plan.pop("dca_lifecycle", None)
        return plan

    def _accepted_orders(self, cycle_id: str, *, adapter=None) -> list[dict[str, Any]]:
        execution = adapter or build_configured_execution_engine_adapter(self.output_root, config=self.config)
        snapshot = execution.snapshot(cycle_id)
        return [
            row
            for row in snapshot.get("orders") or []
            if str(row.get("state") or "").lower() == "accepted"
        ]

    def _advance_selected_execution(
        self,
        adapter,
        cycle_id: str,
        *,
        market: dict[str, Any] | None,
        now: str | None,
        identity: str,
    ) -> dict[str, Any] | None:
        """Make accepted mutations observable before an active Nautilus control returns."""

        if str(getattr(adapter, "name", "")) != "nautilus_paper":
            return None
        return adapter.process_market_event(
            self._market_event(
                cycle_id,
                market=market or {},
                now=now,
                identity=identity,
            )
        )

    def advance_dca_market_event(
        self,
        cycle_id: str,
        event: dict[str, Any],
        *,
        adapter=None,
    ) -> dict[str, Any]:
        """Advance the active Paper DCA round and publish its runtime state."""

        with production_mutation_lock(self.output_root):
            plan = self.active_plan(cycle_id)
            runtime = self.runtime_state(cycle_id)
            if (
                not plan
                or plan.get("strategy_type") != "dca"
                or runtime.get("strategy_type") != "dca"
                or runtime.get("actual_state") != "running"
            ):
                raise ValueError("active running DCA StrategyPlan is required")
            execution = adapter or build_configured_execution_engine_adapter(
                self.output_root,
                config=self.config,
            )
            if "paper" not in str(getattr(execution, "name", "")):
                raise ValueError("DCA market advancement is Paper-only")
            result = DcaPaperLifecycle(
                self.output_root,
                execution,
            ).process_market_event(plan, event)
            state = dict(result.get("state") or {})
            snapshot = execution.snapshot(cycle_id)
            accepted = [
                row
                for row in snapshot.get("orders") or []
                if str(row.get("state") or "").lower() == "accepted"
            ]
            terminal = str(state.get("status") or "") in {
                "target_closed",
                "stop_closed",
                "flattened",
                "closed",
            }
            published = {
                **runtime,
                "desired_state": "stopped" if terminal else "running",
                "actual_state": "stopped" if terminal else "running",
                "updated_at": str(event.get("ts_event") or _timestamp(None)),
                "last_action": "dca_round_closed" if terminal else "dca_market_event",
                "last_error": None,
                "accepted_order_count": len(accepted),
                "accepted_order_count_known": True,
                "dca_lifecycle_status": state.get("status"),
            }
            self._write_runtime(published)
            return {**result, "runtime": published}

    def _market_event(
        self,
        cycle_id: str,
        *,
        market: dict[str, Any],
        now: str | None,
        identity: str,
    ) -> dict[str, Any]:
        _validate_market(market)
        price = _positive_number(market.get("latest_close"), "market latest_close")
        timestamp = _timestamp(now)
        return {
            "schema_version": "dualtrack-market-event-v1",
            "event_id": f"{identity}:{cycle_id}:{timestamp}",
            "cycle_id": cycle_id,
            "ts_event": timestamp,
            "event_started_at": market.get("latest_timestamp"),
            "source": str(market.get("provider") or market.get("source_mode") or ""),
            "provider": str(market.get("provider") or ""),
            "instrument_id": str(
                ((self.config.get("execution_shadow") or {}).get("nautilus") or {}).get(
                    "execution_instrument_id"
                )
                or market.get("symbol")
                or ""
            ),
            "symbol": str(market.get("symbol") or "GOLD"),
            "timeframe": str(market.get("timeframe") or "1m"),
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "price": price,
            "fresh": True,
            "is_synthetic": False,
        }

    @staticmethod
    def _settle_selected_execution_mutations(adapter, cycle_id: str) -> dict[str, Any] | None:
        """Flush accepted/cancel commands without injecting a synthetic market tick."""

        if str(getattr(adapter, "name", "")) != "nautilus_paper":
            return None
        flush = getattr(adapter, "flush_commands", None)
        if not callable(flush):
            raise RuntimeError(
                "nautilus paper adapter cannot flush commands without market events"
            )
        return flush(cycle_id)

    def _cancel_pending(
        self,
        cycle_id: str,
        *,
        now: str | None,
        strategy_plan_id: str | None = None,
        adapter=None,
        reason: str = "",
    ) -> int:
        execution = adapter or build_configured_execution_engine_adapter(self.output_root, config=self.config)
        receipt = execution.cancel_orders(
            cycle_id,
            strategy_plan_id=strategy_plan_id,
            ts=_timestamp(now),
            reason=reason,
        )
        return int(receipt.get("cancelled_order_count") or len(receipt.get("cancelled_order_ids") or []))

    def _write_runtime(self, row: dict[str, Any]) -> None:
        write_json(self.root / "runtime.json", [row])

    def _activate_plan(self, plan: dict[str, Any]) -> None:
        target_id = str(plan.get("strategy_plan_id") or "")
        for active in self._all_active_plans():
            if str(active.get("strategy_plan_id") or "") == target_id:
                continue
            active["status"] = "superseded"
            self._write_plan(active)
        plan["status"] = "active"
        self._write_plan(plan)

    def _activate_staged_range_plan(
        self,
        plan: dict[str, Any],
        *,
        expected_active_plan_id: str,
    ) -> None:
        """Atomically swap two same-cycle plans after edge mutation verification."""

        cycle_id = str(plan.get("cycle_id") or "")
        target_id = str(plan.get("strategy_plan_id") or "")
        if not cycle_id or not target_id or not expected_active_plan_id:
            raise ValueError("range adjustment activation identity is incomplete")
        path = self._plans_path(cycle_id)
        rows = [dict(row) for row in load_json(path) if isinstance(row, dict)]
        target = next(
            (row for row in rows if str(row.get("strategy_plan_id") or "") == target_id),
            None,
        )
        current = next(
            (
                row
                for row in rows
                if str(row.get("strategy_plan_id") or "")
                == expected_active_plan_id
            ),
            None,
        )
        if not target or str(target.get("status") or "") != "staging":
            raise RuntimeError("range adjustment staging plan is unavailable")
        if not current or str(current.get("status") or "") != "active":
            raise RuntimeError("range adjustment source plan is no longer active")
        foreign_active = [
            row
            for row in self._all_active_plans()
            if str(row.get("strategy_plan_id") or "") != expected_active_plan_id
        ]
        if foreign_active:
            raise RuntimeError("another production plan is active")
        for row in rows:
            row_id = str(row.get("strategy_plan_id") or "")
            if row_id == expected_active_plan_id:
                row["status"] = "superseded"
            elif row_id == target_id:
                row["status"] = "active"
        write_json(path, rows)
        plan["status"] = "active"

    def _proposals_path(self, cycle_id: str) -> Path:
        return self.root / "proposals" / f"{cycle_id}.json"

    def _plans_path(self, cycle_id: str) -> Path:
        return self.root / "plans" / f"{cycle_id}.json"

    def _prepared_starts_path(self, cycle_id: str) -> Path:
        return self.root / "prepared_starts" / f"{cycle_id}.json"

    def _write_dca_risk_decision(
        self,
        cycle_id: str,
        decision: dict[str, Any],
    ) -> None:
        path = self.root / "dca_risk_decisions" / f"{cycle_id}.json"
        decision_id = str(decision.get("decision_id") or "")
        rows = [
            row
            for row in load_json(path)
            if str(row.get("decision_id") or "") != decision_id
        ]
        rows.append(decision)
        write_json(path, rows)

    def _write_prepared_start(self, record: dict[str, Any]) -> None:
        path = self._prepared_starts_path(str(record["cycle_id"]))
        prepared_start_id = str(record.get("prepared_start_id") or "")
        rows = [
            row
            for row in load_json(path)
            if isinstance(row, dict)
            and str(row.get("prepared_start_id") or "") != prepared_start_id
        ]
        rows.append(record)
        write_json(path, rows[-20:])

    def _next_plan_version(self, cycle_id: str) -> int:
        versions = [
            int(row.get("version") or 0)
            for row in load_json(self._plans_path(cycle_id))
        ]
        return max(versions, default=0) + 1

    def _write_plan(self, plan: dict[str, Any]) -> None:
        path = self._plans_path(str(plan["cycle_id"]))
        rows = [row for row in load_json(path) if str(row.get("strategy_plan_id")) != str(plan.get("strategy_plan_id"))]
        rows.append(plan)
        write_json(path, rows)

    def _all_active_plans(self) -> list[dict[str, Any]]:
        folder = self.root / "plans"
        if not folder.exists():
            return []
        return [row for path in folder.glob("*.json") for row in load_json(path) if row.get("status") == "active"]


def _required_order_ids(rows: list[dict[str, Any]], label: str) -> list[str]:
    order_ids = [str(row.get("order_id") or "").strip() for row in rows]
    if any(not value for value in order_ids):
        raise ValueError(f"{label} requires an order_id")
    if len(set(order_ids)) != len(order_ids):
        raise ValueError(f"{label} contains duplicate order_id")
    return order_ids


def _legacy_single_side_range_migration(
    plan: dict[str, Any],
) -> dict[str, Any] | None:
    """Describe the explicit next-edit migration for pre-scope one-sided plans."""

    direction = str(plan.get("direction") or "").lower()
    current_range = dict(plan.get("range") or {})
    if direction not in {"long", "short"} or str(current_range.get("scope") or ""):
        return None
    low = _positive_number(current_range.get("low"), "legacy grid low")
    high = _positive_number(current_range.get("high"), "legacy grid high")
    if high <= low:
        raise ValueError("legacy grid range must have positive low below high")
    old_count = int((plan.get("grid") or {}).get("count") or 0)
    if old_count < 2:
        raise ValueError("legacy grid count must be at least two")
    plan_market = dict((plan.get("execution_context") or {}).get("market") or {})
    split_price = _number_or(
        current_range.get("split_price"),
        _number_or(plan_market.get("price"), math.nan),
    )
    if not math.isfinite(split_price) or split_price <= 0:
        split_price = (low + high) / 2.0
    executable_low, executable_high = low, high
    if low < split_price < high:
        if direction == "long":
            executable_high = split_price
        else:
            executable_low = split_price
    return {
        "schema_version": "legacy-single-side-grid-migration-v1",
        "reason": "pre_scope_single_side_plan",
        "direction": direction,
        "legacy_grid_count": old_count,
        "executable_grid_count": math.ceil(old_count / 2),
        "split_price": split_price,
        "legacy_range": {"low": low, "high": high},
        "executable_range": {
            "low": executable_low,
            "high": executable_high,
        },
        "applies_on_final_confirmation_only": True,
    }


def _range_preview_specification(
    source: dict[str, Any],
    *,
    canonical_metrics: Any = None,
    post_flatten_candidate_only: bool = False,
) -> dict[str, Any]:
    current_range = dict(source.get("range") or {})
    grid = dict(source.get("grid") or {})
    risk = (
        dict(source.get("risk") or {})
        if isinstance(source.get("risk"), dict)
        else dict(source.get("risk_budget") or {})
    )
    metrics = (
        dict(canonical_metrics)
        if isinstance(canonical_metrics, dict)
        else {}
    )
    low = _positive_number(current_range.get("low"), "grid low")
    high = _positive_number(current_range.get("high"), "grid high")
    count = int(grid.get("count") or 0)
    notional = _positive_number(grid.get("notional_per_grid"), "notional_per_grid")
    orders = (
        list(source.get("orders") or [])
        if isinstance(source.get("orders"), list)
        else list(grid.get("orders") or [])
    )
    notional_by_side = {"buy": 0.0, "sell": 0.0}
    for row in orders:
        if not isinstance(row, dict):
            continue
        side = str(row.get("side") or "").lower()
        if side not in notional_by_side:
            continue
        order_notional = _number_or(row.get("notional"), 0.0)
        if order_notional <= 0:
            order_notional = _number_or(row.get("price"), 0.0) * _number_or(
                row.get("quantity"),
                0.0,
            )
        notional_by_side[side] += max(0.0, order_notional)
    if post_flatten_candidate_only:
        candidate_notional_by_side = dict(
            metrics.get("candidate_notional_by_side") or {}
        )
        candidate_loss_by_side = dict(metrics.get("candidate_loss_by_side") or {})
        candidate_max_notional = max(
            (_number_or(value, 0.0) for value in candidate_notional_by_side.values()),
            default=0.0,
        )
        candidate_max_loss = max(
            (_number_or(value, 0.0) for value in candidate_loss_by_side.values()),
            default=0.0,
        )
        equity = _number_or(metrics.get("equity"), 0.0)
        requested_leverage = _number_or(grid.get("leverage"), 0.0)
        estimated_margin = (
            candidate_max_notional / requested_leverage
            if requested_leverage > 0
            else None
        )
        actual_leverage = (
            candidate_max_notional / equity if equity > 0 else None
        )
        max_loss = candidate_max_loss
    else:
        estimated_margin = metrics.get(
            "projected_margin",
            risk.get("estimated_margin"),
        )
        actual_leverage = metrics.get(
            "projected_actual_leverage",
            risk.get("actual_leverage"),
        )
        max_loss = metrics.get(
            "projected_max_loss",
            risk.get("max_loss"),
        )
    return {
        "range_low": low,
        "range_high": high,
        "range_width": high - low,
        "mode": str(grid.get("mode") or "arithmetic"),
        "grid_count": count,
        "spacing": grid.get("spacing"),
        "spacing_ratio": grid.get("spacing_ratio"),
        "notional_per_grid": notional,
        "notional_mode": grid.get("notional_mode"),
        "total_grid_notional": round(count * notional, 2),
        "order_count": len(orders),
        "total_order_notional": round(sum(notional_by_side.values()), 2),
        "max_side_order_notional": round(max(notional_by_side.values()), 2),
        "estimated_margin": estimated_margin,
        "actual_leverage": actual_leverage,
        "max_loss": max_loss,
        "min_net_profit_per_grid_usd": metrics.get(
            "minimum_planned_net_profit_per_grid_usd",
            grid.get("min_net_profit_per_grid_usd"),
        ),
        "target_net_profit_per_grid_usd": grid.get(
            "target_net_profit_per_grid_usd"
        ),
        "canonical_max_side_notional": (
            candidate_max_notional
            if post_flatten_candidate_only
            else metrics.get("projected_max_side_notional")
        ),
        "canonical_equity": metrics.get("equity"),
        "leverage": grid.get("leverage"),
    }


def _manual_range_acknowledgement_contract(
    *,
    preview_id: str,
    old: dict[str, Any],
    new: dict[str, Any],
    blocker_codes: set[str],
    local_profit_target_not_met: bool,
    limits: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build explicit operator acknowledgements from server-calculated facts."""

    rows: list[dict[str, Any]] = [
        {
            "code": "specification_change",
            "severity": "warning",
            "title": "我确认网格规格变化",
            "summary": (
                f"Range 宽度 {old.get('range_width'):.2f} → "
                f"{new.get('range_width'):.2f} USD；单格间距/比例与计划净利会随之变化。"
            ),
            "facts": {
                "old_range_width": old.get("range_width"),
                "new_range_width": new.get("range_width"),
                "old_spacing": old.get("spacing"),
                "new_spacing": new.get("spacing"),
                "old_spacing_ratio": old.get("spacing_ratio"),
                "new_spacing_ratio": new.get("spacing_ratio"),
                "old_profit_per_grid": old.get("min_net_profit_per_grid_usd"),
                "new_profit_per_grid": new.get("min_net_profit_per_grid_usd"),
            },
        },
        {
            "code": "maximum_loss_scenario",
            "severity": "critical",
            "title": "我确认预计最大损失",
            "summary": (
                f"预计最大损失 {old.get('max_loss')} → {new.get('max_loss')} USD。"
                "新值按旧持仓先平仓、同侧新网格订单全部成交并触及计划 SL 计算；"
                "计划 SL 位于 Range 边界外一格/一档，不包含极端滑点和资金费。"
            ),
            "facts": {
                "old_max_loss": old.get("max_loss"),
                "new_max_loss": new.get("max_loss"),
                "assumption": (
                    "old_positions_flatten_then_all_candidate_same_side_entries_fill_"
                    "then_planned_stop_beyond_range"
                ),
                "excluded": ["extreme_slippage", "funding"],
            },
        },
    ]
    if "candidate_grid_count_out_of_bounds" in blocker_codes:
        rows.append(
            {
                "code": "grid_count_outside_preferred_band",
                "severity": "warning",
                "title": "我确认网格数量超出建议区间",
                "summary": (
                    f"网格数量 {old.get('grid_count')} → {new.get('grid_count')} 格；"
                    f"自动建议区间为 {limits.get('min_grid_count')}–"
                    f"{limits.get('max_grid_count')} 格。"
                ),
                "facts": {
                    "old_grid_count": old.get("grid_count"),
                    "new_grid_count": new.get("grid_count"),
                    "minimum": limits.get("min_grid_count"),
                    "maximum": limits.get("max_grid_count"),
                },
            }
        )
    if (
        "grid_profit_target_not_met" in blocker_codes
        or local_profit_target_not_met
    ):
        rows.append(
            {
                "code": "profit_target_shortfall",
                "severity": "critical",
                "title": "我确认每格计划净利低于自动目标",
                "summary": (
                    f"每格计划净利 {old.get('min_net_profit_per_grid_usd')} → "
                    f"{new.get('min_net_profit_per_grid_usd')} USD；自动目标为至少 "
                    f"{limits.get('min_net_profit_per_grid_usd')} USD。"
                ),
                "facts": {
                    "old_profit_per_grid": old.get("min_net_profit_per_grid_usd"),
                    "new_profit_per_grid": new.get("min_net_profit_per_grid_usd"),
                    "target": limits.get("min_net_profit_per_grid_usd"),
                },
            }
        )
    leverage_limit = _number_or(limits.get("max_leverage"), 0.0)
    margin_budget = _number_or(limits.get("margin_budget"), 0.0)
    new_actual_leverage = _number_or(new.get("actual_leverage"), 0.0)
    new_estimated_margin = _number_or(new.get("estimated_margin"), 0.0)
    if (
        (leverage_limit > 0 and new_actual_leverage > leverage_limit + 1e-8)
        or (margin_budget > 0 and new_estimated_margin > margin_budget + 1e-8)
    ):
        rows.append(
            {
                "code": "leverage_and_margin_risk",
                "severity": "critical",
                "title": "我确认杠杆与保证金风险",
                "summary": (
                    f"实际杠杆 {old.get('actual_leverage')}x → {new.get('actual_leverage')}x"
                    f"（自动上限 {limits.get('max_leverage')}x）；预计保证金 "
                    f"{old.get('estimated_margin')} → {new.get('estimated_margin')} USD。"
                ),
                "facts": {
                    "old_actual_leverage": old.get("actual_leverage"),
                    "new_actual_leverage": new.get("actual_leverage"),
                    "leverage_limit": limits.get("max_leverage"),
                    "old_estimated_margin": old.get("estimated_margin"),
                    "new_estimated_margin": new.get("estimated_margin"),
                    "margin_budget": limits.get("margin_budget"),
                },
            }
        )
    if "market_price_outside_range" in blocker_codes:
        rows.append(
            {
                "code": "market_outside_range",
                "severity": "critical",
                "title": "我确认当前价位于新 Range 外",
                "summary": "新网格启动时当前价不在区间内，可能立即形成单边暴露或长期没有预期成交。",
                "facts": {"preview_id": preview_id},
            }
        )
    return rows


def _manual_range_acknowledgement_facts_digest(
    *,
    old: dict[str, Any],
    new: dict[str, Any],
    limits: dict[str, Any],
    acknowledgements: list[dict[str, Any]],
) -> str:
    """Bind the operator click to every numeric fact rendered in the card."""

    payload = {
        "schema_version": MANUAL_RANGE_RISK_ACK_SCHEMA,
        "old": old,
        "new": new,
        "limits": limits,
        "acknowledgements": acknowledgements,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"grid-range-facts-{hashlib.sha256(raw.encode()).hexdigest()}"


def _manual_range_risk_snapshot_digest(decision: dict[str, Any]) -> str:
    """Hash economic risk facts without invalidating consent on harmless ticks."""

    request = (
        dict(decision.get("request") or {})
        if isinstance(decision.get("request"), dict)
        else {}
    )
    candidate = (
        dict(request.get("candidate") or {})
        if isinstance(request.get("candidate"), dict)
        else {}
    )
    market = (
        dict(request.get("market") or {})
        if isinstance(request.get("market"), dict)
        else {}
    )
    metrics = (
        dict(decision.get("metrics") or {})
        if isinstance(decision.get("metrics"), dict)
        else {}
    )
    limits = (
        dict(decision.get("limits") or {})
        if isinstance(decision.get("limits"), dict)
        else {}
    )
    economic_commands = []
    for row in candidate.get("commands") or []:
        if not isinstance(row, dict):
            continue
        economic_commands.append(
            {
                key: row.get(key)
                for key in (
                    "side",
                    "event",
                    "order_type",
                    "price",
                    "quantity",
                    "notional",
                    "sl",
                    "tp",
                    "symbol",
                )
            }
        )
    economic_commands.sort(
        key=lambda row: (
            str(row.get("side") or ""),
            _number_or(row.get("price"), 0.0),
            _number_or(row.get("quantity"), 0.0),
        )
    )
    payload = {
        "schema_version": MANUAL_RANGE_RISK_ACK_SCHEMA,
        "candidate": {
            key: candidate.get(key)
            for key in (
                "preview_id",
                "direction",
                "range_low",
                "range_high",
                "grid_mode",
                "grid_count",
                "notional_per_grid",
                "leverage",
            )
        },
        "economic_commands": economic_commands,
        "market": {
            key: market.get(key)
            for key in (
                "provider",
                "source_mode",
                "fresh",
                "is_synthetic",
            )
        },
        "equity": metrics.get("equity"),
        "candidate_notional_by_side": metrics.get("candidate_notional_by_side"),
        "candidate_loss_by_side": metrics.get("candidate_loss_by_side"),
        "minimum_planned_net_profit_per_grid_usd": metrics.get(
            "minimum_planned_net_profit_per_grid_usd"
        ),
        "limits": limits,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"grid-range-risk-{hashlib.sha256(raw.encode()).hexdigest()}"


def _manual_range_effective_blocker_codes(decision: dict[str, Any]) -> set[str]:
    """Remove exposure that the replacement transaction flattens before launch."""

    blocker_codes = {
        str(row.get("code") or "risk_blocked")
        for row in decision.get("blockers") or []
        if isinstance(row, dict)
    }
    request = (
        dict(decision.get("request") or {})
        if isinstance(decision.get("request"), dict)
        else {}
    )
    candidate = (
        dict(request.get("candidate") or {})
        if isinstance(request.get("candidate"), dict)
        else {}
    )
    metrics = (
        dict(decision.get("metrics") or {})
        if isinstance(decision.get("metrics"), dict)
        else {}
    )
    limits = (
        dict(decision.get("limits") or {})
        if isinstance(decision.get("limits"), dict)
        else {}
    )
    candidate_notional = max(
        (
            _number_or(value, 0.0)
            for value in dict(metrics.get("candidate_notional_by_side") or {}).values()
        ),
        default=0.0,
    )
    equity = _number_or(metrics.get("equity"), 0.0)
    leverage = _number_or(candidate.get("leverage"), 0.0)
    max_leverage = _number_or(limits.get("max_leverage"), 0.0)
    margin_budget = _number_or(limits.get("margin_budget"), 0.0)
    candidate_actual_leverage = candidate_notional / equity if equity > 0 else math.inf
    candidate_margin = candidate_notional / leverage if leverage > 0 else math.inf
    if max_leverage > 0 and candidate_actual_leverage <= max_leverage + 1e-8:
        blocker_codes.discard("projected_leverage_exceeded")
    if margin_budget > 0 and candidate_margin <= margin_budget + 1e-8:
        blocker_codes.discard("projected_margin_exceeded")
    return blocker_codes


def _validated_manual_range_acknowledgement(
    preview: dict[str, Any],
    payload: dict[str, Any],
    *,
    now: str | None,
) -> dict[str, Any]:
    contract = (
        dict(preview.get("manual_confirmation") or {})
        if isinstance(preview.get("manual_confirmation"), dict)
        else {}
    )
    if contract.get("available") is not True:
        reasons = ",".join(contract.get("non_overridable_blocker_codes") or [])
        raise ValueError(f"range_replacement_non_overridable:{reasons or 'risk_blocked'}")
    acknowledgement = (
        dict(payload.get("risk_acknowledgements") or {})
        if isinstance(payload.get("risk_acknowledgements"), dict)
        else {}
    )
    required_codes = {
        str(row.get("code") or "")
        for row in contract.get("required_acknowledgements") or []
        if isinstance(row, dict) and str(row.get("code") or "")
    }
    provided_codes = {
        str(code)
        for code in acknowledgement.get("codes") or []
        if str(code)
    }
    if (
        acknowledgement.get("schema_version") != MANUAL_RANGE_RISK_ACK_SCHEMA
        or str(acknowledgement.get("preview_id") or "")
        != str(preview.get("preview_id") or "")
        or str(acknowledgement.get("facts_digest") or "")
        != str(contract.get("facts_digest") or "")
        or str(acknowledgement.get("risk_snapshot_digest") or "")
        != str(contract.get("risk_snapshot_digest") or "")
        or provided_codes != required_codes
    ):
        raise ValueError("range_risk_acknowledgements_incomplete")
    overridden = sorted(contract.get("overridable_blocker_codes") or [])
    return {
        "schema_version": MANUAL_RANGE_RISK_ACK_SCHEMA,
        "scope": "paper_only",
        "preview_id": preview.get("preview_id"),
        "facts_digest": contract.get("facts_digest"),
        "risk_snapshot_digest": contract.get("risk_snapshot_digest"),
        "acknowledgement_codes": sorted(provided_codes),
        "overridden_blocker_codes": overridden,
        "confirmed_at": _timestamp(now),
    }


def _staged_manual_range_override(plan: dict[str, Any]) -> dict[str, Any] | None:
    request = (
        dict(plan.get("replacement_request") or {})
        if isinstance(plan.get("replacement_request"), dict)
        else {}
    )
    acknowledgement = request.get("risk_acknowledgement")
    if not isinstance(acknowledgement, dict):
        return None
    return dict(acknowledgement)


def _manual_override_allows(
    acknowledgement: dict[str, Any] | None,
    blocker_code: str,
) -> bool:
    return bool(
        acknowledgement
        and acknowledgement.get("schema_version") == MANUAL_RANGE_RISK_ACK_SCHEMA
        and acknowledgement.get("scope") == "paper_only"
        and blocker_code
        in set(acknowledgement.get("overridden_blocker_codes") or [])
    )


def _require_acknowledged_paper_grid_risk(
    decision: dict[str, Any],
    acknowledgement: dict[str, Any],
    *,
    adapter_name: str,
) -> None:
    if (
        acknowledgement.get("overridden_blocker_codes")
        and adapter_name != "nautilus_paper"
    ):
        raise ValueError("manual range risk override is paper-only")
    if (
        acknowledgement.get("schema_version") != MANUAL_RANGE_RISK_ACK_SCHEMA
        or acknowledgement.get("scope") != "paper_only"
    ):
        raise ValueError("manual range risk override acknowledgement is invalid")
    if str(acknowledgement.get("risk_snapshot_digest") or "") != (
        _manual_range_risk_snapshot_digest(decision)
    ):
        raise ValueError("manual range risk facts changed")
    request = (
        dict(decision.get("request") or {})
        if isinstance(decision.get("request"), dict)
        else {}
    )
    candidate = (
        dict(request.get("candidate") or {})
        if isinstance(request.get("candidate"), dict)
        else {}
    )
    if str(candidate.get("preview_id") or "") != str(
        acknowledgement.get("preview_id") or ""
    ):
        raise ValueError("manual range risk override preview changed")
    if str(candidate.get("intent") or "") == "start_grid":
        blocker_codes = {
            str(row.get("code") or "risk_blocked")
            for row in decision.get("blockers") or []
            if isinstance(row, dict)
        }
    else:
        blocker_codes = _manual_range_effective_blocker_codes(decision)
    non_overridable = blocker_codes - MANUAL_RANGE_RISK_OVERRIDABLE_BLOCKERS
    if non_overridable:
        raise ValueError(
            "range replacement has non-overridable blockers:"
            + ",".join(sorted(non_overridable))
        )
    acknowledged = set(acknowledgement.get("overridden_blocker_codes") or [])
    missing = blocker_codes - acknowledged
    if missing:
        raise ValueError(
            "range replacement risk acknowledgement missing:"
            + ",".join(sorted(missing))
        )


def _retained_entry_risk_commands(
    accepted_entries: list[dict[str, Any]],
    *,
    plan_history: list[dict[str, Any]],
    candidate_plan: dict[str, Any],
    timestamp: str,
) -> list[dict[str, Any]]:
    """Bind each retained engine order to its exact planned SL/TP economics."""

    references = [
        {
            **dict(order),
            "_strategy_plan_id": str(plan.get("strategy_plan_id") or ""),
            "_strategy_plan_version": plan.get("version"),
        }
        for plan in plan_history
        for order in (plan.get("grid") or {}).get("orders") or []
        if isinstance(order, dict)
    ]
    context = (
        dict(candidate_plan.get("execution_context") or {})
        if isinstance(candidate_plan.get("execution_context"), dict)
        else {}
    )
    market = (
        dict(context.get("market") or {})
        if isinstance(context.get("market"), dict)
        else {}
    )
    symbol = str(market.get("symbol") or "").strip()
    market_price = _positive_number(market.get("price"), "execution market context price")
    if not symbol:
        raise ValueError("execution market context symbol is required")

    commands: list[dict[str, Any]] = []
    for row in accepted_entries:
        order_id = str(row.get("order_id") or "").strip()
        if not order_id:
            raise ValueError("retained entry requires an order_id")
        side = str(row.get("side") or "").lower()
        if side not in {"buy", "sell"}:
            raise ValueError("retained entry side is invalid")
        price = _positive_number(row.get("price"), "retained entry price")
        source_plan_id = str(row.get("strategy_plan_id") or "")
        candidates = [
            planned
            for planned in references
            if str(planned.get("side") or "").lower() == side
            and _same_core_number(planned.get("price"), price)
        ]
        if source_plan_id:
            candidates = [
                planned
                for planned in candidates
                if str(planned.get("_strategy_plan_id") or "") == source_plan_id
            ]
        if len(candidates) > 1:
            raise ValueError(f"retained entry risk reference is ambiguous:{order_id}")
        planned = candidates[0] if candidates else {}
        quantity = _positive_number(
            row.get("quantity", planned.get("quantity")),
            "retained entry quantity",
        )
        sl = _positive_number(row.get("sl", planned.get("sl")), "retained entry sl")
        tp = _positive_number(row.get("tp", planned.get("tp")), "retained entry tp")
        commands.append(
            {
                "cycle_id": str(candidate_plan.get("cycle_id") or ""),
                "ts": timestamp,
                "symbol": symbol,
                "side": side,
                "event": "entry",
                "order_type": str(row.get("order_type") or "limit").lower(),
                "price": price,
                "market_price": market_price,
                "quantity": quantity,
                "notional": price * quantity,
                "sl": sl,
                "tp": tp,
                "source": "existing_execution_order",
                "source_fill_id": f"risk-retained:{order_id}",
                "existing_order_id": order_id,
                "strategy_plan_id": source_plan_id
                or str(planned.get("_strategy_plan_id") or ""),
                "strategy_plan_version": row.get(
                    "strategy_plan_version",
                    planned.get("_strategy_plan_version"),
                ),
            }
        )
    return commands


def _entry_core(rows: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    return sorted(
        (
            str(row.get("order_id") or ""),
            str(row.get("side") or "").lower(),
            str(row.get("event") or "entry").lower(),
            str(row.get("order_type") or "").lower(),
            _core_number(row.get("price")),
            _core_number(row.get("quantity")),
            _core_number(row.get("sl")),
            _core_number(row.get("tp")),
            str(row.get("strategy_plan_id") or ""),
            str(row.get("strategy_plan_version") or ""),
        )
        for row in rows
    )


def _entry_geometry_price(row: dict[str, Any]) -> float:
    requested = row.get("requested_price")
    return _positive_number(
        requested if requested not in (None, "") else row.get("price"),
        "accepted entry geometry price",
    )


def _protection_core(rows: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    return sorted(
        (
            str(row.get("order_id") or ""),
            str(row.get("state") or "").lower(),
            str(row.get("side") or "").lower(),
            str(row.get("event") or "").lower(),
            str(row.get("order_type") or "").lower(),
            _core_number(row.get("price")),
            _core_number(row.get("quantity")),
            str(row.get("trade_id") or ""),
            str(row.get("position_id") or ""),
            str(row.get("strategy_plan_id") or ""),
        )
        for row in rows
    )


def _position_core(rows: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    return sorted(
        (
            str(row.get("position_id") or ""),
            str(row.get("trade_id") or ""),
            str(row.get("status") or "").lower(),
            str(row.get("side") or "").lower(),
            _core_number(
                row.get("remaining_units", row.get("remaining_quantity", row.get("quantity")))
            ),
            _core_number(row.get("entry_price")),
            _core_number(row.get("sl")),
            _core_number(row.get("tp")),
            str(row.get("strategy_plan_id") or ""),
        )
        for row in rows
        if str(row.get("status") or "").lower() == "open"
    )


def _core_number(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        return f"{float(value):.8f}"
    except (TypeError, ValueError):
        return ""


def _same_core_number(left: Any, right: Any) -> bool:
    try:
        left_value = float(left)
        right_value = float(right)
    except (TypeError, ValueError):
        return False
    return abs(left_value - right_value) <= max(1e-8, abs(right_value) * 1e-8)


def _extend_range_request_fingerprint(
    from_plan_id: str,
    requested_range: dict[str, Any],
) -> str:
    raw = json.dumps(
        {
            "from_plan_id": str(from_plan_id),
            "requested_range": {
                "low": float(requested_range["low"]),
                "high": float(requested_range["high"]),
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _range_replacement_request_fingerprint(
    from_plan_id: str,
    from_plan_version: int,
    preview_id: str,
    *,
    handle: str,
    requested_range: dict[str, Any],
    recalculate_notional: bool,
    expected_execution: tuple[set[str], set[str]],
    acknowledgement_codes: list[str],
    acknowledgement_facts_digest: str,
    risk_snapshot_digest: str,
) -> str:
    low = _positive_number(requested_range.get("low"), "replacement range low")
    high = _positive_number(requested_range.get("high"), "replacement range high")
    if high <= low:
        raise ValueError("replacement range high must be above low")
    raw = json.dumps(
        {
            "from_plan_id": str(from_plan_id),
            "from_plan_version": int(from_plan_version),
            "preview_id": str(preview_id),
            "handle": str(handle),
            "requested_range": {
                "low": low,
                "high": high,
            },
            "recalculate_notional": bool(recalculate_notional),
            "expected_execution": {
                "accepted_order_ids": sorted(expected_execution[0]),
                "open_position_ids": sorted(expected_execution[1]),
            },
            "acknowledgement_codes": sorted(set(acknowledgement_codes)),
            "acknowledgement_facts_digest": str(acknowledgement_facts_digest),
            "risk_snapshot_digest": str(risk_snapshot_digest),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _grid_preview_payload_from_plan(plan: dict[str, Any]) -> dict[str, Any]:
    grid = dict(plan.get("grid") or {})
    return {
        "direction": plan.get("direction"),
        "style": plan.get("style"),
        "out_of_range": grid.get("out_of_range"),
        "range": dict(plan.get("range") or {}),
        "grid": {
            "count": grid.get("count"),
            "mode": grid.get("mode"),
            "notional_per_grid": grid.get("notional_per_grid"),
            "notional_mode": "manual",
            "out_of_range": grid.get("out_of_range"),
        },
        "risk_budget": {"leverage": grid.get("leverage")},
    }


def _rearm_completed_edge_commands(
    commands: list[dict[str, Any]],
    execution_orders: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Give a completed edge lifecycle a new deterministic command identity."""

    rearmed: list[dict[str, Any]] = []
    for command in commands:
        plan_id = str(command.get("strategy_plan_id") or "")
        side = str(command.get("side") or "").lower()
        price = command.get("price")
        completed_count = sum(
            1
            for row in execution_orders
            if str(row.get("event") or "entry").lower() == "entry"
            and str(row.get("state") or "").lower() == "filled"
            and str(row.get("strategy_plan_id") or "") == plan_id
            and str(row.get("side") or "").lower() == side
            and _same_core_number(row.get("price"), price)
        )
        if not completed_count:
            rearmed.append(dict(command))
            continue
        source_id = str(command.get("source_fill_id") or "")
        if not source_id:
            raise ValueError("edge rearm requires source_fill_id")
        rearmed.append(
            {
                **command,
                "source_fill_id": f"{source_id}:rearm:{completed_count}",
            }
        )
    return rearmed


def _matching_staged_range_adjustment(
    plans: list[Any],
    *,
    from_plan_id: str,
    request_fingerprint: str,
) -> dict[str, Any]:
    candidates = []
    matches = []
    for row in plans:
        if not isinstance(row, dict) or str(row.get("status") or "") != "staging":
            continue
        adjustment = (
            dict(row.get("range_adjustment") or {})
            if isinstance(row.get("range_adjustment"), dict)
            else {}
        )
        if str(adjustment.get("from_plan_id") or "") != from_plan_id:
            continue
        candidates.append(dict(row))
        if str(adjustment.get("request_fingerprint") or "") == request_fingerprint:
            matches.append(dict(row))
    if candidates and not matches:
        raise ValueError("range_adjustment_in_progress")
    if len(matches) > 1:
        raise RuntimeError("multiple staged range adjustments match one request")
    return matches[0] if matches else {}


def _content_id(prefix: str, payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"{prefix}-{hashlib.sha256(raw.encode()).hexdigest()[:16]}"


def _dca_confirmation_contract(preview: dict[str, Any]) -> dict[str, Any]:
    dca = dict(preview.get("dca") or {})
    risk = dict(preview.get("risk") or {})
    rows = [
        {
            "code": "dca_accumulation_specification",
            "severity": "warning",
            "title": "我确认 DCA 补仓与整轮止盈规格",
            "summary": (
                f"最多 {dca.get('max_additions')} 次、每次名义 "
                f"{dca.get('notional_per_addition')} USD；全部筹码在 "
                f"{dca.get('target_price')} 统一退出。"
            ),
            "facts": {
                "direction": preview.get("direction"),
                "entry_levels": list(dca.get("entry_levels") or []),
                "max_additions": dca.get("max_additions"),
                "notional_per_addition": dca.get("notional_per_addition"),
                "target_price": dca.get("target_price"),
                "loop_enabled": dca.get("loop_enabled"),
            },
        },
        {
            "code": "dca_maximum_loss",
            "severity": "critical",
            "title": "我确认满仓与最大损失情景",
            "summary": (
                f"全部补仓成交时总名义 {dca.get('total_possible_notional')} USD；"
                f"触及 {dca.get('stop_price')} 的预计最大损失为 "
                f"{risk.get('maximum_loss_at_full_depth')} USD。"
            ),
            "facts": {
                "total_possible_notional": dca.get("total_possible_notional"),
                "stop_price": dca.get("stop_price"),
                "maximum_loss_at_full_depth": risk.get("maximum_loss_at_full_depth"),
                "estimated_margin_at_full_depth": risk.get("estimated_margin_at_full_depth"),
                "actual_leverage_at_full_depth": risk.get("actual_leverage_at_full_depth"),
            },
        },
    ]
    if risk.get("capacity_exceeded") is True:
        rows.append({
            "code": "dca_capacity_exceeded",
            "severity": "critical",
            "title": "我确认 DCA 满仓容量超过建议值",
            "summary": (
                f"满仓实际杠杆 {risk.get('actual_leverage_at_full_depth')}x，"
                f"高于建议上限 {risk.get('leverage_limit')}x；仅 Paper 可确认继续。"
            ),
            "facts": {
                "actual_leverage_at_full_depth": risk.get("actual_leverage_at_full_depth"),
                "leverage_limit": risk.get("leverage_limit"),
            },
        })
    digest_payload = {
        "schema_version": DCA_RISK_ACK_SCHEMA,
        "preview_id": preview.get("preview_id"),
        "dca": dca,
        "risk": risk,
        "required_acknowledgements": rows,
    }
    return {
        "schema_version": DCA_RISK_ACK_SCHEMA,
        "scope": "paper_only",
        "required": True,
        "available": True,
        "preview_id": preview.get("preview_id"),
        "facts_digest": _content_id("dca-risk-facts", digest_payload),
        "required_acknowledgements": rows,
    }


def _validated_dca_acknowledgement(
    preview: dict[str, Any],
    payload: dict[str, Any],
    *,
    now: str | None,
) -> dict[str, Any]:
    contract = dict(preview.get("manual_confirmation") or {})
    supplied = (
        dict(payload.get("risk_acknowledgements") or {})
        if isinstance(payload.get("risk_acknowledgements"), dict)
        else {}
    )
    required = {
        str(row.get("code") or "")
        for row in contract.get("required_acknowledgements") or []
        if isinstance(row, dict) and row.get("code")
    }
    provided = {str(code) for code in supplied.get("codes") or [] if str(code)}
    if (
        contract.get("schema_version") != DCA_RISK_ACK_SCHEMA
        or contract.get("scope") != "paper_only"
        or supplied.get("schema_version") != DCA_RISK_ACK_SCHEMA
        or str(supplied.get("preview_id") or "")
        != str(preview.get("preview_id") or "")
        or str(supplied.get("facts_digest") or "")
        != str(contract.get("facts_digest") or "")
        or provided != required
    ):
        raise ValueError("dca_risk_acknowledgements_incomplete")
    return {
        "schema_version": DCA_RISK_ACK_SCHEMA,
        "scope": "paper_only",
        "preview_id": preview.get("preview_id"),
        "facts_digest": contract.get("facts_digest"),
        "acknowledgement_codes": sorted(provided),
        "confirmed_at": _timestamp(now),
    }


def normalize_proposal(payload: dict[str, Any], *, now: str | None = None, legacy: bool = False) -> dict[str, Any]:
    source = str(payload.get("source") or payload.get("author") or "").lower()
    if source not in {"human", "ai"}:
        raise ValueError("proposal source must be human or ai")
    cycle_id = str(payload.get("cycle_id") or "")
    if not cycle_id:
        raise ValueError("proposal cycle_id is required")
    proposal = {
        "schema_version": PROPOSAL_SCHEMA,
        "cycle_id": cycle_id,
        "source": source,
        "created_at": _timestamp(now or payload.get("locked_at")),
        "direction": str(payload.get("direction") or "neutral"),
        "style": str(payload.get("style") or "steady"),
        "strategy_type": str(payload.get("strategy_type") or "grid").lower(),
        "range": dict(payload.get("range") or {}),
        "key_levels": list(payload.get("key_levels") or []),
        "grid": dict(payload.get("grid") or {"orders": list(payload.get("grid_orders") or [])}),
        "signal": dict(payload.get("signal") or {"confidence": payload.get("confidence")}),
        "tp_sl": dict(payload.get("tp_sl") or payload.get("bracket") or {}),
        "risk_budget": dict(payload.get("risk_budget") or {}),
        "intraday_rules": list(payload.get("intraday_rules") or payload.get("invalidation") or []),
        "legacy_status": str(payload.get("status") or ("locked" if payload.get("locked_at") else "")),
        "legacy": bool(legacy),
        "rationale": str(payload.get("rationale") or ""),
        "evidence_used": list(payload.get("evidence_used") or []),
        "analysis": dict(payload.get("analysis") or {}),
        "prompt_contract": dict(payload.get("prompt_contract") or {}),
        "evaluation_receipt": dict(payload.get("evaluation_receipt") or {}),
        "preview_id": payload.get("preview_id"),
    }
    proposal["proposal_id"] = str(payload.get("proposal_id") or _proposal_id(proposal))
    return proposal


def _field_sources(value: dict[str, str] | None, *, selected_source: str) -> dict[str, str]:
    sources = {key: selected_source for key in PLAN_FIELDS}
    for key, source in (value or {}).items():
        if key not in PLAN_FIELDS or source not in FIELD_SOURCES:
            raise ValueError("invalid field source")
        sources[key] = source
    return sources


def _proposal_id(proposal: dict[str, Any]) -> str:
    raw = json.dumps({key: proposal.get(key) for key in ("cycle_id", "source", *PLAN_FIELDS)}, sort_keys=True, separators=(",", ":"))
    return f"proposal-{proposal['source']}-{hashlib.sha256(raw.encode()).hexdigest()[:12]}"


def _plan_id(cycle_id: str, version: int, proposal_id: str) -> str:
    return f"strategy-plan-{cycle_id}-{version}-{hashlib.sha256(proposal_id.encode()).hexdigest()[:8]}"


def _timestamp(value: str | None) -> str:
    if value:
        return str(value)
    return datetime.now(timezone.utc).isoformat()
