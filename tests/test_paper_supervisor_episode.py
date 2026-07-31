from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services.paper_supervisor_classifier import (
    STRUCTURAL,
    STRUCTURAL_MACHINE_CODES,
    TRANSIENT,
    TRANSIENT_MACHINE_CODES,
    classify_blocker,
)
from services.paper_supervisor_episode import (
    CLEAN_REFUSAL_LIMIT,
    CLEAN_REFUSAL_WARNING_AT,
    DANGEROUS_START_LIMIT,
    HEARTBEAT_STRUCTURAL_AFTER_SECONDS,
    PROBE_INTERVAL_SECONDS,
    SHORT_BACKOFF_SECONDS,
    CleanRefusalProof,
    SupervisorEpisodeError,
    SupervisorEpisodeMachine,
    classify_attempt_deadline,
    classify_upstream_timeout,
)


CYCLE = "2026-07-30_DAY"
NEXT_CYCLE = "2026-07-30_NIGHT"
START = datetime(2026, 7, 30, 8, 0, tzinfo=timezone.utc)
AUTHORITY_DIGEST = "a" * 64


def _new() -> dict:
    return SupervisorEpisodeMachine().new_cycle(
        CYCLE,
        observed_at=START,
    )


def _transient(code: str = "prepared_start_market_moved") -> dict:
    result = classify_blocker(control_code=code)
    assert result["classification"] == TRANSIENT
    return result


def _structural(code: str = "ledger_reconciliation_drift") -> dict:
    result = classify_blocker(control_code=code)
    assert result["classification"] == STRUCTURAL
    return result


def _five_transient_failures(
    machine: SupervisorEpisodeMachine,
    state: dict,
) -> tuple[dict, datetime]:
    observed = START + timedelta(minutes=1)
    for index in range(5):
        state = machine.record_transient_failure(
            state,
            classification=_transient(),
            observed_at=observed,
        )
        assert state["episode"]["short_backoff_seconds"] == (
            SHORT_BACKOFF_SECONDS[index]
        )
        next_attempt = state["episode"]["next_attempt_at"]
        if state["mode"] == "backing_off":
            assert state["alert_required"] is False
            observed = datetime.fromisoformat(next_attempt)
    return state, observed


def _proof(
    *,
    runtime: str = "stopped",
    orders: int = 0,
    audit_result: str = "rejected",
    audit_prepared: str = "prepared-start-1",
    runtime_prepared: str | None = None,
) -> CleanRefusalProof:
    return CleanRefusalProof(
        prepared_start_id="prepared-start-1",
        runtime_actual_state=runtime,
        runtime_prepared_start_id=runtime_prepared,
        accepted_order_count=orders,
        control_audit_result=audit_result,
        control_audit_prepared_start_id=audit_prepared,
        authority_digest=AUTHORITY_DIGEST,
    )


def test_classifier_sets_match_the_approved_machine_contract_exactly() -> None:
    root = Path(__file__).resolve().parents[1]
    contract = json.loads(
        (
            root
            / "docs"
            / "contracts"
            / "paper-supervisor-blocker-v2.json"
        ).read_text(encoding="utf-8")
    )

    assert TRANSIENT_MACHINE_CODES == frozenset(contract["transient"])
    assert STRUCTURAL_MACHINE_CODES == frozenset(contract["structural"])
    for code in TRANSIENT_MACHINE_CODES:
        assert classify_blocker(control_code=code)["classification"] == TRANSIENT
    for code in STRUCTURAL_MACHINE_CODES:
        assert classify_blocker(control_code=code)["classification"] == STRUCTURAL
    assert classify_blocker(
        control_code="prepared_start_market_moved_more"
    )["machine_code"] == "unknown_blocker"


