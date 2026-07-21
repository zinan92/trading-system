"""Deterministic strategy counterfactuals, distinct from execution shadows."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from services.journal_store import write_json


class StrategyShadowRunner:
    """Replay one frozen plan against chronologically supplied market events only."""

    def __init__(self, output_root: Path) -> None:
        self.output_root = Path(output_root)

    def run(self, *, cycle_id: str, variant_id: str, plan: dict[str, Any], market_events: list[dict[str, Any]]) -> dict[str, Any]:
        events = _events(market_events)
        if not events:
            raise ValueError("strategy shadow requires chronological market events")
        input_hash = _hash({"cycle_id": cycle_id, "variant_id": variant_id, "plan": plan, "events": events})
        grid_orders = _grid_orders(plan)
        direction = str(plan.get("direction") or "neutral")
        tp_sl = dict(plan.get("tp_sl") or {})
        tp, sl = _number(tp_sl.get("tp")), _number(tp_sl.get("sl"))
        orders: list[dict[str, Any]] = []
        fills: list[dict[str, Any]] = []
        open_positions: list[dict[str, Any]] = []
        for event in events:
            # The decision can see this event, never any event that follows it.
            # Existing positions are evaluated before new entries. A position
            # opened inside this candle never gets an invented same-candle exit.
            survivors = []
            for position in open_positions:
                exit_price, reason = _protective_exit(
                    position,
                    event,
                    tp=_number(position.get("tp")) if position.get("tp") is not None else tp,
                    sl=_number(position.get("sl")) if position.get("sl") is not None else sl,
                )
                if exit_price is None:
                    survivors.append(position)
                    continue
                pnl = (exit_price - position["entry_price"]) * (1 if position["side"] == "buy" else -1) * position["quantity"]
                fills.append({"fill_id": f"{position['position_id']}-exit", "event": reason, "side": "sell" if position["side"] == "buy" else "buy", "price": exit_price, "quantity": position["quantity"], "ts": event["timestamp"], "realized_pnl": pnl, "position_id": position["position_id"]})
            open_positions = survivors
            for template in grid_orders:
                level = template["price"]
                touched = event["low"] <= level <= event["high"]
                if not touched or any(p["template_id"] == template["template_id"] for p in open_positions):
                    continue
                side = template["side"]
                if direction == "long" and side != "buy" or direction == "short" and side != "sell":
                    continue
                if direction not in {"neutral", "long", "short"}:
                    continue
                position = {
                    "position_id": f"{variant_id}-{len(fills)+1}",
                    "template_id": template["template_id"],
                    "side": side,
                    "entry_price": level,
                    "entry_ts": event["timestamp"],
                    "quantity": template["quantity"],
                    "tp": template.get("tp"),
                    "sl": template.get("sl"),
                }
                orders.append({"order_id": position["position_id"], "state": "filled", "side": side, "price": level, "quantity": position["quantity"], "ts": event["timestamp"]})
                fills.append({"fill_id": position["position_id"], "event": "entry", "side": side, "price": level, "quantity": position["quantity"], "ts": event["timestamp"]})
                open_positions.append(position)
        mark = events[-1]["close"]
        realized = sum(float(row.get("realized_pnl") or 0.0) for row in fills)
        unrealized = sum((mark - row["entry_price"]) * (1 if row["side"] == "buy" else -1) * row["quantity"] for row in open_positions)
        closed = [row for row in fills if row.get("event") != "entry"]
        metrics = {"net_pnl": round(realized + unrealized, 8), "realized_pnl": round(realized, 8), "unrealized_pnl": round(unrealized, 8), "max_drawdown": _max_drawdown(fills), "win_rate": (sum(1 for row in closed if row.get("realized_pnl", 0) > 0) / len(closed)) if closed else 0.0, "average_r": 0.0, "trade_count": len(closed), "cost": 0.0}
        payload = {"schema_version": "strategy-shadow-run-v1", "cycle_id": cycle_id, "variant_id": variant_id, "input_hash": input_hash, "plan": plan, "orders": orders, "fills": fills, "positions": open_positions, "pnl": {"realized": realized, "unrealized": unrealized}, "metrics": metrics, "review": {"future_function": False, "market_event_count": len(events), "input_hash": input_hash}, "safety": {"execution_shadow": False, "writes_production_ledger": False, "real_orders": False}}
        write_json(self.output_root / "dualtrack" / "strategy_shadows" / f"{cycle_id}_{variant_id}.json", [payload])
        return payload


def _events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        try:
            item = {"timestamp": str(row["timestamp"]), **{key: float(row[key]) for key in ("open", "high", "low", "close")}}
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid strategy shadow market event") from exc
        result.append(item)
    if result != sorted(result, key=lambda item: item["timestamp"]):
        raise ValueError("strategy shadow events must be chronological")
    return result


def _grid_levels(plan: dict[str, Any]) -> list[float]:
    area = dict(plan.get("range") or {})
    low, high = _number(area.get("low")), _number(area.get("high"))
    count = int(dict(plan.get("grid") or {}).get("count") or 0)
    if low is None or high is None or high <= low or count < 1:
        return []
    return [round(low + ((high - low) * index / count), 8) for index in range(count + 1)]


def _grid_orders(plan: dict[str, Any]) -> list[dict[str, Any]]:
    grid = dict(plan.get("grid") or {})
    configured = [row for row in grid.get("orders") or [] if isinstance(row, dict)]
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(configured):
        price = _number(row.get("price", row.get("entry")))
        side = str(row.get("side") or "").lower()
        side = "buy" if side in {"buy", "long"} else "sell" if side in {"sell", "short"} else ""
        if price is None or not side:
            continue
        quantity = _number(row.get("quantity"))
        if quantity is None:
            notional = _number(row.get("notional", grid.get("notional_per_grid")))
            quantity = (notional / price) if notional is not None and price > 0 else 1.0
        rows.append({
            "template_id": str(row.get("preview_order_id") or row.get("order_id") or f"grid-{index}"),
            "side": side,
            "price": price,
            "quantity": quantity,
            "tp": _number(row.get("tp", row.get("take_profit"))),
            "sl": _number(row.get("sl", row.get("stop_loss"))),
        })
    if rows:
        return rows
    direction = str(plan.get("direction") or "neutral")
    levels = _grid_levels(plan)
    midpoint = (levels[0] + levels[-1]) / 2 if levels else 0.0
    notional = _number(grid.get("notional_per_grid"))
    return [
        {
            "template_id": f"grid-{index}",
            "side": "buy" if direction == "long" or (direction == "neutral" and level < midpoint) else "sell",
            "price": level,
            "quantity": (notional / level) if notional is not None and level > 0 else 1.0,
            "tp": None,
            "sl": None,
        }
        for index, level in enumerate(levels)
        if direction in {"neutral", "long", "short"}
    ]


def _protective_exit(position: dict[str, Any], event: dict[str, Any], *, tp: float | None, sl: float | None) -> tuple[float | None, str]:
    # If one candle crosses both levels, take the adverse leg. It is deterministic
    # and avoids inventing an intrabar path from future information.
    if position["side"] == "buy":
        if sl is not None and event["low"] <= sl:
            return sl, "stop"
        if tp is not None and event["high"] >= tp:
            return tp, "target"
    else:
        if sl is not None and event["high"] >= sl:
            return sl, "stop"
        if tp is not None and event["low"] <= tp:
            return tp, "target"
    return None, ""


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _max_drawdown(fills: list[dict[str, Any]]) -> float:
    equity = peak = 0.0
    max_dd = 0.0
    for fill in fills:
        equity += float(fill.get("realized_pnl") or 0.0)
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return round(max_dd, 8)
