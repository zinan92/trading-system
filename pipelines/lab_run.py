from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json
from services.lab_evaluator import (
    LabSimulationConfig,
    equity_curve,
    evaluate_signals,
    metrics_from_trades,
    regenerate_macd_signals,
)
from services.lab_objective import ObjectiveConfig, evaluate_objective
from services.lab_promotion import record_paper_eligibility
from services.lab_regimes import build_coverage_report, label_regimes, write_regime_artifact
from services.lab_registry import LabRegistry
from services.lab_walkforward import HoldoutQuarantine, WalkForwardConfig, build_windows, load_gold_1m_bars
from services.market_view import direction_bias_from_score

COST_GRID = [0.0, 0.5, 1.0, 2.0, 5.0]
BINANCE_REALITY = {
    "maker_bp": 2.0,
    "taker_bp": 5.0,
    "assumption": "VIP0, no BNB fee discount; fee page is dynamic, authenticated commission endpoint unavailable without keys",
    "sources": [
        "https://www.binance.com/en/fee/futureFee",
        "https://www.binance.com/en/support/faq/detail/360033544231",
        "https://developers.binance.com/docs/derivatives/usds-margined-futures/account/rest-api/User-Commission-Rate",
    ],
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Strategy Lab experiment registry and runner")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    show = sub.add_parser("show")
    show.add_argument("exp_id")
    compare = sub.add_parser("compare")
    compare.add_argument("left")
    compare.add_argument("right")
    run = sub.add_parser("run")
    run.add_argument("experiment", choices=["coverage", "e1", "e2", "e3", "e4", "all"])
    run.add_argument("--start", default="")
    run.add_argument("--end", default="")
    args = parser.parse_args()

    output_root, market_db = _paths()
    registry = LabRegistry(output_root)
    if args.command == "list":
        print(json.dumps(registry.entries(), indent=2, ensure_ascii=False))
        return
    if args.command == "show":
        print(json.dumps(registry.load(args.exp_id), indent=2, ensure_ascii=False))
        return
    if args.command == "compare":
        print(json.dumps({"left": _summary(registry.load(args.left)), "right": _summary(registry.load(args.right))}, indent=2, ensure_ascii=False))
        return

    bars = load_gold_1m_bars(market_db, args.start or None, args.end or None)
    if args.experiment in {"coverage", "all"}:
        payload = write_regime_artifact(output_root, _range_name(bars), bars)
        _write_json_report(output_root, "coverage", payload)
    if args.experiment in {"e1", "all"}:
        _run_e1(output_root, registry, bars)
    if args.experiment in {"e2", "all"}:
        _run_e2(output_root, registry, bars)
    if args.experiment in {"e3", "all"}:
        _run_e3(output_root, registry, bars)
    if args.experiment in {"e4", "all"}:
        _run_e4(output_root, registry, bars)


def _run_e1(output_root: Path, registry: LabRegistry, bars: list) -> dict:
    exp_id = "E1_macd_baseline_cost_curve"
    spec = _spec(exp_id, "Does 1m MACD baseline have positive expectancy after costs?", "macd_baseline", bars)
    registry.start(spec, exp_id=exp_id)
    cost_results = _cost_curve(bars, maker_limit_entry=False)
    taker_full = _evaluate_cost(bars, BINANCE_REALITY["taker_bp"], maker_limit_entry=False)
    stress = _evaluate_cost(bars, BINANCE_REALITY["taker_bp"] * 1.5, maker_limit_entry=False)
    holdout = _evaluate_holdout(registry, exp_id, bars, BINANCE_REALITY["taker_bp"], maker_limit_entry=False)
    objective = {
        "walkforward": _objective_from_cost_result(taker_full, stress, _regime_slices_from_trades(taker_full.get("trades", []), bars)),
        "holdout": _objective_from_cost_result(holdout, holdout, _regime_slices_from_trades(holdout.get("trades", []), bars)),
    }
    status = _status_with_coverage("valid" if any(item.get("status") == "valid" for item in cost_results.values()) else "invalid", bars)
    registry.finalize(exp_id, status=status, results={"objective": objective, "cost_grid_results": cost_results})
    eligibility = record_paper_eligibility(output_root, "gold_1m_macd")
    payload = {
        "exp_id": exp_id,
        "data_coverage": _data_coverage(bars),
        "cost_grid_results": cost_results,
        "break_even_bp": _break_even_bp(cost_results),
        "objective": objective,
        "regime_slice_results": _regime_slices_from_trades(taker_full.get("trades", []), bars),
        "holdout": _compact_eval(holdout),
        "paper_eligibility": eligibility,
        "binance_reality": BINANCE_REALITY,
    }
    report = _write_markdown_report(output_root, exp_id, _report_markdown("E1 macd_baseline_cost_curve", registry, "macd_baseline", payload))
    return registry.finalize(exp_id, status=status, results={**payload, "reports": {"markdown": str(report)}})


def _run_e2(output_root: Path, registry: LabRegistry, bars: list) -> dict:
    exp_id = "E2_direction_filter_ab"
    family = "direction_filter_ab"
    registry.start(_spec(exp_id, "Does DirectionBiasGate add alpha over MACD?", family, bars), exp_id=exp_id)
    views = _market_views(output_root)
    baseline_signals = regenerate_macd_signals(bars)
    retro_signals = [item for item in baseline_signals if item.get("timestamp", "")[:10] in views]
    filtered_signals = _apply_direction_filter(retro_signals, views)
    cfg = LabSimulationConfig(cost_bp_per_side=BINANCE_REALITY["taker_bp"])
    baseline = evaluate_signals(bars, retro_signals, cfg) if retro_signals else {"status": "invalid", "reason": "no_direction_history_signals"}
    filtered = evaluate_signals(bars, filtered_signals, cfg) if filtered_signals else {"status": "invalid", "reason": "no_filtered_signals"}
    payload = {
        "exp_id": exp_id,
        "data_coverage": _data_coverage(bars),
        "market_view_days": len(views),
        "direction_history_thin": len(views) < 60,
        "retro_signal_count": len(retro_signals),
        "filtered_signal_count": len(filtered_signals),
        "baseline": _compact_eval(baseline),
        "filtered": _compact_eval(filtered),
        "alpha_expectancy_per_trade": _expectancy(filtered) - _expectancy(baseline) if baseline.get("status") == filtered.get("status") == "valid" else None,
        "prospective_logging": "existing DirectionBiasGate writes dated outputs/direction_bias_decisions; lab does not alter production gates",
    }
    report = _write_markdown_report(output_root, exp_id, _report_markdown("E2 direction_filter_ab", registry, family, payload))
    return registry.finalize(
        exp_id,
        status=_status_with_coverage("valid" if baseline.get("status") == "valid" or filtered.get("status") == "valid" else "invalid", bars),
        results={"reports": {"markdown": str(report)}, "objective": payload, "data_coverage": payload["data_coverage"]},
        notes=["direction history is thin"] if len(views) < 60 else [],
    )


def _run_e3(output_root: Path, registry: LabRegistry, bars: list) -> dict:
    exp_id = "E3_gold_regime_share"
    family = "gold_regime_share"
    registry.start(_spec(exp_id, "What fraction of time is gold in a tradeable directional regime?", family, bars), exp_id=exp_id)
    regimes = label_regimes(bars)
    coverage = build_coverage_report(bars)
    write_json(output_root / "lab" / "regimes" / f"{_range_name(bars)}.json", [{"coverage": coverage, "regimes": regimes}])
    payload = {"exp_id": exp_id, "data_coverage": _data_coverage(bars), "coverage": coverage, "regimes": regimes}
    report = _write_markdown_report(output_root, exp_id, _report_markdown("E3 gold_regime_share", registry, family, payload))
    return registry.finalize(exp_id, status=_status_with_coverage(regimes.get("status", "invalid"), bars), results={"reports": {"markdown": str(report)}, "objective": payload, "data_coverage": payload["data_coverage"]})


def _run_e4(output_root: Path, registry: LabRegistry, bars: list) -> dict:
    exp_id = "E4_maker_vs_taker"
    family = "maker_vs_taker"
    registry.start(_spec(exp_id, "How much does maker-vs-taker execution change E1?", family, bars), exp_id=exp_id)
    taker = _evaluate_cost(bars, BINANCE_REALITY["taker_bp"], maker_limit_entry=False)
    maker = _evaluate_cost(bars, BINANCE_REALITY["maker_bp"], maker_limit_entry=True)
    payload = {
        "exp_id": exp_id,
        "data_coverage": _data_coverage(bars),
        "taker": _compact_eval(taker),
        "maker_limit": _compact_eval(maker),
        "expectancy_delta": _expectancy(maker) - _expectancy(taker) if maker.get("status") == taker.get("status") == "valid" else None,
        "fill_assumption": "maker entry fills only if the next bar trades through the signal-close limit price",
        "binance_reality": BINANCE_REALITY,
    }
    report = _write_markdown_report(output_root, exp_id, _report_markdown("E4 maker_vs_taker", registry, family, payload))
    return registry.finalize(
        exp_id,
        status=_status_with_coverage("valid" if maker.get("status") == "valid" or taker.get("status") == "valid" else "invalid", bars),
        results={"reports": {"markdown": str(report)}, "objective": payload, "data_coverage": payload["data_coverage"]},
    )


def _cost_curve(bars: list, maker_limit_entry: bool) -> dict:
    results = {f"{bp:g}bp": _compact_eval(_evaluate_cost(bars, bp, maker_limit_entry=maker_limit_entry)) for bp in COST_GRID}
    results["binance_maker"] = _compact_eval(_evaluate_cost(bars, BINANCE_REALITY["maker_bp"], maker_limit_entry=True))
    results["binance_taker"] = _compact_eval(_evaluate_cost(bars, BINANCE_REALITY["taker_bp"], maker_limit_entry=False))
    return results


def _evaluate_cost(bars: list, bp: float, maker_limit_entry: bool) -> dict:
    cfg = WalkForwardConfig()
    quarantine = HoldoutQuarantine(bars, cfg)
    all_trades: list[dict] = []
    invalid_reasons: list[str] = []
    for window in build_windows(bars, cfg):
        start = datetime.fromisoformat(window["validate_start"])
        end = datetime.fromisoformat(window["validate_end"])
        window_bars = quarantine.public_bars_between(start, end)
        signals = regenerate_macd_signals(window_bars)
        result = evaluate_signals(window_bars, signals, LabSimulationConfig(cost_bp_per_side=bp, maker_limit_entry=maker_limit_entry))
        if result.get("status") == "valid":
            all_trades.extend(result["trades"])
        else:
            invalid_reasons.append(str(result.get("reason", "invalid")))
    if not all_trades:
        return {"status": "invalid", "reason": ",".join(sorted(set(invalid_reasons))) or "zero_trades", "window_count": len(build_windows(bars, cfg))}
    equity = equity_curve(all_trades, 10_000)
    return {
        "status": "valid" if not invalid_reasons else "invalid",
        "reason": "pass" if not invalid_reasons else ",".join(sorted(set(invalid_reasons))),
        "window_count": len(build_windows(bars, cfg)),
        "trades": all_trades,
        "equity": equity,
        "metrics": metrics_from_trades(all_trades, equity, LabSimulationConfig(cost_bp_per_side=bp, maker_limit_entry=maker_limit_entry)),
    }


def _evaluate_holdout(registry: LabRegistry, exp_id: str, bars: list, bp: float, maker_limit_entry: bool) -> dict:
    quarantine = HoldoutQuarantine(bars, WalkForwardConfig())
    holdout_bars = quarantine.consume_holdout(registry, exp_id)
    return _evaluate_direct(holdout_bars, bp, maker_limit_entry)


def _evaluate_direct(bars: list, bp: float, maker_limit_entry: bool) -> dict:
    signals = regenerate_macd_signals(bars)
    return evaluate_signals(bars, signals, LabSimulationConfig(cost_bp_per_side=bp, maker_limit_entry=maker_limit_entry))


def _objective_from_cost_result(base: dict, stress: dict, regime_slice_results: dict[str, dict] | None = None) -> dict:
    if base.get("status") != "valid":
        return {"status": "invalid", "reason": base.get("reason", "invalid"), "passed": False}
    return evaluate_objective(
        base.get("trades", []),
        base.get("equity", []),
        ObjectiveConfig(max_drawdown_pct=8.0),
        stress_expectancy_per_trade=_expectancy(stress) if stress.get("status") == "valid" else None,
        regime_slice_results=regime_slice_results or {},
    )


def _regime_slices_from_trades(trades: list[dict], bars: list) -> dict[str, dict]:
    regimes = label_regimes(bars).get("four_hour_blocks", [])
    regime_by_bucket = {item["key"]: f"{item['volatility_bucket']}|{item['trend_bucket']}" for item in regimes}
    grouped: dict[str, list[float]] = {}
    for trade in trades:
        bucket = _four_hour_bucket(str(trade.get("exit_timestamp", "")))
        key = regime_by_bucket.get(bucket, "unmatched")
        grouped.setdefault(key, []).append(float(trade.get("net_pnl", 0)))
    return {
        key: {
            "trade_count": len(values),
            "net_pnl": round(sum(values), 6),
            "expectancy_per_trade": round(sum(values) / len(values), 6) if values else 0.0,
        }
        for key, values in sorted(grouped.items())
    }


def _apply_direction_filter(signals: list[dict], views: dict[str, dict]) -> list[dict]:
    kept = []
    for signal in signals:
        view = views.get(str(signal.get("timestamp", ""))[:10], {})
        bias = direction_bias_from_score(view.get("direction_score", 50))
        direction = str(signal.get("direction", ""))
        if direction in set(bias.get("blocked_directions", [])):
            continue
        if (bias.get("bias") == "short_bias" and direction == "long") or (bias.get("bias") == "long_bias" and direction == "short"):
            continue
        kept.append(signal)
    return kept


def _market_views(output_root: Path) -> dict[str, dict]:
    out = {}
    for path in sorted((output_root / "market_views").glob("20*.json")):
        rows = load_json(path)
        if rows:
            out[path.stem] = rows[0]
    return out


def _data_coverage(bars: list) -> dict:
    if not bars:
        return {"observed_days": 0.0, "required_days": 365, "meets_12_month_requirement": False}
    start = datetime.fromisoformat(bars[0].timestamp.replace("Z", "+00:00"))
    end = datetime.fromisoformat(bars[-1].timestamp.replace("Z", "+00:00"))
    observed_days = (end - start).total_seconds() / 86_400
    return {
        "start": bars[0].timestamp,
        "end": bars[-1].timestamp,
        "observed_days": round(observed_days, 6),
        "required_days": 365,
        "meets_12_month_requirement": observed_days >= 365,
        "acceptance_blocker": observed_days < 365,
    }


def _status_with_coverage(status: str, bars: list) -> str:
    if _data_coverage(bars).get("acceptance_blocker"):
        return "invalid"
    return status


def _four_hour_bucket(timestamp: str) -> str:
    ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    ts = ts.astimezone(timezone.utc).replace(hour=(ts.hour // 4) * 4, minute=0, second=0, microsecond=0)
    return ts.isoformat()


def _break_even_bp(cost_results: dict) -> float | None:
    points = []
    for label, result in cost_results.items():
        if not label.endswith("bp") or result.get("status") != "valid":
            continue
        bp = float(label.removesuffix("bp"))
        points.append((bp, float(result.get("metrics", {}).get("expectancy_per_trade", 0))))
    points.sort()
    if not points:
        return None
    if points[0][1] <= 0:
        return 0.0
    for (left_bp, left_exp), (right_bp, right_exp) in zip(points, points[1:]):
        if right_exp <= 0:
            slope = (right_exp - left_exp) / (right_bp - left_bp)
            return round(left_bp - left_exp / slope, 6) if slope else left_bp
    return round(points[-1][0], 6)


def _compact_eval(result: dict) -> dict:
    return {
        "status": result.get("status"),
        "reason": result.get("reason", ""),
        "window_count": result.get("window_count"),
        "trade_count": result.get("metrics", {}).get("trade_count", len(result.get("trades", []))),
        "metrics": result.get("metrics", {}),
    }


def _expectancy(result: dict) -> float:
    return float(result.get("metrics", {}).get("expectancy_per_trade", 0) or 0)


def _spec(exp_id: str, hypothesis: str, family: str, bars: list) -> dict:
    return {
        "exp_id": exp_id,
        "hypothesis": hypothesis,
        "family": family,
        "strategy_ref": {"strategy_id": "gold_1m_macd", "engine": "macd", "symbol": "GOLD", "timeframe": "1m"},
        "params_diff": {},
        "data_range": {"start": bars[0].timestamp if bars else "", "end": bars[-1].timestamp if bars else "", "bars": len(bars)},
        "windows": {"train_days": 60, "validate_days": 14, "step_days": 14, "holdout_days": 28},
    }


def _report_markdown(title: str, registry: LabRegistry, family: str, payload: dict) -> str:
    trial_count = registry.trial_count(family)
    return "\n".join([
        f"# {title}",
        "",
        f"- Trial count for family `{family}`: {trial_count}",
        f"- Generated at: {_now()}",
        "",
        "```json",
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True),
        "```",
        "",
    ])


def _write_markdown_report(output_root: Path, exp_id: str, body: str) -> Path:
    path = output_root / "lab" / "reports" / f"{exp_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(body)
    os.replace(tmp_name, path)
    return path


def _write_json_report(output_root: Path, name: str, payload: dict) -> None:
    write_json(output_root / "lab" / "reports" / f"{name}.json", [payload])


def _summary(entry: dict) -> dict:
    return {
        "exp_id": entry.get("exp_id"),
        "family": entry.get("family"),
        "status": entry.get("status"),
        "objective": entry.get("objective"),
    }


def _range_name(bars: list) -> str:
    if not bars:
        return "empty"
    return f"{bars[0].timestamp[:10]}_{bars[-1].timestamp[:10]}".replace("-", "")


def _paths() -> tuple[Path, Path]:
    config = load_pipeline_config()
    output_root = Path(os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(ROOT / config.get("output_root", "outputs"))))
    market_db = Path(os.getenv("TRADING_ORCHESTRATOR_MARKET_DB", str(ROOT / config.get("local_market_db", "data/market_data.db"))))
    return output_root, market_db


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


if __name__ == "__main__":
    main()
