"""Single-production-plan control plane with a non-destructive DualTrack migration.

This module deliberately owns *selection and attribution*, not execution.  The
legacy human/machine files remain immutable source records and are exposed as
same-schema proposals until a production plan is locked.
"""

from __future__ import annotations

import hashlib
import fcntl
import json
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
    validate_market as _validate_market,
    positive_number as _positive_number,
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
                if str(action or "").lower() != "preview":
                    self._audit_control(cycle_id, action, payload, actor=actor, result="rejected", error=str(exc), now=now)
                raise
            if str(action or "").lower() != "preview":
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
    ) -> list[dict[str, Any]]:
        receipts: list[dict[str, Any]] = []
        command_rows = commands if commands is not None else build_plan_grid_entry_commands(plan, timestamp=timestamp)
        for command in command_rows:
            receipt = adapter.submit_order(command)
            if str(receipt.get("state") or receipt.get("status") or "") != "accepted":
                raise ValueError("paper execution did not accept a grid order")
            receipts.append(receipt)
        return receipts

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

    def _proposals_path(self, cycle_id: str) -> Path:
        return self.root / "proposals" / f"{cycle_id}.json"

    def _plans_path(self, cycle_id: str) -> Path:
        return self.root / "plans" / f"{cycle_id}.json"

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
