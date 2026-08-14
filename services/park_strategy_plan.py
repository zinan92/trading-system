"""Pure Park input normalization and deterministic Paper risk planning."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Mapping


PARK_PLAN_SCHEMA = "park-strategy-plan-v1"
_DIRECTION_ALIASES = {
    "long": "long", "做多": "long", "多": "long",
    "short": "short", "做空": "short", "空": "short",
}
_TYPE_ALIASES = {
    "dca": "dca", "趋势": "dca", "trend": "dca",
    "grid": "grid", "震荡": "grid", "range": "grid",
}
_NUMBER = r"([0-9]+(?:\.[0-9]+)?)"


class ParkStrategyPlanError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _number(value: Any, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ParkStrategyPlanError("invalid_number", f"{field} must be numeric") from exc
    if result <= 0:
        raise ParkStrategyPlanError("invalid_number", f"{field} must be positive")
    return round(result, 12)


def _find_one(text: str, patterns: tuple[str, ...], field: str) -> float | None:
    values: list[float] = []
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            values.append(float(match.group(1)))
    if not values:
        return None
    if len(set(values)) > 1:
        raise ParkStrategyPlanError("ambiguous_input", f"{field} is specified more than once")
    return _number(values[0], field)


def normalize_park_input(payload: Mapping[str, Any] | str) -> dict[str, Any]:
    """Normalize explicit Park input without filling authorization gaps."""

    body = dict(payload) if isinstance(payload, Mapping) else {}
    text = payload if isinstance(payload, str) else ""
    direction_values = [
        value for key, value in body.items() if key in {"direction", "side"} and value is not None
    ]
    direction: str | None = None
    for alias, normalized in _DIRECTION_ALIASES.items():
        if any(str(value).strip().lower() == alias.lower() for value in direction_values):
            if direction and direction != normalized:
                raise ParkStrategyPlanError("ambiguous_direction", "direction is ambiguous")
            direction = normalized
    if text:
        # Single-character ``多`` also appears in ``最多`` (maximum), so only
        # accept the explicit Chinese compound or English token in free text.
        text_aliases = {"做多": "long", "做空": "short", "long": "long", "short": "short"}
        matches = [normalized for alias, normalized in text_aliases.items() if alias in text.lower()]
        if len(set(matches)) > 1:
            raise ParkStrategyPlanError("ambiguous_direction", "direction is ambiguous")
        if matches:
            direction = matches[0]
    if direction not in {"long", "short"}:
        raise ParkStrategyPlanError("missing_direction", "Park must explicitly provide long or short")

    strategy_type: str | None = None
    raw_type = body.get("strategy_type") or body.get("type")
    if raw_type is not None:
        strategy_type = _TYPE_ALIASES.get(str(raw_type).strip().lower())
    if text:
        matches = [normalized for alias, normalized in _TYPE_ALIASES.items() if alias in text.lower()]
        if len(set(matches)) > 1:
            raise ParkStrategyPlanError("ambiguous_strategy_type", "strategy type is ambiguous")
        if matches:
            strategy_type = matches[0]
    if strategy_type not in {"dca", "grid"}:
        raise ParkStrategyPlanError("missing_strategy_type", "Park must explicitly provide DCA or Grid")

    upper = body.get("upper_price_boundary")
    lower = body.get("lower_price_boundary")
    if upper is None or lower is None:
        range_match = re.search(_NUMBER + r"\s*(?:~|～|-|到|至)\s*" + _NUMBER, text)
        if range_match:
            first, second = float(range_match.group(1)), float(range_match.group(2))
            upper, lower = max(first, second), min(first, second)
    if upper is None or lower is None:
        raise ParkStrategyPlanError("missing_price_boundary", "Park must provide upper and lower boundaries")
    upper_value = _number(upper, "upper_price_boundary")
    lower_value = _number(lower, "lower_price_boundary")
    if upper_value <= lower_value:
        raise ParkStrategyPlanError("invalid_price_boundary", "upper boundary must exceed lower boundary")

    max_leverage = body.get("maximum_leverage") or body.get("max_leverage")
    if max_leverage is None:
        max_leverage = _find_one(text, (_NUMBER + r"\s*[倍xX]\s*(?:杠杆|leverage)?",), "maximum_leverage")
    max_loss = body.get("maximum_acceptable_loss") or body.get("max_loss")
    if max_loss is None:
        max_loss = _find_one(text, (_NUMBER + r"\s*(?:最大可接受亏损|最大亏损|max(?:imum)?\s*loss)",), "maximum_acceptable_loss")
    if max_leverage is None and max_loss is None:
        raise ParkStrategyPlanError("missing_risk_authority", "Park must provide maximum leverage or maximum acceptable loss")

    result: dict[str, Any] = {
        "schema_version": PARK_PLAN_SCHEMA,
        "direction": direction,
        "strategy_type": strategy_type,
        "upper_price_boundary": upper_value,
        "lower_price_boundary": lower_value,
        "maximum_leverage": _number(max_leverage, "maximum_leverage") if max_leverage is not None else None,
        "maximum_acceptable_loss": _number(max_loss, "maximum_acceptable_loss") if max_loss is not None else None,
        "stop_price": _number(body["stop_price"], "stop_price") if body.get("stop_price") is not None else None,
        "take_profit_price": _number(body["take_profit_price"], "take_profit_price") if body.get("take_profit_price") is not None else None,
        "order_count": int(body.get("order_count", 1)),
        "source_text": text or None,
    }
    if result["order_count"] <= 0:
        raise ParkStrategyPlanError("invalid_order_count", "order_count must be positive")
    return result


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _market_price(market: Mapping[str, Any]) -> tuple[float, str, str]:
    if market.get("trusted") is not True:
        raise ParkStrategyPlanError("market_untrusted", "current price requires trusted market evidence")
    if market.get("fresh") is not True:
        raise ParkStrategyPlanError("market_stale", "current price requires a fresh tick")
    source = str(market.get("source") or "").strip()
    observed_at = str(market.get("observed_at") or "").strip()
    if not source or not observed_at:
        raise ParkStrategyPlanError("market_evidence_incomplete", "market source and observed_at are required")
    try:
        datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ParkStrategyPlanError("market_evidence_invalid", "observed_at must be ISO-8601") from exc
    return _number(market.get("price"), "current_price"), source, observed_at


def build_deterministic_risk_plan(
    normalized: Mapping[str, Any],
    *,
    market: Mapping[str, Any],
    account_equity: float | None,
) -> dict[str, Any]:
    """Build one immutable risk plan; no provider or execution side effects."""

    direction = str(normalized.get("direction") or "")
    if direction not in {"long", "short"}:
        raise ParkStrategyPlanError("missing_direction", "normalized direction is invalid")
    current_price, source, observed_at = _market_price(market)
    equity = _number(account_equity, "account_equity") if account_equity is not None else None
    if equity is None:
        raise ParkStrategyPlanError("account_equity_missing", "authoritative Paper equity is required")
    upper = _number(normalized.get("upper_price_boundary"), "upper_price_boundary")
    lower = _number(normalized.get("lower_price_boundary"), "lower_price_boundary")
    if not lower <= current_price <= upper:
        raise ParkStrategyPlanError("current_price_outside_range", "strategy is already outside its authorized range")
    adverse_distance = (current_price - lower) if direction == "long" else (upper - current_price)
    if adverse_distance <= 0:
        raise ParkStrategyPlanError("boundary_already_invalid", "current price is at the adverse boundary")
    adverse_fraction = round(adverse_distance / current_price, 12)
    maximum_leverage = normalized.get("maximum_leverage")
    leverage_cap = round(equity * float(maximum_leverage), 12) if maximum_leverage is not None else None
    maximum_loss = normalized.get("maximum_acceptable_loss")
    loss_cap = round(float(maximum_loss) / adverse_fraction, 12) if maximum_loss is not None else None
    caps = [cap for cap in (leverage_cap, loss_cap) if cap is not None]
    if not caps:
        raise ParkStrategyPlanError("missing_risk_authority", "no usable maximum leverage or loss cap")
    max_notional = min(caps)
    selected_constraint = "maximum_leverage" if loss_cap is None or (leverage_cap is not None and leverage_cap <= loss_cap) else "maximum_acceptable_loss"
    order_count = int(normalized.get("order_count") or 1)
    per_order_notional = round(max_notional / order_count, 12)
    per_order_quantity = round(per_order_notional / current_price, 12)
    theoretical_max_loss = round(max_notional * adverse_fraction, 12)
    risk = {
        "account_equity": equity,
        "leverage_cap_notional": leverage_cap,
        "maximum_loss_cap_notional": loss_cap,
        "selected_constraint": selected_constraint,
        "maximum_notional": max_notional,
        "effective_leverage": round(max_notional / equity, 12),
        "theoretical_max_loss": theoretical_max_loss,
        "order_count": order_count,
        "per_order_notional": per_order_notional,
        "per_order_quantity": per_order_quantity,
    }
    plan = {
        "schema_version": PARK_PLAN_SCHEMA,
        "normalized_input": dict(normalized),
        "market": {"price": current_price, "source": source, "observed_at": observed_at},
        "risk": risk,
        "invalidation": {
            "trigger": "touch_or_cross",
            "upper_price_boundary": upper,
            "lower_price_boundary": lower,
            "automatic_reopen": False,
        },
    }
    plan["plan_digest"] = "sha256:" + hashlib.sha256(_canonical(plan).encode("utf-8")).hexdigest()
    return plan
