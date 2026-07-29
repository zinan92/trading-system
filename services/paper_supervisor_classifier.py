"""Closed, typed blocker taxonomy for the Paper Supervisor.

This module deliberately does not inspect exception prose.  Callers pass an
exact control code or typed authoritative evidence; every other condition is
classified as ``unknown_blocker`` / structural.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


CLASSIFIER_VERSION = "paper-supervisor-blocker-v1"
TRANSIENT = "transient"
STRUCTURAL = "structural"

_EXACT_TRANSIENT = {
    "prepared_start_market_moved": "prepared_start_market_moved",
    "prepared_start_expired": "prepared_start_expired",
}
_EXACT_STRUCTURAL = {
    "prepared_start_changed": "prepared_start_identity_changed",
    "strategy_preview_changed": "strategy_preview_identity_changed",
    "cannot prepare start without an active StrategyPlan": "active_plan_missing",
    "cannot start without an already selected active StrategyPlan": "active_plan_missing",
    "robot is already running; stop it before changing the grid": "runtime_state_conflict",
    "strategy_plan_changed": "runtime_state_conflict",
    "new grid start requires zero accepted orders and zero open positions": "existing_exposure_conflict",
    "paper ledger reconciliation failed": "ledger_reconciliation_drift",
    "paper start receipts require valid unique order IDs": "execution_receipt_identity_invalid",
    "range_risk_acknowledgements_incomplete": "manual_risk_confirmation_required",
    "dca_risk_acknowledgements_incomplete": "manual_risk_confirmation_required",
}
_TEMPORARY_SOURCE_FAILURES = {
    "upstream_timeout",
    "upstream_connect_failure",
    "upstream_http_5xx",
}


def classify_blocker(
    *,
    control_code: str | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify only explicit, known conditions; otherwise fail closed."""

    code = str(control_code or "")
    typed = evidence if isinstance(evidence, Mapping) else {}
    if typed.get("control_outcome") == "unknown":
        return _result("control_outcome_unknown", STRUCTURAL, code, typed)
    if typed.get("immutable_fill_guard") is True:
        return _result("immutable_fill_guard_triggered", STRUCTURAL, code, typed)
    if typed.get("previous_cycle_unresolved") is True:
        return _result("previous_cycle_paper_state_unresolved", STRUCTURAL, code, typed)
    if typed.get("partial_execution_or_cleanup_required") is True:
        return _result("partial_execution_or_cleanup_required", STRUCTURAL, code, typed)
    if typed.get("outer_strategy_policy") in {"missing", "expired"}:
        return _result("outer_strategy_policy_missing", STRUCTURAL, code, typed)
    if typed.get("outer_strategy_policy") == "invalid":
        return _result("outer_strategy_policy_invalid", STRUCTURAL, code, typed)
    if typed.get("outer_strategy_policy") == "out_of_bounds":
        return _result("outer_strategy_policy_envelope_out_of_bounds", STRUCTURAL, code, typed)
    if typed.get("risk_envelope") in {"missing", "expired"}:
        return _result("risk_envelope_missing", STRUCTURAL, code, typed)
    if typed.get("risk_envelope") == "invalid":
        return _result("risk_envelope_authorization_invalid", STRUCTURAL, code, typed)
    if typed.get("risk_envelope") == "out_of_bounds":
        return _result("risk_envelope_preview_out_of_bounds", STRUCTURAL, code, typed)
    if typed.get("tick_episode_seconds") is not None:
        age = _finite_nonnegative(typed.get("tick_episode_seconds"))
        if age is None:
            return _result("unknown_blocker", STRUCTURAL, code, typed)
        if typed.get("tick_health") == "missing":
            return _result(
                "execution_tick_scheduler_down" if age > 600 else "execution_tick_heartbeat_temporarily_missing",
                STRUCTURAL if age > 600 else TRANSIENT,
                code,
                typed,
            )
    source_failure = typed.get("source_failure")
    if source_failure in _TEMPORARY_SOURCE_FAILURES:
        if typed.get("market_trusted") is False:
            return _result("trusted_market_temporarily_unavailable", TRANSIENT, code, typed)
        return _result("upstream_data_source_transient_failure", TRANSIENT, code, typed)
    if code in _EXACT_TRANSIENT:
        return _result(_EXACT_TRANSIENT[code], TRANSIENT, code, typed)
    if code in _EXACT_STRUCTURAL:
        return _result(_EXACT_STRUCTURAL[code], STRUCTURAL, code, typed)
    return _result("unknown_blocker", STRUCTURAL, code, typed)


def _result(normalized_code: str, classification: str, raw_code: str, evidence: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "classifier_version": CLASSIFIER_VERSION,
        "machine_code": normalized_code,
        "classification": classification,
        "raw_control_code": raw_code or None,
        "evidence": dict(evidence),
    }


def _finite_nonnegative(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 and number != float("inf") else None
