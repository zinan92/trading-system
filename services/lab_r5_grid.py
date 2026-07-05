"""R5-B: oracle-bracket conditional grid experiment for Strategy Lab.

Question: how much is a correct 12h direction call worth to a conditional
grid, and what direction hit-rate is required to break even?

Design
------
- Cycles follow the dual-track console definition: UTC 01:00-13:00 (DAY,
  09:00-21:00 Beijing) and 13:00-01:00 (NIGHT).
- Direction arms per cycle: ``oracle`` (realized cycle direction, deliberate
  hindsight — diagnostic upper bound, never promotion evidence), ``anti``
  (always wrong, lower bound), ``random`` (seeded null), ``always_long``.
- Grid mechanics (long; short is the mirror): anchor = cycle open. Range
  half-width = ``k`` x previous cycle high-low range. Buy rungs one
  ``spacing`` apart below the anchor, take-profit one spacing above each
  fill, hard stop at the adverse range edge, everything flattened at cycle
  end (no financing carry).
- Conservative simulation: stop is checked before fills within a bar; a
  breach bar fills at the worse of bar open and stop; a rung cannot buy and
  take profit inside the same 1m bar; fills require the bar to trade through
  the limit price.
- Fixed research sizing: $1,000 notional per rung, max 10 rungs — matches
  the $10k/track console budget at 1x. Costs are per-side basis points on
  rung notional; the grid covers CFD raw-pricing (~0.3-0.5bp) through
  Binance taker (5bp).

Every arm x geometry cell is registered in the lab registry before results
are inspected (family ``r5_grid_direction_gold_1m``).
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any

from schemas.market_data import Bar
from services.journal_store import write_json
from services.lab_registry import LabRegistry

ARMS = ("oracle", "anti", "random", "always_long")
CYCLE_MIN_BARS = 600


@dataclass(frozen=True)
class R5GridConfig:
    spacing_bp: tuple[float, ...] = (10.0, 20.0, 30.0)
    range_k: tuple[float, ...] = (0.5, 0.75, 1.0)
    cost_grid_bp: tuple[float, ...] = (0.3, 0.5, 1.0, 2.0, 5.0)
    rung_notional: float = 1_000.0
    max_rungs: int = 10
    min_cycles: int = 100
    random_seed: int = 7


@dataclass(frozen=True)
class Cycle:
    cycle_id: str
    kind: str
    bars: tuple[Bar, ...]
    prev_range: float

    @property
    def anchor(self) -> float:
        return float(self.bars[0].open)

    @property
    def realized_direction(self) -> int:
        move = float(self.bars[-1].close) - self.anchor
        if move > 0:
            return 1
        if move < 0:
            return -1
        return 0


def segment_cycles(bars: list[Bar]) -> list[Cycle]:
    """Split bars into Park's 12h cycles; drop gap-ridden cycles."""

    buckets: dict[tuple[str, str], list[Bar]] = {}
    order: list[tuple[str, str]] = []
    for bar in bars:
        key = _cycle_key(_parse_ts(bar.timestamp))
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(bar)

    cycles: list[Cycle] = []
    prev_range = 0.0
    for key in order:
        rows = buckets[key]
        rng = max(float(b.high) for b in rows) - min(float(b.low) for b in rows)
        if len(rows) >= CYCLE_MIN_BARS and prev_range > 0:
            cycles.append(Cycle(cycle_id=f"{key[0]}_{key[1]}", kind=key[1], bars=tuple(rows), prev_range=prev_range))
        prev_range = rng
    return cycles