def test_pre_intent_deadline_is_transient_but_post_intent_is_unknown() -> None:
    before = classify_attempt_deadline(start_intent_persisted=False)
    after = classify_attempt_deadline(start_intent_persisted=True)

    assert before["machine_code"] == (
        "supervisor_attempt_deadline_before_intent"
    )
    assert before["classification"] == TRANSIENT
    assert after["machine_code"] == "control_outcome_unknown"
    assert after["classification"] == STRUCTURAL

    upstream = classify_upstream_timeout(
        source_failure="upstream_timeout",
        market_trusted=True,
    )
    assert upstream["machine_code"] == (
        "upstream_data_source_transient_failure"
    )
    assert upstream != before


def test_five_transient_failures_alert_and_enter_nonterminal_probe_mode() -> None:
    machine = SupervisorEpisodeMachine()
    state, observed = _five_transient_failures(machine, _new())

    assert state["mode"] == "probing"
    assert state["blocker"] is None
    assert state["alert_required"] is True
    assert state["episode"]["consecutive_transient_failures"] == 5
    assert state["episode"]["short_backoff_seconds"] == 1200
    assert state["episode"]["next_attempt_at"] == (
        observed + timedelta(seconds=PROBE_INTERVAL_SECONDS)
    ).isoformat()
    exhausted = state["events"][-1]
    assert exhausted["event_label"] == "episode_short_budget_exhausted"
    assert exhausted["detail"]["cycle_terminated"] is False
    assert not machine.attempt_is_due(
        state,
        observed_at=observed + timedelta(seconds=1799),
    )
    state, early_guard = machine.guard_start_intent(
        state,
        observed_at=observed + timedelta(seconds=1799),
    )
    assert early_guard["allowed"] is False
    assert early_guard["reason"] == "probing"
    assert early_guard["start_intent_written"] is False
    assert machine.attempt_is_due(
        state,
        observed_at=observed + timedelta(seconds=1800),
    )


def test_short_backoff_blocks_before_start_intent_until_due() -> None:
    machine = SupervisorEpisodeMachine()
    failed_at = START + timedelta(minutes=1)
    state = machine.record_transient_failure(
        _new(),
        classification=_transient(),
        observed_at=failed_at,
    )

    state, early_guard = machine.guard_start_intent(
        state,
        observed_at=failed_at + timedelta(seconds=59),
    )
    assert early_guard == {
        "allowed": False,
        "machine_code": None,
        "classification": None,
        "reason": "backing_off",
        "next_attempt_at": (
            failed_at + timedelta(seconds=SHORT_BACKOFF_SECONDS[0])
        ).isoformat(),
        "start_intent_written": False,
    }
    state, due_guard = machine.guard_start_intent(
        state,
        observed_at=failed_at + timedelta(seconds=60),
    )
    assert due_guard["allowed"] is True
    assert due_guard["start_intent_written"] is False
    with pytest.raises(
        SupervisorEpisodeError,
        match="supervisor_attempt_not_due",
    ):
        machine.record_transient_failure(
            state,
            classification=_transient(),
            observed_at=failed_at + timedelta(seconds=59),
        )


def test_probe_failure_stays_live_and_prepare_success_resets_episode() -> None:
    machine = SupervisorEpisodeMachine()
    state, fifth_failure_at = _five_transient_failures(machine, _new())
    old_episode = state["episode"]["episode_id"]
    probe_at = fifth_failure_at + timedelta(minutes=30)
    state = machine.record_transient_failure(
        state,
        classification=_transient("prepared_start_expired"),
        observed_at=probe_at,
    )
    assert state["mode"] == "probing"
    assert state["episode"]["probe_attempts"] == 1
    assert state["episode"]["next_attempt_at"] == (
        probe_at + timedelta(minutes=30)
    ).isoformat()

    state = machine.record_prepare_start_success(
        state,
        observed_at=probe_at + timedelta(minutes=30),
    )
    assert state["mode"] == "ready"
    assert state["alert_required"] is False
    assert state["episode"]["episode_id"] != old_episode
    assert state["episode"]["consecutive_transient_failures"] == 0
    assert state["episode"]["probe_attempts"] == 0
    assert state["episode"]["next_attempt_at"] is None

    state = machine.record_transient_failure(
        state,
        classification=_transient(),
        observed_at=probe_at + timedelta(minutes=31),
        prepare_start_succeeded=True,
    )
    assert state["episode"]["consecutive_transient_failures"] == 1
    assert state["mode"] == "backing_off"


