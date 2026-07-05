"""R3 hold-time / exit-geometry sweep for Strategy Lab."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from schemas.market_data import Bar
from services.journal_store import write_json
from services.lab_evaluator import LabSimulationConfig, timeframe_seconds
from services.lab_r1_scan import (
    R1ScanConfig,
    _compact_eval,
    _data_range,
    _evaluate_replayed_windows,
    _fmt_float,
    _fmt_money,
    _fmt_ratio_pct,
    _fmt_percent_value,
    _replay_strategy_windows,
    _write_report,
    break_even_bp,
)
from services.lab_registry import LabRegistry
from services.lab_walkforward import WalkForwardConfig
from services.strategy_registry import Strategy, StrategyRegistry

R3_SUBJECTS = (
    "gold_5m_psych_level_rejection",
    "gold_5m_vwap_extension_reversion",
    "gold_5m_ema50_position",
    "gold_1m_breakout",
    "gold_1m_macd",
)
HOLD_MULTIPLIERS = (1, 2, 4, 8)
STOP_TARGET_SCALES = (1.0, 1.5, 2.0)


@dataclass(frozen=True)
class R3SweepConfig:
    cost_grid: tuple[float, ...] = (0.0, 0.5, 1.0, 2.0, 5.0)
    maker_bp: float = 2.0
    min_oos_trades: int = 100
    walkforward: WalkForwardConfig = field(default_factory=WalkForwardConfig)


def run_r3_sweep(
    output_root: Path,
    registry: LabRegistry,
    bars: list[Bar],
    *,
    config: R3SweepConfig | None = None,
    anchor_expected: dict | None = None,
) -> dict:
    cfg = config or R3SweepConfig()
    registry_by_id = {strategy.strategy_id: strategy for strategy in StrategyRegistry().strategies()}
    selected = [registry_by_id[sid] for sid in R3_SUBJECTS if sid in registry_by_id]
    rows: list[dict] = []
    entries: dict[str, dict] = {}
    for strategy in selected:
        replayed = _replay_strategy_windows(strategy, bars, cfg.walkforward)
        for hold_multiplier in HOLD_MULTIPLIERS:
            for scale in STOP_TARGET_SCALES:
                exp_id = _exp_id(strategy.strategy_id, hold_multiplier, scale)
                spec = _spec(strategy, bars, cfg, hold_multiplier, scale)
                registry.start(spec, exp_id=exp_id)
                payload = _evaluate_variant(strategy, replayed, hold_multiplier, scale, cfg)
                row = payload["row"]
                entry_status = "valid" if row["status"] in {"valid", "insufficient_sample"} else "invalid"
                entry = registry.finalize(
                    exp_id,
                    status=entry_status,
                    results={
                        "cost_grid_results": payload.get("cost_grid_results", {}),
                        "break_even_bp": row.get("break_even_bp"),
                        "data_coverage": _data_range(bars),
                        "r3_summary": row,
                        "objective": {"r3_hold_sweep": row},
                    },
                    notes=payload.get("notes", []),
                )
                rows.append(row)
                entries[exp_id] = entry
    divergence = _anchor_divergence(entries.get(_exp_id("gold_1m_macd", 1, 1.0), {}), anchor_expected or {})
    if divergence:
        path = _write_report(output_root, "R3_hold_sweep", _divergence_report(divergence))
        raise RuntimeError(f"R3 anchor divergence; see {path}: {divergence}")
    recommendation = recommend_label_horizon(rows)
    body = render_r3_report(rows, registry, recommendation, total_trials=len(rows), min_oos_trades=cfg.min_oos_trades)
    report_path = _write_report(output_root, "R3_hold_sweep", body)
    write_json(output_root / "lab" / "reports" / "R3_hold_sweep.json", [{"rows": rows, "recommendation": recommendation}])
    return {"status": "valid", "total_trials": len(rows), "rows": rows, "recommendation": recommendation, "report": str(report_path)}


def recommend_label_horizon(rows: list[dict]) -> dict:
    candidates = [
        row
        for row in rows
        if row.get("status") == "valid"
        and int(row.get("oos_trades") or 0) >= 100
        and row.get("gross_bp_per_trip") is not None
    ]
    if not candidates:
        return {"status": "inconclusive", "horizon_minutes": 240, "reason": "no_valid_100_trade_cells; default 4h"}
    ranked = sorted(
        candidates,
        key=lambda row: (
            -float(row.get("gross_bp_per_trip") or 0) / max(float(row.get("stop_target_scale") or 1), 0.1),
            -float(row.get("maker_2bp_expectancy_per_trade") or -999),
        ),
    )
    best = ranked[0]
    return {
        "status": "selected",
        "strategy": best["strategy"],
        "horizon_minutes": int(best["hold_minutes"]),
        "hold_multiplier": best["hold_multiplier"],
        "stop_target_scale": best["stop_target_scale"],
        "score_gross_bp_per_risk_scale": round(float(best["gross_bp_per_trip"]) / float(best["stop_target_scale"]), 6),
        "reason": "max gross bp/trip divided by stop/target scale among valid cells with >=100 trades",
    }


def render_r3_report(rows: list[dict], registry: LabRegistry, recommendation: dict, *, total_trials: int, min_oos_trades: int) -> str:
    lines = [
        "# R3 Hold Sweep",
        "",
        f"- Total R3 trials: {total_trials}",
        "- Multiple-testing caveat: 60 geometry cells were scanned; this is exploration, not promotion.",
        "- Holdout policy: R3 does not consume holdout.",
        f"- Recommended R2 label horizon: {recommendation['horizon_minutes']} minutes ({recommendation['reason']}).",
        "",
    ]
    for strategy in R3_SUBJECTS:
        subject_rows = [row for row in rows if row["strategy"] == strategy]
        if not subject_rows:
            continue
        lines.extend([f"## {strategy}", "", f"- Trial count for family `r3_hold_{strategy}`: {registry.trial_count(f'r3_hold_{strategy}')}", ""])
        headers = ["hold x scale", "1x", "1.5x", "2x"]
        lines.append("|" + "|".join(headers) + "|")
        lines.append("|" + "|".join("---" for _ in headers) + "|")
        by_cell = {(row["hold_multiplier"], row["stop_target_scale"]): row for row in subject_rows}
        for hold in HOLD_MULTIPLIERS:
            cells = [f"{hold}x"]
            for scale in STOP_TARGET_SCALES:
                row = by_cell.get((hold, scale), {})
                cells.append(f"{_fmt_float(row.get('gross_bp_per_trip'))} bp / {_fmt_money(row.get('maker_2bp_expectancy_per_trade'))}")
            lines.append("|" + "|".join(cells) + "|")
        best = _best_row(subject_rows)
        lines.extend([
            "",
            f"- Break-even frontier: {_frontier(subject_rows)}",
            f"- Best cell: hold {best.get('hold_multiplier')}x, scale {best.get('stop_target_scale')}x, gross {_fmt_float(best.get('gross_bp_per_trip'))} bp/trip, maker net {_fmt_money(best.get('maker_2bp_expectancy_per_trade'))}, trades {best.get('oos_trades')}.",
            "",
        ])
    winners = [
        row
        for row in rows
        if row.get("maker_2bp_expectancy_per_trade") is not None
        and float(row["maker_2bp_expectancy_per_trade"]) > 0
        and int(row.get("oos_trades") or 0) >= min_oos_trades
    ]
    lines.extend(["## Verdict", ""])
    if winners:
        lines.append(f"Cells net-positive at maker 2bp with >= {min_oos_trades} OOS trades:")
        for row in sorted(winners, key=lambda item: -float(item["maker_2bp_expectancy_per_trade"])):
            lines.append(f"- {row['strategy']} hold {row['hold_multiplier']}x scale {row['stop_target_scale']}x: maker net {_fmt_money(row['maker_2bp_expectancy_per_trade'])}, gross {_fmt_float(row['gross_bp_per_trip'])} bp/trip, trades {row['oos_trades']}.")
    else:
        lines.append(f"No R3 cell was net-positive at maker 2bp with >= {min_oos_trades} OOS trades.")
    lines.append("")
    return "\n".join(lines)


def _evaluate_variant(strategy: Strategy, replayed: dict, hold_multiplier: int, scale: float, config: R3SweepConfig) -> dict:
    hold_bars = 24 * hold_multiplier
    sim = LabSimulationConfig(stop_pct=0.006 * scale, target_pct=0.012 * scale, max_hold_bars=hold_bars)
    if replayed.get("status") != "valid":
        row = _empty_row(strategy, hold_multiplier, scale, hold_bars, str(replayed.get("reason", "not_replayable")))
        return {"row": row, "notes": [row["reason"]]}
    cost_results = {f"{bp:g}bp": _compact_eval(_evaluate_replayed_windows(replayed["windows"], bp, sim)) for bp in config.cost_grid}
    row = _row(strategy, hold_multiplier, scale, hold_bars, cost_results, config)
    return {"row": row, "cost_grid_results": cost_results, "notes": [] if row["status"] in {"valid", "insufficient_sample"} else [row["reason"]]}


def _row(strategy: Strategy, hold_multiplier: int, scale: float, hold_bars: int, costs: dict, config: R3SweepConfig) -> dict:
    gross = costs.get("0bp", {})
    maker = costs.get(f"{config.maker_bp:g}bp", {})
    metrics = gross.get("metrics", {})
    trade_count = int(metrics.get("trade_count") or 0)
    status = "valid" if gross.get("status") == "valid" else "invalid"
    reason = str(gross.get("reason", "pass" if status == "valid" else "invalid"))
    if status == "valid" and trade_count < config.min_oos_trades:
        status = "insufficient_sample"
        reason = f"oos_trades<{config.min_oos_trades}"
    return {
        "strategy": strategy.strategy_id,
        "timeframe": strategy.timeframe,
        "hold_multiplier": hold_multiplier,
        "stop_target_scale": scale,
        "max_hold_bars": hold_bars,
        "hold_minutes": int(hold_bars * timeframe_seconds(strategy.timeframe) / 60),
        "oos_trades": trade_count if metrics else None,
        "win_rate": metrics.get("win_rate"),
        "gross_bp_per_trip": metrics.get("expectancy_bp_on_notional"),
        "break_even_bp": break_even_bp(costs),
        "maker_2bp_expectancy_per_trade": maker.get("metrics", {}).get("expectancy_per_trade"),
        "max_drawdown_pct": maker.get("metrics", {}).get("max_drawdown_pct"),
        "sortino_at_2bp": maker.get("metrics", {}).get("sortino"),
        "status": status,
        "reason": reason,
    }


def _empty_row(strategy: Strategy, hold_multiplier: int, scale: float, hold_bars: int, reason: str) -> dict:
    return {
        "strategy": strategy.strategy_id,
        "timeframe": strategy.timeframe,
        "hold_multiplier": hold_multiplier,
        "stop_target_scale": scale,
        "max_hold_bars": hold_bars,
        "hold_minutes": int(hold_bars * timeframe_seconds(strategy.timeframe) / 60),
        "oos_trades": None,
        "win_rate": None,
        "gross_bp_per_trip": None,
        "break_even_bp": None,
        "maker_2bp_expectancy_per_trade": None,
        "max_drawdown_pct": None,
        "sortino_at_2bp": None,
        "status": "invalid",
        "reason": reason,
    }


def _best_row(rows: list[dict]) -> dict:
    return sorted(rows, key=lambda row: (row.get("maker_2bp_expectancy_per_trade") is None, -float(row.get("maker_2bp_expectancy_per_trade") or -999)))[0]


def _frontier(rows: list[dict]) -> str:
    parts = []
    for hold in HOLD_MULTIPLIERS:
        subset = [row for row in rows if row["hold_multiplier"] == hold and row.get("break_even_bp") is not None]
        if subset:
            best = max(subset, key=lambda row: float(row["break_even_bp"]))
            parts.append(f"{hold}x:{_fmt_float(best['break_even_bp'])}bp@{best['stop_target_scale']}x")
    return ", ".join(parts) if parts else "n/a"


def _spec(strategy: Strategy, bars: list[Bar], config: R3SweepConfig, hold: int, scale: float) -> dict:
    return {
        "hypothesis": f"R3 hold/exit sweep for {strategy.strategy_id}",
        "family": f"r3_hold_{strategy.strategy_id}",
        "strategy_ref": {"strategy_id": strategy.strategy_id, "engine": strategy.params.get("engine", "ma"), "timeframe": strategy.timeframe, "symbol": strategy.symbol},
        "params_diff": {"hold_multiplier": hold, "stop_target_scale": scale},
        "data_range": _data_range(bars),
        "windows": {"train_days": config.walkforward.train_days, "validate_days": config.walkforward.validate_days, "step_days": config.walkforward.step_days, "holdout_days": config.walkforward.holdout_days},
        "notes": ["R3 exploration only; holdout must remain unconsumed"],
    }


def _exp_id(strategy_id: str, hold: int, scale: float) -> str:
    return f"R3_{strategy_id}_h{hold}x_s{str(scale).replace('.', 'p')}x"


def _anchor_divergence(anchor_entry: dict, expected: dict) -> dict:
    if not expected:
        return {}
    row = anchor_entry.get("r3_summary", {})
    costs = anchor_entry.get("cost_grid_results", {})
    out: dict[str, Any] = {}
    if row.get("break_even_bp") != expected.get("break_even_bp"):
        out["break_even_bp"] = {"expected": expected.get("break_even_bp"), "actual": row.get("break_even_bp")}
    if row.get("oos_trades") != expected.get("trade_count"):
        out["trade_count"] = {"expected": expected.get("trade_count"), "actual": row.get("oos_trades")}
    for label, value in (expected.get("expectancy_per_trade") or {}).items():
        actual = costs.get(label, {}).get("metrics", {}).get("expectancy_per_trade")
        if actual != value:
            out.setdefault("expectancy_per_trade", {})[label] = {"expected": value, "actual": actual}
    return out


def _divergence_report(divergence: dict) -> str:
    return "# R3 Anchor Divergence\n\n```json\n" + str(divergence) + "\n```\n"
