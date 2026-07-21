"""Pure geometry for changing the edges of a running grid.

This module deliberately has no execution or account access.  It snaps an
operator request to the already-running arithmetic spacing or geometric ratio,
then describes only the new edge orders.  The control plane owns risk and all
order mutations.
"""

from __future__ import annotations

import math
from typing import Any

from services.grid_sizing import number_or, positive_number, preview_id, validate_market


DRAG_HANDLES = frozenset({"range", "lower", "upper"})


def build_dragged_range(
    plan: dict[str, Any],
    requested_range: dict[str, Any],
    *,
    handle: str,
) -> dict[str, Any]:
    """Validate one pointer geometry change without creating a StrategyPlan."""

    resolved_handle = str(handle or "").lower()
    if resolved_handle not in DRAG_HANDLES:
        raise ValueError("range drag handle must be range, lower, or upper")
    current = dict(plan.get("range") or {})
    old_low = positive_number(current.get("low"), "current grid low")
    old_high = positive_number(current.get("high"), "current grid high")
    new_low = positive_number(requested_range.get("low"), "requested grid low")
    new_high = positive_number(requested_range.get("high"), "requested grid high")
    if old_high <= old_low or new_high <= new_low:
        raise ValueError("grid range must have positive low below high")
    tolerance = max(1e-10, (old_high - old_low) * 1e-12)
    if resolved_handle == "range":
        low_delta = new_low - old_low
        high_delta = new_high - old_high
        if abs(low_delta - high_delta) > tolerance:
            raise ValueError("range drag must move both boundaries by the same delta")
        new_high = old_high + low_delta
    elif resolved_handle == "lower":
        if abs(new_high - old_high) > tolerance:
            raise ValueError("lower-boundary drag must keep the upper boundary fixed")
        new_high = old_high
    elif resolved_handle == "upper":
        if abs(new_low - old_low) > tolerance:
            raise ValueError("upper-boundary drag must keep the lower boundary fixed")
        new_low = old_low
    return {
        "handle": resolved_handle,
        "old_range": {"low": old_low, "high": old_high},
        "new_range": {"low": new_low, "high": new_high},
        "delta": {
            "low": new_low - old_low,
            "high": new_high - old_high,
        },
        "width": {
            "old": old_high - old_low,
            "new": new_high - new_low,
        },
    }


