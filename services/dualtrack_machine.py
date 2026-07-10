from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from schemas.market_data import Bar
from services.config_loader import ROOT, load_pipeline_config, load_risk_rules
from services.dualtrack_clock import cycle_window_from_id, parse_utc
from services.dualtrack_config import base_rung_notional, dualtrack_config, track_notional_budget
from services.dualtrack_costs import dualtrack_cost_descriptor, dualtrack_order_cost
from services.dualtrack_grid_core import GridStop, simulate_conditional_grid, simulate_explicit_grid
from services.dualtrack_scoring import filter_invalid_machine_fills
from services.dualtrack_store import DualTrackPlanStore
from services.journal_store import load_json, write_json


class DualTrackMachineRunner:
    def __init__(self, output_root: Path | None = None, *, config: dict[str, Any] | None = None) -> None:
        pipeline_config = load_pipeline_config()
        self.output_root = Path(output_root) if output_root else ROOT / pipeline_config.get("output_root", "outputs")
        self.root = self.output_root / "dualtrack"
        self.config = config or dualtrack_config()
        self.cost_rules = load_risk_rules().get("default", {}).get("paper_execution_costs", {})
        self.store = DualTrackPlanStore(self.output_root, config=self.config)

    def run_effective_plan(
        self,
        cycle_id: str,
        bars: Iterable[Bar],
        *,
        prev_range: float,
        as_of: str | datetime | None = None,
        trend_gate_armed: bool | None = None,
        finalize: bool = True,
    ) -> dict[str, Any]:
        plan = self.store.machine_plan(cycle_id)
        return self.run_plan(
            cycle_id,
            plan,
            bars,
            prev_range=prev_range,
            trend_gate_armed=trend_gate_armed,
            finalize=finalize,
        )

    def run_plan(
        self,
        cycle_id: str,
        plan: dict[str, Any] | None,
        bars: Iterable[Bar],
        *,
        prev_range: float,
        trend_gate_armed: bool | None = None,
        finalize: bool = True,
    ) -> dict[str, Any]:
        rows = tuple(bars)
        if not rows:
            raise ValueError("bars are required")
        gate_armed = self._resolved_trend_gate_armed(cycle_id, trend_gate_armed)
        if plan is None:
            return self._stand_down(cycle_id, rows, reason="no_effective_plan", trend_gate_armed=gate_armed)
        direction = _direction_to_int(plan.get("direction"))
        if direction == 0:
            return self._neutral_decision(
                cycle_id,
                rows,
                reason="decision_error" if plan.get("degraded") else "neutral_decision",
                effective_plan_author=plan.get("effective_author") or plan.get("author"),
                trend_gate_armed=gate_armed,
                stood_down=bool(plan.get("degraded")),
            )

        existing_cycle = self._existing_cycle_state(cycle_id)
        explicit_orders = plan.get("grid_orders") if isinstance(plan.get("grid_orders"), list) else []
        if explicit_orders:
            stop = _hard_stop(plan, direction)
            if stop is None:
                return self._stand_down(
                    cycle_id,
                    rows,
                    reason="machine_plan_stop_missing",
                    effective_plan_author="ai",
                    trend_gate_armed=False,
                )
            result = simulate_explicit_grid(
                cycle_id=cycle_id,
                bars=rows,
                direction=direction,
                orders=explicit_orders,
                stop=stop,
                rung_notional=track_notional_budget(self.config),
                cost_per_side_bp=float(self.config["cost_per_side_bp"]),
                finalize=finalize,
                layer="ai_grid",
                **self._grid_cost_kwargs(),
            )
            fills = [self._annotate_fill(fill) for fill in result.fills]
            fills, fill_quality = filter_invalid_machine_fills(fills)
            open_inventory = any(fill.get("event") == "entry" and fill.get("position_status") == "open" for fill in fills)
            grid_status = "open" if open_inventory else "traded" if result.traded else "armed_no_fill"
            layers = [
                "decision:ai_independent",
                f"grid:ai_levels_{grid_status}",
                *([f"invalid_fills:{fill_quality['invalid_machine_fill_count']}"] if fill_quality.get("invalid_machine_fill_count") else []),
            ]
            write_json(self._fills_path(cycle_id), fills)
            self._write_account(cycle_id, "machine", fills)
            state = self._cycle_state(
                cycle_id,
                rows,
                fills,
                machine_stood_down=False,
                effective_plan_author="ai",
                layers=layers,
                trend_gate_armed=False,
                stop_hit=result.stop_hit,
                rearms=0,
            )
            _preserve_gate_snapshot(state, existing_cycle)
            write_json(self._cycle_path(cycle_id), [state])
            return state
        bracket = _plan_bracket(plan)
        if bracket:
            fills, layers, stop_hit = self._simulate_bracket(cycle_id, plan, bracket, rows, finalize=finalize)
            fills, fill_quality = filter_invalid_machine_fills(fills)
            if fill_quality.get("invalid_machine_fill_count"):
                layers = [*layers, f"invalid_fills:{fill_quality['invalid_machine_fill_count']}"]
                stop_hit = False
            write_json(self._fills_path(cycle_id), fills)
            self._write_account(cycle_id, "machine", fills)
            state = self._cycle_state(
                cycle_id,
                rows,
                fills,
                machine_stood_down=False,
                effective_plan_author=str(plan.get("effective_author") or plan.get("author") or ""),
                layers=layers,
                trend_gate_armed=gate_armed,
                stop_hit=stop_hit,
                rearms=0,
            )
            _preserve_gate_snapshot(state, existing_cycle)
            write_json(self._cycle_path(cycle_id), [state])
            return state
        grid = self.config["grid"]
        stop = _hard_stop(plan, direction)
        max_rungs = self._grid_max_rungs()
        grid_cost_kwargs = self._grid_cost_kwargs()
        base = simulate_conditional_grid(
            cycle_id=cycle_id,
            bars=rows,
            direction=direction,
            prev_range=prev_range,
            spacing_bp=float(grid["spacing_bp"]),
            range_k=float(grid["range_k"]),
            rung_notional=base_rung_notional(self.config, max_rungs=max_rungs),
            max_rungs=max_rungs,
            cost_per_side_bp=float(self.config["cost_per_side_bp"]),
            tp_mult=float(grid["tp_mult_base"]),
            re_arm_max=int(grid["re_arm_max"]),
            budget_sizing=False,
            layer="grid",
            stop=stop,
            **grid_cost_kwargs,
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
                rung_notional=base_rung_notional(self.config, max_rungs=max_rungs) * trend_budget_pct,
                max_rungs=max_rungs,
                cost_per_side_bp=float(self.config["cost_per_side_bp"]),
                tp_mult=float(grid["tp_mult_trend"]),
                re_arm_max=int(grid["re_arm_max"]),
                budget_sizing=False,
                layer="trend",
                stop=stop,
                **grid_cost_kwargs,
            )
            fills.extend(self._annotate_fill(fill) for fill in trend.fills)
        fills, fill_quality = filter_invalid_machine_fills(fills)
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
                *([f"invalid_fills:{fill_quality['invalid_machine_fill_count']}"] if fill_quality.get("invalid_machine_fill_count") else []),
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

    def _neutral_decision(
        self,
        cycle_id: str,
        bars: tuple[Bar, ...],
        *,
        reason: str,
        effective_plan_author: str,
        trend_gate_armed: bool,
        stood_down: bool,
    ) -> dict[str, Any]:
        existing_cycle = self._existing_cycle_state(cycle_id)
        write_json(self._fills_path(cycle_id), [])
        self._write_account(cycle_id, "machine", [])
        state = self._cycle_state(
            cycle_id,
            bars,
            [],
            machine_stood_down=stood_down,
            effective_plan_author=effective_plan_author,
            layers=[f"decision:{reason}", "grid:neutral_no_orders"],
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
            "cost_model": fill.get("cost_model") or self._cost_model_descriptor(),
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
            "cost_model": self._cost_model_descriptor(),
        }])

    def _grid_max_rungs(self) -> int:
        return int(self.config.get("grid", {}).get("max_rungs", 10))

    def _grid_cost_kwargs(self) -> dict[str, Any]:
        model = self.config.get("execution_cost_model")
        if not isinstance(model, dict) or not model.get("venue"):
            return {}
        return {"execution_cost_model": model, "cost_rules": self.cost_rules}

    def _cost_model_descriptor(self) -> dict[str, Any]:
        return dualtrack_cost_descriptor(self.config, self.cost_rules)

    def _bracket_notional(self, bracket: dict[str, Any]) -> float | None:
        value = _optional_float(bracket.get("notional"))
        if value is not None:
            return value
        model = self.config.get("execution_cost_model")
        if isinstance(model, dict) and model.get("venue"):
            return None
        return base_rung_notional(self.config, max_rungs=self._grid_max_rungs())

    def _simulate_bracket(
        self,
        cycle_id: str,
        plan: dict[str, Any],
        bracket: dict[str, Any],
        rows: tuple[Bar, ...],
        *,
        finalize: bool,
    ) -> tuple[list[dict[str, Any]], list[str], bool]:
        direction = _direction_to_int(plan.get("direction"))
        entry_price = float(bracket["entry"])
        take_profit = float(bracket["take_profit"])
        stop_loss = float(bracket["stop_loss"])
        entry_index = _first_touch_index(rows, entry_price)
        if entry_index is None:
            return [], ["bracket:armed_no_fill"], False

        entry_cost = dualtrack_order_cost(
            config=self.config,
            price=entry_price,
            notional=self._bracket_notional(bracket),
            contracts=_optional_float(bracket.get("contracts")),
            cost_rules=self.cost_rules,
            require_contracts=False,
        )
        entry_fill = _bracket_fill(
            cycle_id,
            rows[entry_index],
            side="buy" if direction > 0 else "sell",
            price=entry_price,
            event="entry",
            order_cost=entry_cost,
            realized_pnl=-entry_cost.cost,
            sl=stop_loss,
            tp=take_profit,
        )
        fills = [self._annotate_fill(entry_fill)]
        exit_event = ""
        exit_price = 0.0
        exit_bar = rows[-1]
        for bar in rows[entry_index:]:
            stop_hit = _stop_touched(bar, direction=direction, stop_loss=stop_loss)
            target_hit = _target_touched(bar, direction=direction, take_profit=take_profit)
            if not stop_hit and not target_hit:
                continue
            if stop_hit and target_hit and str(bracket.get("same_bar_priority") or "stop") != "target":
                exit_event, exit_price = "stop", stop_loss
            elif target_hit:
                exit_event, exit_price = "target", take_profit
            else:
                exit_event, exit_price = "stop", stop_loss
            exit_bar = bar
            break
        if not exit_event and not finalize:
            return fills, ["bracket:open"], False
        if not exit_event:
            exit_event = "flatten"
            exit_price = float(rows[-1].close)
        exit_cost = dualtrack_order_cost(
            config=self.config,
            price=exit_price,
            notional=_exit_notional(entry_cost, entry_price, exit_price),
            contracts=entry_cost.contracts,
            cost_rules=self.cost_rules,
            require_contracts=False,
        )
        gross_pnl = direction * (exit_price - entry_price) * _cost_units(entry_cost, entry_price)
        exit_fill = _bracket_fill(
            cycle_id,
            exit_bar,
            side="sell" if direction > 0 else "buy",
            price=exit_price,
            event=exit_event,
            order_cost=exit_cost,
            realized_pnl=gross_pnl - exit_cost.cost,
            sl=stop_loss,
            tp=take_profit if exit_event != "stop" else None,
            gross_pnl=gross_pnl,
        )
        exit_fill["matched_entries"] = [{
            "fill_id": entry_fill["fill_id"],
            "trade_id": entry_fill["trade_id"],
            "units": _cost_units(entry_cost, entry_price),
            "entry_price": entry_price,
            "gross_pnl": round(gross_pnl, 8),
            "realized_pnl": round(gross_pnl - exit_cost.cost, 8),
        }]
        fills[0]["remaining_units"] = 0.0
        fills[0]["closed_units"] = round(_cost_units(entry_cost, entry_price), 10)
        fills[0]["position_status"] = "closed"
        fills.append(self._annotate_fill(exit_fill))
        return fills, [f"bracket:{exit_event}"], exit_event == "stop"


