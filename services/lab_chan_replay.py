"""Causal Chan replay for Strategy Lab addenda."""

from __future__ import annotations

import warnings
from datetime import datetime
from pathlib import Path

from schemas.market_data import Bar
from services.journal_store import load_json, write_json
from services.lab_evaluator import LabSimulationConfig, evaluate_signals
from services.lab_r1_scan import _compact_eval, _data_range, _fmt_float, _fmt_money, _replay_summary, _write_report, break_even_bp
from services.lab_registry import LabRegistry
from services.lab_walkforward import HoldoutQuarantine, WalkForwardConfig, build_windows
from services.strategy_registry import Strategy, StrategyRegistry

CHAN_STRATEGIES = ("gold_1m_chan", "gold_1m_chan_ungated", "gold_1m_chan_buy1", "gold_1m_chan_macdfilter")
MAX_CAUSAL_REPLAY_BARS = 1_500


class _DirectionOnly:
    def __init__(self, direction: str) -> None:
        self.direction = direction


def causal_chan_signals(strategy: Strategy, bars: list[Bar]) -> dict:
    engine = strategy.signal_engine()
    filters = list(getattr(engine, "filters", []) or [])
    base = getattr(engine, "base", engine)
    detect = getattr(base, "detect_points", None)
    if not callable(detect):
        return {"status": "not_replayable", "reason": "missing_detect_points", "signals": [], "bars": bars}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        points = detect(bars)
    index_by_norm = {_norm(bar.timestamp): index for index, bar in enumerate(bars)}
    signals = []
    for point in points:
        index = index_by_norm.get(_norm_chan(str(point.get("time", ""))))
        if index is None:
            continue
        direction = "long" if point.get("is_buy") else "short"
        stub = _DirectionOnly(direction)
        if filters and not all(signal_filter.accepts(stub, bars[: index + 1])[0] for signal_filter in filters):
            continue
        signals.append({"index": index, "direction": direction, "timestamp": bars[index].timestamp, "close": float(bars[index].close)})
    return {"status": "valid", "reason": "pass", "signals": signals, "bars": bars, "signal_count": len(signals), "timeframe": strategy.timeframe, "engine_chain": ["causal_chan"]}


def run_chan_addendum(output_root: Path, registry: LabRegistry, bars: list[Bar], *, cost_grid: tuple[float, ...] = (0.0, 0.5, 1.0, 2.0, 5.0)) -> dict:
    strategies = {strategy.strategy_id: strategy for strategy in StrategyRegistry().strategies()}
    rows = []
    parity = chan_live_parity(output_root, bars)
    for sid in CHAN_STRATEGIES:
        strategy = strategies.get(sid)
        if not strategy:
            continue
        exp_id = f"R1_CHAN_CAUSAL_{sid}"
        registry.start(_spec(strategy, bars), exp_id=exp_id)
        payload = _evaluate_chan(strategy, bars, cost_grid)
        row = payload["row"]
        entry_status = "valid" if row["status"] in {"valid", "insufficient_sample"} else "invalid"
        registry.finalize(
            exp_id,
            status=entry_status,
            results={"cost_grid_results": payload.get("cost_grid_results", {}), "objective": {"chan_causal_addendum": row}, "data_coverage": _data_range(bars), "chan_summary": row},
            notes=payload.get("notes", []),
        )
        rows.append(row)
    body = render_chan_addendum(rows, parity)
    path = _write_report(output_root, "R1_addendum_chan", body)
    write_json(output_root / "lab" / "reports" / "R1_addendum_chan.json", [{"rows": rows, "parity": parity}])
    return {"status": "valid", "rows": rows, "parity": parity, "report": str(path)}


def chan_live_parity(output_root: Path, bars: list[Bar]) -> dict:
    records = []
    for sid in CHAN_STRATEGIES:
        for path in sorted((output_root / "strategies" / sid / "signals").glob("20*.json")):
            for row in load_json(path):
                records.append({"strategy": sid, **row})
    directional = [row for row in records if row.get("direction") in {"long", "short"}]
    if not directional:
        return {"status": "no_directional_live_records", "records_checked": len(records), "matches": 0, "mismatches": 0, "details": []}
    missing = _missing_source_artifacts(output_root, directional)
    if missing:
        return {
            "status": "source_artifacts_missing",
            "records_checked": len(directional),
            "matches": 0,
            "mismatches": len(missing),
            "details": missing[:20],
        }
    details = []
    by_strategy = {strategy.strategy_id: strategy for strategy in StrategyRegistry().strategies()}
    for row in directional[-20:]:
        strategy = by_strategy.get(row["strategy"])
        if not strategy:
            continue
        generated_at = str(row.get("generated_at", ""))
        if not generated_at:
            continue
        end = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        window = [bar for bar in bars if datetime.fromisoformat(bar.timestamp) <= end][-7200:]
        replay = causal_chan_signals(strategy, window)
        hit = any(item["direction"] == row["direction"] for item in replay.get("signals", [])[-3:])
        details.append({"strategy": row["strategy"], "generated_at": generated_at, "live_direction": row["direction"], "match": hit})
    mismatches = [item for item in details if not item["match"]]
    return {"status": "match" if details and not mismatches else "mismatch", "records_checked": len(details), "matches": len(details) - len(mismatches), "mismatches": len(mismatches), "details": details}


