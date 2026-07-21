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
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.execution_plugin_composition import build_configured_execution_engine_adapter
from services.dualtrack_clock import parse_utc
from services.dualtrack_config import dualtrack_config
from services.dualtrack_store import DualTrackPlanStore
from services.control_audit import append_control_event, build_control_event, read_last_control_event
from services.grid_sizing import (
    GRID_STYLES,
    build_grid_preview,
    number_or as _number_or,
    validate_market as _validate_market,
    positive_number as _positive_number,
)
from services.grid_range_adjustment import (
    build_dragged_range,
    build_range_extension,
    range_adjustment_steps,
)
from services.journal_store import load_json, write_json
from services.risk_policy_composition import (
    build_risk_decision_store,
    compose_grid_risk_policy,
)
from services.risk_port import (
    RiskDecisionPort,
    RiskDecisionStorePort,
    action_class_for_command,
    assert_matching_risk_decision,
    build_paper_safe_action_market_gate,
    build_grid_risk_request,
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
        del as_of
        proposals = self.proposals(cycle_id)
        plan = self.active_plan(cycle_id)
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
            "runtime": self.runtime_state(cycle_id),
        }

    def runtime_state(self, cycle_id: str) -> dict[str, Any]:
        rows = load_json(self.root / "runtime.json")
        row = rows[-1] if rows else {}
        stored_cycle_id = str(row.get("cycle_id") or "")
        if stored_cycle_id and stored_cycle_id != cycle_id:
            return {
                "desired_state": "stopped",
                "actual_state": "stopped",
                "cycle_id": cycle_id,
                "updated_at": row.get("updated_at"),
                "statistics_baseline_at": None,
                "strategy_plan_id": None,
                "strategy_plan_version": None,
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
                "previous_actual_state": str(row.get("actual_state") or row.get("desired_state") or "stopped"),
                "last_control_event": self._last_control_event(),
            }
        return {
            "desired_state": str(row.get("desired_state") or "stopped"),
            "actual_state": str(row.get("actual_state") or row.get("desired_state") or "stopped"),
            "cycle_id": str(row.get("cycle_id") or cycle_id),
            "updated_at": row.get("updated_at"),
            "statistics_baseline_at": row.get("statistics_baseline_at"),
            "strategy_plan_id": row.get("strategy_plan_id"),
            "strategy_plan_version": row.get("strategy_plan_version"),
            "preview_id": row.get("preview_id"),
            "risk_decision_id": row.get("risk_decision_id"),
            "risk_policy_id": row.get("risk_policy_id"),
            "accepted_order_count": int(row.get("accepted_order_count") or 0),
            "accepted_order_count_known": bool(row.get("accepted_order_count_known", True)),
            "transition_owner": row.get("transition_owner"),
            "last_action": row.get("last_action"),
            "last_error": row.get("last_error"),
            "stale_cycle": False,
            "previous_cycle_id": None,
            "previous_actual_state": None,
            "last_control_event": self._last_control_event(),
        }

    def _last_control_event(self) -> dict[str, Any] | None:
        try:
            return read_last_control_event(self.output_root)
        except OSError:
            return None

    def runtime_configured(self) -> bool:
        return (self.root / "runtime.json").exists()

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

    def preview(
        self,
        cycle_id: str,
        payload: dict[str, Any] | None = None,
        *,
        market: dict[str, Any],
        account: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Geometry and capital sizing stay pure and shared in grid_sizing;
        # the control plane owns only locking, persistence and runtime state.
        return build_grid_preview(cycle_id, payload, market=market, account=account, config=self.config)

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
        geometry = build_dragged_range(
            current,
            requested,
            handle=str(payload.get("handle") or ""),
        )
        grid = dict(current.get("grid") or {})
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
                "range": geometry["new_range"],
                "grid": {
                    "count": grid.get("count"),
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
        local_budget_blocked = bool(
            local_risk.get("risk_budget_exceeded")
            or local_risk.get("capital_budget_exceeded")
        )
        blockers = list(risk_decision.get("blockers") or [])
        budget_blocker_codes = {
            "plan_loss_budget_exceeded",
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
                trial_local_risk.get("risk_budget_exceeded")
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
            local_budget_blocked = bool(
                local_risk.get("risk_budget_exceeded")
                or local_risk.get("capital_budget_exceeded")
            )
            blockers = list(risk_decision.get("blockers") or [])
        can_apply = bool(risk_decision.get("allow_exposure_increase")) and not local_budget_blocked
        return {
            "schema_version": "grid-range-drag-preview-v1",
            "cycle_id": cycle_id,
            "expected_strategy_plan_id": expected_plan_id,
            "expected_strategy_plan_version": expected_plan_version,
            "preview_id": candidate["preview_id"],
            "geometry": geometry,
            "old": _range_preview_specification(
                current,
                canonical_metrics=current_risk_decision.get("metrics"),
            ),
            "new": _range_preview_specification(
                candidate,
                canonical_metrics=risk_decision.get("metrics"),
            ),
            "candidate": candidate,
            "canonical_risk": {
                "basis": "exact_commands_plus_current_canonical_accounting",
                "old": current_risk_decision,
                "new": risk_decision,
            },
            "risk_decision": risk_decision,
            "can_apply": can_apply,
            "confirm_disabled_reasons": [
                str(row.get("code") or "risk_blocked")
                for row in blockers
                if isinstance(row, dict)
            ]
            + (
                ["preview_risk_budget_exceeded"]
                if local_budget_blocked
                else []
            ),
            "risk_recalculation": {
                "available": recalculation_available and not recalculated,
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
            return {"action": action, "preview": self.preview(cycle_id, body, market=market or {}, account=account)}
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
        current = self.active_plan(cycle_id)
        if not current:
            raise ValueError("cannot start without an already selected active StrategyPlan")
        preview = self.preview(cycle_id, body, market=market, account=account)
        runtime = self.runtime_state(cycle_id)
        adapter = build_configured_execution_engine_adapter(self.output_root, config=self.config)
        pending = self._accepted_orders(cycle_id, adapter=adapter)
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

        adjusted = self._plan_from_preview(current, preview, now=now)
        timestamp = _timestamp(now)
        commands = build_plan_grid_entry_commands(adjusted, timestamp=timestamp)
        risk_decision = self._authorize_grid_mutation(
            cycle_id,
            action_class="increase_exposure",
            intent="start_grid",
            plan=adjusted,
            commands=commands,
            account=account,
            market=market,
            adapter=adapter,
            timestamp=timestamp,
        )
        self._assert_rollover_start_guard(body)
        # From this point through the first submit the shared control lock owns
        # every in-process production mutation path. Plan/runtime writes do not
        # alter any economic input bound by the immediately preceding recheck.
        self._activate_plan(adjusted)
        starting = {
            **runtime,
            "cycle_id": cycle_id,
            "desired_state": "running",
            "actual_state": "starting",
            "updated_at": timestamp,
            "last_action": "start",
            "last_error": None,
            "strategy_plan_id": adjusted["strategy_plan_id"],
            "strategy_plan_version": adjusted["version"],
            "preview_id": preview["preview_id"],
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
        request_fingerprint = _range_replacement_request_fingerprint(
            expected_plan_id,
            expected_plan_version,
            expected_preview_id,
            handle=handle,
            requested_range=requested_range,
            recalculate_notional=recalculate,
            expected_execution=expected_execution,
        )
        prior = self._replacement_plan(cycle_id, request_fingerprint)
        if prior and str(prior.get("status") or "") == "active":
            return self._reconcile_completed_replacement(
                cycle_id,
                prior,
                market=market,
                now=now,
            )

        runtime = self.runtime_state(cycle_id)
        current = self.active_plan(cycle_id)
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
        if latest.get("can_apply") is not True:
            reasons = ",".join(latest.get("confirm_disabled_reasons") or [])
            raise ValueError(f"range_replacement_blocked:{reasons or 'risk_blocked'}")

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
        latest_price = _positive_number(market.get("latest_close"), "market latest_close")
        candidate_range = dict(staged.get("range") or {})
        if not (
            _positive_number(candidate_range.get("low"), "replacement range low")
            <= latest_price
            <= _positive_number(candidate_range.get("high"), "replacement range high")
        ):
            raise ValueError("market_outside_requested_range")
        rebuilt = self.preview(
            cycle_id,
            _grid_preview_payload_from_plan(staged),
            market=market,
            account=account,
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
        latest_price = _positive_number(market.get("latest_close"), "market latest_close")
        candidate_range = dict(staged.get("range") or {})
        if not (
            _positive_number(candidate_range.get("low"), "replacement range low")
            <= latest_price
            <= _positive_number(candidate_range.get("high"), "replacement range high")
        ):
            raise ValueError("market_outside_requested_range")
        rebuilt = self.preview(
            cycle_id,
            _grid_preview_payload_from_plan(staged),
            market=market,
            account=account,
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
        )

        effective_low = float(adjustment["effective_range"]["low"])
        effective_high = float(adjustment["effective_range"]["high"])
        retained_entries = [
            row
            for row in accepted_entries
            if effective_low - 1e-8
            <= _positive_number(row.get("price"), "accepted entry price")
            <= effective_high + 1e-8
        ]
        outside_entries = [
            row
            for row in accepted_entries
            if not (
                effective_low - 1e-8
                <= _positive_number(row.get("price"), "accepted entry price")
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
        adjusted["risk_budget"] = {
            **dict(current.get("risk_budget") or {}),
            "leverage": adjusted["grid"].get("leverage"),
            "max_loss": risk_metrics.get("projected_max_loss"),
            "estimated_margin": risk_metrics.get("projected_margin"),
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
        by_id = dict(zip(order_ids, rows, strict=True))
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
        orders_by_id = dict(zip(order_ids, rows, strict=True))
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
        return assert_matching_risk_decision(self.risk_port, decision, current_request).to_dict()

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
        return build_grid_risk_request(
            checked_at=timestamp,
            action_class=action_class,
            intent=intent,
            plan=plan,
            commands=commands,
            account_context=account,
            market=market,
            execution_snapshot=adapter.snapshot(cycle_id),
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
        self._write_runtime(stopped)
        return {
            "action": "stop",
            "runtime": stopped,
            "cancelled_orders": cancelled,
            "flattened_positions": len(flattened),
            "execution_event": execution_event,
            "reconciliation": reconciliation,
            "historical_records_preserved": True,
            "safe_action_market_gates": safe_action_market_gates,
        }

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
            "locked_at": _timestamp(now),
            "direction": preview["direction"],
            "style": preview["style"],
            "range": dict(preview["range"]),
            "grid": {**preview["grid"], "orders": [dict(order) for order in preview["orders"]]},
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
        _validate_market(market or {})
        price = _positive_number((market or {}).get("latest_close"), "market latest_close")
        timestamp = _timestamp(now)
        return adapter.process_market_event({
            "schema_version": "dualtrack-market-event-v1",
            "event_id": f"{identity}:{cycle_id}:{timestamp}",
            "cycle_id": cycle_id,
            "ts_event": timestamp,
            "event_started_at": (market or {}).get("latest_timestamp"),
            "source": str((market or {}).get("provider") or (market or {}).get("source_mode") or ""),
            "provider": str((market or {}).get("provider") or ""),
            "instrument_id": str(
                ((self.config.get("execution_shadow") or {}).get("nautilus") or {}).get(
                    "execution_instrument_id"
                )
                or (market or {}).get("symbol")
                or ""
            ),
            "symbol": str((market or {}).get("symbol") or "GOLD"),
            "timeframe": str((market or {}).get("timeframe") or "1m"),
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "price": price,
            "fresh": True,
            "is_synthetic": False,
        })

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


def _range_preview_specification(
    source: dict[str, Any],
    *,
    canonical_metrics: Any = None,
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
        "estimated_margin": metrics.get(
            "projected_margin",
            risk.get("estimated_margin"),
        ),
        "actual_leverage": metrics.get(
            "projected_actual_leverage",
            risk.get("actual_leverage"),
        ),
        "max_loss": metrics.get(
            "projected_max_loss",
            risk.get("max_loss"),
        ),
        "canonical_max_side_notional": metrics.get("projected_max_side_notional"),
        "canonical_equity": metrics.get("equity"),
        "leverage": grid.get("leverage"),
    }


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
