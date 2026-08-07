"""Pure DCA round planning and deterministic preview replay.

The DCA contract is deliberately separate from per-grid take-profit lifecycle
state.  Entry fills accumulate into one directional round; one aggregate exit
specification describes how the complete open quantity must be closed later by
an execution adapter.  This module performs no I/O and never writes a plan,
order, position, or risk receipt.
"""

from __future__ import annotations

import hashlib
import json
import math
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable

from services.dualtrack_execution_contract import normalize_execution_command
from services.grid_sizing import account_equity, order_at_notional, validate_market


DCA_PREVIEW_SCHEMA = "strategy-dca-preview-v1"
DCA_PLAN_SCHEMA = "strategy-plan-v1"
DCA_REPLAY_SCHEMA = "strategy-dca-replay-v1"
DCA_DIRECTIONS = {"long", "short"}
DETERMINISTIC_DCA_CANDIDATE_VERSION = "dca-smart-fill-v1"


def build_deterministic_dca_candidate_payload_v1(
    *,
    direction: str,
    market_price: float,
) -> dict[str, Any]:
    """Return the existing Dashboard smart-fill contract as a pure payload."""

    normalized_direction = str(direction or "").strip().lower()
    if normalized_direction not in DCA_DIRECTIONS:
        raise ValueError("DCA recommendation requires long or short")
    price = _positive_number(market_price, "market price")
    is_long = normalized_direction == "long"
    low = _dashboard_price_2(
        price * (0.98 if is_long else 1.0015)
    )
    high = _dashboard_price_2(
        price * (0.9985 if is_long else 1.02)
    )
    count = 6
    step = (high - low) / (count - 1)
    levels = [
        high - index * step if is_long else low + index * step
        for index in range(count)
    ]
    return {
        "candidate_builder_version": DETERMINISTIC_DCA_CANDIDATE_VERSION,
        "strategy_type": "dca",
        "direction": normalized_direction,
        "dca": {
            "entry_levels": levels,
            "target_price": _dashboard_price_2(
                price * (1.01 if is_long else 0.99)
            ),
            "stop_price": _dashboard_price_2(
                price * (0.97 if is_long else 1.03)
            ),
            "notional_per_addition": 2_000.0,
            "max_additions": count,
            "loop_enabled": False,
        },
        "risk_budget": {"leverage": 10.0},
    }


def _dashboard_price_2(value: float) -> float:
    """Apply ``toFixed(2)`` to the exact IEEE-754 product value."""

    return float(
        Decimal.from_float(value).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    )