def simulate_cycle(cycle: Cycle, direction: int, *, spacing_bp: float, range_k: float, config: R5GridConfig) -> dict:
    """Run one conditional grid over one cycle. Returns fills/PnL breakdown."""

    if direction == 0:
        return _idle_result()
    anchor = cycle.anchor
    spacing = anchor * spacing_bp / 10_000.0
    half_width = range_k * cycle.prev_range
    n_rungs = min(config.max_rungs, int(half_width / spacing)) if spacing > 0 else 0
    if n_rungs < 1:
        return _idle_result()

    sign = 1 if direction > 0 else -1
    levels = [anchor - sign * spacing * (i + 1) for i in range(n_rungs)]
    stop = anchor - sign * half_width
    holding: dict[int, int] = {}  # rung index -> fill bar index
    filled_gross = 0.0
    sides = 0
    round_trips = 0
    stop_hit = False
    max_inventory = 0

    for i, bar in enumerate(cycle.bars):
        low, high = float(bar.low), float(bar.high)
        breached = low <= stop if sign > 0 else high >= stop
        for rung, level in enumerate(levels):
            if rung in holding:
                continue
            hits = low <= level if sign > 0 else high >= level
            if hits:
                holding[rung] = i
                sides += 1
                max_inventory = max(max_inventory, len(holding))
        if breached:
            # conservative: rungs fill at their levels, then everything stops out
            exit_price = min(float(bar.open), stop) if sign > 0 else max(float(bar.open), stop)
            for rung in list(holding):
                filled_gross += sign * (exit_price - levels[rung]) * _units(levels[rung], config)
                sides += 1
            holding.clear()
            stop_hit = True
            break
        for rung in list(holding):
            if holding[rung] >= i:
                continue  # no same-bar round trip
            target = levels[rung] + sign * spacing
            done = high >= target if sign > 0 else low <= target
            if done:
                filled_gross += sign * (target - levels[rung]) * _units(levels[rung], config)
                sides += 1
                round_trips += 1
                del holding[rung]

    if holding:
        last_close = float(cycle.bars[-1].close)
        for rung in list(holding):
            filled_gross += sign * (last_close - levels[rung]) * _units(levels[rung], config)
            sides += 1
        holding.clear()

    net_by_cost = {
        _cost_key(bp): filled_gross - sides * config.rung_notional * bp / 10_000.0
        for bp in config.cost_grid_bp
    }
    return {
        "traded": True,
        "gross_pnl": filled_gross,
        "net_by_cost": net_by_cost,
        "sides": sides,
        "round_trips": round_trips,
        "stop_hit": stop_hit,
        "max_inventory": max_inventory,
    }


def run_r5_grid(output_root: Path, registry: LabRegistry, bars: list[Bar], *, config: R5GridConfig | None = None) -> dict:
    cfg = config or R5GridConfig()
    cycles = segment_cycles(bars)
    data_range = _data_range(bars)
    rng = random.Random(cfg.random_seed)
    random_dirs = {cycle.cycle_id: rng.choice((1, -1)) for cycle in cycles}

    cells: list[dict] = []
    for arm in ARMS:
        for k in cfg.range_k:
            for s_bp in cfg.spacing_bp:
                exp_id = f"R5_grid_{arm}_k{_fmt_tag(k)}_s{_fmt_tag(s_bp)}bp"
                entry = registry.start(
                    {
                        "hypothesis": "R5-B: value of the 12h direction call to a conditional grid",
                        "family": "r5_grid_direction_gold_1m",
                        "strategy_ref": {"strategy_id": "conditional_grid", "engine": "r5_grid", "symbol": "GOLD", "timeframe": "1m"},
                        "params_diff": {"arm": arm, "range_k": k, "spacing_bp": s_bp},
                        "data_range": data_range,
                        "notes": ["oracle/anti arms use deliberate hindsight; diagnostic only, never promotion evidence"],
                    },
                    exp_id=exp_id,
                )
                summary = _run_cell(cycles, arm, random_dirs, spacing_bp=s_bp, range_k=k, config=cfg)
                cost_rows = summary.pop("cost_grid_results")
                status = "valid" if summary["cycles_traded"] >= cfg.min_cycles else "invalid"
                registry.finalize(
                    entry["exp_id"],
                    status=status,
                    results={
                        "cost_grid_results": cost_rows,
                        "objective": {"r5_grid": {**summary, "status": status}},
                    },
                )
                cells.append({
                    "exp_id": exp_id, "arm": arm, "range_k": k, "spacing_bp": s_bp,
                    "status": status, "cost_grid_results": cost_rows, **summary,
                })

    report = _build_report(cells, cfg, data_range, len(cycles))
    write_json(output_root / "lab" / "reports" / "R5_grid_oracle.json", [report])
    _write_markdown(output_root / "lab" / "reports" / "R5_grid_oracle.md", report)
    return report


