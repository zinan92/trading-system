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
from copy import deepcopy
from decimal import Decimal, ROUND_FLOOR
from typing import Any

from services.dualtrack_execution_contract import normalize_execution_command

GRID_DIRECTIONS = {"neutral", "long", "short"}
GRID_STYLES = {"steady", "aggressive"}
GRID_MODES = {"arithmetic", "geometric"}
MIN_GRID_COUNT = 30
MAX_GRID_COUNT = 70
ADAPTIVE_MIN_GRID_COUNT = 2
ADAPTIVE_MAX_GRID_COUNT = 200
ADAPTIVE_MANUAL_LEVERAGE_LIMIT = 20.0
ADAPTIVE_LOCKS = {
    "range",
    "grid_count",
    "profit_target",
    "notional_per_grid",
    "leverage",
}


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


def order_at_notional(
    order: dict[str, Any],
    notional: float,
    config: dict[str, Any],
) -> dict[str, Any]:
    normalized = normalize_execution_command(order, config)
    price = positive_number(normalized.get("price"), "executable grid price")
    quantity = _floor_quantity(notional / price, config)
    return normalize_execution_command({**normalized, "quantity": quantity}, config)


def orders_at_notional(
    provisional: list[dict[str, Any]],
    notional: float,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    return [order_at_notional(order, notional, config) for order in provisional]


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


def _directional_count_bounds(
    minimum: int,
    maximum: int,
    direction: str,
) -> tuple[int, int]:
    """Return user-visible executable-count bounds for the armed side(s)."""

    if direction == "neutral":
        return minimum, maximum
    return max(2, math.ceil(minimum / 2)), max(2, math.ceil(maximum / 2))


def _executable_range(
    low: float,
    high: float,
    latest: float,
    direction: str,
) -> tuple[float, float]:
    """Trim a two-sided envelope to the side which can actually be armed.

    A previously returned one-sided range is stable on the next preview because
    its market-side boundary already equals ``latest``.  Ranges wholly on the
    executable side are also preserved; the existing geometry validator rejects
    ranges which cannot contain an order for the selected direction.
    """

    if low < latest < high:
        if direction == "long":
            return low, latest
        if direction == "short":
            return latest, high
    return low, high


def preview_id(preview: dict[str, Any]) -> str:
    solver = preview.get("solver") if isinstance(preview.get("solver"), dict) else {}
    raw = json.dumps({
        "cycle_id": preview.get("cycle_id"),
        "direction": preview.get("direction"),
        "style": preview.get("style"),
        "range": preview.get("range"),
        "grid": preview.get("grid"),
        "orders": preview.get("orders"),
        "solver": {
            "mode": solver.get("mode"),
            "locked": solver.get("locked"),
            "locked_inputs": solver.get("locked_inputs"),
        } if solver else None,
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
    solver = body.get("solver") if isinstance(body.get("solver"), dict) else {}
    if str(solver.get("mode") or "") == "manual_adaptive":
        return build_adaptive_grid_preview(
            cycle_id,
            body,
            market=market,
            account=account,
            config=config,
        )
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

    # The ATR envelope is analysis input.  The public Range is the executable
    # price region: long uses its lower half, short its upper half, while neutral
    # keeps the complete envelope.  This keeps displayed geometry identical to
    # the orders which can actually be submitted.
    half_span = range_atr * range_multiple
    envelope_low, envelope_high = latest - half_span, latest + half_span

    range_input = body.get("range") if isinstance(body.get("range"), dict) else {}
    requested_low = number_or(range_input.get("low"), envelope_low)
    requested_high = number_or(range_input.get("high"), envelope_high)
    source_envelope_input = (
        range_input.get("source_envelope")
        if isinstance(range_input.get("source_envelope"), dict)
        else {}
    )
    source_envelope_low = number_or(
        source_envelope_input.get("low"),
        requested_low,
    )
    source_envelope_high = number_or(
        source_envelope_input.get("high"),
        requested_high,
    )
    requested_scope = str(range_input.get("scope") or "")
    requested_split_price = number_or(range_input.get("split_price"), latest)
    if requested_scope == f"{direction}_side":
        low, high = requested_low, requested_high
    else:
        low, high = _executable_range(
            requested_low,
            requested_high,
            latest,
            direction,
        )
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
    configured_min_count = max(2, int(strategy_cfg.get("min_grid_count") or MIN_GRID_COUNT))
    configured_max_count = max(
        configured_min_count,
        int(strategy_cfg.get("max_grid_count") or MAX_GRID_COUNT),
    )
    min_count, max_count = _directional_count_bounds(
        configured_min_count,
        configured_max_count,
        direction,
    )
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
            orders = orders_at_notional(provisional_orders, notional, config)
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
            "scope": "full" if direction == "neutral" else f"{direction}_side",
            "split_price": round(
                requested_split_price
                if requested_scope == f"{direction}_side"
                else latest,
                4,
            ),
            "source_envelope": {
                "low": round(source_envelope_low, 4),
                "high": round(source_envelope_high, 4),
            },
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


def _adaptive_config(
    config: dict[str, Any],
    *,
    leverage: float,
    target_profit: float,
) -> dict[str, Any]:
    adapted = deepcopy(config)
    strategy = dict(adapted.get("strategy_grid") or {})
    strategy.update({
        "min_grid_count": ADAPTIVE_MIN_GRID_COUNT,
        "max_grid_count": ADAPTIVE_MAX_GRID_COUNT,
        "required_leverage": leverage,
        "min_net_profit_per_grid_usd": target_profit,
    })
    adapted["strategy_grid"] = strategy
    adapted["max_leverage"] = leverage
    return adapted


def _adaptive_candidate(
    cycle_id: str,
    body: dict[str, Any],
    *,
    market: dict[str, Any],
    account: dict[str, Any],
    config: dict[str, Any],
    count: int,
    leverage: float,
    target_profit: float,
    notional: float | None,
) -> dict[str, Any]:
    candidate_body = {
        key: value
        for key, value in body.items()
        if key != "solver"
    }
    grid = dict(candidate_body.get("grid") or {})
    grid["count"] = count
    if notional is None:
        grid.pop("notional_per_grid", None)
        grid["notional_mode"] = "auto"
    else:
        grid["notional_per_grid"] = notional
        grid["notional_mode"] = "manual"
    candidate_body["grid"] = grid
    candidate_body["risk_budget"] = {"leverage": leverage}
    return build_grid_preview(
        cycle_id,
        candidate_body,
        market=market,
        account=account,
        config=_adaptive_config(
            config,
            leverage=leverage,
            target_profit=target_profit,
        ),
        allow_unsafe_manual_preview=True,
    )


def _required_notional_for_profit(
    cycle_id: str,
    body: dict[str, Any],
    *,
    market: dict[str, Any],
    account: dict[str, Any],
    config: dict[str, Any],
    count: int,
    leverage: float,
    target_profit: float,
) -> float | None:
    """Estimate the venue-rounded notional required for the requested profit."""

    equity = account_equity(account)
    upper = max(equity * ADAPTIVE_MANUAL_LEVERAGE_LIMIT, 1_000.0)
    try:
        upper_preview = _adaptive_candidate(
            cycle_id,
            body,
            market=market,
            account=account,
            config=config,
            count=count,
            leverage=leverage,
            target_profit=target_profit,
            notional=upper,
        )
    except ValueError as error:
        if str(error) == "execution quantity rounds to zero at venue precision":
            return None
        raise
    reference_profit = min(
        float(order["planned_net_profit_usd"])
        for order in upper_preview["orders"]
    )
    if reference_profit <= 0 or reference_profit + 1e-8 < target_profit:
        return None
    notional = upper * target_profit / reference_profit
    for _ in range(8):
        # One venue quantity step can move the minimum order after scaling.
        # A deterministic cushion prevents a displayed 10.00 from being
        # internally classified as 9.999999 after the final normalization.
        notional = math.ceil(notional * 1.001 * 100.0) / 100.0
        try:
            preview = _adaptive_candidate(
                cycle_id,
                body,
                market=market,
                account=account,
                config=config,
                count=count,
                leverage=leverage,
                target_profit=target_profit,
                notional=notional,
            )
        except ValueError as error:
            if str(error) != "execution quantity rounds to zero at venue precision":
                raise
            notional *= 2.0
            continue
        achieved = min(
            float(order["planned_net_profit_usd"])
            for order in preview["orders"]
        )
        if achieved + 1e-8 >= target_profit:
            return notional
        if achieved <= 0:
            return None
        notional *= target_profit / achieved
    return notional


def build_adaptive_grid_preview(
    cycle_id: str,
    payload: dict[str, Any],
    *,
    market: dict[str, Any],
    account: dict[str, Any] | None,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Solve unlocked Paper-grid variables and expose policy deviations as flags.

    This is intentionally a preview-only policy layer over the existing geometry,
    venue rounding and economics implementation. It never weakens malformed range,
    stale market, synthetic data, or venue-precision validation.
    """

    body = dict(payload or {})
    solver = dict(body.get("solver") or {})
    locks = {
        str(value)
        for value in solver.get("locked") or []
        if str(value) in ADAPTIVE_LOCKS
    }
    grid = dict(body.get("grid") or {})
    risk_budget = dict(body.get("risk_budget") or {})
    strategy = dict(config.get("strategy_grid") or {})
    direction = str(body.get("direction") or "neutral").lower()
    configured_preferred_min = max(
        2,
        int(strategy.get("min_grid_count") or MIN_GRID_COUNT),
    )
    configured_preferred_max = max(
        configured_preferred_min,
        int(strategy.get("max_grid_count") or MAX_GRID_COUNT),
    )
    preferred_min, preferred_max = _directional_count_bounds(
        configured_preferred_min,
        configured_preferred_max,
        direction,
    )
    recommended_leverage = positive_number(
        strategy.get("required_leverage", config.get("max_leverage") or 10.0),
        "recommended leverage",
    )
    target_profit = positive_number(
        (
            grid.get("target_net_profit_per_grid_usd")
            if "profit_target" in locks
            else strategy.get("min_net_profit_per_grid_usd")
        )
        or 10.0,
        "minimum net profit per grid",
    )
    requested_count_value = number_or(grid.get("count"), math.nan)
    requested_count = int(requested_count_value) if math.isfinite(requested_count_value) else 0
    if "grid_count" in locks:
        if not math.isfinite(requested_count_value) or not requested_count_value.is_integer():
            raise ValueError("locked grid count must be an integer")
        if not ADAPTIVE_MIN_GRID_COUNT <= requested_count <= ADAPTIVE_MAX_GRID_COUNT:
            raise ValueError(
                f"grid count must be between {ADAPTIVE_MIN_GRID_COUNT} and {ADAPTIVE_MAX_GRID_COUNT}"
            )
    requested_notional = number_or(grid.get("notional_per_grid"), 0.0)
    if "notional_per_grid" in locks and requested_notional <= 0:
        raise ValueError("notional per grid must be positive")
    requested_leverage = number_or(risk_budget.get("leverage", body.get("leverage")), recommended_leverage)
    if "leverage" in locks and not (1.0 <= requested_leverage <= ADAPTIVE_MANUAL_LEVERAGE_LIMIT):
        raise ValueError(
            f"manual Paper leverage must be between 1x and {ADAPTIVE_MANUAL_LEVERAGE_LIMIT:g}x"
        )
    sizing_leverage = requested_leverage if "leverage" in locks else recommended_leverage
    account_body = dict(account or {})
    if "range" not in locks:
        body.pop("range", None)

    current_count = int(number_or(solver.get("current_grid_count"), preferred_max))
    current_direction = str(solver.get("current_direction") or direction).lower()
    direction_changed = (
        current_direction in GRID_DIRECTIONS
        and direction in GRID_DIRECTIONS
        and current_direction != direction
    )
    if current_direction == "neutral" and direction in {"long", "short"}:
        current_count = math.ceil(current_count / 2)
    elif current_direction in {"long", "short"} and direction == "neutral":
        current_count *= 2
    current_count = max(
        ADAPTIVE_MIN_GRID_COUNT,
        min(ADAPTIVE_MAX_GRID_COUNT, current_count),
    )

    if "grid_count" in locks:
        counts = [requested_count]
    elif direction_changed:
        # A direction click has an explicit geometric meaning: preserve the
        # current density, mapping neutral to one executable side (and back).
        # Capital/profit deviations are surfaced as flags rather than silently
        # changing the user's 39 -> 20 expectation into another grid count.
        counts = [current_count]
    else:
        counts = list(range(preferred_max, ADAPTIVE_MIN_GRID_COUNT - 1, -1))

    evaluated: list[dict[str, Any]] = []
    precision_error: ValueError | None = None
    for count in counts:
        try:
            cap_preview = _adaptive_candidate(
                cycle_id,
                body,
                market=market,
                account=account_body,
                config=config,
                count=count,
                leverage=sizing_leverage,
                target_profit=target_profit,
                notional=None,
            )
        except ValueError as error:
            if (
                "grid_count" not in locks
                and str(error) == "grid spacing is smaller than venue price precision"
            ):
                precision_error = error
                continue
            raise
        safe_notional = float(cap_preview["risk"]["safe_notional_cap_per_grid"])
        try:
            required_notional = _required_notional_for_profit(
                cycle_id,
                body,
                market=market,
                account=account_body,
                config=config,
                count=count,
                leverage=sizing_leverage,
                target_profit=target_profit,
            )
        except ValueError as error:
            if (
                "grid_count" not in locks
                and str(error) == "grid spacing is smaller than venue price precision"
            ):
                precision_error = error
                continue
            raise
        if "notional_per_grid" in locks:
            chosen_notional = requested_notional
        elif "leverage" in locks:
            chosen_notional = min(required_notional or safe_notional, safe_notional)
        else:
            manual_cap_preview = _adaptive_candidate(
                cycle_id,
                body,
                market=market,
                account=account_body,
                config=config,
                count=count,
                leverage=ADAPTIVE_MANUAL_LEVERAGE_LIMIT,
                target_profit=target_profit,
                notional=None,
            )
            manual_notional_cap = float(
                manual_cap_preview["risk"]["safe_notional_cap_per_grid"]
            )
            chosen_notional = min(
                required_notional or manual_notional_cap,
                manual_notional_cap,
            )
        try:
            preview = _adaptive_candidate(
                cycle_id,
                body,
                market=market,
                account=account_body,
                config=config,
                count=count,
                leverage=sizing_leverage,
                target_profit=target_profit,
                notional=chosen_notional,
            )
        except ValueError as error:
            if (
                "grid_count" not in locks
                and str(error) == "grid spacing is smaller than venue price precision"
            ):
                precision_error = error
                continue
            raise
        actual_leverage = float(preview["risk"]["actual_leverage"] or 0.0)
        if "leverage" not in locks:
            selected_leverage = min(
                ADAPTIVE_MANUAL_LEVERAGE_LIMIT,
                max(recommended_leverage, math.ceil(actual_leverage * 100.0) / 100.0),
            )
            preview = _adaptive_candidate(
                cycle_id,
                body,
                market=market,
                account=account_body,
                config=config,
                count=count,
                leverage=selected_leverage,
                target_profit=target_profit,
                notional=chosen_notional,
            )
        evaluated.append(preview)

    if not evaluated:
        if precision_error is not None:
            raise precision_error
        raise ValueError("no executable grid candidate could be generated")

    def candidate_score(row: dict[str, Any]) -> tuple[float, ...]:
        row_count = int(row["grid"]["count"])
        row_actual_leverage = float(row["risk"]["actual_leverage"] or math.inf)
        row_margin = float(row["risk"]["estimated_margin"] or math.inf)
        row_equity = float(row["risk"]["equity"] or 0.0)
        row_profit = float(row["grid"]["min_net_profit_per_grid_usd"] or 0.0)
        band_distance = (
            preferred_min - row_count
            if row_count < preferred_min
            else row_count - preferred_max
            if row_count > preferred_max
            else 0
        )
        return (
            max(0.0, row_actual_leverage - ADAPTIVE_MANUAL_LEVERAGE_LIMIT),
            max(0.0, row_margin - row_equity),
            max(0.0, row_actual_leverage - recommended_leverage),
            max(0.0, target_profit - row_profit),
            float(band_distance),
            float(abs(row_count - current_count)),
            float(-row_count),
        )

    selected = evaluated[0] if "grid_count" in locks else min(evaluated, key=candidate_score)

    count = int(selected["grid"]["count"])
    actual_leverage = float(selected["risk"]["actual_leverage"] or 0.0)
    selected_leverage = float(selected["grid"]["leverage"])
    latest = positive_number(market.get("latest_close"), "market latest_close")
    low = float(selected["range"]["low"])
    high = float(selected["range"]["high"])
    flags: list[dict[str, Any]] = []

    def flag(code: str, severity: str, message: str) -> None:
        flags.append({"code": code, "severity": severity, "message": message})

    if not preferred_min <= count <= preferred_max:
        flag(
            "grid_count_outside_preferred_band",
            "warning",
            f"{count} 格不在建议的 {preferred_min}–{preferred_max} 格内。",
        )
    if selected["grid"]["profit_target_met"] is not True:
        flag(
            "grid_profit_target_not_met",
            "critical",
            f"每格计划净利 {selected['grid']['min_net_profit_per_grid_usd']:.2f} USD，低于目标 {target_profit:.2f} USD。",
        )
    if actual_leverage > recommended_leverage + 1e-8 or selected_leverage > recommended_leverage + 1e-8:
        flag(
            "recommended_leverage_exceeded",
            "critical",
            f"实际杠杆 {actual_leverage:.2f}x，超过建议 {recommended_leverage:g}x。",
        )
    if float(selected["risk"]["estimated_margin"] or 0.0) > (
        float(selected["risk"]["equity"] or 0.0) + 1e-8
    ):
        flag(
            "margin_budget_exceeded",
            "critical",
            f"预计保证金 {selected['risk']['estimated_margin']:.2f} USD，"
            f"超过账户权益 {selected['risk']['equity']:.2f} USD。",
        )
    if actual_leverage > ADAPTIVE_MANUAL_LEVERAGE_LIMIT + 1e-8:
        flag(
            "manual_leverage_capacity_exceeded",
            "critical",
            f"实际杠杆 {actual_leverage:.2f}x，超过 Paper 手动容量 {ADAPTIVE_MANUAL_LEVERAGE_LIMIT:g}x。",
        )
    if not low <= latest <= high:
        flag(
            "market_price_outside_range",
            "critical",
            f"当前价 {latest:.2f} 位于新 Range 外。",
        )

    def alternative(identifier: str, label: str, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": identifier,
            "label": label,
            "selected": row is selected,
            "grid_count": int(row["grid"]["count"]),
            "notional_per_grid": row["grid"]["notional_per_grid"],
            "planned_profit_per_grid": row["grid"]["min_net_profit_per_grid_usd"],
            "actual_leverage": row["risk"]["actual_leverage"],
            "estimated_margin": row["risk"]["estimated_margin"],
            "max_loss": row["risk"]["max_loss"],
        }

    preserve_count = min(
        evaluated,
        key=lambda row: (
            abs(int(row["grid"]["count"]) - current_count),
            candidate_score(row),
        ),
    )
    profitable = [row for row in evaluated if row["grid"]["profit_target_met"] is True]
    preserve_profit = min(profitable or evaluated, key=candidate_score)
    within_recommended_leverage = [
        row
        for row in evaluated
        if float(row["risk"]["actual_leverage"] or math.inf)
        <= recommended_leverage + 1e-8
    ]
    preserve_leverage = min(
        within_recommended_leverage or evaluated,
        key=candidate_score,
    )

    selected["solver"] = {
        "schema_version": "grid-parameter-solver-v1",
        "mode": "manual_adaptive",
        "locked": sorted(locks),
        "locked_inputs": {
            key: value
            for key, value in {
                "range": (
                    {
                        "low": number_or(
                            (
                                body.get("range")
                                if isinstance(body.get("range"), dict)
                                else {}
                            ).get("low"),
                            0.0,
                        ),
                        "high": number_or(
                            (
                                body.get("range")
                                if isinstance(body.get("range"), dict)
                                else {}
                            ).get("high"),
                            0.0,
                        ),
                    }
                    if "range" in locks
                    else None
                ),
                "grid_count": requested_count if "grid_count" in locks else None,
                "profit_target": target_profit if "profit_target" in locks else None,
                "notional_per_grid": (
                    requested_notional if "notional_per_grid" in locks else None
                ),
                "leverage": requested_leverage if "leverage" in locks else None,
            }.items()
            if value is not None
        },
        "preferred": {
            "min_grid_count": preferred_min,
            "max_grid_count": preferred_max,
            "target_net_profit_per_grid_usd": round(target_profit, 2),
            "recommended_leverage": round(recommended_leverage, 2),
            "manual_paper_leverage_limit": ADAPTIVE_MANUAL_LEVERAGE_LIMIT,
        },
        "risk_flags": flags,
        "selection": (
            "lowest_risk_then_profit_then_grid_band_then_current_plan_distance"
            if "grid_count" not in locks
            else "respect_locked_grid_count"
        ),
        "alternatives": [
            alternative("preserve_grid_count", "保格数", preserve_count),
            alternative("preserve_profit_target", "保收益", preserve_profit),
            alternative("preserve_recommended_leverage", "保杠杆", preserve_leverage),
        ],
    }
    selected["preview_id"] = preview_id(selected)
    return selected
