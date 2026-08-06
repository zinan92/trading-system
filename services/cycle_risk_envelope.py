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
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping

from services.cloud_ai_provider import validate_provider_readiness_proof

from services.cloud_access_gateway import authenticated_access_identity
from services.journal_store import load_json, write_json
from services.paper_release_receipt import current_source_attestation
from services.paper_supervisor_recovery import (
    PAPER_CONTINUITY_PROPOSAL_SOURCE,
)
from services.supervisor_execution_profile import (
    EXECUTION_PROFILES,
    FAIL_CLOSED,
    PAPER_CONTINUOUS,
)


OUTER_POLICY_SCHEMA_V1 = "paper-strategy-policy-boundary-v1"
OUTER_POLICY_SCHEMA_V2 = "paper-strategy-policy-boundary-v2"
# Backward-compatible import for callers and tests that name the original
# single-direction schema.  New records select v2 explicitly in their payload.
OUTER_POLICY_SCHEMA = OUTER_POLICY_SCHEMA_V1
OUTER_POLICY_BINDING_SCHEMA = "paper-supervisor-policy-binding-v1"
ENVELOPE_SCHEMA = "cycle-risk-envelope-v1"
OUTER_POLICY_REJECTION_SCHEMA = (
    "paper-supervisor-outer-policy-rejection-v1"
)
OUTER_POLICY_RECHECK_SCHEMA = (
    "paper-supervisor-outer-policy-recheck-v1"
)
LEGACY_REJECTION_RESOLUTION_SCHEMA = (
    "paper-supervisor-legacy-policy-rejection-resolution-v1"
)
PAPER_POLICY_RENEWAL_SCHEMA = "paper-continuity-policy-renewal-v1"
PAPER_POLICY_RENEWAL_SECONDS = 24 * 60 * 60
AUTHORIZATION_KINDS = {
    "human_explicit",
    "ai_policy_within_preapproved_strategy_boundary",
    "paper_continuity_within_preapproved_strategy_boundary",
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
_UNBOUNDED = "unbounded"
_DIRECTION_ORDER = ("long", "neutral", "short")
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

    def __init__(
        self,
        code: str,
        *,
        evidence: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.evidence = (
            dict(evidence)
            if isinstance(evidence, Mapping)
            else None
        )


class CycleRiskEnvelopeStore:
    """Append-only persistence and exact comparison for Paper envelopes."""

    def __init__(
        self,
        output_root: Path,
        *,
        supervisor_policy_binding_ref: Mapping[str, Any] | None = None,
        authorization_clock: Callable[[], str] | None = None,
        execution_profile: str = FAIL_CLOSED,
        source_attestation: Callable[[], Mapping[str, Any]] | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.root = self.output_root / "dualtrack" / "supervisor" / "risk_envelopes"
        self.policy_root = self.root / "outer_strategy_policies"
        self.binding_root = self.root / "outer_strategy_policy_bindings"
        self.verification_root = self.root / "start_verifications"
        self.rejection_root = self.root / "outer_policy_rejections"
        self.legacy_resolution_root = (
            self.root / "outer_policy_rejection_resolutions"
        )
        self.paper_policy_renewal_root = (
            self.root / "paper_continuity_policy_renewals"
        )
        self.supervisor_policy_binding_ref = (
            dict(supervisor_policy_binding_ref)
            if isinstance(supervisor_policy_binding_ref, Mapping)
            else _binding_ref_from_environment()
        )
        self.authorization_clock = authorization_clock
        profile = str(execution_profile or "")
        if profile not in EXECUTION_PROFILES:
            raise CycleRiskEnvelopeError("outer_strategy_policy_invalid")
        self.execution_profile = profile
        self.source_attestation = source_attestation or (
            lambda: current_source_attestation()
        )

    def renew_expired_policy_for_paper_continuity(
        self,
        *,
        now: str | None = None,
    ) -> dict[str, Any]:
        """Append a source-bound Paper-only receipt without editing Park policy."""

        if self.execution_profile != PAPER_CONTINUOUS:
            raise CycleRiskEnvelopeError("outer_strategy_policy_expired")
        renewed_at = _utc_timestamp(
            now
            or (
                self.authorization_clock()
                if self.authorization_clock
                else None
            ),
            "outer_strategy_policy_invalid",
        )
        binding, policy = self._load_bound_outer_policy_unchecked()
        if _parse_timestamp(renewed_at) < _parse_timestamp(
            str(policy["expires_at"])
        ):
            return {
                "renewed": False,
                "policy_current": True,
                "receipt": None,
            }
        existing = self._current_paper_policy_renewal(
            binding=binding,
            policy=policy,
            at=renewed_at,
            required=False,
        )
        if existing is not None:
            return {
                "renewed": False,
                "policy_current": False,
                "receipt": existing,
            }
        source = self._verified_source_attestation()
        path = self.paper_policy_renewal_root / (
            f"{_safe_filename(str(binding['binding_id']))}.json"
        )
        rows = _rows(path)
        _validate_paper_policy_renewal_registry(rows)
        previous_digest = (
            str(rows[-1].get("receipt_digest") or "") if rows else ""
        )
        expires_at = (
            _parse_timestamp(renewed_at)
            + timedelta(seconds=PAPER_POLICY_RENEWAL_SECONDS)
        ).isoformat()
        identity_seed = {
            "binding_digest": binding["binding_digest"],
            "policy_digest": policy["policy_digest"],
            "renewed_at": renewed_at,
            "source_sha": source["source_sha"],
            "source_tree_sha": source["source_tree_sha"],
        }
        record: dict[str, Any] = {
            "schema_version": PAPER_POLICY_RENEWAL_SCHEMA,
            "renewal_id": "paper-policy-renewal-"
            + _digest(identity_seed)[:24],
            "binding": {
                "binding_id": binding["binding_id"],
                "binding_version": binding["binding_version"],
                "binding_digest": binding["binding_digest"],
            },
            "policy": {
                "policy_id": policy["policy_id"],
                "policy_version": policy["version"],
                "policy_digest": policy["policy_digest"],
                "original_expires_at": policy["expires_at"],
            },
            "execution_profile": PAPER_CONTINUOUS,
            "scope": "paper_only",
            "real_money_eligible": False,
            "renewed_at": renewed_at,
            "expires_at": expires_at,
            "source": source,
            "previous_receipt_digest": previous_digest,
        }
        record["receipt_digest"] = _digest(record)
        try:
            receipt = _append_immutable_record(
                path,
                record=record,
                identity={"renewal_id": record["renewal_id"]},
                digest_field="receipt_digest",
                conflict_code="outer_strategy_policy_invalid",
                registry_validator=_validate_paper_policy_renewal_registry,
            )
        except CycleRiskEnvelopeError:
            # Another Supervisor process may have renewed the same exact
            # source/policy while this process waited on the registry lock.
            # Adopt only a receipt that independently verifies at this instant.
            concurrent = self._current_paper_policy_renewal(
                binding=binding,
                policy=policy,
                at=renewed_at,
                required=False,
            )
            if concurrent is None:
                raise
            return {
                "renewed": False,
                "policy_current": False,
                "receipt": concurrent,
            }
        return {
            "renewed": True,
            "policy_current": False,
            "receipt": receipt,
        }

    def paper_continuity_outer_policy(
        self,
        *,
        at: str | None = None,
    ) -> dict[str, Any]:
        """Return the exact bound policy after profile-scoped validity proof."""

        checked_at = _utc_timestamp(
            at
            or (
                self.authorization_clock()
                if self.authorization_clock
                else None
            ),
            "outer_strategy_policy_invalid",
        )
        _, policy = self._load_bound_outer_policy(at=checked_at)
        return dict(policy)

    def authorize_outer_policy(
        self,
        *,
        payload: Mapping[str, Any],
        actor: Mapping[str, Any] | None,
        now: str | None = None,
    ) -> dict[str, Any]:
        """Persist a Park-explicit outer strategy boundary exactly once."""

        actor_row = _park_actor(actor)
        schema_version = str(
            payload.get("schema_version") or OUTER_POLICY_SCHEMA_V1
        ).strip()
        v1_fields = {
            "policy_id",
            "version",
            "strategy_type",
            "direction",
            "summary",
            "expires_at",
            "limits",
        }
        v2_fields = {
            "schema_version",
            "policy_id",
            "version",
            "strategy_type",
            "allowed_directions",
            "summary",
        }
        if schema_version == OUTER_POLICY_SCHEMA_V1:
            if frozenset(payload) not in {
                frozenset(v1_fields),
                frozenset(v1_fields | {"schema_version"}),
            }:
                raise CycleRiskEnvelopeError("outer_strategy_policy_invalid")
        elif schema_version == OUTER_POLICY_SCHEMA_V2:
            if set(payload) != v2_fields:
                raise CycleRiskEnvelopeError("outer_strategy_policy_invalid")
        else:
            raise CycleRiskEnvelopeError("outer_strategy_policy_invalid")
        strategy_type = _strategy_type(payload)
        allowed_directions: list[str] | None = None
        if (
            schema_version == OUTER_POLICY_SCHEMA_V2
            and strategy_type != "grid"
        ):
            raise CycleRiskEnvelopeError("outer_strategy_policy_invalid")
        if schema_version == OUTER_POLICY_SCHEMA_V2:
            allowed_directions = _allowed_directions(
                payload.get("allowed_directions"),
                "outer_strategy_policy_invalid",
            )
        policy_id = _required_text(payload.get("policy_id"), "outer_strategy_policy_invalid")
        version = _positive_int(payload.get("version"), "outer_strategy_policy_invalid")
        authorized_at = _utc_timestamp(
            self.authorization_clock() if self.authorization_clock else now,
            "outer_strategy_policy_invalid",
        )
        inherited_from: dict[str, Any] | None = None
        if schema_version == OUTER_POLICY_SCHEMA_V1:
            limits = _canonical_limits(
                strategy_type,
                payload.get("limits"),
                code="outer_strategy_policy_invalid",
                allow_unbounded_grid_count=True,
            )
            expires_at = _utc_timestamp(
                _required_text(
                    payload.get("expires_at"),
                    "outer_strategy_policy_invalid",
                ),
                "outer_strategy_policy_invalid",
            )
        else:
            source_binding, source_policy = self._load_bound_outer_policy(
                at=authorized_at
            )
            if (
                source_policy.get("schema_version")
                != OUTER_POLICY_SCHEMA_V1
                or str(source_policy.get("strategy_type") or "")
                != "grid"
            ):
                raise CycleRiskEnvelopeError(
                    "outer_strategy_policy_invalid"
                )
            limits = dict(source_policy["limits"])
            expires_at = str(source_policy["expires_at"])
            inherited_from = _outer_policy_inheritance_reference(
                source_binding,
                source_policy,
            )
        if _parse_timestamp(expires_at) <= _parse_timestamp(authorized_at):
            raise CycleRiskEnvelopeError("outer_strategy_policy_invalid")
        record = {
            "schema_version": schema_version,
            "policy_id": policy_id,
            "version": version,
            "strategy_type": strategy_type,
            "summary": _required_text(payload.get("summary"), "outer_strategy_policy_invalid"),
            "limits": limits,
            "authorized_at": authorized_at,
            "expires_at": expires_at,
            "actor": actor_row,
        }
        if schema_version == OUTER_POLICY_SCHEMA_V1:
            record["direction"] = _direction(
                payload.get("direction"),
                "outer_strategy_policy_invalid",
            )
        else:
            record["allowed_directions"] = allowed_directions
            record["inherited_from"] = inherited_from
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
        if policy.get("schema_version") == OUTER_POLICY_SCHEMA_V2:
            current_binding, current_policy = (
                self._load_bound_outer_policy(at=bound_at)
            )
            if (
                current_policy.get("schema_version")
                != OUTER_POLICY_SCHEMA_V1
                or policy.get("inherited_from")
                != _outer_policy_inheritance_reference(
                    current_binding,
                    current_policy,
                )
            ):
                raise CycleRiskEnvelopeError(
                    "outer_strategy_policy_invalid"
                )
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
        supervisor_attempt_id: str | None = None,
        now: str | None = None,
    ) -> dict[str, Any]:
        """Authorize an AI candidate before any production plan is written."""

        authorized_at = _utc_timestamp(
            now,
            "risk_envelope_authorization_invalid",
        )
        candidate = self.supervisor_candidate_identity(
            cycle_id=cycle_id,
            proposal=proposal,
            preview=preview,
            supervisor_attempt_id=supervisor_attempt_id,
        )
        proposal_id = str(candidate["proposal_id"])
        strategy_type = str(candidate["strategy_type"])
        direction = str(candidate["direction"])
        limits = dict(candidate["limits"])
        binding, outer = self._load_bound_outer_policy(at=authorized_at)
        comparisons = _compare_outer_policy(
            outer,
            limits,
            strategy_type=strategy_type,
            direction=direction,
        )
        if not all(row["pass"] for row in comparisons):
            if supervisor_attempt_id:
                rejection = self._persist_outer_policy_rejection(
                    cycle_id=cycle_id,
                    candidate=candidate,
                    binding=binding,
                    outer_policy=outer,
                    comparisons=comparisons,
                    rejected_at=authorized_at,
                )
                raise CycleRiskEnvelopeError(
                    "outer_strategy_policy_envelope_out_of_bounds",
                    evidence={
                        "rejection_id": rejection["rejection_id"],
                        "rejection_digest": rejection[
                            "rejection_digest"
                        ],
                    },
                )
            raise CycleRiskEnvelopeError(
                "outer_strategy_policy_envelope_out_of_bounds"
            )
        source_proposal = {
            "proposal_id": proposal_id,
            "proposal_digest": candidate["proposal_digest"],
            "preview_id": candidate["preview_id"],
            "preview_digest": candidate["preview_digest"],
            "execution_shape_digest": (
                _preview_execution_shape_digest(preview)
                if strategy_type == "dca"
                else None
            ),
        }
        recovery_source = (
            proposal.get("source") == PAPER_CONTINUITY_PROPOSAL_SOURCE
        )
        if proposal.get("provider_readiness") is not None:
            source_proposal["provider_readiness"] = (
                validate_provider_readiness_proof(
                    proposal.get("provider_readiness") or {}
                )
            )
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
                "paper_continuity_within_preapproved_strategy_boundary"
                if recovery_source
                else "ai_policy_within_preapproved_strategy_boundary"
            ),
            "limits": limits,
            "authorized_at": authorized_at,
            "actor": {
                "email": None,
                "transport": (
                    PAPER_CONTINUITY_PROPOSAL_SOURCE
                    if recovery_source
                    else "ai_policy"
                ),
            },
            "outer_policy": _outer_policy_reference(binding, outer),
            "outer_policy_comparisons": comparisons,
        }
        return self._persist_envelope(record)

    def supervisor_candidate_identity(
        self,
        *,
        cycle_id: str,
        proposal: Mapping[str, Any],
        preview: Mapping[str, Any],
        supervisor_attempt_id: str | None = None,
    ) -> dict[str, Any]:
        """Return the exact candidate identity persisted before authorization."""

        source = str(proposal.get("source") or "")
        recovery_analysis = dict(proposal.get("analysis") or {})
        recovery_source = (
            self.execution_profile == PAPER_CONTINUOUS
            and source == PAPER_CONTINUITY_PROPOSAL_SOURCE
            and recovery_analysis.get("new_ai_judgment") is False
            and recovery_analysis.get("inherited_intent_source") == "ai"
            and bool(proposal.get("recovery_proposal_digest"))
        )
        if (
            str(proposal.get("cycle_id") or "") != str(cycle_id)
            or (source != "ai" and not recovery_source)
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
        confirmation = (
            dict(preview.get("manual_confirmation") or {})
            if isinstance(preview.get("manual_confirmation"), Mapping)
            else {}
        )
        facts_digest = str(confirmation.get("facts_digest") or "") or None
        identity: dict[str, Any] = {
            "proposal_id": proposal_id,
            "proposal_digest": _proposal_plan_digest(proposal),
            "preview_id": _required_text(
                preview.get("preview_id"),
                "plan_identity_conflict",
            ),
            "preview_digest": _digest(dict(preview)),
            "facts_digest": facts_digest,
            "confirmation_digest": (
                _digest(confirmation) if confirmation else None
            ),
            "strategy_type": strategy_type,
            "direction": direction,
            "limits": _limits_from_preview(preview, strategy_type),
        }
        if supervisor_attempt_id is not None:
            identity["supervisor_attempt_id"] = _required_text(
                supervisor_attempt_id,
                "outer_strategy_policy_invalid",
            )
        return identity

    def _persist_outer_policy_rejection(
        self,
        *,
        cycle_id: str,
        candidate: Mapping[str, Any],
        binding: Mapping[str, Any],
        outer_policy: Mapping[str, Any],
        comparisons: list[dict[str, Any]],
        rejected_at: str,
    ) -> dict[str, Any]:
        """Durably record a deny-only candidate decision before raising."""

        candidate = dict(candidate)
        if "supervisor_attempt_id" not in candidate:
            raise CycleRiskEnvelopeError("outer_strategy_policy_invalid")
        record: dict[str, Any] = {
            "schema_version": OUTER_POLICY_REJECTION_SCHEMA,
            "cycle_id": _required_text(
                cycle_id,
                "plan_identity_conflict",
            ),
            "rejected_at": _utc_timestamp(
                rejected_at,
                "outer_strategy_policy_invalid",
            ),
            "machine_code": (
                "outer_strategy_policy_envelope_out_of_bounds"
            ),
            "authorization_effect": "deny_only",
            "candidate": candidate,
            "outer_policy": _outer_policy_reference(
                binding,
                outer_policy,
            ),
            "comparisons": [dict(row) for row in comparisons],
            "passed": False,
        }
        record["rejection_id"] = (
            "outer-policy-rejection-"
            + _digest(record)[:32]
        )
        record["rejection_digest"] = _digest(record)
        return _append_immutable_record(
            self.rejection_root
            / f"{_safe_filename(str(cycle_id))}.json",
            record=record,
            identity={"rejection_id": record["rejection_id"]},
            digest_field="rejection_digest",
            conflict_code="attempt_store_corrupt",
            registry_validator=_validate_outer_policy_rejection_registry,
        )

    def outer_policy_rejection(
        self,
        *,
        cycle_id: str,
        rejection_id: str,
        rejection_digest: str,
    ) -> dict[str, Any]:
        """Load one exact deny-only decision and reject ambiguity/tampering."""

        rows = _rows(
            self.rejection_root
            / f"{_safe_filename(cycle_id)}.json"
        )
        _validate_outer_policy_rejection_registry(rows)
        matches = [
            row
            for row in rows
            if row.get("rejection_id") == rejection_id
        ]
        if (
            len(matches) != 1
            or matches[0].get("cycle_id") != cycle_id
            or matches[0].get("rejection_digest")
            != rejection_digest
        ):
            raise CycleRiskEnvelopeError("attempt_store_corrupt")
        return dict(matches[0])

    def outer_policy_rejection_for_attempt(
        self,
        *,
        cycle_id: str,
        supervisor_attempt_id: str,
    ) -> dict[str, Any]:
        """Load the sole rejection for one attempt or fail closed."""

        match = self.find_outer_policy_rejection_for_attempt(
            cycle_id=cycle_id,
            supervisor_attempt_id=supervisor_attempt_id,
        )
        if match is None:
            raise CycleRiskEnvelopeError("attempt_store_corrupt")
        return match

    def find_outer_policy_rejection_for_attempt(
        self,
        *,
        cycle_id: str,
        supervisor_attempt_id: str,
    ) -> dict[str, Any] | None:
        """Return an optional unique rejection for an in-process outcome join."""

        attempt_id = _required_text(
            supervisor_attempt_id,
            "attempt_store_corrupt",
        )
        path = self.rejection_root / f"{_safe_filename(cycle_id)}.json"
        rows = _rows(path) if path.exists() else []
        _validate_outer_policy_rejection_registry(rows)
        matches = [
            dict(row)
            for row in rows
            if dict(row.get("candidate") or {}).get(
                "supervisor_attempt_id"
            )
            == attempt_id
        ]
        if len(matches) > 1:
            raise CycleRiskEnvelopeError("attempt_store_corrupt")
        return matches[0] if matches else None

    def recheck_outer_policy_rejection(
        self,
        *,
        cycle_id: str,
        rejection_id: str,
        rejection_digest: str,
        at: str | None = None,
    ) -> dict[str, Any]:
        """Compare the stored candidate with a different current binding."""

        checked_at = _utc_timestamp(
            self.authorization_clock()
            if self.authorization_clock
            else at,
            "outer_strategy_policy_invalid",
        )
        rejection = self.outer_policy_rejection(
            cycle_id=cycle_id,
            rejection_id=rejection_id,
            rejection_digest=rejection_digest,
        )
        recorded_outer = dict(rejection["outer_policy"])
        source_binding = self._load_binding_exact(
            binding_id=str(recorded_outer["binding_id"]),
            binding_version=int(recorded_outer["binding_version"]),
        )
        source_policy = self._load_outer_policy_exact(
            policy_id=str(recorded_outer["policy_id"]),
            version=int(recorded_outer["version"]),
        )
        if _outer_policy_reference(
            source_binding,
            source_policy,
        ) != recorded_outer:
            raise CycleRiskEnvelopeError("attempt_store_corrupt")
        current_binding, current_policy = self._load_bound_outer_policy(
            at=checked_at
        )
        candidate = dict(rejection["candidate"])
        comparisons = _compare_outer_policy(
            current_policy,
            candidate["limits"],
            strategy_type=str(candidate["strategy_type"]),
            direction=str(candidate["direction"]),
        )
        binding_changed = (
            current_binding["binding_digest"]
            != source_binding["binding_digest"]
        )
        result: dict[str, Any] = {
            "schema_version": OUTER_POLICY_RECHECK_SCHEMA,
            "checked_at": checked_at,
            "cycle_id": cycle_id,
            "rejection_id": rejection_id,
            "rejection_digest": rejection_digest,
            "prior_outer_policy": recorded_outer,
            "current_outer_policy": _outer_policy_reference(
                current_binding,
                current_policy,
            ),
            "binding_changed": binding_changed,
            "comparisons": comparisons,
            "passed": bool(
                binding_changed
                and all(row["pass"] for row in comparisons)
            ),
            "control_actions_executed": 0,
        }
        result["recheck_digest"] = _digest(result)
        return result

    def verify_outer_policy_recheck_proof(
        self,
        *,
        proof: Mapping[str, Any],
        cycle_id: str,
        rejection_id: str,
        rejection_digest: str,
    ) -> dict[str, Any]:
        """Strictly verify a read-only recheck proof before clearance."""

        expected_fields = {
            "schema_version",
            "checked_at",
            "cycle_id",
            "rejection_id",
            "rejection_digest",
            "prior_outer_policy",
            "current_outer_policy",
            "binding_changed",
            "comparisons",
            "passed",
            "control_actions_executed",
            "recheck_digest",
        }
        result = dict(proof)
        if (
            set(result) != expected_fields
            or result.get("schema_version")
            != OUTER_POLICY_RECHECK_SCHEMA
            or result.get("cycle_id") != cycle_id
            or result.get("rejection_id") != rejection_id
            or result.get("rejection_digest") != rejection_digest
            or result.get("control_actions_executed") != 0
            or result.get("recheck_digest")
            != _digest(
                {
                    key: value
                    for key, value in result.items()
                    if key != "recheck_digest"
                }
            )
        ):
            raise CycleRiskEnvelopeError("attempt_store_corrupt")
        checked_at = _utc_timestamp(
            result.get("checked_at"),
            "attempt_store_corrupt",
        )
        rejection = self.outer_policy_rejection(
            cycle_id=cycle_id,
            rejection_id=rejection_id,
            rejection_digest=rejection_digest,
        )
        prior = _validate_outer_policy_reference(
            result.get("prior_outer_policy"),
            code="attempt_store_corrupt",
        )
        current = _validate_outer_policy_reference(
            result.get("current_outer_policy"),
            code="attempt_store_corrupt",
        )
        if prior != rejection["outer_policy"]:
            raise CycleRiskEnvelopeError("attempt_store_corrupt")
        prior_binding = self._load_binding_exact(
            binding_id=str(prior["binding_id"]),
            binding_version=int(prior["binding_version"]),
        )
        prior_policy = self._load_outer_policy_exact(
            policy_id=str(prior["policy_id"]),
            version=int(prior["version"]),
        )
        if _outer_policy_reference(
            prior_binding,
            prior_policy,
        ) != prior:
            raise CycleRiskEnvelopeError("attempt_store_corrupt")
        binding, policy = self._load_bound_outer_policy(at=checked_at)
        if _outer_policy_reference(binding, policy) != current:
            raise CycleRiskEnvelopeError("attempt_store_corrupt")
        candidate = dict(rejection["candidate"])
        expected_comparisons = _compare_outer_policy(
            policy,
            candidate["limits"],
            strategy_type=str(candidate["strategy_type"]),
            direction=str(candidate["direction"]),
        )
        binding_changed = (
            current["binding_digest"] != prior["binding_digest"]
        )
        passed = bool(
            binding_changed
            and all(row["pass"] for row in expected_comparisons)
        )
        if (
            result.get("binding_changed") is not binding_changed
            or result.get("comparisons") != expected_comparisons
            or result.get("passed") is not passed
        ):
            raise CycleRiskEnvelopeError("attempt_store_corrupt")
        return result

    def authorize_legacy_rejection_resolution(
        self,
        *,
        cycle_id: str,
        payload: Mapping[str, Any],
        actor: Mapping[str, Any] | None,
        now: str | None = None,
    ) -> dict[str, Any]:
        """Persist Park's one-time resolution of a receipt-less blocker."""

        expected_fields = {
            "resolution_id",
            "resolution_version",
            "blocked_at",
            "machine_code",
            "summary",
        }
        if set(payload) != expected_fields:
            raise CycleRiskEnvelopeError(
                "outer_strategy_policy_invalid"
            )
        machine_code = _required_text(
            payload.get("machine_code"),
            "outer_strategy_policy_invalid",
        )
        if machine_code != (
            "outer_strategy_policy_envelope_out_of_bounds"
        ):
            raise CycleRiskEnvelopeError(
                "outer_strategy_policy_invalid"
            )
        authorized_at = _utc_timestamp(
            self.authorization_clock()
            if self.authorization_clock
            else now,
            "outer_strategy_policy_invalid",
        )
        blocked_at = _utc_timestamp(
            payload.get("blocked_at"),
            "outer_strategy_policy_invalid",
        )
        if _parse_timestamp(blocked_at) > _parse_timestamp(
            authorized_at
        ):
            raise CycleRiskEnvelopeError(
                "outer_strategy_policy_invalid"
            )
        record = {
            "schema_version": LEGACY_REJECTION_RESOLUTION_SCHEMA,
            "resolution_id": _required_text(
                payload.get("resolution_id"),
                "outer_strategy_policy_invalid",
            ),
            "resolution_version": _positive_int(
                payload.get("resolution_version"),
                "outer_strategy_policy_invalid",
            ),
            "cycle_id": _required_text(
                cycle_id,
                "outer_strategy_policy_invalid",
            ),
            "machine_code": machine_code,
            "blocked_at": blocked_at,
            "summary": _required_text(
                payload.get("summary"),
                "outer_strategy_policy_invalid",
            ),
            "authorization_kind": (
                "park_explicit_legacy_structural_resolution"
            ),
            "authorized_at": authorized_at,
            "actor": _park_actor(actor),
        }
        record["resolution_digest"] = _digest(record)
        return _append_immutable_record(
            self.legacy_resolution_root
            / f"{_safe_filename(cycle_id)}.json",
            record=record,
            identity={
                "resolution_id": record["resolution_id"],
                "resolution_version": record[
                    "resolution_version"
                ],
            },
            digest_field="resolution_digest",
            conflict_code="outer_strategy_policy_invalid",
            registry_validator=(
                _validate_legacy_rejection_resolution_registry
            ),
        )

    def legacy_rejection_resolution(
        self,
        *,
        cycle_id: str,
        machine_code: str,
        blocked_at: str,
    ) -> dict[str, Any] | None:
        """Return the sole exact Park resolution or fail on ambiguity."""

        rows = _rows(
            self.legacy_resolution_root
            / f"{_safe_filename(cycle_id)}.json"
        )
        _validate_legacy_rejection_resolution_registry(rows)
        matches = [
            dict(row)
            for row in rows
            if row.get("cycle_id") == cycle_id
            and row.get("machine_code") == machine_code
            and row.get("blocked_at") == blocked_at
        ]
        if len(matches) > 1:
            raise CycleRiskEnvelopeError("attempt_store_corrupt")
        return matches[0] if matches else None

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
        self._require_authorization_kind_for_profile(kind)
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
        self._require_authorization_kind_for_profile(
            envelope.get("authorization_kind")
        )
        if envelope.get("authorization_kind") in {
            "ai_policy_within_preapproved_strategy_boundary",
            "paper_continuity_within_preapproved_strategy_boundary",
        }:
            self._verify_envelope_outer_policy(
                envelope,
                at=_utc_timestamp(
                    now,
                    "outer_strategy_policy_invalid",
                ),
            )
        source_proposal = envelope.get("source_proposal")
        if isinstance(source_proposal, Mapping):
            # The source proposal identifies the immutable AI candidate that
            # produced the cycle envelope.  An already-active Grid plan may
            # be prepared against a newly rebuilt execution preview (for
            # example after a transient market move).  That fresh preview
            # intentionally has a new id/facts digest; its identity is bound
            # below by the exact numeric comparisons and the later derived
            # execution-plan receipt.  DCA retains exact execution-shape
            # identity, and no-plan candidate verification still requires the
            # source proposal preview to match exactly.
            active_grid_plan = (
                bool(plan)
                and _strategy_type(plan) == "grid"
                and str(envelope.get("strategy_type") or "") == "grid"
            )
            if not active_grid_plan and (
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
        self._require_authorization_kind_for_profile(
            envelope.get("authorization_kind")
        )
        if (
            str(envelope.get("cycle_id") or "") != str(cycle_id)
            or not _matching_candidate_plan(envelope, plan)
        ):
            raise CycleRiskEnvelopeError("plan_identity_conflict")
        if envelope.get("authorization_kind") not in {
            "ai_policy_within_preapproved_strategy_boundary",
            "paper_continuity_within_preapproved_strategy_boundary",
        }:
            raise CycleRiskEnvelopeError("plan_identity_conflict")
        self._verify_envelope_outer_policy(
            envelope,
            at=_utc_timestamp(
                now,
                "outer_strategy_policy_invalid",
            ),
        )
        return dict(envelope)

    def _require_authorization_kind_for_profile(self, kind: Any) -> None:
        """Never allow a Paper recovery authority to cross profiles."""

        if (
            str(kind or "")
            == "paper_continuity_within_preapproved_strategy_boundary"
            and self.execution_profile != PAPER_CONTINUOUS
        ):
            raise CycleRiskEnvelopeError("plan_identity_conflict")

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
        binding, policy = self._load_bound_outer_policy_unchecked()
        self._require_policy_current(
            policy,
            binding=binding,
            at=at,
        )
        return binding, policy

    def _load_bound_outer_policy_unchecked(
        self,
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
        schema_version = str(policy.get("schema_version") or "")
        if (
            schema_version not in {
                OUTER_POLICY_SCHEMA_V1,
                OUTER_POLICY_SCHEMA_V2,
            }
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
        strategy_type = _strategy_type(
            policy,
            "outer_strategy_policy_invalid",
        )
        if schema_version == OUTER_POLICY_SCHEMA_V1:
            _direction(
                policy.get("direction"),
                "outer_strategy_policy_invalid",
            )
        else:
            if strategy_type != "grid":
                raise CycleRiskEnvelopeError(
                    "outer_strategy_policy_invalid"
                )
            _allowed_directions(
                policy.get("allowed_directions"),
                "outer_strategy_policy_invalid",
            )
            inherited_from = _validate_policy_inheritance_reference(
                policy.get("inherited_from")
            )
            source_binding = self._load_binding_exact(
                binding_id=inherited_from["binding_id"],
                binding_version=inherited_from["binding_version"],
            )
            if (
                source_binding["binding_digest"]
                != inherited_from["binding_digest"]
                or source_binding["policy_id"]
                != inherited_from["policy_id"]
                or source_binding["policy_version"]
                != inherited_from["policy_version"]
                or source_binding["policy_digest"]
                != inherited_from["policy_digest"]
            ):
                raise CycleRiskEnvelopeError(
                    "outer_strategy_policy_invalid"
                )
            source_rows = _rows(
                self.policy_root
                / f"{_safe_filename(inherited_from['policy_id'])}.json"
            )
            _validate_policy_registry(source_rows)
            source_row = next(
                (
                    row
                    for row in source_rows
                    if row.get("policy_id")
                    == inherited_from["policy_id"]
                    and row.get("version")
                    == inherited_from["policy_version"]
                ),
                None,
            )
            if (
                source_row is None
                or source_row.get("schema_version")
                != OUTER_POLICY_SCHEMA_V1
            ):
                raise CycleRiskEnvelopeError(
                    "outer_strategy_policy_invalid"
                )
            source_policy = self._load_outer_policy_exact(
                policy_id=inherited_from["policy_id"],
                version=inherited_from["policy_version"],
            )
            if (
                source_policy["policy_digest"]
                != inherited_from["policy_digest"]
                or source_policy["limits"] != policy.get("limits")
                or source_policy["expires_at"]
                != policy.get("expires_at")
                or source_policy["strategy_type"]
                != policy.get("strategy_type")
            ):
                raise CycleRiskEnvelopeError(
                    "outer_strategy_policy_invalid"
                )
        _canonical_limits(
            strategy_type,
            policy.get("limits"),
            code="outer_strategy_policy_invalid",
            allow_unbounded_grid_count=True,
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
        binding: Mapping[str, Any] | None = None,
        at: str,
    ) -> None:
        if _parse_timestamp(at) >= _parse_timestamp(
            _utc_timestamp(
                policy.get("expires_at"),
                "outer_strategy_policy_invalid",
            )
        ):
            if (
                self.execution_profile == PAPER_CONTINUOUS
                and isinstance(binding, Mapping)
                and self._current_paper_policy_renewal(
                    binding=binding,
                    policy=policy,
                    at=at,
                    required=False,
                )
                is not None
            ):
                return
            raise CycleRiskEnvelopeError("outer_strategy_policy_expired")

    def _current_paper_policy_renewal(
        self,
        *,
        binding: Mapping[str, Any],
        policy: Mapping[str, Any],
        at: str,
        required: bool,
    ) -> dict[str, Any] | None:
        path = self.paper_policy_renewal_root / (
            f"{_safe_filename(str(binding['binding_id']))}.json"
        )
        rows = _rows(path)
        _validate_paper_policy_renewal_registry(rows)
        try:
            source = self._verified_source_attestation()
        except CycleRiskEnvelopeError:
            if required:
                raise
            return None
        checked_at = _parse_timestamp(at)
        matching = [
            row
            for row in rows
            if row.get("execution_profile") == PAPER_CONTINUOUS
            and row.get("scope") == "paper_only"
            and row.get("real_money_eligible") is False
            and dict(row.get("binding") or {})
            == {
                "binding_id": binding["binding_id"],
                "binding_version": binding["binding_version"],
                "binding_digest": binding["binding_digest"],
            }
            and dict(row.get("policy") or {})
            == {
                "policy_id": policy["policy_id"],
                "policy_version": policy["version"],
                "policy_digest": policy["policy_digest"],
                "original_expires_at": policy["expires_at"],
            }
            and dict(row.get("source") or {}) == source
            and _parse_timestamp(str(row.get("renewed_at") or ""))
            <= checked_at
            < _parse_timestamp(str(row.get("expires_at") or ""))
        ]
        if matching:
            return dict(matching[-1])
        if required:
            raise CycleRiskEnvelopeError("outer_strategy_policy_expired")
        return None

    def _verified_source_attestation(self) -> dict[str, Any]:
        try:
            source = dict(self.source_attestation())
        except Exception as exc:  # noqa: BLE001 - source proof is mandatory.
            raise CycleRiskEnvelopeError(
                "outer_strategy_policy_invalid"
            ) from exc
        source_sha = str(source.get("source_sha") or "").lower()
        tree_sha = str(source.get("source_tree_sha") or "").lower()
        if (
            source.get("tracked_tree_clean") is not True
            or len(source_sha) != 40
            or len(tree_sha) != 40
            or any(value not in "0123456789abcdef" for value in source_sha)
            or any(value not in "0123456789abcdef" for value in tree_sha)
        ):
            raise CycleRiskEnvelopeError("outer_strategy_policy_invalid")
        return {
            "source_sha": source_sha,
            "source_tree_sha": tree_sha,
            "tracked_tree_clean": True,
        }

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
        observed = values[field]
        raw_limit = limits.get(limit_key)
        if (
            strategy_type == "grid"
            and limit_key == "max_grid_count"
            and raw_limit == _UNBOUNDED
        ):
            rows.append(
                {
                    "field": field,
                    "operator": "unbounded",
                    "authorized_limit": _UNBOUNDED,
                    "observed_value": str(observed),
                    "pass": True,
                }
            )
            continue
        limit = _decimal(raw_limit, "risk_envelope_authorization_invalid")
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
    outer_strategy_type = _strategy_type(
        outer,
        "outer_strategy_policy_invalid",
    )
    rows: list[dict[str, Any]] = [
        {
            "field": "strategy_type",
            "operator": "==",
            "authorized_limit": outer_strategy_type,
            "observed_value": strategy_type,
            "pass": outer_strategy_type == strategy_type,
        }
    ]
    schema_version = str(outer.get("schema_version") or "")
    if schema_version == OUTER_POLICY_SCHEMA_V1:
        authorized_direction = _direction(
            outer.get("direction"),
            "outer_strategy_policy_invalid",
        )
        rows.append(
            {
                "field": "direction",
                "operator": "==",
                "authorized_limit": authorized_direction,
                "observed_value": direction,
                "pass": authorized_direction == direction,
            }
        )
    elif schema_version == OUTER_POLICY_SCHEMA_V2:
        allowed_directions = _allowed_directions(
            outer.get("allowed_directions"),
            "outer_strategy_policy_invalid",
        )
        rows.append(
            {
                "field": "direction",
                "operator": "in",
                "authorized_limit": allowed_directions,
                "observed_value": direction,
                "pass": direction in allowed_directions,
            }
        )
    else:
        raise CycleRiskEnvelopeError("outer_strategy_policy_invalid")
    # A strategy mismatch is already a clean out-of-bound result.  Do not try
    # to interpret Grid limit keys as DCA keys (or vice versa), which would
    # incorrectly turn a valid policy into an invalid-policy error.
    if outer_strategy_type != strategy_type:
        return rows
    outer_limits = outer.get("limits") if isinstance(outer.get("limits"), Mapping) else {}
    for field in (_GRID_FIELDS if strategy_type == "grid" else _DCA_FIELDS):
        raw_outer_value = outer_limits.get(field)
        inner_value = _decimal(inner.get(field), "risk_envelope_authorization_invalid")
        if (
            strategy_type == "grid"
            and field == "max_grid_count"
            and raw_outer_value == _UNBOUNDED
        ):
            rows.append(
                {
                    "field": field,
                    "operator": "unbounded",
                    "authorized_limit": _UNBOUNDED,
                    "observed_value": str(inner_value),
                    "pass": True,
                }
            )
            continue
        outer_value = _decimal(raw_outer_value, "outer_strategy_policy_invalid")
        minimum = field.startswith("min_")
        passed = inner_value >= outer_value if minimum else inner_value <= outer_value
        rows.append({"field": field, "operator": ">=" if minimum else "<=", "authorized_limit": str(outer_value), "observed_value": str(inner_value), "pass": passed})
    return rows


def _canonical_limits(
    strategy_type: str,
    source: Any,
    *,
    code: str,
    allow_unbounded_grid_count: bool = False,
) -> dict[str, str]:
    if not isinstance(source, Mapping):
        raise CycleRiskEnvelopeError(code)
    fields = _GRID_FIELDS if strategy_type == "grid" else _DCA_FIELDS
    if set(source) != set(fields):
        raise CycleRiskEnvelopeError(code)
    limits: dict[str, str] = {}
    for field in fields:
        raw_value = source.get(field)
        if (
            strategy_type == "grid"
            and field == "max_grid_count"
            and raw_value == _UNBOUNDED
        ):
            if not allow_unbounded_grid_count:
                raise CycleRiskEnvelopeError(code)
            limits[field] = _UNBOUNDED
            continue
        limits[field] = str(_decimal(raw_value, code))
    min_field = "min_grid_count" if strategy_type == "grid" else "min_additions"
    max_field = "max_grid_count" if strategy_type == "grid" else "max_additions"
    if (
        limits[max_field] != _UNBOUNDED
        and _decimal(limits[min_field], code) > _decimal(limits[max_field], code)
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
        and (
            source.get("provider_readiness") is None
            or source.get("provider_readiness")
            == plan.get("provider_readiness")
        )
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


def _outer_policy_inheritance_reference(
    binding: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "binding_id": binding["binding_id"],
        "binding_version": binding["binding_version"],
        "binding_digest": binding["binding_digest"],
        "policy_id": policy["policy_id"],
        "policy_version": policy["version"],
        "policy_digest": policy["policy_digest"],
        "policy_schema_version": policy["schema_version"],
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
            legacy_source_fields = {
                "proposal_id",
                "proposal_digest",
                "preview_id",
                "preview_digest",
                "execution_shape_digest",
            }
            provider_bound_source_fields = {
                *legacy_source_fields,
                "provider_readiness",
            }
            if (
                kind
                not in {
                    "ai_policy_within_preapproved_strategy_boundary",
                    "paper_continuity_within_preapproved_strategy_boundary",
                }
                or not isinstance(source, Mapping)
                or set(source) not in {
                    frozenset(legacy_source_fields),
                    frozenset(provider_bound_source_fields),
                }
            ):
                raise CycleRiskEnvelopeError(
                    "risk_envelope_authorization_invalid"
                )
            if "provider_readiness" in source:
                try:
                    validate_provider_readiness_proof(
                        source.get("provider_readiness") or {}
                    )
                except ValueError as exc:
                    raise CycleRiskEnvelopeError(
                        "risk_envelope_authorization_invalid"
                    ) from exc
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


def _validate_outer_policy_rejection_registry(
    rows: list[dict[str, Any]],
) -> None:
    identities: set[str] = set()
    expected_fields = {
        "schema_version",
        "rejection_id",
        "rejection_digest",
        "cycle_id",
        "rejected_at",
        "machine_code",
        "authorization_effect",
        "candidate",
        "outer_policy",
        "comparisons",
        "passed",
    }
    candidate_fields = {
        "supervisor_attempt_id",
        "proposal_id",
        "proposal_digest",
        "preview_id",
        "preview_digest",
        "facts_digest",
        "confirmation_digest",
        "strategy_type",
        "direction",
        "limits",
    }
    comparison_fields = {
        "field",
        "operator",
        "authorized_limit",
        "observed_value",
        "pass",
    }
    for row in rows:
        if (
            set(row) != expected_fields
            or row.get("schema_version")
            != OUTER_POLICY_REJECTION_SCHEMA
            or row.get("machine_code")
            != "outer_strategy_policy_envelope_out_of_bounds"
            or row.get("authorization_effect") != "deny_only"
            or row.get("passed") is not False
        ):
            raise CycleRiskEnvelopeError("attempt_store_corrupt")
        rejection_id = _required_text(
            row.get("rejection_id"),
            "attempt_store_corrupt",
        )
        if rejection_id in identities:
            raise CycleRiskEnvelopeError("attempt_store_corrupt")
        identities.add(rejection_id)
        base = {
            key: value
            for key, value in row.items()
            if key not in {"rejection_id", "rejection_digest"}
        }
        if rejection_id != (
            "outer-policy-rejection-" + _digest(base)[:32]
        ):
            raise CycleRiskEnvelopeError("attempt_store_corrupt")
        if row.get("rejection_digest") != _digest(
            {
                key: value
                for key, value in row.items()
                if key != "rejection_digest"
            }
        ):
            raise CycleRiskEnvelopeError("attempt_store_corrupt")
        _required_text(row.get("cycle_id"), "attempt_store_corrupt")
        _utc_timestamp(
            row.get("rejected_at"),
            "attempt_store_corrupt",
        )
        candidate = row.get("candidate")
        if (
            not isinstance(candidate, Mapping)
            or set(candidate) != candidate_fields
        ):
            raise CycleRiskEnvelopeError("attempt_store_corrupt")
        _required_text(
            candidate.get("supervisor_attempt_id"),
            "attempt_store_corrupt",
        )
        _required_text(
            candidate.get("proposal_id"),
            "attempt_store_corrupt",
        )
        _required_digest(
            candidate.get("proposal_digest"),
            "attempt_store_corrupt",
        )
        _required_text(
            candidate.get("preview_id"),
            "attempt_store_corrupt",
        )
        _required_digest(
            candidate.get("preview_digest"),
            "attempt_store_corrupt",
        )
        facts_digest = candidate.get("facts_digest")
        if facts_digest is not None:
            _required_text(
                facts_digest,
                "attempt_store_corrupt",
            )
        confirmation_digest = candidate.get(
            "confirmation_digest"
        )
        if confirmation_digest is not None:
            _required_digest(
                confirmation_digest,
                "attempt_store_corrupt",
            )
        strategy_type = _strategy_type(
            candidate,
            "attempt_store_corrupt",
        )
        _direction(
            candidate.get("direction"),
            "attempt_store_corrupt",
        )
        limits = _canonical_limits(
            strategy_type,
            candidate.get("limits"),
            code="attempt_store_corrupt",
        )
        _validate_outer_policy_reference(
            row.get("outer_policy"),
            code="attempt_store_corrupt",
        )
        comparisons = row.get("comparisons")
        if (
            not isinstance(comparisons, list)
            or not comparisons
            or any(
                not isinstance(comparison, Mapping)
                or set(comparison) != comparison_fields
                or not isinstance(comparison.get("pass"), bool)
                for comparison in comparisons
            )
            or all(comparison["pass"] for comparison in comparisons)
        ):
            raise CycleRiskEnvelopeError("attempt_store_corrupt")
        by_field = {
            str(comparison.get("field") or ""): comparison
            for comparison in comparisons
        }
        if len(by_field) != len(comparisons):
            raise CycleRiskEnvelopeError("attempt_store_corrupt")
        strategy_comparison = by_field.get("strategy_type")
        direction_comparison = by_field.get("direction")
        if (
            strategy_comparison is None
            or direction_comparison is None
            or strategy_comparison.get("operator") != "=="
            or strategy_comparison.get("observed_value")
            != strategy_type
            or direction_comparison.get("operator")
            not in {"==", "in"}
            or direction_comparison.get("observed_value")
            != candidate.get("direction")
        ):
            raise CycleRiskEnvelopeError("attempt_store_corrupt")
        numeric_fields = set(
            _GRID_FIELDS if strategy_type == "grid" else _DCA_FIELDS
        )
        required_fields = {"strategy_type", "direction"}
        if strategy_comparison.get("pass") is True:
            required_fields |= numeric_fields
        if set(by_field) != required_fields:
            raise CycleRiskEnvelopeError("attempt_store_corrupt")
        for field in numeric_fields & set(by_field):
            comparison = by_field[field]
            if (
                comparison.get("observed_value") != limits[field]
                or comparison.get("operator")
                not in {"<=", ">=", "unbounded"}
            ):
                raise CycleRiskEnvelopeError("attempt_store_corrupt")


def _validate_legacy_rejection_resolution_registry(
    rows: list[dict[str, Any]],
) -> None:
    identities: set[tuple[str, int]] = set()
    blocker_identities: set[tuple[str, str, str]] = set()
    expected_fields = {
        "schema_version",
        "resolution_id",
        "resolution_version",
        "resolution_digest",
        "cycle_id",
        "machine_code",
        "blocked_at",
        "summary",
        "authorization_kind",
        "authorized_at",
        "actor",
    }
    actor_fields = {
        "email",
        "transport",
        "subject",
        "access_issued_at",
        "access_expires_at",
        "access_issuer",
        "access_assertion_digest",
    }
    for row in rows:
        if (
            set(row) != expected_fields
            or row.get("schema_version")
            != LEGACY_REJECTION_RESOLUTION_SCHEMA
            or row.get("machine_code")
            != "outer_strategy_policy_envelope_out_of_bounds"
            or row.get("authorization_kind")
            != "park_explicit_legacy_structural_resolution"
        ):
            raise CycleRiskEnvelopeError(
                "outer_strategy_policy_invalid"
            )
        resolution_id = _required_text(
            row.get("resolution_id"),
            "outer_strategy_policy_invalid",
        )
        version = _positive_int(
            row.get("resolution_version"),
            "outer_strategy_policy_invalid",
        )
        identity = (resolution_id, version)
        if identity in identities:
            raise CycleRiskEnvelopeError(
                "outer_strategy_policy_invalid"
            )
        identities.add(identity)
        cycle_id = _required_text(
            row.get("cycle_id"),
            "outer_strategy_policy_invalid",
        )
        blocked_at = _utc_timestamp(
            row.get("blocked_at"),
            "outer_strategy_policy_invalid",
        )
        blocker_identity = (
            cycle_id,
            str(row["machine_code"]),
            blocked_at,
        )
        if blocker_identity in blocker_identities:
            raise CycleRiskEnvelopeError(
                "outer_strategy_policy_invalid"
            )
        blocker_identities.add(blocker_identity)
        authorized_at = _utc_timestamp(
            row.get("authorized_at"),
            "outer_strategy_policy_invalid",
        )
        if _parse_timestamp(blocked_at) > _parse_timestamp(
            authorized_at
        ):
            raise CycleRiskEnvelopeError(
                "outer_strategy_policy_invalid"
            )
        _required_text(
            row.get("summary"),
            "outer_strategy_policy_invalid",
        )
        actor = row.get("actor")
        if (
            not isinstance(actor, Mapping)
            or set(actor) != actor_fields
            or actor.get("transport") != "public_gateway"
        ):
            raise CycleRiskEnvelopeError(
                "outer_strategy_policy_invalid"
            )
        _required_text(
            actor.get("email"),
            "outer_strategy_policy_invalid",
        )
        _required_digest(
            actor.get("access_assertion_digest"),
            "outer_strategy_policy_invalid",
        )
        if row.get("resolution_digest") != _digest(
            {
                key: value
                for key, value in row.items()
                if key != "resolution_digest"
            }
        ):
            raise CycleRiskEnvelopeError(
                "outer_strategy_policy_invalid"
            )


def _validate_outer_policy_reference(
    value: Any,
    *,
    code: str,
) -> dict[str, Any]:
    expected_fields = {
        "policy_id",
        "version",
        "policy_digest",
        "authorized_at",
        "expires_at",
        "binding_id",
        "binding_version",
        "binding_digest",
        "bound_at",
    }
    if not isinstance(value, Mapping) or set(value) != expected_fields:
        raise CycleRiskEnvelopeError(code)
    result = dict(value)
    _required_text(result.get("policy_id"), code)
    _positive_int(result.get("version"), code)
    _required_digest(result.get("policy_digest"), code)
    _utc_timestamp(result.get("authorized_at"), code)
    _utc_timestamp(result.get("expires_at"), code)
    _required_text(result.get("binding_id"), code)
    _positive_int(result.get("binding_version"), code)
    _required_digest(result.get("binding_digest"), code)
    _utc_timestamp(result.get("bound_at"), code)
    return result


def _validate_policy_registry(rows: list[dict[str, Any]]) -> None:
    identities: set[tuple[str, int]] = set()
    v1_fields = {
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
    v2_fields = (v1_fields - {"direction"}) | {
        "allowed_directions",
        "inherited_from",
    }
    for row in rows:
        try:
            schema_version = str(row.get("schema_version") or "")
            expected_fields = (
                v1_fields
                if schema_version == OUTER_POLICY_SCHEMA_V1
                else v2_fields
                if schema_version == OUTER_POLICY_SCHEMA_V2
                else set()
            )
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
                row.get("policy_digest")
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
            if schema_version == OUTER_POLICY_SCHEMA_V1:
                _direction(
                    row.get("direction"),
                    "outer_strategy_policy_invalid",
                )
            else:
                if strategy_type != "grid":
                    raise CycleRiskEnvelopeError(
                        "outer_strategy_policy_invalid"
                    )
                _allowed_directions(
                    row.get("allowed_directions"),
                    "outer_strategy_policy_invalid",
                )
                _validate_policy_inheritance_reference(
                    row.get("inherited_from")
                )
            _required_text(
                row.get("summary"),
                "outer_strategy_policy_invalid",
            )
            _canonical_limits(
                strategy_type,
                row.get("limits"),
                code="outer_strategy_policy_invalid",
                allow_unbounded_grid_count=True,
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


def _validate_paper_policy_renewal_registry(
    rows: list[dict[str, Any]],
) -> None:
    expected_fields = {
        "schema_version",
        "renewal_id",
        "binding",
        "policy",
        "execution_profile",
        "scope",
        "real_money_eligible",
        "renewed_at",
        "expires_at",
        "source",
        "previous_receipt_digest",
        "receipt_digest",
    }
    identities: set[str] = set()
    previous_digest = ""
    for row in rows:
        if set(row) != expected_fields:
            raise CycleRiskEnvelopeError("outer_strategy_policy_invalid")
        renewal_id = _required_text(
            row.get("renewal_id"),
            "outer_strategy_policy_invalid",
        )
        if renewal_id in identities:
            raise CycleRiskEnvelopeError("outer_strategy_policy_invalid")
        identities.add(renewal_id)
        binding = row.get("binding")
        policy = row.get("policy")
        source = row.get("source")
        if (
            row.get("schema_version") != PAPER_POLICY_RENEWAL_SCHEMA
            or row.get("execution_profile") != PAPER_CONTINUOUS
            or row.get("scope") != "paper_only"
            or row.get("real_money_eligible") is not False
            or not isinstance(binding, Mapping)
            or set(binding)
            != {"binding_id", "binding_version", "binding_digest"}
            or not isinstance(policy, Mapping)
            or set(policy)
            != {
                "policy_id",
                "policy_version",
                "policy_digest",
                "original_expires_at",
            }
            or not isinstance(source, Mapping)
            or set(source)
            != {"source_sha", "source_tree_sha", "tracked_tree_clean"}
            or source.get("tracked_tree_clean") is not True
            or row.get("previous_receipt_digest") != previous_digest
            or row.get("receipt_digest")
            != _digest(
                {
                    key: value
                    for key, value in row.items()
                    if key != "receipt_digest"
                }
            )
        ):
            raise CycleRiskEnvelopeError("outer_strategy_policy_invalid")
        _required_text(binding.get("binding_id"), "outer_strategy_policy_invalid")
        _positive_int(
            binding.get("binding_version"),
            "outer_strategy_policy_invalid",
        )
        _required_digest(
            binding.get("binding_digest"),
            "outer_strategy_policy_invalid",
        )
        _required_text(policy.get("policy_id"), "outer_strategy_policy_invalid")
        _positive_int(
            policy.get("policy_version"),
            "outer_strategy_policy_invalid",
        )
        _required_digest(
            policy.get("policy_digest"),
            "outer_strategy_policy_invalid",
        )
        _utc_timestamp(
            policy.get("original_expires_at"),
            "outer_strategy_policy_invalid",
        )
        renewed_at = _utc_timestamp(
            row.get("renewed_at"),
            "outer_strategy_policy_invalid",
        )
        expires_at = _utc_timestamp(
            row.get("expires_at"),
            "outer_strategy_policy_invalid",
        )
        expected_expires_at = (
            _parse_timestamp(renewed_at)
            + timedelta(seconds=PAPER_POLICY_RENEWAL_SECONDS)
        ).isoformat()
        expected_renewal_id = "paper-policy-renewal-" + _digest(
            {
                "binding_digest": binding["binding_digest"],
                "policy_digest": policy["policy_digest"],
                "renewed_at": renewed_at,
                "source_sha": str(source["source_sha"]).lower(),
                "source_tree_sha": str(source["source_tree_sha"]).lower(),
            }
        )[:24]
        if (
            expires_at != expected_expires_at
            or renewal_id != expected_renewal_id
        ):
            raise CycleRiskEnvelopeError("outer_strategy_policy_invalid")
        for field in ("source_sha", "source_tree_sha"):
            value = str(source.get(field) or "").lower()
            if len(value) != 40 or any(
                character not in "0123456789abcdef"
                for character in value
            ):
                raise CycleRiskEnvelopeError(
                    "outer_strategy_policy_invalid"
                )
        if previous_digest:
            _required_digest(
                row.get("previous_receipt_digest"),
                "outer_strategy_policy_invalid",
            )
        previous_digest = str(row["receipt_digest"])


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


def _allowed_directions(value: Any, code: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise CycleRiskEnvelopeError(code)
    rendered = [_direction(item, code) for item in value]
    if len(rendered) != len(set(rendered)):
        raise CycleRiskEnvelopeError(code)
    canonical = [
        direction
        for direction in _DIRECTION_ORDER
        if direction in rendered
    ]
    if rendered != canonical:
        raise CycleRiskEnvelopeError(code)
    return rendered


def _validate_policy_inheritance_reference(value: Any) -> dict[str, Any]:
    code = "outer_strategy_policy_invalid"
    expected_fields = {
        "binding_id",
        "binding_version",
        "binding_digest",
        "policy_id",
        "policy_version",
        "policy_digest",
        "policy_schema_version",
    }
    if not isinstance(value, Mapping) or set(value) != expected_fields:
        raise CycleRiskEnvelopeError(code)
    reference = {
        "binding_id": _required_text(value.get("binding_id"), code),
        "binding_version": _positive_int(
            value.get("binding_version"), code
        ),
        "binding_digest": _required_digest(
            value.get("binding_digest"), code
        ),
        "policy_id": _required_text(value.get("policy_id"), code),
        "policy_version": _positive_int(
            value.get("policy_version"), code
        ),
        "policy_digest": _required_digest(
            value.get("policy_digest"), code
        ),
        "policy_schema_version": _required_text(
            value.get("policy_schema_version"), code
        ),
    }
    if reference["policy_schema_version"] != OUTER_POLICY_SCHEMA_V1:
        raise CycleRiskEnvelopeError(code)
    return reference


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