def test_clean_refusals_use_separate_guard_and_warn_without_alerting() -> None:
    machine = SupervisorEpisodeMachine()
    state = _new()
    for index in range(CLEAN_REFUSAL_WARNING_AT):
        state = machine.record_clean_refusal(
            state,
            classification=_transient(),
            proof=_proof(),
            observed_at=START + timedelta(minutes=index + 1),
            prepare_start_succeeded=True,
        )

    assert state["budgets"]["dangerous_start_attempts"] == 0
    assert state["budgets"]["clean_refusal_observations"] == 40
    assert state["warning_required"] is True
    assert any(
        row["event_label"]
        == "clean_refusal_observation_budget_warning"
        for row in state["events"]
    )
    assert state["blocker"] is None

    for index in range(
        CLEAN_REFUSAL_WARNING_AT,
        CLEAN_REFUSAL_LIMIT,
    ):
        state = machine.record_clean_refusal(
            state,
            classification=_transient(),
            proof=_proof(),
            observed_at=START + timedelta(minutes=index + 1),
            prepare_start_succeeded=True,
        )
    assert state["budgets"]["clean_refusal_observations"] == 48
    state, decision = machine.guard_start_intent(
        state,
        observed_at=START + timedelta(hours=2),
    )
    assert decision == {
        "allowed": False,
        "machine_code": "clean_refusal_observation_cap_reached",
        "classification": STRUCTURAL,
        "start_intent_written": False,
    }
    assert state["mode"] == "blocked_structural"
    assert state["alert_required"] is True
    with pytest.raises(
        SupervisorEpisodeError,
        match="cycle_budget_blocker_not_clearable",
    ):
        machine.recheck_structural_blocker(
            state,
            machine_code="clean_refusal_observation_cap_reached",
            condition_cleared=True,
            observed_at=START + timedelta(hours=2, seconds=1),
        )


@pytest.mark.parametrize(
    "proof",
    [
        _proof(runtime="running"),
        _proof(orders=1),
        _proof(audit_result="accepted"),
        _proof(audit_prepared="prepared-start-other"),
        _proof(runtime_prepared="prepared-start-other"),
    ],
)
def test_unproven_clean_refusal_consumes_dangerous_budget(
    proof: CleanRefusalProof,
) -> None:
    state = SupervisorEpisodeMachine().record_clean_refusal(
        _new(),
        classification=_transient(),
        proof=proof,
        observed_at=START + timedelta(minutes=1),
        prepare_start_succeeded=True,
    )

    assert state["budgets"]["clean_refusal_observations"] == 0
    assert state["budgets"]["dangerous_start_attempts"] == 1
    assert state["mode"] == "blocked_structural"
    assert state["blocker"]["machine_code"] == "control_outcome_unknown"


def test_two_dangerous_outcomes_block_a_would_be_third_before_intent() -> None:
    machine = SupervisorEpisodeMachine()
    state = _new()
    for index, code in enumerate(
        [
            "control_outcome_unknown",
            "partial_execution_or_cleanup_required",
        ]
    ):
        state = machine.record_dangerous_outcome(
            state,
            machine_code=code,
            observed_at=START + timedelta(minutes=index + 1),
        )
        assert state["mode"] == "blocked_structural"
        state = machine.recheck_structural_blocker(
            state,
            machine_code=code,
            condition_cleared=True,
            observed_at=START + timedelta(minutes=index + 1, seconds=1),
        )
    assert state["budgets"]["dangerous_start_attempts"] == (
        DANGEROUS_START_LIMIT
    )

    state, decision = machine.guard_start_intent(
        state,
        observed_at=START + timedelta(minutes=3),
    )
    assert decision["allowed"] is False
    assert decision["machine_code"] == (
        "dangerous_start_attempt_cap_reached"
    )
    assert decision["start_intent_written"] is False
    assert state["alert_required"] is True
    with pytest.raises(
        SupervisorEpisodeError,
        match="cycle_budget_blocker_not_clearable",
    ):
        machine.recheck_structural_blocker(
            state,
            machine_code="dangerous_start_attempt_cap_reached",
            condition_cleared=True,
            observed_at=START + timedelta(minutes=4),
        )


