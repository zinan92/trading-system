"""Immutable contracts for pre-execution risk decisions.

The request binds facts.  The decision explains what those facts allow.  A
persisted decision is audit evidence only; callers must rebuild a request from
current state before every exposure-increasing mutation.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any


RISK_REQUEST_SCHEMA = "risk-request-v1"
RISK_DECISION_SCHEMA = "risk-decision-v1"
RISK_ACTION_CLASSES = frozenset({"increase_exposure", "replace_pending", "reduce_only", "cancel"})
RISK_OUTCOMES = frozenset({"allow", "block"})


@dataclass(frozen=True)
class RiskRequest:
    schema_version: str
    request_id: str
    checked_at: str
    scope: str
    action_class: str
    candidate: Mapping[str, Any]
    account: Mapping[str, Any]
    market: Mapping[str, Any]
    execution: Mapping[str, Any]
    policy: Mapping[str, Any]
    evaluator: Mapping[str, Any]
    bindings: Mapping[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "checked_at": self.checked_at,
            "scope": self.scope,
            "action_class": self.action_class,
            "candidate": _thaw(self.candidate),
            "account": _thaw(self.account),
            "market": _thaw(self.market),
            "execution": _thaw(self.execution),
            "policy": _thaw(self.policy),
            "evaluator": _thaw(self.evaluator),
            "bindings": _thaw(self.bindings),
        }


@dataclass(frozen=True)
class RiskDecision:
    schema_version: str
    decision_id: str
    request_id: str
    checked_at: str
    scope: str
    action_class: str
    outcome: str
    allow_exposure_increase: bool
    allow_reduce_only: bool
    allow_cancel: bool
    primary_blocker: Mapping[str, Any]
    blockers: tuple[Mapping[str, Any], ...]
    warnings: tuple[Mapping[str, Any], ...]
    metrics: Mapping[str, Any]
    limits: Mapping[str, Any]
    recommendation: Mapping[str, Any]
    policy: Mapping[str, Any]
    evaluator: Mapping[str, Any]
    bindings: Mapping[str, str]
    request: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "decision_id": self.decision_id,
            "request_id": self.request_id,
            "checked_at": self.checked_at,
            "scope": self.scope,
            "action_class": self.action_class,
            "outcome": self.outcome,
            "allow_exposure_increase": self.allow_exposure_increase,
            "allow_reduce_only": self.allow_reduce_only,
            "allow_cancel": self.allow_cancel,
            "primary_blocker": _thaw(self.primary_blocker),
            "blockers": _thaw(self.blockers),
            "warnings": _thaw(self.warnings),
            "metrics": _thaw(self.metrics),
            "limits": _thaw(self.limits),
            "recommendation": _thaw(self.recommendation),
            "policy": _thaw(self.policy),
            "evaluator": _thaw(self.evaluator),
            "bindings": _thaw(self.bindings),
            "request": _thaw(self.request),
        }


def build_risk_request(
    *,
    checked_at: str,
    scope: str,
    action_class: str,
    candidate: Mapping[str, Any],
    account: Mapping[str, Any],
    market: Mapping[str, Any],
    execution: Mapping[str, Any],
    policy: Mapping[str, Any],
    evaluator: Mapping[str, Any],
) -> RiskRequest:
    """Freeze JSON facts and derive component plus whole-request identities."""

    timestamp = _aware_iso(checked_at, "checked_at")
    resolved_scope = _required_text(scope, "scope")
    resolved_action = str(action_class or "").strip().lower()
    if resolved_action not in RISK_ACTION_CLASSES:
        raise ValueError(f"unsupported risk action class: {resolved_action or 'missing'}")
    components = {
        "candidate": _plain_json(candidate),
        "account": _plain_json(account),
        "market": _plain_json(market),
        "execution": _plain_json(execution),
        "policy": _plain_json(policy),
        "evaluator": _plain_json(evaluator),
    }
    for name, value in components.items():
        if not isinstance(value, dict):
            raise TypeError(f"risk request {name} must be an object")
    bindings = {
        f"{name}_sha256": _digest(value)
        for name, value in components.items()
    }
    body = {
        "schema_version": RISK_REQUEST_SCHEMA,
        "checked_at": timestamp,
        "scope": resolved_scope,
        "action_class": resolved_action,
        **components,
        "bindings": bindings,
    }
    request_id = f"risk-request-{_digest(body)}"
    return RiskRequest(
        schema_version=RISK_REQUEST_SCHEMA,
        request_id=request_id,
        checked_at=timestamp,
        scope=resolved_scope,
        action_class=resolved_action,
        candidate=_freeze(components["candidate"]),
        account=_freeze(components["account"]),
        market=_freeze(components["market"]),
        execution=_freeze(components["execution"]),
        policy=_freeze(components["policy"]),
        evaluator=_freeze(components["evaluator"]),
        bindings=_freeze(bindings),
    )


def build_risk_decision(
    request: RiskRequest | Mapping[str, Any],
    *,
    blockers: Sequence[Mapping[str, Any]] = (),
    warnings: Sequence[Mapping[str, Any]] = (),
    metrics: Mapping[str, Any] | None = None,
    limits: Mapping[str, Any] | None = None,
    recommendation: Mapping[str, Any] | None = None,
) -> RiskDecision:
    """Build a content-addressed answer without granting authority to storage."""

    request_payload = validate_risk_request(request).to_dict()
    blocker_rows = tuple(_validated_notice(item, "blocker") for item in blockers)
    warning_rows = tuple(_validated_notice(item, "warning") for item in warnings)
    action_class = request_payload["action_class"]
    exposure_action = action_class in {"increase_exposure", "replace_pending"}
    outcome = "block" if blocker_rows else "allow"
    allow_exposure = exposure_action and not blocker_rows
    allow_reduce = action_class == "reduce_only" and not blocker_rows
    allow_cancel = action_class == "cancel" and not blocker_rows
    primary = blocker_rows[0] if blocker_rows else {}
    body = {
        "schema_version": RISK_DECISION_SCHEMA,
        "request_id": request_payload["request_id"],
        "checked_at": request_payload["checked_at"],
        "scope": request_payload["scope"],
        "action_class": action_class,
        "outcome": outcome,
        "allow_exposure_increase": allow_exposure,
        "allow_reduce_only": allow_reduce,
        "allow_cancel": allow_cancel,
        "primary_blocker": _plain_json(primary),
        "blockers": _plain_json(list(blocker_rows)),
        "warnings": _plain_json(list(warning_rows)),
        "metrics": _plain_json(metrics or {}),
        "limits": _plain_json(limits or {}),
        "recommendation": _plain_json(recommendation or {}),
        "policy": request_payload["policy"],
        "evaluator": request_payload["evaluator"],
        "bindings": request_payload["bindings"],
        "request": request_payload,
    }
    decision_id = f"risk-decision-{_digest(body)}"
    return RiskDecision(
        schema_version=RISK_DECISION_SCHEMA,
        decision_id=decision_id,
        request_id=body["request_id"],
        checked_at=body["checked_at"],
        scope=body["scope"],
        action_class=action_class,
        outcome=outcome,
        allow_exposure_increase=allow_exposure,
        allow_reduce_only=allow_reduce,
        allow_cancel=allow_cancel,
        primary_blocker=_freeze(body["primary_blocker"]),
        blockers=_freeze(body["blockers"]),
        warnings=_freeze(body["warnings"]),
        metrics=_freeze(body["metrics"]),
        limits=_freeze(body["limits"]),
        recommendation=_freeze(body["recommendation"]),
        policy=_freeze(body["policy"]),
        evaluator=_freeze(body["evaluator"]),
        bindings=_freeze(body["bindings"]),
        request=_freeze(body["request"]),
    )


def validate_risk_request(request: RiskRequest | Mapping[str, Any]) -> RiskRequest:
    payload = request.to_dict() if isinstance(request, RiskRequest) else _plain_json(request)
    if not isinstance(payload, dict) or payload.get("schema_version") != RISK_REQUEST_SCHEMA:
        raise ValueError("risk request schema is invalid")
    rebuilt = build_risk_request(
        checked_at=payload.get("checked_at"),
        scope=payload.get("scope"),
        action_class=payload.get("action_class"),
        candidate=payload.get("candidate") or {},
        account=payload.get("account") or {},
        market=payload.get("market") or {},
        execution=payload.get("execution") or {},
        policy=payload.get("policy") or {},
        evaluator=payload.get("evaluator") or {},
    )
    if payload.get("request_id") != rebuilt.request_id:
        raise ValueError("risk request identity mismatch")
    if payload.get("bindings") != rebuilt.to_dict()["bindings"]:
        raise ValueError("risk request binding mismatch")
    return rebuilt


def validate_risk_decision(
    decision: RiskDecision | Mapping[str, Any],
    *,
    expected_request: RiskRequest | Mapping[str, Any] | None = None,
) -> RiskDecision:
    payload = decision.to_dict() if isinstance(decision, RiskDecision) else _plain_json(decision)
    if not isinstance(payload, dict) or payload.get("schema_version") != RISK_DECISION_SCHEMA:
        raise ValueError("risk decision schema is invalid")
    request = validate_risk_request(payload.get("request") or {})
    rebuilt = build_risk_decision(
        request,
        blockers=payload.get("blockers") or [],
        warnings=payload.get("warnings") or [],
        metrics=payload.get("metrics") or {},
        limits=payload.get("limits") or {},
        recommendation=payload.get("recommendation") or {},
    )
    if payload != rebuilt.to_dict():
        raise ValueError("risk decision identity or derived field mismatch")
    if expected_request is not None:
        expected = validate_risk_request(expected_request)
        if request.request_id != expected.request_id:
            raise ValueError("risk decision is stale for the current request")
    return rebuilt


def _validated_notice(value: Mapping[str, Any], kind: str) -> dict[str, Any]:
    row = _plain_json(value)
    if not isinstance(row, dict):
        raise TypeError(f"risk {kind} must be an object")
    for field in ("code", "source", "message"):
        row[field] = _required_text(row.get(field), f"{kind} {field}")
    evidence = row.get("evidence", {})
    if not isinstance(evidence, dict):
        raise TypeError(f"risk {kind} evidence must be an object")
    row["evidence"] = evidence
    return row


def _aware_iso(value: Any, label: str) -> str:
    rendered = _required_text(value, label)
    try:
        parsed = datetime.fromisoformat(rendered.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"risk {label} is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"risk {label} must include timezone")
    return parsed.isoformat()


def _required_text(value: Any, label: str) -> str:
    rendered = str(value or "").strip()
    if not rendered:
        raise ValueError(f"risk {label} is required")
    return rendered


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        # Round-trip rejects NaN/Infinity while preserving JSON scalar values.
        json.dumps(value, allow_nan=False)
        return value
    raise TypeError(f"risk contract contains non-JSON value: {type(value).__name__}")


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value
