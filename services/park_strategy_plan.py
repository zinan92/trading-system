"""Pure Park input normalization and deterministic Paper risk planning."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Mapping


PARK_PLAN_SCHEMA = "park-strategy-plan-v1"
DEFAULT_NEUTRAL_GRID_ORDER_COUNT = 30
_DIRECTION_ALIASES = {
    "long": "long", "做多": "long", "多": "long",
    "short": "short", "做空": "short", "空": "short",
    "neutral": "neutral", "中性": "neutral",
}
_TYPE_ALIASES = {
    "dca": "dca", "趋势": "dca", "trend": "dca",
    "grid": "grid", "网格": "grid", "震荡": "grid", "range": "grid",
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


def _find_labeled_number(text: str, labels: tuple[str, ...], field: str) -> float | None:
    """Read an explicitly labelled price in either ``label 1`` or ``1 label`` form."""

    patterns = tuple(
        pattern
        for label in labels
        for pattern in (
            label + r"\s*(?:位|price)?\s*[:：=]?\s*" + _NUMBER,
        )
    )
    return _find_one(text, patterns, field)


def normalize_park_input(payload: Mapping[str, Any] | str) -> dict[str, Any]:
    """Normalize explicit Park input without filling authorization gaps."""

    body = dict(payload) if isinstance(payload, Mapping) else {}
    text = payload if isinstance(payload, str) else str(body.get("source_text") or "")
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
        text_aliases = {
            "做多": "long",
            "做空": "short",
            "中性": "neutral",
            "long": "long",
            "short": "short",
            "neutral": "neutral",
        }
        matches = [normalized for alias, normalized in text_aliases.items() if alias in text.lower()]
        if len(set(matches)) > 1:
            raise ParkStrategyPlanError("ambiguous_direction", "direction is ambiguous")
        if matches:
            direction = matches[0]
    if direction not in {"long", "short", "neutral"}:
        raise ParkStrategyPlanError("missing_direction", "Park must explicitly provide long, short, or neutral")

    strategy_type: str | None = None
    raw_type = body.get("strategy_type") or body.get("type")
    if raw_type is not None:
        strategy_type = _TYPE_ALIASES.get(str(raw_type).strip().lower())
    if text:
        lowered_text = text.lower()
        # Explicit strategy words win over market-regime commentary.  Park
        # can say "震荡向上 ... 做多 DCA"; the former describes the tape, while
        # the latter is the execution type.  Only explicit DCA/Grid tokens
        # participate in this precedence rule; two explicit types remain an
        # ambiguity and still fail closed.
        explicit_matches = [
            normalized
            for alias, normalized in _TYPE_ALIASES.items()
            if normalized in {"dca", "grid"}
            and (alias in {"dca", "grid"} and re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", lowered_text))
        ]
        matches = explicit_matches or [
            normalized for alias, normalized in _TYPE_ALIASES.items() if alias in lowered_text
        ]
        if len(set(matches)) > 1:
            raise ParkStrategyPlanError("ambiguous_strategy_type", "strategy type is ambiguous")
        if matches:
            strategy_type = matches[0]
    if strategy_type not in {"dca", "grid"}:
        raise ParkStrategyPlanError("missing_strategy_type", "Park must explicitly provide DCA or Grid")
    if direction == "neutral" and strategy_type != "grid":
        raise ParkStrategyPlanError("neutral_direction_requires_grid", "neutral direction is only valid for Grid")

    upper = body.get("upper_price_boundary")
    lower = body.get("lower_price_boundary")
    if upper is None or lower is None:
        range_match = re.search(_NUMBER + r"\s*(?:~|～|-|到|至)\s*" + _NUMBER, text)
        if range_match is None:
            # Park often omits the tilde in a short Telegram message, e.g.
            # ``中性网格策略 4450 4100 最大20x杠杆``.  Use only the first
            # adjacent positive pair as the authorized range; all later
            # numbers remain available for leverage/loss parsing.
            range_match = re.search(_NUMBER + r"\s+" + _NUMBER, text)
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

    stop_price = body.get("stop_price")
    if stop_price is None:
        stop_price = _find_labeled_number(text, ("止损", "stop(?:_price)?"), "stop_price")
    take_profit_price = body.get("take_profit_price")
    if take_profit_price is None:
        take_profit_price = _find_labeled_number(
            text,
            ("止盈", "take(?:_profit)?(?:_price)?", "tp"),
            "take_profit_price",
        )
    grid_spacing = body.get("grid_spacing") or body.get("spacing")
    if grid_spacing is None and strategy_type == "grid":
        grid_spacing = _find_labeled_number(
            text,
            ("网格间距", "间距", "grid[_ ]?spacing", "spacing"),
            "grid_spacing",
        )
    local_stop_authorized = body.get("local_stop_authorized") is True or body.get("per_order_stop_authorized") is True
    if not local_stop_authorized and text:
        local_stop_authorized = bool(re.search(r"(?:逐单|每单|local|per[-_ ]order)\s*(?:止损|stop)", text, re.IGNORECASE))
    stop_was_explicit = stop_price is not None
    if strategy_type == "grid" and direction in {"long", "short"} and stop_price is None:
        # Grid's default hard stop is the adverse outer boundary.  This is a
        # deterministic strategy rule, not an AI authorization or a local
        # per-rung stop.
        stop_price = lower if direction == "long" else upper
    hard_stop_source = (
        "explicit_stop_price" if stop_was_explicit else "authorized_price_boundary"
    ) if strategy_type == "grid" else None
    raw_order_count = body.get("order_count")
    if raw_order_count in (None, ""):
        raw_order_count = DEFAULT_NEUTRAL_GRID_ORDER_COUNT if direction == "neutral" and strategy_type == "grid" else 1
    if strategy_type == "grid" and grid_spacing is not None:
        spacing_value = _number(grid_spacing, "grid_spacing")
        intervals = (upper_value - lower_value) / spacing_value
        rounded_intervals = int(round(intervals))
        if rounded_intervals < 2 or abs(intervals - rounded_intervals) > 1e-9:
            raise ParkStrategyPlanError(
                "grid_spacing_not_integral",
                "grid spacing must divide the authorized boundary width into complete intervals",
            )
        derived_count = rounded_intervals - 1
        if body.get("order_count") not in (None, "") and int(raw_order_count) != derived_count:
            raise ParkStrategyPlanError(
                "grid_geometry_mismatch",
                "grid order_count conflicts with the explicitly provided spacing and boundaries",
            )
        raw_order_count = derived_count
    if isinstance(stop_price, Mapping):
        normalized_stop_price: Any = {
            str(key): _number(value, f"stop_price.{key}")
            for key, value in stop_price.items()
        }
    else:
        normalized_stop_price = _number(stop_price, "stop_price") if stop_price is not None else None
    result: dict[str, Any] = {
        "schema_version": PARK_PLAN_SCHEMA,
        "direction": direction,
        "strategy_type": strategy_type,
        "upper_price_boundary": upper_value,
        "lower_price_boundary": lower_value,
        "maximum_leverage": _number(max_leverage, "maximum_leverage") if max_leverage is not None else None,
        "maximum_acceptable_loss": _number(max_loss, "maximum_acceptable_loss") if max_loss is not None else None,
        "stop_price": normalized_stop_price,
        "take_profit_price": _number(take_profit_price, "take_profit_price") if take_profit_price is not None else None,
        "grid_spacing": _number(grid_spacing, "grid_spacing") if grid_spacing is not None else None,
        "local_stop_authorized": local_stop_authorized,
        "order_count": int(raw_order_count),
        "source_text": text or None,
    }
    if hard_stop_source is not None:
        result["hard_stop_source"] = hard_stop_source
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


def _build_grid_risk_plan(
    normalized: Mapping[str, Any],
    *,
    current_price: float,
    source: str,
    observed_at: str,
    equity: float,
    lower: float,
    upper: float,
) -> dict[str, Any]:
    """Build full-depth Grid geometry and hard-loss sizing."""

    direction = str(normalized.get("direction") or "")
    spacing_value = normalized.get("grid_spacing")
    order_count = int(normalized.get("order_count") or 0)
    if spacing_value not in (None, ""):
        spacing = _number(spacing_value, "grid_spacing")
        intervals = (upper - lower) / spacing
        rounded_intervals = int(round(intervals))
        if rounded_intervals < 2 or abs(intervals - rounded_intervals) > 1e-9:
            raise ParkStrategyPlanError("grid_spacing_not_integral", "grid spacing must divide the boundary width")
        derived_count = rounded_intervals - 1
        if order_count not in {0, derived_count}:
            raise ParkStrategyPlanError("grid_geometry_mismatch", "grid count and spacing disagree")
        order_count = derived_count
    elif order_count <= 0:
        raise ParkStrategyPlanError("invalid_order_count", "Grid order_count must be positive")
    else:
        spacing = round((upper - lower) / (order_count + 1), 12)
    if order_count <= 0 or spacing <= 0:
        raise ParkStrategyPlanError("grid_geometry_invalid", "Grid geometry must contain positive rungs")
    entry_lower = round(lower + spacing, 12)
    entry_upper = round(upper - spacing, 12)
    prices = [round(lower + spacing * (index + 1), 12) for index in range(order_count)]
    if not prices or any(price <= lower or price >= upper for price in prices):
        raise ParkStrategyPlanError("grid_geometry_invalid", "Grid entries must be strictly inside boundaries")
    if not lower < current_price < upper:
        raise ParkStrategyPlanError("grid_requires_interior_price", "Grid requires the trusted current price inside its range")

    explicit_stop = normalized.get("stop_price")
    if direction != "neutral" and isinstance(explicit_stop, Mapping):
        raise ParkStrategyPlanError("invalid_stop_price", "Long/Short Grid hard stop must be one price")
    if direction == "long":
        hard_stop: float | dict[str, float] = _number(explicit_stop or lower, "stop_price")
        if hard_stop >= current_price:
            raise ParkStrategyPlanError("invalid_stop_price", "long Grid hard stop must be below current price")
    elif direction == "short":
        hard_stop = _number(explicit_stop or upper, "stop_price")
        if hard_stop <= current_price:
            raise ParkStrategyPlanError("invalid_stop_price", "short Grid hard stop must be above current price")
    else:
        if isinstance(explicit_stop, Mapping):
            if set(explicit_stop) != {"long", "short"}:
                raise ParkStrategyPlanError("neutral_grid_hard_stop_incomplete", "neutral Grid explicit hard stop must provide long and short legs")
            hard_stop = {
                "long": _number(explicit_stop["long"], "stop_price.long"),
                "short": _number(explicit_stop["short"], "stop_price.short"),
            }
            if hard_stop["long"] >= current_price or hard_stop["short"] <= current_price:
                raise ParkStrategyPlanError("invalid_stop_price", "neutral Grid hard stops must remain adverse to each leg")
        elif explicit_stop not in (None, ""):
            raise ParkStrategyPlanError("neutral_grid_hard_stop_incomplete", "neutral Grid explicit hard stop must provide long and short legs")
        elif normalized.get("take_profit_price") not in (None, ""):
            raise ParkStrategyPlanError("neutral_grid_boundary_only", "neutral Grid uses per-rung TP geometry")
        else:
            hard_stop = {"long": lower, "short": upper}

    rungs: list[dict[str, Any]] = []
    for index, price in enumerate(prices):
        if direction == "neutral":
            side = "buy" if price < current_price else "sell"
        else:
            side = "buy" if direction == "long" else "sell"
        if side == "buy":
            take_profit = prices[index + 1] if index + 1 < len(prices) else upper
            stop = hard_stop["long"] if isinstance(hard_stop, dict) else hard_stop
        else:
            take_profit = prices[index - 1] if index > 0 else lower
            stop = hard_stop["short"] if isinstance(hard_stop, dict) else hard_stop
        rungs.append({
            "rung": index + 1,
            "price": price,
            "side": side,
            "take_profit": round(take_profit, 12),
            "hard_stop": round(float(stop), 12),
            "local_stop": round(float(stop), 12) if bool(normalized.get("local_stop_authorized")) else None,
        })
    if direction == "neutral" and {rung["side"] for rung in rungs} != {"buy", "sell"}:
        raise ParkStrategyPlanError("neutral_grid_requires_two_legs", "neutral Grid must contain both buy and sell rungs")

    # Quantity is floored from the highest fillable rung so the sum of every
    # rung's actual price*quantity cannot exceed the authorized notional cap.
    quantity_price_basis = upper
    loss_rate = sum(abs(rung["price"] - rung["hard_stop"]) for rung in rungs) / (order_count * quantity_price_basis)
    if loss_rate <= 0:
        raise ParkStrategyPlanError("grid_loss_geometry_invalid", "Grid hard-stop loss geometry is not positive")
    maximum_leverage = normalized.get("maximum_leverage")
    leverage_cap = round(equity * float(maximum_leverage), 12) if maximum_leverage is not None else None
    maximum_loss = normalized.get("maximum_acceptable_loss")
    loss_cap = round(float(maximum_loss) / loss_rate, 12) if maximum_loss is not None else None
    caps = [cap for cap in (leverage_cap, loss_cap) if cap is not None]
    if not caps:
        raise ParkStrategyPlanError("missing_risk_authority", "no usable maximum leverage or loss cap")
    maximum_notional = min(caps)
    selected_constraint = "maximum_leverage" if loss_cap is None or (leverage_cap is not None and leverage_cap <= loss_cap) else "maximum_acceptable_loss"
    per_order_notional = round(maximum_notional / order_count, 12)
    per_order_quantity = round(per_order_notional / quantity_price_basis, 12)
    theoretical_max_loss = round(sum(abs(rung["price"] - rung["hard_stop"]) * per_order_quantity for rung in rungs), 12)
    if maximum_loss is not None:
        theoretical_max_loss = min(theoretical_max_loss, round(float(maximum_loss), 12))
    normalized_input = dict(normalized)
    normalized_input.update({
        "order_count": order_count,
        "grid_spacing": spacing,
        "grid_entry_lower": entry_lower,
        "grid_entry_upper": entry_upper,
    })
    risk = {
        "account_equity": equity,
        "leverage_cap_notional": leverage_cap,
        "maximum_loss_cap_notional": loss_cap,
        "selected_constraint": selected_constraint,
        "maximum_notional": maximum_notional,
        "effective_leverage": round(maximum_notional / equity, 12),
        "theoretical_max_loss": theoretical_max_loss,
        "risk_boundary": {"lower": lower, "upper": upper} if direction == "neutral" else hard_stop,
        "risk_boundary_source": "explicit_stop_price" if explicit_stop not in (None, "") else "authorized_price_boundaries",
        "hard_stop": hard_stop,
        "hard_stop_source": normalized.get("hard_stop_source") or ("explicit_stop_price" if explicit_stop not in (None, "") else "authorized_price_boundary"),
        "order_count": order_count,
        "per_order_notional": per_order_notional,
        "quantity_price_basis": quantity_price_basis,
        "per_order_quantity": per_order_quantity,
        "grid_spacing": spacing,
        "grid_entry_range": {"lower": entry_lower, "upper": entry_upper},
        "grid_rung_prices": prices,
        "grid_rungs": rungs,
        "local_stop_authorized": bool(normalized.get("local_stop_authorized")),
        "grid_loss_model": "full_depth_all_rungs_to_hard_stop",
    }
    if direction == "neutral":
        risk["legs"] = {
            "long": {"boundary": lower, "hard_stop": lower},
            "short": {"boundary": upper, "hard_stop": upper},
        }
    plan = {
        "schema_version": PARK_PLAN_SCHEMA,
        "strategy_session_id": str(normalized.get("strategy_session_id") or "").strip() or None,
        "strategy_revision_id": str(normalized.get("strategy_revision_id") or "").strip() or None,
        "normalized_input": normalized_input,
        "market": {"price": current_price, "source": source, "observed_at": observed_at},
        "risk": risk,
        "invalidation": {
            "trigger": "touch_or_cross",
            "upper_price_boundary": upper,
            "lower_price_boundary": lower,
            "hard_stop": hard_stop,
            "automatic_reopen": False,
        },
    }
    plan["plan_digest"] = "sha256:" + hashlib.sha256(_canonical(plan).encode("utf-8")).hexdigest()
    return plan


def build_deterministic_risk_plan(
    normalized: Mapping[str, Any],
    *,
    market: Mapping[str, Any],
    account_equity: float | None,
) -> dict[str, Any]:
    """Build one immutable risk plan; no provider or execution side effects."""

    direction = str(normalized.get("direction") or "")
    strategy_type = str(normalized.get("strategy_type") or "")
    if direction not in {"long", "short", "neutral"}:
        raise ParkStrategyPlanError("missing_direction", "normalized direction is invalid")
    if direction == "neutral" and strategy_type != "grid":
        raise ParkStrategyPlanError("neutral_direction_requires_grid", "neutral direction is only valid for Grid")
    current_price, source, observed_at = _market_price(market)
    equity = _number(account_equity, "account_equity") if account_equity is not None else None
    if equity is None:
        raise ParkStrategyPlanError("account_equity_missing", "authoritative Paper equity is required")
    upper = _number(normalized.get("upper_price_boundary"), "upper_price_boundary")
    lower = _number(normalized.get("lower_price_boundary"), "lower_price_boundary")
    if not lower <= current_price <= upper:
        raise ParkStrategyPlanError("current_price_outside_range", "strategy is already outside its authorized range")
    if strategy_type == "grid":
        return _build_grid_risk_plan(
            normalized,
            current_price=current_price,
            source=source,
            observed_at=observed_at,
            equity=equity,
            lower=lower,
            upper=upper,
        )
    explicit_stop = normalized.get("stop_price")
    explicit_take_profit = normalized.get("take_profit_price")
    if direction == "neutral" and (explicit_stop not in (None, "") or explicit_take_profit not in (None, "")):
        raise ParkStrategyPlanError(
            "neutral_grid_boundary_only",
            "neutral Grid uses its upper and lower boundaries as invalidation; stop/take-profit must be omitted",
        )
    if direction == "neutral" and not lower < current_price < upper:
        raise ParkStrategyPlanError(
            "neutral_grid_requires_interior_price",
            "neutral Grid requires the trusted current price to be strictly inside its range",
        )
    neutral_legs: dict[str, dict[str, Any]] | None = None
    if explicit_stop not in (None, ""):
        stop_price = _number(explicit_stop, "stop_price")
        if direction == "long":
            if stop_price >= current_price:
                raise ParkStrategyPlanError("invalid_stop_price", "long stop_price must be below current price")
            adverse_distance = current_price - stop_price
        else:
            if stop_price <= current_price:
                raise ParkStrategyPlanError("invalid_stop_price", "short stop_price must be above current price")
            adverse_distance = stop_price - current_price
        risk_boundary = stop_price
        risk_boundary_source = "explicit_stop_price"
    elif direction == "neutral":
        long_distance = current_price - lower
        short_distance = upper - current_price
        long_fraction = round(long_distance / current_price, 12)
        short_fraction = round(short_distance / current_price, 12)
        # Every neutral Grid order is budgeted from the same total notional.
        # Taking the larger bilateral adverse fraction is conservative: it
        # remains a hard upper bound even when both sides have filled before a
        # boundary breach is observed.
        adverse_distance = max(long_distance, short_distance)
        risk_boundary = {"lower": lower, "upper": upper}
        risk_boundary_source = "authorized_price_boundaries"
        neutral_legs = {
            "long": {
                "boundary": lower,
                "adverse_distance": round(long_distance, 12),
                "adverse_fraction": long_fraction,
            },
            "short": {
                "boundary": upper,
                "adverse_distance": round(short_distance, 12),
                "adverse_fraction": short_fraction,
            },
        }
    else:
        adverse_distance = (current_price - lower) if direction == "long" else (upper - current_price)
        risk_boundary = lower if direction == "long" else upper
        risk_boundary_source = "authorized_price_boundary"
    if explicit_take_profit not in (None, ""):
        take_profit_price = _number(explicit_take_profit, "take_profit_price")
        if (direction == "long" and take_profit_price <= current_price) or (
            direction == "short" and take_profit_price >= current_price
        ):
            raise ParkStrategyPlanError(
                "invalid_take_profit_price",
                f"{direction} take_profit_price must be beyond current price",
            )
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
    # Neutral entries span both sides of the current mark.  Size against the
    # highest possible entry price so the sum of accepted limit notionals never
    # exceeds the hard notional cap when sell levels are above the mark.
    quantity_price_basis = upper if direction == "neutral" else current_price
    per_order_quantity = round(per_order_notional / quantity_price_basis, 12)
    theoretical_max_loss = round(max_notional * adverse_fraction, 12)
    risk = {
        "account_equity": equity,
        "leverage_cap_notional": leverage_cap,
        "maximum_loss_cap_notional": loss_cap,
        "selected_constraint": selected_constraint,
        "maximum_notional": max_notional,
        "effective_leverage": round(max_notional / equity, 12),
        "theoretical_max_loss": theoretical_max_loss,
        "risk_boundary": risk_boundary,
        "risk_boundary_source": risk_boundary_source,
        "order_count": order_count,
        "per_order_notional": per_order_notional,
        "quantity_price_basis": quantity_price_basis,
        "per_order_quantity": per_order_quantity,
    }
    if neutral_legs is not None:
        for leg in neutral_legs.values():
            leg["theoretical_max_loss"] = round(max_notional * float(leg["adverse_fraction"]), 12)
        risk["legs"] = neutral_legs
        risk["risk_model"] = "bilateral_conservative_max_leg"
    plan = {
        "schema_version": PARK_PLAN_SCHEMA,
        "strategy_session_id": str(normalized.get("strategy_session_id") or "").strip() or None,
        "strategy_revision_id": str(normalized.get("strategy_revision_id") or "").strip() or None,
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
