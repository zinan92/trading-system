"""Immutable Paper-only cycle risk envelopes.

An envelope is a human (or policy-nested AI) authorization boundary for one
already-active strategy plan.  It is deliberately narrower than a preview: a
preview is always rebuilt and independently checked by the control plane, but
an in-bound preview may rely on the envelope without copying an old preview's
facts digest or acknowledgement.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

from services.journal_store import load_json, write_json


OUTER_POLICY_SCHEMA = "paper-strategy-policy-boundary-v1"
ENVELOPE_SCHEMA = "cycle-risk-envelope-v1"
AUTHORIZATION_KINDS = {
    "human_explicit",
    "ai_policy_within_preapproved_strategy_boundary",
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


class CycleRiskEnvelopeError(ValueError):
    """A stable, fail-closed envelope authorization error."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class CycleRiskEnvelopeStore:
    """Append-only persistence and exact comparison for Paper envelopes."""

    def __init__(self, output_root: Path) -> None:
        self.output_root = Path(output_root)
        self.root = self.output_root / "dualtrack" / "supervisor" / "risk_envelopes"
        self.policy_root = self.root / "outer_strategy_policies"
        self.verification_root = self.root / "start_verifications"

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
        limits = _canonical_limits(strategy_type, payload.get("limits"))
        policy_id = _required_text(payload.get("policy_id"), "outer_strategy_policy_invalid")
        version = _positive_int(payload.get("version"), "outer_strategy_policy_invalid")
        record = {
            "schema_version": OUTER_POLICY_SCHEMA,
            "policy_id": policy_id,
            "version": version,
            "strategy_type": strategy_type,
            "direction": _required_text(payload.get("direction"), "outer_strategy_policy_invalid"),
            "summary": _required_text(payload.get("summary"), "outer_strategy_policy_invalid"),
            "limits": limits,
            "authorized_at": _timestamp(now),
            "actor": actor_row,
        }
        record["policy_digest"] = _digest(record)
        path = self.policy_root / f"{_safe_filename(policy_id)}.json"
        rows = _rows(path)
        prior = next((row for row in rows if row.get("policy_id") == policy_id and row.get("version") == version), None)
        if prior is not None:
            if prior.get("policy_digest") != record["policy_digest"]:
                raise CycleRiskEnvelopeError("outer_strategy_policy_invalid")
            return prior
        rows.append(record)
        write_json(path, rows)
        return record

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
        if str(plan.get("cycle_id") or "") != str(cycle_id):
            raise CycleRiskEnvelopeError("plan_identity_conflict")
        plan_id = _required_text(plan.get("strategy_plan_id"), "plan_identity_conflict")
        plan_version = _positive_int(plan.get("version"), "plan_identity_conflict")
        limits = _canonical_limits(strategy_type, payload.get("limits"))
        record: dict[str, Any] = {
            "schema_version": ENVELOPE_SCHEMA,
            "cycle_id": str(cycle_id),
            "strategy_plan_id": plan_id,
            "strategy_plan_version": plan_version,
            "strategy_type": strategy_type,
            "direction": _required_text(plan.get("direction"), "plan_identity_conflict"),
            "plan_digest": _plan_digest(plan),
            "authorization_kind": kind,
            "limits": limits,
            "authorized_at": _timestamp(now),
        }
        if kind == "human_explicit":
            record["actor"] = _human_actor(actor)
            record["outer_policy"] = None
            record["outer_policy_comparisons"] = []
        else:
            outer = self._load_outer_policy(payload)
            comparisons = _compare_outer_policy(
                outer,
                limits,
                strategy_type=strategy_type,
                direction=str(record["direction"]),
            )
            if not all(row["pass"] for row in comparisons):
                raise CycleRiskEnvelopeError("outer_strategy_policy_envelope_out_of_bounds")
            record["actor"] = {"email": None, "transport": "ai_policy"}
            record["outer_policy"] = {
                "policy_id": outer["policy_id"],
                "version": outer["version"],
                "policy_digest": outer["policy_digest"],
                "authorized_at": outer["authorized_at"],
            }
            record["outer_policy_comparisons"] = comparisons
        record["envelope_authorization_id"] = _digest({key: value for key, value in record.items() if key != "envelope_authorization_id"})[:40]
        record["authorization_digest"] = _digest(record)
        path = self.root / f"{_safe_filename(cycle_id)}.json"
        rows = _rows(path)
        prior = next((row for row in rows if row.get("envelope_authorization_id") == record["envelope_authorization_id"]), None)
        if prior is not None:
            if prior.get("authorization_digest") != record["authorization_digest"]:
                raise CycleRiskEnvelopeError("risk_envelope_authorization_invalid")
            return prior
        rows.append(record)
        write_json(path, rows)
        return record

    def envelope(self, cycle_id: str, envelope_authorization_id: str) -> dict[str, Any] | None:
        return next(
            (
                row
                for row in _rows(self.root / f"{_safe_filename(cycle_id)}.json")
                if str(row.get("envelope_authorization_id") or "") == str(envelope_authorization_id)
            ),
            None,
        )

    def outer_policy(self, policy_id: str, version: int) -> dict[str, Any] | None:
        """Read one exact Park-authorized outer boundary, fail closed on tamper.

        The Supervisor may use this only after its deployment configuration
        names the exact policy id and version. There is deliberately no
        latest selection or AI fallback.
        """

        try:
            return self._load_outer_policy(
                {"outer_policy_id": policy_id, "outer_policy_version": version}
            )
        except CycleRiskEnvelopeError as exc:
            if exc.code == "outer_strategy_policy_missing":
                return None
            raise

    def verify_preview(
        self,
        *,
        cycle_id: str,
        plan: Mapping[str, Any],
        envelope_authorization_id: str,
        preview: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Return exact field-level evidence or fail closed before start."""

        envelope = self.envelope(cycle_id, envelope_authorization_id)
        if envelope is None:
            raise CycleRiskEnvelopeError("risk_envelope_missing")
        if envelope.get("schema_version") != ENVELOPE_SCHEMA or envelope.get("authorization_digest") != _digest({key: value for key, value in envelope.items() if key != "authorization_digest"}):
            raise CycleRiskEnvelopeError("risk_envelope_authorization_invalid")
        if not _matching_plan(envelope, plan):
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
            "authorized_source_plan": {
                "cycle_id": envelope["cycle_id"],
                "strategy_plan_id": envelope["strategy_plan_id"],
                "strategy_plan_version": envelope["strategy_plan_version"],
                "plan_digest": envelope["plan_digest"],
                "strategy_type": envelope["strategy_type"],
                "direction": envelope["direction"],
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
        result = dict(verification)
        result["execution_plan"] = {
            "strategy_plan_id": _required_text(execution_plan.get("strategy_plan_id"), "plan_identity_conflict"),
            "strategy_plan_version": _positive_int(execution_plan.get("version"), "plan_identity_conflict"),
            "plan_digest": _plan_digest(execution_plan),
        }
        return result

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
        path = self.verification_root / f"{_safe_filename(cycle_id)}.json"
        existing = _rows(path)
        if any(row.get("verification_record_id") == record["verification_record_id"] for row in existing):
            raise CycleRiskEnvelopeError("risk_envelope_verification_reused")
        existing.append(record)
        write_json(path, existing)
        return record

    def _load_outer_policy(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        policy_id = _required_text(payload.get("outer_policy_id"), "outer_strategy_policy_missing")
        version = _positive_int(payload.get("outer_policy_version"), "outer_strategy_policy_invalid")
        rows = _rows(self.policy_root / f"{_safe_filename(policy_id)}.json")
        policy = next((row for row in rows if row.get("policy_id") == policy_id and row.get("version") == version), None)
        if policy is None:
            raise CycleRiskEnvelopeError("outer_strategy_policy_missing")
        if policy.get("schema_version") != OUTER_POLICY_SCHEMA or policy.get("policy_digest") != _digest({key: value for key, value in policy.items() if key != "policy_digest"}):
            raise CycleRiskEnvelopeError("outer_strategy_policy_invalid")
        return policy


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


def _canonical_limits(strategy_type: str, source: Any) -> dict[str, str]:
    if not isinstance(source, Mapping):
        raise CycleRiskEnvelopeError("risk_envelope_authorization_invalid")
    fields = _GRID_FIELDS if strategy_type == "grid" else _DCA_FIELDS
    limits = {field: str(_decimal(source.get(field), "risk_envelope_authorization_invalid")) for field in fields}
    if _decimal(limits["min_grid_count" if strategy_type == "grid" else "min_additions"], "risk_envelope_authorization_invalid") > _decimal(limits["max_grid_count" if strategy_type == "grid" else "max_additions"], "risk_envelope_authorization_invalid"):
        raise CycleRiskEnvelopeError("risk_envelope_authorization_invalid")
    return limits


def _matching_plan(envelope: Mapping[str, Any], plan: Mapping[str, Any]) -> bool:
    return (
        str(envelope.get("strategy_plan_id") or "") == str(plan.get("strategy_plan_id") or "")
        and int(envelope.get("strategy_plan_version") or 0) == int(plan.get("version") or 0)
        and str(envelope.get("direction") or "") == str(plan.get("direction") or "")
        and str(envelope.get("plan_digest") or "") == _plan_digest(plan)
    )


def _plan_digest(plan: Mapping[str, Any]) -> str:
    keys = ("strategy_plan_id", "cycle_id", "version", "strategy_type", "direction", "range", "grid", "dca", "risk_budget")
    return _digest({key: plan.get(key) for key in keys})


def _strategy_type(row: Mapping[str, Any]) -> str:
    value = str(row.get("strategy_type") or "grid").lower()
    if value not in {"grid", "dca"}:
        raise CycleRiskEnvelopeError("risk_envelope_authorization_invalid")
    return value


def _human_actor(actor: Mapping[str, Any] | None) -> dict[str, Any]:
    source = actor if isinstance(actor, Mapping) else {}
    email = str(source.get("email") or "").strip().lower()
    if not email:
        raise CycleRiskEnvelopeError("risk_envelope_authorization_invalid")
    return {"email": email, "transport": str(source.get("transport") or "public_gateway")}


def _park_actor(actor: Mapping[str, Any] | None) -> dict[str, Any]:
    """Outer policy is a Park authorization, never a generic operator action."""

    normalized = _human_actor(actor)
    expected = os.getenv("GOLDBOT_ACCESS_EMAIL", "").strip().lower()
    if not expected or normalized["email"] != expected:
        raise CycleRiskEnvelopeError("outer_strategy_policy_invalid")
    return normalized


def _rows(path: Path) -> list[dict[str, Any]]:
    try:
        return [dict(row) for row in load_json(path) if isinstance(row, dict)]
    except (OSError, TypeError, ValueError) as exc:
        raise CycleRiskEnvelopeError("attempt_store_corrupt") from exc


def _timestamp(now: str | None) -> str:
    return str(now) if now else datetime.now(timezone.utc).isoformat()


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


def _safe_filename(value: str) -> str:
    rendered = "".join(character for character in value if character.isalnum() or character in {"-", "_"})
    return rendered or "unscoped"