def breakeven_hit_rate(oracle_mean: float, anti_mean: float) -> float | None:
    """Hit rate where hr*oracle + (1-hr)*anti = 0; None when not bracketed."""

    if oracle_mean <= 0 or anti_mean >= 0:
        return None
    return -anti_mean / (oracle_mean - anti_mean)


def _run_cell(cycles: list[Cycle], arm: str, random_dirs: dict[str, int], *, spacing_bp: float, range_k: float, config: R5GridConfig) -> dict:
    per_cost: dict[str, list[float]] = {_cost_key(bp): [] for bp in config.cost_grid_bp}
    traded = 0
    sides = 0
    trips = 0
    stops = 0
    for cycle in cycles:
        direction = _arm_direction(arm, cycle, random_dirs)
        result = simulate_cycle(cycle, direction, spacing_bp=spacing_bp, range_k=range_k, config=config)
        if not result["traded"]:
            continue
        traded += 1
        sides += result["sides"]
        trips += result["round_trips"]
        stops += 1 if result["stop_hit"] else 0
        for key, value in result["net_by_cost"].items():
            per_cost[key].append(value)

    cost_rows = {}
    for key, values in per_cost.items():
        if values:
            cost_rows[key] = {
                "mean_net_per_cycle": round(mean(values), 4),
                "median_net_per_cycle": round(median(values), 4),
                "total_net": round(sum(values), 2),
                "cycle_win_rate": round(sum(1 for v in values if v > 0) / len(values), 4),
                "worst_cycle": round(min(values), 2),
            }
    return {
        "cycles_traded": traded,
        "avg_sides_per_cycle": round(sides / traded, 2) if traded else 0.0,
        "avg_round_trips_per_cycle": round(trips / traded, 2) if traded else 0.0,
        "stop_rate": round(stops / traded, 4) if traded else 0.0,
        "cost_grid_results": cost_rows,
    }


def _build_report(cells: list[dict], cfg: R5GridConfig, data_range: dict, n_cycles: int) -> dict:
    bars_note = f"{n_cycles} research cycles (holdout excluded upstream)"
    breakevens: dict[str, dict[str, float | None]] = {}
    for k in cfg.range_k:
        for s_bp in cfg.spacing_bp:
            cell_key = f"k{_fmt_tag(k)}_s{_fmt_tag(s_bp)}bp"
            row: dict[str, float | None] = {}
            oracle = _find_cell(cells, "oracle", k, s_bp)
            anti = _find_cell(cells, "anti", k, s_bp)
            for bp in cfg.cost_grid_bp:
                key = _cost_key(bp)
                o_mean = oracle["cost_grid_results_mean"].get(key) if oracle else None
                a_mean = anti["cost_grid_results_mean"].get(key) if anti else None
                row[key] = (
                    round(breakeven_hit_rate(o_mean, a_mean), 4)
                    if o_mean is not None and a_mean is not None and breakeven_hit_rate(o_mean, a_mean) is not None
                    else None
                )
            breakevens[cell_key] = row
    return {
        "generated_at": _now(),
        "family": "r5_grid_direction_gold_1m",
        "trial_count": len(cells),
        "data_range": data_range,
        "coverage_note": bars_note,
        "sizing_note": "$1,000 per rung, max 10 rungs ($10k at 1x); costs are per-side bp on rung notional",
        "cells": cells,
        "breakeven_hit_rate": breakevens,
    }