def range_adjustment_steps(
    plan: dict[str, Any],
    requested_range: dict[str, Any],
) -> dict[str, int]:
    """Return nearest whole-grid movements for both range edges."""

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
        spacing_ratio = positive_number(
            current_grid.get("spacing_ratio"),
            "current grid spacing ratio",
        )
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
    accepted_entries: list[dict[str, Any]],
    positions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a fixed-spacing edge adjustment without mutating the active plan."""

    validate_market(market)
    current_range = dict(plan.get("range") or {})
    current_grid = dict(plan.get("grid") or {})
    old_low = positive_number(current_range.get("low"), "current grid low")
    old_high = positive_number(current_range.get("high"), "current grid high")
    if old_high <= old_low:
        raise ValueError("active grid range is invalid")

    old_count = int(current_grid.get("count") or 0)
    old_levels = [float(value) for value in current_grid.get("levels") or []]
    if old_count < 2 or old_count > 80 or len(old_levels) != old_count + 1:
        raise ValueError("active grid geometry is invalid")
    if any(right <= left for left, right in zip(old_levels, old_levels[1:])):
        raise ValueError("active grid levels are not strictly increasing")
    if not _same_price(old_levels[0], old_low) or not _same_price(old_levels[-1], old_high):
        raise ValueError("active grid levels do not match its range")

    mode = str(current_grid.get("mode") or "arithmetic").lower()
    steps = range_adjustment_steps(plan, requested_range)
    lower_steps = int(steps["low"])
    upper_steps = int(steps["high"])
    lower_out, upper_out = max(0, lower_steps), max(0, upper_steps)
    lower_in, upper_in = max(0, -lower_steps), max(0, -upper_steps)

    spacing_ratio: float | None = None
    if mode == "arithmetic":
        spacing = positive_number(current_grid.get("spacing"), "current grid spacing")
        lower_levels = [
            _round_price(old_low - spacing * index)
            for index in range(lower_out, 0, -1)
        ]
        upper_levels = [
            _round_price(old_high + spacing * index)
            for index in range(1, upper_out + 1)
        ]
    elif mode == "geometric":
        spacing_ratio = positive_number(
            current_grid.get("spacing_ratio"),
            "current grid spacing ratio",
        )
        if spacing_ratio <= 1.0:
            raise ValueError("current grid spacing ratio must be greater than one")
        spacing = positive_number(current_grid.get("spacing"), "current grid spacing")
        lower_levels = [
            _round_price(old_low / (spacing_ratio**index))
            for index in range(lower_out, 0, -1)
        ]
        upper_levels = [
            _round_price(old_high * (spacing_ratio**index))
            for index in range(1, upper_out + 1)
        ]
    else:
        raise ValueError("active grid mode is unsupported")

    retained_stop = len(old_levels) - upper_in if upper_in else None
    retained_levels = old_levels[lower_in:retained_stop]
    levels = [*lower_levels, *retained_levels, *upper_levels]
    count = len(levels) - 1
    if count < 2 or count > 80:
        raise ValueError("grid count must be between 2 and 80")
    if any(right <= left for left, right in zip(levels, levels[1:])):
        raise ValueError("adjusted grid levels are not strictly increasing")

    latest = positive_number(market.get("latest_close"), "market latest_close")
    if not levels[0] <= latest <= levels[-1]:
        raise ValueError("market_outside_requested_range")
    direction = str(plan.get("direction") or "neutral").lower()
    if direction not in {"neutral", "long", "short"}:
        raise ValueError("active grid direction is unsupported")
    notional = positive_number(
        current_grid.get("notional_per_grid"),
        "current notional_per_grid",
    )
    if mode == "geometric":
        lower_stop = _round_price(levels[0] / float(spacing_ratio or 0.0))
        upper_stop = _round_price(levels[-1] * float(spacing_ratio or 0.0))
    else:
        lower_stop = _round_price(levels[0] - spacing)
        upper_stop = _round_price(levels[-1] + spacing)

    edge_indexes = [
        *range(lower_out),
        *range(len(levels) - upper_out, len(levels)),
    ]
    planned_edge_orders: list[dict[str, Any]] = []
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
        tp = (
            levels[index + 1]
            if side == "buy" and index + 1 < len(levels)
            else levels[index - 1]
            if side == "sell" and index > 0
            else _next_level(price, mode, spacing, spacing_ratio, up=side == "buy")
        )
        planned = {
            "preview_order_id": f"edge-{side}-{_price_id(price)}",
            "level": index,
            "state": "preview",
            "side": side,
            "event": "entry",
            "order_type": "limit",
            "price": _round_price(price),
            "tp": _round_price(float(tp)),
            "sl": lower_stop if side == "buy" else upper_stop,
            "quantity": round(notional / price, 8),
            "notional": round(notional, 2),
        }
        planned_edge_orders.append(planned)
        if not _entry_already_exists(side, price, accepted_entries, positions):
            edge_orders.append(dict(planned))

    internal_orders = [
        dict(order)
        for order in current_grid.get("orders") or []
        if levels[0] - 1e-8
        <= number_or(order.get("price"), 0.0)
        <= levels[-1] + 1e-8
    ]
    combined_orders = [*internal_orders, *planned_edge_orders]
    level_spacings = [right - left for left, right in zip(levels, levels[1:])]
    grid = {
        **current_grid,
        "count": count,
        "levels": levels,
        "spacing": current_grid.get("spacing"),
        "spacing_ratio": current_grid.get("spacing_ratio"),
        "min_spacing": round(min(level_spacings), 4),
        "max_spacing": round(max(level_spacings), 4),
        "notional_per_grid": current_grid.get("notional_per_grid"),
        "notional_mode": current_grid.get("notional_mode"),
        "orders": combined_orders,
    }
    effective_range = {"low": levels[0], "high": levels[-1]}
    identity = {
        "cycle_id": cycle_id,
        "direction": direction,
        "style": str(plan.get("style") or "steady"),
        "range": effective_range,
        "grid": grid,
        "orders": combined_orders,
    }
    return {
        "effective_range": effective_range,
        "steps": steps,
        "grid": grid,
        "edge_orders": edge_orders,
        "preview_id": preview_id(identity),
        "changed": bool(lower_steps or upper_steps),
    }


def _entry_already_exists(
    side: str,
    price: float,
    accepted_entries: list[dict[str, Any]],
    positions: list[dict[str, Any]],
) -> bool:
    # A completed entry/exit lifecycle must be allowed to re-arm at the same
    # price. Only currently accepted entries and open positions occupy a line.
    occupied_positions = [
        row
        for row in positions
        if str(row.get("status") or "open").lower() == "open"
    ]
    for row in [*accepted_entries, *occupied_positions]:
        raw_side = str(row.get("side") or "").lower()
        normalized_side = (
            "buy"
            if raw_side in {"buy", "long"}
            else "sell"
            if raw_side in {"sell", "short"}
            else ""
        )
        candidate_price = number_or(
            row.get("price", row.get("entry_price")),
            0.0,
        )
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
