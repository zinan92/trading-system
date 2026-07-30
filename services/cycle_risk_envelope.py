"""Immutable Paper-only cycle risk envelopes.

An envelope is a human (or policy-nested AI) authorization boundary for one
already-active strategy plan.  It is deliberately narrower than a preview: a
preview is always rebuilt and independently checked by the control plane, but
an in-bound preview may rely on the envelope without copying an old preview's
facts digest or acknowledgement.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping

from services.cloud_access_gateway import authenticated_access_identity
from services.journal_store import load_json, write_json


OUTER_POLICY_SCHEMA = "paper-strategy-policy-boundary-v1"
OUTER_POLICY_BINDING_SCHEMA = "paper-supervisor-policy-binding-v1"
ENVELOPE_SCHEMA = "cycle-risk-envelope-v1"
AUTHORIZATION_KINDS = {
    "human_explicit",
    "ai_policy_within_preapproved_strategy_boundary",
}
_AI_ENVELOPE_FIELDS = {
    "authorization_kind",
    "limits",
}
_AI_FORBIDDEN_AUTHORIZATION_FIELDS = {
    "outer_policy_id",
    "outer_policy_version",
    "outer_policy_digest",
    "outer_policy_binding_id",
    "outer_policy_binding_version",
    "outer_policy_binding_digest",
    "facts_digest",
    "acknowledgement",
    "risk_acknowledgements",
}

_GRID_FIELDS = (
    "max_actual_leverage",
    "max_full_depth_loss",
    "max_notional_per_grid",
    "min_grid_count",
    "max_grid_count",
)
_DCA_FIELDS = (
    "max_actual_leverage",
    "max_full_depth_loss",
    "max_notional_per_addition",
    "max_total_possible_notional",
    "min_additions",
    "max_additions",
)
_PLAN_SHAPE_FIELDS = (
    "direction",
    "style",
    "range",
    "key_levels",
    "grid",
    "signal",
    "tp_sl",
    "risk_budget",
    "intraday_rules",
)


class CycleRiskEnvelopeError(ValueError):
    """A stable, fail-closed envelope authorization error."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class CycleRiskEnvelopeStore:
    """Append-only persistence and exact comparison for Paper envelopes."""

    def __init__(
        self,
        output_root: Path,
        *,
        supervisor_policy_binding_ref: Mapping[str, Any] | None = None,
        authorization_clock: Callable[[], str] | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.root = self.output_root / "dualtrack" / "supervisor" / "risk_envelopes"
        self.policy_root = self.root / "outer_strategy_policies"
        self.binding_root = self.root / "outer_strategy_policy_bindings"
        self.verification_root = self.root / "start_verifications"
        self.supervisor_policy_binding_ref = (
            dict(supervisor_policy_binding_ref)
            if isinstance(supervisor_policy_binding_ref, Mapping)
            else _binding_ref_from_environment()
        )
        self.authorization_clock = authorization_clock

    def authorize_outer_policy(
        self,
        *,
        payload: Mapping[str, Any],
        actor: Mapping[str, Any] | None,
        now: str | None = None,
    ) -> dict[str, Any]:
        """Persist a Park-explicit outer strategy boundary exactly once."""

        actor_row = _park_actor(actor)
        strategy_type = _strategy_type(payload)
        limits = _canonical_limits(
            strategy_type,
            payload.get("limits"),
            code="outer_strategy_policy_invalid",
        )
        policy_id = _required_text(payload.get("policy_id"), "outer_strategy_policy_invalid")
        version = _positive_int(payload.get("version"), "outer_strategy_policy_invalid")
        authorized_at = _utc_timestamp(
            self.authorization_clock() if self.authorization_clock else now,
            "outer_strategy_policy_invalid",
        )
        expires_at = _utc_timestamp(
            _required_text(
                payload.get("expires_at"),
                "outer_strategy_policy_invalid",
            ),
            "outer_strategy_policy_invalid",
        )
        if _parse_timestamp(expires_at) <= _parse_timestamp(authorized_at):
            raise CycleRiskEnvelopeError("outer_strategy_policy_invalid")
        record = {
            "schema_version": OUTER_POLICY_SCHEMA,
            "policy_id": policy_id,
            "version": version,
            "strategy_type": strategy_type,
            "direction": _direction(
                payload.get("direction"),
                "outer_strategy_policy_invalid",
            ),
            "summary": _required_text(payload.get("summary"), "outer_strategy_policy_invalid"),
            "limits": limits,
            "authorized_at": authorized_at,
            "expires_at": expires_at,
            "actor": actor_row,
        }
        record["policy_digest"] = _digest(record)
        path = self.policy_root / f"{_safe_filename(policy_id)}.json"
        return _append_immutable_record(
            path,
            record=record,
            identity={
                "policy_id": policy_id,
                "version": version,
            },
            digest_field="policy_digest",
            conflict_code="outer_strategy_policy_invalid",
            registry_validator=_validate_policy_registry,
        )

    def bind_supervisor_outer_policy(
        self,
        *,
        payload: Mapping[str, Any],
        actor: Mapping[str, Any] | None,
        now: str | None = None,
    ) -> dict[str, Any]:
        """Persist one Park-written exact Supervisor policy binding."""

        actor_row = _park_actor(actor)
        bound_at = _utc_timestamp(
            self.authorization_clock() if self.authorization_clock else now,
            "outer_strategy_policy_invalid",
        )
        binding_id = _required_text(
            payload.get("binding_id"),
            "outer_strategy_policy_invalid",
        )
        binding_version = _positive_int(
            payload.get("binding_version"),
            "outer_strategy_policy_invalid",
        )
        policy_id = _required_text(
            payload.get("policy_id"),
            "outer_strategy_policy_invalid",
        )
        policy_version = _positive_int(
            payload.get("policy_version"),
            "outer_strategy_policy_invalid",
        )
        policy_digest = _required_digest(
            payload.get("policy_digest"),
            "outer_strategy_policy_invalid",
        )
        policy = self._load_outer_policy_exact(
            policy_id=policy_id,
            version=policy_version,
        )
        if policy["policy_digest"] != policy_digest:
            raise CycleRiskEnvelopeError("outer_strategy_policy_invalid")
        self._require_policy_current(policy, at=bound_at)
        record = {
            "schema_version": OUTER_POLICY_BINDING_SCHEMA,
            "binding_id": binding_id,
            "binding_version": binding_version,
            "policy_id": policy_id,
            "policy_version": policy_version,
            "policy_digest": policy_digest,
            "policy_expires_at": policy["expires_at"],
            "summary": _required_text(
                payload.get("summary"),
                "outer_strategy_policy_invalid",
            ),
            "bound_at": bound_at,
            "actor": actor_row,
        }
        record["binding_digest"] = _digest(record)
        path = self.binding_root / f"{_safe_filename(binding_id)}.json"
        return _append_immutable_record(
            path,
            record=record,
            identity={
                "binding_id": binding_id,
                "binding_version": binding_version,
            },
            digest_field="binding_digest",
            conflict_code="outer_strategy_policy_invalid",
            registry_validator=_validate_binding_registry,
        )

    def outer_policy(self, policy_id: str, version: int) -> dict[str, Any]:
        """Read one exact immutable policy; never choose a latest version."""

        return self._load_outer_policy_exact(
            policy_id=_required_text(
                policy_id,
                "outer_strategy_policy_missing",
            ),
            version=_positive_int(
                version,
                "outer_strategy_policy_invalid",
            ),
        )

    def supervisor_binding(
        self,
        binding_id: str,
        binding_version: int,
    ) -> dict[str, Any]:
        """Read one exact immutable binding; never choose a fallback."""

        return self._load_binding_exact(
            binding_id=_required_text(
                binding_id,
                "outer_strategy_policy_missing",
            ),
            binding_version=_positive_int(
                binding_version,
                "outer_strategy_policy_invalid",
            ),
        )

    def verify_supervisor_outer_policy(
        self,
        *,
        at: str | None = None,
    ) -> dict[str, Any]:
        """Preflight the exact Park binding before autonomous AI planning."""

        checked_at = _utc_timestamp(
            at,
            "outer_strategy_policy_invalid",
        )
        binding, policy = self._load_bound_outer_policy(at=checked_at)
        return {
            "schema_version": "paper-supervisor-policy-preflight-v1",
            "checked_at": checked_at,
            "binding_id": binding["binding_id"],
            "binding_version": binding["binding_version"],
            "binding_digest": binding["binding_digest"],
            "policy_id": policy["policy_id"],
            "policy_version": policy["version"],
            "policy_digest": policy["policy_digest"],
            "policy_expires_at": policy["expires_at"],
            "passed": True,
        }

    def authorize_ai_candidate_envelope(
        self,
        *,
        cycle_id: str,
        proposal: Mapping[str, Any],
        preview: Mapping[str, Any],
        now: str | None = None,
    ) -> dict[str, Any]:
        """Authorize an AI candidate before any production plan is written."""

        authorized_at = _utc_timestamp(
            now,
            "risk_envelope_authorization_invalid",
        )
        if (
            str(proposal.get("cycle_id") or "") != str(cycle_id)
            or str(proposal.get("source") or "") != "ai"
        ):
            raise CycleRiskEnvelopeError("plan_identity_conflict")
        proposal_id = _required_text(
            proposal.get("proposal_id"),
            "plan_identity_conflict",
        )
        strategy_type = _strategy_type(proposal)
        direction = _direction(
            proposal.get("direction"),
            "plan_identity_conflict",
        )
        if (
            _strategy_type(preview) != strategy_type
            or _direction(
                preview.get("direction"),
                "plan_identity_conflict",
            )
            != direction
            or str(proposal.get("preview_id") or "")
            != str(preview.get("preview_id") or "")
        ):
            raise CycleRiskEnvelopeError("plan_identity_conflict")
        limits = _limits_from_preview(preview, strategy_type)
        binding, outer = self._load_bound_outer_policy(at=authorized_at)
        comparisons = _compare_outer_policy(
            outer,
            limits,
            strategy_type=strategy_type,
            direction=direction,
        )
        if not all(row["pass"] for row in comparisons):
            raise CycleRiskEnvelopeError(
                "outer_strategy_policy_envelope_out_of_bounds"
            )
        source_proposal = {
            "proposal_id": proposal_id,
            "proposal_digest": _proposal_plan_digest(proposal),
            "preview_id": _required_text(
                preview.get("preview_id"),
                "plan_identity_conflict",
            ),
            "preview_digest": _digest(dict(preview)),
            "execution_shape_digest": (
                _preview_execution_shape_digest(preview)
                if strategy_type == "dca"
                else None
            ),
        }
        record: dict[str, Any] = {
            "schema_version": ENVELOPE_SCHEMA,
            "cycle_id": str(cycle_id),
            "strategy_plan_id": None,
            "strategy_plan_version": None,
            "strategy_type": strategy_type,
            "direction": direction,
            "plan_digest": None,
            "source_proposal": source_proposal,
            "authorization_kind": (
                "ai_policy_within_preapproved_strategy_boundary"
            ),
            "limits": limits,
            "authorized_at": authorized_at,
            "actor": {"email": None, "transport": "ai_policy"},
            "outer_policy": _outer_policy_reference(binding, outer),
            "outer_policy_comparisons": comparisons,
        }
        return self._persist_envelope(record)

    def authorize_envelope(
        self,
        *,
        cycle_id: str,
        plan: Mapping[str, Any],
        payload: Mapping[str, Any],
        actor: Mapping[str, Any] | None,
        now: str | None = None,
    ) -> dict[str, Any]:
        """Authorize one immutable envelope bound to an active plan identity."""

        strategy_type = _strategy_type(plan)
        kind = _required_text(payload.get("authorization_kind"), "risk_envelope_authorization_invalid")
        if kind not in AUTHORIZATION_KINDS:
            raise CycleRiskEnvelopeError("risk_envelope_authorization_invalid")
        authorized_at = _utc_timestamp(
            now,
            "risk_envelope_authorization_invalid",
        )
        if str(plan.get("cycle_id") or "") != str(cycle_id):
            raise CycleRiskEnvelopeError("plan_identity_conflict")
        plan_id = _required_text(plan.get("strategy_plan_id"), "plan_identity_conflict")
        plan_version = _positive_int(plan.get("version"), "plan_identity_conflict")
        limits = _canonical_limits(
            strategy_type,
            payload.get("limits"),
            code="risk_envelope_authorization_invalid",
        )
        record: dict[str, Any] = {
            "schema_version": ENVELOPE_SCHEMA,
            "cycle_id": str(cycle_id),
            "strategy_plan_id": plan_id,
            "strategy_plan_version": plan_version,
            "strategy_type": strategy_type,
            "direction": _required_text(plan.get("direction"), "plan_identity_conflict"),
            "plan_digest": _plan_digest(plan),
            "source_proposal": None,
            "authorization_kind": kind,
            "limits": limits,
            "authorized_at": authorized_at,
        }
        if kind == "human_explicit":
            record["actor"] = _human_actor(actor)
            record["outer_policy"] = None
            record["outer_policy_comparisons"] = []
        else:
            if actor is not None:
                raise CycleRiskEnvelopeError(
                    "risk_envelope_authorization_invalid"
                )
            if (
                set(payload) - _AI_ENVELOPE_FIELDS
                or set(payload) & _AI_FORBIDDEN_AUTHORIZATION_FIELDS
            ):
                raise CycleRiskEnvelopeError(
                    "risk_envelope_authorization_invalid"
                )
            binding, outer = self._load_bound_outer_policy(at=authorized_at)
            comparisons = _compare_outer_policy(
                outer,
                limits,
                strategy_type=strategy_type,
                direction=str(record["direction"]),
            )
            if not all(row["pass"] for row in comparisons):
                raise CycleRiskEnvelopeError("outer_strategy_policy_envelope_out_of_bounds")
            record["actor"] = {"email": None, "transport": "ai_policy"}
            record["outer_policy"] = _outer_policy_reference(
                binding,
                outer,
            )
            record["outer_policy_comparisons"] = comparisons
        return self._persist_envelope(record)

    def _persist_envelope(
        self,
        record: dict[str, Any],
    ) -> dict[str, Any]:
        record["envelope_authorization_id"] = _digest(
            {
                key: value
                for key, value in record.items()
                if key != "envelope_authorization_id"
            }
        )[:40]
        record["authorization_digest"] = _digest(record)
        return _append_immutable_record(
            self.root
            / f"{_safe_filename(str(record['cycle_id']))}.json",
            record=record,
            identity={
                "envelope_authorization_id": record[
                    "envelope_authorization_id"
                ],
            },
            digest_field="authorization_digest",
            conflict_code="risk_envelope_authorization_invalid",
            registry_validator=_validate_envelope_registry,
        )

    def envelope(self, cycle_id: str, envelope_authorization_id: str) -> dict[str, Any] | None:
        rows = _rows(
            self.root / f"{_safe_filename(cycle_id)}.json"
        )
        _validate_envelope_registry(rows)
        return next(
            (
                row
                for row in rows
                if str(row.get("envelope_authorization_id") or "") == str(envelope_authorization_id)
            ),
            None,
        )

    def verify_preview(
        self,
        *,
        cycle_id: str,
        plan: Mapping[str, Any],
        envelope_authorization_id: str,
        preview: Mapping[str, Any],
        proposal: Mapping[str, Any] | None = None,
        now: str | None = None,
    ) -> dict[str, Any]:
        """Return exact field-level evidence or fail closed before start."""

        envelope = self.envelope(cycle_id, envelope_authorization_id)
        if envelope is None:
            raise CycleRiskEnvelopeError("risk_envelope_missing")
        if envelope.get("schema_version") != ENVELOPE_SCHEMA or envelope.get("authorization_digest") != _digest({key: value for key, value in envelope.items() if key != "authorization_digest"}):
            raise CycleRiskEnvelopeError("risk_envelope_authorization_invalid")
        if (
            envelope.get("authorization_kind")
            == "ai_policy_within_preapproved_strategy_boundary"
        ):
            self._verify_envelope_outer_policy(
                envelope,
                at=_utc_timestamp(
                    now,
                    "outer_strategy_policy_invalid",
                ),
            )
        source_proposal = envelope.get("source_proposal")
        if isinstance(source_proposal, Mapping):
            if (
                str(source_proposal.get("preview_id") or "")
                != str(preview.get("preview_id") or "")
                or str(source_proposal.get("preview_digest") or "")
                != _digest(dict(preview))
            ):
                raise CycleRiskEnvelopeError("plan_identity_conflict")
            if (
                str(envelope.get("strategy_type") or "") == "dca"
                and str(
                    source_proposal.get("execution_shape_digest") or ""
                )
                != _preview_execution_shape_digest(preview)
            ):
                raise CycleRiskEnvelopeError("plan_identity_conflict")
            if plan:
                if not _matching_candidate_plan(envelope, plan):
                    raise CycleRiskEnvelopeError("plan_identity_conflict")
            elif (
                envelope.get("strategy_type") != "dca"
                or not isinstance(proposal, Mapping)
                or str(source_proposal.get("proposal_id") or "")
                != str(proposal.get("proposal_id") or "")
                or str(
                    source_proposal.get("proposal_digest") or ""
                )
                != _proposal_plan_digest(proposal)
            ):
                raise CycleRiskEnvelopeError("plan_identity_conflict")
        elif not _matching_plan(envelope, plan):
            raise CycleRiskEnvelopeError("plan_identity_conflict")
        strategy_type = _strategy_type(preview)
        if strategy_type != envelope.get("strategy_type"):
            raise CycleRiskEnvelopeError("plan_identity_conflict")
        if str(preview.get("direction") or "") != str(envelope.get("direction") or ""):
            raise CycleRiskEnvelopeError("plan_identity_conflict")
        values = _preview_values(preview, strategy_type)
        comparisons = _compare_preview(envelope["limits"], values, strategy_type)
        if not all(row["pass"] for row in comparisons):
            raise CycleRiskEnvelopeError("risk_envelope_preview_out_of_bounds")
        return {
            "schema_version": "cycle-risk-envelope-preview-verification-v1",
            "envelope_authorization_id": envelope["envelope_authorization_id"],
            "authorization_digest": envelope["authorization_digest"],
            "preview_id": _required_text(preview.get("preview_id"), "risk_envelope_preview_out_of_bounds"),
            "preview_facts_digest": _digest(dict(preview)),
            "source_execution_shape_digest": (
                source_proposal.get("execution_shape_digest")
                if isinstance(source_proposal, Mapping)
                else None
            ),
            "authorized_source_plan": {
                "cycle_id": envelope["cycle_id"],
                "strategy_plan_id": (
                    plan.get("strategy_plan_id")
                    if plan
                    else envelope["strategy_plan_id"]
                ),
                "strategy_plan_version": (
                    plan.get("version")
                    if plan
                    else envelope["strategy_plan_version"]
                ),
                "plan_digest": (
                    _plan_digest(plan)
                    if plan
                    else envelope["plan_digest"]
                ),
                "strategy_type": envelope["strategy_type"],
                "direction": envelope["direction"],
                "source_proposal": (
                    dict(source_proposal)
                    if isinstance(source_proposal, Mapping)
                    else None
                ),
            },
            "comparisons": comparisons,
            "verified_values": {key: str(value) for key, value in values.items()},
            "passed": True,
        }

    def bind_execution_plan(
        self,
        verification: Mapping[str, Any],
        execution_plan: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Record the source-policy → fresh-preview → execution-plan link.

        Starts intentionally materialize a new immutable execution plan.  This
        does not change the envelope's source-plan binding; it makes the
        derived execution identity explicit and verifies its strategy shape.
        """

        source = verification.get("authorized_source_plan")
        if not isinstance(source, Mapping):
            raise CycleRiskEnvelopeError("risk_envelope_authorization_invalid")
        if (
            str(execution_plan.get("cycle_id") or "") != str(source.get("cycle_id") or "")
            or str(execution_plan.get("direction") or "") != str(source.get("direction") or "")
            or _strategy_type(execution_plan) != str(source.get("strategy_type") or "")
            or str(execution_plan.get("preview_id") or "")
            != str(verification.get("preview_id") or "")
        ):
            raise CycleRiskEnvelopeError("plan_identity_conflict")
        verified_values = verification.get("verified_values")
        if not isinstance(verified_values, Mapping):
            raise CycleRiskEnvelopeError("risk_envelope_authorization_invalid")
        execution_values = _execution_values(execution_plan, str(source["strategy_type"]))
        if {
            key: str(value) for key, value in execution_values.items()
        } != {
            key: str(value) for key, value in verified_values.items()
        }:
            raise CycleRiskEnvelopeError("plan_identity_conflict")
        source_shape_digest = str(
            verification.get("source_execution_shape_digest") or ""
        )
        if (
            source_shape_digest
            and source_shape_digest
            != _execution_plan_shape_digest(
                execution_plan,
                str(source["strategy_type"]),
            )
        ):
            raise CycleRiskEnvelopeError("plan_identity_conflict")
        result = dict(verification)
        result["execution_plan"] = {
            "strategy_plan_id": _required_text(execution_plan.get("strategy_plan_id"), "plan_identity_conflict"),
            "strategy_plan_version": _positive_int(execution_plan.get("version"), "plan_identity_conflict"),
            "plan_digest": _plan_digest(execution_plan),
        }
        return result

    def verify_candidate_plan_identity(
        self,
        *,
        cycle_id: str,
        envelope_authorization_id: str,
        plan: Mapping[str, Any],
        now: str | None = None,
    ) -> dict[str, Any]:
        """Verify a candidate plan before the production plan is activated."""

        envelope = self.envelope(
            cycle_id,
            envelope_authorization_id,
        )
        if envelope is None:
            raise CycleRiskEnvelopeError("risk_envelope_missing")
        if (
            envelope.get("schema_version") != ENVELOPE_SCHEMA
            or envelope.get("authorization_digest")
            != _digest(
                {
                    key: value
                    for key, value in envelope.items()
                    if key != "authorization_digest"
                }
            )
        ):
            raise CycleRiskEnvelopeError(
                "risk_envelope_authorization_invalid"
            )
        if (
            str(envelope.get("cycle_id") or "") != str(cycle_id)
            or not _matching_candidate_plan(envelope, plan)
        ):
            raise CycleRiskEnvelopeError("plan_identity_conflict")
        if (
            envelope.get("authorization_kind")
            != "ai_policy_within_preapproved_strategy_boundary"
        ):
            raise CycleRiskEnvelopeError("plan_identity_conflict")
        self._verify_envelope_outer_policy(
            envelope,
            at=_utc_timestamp(
                now,
                "outer_strategy_policy_invalid",
            ),
        )
        return dict(envelope)

    def record_start_verification(
        self,
        *,
        cycle_id: str,
        verification: Mapping[str, Any],
        prepared_start_id: str | None,
        now: str | None = None,
    ) -> dict[str, Any]:
        """Append immutable full comparison evidence before a start invocation.

        Control audit intentionally bounds nested payloads.  This narrow receipt
        is the durable authority for exact comparison rows and plan linkage.
        It records an authorization attempt, not a claim that order submission
        or execution succeeded.
        """

        if not isinstance(verification.get("execution_plan"), Mapping):
            raise CycleRiskEnvelopeError("risk_envelope_authorization_invalid")
        if str(verification.get("authorized_source_plan", {}).get("cycle_id") or "") != str(cycle_id):
            raise CycleRiskEnvelopeError("plan_identity_conflict")
        rows = verification.get("comparisons")
        if not isinstance(rows, list) or not rows or not all(isinstance(row, Mapping) for row in rows):
            raise CycleRiskEnvelopeError("risk_envelope_authorization_invalid")
        record = {
            "schema_version": "cycle-risk-envelope-start-verification-v1",
            "cycle_id": str(cycle_id),
            "recorded_at": _timestamp(now),
            "prepared_start_id": str(prepared_start_id or "") or None,
            "envelope_authorization_id": verification.get("envelope_authorization_id"),
            "authorization_digest": verification.get("authorization_digest"),
            "preview_id": verification.get("preview_id"),
            "preview_facts_digest": verification.get("preview_facts_digest"),
            "authorized_source_plan": dict(verification["authorized_source_plan"]),
            "execution_plan": dict(verification["execution_plan"]),
            "verified_values": dict(verification.get("verified_values") or {}),
            "comparisons": [dict(row) for row in rows],
            "passed": verification.get("passed") is True,
        }
        record["verification_record_id"] = _digest({
            key: value for key, value in record.items() if key != "verification_record_id"
        })[:40]
        return _append_immutable_record(
            self.verification_root
            / f"{_safe_filename(cycle_id)}.json",
            record=record,
            identity={
                "verification_record_id": record[
                    "verification_record_id"
                ],
            },
            digest_field="verification_record_id",
            conflict_code="risk_envelope_verification_reused",
            registry_validator=_validate_start_verification_registry,
            reject_existing=True,
        )

    def _load_bound_outer_policy(
        self,
        *,
        at: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        reference = self.supervisor_policy_binding_ref
        if not isinstance(reference, Mapping):
            raise CycleRiskEnvelopeError("outer_strategy_policy_missing")
        binding_id = _required_text(
            reference.get("binding_id"),
            "outer_strategy_policy_invalid",
        )
        binding_version = _positive_int(
            reference.get("binding_version"),
            "outer_strategy_policy_invalid",
        )
        binding_digest = _required_digest(
            reference.get("binding_digest"),
            "outer_strategy_policy_invalid",
        )
        binding = self._load_binding_exact(
            binding_id=binding_id,
            binding_version=binding_version,
        )
        if binding["binding_digest"] != binding_digest:
            raise CycleRiskEnvelopeError("outer_strategy_policy_invalid")
        policy = self._load_outer_policy_exact(
            policy_id=str(binding["policy_id"]),
            version=int(binding["policy_version"]),
        )
        if policy["policy_digest"] != binding["policy_digest"]:
            raise CycleRiskEnvelopeError("outer_strategy_policy_invalid")
        self._require_policy_current(policy, at=at)
        return binding, policy

    def _load_outer_policy_exact(
        self,
        *,
        policy_id: str,
        version: int,
    ) -> dict[str, Any]:
        rows = _rows(
            self.policy_root / f"{_safe_filename(policy_id)}.json"
        )
        _validate_policy_registry(rows)
        policy = next(
            (
                row
                for row in rows
                if row.get("policy_id") == policy_id
                and row.get("version") == version
            ),
            None,
        )
        if policy is None:
            raise CycleRiskEnvelopeError("outer_strategy_policy_missing")
        if (
            policy.get("schema_version") != OUTER_POLICY_SCHEMA
            or policy.get("policy_digest")
            != _digest(
                {
                    key: value
                    for key, value in policy.items()
                    if key != "policy_digest"
                }
            )
        ):
            raise CycleRiskEnvelopeError("outer_strategy_policy_invalid")
        _strategy_type(policy, "outer_strategy_policy_invalid")
        _direction(
            policy.get("direction"),
            "outer_strategy_policy_invalid",
        )
        _canonical_limits(
            str(policy["strategy_type"]),
            policy.get("limits"),
            code="outer_strategy_policy_invalid",
        )
        authorized_at = _utc_timestamp(
            policy.get("authorized_at"),
            "outer_strategy_policy_invalid",
        )
        expires_at = _utc_timestamp(
            _required_text(
                policy.get("expires_at"),
                "outer_strategy_policy_invalid",
            ),
            "outer_strategy_policy_invalid",
        )
        if _parse_timestamp(expires_at) <= _parse_timestamp(authorized_at):
            raise CycleRiskEnvelopeError("outer_strategy_policy_invalid")
        return policy

    def _load_binding_exact(
        self,
        *,
        binding_id: str,
        binding_version: int,
    ) -> dict[str, Any]:
        rows = _rows(
            self.binding_root / f"{_safe_filename(binding_id)}.json"
        )
        _validate_binding_registry(rows)
        binding = next(
            (
                row
                for row in rows
                if row.get("binding_id") == binding_id
                and row.get("binding_version") == binding_version
            ),
            None,
        )
        if binding is None:
            raise CycleRiskEnvelopeError("outer_strategy_policy_missing")
        if (
            binding.get("schema_version") != OUTER_POLICY_BINDING_SCHEMA
            or binding.get("binding_digest")
            != _digest(
                {
                    key: value
                    for key, value in binding.items()
                    if key != "binding_digest"
                }
            )
        ):
            raise CycleRiskEnvelopeError("outer_strategy_policy_invalid")
        _required_text(
            binding.get("policy_id"),
            "outer_strategy_policy_invalid",
        )
        _positive_int(
            binding.get("policy_version"),
            "outer_strategy_policy_invalid",
        )
        _required_digest(
            binding.get("policy_digest"),
            "outer_strategy_policy_invalid",
        )
        _utc_timestamp(
            binding.get("bound_at"),
            "outer_strategy_policy_invalid",
        )
        return binding

    def _require_policy_current(
        self,
        policy: Mapping[str, Any],
        *,
        at: str,
    ) -> None:
        if _parse_timestamp(at) >= _parse_timestamp(
            _utc_timestamp(
                policy.get("expires_at"),
                "outer_strategy_policy_invalid",
            )
        ):
            raise CycleRiskEnvelopeError("outer_strategy_policy_expired")

    def _verify_envelope_outer_policy(
        self,
        envelope: Mapping[str, Any],
        *,
        at: str,
    ) -> None:
        recorded = envelope.get("outer_policy")
        if not isinstance(recorded, Mapping):
            raise CycleRiskEnvelopeError("outer_strategy_policy_missing")
        binding, policy = self._load_bound_outer_policy(at=at)
        expected = {
            "policy_id": policy["policy_id"],
            "version": policy["version"],
            "policy_digest": policy["policy_digest"],
            "authorized_at": policy["authorized_at"],
            "expires_at": policy["expires_at"],
            "binding_id": binding["binding_id"],
            "binding_version": binding["binding_version"],
            "binding_digest": binding["binding_digest"],
            "bound_at": binding["bound_at"],
        }
        if dict(recorded) != expected:
            raise CycleRiskEnvelopeError("outer_strategy_policy_invalid")
        comparisons = envelope.get("outer_policy_comparisons")
        if (
            not isinstance(comparisons, list)
            or not comparisons
            or any(
                not isinstance(row, Mapping) or row.get("pass") is not True
                for row in comparisons
            )
        ):
            raise CycleRiskEnvelopeError("outer_strategy_policy_invalid")


def _preview_values(preview: Mapping[str, Any], strategy_type: str) -> dict[str, Decimal]:
    risk = preview.get("risk") if isinstance(preview.get("risk"), Mapping) else {}
    if strategy_type == "grid":
        grid = preview.get("grid") if isinstance(preview.get("grid"), Mapping) else {}
        return {
            "actual_leverage": _decimal(risk.get("actual_leverage"), "risk_envelope_preview_out_of_bounds"),
            "full_depth_loss": _decimal(risk.get("max_loss"), "risk_envelope_preview_out_of_bounds"),
            "notional_per_grid": _decimal(grid.get("notional_per_grid"), "risk_envelope_preview_out_of_bounds"),
            "grid_count": _decimal(grid.get("count"), "risk_envelope_preview_out_of_bounds"),
        }
    dca = preview.get("dca") if isinstance(preview.get("dca"), Mapping) else {}
    return {
        "actual_leverage": _decimal(risk.get("actual_leverage_at_full_depth"), "risk_envelope_preview_out_of_bounds"),
        "full_depth_loss": _decimal(risk.get("maximum_loss_at_full_depth"), "risk_envelope_preview_out_of_bounds"),
        "notional_per_addition": _decimal(dca.get("notional_per_addition"), "risk_envelope_preview_out_of_bounds"),
        "total_possible_notional": _decimal(dca.get("total_possible_notional"), "risk_envelope_preview_out_of_bounds"),
        "additions": _decimal(dca.get("max_additions"), "risk_envelope_preview_out_of_bounds"),
    }


def _limits_from_preview(
    preview: Mapping[str, Any],
    strategy_type: str,
) -> dict[str, str]:
    values = _preview_values(preview, strategy_type)
    if strategy_type == "grid":
        count = str(values["grid_count"])
        return {
            "max_actual_leverage": str(values["actual_leverage"]),
            "max_full_depth_loss": str(values["full_depth_loss"]),
            "max_notional_per_grid": str(values["notional_per_grid"]),
            "min_grid_count": count,
            "max_grid_count": count,
        }
    additions = str(values["additions"])
    return {
        "max_actual_leverage": str(values["actual_leverage"]),
        "max_full_depth_loss": str(values["full_depth_loss"]),
        "max_notional_per_addition": str(
            values["notional_per_addition"]
        ),
        "max_total_possible_notional": str(
            values["total_possible_notional"]
        ),
        "min_additions": additions,
        "max_additions": additions,
    }


def _execution_values(plan: Mapping[str, Any], strategy_type: str) -> dict[str, Decimal]:
    """Extract the exact bounded execution values from a derived plan."""

    risk = plan.get("risk_budget") if isinstance(plan.get("risk_budget"), Mapping) else {}
    if strategy_type == "grid":
        grid = plan.get("grid") if isinstance(plan.get("grid"), Mapping) else {}
        return {
            "actual_leverage": _decimal(grid.get("actual_leverage"), "plan_identity_conflict"),
            "full_depth_loss": _decimal(risk.get("max_loss"), "plan_identity_conflict"),
            "notional_per_grid": _decimal(grid.get("notional_per_grid"), "plan_identity_conflict"),
            "grid_count": _decimal(grid.get("count"), "plan_identity_conflict"),
        }
    dca = plan.get("dca") if isinstance(plan.get("dca"), Mapping) else {}
    return {
        "actual_leverage": _decimal(risk.get("actual_leverage_at_full_depth"), "plan_identity_conflict"),
        "full_depth_loss": _decimal(risk.get("maximum_loss_at_full_depth"), "plan_identity_conflict"),
        "notional_per_addition": _decimal(dca.get("notional_per_addition"), "plan_identity_conflict"),
        "total_possible_notional": _decimal(dca.get("total_possible_notional"), "plan_identity_conflict"),
        "additions": _decimal(dca.get("max_additions"), "plan_identity_conflict"),
    }


def _preview_execution_shape_digest(
    preview: Mapping[str, Any],
) -> str:
    dca = (
        dict(preview.get("dca") or {})
        if isinstance(preview.get("dca"), Mapping)
        else {}
    )
    dca["entries"] = [
        dict(row)
        for row in preview.get("entries") or []
        if isinstance(row, Mapping)
    ]
    dca["aggregate_take_profit"] = dict(
        preview.get("aggregate_take_profit") or {}
    )
    return _digest(
        {
            "strategy_type": "dca",
            "direction": preview.get("direction"),
            "preview_id": preview.get("preview_id"),
            "dca": dca,
            "risk_budget": dict(preview.get("risk") or {}),
        }
    )


def _execution_plan_shape_digest(
    plan: Mapping[str, Any],
    strategy_type: str,
) -> str:
    if strategy_type != "dca":
        return ""
    return _digest(
        {
            "strategy_type": "dca",
            "direction": plan.get("direction"),
            "preview_id": plan.get("preview_id"),
            "dca": dict(plan.get("dca") or {}),
            "risk_budget": dict(plan.get("risk_budget") or {}),
        }
    )


def _compare_preview(limits: Mapping[str, Any], values: Mapping[str, Decimal], strategy_type: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if strategy_type == "grid":
        checks = (
            ("actual_leverage", "<=", "max_actual_leverage"),
            ("full_depth_loss", "<=", "max_full_depth_loss"),
            ("notional_per_grid", "<=", "max_notional_per_grid"),
            ("grid_count", ">=", "min_grid_count"),
            ("grid_count", "<=", "max_grid_count"),
        )
    else:
        checks = (
            ("actual_leverage", "<=", "max_actual_leverage"),
            ("full_depth_loss", "<=", "max_full_depth_loss"),
            ("notional_per_addition", "<=", "max_notional_per_addition"),
            ("total_possible_notional", "<=", "max_total_possible_notional"),
            ("additions", ">=", "min_additions"),
            ("additions", "<=", "max_additions"),
        )
    for field, operator, limit_key in checks:
        observed, limit = values[field], _decimal(limits.get(limit_key), "risk_envelope_authorization_invalid")
        passed = observed <= limit if operator == "<=" else observed >= limit
        rows.append({"field": field, "operator": operator, "authorized_limit": str(limit), "observed_value": str(observed), "pass": passed})
    return rows


def _compare_outer_policy(
    outer: Mapping[str, Any],
    inner: Mapping[str, Any],
    *,
    strategy_type: str,
    direction: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    rows.extend(
        [
            {
                "field": "strategy_type",
                "operator": "==",
                "authorized_limit": str(outer.get("strategy_type") or ""),
                "observed_value": strategy_type,
                "pass": str(outer.get("strategy_type") or "") == strategy_type,
            },
            {
                "field": "direction",
                "operator": "==",
                "authorized_limit": str(outer.get("direction") or ""),
                "observed_value": direction,
                "pass": str(outer.get("direction") or "") == direction,
            },
        ]
    )
    outer_limits = outer.get("limits") if isinstance(outer.get("limits"), Mapping) else {}
    for field in (_GRID_FIELDS if strategy_type == "grid" else _DCA_FIELDS):
        outer_value = _decimal(outer_limits.get(field), "outer_strategy_policy_invalid")
        inner_value = _decimal(inner.get(field), "risk_envelope_authorization_invalid")
        minimum = field.startswith("min_")
        passed = inner_value >= outer_value if minimum else inner_value <= outer_value
        rows.append({"field": field, "operator": ">=" if minimum else "<=", "authorized_limit": str(outer_value), "observed_value": str(inner_value), "pass": passed})
    return rows


def _canonical_limits(
    strategy_type: str,
    source: Any,
    *,
    code: str,
) -> dict[str, str]:
    if not isinstance(source, Mapping):
        raise CycleRiskEnvelopeError(code)
    fields = _GRID_FIELDS if strategy_type == "grid" else _DCA_FIELDS
    if set(source) != set(fields):
        raise CycleRiskEnvelopeError(code)
    limits = {
        field: str(_decimal(source.get(field), code))
        for field in fields
    }
    if _decimal(
        limits[
            "min_grid_count"
            if strategy_type == "grid"
            else "min_additions"
        ],
        code,
    ) > _decimal(
        limits[
            "max_grid_count"
            if strategy_type == "grid"
            else "max_additions"
        ],
        code,
    ):
        raise CycleRiskEnvelopeError(code)
    return limits


def _matching_plan(envelope: Mapping[str, Any], plan: Mapping[str, Any]) -> bool:
    return (
        str(envelope.get("strategy_plan_id") or "") == str(plan.get("strategy_plan_id") or "")
        and int(envelope.get("strategy_plan_version") or 0) == int(plan.get("version") or 0)
        and str(envelope.get("direction") or "") == str(plan.get("direction") or "")
        and str(envelope.get("plan_digest") or "") == _plan_digest(plan)
    )


def _matching_candidate_plan(
    envelope: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> bool:
    source = envelope.get("source_proposal")
    if not isinstance(source, Mapping):
        return False
    source_ids = {
        str(value)
        for value in plan.get("source_proposal_ids") or []
        if str(value)
    }
    return (
        str(source.get("proposal_id") or "") in source_ids
        and str(source.get("proposal_digest") or "")
        == _proposal_plan_digest(plan)
        and str(envelope.get("direction") or "")
        == str(plan.get("direction") or "")
        and str(envelope.get("strategy_type") or "")
        == _strategy_type(plan)
    )


def _plan_digest(plan: Mapping[str, Any]) -> str:
    keys = ("strategy_plan_id", "cycle_id", "version", "strategy_type", "direction", "range", "grid", "dca", "risk_budget")
    return _digest({key: plan.get(key) for key in keys})


def _proposal_plan_digest(row: Mapping[str, Any]) -> str:
    return _digest(
        {
            **{
                key: row.get(key)
                for key in _PLAN_SHAPE_FIELDS
            },
            "strategy_type": _strategy_type(row),
            "dca": dict(row.get("dca") or {}),
        }
    )


def _outer_policy_reference(
    binding: Mapping[str, Any],
    outer: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "policy_id": outer["policy_id"],
        "version": outer["version"],
        "policy_digest": outer["policy_digest"],
        "authorized_at": outer["authorized_at"],
        "expires_at": outer["expires_at"],
        "binding_id": binding["binding_id"],
        "binding_version": binding["binding_version"],
        "binding_digest": binding["binding_digest"],
        "bound_at": binding["bound_at"],
    }


def _strategy_type(
    row: Mapping[str, Any],
    code: str = "risk_envelope_authorization_invalid",
) -> str:
    value = str(row.get("strategy_type") or "grid").lower()
    if value not in {"grid", "dca"}:
        raise CycleRiskEnvelopeError(code)
    return value


def _human_actor(actor: Mapping[str, Any] | None) -> dict[str, Any]:
    source = actor if isinstance(actor, Mapping) else {}
    email = str(source.get("email") or "").strip().lower()
    if not email:
        raise CycleRiskEnvelopeError("risk_envelope_authorization_invalid")
    return {"email": email, "transport": str(source.get("transport") or "public_gateway")}


def _park_actor(actor: Mapping[str, Any] | None) -> dict[str, Any]:
    """Outer policy is a Park authorization, never a generic operator action."""

    source = actor if isinstance(actor, Mapping) else {}
    assertion = str(source.get("_access_assertion") or "").strip()
    identity = authenticated_access_identity(
        {"Cf-Access-Jwt-Assertion": assertion}
    )
    expected = os.getenv("GOLDBOT_ACCESS_EMAIL", "").strip().lower()
    if (
        not assertion
        or not isinstance(identity, Mapping)
        or not expected
        or str(identity.get("email") or "").strip().lower() != expected
        or str(source.get("email") or "").strip().lower() != expected
        or str(source.get("transport") or "") != "public_gateway"
    ):
        raise CycleRiskEnvelopeError("outer_strategy_policy_invalid")
    return {
        "email": expected,
        "transport": "public_gateway",
        "subject": str(identity.get("subject") or "") or None,
        "access_issued_at": identity.get("issued_at"),
        "access_expires_at": identity.get("expires_at"),
        "access_issuer": str(identity.get("issuer") or "") or None,
        "access_assertion_digest": hashlib.sha256(
            assertion.encode("utf-8")
        ).hexdigest(),
    }


def _rows(path: Path) -> list[dict[str, Any]]:
    try:
        loaded = load_json(path)
    except (OSError, TypeError, ValueError) as exc:
        raise CycleRiskEnvelopeError("attempt_store_corrupt") from exc
    if not isinstance(loaded, list) or any(
        not isinstance(row, dict)
        for row in loaded
    ):
        raise CycleRiskEnvelopeError("attempt_store_corrupt")
    return [dict(row) for row in loaded]


def _append_immutable_record(
    path: Path,
    *,
    record: dict[str, Any],
    identity: Mapping[str, Any],
    digest_field: str,
    conflict_code: str,
    registry_validator: Callable[[list[dict[str, Any]]], None] | None = None,
    reject_existing: bool = False,
) -> dict[str, Any]:
    """Serialize one immutable identity and durably retain every prior row."""

    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            rows = _rows(path)
            if registry_validator is not None:
                registry_validator(rows)
            prior = next(
                (
                    row
                    for row in rows
                    if all(
                        row.get(key) == value
                        for key, value in identity.items()
                    )
                ),
                None,
            )
            if prior is not None:
                if reject_existing:
                    raise CycleRiskEnvelopeError(conflict_code)
                if prior.get(digest_field) != record.get(digest_field):
                    raise CycleRiskEnvelopeError(conflict_code)
                return prior
            candidate_rows = [*rows, record]
            if registry_validator is not None:
                registry_validator(candidate_rows)
            rows = candidate_rows
            write_json(path, rows)
            with path.open("rb") as persisted:
                os.fsync(persisted.fileno())
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            return record
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _validate_envelope_registry(rows: list[dict[str, Any]]) -> None:
    identities: set[str] = set()
    expected_fields = {
        "schema_version",
        "cycle_id",
        "strategy_plan_id",
        "strategy_plan_version",
        "strategy_type",
        "direction",
        "plan_digest",
        "source_proposal",
        "authorization_kind",
        "limits",
        "authorized_at",
        "actor",
        "outer_policy",
        "outer_policy_comparisons",
        "envelope_authorization_id",
        "authorization_digest",
    }
    for row in rows:
        if set(row) != expected_fields:
            raise CycleRiskEnvelopeError(
                "risk_envelope_authorization_invalid"
            )
        identity = _required_text(
            row.get("envelope_authorization_id"),
            "risk_envelope_authorization_invalid",
        )
        if identity in identities:
            raise CycleRiskEnvelopeError(
                "risk_envelope_authorization_invalid"
            )
        identities.add(identity)
        expected_identity = _digest(
            {
                key: value
                for key, value in row.items()
                if key not in {
                    "envelope_authorization_id",
                    "authorization_digest",
                }
            }
        )[:40]
        if (
            row.get("schema_version") != ENVELOPE_SCHEMA
            or identity != expected_identity
            or row.get("authorization_digest")
            != _digest(
                {
                    key: value
                    for key, value in row.items()
                    if key != "authorization_digest"
                }
            )
        ):
            raise CycleRiskEnvelopeError(
                "risk_envelope_authorization_invalid"
            )
        _required_text(
            row.get("cycle_id"),
            "risk_envelope_authorization_invalid",
        )
        strategy_type = _strategy_type(row)
        _direction(
            row.get("direction"),
            "risk_envelope_authorization_invalid",
        )
        _canonical_limits(
            strategy_type,
            row.get("limits"),
            code="risk_envelope_authorization_invalid",
        )
        _utc_timestamp(
            row.get("authorized_at"),
            "risk_envelope_authorization_invalid",
        )
        kind = str(row.get("authorization_kind") or "")
        if kind not in AUTHORIZATION_KINDS:
            raise CycleRiskEnvelopeError(
                "risk_envelope_authorization_invalid"
            )
        source = row.get("source_proposal")
        if source is None:
            _required_text(
                row.get("strategy_plan_id"),
                "risk_envelope_authorization_invalid",
            )
            _positive_int(
                row.get("strategy_plan_version"),
                "risk_envelope_authorization_invalid",
            )
            _required_digest(
                row.get("plan_digest"),
                "risk_envelope_authorization_invalid",
            )
        else:
            if (
                kind
                != "ai_policy_within_preapproved_strategy_boundary"
                or not isinstance(source, Mapping)
                or set(source)
                != {
                    "proposal_id",
                    "proposal_digest",
                    "preview_id",
                    "preview_digest",
                    "execution_shape_digest",
                }
            ):
                raise CycleRiskEnvelopeError(
                    "risk_envelope_authorization_invalid"
                )
            _required_text(
                source.get("proposal_id"),
                "risk_envelope_authorization_invalid",
            )
            _required_digest(
                source.get("proposal_digest"),
                "risk_envelope_authorization_invalid",
            )
            _required_text(
                source.get("preview_id"),
                "risk_envelope_authorization_invalid",
            )
            _required_digest(
                source.get("preview_digest"),
                "risk_envelope_authorization_invalid",
            )
            if strategy_type == "dca":
                _required_digest(
                    source.get("execution_shape_digest"),
                    "risk_envelope_authorization_invalid",
                )
            elif source.get("execution_shape_digest") is not None:
                raise CycleRiskEnvelopeError(
                    "risk_envelope_authorization_invalid"
                )
            if any(
                row.get(field) is not None
                for field in (
                    "strategy_plan_id",
                    "strategy_plan_version",
                    "plan_digest",
                )
            ):
                raise CycleRiskEnvelopeError(
                    "risk_envelope_authorization_invalid"
                )
        actor = row.get("actor")
        if not isinstance(actor, Mapping):
            raise CycleRiskEnvelopeError(
                "risk_envelope_authorization_invalid"
            )
        outer = row.get("outer_policy")
        comparisons = row.get("outer_policy_comparisons")
        if kind == "human_explicit":
            if outer is not None or comparisons != []:
                raise CycleRiskEnvelopeError(
                    "risk_envelope_authorization_invalid"
                )
        elif (
            not isinstance(outer, Mapping)
            or not isinstance(comparisons, list)
            or not comparisons
            or any(
                not isinstance(comparison, Mapping)
                or comparison.get("pass") is not True
                for comparison in comparisons
            )
        ):
            raise CycleRiskEnvelopeError(
                "risk_envelope_authorization_invalid"
            )


def _validate_start_verification_registry(
    rows: list[dict[str, Any]],
) -> None:
    identities: set[str] = set()
    for row in rows:
        if (
            row.get("schema_version")
            != "cycle-risk-envelope-start-verification-v1"
        ):
            raise CycleRiskEnvelopeError(
                "risk_envelope_authorization_invalid"
            )
        identity = _required_text(
            row.get("verification_record_id"),
            "risk_envelope_authorization_invalid",
        )
        if identity in identities:
            raise CycleRiskEnvelopeError(
                "risk_envelope_authorization_invalid"
            )
        identities.add(identity)
        expected = _digest(
            {
                key: value
                for key, value in row.items()
                if key != "verification_record_id"
            }
        )[:40]
        if identity != expected:
            raise CycleRiskEnvelopeError(
                "risk_envelope_authorization_invalid"
            )


def _validate_policy_registry(rows: list[dict[str, Any]]) -> None:
    identities: set[tuple[str, int]] = set()
    expected_fields = {
        "schema_version",
        "policy_id",
        "version",
        "strategy_type",
        "direction",
        "summary",
        "limits",
        "authorized_at",
        "expires_at",
        "actor",
        "policy_digest",
    }
    for row in rows:
        try:
            if set(row) != expected_fields:
                raise CycleRiskEnvelopeError("outer_strategy_policy_invalid")
            policy_id = _required_text(
                row.get("policy_id"),
                "outer_strategy_policy_invalid",
            )
            version = _positive_int(
                row.get("version"),
                "outer_strategy_policy_invalid",
            )
            identity = (policy_id, version)
            if identity in identities:
                raise CycleRiskEnvelopeError("outer_strategy_policy_invalid")
            identities.add(identity)
            if (
                row.get("schema_version") != OUTER_POLICY_SCHEMA
                or row.get("policy_digest")
                != _digest(
                    {
                        key: value
                        for key, value in row.items()
                        if key != "policy_digest"
                    }
                )
            ):
                raise CycleRiskEnvelopeError("outer_strategy_policy_invalid")
            strategy_type = _strategy_type(
                row,
                "outer_strategy_policy_invalid",
            )
            _direction(
                row.get("direction"),
                "outer_strategy_policy_invalid",
            )
            _required_text(
                row.get("summary"),
                "outer_strategy_policy_invalid",
            )
            _canonical_limits(
                strategy_type,
                row.get("limits"),
                code="outer_strategy_policy_invalid",
            )
            authorized_at = _utc_timestamp(
                row.get("authorized_at"),
                "outer_strategy_policy_invalid",
            )
            expires_at = _utc_timestamp(
                row.get("expires_at"),
                "outer_strategy_policy_invalid",
            )
            if _parse_timestamp(expires_at) <= _parse_timestamp(authorized_at):
                raise CycleRiskEnvelopeError("outer_strategy_policy_invalid")
            _validate_park_actor_record(row.get("actor"))
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, CycleRiskEnvelopeError):
                raise
            raise CycleRiskEnvelopeError(
                "outer_strategy_policy_invalid"
            ) from exc


def _validate_binding_registry(rows: list[dict[str, Any]]) -> None:
    identities: set[tuple[str, int]] = set()
    expected_fields = {
        "schema_version",
        "binding_id",
        "binding_version",
        "policy_id",
        "policy_version",
        "policy_digest",
        "policy_expires_at",
        "summary",
        "bound_at",
        "actor",
        "binding_digest",
    }
    for row in rows:
        if set(row) != expected_fields:
            raise CycleRiskEnvelopeError("outer_strategy_policy_invalid")
        binding_id = _required_text(
            row.get("binding_id"),
            "outer_strategy_policy_invalid",
        )
        binding_version = _positive_int(
            row.get("binding_version"),
            "outer_strategy_policy_invalid",
        )
        identity = (binding_id, binding_version)
        if identity in identities:
            raise CycleRiskEnvelopeError("outer_strategy_policy_invalid")
        identities.add(identity)
        if (
            row.get("schema_version") != OUTER_POLICY_BINDING_SCHEMA
            or row.get("binding_digest")
            != _digest(
                {
                    key: value
                    for key, value in row.items()
                    if key != "binding_digest"
                }
            )
        ):
            raise CycleRiskEnvelopeError("outer_strategy_policy_invalid")
        _required_text(
            row.get("policy_id"),
            "outer_strategy_policy_invalid",
        )
        _positive_int(
            row.get("policy_version"),
            "outer_strategy_policy_invalid",
        )
        _required_digest(
            row.get("policy_digest"),
            "outer_strategy_policy_invalid",
        )
        _utc_timestamp(
            row.get("policy_expires_at"),
            "outer_strategy_policy_invalid",
        )
        _required_text(
            row.get("summary"),
            "outer_strategy_policy_invalid",
        )
        _utc_timestamp(
            row.get("bound_at"),
            "outer_strategy_policy_invalid",
        )
        _validate_park_actor_record(row.get("actor"))


def _validate_park_actor_record(value: Any) -> None:
    if not isinstance(value, Mapping) or set(value) != {
        "email",
        "transport",
        "subject",
        "access_issued_at",
        "access_expires_at",
        "access_issuer",
        "access_assertion_digest",
    }:
        raise CycleRiskEnvelopeError("outer_strategy_policy_invalid")
    if (
        not str(value.get("email") or "").strip().lower()
        or value.get("transport") != "public_gateway"
    ):
        raise CycleRiskEnvelopeError("outer_strategy_policy_invalid")
    _required_digest(
        value.get("access_assertion_digest"),
        "outer_strategy_policy_invalid",
    )
    for field in ("access_issued_at", "access_expires_at"):
        number = _decimal(
            value.get(field),
            "outer_strategy_policy_invalid",
        )
        if number != number.to_integral_value():
            raise CycleRiskEnvelopeError("outer_strategy_policy_invalid")


def _timestamp(now: str | None) -> str:
    return str(now) if now else datetime.now(timezone.utc).isoformat()


def _utc_timestamp(value: Any, code: str) -> str:
    rendered = (
        str(value).strip()
        if value not in (None, "")
        else datetime.now(timezone.utc).isoformat()
    )
    parsed = _parse_timestamp(rendered, code=code)
    return parsed.astimezone(timezone.utc).isoformat()


def _parse_timestamp(
    value: str,
    *,
    code: str = "outer_strategy_policy_invalid",
) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise CycleRiskEnvelopeError(code) from exc
    if parsed.tzinfo is None:
        raise CycleRiskEnvelopeError(code)
    return parsed.astimezone(timezone.utc)


def _digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _decimal(value: Any, code: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise CycleRiskEnvelopeError(code) from exc
    if not result.is_finite() or result < 0:
        raise CycleRiskEnvelopeError(code)
    return result


def _positive_int(value: Any, code: str) -> int:
    decimal = _decimal(value, code)
    if decimal <= 0 or decimal != decimal.to_integral_value():
        raise CycleRiskEnvelopeError(code)
    return int(decimal)


def _required_text(value: Any, code: str) -> str:
    rendered = str(value or "").strip()
    if not rendered:
        raise CycleRiskEnvelopeError(code)
    return rendered


def _required_digest(value: Any, code: str) -> str:
    rendered = str(value or "").strip().lower()
    if len(rendered) != 64 or any(
        character not in "0123456789abcdef"
        for character in rendered
    ):
        raise CycleRiskEnvelopeError(code)
    return rendered


def _direction(value: Any, code: str) -> str:
    rendered = _required_text(value, code).lower()
    if rendered not in {"long", "short", "neutral"}:
        raise CycleRiskEnvelopeError(code)
    return rendered


def _binding_ref_from_environment() -> dict[str, Any] | None:
    values = {
        "binding_id": os.getenv(
            "GRIDMIND_PAPER_SUPERVISOR_POLICY_BINDING_ID",
            "",
        ).strip(),
        "binding_version": os.getenv(
            "GRIDMIND_PAPER_SUPERVISOR_POLICY_BINDING_VERSION",
            "",
        ).strip(),
        "binding_digest": os.getenv(
            "GRIDMIND_PAPER_SUPERVISOR_POLICY_BINDING_DIGEST",
            "",
        ).strip(),
    }
    return values if any(values.values()) else None


def _safe_filename(value: str) -> str:
    rendered = "".join(character for character in value if character.isalnum() or character in {"-", "_"})
    return rendered or "unscoped"
