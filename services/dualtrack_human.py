from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from services.config_loader import ROOT, load_pipeline_config
from services.dualtrack_clock import cycle_window, parse_utc
from services.dualtrack_config import dualtrack_config
from services.dualtrack_store import DualTrackPlanStore
from services.journal_store import load_json, write_json

ORDER_SIDES = {"buy", "sell"}
ORDER_TYPES = {"market", "limit"}


class DualTrackHumanEngine:
    def __init__(self, output_root: Path | None = None, *, config: dict[str, Any] | None = None) -> None:
        pipeline_config = load_pipeline_config()
        self.output_root = Path(output_root) if output_root else ROOT / pipeline_config.get("output_root", "outputs")
        self.root = self.output_root / "dualtrack"
        self.config = config or dualtrack_config()
        self.store = DualTrackPlanStore(self.output_root, config=self.config)

    def submit_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        ts = parse_utc(payload.get("ts"))
        cycle_id = str(payload.get("cycle_id") or cycle_window(ts).cycle_id)
        side = str(payload.get("side") or "").lower()
        order_type = str(payload.get("order_type") or "market").lower()
        if side not in ORDER_SIDES:
            raise ValueError("side must be buy or sell")
        if order_type not in ORDER_TYPES:
            raise ValueError("order_type must be market or limit")
        price = _required_float(payload.get("price") or payload.get("market_price"), "price")
        notional = _required_float(payload.get("notional"), "notional")
        if notional <= 0:
            raise ValueError("notional must be positive")
        plan = self.store.load_plan(cycle_id, "human")
        fill = {
            "fill_id": f"{cycle_id}_human_{len(load_json(self._fills_path(cycle_id))) + 1:04d}",
            "ts": ts.isoformat(),
            "side": side,
            "price": price,
            "notional": notional,
            "sl": _optional_float(payload.get("sl")),
            "tp": _optional_float(payload.get("tp")),
            "layer": "manual",
            "order_type": order_type,
            "out_of_plan": _out_of_plan(plan, side=side, price=price),
            "realized_pnl": -_cost(notional, float(self.config["cost_per_side_bp"])),
            "track": "human",
            "cost_model": {"cost_per_side_bp": float(self.config["cost_per_side_bp"])},
        }
        rows = load_json(self._fills_path(cycle_id))
        rows.append(fill)
        write_json(self._fills_path(cycle_id), rows)
        self._write_account(cycle_id, rows)
        return fill

    def human_payload(self, cycle_id: str) -> dict[str, Any]:
        fills = load_json(self._fills_path(cycle_id))
        account = load_json(self._account_path(cycle_id))
        return {
            "cycle_id": cycle_id,
            "track": "human",
            "fills": fills,
            "account": account[-1] if account else {},
            "realized_pnl": round(sum(float(fill.get("realized_pnl", 0.0)) for fill in fills), 8),
        }

    def _fills_path(self, cycle_id: str) -> Path:
        return self.root / "fills" / f"{cycle_id}_human.json"

    def _account_path(self, cycle_id: str) -> Path:
        return self.root / "accounts" / f"{cycle_id}_human.json"

    def _write_account(self, cycle_id: str, fills: list[dict[str, Any]]) -> None:
        starting = float(self.config["capital_per_track_usd"])
        realized = sum(float(fill.get("realized_pnl", 0.0)) for fill in fills)
        write_json(self._account_path(cycle_id), [{
            "cycle_id": cycle_id,
            "track": "human",
            "starting_cash": starting,
            "realized_pnl": round(realized, 8),
            "ending_cash": round(starting + realized, 8),
            "cost_model": {"cost_per_side_bp": float(self.config["cost_per_side_bp"])},
        }])


def _out_of_plan(plan: dict[str, Any] | None, *, side: str, price: float) -> bool:
    if not plan:
        return True
    bounds = plan.get("range") or {}
    low = bounds.get("low")
    high = bounds.get("high")
    if low is not None and price < float(low):
        return True
    if high is not None and price > float(high):
        return True
    direction = str(plan.get("direction") or "").lower()
    if direction == "long":
        return side == "sell"
    if direction == "short":
        return side == "buy"
    return True


def _required_float(value: Any, field: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a number") from exc


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return _required_float(value, "optional price")


def _cost(notional: float, cost_per_side_bp: float) -> float:
    return float(notional) * float(cost_per_side_bp) / 10_000.0
