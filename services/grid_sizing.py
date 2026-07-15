"""Deterministic grid geometry and capital sizing.

Single source of truth for the fixed-timeframe grid contract: complete D1
ATR14 owns the range, complete 4H ATR14 owns the spacing, and per-grid
notional is the lower of the capital cap (`equity × leverage ×
margin_utilization ÷ max simultaneous same-side levels`) and the plan-loss
cap (`equity × max_plan_loss_pct ÷ worst same-side loss rate`).

Pure functions only: no I/O, no plan or ledger mutation, no clock reads.
Identical market/account/config inputs must produce an identical preview,
including its `preview_id` hash — the control plane, the start transaction
and future shadow runs all rely on that determinism.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

GRID_DIRECTIONS = {"neutral", "long", "short"}
GRID_STYLES = {"steady", "aggressive"}


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
    margin_utilization = positive_number(style_cfg.get("margin_utilization_cap"), "margin utilization cap")
    max_plan_loss_pct = positive_number(style_cfg.get("max_plan_loss_pct"), "max plan loss pct")
    if margin_utilization > 1 or max_plan_loss_pct > 1:
        raise ValueError("strategy risk fractions must not exceed 1")

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
    target_spacing = max(
        spacing_atr * spacing_multiple,
        latest * float(config.get("cost_per_side_bp") or 0.0) * 2.0
        * float(strategy_cfg.get("cost_spacing_multiple") or 1.0) / 10_000.0,
        0.0001,
    )
    min_count = max(2, int(strategy_cfg.get("min_grid_count") or 24))
    max_count = max(min_count, int(strategy_cfg.get("max_grid_count") or 80))
    requested_count = number_or(grid_input.get("count"), 0.0)
    count = int(requested_count) if requested_count > 0 else max(min_count, min(max_count, int((high - low) / target_spacing)))
    if count < 2 or count > 80:
        raise ValueError("grid count must be between 2 and 80")
    spacing = (high - low) / count
    if spacing <= 0:
        raise ValueError("grid spacing must be positive")

    equity = account_equity(account or {})
    risk_input = body.get("risk_budget") if isinstance(body.get("risk_budget"), dict) else {}
    leverage_limit = float(config.get("max_leverage") or 10.0)
    leverage = number_or(risk_input.get("leverage", body.get("leverage")), leverage_limit)
    if leverage <= 0 or leverage > leverage_limit:
        raise ValueError(f"leverage must be greater than 0 and at most {leverage_limit:g}")

    levels = [low + spacing * index for index in range(count + 1)]
    nearest_index = min(range(len(levels)), key=lambda index: abs(levels[index] - latest))
    provisional_orders: list[dict[str, Any]] = []
    for index, raw_price in enumerate(levels):
        if index == nearest_index:
            continue
        price = round(raw_price, 4)
        side = "buy" if price < latest else "sell"
        if direction == "long" and side != "buy":
            continue
        if direction == "short" and side != "sell":
            continue
        tp = price + spacing if side == "buy" else price - spacing
        sl = low - spacing if side == "buy" else high + spacing
        provisional_orders.append({
            "preview_order_id": f"preview-{index:02d}-{side}",
            "level": index,
            "state": "preview",
            "side": side,
            "event": "entry",
            "order_type": "limit",
            "price": price,
            "tp": round(tp, 4),
            "sl": round(sl, 4),
        })
    if not provisional_orders:
        raise ValueError("selected direction has no executable grid orders in this range")

    side_counts = {
        side: sum(1 for order in provisional_orders if order["side"] == side)
        for side in ("buy", "sell")
    }
    max_simultaneous_levels = max(side_counts.values())
    absolute_notional_ceiling = equity * leverage
    capital_budget = absolute_notional_ceiling * margin_utilization
    capital_notional_cap = capital_budget / max_simultaneous_levels
    side_loss_rates = {
        side: sum(abs(order["price"] - order["sl"]) / order["price"] for order in provisional_orders if order["side"] == side)
        for side in ("buy", "sell")
    }
    max_side_loss_rate = max(side_loss_rates.values())
    max_loss_budget = equity * max_plan_loss_pct
    risk_notional_cap = max_loss_budget / max_side_loss_rate if max_side_loss_rate > 0 else capital_notional_cap
    safe_notional = min(capital_notional_cap, risk_notional_cap)
    requested_notional = number_or(grid_input.get("notional_per_grid"), 0.0)
    default_notional_mode = "manual" if requested_notional > 0 else "auto"
    notional_mode = str(grid_input.get("notional_mode") or default_notional_mode).lower()
    if notional_mode not in {"auto", "manual"}:
        raise ValueError("grid notional_mode must be auto or manual")
    if notional_mode == "manual":
        if requested_notional <= 0:
            raise ValueError("manual notional_per_grid must be greater than zero")
        if requested_notional > safe_notional + 1e-8:
            raise ValueError(
                f"notional_per_grid {requested_notional:.2f} exceeds safe cap {safe_notional:.2f} "
                "for the selected leverage and risk budget"
            )
        notional = requested_notional
    else:
        # Auto sizing is a policy, not a fixed quote. Recalculate it from the
        # same trusted market/account snapshot used by the start transaction.
        notional = safe_notional
    if notional <= 0:
        raise ValueError("safe per-grid notional is zero")
    orders = [
        {
            **order,
            "quantity": round(notional / order["price"], 8),
            "notional": round(notional, 2),
        }
        for order in provisional_orders
    ]
    side_losses = {
        side: sum(abs(order["price"] - order["sl"]) * order["quantity"] for order in orders if order["side"] == side)
        for side in ("buy", "sell")
    }
    max_loss = max(side_losses.values())
    max_side_notional = max(side_counts[side] * notional for side in side_counts)
    estimated_margin = max_side_notional / leverage
    preview = {
        "schema_version": "strategy-grid-preview-v1",
        "cycle_id": cycle_id,
        "direction": direction,
        "style": style,
        "market": {
            "price": latest,
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
            "spacing": round(spacing, 4),
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
            "out_of_range": str(body.get("out_of_range") or grid_input.get("out_of_range") or "exit_only"),
        },
        "orders": orders,
        "risk": {
            "equity": round(equity, 2),
            "estimated_margin": round(estimated_margin, 2),
            "max_loss": round(max_loss, 2),
            "max_loss_budget": round(max_loss_budget, 2),
            "absolute_notional_ceiling": round(absolute_notional_ceiling, 2),
            "capital_budget": round(capital_budget, 2),
            "capital_notional_cap_per_grid": round(capital_notional_cap, 2),
            "risk_notional_cap_per_grid": round(risk_notional_cap, 2),
            "max_simultaneous_same_side_levels": max_simultaneous_levels,
            "actual_leverage": round(max_side_notional / equity, 4) if equity else None,
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