def render_chan_addendum(rows: list[dict], parity: dict) -> str:
    lines = [
        "# R1 Addendum: Causal Chan Replay",
        "",
        f"- Live parity status: {parity.get('status')}; checked {parity.get('records_checked')} record(s), matches {parity.get('matches')}, mismatches {parity.get('mismatches')}.",
        "- Holdout policy: addendum does not consume holdout.",
        "",
        "|strategy|OOS trades|gross bp/trip|break-even bp/side|maker 2bp net/trade|status|reason|",
        "|---|---:|---:|---:|---:|---|---|",
    ]
    for row in rows:
        lines.append(f"|{row['strategy']}|{row.get('oos_trades') or 'n/a'}|{_fmt_float(row.get('gross_bp_per_trip'))}|{_fmt_float(row.get('break_even_bp'))}|{_fmt_money(row.get('maker_2bp_expectancy_per_trade'))}|{row['status']}|{row['reason']}|")
    lines.append("")
    return "\n".join(lines)


def _evaluate_chan(strategy: Strategy, bars: list[Bar], cost_grid: tuple[float, ...]) -> dict:
    if len(bars) > MAX_CAUSAL_REPLAY_BARS:
        row = _empty(strategy, f"not_replayable: causal chan replay requires {len(bars)} bars; bounded lab replay limit is {MAX_CAUSAL_REPLAY_BARS}")
        return {"row": row, "cost_grid_results": {}, "notes": [row["reason"]]}
    quarantine = HoldoutQuarantine(bars, WalkForwardConfig())
    replayed = []
    for window in build_windows(bars, WalkForwardConfig()):
        window_bars = quarantine.public_bars_between(datetime.fromisoformat(window["validate_start"]), datetime.fromisoformat(window["validate_end"]))
        replay = causal_chan_signals(strategy, window_bars)
        if replay.get("status") != "valid":
            row = _empty(strategy, str(replay.get("reason", "not_replayable")))
            return {"row": row, "notes": [row["reason"]], "replay": _replay_summary(replay)}
        replayed.append({"bars": replay["bars"], "signals": replay["signals"]})
    costs = {}
    for bp in cost_grid:
        all_trades = []
        invalid = []
        for item in replayed:
            result = evaluate_signals(item["bars"], item["signals"], LabSimulationConfig(cost_bp_per_side=bp))
            if result.get("status") == "valid":
                all_trades.extend(result["trades"])
            else:
                invalid.append(str(result.get("reason", "invalid")))
        if all_trades:
            from services.lab_evaluator import equity_curve, metrics_from_trades

            equity = equity_curve(all_trades, 10_000)
            costs[f"{bp:g}bp"] = _compact_eval({"status": "valid" if not invalid else "invalid", "reason": "pass" if not invalid else ",".join(sorted(set(invalid))), "trades": all_trades, "equity": equity, "metrics": metrics_from_trades(all_trades, equity, LabSimulationConfig(cost_bp_per_side=bp))})
        else:
            costs[f"{bp:g}bp"] = {"status": "invalid", "reason": ",".join(sorted(set(invalid))) or "zero_trades", "trade_count": 0, "metrics": {}}
    row = _row(strategy, costs)
    return {"row": row, "cost_grid_results": costs, "notes": [] if row["status"] == "valid" else [row["reason"]]}


def _row(strategy: Strategy, costs: dict) -> dict:
    gross = costs.get("0bp", {})
    maker = costs.get("2bp", {})
    metrics = gross.get("metrics", {})
    status = "valid" if gross.get("status") == "valid" else "invalid"
    trade_count = int(metrics.get("trade_count") or 0)
    if status == "valid" and trade_count < 100:
        status = "insufficient_sample"
    return {"strategy": strategy.strategy_id, "oos_trades": trade_count if metrics else None, "gross_bp_per_trip": metrics.get("expectancy_bp_on_notional"), "break_even_bp": break_even_bp(costs), "maker_2bp_expectancy_per_trade": maker.get("metrics", {}).get("expectancy_per_trade"), "status": status, "reason": "pass" if status == "valid" else str(gross.get("reason", "invalid"))}


def _empty(strategy: Strategy, reason: str) -> dict:
    return {"strategy": strategy.strategy_id, "oos_trades": None, "gross_bp_per_trip": None, "break_even_bp": None, "maker_2bp_expectancy_per_trade": None, "status": "not_replayable", "reason": reason}


def _spec(strategy: Strategy, bars: list[Bar]) -> dict:
    return {"hypothesis": f"Causal chan R1 addendum for {strategy.strategy_id}", "family": f"r1_chan_causal_{strategy.strategy_id}", "strategy_ref": {"strategy_id": strategy.strategy_id, "engine": "chan", "timeframe": strategy.timeframe}, "data_range": _data_range(bars), "notes": ["R1 addendum only; holdout remains unconsumed"]}


def _norm(value: str) -> str:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).strftime("%Y%m%d%H%M")


def _norm_chan(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())[:12]


def _missing_source_artifacts(output_root: Path, rows: list[dict]) -> list[dict]:
    missing = []
    for row in rows:
        for artifact in row.get("source_artifacts") or []:
            path = Path(str(artifact))
            candidate = path if path.is_absolute() else output_root / path
            if not candidate.exists():
                missing.append({
                    "strategy": row.get("strategy"),
                    "generated_at": row.get("generated_at"),
                    "direction": row.get("direction"),
                    "missing_artifact": str(candidate),
                })
                break
    return missing
