"""Pure geometry and risk checks for a running grid range extension."""

from __future__ import annotations

import math
from typing import Any

from services.grid_sizing import (
    number_or,
    positive_number,
    preview_id,
    trusted_account_equity,
    validate_market,
)


def range_adjustment_steps(plan: dict[str, Any], requested_range: dict[str, Any]) -> dict[str, int]:
    current_range = dict(plan.get("range") or {})
    current_grid = dict(plan.get("grid") or {})
    old_low = positive_number(current_range.get("low"), "current grid low")
    old_high = positive_number(current_range.get("high"), "current grid high")
    requested_low = positive_number(requested_range.get("low"), "requested grid low")
    requested_high = positive_number(requested_range.get("high"), "requested grid high")
    if old_high <= old_low or requested_high <= requested_low:
        raise ValueError("grid range must have positive low below high")
    mode = str(current_grid.get("mode") or "arithmetic").lower()
    if mode == "arithmetic":
        spacing = positive_number(current_grid.get("spacing"), "current grid spacing")
        lower_steps = _nearest_step((old_low - requested_low) / spacing)
        upper_steps = _nearest_step((requested_high - old_high) / spacing)
    elif mode == "geometric":
        spacing_ratio = positive_number(current_grid.get("spacing_ratio"), "current grid spacing ratio")
        if spacing_ratio <= 1.0:
            raise ValueError("current grid spacing ratio must be greater than one")
        log_ratio = math.log(spacing_ratio)
        lower_steps = _nearest_step(math.log(old_low / requested_low) / log_ratio)
        upper_steps = _nearest_step(math.log(requested_high / old_high) / log_ratio)
    else:
        raise ValueError("active grid mode is unsupported")
    return {"low": lower_steps, "high": upper_steps}


