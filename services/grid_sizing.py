"""Deterministic grid geometry and capital sizing.

Single source of truth for the fixed-timeframe grid contract: complete D1
ATR14 owns the range, complete 4H ATR14 proposes the densest spacing, and the
planner searches down to the configured grid-count floor until every complete
grid clears its dollar-profit target within the 10x capital ceiling. Maximum
stop loss remains an advisory diagnostic only.

Pure functions only: no I/O, no plan or ledger mutation, no clock reads.
Identical market/account/config inputs must produce an identical preview,
including its `preview_id` hash — the control plane, the start transaction
and future shadow runs all rely on that determinism.
"""

from __future__ import annotations

import hashlib
import json
import math
from decimal import Decimal, ROUND_FLOOR
from typing import Any

from services.dualtrack_execution_contract import normalize_execution_command

GRID_DIRECTIONS = {"neutral", "long", "short"}
GRID_STYLES = {"steady", "aggressive"}
GRID_MODES = {"arithmetic", "geometric"}
MIN_GRID_COUNT = 30
MAX_GRID_COUNT = 70


def _floor_quantity(value: float, config: dict[str, Any]) -> float:
    settings = dict(config.get("execution_contract") or {})
    increment = Decimal(str(settings.get("quantity_increment") or "0.00000001"))
    quantity = (Decimal(str(value)) / increment).to_integral_value(rounding=ROUND_FLOOR) * increment
    if quantity <= 0:
        raise ValueError("execution quantity rounds to zero at venue precision")
    return float(quantity)


def _grid_geometry(
    *,
    low: float,
    high: float,
    count: int,
    mode: str,
    latest: float,
    direction: str,
    config: dict[str, Any],
) -> tuple[list[float], float | None, float, float, list[dict[str, Any]]]:
    spacing = (high - low) / count
    if mode == "geometric":
        spacing_ratio = (high / low) ** (1.0 / count)
        levels = [low * (spacing_ratio ** index) for index in range(count + 1)]
        lower_stop = low / spacing_ratio
        upper_stop = high * spacing_ratio
    else:
        spacing_ratio = None
        levels = [low + spacing * index for index in range(count + 1)]
        lower_stop = low - spacing
        upper_stop = high + spacing
    nearest_index = min(range(len(levels)), key=lambda index: abs(levels[index] - latest))
    provisional: list[dict[str, Any]] = []
    for index, raw_price in enumerate(levels):
        if index == nearest_index:
            continue
        side = "buy" if raw_price < latest else "sell"
        if direction == "long" and side != "buy":
            continue
        if direction == "short" and side != "sell":
            continue
        raw_tp = levels[index + 1] if side == "buy" else levels[index - 1]
        raw_sl = lower_stop if side == "buy" else upper_stop
        normalized = normalize_execution_command(
            {
                "preview_order_id": f"preview-{index:02d}-{side}",
                "level": index,
                "state": "preview",
                "side": side,
                "event": "entry",
                "order_type": "limit",
                "price": raw_price,
                "tp": raw_tp,
                "sl": raw_sl,
            },
            config,
        )
        price = positive_number(normalized.get("price"), "executable grid price")
        tp = positive_number(normalized.get("tp"), "executable grid take profit")
        if price == tp:
            raise ValueError("grid spacing is smaller than venue price precision")
        provisional.append(normalized)
    if not provisional:
        raise ValueError("selected direction has no executable grid orders in this range")
    return levels, spacing_ratio, lower_stop, upper_stop, provisional


