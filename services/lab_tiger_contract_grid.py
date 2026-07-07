from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any

from schemas.market_data import Bar
from services.config_loader import load_risk_rules
from services.journal_store import write_json
from services.lab_r5_grid import Cycle, breakeven_hit_rate, segment_cycles
from services.lab_registry import LabRegistry
from services.venue_costs import venue_side_cost

ARMS = ("oracle", "anti", "random", "always_long")


@dataclass(frozen=True)
class TigerContractGridConfig:
    spacing_usd: tuple[float, ...] = (2.5, 5.0, 8.0)
    range_k: tuple[float, ...] = (0.5, 1.0)
    max_contracts: tuple[int, ...] = (1, 2)
    contract_multiplier: float = 10.0
    venue: str = "tiger_mgc"
    min_cycles: int = 5
    random_seed: int = 7


def simulate_contract_cycle(
    cycle: Cycle,
    direction: int,
    *,
    spacing_usd: float,
    range_k: float,
    max_contracts: int,
    config: TigerContractGridConfig,
    cost_rules: dict | None = None,
) -> dict:
    if direction == 0 or not cycle.bars:
        return _idle()
    cost_rules = cost_rules or load_risk_rules().get("default", {}).get("paper_execution_costs", {})
    rows = tuple(cycle.bars)
    anchor = float(rows[0].open)
    half_width = float(range_k) * float(cycle.prev_range)
    n_rungs = min(int(max_contracts), int(half_width / float(spacing_usd))) if spacing_usd > 0 else 0
    if n_rungs < 1:
        return _idle()

    sign = 1 if direction > 0 else -1
    levels = [anchor - sign * float(spacing_usd) * (idx + 1) for idx in range(n_rungs)]
    stop = anchor - sign * half_width
    holding: dict[int, int] = {}
    fills: list[dict[str, Any]] = []
    gross_pnl = 0.0
    total_cost = 0.0
    sides = 0
    round_trips = 0
    stop_hit = False
    max_inventory = 0

    for i, bar in enumerate(rows):
        low = float(bar.low)
        high = float(bar.high)
        for rung, level in enumerate(levels):
            if rung in holding:
                continue
            hit = low <= level if sign > 0 else high >= level
            if not hit:
                continue
            cost = venue_side_cost(cost_rules, venue=config.venue, price=level, quantity=1, contract_multiplier=config.contract_multiplier)
            total_cost += cost.total_cost
            sides += 1
            holding[rung] = i
            max_inventory = max(max_inventory, len(holding))
            fills.append(_fill(cycle.cycle_id, fills, bar, side="buy" if sign > 0 else "sell", price=level, event="entry", rung=rung, gross_pnl=0.0, cost=cost.total_cost))

        breached = low <= stop if sign > 0 else high >= stop
        if breached:
            exit_price = min(float(bar.open), stop) if sign > 0 else max(float(bar.open), stop)
            for rung in list(holding):
                entry = levels[rung]
                gross = sign * (exit_price - entry) * float(config.contract_multiplier)
                cost = venue_side_cost(cost_rules, venue=config.venue, price=exit_price, quantity=1, contract_multiplier=config.contract_multiplier)
                gross_pnl += gross
                total_cost += cost.total_cost
                sides += 1
                fills.append(_fill(cycle.cycle_id, fills, bar, side="sell" if sign > 0 else "buy", price=exit_price, event="stop", rung=rung, gross_pnl=gross, cost=cost.total_cost))
            holding.clear()
            stop_hit = True
            break

        for rung in list(holding):
            if holding[rung] >= i:
                continue
            target = levels[rung] + sign * float(spacing_usd)
            done = high >= target if sign > 0 else low <= target
            if not done:
                continue
            gross = sign * (target - levels[rung]) * float(config.contract_multiplier)
            cost = venue_side_cost(cost_rules, venue=config.venue, price=target, quantity=1, contract_multiplier=config.contract_multiplier)
            gross_pnl += gross
            total_cost += cost.total_cost
            sides += 1
            round_trips += 1
            fills.append(_fill(cycle.cycle_id, fills, bar, side="sell" if sign > 0 else "buy", price=target, event="target", rung=rung, gross_pnl=gross, cost=cost.total_cost))
            del holding[rung]

    if holding:
        last = rows[-1]
        exit_price = float(last.close)
        for rung in list(holding):
            entry = levels[rung]
            gross = sign * (exit_price - entry) * float(config.contract_multiplier)
            cost = venue_side_cost(cost_rules, venue=config.venue, price=exit_price, quantity=1, contract_multiplier=config.contract_multiplier)
            gross_pnl += gross
            total_cost += cost.total_cost
            sides += 1
            fills.append(_fill(cycle.cycle_id, fills, last, side="sell" if sign > 0 else "buy", price=exit_price, event="flatten", rung=rung, gross_pnl=gross, cost=cost.total_cost))
        holding.clear()

    return {
        "traded": bool(fills),
        "gross_pnl": gross_pnl,
        "total_cost": total_cost,
        "net_pnl": gross_pnl - total_cost,
        "sides": sides,
        "round_trips": round_trips,
        "stop_hit": stop_hit,
        "max_inventory": max_inventory,
        "fills": fills,
    }