def build_dca_preview(
    cycle_id: str,
    payload: dict[str, Any] | None,
    *,
    market: dict[str, Any],
    account: dict[str, Any] | None,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Build one deterministic fixed-target DCA round preview."""

    validate_market(market)
    body = dict(payload or {})
    rendered_cycle_id = _required_text(cycle_id, "cycle_id")
    direction = str(body.get("direction") or "").strip().lower()
    if direction not in DCA_DIRECTIONS:
        raise ValueError("DCA direction must be long or short")

    settings = body.get("dca") if isinstance(body.get("dca"), dict) else {}
    raw_levels = settings.get("entry_levels")
    if not isinstance(raw_levels, list) or not raw_levels:
        raise ValueError("DCA entry_levels must be a non-empty list")
    max_additions = _positive_integer(
        settings.get("max_additions", len(raw_levels)),
        "DCA max_additions",
    )
    if max_additions > len(raw_levels):
        raise ValueError("DCA max_additions cannot exceed entry level count")

    notional_per_addition = _positive_number(
        settings.get("notional_per_addition"),
        "DCA notional_per_addition",
    )
    target_price = _normalized_price(
        settings.get("target_price"),
        "DCA target_price",
        config,
    )
    stop_price = _normalized_price(
        settings.get("stop_price"),
        "DCA stop_price",
        config,
    )
    loop_enabled = settings.get("loop_enabled", False)
    if not isinstance(loop_enabled, bool):
        raise ValueError("DCA loop_enabled must be boolean")
    if loop_enabled:
        # No engine path starts another accumulation round in v1; accepting the
        # flag would advertise behavior that never executes.
        raise ValueError(
            "DCA loop_enabled=true is not supported in v1; "
            "the round stops after its aggregate TP/SL"
        )

    entries: list[dict[str, Any]] = []
    for index, raw_price in enumerate(raw_levels[:max_additions], start=1):
        normalized = order_at_notional(
            {
                "preview_entry_id": f"dca-entry-{index:02d}",
                "level": index,
                "state": "preview",
                "side": "buy" if direction == "long" else "sell",
                "event": "entry",
                "order_type": "limit",
                "price": _positive_number(raw_price, "DCA entry price"),
                "sl": stop_price,
            },
            notional_per_addition,
            config,
        )
        entries.append(normalized)

    prices = [float(row["price"]) for row in entries]
    if len(set(prices)) != len(prices):
        raise ValueError("DCA entry levels collapse to duplicates at venue precision")
    if direction == "long":
        if any(left <= right for left, right in zip(prices, prices[1:])):
            raise ValueError("long DCA entry levels must be strictly descending")
        if target_price <= max(prices):
            raise ValueError("long DCA target must be above every entry")
        if stop_price >= min(prices):
            raise ValueError("long DCA stop must be below every entry")
    else:
        if any(left >= right for left, right in zip(prices, prices[1:])):
            raise ValueError("short DCA entry levels must be strictly ascending")
        if target_price >= min(prices):
            raise ValueError("short DCA target must be below every entry")
        if stop_price <= max(prices):
            raise ValueError("short DCA stop must be above every entry")

    cost_rate = _non_negative_number(
        config.get("cost_per_side_bp", 0.5),
        "cost_per_side_bp",
    ) / 10_000.0
    depth_rows = _depth_economics(
        entries,
        direction=direction,
        target_price=target_price,
        stop_price=stop_price,
        cost_rate=cost_rate,
    )
    equity = account_equity(account or {})
    leverage_limit = _positive_number(config.get("max_leverage", 10.0), "max_leverage")
    risk_budget = body.get("risk_budget") if isinstance(body.get("risk_budget"), dict) else {}
    leverage = _positive_number(
        risk_budget.get("leverage", leverage_limit),
        "DCA leverage",
    )
    capacity_exceeded = leverage > leverage_limit
    total_notional = sum(float(row["notional"]) for row in entries)
    actual_leverage = total_notional / equity
    if actual_leverage > leverage_limit + 1e-12:
        capacity_exceeded = True
    risk_flags: list[dict[str, Any]] = []
    if capacity_exceeded:
        risk_flags.append({
            "code": "dca_capacity_exceeded",
            "severity": "critical",
            "message": (
                f"DCA full-depth exposure {actual_leverage:.2f}x exceeds "
                f"the configured {leverage_limit:g}x capacity."
            ),
        })
    if float(depth_rows[-1]["target_net_pnl_usd"]) <= 0:
        risk_flags.append({
            "code": "dca_target_net_profit_not_positive",
            "severity": "warning",
            "message": "Modeled entry and exit fees consume the full-depth target profit.",
        })

    preview = {
        "schema_version": DCA_PREVIEW_SCHEMA,
        "strategy_type": "dca",
        "cycle_id": rendered_cycle_id,
        "direction": direction,
        "market": {
            "price": _positive_number(market.get("latest_close"), "market latest_close"),
            "symbol": str(market.get("symbol") or "GOLD"),
            "timestamp": market.get("latest_timestamp"),
            "provider": market.get("provider"),
            "timeframe": market.get("timeframe"),
        },
        "dca": {
            "target_mode": "fixed_price",
            "target_price": target_price,
            "stop_price": stop_price,
            "max_additions": max_additions,
            "notional_per_addition": round(notional_per_addition, 8),
            "loop_enabled": loop_enabled,
            "entry_levels": prices,
            "entry_count": len(entries),
            "total_possible_notional": round(total_notional, 8),
            "profit_calculation": (
                "modeled_entry_exit_fees_after_execution_rounding_funding_excluded"
            ),
        },
        "entries": entries,
        "aggregate_take_profit": {
            "side": "sell" if direction == "long" else "buy",
            "event": "target",
            "order_type": "limit",
            "reduce_only": True,
            "price": target_price,
            "quantity_source": "reconciled_open_dca_round_quantity",
            "replace_after_each_entry_fill": True,
            "one_active_order_required": True,
        },
        "depth_economics": depth_rows,
        "risk": {
            "equity": round(equity, 2),
            "selected_leverage": round(leverage, 4),
            "leverage_limit": round(leverage_limit, 4),
            "actual_leverage_at_full_depth": round(actual_leverage, 4),
            "estimated_margin_at_full_depth": round(total_notional / leverage, 2),
            "maximum_loss_at_full_depth": round(
                max(0.0, -float(depth_rows[-1]["stop_net_pnl_usd"])),
                8,
            ),
            "capacity_exceeded": capacity_exceeded,
            "risk_flags": risk_flags,
        },
    }
    if body.get("start_facts_digest") is not None:
        preview["start_facts_digest"] = str(
            body["start_facts_digest"]
        )
    preview["preview_id"] = dca_preview_id(preview)
    return preview


def build_dca_strategy_plan(
    preview: dict[str, Any],
    *,
    strategy_plan_id: str,
    version: int,
    locked_at: str,
) -> dict[str, Any]:
    """Project a preview into a serializable StrategyPlan without persistence."""

    if preview.get("schema_version") != DCA_PREVIEW_SCHEMA:
        raise ValueError("DCA StrategyPlan requires a versioned DCA preview")
    return {
        "schema_version": DCA_PLAN_SCHEMA,
        "strategy_type": "dca",
        "strategy_plan_id": _required_text(strategy_plan_id, "strategy_plan_id"),
        "cycle_id": _required_text(preview.get("cycle_id"), "cycle_id"),
        "version": _positive_integer(version, "strategy plan version"),
        "status": "active",
        "locked_at": _required_text(locked_at, "locked_at"),
        "direction": preview["direction"],
        "dca": {
            **dict(preview["dca"]),
            "entries": [dict(row) for row in preview["entries"]],
            "aggregate_take_profit": dict(preview["aggregate_take_profit"]),
        },
        "execution_context": {"market": dict(preview["market"])},
        "risk_budget": dict(preview["risk"]),
        "preview_id": preview["preview_id"],
        "field_sources": {
            "direction": "confirmed",
            "dca": "confirmed",
            "risk_budget": "confirmed",
        },
    }


def build_dca_entry_commands(
    plan: dict[str, Any],
    *,
    timestamp: str,
) -> list[dict[str, Any]]:
    """Project a DCA StrategyPlan into idempotent accumulation entries."""

    if (
        not isinstance(plan, dict)
        or plan.get("schema_version") != DCA_PLAN_SCHEMA
        or plan.get("strategy_type") != "dca"
    ):
        raise ValueError("DCA execution requires a versioned DCA StrategyPlan")
    plan_id = _required_text(plan.get("strategy_plan_id"), "strategy_plan_id")
    cycle_id = _required_text(plan.get("cycle_id"), "cycle_id")
    version = _positive_integer(plan.get("version"), "strategy plan version")
    submitted_at = _required_text(timestamp, "submission timestamp")
    direction = str(plan.get("direction") or "").lower()
    if direction not in DCA_DIRECTIONS:
        raise ValueError("DCA direction must be long or short")
    dca = plan.get("dca") if isinstance(plan.get("dca"), dict) else {}
    entries = dca.get("entries") if isinstance(dca.get("entries"), list) else []
    if not entries:
        raise ValueError("DCA StrategyPlan has no executable entries")
    context = plan.get("execution_context") if isinstance(plan.get("execution_context"), dict) else {}
    market = context.get("market") if isinstance(context.get("market"), dict) else {}
    market_price = _positive_number(market.get("price"), "execution market context price")
    symbol = _required_text(market.get("symbol"), "execution market context symbol")
    round_id = f"dca-round:{plan_id}"
    commands: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("DCA StrategyPlan entry must be an object")
        entry_id = _required_text(entry.get("preview_entry_id"), "DCA preview_entry_id")
        if entry_id in seen_ids:
            raise ValueError("DCA StrategyPlan contains duplicate entry identity")
        seen_ids.add(entry_id)
        commands.append({
            "cycle_id": cycle_id,
            "ts": submitted_at,
            "symbol": symbol,
            "side": "buy" if direction == "long" else "sell",
            "event": "entry",
            "order_type": "limit",
            "price": _positive_number(entry.get("price"), "DCA entry price"),
            "market_price": market_price,
            "quantity": _positive_number(entry.get("quantity"), "DCA entry quantity"),
            "notional": _positive_number(entry.get("notional"), "DCA entry notional"),
            "sl": _positive_number(dca.get("stop_price"), "DCA stop price"),
            "source": "strategy_dca_paper",
            "source_fill_id": f"strategy-dca:{plan_id}:{entry_id}",
            "preview_entry_id": entry_id,
            "trade_id": round_id,
            "position_id": round_id,
            "dca_round_id": round_id,
            "strategy_type": "dca",
            "strategy_plan_id": plan_id,
            "strategy_plan_version": version,
        })
    return commands


def replay_dca_marks(preview: dict[str, Any], marks: Iterable[float]) -> dict[str, Any]:
    """Replay complete marks against one preview for pure model verification."""

    if preview.get("schema_version") != DCA_PREVIEW_SCHEMA:
        raise ValueError("DCA replay requires a versioned DCA preview")
    direction = str(preview["direction"])
    entries = [dict(row) for row in preview.get("entries") or []]
    target = float(preview["aggregate_take_profit"]["price"])
    stop = float(preview["dca"]["stop_price"])
    filled_ids: set[str] = set()
    open_entries: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    status = "waiting_entry"

    for sequence, raw_mark in enumerate(marks, start=1):
        mark = _positive_number(raw_mark, "DCA replay mark")
        if status in {"target_closed", "stop_closed"}:
            break
        for entry in entries:
            identity = str(entry["preview_entry_id"])
            crossed = mark <= float(entry["price"]) if direction == "long" else mark >= float(entry["price"])
            if identity in filled_ids or not crossed:
                continue
            filled_ids.add(identity)
            open_entries.append(entry)
            events.append({
                "sequence": sequence,
                "event": "entry",
                "entry_id": identity,
                "price": float(entry["price"]),
                "quantity": float(entry["quantity"]),
                "aggregate_quantity": round(
                    sum(float(row["quantity"]) for row in open_entries),
                    8,
                ),
            })
            status = "open"
        if not open_entries:
            continue
        target_hit = mark >= target if direction == "long" else mark <= target
        stop_hit = mark <= stop if direction == "long" else mark >= stop
        if target_hit or stop_hit:
            quantity = sum(float(row["quantity"]) for row in open_entries)
            events.append({
                "sequence": sequence,
                "event": "target" if target_hit else "stop",
                "price": target if target_hit else stop,
                "quantity": round(quantity, 8),
                "covered_entry_ids": [str(row["preview_entry_id"]) for row in open_entries],
            })
            status = "target_closed" if target_hit else "stop_closed"
            open_entries = []

    quantity = sum(float(row["quantity"]) for row in open_entries)
    weighted_average = (
        sum(float(row["price"]) * float(row["quantity"]) for row in open_entries) / quantity
        if quantity > 0
        else None
    )
    return {
        "schema_version": DCA_REPLAY_SCHEMA,
        "preview_id": preview["preview_id"],
        "direction": direction,
        "status": status,
        "additions_filled": len(filled_ids),
        "open_quantity": round(quantity, 8),
        "average_entry_price": round(weighted_average, 8) if weighted_average else None,
        "events": events,
    }


def _depth_economics(
    entries: list[dict[str, Any]],
    *,
    direction: str,
    target_price: float,
    stop_price: float,
    cost_rate: float,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    accumulated: list[dict[str, Any]] = []
    for depth, entry in enumerate(entries, start=1):
        accumulated.append(entry)
        quantity = sum(float(row["quantity"]) for row in accumulated)
        entry_value = sum(float(row["price"]) * float(row["quantity"]) for row in accumulated)
        average = entry_value / quantity
        target_value = target_price * quantity
        stop_value = stop_price * quantity
        target_gross = target_value - entry_value if direction == "long" else entry_value - target_value
        stop_gross = stop_value - entry_value if direction == "long" else entry_value - stop_value
        entry_fees = entry_value * cost_rate
        rows.append({
            "fill_depth": depth,
            "accumulated_quantity": round(quantity, 8),
            "accumulated_notional": round(entry_value, 8),
            "weighted_average_entry": round(average, 8),
            "aggregate_target_quantity": round(quantity, 8),
            "target_price": target_price,
            "target_gross_pnl_usd": round(target_gross, 8),
            "target_modeled_fees_usd": round(entry_fees + target_value * cost_rate, 8),
            "target_net_pnl_usd": round(target_gross - entry_fees - target_value * cost_rate, 8),
            "stop_price": stop_price,
            "stop_gross_pnl_usd": round(stop_gross, 8),
            "stop_modeled_fees_usd": round(entry_fees + stop_value * cost_rate, 8),
            "stop_net_pnl_usd": round(stop_gross - entry_fees - stop_value * cost_rate, 8),
        })
    return rows


def _normalized_price(value: Any, label: str, config: dict[str, Any]) -> float:
    normalized = normalize_execution_command(
        {"price": _positive_number(value, label)},
        config,
    )
    return _positive_number(normalized.get("price"), label)


def dca_preview_id(preview: dict[str, Any]) -> str:
    """Return the content identity for a DCA preview envelope."""

    canonical = dict(preview)
    canonical.pop("preview_id", None)
    canonical.pop("manual_confirmation", None)
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return f"dca-preview-{hashlib.sha256(encoded.encode()).hexdigest()[:12]}"


def _required_text(value: Any, label: str) -> str:
    rendered = str(value or "").strip()
    if not rendered:
        raise ValueError(f"{label} is required")
    return rendered


def _positive_number(value: Any, label: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be positive") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{label} must be positive")
    return parsed


def _non_negative_number(value: Any, label: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be non-negative") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError(f"{label} must be non-negative")
    return parsed


def _positive_integer(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive integer") from exc
    if parsed <= 0 or str(parsed) != str(value).strip():
        raise ValueError(f"{label} must be a positive integer")
    return parsed