def _find_cell(cells: list[dict], arm: str, k: float, s_bp: float) -> dict | None:
    for cell in cells:
        if cell["arm"] == arm and cell["range_k"] == k and cell["spacing_bp"] == s_bp:
            means = {key: row["mean_net_per_cycle"] for key, row in cell.get("cost_grid_results", {}).items()}
            return {**cell, "cost_grid_results_mean": means}
    return None


def _write_markdown(path: Path, report: dict) -> None:
    lines = [
        "# R5-B Oracle-Bracket Conditional Grid",
        "",
        f"- Generated: {report['generated_at']}",
        f"- Trials registered: {report['trial_count']} (family `{report['family']}`)",
        f"- Coverage: {report['coverage_note']}; range {report['data_range'].get('start')} -> {report['data_range'].get('end')}",
        f"- Sizing: {report['sizing_note']}",
        "- Oracle/anti arms are deliberate hindsight: diagnostic bounds only, never promotion evidence.",
        "",
        "## Mean net PnL per cycle ($) by arm x geometry x cost",
        "",
    ]
    costs = sorted(report["cells"][0]["cost_grid_results"].keys(), key=lambda key: float(key.rstrip("bp"))) if report["cells"] else []
    lines.append("|arm|k|spacing bp|" + "|".join(costs) + "|cycle win@0.5bp|stop rate|")
    lines.append("|---|---|---|" + "|".join(["---"] * len(costs)) + "|---|---|")
    for cell in report["cells"]:
        rows = cell.get("cost_grid_results", {})
        win = rows.get("0.5bp", {}).get("cycle_win_rate", "")
        lines.append(
            f"|{cell['arm']}|{cell['range_k']}|{cell['spacing_bp']}|"
            + "|".join(str(rows.get(c, {}).get("mean_net_per_cycle", "")) for c in costs)
            + f"|{win}|{cell.get('stop_rate', '')}|"
        )
    lines += ["", "## Break-even direction hit rate (oracle vs anti)", ""]
    lines.append("|geometry|" + "|".join(costs) + "|")
    lines.append("|---|" + "|".join(["---"] * len(costs)) + "|")
    for cell_key, row in report["breakeven_hit_rate"].items():
        lines.append(f"|{cell_key}|" + "|".join("n/a" if row.get(c) is None else f"{row[c]:.1%}" for c in costs) + "|")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _arm_direction(arm: str, cycle: Cycle, random_dirs: dict[str, int]) -> int:
    if arm == "oracle":
        return cycle.realized_direction
    if arm == "anti":
        return -cycle.realized_direction
    if arm == "random":
        return random_dirs[cycle.cycle_id]
    return 1


def _idle_result() -> dict:
    return {"traded": False, "gross_pnl": 0.0, "net_by_cost": {}, "sides": 0, "round_trips": 0, "stop_hit": False, "max_inventory": 0}


def _units(level: float, config: R5GridConfig) -> float:
    return config.rung_notional / level if level > 0 else 0.0


def _cycle_key(ts: datetime) -> tuple[str, str]:
    utc = ts.astimezone(timezone.utc)
    if 1 <= utc.hour < 13:
        return utc.strftime("%Y-%m-%d"), "DAY"
    anchor = utc if utc.hour >= 13 else _shift_day(utc)
    return anchor.strftime("%Y-%m-%d"), "NIGHT"


def _shift_day(ts: datetime) -> datetime:
    from datetime import timedelta

    return ts - timedelta(days=1)


def _parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _data_range(bars: list[Bar]) -> dict[str, Any]:
    if not bars:
        return {"start": "", "end": "", "bars": 0}
    return {"start": bars[0].timestamp, "end": bars[-1].timestamp, "bars": len(bars)}


def _cost_key(bp: float) -> str:
    return f"{bp:g}bp"


def _fmt_tag(value: float) -> str:
    return f"{value:g}".replace(".", "p")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
