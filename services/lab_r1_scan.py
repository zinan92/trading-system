"""R1 Strategy Lab triage scan.

R1 scans every registered strategy with the same walk-forward research path used
by E1, but it never consumes holdout. The scan includes each strategy's own
``Strategy.signal_engine()`` filters and excludes runner-level gates.

5m strategies are replayed by resampling the 1m SQLite research bars in memory
using UTC epoch-floor buckets. This matches ``MarketStore.load_aggregated_bars_between``:
timestamps are the bucket start, OHLC is first/max/min/last, and volume is
summed. Warmup is owned by each engine's configured ``min_bars``; filtered
engines apply filters causally on the per-signal historical window.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from schemas.market_data import Bar
from services.journal_store import write_json
from services.lab_evaluator import (
    LabSimulationConfig,
    equity_curve,
    evaluate_signals,
    metrics_from_trades,
    regenerate_strategy_signals,
)
from services.lab_registry import LabRegistry
from services.lab_walkforward import HoldoutQuarantine, WalkForwardConfig, build_windows
from services.strategy_registry import Strategy, StrategyRegistry


@dataclass(frozen=True)
class R1ScanConfig:
    cost_grid: tuple[float, ...] = (0.0, 0.5, 1.0, 2.0, 5.0)
    maker_bp: float = 2.0
    taker_bp: float = 5.0
    min_oos_trades: int = 100
    walkforward: WalkForwardConfig = field(default_factory=WalkForwardConfig)


def run_r1_scan(
    output_root: Path,
    registry: LabRegistry,
    bars: list[Bar],
    *,
    strategies: list[Strategy] | None = None,
    config: R1ScanConfig | None = None,
    anchor_expected: dict | None = None,
) -> dict:
    cfg = config or R1ScanConfig()
    selected = strategies if strategies is not None else StrategyRegistry().strategies()
    rows: list[dict] = []
    artifacts: dict[str, dict] = {}
    for strategy in selected:
        exp_id = f"R1_{strategy.strategy_id}"
        registry.start(_r1_spec(strategy, bars, cfg), exp_id=exp_id)
        payload = evaluate_r1_strategy(strategy, bars, cfg)
        entry_status = "valid" if payload["status"] in {"valid", "insufficient_sample"} else "invalid"
        report_row = payload["report_row"]
        report_row["registry_status"] = entry_status
        entry = registry.finalize(
            exp_id,
            status=entry_status,
            results={
                "cost_grid_results": payload.get("cost_grid_results", {}),
                "objective": {"r1_triage": report_row, "replay": payload.get("replay", {})},
                "break_even_bp": report_row.get("break_even_bp"),
                "data_coverage": _data_range(bars),
                "r1_summary": report_row,
                "replay": payload.get("replay", {}),
            },
            notes=payload.get("notes", []),
        )
        rows.append(report_row)
        artifacts[strategy.strategy_id] = entry
    divergence = _anchor_divergence(artifacts.get("gold_1m_macd", {}), anchor_expected or {})
    if divergence:
        body = render_r1_divergence_report(divergence)
        report_path = _write_report(output_root, "R1_strategy_scan", body)
        raise RuntimeError(f"R1 anchor divergence; see {report_path}: {divergence}")
    rows = sorted(rows, key=_leaderboard_key)
    report = render_r1_report(rows, total_trials=len(selected), min_oos_trades=cfg.min_oos_trades)
    report_path = _write_report(output_root, "R1_strategy_scan", report)
    write_json(output_root / "lab" / "reports" / "R1_strategy_scan.json", [{"total_trials": len(selected), "rows": rows}])
    return {"status": "valid", "total_trials": len(selected), "rows": rows, "report": str(report_path)}


def evaluate_r1_strategy(strategy: Strategy, bars: list[Bar], config: R1ScanConfig | None = None) -> dict:
    cfg = config or R1ScanConfig()
    replayed = _replay_strategy_windows(strategy, bars, cfg.walkforward)
    replay_summary = replayed["replay"]
    if replayed.get("status") == "not_replayable":
        row = _empty_row(strategy, "not_replayable", str(replayed.get("reason", "not_replayable")))
        return {"status": "not_replayable", "reason": row["reason"], "report_row": row, "replay": replay_summary, "notes": [row["reason"]]}
    cost_results: dict[str, dict] = {}
    for bp in cfg.cost_grid:
        label = f"{bp:g}bp"
        cost_results[label] = _compact_eval(_evaluate_replayed_windows(replayed["windows"], bp))
    row = _row_from_costs(strategy, cost_results, cfg)
    return {
        "status": row["status"],
        "reason": row["reason"],
        "cost_grid_results": cost_results,
        "report_row": row,
        "replay": replay_summary,
        "notes": [] if row["status"] in {"valid", "insufficient_sample"} else [row["reason"]],
    }


def render_r1_report(rows: list[dict], *, total_trials: int, min_oos_trades: int) -> str:
    headers = [
        "strategy",
        "timeframe",
        "OOS trades",
        "win rate",
        "gross bp/trip",
        "break-even bp/side",
        "maker 2bp net/trade",
        "taker 5bp net/trade",
        "max DD",
        "sortino@2bp",
        "status",
    ]
    lines = [
        "# R1 Strategy Scan",
        "",
        f"- Total R1 trials: {total_trials}",
        "- Multiple-testing caveat: this is a best-of-many triage screen; top rows can be inflated by scanning many hypotheses and are not promotion evidence.",
        "- Holdout policy: R1 does not consume holdout; registry entries must keep `holdout_consumed=false`.",
        "",
        "|" + "|".join(headers) + "|",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    for row in rows:
        lines.append(
            "|"
            + "|".join(
                [
                    str(row["strategy"]),
                    str(row["timeframe"]),
                    _fmt_int(row.get("oos_trades")),
                    _fmt_ratio_pct(row.get("win_rate")),
                    _fmt_float(row.get("gross_bp_per_trip")),
                    _fmt_float(row.get("break_even_bp")),
                    _fmt_money(row.get("maker_2bp_expectancy_per_trade")),
                    _fmt_money(row.get("taker_5bp_expectancy_per_trade")),
                    _fmt_percent_value(row.get("max_drawdown_pct")),
                    _fmt_float(row.get("sortino_at_2bp")),
                    str(row["status"]),
                ]
            )
            + "|"
        )
    qualified = [
        row
        for row in rows
        if row.get("gross_bp_per_trip") is not None
        and float(row["gross_bp_per_trip"]) > 2.0
        and int(row.get("oos_trades") or 0) >= min_oos_trades
    ]
    lines.extend(["", "## Verdict", ""])
    if qualified:
        lines.append(f"Strategies exceeding 2bp/trip gross with >= {min_oos_trades} OOS trades:")
        for row in qualified:
            lines.append(f"- {row['strategy']}: gross {row['gross_bp_per_trip']} bp/trip over {row['oos_trades']} trades.")
    else:
        lines.append(f"No strategy exceeded 2bp/trip gross with >= {min_oos_trades} OOS trades.")
    not_replayable = [row for row in rows if row.get("status") == "not_replayable"]
    if not_replayable:
        lines.extend(["", "## Not Replayable", ""])
        for row in not_replayable:
            lines.append(f"- {row['strategy']}: {row.get('reason', 'not_replayable')}")
    lines.append("")
    return "\n".join(lines)


def render_r1_divergence_report(divergence: dict) -> str:
    return "\n".join([
        "# R1 Strategy Scan Anchor Divergence",
        "",
        "The batch path did not reproduce E1 for `gold_1m_macd`; R1 stopped before publishing a leaderboard.",
        "",
        "```json",
        _json_dumps(divergence),
        "```",
        "",
    ])


def _replay_strategy_windows(strategy: Strategy, bars: list[Bar], walkforward: WalkForwardConfig) -> dict:
    quarantine = HoldoutQuarantine(bars, walkforward)
    windows = build_windows(bars, walkforward)
    replayed = []
    first_summary: dict | None = None
    for window in windows:
        start = datetime.fromisoformat(window["validate_start"])
        end = datetime.fromisoformat(window["validate_end"])
        window_bars = quarantine.public_bars_between(start, end)
        replay = regenerate_strategy_signals(strategy, window_bars)
        if first_summary is None:
            first_summary = _replay_summary(replay)
        if replay.get("status") == "not_replayable":
            return {"status": "not_replayable", "reason": replay.get("reason", "not_replayable"), "windows": [], "replay": first_summary}
        replayed.append({"window": window, "bars": replay["bars"], "signals": replay["signals"]})
    if first_summary is None:
        first_replay = regenerate_strategy_signals(strategy, HoldoutQuarantine(bars, walkforward).research_bars())
        first_summary = _replay_summary(first_replay)
        if first_replay.get("status") == "not_replayable":
            return {"status": "not_replayable", "reason": first_replay.get("reason", "not_replayable"), "windows": [], "replay": first_summary}
    return {"status": "valid", "reason": "pass", "windows": replayed, "replay": first_summary}


def _evaluate_replayed_windows(windows: list[dict], bp: float) -> dict:
    all_trades: list[dict] = []
    invalid_reasons: list[str] = []
    for replay in windows:
        result = evaluate_signals(replay["bars"], replay["signals"], LabSimulationConfig(cost_bp_per_side=bp))
        if result.get("status") == "valid":
            all_trades.extend(result["trades"])
        else:
            invalid_reasons.append(str(result.get("reason", "invalid")))
    if not all_trades:
        return {"status": "invalid", "reason": ",".join(sorted(set(invalid_reasons))) or "zero_trades", "window_count": len(windows)}
    equity = equity_curve(all_trades, 10_000)
    metrics = metrics_from_trades(all_trades, equity, LabSimulationConfig(cost_bp_per_side=bp))
    return {
        "status": "valid" if not invalid_reasons else "invalid",
        "reason": "pass" if not invalid_reasons else ",".join(sorted(set(invalid_reasons))),
        "window_count": len(windows),
        "trades": all_trades,
        "equity": equity,
        "metrics": metrics,
    }


def _row_from_costs(strategy: Strategy, cost_results: dict[str, dict], config: R1ScanConfig) -> dict:
    gross = cost_results.get("0bp", {})
    maker = cost_results.get(f"{config.maker_bp:g}bp", {})
    taker = cost_results.get(f"{config.taker_bp:g}bp", {})
    sortino_source = maker
    metrics = gross.get("metrics", {})
    if gross.get("status") == "not_replayable":
        return _empty_row(strategy, "not_replayable", str(gross.get("reason", "not_replayable")))
    status = "valid" if gross.get("status") == "valid" else "invalid"
    reason = str(gross.get("reason", "pass" if status == "valid" else "invalid"))
    trade_count = int(metrics.get("trade_count") or gross.get("trade_count") or 0)
    if status == "valid" and trade_count < config.min_oos_trades:
        status = "insufficient_sample"
        reason = f"oos_trades<{config.min_oos_trades}"
    return {
        "strategy": strategy.strategy_id,
        "timeframe": strategy.timeframe,
        "oos_trades": trade_count if metrics else None,
        "win_rate": metrics.get("win_rate"),
        "gross_bp_per_trip": metrics.get("expectancy_bp_on_notional"),
        "break_even_bp": break_even_bp(cost_results),
        "maker_2bp_expectancy_per_trade": maker.get("metrics", {}).get("expectancy_per_trade"),
        "taker_5bp_expectancy_per_trade": taker.get("metrics", {}).get("expectancy_per_trade"),
        "max_drawdown_pct": maker.get("metrics", {}).get("max_drawdown_pct"),
        "sortino_at_2bp": sortino_source.get("metrics", {}).get("sortino"),
        "status": status,
        "reason": reason,
    }


def break_even_bp(cost_results: dict[str, dict]) -> float | None:
    points: list[tuple[float, float]] = []
    for label, result in cost_results.items():
        if not label.endswith("bp") or result.get("status") != "valid":
            continue
        points.append((float(label.removesuffix("bp")), float(result.get("metrics", {}).get("expectancy_per_trade", 0))))
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


def _empty_row(strategy: Strategy, status: str, reason: str) -> dict:
    return {
        "strategy": strategy.strategy_id,
        "timeframe": strategy.timeframe,
        "oos_trades": None,
        "win_rate": None,
        "gross_bp_per_trip": None,
        "break_even_bp": None,
        "maker_2bp_expectancy_per_trade": None,
        "taker_5bp_expectancy_per_trade": None,
        "max_drawdown_pct": None,
        "sortino_at_2bp": None,
        "status": status,
        "reason": reason,
    }


def _r1_spec(strategy: Strategy, bars: list[Bar], config: R1ScanConfig) -> dict:
    return {
        "exp_id": f"R1_{strategy.strategy_id}",
        "hypothesis": f"R1 triage scan for {strategy.strategy_id}",
        "family": f"r1_scan_{strategy.strategy_id}",
        "strategy_ref": {
            "strategy_id": strategy.strategy_id,
            "engine": str(strategy.params.get("engine", "ma")),
            "symbol": strategy.symbol,
            "timeframe": strategy.timeframe,
        },
        "params_diff": {},
        "data_range": _data_range(bars),
        "windows": {
            "train_days": config.walkforward.train_days,
            "validate_days": config.walkforward.validate_days,
            "step_days": config.walkforward.step_days,
            "holdout_days": config.walkforward.holdout_days,
        },
        "notes": ["R1 triage only; holdout must remain unconsumed"],
    }


def _replay_summary(replay: dict) -> dict:
    return {
        "status": replay.get("status"),
        "reason": replay.get("reason"),
        "timeframe": replay.get("timeframe"),
        "engine_chain": replay.get("engine_chain", []),
        "warmup": replay.get("warmup", {}),
        "bars": len(replay.get("bars", [])),
        "signals": replay.get("signal_count", 0),
    }


def _anchor_divergence(anchor_entry: dict, expected: dict) -> dict:
    if not expected:
        return {}
    if not anchor_entry:
        return {"missing_anchor": True}
    out: dict[str, Any] = {}
    summary = anchor_entry.get("r1_summary", anchor_entry.get("objective", {}).get("r1_triage", {}))
    if summary.get("break_even_bp") != expected.get("break_even_bp"):
        out["break_even_bp"] = {"expected": expected.get("break_even_bp"), "actual": summary.get("break_even_bp")}
    if summary.get("oos_trades") != expected.get("trade_count"):
        out["trade_count"] = {"expected": expected.get("trade_count"), "actual": summary.get("oos_trades")}
    costs = anchor_entry.get("cost_grid_results", {})
    for label, expected_value in (expected.get("expectancy_per_trade") or {}).items():
        actual = costs.get(label, {}).get("metrics", {}).get("expectancy_per_trade")
        if actual != expected_value:
            out.setdefault("expectancy_per_trade", {})[label] = {"expected": expected_value, "actual": actual}
    return out


def _leaderboard_key(row: dict) -> tuple[int, float, str]:
    gross = row.get("gross_bp_per_trip")
    if gross is None:
        return (1, 0.0, str(row.get("strategy", "")))
    return (0, -float(gross), str(row.get("strategy", "")))


def _data_range(bars: list[Bar]) -> dict:
    return {"start": bars[0].timestamp if bars else "", "end": bars[-1].timestamp if bars else "", "bars": len(bars)}


def _write_report(output_root: Path, name: str, body: str) -> Path:
    path = output_root / "lab" / "reports" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(body)
    os.replace(tmp_name, path)
    return path


def _json_dumps(payload: dict) -> str:
    import json

    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)


def _fmt_int(value: Any) -> str:
    return "n/a" if value is None else str(int(value))


def _fmt_float(value: Any) -> str:
    return "n/a" if value is None else f"{float(value):.6f}"


def _fmt_money(value: Any) -> str:
    return "n/a" if value is None else f"{float(value):.6f}"


def _fmt_ratio_pct(value: Any) -> str:
    return "n/a" if value is None else f"{float(value) * 100:.2f}%"


def _fmt_percent_value(value: Any) -> str:
    return "n/a" if value is None else f"{float(value):.2f}%"
