"""Immutable next-cycle Paper candidate staging outside the boundary path.

The pre-generator may call the AI provider while the current cycle is proven
running.  It can persist proposals, previews, envelope decisions, and this
artifact, but it never activates a plan, prepares a start, or mutates runtime,
orders, or positions.  The deterministic current-market/authoritative-equity
builder is the boundary guarantee; a valid artifact is an optional enhancement
and provider failure is explicitly non-blocking.  At the boundary the
Supervisor may adopt an exact valid artifact or records why it used the
deterministic Paper recovery builder.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
import tempfile
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from services.dualtrack_clock import cycle_window, cycle_window_from_id, parse_utc
from services.cloud_ai_provider import (
    CloudAIProviderReadinessGateError,
    PROVIDER_READINESS_INVALID,
    PROVIDER_READINESS_UNAVAILABLE,
    RECOVERABLE_PROVIDER_CODES,
)
from services.journal_store import load_json
from services.paper_start_facts import (
    PaperStartFactsError,
    same_execution_authority,
    validate_start_facts,
)
from services.paper_supervisor_evidence import (
    RunningEvidenceError,
    validate_running_evidence,
)
from services.paper_supervisor_heartbeat import validate_complete_tick_heartbeat
from services.paper_supervisor_store import PaperSupervisorStore, SupervisorStoreError
from services.supervisor_execution_profile import PAPER_CONTINUOUS


VERIFIED_WAITING_SCHEMA_VERSION = "paper-next-cycle-verified-waiting-v1"
BOUNDARY_EVENT_SCHEMA_VERSION = "paper-next-cycle-boundary-event-v1"
_CYCLE_ID = re.compile(r"^\d{4}-\d{2}-\d{2}_(DAY|NIGHT)$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")
_MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
_OPTIONAL_PROVIDER_CODES = frozenset(
    {
        *RECOVERABLE_PROVIDER_CODES,
        "strategy_recommendation_provider_missing",
        "strategy_recommendation_provider_not_executable",
        "strategy_recommendation_provider_invalid_output",
        "strategy_recommendation_provider_command_invalid",
        "strategy_recommendation_provider_timeout_invalid",
        "strategy_recommendation_provider_auth_not_ready",
        PROVIDER_READINESS_INVALID,
        PROVIDER_READINESS_UNAVAILABLE,
    }
)
_PROCESS_LOCKS: dict[str, threading.Lock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()
_ARTIFACT_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "current_cycle_id",
        "target_cycle_id",
        "generated_at",
        "validated_at",
        "valid_from",
        "expires_at",
        "execution_profile",
        "source",
        "proposal",
        "preview",
        "candidate",
        "plan",
        "envelope",
        "start_facts_digest",
        "outer_policy",
        "provider_call_count",
        "operations",
        "artifact_digest",
    }
)
_EVENT_FIELDS = frozenset(
    {
        "schema_version",
        "event_id",
        "target_cycle_id",
        "artifact_digest",
        "outcome",
        "reason",
        "observed_at",
        "current_start_facts_digest",
        "boundary_ai_provider_calls",
        "deterministic_rebuild_used",
        "event_digest",
    }
)


class NextCyclePlanError(ValueError):
    """Stable fail-closed signal for staged-candidate evidence."""


def _optional_provider_failure(exc: Exception) -> tuple[str, int] | None:
    """Return only explicitly whitelisted, non-blocking pre-generation failures."""

    code = str(getattr(exc, "code", "") or "")
    if code not in _OPTIONAL_PROVIDER_CODES:
        return None
    provider_calls = 0 if isinstance(exc, CloudAIProviderReadinessGateError) else 1
    return code, provider_calls


class VerifiedWaitingPlanStore:
    """Persist one content-addressed waiting candidate per target cycle."""

    def __init__(self, output_root: Path) -> None:
        self.output_root = Path(output_root)
        self.root = (
            self.output_root
            / "dualtrack"
            / "supervisor"
            / "next_cycle_plans"
        )

    def candidate_lock(self, target_cycle_id: str):
        self._require_root()
        return _CandidateLock(self.root, _cycle_id(target_cycle_id))

    def record(self, value: Mapping[str, Any]) -> dict[str, Any]:
        self._require_root()
        candidate = validate_verified_waiting_plan(value)
        path = self._artifact_path(candidate["target_cycle_id"])
        rows = self._artifact_rows(candidate["target_cycle_id"])
        if rows:
            existing = rows[0]
            if existing != candidate:
                raise NextCyclePlanError(
                    "next_cycle_verified_plan_identity_conflict"
                )
            return existing
        _atomic_write_json_fsync(path, [candidate])
        return candidate

    def load(self, target_cycle_id: str) -> dict[str, Any] | None:
        rows = self._artifact_rows(target_cycle_id)
        return rows[0] if rows else None

    def record_boundary_event(
        self,
        *,
        target_cycle_id: str,
        artifact_digest: str | None,
        outcome: str,
        reason: str,
        observed_at: str,
        current_start_facts_digest: str | None,
        boundary_ai_provider_calls: int,
        deterministic_rebuild_used: bool,
    ) -> dict[str, Any]:
        cycle = _cycle_id(target_cycle_id)
        timestamp = _timestamp(observed_at)
        base = {
            "schema_version": BOUNDARY_EVENT_SCHEMA_VERSION,
            "target_cycle_id": cycle,
            "artifact_digest": (
                _required_digest(artifact_digest)
                if artifact_digest
                else None
            ),
            "outcome": _required_text(outcome),
            "reason": _required_text(reason),
            "observed_at": timestamp,
            "current_start_facts_digest": (
                _required_digest(current_start_facts_digest)
                if current_start_facts_digest
                else None
            ),
            "boundary_ai_provider_calls": _nonnegative_int(
                boundary_ai_provider_calls
            ),
            "deterministic_rebuild_used": bool(
                deterministic_rebuild_used
            ),
        }
        base["event_id"] = "next-cycle-boundary-" + _digest(base)[:24]
        event = {**base, "event_digest": _digest(base)}
        validate_boundary_event(event, target_cycle_id=cycle)
        path = self._events_path(cycle)
        rows = self.boundary_events(cycle)
        for existing in rows:
            if existing["event_id"] != event["event_id"]:
                continue
            if existing != event:
                raise NextCyclePlanError(
                    "next_cycle_boundary_event_identity_conflict"
                )
            return existing
        _atomic_write_json_fsync(path, [*rows, event])
        return event

    def boundary_events(self, target_cycle_id: str) -> list[dict[str, Any]]:
        self._require_root()
        cycle = _cycle_id(target_cycle_id)
        path = self._events_path(cycle)
        events_root = path.parent
        if events_root.exists() and (
            events_root.is_symlink() or not events_root.is_dir()
        ):
            raise NextCyclePlanError(
                "next_cycle_boundary_event_store_corrupt"
            )
        rows = _load_bounded_rows(
            path,
            corrupt_code="next_cycle_boundary_event_store_corrupt",
        )
        return [
            validate_boundary_event(row, target_cycle_id=cycle)
            for row in rows
        ]

    def projection(self, target_cycle_id: str) -> dict[str, Any]:
        cycle = _cycle_id(target_cycle_id)
        try:
            artifact = self.load(cycle)
            events = self.boundary_events(cycle)
        except NextCyclePlanError as exc:
            return {
                "status": "unavailable",
                "target_cycle_id": cycle,
                "reason": str(exc),
                "artifact_digest": None,
                "boundary_events": [],
            }
        if artifact is None:
            return {
                "status": "missing",
                "target_cycle_id": cycle,
                "reason": "next_cycle_verified_plan_missing",
                "artifact_digest": None,
                "boundary_events": events,
            }
        return {
            "status": artifact["status"],
            "target_cycle_id": cycle,
            "generated_at": artifact["generated_at"],
            "validated_at": artifact["validated_at"],
            "valid_from": artifact["valid_from"],
            "expires_at": artifact["expires_at"],
            "proposal_id": artifact["proposal"]["proposal_id"],
            "preview_id": artifact["preview"]["preview_id"],
            "strategy_plan_id": artifact["plan"]["strategy_plan_id"],
            "envelope_authorization_id": artifact["envelope"][
                "envelope_authorization_id"
            ],
            "start_facts_digest": artifact["start_facts_digest"],
            "artifact_digest": artifact["artifact_digest"],
            "provider_call_count": artifact["provider_call_count"],
            "boundary_events": events,
        }

    def _artifact_rows(self, target_cycle_id: str) -> list[dict[str, Any]]:
        self._require_root()
        cycle = _cycle_id(target_cycle_id)
        rows = _load_bounded_rows(
            self._artifact_path(cycle),
            corrupt_code="next_cycle_verified_plan_corrupt",
        )
        if len(rows) > 1:
            raise NextCyclePlanError("next_cycle_verified_plan_corrupt")
        return [validate_verified_waiting_plan(row) for row in rows]

    def _artifact_path(self, target_cycle_id: str) -> Path:
        return self.root / f"{_cycle_id(target_cycle_id)}.json"

    def _events_path(self, target_cycle_id: str) -> Path:
        return self.root / "boundary_events" / f"{_cycle_id(target_cycle_id)}.json"

    def _require_root(self) -> None:
        if self.root.exists() and (
            self.root.is_symlink() or not self.root.is_dir()
        ):
            raise NextCyclePlanError("next_cycle_verified_plan_corrupt")


class NextCyclePlanPrecomputer:
    """Generate at most one verified waiting candidate in the lead window."""

    def __init__(
        self,
        output_root: Path,
        *,
        plane: Any,
        candidate_builder: Callable[[str, str], Mapping[str, Any]],
        execution_profile: str,
        lead_minutes: int = 60,
        store: VerifiedWaitingPlanStore | None = None,
        supervisor_store: PaperSupervisorStore | None = None,
        heartbeat_provider: Callable[[str], Mapping[str, Any] | None] | None = None,
    ) -> None:
        if not 5 <= int(lead_minutes) <= 360:
            raise NextCyclePlanError("next_cycle_precompute_configuration_invalid")
        self.output_root = Path(output_root)
        self.plane = plane
        self.candidate_builder = candidate_builder
        self.execution_profile = str(execution_profile)
        self.lead_minutes = int(lead_minutes)
        self.store = store or VerifiedWaitingPlanStore(self.output_root)
        self.supervisor_store = supervisor_store or PaperSupervisorStore(
            self.output_root
        )
        self.heartbeat_provider = heartbeat_provider or self._latest_heartbeat

    def run(self, *, observed_at: str) -> dict[str, Any]:
        observed = parse_utc(observed_at)
        current = cycle_window(observed)
        target = cycle_window(current.end)
        base = {
            "current_cycle_id": current.cycle_id,
            "target_cycle_id": target.cycle_id,
            "observed_at": observed.isoformat(),
            "control_actions_executed": 0,
            "orders_created": 0,
            "plans_activated": 0,
            "prepared_starts_created": 0,
        }
        if self.execution_profile != PAPER_CONTINUOUS:
            return {**base, "status": "disabled_fail_closed_profile"}
        seconds_to_boundary = (current.end - observed).total_seconds()
        if seconds_to_boundary > self.lead_minutes * 60:
            return {
                **base,
                "status": "not_due",
                "seconds_to_boundary": int(seconds_to_boundary),
            }
        self._require_current_running(current.cycle_id)
        heartbeat = validate_complete_tick_heartbeat(
            self.heartbeat_provider(current.cycle_id),
            cycle_id=current.cycle_id,
            observed_at=observed,
        )
        if heartbeat.get("status") != "ready":
            raise NextCyclePlanError(str(heartbeat["machine_code"]))
        self.plane.verify_supervisor_outer_policy()

        with self.store.candidate_lock(target.cycle_id):
            existing = self.store.load(target.cycle_id)
            if existing is not None:
                return {
                    **base,
                    "status": "already_verified",
                    "artifact": existing,
                    "provider_call_count": 0,
                }
            before_plan = self.plane.active_plan(target.cycle_id)
            if before_plan is not None:
                raise NextCyclePlanError(
                    "next_cycle_plan_activation_precedes_boundary"
                )
            takeover_from = ""
            persisted_runtime = getattr(
                self.plane,
                "persisted_runtime_state",
                None,
            )
            if callable(persisted_runtime):
                runtime = dict(persisted_runtime() or {})
                if (
                    str(runtime.get("cycle_id") or "") == current.cycle_id
                    and str(runtime.get("desired_state") or "") == "running"
                    and str(runtime.get("actual_state") or "") == "running"
                ):
                    takeover_from = str(runtime.get("strategy_plan_id") or "")
            try:
                evaluation = dict(
                    self.candidate_builder(
                        target.cycle_id,
                        observed.isoformat(),
                    )
                )
            except Exception as exc:  # noqa: BLE001 - explicit provider allowlist only.
                optional_failure = _optional_provider_failure(exc)
                if optional_failure is None:
                    raise
                machine_code, provider_call_count = optional_failure
                return {
                    **base,
                    "status": "enhancement_unavailable",
                    "enhancement": "ai_next_cycle_precompute",
                    "non_blocking": True,
                    "machine_code": machine_code,
                    "provider_call_count": provider_call_count,
                    "next_action": "deterministic_rebuild_at_boundary",
                }
            proposal = _json_mapping(evaluation.get("proposal"))
            preview = _json_mapping(evaluation.get("preview"))
            candidate = self.plane.supervisor_candidate_identity(
                target.cycle_id,
                proposal=proposal,
                preview=preview,
            )
            envelope = self.plane.authorize_supervisor_ai_envelope(
                target.cycle_id,
                proposal=proposal,
                preview=preview,
            )
            envelope_id = _required_text(
                envelope.get("envelope_authorization_id")
            )
            projection_kwargs = {
                "cycle_risk_envelope_id": envelope_id,
                "now": observed.isoformat(),
            }
            if takeover_from:
                projection_kwargs["takeover_from_strategy_plan_id"] = (
                    takeover_from
                )
            strategy_type = str(
                proposal.get("strategy_type") or "grid"
            ).lower()
            if strategy_type == "dca":
                projected_plan = self.plane.project_dca_production_plan(
                    target.cycle_id,
                    selected_proposal_id=str(proposal["proposal_id"]),
                    preview=preview,
                    **projection_kwargs,
                )
            else:
                projected_plan = self.plane.project_production_plan(
                    target.cycle_id,
                    selected_proposal_id=str(proposal["proposal_id"]),
                    **projection_kwargs,
                )
                self.plane.risk_envelopes.verify_candidate_plan_identity(
                    cycle_id=target.cycle_id,
                    envelope_authorization_id=envelope_id,
                    plan=projected_plan,
                )
            if self.plane.active_plan(target.cycle_id) is not None:
                raise NextCyclePlanError(
                    "next_cycle_plan_activation_precedes_boundary"
                )
            start_facts_digest = _required_digest(
                candidate.get("start_facts_digest")
            )
            try:
                facts = self.plane.start_facts.require(
                    target.cycle_id,
                    start_facts_digest,
                )
            except PaperStartFactsError as exc:
                raise NextCyclePlanError(str(exc)) from exc
            artifact = build_verified_waiting_plan(
                current_cycle_id=current.cycle_id,
                target_cycle_id=target.cycle_id,
                generated_at=observed.isoformat(),
                validated_at=observed.isoformat(),
                valid_from=target.start.isoformat(),
                expires_at=target.end.isoformat(),
                execution_profile=self.execution_profile,
                start_facts=facts,
                candidate_identity=candidate,
                projected_plan=projected_plan,
                envelope=envelope,
            )
            stored = self.store.record(artifact)
            return {
                **base,
                "status": "verified_waiting",
                "artifact": stored,
                "provider_call_count": 1,
            }

    def _require_current_running(self, cycle_id: str) -> None:
        try:
            observations = self.supervisor_store.observations(cycle_id)
            evidence = (
                dict(observations[-1].get("payload", {}).get("running_evidence") or {})
                if observations
                else {}
            )
            validated = validate_running_evidence(evidence)
        except (
            OSError,
            SupervisorStoreError,
            RunningEvidenceError,
            TypeError,
            ValueError,
        ) as exc:
            raise NextCyclePlanError(
                "next_cycle_current_not_running_proven"
            ) from exc
        if (
            validated.get("cycle_id") != cycle_id
            or validated.get("running_proven") is not True
        ):
            raise NextCyclePlanError(
                "next_cycle_current_not_running_proven"
            )

    def _latest_heartbeat(self, cycle_id: str) -> Mapping[str, Any] | None:
        rows = load_json(
            self.output_root
            / "dualtrack"
            / "runner"
            / f"{_cycle_id(cycle_id)}.json"
        )
        return rows[-1] if rows and isinstance(rows[-1], Mapping) else None


def build_verified_waiting_plan(
    *,
    current_cycle_id: str,
    target_cycle_id: str,
    generated_at: str,
    validated_at: str,
    valid_from: str,
    expires_at: str,
    execution_profile: str,
    start_facts: Mapping[str, Any],
    candidate_identity: Mapping[str, Any],
    projected_plan: Mapping[str, Any],
    envelope: Mapping[str, Any],
) -> dict[str, Any]:
    facts = validate_start_facts(start_facts)
    candidate = _json_mapping(candidate_identity)
    plan = _json_mapping(projected_plan)
    envelope_row = _json_mapping(envelope)
    if (
        facts["cycle_id"] != _cycle_id(target_cycle_id)
        or candidate.get("start_facts_digest")
        != facts["start_facts_digest"]
        or str(plan.get("cycle_id") or "") != target_cycle_id
        or str(envelope_row.get("cycle_id") or "") != target_cycle_id
    ):
        raise NextCyclePlanError("next_cycle_verified_plan_identity_conflict")
    plan_record = {
        "strategy_plan_id": _required_text(
            plan.get("strategy_plan_id")
        ),
        "version": _positive_int(plan.get("version")),
        "strategy_type": _required_text(
            plan.get("strategy_type") or "grid"
        ),
        "content_digest": plan_content_digest(plan),
        "projected": plan,
    }
    takeover_from = str(plan.get("takeover_from_strategy_plan_id") or "")
    if takeover_from:
        plan_record["takeover_from_strategy_plan_id"] = takeover_from
    record: dict[str, Any] = {
        "schema_version": VERIFIED_WAITING_SCHEMA_VERSION,
        "status": "verified_waiting",
        "current_cycle_id": _cycle_id(current_cycle_id),
        "target_cycle_id": _cycle_id(target_cycle_id),
        "generated_at": _timestamp(generated_at),
        "validated_at": _timestamp(validated_at),
        "valid_from": _timestamp(valid_from),
        "expires_at": _timestamp(expires_at),
        "execution_profile": _required_text(execution_profile),
        "source": _json_mapping(facts["source"]),
        "proposal": {
            "proposal_id": _required_text(candidate.get("proposal_id")),
            "proposal_digest": _required_digest(
                candidate.get("proposal_digest")
            ),
        },
        "preview": {
            "preview_id": _required_text(candidate.get("preview_id")),
            "preview_digest": _required_digest(
                candidate.get("preview_digest")
            ),
        },
        "candidate": candidate,
        "plan": plan_record,
        "envelope": {
            "envelope_authorization_id": _required_text(
                envelope_row.get("envelope_authorization_id")
            ),
            "authorization_digest": _required_digest(
                envelope_row.get("authorization_digest")
            ),
        },
        "start_facts_digest": facts["start_facts_digest"],
        "outer_policy": _json_mapping(facts["outer_policy"]),
        "provider_call_count": 1,
        "operations": {
            "control_actions_executed": 0,
            "orders_created": 0,
            "positions_created": 0,
            "plans_activated": 0,
            "prepared_starts_created": 0,
        },
    }
    record["artifact_digest"] = _digest(record)
    return validate_verified_waiting_plan(record)


def validate_verified_waiting_plan(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    row = _json_mapping(value)
    if (
        set(row) != _ARTIFACT_FIELDS
        or row.get("schema_version") != VERIFIED_WAITING_SCHEMA_VERSION
        or row.get("status") != "verified_waiting"
        or row.get("execution_profile") != PAPER_CONTINUOUS
        or row.get("provider_call_count") != 1
    ):
        raise NextCyclePlanError("next_cycle_verified_plan_corrupt")
    current = _cycle_id(row.get("current_cycle_id"))
    target = _cycle_id(row.get("target_cycle_id"))
    if cycle_window(cycle_window_from_id(current).end).cycle_id != target:
        raise NextCyclePlanError("next_cycle_verified_plan_wrong_cycle")
    generated = parse_utc(_timestamp(row.get("generated_at")))
    validated = parse_utc(_timestamp(row.get("validated_at")))
    valid_from = parse_utc(_timestamp(row.get("valid_from")))
    expires = parse_utc(_timestamp(row.get("expires_at")))
    target_window = cycle_window_from_id(target)
    if (
        generated > validated
        or validated >= valid_from
        or valid_from != target_window.start
        or expires != target_window.end
    ):
        raise NextCyclePlanError("next_cycle_verified_plan_corrupt")
    source = _json_mapping(row.get("source"))
    if (
        set(source) != {"source_sha", "source_tree_sha", "tracked_tree_clean"}
        or not _SOURCE_SHA.fullmatch(str(source.get("source_sha") or ""))
        or not _SOURCE_SHA.fullmatch(str(source.get("source_tree_sha") or ""))
        or source.get("tracked_tree_clean") is not True
    ):
        raise NextCyclePlanError("next_cycle_verified_plan_wrong_source")
    proposal = _json_mapping(row.get("proposal"))
    preview = _json_mapping(row.get("preview"))
    plan = _json_mapping(row.get("plan"))
    envelope = _json_mapping(row.get("envelope"))
    candidate = _json_mapping(row.get("candidate"))
    expected_candidate_fields = {
        "proposal_id",
        "proposal_digest",
        "preview_id",
        "preview_digest",
        "facts_digest",
        "confirmation_digest",
        "strategy_type",
        "direction",
        "limits",
        "start_facts_digest",
    }
    plan_fields = {
        "strategy_plan_id",
        "version",
        "strategy_type",
        "content_digest",
        "projected",
    }
    projected = _json_mapping(plan.get("projected"))
    takeover_from = str(
        projected.get("takeover_from_strategy_plan_id") or ""
    )
    if takeover_from:
        plan_fields.add("takeover_from_strategy_plan_id")
    if (
        set(proposal) != {"proposal_id", "proposal_digest"}
        or set(preview) != {"preview_id", "preview_digest"}
        or set(plan) != plan_fields
        or set(envelope)
        != {"envelope_authorization_id", "authorization_digest"}
        or str(candidate.get("proposal_id") or "")
        != str(proposal.get("proposal_id") or "")
        or str(candidate.get("proposal_digest") or "")
        != str(proposal.get("proposal_digest") or "")
        or str(candidate.get("preview_id") or "")
        != str(preview.get("preview_id") or "")
        or str(candidate.get("preview_digest") or "")
        != str(preview.get("preview_digest") or "")
        or str(candidate.get("start_facts_digest") or "")
        != str(row.get("start_facts_digest") or "")
        or set(candidate) != expected_candidate_fields
        or candidate.get("strategy_type") not in {"grid", "dca"}
        or candidate.get("direction") not in {"long", "neutral", "short"}
        or not isinstance(candidate.get("limits"), Mapping)
    ):
        raise NextCyclePlanError("next_cycle_verified_plan_corrupt")
    _required_text(proposal.get("proposal_id"))
    _required_digest(proposal.get("proposal_digest"))
    _required_text(preview.get("preview_id"))
    _required_digest(preview.get("preview_digest"))
    if (
        _required_text(plan.get("strategy_plan_id"))
        != str(projected.get("strategy_plan_id") or "")
        or _positive_int(plan.get("version"))
        != int(projected.get("version") or 0)
        or _required_text(plan.get("strategy_type"))
        != str(projected.get("strategy_type") or "grid")
        or _required_digest(plan.get("content_digest"))
        != plan_content_digest(projected)
        or str(projected.get("cycle_id") or "") != target
        or str(row.get("start_facts_digest") or "")
        != str(projected.get("start_facts_digest") or "")
        or str(proposal.get("proposal_id") or "")
        not in {
            str(value)
            for value in projected.get("source_proposal_ids") or []
        }
        or str(envelope.get("envelope_authorization_id") or "")
        != str(projected.get("cycle_risk_envelope_id") or "")
        or str(plan.get("takeover_from_strategy_plan_id") or "")
        != takeover_from
    ):
        raise NextCyclePlanError("next_cycle_verified_plan_corrupt")
    _required_text(envelope.get("envelope_authorization_id"))
    _required_digest(envelope.get("authorization_digest"))
    _required_digest(row.get("start_facts_digest"))
    _json_mapping(row.get("outer_policy"))
    operations = _json_mapping(row.get("operations"))
    if operations != {
        "control_actions_executed": 0,
        "orders_created": 0,
        "positions_created": 0,
        "plans_activated": 0,
        "prepared_starts_created": 0,
    }:
        raise NextCyclePlanError("next_cycle_verified_plan_side_effect")
    supplied = _required_digest(row.get("artifact_digest"))
    expected = _digest(
        {key: item for key, item in row.items() if key != "artifact_digest"}
    )
    if not hmac.compare_digest(supplied, expected):
        raise NextCyclePlanError("next_cycle_verified_plan_corrupt")
    return row


def validate_boundary_event(
    value: Mapping[str, Any],
    *,
    target_cycle_id: str,
) -> dict[str, Any]:
    row = _json_mapping(value)
    if (
        set(row) != _EVENT_FIELDS
        or row.get("schema_version") != BOUNDARY_EVENT_SCHEMA_VERSION
        or row.get("target_cycle_id") != _cycle_id(target_cycle_id)
        or row.get("outcome") not in {"adopted", "invalidated", "missing", "blocked"}
        or not isinstance(row.get("deterministic_rebuild_used"), bool)
        or _nonnegative_int(row.get("boundary_ai_provider_calls")) != 0
    ):
        raise NextCyclePlanError("next_cycle_boundary_event_store_corrupt")
    _required_text(row.get("event_id"))
    _required_text(row.get("reason"))
    _timestamp(row.get("observed_at"))
    if row.get("artifact_digest") is not None:
        _required_digest(row["artifact_digest"])
    if row.get("current_start_facts_digest") is not None:
        _required_digest(row["current_start_facts_digest"])
    supplied = _required_digest(row.get("event_digest"))
    expected = _digest(
        {key: item for key, item in row.items() if key != "event_digest"}
    )
    if not hmac.compare_digest(supplied, expected):
        raise NextCyclePlanError("next_cycle_boundary_event_store_corrupt")
    return row


def staged_facts_status(
    artifact: Mapping[str, Any],
    bound_start_facts: Mapping[str, Any],
    current_start_facts: Mapping[str, Any],
    *,
    cycle_id: str,
    observed_at: str,
) -> dict[str, Any]:
    staged = validate_verified_waiting_plan(artifact)
    bound = validate_start_facts(bound_start_facts)
    current = validate_start_facts(current_start_facts)
    cycle = _cycle_id(cycle_id)
    if (
        staged["target_cycle_id"] != cycle
        or bound["cycle_id"] != cycle
        or current["cycle_id"] != cycle
        or bound["start_facts_digest"]
        != staged["start_facts_digest"]
    ):
        return {"valid": False, "reason": "next_cycle_verified_plan_wrong_cycle"}
    observed = parse_utc(observed_at)
    if observed < parse_utc(staged["valid_from"]):
        return {"valid": False, "reason": "next_cycle_verified_plan_not_yet_valid"}
    if observed >= parse_utc(staged["expires_at"]):
        return {"valid": False, "reason": "next_cycle_verified_plan_expired"}
    if staged["source"] != current["source"]:
        return {"valid": False, "reason": "next_cycle_verified_plan_wrong_source"}
    if staged["outer_policy"] != current["outer_policy"]:
        return {"valid": False, "reason": "next_cycle_verified_plan_wrong_policy"}
    authority_matches = same_execution_authority(bound, current)
    return {
        "valid": authority_matches,
        "reason": (
            "verified_waiting_valid"
            if authority_matches
            else "next_cycle_verified_plan_facts_stale"
        ),
    }


def plan_content_digest(plan: Mapping[str, Any]) -> str:
    body = {
        key: value
        for key, value in _json_mapping(plan).items()
        if key not in {"locked_at", "status"}
    }
    return _digest(body)


class _CandidateLock:
    def __init__(self, root: Path, cycle_id: str) -> None:
        self.root = Path(root)
        self.cycle_id = cycle_id
        self._handle = None
        with _PROCESS_LOCKS_GUARD:
            self._process_lock = _PROCESS_LOCKS.setdefault(
                str(self.root / cycle_id),
                threading.Lock(),
            )

    def __enter__(self):
        self._process_lock.acquire()
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self._handle = (self.root / f".{self.cycle_id}.lock").open(
                "a+",
                encoding="utf-8",
            )
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        except Exception:
            self._process_lock.release()
            raise
        return self

    def __exit__(self, *_args) -> None:
        try:
            if self._handle is not None:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
                self._handle.close()
        finally:
            self._process_lock.release()


def _load_bounded_rows(path: Path, *, corrupt_code: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    if path.is_symlink() or not path.is_file():
        raise NextCyclePlanError(corrupt_code)
    try:
        if path.stat().st_size > _MAX_ARTIFACT_BYTES:
            raise NextCyclePlanError(corrupt_code)
        rows = load_json(path)
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        raise NextCyclePlanError(corrupt_code) from exc
    if not isinstance(rows, list) or not all(
        isinstance(row, Mapping) for row in rows
    ):
        raise NextCyclePlanError(corrupt_code)
    return [dict(row) for row in rows]


def _atomic_write_json_fsync(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        rows,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
    ) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _cycle_id(value: Any) -> str:
    rendered = str(value or "")
    if not _CYCLE_ID.fullmatch(rendered):
        raise NextCyclePlanError("next_cycle_verified_plan_wrong_cycle")
    return rendered


def _timestamp(value: Any) -> str:
    try:
        return parse_utc(str(value)).isoformat()
    except (TypeError, ValueError) as exc:
        raise NextCyclePlanError("next_cycle_verified_plan_corrupt") from exc


def _required_text(value: Any) -> str:
    rendered = str(value or "").strip()
    if not rendered:
        raise NextCyclePlanError("next_cycle_verified_plan_corrupt")
    return rendered


def _required_digest(value: Any) -> str:
    rendered = str(value or "").lower()
    if not _DIGEST.fullmatch(rendered):
        raise NextCyclePlanError("next_cycle_verified_plan_corrupt")
    return rendered


def _positive_int(value: Any) -> int:
    try:
        rendered = int(value)
    except (TypeError, ValueError) as exc:
        raise NextCyclePlanError("next_cycle_verified_plan_corrupt") from exc
    if rendered <= 0:
        raise NextCyclePlanError("next_cycle_verified_plan_corrupt")
    return rendered


def _nonnegative_int(value: Any) -> int:
    try:
        rendered = int(value)
    except (TypeError, ValueError) as exc:
        raise NextCyclePlanError("next_cycle_verified_plan_corrupt") from exc
    if rendered < 0:
        raise NextCyclePlanError("next_cycle_verified_plan_corrupt")
    return rendered


def _json_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise NextCyclePlanError("next_cycle_verified_plan_corrupt")
    try:
        return json.loads(
            json.dumps(dict(value), sort_keys=True, allow_nan=False)
        )
    except (TypeError, ValueError) as exc:
        raise NextCyclePlanError("next_cycle_verified_plan_corrupt") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
