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
        grid = _grid_levels(plan)
        direction = str(plan.get("direction") or "neutral")
        tp_sl = dict(plan.get("tp_sl") or {})
        tp, sl = _number(tp_sl.get("tp")), _number(tp_sl.get("sl"))
        orders: list[dict[str, Any]] = []
        fills: list[dict[str, Any]] = []
        open_positions: list[dict[str, Any]] = []
        for event in events:
            # The decision can see this event, never any event that follows it.
            for level in grid:
                touched = event["low"] <= level <= event["high"]
                if direction not in {"long", "short"} or not touched or any(p["entry_price"] == level for p in open_positions):
                    continue
                side = "buy" if direction == "long" else "sell"
                position = {"position_id": f"{variant_id}-{len(fills)+1}", "side": side, "entry_price": level, "entry_ts": event["timestamp"], "quantity": 1.0}
                orders.append({"order_id": position["position_id"], "state": "filled", "side": side, "price": level, "ts": event["timestamp"]})
                fills.append({"fill_id": position["position_id"], "event": "entry", "side": side, "price": level, "ts": event["timestamp"]})
                open_positions.append(position)
            survivors = []
            for position in open_positions:
                exit_price, reason = _protective_exit(position, event, tp=tp, sl=sl)
                if exit_price is None:
                    survivors.append(position)
                    continue
                pnl = (exit_price - position["entry_price"]) * (1 if position["side"] == "buy" else -1)
                fills.append({"fill_id": f"{position['position_id']}-exit", "event": reason, "side": "sell" if position["side"] == "buy" else "buy", "price": exit_price, "ts": event["timestamp"], "realized_pnl": pnl, "position_id": position["position_id"]})
            open_positions = survivors
        mark = events[-1]["close"]
        realized = sum(float(row.get("realized_pnl") or 0.0) for row in fills)
        unrealized = sum((mark - row["entry_price"]) * (1 if row["side"] == "buy" else -1) for row in open_positions)
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