def test_proven_structural_rejection_is_zero_order_but_never_retried() -> None:
    state = SupervisorEpisodeMachine().record_clean_refusal(
        _new(),
        classification=_structural(),
        proof=_proof(),
        observed_at=START + timedelta(minutes=1),
        prepare_start_succeeded=True,
    )

    assert state["budgets"]["clean_refusal_observations"] == 1
    assert state["budgets"]["dangerous_start_attempts"] == 0
    assert state["mode"] == "blocked_structural"
    assert state["blocker"]["machine_code"] == (
        "ledger_reconciliation_drift"
    )
    assert state["alert_required"] is True


def test_structural_blocker_requires_exact_recheck_and_executes_no_control() -> None:
    machine = SupervisorEpisodeMachine()
    state = machine.record_structural_blocker(
        _new(),
        classification=_structural(),
        observed_at=START + timedelta(minutes=1),
    )
    assert state["mode"] == "blocked_structural"
    assert state["alert_required"] is True
    state, guard = machine.guard_start_intent(
        state,
        observed_at=START + timedelta(minutes=2),
    )
    assert guard["start_intent_written"] is False

    state = machine.recheck_structural_blocker(
        state,
        machine_code="ledger_reconciliation_drift",
        condition_cleared=False,
        observed_at=START + timedelta(minutes=3),
    )
    assert state["mode"] == "blocked_structural"
    assert state["events"][-1]["detail"]["control_actions_executed"] == 0

    with pytest.raises(
        SupervisorEpisodeError,
        match="structural_recheck_identity_invalid",
    ):
        machine.recheck_structural_blocker(
            state,
            machine_code="order_identity_conflict",
            condition_cleared=True,
            observed_at=START + timedelta(minutes=4),
        )
    state = machine.recheck_structural_blocker(
        state,
        machine_code="ledger_reconciliation_drift",
        condition_cleared=True,
        observed_at=START + timedelta(minutes=4),
    )
    assert state["mode"] == "ready"
    assert state["blocker"] is None
    assert state["alert_required"] is False
    assert state["events"][-1]["detail"]["old_command_replayed"] is False


def test_cleared_structural_overlay_resumes_probe_without_command_replay() -> None:
    machine = SupervisorEpisodeMachine()
    state, fifth_failure_at = _five_transient_failures(machine, _new())
    probe_due = state["episode"]["next_attempt_at"]

    state = machine.record_structural_blocker(
        state,
        classification=_structural(),
        observed_at=fifth_failure_at + timedelta(minutes=1),
    )
    assert state["blocker"]["resume_mode"] == "probing"

    state = machine.recheck_structural_blocker(
        state,
        machine_code="ledger_reconciliation_drift",
        condition_cleared=True,
        observed_at=fifth_failure_at + timedelta(minutes=2),
    )
    assert state["mode"] == "probing"
    assert state["episode"]["next_attempt_at"] == probe_due
    assert state["alert_required"] is True
    assert state["events"][-1]["detail"] == {
        "condition_cleared": True,
        "control_actions_executed": 0,
        "old_command_replayed": False,
        "resume_mode": "probing",
    }


