"""Content-bound execution scenarios and Nautilus candidate receipts.

This module owns evidence contracts only. It cannot submit or cancel orders and
does not compare a Nautilus snapshot with itself as proof of engine parity.
Platform Legacy-to-Nautilus parity remains a separate gate.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from services.accounting_projection import project_execution_accounting
from services.dualtrack_execution_contract import canonical_market_event, normalize_execution_command
from services.dualtrack_nautilus_execution_adapter import REPLAY_VERSION
from services.dualtrack_nautilus_parity_contract import (
    EXPECTED_NAUTILUS_VERSION,
    platform_parity_code_hash,
)


EXECUTION_SCENARIO_SCHEMA = "strategy-execution-scenario-v1"
CANDIDATE_RECEIPT_SCHEMA = "nautilus-candidate-execution-receipt-v1"
_PLAN_SCHEMA = "strategy-plan-v1"
_SNAPSHOT_SCHEMA = "dualtrack-execution-v1"
_RECONCILIATION_SCHEMA = "dualtrack-execution-reconciliation-v1"
_RESERVED_NAMESPACES = {"legacy_paper", "nautilus_paper", "nautilus_authoritative"}


def build_execution_scenario(
    *,
    candidate_id: str,
    plan: dict[str, Any],
    commands: list[dict[str, Any]],
    market_events: list[dict[str, Any]],
    config: dict[str, Any],
    available_at: str | None = None,
) -> dict[str, Any]:
    """Freeze one executable candidate and reject chronology ambiguity."""

    candidate = _required_text(candidate_id, "candidate_id")
    if not isinstance(plan, dict) or plan.get("schema_version") != _PLAN_SCHEMA:
        raise ValueError("execution scenario requires a versioned StrategyPlan")
    frozen_plan = _json_copy(plan)
    plan_id = _required_text(frozen_plan.get("strategy_plan_id"), "strategy_plan_id")
    cycle_id = _required_text(frozen_plan.get("cycle_id"), "plan cycle_id")
    plan_version = _positive_integer(frozen_plan.get("version"), "strategy plan version")
    locked_at = _iso_utc(frozen_plan.get("locked_at"), "StrategyPlan locked_at")
    available = _iso_utc(available_at or locked_at, "plan available_at")
    available_dt = _parse_iso(available, "plan available_at")
    if available_dt < _parse_iso(locked_at, "StrategyPlan locked_at"):
        raise ValueError("execution scenario availability predates StrategyPlan lock")

    if not isinstance(commands, list) or not commands:
        raise ValueError("execution scenario requires at least one command")
    normalized_commands: list[dict[str, Any]] = []
    command_times: list[datetime] = []
    contract_pairs: set[tuple[str, str]] = set()
    for raw in commands:
        if not isinstance(raw, dict):
            raise ValueError("execution scenario command must be an object")
        command = normalize_execution_command(raw, config)
        if str(command.get("cycle_id") or "") != cycle_id:
            raise ValueError("execution scenario command cycle_id mismatch")
        if (
            str(command.get("strategy_plan_id") or "") != plan_id
            or _integer(command.get("strategy_plan_version")) != plan_version
        ):
            raise ValueError("execution scenario command plan identity mismatch")
        command_time = _parse_iso(command.get("ts"), "execution command timestamp")
        if command_time < available_dt:
            raise ValueError("execution scenario command predates plan availability")
        command["ts"] = command_time.astimezone(timezone.utc).isoformat()
        command_times.append(command_time)
        contract = command.get("execution_contract")
        if not isinstance(contract, dict):
            raise ValueError("execution scenario command contract is missing")
        execution_hash = _required_text(contract.get("contract_hash"), "execution contract hash")
        fee_hash = _required_text(contract.get("fee_contract_hash"), "fee contract hash")
        contract_pairs.add((execution_hash, fee_hash))
        normalized_commands.append(_json_copy(command))
    if command_times != sorted(command_times):
        raise ValueError("execution scenario commands must be chronological")
    if len(contract_pairs) != 1:
        raise ValueError("execution scenario commands use different contracts")

    if not isinstance(market_events, list) or not market_events:
        raise ValueError("execution scenario requires market events")
    normalized_events = [canonical_market_event(row) for row in market_events]
    if any(event["cycle_id"] != cycle_id for event in normalized_events):
        raise ValueError("execution scenario market event cycle_id mismatch")
    event_ids = [str(event.get("event_id") or "") for event in normalized_events]
    if len(set(event_ids)) != len(event_ids):
        raise ValueError("execution scenario contains duplicate market event IDs")

    event_keys: list[tuple[datetime, str]] = []
    event_starts: list[datetime] = []
    for event in normalized_events:
        event_at = _parse_iso(event.get("ts_event"), "market event timestamp")
        event_start = _parse_iso(
            event.get("event_started_at") or event.get("ts_event"),
            "market event start timestamp",
        )
        if event_start > event_at:
            raise ValueError("market event start is after its timestamp")
        if event_start < available_dt:
            raise ValueError("execution scenario market event predates plan availability")
        if event_start < max(command_times):
            raise ValueError("execution scenario market event predates command availability")
        event_keys.append((event_at, str(event["event_id"])))
        event_starts.append(event_start)
    if event_keys != sorted(event_keys):
        raise ValueError("execution scenario market events must be chronological")

    execution_hash, fee_hash = next(iter(contract_pairs))
    plan_hash = _hash(frozen_plan)
    command_hash = _hash(normalized_commands)
    market_event_hash = _hash(normalized_events)
    input_hash = _hash({
        "plan_hash": plan_hash,
        "command_hash": command_hash,
        "market_event_hash": market_event_hash,
        "execution_contract_hash": execution_hash,
        "fee_contract_hash": fee_hash,
    })
    payload = {
        "schema_version": EXECUTION_SCENARIO_SCHEMA,
        "candidate_id": candidate,
        "cycle_id": cycle_id,
        "plan_identity": {
            "strategy_plan_id": plan_id,
            "strategy_plan_version": plan_version,
            "available_at": available,
        },
        "plan": frozen_plan,
        "commands": normalized_commands,
        "market_events": normalized_events,
        "evaluation_window": {
            "started_at": min(event_starts).astimezone(timezone.utc).isoformat(),
            "ended_at": max(key[0] for key in event_keys).astimezone(timezone.utc).isoformat(),
            "event_count": len(normalized_events),
        },
        "contracts": {
            "execution_contract_hash": execution_hash,
            "fee_contract_hash": fee_hash,
        },
        "hashes": {
            "plan_hash": plan_hash,
            "command_hash": command_hash,
            "market_event_hash": market_event_hash,
            "input_hash": input_hash,
        },
    }
    payload["scenario_id"] = f"execution-scenario-{_raw_digest(payload)}"
    return payload


def build_candidate_execution_receipt(
    *,
    scenario: dict[str, Any],
    snapshot: dict[str, Any],
    reconciliation: dict[str, Any],
    storage_namespace: str,
) -> dict[str, Any]:
    """Bind one Nautilus result to its exact input; return blocked evidence on drift."""

    blockers: list[str] = []
    if not isinstance(scenario, dict) or scenario.get("schema_version") != EXECUTION_SCENARIO_SCHEMA:
        raise ValueError("candidate receipt requires an execution scenario")
    scenario_id = _required_text(scenario.get("scenario_id"), "scenario_id")
    cycle_id = _required_text(scenario.get("cycle_id"), "scenario cycle_id")
    plan_identity = dict(scenario.get("plan_identity") or {})
    namespace = str(storage_namespace or "").strip()
    if namespace in _RESERVED_NAMESPACES or not namespace.startswith("strategy_shadow_"):
        blockers.append("unsafe_storage_namespace")

    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != _SNAPSHOT_SCHEMA:
        blockers.append("unsupported_execution_snapshot_schema")
        snapshot = dict(snapshot or {})
    if str(snapshot.get("engine") or "") != "nautilus_paper":
        blockers.append("snapshot_engine_not_nautilus")
    if str(snapshot.get("cycle_id") or "") != cycle_id:
        blockers.append("snapshot_cycle_mismatch")
    capabilities = snapshot.get("capabilities") if isinstance(snapshot.get("capabilities"), dict) else {}
    replay_version = str(capabilities.get("replay_version") or "")
    if replay_version != REPLAY_VERSION:
        blockers.append("replay_version_mismatch")
    nautilus_version = str(capabilities.get("nautilus_version") or "").strip()
    if not nautilus_version:
        blockers.append("nautilus_runtime_version_missing")
    elif nautilus_version != EXPECTED_NAUTILUS_VERSION:
        blockers.append("nautilus_runtime_version_mismatch")
    snapshot_code_hash = str(capabilities.get("platform_code_hash") or "")
    try:
        current_code_hash = platform_parity_code_hash()
    except OSError:
        current_code_hash = ""
    if not current_code_hash or snapshot_code_hash != current_code_hash:
        blockers.append("snapshot_platform_code_stale")
    blockers.extend(_snapshot_plan_trace_blockers(snapshot, plan_identity))

    if not isinstance(reconciliation, dict) or reconciliation.get("schema_version") != _RECONCILIATION_SCHEMA:
        blockers.append("unsupported_execution_reconciliation_schema")
        reconciliation = dict(reconciliation or {})
    if str(reconciliation.get("engine") or "") != "nautilus_paper":
        blockers.append("reconciliation_engine_not_nautilus")
    if str(reconciliation.get("cycle_id") or "") != cycle_id:
        blockers.append("reconciliation_cycle_mismatch")
    if str(reconciliation.get("status") or "") != "ok" or reconciliation.get("issues"):
        blockers.append("execution_reconciliation_not_ok")

    accounting_snapshot: dict[str, Any] = {}
    try:
        accounting_snapshot = project_execution_accounting(
            snapshot,
            source_type="strategy_shadow_execution",
            scope={"cycle_id": cycle_id, "scenario_id": scenario_id},
        ).to_dict()
        if str((accounting_snapshot.get("reconciliation") or {}).get("status") or "") != "pass":
            blockers.append("accounting_reconciliation_not_pass")
    except Exception:
        blockers.append("accounting_projection_failed")

    payload = {
        "schema_version": CANDIDATE_RECEIPT_SCHEMA,
        "status": "pass" if not blockers else "blocked",
        "blockers": sorted(set(blockers)),
        "scenario_id": scenario_id,
        "candidate_id": str(scenario.get("candidate_id") or ""),
        "cycle_id": cycle_id,
        "plan_identity": plan_identity,
        "contracts": dict(scenario.get("contracts") or {}),
        "input_hashes": dict(scenario.get("hashes") or {}),
        "engine": "nautilus_paper",
        "replay_version": replay_version,
        "nautilus_version": nautilus_version,
        "platform_code_hash": snapshot_code_hash,
        "storage_namespace": namespace,
        "execution_snapshot_hash": _hash(snapshot),
        "execution_reconciliation_hash": _hash(reconciliation),
        "accounting_snapshot_id": str(accounting_snapshot.get("snapshot_id") or ""),
        "safety": {
            "paper_only": True,
            "real_orders": False,
            "writes_production_ledger": False,
            "writes_authority_gate_evidence": False,
        },
    }
    payload["receipt_id"] = f"candidate-receipt-{_raw_digest(payload)}"
    return payload


def candidate_receipt_blockers(
    receipt: dict[str, Any],
    *,
    expected_scenario_id: str | None = None,
) -> list[str]:
    """Verify a persisted candidate receipt without granting promotion itself."""

    if not isinstance(receipt, dict) or receipt.get("schema_version") != CANDIDATE_RECEIPT_SCHEMA:
        return ["unsupported_candidate_receipt_schema"]
    blockers = [str(item) for item in receipt.get("blockers") or [] if str(item)]
    if receipt.get("status") != "pass":
        blockers.append("candidate_receipt_not_pass")
    if str(receipt.get("engine") or "") != "nautilus_paper":
        blockers.append("candidate_receipt_engine_not_nautilus")
    if str(receipt.get("replay_version") or "") != REPLAY_VERSION:
        blockers.append("candidate_receipt_replay_version_mismatch")
    if not str(receipt.get("nautilus_version") or "").strip():
        blockers.append("candidate_receipt_nautilus_version_missing")
    elif str(receipt.get("nautilus_version") or "").strip() != EXPECTED_NAUTILUS_VERSION:
        blockers.append("candidate_receipt_nautilus_version_mismatch")
    try:
        current_code_hash = platform_parity_code_hash()
    except OSError:
        current_code_hash = ""
    if not current_code_hash or str(receipt.get("platform_code_hash") or "") != current_code_hash:
        blockers.append("candidate_receipt_platform_code_stale")
    if expected_scenario_id is not None and str(receipt.get("scenario_id") or "") != str(expected_scenario_id):
        blockers.append("candidate_receipt_scenario_mismatch")
    safety = receipt.get("safety") if isinstance(receipt.get("safety"), dict) else {}
    if safety != {
        "paper_only": True,
        "real_orders": False,
        "writes_production_ledger": False,
        "writes_authority_gate_evidence": False,
    }:
        blockers.append("candidate_receipt_safety_mismatch")
    expected_id = f"candidate-receipt-{_raw_digest({key: value for key, value in receipt.items() if key != 'receipt_id'})}"
    if str(receipt.get("receipt_id") or "") != expected_id:
        blockers.append("candidate_receipt_integrity_mismatch")
    return sorted(set(blockers))


def _snapshot_plan_trace_blockers(snapshot: dict[str, Any], plan_identity: dict[str, Any]) -> list[str]:
    expected_id = str(plan_identity.get("strategy_plan_id") or "")
    expected_version = _integer(plan_identity.get("strategy_plan_version"))
    blockers: list[str] = []
    for field in ("orders", "fills", "positions"):
        rows = snapshot.get(field)
        if not isinstance(rows, list):
            blockers.append(f"snapshot_{field}_missing")
            continue
        for row in rows:
            if not isinstance(row, dict):
                blockers.append(f"snapshot_{field}_invalid")
                continue
            if str(row.get("strategy_plan_id") or "") != expected_id:
                blockers.append(f"snapshot_{field}_plan_id_mismatch")
            if _integer(row.get("strategy_plan_version")) != expected_version:
                blockers.append(f"snapshot_{field}_plan_version_mismatch")
    return blockers


def _json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=True, allow_nan=False))


def _hash(value: Any) -> str:
    return f"sha256:{_raw_digest(value)}"


def _raw_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _required_text(value: Any, label: str) -> str:
    rendered = str(value or "").strip()
    if not rendered:
        raise ValueError(f"execution scenario {label} is required")
    return rendered


def _positive_integer(value: Any, label: str) -> int:
    parsed = _integer(value)
    if parsed is None or parsed <= 0:
        raise ValueError(f"execution scenario {label} must be positive")
    return parsed


def _integer(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_iso(value: Any, label: str) -> datetime:
    if value in (None, ""):
        raise ValueError(f"{label} is required")
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include timezone")
    return parsed.astimezone(timezone.utc)


def _iso_utc(value: Any, label: str) -> str:
    return _parse_iso(value, label).isoformat()
