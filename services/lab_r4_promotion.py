"""R4 pre-registered promotion protocol.

R4 is the only Strategy Lab experiment allowed to consume holdout. It evaluates
two fixed R3 winners, uses 0.5 bp/side as the primary promotion basis, reports
2 bp/side as the Binance-maker reference, and excludes funding by owner
decision.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from schemas.market_data import Bar
from services.journal_store import load_json, write_json
from services.lab_evaluator import LabSimulationConfig, equity_curve, evaluate_signals, regenerate_strategy_signals
from services.lab_objective import ObjectiveConfig, evaluate_objective
from services.lab_promotion import record_paper_eligibility
from services.lab_r1_scan import _compact_eval, _data_range, _evaluate_replayed_windows, _fmt_float, _fmt_money, _write_report
from services.lab_regimes import label_regimes
from services.lab_registry import LabRegistry
from services.lab_walkforward import HoldoutQuarantine, WalkForwardConfig
from services.strategy_registry import Strategy, StrategyRegistry

FUNDING_FOOTNOTE = "Funding excluded (owner decision); revisit before real-money on any perpetual venue."


@dataclass(frozen=True)
class R4Candidate:
    code: str
    family: str
    base_strategy_id: str
    promoted_strategy_id: str
    hold_multiplier: int
    stop_target_scale: float
    expected_trades: int
    expected_maker_2bp_net: float

    @property
    def max_hold_bars(self) -> int:
        return 24 * self.hold_multiplier


@dataclass(frozen=True)
class R4PromotionConfig:
    primary_bp: float = 0.5
    secondary_bp: float = 2.0
    primary_stress_bp: float = 0.75
    secondary_stress_bp: float = 3.0
    cost_grid: tuple[float, ...] = (0.0, 0.5, 0.75, 1.0, 2.0, 3.0, 5.0)
    walkforward: WalkForwardConfig = field(default_factory=WalkForwardConfig)
    objective: ObjectiveConfig = field(default_factory=ObjectiveConfig)
    bootstrap_samples: int = 2000
    bootstrap_seed: int = 44


CANDIDATES: tuple[R4Candidate, ...] = (
    R4Candidate("C1", "r4_ema50_8x_s2p0", "gold_5m_ema50_position", "gold_5m_ema50_position_swing", 8, 2.0, 171, 0.631297),
    R4Candidate("C2", "r4_psych_8x_s1p0", "gold_5m_psych_level_rejection", "gold_5m_psych_level_rejection_swing", 8, 1.0, 128, 0.263160),
)


def run_r4_promotion(output_root: Path, registry: LabRegistry, bars: list[Bar], config: R4PromotionConfig | None = None) -> dict:
    cfg = config or R4PromotionConfig()
    eval_bars = _pin_to_r3_range(output_root, bars)
    strategies = {strategy.strategy_id: strategy for strategy in StrategyRegistry().strategies()}
    rows = []
    for candidate in CANDIDATES:
        exp_id = f"R4_{candidate.promoted_strategy_id}"
        existing = registry.load(exp_id)
        if existing.get("holdout_consumed"):
            raise RuntimeError(f"R4 holdout already consumed for {candidate.family}")
        strategy = strategies.get(candidate.base_strategy_id)
        if strategy is None:
            rows.append(_error_row(candidate, "missing_base_strategy"))
            continue
        registry.start(_spec(candidate, strategy, eval_bars, cfg), exp_id=exp_id)
        row = _run_candidate(output_root, registry, exp_id, candidate, strategy, eval_bars, cfg)
        rows.append(row)
    report = render_r4_report(rows)
    report_path = _write_report(output_root, "R4_promotion", report)
    write_json(output_root / "lab" / "reports" / "R4_promotion.json", [{"rows": rows, "funding_footnote": FUNDING_FOOTNOTE}])
    return {"status": "valid", "rows": rows, "report": str(report_path)}


def render_r4_report(rows: list[dict]) -> str:
    lines = [
        "# R4 Promotion Protocol",
        "",
        f"- {FUNDING_FOOTNOTE}",
        "- Primary basis: 0.5 bp/side. Secondary reference: 2 bp/side.",
        "- Holdout policy: only primary Phase-1 passers consume holdout.",
        "",
        "## Phase 1 Gate Table",
        "",
        "|candidate|primary gates|primary net/trade|primary stress|primary regimes|secondary net/trade|secondary stress|secondary regimes|anchor|",
        "|---|---|---:|---:|---|---:|---:|---|---|",
    ]
    for row in rows:
        primary = row.get("phase1", {}).get("primary", {})
        secondary = row.get("phase1", {}).get("secondary", {})
        lines.append(
            "|"
            + "|".join([
                row["candidate"],
                _gate_summary(primary),
                _fmt_money(_metric(primary, "expectancy_per_trade")),
                _fmt_money(primary.get("stress_expectancy_per_trade")),
                _regime_summary(primary),
                _fmt_money(_metric(secondary, "expectancy_per_trade")),
                _fmt_money(secondary.get("stress_expectancy_per_trade")),
                _regime_summary(secondary),
                row.get("anchor_status", "n/a"),
            ])
            + "|"
        )
    lines.extend(["", "## Holdout Verdicts", ""])
    lines.append("|candidate|shot|primary trades|primary net/trade|primary 95% CI|primary max DD|primary verdict|secondary trades|secondary net/trade|secondary 95% CI|secondary max DD|")
    lines.append("|---|---|---:|---:|---|---:|---|---:|---:|---|---:|")
    for row in rows:
        holdout = row.get("holdout", {})
        primary = holdout.get("primary", {})
        secondary = holdout.get("secondary", {})
        lines.append(
            "|"
            + "|".join([
                row["candidate"],
                holdout.get("status", "forfeited"),
                str(_metric(primary, "trade_count") or "n/a"),
                _fmt_money(_metric(primary, "expectancy_per_trade")),
                _ci(primary),
                _fmt_float(_metric(primary, "max_drawdown_pct")),
                str(primary.get("verdict", "n/a")),
                str(_metric(secondary, "trade_count") or "n/a"),
                _fmt_money(_metric(secondary, "expectancy_per_trade")),
                _ci(secondary),
                _fmt_float(_metric(secondary, "max_drawdown_pct")),
            ])
            + "|"
        )
    lines.extend(["", "## Closing Sentences", ""])
    for row in rows:
        lines.append(f"- {row['candidate']}: {row['closing_sentence']}")
    lines.append("")
    return "\n".join(lines)


def _run_candidate(output_root: Path, registry: LabRegistry, exp_id: str, candidate: R4Candidate, strategy: Strategy, bars: list[Bar], cfg: R4PromotionConfig) -> dict:
    from services.lab_r1_scan import _replay_strategy_windows

    replayed = _replay_strategy_windows(strategy, bars, cfg.walkforward)
    if replayed.get("status") != "valid":
        row = _error_row(candidate, str(replayed.get("reason", "not_replayable")))
        registry.finalize(exp_id, status="invalid", results={"r4_summary": row}, notes=[row["reason"]])
        return row
    sim = _simulation(candidate)
    full_costs = {bp: _evaluate_replayed_windows(replayed["windows"], bp, sim) for bp in cfg.cost_grid}
    anchor = _anchor_check(candidate, full_costs[cfg.secondary_bp])
    if anchor:
        row = _error_row(candidate, f"anchor_mismatch:{anchor}")
        row["anchor_status"] = "mismatch"
        registry.finalize(exp_id, status="invalid", results={"r4_summary": row, "cost_grid_results": _compact_costs(full_costs)}, notes=[row["reason"]])
        _write_report(output_root, "R4_promotion", _anchor_report(row))
        raise RuntimeError(f"R4 anchor mismatch for {candidate.code}: {anchor}")
    phase1 = _phase1(full_costs, bars, cfg)
    holdout = {"status": "forfeited", "reason": "phase1_primary_failed"}
    expectation: dict[str, Any] = {}
    if phase1["primary"]["passed"]:
        holdout = _holdout(registry, exp_id, candidate, strategy, bars, cfg)
        if holdout["primary"].get("passed"):
            expectation = _lab_expectation(candidate, phase1, holdout, cfg)
    row = {
        "candidate": candidate.code,
        "family": candidate.family,
        "base_strategy_id": candidate.base_strategy_id,
        "promoted_strategy_id": candidate.promoted_strategy_id,
        "anchor_status": "matched",
        "phase1": phase1,
        "holdout": holdout,
        "paper_eligible": bool(expectation),
    }
    row["closing_sentence"] = _closing_sentence(row)
    results = {
        "cost_grid_results": _compact_costs(full_costs),
        "regime_slice_results": phase1["primary"].get("regime_slices", {}),
        "objective": {"walkforward": phase1["primary"]["objective"], "walkforward_secondary": phase1["secondary"]["objective"], "holdout": holdout.get("primary", {}).get("objective", {}), "holdout_secondary": holdout.get("secondary", {}).get("objective", {})},
        "r4_summary": row,
        "data_coverage": _data_range(bars),
    }
    if expectation:
        results["lab_expectation"] = expectation
    entry = registry.finalize(exp_id, status="valid", results=results)
    if expectation:
        row["paper_eligibility"] = record_paper_eligibility(output_root, candidate.promoted_strategy_id)
        registry.finalize(exp_id, status="valid", results={"paper_eligibility": row["paper_eligibility"]})
    return row


def _phase1(costs: dict[float, dict], bars: list[Bar], cfg: R4PromotionConfig) -> dict:
    return {
        "primary": _basis_phase1(costs[cfg.primary_bp], costs[cfg.primary_stress_bp], bars, cfg),
        "secondary": _basis_phase1(costs[cfg.secondary_bp], costs[cfg.secondary_stress_bp], bars, cfg),
    }


def _basis_phase1(result: dict, stress: dict, bars: list[Bar], cfg: R4PromotionConfig) -> dict:
    trades = result.get("trades", [])
    equity = result.get("equity", [])
    slices = _regime_slices(trades, bars)
    objective = evaluate_objective(trades, equity, cfg.objective, stress_expectancy_per_trade=stress.get("metrics", {}).get("expectancy_per_trade"), regime_slice_results={key: value for key, value in slices.items() if value.get("trade_count", 0) > 0})
    return {
        "status": result.get("status", "invalid"),
        "metrics": result.get("metrics", {}),
        "window_count": result.get("window_count", 0),
        "stress_expectancy_per_trade": stress.get("metrics", {}).get("expectancy_per_trade"),
        "regime_slices": slices,
        "objective": objective,
        "passed": bool(objective.get("passed")),
    }


def _holdout(registry: LabRegistry, exp_id: str, candidate: R4Candidate, strategy: Strategy, bars: list[Bar], cfg: R4PromotionConfig) -> dict:
    quarantine = HoldoutQuarantine(bars, cfg.walkforward)
    holdout_bars = quarantine.consume_holdout(registry, exp_id)
    replay = regenerate_strategy_signals(strategy, holdout_bars)
    if replay.get("status") != "valid":
        return {"status": "consumed_invalid", "reason": str(replay.get("reason", "not_replayable")), "primary": _invalid_holdout(), "secondary": _invalid_holdout()}
    primary = _holdout_basis(replay["bars"], replay["signals"], candidate, cfg.primary_bp, cfg)
    secondary = _holdout_basis(replay["bars"], replay["signals"], candidate, cfg.secondary_bp, cfg)
    return {"status": "consumed", "data_range": _data_range(holdout_bars), "primary": primary, "secondary": secondary}


def _holdout_basis(bars: list[Bar], signals: list[dict], candidate: R4Candidate, bp: float, cfg: R4PromotionConfig) -> dict:
    result = evaluate_signals(bars, signals, _simulation(candidate, bp))
    if result.get("status") != "valid":
        return _invalid_holdout(str(result.get("reason", "invalid")))
    metrics = result["metrics"]
    passed = metrics["expectancy_per_trade"] > 0 and metrics["max_drawdown_pct"] <= cfg.objective.max_drawdown_pct
    objective = {"passed": passed, "metrics": metrics, "criteria": {"expectancy_per_trade_gt_0": metrics["expectancy_per_trade"] > 0, "max_drawdown_within_cap": metrics["max_drawdown_pct"] <= cfg.objective.max_drawdown_pct}}
    return {"status": "valid", "passed": passed, "verdict": "pass" if passed else "fail", "metrics": metrics, "bootstrap_ci": _bootstrap_ci(result["trades"], cfg.bootstrap_samples, cfg.bootstrap_seed), "objective": objective}


def _regime_slices(trades: list[dict], bars: list[Bar]) -> dict[str, dict]:
    regimes = label_regimes(bars).get("four_hour_blocks", [])
    by_key = {item["key"]: item["volatility_bucket"] for item in regimes}
    buckets = {"low": [], "mid": [], "high": []}
    for trade in trades:
        bucket = by_key.get(_block_key(str(trade.get("entry_timestamp", ""))))
        if bucket in buckets:
            buckets[bucket].append(trade)
    out = {}
    for bucket, rows in buckets.items():
        total = sum(float(item.get("net_pnl", 0)) for item in rows)
        out[bucket] = {"trade_count": len(rows), "net_pnl": round(total, 6), "expectancy_per_trade": round(total / len(rows), 6) if rows else None}
    return out


def _bootstrap_ci(trades: list[dict], samples: int, seed: int) -> dict:
    values = [float(item.get("net_pnl", 0)) for item in trades]
    if not values:
        return {"level": 0.95, "low": None, "high": None, "width": None}
    rng = random.Random(seed)
    means = []
    for _ in range(samples):
        draw = [values[rng.randrange(len(values))] for _ in values]
        means.append(sum(draw) / len(draw))
    means.sort()
    low = means[int(0.025 * (len(means) - 1))]
    high = means[int(0.975 * (len(means) - 1))]
    return {"level": 0.95, "low": round(low, 6), "high": round(high, 6), "width": round(high - low, 6)}


def _lab_expectation(candidate: R4Candidate, phase1: dict, holdout: dict, cfg: R4PromotionConfig) -> dict:
    primary = phase1["primary"]["metrics"]
    secondary = phase1["secondary"]["metrics"]
    expected = _expected_trades_per_week(phase1["primary"], cfg)
    weeks = max(1.0, float(primary.get("trade_count", 0) or 0) / max(0.0001, expected))
    return {
        "basis_expectations": {
            "primary_0p5bp": {"net_expectancy_per_trade": primary.get("expectancy_per_trade"), "basis_bp_per_side": cfg.primary_bp},
            "secondary_2bp": {"net_expectancy_per_trade": secondary.get("expectancy_per_trade"), "basis_bp_per_side": cfg.secondary_bp},
        },
        "expected_trades_per_week": expected,
        "funding_footnote": FUNDING_FOOTNOTE,
        "candidate_family": candidate.family,
        "holdout_primary": holdout.get("primary", {}).get("metrics", {}),
        "holdout_secondary": holdout.get("secondary", {}).get("metrics", {}),
        "evidence_basis": "primary_0p5bp",
        "weeks_observed": round(weeks, 6),
    }


def _expected_trades_per_week(phase: dict, cfg: R4PromotionConfig) -> float:
    weeks = cfg.walkforward.validate_days / 7 * max(1, int(phase.get("window_count", 8) or 8))
    return round(float(phase.get("metrics", {}).get("trade_count", 0) or 0) / weeks, 6) if weeks else 0.0


def _simulation(candidate: R4Candidate, bp: float = 0.0) -> LabSimulationConfig:
    return LabSimulationConfig(stop_pct=0.006 * candidate.stop_target_scale, target_pct=0.012 * candidate.stop_target_scale, max_hold_bars=candidate.max_hold_bars, cost_bp_per_side=bp)


def _compact_costs(costs: dict[float, dict]) -> dict:
    return {f"{bp:g}bp": _compact_eval(value) for bp, value in costs.items()}


def _anchor_check(candidate: R4Candidate, result: dict) -> dict:
    metrics = result.get("metrics", {})
    out = {}
    if metrics.get("trade_count") != candidate.expected_trades:
        out["trade_count"] = {"expected": candidate.expected_trades, "actual": metrics.get("trade_count")}
    if metrics.get("expectancy_per_trade") != candidate.expected_maker_2bp_net:
        out["maker_2bp_net"] = {"expected": candidate.expected_maker_2bp_net, "actual": metrics.get("expectancy_per_trade")}
    return out


def _spec(candidate: R4Candidate, strategy: Strategy, bars: list[Bar], cfg: R4PromotionConfig) -> dict:
    return {
        "hypothesis": f"R4 promotion protocol for {candidate.promoted_strategy_id}",
        "family": candidate.family,
        "strategy_ref": {"strategy_id": candidate.promoted_strategy_id, "base_strategy_id": strategy.strategy_id, "engine": strategy.params.get("engine", "ma"), "symbol": strategy.symbol, "timeframe": strategy.timeframe},
        "params_diff": {"hold_multiplier": candidate.hold_multiplier, "max_hold_bars": candidate.max_hold_bars, "stop_target_scale": candidate.stop_target_scale, "primary_bp": cfg.primary_bp, "secondary_bp": cfg.secondary_bp},
        "data_range": _data_range(bars),
        "windows": {"train_days": cfg.walkforward.train_days, "validate_days": cfg.walkforward.validate_days, "step_days": cfg.walkforward.step_days, "holdout_days": cfg.walkforward.holdout_days},
        "notes": [FUNDING_FOOTNOTE, "R4 pre-registered promotion protocol"],
    }


def _pin_to_r3_range(output_root: Path, bars: list[Bar]) -> list[Bar]:
    ends = []
    for candidate in CANDIDATES:
        path = output_root / "lab" / "experiments" / f"R3_{candidate.base_strategy_id}_h{candidate.hold_multiplier}x_s{str(candidate.stop_target_scale).replace('.', 'p')}x.json"
        rows = load_json(path)
        if rows and rows[0].get("data_range", {}).get("end"):
            ends.append(str(rows[0]["data_range"]["end"]))
    if not ends:
        return bars
    cutoff = min(_parse_ts(value) for value in ends)
    return [bar for bar in bars if _parse_ts(bar.timestamp) <= cutoff]


def _closing_sentence(row: dict) -> str:
    if row.get("paper_eligible"):
        return f"{row['promoted_strategy_id']} is paper-eligible because Phase-1 primary gates and the holdout criteria both passed."
    if row.get("holdout", {}).get("status") == "forfeited":
        return f"{row['promoted_strategy_id']} is not paper-eligible because Phase-1 primary gates failed, so the holdout shot was not consumed."
    return f"{row['promoted_strategy_id']} is not paper-eligible because the pre-registered holdout criteria failed."


def _gate_summary(row: dict) -> str:
    gates = row.get("objective", {}).get("gates", {})
    if not gates:
        return "n/a"
    return ", ".join(f"{name}={'pass' if item.get('passed') else 'fail'}" for name, item in gates.items())


def _regime_summary(row: dict) -> str:
    slices = row.get("regime_slices", {})
    non_negative = sum(1 for item in slices.values() if item.get("expectancy_per_trade") is not None and float(item["expectancy_per_trade"]) >= 0)
    return f"{non_negative}/3 non-negative"


def _metric(row: dict, key: str) -> Any:
    return row.get("metrics", {}).get(key)


def _ci(row: dict) -> str:
    ci = row.get("bootstrap_ci", {})
    if ci.get("low") is None:
        return "n/a"
    return f"[{_fmt_money(ci.get('low'))}, {_fmt_money(ci.get('high'))}], width {_fmt_money(ci.get('width'))}"


def _invalid_holdout(reason: str = "invalid") -> dict:
    return {"status": "invalid", "passed": False, "verdict": "fail", "reason": reason, "metrics": {}, "bootstrap_ci": {"level": 0.95, "low": None, "high": None, "width": None}, "objective": {"passed": False, "metrics": {}}}


def _error_row(candidate: R4Candidate, reason: str) -> dict:
    return {"candidate": candidate.code, "family": candidate.family, "base_strategy_id": candidate.base_strategy_id, "promoted_strategy_id": candidate.promoted_strategy_id, "status": "invalid", "reason": reason, "anchor_status": "n/a", "phase1": {}, "holdout": {"status": "forfeited", "reason": reason}, "paper_eligible": False, "closing_sentence": f"{candidate.promoted_strategy_id} is not paper-eligible because {reason}."}


def _anchor_report(row: dict) -> str:
    return f"# R4 Anchor Divergence\n\n- {FUNDING_FOOTNOTE}\n- {row['candidate']}: {row['reason']}\n"


def _block_key(value: str) -> str:
    ts = _parse_ts(value)
    return ts.replace(hour=(ts.hour // 4) * 4, minute=0, second=0, microsecond=0).isoformat()


def _parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0)