def run_tiger_contract_grid(output_root: Path, registry: LabRegistry, bars: list[Bar], *, config: TigerContractGridConfig | None = None) -> dict:
    cfg = config or TigerContractGridConfig()
    cycles = segment_cycles(bars)
    rng = random.Random(cfg.random_seed)
    random_dirs = {cycle.cycle_id: rng.choice((1, -1)) for cycle in cycles}
    cost_rules = load_risk_rules().get("default", {}).get("paper_execution_costs", {})

    cells: list[dict] = []
    for arm in ARMS:
        for k in cfg.range_k:
            for spacing in cfg.spacing_usd:
                for max_contracts in cfg.max_contracts:
                    exp_id = f"R5_tiger_mgc_{arm}_k{_tag(k)}_sp{_tag(spacing)}_max{max_contracts}"
                    registry.start(
                        {
                            "hypothesis": "M3: Tiger MGC integer-contract grid viability under fixed per-contract costs",
                            "family": "r5_tiger_mgc_contract_grid",
                            "strategy_ref": {"strategy_id": "tiger_mgc_contract_grid", "engine": "contract_grid", "symbol": bars[0].symbol if bars else "MGCmain", "timeframe": "1m"},
                            "params_diff": {"arm": arm, "range_k": k, "spacing_usd": spacing, "max_contracts": max_contracts},
                            "data_range": _data_range(bars),
                            "notes": ["exploratory sample; not promotion evidence", "oracle/anti arms use deliberate hindsight"],
                        },
                        exp_id=exp_id,
                    )
                    summary = _run_cell(cycles, arm, random_dirs, spacing_usd=spacing, range_k=k, max_contracts=max_contracts, config=cfg, cost_rules=cost_rules)
                    status = "valid" if summary["cycles_traded"] >= cfg.min_cycles else "invalid"
                    registry.finalize(exp_id, status=status, results={"objective": {"tiger_contract_grid": {**summary, "status": status}}})
                    cells.append({"exp_id": exp_id, "arm": arm, "range_k": k, "spacing_usd": spacing, "max_contracts": max_contracts, "status": status, **summary})

    report = _build_report(cells, cfg, bars, len(cycles), cost_rules)
    write_json(output_root / "lab" / "reports" / "R5_tiger_mgc_contract_grid.json", [report])
    _write_markdown(output_root / "lab" / "reports" / "R5_tiger_mgc_contract_grid.md", report)
    return report


def _run_cell(
    cycles: list[Cycle],
    arm: str,
    random_dirs: dict[str, int],
    *,
    spacing_usd: float,
    range_k: float,
    max_contracts: int,
    config: TigerContractGridConfig,
    cost_rules: dict,
) -> dict:
    values: list[float] = []
    gross_values: list[float] = []
    costs: list[float] = []
    traded = 0
    sides = 0
    trips = 0
    stops = 0
    max_inventory = 0
    for cycle in cycles:
        result = simulate_contract_cycle(
            cycle,
            _arm_direction(arm, cycle, random_dirs),
            spacing_usd=spacing_usd,
            range_k=range_k,
            max_contracts=max_contracts,
            config=config,
            cost_rules=cost_rules,
        )
        if not result["traded"]:
            continue
        traded += 1
        values.append(float(result["net_pnl"]))
        gross_values.append(float(result["gross_pnl"]))
        costs.append(float(result["total_cost"]))
        sides += int(result["sides"])
        trips += int(result["round_trips"])
        stops += 1 if result["stop_hit"] else 0
        max_inventory = max(max_inventory, int(result["max_inventory"]))
    return {
        "cycles_traded": traded,
        "mean_net_per_cycle": round(mean(values), 4) if values else 0.0,
        "median_net_per_cycle": round(median(values), 4) if values else 0.0,
        "total_net": round(sum(values), 2),
        "mean_gross_per_cycle": round(mean(gross_values), 4) if gross_values else 0.0,
        "mean_cost_per_cycle": round(mean(costs), 4) if costs else 0.0,
        "cycle_win_rate": round(sum(1 for value in values if value > 0) / len(values), 4) if values else 0.0,
        "worst_cycle": round(min(values), 2) if values else 0.0,
        "avg_sides_per_cycle": round(sides / traded, 2) if traded else 0.0,
        "avg_round_trips_per_cycle": round(trips / traded, 2) if traded else 0.0,
        "stop_rate": round(stops / traded, 4) if traded else 0.0,
        "max_inventory": max_inventory,
    }


