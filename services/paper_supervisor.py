"""State-driven, Paper-only convergence through the public control plane."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from services.control_audit import read_control_events_strict
from services.cloud_ai_provider import (
    CloudAIProviderReadiness,
    CloudAIProviderReadinessGateError,
    RECOVERABLE_PROVIDER_CODES,
    current_cloud_ai_provider_readiness,
    validate_provider_readiness_proof,
)
from services.dca_plan import build_dca_entry_commands
from services.paper_supervisor_classifier import (
    STRUCTURAL,
    TRANSIENT,
    classify_blocker,
)
from services.paper_degradation_events import (
    PaperDegradationEventStore,
)
from services.paper_supervisor_episode import (
    PAPER_CONTINUITY_WATCHDOG_SECONDS,
    CleanRefusalProof,
    SupervisorEpisodeMachine,
)
from services.paper_supervisor_heartbeat import (
    validate_complete_tick_heartbeat,
)
from services.paper_next_cycle_plan import (
    NextCyclePlanError,
    VerifiedWaitingPlanStore,
    plan_content_digest,
    staged_facts_status,
)
from services.paper_supervisor_identity import (
    INTENT_CONTRACT_SCHEMA_VERSION,
    accepted_order_fingerprints,
    accepted_order_identities,
    all_plan_entry_fingerprints,
    build_start_intent_contract,
    open_position_identities,
    order_fingerprint,
)
from services.paper_supervisor_exception_provenance import (
    PaperSupervisorExceptionEvidenceError,
    PaperSupervisorExceptionStore,
    exception_receipt_ref,
)
from services.paper_supervisor_evidence import (
    draft_running_evidence,
    finalize_running_evidence,
)
from services.paper_supervisor_store import (
    PaperSupervisorStore,
    StartAuthoritySnapshot,
)
from services.strategy_plan_execution import (
    build_plan_grid_entry_commands,
)
from services.strategy_control_plane import production_mutation_lock
from services.supervisor_execution_profile import (
    EXECUTION_PROFILES,
    FAIL_CLOSED,
    PAPER_CONTINUOUS,
)
from services.grid_lifecycle_evidence import build_grid_lifecycle_evidence
from services.journal_store import load_json


SUPERVISOR_SCHEMA_VERSION = "paper-supervisor-convergence-v1"
DEFAULT_ATTEMPT_DEADLINE_SECONDS = 45
HARD_DEADLINE_ENV = "GRIDMIND_LIVE_TICK_HARD_DEADLINE_MONOTONIC"
HARD_DEADLINE_RECOVERY_MARGIN_SECONDS = 5.0
_MARKET_GATES = frozenset(
    {
        "trusted_market_temporarily_unavailable",
        "trusted_market_provenance_invalid",
    }
)
_POST_INTENT_UNCERTAINTY_GATES = frozenset(
    {
        "control_outcome_unknown",
        "partial_execution_or_cleanup_required",
    }
)


class SupervisorAttemptDeadline(Exception):
    """The local convergence budget elapsed before the caller returned."""


class PaperSupervisor:
    """Perform at most one eligible convergence attempt per observation."""

    def __init__(
        self,
        output_root: Path,
        *,
        plane: Any,
        execution: Any,
        control: Callable[[str, dict[str, Any]], dict[str, Any]],
        accounting_reconciliation: Callable[[], str],
        pre_intent_diagnostic: (
            Callable[
                [dict[str, Any], str],
                Mapping[str, Any],
            ]
            | None
        ) = None,
        store: PaperSupervisorStore | None = None,
        episode_machine: SupervisorEpisodeMachine | None = None,
        monotonic: Callable[[], float] | None = None,
        attempt_deadline_seconds: int = DEFAULT_ATTEMPT_DEADLINE_SECONDS,
        provider_readiness_verifier: (
            Callable[[], Mapping[str, Any]] | None
        ) = None,
        exception_provenance_store: (
            PaperSupervisorExceptionStore | None
        ) = None,
        exception_source_attestation: (
            Callable[[], Mapping[str, Any]] | None
        ) = None,
        execution_profile: str = FAIL_CLOSED,
    ) -> None:
        if attempt_deadline_seconds <= 0:
            raise ValueError("supervisor_configuration_invalid")
        self.output_root = Path(output_root)
        self.plane = plane
        self.execution = execution
        self.control = control
        self.accounting_reconciliation = accounting_reconciliation
        self.pre_intent_diagnostic = pre_intent_diagnostic
        self.store = store or PaperSupervisorStore(self.output_root)
        self.episodes = episode_machine or SupervisorEpisodeMachine()
        self.monotonic = monotonic or time.monotonic
        self.attempt_deadline_seconds = int(
            attempt_deadline_seconds
        )
        self.provider_readiness_verifier = provider_readiness_verifier
        profile = str(execution_profile or "")
        if profile not in EXECUTION_PROFILES:
            raise ValueError("supervisor_execution_profile_invalid")
        self.execution_profile = profile
        self.degradation_events = PaperDegradationEventStore(
            self.output_root
        )
        self.next_cycle_plans = VerifiedWaitingPlanStore(
            self.output_root
        )
        source_attestation = exception_source_attestation
        if source_attestation is None:
            candidate = getattr(self.plane, "_source_attestation", None)
            source_attestation = candidate if callable(candidate) else None
        self.exception_provenance = (
            exception_provenance_store
            or PaperSupervisorExceptionStore(
                self.output_root,
                source_attestation=source_attestation,
            )
        )

    def converge_once(
        self,
        cycle_id: str,
        *,
        observed_at: str,
        heartbeat: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        started = self.monotonic()
        holder_id = f"paper-supervisor:{uuid.uuid4().hex}"
        with self.store.try_lease(
            cycle_id,
            holder_id=holder_id,
        ) as lease:
            if lease is None:
                return self._result(
                    cycle_id=cycle_id,
                    observed_at=observed_at,
                    status="lease_held",
                    control_actions=0,
                )
            health = validate_complete_tick_heartbeat(
                heartbeat,
                cycle_id=cycle_id,
                observed_at=observed_at,
            )
            heartbeat_digest = _digest(dict(heartbeat or {}))
            health = {
                **dict(health),
                "heartbeat_digest": heartbeat_digest,
            }
            source_tick_key, trust = self._source_tick_identity(
                cycle_id,
                health=health,
            )
            pending_tick = self.store.unfinished_tick(cycle_id)
            if pending_tick is not None:
                lease.resume_tick(pending_tick)
                recovery_heartbeat = self._claim_heartbeat(
                    pending_tick
                )
                if (
                    str(
                        pending_tick.get("source_tick_key") or ""
                    )
                    == source_tick_key
                    and str(
                        pending_tick.get("heartbeat_digest") or ""
                    )
                    != heartbeat_digest
                ):
                    state = self.store.episode_state(cycle_id)
                    if state is None:
                        state = self._new_cycle_state(
                            cycle_id,
                            observed_at=str(
                                pending_tick.get("claimed_at")
                                or observed_at
                            ),
                        )
                    state = self._reconcile_operational_wal(
                        state,
                        cycle_id=cycle_id,
                    )
                    state, _ = self._reconcile_terminal_outcomes(
                        state,
                        cycle_id=cycle_id,
                    )
                    return self._structural(
                        lease,
                        state,
                        cycle_id=cycle_id,
                        observed_at=observed_at,
                        machine_code="attempt_store_corrupt",
                        heartbeat=recovery_heartbeat,
                    )
                return self._recover_unfinished_tick(
                    lease,
                    pending_tick,
                    cycle_id=cycle_id,
                    observed_at=observed_at,
                    heartbeat=recovery_heartbeat,
                    started=started,
                )
            existing_observation = self.store.observation_for_tick(
                cycle_id,
                source_tick_key,
            )
            if existing_observation is not None:
                claim = dict(
                    (
                        existing_observation.get("payload") or {}
                    ).get("tick_claim")
                    or {}
                )
                if claim.get("heartbeat_digest") != heartbeat_digest:
                    return self._result(
                        cycle_id=cycle_id,
                        observed_at=observed_at,
                        status="blocked_structural",
                        machine_code="attempt_store_corrupt",
                        classification=STRUCTURAL,
                        control_actions=0,
                        heartbeat=health,
                    )
                return self._result_from_observation(
                    existing_observation
                )
            lease.claim_tick(
                source_tick_key=source_tick_key,
                heartbeat_digest=heartbeat_digest,
                trust=trust,
                claimed_at=self.store.now().isoformat(),
                heartbeat_recorded_at=health.get("recorded_at"),
            )
            state = self.store.episode_state(cycle_id)
            if state is None:
                state = self._new_cycle_state(
                    cycle_id,
                    observed_at=observed_at,
                )
            try:
                state = self._reconcile_operational_wal(
                    state,
                    cycle_id=cycle_id,
                )
                state, replayed_terminal_outcomes = (
                    self._reconcile_terminal_outcomes(
                        state,
                        cycle_id=cycle_id,
                    )
                )
            except Exception:  # noqa: BLE001 - durable divergence fails closed.
                return self._result(
                    cycle_id=cycle_id,
                    observed_at=observed_at,
                    status="blocked_structural",
                    machine_code="attempt_store_corrupt",
                    classification=STRUCTURAL,
                    control_actions=0,
                    heartbeat={},
                )
            unfinished_pre_intent = (
                self.store.unfinished_pre_intent(cycle_id)
            )
            if unfinished_pre_intent is not None:
                if isinstance(
                    unfinished_pre_intent.get("candidate_identity"),
                    Mapping,
                ):
                    return self._recover_crashed_candidate_rejection(
                        lease,
                        state,
                        cycle_id=cycle_id,
                        unfinished=unfinished_pre_intent,
                        observed_at=observed_at,
                        heartbeat=health,
                    )
                lease.abandon_pre_intent(
                    attempt_id=str(
                        unfinished_pre_intent["attempt_id"]
                    ),
                    observed_at=observed_at,
                )
            heartbeat_status = (
                "fresh"
                if health["status"] == "ready"
                else "missing"
            )
            lease.record_typed_heartbeat(
                observation_id=(
                    f"heartbeat-observation-{uuid.uuid4().hex}"
                ),
                observed_at=observed_at,
                status=heartbeat_status,
                machine_code=(
                    "heartbeat_fresh"
                    if heartbeat_status == "fresh"
                    else str(
                        health.get("machine_code")
                        or "execution_tick_heartbeat_temporarily_missing"
                    )
                ),
                reason=str(health.get("reason") or "complete"),
                heartbeat_recorded_at=health.get("recorded_at"),
                heartbeat_digest=heartbeat_digest,
            )
            state = self._reconcile_operational_wal(
                state,
                cycle_id=cycle_id,
            )
            if health["status"] != "ready":
                blocker = dict(state.get("blocker") or {})
                classified = (
                    {
                        "machine_code": blocker["machine_code"],
                        "classification": STRUCTURAL,
                    }
                    if (
                        state.get("mode") == "blocked_structural"
                        and blocker.get("machine_code")
                        == "execution_tick_scheduler_down"
                    )
                    else {
                        "machine_code": (
                            "execution_tick_heartbeat_temporarily_missing"
                        ),
                        "classification": TRANSIENT,
                    }
                )
                return self._finish(
                    lease,
                    state,
                    self._result(
                        cycle_id=cycle_id,
                        observed_at=observed_at,
                        status=(
                            "blocked_structural"
                            if classified["classification"]
                            == STRUCTURAL
                            else "backing_off"
                        ),
                        machine_code=classified["machine_code"],
                        classification=classified["classification"],
                        control_actions=0,
                        heartbeat=health,
                    ),
                )
            unfinished = self.store.unfinished_intent(cycle_id)
            if unfinished is not None:
                return self._recover_unfinished(
                    lease,
                    state,
                    unfinished=unfinished,
                    observed_at=observed_at,
                    heartbeat=health,
                    started=started,
                )

            attempt_id = f"supervisor-attempt-{uuid.uuid4().hex}"
            lease.record_pre_intent_started(
                attempt_id=attempt_id,
                observed_at=observed_at,
                phase_scope="create_or_prepare",
            )
            try:
                with self._attempt_deadline(started):
                    authority = self._authority(
                        cycle_id,
                        heartbeat=health,
                    )
            except SupervisorAttemptDeadline as exc:
                return self._classify_before_intent(
                    lease,
                    state,
                    cycle_id=cycle_id,
                    observed_at=observed_at,
                    exc=exc,
                    heartbeat=health,
                    deadline_exceeded=True,
                    phase="authority_snapshot",
                )
            except Exception as exc:  # noqa: BLE001 - malformed authority fails closed.
                machine_code = (
                    "attempt_store_corrupt"
                    if str(exc) == "attempt_store_corrupt"
                    else "order_identity_conflict"
                    if str(exc)
                    == "execution_receipt_identity_invalid"
                    else "unknown_blocker"
                )
                return self._classify_before_intent(
                    lease,
                    state,
                    cycle_id=cycle_id,
                    observed_at=observed_at,
                    exc=exc,
                    heartbeat=health,
                    deadline_exceeded=False,
                    phase="authority_snapshot",
                    classification_control_code=machine_code,
                )
            plan = dict(authority.active_plan)
            runtime = dict(authority.runtime)
            continuity_recovery_code: str | None = None
            latched_blocker = dict(state.get("blocker") or {})
            latched_machine_code = str(
                latched_blocker.get("machine_code")
                or dict(state.get("episode") or {}).get(
                    "last_transient_code"
                )
                or ""
            )
            if (
                self._paper_continuous
                and state.get("mode")
                in {"blocked_structural", "backing_off", "probing"}
                and latched_machine_code
                in _POST_INTENT_UNCERTAINTY_GATES
            ):
                state, cleared = self._recheck_structural(
                    state,
                    authority=authority,
                    observed_at=observed_at,
                )
                if not cleared:
                    return self._finish(
                        lease,
                        state,
                        self._result(
                            cycle_id=cycle_id,
                            observed_at=observed_at,
                            status="blocked_structural",
                            machine_code=latched_machine_code,
                            classification=STRUCTURAL,
                            control_actions=0,
                            heartbeat=health,
                            rechecked=True,
                        ),
                        authority=authority,
                    )
                return self._finish(
                    lease,
                    state,
                    self._result(
                        cycle_id=cycle_id,
                        observed_at=observed_at,
                        status="structural_cleared",
                        terminal_status="structural_cleared",
                        control_actions=0,
                        heartbeat=health,
                        rechecked=True,
                    ),
                    authority=authority,
                )
            if (
                self._paper_continuous
                and state.get("mode")
                in {"blocked_structural", "backing_off", "probing"}
            ):
                blocker = dict(state.get("blocker") or {})
                machine_code = str(
                    blocker.get("machine_code")
                    or dict(state.get("episode") or {}).get(
                        "last_transient_code"
                    )
                    or "supervisor_retry_state_latched"
                )
                continuity_recovery_code = machine_code
                if machine_code not in _MARKET_GATES:
                    self._record_degradation(
                        event_id=(
                            f"{attempt_id}:state-reset:{machine_code}"
                        ),
                        cycle_id=cycle_id,
                        bypassed_gate=(
                            "structural_blocker_latch"
                            if state.get("mode")
                            == "blocked_structural"
                            else "supervisor_retry_backoff"
                        ),
                        original_machine_code=machine_code,
                        original_reason=(
                            "the fail-closed profile would preserve this "
                            "Supervisor state and defer a fresh start"
                        ),
                        alternative_action=(
                            "clear_latch_and_schedule_fresh_full_start"
                        ),
                        occurred_at=observed_at,
                    )
                state = self.episodes.reset_for_paper_continuity(
                    state,
                    observed_at=observed_at,
                    reason=(
                        "hard_market_gate_recheck"
                        if machine_code in _MARKET_GATES
                        else "paper_continuous_watchdog"
                    ),
                )
            elif state.get("mode") == "blocked_structural":
                state, cleared = self._recheck_structural(
                    state,
                    authority=authority,
                    observed_at=observed_at,
                )
                if not cleared:
                    blocker = dict(state.get("blocker") or {})
                    return self._finish(
                        lease,
                        state,
                        self._result(
                            cycle_id=cycle_id,
                            observed_at=observed_at,
                            status="blocked_structural",
                            machine_code=blocker.get(
                                "machine_code"
                            ),
                            classification=STRUCTURAL,
                            control_actions=0,
                            heartbeat=health,
                            rechecked=True,
                        ),
                        authority=authority,
                    )
                # A recheck may clear the reason for a structural block, but
                # it must not also create a new start intent in this tick.
                # Persist the clearance as its own terminal observation; the
                # next independently claimed fresh heartbeat can then begin a
                # normal convergence attempt.
                return self._finish(
                    lease,
                    state,
                    self._result(
                        cycle_id=cycle_id,
                        observed_at=observed_at,
                        status="structural_cleared",
                        terminal_status="structural_cleared",
                        control_actions=0,
                        heartbeat=health,
                        rechecked=True,
                    ),
                    authority=authority,
                )
            persisted_cycle_id = str(runtime.get("cycle_id") or "")
            if (
                persisted_cycle_id
                and persisted_cycle_id != cycle_id
                and (
                    self._runtime_is_running(runtime)
                    or int(runtime.get("accepted_order_count") or 0)
                    > 0
                )
            ):
                if self._paper_continuous:
                    self._record_degradation(
                        event_id=(
                            f"{attempt_id}:previous-cycle-runtime"
                        ),
                        cycle_id=cycle_id,
                        bypassed_gate="previous_cycle_state_gate",
                        original_machine_code=(
                            "previous_cycle_paper_state_unresolved"
                        ),
                        original_reason=(
                            "authoritative Paper runtime still names the "
                            "previous cycle"
                        ),
                        alternative_action=(
                            "continue_current_cycle_full_start_flow"
                        ),
                        occurred_at=observed_at,
                    )
                else:
                    return self._structural(
                        lease,
                        state,
                        cycle_id=cycle_id,
                        observed_at=observed_at,
                        machine_code=(
                            "previous_cycle_paper_state_unresolved"
                        ),
                        heartbeat=health,
                        authority=authority,
                    )
            if self._runtime_is_running(runtime):
                try:
                    with self._attempt_deadline(started):
                        return self._adopt_or_block(
                            lease,
                            state,
                            authority=authority,
                            attempt_id=attempt_id,
                            observed_at=observed_at,
                            heartbeat=health,
                        )
                except SupervisorAttemptDeadline as exc:
                    return self._classify_before_intent(
                        lease,
                        state,
                        cycle_id=cycle_id,
                        observed_at=observed_at,
                        exc=exc,
                        heartbeat=health,
                        deadline_exceeded=True,
                        phase="runtime_adoption",
                    )
            if self._has_exposure(authority):
                if self._paper_continuous:
                    self._record_degradation(
                        event_id=f"{attempt_id}:existing-exposure",
                        cycle_id=cycle_id,
                        bypassed_gate="existing_exposure_gate",
                        original_machine_code=(
                            "existing_exposure_conflict"
                        ),
                        original_reason=(
                            "authoritative Paper snapshot contains existing "
                            "orders or positions while runtime is not proven"
                        ),
                        alternative_action=(
                            "continue_current_cycle_full_start_flow"
                        ),
                        occurred_at=observed_at,
                    )
                else:
                    return self._structural(
                        lease,
                        state,
                        cycle_id=cycle_id,
                        observed_at=observed_at,
                        machine_code="existing_exposure_conflict",
                        heartbeat=health,
                        authority=authority,
                    )
            if not self._reconciliation_exact(authority):
                if self._paper_continuous:
                    self._record_degradation(
                        event_id=f"{attempt_id}:reconciliation",
                        cycle_id=cycle_id,
                        bypassed_gate="ledger_reconciliation_gate",
                        original_machine_code=(
                            "ledger_reconciliation_drift"
                        ),
                        original_reason=(
                            "execution or accounting reconciliation is not "
                            "exact for the current Paper snapshot"
                        ),
                        alternative_action=(
                            "continue_current_cycle_full_start_flow"
                        ),
                        occurred_at=observed_at,
                    )
                else:
                    return self._structural(
                        lease,
                        state,
                        cycle_id=cycle_id,
                        observed_at=observed_at,
                        machine_code="ledger_reconciliation_drift",
                        heartbeat=health,
                        authority=authority,
                    )
            if self._paper_continuous:
                due, next_attempt_at = (
                    self._paper_continuity_watchdog_due(
                        cycle_id,
                        observed_at=observed_at,
                    )
                )
            else:
                due = self.episodes.attempt_is_due(
                    state,
                    observed_at=observed_at,
                )
                next_attempt_at = dict(
                    state.get("episode") or {}
                ).get("next_attempt_at")
            if not due:
                return self._finish(
                    lease,
                    state,
                    self._result(
                        cycle_id=cycle_id,
                        observed_at=observed_at,
                        status=(
                            "watchdog_waiting"
                            if self._paper_continuous
                            else str(
                                state.get("mode") or "backing_off"
                            )
                        ),
                        control_actions=0,
                        heartbeat=health,
                        next_attempt_at=next_attempt_at,
                    ),
                    authority=authority,
                )
            if self._paper_continuous:
                self._record_degradation(
                    event_id=f"{attempt_id}:watchdog-attempt",
                    cycle_id=cycle_id,
                    bypassed_gate="stopped_absorbing_state",
                    original_machine_code=(
                        "runtime_not_running_proven"
                    ),
                    original_reason=(
                        "the current cycle lacks sealed running_proven "
                        "authority"
                    ),
                    alternative_action=(
                        "force_fresh_full_start_flow"
                    ),
                    occurred_at=observed_at,
                )
                state = (
                    self.episodes.record_paper_continuity_watchdog_attempt(
                        state,
                        observed_at=observed_at,
                    )
                )

            if self._paper_continuous:
                try:
                    self.plane.verify_supervisor_outer_policy()
                except Exception as exc:  # noqa: BLE001 - exact typed code below.
                    if str(getattr(exc, "code", str(exc))) != (
                        "outer_strategy_policy_expired"
                    ):
                        return self._classify_before_intent(
                            lease,
                            state,
                            cycle_id=cycle_id,
                            observed_at=observed_at,
                            exc=exc,
                            heartbeat=health,
                            deadline_exceeded=False,
                            phase="outer_policy",
                        )
                    self._record_degradation(
                        event_id=f"{attempt_id}:policy-renewal",
                        cycle_id=cycle_id,
                        bypassed_gate="outer_strategy_policy_expiry_gate",
                        original_machine_code=(
                            "outer_strategy_policy_expired"
                        ),
                        original_reason=(
                            "the exact Park strategy policy has passed its "
                            "original expiry timestamp"
                        ),
                        alternative_action=(
                            "append_source_bound_paper_only_policy_renewal"
                        ),
                        occurred_at=observed_at,
                    )
                    try:
                        self.plane.renew_expired_supervisor_outer_policy()
                    except Exception as renewal_exc:  # noqa: BLE001
                        return self._classify_before_intent(
                            lease,
                            state,
                            cycle_id=cycle_id,
                            observed_at=observed_at,
                            exc=renewal_exc,
                            heartbeat=health,
                            deadline_exceeded=False,
                            phase="outer_policy",
                        )

            provider_fallback = False
            provider_degradation_event: dict[str, Any] | None = None
            try:
                attempt_readiness = (
                    current_cloud_ai_provider_readiness(
                        self.output_root,
                        verifier=self.provider_readiness_verifier,
                    )
                )
            except CloudAIProviderReadinessGateError as exc:
                if not self._paper_continuous:
                    return self._classify_before_intent(
                        lease,
                        state,
                        cycle_id=cycle_id,
                        observed_at=observed_at,
                        exc=exc,
                        heartbeat=health,
                        deadline_exceeded=False,
                        phase="provider_readiness",
                    )
                try:
                    provider_exception_ref = (
                        self._record_pre_intent_exception(
                            cycle_id=cycle_id,
                            observed_at=observed_at,
                            exc=exc,
                            phase="provider_readiness",
                            attempt_id=attempt_id,
                        )
                    )
                except PaperSupervisorExceptionEvidenceError:
                    return self._classify_before_intent(
                        lease,
                        state,
                        cycle_id=cycle_id,
                        observed_at=observed_at,
                        exc=exc,
                        heartbeat=health,
                        deadline_exceeded=False,
                        phase="provider_readiness",
                    )
                provider_fallback = True
                attempt_readiness = None
                provider_degradation_event = self._record_degradation(
                    event_id=f"{attempt_id}:provider-fallback",
                    cycle_id=cycle_id,
                    bypassed_gate="cloud_ai_provider_readiness_gate",
                    original_machine_code=str(
                        getattr(exc, "code", str(exc))
                    ),
                    original_reason=str(
                        dict(getattr(exc, "evidence", {}) or {}).get(
                            "readiness_blocker"
                        )
                        or "Cloud AI provider readiness is unavailable"
                    ),
                    alternative_action=(
                        "reuse_verified_strategy_intent_without_new_ai_call"
                    ),
                    occurred_at=observed_at,
                    exception_receipt_digest=(
                        provider_exception_ref["receipt_digest"]
                    ),
                )

            request: dict[str, Any]
            recovery_candidate = bool(
                self._paper_continuous
                and (
                    bool(plan)
                    or (
                        not plan
                        and (
                            provider_fallback
                            or continuity_recovery_code
                            in {
                                "prepared_start_market_moved",
                                "frozen_grid_preview_market_moved",
                                "outer_strategy_policy_envelope_out_of_bounds",
                                "risk_envelope_preview_out_of_bounds",
                            }
                        )
                    )
                )
            )
            if not plan or recovery_candidate:
                try:
                    with self._attempt_deadline(started):
                        plan, request = self._create_plan(
                            cycle_id,
                            lease=lease,
                            attempt_id=attempt_id,
                            observed_at=observed_at,
                            recovery_candidate=recovery_candidate,
                            recovery_machine_code=(
                                continuity_recovery_code
                            ),
                            provider_fallback=provider_fallback,
                            initial_degradation_events=(
                                [provider_degradation_event]
                                if provider_degradation_event is not None
                                else []
                            ),
                        )
                except SupervisorAttemptDeadline as exc:
                    return self._classify_before_intent(
                        lease,
                        state,
                        cycle_id=cycle_id,
                        observed_at=observed_at,
                        exc=exc,
                        heartbeat=health,
                        deadline_exceeded=True,
                        phase=str(
                            getattr(
                                exc,
                                "_paper_supervisor_phase",
                                "candidate_build",
                            )
                        ),
                    )
                except Exception as exc:  # noqa: BLE001 - exact classifier is fail-closed.
                    return self._classify_before_intent(
                        lease,
                        state,
                        cycle_id=cycle_id,
                        observed_at=observed_at,
                        exc=exc,
                        heartbeat=health,
                        deadline_exceeded=isinstance(
                            exc,
                            SupervisorAttemptDeadline,
                        ),
                        phase=str(
                            getattr(
                                exc,
                                "_paper_supervisor_phase",
                                "candidate_build",
                            )
                        ),
                    )
            else:
                # ``StartAuthoritySnapshot.active_plan`` intentionally carries
                # only the immutable identity fields.  It is not the economic
                # plan and must never be used to build a start request: doing
                # so silently drops the envelope, range, and grid geometry.
                # Re-read the full active plan and require its identity to
                # match the authority snapshot before asking the public
                # control plane to prepare anything.
                full_plan = self.plane.active_plan(cycle_id) or {}
                if not full_plan:
                    return self._structural(
                        lease,
                        state,
                        cycle_id=cycle_id,
                        observed_at=observed_at,
                        machine_code="active_plan_missing",
                        heartbeat=health,
                        authority=authority,
                    )
                if self._plan_identity(full_plan) != plan:
                    return self._structural(
                        lease,
                        state,
                        cycle_id=cycle_id,
                        observed_at=observed_at,
                        machine_code="plan_identity_conflict",
                        heartbeat=health,
                        authority=authority,
                    )
                request = self._request_from_plan(full_plan)

            if provider_degradation_event is not None:
                self._append_degradation_ref(
                    request,
                    provider_degradation_event,
                )

            if request.get("paper_continuity_provider_fallback") is True:
                provider_fallback = True
                attempt_readiness = None
            if provider_fallback:
                request["paper_continuity_provider_fallback"] = True

            if self._deadline_exceeded(started):
                return self._pre_intent_deadline(
                    lease,
                    state,
                    cycle_id=cycle_id,
                    observed_at=observed_at,
                    heartbeat=health,
                )
            try:
                with self._attempt_deadline(started):
                    prepared = self.control(
                        "prepare_start",
                        {
                            **request,
                            "supervisor_attempt_id": attempt_id,
                        },
                    )
            except SupervisorAttemptDeadline as exc:
                return self._classify_before_intent(
                    lease,
                    state,
                    cycle_id=cycle_id,
                    observed_at=observed_at,
                    exc=exc,
                    heartbeat=health,
                    deadline_exceeded=True,
                    phase="prepare_start",
                )
            except Exception as exc:  # noqa: BLE001 - exact classifier is fail-closed.
                return self._classify_before_intent(
                    lease,
                    state,
                    cycle_id=cycle_id,
                    observed_at=observed_at,
                    exc=exc,
                    heartbeat=health,
                    deadline_exceeded=isinstance(
                        exc,
                        SupervisorAttemptDeadline,
                    ),
                    phase="prepare_start",
                )
            try:
                if not isinstance(prepared, Mapping):
                    raise ValueError(
                        "execution_receipt_identity_invalid"
                    )
                preview = dict(prepared.get("preview") or {})
                confirmation = dict(
                    preview.get("manual_confirmation") or {}
                )
                intent_contract = self._intent_contract(prepared)
                prepared_start_id = str(
                    prepared["prepared_start_id"]
                )
                preview_id = str(preview["preview_id"])
                if not prepared_start_id or not preview_id:
                    raise ValueError(
                        "execution_receipt_identity_invalid"
                    )
                prepared_readiness = (
                    validate_provider_readiness_proof(
                        prepared.get("provider_readiness") or {}
                    )
                    if attempt_readiness is not None
                    else None
                )
                if (
                    attempt_readiness is not None
                    and prepared_readiness != attempt_readiness
                ):
                    raise CloudAIProviderReadinessGateError(
                        "cloud_ai_provider_readiness_unavailable",
                        readiness_blocker=(
                            "cloud_ai_provider_readiness_changed"
                        ),
                    )
                if (
                    provider_fallback
                    and prepared.get(
                        "paper_continuity_provider_fallback"
                    )
                    is not True
                ):
                    raise ValueError(
                        "execution_receipt_identity_invalid"
                    )
                if (
                    request.get(
                        "paper_continuity_allow_lower_profit_target"
                    )
                    is True
                    and prepared.get(
                        "paper_continuity_allow_lower_profit_target"
                    )
                    is not True
                ):
                    raise ValueError(
                        "execution_receipt_identity_invalid"
                    )
                if (
                    request.get(
                        "paper_continuity_dca_carry_forward"
                    )
                    is True
                    and (
                        prepared.get(
                            "paper_continuity_dca_carry_forward"
                        )
                        is not True
                        or str(preview.get("strategy_type") or "").lower()
                        != "dca"
                    )
                ):
                    raise ValueError(
                        "execution_receipt_identity_invalid"
                    )
            except Exception as exc:  # noqa: BLE001 - malformed public receipt fails closed.
                return self._classify_before_intent(
                    lease,
                    state,
                    cycle_id=cycle_id,
                    observed_at=observed_at,
                    exc=exc,
                    heartbeat=health,
                    deadline_exceeded=False,
                    phase="prepared_receipt_validation",
                )
            lease.record_pre_intent_prepare_succeeded(
                attempt_id=attempt_id,
                observed_at=observed_at,
            )
            state = self._reconcile_operational_wal(
                state,
                cycle_id=cycle_id,
            )
            if (
                confirmation.get("required") is True
                and not (
                    request.get(
                        "paper_continuity_dca_carry_forward"
                    )
                    is True
                    and str(preview.get("strategy_type") or "").lower()
                    == "dca"
                )
            ):
                return self._structural(
                    lease,
                    state,
                    cycle_id=cycle_id,
                    observed_at=observed_at,
                    machine_code="manual_risk_confirmation_required",
                    heartbeat=health,
                    preview_id=preview_id,
                    prepared_start_id=prepared_start_id,
                    authority=authority,
                )
            if self._deadline_exceeded(started):
                return self._pre_intent_deadline(
                    lease,
                    state,
                    cycle_id=cycle_id,
                    observed_at=observed_at,
                    heartbeat=health,
                    preview_id=preview_id,
                    prepared_start_id=prepared_start_id,
                )
            start_payload = {
                **request,
                "prepared_start_id": prepared_start_id,
                "expected_preview_id": preview_id,
                "supervisor_attempt_id": attempt_id,
            }
            post_authority: StartAuthoritySnapshot | None = None

            def operation() -> Mapping[str, Any]:
                nonlocal post_authority
                response = self.control("start", start_payload)
                post_authority = self._authority(
                    cycle_id,
                    heartbeat=health,
                )
                exact = self._start_response_exact(
                    response,
                    post_authority,
                    intent_contract=intent_contract,
                    preview_id=preview_id,
                    prepared_start_id=prepared_start_id,
                )
                return {
                    "result": "accepted" if exact else "unknown",
                    "machine_code": (
                        None
                        if exact
                        else "partial_execution_or_cleanup_required"
                    ),
                    "public_response_digest": _digest(response),
                }

            try:
                with self._attempt_deadline(started):
                    with production_mutation_lock(self.output_root):
                        fresh = self._authority(
                            cycle_id,
                            heartbeat=health,
                        )
                        fresh_blocker = (
                            self._pre_intent_authority_blocker(
                                fresh,
                                intent_contract=intent_contract,
                            )
                        )
                        if fresh_blocker is not None:
                            return self._structural(
                                lease,
                                state,
                                cycle_id=cycle_id,
                                observed_at=observed_at,
                                machine_code=fresh_blocker,
                                heartbeat=health,
                                preview_id=preview_id,
                                prepared_start_id=prepared_start_id,
                                authority=fresh,
                            )
                        if not provider_fallback:
                            current_cloud_ai_provider_readiness(
                                self.output_root,
                                verifier=self.provider_readiness_verifier,
                                expected_digest=(
                                    str(
                                        (prepared_readiness or {}).get(
                                            "readiness_digest"
                                        )
                                        or ""
                                    )
                                    if attempt_readiness is not None
                                    else None
                                ),
                            )
                        state, guard = (
                            self.episodes.guard_start_intent(
                                state,
                                observed_at=observed_at,
                            )
                        )
                        if guard["allowed"] is not True:
                            return self._finish(
                                lease,
                                state,
                                self._result(
                                    cycle_id=cycle_id,
                                    observed_at=observed_at,
                                    status=str(
                                        state.get("mode")
                                        or "backing_off"
                                    ),
                                    machine_code=guard.get(
                                        "machine_code"
                                    ),
                                    classification=guard.get(
                                        "classification"
                                    ),
                                    control_actions=0,
                                    heartbeat=health,
                                ),
                                authority=fresh,
                            )
                        terminal = lease.execute_start(
                            attempt_id=attempt_id,
                            preview_id=preview_id,
                            prepared_start_id=prepared_start_id,
                            plan_identity=intent_contract[
                                "plan_identity"
                            ],
                            pre_start_plan_identity=(
                                intent_contract.get(
                                    "pre_start_plan_identity"
                                )
                            ),
                            expected_order_fingerprints=(
                                intent_contract[
                                    "expected_order_fingerprints"
                                ]
                            ),
                            operation=operation,
                        )
            except SupervisorAttemptDeadline:
                unfinished = self.store.unfinished_intent(cycle_id)
                if unfinished is None:
                    return self._pre_intent_deadline(
                        lease,
                        state,
                        cycle_id=cycle_id,
                        observed_at=observed_at,
                        heartbeat=health,
                        preview_id=preview_id,
                        prepared_start_id=prepared_start_id,
                    )
                return self._recover_unfinished(
                    lease,
                    state,
                    unfinished=unfinished,
                    observed_at=observed_at,
                    heartbeat=health,
                    started=started,
                )
            except CloudAIProviderReadinessGateError as exc:
                return self._classify_before_intent(
                    lease,
                    state,
                    cycle_id=cycle_id,
                    observed_at=observed_at,
                    exc=exc,
                    heartbeat=health,
                    deadline_exceeded=False,
                    phase="pre_start_provider_readiness",
                )
            except Exception:  # noqa: BLE001 - outcome is authority, never exception prose.
                unfinished = self.store.unfinished_intent(cycle_id)
                return self._recover_unfinished(
                    lease,
                    state,
                    unfinished=unfinished or {},
                    observed_at=observed_at,
                    heartbeat=health,
                    started=started,
                )
            if str(terminal.get("result") or "") != "accepted":
                state = self.episodes.record_dangerous_outcome(
                    state,
                    machine_code=(
                        "partial_execution_or_cleanup_required"
                    ),
                    observed_at=observed_at,
                    detail={
                        "public_start_returned": True,
                        "exact_n_of_n": False,
                    },
                )
                return self._finish(
                    lease,
                    state,
                    self._result(
                        cycle_id=cycle_id,
                        observed_at=observed_at,
                        status="blocked_structural",
                        machine_code=(
                            "partial_execution_or_cleanup_required"
                        ),
                        classification=STRUCTURAL,
                        control_actions=1,
                        heartbeat=health,
                        attempt_id=attempt_id,
                        preview_id=preview_id,
                        prepared_start_id=prepared_start_id,
                    ),
                    authority=post_authority,
                )
            return self._finish(
                lease,
                state,
                self._result(
                    cycle_id=cycle_id,
                    observed_at=observed_at,
                    status="executed",
                    terminal_status="executed",
                    control_actions=1,
                    heartbeat=health,
                    attempt_id=attempt_id,
                    preview_id=preview_id,
                    prepared_start_id=prepared_start_id,
                    expected_order_count=intent_contract[
                        "expected_order_count"
                    ],
                ),
                authority=post_authority,
            )

    @staticmethod
    def _source_tick_identity(
        cycle_id: str,
        *,
        health: Mapping[str, Any],
    ) -> tuple[str, str]:
        recorded_at = str(health.get("recorded_at") or "")
        if health.get("status") == "ready" and recorded_at:
            digest = hashlib.sha256(
                f"{cycle_id}|{recorded_at}".encode("utf-8")
            ).hexdigest()
            return f"supervisor-tick-{digest}", "fresh"
        return (
            f"supervisor-tick-ambiguous-{uuid.uuid4().hex}",
            "ambiguous",
        )

    @property
    def _paper_continuous(self) -> bool:
        return self.execution_profile == PAPER_CONTINUOUS

    def _new_cycle_state(
        self,
        cycle_id: str,
        *,
        observed_at: str,
    ) -> dict[str, Any]:
        """Start each cycle clean and durably attest that no latch survived."""

        state = self.episodes.new_cycle(
            cycle_id,
            observed_at=observed_at,
        )
        if not self._paper_continuous:
            return state
        event_id = f"paper-continuity-cycle-reset:{cycle_id}"
        existing = next(
            (
                row
                for row in self.degradation_events.events(cycle_id)
                if row.get("event_id") == event_id
            ),
            None,
        )
        if existing is None:
            self._record_degradation(
                event_id=event_id,
                cycle_id=cycle_id,
                bypassed_gate="prior_cycle_blocker_latch",
                original_machine_code="cycle_boundary_reset",
                original_reason=(
                    "a new Paper cycle must not inherit retry, probe, "
                    "exhaustion, or blocker state"
                ),
                alternative_action=(
                    "initialize_clean_cycle_convergence_state"
                ),
                occurred_at=observed_at,
            )
        return state

    def _record_degradation(
        self,
        *,
        event_id: str,
        cycle_id: str,
        bypassed_gate: str,
        original_machine_code: str,
        original_reason: str,
        alternative_action: str,
        occurred_at: str,
        exception_receipt_digest: str | None = None,
    ) -> dict[str, Any]:
        if not self._paper_continuous:
            raise ValueError("paper_degradation_requires_continuous_profile")
        return self.degradation_events.record(
            event_id=event_id,
            cycle_id=cycle_id,
            execution_profile=self.execution_profile,
            bypassed_gate=bypassed_gate,
            original_machine_code=original_machine_code,
            original_reason=original_reason,
            alternative_action=alternative_action,
            occurred_at=occurred_at,
            exception_receipt_digest=exception_receipt_digest,
        )

    @staticmethod
    def _append_degradation_ref(
        payload: dict[str, Any],
        event: Mapping[str, Any],
    ) -> None:
        ref = {
            "event_id": str(event.get("event_id") or ""),
            "event_digest": str(event.get("event_digest") or ""),
        }
        if not all(ref.values()):
            raise ValueError("paper_degradation_event_store_corrupt")
        refs = [
            dict(row)
            for row in payload.get(
                "paper_continuity_degradation_event_refs"
            )
            or []
            if isinstance(row, Mapping)
        ]
        if ref not in refs:
            refs.append(ref)
        payload["paper_continuity_degradation_event_refs"] = refs

    def _paper_continuity_watchdog_due(
        self,
        cycle_id: str,
        *,
        observed_at: str,
    ) -> tuple[bool, str | None]:
        """Use the fsynced degradation journal as the retry cadence authority."""

        now = _utc_timestamp(observed_at)
        attempt_actions = {
            "force_fresh_full_start_flow",
            "stop_nonproven_runtime_before_fresh_start",
        }
        attempts = [
            row
            for row in self.degradation_events.events(cycle_id)
            if row.get("alternative_action") in attempt_actions
        ]
        if not attempts:
            return True, None
        latest = max(
            (_utc_timestamp(str(row["occurred_at"])) for row in attempts),
        )
        next_attempt = latest + timedelta(
            seconds=PAPER_CONTINUITY_WATCHDOG_SECONDS
        )
        return now >= next_attempt, next_attempt.isoformat()

    @staticmethod
    def _claim_heartbeat(claim: Mapping[str, Any]) -> dict[str, Any]:
        """Use durable claim facts when finishing a crashed tick.

        A later live tick must never be written into the observation that
        closes an older claim.  A legacy claim without its original heartbeat
        timestamp is deliberately unknown rather than reconstructed.
        """

        recorded_at = claim.get("heartbeat_recorded_at")
        digest = str(claim.get("heartbeat_digest") or "")
        trust = str(claim.get("trust") or "")
        return {
            "status": (
                "ready"
                if trust == "fresh" and recorded_at and digest
                else "unknown"
            ),
            "machine_code": None,
            "cycle_id": None,
            "recorded_at": recorded_at,
            "age_seconds": None,
            "complete": False,
            "heartbeat_digest": digest or _digest({}),
            "recovered_from_tick_claim": True,
        }

    @staticmethod
    def _result_from_observation(
        observation: Mapping[str, Any],
    ) -> dict[str, Any]:
        payload = dict(observation.get("payload") or {})
        for key in (
            "episode",
            "episode_snapshot",
            "episode_events_delta",
            "episode_state",
            "wal_anchor",
            "tick_claim",
            "running_evidence",
        ):
            payload.pop(key, None)
        return payload

    def _recover_unfinished_tick(
        self,
        lease,
        claim: Mapping[str, Any],
        *,
        cycle_id: str,
        observed_at: str,
        heartbeat: Mapping[str, Any],
        started: float,
    ) -> dict[str, Any]:
        """Finish one claimed tick without starting a second attempt."""

        state = self.store.episode_state(cycle_id)
        if state is None:
            state = self._new_cycle_state(
                cycle_id,
                observed_at=str(claim.get("claimed_at") or observed_at),
            )
        state = self._reconcile_operational_wal(
            state,
            cycle_id=cycle_id,
        )
        state, _ = self._reconcile_terminal_outcomes(
            state,
            cycle_id=cycle_id,
        )
        unfinished = self.store.unfinished_intent(cycle_id)
        if unfinished is not None:
            return self._recover_unfinished(
                lease,
                state,
                unfinished=unfinished,
                observed_at=observed_at,
                heartbeat=heartbeat,
                started=started,
            )
        pending_pre = self.store.unfinished_pre_intent(cycle_id)
        if pending_pre is not None:
            if isinstance(
                pending_pre.get("candidate_identity"),
                Mapping,
            ):
                return self._recover_crashed_candidate_rejection(
                    lease,
                    state,
                    cycle_id=cycle_id,
                    unfinished=pending_pre,
                    observed_at=observed_at,
                    heartbeat=heartbeat,
                )
            lease.abandon_pre_intent(
                attempt_id=str(pending_pre["attempt_id"]),
                observed_at=observed_at,
            )
        state = self._reconcile_operational_wal(
            state,
            cycle_id=cycle_id,
        )
        projection = self.store.current_state(cycle_id)
        tick_key = str(claim.get("source_tick_key") or "")
        attempts = [
            dict(row)
            for row in projection.get("attempts") or []
            if isinstance(row, Mapping)
            and str(
                row.get("recovery_source_tick_key")
                or row.get("source_tick_key")
                or ""
            )
            == tick_key
        ]
        pre_attempts = [
            dict(row)
            for row in projection.get("pre_intent_attempts") or []
            if isinstance(row, Mapping)
            and str(
                row.get("recovery_source_tick_key")
                or row.get("source_tick_key")
                or ""
            )
            == tick_key
        ]
        if len(attempts) > 1 or len(pre_attempts) > 1:
            raise ValueError("attempt_store_corrupt")
        if attempts:
            attempt = attempts[0]
            terminal = str(attempt.get("terminal_result") or "")
            if terminal in {"accepted", "executed"}:
                authority = self._authority(
                    cycle_id,
                    heartbeat=heartbeat,
                )
                return self._finish(
                    lease,
                    state,
                    self._result(
                        cycle_id=cycle_id,
                        observed_at=observed_at,
                        status=(
                            "executed"
                            if terminal == "accepted"
                            else "healthy"
                        ),
                        terminal_status=(
                            "executed"
                            if terminal == "accepted"
                            else "adopted_existing"
                        ),
                        control_actions=0,
                        heartbeat=heartbeat,
                        recovered_attempt_id=attempt.get(
                            "attempt_id"
                        ),
                        attempt_id=attempt.get("attempt_id"),
                        preview_id=attempt.get("preview_id"),
                        prepared_start_id=attempt.get(
                            "prepared_start_id"
                        ),
                        expected_order_count=attempt.get(
                            "expected_order_count"
                        ),
                    ),
                    authority=authority,
                )
            if terminal == "clean_rejection":
                classified = classify_blocker(
                    control_code=attempt.get(
                        "terminal_machine_code"
                    )
                )
                return self._finish(
                    lease,
                    state,
                    self._result(
                        cycle_id=cycle_id,
                        observed_at=observed_at,
                        status=str(
                            state.get("mode") or "backing_off"
                        ),
                        machine_code=classified["machine_code"],
                        classification=classified["classification"],
                        control_actions=0,
                        heartbeat=heartbeat,
                        recovered_attempt_id=attempt.get("attempt_id"),
                    ),
                )
            return self._structural(
                lease,
                state,
                cycle_id=cycle_id,
                observed_at=observed_at,
                machine_code=(
                    str(attempt.get("terminal_machine_code") or "")
                    if terminal == "unknown"
                    else "control_outcome_unknown"
                )
                or "control_outcome_unknown",
                heartbeat=heartbeat,
            )
        if pre_attempts:
            pre = pre_attempts[0]
            classification = pre.get("terminal_classification")
            machine_code = pre.get("terminal_machine_code")
            status = (
                "blocked_structural"
                if classification == STRUCTURAL
                else str(state.get("mode") or "backing_off")
            )
            return self._finish(
                lease,
                state,
                self._result(
                    cycle_id=cycle_id,
                    observed_at=observed_at,
                    status=status,
                    machine_code=machine_code,
                    classification=classification,
                    control_actions=0,
                    heartbeat=heartbeat,
                    recovered_attempt_id=pre.get("attempt_id"),
                ),
            )
        return self._finish(
            lease,
            state,
            self._result(
                cycle_id=cycle_id,
                observed_at=observed_at,
                status="recovered_no_action",
                control_actions=0,
                heartbeat=heartbeat,
            ),
        )

    def _create_plan(
        self,
        cycle_id: str,
        *,
        lease,
        attempt_id: str,
        observed_at: str,
        recovery_candidate: bool = False,
        recovery_machine_code: str | None = None,
        provider_fallback: bool = False,
        initial_degradation_events: list[Mapping[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        with self._pre_intent_exception_phase("outer_policy"):
            self.plane.verify_supervisor_outer_policy()
        recovery = bool(recovery_candidate)
        if self._paper_continuous and not recovery:
            adopted, invalidation_reason = (
                self._adopt_verified_waiting_plan(
                    cycle_id,
                    lease=lease,
                    attempt_id=attempt_id,
                    observed_at=observed_at,
                )
            )
            if adopted is not None:
                return adopted
            # Missing or stale staging is never permission to call AI at the
            # boundary.  Paper-continuous records the exact reason and uses
            # the existing current-market + authoritative-equity builder.
            recovery = True
            recovery_machine_code = invalidation_reason
        degradation_evidence: dict[str, Any] = {}
        for event in initial_degradation_events or []:
            self._append_degradation_ref(degradation_evidence, event)
        if recovery:
            recovery_event = self._record_recovery_candidate_degradation(
                cycle_id=cycle_id,
                attempt_id=attempt_id,
                observed_at=observed_at,
                machine_code=(
                    recovery_machine_code
                    or (
                        "cloud_ai_provider_readiness_unavailable"
                        if provider_fallback
                        else "runtime_not_running_proven"
                    )
                ),
            )
            self._append_degradation_ref(
                degradation_evidence,
                recovery_event,
            )
        evaluation_phase = (
            "provider_fallback"
            if provider_fallback
            else "candidate_build"
            if recovery
            else "primary_ai"
        )
        try:
            with self._pre_intent_exception_phase(evaluation_phase):
                evaluation = self.control(
                    (
                        "paper_continuity_candidate"
                        if recovery
                        else "refresh_recommendation"
                    ),
                    {
                        **(
                            {"paper_continuity_provider_fallback": True}
                            if provider_fallback
                            else {}
                        ),
                        **(
                            {"supervisor_attempt_id": attempt_id}
                            if recovery
                            else {}
                        ),
                        **degradation_evidence,
                    },
                )
        except Exception as exc:  # noqa: BLE001 - typed provider fallback only.
            code = str(getattr(exc, "code", str(exc)))
            provider_codes = set(RECOVERABLE_PROVIDER_CODES) | {
                "cloud_ai_provider_readiness_unavailable",
                "cloud_ai_provider_readiness_invalid",
            }
            if (
                not self._paper_continuous
                or recovery
                or code not in provider_codes
            ):
                raise
            provider_exception_ref = self._record_pre_intent_exception(
                cycle_id=cycle_id,
                observed_at=observed_at,
                exc=exc,
                phase="primary_ai",
                attempt_id=attempt_id,
            )
            provider_event = self._record_degradation(
                event_id=f"{attempt_id}:provider-call-fallback",
                cycle_id=cycle_id,
                bypassed_gate="ai_recommendation_provider_gate",
                original_machine_code=code,
                original_reason=(
                    "the AI provider could not produce a new-cycle judgment"
                ),
                alternative_action=(
                    "reuse_verified_strategy_intent_without_new_ai_call"
                ),
                occurred_at=observed_at,
                exception_receipt_digest=(
                    provider_exception_ref["receipt_digest"]
                ),
            )
            self._append_degradation_ref(
                degradation_evidence,
                provider_event,
            )
            recovery = True
            provider_fallback = True
            recovery_event = self._record_recovery_candidate_degradation(
                cycle_id=cycle_id,
                attempt_id=attempt_id,
                observed_at=observed_at,
                machine_code=code,
            )
            self._append_degradation_ref(
                degradation_evidence,
                recovery_event,
            )
            with self._pre_intent_exception_phase("provider_fallback"):
                evaluation = self.control(
                    "paper_continuity_candidate",
                    {
                        "paper_continuity_provider_fallback": True,
                        "supervisor_attempt_id": attempt_id,
                        **degradation_evidence,
                    },
                )
        with self._pre_intent_exception_phase("candidate_build"):
            recommendation = dict(
                evaluation.get("recommendation") or {}
            )
            proposal = dict(evaluation.get("proposal") or {})
            preview = dict(evaluation.get("preview") or {})
            recovery_detail = dict(evaluation.get("recovery") or {})
        if recovery_detail.get("risk_repriced") is True:
            risk_event = self._record_degradation(
                event_id=f"{attempt_id}:risk-repriced",
                cycle_id=cycle_id,
                bypassed_gate="outer_strategy_policy_envelope_gate",
                original_machine_code=(
                    recovery_machine_code
                    or "outer_strategy_policy_envelope_out_of_bounds"
                ),
                original_reason=(
                    "the inherited per-grid amount exceeds the current "
                    "authoritative Paper account or Park boundary"
                ),
                alternative_action=(
                    "cap_notional_inside_exact_outer_policy_boundary"
                ),
                occurred_at=observed_at,
            )
            self._append_degradation_ref(
                degradation_evidence,
                risk_event,
            )
        lower_profit_target = (
            dict(preview.get("risk") or {}).get("profit_target_met")
            is False
            or dict(preview.get("grid") or {}).get("profit_target_met")
            is False
        )
        if lower_profit_target:
            profit_event = self._record_degradation(
                event_id=f"{attempt_id}:profit-target-degraded",
                cycle_id=cycle_id,
                bypassed_gate="grid_profit_target_gate",
                original_machine_code="grid_profit_target_not_met",
                original_reason=(
                    "the boundary-capped Grid cannot meet the configured "
                    "per-grid profit objective"
                ),
                alternative_action=(
                    "accept_lower_paper_profit_target_and_continue"
                ),
                occurred_at=observed_at,
            )
            self._append_degradation_ref(
                degradation_evidence,
                profit_event,
            )
        with self._pre_intent_exception_phase("candidate_build"):
            candidate_identity = self.plane.supervisor_candidate_identity(
                cycle_id,
                proposal=proposal,
                preview=preview,
                supervisor_attempt_id=attempt_id,
            )
        lease.record_pre_intent_candidate_observed(
            attempt_id=attempt_id,
            observed_at=observed_at,
            candidate_identity=candidate_identity,
        )
        rejected_candidates = self._cleared_rejected_candidates(
            cycle_id
        )
        if any(
            self._candidate_identity_reused(
                candidate_identity,
                rejected_candidate,
            )
            for rejected_candidate in rejected_candidates
        ):
            raise ValueError("plan_identity_conflict")
        with self._pre_intent_exception_phase("envelope_authorization"):
            envelope = self.plane.authorize_supervisor_ai_envelope(
                cycle_id,
                proposal=proposal,
                preview=preview,
                supervisor_attempt_id=attempt_id,
            )
        envelope_id = str(
            envelope.get("envelope_authorization_id") or ""
        )
        if not envelope_id:
            raise ValueError("risk_envelope_authorization_invalid")
        strategy_type = str(
            recommendation.get("strategy_type")
            or proposal.get("strategy_type")
            or "grid"
        ).lower()
        if strategy_type != "dca":
            with self._pre_intent_exception_phase("plan_lock"):
                self.plane.lock_production_plan(
                    cycle_id,
                    selected_proposal_id=str(
                        proposal.get("proposal_id") or ""
                    ),
                    cycle_risk_envelope_id=envelope_id,
                    now=observed_at,
                )
        plan = self.plane.active_plan(cycle_id) or {}
        # Once a Grid proposal is locked, every start path must carry its full
        # frozen execution shape.  Sending only direction/style here caused
        # prepare_start to recalculate a second grid/account reality after the
        # envelope had already authorized the first one.
        request = (
            self._request_from_plan(plan)
            if plan
            else {
                "direction": recommendation.get("direction")
                or proposal.get("direction"),
                "style": recommendation.get("style")
                or proposal.get("style"),
                "strategy_type": strategy_type,
                "cycle_risk_envelope_id": envelope_id,
            }
        )
        if preview.get("start_facts_digest") is not None:
            request["start_facts_digest"] = str(
                preview["start_facts_digest"]
            )
        if provider_fallback:
            request["paper_continuity_provider_fallback"] = True
        if lower_profit_target:
            request["paper_continuity_allow_lower_profit_target"] = True
        if strategy_type == "dca":
            dca = dict(proposal.get("dca") or {})
            risk = dict(preview.get("risk") or {})
            request["dca"] = {
                key: dca.get(key)
                for key in (
                    "entry_levels",
                    "target_price",
                    "stop_price",
                    "notional_per_addition",
                    "max_additions",
                    "loop_enabled",
                )
            }
            request["risk_budget"] = {
                "leverage": risk.get("selected_leverage")
            }
            if recovery:
                dca_event = self._record_degradation(
                    event_id=f"{attempt_id}:dca-confirmation-degraded",
                    cycle_id=cycle_id,
                    bypassed_gate="dca_manual_risk_confirmation_gate",
                    original_machine_code=(
                        "manual_risk_confirmation_required"
                    ),
                    original_reason=(
                        "a fresh DCA preview normally requires an attended "
                        "Paper risk acknowledgement"
                    ),
                    alternative_action=(
                        "reuse_verified_dca_intent_inside_exact_outer_policy"
                    ),
                    occurred_at=observed_at,
                )
                self._append_degradation_ref(
                    degradation_evidence,
                    dca_event,
                )
                request["paper_continuity_dca_carry_forward"] = True
        elif not plan:
            raise ValueError("active_plan_missing")
        if (
            self._paper_continuous
            and str(recovery_machine_code or "").startswith(
                "next_cycle_verified_plan_"
            )
        ):
            request["boundary_ai_provider_calls"] = 0
            request["verified_waiting_validation"] = str(
                recovery_machine_code
            )
            request["deterministic_rebuild_used"] = True
        if degradation_evidence:
            request.update(degradation_evidence)
        return plan, request

    def _adopt_verified_waiting_plan(
        self,
        cycle_id: str,
        *,
        lease,
        attempt_id: str,
        observed_at: str,
    ) -> tuple[
        tuple[dict[str, Any], dict[str, Any]] | None,
        str,
    ]:
        """Adopt one exact staged candidate or return a typed fallback reason."""

        artifact: dict[str, Any] | None = None
        try:
            artifact = self.next_cycle_plans.load(cycle_id)
        except NextCyclePlanError as exc:
            reason = str(exc)
            self.next_cycle_plans.record_boundary_event(
                target_cycle_id=cycle_id,
                artifact_digest=None,
                outcome="invalidated",
                reason=reason,
                observed_at=observed_at,
                current_start_facts_digest=None,
                boundary_ai_provider_calls=0,
                deterministic_rebuild_used=True,
            )
            return None, reason
        if artifact is None:
            reason = "next_cycle_verified_plan_missing"
            self.next_cycle_plans.record_boundary_event(
                target_cycle_id=cycle_id,
                artifact_digest=None,
                outcome="missing",
                reason=reason,
                observed_at=observed_at,
                current_start_facts_digest=None,
                boundary_ai_provider_calls=0,
                deterministic_rebuild_used=True,
            )
            return None, reason

        validation = self.control(
            "validate_verified_waiting_plan",
            {
                "start_facts_digest": artifact[
                    "start_facts_digest"
                ],
            },
        )
        if (
            not isinstance(validation, Mapping)
            or int(validation.get("control_actions_executed", -1)) != 0
            or int(validation.get("orders_created", -1)) != 0
            or int(validation.get("plans_activated", -1)) != 0
            or int(validation.get("prepared_starts_created", -1)) != 0
        ):
            raise ValueError("execution_receipt_identity_invalid")
        current_facts = dict(
            validation.get("current_start_facts") or {}
        )
        try:
            bound_facts = self.plane.start_facts.require(
                cycle_id,
                str(artifact["start_facts_digest"]),
            )
            status = staged_facts_status(
                artifact,
                bound_facts,
                current_facts,
                cycle_id=cycle_id,
                observed_at=observed_at,
            )
        except (NextCyclePlanError, ValueError) as exc:
            status = {"valid": False, "reason": str(exc)}
        current_digest = str(
            current_facts.get("start_facts_digest") or ""
        ) or None
        if status.get("valid") is not True:
            reason = str(
                status.get("reason")
                or "next_cycle_verified_plan_facts_stale"
            )
            self.next_cycle_plans.record_boundary_event(
                target_cycle_id=cycle_id,
                artifact_digest=artifact["artifact_digest"],
                outcome="invalidated",
                reason=reason,
                observed_at=observed_at,
                current_start_facts_digest=current_digest,
                boundary_ai_provider_calls=0,
                deterministic_rebuild_used=True,
            )
            return None, reason

        proposal_id = str(artifact["proposal"]["proposal_id"])
        proposals = [
            dict(row)
            for row in self.plane.proposals(cycle_id)
            if str(row.get("proposal_id") or "") == proposal_id
        ]
        envelope = self.plane.risk_envelopes.envelope(
            cycle_id,
            str(
                artifact["envelope"][
                    "envelope_authorization_id"
                ]
            ),
        )
        if (
            len(proposals) != 1
            or not isinstance(envelope, Mapping)
            or str(envelope.get("authorization_digest") or "")
            != str(artifact["envelope"]["authorization_digest"])
        ):
            reason = "next_cycle_verified_plan_identity_conflict"
            self.next_cycle_plans.record_boundary_event(
                target_cycle_id=cycle_id,
                artifact_digest=artifact["artifact_digest"],
                outcome="invalidated",
                reason=reason,
                observed_at=observed_at,
                current_start_facts_digest=current_digest,
                boundary_ai_provider_calls=0,
                deterministic_rebuild_used=True,
            )
            return None, reason

        candidate_identity = dict(artifact["candidate"])
        candidate_identity["supervisor_attempt_id"] = attempt_id
        lease.record_pre_intent_candidate_observed(
            attempt_id=attempt_id,
            observed_at=observed_at,
            candidate_identity=candidate_identity,
        )
        rejected_candidates = self._cleared_rejected_candidates(
            cycle_id
        )
        if any(
            self._candidate_identity_reused(
                candidate_identity,
                rejected_candidate,
            )
            for rejected_candidate in rejected_candidates
        ):
            raise ValueError("plan_identity_conflict")

        projected_plan = dict(artifact["plan"]["projected"])
        strategy_type = str(
            artifact["plan"]["strategy_type"]
        ).lower()
        if strategy_type == "dca":
            plan: dict[str, Any] = {}
            request = self._request_from_plan(projected_plan)
        else:
            with self._pre_intent_exception_phase("plan_lock"):
                plan = self.plane.lock_production_plan(
                    cycle_id,
                    selected_proposal_id=proposal_id,
                    cycle_risk_envelope_id=str(
                        artifact["envelope"][
                            "envelope_authorization_id"
                        ]
                    ),
                    takeover_from_strategy_plan_id=(
                        str(
                            projected_plan.get(
                                "takeover_from_strategy_plan_id"
                            )
                            or ""
                        )
                        or None
                    ),
                    now=observed_at,
                )
            if (
                plan_content_digest(plan)
                != artifact["plan"]["content_digest"]
            ):
                raise ValueError("plan_identity_conflict")
            request = self._request_from_plan(plan)

        request["boundary_ai_provider_calls"] = 0
        request["verified_waiting_artifact_digest"] = artifact[
            "artifact_digest"
        ]
        request["verified_waiting_validation"] = "adopted"
        request["deterministic_rebuild_used"] = False
        if strategy_type == "dca":
            dca_event = self._record_degradation(
                event_id=f"{attempt_id}:dca-confirmation-degraded",
                cycle_id=cycle_id,
                bypassed_gate="dca_manual_risk_confirmation_gate",
                original_machine_code=(
                    "manual_risk_confirmation_required"
                ),
                original_reason=(
                    "a pre-generated DCA preview normally requires an "
                    "attended Paper risk acknowledgement"
                ),
                alternative_action=(
                    "reuse_verified_dca_intent_inside_exact_outer_policy"
                ),
                occurred_at=observed_at,
            )
            self._append_degradation_ref(request, dca_event)
            request["paper_continuity_dca_carry_forward"] = True
        grid = dict(projected_plan.get("grid") or {})
        risk = dict(projected_plan.get("risk_budget") or {})
        if (
            strategy_type == "grid"
            and (
                grid.get("profit_target_met") is False
                or risk.get("profit_target_met") is False
            )
        ):
            profit_event = self._record_degradation(
                event_id=f"{attempt_id}:profit-target-degraded",
                cycle_id=cycle_id,
                bypassed_gate="grid_profit_target_gate",
                original_machine_code="grid_profit_target_not_met",
                original_reason=(
                    "the boundary-capped Grid cannot meet the configured "
                    "per-grid profit objective"
                ),
                alternative_action=(
                    "accept_lower_paper_profit_target_and_continue"
                ),
                occurred_at=observed_at,
            )
            self._append_degradation_ref(request, profit_event)
            request["paper_continuity_allow_lower_profit_target"] = True
        self.next_cycle_plans.record_boundary_event(
            target_cycle_id=cycle_id,
            artifact_digest=artifact["artifact_digest"],
            outcome="adopted",
            reason="verified_waiting_valid",
            observed_at=observed_at,
            current_start_facts_digest=current_digest,
            boundary_ai_provider_calls=0,
            deterministic_rebuild_used=False,
        )
        return (plan, request), "verified_waiting_valid"

    def _record_recovery_candidate_degradation(
        self,
        *,
        cycle_id: str,
        attempt_id: str,
        observed_at: str,
        machine_code: str,
    ) -> dict[str, Any]:
        if not self._paper_continuous:
            raise ValueError("paper_degradation_requires_continuous_profile")
        return self._record_degradation(
            event_id=f"{attempt_id}:recenter-and-reprice:{machine_code}",
            cycle_id=cycle_id,
            bypassed_gate="stale_strategy_candidate_gate",
            original_machine_code=str(machine_code),
            original_reason=(
                "the prior candidate cannot prove a start against current "
                "market and Paper account facts"
            ),
            alternative_action=(
                "rebuild_candidate_from_current_market_and_"
                "authoritative_paper_equity"
            ),
            occurred_at=observed_at,
        )

    def _recover_crashed_candidate_rejection(
        self,
        lease,
        state: dict[str, Any],
        *,
        cycle_id: str,
        unfinished: Mapping[str, Any],
        observed_at: str,
        heartbeat: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Recover the receipt-to-WAL crash window without another AI call."""

        attempt_id = str(unfinished.get("attempt_id") or "")
        machine_code = "attempt_store_corrupt"
        evidence: dict[str, Any] | None = None
        exception_ref: dict[str, str] | None = None
        try:
            risk_envelopes = self.plane.risk_envelopes
            load_for_attempt = getattr(
                risk_envelopes,
                "outer_policy_rejection_for_attempt",
            )
            rejection = dict(
                load_for_attempt(
                    cycle_id=cycle_id,
                    supervisor_attempt_id=attempt_id,
                )
            )
            if (
                dict(rejection.get("candidate") or {})
                != dict(unfinished["candidate_identity"])
                or rejection.get("machine_code")
                != "outer_strategy_policy_envelope_out_of_bounds"
            ):
                raise ValueError("attempt_store_corrupt")
            evidence = {
                "rejection_id": str(rejection["rejection_id"]),
                "rejection_digest": str(
                    rejection["rejection_digest"]
                ),
            }
            machine_code = (
                "outer_strategy_policy_envelope_out_of_bounds"
            )
        except Exception as exc:  # noqa: BLE001 - missing or ambiguous receipt fails closed.
            try:
                exception_ref = self._record_pre_intent_exception(
                    cycle_id=cycle_id,
                    observed_at=observed_at,
                    exc=exc,
                    phase="envelope_authorization",
                    attempt_id=attempt_id,
                )
            except PaperSupervisorExceptionEvidenceError:
                exception_ref = None
            evidence = None
            machine_code = "attempt_store_corrupt"
        try:
            lease.recover_pre_intent_finished(
                attempt_id=attempt_id,
                machine_code=machine_code,
                observed_at=observed_at,
                evidence=evidence,
                exception_receipt=exception_ref,
            )
            state = self._reconcile_operational_wal(
                state,
                cycle_id=cycle_id,
            )
        except Exception:  # noqa: BLE001 - a torn recovery remains fail closed.
            return self._result(
                cycle_id=cycle_id,
                observed_at=observed_at,
                status="blocked_structural",
                machine_code="attempt_store_corrupt",
                classification=STRUCTURAL,
                control_actions=0,
                heartbeat=heartbeat,
                **(
                    {"exception_receipt": exception_ref}
                    if exception_ref is not None
                    else {}
                ),
            )
        return self._finish(
            lease,
            state,
            self._result(
                cycle_id=cycle_id,
                observed_at=observed_at,
                status="blocked_structural",
                machine_code=machine_code,
                classification=STRUCTURAL,
                control_actions=0,
                heartbeat=heartbeat,
                recovered_attempt_id=attempt_id,
                **(
                    {"exception_receipt": exception_ref}
                    if exception_ref is not None
                    else {}
                ),
            ),
        )

    def _cleared_rejected_candidates(
        self,
        cycle_id: str,
    ) -> list[dict[str, Any]]:
        state = self.store.episode_state(cycle_id) or {}
        risk_envelopes = getattr(self.plane, "risk_envelopes", None)
        loader = getattr(
            risk_envelopes,
            "outer_policy_rejection",
            None,
        )
        rejected: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for event in list(state.get("events") or []):
            if (
                not isinstance(event, Mapping)
                or event.get("event_type")
                != "structural_blocker_cleared"
                or event.get("machine_code")
                != "outer_strategy_policy_envelope_out_of_bounds"
            ):
                continue
            detail = event.get("detail")
            proof = (
                dict(detail.get("evidence") or {})
                if isinstance(detail, Mapping)
                else {}
            )
            rejection_id = str(proof.get("rejection_id") or "")
            rejection_digest = str(
                proof.get("rejection_digest") or ""
            )
            if not rejection_id or not rejection_digest or not callable(loader):
                continue
            identity = (rejection_id, rejection_digest)
            if identity in seen:
                continue
            rejection = dict(
                loader(
                    cycle_id=cycle_id,
                    rejection_id=rejection_id,
                    rejection_digest=rejection_digest,
                )
            )
            rejected.append(dict(rejection.get("candidate") or {}))
            seen.add(identity)
        return rejected

    @staticmethod
    def _candidate_identity_reused(
        candidate: Mapping[str, Any],
        rejected: Mapping[str, Any],
    ) -> bool:
        for field in (
            "proposal_id",
            "proposal_digest",
            "preview_id",
            "preview_digest",
            "facts_digest",
            "confirmation_digest",
        ):
            current = candidate.get(field)
            prior = rejected.get(field)
            if current not in {None, ""} and current == prior:
                return True
        return False

    @staticmethod
    def _request_from_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
        request = {
            "direction": plan.get("direction"),
            "style": plan.get("style"),
            "strategy_type": plan.get("strategy_type") or "grid",
            "cycle_risk_envelope_id": plan.get(
                "cycle_risk_envelope_id"
            ),
        }
        if plan.get("start_facts_digest") is not None:
            request["start_facts_digest"] = str(
                plan["start_facts_digest"]
            )
        if str(request["strategy_type"]).lower() == "dca":
            dca = dict(plan.get("dca") or {})
            risk = dict(plan.get("risk_budget") or {})
            request["dca"] = {
                "entry_levels": [
                    row.get("price")
                    for row in dca.get("entries") or []
                    if isinstance(row, Mapping)
                ],
                "target_price": dca.get("target_price"),
                "stop_price": dca.get("stop_price"),
                "notional_per_addition": dca.get(
                    "notional_per_addition"
                ),
                "max_additions": dca.get("max_additions"),
                "loop_enabled": dca.get("loop_enabled"),
            }
            request["risk_budget"] = {
                "leverage": (
                    risk.get("leverage")
                    or risk.get("selected_leverage")
                )
            }
        else:
            # Starting an already-selected Grid plan must refresh the trusted
            # market facts without silently recalculating its economic
            # geometry.  This keeps the immutable cycle envelope exact while
            # still allowing each Supervisor attempt to receive a new
            # preview/prepared identity.
            grid = dict(plan.get("grid") or {})
            request["range"] = {
                key: plan.get("range", {}).get(key)
                for key in (
                    "low",
                    "high",
                    "scope",
                    "split_price",
                    "source_envelope",
                )
                if isinstance(plan.get("range"), Mapping)
                and plan.get("range", {}).get(key) is not None
            }
            request["grid"] = {
                key: grid.get(key)
                for key in (
                    "count",
                    "mode",
                    "notional_per_grid",
                    "out_of_range",
                )
                if grid.get(key) is not None
            }
            # Preserve the plan's current per-grid amount instead of
            # re-sizing from a moving account balance during a retry.
            if grid.get("notional_per_grid") is not None:
                request["grid"]["notional_mode"] = "manual"
            if grid.get("leverage") is not None:
                request["risk_budget"] = {"leverage": grid["leverage"]}
        return request

    def _authority(
        self,
        cycle_id: str,
        *,
        heartbeat: Mapping[str, Any] | None = None,
    ) -> StartAuthoritySnapshot:
        with production_mutation_lock(self.output_root):
            plan = self.plane.active_plan(cycle_id) or {}
            runtime = self.plane.persisted_runtime_state()
            snapshot = self.execution.snapshot(cycle_id)
            reconciliation = self.execution.reconcile(cycle_id)
            execution_ok = (
                str(reconciliation.get("status") or "") == "ok"
                and not list(reconciliation.get("issues") or [])
            )
            accounting = str(self.accounting_reconciliation() or "")
            plan_identity = self._plan_identity(plan)
            authorized = (
                self._authoritative_command_identities(
                    cycle_id=cycle_id,
                    plan=plan_identity,
                )
                if plan_identity
                else {}
            )
            position_identities = open_position_identities(snapshot)
            running_evidence = self._running_evidence_for_snapshot(
                cycle_id=cycle_id,
                plan=plan,
                runtime=runtime,
                snapshot=snapshot,
                reconciliation={
                    "execution": "ok" if execution_ok else "drift",
                    "accounting": accounting,
                },
                heartbeat=heartbeat,
            )
            return StartAuthoritySnapshot(
                cycle_id=cycle_id,
                active_plan=plan_identity,
                runtime=runtime,
                accepted_order_fingerprints=(
                    accepted_order_fingerprints(snapshot)
                ),
                accepted_order_identities=(
                    accepted_order_identities(snapshot)
                ),
                authorized_order_identities=[
                    {
                        "order_id": order_id,
                        "fingerprint": identity["fingerprint"],
                        "side": identity["side"],
                        "quantity": identity["quantity"],
                    }
                    for order_id, identity in sorted(
                        authorized.items()
                    )
                ],
                open_position_identities=position_identities,
                open_position_count=len(position_identities),
                reconciliation={
                    "execution": "ok" if execution_ok else "drift",
                    "accounting": accounting,
                },
                control_events=read_control_events_strict(
                    self.output_root
                ),
                running_evidence=running_evidence,
            )

    def _running_evidence_for_snapshot(
        self,
        *,
        cycle_id: str,
        plan: Mapping[str, Any],
        runtime: Mapping[str, Any],
        snapshot: Mapping[str, Any],
        reconciliation: Mapping[str, Any],
        heartbeat: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        evidence_at = self.store.now().isoformat()
        health = dict(heartbeat or {})
        heartbeat_digest = str(
            health.get("heartbeat_digest") or _digest({})
        )
        plan_identity = self._plan_identity(plan)
        if not plan_identity:
            return draft_running_evidence(
                cycle_id=cycle_id,
                evidence_at=evidence_at,
                heartbeat_recorded_at=health.get("recorded_at"),
                heartbeat_digest=heartbeat_digest,
                authority_status="unknown",
                plan_identity=None,
                runtime=None,
                reconciliation=None,
            )
        runtime_evidence = {
            "cycle_id": str(runtime.get("cycle_id") or ""),
            "strategy_plan_id": str(
                runtime.get("strategy_plan_id") or ""
            ),
            "strategy_plan_version": int(
                runtime.get("strategy_plan_version") or 0
            ),
            "desired_state": str(
                runtime.get("desired_state") or "stopped"
            ),
            "actual_state": str(
                runtime.get("actual_state") or "stopped"
            ),
            "accepted_order_count": int(
                runtime.get("accepted_order_count") or 0
            ),
        }
        expected_slots: list[dict[str, Any]] = []
        current_slots: list[dict[str, Any]] = []
        try:
            expected = self._plan_fingerprints(
                plan,
                observed_at=evidence_at,
            )
            commands = self._authoritative_entry_command_rows(
                cycle_id=cycle_id,
                plan=plan_identity,
            )
            command_by_id = {
                row["command_id"]: row for row in commands
            }
            initial_by_fingerprint = {
                row["fingerprint"]: row
                for row in commands
                if row["generation"] == 1
                and row["fingerprint"] in set(expected)
            }
            if (
                len(initial_by_fingerprint) != len(expected)
                or set(initial_by_fingerprint) != set(expected)
            ):
                raise ValueError(
                    "execution_receipt_identity_invalid"
                )
            expected_slots = sorted(
                (
                    {
                        "slot_id": row["slot_id"],
                        "initial_command_id": row["command_id"],
                        "expected_fingerprint": fingerprint,
                        "authorized_commands": [
                            self._running_evidence_command(
                                candidate
                            )
                            for candidate in sorted(
                                (
                                    candidate
                                    for candidate in commands
                                    if candidate["slot_id"]
                                    == row["slot_id"]
                                ),
                                key=lambda candidate: int(
                                    candidate["generation"]
                                ),
                            )
                        ],
                    }
                    for fingerprint, row in (
                        initial_by_fingerprint.items()
                    )
                ),
                key=lambda row: row["slot_id"],
            )
            lifecycle = (
                build_grid_lifecycle_evidence(
                    self.output_root,
                    cycle_id=cycle_id,
                    execution_snapshot=snapshot,
                    reconciliation={
                        "status": reconciliation.get("execution")
                    },
                )
                if plan_identity["strategy_type"] == "grid"
                else {}
            )
            accepted = accepted_order_identities(snapshot)
            positions = open_position_identities(snapshot)
            representatives: list[dict[str, Any]] = []
            for row in accepted:
                command = command_by_id.get(row["order_id"])
                if (
                    command is None
                    or row["fingerprint"]
                    != command["fingerprint"]
                    or row["side"] != command["side"]
                    or row["quantity"] != command["quantity"]
                ):
                    raise ValueError(
                        "execution_receipt_identity_invalid"
                    )
                representatives.append(
                    self._slot_representative(
                        command=command,
                        commands=commands,
                        lifecycle=lifecycle,
                        representative_kind="accepted_order",
                        representative_id=row["order_id"],
                        position=None,
                    )
                )
            for row in positions:
                command = command_by_id.get(row["trade_id"])
                if (
                    command is None
                    or row["strategy_plan_id"]
                    != plan_identity["strategy_plan_id"]
                    or row["strategy_plan_version"]
                    != plan_identity["strategy_plan_version"]
                ):
                    raise ValueError(
                        "execution_receipt_identity_invalid"
                    )
                representatives.append(
                    self._slot_representative(
                        command=command,
                        commands=commands,
                        lifecycle=lifecycle,
                        representative_kind="open_position",
                        representative_id=row["trade_id"],
                        position={
                            "trade_id": row["trade_id"],
                            "strategy_plan_id": row[
                                "strategy_plan_id"
                            ],
                            "strategy_plan_version": row[
                                "strategy_plan_version"
                            ],
                            "side": row["side"],
                            "order_quantity": row[
                                "order_quantity"
                            ],
                        },
                    )
                )
            current_slots = sorted(
                representatives,
                key=lambda row: (
                    row["slot_id"],
                    row["representative_id"],
                ),
            )
        except (
            json.JSONDecodeError,
            KeyError,
            OSError,
            TypeError,
            ValueError,
        ):
            expected_slots = []
            current_slots = []
        return draft_running_evidence(
            cycle_id=cycle_id,
            evidence_at=evidence_at,
            heartbeat_recorded_at=health.get("recorded_at"),
            heartbeat_digest=heartbeat_digest,
            authority_status="available",
            plan_identity=plan_identity,
            runtime=runtime_evidence,
            expected_slots=expected_slots,
            current_slots=current_slots,
            reconciliation={
                "execution": str(
                    reconciliation.get("execution") or "drift"
                ),
                "accounting": str(
                    reconciliation.get("accounting") or "drift"
                ),
            },
        )

    def _authoritative_entry_command_rows(
        self,
        *,
        cycle_id: str,
        plan: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        commands_path = (
            self.output_root
            / "dualtrack"
            / "nautilus_authoritative"
            / "commands"
            / f"{cycle_id}.json"
        )
        rows = json.loads(commands_path.read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            raise ValueError("execution_receipt_identity_invalid")
        selected: list[dict[str, Any]] = []
        for raw in rows:
            command = (
                raw.get("command")
                if isinstance(raw, Mapping)
                else None
            )
            if (
                not isinstance(command, Mapping)
                or str(command.get("event") or "entry").lower()
                != "entry"
                or str(command.get("strategy_plan_id") or "")
                != str(plan.get("strategy_plan_id") or "")
                or int(
                    command.get("strategy_plan_version") or 0
                )
                != int(plan.get("strategy_plan_version") or 0)
            ):
                continue
            command_id = str(raw.get("command_id") or "")
            side = str(command.get("side") or "").lower()
            if not command_id or side not in {"buy", "sell"}:
                raise ValueError(
                    "execution_receipt_identity_invalid"
                )
            generation = int(command.get("grid_generation") or 1)
            slot_id = str(
                command.get("grid_line_id") or command_id
            )
            selected.append(
                {
                    "command_id": command_id,
                    "fingerprint": order_fingerprint(command),
                    "side": side,
                    "quantity": _positive_number_text(
                        command.get("quantity")
                        or command.get("contracts")
                    ),
                    "price": _positive_number_text(
                        command.get("price")
                    ),
                    "generation": generation,
                    "slot_id": slot_id,
                    "rearm_of_order_id": (
                        str(command.get("rearm_of_order_id") or "")
                        or None
                    ),
                    "economics": {
                        "event": "entry",
                        "symbol": str(command.get("symbol") or ""),
                        "order_type": str(
                            command.get("order_type") or ""
                        ).lower(),
                        "notional": _positive_number_text(
                            command.get("notional")
                        ),
                        "sl": _positive_number_text(
                            command.get("sl")
                        ),
                        "tp": _optional_positive_number_text(
                            command.get("tp")
                        ),
                        "strategy_plan_id": str(
                            command.get("strategy_plan_id") or ""
                        ),
                        "strategy_plan_version": int(
                            command.get(
                                "strategy_plan_version"
                            )
                            or 0
                        ),
                    },
                }
            )
        ids = [row["command_id"] for row in selected]
        if len(ids) != len(set(ids)):
            raise ValueError("execution_receipt_identity_invalid")
        self._validate_authoritative_rearm_topology(selected)
        return selected

    @staticmethod
    def _validate_authoritative_rearm_topology(
        commands: list[dict[str, Any]],
    ) -> None:
        """Reject descendants that try to escape their initial Grid slot."""

        by_id = {row["command_id"]: row for row in commands}
        generations_by_slot: dict[str, set[int]] = {}
        for row in commands:
            slot_id = str(row["slot_id"])
            generation = int(row["generation"])
            parent_id = row.get("rearm_of_order_id")
            if generation < 1:
                raise ValueError("execution_receipt_identity_invalid")
            if generation == 1:
                if parent_id is not None:
                    raise ValueError("execution_receipt_identity_invalid")
            else:
                parent = by_id.get(str(parent_id or ""))
                if (
                    parent is None
                    or int(parent["generation"]) != generation - 1
                    or str(parent["slot_id"]) != slot_id
                    or parent["side"] != row["side"]
                    or parent["quantity"] != row["quantity"]
                    or parent["price"] != row["price"]
                    or parent["economics"] != row["economics"]
                ):
                    raise ValueError("execution_receipt_identity_invalid")
            seen = generations_by_slot.setdefault(slot_id, set())
            if generation in seen:
                raise ValueError("execution_receipt_identity_invalid")
            seen.add(generation)
        for generations in generations_by_slot.values():
            if generations != set(range(1, max(generations) + 1)):
                raise ValueError("execution_receipt_identity_invalid")

    @staticmethod
    def _running_evidence_command(
        command: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "command_id": str(command["command_id"]),
            "fingerprint": str(command["fingerprint"]),
            "side": str(command["side"]),
            "quantity": str(command["quantity"]),
            "price": str(command["price"]),
            "generation": int(command["generation"]),
            "economics": dict(command["economics"]),
        }

    @staticmethod
    def _slot_representative(
        *,
        command: Mapping[str, Any],
        commands: list[dict[str, Any]],
        lifecycle: Mapping[str, Any],
        representative_kind: str,
        representative_id: str,
        position: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        slot_id = str(command["slot_id"])
        generation = int(command["generation"])
        slot_commands = [
            row for row in commands if row["slot_id"] == slot_id
        ]
        max_generation = max(
            int(row["generation"]) for row in slot_commands
        )
        if generation != max_generation:
            raise ValueError("execution_receipt_identity_invalid")
        lineage = sorted(
            (
                row for row in slot_commands
            ),
            key=lambda row: int(row["generation"]),
        )
        if [row["generation"] for row in lineage] != list(
            range(1, generation + 1)
        ):
            raise ValueError("execution_receipt_identity_invalid")
        root = lineage[0]
        if any(
            row["side"] != root["side"]
            or row["quantity"] != root["quantity"]
            or row["price"] != root["price"]
            or row["economics"] != root["economics"]
            for row in lineage[1:]
        ):
            raise ValueError("execution_receipt_identity_invalid")
        packages = {
            (
                str(row.get("line_id") or ""),
                int(row.get("generation") or 0),
            ): dict(row)
            for row in lifecycle.get("lines") or []
            if isinstance(row, Mapping)
        }
        ancestry: list[dict[str, Any]] = []
        for index, row in enumerate(lineage):
            if index == 0:
                ancestry.append(
                    {
                        "generation": 1,
                        "command_id": row["command_id"],
                        "rearm_of_order_id": None,
                        "lifecycle_status": "initial",
                        "reorder_order_id": None,
                    }
                )
                continue
            previous = lineage[index - 1]
            package = packages.get(
                (slot_id, int(previous["generation"]))
            )
            if (
                row.get("rearm_of_order_id")
                != previous["command_id"]
                or package is None
                or package.get("status")
                != "completed_rearmed"
                or str(
                    (package.get("reorder") or {}).get(
                        "order_id"
                    )
                    or ""
                )
                != row["command_id"]
            ):
                raise ValueError(
                    "execution_receipt_identity_invalid"
                )
            ancestry[-1]["lifecycle_status"] = (
                "completed_rearmed"
            )
            ancestry[-1]["reorder_order_id"] = row[
                "command_id"
            ]
            ancestry.append(
                {
                    "generation": int(row["generation"]),
                    "command_id": row["command_id"],
                    "rearm_of_order_id": row.get(
                        "rearm_of_order_id"
                    ),
                    "lifecycle_status": "active",
                    "reorder_order_id": None,
                }
            )
        return {
            "slot_id": slot_id,
            "representative_kind": representative_kind,
            "representative_id": representative_id,
            "command": {
                "command_id": command["command_id"],
                "fingerprint": command["fingerprint"],
                "side": command["side"],
                "quantity": command["quantity"],
                "price": command["price"],
                "generation": generation,
                "economics": dict(command["economics"]),
            },
            "ancestry": ancestry,
            "position": dict(position) if position else None,
        }

    def _adopt_or_block(
        self,
        lease,
        state: dict[str, Any],
        *,
        authority: StartAuthoritySnapshot,
        attempt_id: str,
        observed_at: str,
        heartbeat: Mapping[str, Any],
    ) -> dict[str, Any]:
        plan = dict(authority.active_plan)
        runtime = dict(authority.runtime)
        cycle_id = authority.cycle_id
        try:
            expected = self._plan_fingerprints(
                self.plane.active_plan(cycle_id) or {},
                observed_at=observed_at,
            )
            snapshot = self.execution.snapshot(cycle_id)
            actual = all_plan_entry_fingerprints(
                snapshot,
                strategy_plan_id=plan["strategy_plan_id"],
                strategy_plan_version=plan[
                    "strategy_plan_version"
                ],
            )
            accepted = accepted_order_fingerprints(snapshot)
            accepted_rows = [
                row
                for row in snapshot.get("orders") or []
                if isinstance(row, Mapping)
                and str(row.get("state") or "").lower()
                == "accepted"
                and str(row.get("event") or "entry").lower()
                == "entry"
            ]
            open_positions = [
                row
                for row in snapshot.get("positions") or []
                if isinstance(row, Mapping)
                and str(row.get("status") or "").lower()
                == "open"
            ]
            position_identities = open_position_identities(snapshot)
            exact_positions = all(
                str(row.get("strategy_plan_id") or "")
                == plan["strategy_plan_id"]
                and int(row.get("strategy_plan_version") or 0)
                == plan["strategy_plan_version"]
                for row in open_positions
            )
            exact_accepted = all(
                str(row.get("strategy_plan_id") or "")
                == plan["strategy_plan_id"]
                and int(row.get("strategy_plan_version") or 0)
                == plan["strategy_plan_version"]
                for row in accepted_rows
            )
            expected_set = set(expected)
            accepted_by_order_id = {
                str(row.get("order_id") or ""): order_fingerprint(row)
                for row in accepted_rows
            }
            if (
                "" in accepted_by_order_id
                or len(accepted_by_order_id) != len(accepted_rows)
            ):
                raise ValueError("execution_receipt_identity_invalid")
            authoritative_identities = (
                self._authoritative_command_identities(
                    cycle_id=cycle_id, plan=plan
                )
            )
            authorized_commands = {
                order_id: row["fingerprint"]
                for order_id, row in authoritative_identities.items()
            }
            verified_rearms = self._verified_rearm_fingerprints(
                cycle_id=cycle_id,
                plan=plan,
                snapshot=snapshot,
            )
            exact_current_entries = all(
                authorized_commands.get(order_id) == fingerprint
                and (
                    fingerprint in expected_set
                    or verified_rearms.get(order_id) == fingerprint
                )
                for order_id, fingerprint in accepted_by_order_id.items()
            )
            exact_position_receipts = all(
                (
                    authorized_commands.get(row["trade_id"])
                    in expected_set
                    or verified_rearms.get(row["trade_id"])
                    == authorized_commands.get(row["trade_id"])
                )
                and row["strategy_plan_id"]
                == plan["strategy_plan_id"]
                and row["strategy_plan_version"]
                == plan["strategy_plan_version"]
                and _position_side_for_order(
                    authoritative_identities[row["trade_id"]]["side"]
                )
                == row["side"]
                and authoritative_identities[row["trade_id"]][
                    "quantity"
                ]
                == row["order_quantity"]
                for row in position_identities
                if row["trade_id"] in authoritative_identities
            )
            exact_position_receipts = (
                exact_position_receipts
                and all(
                    row["trade_id"] in authoritative_identities
                    for row in position_identities
                )
            )
            runtime_preview_id = str(
                runtime.get("preview_id") or ""
            )
            runtime_prepared_start_id = str(
                runtime.get("prepared_start_id") or ""
            )
            supervisor_started = bool(
                runtime_preview_id or runtime_prepared_start_id
            )
            matching_audits = self._matching_start_audits(
                authority,
                preview_id=runtime_preview_id,
                prepared_start_id=runtime_prepared_start_id,
            )
            audit_exact = (
                len(matching_audits) == 1
                and matching_audits[0].get("result") == "accepted"
                and matching_audits[0].get("error") in {None, ""}
                if supervisor_started
                else True
            )
            exact = (
                self._reconciliation_exact(authority)
                and set(expected).issubset(set(actual))
                and exact_positions
                and exact_position_receipts
                and exact_accepted
                and exact_current_entries
                and audit_exact
                and str(runtime.get("cycle_id") or "") == cycle_id
                and str(runtime.get("strategy_plan_id") or "")
                == plan["strategy_plan_id"]
                and int(runtime.get("strategy_plan_version") or 0)
                == plan["strategy_plan_version"]
                # Persisted runtime counts the start-accepted logical slots.
                # A later fill changes one slot's current representative from
                # an accepted order to an open position; it does not remove
                # that slot from the accepted start set.
                and int(runtime.get("accepted_order_count") or 0)
                == len(expected)
                and len(accepted) + len(position_identities)
                == len(expected)
            )
        except (KeyError, TypeError, ValueError):
            exact = False
        if not exact:
            if self._paper_continuous:
                due, next_attempt_at = (
                    self._paper_continuity_watchdog_due(
                        cycle_id,
                        observed_at=observed_at,
                    )
                )
                if not due:
                    return self._finish(
                        lease,
                        state,
                        self._result(
                            cycle_id=cycle_id,
                            observed_at=observed_at,
                            status="watchdog_waiting",
                            control_actions=0,
                            heartbeat=heartbeat,
                            next_attempt_at=next_attempt_at,
                        ),
                        authority=authority,
                    )
                self._record_degradation(
                    event_id=f"{attempt_id}:watchdog-runtime-reset",
                    cycle_id=cycle_id,
                    bypassed_gate="running_identity_gate",
                    original_machine_code="order_identity_conflict",
                    original_reason=(
                        "Paper runtime reports running but its current plan, "
                        "orders, positions, or audit identity cannot be "
                        "sealed as running_proven"
                    ),
                    alternative_action=(
                        "stop_nonproven_runtime_before_fresh_start"
                    ),
                    occurred_at=observed_at,
                )
                state = (
                    self.episodes.record_paper_continuity_watchdog_attempt(
                        state,
                        observed_at=observed_at,
                    )
                )
                try:
                    with production_mutation_lock(self.output_root):
                        self.control(
                            "stop",
                            {
                                "expected_runtime_updated_at": (
                                    runtime.get("updated_at")
                                ),
                                "transition_owner": (
                                    "paper-supervisor-continuity"
                                ),
                            },
                        )
                        post_authority = self._authority(
                            cycle_id,
                            heartbeat=heartbeat,
                        )
                except Exception:  # noqa: BLE001 - response loss is unknown.
                    return self._structural(
                        lease,
                        state,
                        cycle_id=cycle_id,
                        observed_at=observed_at,
                        machine_code="control_outcome_unknown",
                        heartbeat=heartbeat,
                        authority=authority,
                        control_actions=1,
                    )
                if (
                    str(
                        post_authority.runtime.get("actual_state") or ""
                    )
                    != "stopped"
                    or self._has_exposure(post_authority)
                    or not self._reconciliation_exact(post_authority)
                ):
                    return self._structural(
                        lease,
                        state,
                        cycle_id=cycle_id,
                        observed_at=observed_at,
                        machine_code=(
                            "partial_execution_or_cleanup_required"
                        ),
                        heartbeat=heartbeat,
                        authority=post_authority,
                        control_actions=1,
                    )
                return self._finish(
                    lease,
                    state,
                    self._result(
                        cycle_id=cycle_id,
                        observed_at=observed_at,
                        status="watchdog_runtime_reset",
                        terminal_status="watchdog_runtime_reset",
                        control_actions=1,
                        heartbeat=heartbeat,
                        next_attempt_at=(
                            _utc_timestamp(observed_at)
                            + timedelta(
                                seconds=(
                                    PAPER_CONTINUITY_WATCHDOG_SECONDS
                                )
                            )
                        ).isoformat(),
                    ),
                    authority=post_authority,
                )
            return self._structural(
                lease,
                state,
                cycle_id=cycle_id,
                observed_at=observed_at,
                machine_code="order_identity_conflict",
                heartbeat=heartbeat,
                authority=authority,
            )
        return self._finish(
            lease,
            state,
            self._result(
                cycle_id=cycle_id,
                observed_at=observed_at,
                status="healthy",
                terminal_status="adopted_existing",
                control_actions=0,
                heartbeat=heartbeat,
                expected_order_count=len(expected),
            ),
            authority=authority,
        )

    def _recover_unfinished(
        self,
        lease,
        state: dict[str, Any],
        *,
        unfinished: Mapping[str, Any],
        observed_at: str,
        heartbeat: Mapping[str, Any],
        started: float,
    ) -> dict[str, Any]:
        cycle_id = lease.cycle_id
        if not unfinished:
            return self._structural(
                lease,
                state,
                cycle_id=cycle_id,
                observed_at=observed_at,
                machine_code="control_outcome_unknown",
                heartbeat=heartbeat,
            )
        try:
            # The authority read and its durable recovery interpretation share
            # the same lock as every public production mutation.  No second
            # start can splice a different runtime/order state into the
            # snapshot between read and recovery fsync.
            with self._attempt_deadline(started):
                with production_mutation_lock(self.output_root):
                    authority = self._authority(
                        cycle_id,
                        heartbeat=heartbeat,
                    )
                    resolution = lease.recover_unfinished_intent(
                        authority
                    )
        except SupervisorAttemptDeadline:
            return self._finish(
                lease,
                state,
                self._result(
                    cycle_id=cycle_id,
                    observed_at=observed_at,
                    status="blocked_structural",
                    machine_code="control_outcome_unknown",
                    classification=STRUCTURAL,
                    control_actions=0,
                    heartbeat=heartbeat,
                    recovered_attempt_id=unfinished.get(
                        "attempt_id"
                    ),
                    authority_recovery_pending=True,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - malformed authority fails closed.
            return self._structural(
                lease,
                state,
                cycle_id=cycle_id,
                observed_at=observed_at,
                machine_code=(
                    "attempt_store_corrupt"
                    if str(exc) == "attempt_store_corrupt"
                    else "unknown_blocker"
                ),
                heartbeat=heartbeat,
            )
        if not isinstance(resolution, dict):
            return self._structural(
                lease,
                state,
                cycle_id=cycle_id,
                observed_at=observed_at,
                machine_code="control_outcome_unknown",
                heartbeat=heartbeat,
            )
        if resolution["resolution"] == "executed":
            return self._finish(
                lease,
                state,
                self._result(
                    cycle_id=cycle_id,
                    observed_at=observed_at,
                    status="healthy",
                    terminal_status="adopted_existing",
                    machine_code=None,
                    control_actions=0,
                    heartbeat=heartbeat,
                    recovered_attempt_id=unfinished.get(
                        "attempt_id"
                    ),
                ),
                authority=authority,
            )
        if resolution["resolution"] == "clean_rejection":
            classified = classify_blocker(
                control_code=resolution.get("machine_code")
            )
            proof = self._clean_refusal_proof(
                unfinished,
                authority,
                resolution,
            )
            state = self.episodes.record_clean_refusal(
                state,
                classification=classified,
                proof=proof,
                observed_at=observed_at,
                prepare_start_succeeded=False,
            )
            return self._finish(
                lease,
                state,
                self._result(
                    cycle_id=cycle_id,
                    observed_at=observed_at,
                    status=str(state.get("mode") or "backing_off"),
                    machine_code=classified["machine_code"],
                    classification=classified["classification"],
                    control_actions=0,
                    heartbeat=heartbeat,
                    recovered_attempt_id=unfinished.get(
                        "attempt_id"
                    ),
                ),
                authority=authority,
            )
        state = self.episodes.record_dangerous_outcome(
            state,
            machine_code="control_outcome_unknown",
            observed_at=observed_at,
            detail={
                "authority_digest": resolution.get(
                    "authority_digest"
                )
            },
        )
        return self._finish(
            lease,
            state,
            self._result(
                cycle_id=cycle_id,
                observed_at=observed_at,
                status="blocked_structural",
                machine_code="control_outcome_unknown",
                classification=STRUCTURAL,
                control_actions=0,
                heartbeat=heartbeat,
                recovered_attempt_id=unfinished.get(
                    "attempt_id"
                ),
            ),
            authority=authority,
        )

    @contextmanager
    def _pre_intent_exception_phase(self, phase: str):
        """Tag a propagating exception with the exact pre-intent stage."""

        try:
            yield
        except BaseException as exc:
            try:
                setattr(exc, "_paper_supervisor_phase", str(phase))
            except Exception:  # pragma: no cover - exotic immutable exceptions.
                pass
            raise

    def _record_pre_intent_exception(
        self,
        *,
        cycle_id: str,
        observed_at: str,
        exc: BaseException,
        phase: str,
        attempt_id: str | None = None,
    ) -> dict[str, str]:
        attached = getattr(
            exc,
            "_paper_supervisor_exception_receipt",
            None,
        )
        if isinstance(attached, Mapping):
            return exception_receipt_ref(attached)
        pending = self.store.unfinished_pre_intent(cycle_id)
        resolved_attempt_id = str(
            attempt_id
            or dict(pending or {}).get("attempt_id")
            or ""
        )
        receipt = self.exception_provenance.record_exception(
            cycle_id=cycle_id,
            attempt_id=resolved_attempt_id,
            phase=phase,
            occurred_at=observed_at,
            exc=exc,
        )
        ref = exception_receipt_ref(receipt)
        try:
            setattr(exc, "_paper_supervisor_phase", str(phase))
            setattr(
                exc,
                "_paper_supervisor_exception_receipt",
                ref,
            )
        except Exception:  # pragma: no cover - exotic immutable exceptions.
            pass
        return ref

    def _classify_before_intent(
        self,
        lease,
        state: dict[str, Any],
        *,
        cycle_id: str,
        observed_at: str,
        exc: BaseException,
        heartbeat: Mapping[str, Any],
        deadline_exceeded: bool,
        phase: str,
        classification_control_code: str | None = None,
    ) -> dict[str, Any]:
        exception_ref: dict[str, str] | None = None
        provenance_failed = False
        try:
            exception_ref = self._record_pre_intent_exception(
                cycle_id=cycle_id,
                observed_at=observed_at,
                exc=exc,
                phase=phase,
            )
        except PaperSupervisorExceptionEvidenceError:
            # Losing diagnostic provenance must fail closed, but must never
            # turn into a second attempt or obscure start-intent ordering.
            provenance_failed = True
        classified = (
            classify_blocker(
                evidence={
                    "local_attempt_deadline": "exceeded",
                    "start_intent_persisted": False,
                }
            )
            if deadline_exceeded
            else classify_blocker(
                control_code=(
                    str(classification_control_code)
                    if classification_control_code is not None
                    else str(getattr(exc, "code", str(exc)))
                )
            )
        )
        raw_evidence = getattr(exc, "evidence", None)
        blocker_evidence = None
        pending = self.store.unfinished_pre_intent(cycle_id)
        durable_rejection: dict[str, Any] | None = None
        candidate = (
            pending.get("candidate_identity")
            if isinstance(pending, Mapping)
            else None
        )
        if isinstance(candidate, Mapping):
            try:
                risk_envelopes = self.plane.risk_envelopes
                finder = getattr(
                    risk_envelopes,
                    "find_outer_policy_rejection_for_attempt",
                )
                if not callable(finder):
                    raise ValueError("attempt_store_corrupt")
                found = finder(
                    cycle_id=cycle_id,
                    supervisor_attempt_id=str(pending["attempt_id"]),
                )
                if found is not None:
                    durable_rejection = dict(found)
                    if (
                        dict(
                            durable_rejection.get("candidate") or {}
                        )
                        != dict(candidate)
                        or durable_rejection.get("machine_code")
                        != "outer_strategy_policy_envelope_out_of_bounds"
                    ):
                        raise ValueError("attempt_store_corrupt")
            except Exception:  # noqa: BLE001 - ambiguous durable evidence fails closed.
                classified = classify_blocker(
                    control_code="attempt_store_corrupt"
                )
                durable_rejection = None
        if durable_rejection is not None:
            exact_evidence = {
                "rejection_id": str(
                    durable_rejection["rejection_id"]
                ),
                "rejection_digest": str(
                    durable_rejection["rejection_digest"]
                ),
            }
            if (
                isinstance(raw_evidence, Mapping)
                and dict(raw_evidence) != exact_evidence
            ):
                classified = classify_blocker(
                    control_code="attempt_store_corrupt"
                )
            else:
                classified = classify_blocker(
                    control_code=(
                        "outer_strategy_policy_envelope_out_of_bounds"
                    )
                )
                blocker_evidence = exact_evidence
        elif classified["machine_code"] == (
            "outer_strategy_policy_envelope_out_of_bounds"
        ):
            classified = classify_blocker(
                control_code="attempt_store_corrupt"
            )
        elif (
            classified["machine_code"]
            in {
                "cloud_ai_provider_readiness_unavailable",
                "cloud_ai_provider_readiness_invalid",
            }
            and isinstance(raw_evidence, Mapping)
        ):
            blocker_evidence = dict(raw_evidence)
        if provenance_failed:
            classified = classify_blocker(
                control_code="attempt_store_corrupt"
            )
            blocker_evidence = None
        if pending is not None:
            lease.record_pre_intent_finished(
                attempt_id=str(pending["attempt_id"]),
                result=classified["classification"],
                machine_code=classified["machine_code"],
                classification=classified["classification"],
                observed_at=observed_at,
                evidence=blocker_evidence,
                exception_receipt=exception_ref,
            )
            state = self._reconcile_operational_wal(
                state,
                cycle_id=cycle_id,
            )
        elif classified["classification"] == TRANSIENT:
            state = self.episodes.record_transient_failure(
                state,
                classification=classified,
                observed_at=observed_at,
            )
        else:
            state = self.episodes.record_structural_blocker(
                state,
                classification=classified,
                observed_at=observed_at,
                evidence=blocker_evidence,
            )
        return self._finish(
            lease,
            state,
            self._result(
                cycle_id=cycle_id,
                observed_at=observed_at,
                status=(
                    str(state.get("mode") or "backing_off")
                    if classified["classification"] == TRANSIENT
                    else "blocked_structural"
                ),
                machine_code=classified["machine_code"],
                classification=classified["classification"],
                control_actions=0,
                heartbeat=heartbeat,
                **(
                    {"exception_receipt": exception_ref}
                    if exception_ref is not None
                    else {}
                ),
            ),
        )

    def _recheck_structural(
        self,
        state: dict[str, Any],
        *,
        authority: StartAuthoritySnapshot,
        observed_at: str,
    ) -> tuple[dict[str, Any], bool]:
        blocker = dict(state.get("blocker") or {})
        machine_code = str(
            blocker.get("machine_code") or "unknown_blocker"
        )
        cleared = False
        recheck_evidence: dict[str, Any] | None = None
        if machine_code == "ledger_reconciliation_drift":
            cleared = self._reconciliation_exact(authority)
        elif machine_code == "execution_tick_scheduler_down":
            cleared = True
        elif machine_code == "existing_exposure_conflict":
            cleared = not self._has_exposure(authority)
        elif machine_code == "active_plan_missing":
            cleared = bool(authority.active_plan)
        elif machine_code == "previous_cycle_paper_state_unresolved":
            runtime = dict(authority.runtime)
            cleared = (
                str(runtime.get("cycle_id") or "")
                == authority.cycle_id
                or (
                    not self._runtime_is_running(runtime)
                    and int(
                        runtime.get("accepted_order_count") or 0
                    )
                    == 0
                )
            )
        elif machine_code in {
            "risk_envelope_missing",
            "risk_envelope_authorization_invalid",
            "risk_envelope_preview_out_of_bounds",
        }:
            cleared = bool(
                dict(
                    self.plane.active_plan(
                        authority.cycle_id
                    )
                    or {}
                ).get(
                    "cycle_risk_envelope_id"
                )
            )
        elif machine_code == "plan_identity_conflict":
            # A release can fix the exact preview/plan identity check after a
            # prior deployment has already persisted this structural result.
            # Recheck the same immutable candidate-plan gate before allowing
            # the next fresh heartbeat to attempt convergence.  This is
            # deliberately narrower than clearing the blocker from presence
            # of a plan: the authoritative snapshot must still be clean and
            # the existing envelope verifier must pass without mutation.
            plan = dict(authority.active_plan or {})
            envelope_id = str(
                plan.get("cycle_risk_envelope_id") or ""
            )
            risk_envelopes = getattr(
                self.plane,
                "risk_envelopes",
                None,
            )
            verify_candidate = getattr(
                risk_envelopes,
                "verify_candidate_plan_identity",
                None,
            )
            if (
                plan
                and envelope_id
                and not self._has_exposure(authority)
                and self._reconciliation_exact(authority)
                and callable(verify_candidate)
            ):
                try:
                    verify_candidate(
                        cycle_id=authority.cycle_id,
                        envelope_authorization_id=envelope_id,
                        plan=plan,
                        now=observed_at,
                    )
                except Exception:  # noqa: BLE001 - fail closed on any mismatch.
                    cleared = False
                else:
                    cleared = True
        elif machine_code == "order_identity_conflict":
            cleared = self._sealed_running_identity_exact(authority)
        elif machine_code == "supervisor_configuration_invalid":
            # Reaching this pass proves the unique mode and fixed budgets were
            # accepted by the scheduler composition.
            cleared = True
        elif machine_code in {
            "outer_strategy_policy_missing",
            "outer_strategy_policy_invalid",
            "outer_strategy_policy_expired",
        }:
            try:
                self.plane.verify_supervisor_outer_policy()
                cleared = True
            except Exception:  # noqa: BLE001 - exact blocker remains active.
                cleared = False
        elif machine_code == "cloud_ai_provider_readiness_invalid":
            try:
                current_cloud_ai_provider_readiness(
                    self.output_root,
                    verifier=self.provider_readiness_verifier,
                )
                cleared = True
            except Exception:  # noqa: BLE001 - exact readiness remains blocked.
                cleared = False
        elif machine_code == (
            "outer_strategy_policy_envelope_out_of_bounds"
        ):
            cleared, recheck_evidence = (
                self._outer_policy_rejection_cleared(
                    authority,
                    blocker=blocker,
                    observed_at=observed_at,
                )
            )
        elif machine_code == "unknown_blocker":
            # Older deployments classified a provider timeout fail-closed as
            # unknown_blocker.  Clear that exact historical scene only when
            # the same cycle has a typed provider failure after the block and
            # a fresh source-bound provider readiness receipt now passes.
            cleared = self._provider_failure_cleared(
                authority.cycle_id,
                blocked_at=str(blocker.get("blocked_at") or ""),
            )
            if not cleared:
                cleared = self._legacy_clean_pre_intent_cleared(
                    authority,
                    blocked_at=str(
                        blocker.get("blocked_at") or ""
                    ),
                )
        updated = self.episodes.recheck_structural_blocker(
            state,
            machine_code=machine_code,
            condition_cleared=cleared,
            observed_at=observed_at,
            evidence=recheck_evidence,
        )
        return updated, cleared

    def _outer_policy_rejection_cleared(
        self,
        authority: StartAuthoritySnapshot,
        *,
        blocker: Mapping[str, Any],
        observed_at: str,
    ) -> tuple[bool, dict[str, Any] | None]:
        """Read-only recheck of one exact rejected candidate."""

        if not self._outer_policy_recheck_authority_clean(authority):
            return False, None
        risk_envelopes = getattr(
            self.plane,
            "risk_envelopes",
            None,
        )
        evidence = blocker.get("evidence")
        if isinstance(evidence, Mapping):
            load_for_attempt = getattr(
                risk_envelopes,
                "outer_policy_rejection_for_attempt",
                None,
            )
            recheck = getattr(
                risk_envelopes,
                "recheck_outer_policy_rejection",
                None,
            )
            verify_proof = getattr(
                risk_envelopes,
                "verify_outer_policy_recheck_proof",
                None,
            )
            if not all(
                callable(item)
                for item in (
                    load_for_attempt,
                    recheck,
                    verify_proof,
                )
            ):
                return False, None
            try:
                rejection = self._bound_outer_policy_rejection(
                    authority.cycle_id,
                    blocker=blocker,
                    loader=load_for_attempt,
                )
                result = dict(
                    recheck(
                        cycle_id=authority.cycle_id,
                        rejection_id=str(
                            evidence.get("rejection_id") or ""
                        ),
                        rejection_digest=str(
                            evidence.get("rejection_digest") or ""
                        ),
                        at=observed_at,
                    )
                )
                verified = dict(
                    verify_proof(
                        proof=result,
                        cycle_id=authority.cycle_id,
                        rejection_id=str(
                            rejection["rejection_id"]
                        ),
                        rejection_digest=str(
                            rejection["rejection_digest"]
                        ),
                    )
                )
            except Exception:  # noqa: BLE001 - malformed evidence stays blocked.
                return False, None
            return verified.get("passed") is True, verified

        legacy_resolution = getattr(
            risk_envelopes,
            "legacy_rejection_resolution",
            None,
        )
        if not callable(legacy_resolution):
            return False, None
        try:
            self.plane.verify_supervisor_outer_policy()
            resolution = legacy_resolution(
                cycle_id=authority.cycle_id,
                machine_code=(
                    "outer_strategy_policy_envelope_out_of_bounds"
                ),
                blocked_at=str(blocker.get("blocked_at") or ""),
            )
        except Exception:  # noqa: BLE001 - missing/invalid resolution stays blocked.
            return False, None
        if not isinstance(resolution, Mapping):
            return False, None
        resolution_evidence = {
            "schema_version": (
                "paper-supervisor-legacy-policy-recheck-v1"
            ),
            "cycle_id": authority.cycle_id,
            "blocked_at": str(blocker.get("blocked_at") or ""),
            "resolution_id": resolution.get("resolution_id"),
            "resolution_version": resolution.get(
                "resolution_version"
            ),
            "resolution_digest": resolution.get(
                "resolution_digest"
            ),
            "control_actions_executed": 0,
            "passed": True,
        }
        resolution_evidence["recheck_digest"] = _digest(
            resolution_evidence
        )
        return True, resolution_evidence

    def _bound_outer_policy_rejection(
        self,
        cycle_id: str,
        *,
        blocker: Mapping[str, Any],
        loader: Callable[..., Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Bind one blocker to its unique WAL attempt and rejection receipt."""

        evidence = blocker.get("evidence")
        if (
            not isinstance(evidence, Mapping)
            or set(evidence) != {"rejection_id", "rejection_digest"}
        ):
            raise ValueError("attempt_store_corrupt")
        projection = self.store.current_state(cycle_id)
        blocked_at = str(blocker.get("blocked_at") or "")
        matches = [
            dict(row)
            for row in projection.get("pre_intent_attempts") or []
            if isinstance(row, Mapping)
            and row.get("terminal_result") == "structural"
            and row.get("terminal_classification") == "structural"
            and row.get("terminal_machine_code")
            == "outer_strategy_policy_envelope_out_of_bounds"
            and row.get("terminal_observed_at") == blocked_at
            and row.get("terminal_evidence") == dict(evidence)
            and isinstance(row.get("candidate_identity"), Mapping)
        ]
        if len(matches) != 1:
            raise ValueError("attempt_store_corrupt")
        attempt = matches[0]
        rejection = dict(
            loader(
                cycle_id=cycle_id,
                supervisor_attempt_id=str(attempt["attempt_id"]),
            )
        )
        if (
            rejection.get("cycle_id") != cycle_id
            or rejection.get("rejection_id")
            != evidence.get("rejection_id")
            or rejection.get("rejection_digest")
            != evidence.get("rejection_digest")
            or dict(rejection.get("candidate") or {})
            != dict(attempt["candidate_identity"])
            or dict(rejection.get("candidate") or {}).get(
                "supervisor_attempt_id"
            )
            != attempt["attempt_id"]
        ):
            raise ValueError("attempt_store_corrupt")
        return rejection

    def _outer_policy_recheck_authority_clean(
        self,
        authority: StartAuthoritySnapshot,
    ) -> bool:
        runtime = dict(authority.runtime)
        if (
            authority.active_plan
            or self._has_exposure(authority)
            or not self._reconciliation_exact(authority)
            or int(runtime.get("accepted_order_count") or 0) != 0
            or str(runtime.get("desired_state") or "stopped")
            != "stopped"
            or str(runtime.get("actual_state") or "stopped")
            != "stopped"
        ):
            return False
        try:
            projection = self.store.current_state(
                authority.cycle_id
            )
        except Exception:  # noqa: BLE001 - malformed WAL stays blocked.
            return False
        if isinstance(projection.get("unfinished_intent"), Mapping):
            return False
        return not any(
            str(row.get("terminal_result") or "")
            in {"unknown", "control_outcome_unknown"}
            or str(row.get("terminal_machine_code") or "")
            in {
                "control_outcome_unknown",
                "partial_execution_or_cleanup_required",
            }
            for row in projection.get("attempts") or []
            if isinstance(row, Mapping)
        )

    def _sealed_running_identity_exact(
        self,
        authority: StartAuthoritySnapshot,
    ) -> bool:
        """Recheck the same sealed slot identity without a control action."""

        try:
            evidence = finalize_running_evidence(
                dict(authority.running_evidence or {}),
                persisted_at=self.store.now().isoformat(),
            )
            runtime = dict(authority.runtime)
            preview_id = str(runtime.get("preview_id") or "")
            prepared_start_id = str(
                runtime.get("prepared_start_id") or ""
            )
            supervisor_started = bool(preview_id or prepared_start_id)
            matching_audits = self._matching_start_audits(
                authority,
                preview_id=preview_id,
                prepared_start_id=prepared_start_id,
            )
            audit_exact = (
                len(matching_audits) == 1
                and matching_audits[0].get("result") == "accepted"
                and matching_audits[0].get("error") in {None, ""}
                if supervisor_started
                else True
            )
            return bool(evidence["running_proven"] and audit_exact)
        except (KeyError, TypeError, ValueError):
            return False

    def _legacy_clean_pre_intent_cleared(
        self,
        authority: StartAuthoritySnapshot,
        *,
        blocked_at: str,
    ) -> bool:
        """Recheck one legacy unknown without reading its error prose."""

        if not callable(self.pre_intent_diagnostic):
            return False
        runtime = dict(authority.runtime)
        if (
            self._has_exposure(authority)
            or bool(authority.authorized_order_identities)
            or not self._reconciliation_exact(authority)
            or int(runtime.get("accepted_order_count") or 0) != 0
            or str(runtime.get("desired_state") or "stopped")
            != "stopped"
            or str(runtime.get("actual_state") or "stopped")
            != "stopped"
        ):
            return False
        try:
            projection = self.store.current_state(
                authority.cycle_id
            )
        except Exception:  # noqa: BLE001 - malformed WAL stays blocked.
            return False
        historical = [
            dict(row)
            for row in projection.get("pre_intent_attempts") or []
            if isinstance(row, Mapping)
            and str(row.get("terminal_observed_at") or "")
            == blocked_at
            and str(row.get("terminal_machine_code") or "")
            == "unknown_blocker"
            and str(row.get("terminal_result") or "")
            == "structural"
            and row.get("prepare_succeeded_sequence") is None
        ]
        if len(historical) != 1:
            return False
        attempt_id = str(historical[0].get("attempt_id") or "")
        if not attempt_id:
            return False
        if any(
            str(row.get("attempt_id") or "") == attempt_id
            for row in projection.get("attempts") or []
            if isinstance(row, Mapping)
        ):
            return False
        unfinished_intent = projection.get("unfinished_intent")
        if isinstance(unfinished_intent, Mapping):
            return False
        if any(
            str(row.get("terminal_result") or "")
            in {"unknown", "control_outcome_unknown"}
            or str(row.get("terminal_machine_code") or "")
            in {
                "control_outcome_unknown",
                "partial_execution_or_cleanup_required",
            }
            for row in projection.get("attempts") or []
            if isinstance(row, Mapping)
        ):
            return False

        matching_prepare = []
        related_controls = []
        for raw in authority.control_events:
            if not isinstance(raw, Mapping):
                continue
            request = raw.get("request")
            if not isinstance(request, Mapping):
                continue
            if (
                str(raw.get("cycle_id") or "")
                != authority.cycle_id
                or str(request.get("supervisor_attempt_id") or "")
                != attempt_id
            ):
                continue
            related_controls.append(dict(raw))
            if (
                str(raw.get("action") or "") == "prepare_start"
                and str(raw.get("ts") or "") == blocked_at
                and str(raw.get("result") or "") == "rejected"
            ):
                matching_prepare.append(dict(raw))
        if (
            len(matching_prepare) != 1
            or len(related_controls) != 1
        ):
            return False

        full_plan = dict(
            self.plane.active_plan(authority.cycle_id) or {}
        )
        if (
            not full_plan
            or self._plan_identity(full_plan)
            != dict(authority.active_plan)
        ):
            return False
        try:
            diagnostic = dict(
                self.pre_intent_diagnostic(
                    self._request_from_plan(full_plan),
                    blocked_at,
                )
            )
        except Exception:  # noqa: BLE001 - diagnostic failures stay blocked.
            return False
        return (
            diagnostic.get("status") == "blocked"
            and diagnostic.get("code")
            == "frozen_grid_preview_market_moved"
        )

    def _provider_failure_cleared(self, cycle_id: str, *, blocked_at: str) -> bool:
        readiness = CloudAIProviderReadiness(self.output_root).verify()
        if readiness.get("ok") is not True:
            return False
        root = (
            self.output_root
            / "dualtrack"
            / "strategy_control"
            / "evaluations"
            / str(cycle_id)
        )
        evaluations: list[dict[str, Any]] = []
        for path in sorted(root.glob("*.json")):
            try:
                rows = load_json(path)
            except Exception:  # noqa: BLE001 - malformed evidence stays blocked.
                continue
            if rows and isinstance(rows[-1], dict):
                evaluations.append(rows[-1])
        if not evaluations:
            return False
        latest = max(evaluations, key=lambda row: str(row.get("evaluated_at") or ""))
        if str(latest.get("evaluated_at") or "") < str(blocked_at or ""):
            return False
        output = latest.get("output")
        if (
            latest.get("schema_version")
            != "strategy-ai-evaluation-v2"
            or latest.get("status") != "failed"
            or not isinstance(output, Mapping)
        ):
            return False
        code = output.get("machine_code")
        return (
            isinstance(code, str)
            and code in RECOVERABLE_PROVIDER_CODES
        )

    def _pre_intent_deadline(
        self,
        lease,
        state: dict[str, Any],
        *,
        cycle_id: str,
        observed_at: str,
        heartbeat: Mapping[str, Any],
        preview_id: Any = None,
        prepared_start_id: Any = None,
    ) -> dict[str, Any]:
        return self._classify_before_intent(
            lease,
            state,
            cycle_id=cycle_id,
            observed_at=observed_at,
            exc=TimeoutError(
                "supervisor_attempt_deadline_before_intent"
            ),
            heartbeat=heartbeat,
            deadline_exceeded=True,
            phase="attempt_deadline",
        )

    def _structural(
        self,
        lease,
        state: dict[str, Any],
        *,
        cycle_id: str,
        observed_at: str,
        machine_code: str,
        heartbeat: Mapping[str, Any],
        authority: StartAuthoritySnapshot | None = None,
        control_actions: int = 0,
        **detail: Any,
    ) -> dict[str, Any]:
        classified = classify_blocker(control_code=machine_code)
        pending = self.store.unfinished_pre_intent(cycle_id)
        if pending is not None:
            lease.record_pre_intent_finished(
                attempt_id=str(pending["attempt_id"]),
                result="structural",
                machine_code=classified["machine_code"],
                classification=classified["classification"],
                observed_at=observed_at,
            )
            state = self._reconcile_operational_wal(
                state,
                cycle_id=cycle_id,
            )
        else:
            state = self.episodes.record_structural_blocker(
                state,
                classification=classified,
                observed_at=observed_at,
            )
        return self._finish(
            lease,
            state,
            self._result(
                cycle_id=cycle_id,
                observed_at=observed_at,
                status="blocked_structural",
                machine_code=classified["machine_code"],
                classification=classified["classification"],
                control_actions=control_actions,
                heartbeat=heartbeat,
                **detail,
            ),
            authority=authority,
        )

    def _finish(
        self,
        lease,
        state: dict[str, Any],
        result: dict[str, Any],
        *,
        authority: StartAuthoritySnapshot | None = None,
    ) -> dict[str, Any]:
        pending = self.store.unfinished_pre_intent(
            str(result["cycle_id"])
        )
        if pending is not None:
            lease.record_pre_intent_finished(
                attempt_id=str(pending["attempt_id"]),
                result="no_action",
                machine_code=None,
                classification=None,
                observed_at=str(result["observed_at"]),
            )
            state = self._reconcile_operational_wal(
                state,
                cycle_id=str(result["cycle_id"]),
            )
        running_evidence = (
            dict(authority.running_evidence)
            if (
                authority is not None
                and isinstance(
                    authority.running_evidence,
                    Mapping,
                )
            )
            else draft_running_evidence(
                cycle_id=str(result["cycle_id"]),
                evidence_at=self.store.now().isoformat(),
                heartbeat_recorded_at=dict(
                    result.get("heartbeat") or {}
                ).get("recorded_at"),
                heartbeat_digest=str(
                    dict(result.get("heartbeat") or {}).get(
                        "heartbeat_digest"
                    )
                    or _digest({})
                ),
                authority_status="unknown",
                plan_identity=None,
                runtime=None,
                reconciliation=None,
            )
        )
        if running_evidence.get("running_proven") is True:
            state = {
                **state,
                "last_running_proof": {
                    "cycle_id": running_evidence.get("cycle_id"),
                    "evidence_at": running_evidence.get("evidence_at"),
                    "plan_identity": dict(
                        running_evidence.get("plan_identity") or {}
                    ),
                    "runtime": dict(
                        running_evidence.get("runtime") or {}
                    ),
                },
            }
        projected = self.store.current_state(str(result["cycle_id"]))
        pre_intent_attempts = [
            dict(row)
            for row in projected.get("pre_intent_attempts") or []
            if isinstance(row, Mapping)
        ]
        start_attempts = [
            dict(row)
            for row in projected.get("attempts") or []
            if isinstance(row, Mapping)
        ]
        last_attempt = pre_intent_attempts[-1] if pre_intent_attempts else {}
        state = {
            **state,
            "count_summary": {
                "attempt_count": len(pre_intent_attempts),
                "start_intent_count": len(start_attempts),
                "last_attempt": (
                    {
                        "attempt_id": last_attempt.get("attempt_id"),
                        "observed_at": last_attempt.get("observed_at"),
                        "result": last_attempt.get("terminal_result"),
                        "machine_code": last_attempt.get(
                            "terminal_machine_code"
                        ),
                        "classification": last_attempt.get(
                            "terminal_classification"
                        ),
                        "exception_receipt": last_attempt.get(
                            "terminal_exception_receipt"
                        ),
                        "source_tick_key": last_attempt.get(
                            "source_tick_key"
                        ),
                    }
                    if last_attempt
                    else None
                ),
            },
        }
        self.store.commit_episode_observation(
            lease,
            state=state,
            payload={
                **result,
                "episode": {
                    "mode": state.get("mode"),
                    "episode_id": dict(
                        state.get("episode") or {}
                    ).get("episode_id"),
                    "next_attempt_at": dict(
                        state.get("episode") or {}
                    ).get("next_attempt_at"),
                    "budgets": state.get("budgets"),
                    "alert_required": state.get(
                        "alert_required"
                    ),
                },
                "running_evidence": running_evidence,
            },
        )
        return result

    def _start_response_exact(
        self,
        response: Mapping[str, Any],
        authority: StartAuthoritySnapshot,
        *,
        intent_contract: Mapping[str, Any],
        preview_id: str,
        prepared_start_id: str,
    ) -> bool:
        plan = dict(intent_contract["plan_identity"])
        runtime = dict(authority.runtime)
        expected = list(
            intent_contract["expected_order_fingerprints"]
        )
        snapshot = self.execution.snapshot(authority.cycle_id)
        try:
            all_actual = all_plan_entry_fingerprints(
                snapshot,
                strategy_plan_id=plan["strategy_plan_id"],
                strategy_plan_version=plan[
                    "strategy_plan_version"
                ],
            )
            accepted_rows = [
                row
                for row in snapshot.get("orders") or []
                if isinstance(row, Mapping)
                and str(row.get("state") or "").lower()
                == "accepted"
                and str(row.get("event") or "entry").lower()
                == "entry"
            ]
            open_positions = [
                row
                for row in snapshot.get("positions") or []
                if isinstance(row, Mapping)
                and str(row.get("status") or "").lower() == "open"
            ]
            position_identities = open_position_identities(snapshot)
            current_identity = all(
                str(row.get("strategy_plan_id") or "")
                == plan["strategy_plan_id"]
                and int(row.get("strategy_plan_version") or 0)
                == plan["strategy_plan_version"]
                for row in [*accepted_rows, *open_positions]
            )
            accepted_ids = [
                str(row.get("order_id") or "")
                for row in accepted_rows
            ]
            position_ids = [
                row["position_id"] for row in position_identities
            ]
            position_trade_ids = [
                row["trade_id"] for row in position_identities
            ]
            receipt_ids_exact = (
                "" not in accepted_ids
                and len(set(accepted_ids)) == len(accepted_ids)
                and "" not in position_ids
                and len(set(position_ids)) == len(position_ids)
                and "" not in position_trade_ids
                and len(set(position_trade_ids))
                == len(position_trade_ids)
            )
            authoritative_identities = (
                self._authoritative_command_identities(
                    cycle_id=authority.cycle_id, plan=plan
                )
            )
            authorized_commands = {
                order_id: row["fingerprint"]
                for order_id, row in authoritative_identities.items()
            }
            accepted_receipts_exact = all(
                authorized_commands.get(
                    str(row.get("order_id") or "")
                )
                == order_fingerprint(row)
                for row in accepted_rows
            )
            position_fingerprints = [
                authorized_commands.get(row["trade_id"])
                for row in position_identities
            ]
            positions_bound = (
                all(position_fingerprints)
                and all(
                    row["strategy_plan_id"]
                    == plan["strategy_plan_id"]
                    and row["strategy_plan_version"]
                    == plan["strategy_plan_version"]
                    and _position_side_for_order(
                        authoritative_identities[
                            row["trade_id"]
                        ]["side"]
                    )
                    == row["side"]
                    and authoritative_identities[
                        row["trade_id"]
                    ]["quantity"]
                    == row["order_quantity"]
                    for row in position_identities
                    if row["trade_id"] in authoritative_identities
                )
                and all(
                    row["trade_id"] in authoritative_identities
                    for row in position_identities
                )
            )
        except (KeyError, TypeError, ValueError):
            return False
        accepted_count = len(
            authority.accepted_order_fingerprints
        )
        return (
            self._reconciliation_exact(authority)
            and self._runtime_is_running(runtime)
            and dict(authority.active_plan) == plan
            and str(runtime.get("strategy_plan_id") or "")
            == plan["strategy_plan_id"]
            and int(runtime.get("strategy_plan_version") or 0)
            == plan["strategy_plan_version"]
            and all_actual == expected
            and current_identity
            and receipt_ids_exact
            and accepted_receipts_exact
            and positions_bound
            and sorted(
                [
                    *[
                        order_fingerprint(row)
                        for row in accepted_rows
                    ],
                    *[
                        str(fingerprint)
                        for fingerprint in position_fingerprints
                        if fingerprint
                    ],
                ]
            )
            == expected
            and int(runtime.get("accepted_order_count") or 0)
            == accepted_count
            and accepted_count + len(position_identities)
            == len(expected)
            and int(response.get("created_orders") or 0)
            == len(expected)
            and int(response.get("accepted_orders") or 0)
            == accepted_count
            and (
                int(response.get("accepted_orders") or 0)
                + int(response.get("filled_orders") or 0)
                == len(expected)
            )
            and response.get("audit_recorded") is True
            and self._single_accepted_start_audit(
                authority,
                preview_id=preview_id,
                prepared_start_id=prepared_start_id,
            )
        )

    @staticmethod
    def _matching_start_audits(
        authority: StartAuthoritySnapshot,
        *,
        preview_id: str,
        prepared_start_id: str,
    ) -> list[dict[str, Any]]:
        if not preview_id or not prepared_start_id:
            return []
        matches: list[dict[str, Any]] = []
        for raw in authority.control_events:
            if not isinstance(raw, Mapping):
                continue
            request = raw.get("request")
            if not isinstance(request, Mapping):
                continue
            if (
                raw.get("schema_version")
                == "strategy-control-event-v1"
                and str(raw.get("cycle_id") or "")
                == authority.cycle_id
                and str(raw.get("action") or "") == "start"
                and str(request.get("expected_preview_id") or "")
                == preview_id
                and str(request.get("prepared_start_id") or "")
                == prepared_start_id
            ):
                matches.append(dict(raw))
        return matches

    @classmethod
    def _single_accepted_start_audit(
        cls,
        authority: StartAuthoritySnapshot,
        *,
        preview_id: str,
        prepared_start_id: str,
    ) -> bool:
        matches = cls._matching_start_audits(
            authority,
            preview_id=preview_id,
            prepared_start_id=prepared_start_id,
        )
        return (
            len(matches) == 1
            and matches[0].get("result") == "accepted"
            and matches[0].get("error") in {None, ""}
        )

    def _verified_rearm_fingerprints(
        self,
        *,
        cycle_id: str,
        plan: Mapping[str, Any],
        snapshot: Mapping[str, Any],
    ) -> dict[str, str]:
        """Bind every active replacement to immutable command/lifecycle facts."""

        if str(plan.get("strategy_type") or "") != "grid":
            return {}
        reconciliation = self.execution.reconcile(cycle_id)
        evidence = build_grid_lifecycle_evidence(
            self.output_root,
            cycle_id=cycle_id,
            execution_snapshot=snapshot,
            reconciliation=reconciliation,
        )
        verified_order_ids = {
            str((row.get("reorder") or {}).get("order_id") or "")
            for row in evidence.get("lines") or []
            if isinstance(row, Mapping)
            and row.get("status") == "completed_rearmed"
            and isinstance(row.get("reorder"), Mapping)
        } - {""}
        if not verified_order_ids:
            return {}
        authoritative = self._authoritative_command_fingerprints(
            cycle_id=cycle_id,
            plan=plan,
        )
        return {
            order_id: authoritative[order_id]
            for order_id in verified_order_ids
            if order_id in authoritative
        }

    def _authoritative_command_fingerprints(
        self,
        *,
        cycle_id: str,
        plan: Mapping[str, Any],
    ) -> dict[str, str]:
        return {
            order_id: row["fingerprint"]
            for order_id, row in (
                self._authoritative_command_identities(
                    cycle_id=cycle_id,
                    plan=plan,
                )
            ).items()
        }

    def _authoritative_command_identities(
        self,
        *,
        cycle_id: str,
        plan: Mapping[str, Any],
    ) -> dict[str, dict[str, str]]:
        commands_path = (
            self.output_root
            / "dualtrack"
            / "nautilus_authoritative"
            / "commands"
            / f"{cycle_id}.json"
        )
        try:
            rows = json.loads(commands_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return {}
        if not isinstance(rows, list):
            return {}
        selected_rows = [
            raw
            for raw in rows
            if isinstance(raw, Mapping)
            and isinstance(raw.get("command"), Mapping)
            and str(
                (raw.get("command") or {}).get("strategy_plan_id")
                or ""
            )
            == str(plan.get("strategy_plan_id") or "")
            and int(
                (raw.get("command") or {}).get(
                    "strategy_plan_version"
                )
                or 0
            )
            == int(plan.get("strategy_plan_version") or 0)
            and str(
                (raw.get("command") or {}).get("event")
                or "entry"
            ).lower()
            == "entry"
        ]
        selected_ids = [
            str(raw.get("command_id") or "") for raw in selected_rows
        ]
        if (
            "" in selected_ids
            or len(set(selected_ids)) != len(selected_ids)
        ):
            return {}
        verified: dict[str, dict[str, str]] = {}
        for raw in selected_rows:
            order_id = str(raw.get("command_id") or "")
            command = raw.get("command")
            if (
                not order_id
                or not isinstance(command, Mapping)
            ):
                continue
            try:
                side = str(command.get("side") or "").lower()
                if side not in {"buy", "sell"}:
                    return {}
                verified[order_id] = {
                    "fingerprint": order_fingerprint(command),
                    "side": side,
                    "quantity": _positive_number_text(
                        command.get("quantity")
                        or command.get("contracts")
                    ),
                }
            except (TypeError, ValueError):
                return {}
        if len(verified) != len(selected_rows):
            return {}
        return verified

    def _pre_intent_authority_blocker(
        self,
        authority: StartAuthoritySnapshot,
        *,
        intent_contract: Mapping[str, Any],
    ) -> str | None:
        runtime = dict(authority.runtime)
        expected_pre_start = dict(
            intent_contract.get("pre_start_plan_identity") or {}
        )
        if not self._reconciliation_exact(authority):
            return "ledger_reconciliation_drift"
        if self._has_exposure(authority):
            return "existing_exposure_conflict"
        if self._runtime_is_running(runtime) or (
            runtime.get("desired_state") not in {None, "", "stopped"}
            or runtime.get("actual_state") not in {None, "", "stopped"}
        ):
            return "runtime_state_conflict"
        persisted_cycle_id = str(runtime.get("cycle_id") or "")
        if (
            persisted_cycle_id
            and persisted_cycle_id != authority.cycle_id
            and int(runtime.get("accepted_order_count") or 0) > 0
        ):
            return "previous_cycle_paper_state_unresolved"
        if dict(authority.active_plan) != expected_pre_start:
            return "plan_identity_conflict"
        return None

    def _reconcile_terminal_outcomes(
        self,
        state: dict[str, Any],
        *,
        cycle_id: str,
    ) -> tuple[dict[str, Any], bool]:
        """Replay only durable budget effects missing after a crash."""

        projection = self.store.current_state(cycle_id)
        floor = dict(projection.get("budget_floor") or {})
        budgets = dict(state.get("budgets") or {})
        applied_dangerous = int(
            budgets.get("dangerous_start_attempts") or 0
        )
        applied_clean = int(
            budgets.get("clean_refusal_observations") or 0
        )
        dangerous_floor = int(
            floor.get("dangerous_start_attempts") or 0
        )
        clean_floor = int(
            floor.get("clean_refusal_observations") or 0
        )
        if (
            applied_dangerous > dangerous_floor
            or applied_clean > clean_floor
        ):
            raise ValueError("attempt_store_corrupt")
        changed = False
        seen_dangerous = 0
        seen_clean = 0
        attempts = sorted(
            (
                dict(row)
                for row in projection.get("attempts") or []
                if isinstance(row, Mapping)
                and row.get("terminal_sequence") is not None
            ),
            key=lambda row: int(row["terminal_sequence"]),
        )
        for row in attempts:
            event_type = str(row.get("terminal_event_type") or "")
            terminal = str(row.get("terminal_result") or "")
            machine_code = str(
                row.get("terminal_machine_code") or ""
            )
            recorded_at = str(
                row.get("terminal_recorded_at") or ""
            )
            if (
                event_type == "recovery_result"
                and terminal == "clean_rejection"
            ):
                seen_clean += 1
                if seen_clean <= applied_clean:
                    continue
                prepared_start_id = str(
                    row.get("prepared_start_id") or ""
                )
                proof = CleanRefusalProof(
                    prepared_start_id=prepared_start_id,
                    runtime_actual_state="stopped",
                    runtime_prepared_start_id=None,
                    accepted_order_count=0,
                    control_audit_result="rejected",
                    control_audit_prepared_start_id=(
                        prepared_start_id
                    ),
                    authority_digest=str(
                        row.get("terminal_authority_digest") or ""
                    ),
                )
                state = self.episodes.record_clean_refusal(
                    state,
                    classification=classify_blocker(
                        control_code=machine_code
                    ),
                    proof=proof,
                    observed_at=recorded_at,
                    prepare_start_succeeded=False,
                )
                applied_clean += 1
                changed = True
                continue
            dangerous = (
                terminal == "control_outcome_unknown"
                or (
                    event_type == "start_result"
                    and terminal == "unknown"
                )
            )
            if not dangerous:
                continue
            seen_dangerous += 1
            if seen_dangerous <= applied_dangerous:
                continue
            code = (
                machine_code
                if machine_code
                in {
                    "control_outcome_unknown",
                    "partial_execution_or_cleanup_required",
                }
                else "control_outcome_unknown"
            )
            state = self.episodes.record_dangerous_outcome(
                state,
                machine_code=code,
                observed_at=recorded_at,
                detail={"replayed_from_durable_terminal": True},
            )
            applied_dangerous += 1
            changed = True
        if (
            applied_dangerous != dangerous_floor
            or applied_clean != clean_floor
        ):
            raise ValueError("attempt_store_corrupt")
        return state, changed

    def _reconcile_operational_wal(
        self,
        state: dict[str, Any],
        *,
        cycle_id: str,
    ) -> dict[str, Any]:
        """Replay typed pre-intent/heartbeat facts exactly once."""

        cursor = int(state.get("last_applied_wal_sequence") or 0)
        events = self.store.events(cycle_id)
        if cursor < 0 or cursor > len(events):
            raise ValueError("attempt_store_corrupt")
        for event in events:
            sequence = int(event["sequence"])
            if sequence <= cursor:
                continue
            event_type = str(event.get("event_type") or "")
            payload = dict(event.get("payload") or {})
            if event_type == "typed_heartbeat_observed":
                state, _ = self.episodes.observe_heartbeat(
                    state,
                    status=str(payload["status"]),
                    observed_at=str(payload["observed_at"]),
                )
            elif event_type == "pre_intent_prepare_succeeded":
                state = self.episodes.record_prepare_start_success(
                    state,
                    observed_at=str(payload["observed_at"]),
                )
            elif event_type in {
                "pre_intent_attempt_finished",
                "pre_intent_attempt_abandoned",
            }:
                result = str(payload.get("result") or "")
                if result == "prepare_succeeded":
                    state = self.episodes.record_prepare_start_success(
                        state,
                        observed_at=str(payload["observed_at"]),
                    )
                elif result == "transient":
                    state = self.episodes.record_transient_failure(
                        state,
                        classification=classify_blocker(
                            control_code=str(
                                payload["machine_code"]
                            )
                        ),
                        observed_at=str(payload["observed_at"]),
                    )
                elif result == "structural":
                    state = self.episodes.record_structural_blocker(
                        state,
                        classification=classify_blocker(
                            control_code=str(
                                payload["machine_code"]
                            )
                        ),
                        observed_at=str(payload["observed_at"]),
                        evidence=(
                            dict(payload["evidence"])
                            if isinstance(
                                payload.get("evidence"),
                                Mapping,
                            )
                            else None
                        ),
                    )
            state["last_applied_wal_sequence"] = sequence
            cursor = sequence
        return state

    def _plan_fingerprints(
        self,
        plan: Mapping[str, Any],
        *,
        observed_at: str,
    ) -> list[str]:
        strategy_type = str(
            plan.get("strategy_type") or "grid"
        ).lower()
        commands = (
            build_dca_entry_commands(
                dict(plan),
                timestamp=observed_at,
            )
            if strategy_type == "dca"
            else build_plan_grid_entry_commands(
                dict(plan),
                timestamp=observed_at,
            )
        )
        return build_start_intent_contract(
            plan=plan,
            commands=commands,
        )["expected_order_fingerprints"]

    @staticmethod
    def _intent_contract(
        prepared: Mapping[str, Any],
    ) -> dict[str, Any]:
        contract = prepared.get("start_intent_contract")
        if (
            not isinstance(contract, Mapping)
            or contract.get("schema_version")
            != INTENT_CONTRACT_SCHEMA_VERSION
            or not isinstance(contract.get("plan_identity"), Mapping)
            or not isinstance(
                contract.get("expected_order_fingerprints"),
                list,
            )
            or int(contract.get("expected_order_count") or 0)
            != len(contract["expected_order_fingerprints"])
        ):
            raise ValueError("execution_receipt_identity_invalid")
        return dict(contract)

    @staticmethod
    def _plan_identity(plan: Mapping[str, Any]) -> dict[str, Any]:
        if not plan:
            return {}
        return {
            "strategy_plan_id": str(
                plan.get("strategy_plan_id") or ""
            ),
            "strategy_plan_version": int(
                plan.get("version")
                or plan.get("strategy_plan_version")
                or 0
            ),
            "strategy_type": str(
                plan.get("strategy_type") or "grid"
            ),
            "direction": str(plan.get("direction") or ""),
        }

    @staticmethod
    def _runtime_is_running(runtime: Mapping[str, Any]) -> bool:
        return (
            runtime.get("desired_state") == "running"
            and runtime.get("actual_state") == "running"
        )

    @staticmethod
    def _has_exposure(authority: StartAuthoritySnapshot) -> bool:
        return bool(
            authority.accepted_order_fingerprints
            or authority.open_position_count
        )

    @staticmethod
    def _reconciliation_exact(
        authority: StartAuthoritySnapshot,
    ) -> bool:
        return (
            authority.reconciliation.get("execution") == "ok"
            and authority.reconciliation.get("accounting") == "pass"
        )

    def _deadline_exceeded(self, started: float) -> bool:
        return (
            self.monotonic() - started
            >= self.attempt_deadline_seconds
        )

    @contextmanager
    def _attempt_deadline(self, started: float):
        """Interrupt a blocking local phase before systemd's 55s kill."""

        remaining = (
            self.attempt_deadline_seconds
            - (self.monotonic() - started)
        )
        hard_deadline = os.getenv(HARD_DEADLINE_ENV)
        if hard_deadline not in {None, ""}:
            try:
                hard_remaining = (
                    float(hard_deadline)
                    - time.monotonic()
                    - HARD_DEADLINE_RECOVERY_MARGIN_SECONDS
                )
            except (TypeError, ValueError):
                hard_remaining = -1.0
            remaining = min(remaining, hard_remaining)
        if remaining <= 0:
            raise SupervisorAttemptDeadline(
                "supervisor_attempt_deadline_before_intent"
            )
        if threading.current_thread() is not threading.main_thread():
            # Python delivers SIGALRM only on the main interpreter thread.
            # Refuse before entering a potentially mutating operation instead
            # of pretending that a boundary check is a hard deadline.
            raise SupervisorAttemptDeadline(
                "supervisor_attempt_deadline_before_intent"
            )
        prior_handler = signal.getsignal(signal.SIGALRM)
        prior_delay, prior_interval = signal.getitimer(
            signal.ITIMER_REAL
        )
        entered = time.monotonic()

        def expire(_signum, _frame) -> None:
            raise SupervisorAttemptDeadline(
                "supervisor_attempt_deadline"
            )

        signal.signal(signal.SIGALRM, expire)
        alarm_delay = (
            min(remaining, prior_delay)
            if prior_delay > 0
            else remaining
        )
        signal.setitimer(signal.ITIMER_REAL, alarm_delay)
        try:
            yield
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, prior_handler)
            if prior_delay > 0:
                elapsed = max(0.0, time.monotonic() - entered)
                restored = max(0.000001, prior_delay - elapsed)
                signal.setitimer(
                    signal.ITIMER_REAL,
                    restored,
                    prior_interval,
                )

    @staticmethod
    def _clean_refusal_proof(
        unfinished: Mapping[str, Any],
        authority: StartAuthoritySnapshot,
        resolution: Mapping[str, Any],
    ) -> CleanRefusalProof:
        payload = dict(unfinished.get("payload") or {})
        audit = next(
            (
                row
                for row in authority.control_events
                if str(row.get("action") or "") == "start"
                and str(
                    dict(row.get("request") or {}).get(
                        "prepared_start_id"
                    )
                    or ""
                )
                == str(payload.get("prepared_start_id") or "")
            ),
            {},
        )
        return CleanRefusalProof(
            prepared_start_id=str(
                payload.get("prepared_start_id") or ""
            ),
            runtime_actual_state=str(
                authority.runtime.get("actual_state") or ""
            ),
            runtime_prepared_start_id=authority.runtime.get(
                "prepared_start_id"
            ),
            accepted_order_count=len(
                authority.accepted_order_fingerprints
            ),
            control_audit_result=str(
                audit.get("result") or ""
            ),
            control_audit_prepared_start_id=str(
                dict(audit.get("request") or {}).get(
                    "prepared_start_id"
                )
                or ""
            ),
            authority_digest=str(
                resolution.get("authority_digest") or ""
            ),
        )

    @staticmethod
    def _result(
        *,
        cycle_id: str,
        observed_at: str,
        status: str,
        control_actions: int,
        **detail: Any,
    ) -> dict[str, Any]:
        return {
            "schema_version": SUPERVISOR_SCHEMA_VERSION,
            "cycle_id": cycle_id,
            "observed_at": observed_at,
            "status": status,
            "control_actions_executed": int(control_actions),
            **detail,
        }


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _utc_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("supervisor_timestamp_invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("supervisor_timestamp_invalid")
    return parsed.astimezone(timezone.utc)


def _position_side_for_order(value: Any) -> str:
    side = str(value or "").lower()
    if side == "buy":
        return "long"
    if side == "sell":
        return "short"
    raise ValueError("execution_receipt_identity_invalid")


def _positive_number_text(value: Any) -> str:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(
            "execution_receipt_identity_invalid"
        ) from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError("execution_receipt_identity_invalid")
    return format(parsed.normalize(), "f")


def _optional_positive_number_text(value: Any) -> str | None:
    if value in {None, ""}:
        return None
    return _positive_number_text(value)
