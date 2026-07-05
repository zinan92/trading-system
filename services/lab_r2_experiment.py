"""R2 oracle-label and meta-model experiment orchestration."""

from __future__ import annotations

from pathlib import Path

from schemas.market_data import Bar
from services.journal_store import write_json
from services.lab_chan_replay import run_chan_addendum
from services.lab_evaluator import resample_bars
from services.lab_features import build_feature_rows
from services.lab_labels import TripleBarrierConfig, label_base_rates, triple_barrier_labels, zigzag_swing_labels
from services.lab_meta_model import MetaModelConfig, evaluate_meta_rule
from services.lab_r1_scan import _data_range, _fmt_float, _fmt_money, _write_report
from services.lab_registry import LabRegistry

SCHEMES = ("primary", "zigzag")
MODELS = ("logistic_l2", "gbt_depth3")


def run_r2_experiment(output_root: Path, registry: LabRegistry, bars_1m: list[Bar], *, horizon_minutes: int) -> dict:
    bars_5m = resample_bars(bars_1m, "5m")
    label_config = TripleBarrierConfig(horizon_minutes=horizon_minutes)
    primary = triple_barrier_labels(bars_5m, label_config)
    zigzag = zigzag_swing_labels(bars_5m)
    rows = build_feature_rows(bars_5m, primary, zigzag)
    dataset_stats = {
        "bars_5m": len(bars_5m),
        "feature_rows": len(rows),
        "primary_base_rates": label_base_rates(primary),
        "zigzag_base_rates": label_base_rates(zigzag),
        "horizon_minutes": horizon_minutes,
    }
    write_json(output_root / "lab" / "datasets" / "R2_feature_snapshot.json", rows)
    write_json(output_root / "lab" / "datasets" / "R2_dataset_stats.json", [dataset_stats])
    model_config = MetaModelConfig()
    results = []
    for scheme in SCHEMES:
        for model in MODELS:
            for barrier in model_config.barriers:
                exp_id = f"R2_{scheme}_{model}_b{str(barrier).replace('.', 'p')}"
                family = f"r2_meta_{scheme}_{model}"
                registry.start(_spec(exp_id, family, scheme, model, barrier, bars_1m, horizon_minutes), exp_id=exp_id)
                payload = evaluate_meta_rule(rows, bars_5m, scheme=scheme, model_kind=model, barrier=barrier, config=model_config)
                row = payload["row"]
                entry = registry.finalize(
                    exp_id,
                    status="valid" if payload["status"] in {"valid", "insufficient_sample"} else "invalid",
                    results={
                        "cost_grid_results": payload.get("cost_grid_results", {}),
                        "objective": {"r2_meta": row, "calibration": payload.get("calibration", {})},
                        "data_coverage": _data_range(bars_1m),
                        "r2_summary": row,
                        "model_report": {"calibration": payload.get("calibration", {}), "feature_importances": payload.get("feature_importances", [])},
                    },
                    notes=[] if payload["status"] in {"valid", "insufficient_sample"} else [row.get("reason", "invalid")],
                )
                results.append({**row, "calibration": payload.get("calibration", {}), "feature_importances": payload.get("feature_importances", []), "registry_status": entry["status"]})
    chan = run_chan_addendum(output_root, registry, bars_1m)
    r1_top = _r1_top_rows(output_root)
    verdict = _verdict(results, r1_top)
    report = render_r2_report(dataset_stats, results, r1_top, verdict, total_trials=len(results))
    report_path = _write_report(output_root, "R2_meta_model", report)
    write_json(output_root / "lab" / "reports" / "R2_meta_model.json", [{"dataset_stats": dataset_stats, "results": results, "verdict": verdict, "chan": chan}])
    return {"status": "valid", "dataset_stats": dataset_stats, "results": results, "verdict": verdict, "chan": chan, "report": str(report_path)}