def test_heartbeat_promotes_only_after_more_than_ten_continuous_minutes() -> None:
    machine = SupervisorEpisodeMachine()
    state, first = machine.observe_heartbeat(
        _new(),
        status="missing",
        observed_at=START,
    )
    assert first["machine_code"] == (
        "execution_tick_heartbeat_temporarily_missing"
    )
    state, exact = machine.observe_heartbeat(
        state,
        status="missing",
        observed_at=START
        + timedelta(seconds=HEARTBEAT_STRUCTURAL_AFTER_SECONDS),
    )
    assert exact["classification"] == TRANSIENT
    state, late = machine.observe_heartbeat(
        state,
        status="missing",
        observed_at=START
        + timedelta(seconds=HEARTBEAT_STRUCTURAL_AFTER_SECONDS + 1),
    )
    assert late["machine_code"] == "execution_tick_scheduler_down"
    assert late["classification"] == STRUCTURAL
    assert state["mode"] == "blocked_structural"
    assert state["alert_required"] is True

    state, fresh = machine.observe_heartbeat(
        state,
        status="fresh",
        observed_at=START + timedelta(minutes=11),
    )
    assert fresh["machine_code"] is None
    assert state["mode"] == "blocked_structural"
    state = machine.recheck_structural_blocker(
        state,
        machine_code="execution_tick_scheduler_down",
        condition_cleared=True,
        observed_at=START + timedelta(minutes=11, seconds=1),
    )
    state, next_missing = machine.observe_heartbeat(
        state,
        status="missing",
        observed_at=START + timedelta(minutes=12),
    )
    assert next_missing["classification"] == TRANSIENT
    assert state["heartbeat"]["missing_episode_seconds"] == 0.0


def test_new_cycle_has_clean_episode_and_attempt_budgets() -> None:
    machine = SupervisorEpisodeMachine()
    state = machine.record_dangerous_outcome(
        _new(),
        machine_code="control_outcome_unknown",
        observed_at=START + timedelta(minutes=1),
    )
    old_episode = state["episode"]["episode_id"]

    next_state = machine.new_cycle(
        NEXT_CYCLE,
        observed_at=START + timedelta(hours=12),
    )
    assert next_state["cycle_id"] == NEXT_CYCLE
    assert next_state["episode"]["episode_id"] != old_episode
    assert next_state["episode"]["consecutive_transient_failures"] == 0
    assert next_state["budgets"]["dangerous_start_attempts"] == 0
    assert next_state["budgets"]["clean_refusal_observations"] == 0
    assert next_state["blocker"] is None
    assert next_state["events"][0]["detail"] == {
        "budgets_reset": True,
        "prior_cycle_state_inherited": False,
    }


def test_unknown_codes_and_tampered_state_fail_closed() -> None:
    unknown = classify_blocker(
        control_code="prepared_start_market_moved_again"
    )
    assert unknown["machine_code"] == "unknown_blocker"
    state = SupervisorEpisodeMachine().record_structural_blocker(
        _new(),
        classification=unknown,
        observed_at=START + timedelta(minutes=1),
    )
    assert state["blocker"]["machine_code"] == "unknown_blocker"

    tampered = _new()
    tampered["budgets"]["dangerous_start_attempts"] = -1
    with pytest.raises(
        SupervisorEpisodeError,
        match="episode_state_invalid",
    ):
        SupervisorEpisodeMachine().guard_start_intent(
            tampered,
            observed_at=START,
        )


def test_pre_intent_deadline_does_not_consume_dangerous_budget() -> None:
    state = SupervisorEpisodeMachine().record_transient_failure(
        _new(),
        classification=classify_attempt_deadline(
            start_intent_persisted=False
        ),
        observed_at=START + timedelta(minutes=1),
    )
    assert state["budgets"]["dangerous_start_attempts"] == 0
    assert state["mode"] == "backing_off"


def test_unapproved_upstream_timeout_shape_is_not_retryable() -> None:
    classified = classify_upstream_timeout(
        source_failure="provider_slow_maybe",
        market_trusted=False,
    )
    assert classified["machine_code"] == "unknown_blocker"
    assert classified["classification"] == STRUCTURAL

    malformed = classify_blocker(
        evidence={
            "source_failure": "upstream_timeout",
            "market_trusted": "no",
        }
    )
    assert malformed["machine_code"] == "unknown_blocker"
    assert malformed["classification"] == STRUCTURAL