def _hard_stop(plan: dict[str, Any], direction: int) -> GridStop | None:
    wanted = "below" if direction > 0 else "above"
    for row in plan.get("invalidation") or []:
        if row.get("side") == wanted:
            return GridStop(side=wanted, price=float(row["price"]), confirm=str(row.get("confirm") or "touch"))
    return None


def _plan_bracket(plan: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(plan, dict):
        return None
    bracket = plan.get("bracket")
    return bracket if isinstance(bracket, dict) else None


def _first_touch_index(rows: tuple[Bar, ...], price: float) -> int | None:
    for index, bar in enumerate(rows):
        if float(bar.low) <= float(price) <= float(bar.high):
            return index
    return None


def _stop_touched(bar: Bar, *, direction: int, stop_loss: float) -> bool:
    return float(bar.low) <= stop_loss if direction > 0 else float(bar.high) >= stop_loss


def _target_touched(bar: Bar, *, direction: int, take_profit: float) -> bool:
    return float(bar.high) >= take_profit if direction > 0 else float(bar.low) <= take_profit


def _bracket_fill(
    cycle_id: str,
    bar: Bar,
    *,
    side: str,
    price: float,
    event: str,
    order_cost,
    realized_pnl: float,
    sl: float | None,
    tp: float | None,
    gross_pnl: float = 0.0,
) -> dict[str, Any]:
    suffix = "0001" if event == "entry" else "0002"
    trade_id = f"{cycle_id}_ai_bracket_trade_0001"
    return {
        "fill_id": f"{cycle_id}_bracket_{suffix}",
        "trade_id": trade_id,
        "ts": bar.timestamp,
        "side": side,
        "price": float(price),
        "sl": sl,
        "tp": tp,
        "layer": "bracket",
        "event": event,
        "rung": 0,
        "order_type": "limit" if event == "entry" else "market",
        "out_of_plan": False,
        "gross_pnl": round(float(gross_pnl), 8),
        "realized_pnl": round(float(realized_pnl), 8),
        "pnl_units": round(_cost_units(order_cost, price), 10),
        "remaining_units": round(_cost_units(order_cost, price), 10) if event == "entry" else 0.0,
        "position_id": "ai_bracket",
        "position_status": "open" if event == "entry" else "closed",
        **order_cost.fill_fields(),
    }


def _cost_units(order_cost, price: float) -> float:
    if order_cost.contracts is not None:
        multiplier = 1.0
        model = order_cost.cost_model if isinstance(order_cost.cost_model, dict) else {}
        if model.get("contract_multiplier") is not None:
            multiplier = float(model["contract_multiplier"])
        elif isinstance(model.get("side_cost"), dict) and model["side_cost"].get("contract_multiplier") is not None:
            multiplier = float(model["side_cost"]["contract_multiplier"])
        return float(order_cost.contracts) * multiplier
    return float(order_cost.notional) / float(price)


def _exit_notional(entry_cost, entry_price: float, exit_price: float) -> float | None:
    if entry_cost.contracts is not None:
        return None
    return _cost_units(entry_cost, entry_price) * float(exit_price)


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


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
