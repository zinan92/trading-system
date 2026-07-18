"""Provider-free primitives shared by risk policy adapters and port helpers."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from typing import Any

from schemas.risk import RiskDecision


OPEN_ORDER_STATES = frozenset(
    {
        "accepted",
        "new",
        "open",
        "pending",
        "submitted",
        "working",
        "partially_filled",
    }
)
CLOSE_EVENTS = frozenset({"exit", "stop", "target", "flatten"})


def safe_action_identity_blockers(
    payload: Mapping[str, Any],
    *,
    expected: str,
) -> list[dict[str, Any]]:
    candidate = (
        payload.get("candidate")
        if isinstance(payload.get("candidate"), Mapping)
        else {}
    )
    if expected == "cancel":
        if str(candidate.get("cycle_id") or ""):
            return []
        return [
            blocker(
                "cancel_identity_invalid",
                "risk_request.candidate",
                "cancel requires cycle identity",
                {},
            )
        ]
    event = str(candidate.get("event") or "").lower()
    has_position = bool(candidate.get("trade_id") or candidate.get("position_id"))
    if event in CLOSE_EVENTS and has_position:
        return []
    return [
        blocker(
            "close_identity_invalid",
            "risk_request.candidate",
            "reduce-only action requires close event and position identity",
            {"event": event},
        )
    ]


def blocked_message(decision: RiskDecision) -> str:
    primary = decision.to_dict().get("primary_blocker") or {}
    code = str(primary.get("code") or "risk_blocked")
    message = str(primary.get("message") or "risk decision blocks new exposure")
    return f"risk decision blocked: {code}: {message}"


def blocker(
    code: str,
    source: str,
    message: str,
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "code": code,
        "source": source,
        "message": message,
        "evidence": dict(evidence),
    }


def notice(
    code: str,
    source: str,
    message: str,
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "code": code,
        "source": source,
        "message": message,
        "evidence": dict(evidence),
    }


def finite_positive(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def fraction(value: Any) -> float | None:
    parsed = finite_positive(value)
    return parsed if parsed is not None and parsed <= 1 else None


def rounded(value: float | None) -> float | None:
    return None if value is None else round(float(value), 8)


def rounded_mapping(values: Mapping[str, float]) -> dict[str, float]:
    return {key: round(float(value), 8) for key, value in values.items()}


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