def build_range_extension(
    cycle_id: str,
    plan: dict[str, Any],
    requested_range: dict[str, Any],
    *,
    market: dict[str, Any],
    account: dict[str, Any],
    config: dict[str, Any],
    accepted_entries: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    filled_entries: list[dict[str, Any]] | None = None,
    historical_plan_orders: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Extend locked grid geometry outward without rebuilding its internal levels."""

    validate_market(market)
    current_range = dict(plan.get("range") or {})
    current_grid = dict(plan.get("grid") or {})
    old_low = positive_number(current_range.get("low"), "current grid low")
    old_high = positive_number(current_range.get("high"), "current grid high")
    requested_low = positive_number(requested_range.get("low"), "requested grid low")
    requested_high = positive_number(requested_range.get("high"), "requested grid high")
    if old_high <= old_low or requested_high <= requested_low:
        raise ValueError("grid range must have positive low below high")

    mode = str(current_grid.get("mode") or "arithmetic").lower()
    steps = range_adjustment_steps(plan, requested_range)
    lower_steps = int(steps["low"])
    upper_steps = int(steps["high"])
    old_count = int(current_grid.get("count") or 0)
    old_levels = [float(value) for value in current_grid.get("levels") or []]
    if old_count < 2 or old_count > 80 or len(old_levels) != old_count + 1:
        raise ValueError("active grid geometry is invalid")
    if any(right <= left for left, right in zip(old_levels, old_levels[1:])):
        raise ValueError("active grid levels are not strictly increasing")
    if not _same_price(old_levels[0], old_low) or not _same_price(old_levels[-1], old_high):
        raise ValueError("active grid levels do not match its range")

    if mode == "arithmetic":
        spacing = positive_number(current_grid.get("spacing"), "current grid spacing")
        spacing_ratio = None
        lower_out, upper_out = max(0, lower_steps), max(0, upper_steps)
        lower_in, upper_in = max(0, -lower_steps), max(0, -upper_steps)
        lower_levels = [_round_price(old_low - spacing * index) for index in range(lower_out, 0, -1)]
        upper_levels = [_round_price(old_high + spacing * index) for index in range(1, upper_out + 1)]
    elif mode == "geometric":
        spacing_ratio = positive_number(current_grid.get("spacing_ratio"), "current grid spacing ratio")
        if spacing_ratio <= 1.0:
            raise ValueError("current grid spacing ratio must be greater than one")
        spacing = positive_number(current_grid.get("spacing"), "current grid spacing")
        lower_out, upper_out = max(0, lower_steps), max(0, upper_steps)
        lower_in, upper_in = max(0, -lower_steps), max(0, -upper_steps)
        lower_levels = [
            _round_price(old_low / (spacing_ratio ** index))
            for index in range(lower_out, 0, -1)
        ]
        upper_levels = [
            _round_price(old_high * (spacing_ratio ** index))
            for index in range(1, upper_out + 1)
        ]
    else:
        raise ValueError("active grid mode is unsupported")

    retained_levels = old_levels[lower_in:len(old_levels) - upper_in if upper_in else None]
    levels = [*lower_levels, *retained_levels, *upper_levels]
    count = len(levels) - 1
    if count < 2 or count > 80:
        raise ValueError("grid count must be between 2 and 80")
    if any(right <= left for left, right in zip(levels, levels[1:])):
        raise ValueError("extended grid levels are not strictly increasing")

    direction = str(plan.get("direction") or "neutral").lower()
    style = str(plan.get("style") or "steady").lower()
    latest = positive_number(market.get("latest_close"), "market latest_close")
    if direction == "neutral" and not levels[0] <= latest <= levels[-1]:
        raise ValueError("market_outside_requested_range")
    notional = positive_number(current_grid.get("notional_per_grid"), "current notional_per_grid")
    leverage = positive_number(current_grid.get("leverage"), "current grid leverage")
    if mode == "geometric":
        lower_stop = _round_price(levels[0] / float(spacing_ratio or 0.0))
        upper_stop = _round_price(levels[-1] * float(spacing_ratio or 0.0))
    else:
        lower_stop = _round_price(levels[0] - spacing)
        upper_stop = _round_price(levels[-1] + spacing)
    edge_indexes = [*range(lower_out), *range(len(levels) - upper_out, len(levels))]
    edge_orders: list[dict[str, Any]] = []
    for index in edge_indexes:
        price = float(levels[index])
        if _same_price(price, latest):
            continue
        side = "buy" if price < latest else "sell"
        if direction == "long" and side != "buy":
            continue
        if direction == "short" and side != "sell":
            continue
        if _entry_already_exists(
            side,
            price,
            accepted_entries,
            filled_entries or [],
            positions,
        ):
            continue
        if side == "buy":
            tp = levels[index + 1] if index + 1 < len(levels) else _next_level(price, mode, spacing, spacing_ratio, up=True)
            sl = lower_stop
        else:
            tp = levels[index - 1] if index > 0 else _next_level(price, mode, spacing, spacing_ratio, up=False)
            sl = upper_stop
        edge_orders.append({
            "preview_order_id": f"extend-{index:02d}-{side}-{_price_id(price)}",
            "level": index,
            "state": "preview",
            "side": side,
            "event": "entry",
            "order_type": "limit",
            "price": _round_price(price),
            "tp": _round_price(float(tp)),
            "sl": _round_price(float(sl)),
            "quantity": round(notional / price, 8),
            "notional": round(notional, 2),
        })

    plan_orders = [dict(order) for order in current_grid.get("orders") or []]
    risk_reference_orders = [
        *[
            {**dict(order), "_strategy_plan_id": str(plan.get("strategy_plan_id") or "")}
            for order in plan_orders
        ],
        *[dict(order) for order in historical_plan_orders or []],
    ]
    internal_orders = [
        dict(order)
        for order in plan_orders
        if levels[0] - 1e-8 <= float(order.get("price") or 0.0) <= levels[-1] + 1e-8
    ]
    combined_orders = [*internal_orders, *edge_orders]
    if any(
        number_or(order.get("price"), 0.0) <= 0
        or number_or(order.get("quantity"), 0.0) <= 0
        for order in accepted_entries
    ):
        raise ValueError("risk_evidence_missing")
    actual_pending = [
        _enrich_pending_order(order, risk_reference_orders)
        for order in accepted_entries
    ]
    risk = _fixed_notional_risk(
        [*actual_pending, *edge_orders],
        positions=[_enrich_position(position, risk_reference_orders) for position in positions],
        equity=trusted_account_equity(account),
        leverage=leverage,
        config=config,
    )
    if risk["capital_budget_exceeded"] or risk["risk_budget_exceeded"]:
        raise ValueError("risk_budget_exceeded")

    effective_range = {"low": levels[0], "high": levels[-1]}
    grid = {
        **current_grid,
        "count": count,
        "mode": mode,
        "levels": levels,
        "spacing": current_grid.get("spacing"),
        "spacing_ratio": current_grid.get("spacing_ratio"),
        "notional_per_grid": current_grid.get("notional_per_grid"),
        "notional_mode": "manual",
        "leverage": current_grid.get("leverage"),
        "orders": combined_orders,
    }
    identity = {
        "cycle_id": cycle_id,
        "direction": direction,
        "style": style,
        "range": effective_range,
        "grid": grid,
        "orders": combined_orders,
    }
    return {
        "effective_range": effective_range,
        "steps": {"low": lower_steps, "high": upper_steps},
        "grid": grid,
        "edge_orders": edge_orders,
        "risk": risk,
        "preview_id": preview_id(identity),
        "changed": bool(lower_steps or upper_steps),
    }


def _fixed_notional_risk(
    orders: list[dict[str, Any]],
    *,
    positions: list[dict[str, Any]],
    equity: float,
    leverage: float,
    config: dict[str, Any],
) -> dict[str, Any]:
    strategy_cfg = dict(config.get("strategy_grid") or {})
    utilization = positive_number(strategy_cfg.get("capital_utilization_cap", 1.0), "capital utilization cap")
    max_plan_loss_pct = positive_number(strategy_cfg.get("max_plan_loss_pct", 0.10), "max plan loss pct")
    if utilization > 1 or max_plan_loss_pct > 1:
        raise ValueError("strategy risk fractions must not exceed 1")
    max_leverage = positive_number(config.get("max_leverage", 10.0), "max leverage")
    if leverage > max_leverage:
        raise ValueError("risk_budget_exceeded")

    side_notionals = {"buy": 0.0, "sell": 0.0}
    side_losses = {"buy": 0.0, "sell": 0.0}
    for order in orders:
        side = str(order.get("side") or "").lower()
        if side not in side_notionals:
            raise ValueError("risk_evidence_missing")
        price = number_or(order.get("price"), 0.0)
        quantity = number_or(order.get("quantity"), 0.0)
        notional = max(number_or(order.get("notional"), 0.0), price * quantity)
        stop = number_or(order.get("sl"), 0.0)
        if price <= 0 or quantity <= 0 or stop <= 0:
            raise ValueError("risk_evidence_missing")
        side_notionals[side] += notional
        side_losses[side] += abs(price - stop) * quantity
    for position in positions:
        if str(position.get("status") or "").lower() != "open":
            continue
        raw_side = str(position.get("side") or "").lower()
        side = "buy" if raw_side in {"buy", "long"} else "sell" if raw_side in {"sell", "short"} else ""
        if not side:
            raise ValueError("risk_evidence_missing")
        price = number_or(position.get("entry_price"), 0.0)
        quantity = number_or(
            position.get("remaining_units", position.get("quantity", position.get("contracts"))),
            0.0,
        )
        stop = number_or(position.get("sl"), 0.0)
        if price <= 0 or quantity <= 0 or stop <= 0:
            raise ValueError("risk_evidence_missing")
        side_notionals[side] += price * quantity
        side_losses[side] += abs(price - stop) * quantity
    max_side_notional = max(side_notionals.values(), default=0.0)
    max_loss = max(side_losses.values(), default=0.0)
    capital_budget = equity * leverage * utilization
    max_loss_budget = equity * max_plan_loss_pct
    return {
        "equity": round(equity, 2),
        "estimated_margin": round(max_side_notional / leverage, 2),
        "max_loss": round(max_loss, 2),
        "max_loss_budget": round(max_loss_budget, 2),
        "capital_budget": round(capital_budget, 2),
        "max_side_notional": round(max_side_notional, 2),
        "capital_budget_exceeded": max_side_notional > capital_budget + 1e-8,
        "risk_budget_exceeded": max_loss > max_loss_budget + 1e-8,
        "pending_entry_count": len(orders),
        "open_position_count": sum(
            1 for position in positions if str(position.get("status") or "").lower() == "open"
        ),
    }


def _enrich_pending_order(order: dict[str, Any], plan_orders: list[dict[str, Any]]) -> dict[str, Any]:
    enriched = dict(order)
    side = str(order.get("side") or "").lower()
    price = number_or(order.get("price"), 0.0)
    match = _select_reference_order(
        plan_orders,
        side=side,
        price=price,
        strategy_plan_id=str(order.get("strategy_plan_id") or ""),
    )
    for field in ("sl", "tp", "quantity", "notional", "preview_order_id"):
        if enriched.get(field) in (None, "") and match.get(field) not in (None, ""):
            enriched[field] = match[field]
    if enriched.get("notional") in (None, ""):
        enriched["notional"] = price * number_or(enriched.get("quantity"), 0.0)
    return enriched


def _enrich_position(position: dict[str, Any], plan_orders: list[dict[str, Any]]) -> dict[str, Any]:
    enriched = dict(position)
    raw_side = str(position.get("side") or "").lower()
    side = "buy" if raw_side in {"buy", "long"} else "sell" if raw_side in {"sell", "short"} else ""
    entry_price = number_or(position.get("entry_price"), 0.0)
    match = _select_reference_order(
        plan_orders,
        side=side,
        price=entry_price,
        strategy_plan_id=str(position.get("strategy_plan_id") or ""),
    )
    for field in ("sl", "tp"):
        if enriched.get(field) in (None, "") and match.get(field) not in (None, ""):
            enriched[field] = match[field]
    return enriched


def _select_reference_order(
    plan_orders: list[dict[str, Any]],
    *,
    side: str,
    price: float,
    strategy_plan_id: str,
) -> dict[str, Any]:
    candidates = [
        planned
        for planned in plan_orders
        if str(planned.get("side") or "").lower() == side
        and _same_price(number_or(planned.get("price"), 0.0), price)
    ]
    if strategy_plan_id:
        exact = [
            planned
            for planned in candidates
            if str(planned.get("_strategy_plan_id") or "") == strategy_plan_id
        ]
        return dict(exact[0]) if len(exact) == 1 else {}
    return dict(candidates[0]) if len(candidates) == 1 else {}


def _entry_already_exists(
    side: str,
    price: float,
    accepted_entries: list[dict[str, Any]],
    filled_entries: list[dict[str, Any]],
    positions: list[dict[str, Any]],
) -> bool:
    candidates = [*accepted_entries, *filled_entries, *positions]
    for row in candidates:
        raw_side = str(row.get("side") or "").lower()
        normalized_side = (
            "buy" if raw_side in {"buy", "long"}
            else "sell" if raw_side in {"sell", "short"}
            else ""
        )
        candidate_price = number_or(row.get("price", row.get("entry_price")), 0.0)
        if normalized_side == side and candidate_price > 0 and _same_price(candidate_price, price):
            return True
    return False


def _nearest_step(value: float) -> int:
    if value >= 0:
        return int(math.floor(value + 0.5 + 1e-10))
    return int(math.ceil(value - 0.5 - 1e-10))


def _round_price(value: float) -> float:
    return round(float(value), 8)


def _same_price(left: float, right: float) -> bool:
    return abs(float(left) - float(right)) <= max(1e-8, abs(float(right)) * 1e-8)


def _next_level(
    price: float,
    mode: str,
    spacing: float,
    spacing_ratio: float | None,
    *,
    up: bool,
) -> float:
    if mode == "geometric":
        ratio = float(spacing_ratio or 0.0)
        return price * ratio if up else price / ratio
    return price + spacing if up else price - spacing


def _price_id(value: float) -> str:
    return f"{float(value):.8f}".replace(".", "p").replace("-", "m")
