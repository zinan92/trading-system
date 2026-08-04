"""Pure Paper Supervisor retry episodes and two-tier attempt budgets."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from services.paper_supervisor_classifier import (
    CLASSIFIER_VERSION,
    STRUCTURAL,
    STRUCTURAL_MACHINE_CODES,
    TRANSIENT,
    TRANSIENT_MACHINE_CODES,
    classify_blocker,
)


STATE_SCHEMA_VERSION = "paper-supervisor-episode-state-v1"
EVENT_SCHEMA_VERSION = "paper-supervisor-episode-event-v1"
SHORT_BACKOFF_SECONDS = (60, 120, 300, 600, 1200)
SHORT_FAILURE_LIMIT = 5
PROBE_INTERVAL_SECONDS = 1800
DANGEROUS_START_LIMIT = 2
CLEAN_REFUSAL_LIMIT = 48
CLEAN_REFUSAL_WARNING_AT = 40
HEARTBEAT_STRUCTURAL_AFTER_SECONDS = 600
_MODES = frozenset(
    {
        "ready",
        "backing_off",
        "probing",
        "blocked_structural",
    }
)


class SupervisorEpisodeError(RuntimeError):
    """Stable fail-closed episode-state error."""

    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


@dataclass(frozen=True)
class CleanRefusalProof:
    """Three-authority proof that one rejected start created zero orders."""

    prepared_start_id: str
    runtime_actual_state: str
    runtime_prepared_start_id: str | None
    accepted_order_count: int
    control_audit_result: str
    control_audit_prepared_start_id: str
    authority_digest: str

    def is_exact(self) -> bool:
        return (
            _identity_or_none(self.prepared_start_id) is not None
            and self.runtime_actual_state == "stopped"
            and (
                self.runtime_prepared_start_id in {None, ""}
                or self.runtime_prepared_start_id
                == self.prepared_start_id
            )
            and self.accepted_order_count == 0
            and self.control_audit_result == "rejected"
            and self.control_audit_prepared_start_id
            == self.prepared_start_id
            and _digest_or_none(self.authority_digest) is not None
        )

    def evidence_digest(self) -> str:
        return _digest(
            {
                "prepared_start_id": self.prepared_start_id,
                "runtime_actual_state": self.runtime_actual_state,
                "runtime_prepared_start_id": (
                    self.runtime_prepared_start_id
                ),
                "accepted_order_count": self.accepted_order_count,
                "control_audit_result": self.control_audit_result,
                "control_audit_prepared_start_id": (
                    self.control_audit_prepared_start_id
                ),
                "authority_digest": self.authority_digest,
            }
        )


class SupervisorEpisodeMachine:
    """State transitions only; this class performs no control operation."""

    def new_cycle(
        self,
        cycle_id: str,
        *,
        observed_at: str | datetime,
    ) -> dict[str, Any]:
        cycle = _cycle_id(cycle_id)
        now = _timestamp(observed_at)
        state = {
            "schema_version": STATE_SCHEMA_VERSION,
            "cycle_id": cycle,
            "mode": "ready",
            "episode": self._new_episode(
                generation=1,
                now=now,
            ),
            "budgets": {
                "dangerous_start_attempts": 0,
                "dangerous_start_limit": DANGEROUS_START_LIMIT,
                "clean_refusal_observations": 0,
                "clean_refusal_limit": CLEAN_REFUSAL_LIMIT,
                "clean_refusal_warning_at": CLEAN_REFUSAL_WARNING_AT,
            },
            "heartbeat": {
                "status": "unknown",
                "missing_since": None,
                "missing_episode_seconds": None,
            },
            "blocker": None,
            "alert_required": False,
            "warning_required": False,
            "events": [],
        }
        return self._event(
            state,
            event_type="cycle_initialized",
            observed_at=now,
            detail={
                "budgets_reset": True,
                "prior_cycle_state_inherited": False,
            },
        )

    def guard_start_intent(
        self,
        state: Mapping[str, Any],
        *,
        observed_at: str | datetime,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Block before a would-be third dangerous or 49th clean attempt."""

        current = self._state(state)
        now = _timestamp(observed_at)
        if current["mode"] == "blocked_structural":
            blocker = dict(current["blocker"] or {})
            return current, {
                "allowed": False,
                "machine_code": blocker.get("machine_code")
                or "unknown_blocker",
                "classification": STRUCTURAL,
                "start_intent_written": False,
            }
        next_attempt = current["episode"].get("next_attempt_at")
        if (
            current["mode"] in {"backing_off", "probing"}
            and next_attempt
            and now < _timestamp(next_attempt)
        ):
            return current, {
                "allowed": False,
                "machine_code": None,
                "classification": None,
                "reason": current["mode"],
                "next_attempt_at": next_attempt,
                "start_intent_written": False,
            }
        budgets = current["budgets"]
        if (
            budgets["dangerous_start_attempts"]
            >= DANGEROUS_START_LIMIT
        ):
            blocked = self._structural(
                current,
                machine_code="dangerous_start_attempt_cap_reached",
                observed_at=now,
                detail={
                    "dangerous_start_attempts": budgets[
                        "dangerous_start_attempts"
                    ],
                    "limit": DANGEROUS_START_LIMIT,
                },
            )
            return blocked, self._guard_decision(blocked)
        if (
            budgets["clean_refusal_observations"]
            >= CLEAN_REFUSAL_LIMIT
        ):
            blocked = self._structural(
                current,
                machine_code="clean_refusal_observation_cap_reached",
                observed_at=now,
                detail={
                    "clean_refusal_observations": budgets[
                        "clean_refusal_observations"
                    ],
                    "limit": CLEAN_REFUSAL_LIMIT,
                },
            )
            return blocked, self._guard_decision(blocked)
        return current, {
            "allowed": True,
            "machine_code": None,
            "classification": None,
            "start_intent_written": False,
        }

    def record_transient_failure(
        self,
        state: Mapping[str, Any],
        *,
        classification: Mapping[str, Any],
        observed_at: str | datetime,
        prepare_start_succeeded: bool = False,
    ) -> dict[str, Any]:
        """Back off, then probe forever after the fifth short failure."""

        current = self._state(state)
        now = _timestamp(observed_at)
        classified = _classification(classification, expected=TRANSIENT)
        if current["mode"] == "blocked_structural":
            raise SupervisorEpisodeError(
                "supervisor_structural_blocker_requires_recheck"
            )
        self._require_attempt_due(current, observed_at=now)
        if prepare_start_succeeded:
            current = self._reset_episode(
                current,
                observed_at=now,
                reason="prepare_start_succeeded",
            )
        episode = current["episode"]
        if current["mode"] == "probing":
            episode["probe_attempts"] += 1
            episode["next_attempt_at"] = (
                now + timedelta(seconds=PROBE_INTERVAL_SECONDS)
            ).isoformat()
            episode["last_transient_code"] = classified["machine_code"]
            current["alert_required"] = True
            return self._event(
                current,
                event_type="probe_transient_failure",
                observed_at=now,
                machine_code=classified["machine_code"],
                detail={
                    "probe_interval_seconds": PROBE_INTERVAL_SECONDS,
                    "next_attempt_at": episode["next_attempt_at"],
                },
            )
        failure_number = (
            int(episode["consecutive_transient_failures"]) + 1
        )
        if failure_number > SHORT_FAILURE_LIMIT:
            raise SupervisorEpisodeError(
                "supervisor_episode_state_invalid"
            )
        episode["consecutive_transient_failures"] = failure_number
        episode["last_transient_code"] = classified["machine_code"]
        episode["short_backoff_seconds"] = SHORT_BACKOFF_SECONDS[
            failure_number - 1
        ]
        if failure_number < SHORT_FAILURE_LIMIT:
            current["mode"] = "backing_off"
            episode["next_attempt_at"] = (
                now
                + timedelta(
                    seconds=episode["short_backoff_seconds"],
                )
            ).isoformat()
            return self._event(
                current,
                event_type="transient_backoff_scheduled",
                observed_at=now,
                machine_code=classified["machine_code"],
                detail={
                    "failure_number": failure_number,
                    "backoff_seconds": episode[
                        "short_backoff_seconds"
                    ],
                    "next_attempt_at": episode["next_attempt_at"],
                },
            )
        current["mode"] = "probing"
        current["alert_required"] = True
        episode["exhausted_at"] = now.isoformat()
        episode["next_attempt_at"] = (
            now + timedelta(seconds=PROBE_INTERVAL_SECONDS)
        ).isoformat()
        return self._event(
            current,
            event_type="episode_short_budget_exhausted",
            observed_at=now,
            machine_code=classified["machine_code"],
            event_label="episode_short_budget_exhausted",
            detail={
                "failure_number": failure_number,
                "final_short_backoff_seconds": episode[
                    "short_backoff_seconds"
                ],
                "probe_interval_seconds": PROBE_INTERVAL_SECONDS,
                "next_attempt_at": episode["next_attempt_at"],
                "cycle_terminated": False,
            },
        )

    def record_prepare_start_success(
        self,
        state: Mapping[str, Any],
        *,
        observed_at: str | datetime,
    ) -> dict[str, Any]:
        """A successful prepare proves the transient episode cleared."""

        current = self._state(state)
        now = _timestamp(observed_at)
        if current["mode"] == "blocked_structural":
            raise SupervisorEpisodeError(
                "supervisor_structural_blocker_requires_recheck"
            )
        self._require_attempt_due(current, observed_at=now)
        return self._reset_episode(
            current,
            observed_at=now,
            reason="prepare_start_succeeded",
        )

    def record_clean_refusal(
        self,
        state: Mapping[str, Any],
        *,
        classification: Mapping[str, Any],
        proof: CleanRefusalProof,
        observed_at: str | datetime,
        prepare_start_succeeded: bool,
    ) -> dict[str, Any]:
        """Count clean evidence only when all three authorities agree."""

        current = self._state(state)
        now = _timestamp(observed_at)
        classified = _classification(classification)
        if current["mode"] == "blocked_structural":
            raise SupervisorEpisodeError(
                "supervisor_structural_blocker_requires_recheck"
            )
        self._require_attempt_due(current, observed_at=now)
        if prepare_start_succeeded:
            current = self._reset_episode(
                current,
                observed_at=now,
                reason="prepare_start_succeeded",
            )
        if not proof.is_exact():
            return self.record_dangerous_outcome(
                current,
                machine_code="control_outcome_unknown",
                observed_at=now,
                detail={
                    "claimed_code": classified["machine_code"],
                    "clean_zero_order_proof": False,
                },
            )
        current["budgets"]["clean_refusal_observations"] += 1
        count = current["budgets"]["clean_refusal_observations"]
        current = self._event(
            current,
            event_type="clean_refusal_proven",
            observed_at=now,
            machine_code=classified["machine_code"],
            detail={
                "clean_refusal_observation": count,
                "proof_digest": proof.evidence_digest(),
                "dangerous_budget_consumed": False,
            },
        )
        if count == CLEAN_REFUSAL_WARNING_AT:
            current["warning_required"] = True
            current = self._event(
                current,
                event_type="clean_refusal_observation_budget_warning",
                observed_at=now,
                event_label=(
                    "clean_refusal_observation_budget_warning"
                ),
                detail={
                    "clean_refusal_observations": count,
                    "limit": CLEAN_REFUSAL_LIMIT,
                },
            )
        if classified["classification"] == STRUCTURAL:
            return self._structural(
                current,
                machine_code=classified["machine_code"],
                observed_at=now,
                detail={
                    "clean_zero_order_proof": True,
                    "dangerous_budget_consumed": False,
                },
            )
        return self.record_transient_failure(
            current,
            classification=classified,
            observed_at=now,
            prepare_start_succeeded=False,
        )

    def record_dangerous_outcome(
        self,
        state: Mapping[str, Any],
        *,
        machine_code: str,
        observed_at: str | datetime,
        detail: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Count an unknown/partial outcome and stop automatic retry."""

        current = self._state(state)
        now = _timestamp(observed_at)
        if current["mode"] == "blocked_structural":
            raise SupervisorEpisodeError(
                "supervisor_structural_blocker_requires_recheck"
            )
        if machine_code not in {
            "control_outcome_unknown",
            "partial_execution_or_cleanup_required",
        }:
            raise SupervisorEpisodeError(
                "supervisor_dangerous_outcome_invalid"
            )
        current["budgets"]["dangerous_start_attempts"] += 1
        count = current["budgets"]["dangerous_start_attempts"]
        if count > DANGEROUS_START_LIMIT:
            raise SupervisorEpisodeError(
                "supervisor_dangerous_budget_overrun"
            )
        return self._structural(
            current,
            machine_code=machine_code,
            observed_at=now,
            detail={
                **dict(detail or {}),
                "dangerous_start_attempts": count,
                "dangerous_start_limit": DANGEROUS_START_LIMIT,
                "automatic_retry_allowed": False,
            },
        )

    def record_structural_blocker(
        self,
        state: Mapping[str, Any],
        *,
        classification: Mapping[str, Any],
        observed_at: str | datetime,
        evidence: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = self._state(state)
        now = _timestamp(observed_at)
        classified = _classification(
            classification,
            expected=STRUCTURAL,
        )
        if (
            current["mode"] == "blocked_structural"
            and dict(current.get("blocker") or {}).get("machine_code")
            != classified["machine_code"]
        ):
            raise SupervisorEpisodeError(
                "supervisor_structural_blocker_requires_recheck"
            )
        detail: dict[str, Any] = {
            "automatic_retry_allowed": False,
        }
        if isinstance(evidence, Mapping):
            detail["evidence"] = dict(evidence)
        return self._structural(
            current,
            machine_code=classified["machine_code"],
            observed_at=now,
            detail=detail,
            blocker_evidence=evidence,
        )

    def recheck_structural_blocker(
        self,
        state: Mapping[str, Any],
        *,
        machine_code: str,
        condition_cleared: bool,
        observed_at: str | datetime,
        evidence: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Clear only a rechecked exact blocker; never replay a command."""

        current = self._state(state)
        now = _timestamp(observed_at)
        blocker = current.get("blocker")
        if (
            current["mode"] != "blocked_structural"
            or not isinstance(blocker, dict)
            or blocker.get("machine_code") != machine_code
        ):
            raise SupervisorEpisodeError(
                "supervisor_structural_recheck_identity_invalid"
            )
        if not condition_cleared:
            detail = {
                "condition_cleared": False,
                "control_actions_executed": 0,
            }
            if isinstance(evidence, Mapping):
                detail["evidence"] = dict(evidence)
            return self._event(
                current,
                event_type="structural_blocker_rechecked",
                observed_at=now,
                machine_code=machine_code,
                detail=detail,
            )
        if machine_code in {
            "dangerous_start_attempt_cap_reached",
            "clean_refusal_observation_cap_reached",
        }:
            raise SupervisorEpisodeError(
                "supervisor_cycle_budget_blocker_not_clearable"
            )
        resume_mode = str(blocker.get("resume_mode") or "ready")
        if resume_mode not in {"ready", "backing_off", "probing"}:
            raise SupervisorEpisodeError(
                "supervisor_episode_state_invalid"
            )
        current["mode"] = resume_mode
        current["blocker"] = None
        current["alert_required"] = resume_mode == "probing"
        detail = {
            "condition_cleared": True,
            "control_actions_executed": 0,
            "old_command_replayed": False,
            "resume_mode": resume_mode,
        }
        if isinstance(evidence, Mapping):
            detail["evidence"] = dict(evidence)
        return self._event(
            current,
            event_type="structural_blocker_cleared",
            observed_at=now,
            machine_code=machine_code,
            detail=detail,
        )

    def observe_heartbeat(
        self,
        state: Mapping[str, Any],
        *,
        status: str,
        observed_at: str | datetime,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Promote one continuous missing episode only after ten minutes."""

        current = self._state(state)
        now = _timestamp(observed_at)
        if status == "fresh":
            was_missing = current["heartbeat"]["missing_since"] is not None
            current["heartbeat"] = {
                "status": "fresh",
                "missing_since": None,
                "missing_episode_seconds": 0.0,
            }
            current = self._event(
                current,
                event_type="heartbeat_fresh",
                observed_at=now,
                detail={"missing_episode_cleared": was_missing},
            )
            return current, {
                "classifier_version": CLASSIFIER_VERSION,
                "machine_code": None,
                "classification": None,
            }
        if status != "missing":
            raise SupervisorEpisodeError(
                "supervisor_heartbeat_status_invalid"
            )
        heartbeat = current["heartbeat"]
        if heartbeat["missing_since"] is None:
            heartbeat["missing_since"] = now.isoformat()
        since = _timestamp(heartbeat["missing_since"])
        seconds = max(0.0, (now - since).total_seconds())
        heartbeat["status"] = "missing"
        heartbeat["missing_episode_seconds"] = seconds
        classified = classify_blocker(
            evidence={
                "tick_health": "missing",
                "tick_episode_seconds": seconds,
            }
        )
        current = self._event(
            current,
            event_type="heartbeat_missing_observed",
            observed_at=now,
            machine_code=classified["machine_code"],
            detail={"missing_episode_seconds": seconds},
        )
        if classified["classification"] == STRUCTURAL:
            current = self._structural(
                current,
                machine_code=classified["machine_code"],
                observed_at=now,
                detail={
                    "missing_episode_seconds": seconds,
                    "automatic_retry_allowed": False,
                },
            )
        return current, classified

    def attempt_is_due(
        self,
        state: Mapping[str, Any],
        *,
        observed_at: str | datetime,
    ) -> bool:
        current = self._state(state)
        if current["mode"] not in {"ready", "backing_off", "probing"}:
            return False
        next_attempt = current["episode"].get("next_attempt_at")
        if not next_attempt:
            return current["mode"] == "ready"
        return _timestamp(observed_at) >= _timestamp(next_attempt)

    @staticmethod
    def _require_attempt_due(
        state: Mapping[str, Any],
        *,
        observed_at: datetime,
    ) -> None:
        if state.get("mode") not in {"backing_off", "probing"}:
            return
        next_attempt = dict(state.get("episode") or {}).get(
            "next_attempt_at"
        )
        if (
            not next_attempt
            or observed_at < _timestamp(next_attempt)
        ):
            raise SupervisorEpisodeError(
                "supervisor_attempt_not_due"
            )

    def _state(self, state: Mapping[str, Any]) -> dict[str, Any]:
        try:
            current = json.loads(_canonical(dict(state)))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SupervisorEpisodeError(
                "supervisor_episode_state_invalid"
            ) from exc
        if (
            current.get("schema_version") != STATE_SCHEMA_VERSION
            or current.get("mode") not in _MODES
            or not isinstance(current.get("episode"), dict)
            or not isinstance(current.get("budgets"), dict)
            or not isinstance(current.get("heartbeat"), dict)
            or not isinstance(current.get("events"), list)
        ):
            raise SupervisorEpisodeError(
                "supervisor_episode_state_invalid"
            )
        _cycle_id(current.get("cycle_id"))
        episode = current["episode"]
        budgets = current["budgets"]
        failures = episode.get("consecutive_transient_failures")
        generation = episode.get("generation")
        probe_attempts = episode.get("probe_attempts")
        dangerous = budgets.get("dangerous_start_attempts")
        clean = budgets.get("clean_refusal_observations")
        if (
            not isinstance(failures, int)
            or not 0 <= failures <= SHORT_FAILURE_LIMIT
            or not isinstance(generation, int)
            or generation < 1
            or not isinstance(probe_attempts, int)
            or probe_attempts < 0
            or not isinstance(dangerous, int)
            or not 0 <= dangerous <= DANGEROUS_START_LIMIT
            or not isinstance(clean, int)
            or not 0 <= clean <= CLEAN_REFUSAL_LIMIT
            or budgets.get("dangerous_start_limit")
            != DANGEROUS_START_LIMIT
            or budgets.get("clean_refusal_limit")
            != CLEAN_REFUSAL_LIMIT
            or budgets.get("clean_refusal_warning_at")
            != CLEAN_REFUSAL_WARNING_AT
        ):
            raise SupervisorEpisodeError(
                "supervisor_episode_state_invalid"
            )
        if (
            current["mode"] == "probing"
            and (
                failures != SHORT_FAILURE_LIMIT
                or episode.get("exhausted_at") is None
                or episode.get("next_attempt_at") is None
            )
        ):
            raise SupervisorEpisodeError(
                "supervisor_episode_state_invalid"
            )
        for sequence, event in enumerate(current["events"], start=1):
            if (
                not isinstance(event, dict)
                or event.get("schema_version") != EVENT_SCHEMA_VERSION
                or event.get("sequence") != sequence
                or event.get("cycle_id") != current["cycle_id"]
            ):
                raise SupervisorEpisodeError(
                    "supervisor_episode_state_invalid"
                )
            supplied = str(event.get("event_digest") or "")
            if supplied != _digest(
                {
                    key: value
                    for key, value in event.items()
                    if key != "event_digest"
                }
            ):
                raise SupervisorEpisodeError(
                    "supervisor_episode_state_invalid"
                )
        blocker = current.get("blocker")
        if current["mode"] == "blocked_structural":
            if (
                not isinstance(blocker, dict)
                or blocker.get("classification") != STRUCTURAL
                or blocker.get("machine_code")
                not in STRUCTURAL_MACHINE_CODES
            ):
                raise SupervisorEpisodeError(
                    "supervisor_episode_state_invalid"
                )
            evidence = blocker.get("evidence")
            if evidence is not None and (
                blocker.get("machine_code")
                != "outer_strategy_policy_envelope_out_of_bounds"
                or not isinstance(evidence, dict)
                or set(evidence)
                != {"rejection_id", "rejection_digest"}
                or _identity_or_none(evidence.get("rejection_id"))
                is None
                or _digest_or_none(
                    evidence.get("rejection_digest")
                )
                is None
            ):
                raise SupervisorEpisodeError(
                    "supervisor_episode_state_invalid"
                )
        elif blocker is not None:
            raise SupervisorEpisodeError(
                "supervisor_episode_state_invalid"
            )
        return current

    @staticmethod
    def _new_episode(
        *,
        generation: int,
        now: datetime,
    ) -> dict[str, Any]:
        return {
            "episode_id": f"supervisor-episode-{uuid.uuid4().hex}",
            "generation": generation,
            "started_at": now.isoformat(),
            "consecutive_transient_failures": 0,
            "short_failure_limit": SHORT_FAILURE_LIMIT,
            "short_backoff_schedule_seconds": list(
                SHORT_BACKOFF_SECONDS
            ),
            "short_backoff_seconds": None,
            "last_transient_code": None,
            "exhausted_at": None,
            "probe_attempts": 0,
            "probe_interval_seconds": PROBE_INTERVAL_SECONDS,
            "next_attempt_at": None,
        }

    def _reset_episode(
        self,
        state: dict[str, Any],
        *,
        observed_at: datetime,
        reason: str,
    ) -> dict[str, Any]:
        previous = dict(state["episode"])
        state["episode"] = self._new_episode(
            generation=int(previous["generation"]) + 1,
            now=observed_at,
        )
        state["mode"] = "ready"
        state["alert_required"] = False
        return self._event(
            state,
            event_type="episode_reset",
            observed_at=observed_at,
            detail={
                "reason": reason,
                "previous_episode_id": previous["episode_id"],
                "previous_failure_count": previous[
                    "consecutive_transient_failures"
                ],
                "previous_probe_mode": (
                    previous["exhausted_at"] is not None
                ),
            },
        )

    def _structural(
        self,
        state: dict[str, Any],
        *,
        machine_code: str,
        observed_at: datetime,
        detail: Mapping[str, Any],
        blocker_evidence: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if machine_code not in STRUCTURAL_MACHINE_CODES:
            machine_code = "unknown_blocker"
        existing_blocker = state.get("blocker")
        resume_mode = (
            str(existing_blocker.get("resume_mode") or "ready")
            if state.get("mode") == "blocked_structural"
            and isinstance(existing_blocker, dict)
            else str(state.get("mode") or "ready")
        )
        if resume_mode not in {"ready", "backing_off", "probing"}:
            resume_mode = "ready"
        state["mode"] = "blocked_structural"
        state["alert_required"] = True
        state["blocker"] = {
            "classifier_version": CLASSIFIER_VERSION,
            "machine_code": machine_code,
            "classification": STRUCTURAL,
            "blocked_at": observed_at.isoformat(),
            "automatic_retry_allowed": False,
            "resume_mode": resume_mode,
        }
        if isinstance(blocker_evidence, Mapping):
            state["blocker"]["evidence"] = dict(
                blocker_evidence
            )
        return self._event(
            state,
            event_type="structural_blocked",
            observed_at=observed_at,
            machine_code=machine_code,
            detail=dict(detail),
        )

    @staticmethod
    def _guard_decision(state: Mapping[str, Any]) -> dict[str, Any]:
        blocker = dict(state.get("blocker") or {})
        return {
            "allowed": False,
            "machine_code": blocker["machine_code"],
            "classification": STRUCTURAL,
            "start_intent_written": False,
        }

    @staticmethod
    def _event(
        state: dict[str, Any],
        *,
        event_type: str,
        observed_at: datetime,
        machine_code: str | None = None,
        event_label: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        sequence = len(state["events"]) + 1
        event = {
            "schema_version": EVENT_SCHEMA_VERSION,
            "sequence": sequence,
            "event_type": str(event_type),
            "cycle_id": state["cycle_id"],
            "observed_at": observed_at.isoformat(),
            "machine_code": machine_code,
            "event_label": event_label,
            "detail": dict(detail or {}),
        }
        event["event_digest"] = _digest(event)
        state["events"].append(event)
        return state


def classify_attempt_deadline(
    *,
    start_intent_persisted: bool,
) -> dict[str, Any]:
    """Keep pre-intent local pressure distinct from post-intent ambiguity."""

    if not isinstance(start_intent_persisted, bool):
        raise SupervisorEpisodeError(
            "supervisor_deadline_evidence_invalid"
        )
    return classify_blocker(
        evidence={
            "local_attempt_deadline": "exceeded",
            "start_intent_persisted": bool(start_intent_persisted),
        }
    )


def classify_upstream_timeout(
    *,
    source_failure: str,
    market_trusted: bool,
) -> dict[str, Any]:
    """Classify only the approved typed external failure evidence."""

    return classify_blocker(
        evidence={
            "source_failure": source_failure,
            "market_trusted": market_trusted,
        }
    )


def _classification(
    value: Mapping[str, Any],
    *,
    expected: str | None = None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SupervisorEpisodeError(
            "supervisor_classification_invalid"
        )
    classified = dict(value)
    machine_code = str(classified.get("machine_code") or "")
    classification = str(classified.get("classification") or "")
    expected_codes = (
        TRANSIENT_MACHINE_CODES
        if classification == TRANSIENT
        else STRUCTURAL_MACHINE_CODES
        if classification == STRUCTURAL
        else frozenset()
    )
    if (
        classified.get("classifier_version") != CLASSIFIER_VERSION
        or machine_code not in expected_codes
        or (expected is not None and classification != expected)
    ):
        raise SupervisorEpisodeError(
            "supervisor_classification_invalid"
        )
    return classified


def _cycle_id(value: Any) -> str:
    cycle = str(value or "")
    suffix = (
        "_DAY"
        if cycle.endswith("_DAY")
        else "_NIGHT"
        if cycle.endswith("_NIGHT")
        else ""
    )
    if not suffix:
        raise SupervisorEpisodeError("supervisor_cycle_id_invalid")
    date_text = cycle[: -len(suffix)]
    try:
        parsed = datetime.fromisoformat(date_text)
    except ValueError as exc:
        raise SupervisorEpisodeError(
            "supervisor_cycle_id_invalid"
        ) from exc
    if (
        len(date_text) != 10
        or parsed.time().isoformat() != "00:00:00"
        or cycle != f"{parsed.date().isoformat()}{suffix}"
    ):
        raise SupervisorEpisodeError("supervisor_cycle_id_invalid")
    return cycle


def _timestamp(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(
                str(value).replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise SupervisorEpisodeError(
                "supervisor_timestamp_invalid"
            ) from exc
    if parsed.tzinfo is None:
        raise SupervisorEpisodeError("supervisor_timestamp_invalid")
    return parsed.astimezone(timezone.utc)


def _identity_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    if (
        not text
        or len(text) > 256
        or any(
            character
            not in (
                "abcdefghijklmnopqrstuvwxyz"
                "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                "0123456789"
                "._:-"
            )
            for character in text
        )
    ):
        return None
    return text


def _digest_or_none(value: Any) -> str | None:
    text = str(value or "").lower()
    if len(text) != 64 or any(
        character not in "0123456789abcdef" for character in text
    ):
        return None
    return text


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()