def _orders_at_notional(
    provisional: list[dict[str, Any]],
    notional: float,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    orders = []
    for order in provisional:
        price = positive_number(order.get("price"), "executable grid price")
        quantity = _floor_quantity(notional / price, config)
        orders.append(normalize_execution_command({**order, "quantity": quantity}, config))
    return orders


def planned_net_profit_usd(order: dict[str, Any], cost_per_side_rate: float) -> float:
    price = positive_number(order.get("price"), "grid price")
    tp = positive_number(order.get("tp"), "grid take profit")
    quantity = positive_number(order.get("quantity"), "grid quantity")
    gross = abs(tp - price) * quantity
    modeled_fees = (price + tp) * quantity * cost_per_side_rate
    return gross - modeled_fees


def validate_market(market: dict[str, Any]) -> None:
    if market.get("status") not in {"ready", "derived"} or market.get("fresh") is not True:
        raise ValueError("market data is stale")
    if market.get("is_synthetic") is not False:
        raise ValueError("synthetic market data is forbidden")
    if not str(market.get("provider") or "").strip():
        raise ValueError("market provider is missing")
    positive_number(market.get("latest_close"), "market latest_close")
    if len(market.get("bars") or []) < 15:
        raise ValueError("market history is insufficient for ATR grid planning")


def strategy_bars(market: dict[str, Any], timeframe: str, period: int) -> list[dict[str, Any]]:
    contexts = market.get("strategy_timeframes")
    context = contexts.get(timeframe) if isinstance(contexts, dict) else None
    if not isinstance(context, dict):
        raise ValueError(f"strategy timeframe {timeframe} is unavailable")
    if context.get("is_synthetic") is not False:
        raise ValueError(f"strategy timeframe {timeframe} is synthetic")
    if not str(context.get("provider") or "").strip():
        raise ValueError(f"strategy timeframe {timeframe} provider is missing")
    bars = list(context.get("bars") or [])
    if len(bars) < period + 1:
        raise ValueError(f"strategy timeframe {timeframe} history is insufficient")
    return bars


def average_true_range(bars: list[dict[str, Any]], period: int = 14) -> float:
    if len(bars) < period + 1:
        raise ValueError("market history is insufficient for ATR grid planning")
    true_ranges: list[float] = []
    previous_close: float | None = None
    for bar in bars[-(period + 1):]:
        high = positive_number(bar.get("high"), "bar high")
        low = positive_number(bar.get("low"), "bar low")
        close = positive_number(bar.get("close"), "bar close")
        if high < low:
            raise ValueError("bar high is below bar low")
        if previous_close is not None:
            true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
        previous_close = close
    return sum(true_ranges[-period:]) / period


def account_equity(account: dict[str, Any]) -> float:
    for key in ("equity", "ending_cash", "starting_cash"):
        value = number_or(account.get(key), 0.0)
        if value > 0:
            return value
    return 10_000.0


def positive_number(value: Any, label: str) -> float:
    parsed = number_or(value, math.nan)
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{label} must be positive")
    return parsed


def number_or(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return float(default)
    return parsed if math.isfinite(parsed) else float(default)


def non_negative_number(value: Any, label: str) -> float:
    parsed = number_or(value, math.nan)
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError(f"{label} must be non-negative")
    return parsed


def preview_id(preview: dict[str, Any]) -> str:
    raw = json.dumps({
        "cycle_id": preview.get("cycle_id"),
        "direction": preview.get("direction"),
        "style": preview.get("style"),
        "range": preview.get("range"),
        "grid": preview.get("grid"),
        "orders": preview.get("orders"),
    }, sort_keys=True, separators=(",", ":"))
    return f"grid-preview-{hashlib.sha256(raw.encode()).hexdigest()[:12]}"


def build_grid_preview(
    cycle_id: str,
    payload: dict[str, Any] | None = None,
    *,
    market: dict[str, Any],
    account: dict[str, Any] | None = None,
    config: dict[str, Any],
    allow_unsafe_manual_preview: bool = False,
) -> dict[str, Any]:
    body = dict(payload or {})
    validate_market(market)
    direction = str(body.get("direction") or "neutral").lower()
    style = str(body.get("style") or "steady").lower()
    if direction not in GRID_DIRECTIONS:
        raise ValueError("direction must be neutral, long, or short")
    if style not in GRID_STYLES:
        raise ValueError("style must be steady or aggressive")

    latest = positive_number(market.get("latest_close"), "market latest_close")
    strategy_cfg = dict(config.get("strategy_grid") or {})
    range_timeframe = str(strategy_cfg.get("range_timeframe") or "1d")
    spacing_timeframe = str(strategy_cfg.get("spacing_timeframe") or "4h")
    execution_timeframe = str(strategy_cfg.get("execution_timeframe") or "1m")
    range_period = int(strategy_cfg.get("range_atr_period") or 14)
    spacing_period = int(strategy_cfg.get("spacing_atr_period") or 14)
    range_bars = strategy_bars(market, range_timeframe, range_period)
    spacing_bars = strategy_bars(market, spacing_timeframe, spacing_period)
    range_atr = average_true_range(range_bars, period=range_period)
    spacing_atr = average_true_range(spacing_bars, period=spacing_period)
    styles = strategy_cfg.get("styles") if isinstance(strategy_cfg.get("styles"), dict) else {}
    style_cfg = dict(styles.get(style) or {})
    range_multiple = positive_number(style_cfg.get("range_atr_multiple"), "range ATR multiple")
    spacing_multiple = positive_number(style_cfg.get("spacing_atr_multiple"), "spacing ATR multiple")
    # Capital and profit policy are global. A style changes price geometry
    # only; it must not secretly change leverage or the dollar objective.
    margin_utilization = positive_number(strategy_cfg.get("capital_utilization_cap", 1.0), "capital utilization cap")
    if margin_utilization > 1:
        raise ValueError("capital utilization cap must not exceed 1")
    min_net_profit_target = positive_number(
        strategy_cfg.get("min_net_profit_per_grid_usd", 10.0),
        "minimum net profit per grid",
    )
    cost_per_side_rate = non_negative_number(
        config.get("cost_per_side_bp", 0.5),
        "cost per side bp",
    ) / 10_000.0

    # Direction controls which side is armed. It does not secretly move the
    # market range; identical market evidence must produce identical geometry.
    half_span = range_atr * range_multiple
    suggested_low, suggested_high = latest - half_span, latest + half_span

    range_input = body.get("range") if isinstance(body.get("range"), dict) else {}
    low = number_or(range_input.get("low"), suggested_low)
    high = number_or(range_input.get("high"), suggested_high)
    if low <= 0 or high <= low:
        raise ValueError("grid range must have positive low below high")
    grid_input = body.get("grid") if isinstance(body.get("grid"), dict) else {}
    mode = str(grid_input.get("mode") or strategy_cfg.get("default_mode") or "arithmetic").lower()
    if mode not in GRID_MODES:
        raise ValueError("grid mode must be arithmetic or geometric")
    target_spacing = max(
        spacing_atr * spacing_multiple,
        latest * float(config.get("cost_per_side_bp") or 0.0) * 2.0
        * float(strategy_cfg.get("cost_spacing_multiple") or 1.0) / 10_000.0,
        0.0001,
    )
    min_count = max(2, int(strategy_cfg.get("min_grid_count") or MIN_GRID_COUNT))
    max_count = max(min_count, int(strategy_cfg.get("max_grid_count") or MAX_GRID_COUNT))
    requested_count = number_or(grid_input.get("count"), 0.0)
    initial_count = int(requested_count) if requested_count > 0 else max(
        min_count,
        min(max_count, int((high - low) / target_spacing)),
    )
    if initial_count < min_count or initial_count > max_count:
        raise ValueError(f"grid count must be between {min_count} and {max_count}")

    equity = account_equity(account or {})
    risk_input = body.get("risk_budget") if isinstance(body.get("risk_budget"), dict) else {}
    leverage_limit = float(config.get("max_leverage") or 10.0)
    leverage = positive_number(
        strategy_cfg.get("required_leverage", leverage_limit),
        "required leverage",
    )
    if leverage > leverage_limit or abs(leverage - leverage_limit) > 1e-12:
        raise ValueError("grid required leverage must equal the configured leverage limit")
    requested_leverage = risk_input.get("leverage", body.get("leverage"))
    if requested_leverage not in (None, "") and abs(number_or(requested_leverage, 0.0) - leverage) > 1e-12:
        raise ValueError(f"grid leverage is fixed at {leverage:g}x")

    absolute_notional_ceiling = equity * leverage
    capital_budget = absolute_notional_ceiling * margin_utilization
    requested_notional = number_or(grid_input.get("notional_per_grid"), 0.0)
    default_notional_mode = "manual" if requested_notional > 0 else "auto"
    notional_mode = str(grid_input.get("notional_mode") or default_notional_mode).lower()
    if notional_mode not in {"auto", "manual"}:
        raise ValueError("grid notional_mode must be auto or manual")
    if notional_mode == "manual" and requested_notional <= 0:
        raise ValueError("manual notional_per_grid must be greater than zero")

    # Auto mode promises the densest feasible grid in the configured band.
    # ATR still explains target spacing, but must not truncate feasibility.
    candidates = [initial_count] if requested_count > 0 else list(range(max_count, min_count - 1, -1))
    selected: dict[str, Any] | None = None
    first_candidate: dict[str, Any] | None = None
    for candidate_count in candidates:
        try:
            levels, spacing_ratio, lower_stop, upper_stop, provisional_orders = _grid_geometry(
                low=low,
                high=high,
                count=candidate_count,
                mode=mode,
                latest=latest,
                direction=direction,
                config=config,
            )
        except ValueError as error:
            if (
                requested_count > 0
                or str(error) != "grid spacing is smaller than venue price precision"
            ):
                raise
            continue
        side_counts = {
            side: sum(1 for order in provisional_orders if order["side"] == side)
            for side in ("buy", "sell")
        }
        max_simultaneous_levels = max(side_counts.values())
        capital_notional_cap = capital_budget / max_simultaneous_levels
        notional = requested_notional if notional_mode == "manual" else capital_notional_cap
        try:
            orders = _orders_at_notional(provisional_orders, notional, config)
        except ValueError as error:
            if (
                requested_count > 0
                or str(error) != "execution quantity rounds to zero at venue precision"
            ):
                raise
            continue
        net_profits = [planned_net_profit_usd(order, cost_per_side_rate) for order in orders]
        for order, net_profit in zip(orders, net_profits):
            order["planned_net_profit_usd"] = round(net_profit, 8)
        side_notionals = {
            side: sum(float(order["notional"]) for order in orders if order["side"] == side)
            for side in ("buy", "sell")
        }
        max_side_notional = max(side_notionals.values())
        candidate = {
            "count": candidate_count,
            "levels": levels,
            "spacing_ratio": spacing_ratio,
            "lower_stop": lower_stop,
            "upper_stop": upper_stop,
            "provisional_orders": provisional_orders,
            "orders": orders,
            "side_counts": side_counts,
            "max_simultaneous_levels": max_simultaneous_levels,
            "capital_notional_cap": capital_notional_cap,
            "notional": notional,
            "net_profits": net_profits,
            "max_side_notional": max_side_notional,
            "capital_budget_exceeded": max_side_notional > capital_budget + 1e-8,
            "profit_target_met": min(net_profits) + 1e-8 >= min_net_profit_target,
        }
        first_candidate = first_candidate or candidate
        if not candidate["capital_budget_exceeded"] and candidate["profit_target_met"]:
            selected = candidate
            break

    if selected is None:
        if allow_unsafe_manual_preview and first_candidate is not None:
            selected = first_candidate
        else:
            raise ValueError(
                f"no grid between {min_count} and {max_count} levels can deliver "
                f"planned net profit of {min_net_profit_target:.2f} USD per grid within {leverage:g}x capacity"
            )

    count = int(selected["count"])
    levels = list(selected["levels"])
    spacing_ratio = selected["spacing_ratio"]
    provisional_orders = list(selected["provisional_orders"])
    orders = list(selected["orders"])
    side_counts = dict(selected["side_counts"])
    max_simultaneous_levels = int(selected["max_simultaneous_levels"])
    capital_notional_cap = float(selected["capital_notional_cap"])
    notional = float(selected["notional"])
    net_profits = list(selected["net_profits"])
    max_side_notional = float(selected["max_side_notional"])
    spacing = (high - low) / count
    level_spacings = [right - left for left, right in zip(levels, levels[1:])]
    side_losses = {
        side: sum(abs(order["price"] - order["sl"]) * order["quantity"] for order in orders if order["side"] == side)
        for side in ("buy", "sell")
    }
    max_loss = max(side_losses.values())
    estimated_margin = max_side_notional / leverage
    net_profit_rates = [net / float(order["notional"]) for order, net in zip(orders, net_profits)]
    preview = {
        "schema_version": "strategy-grid-preview-v1",
        "cycle_id": cycle_id,
        "direction": direction,
        "style": style,
        "market": {
            "price": latest,
            "symbol": str(market.get("symbol") or "GOLD"),
            "timestamp": market.get("latest_timestamp"),
            "provider": market.get("provider"),
            "timeframe": market.get("timeframe"),
            "execution_timeframe": execution_timeframe,
        },
        "range": {
            "low": round(low, 4),
            "high": round(high, 4),
            "method": f"D1 ATR{range_period} × {range_multiple:g}",
            "source_timeframe": range_timeframe,
            "atr_period": range_period,
            "atr": round(range_atr, 4),
            "atr_multiple": range_multiple,
        },
        "grid": {
            "count": count,
            "mode": mode,
            "levels": [round(level, 8) for level in levels],
            "spacing": round(spacing, 4),
            "spacing_ratio": round(spacing_ratio, 10) if spacing_ratio else None,
            "min_spacing": round(min(level_spacings), 4),
            "max_spacing": round(max(level_spacings), 4),
            "target_spacing": round(target_spacing, 4),
            "spacing_source_timeframe": spacing_timeframe,
            "spacing_atr_period": spacing_period,
            "spacing_atr": round(spacing_atr, 4),
            "spacing_atr_multiple": spacing_multiple,
            "notional_per_grid": round(notional, 2),
            "notional_mode": notional_mode,
            "leverage": round(leverage, 2),
            "leverage_limit": round(leverage_limit, 2),
            "margin_utilization_cap": margin_utilization,
            "net_profit_per_grid_pct": round(min(net_profit_rates) * 100.0, 4),
            "min_net_profit_per_grid_usd": round(min(net_profits), 2),
            "target_net_profit_per_grid_usd": round(min_net_profit_target, 2),
            "profit_target_met": min(net_profits) + 1e-8 >= min_net_profit_target,
            "profit_calculation": "modeled_entry_exit_fees_after_execution_rounding_funding_excluded",
            "out_of_range": str(body.get("out_of_range") or grid_input.get("out_of_range") or "exit_only"),
        },
        "orders": orders,
        "risk": {
            "equity": round(equity, 2),
            "estimated_margin": round(estimated_margin, 2),
            "max_loss": round(max_loss, 2),
            "max_loss_role": "advisory_only",
            "absolute_notional_ceiling": round(absolute_notional_ceiling, 2),
            "capital_budget": round(capital_budget, 2),
            "capital_notional_cap_per_grid": round(capital_notional_cap, 2),
            "safe_notional_cap_per_grid": round(capital_notional_cap, 2),
            "max_simultaneous_same_side_levels": max_simultaneous_levels,
            "actual_leverage": round(max_side_notional / equity, 4) if equity else None,
            "capital_utilization_pct": round(max_side_notional / absolute_notional_ceiling * 100.0, 4),
            "sizing_constraint": "min_net_profit_within_leverage_capacity",
            "profit_target_met": min(net_profits) + 1e-8 >= min_net_profit_target,
            "capital_budget_exceeded": bool(selected["capital_budget_exceeded"]),
            "calibration_status": "shadow_candidate",
        },
        "strategy_timeframes": {
            "range": range_timeframe,
            "spacing": spacing_timeframe,
            "execution": execution_timeframe,
        },
    }
    preview["preview_id"] = preview_id(preview)
    return preview
