import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "docs" / "contracts" / "paper-supervisor-convergence-v2.md"
VOCABULARY_PATH = ROOT / "docs" / "contracts" / "paper-supervisor-blocker-v3.json"


def _vocabulary() -> dict:
    return json.loads(VOCABULARY_PATH.read_text(encoding="utf-8"))


def test_v2_classifier_is_exact_and_fail_closed() -> None:
    vocabulary = _vocabulary()

    assert vocabulary["matching"] == "exact_typed_only"
    assert vocabulary["default"] == {
        "machine_code": "unknown_blocker",
        "classification": "structural",
    }

    all_codes = (
        vocabulary["transient"]
        + vocabulary["structural"]
        + vocabulary["event_labels"]
    )
    assert len(all_codes) == len(set(all_codes))
    assert all(re.fullmatch(r"[a-z][a-z0-9_]*", code) for code in all_codes)
    assert not any(any(token in code for token in ("*", ".", "^", "$", "|")) for code in all_codes)


def test_v2_amendment_records_every_approved_change() -> None:
    vocabulary = _vocabulary()

    assert vocabulary["amendments_from_v1"] == [
        {
            "change": "add",
            "from": None,
            "to": "supervisor_attempt_deadline_before_intent",
            "classification": "transient",
        },
        {
            "change": "add",
            "from": None,
            "to": "outer_strategy_policy_expired",
            "classification": "structural",
        },
        {
            "change": "replace",
            "from": "cycle_start_attempt_cap_reached",
            "to": "dangerous_start_attempt_cap_reached",
            "classification": "structural",
        },
        {
            "change": "add",
            "from": None,
            "to": "clean_refusal_observation_cap_reached",
            "classification": "structural",
        },
        {
            "change": "replace",
            "from": "retry_budget_exhausted",
            "to": "episode_short_budget_exhausted",
            "classification": "event",
        },
        {
            "change": "add",
            "from": None,
            "to": "clean_refusal_observation_budget_warning",
            "classification": "event",
        },
    ]


def test_v2_preserves_every_previously_approved_structural_code() -> None:
    structural = set(_vocabulary()["structural"])
    previously_approved = {
        "unknown_blocker",
        "risk_envelope_missing",
        "risk_envelope_authorization_invalid",
        "risk_envelope_preview_out_of_bounds",
        "manual_risk_confirmation_required",
        "prepared_start_identity_changed",
        "strategy_preview_identity_changed",
        "active_plan_missing",
        "previous_cycle_paper_state_unresolved",
        "runtime_state_conflict",
        "existing_exposure_conflict",
        "plan_identity_conflict",
        "order_identity_conflict",
        "execution_receipt_identity_invalid",
        "ledger_reconciliation_drift",
        "immutable_fill_guard_triggered",
        "risk_policy_rejected",
        "trusted_market_provenance_invalid",
        "partial_execution_or_cleanup_required",
        "attempt_store_corrupt",
        "supervisor_configuration_invalid",
        "outer_strategy_policy_missing",
        "outer_strategy_policy_invalid",
        "outer_strategy_policy_envelope_out_of_bounds",
        "execution_tick_scheduler_down",
        "control_outcome_unknown",
    }

    assert previously_approved <= structural


def test_contract_and_machine_vocabulary_match_verbatim() -> None:
    vocabulary = _vocabulary()
    contract = CONTRACT_PATH.read_text(encoding="utf-8")

    for code in (
        vocabulary["transient"]
        + vocabulary["structural"]
        + vocabulary["event_labels"]
    ):
        assert f"`{code}`" in contract

    assert "`retry_budget_exhausted`" in contract
    assert "`cycle_start_attempt_cap_reached`" in contract
    assert "No other addition, removal, rename or semantic change is authorized." in contract


def test_contract_keeps_critical_safety_boundaries() -> None:
    contract = CONTRACT_PATH.read_text(encoding="utf-8")
    contract_lower = contract.lower()
    normalized_contract = " ".join(contract.split())

    required = [
        "`unknown_blocker`/structural",
        "append+fsync start_intent",
        "permanently spent",
        "strictly before durable `start_intent`",
        "at or after `start_intent`",
        "**Dangerous cap: 2.**",
        "**Clean-refusal observation guard: 48.**",
        "Episode id, short budget and probe state never cross a cycle boundary.",
        "48 continuous Cloud hours",
    ]
    for text in required:
        assert text in contract
    assert "substring, prefix, regex" in contract_lower
    assert "It does not drive the dead-man fail endpoint." in normalized_contract