def _build_report(cells: list[dict], cfg: TigerContractGridConfig, bars: list[Bar], n_cycles: int, cost_rules: dict) -> dict:
    breakevens: dict[str, float | None] = {}
    for k in cfg.range_k:
        for spacing in cfg.spacing_usd:
            for max_contracts in cfg.max_contracts:
                key = f"k{_tag(k)}_sp{_tag(spacing)}_max{max_contracts}"
                oracle = _find_cell(cells, "oracle", k, spacing, max_contracts)
                anti = _find_cell(cells, "anti", k, spacing, max_contracts)
                value = breakeven_hit_rate(float(oracle["mean_net_per_cycle"]), float(anti["mean_net_per_cycle"])) if oracle and anti else None
                breakevens[key] = round(value, 4) if value is not None else None
    viable = [
        cell for cell in cells
        if cell["arm"] == "random" and cell["status"] == "valid" and float(cell["mean_net_per_cycle"]) > 0
    ]
    return {
        "generated_at": _now(),
        "family": "r5_tiger_mgc_contract_grid",
        "trial_count": len(cells),
        "data_range": _data_range(bars),
        "coverage_note": f"{n_cycles} cycles, exploratory sample only; no holdout consumed",
        "sizing_note": "integer contracts: one MGC contract per rung, max inventory capped by max_contracts",
        "cost_model": {
            "venue": cfg.venue,
            "contract_multiplier": cfg.contract_multiplier,
            "model": cost_rules.get("venues", {}).get(cfg.venue, {}),
        },
        "cells": cells,
        "breakeven_hit_rate": breakevens,
        "decision_gate": {
            "passed": bool(viable),
            "criterion": "at least one valid random arm has positive mean_net_per_cycle",
            "viable_random_cells": viable,
        },
    }


def _write_markdown(path: Path, report: dict) -> None:
    lines = [
        "# R5 Tiger MGC Contract Grid",
        "",
        f"- Generated: {report['generated_at']}",
        f"- Coverage: {report['coverage_note']}; range {report['data_range'].get('start')} -> {report['data_range'].get('end')}",
        f"- Sizing: {report['sizing_note']}",
        f"- Decision gate: {'PASS' if report['decision_gate']['passed'] else 'FAIL'} - {report['decision_gate']['criterion']}",
        "- This is exploratory only: no holdout was consumed and oracle/anti arms are diagnostic bounds.",
        "",
        "|arm|k|spacing $/oz|max contracts|mean net|mean gross|mean cost|win rate|avg sides|stop rate|status|",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for cell in report["cells"]:
        lines.append(
            f"|{cell['arm']}|{cell['range_k']}|{cell['spacing_usd']}|{cell['max_contracts']}|"
            f"{cell['mean_net_per_cycle']}|{cell['mean_gross_per_cycle']}|{cell['mean_cost_per_cycle']}|"
            f"{cell['cycle_win_rate']}|{cell['avg_sides_per_cycle']}|{cell['stop_rate']}|{cell['status']}|"
        )
    lines += ["", "## Break-even Direction Hit Rate", "", "|geometry|hit rate|", "|---|---|"]
    for key, value in report["breakeven_hit_rate"].items():
        lines.append(f"|{key}|{'n/a' if value is None else f'{value:.1%}'}|")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fill(cycle_id: str, fills: list[dict[str, Any]], bar: Bar, *, side: str, price: float, event: str, rung: int, gross_pnl: float, cost: float) -> dict[str, Any]:
    return {
        "fill_id": f"{cycle_id}_tiger_mgc_{len(fills) + 1:04d}",
        "ts": bar.timestamp,
        "side": side,
        "price": round(float(price), 8),
        "contracts": 1,
        "event": event,
        "rung": rung,
        "gross_pnl": round(float(gross_pnl), 8),
        "cost": round(float(cost), 8),
        "realized_pnl": round(float(gross_pnl) - float(cost), 8),
    }


def _arm_direction(arm: str, cycle: Cycle, random_dirs: dict[str, int]) -> int:
    if arm == "oracle":
        return cycle.realized_direction
    if arm == "anti":
        return -cycle.realized_direction
    if arm == "random":
        return random_dirs[cycle.cycle_id]
    return 1


def _find_cell(cells: list[dict], arm: str, k: float, spacing: float, max_contracts: int) -> dict | None:
    for cell in cells:
        if cell["arm"] == arm and cell["range_k"] == k and cell["spacing_usd"] == spacing and cell["max_contracts"] == max_contracts:
            return cell
    return None


def _idle() -> dict:
    return {"traded": False, "gross_pnl": 0.0, "total_cost": 0.0, "net_pnl": 0.0, "sides": 0, "round_trips": 0, "stop_hit": False, "max_inventory": 0, "fills": []}


def _data_range(bars: list[Bar]) -> dict[str, Any]:
    if not bars:
        return {"start": "", "end": "", "bars": 0, "symbol": ""}
    return {"start": bars[0].timestamp, "end": bars[-1].timestamp, "bars": len(bars), "symbol": bars[0].symbol}


def _tag(value: float) -> str:
    return str(value).replace(".", "p")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
