"""Closed, typed blocker taxonomy for the Paper Supervisor.

This module deliberately does not inspect exception prose.  Callers pass an
exact control code or typed authoritative evidence; every other condition is
classified as ``unknown_blocker`` / structural.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


CLASSIFIER_VERSION = "paper-supervisor-blocker-v5"
TRANSIENT = "transient"
STRUCTURAL = "structural"

TRANSIENT_MACHINE_CODES = frozenset(
    {
        "prepared_start_market_moved",
        "prepared_start_expired",
        "trusted_market_temporarily_unavailable",
        "execution_tick_heartbeat_temporarily_missing",
        "upstream_data_source_transient_failure",
        "supervisor_attempt_deadline_before_intent",
        "strategy_recommendation_provider_timeout",
        "strategy_recommendation_provider_unavailable",
        "strategy_recommendation_provider_failed",
        "frozen_grid_preview_market_moved",
        "cloud_ai_provider_readiness_unavailable",
    }
)
STRUCTURAL_MACHINE_CODES = frozenset(
    {
        "control_outcome_unknown",
        "partial_execution_or_cleanup_required",
        "ledger_reconciliation_drift",
        "previous_cycle_paper_state_unresolved",
        "order_identity_conflict",
        "attempt_store_corrupt",
        "execution_tick_scheduler_down",
        "outer_strategy_policy_missing",
        "outer_strategy_policy_invalid",
        "outer_strategy_policy_expired",
        "outer_strategy_policy_envelope_out_of_bounds",
        "risk_envelope_missing",
        "risk_envelope_authorization_invalid",
        "risk_envelope_preview_out_of_bounds",
        "manual_risk_confirmation_required",
        "prepared_start_identity_changed",
        "strategy_preview_identity_changed",
        "active_plan_missing",
        "runtime_state_conflict",
        "existing_exposure_conflict",
        "plan_identity_conflict",
        "execution_receipt_identity_invalid",
        "immutable_fill_guard_triggered",
        "risk_policy_rejected",
        "trusted_market_provenance_invalid",
        "supervisor_configuration_invalid",
        "strategy_recommendation_provider_missing",
        "strategy_recommendation_provider_not_executable",
        "strategy_recommendation_provider_invalid_output",
        "strategy_recommendation_provider_command_invalid",
        "strategy_recommendation_provider_timeout_invalid",
        "strategy_recommendation_provider_auth_not_ready",
        "cloud_ai_provider_readiness_invalid",
        "dangerous_start_attempt_cap_reached",
        "clean_refusal_observation_cap_reached",
        "paper_start_facts_missing",
        "paper_start_facts_invalid",
        "paper_start_facts_store_corrupt",
        "paper_start_facts_identity_conflict",
        "start_facts_stale",
        "unknown_blocker",
    }
)

_EXACT_TRANSIENT = {
    "prepared_start_market_moved": "prepared_start_market_moved",
    "prepared_start_expired": "prepared_start_expired",
    "strategy_recommendation_provider_timeout": "strategy_recommendation_provider_timeout",
    "strategy_recommendation_provider_unavailable": "strategy_recommendation_provider_unavailable",
    "strategy_recommendation_provider_failed": "strategy_recommendation_provider_failed",
    "cloud_ai_provider_readiness_unavailable": "cloud_ai_provider_readiness_unavailable",
}
_EXACT_STRUCTURAL = {
    "prepared_start_changed": "prepared_start_identity_changed",
    "strategy_preview_changed": "strategy_preview_identity_changed",
    "strategy_plan_changed": "runtime_state_conflict",
    "range_risk_acknowledgements_incomplete": "manual_risk_confirmation_required",
    "dca_risk_acknowledgements_incomplete": "manual_risk_confirmation_required",
    "outer_strategy_policy_missing": "outer_strategy_policy_missing",
    "outer_strategy_policy_expired": "outer_strategy_policy_expired",
    "outer_strategy_policy_invalid": "outer_strategy_policy_invalid",
    "outer_strategy_policy_envelope_out_of_bounds": "outer_strategy_policy_envelope_out_of_bounds",
    "strategy_recommendation_provider_missing": "strategy_recommendation_provider_missing",
    "strategy_recommendation_provider_not_executable": "strategy_recommendation_provider_not_executable",
    "strategy_recommendation_provider_invalid_output": "strategy_recommendation_provider_invalid_output",
    "strategy_recommendation_provider_command_invalid": "strategy_recommendation_provider_command_invalid",
    "strategy_recommendation_provider_timeout_invalid": "strategy_recommendation_provider_timeout_invalid",
    "strategy_recommendation_provider_auth_not_ready": "strategy_recommendation_provider_auth_not_ready",
    "cloud_ai_provider_readiness_invalid": "cloud_ai_provider_readiness_invalid",
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
    if (
        typed.get("local_attempt_deadline") == "exceeded"
        and typed.get("start_intent_persisted") is False
    ):
        return _result(
            "supervisor_attempt_deadline_before_intent",
            TRANSIENT,
            code,
            typed,
        )
    if (
        typed.get("local_attempt_deadline") == "exceeded"
        and typed.get("start_intent_persisted") is True
    ):
        return _result("control_outcome_unknown", STRUCTURAL, code, typed)
    if typed.get("control_outcome") == "unknown":
        return _result("control_outcome_unknown", STRUCTURAL, code, typed)
    if typed.get("immutable_fill_guard") is True:
        return _result("immutable_fill_guard_triggered", STRUCTURAL, code, typed)
    if typed.get("previous_cycle_unresolved") is True:
        return _result("previous_cycle_paper_state_unresolved", STRUCTURAL, code, typed)
    if typed.get("partial_execution_or_cleanup_required") is True:
        return _result("partial_execution_or_cleanup_required", STRUCTURAL, code, typed)
    if typed.get("outer_strategy_policy") == "missing":
        return _result("outer_strategy_policy_missing", STRUCTURAL, code, typed)
    if typed.get("outer_strategy_policy") == "expired":
        return _result("outer_strategy_policy_expired", STRUCTURAL, code, typed)
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
    if typed.get("active_plan") == "missing":
        return _result("active_plan_missing", STRUCTURAL, code, typed)
    if typed.get("runtime") == "conflict":
        return _result("runtime_state_conflict", STRUCTURAL, code, typed)
    if typed.get("execution") == "existing_exposure_conflict":
        return _result("existing_exposure_conflict", STRUCTURAL, code, typed)
    if typed.get("reconciliation") == "drift":
        return _result("ledger_reconciliation_drift", STRUCTURAL, code, typed)
    if typed.get("order_identity") == "invalid":
        return _result("execution_receipt_identity_invalid", STRUCTURAL, code, typed)
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
        if typed.get("market_trusted") not in {True, False}:
            return _result("unknown_blocker", STRUCTURAL, code, typed)
        if typed.get("market_trusted") is False:
            return _result("trusted_market_temporarily_unavailable", TRANSIENT, code, typed)
        return _result("upstream_data_source_transient_failure", TRANSIENT, code, typed)
    if code in _EXACT_TRANSIENT:
        return _result(_EXACT_TRANSIENT[code], TRANSIENT, code, typed)
    if code in _EXACT_STRUCTURAL:
        return _result(_EXACT_STRUCTURAL[code], STRUCTURAL, code, typed)
    if code in TRANSIENT_MACHINE_CODES:
        return _result(code, TRANSIENT, code, typed)
    if code in STRUCTURAL_MACHINE_CODES:
        return _result(code, STRUCTURAL, code, typed)
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