def render_r2_report(dataset_stats: dict, results: list[dict], r1_top: list[dict], verdict: dict, *, total_trials: int) -> str:
    lines = [
        "# R2 Meta Model",
        "",
        f"- Total R2 trials: {total_trials}",
        "- Best-of-N caveat: every scheme/model/barrier combo is exploratory and registered before inspection.",
        "- Holdout policy: R2 does not consume holdout.",
        f"- Feature rows: {dataset_stats['feature_rows']} all 5m bars; negatives included.",
        f"- Primary base rates: {dataset_stats['primary_base_rates']['rates']}",
        f"- Zigzag base rates: {dataset_stats['zigzag_base_rates']['rates']}",
        "",
        "|scheme|model|barrier|OOS trades|gross bp/trip|break-even bp/side|maker 2bp net/trade|max DD|sortino@2bp|Brier|status|",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in sorted(results, key=lambda item: -float(item.get("maker_2bp_expectancy_per_trade") or -999)):
        cal = row.get("calibration", {})
        lines.append(f"|{row['scheme']}|{row['model']}|{row['barrier']}|{row.get('oos_trades') or 'n/a'}|{_fmt_float(row.get('gross_bp_per_trip'))}|{_fmt_float(row.get('break_even_bp'))}|{_fmt_money(row.get('maker_2bp_expectancy_per_trade'))}|{_fmt_float(row.get('max_drawdown_pct'))}|{_fmt_float(row.get('sortino_at_2bp'))}|{_fmt_float(cal.get('brier'))}|{row['status']}|")
    comparable = _comparable_results(results)
    best = max(comparable, key=lambda item: float(item.get("maker_2bp_expectancy_per_trade") or -999)) if comparable else {}
    lines.extend(["", "## Best Comparable R2 Rule", ""])
    if best:
        lines.append(f"- {best['scheme']} / {best['model']} / barrier {best['barrier']}: maker net {_fmt_money(best.get('maker_2bp_expectancy_per_trade'))}, gross {_fmt_float(best.get('gross_bp_per_trip'))} bp/trip.")
        lines.append(f"- Top importances: {best.get('feature_importances', [])[:8]}")
        lines.append(f"- Calibration bins: {best.get('calibration', {}).get('bins', [])[:8]}")
    lines.extend(["", "## R1 Top-3 Comparison", ""])
    for row in r1_top:
        lines.append(f"- {row.get('strategy')}: maker net {_fmt_money(row.get('maker_2bp_expectancy_per_trade'))}, gross {_fmt_float(row.get('gross_bp_per_trip'))} bp/trip, trades {row.get('oos_trades')}.")
    lines.extend(["", "## Verdict", "", verdict["sentence"], ""])
    return "\n".join(lines)


def _spec(exp_id: str, family: str, scheme: str, model: str, barrier: float, bars: list[Bar], horizon_minutes: int) -> dict:
    return {
        "exp_id": exp_id,
        "hypothesis": f"R2 meta-model {scheme}/{model} at p>={barrier}",
        "family": family,
        "strategy_ref": {"strategy_id": "r2_meta_model", "engine": model, "symbol": "GOLD", "timeframe": "5m"},
        "params_diff": {"scheme": scheme, "model": model, "barrier": barrier, "horizon_minutes": horizon_minutes},
        "data_range": _data_range(bars),
        "notes": ["R2 exploration only; holdout remains unconsumed"],
    }


def _r1_top_rows(output_root: Path) -> list[dict]:
    from services.journal_store import load_json

    rows = load_json(output_root / "lab" / "reports" / "R1_strategy_scan.json")
    if not rows:
        return []
    candidates = [
        row
        for row in rows[0].get("rows", [])
        if row.get("gross_bp_per_trip") is not None and int(row.get("oos_trades") or 0) >= 100
    ]
    return sorted(candidates, key=lambda row: -float(row.get("gross_bp_per_trip") or -999))[:3]


def _verdict(results: list[dict], r1_top: list[dict]) -> dict:
    best_r2 = max(_comparable_results(results), key=lambda row: float(row["maker_2bp_expectancy_per_trade"]), default={})
    best_r1 = max((row for row in r1_top if row.get("maker_2bp_expectancy_per_trade") is not None), key=lambda row: float(row["maker_2bp_expectancy_per_trade"]), default={})
    r2_value = float(best_r2.get("maker_2bp_expectancy_per_trade") or -999)
    r1_value = float(best_r1.get("maker_2bp_expectancy_per_trade") or -999)
    beats = r2_value > r1_value
    return {
        "beats_r1_top3": beats,
        "best_r2": best_r2,
        "best_r1_top3": best_r1,
        "sentence": f"Reverse-engineering {'beats' if beats else 'does not beat'} the R1 hand-written top-3 on maker-2bp expectancy over this slice: best R2 {_fmt_money(r2_value)} vs best R1 {_fmt_money(r1_value)}.",
    }


def _comparable_results(results: list[dict]) -> list[dict]:
    return [
        row
        for row in results
        if row.get("status") == "valid"
        and int(row.get("oos_trades") or 0) >= 100
        and row.get("maker_2bp_expectancy_per_trade") is not None
    ]
