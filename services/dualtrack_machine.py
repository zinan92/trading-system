from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from schemas.market_data import Bar
from services.config_loader import ROOT, load_pipeline_config
from services.dualtrack_clock import cycle_window_from_id, parse_utc
from services.dualtrack_config import base_rung_notional, dualtrack_config
from services.dualtrack_grid_core import GridStop, simulate_conditional_grid
from services.dualtrack_store import DualTrackPlanStore
from services.journal_store import load_json, write_json


class DualTrackMachineRunner:
    def __init__(self, output_root: Path | None = None, *, config: dict[str, Any] | None = None) -> None:
        pipeline_config = load_pipeline_config()
        self.output_root = Path(output_root) if output_root else ROOT / pipeline_config.get("output_root", "outputs")
        self.root = self.output_root / "dualtrack"
        self.config = config or dualtrack_config()
        self.store = DualTrackPlanStore(self.output_root, config=self.config)

    def run_effective_plan(
        self,
        cycle_id: str,
        bars: Iterable[Bar],
        *,
        prev_range: float,
        as_of: str | datetime | None = None,
        trend_gate_armed: bool | None = None,
    ) -> dict[str, Any]:
        plan = self.store.effective_plan(cycle_id, as_of=as_of)
        return self.run_plan(cycle_id, plan, bars, prev_range=prev_range, trend_gate_armed=trend_gate_armed)

    def run_plan(
        self,
        cycle_id: str,
        plan: dict[str, Any] | None,
        bars: Iterable[Bar],
        *,
        prev_range: float,
        trend_gate_armed: bool | None = None,
    ) -> dict[str, Any]:
        rows = tuple(bars)
        if not rows:
            raise ValueError("bars are required")
        gate_armed = self._resolved_trend_gate_armed(cycle_id, trend_gate_armed)
        if plan is None:
            return self._stand_down(cycle_id, rows, reason="no_effective_plan", trend_gate_armed=gate_armed)
        direction = _direction_to_int(plan.get("direction"))
        if direction == 0:
            return self._stand_down(
                cycle_id,
                rows,
                reason="flat_plan",
                effective_plan_author=plan.get("effective_author") or plan.get("author"),
                trend_gate_armed=gate_armed,
            )

        existing_cycle = self._existing_cycle_state(cycle_id)
        grid = self.config["grid"]
        stop = _hard_stop(plan, direction)
        base = simulate_conditional_grid(
            cycle_id=cycle_id,
            bars=rows,
            direction=direction,
            prev_range=prev_range,
            spacing_bp=float(grid["spacing_bp"]),
            range_k=float(grid["range_k"]),
            rung_notional=base_rung_notional(self.config),
            max_rungs=10,
            cost_per_side_bp=float(self.config["cost_per_side_bp"]),
            tp_mult=float(grid["tp_mult_base"]),
            re_arm_max=int(grid["re_arm_max"]),
            budget_sizing=False,
            layer="grid",
            stop=stop,
        )
        fills = [self._annotate_fill(fill) for fill in base.fills]
        trend = None
        if gate_armed:
            trend_budget_pct = float(grid["trend_leg_budget_pct"]) / 100.0
            trend = simulate_conditional_grid(
                cycle_id=cycle_id,
                bars=rows,
                direction=direction,
                prev_range=prev_range,
                spacing_bp=float(grid["spacing_bp"]),
                range_k=float(grid["range_k"]),
                rung_notional=base_rung_notional(self.config) * trend_budget_pct,
                max_rungs=10,
                cost_per_side_bp=float(self.config["cost_per_side_bp"]),
                tp_mult=float(grid["tp_mult_trend"]),
                re_arm_max=int(grid["re_arm_max"]),
                budget_sizing=False,
                layer="trend",
                stop=stop,
            )
            fills.extend(self._annotate_fill(fill) for fill in trend.fills)
        # Intraday/auto recomputes must replace the per-cycle machine fills, never append.
        write_json(self._fills_path(cycle_id), fills)
        self._write_account(cycle_id, "machine", fills)
        state = self._cycle_state(
            cycle_id,
            rows,
            fills,
            machine_stood_down=False,
            effective_plan_author=str(plan.get("effective_author") or plan.get("author") or ""),
            layers=[
                f"grid:{'traded' if base.traded else 'armed_no_fill'}",
                f"trend:{'armed' if gate_armed else 'standby'}",
            ],
            trend_gate_armed=gate_armed,
            stop_hit=base.stop_hit or bool(trend and trend.stop_hit),
            rearms=base.rearms + (trend.rearms if trend else 0),
        )
        _preserve_gate_snapshot(state, existing_cycle)
        write_json(self._cycle_path(cycle_id), [state])
        return state

    def machine_payload(self, cycle_id: str, *, as_of: str | datetime | None = None) -> dict[str, Any]:
        now = parse_utc(as_of)
        window = cycle_window_from_id(cycle_id)
        fills = load_json(self._fills_path(cycle_id))
        state_rows = load_json(self._cycle_path(cycle_id))
        state = state_rows[-1] if state_rows else {}
        visible_fills = [fill for fill in fills if parse_utc(fill["ts"]) <= now]
        realized = round(sum(float(fill.get("realized_pnl", 0.0)) for fill in visible_fills), 8)
        if now < window.end:
            return {
                "realized_pnl": realized,
                "unrealized_pnl": 0.0,
                "layers": list(state.get("layers") or ["grid:pending", "trend:pending"]),
            }
        return {
            "cycle_id": cycle_id,
            "status": "closed",
            "realized_pnl": round(sum(float(fill.get("realized_pnl", 0.0)) for fill in fills), 8),
            "unrealized_pnl": 0.0,
            "layers": list(state.get("layers") or []),
            "fills": fills,
            "machine_stood_down": bool(state.get("machine_stood_down", False)),
            "effective_plan_author": state.get("effective_plan_author", ""),
        }

    def _stand_down(
        self,
        cycle_id: str,
        bars: tuple[Bar, ...],
        *,
        reason: str,
        effective_plan_author: str = "",
        trend_gate_armed: bool = False,
    ) -> dict[str, Any]:
        existing_cycle = self._existing_cycle_state(cycle_id)
        write_json(self._fills_path(cycle_id), [])
        self._write_account(cycle_id, "machine", [])
        state = self._cycle_state(
            cycle_id,
            bars,
            [],
            machine_stood_down=True,
            effective_plan_author=effective_plan_author,
            layers=[f"grid:stand_down:{reason}", f"trend:stand_down:{reason}"],
            trend_gate_armed=trend_gate_armed,
            stop_hit=False,
            rearms=0,
        )
        _preserve_gate_snapshot(state, existing_cycle)
        write_json(self._cycle_path(cycle_id), [state])
        return state

    def _cycle_state(
        self,
        cycle_id: str,
        bars: tuple[Bar, ...],
        fills: list[dict[str, Any]],
        *,
        machine_stood_down: bool,
        effective_plan_author: str,
        layers: list[str],
        trend_gate_armed: bool,
        stop_hit: bool,
        rearms: int,
    ) -> dict[str, Any]:
        open_price = float(bars[0].open)
        close_price = float(bars[-1].close)
        return {
            "cycle_id": cycle_id,
            "kind": cycle_id.rsplit("_", 1)[-1],
            "start": bars[0].timestamp,
            "end": bars[-1].timestamp,
            "open_price": open_price,
            "close_price": close_price,
            "realized_direction": _realized_direction(open_price, close_price),
            "effective_plan_author": effective_plan_author,
            "machine_stood_down": machine_stood_down,
            "opportunity_count": 0,
            "machine_captured": 0,
            "human_captured": 0,
            "machine_realized_pnl": round(sum(float(fill.get("realized_pnl", 0.0)) for fill in fills), 8),
            "layers": layers,
            "trend_gate_armed": trend_gate_armed,
            "stop_hit": stop_hit,
            "rearms": rearms,
        }

    def _trend_gate_armed(self) -> bool:
        rows = load_json(self.root / "scoreboard.json")
        board = rows[-1] if rows else {}
        gate = board.get("trend_leg_gate") if isinstance(board.get("trend_leg_gate"), dict) else {}
        return bool(gate.get("armed", False))

    def frozen_trend_gate_armed(self, cycle_id: str) -> bool | None:
        state = self._existing_cycle_state(cycle_id)
        if "trend_gate_armed" not in state:
            return None
        return bool(state["trend_gate_armed"])

    def _resolved_trend_gate_armed(self, cycle_id: str, trend_gate_armed: bool | None) -> bool:
        if trend_gate_armed is not None:
            return bool(trend_gate_armed)
        frozen = self.frozen_trend_gate_armed(cycle_id)
        if frozen is not None:
            return frozen
        return self._trend_gate_armed()

    def _existing_cycle_state(self, cycle_id: str) -> dict[str, Any]:
        rows = load_json(self._cycle_path(cycle_id))
        return rows[-1] if rows else {}

    def _fills_path(self, cycle_id: str) -> Path:
        return self.root / "fills" / f"{cycle_id}_machine.json"

    def _cycle_path(self, cycle_id: str) -> Path:
        return self.root / "cycles" / f"{cycle_id}.json"

    def _account_path(self, cycle_id: str, track: str) -> Path:
        return self.root / "accounts" / f"{cycle_id}_{track}.json"

    def _annotate_fill(self, fill: dict[str, Any]) -> dict[str, Any]:
        return {
            **fill,
            "track": "machine",
            "cost_model": {"cost_per_side_bp": float(self.config["cost_per_side_bp"])},
        }

    def _write_account(self, cycle_id: str, track: str, fills: list[dict[str, Any]]) -> None:
        starting = float(self.config["capital_per_track_usd"])
        realized = sum(float(fill.get("realized_pnl", 0.0)) for fill in fills)
        write_json(self._account_path(cycle_id, track), [{
            "cycle_id": cycle_id,
            "track": track,
            "starting_cash": starting,
            "realized_pnl": round(realized, 8),
            "ending_cash": round(starting + realized, 8),
            "cost_model": {"cost_per_side_bp": float(self.config["cost_per_side_bp"])},
        }])


def _hard_stop(plan: dict[str, Any], direction: int) -> GridStop | None:
    wanted = "below" if direction > 0 else "above"
    for row in plan.get("invalidation") or []:
        if row.get("side") == wanted:
            return GridStop(side=wanted, price=float(row["price"]), confirm=str(row.get("confirm") or "touch"))
    return None


def _direction_to_int(direction: Any) -> int:
    if str(direction).lower() == "long":
        return 1
    if str(direction).lower() == "short":
        return -1
    return 0


def _realized_direction(open_price: float, close_price: float) -> str:
    if close_price > open_price:
        return "long"
    if close_price < open_price:
        return "short"
    return "flat"


def _preserve_gate_snapshot(state: dict[str, Any], existing: dict[str, Any]) -> None:
    for key in ("trend_gate_frozen_at", "trend_gate_source"):
        if existing.get(key):
            state[key] = existing[key]
