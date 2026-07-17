"""Pure StrategyPlan-to-execution-command projection."""

from __future__ import annotations

from typing import Any


def build_plan_grid_entry_commands(
    plan: dict[str, Any],
    *,
    timestamp: str | None = None,
) -> list[dict[str, Any]]:
    """Return the exact raw grid commands shared by paper and Shadow replay."""

    if not isinstance(plan, dict) or plan.get("schema_version") != "strategy-plan-v1":
        raise ValueError("grid execution requires a versioned StrategyPlan")
    plan_id = _required_text(plan.get("strategy_plan_id"), "strategy_plan_id")
    cycle_id = _required_text(plan.get("cycle_id"), "cycle_id")
    plan_version = _positive_integer(plan.get("version"), "strategy plan version")
    submitted_at = _required_text(timestamp or plan.get("locked_at"), "submission timestamp")
    context = plan.get("execution_context") if isinstance(plan.get("execution_context"), dict) else {}
    market = context.get("market") if isinstance(context.get("market"), dict) else {}
    market_price = _positive_number(market.get("price"), "execution market context price")
    symbol = _required_text(market.get("symbol"), "execution market context symbol")
    grid = plan.get("grid") if isinstance(plan.get("grid"), dict) else {}
    orders = grid.get("orders") if isinstance(grid.get("orders"), list) else []
    if not orders:
        raise ValueError("StrategyPlan has no executable grid orders")

    commands: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for order in orders:
        if not isinstance(order, dict):
            raise ValueError("StrategyPlan grid order must be an object")
        preview_order_id = _required_text(order.get("preview_order_id"), "grid preview_order_id")
        if preview_order_id in seen_ids:
            raise ValueError("StrategyPlan contains duplicate grid order identity")
        seen_ids.add(preview_order_id)
        side = str(order.get("side") or "").lower()
        if side not in {"buy", "sell"}:
            raise ValueError("StrategyPlan grid order side must be buy or sell")
        commands.append({
            "cycle_id": cycle_id,
            "ts": submitted_at,
            "symbol": symbol,
            "side": side,
            "event": "entry",
            "order_type": "limit",
            "price": _positive_number(order.get("price"), "grid order price"),
            "market_price": market_price,
            "quantity": _positive_number(order.get("quantity"), "grid order quantity"),
            "notional": _positive_number(order.get("notional"), "grid order notional"),
            "sl": _positive_number(order.get("sl"), "grid order sl"),
            "tp": _positive_number(order.get("tp"), "grid order tp"),
            "source": "strategy_production_console",
            "source_fill_id": f"strategy-grid:{plan_id}:{preview_order_id}",
            "strategy_plan_id": plan_id,
            "strategy_plan_version": plan_version,
        })
    return commands


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
    if parsed <= 0:
        raise ValueError(f"{label} must be positive")
    return parsed


def _positive_integer(value: Any, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be positive") from exc
    if parsed <= 0:
        raise ValueError(f"{label} must be positive")
    return parsed
