"""Strategy leaderboard — ranks every per-strategy paper namespace so you can
see which strategy is winning.

Reads `outputs/strategies/<id>/{equity_curve,performance,clean_bars}` (produced
by MultiStrategyRunner) and computes, per strategy: return %, max drawdown,
win rate, profit factor, net PnL, trade counts, a buy-and-hold gold benchmark
return over the same window, the strategy's edge vs that benchmark, and a simple
Sharpe from the equity points. Ranked by return.
"""

from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config, load_risk_rules, load_strategy_config
from services.execution_accounting import executed_record_ids
from services.journal_store import load_json, write_json
from services.lab_promotion import lab_expectation_for_strategy


class StrategyLeaderboard:
    def __init__(self, output_root: Path | None = None) -> None:
        config = load_pipeline_config()
        risk = load_risk_rules()
        self.output_root = (
            Path(output_root)
            if output_root is not None
            else Path(os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(ROOT / config.get("output_root", "outputs"))))
        )
        self.strategy_config = load_strategy_config()
        trade_quality = (risk.get("default", {}) or {}).get("trade_quality", {}) or {}
        self.min_daily_executed_trades = int(trade_quality.get("min_daily_executed_trades_per_strategy", 2))

    def build(self, run_date: str) -> dict:
        strategies_dir = self.output_root / "strategies"
        rows = []
        if strategies_dir.exists():
            for namespace in sorted(p for p in strategies_dir.iterdir() if p.is_dir()):
                row = self._row(namespace, run_date)
                if row is not None:
                    rows.append(row)
        # Rank by return (descending); strategies without a return sink to the bottom.
        rows.sort(key=lambda r: (r["return_pct"] is None, -(r["return_pct"] or 0.0)))
        for index, row in enumerate(rows, 1):
            row["rank"] = index
        payload = {
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "run_date": run_date,
            "strategy_count": len(rows),
            "strategies": rows,
        }
        write_json(self.output_root / "strategy_leaderboard" / "current.json", [payload])
        write_json(self.output_root / "strategy_leaderboard" / f"{run_date}.json", [payload])
        return payload

    def _row(self, namespace: Path, run_date: str) -> dict | None:
        equity_rows = load_json(namespace / "equity_curve" / "current.json")
        equity = equity_rows[-1] if equity_rows else {}
        if not equity:
            return None
        starting = float(equity.get("starting_equity", 0) or 0)
        current = float(equity.get("current_equity", starting) or starting)
        return_pct = ((current / starting - 1) * 100) if starting else 0.0
        summary = {}
        perf_rows = load_json(namespace / "performance" / "current.json")
        if perf_rows:
            summary = perf_rows[-1].get("summary", {}) or {}
        position = self._position_summary(namespace)
        gold_return = self._gold_return(namespace, run_date)
        strategy_config = self.strategy_config.get(namespace.name, {}) if isinstance(self.strategy_config, dict) else {}
        daily_execution = self._daily_execution(namespace, run_date)
        return {
            "strategy_id": namespace.name,
            "engine": strategy_config.get("engine", "ma"),
            "timeframe": strategy_config.get("timeframe", "5m"),
            "classification": self._classification(namespace.name, strategy_config),
            "daily_execution": daily_execution,
            "effective_today": daily_execution.get("effective_today", False),
            "starting_equity": round(starting, 2),
            "current_equity": round(current, 2),
            "return_pct": round(return_pct, 4),
            "max_drawdown_pct": equity.get("max_drawdown_pct", 0.0),
            "gold_return_pct": round(gold_return, 4) if gold_return is not None else None,
            "vs_gold_pct": round(return_pct - gold_return, 4) if gold_return is not None else None,
            "win_rate": summary.get("win_rate", 0.0),
            "profit_factor": summary.get("profit_factor", 0.0),
            "net_pnl": summary.get("net_pnl_marked", 0.0),
            "closed_trades": summary.get("closed_all_count", 0),
            "open_trades": summary.get("open_trade_count", 0),
            "position": position,
            "lab_expectation": lab_expectation_for_strategy(self.output_root, namespace.name),
            "equity_points": self._compact_equity_points(equity.get("points", [])),
            "sharpe": self._sharpe(equity.get("points", [])),
        }

    def _classification(self, strategy_id: str, strategy_config: dict) -> dict:
        configured = strategy_config.get("classification") or {}
        if configured:
            return configured
        engine = str(strategy_config.get("engine", "ma")).lower()
        if engine == "chan":
            return {
                "family": "chan",
                "family_label": "缠论",
                "style": "chan_bsp",
                "style_label": "缠论买卖点",
                "directionality": "long_short",
                "frequency_bucket": "low",
                "role": "unclassified_chan",
            }
        if engine == "macd":
            return {
                "family": "momentum",
                "family_label": "动量",
                "style": "macd_cross",
                "style_label": "MACD 交叉",
                "directionality": "long_short",
                "frequency_bucket": "medium",
                "role": "unclassified_momentum",
            }
        return {
            "family": "trend_macro",
            "family_label": "趋势/宏观",
            "style": "ma_factor",
            "style_label": "均线/因子",
            "directionality": "long_short",
            "frequency_bucket": "low_to_medium",
            "role": "unclassified",
        }

    def _daily_execution(self, namespace: Path, run_date: str) -> dict:
        signals = load_json(namespace / "signals" / f"{run_date}.json")
        tickets = load_json(namespace / "trade_tickets" / f"{run_date}.json")
        paper_orders = load_json(namespace / "paper_orders" / f"{run_date}.json")
        demo_orders = load_json(namespace / "demo_order_requests" / f"{run_date}.json")
        live_requests = load_json(namespace / "live_order_requests" / f"{run_date}.json")
        decisions = load_json(namespace / "journal_decisions" / f"{run_date}.json")
        executed_decisions = [
            item for item in decisions
            if item.get("decision_status") in {"executed", "executed_paper"}
        ]
        execution_ids: set[str] = set()
        execution_ids.update(executed_record_ids(paper_orders, legacy_count_missing_status=True))
        execution_ids.update(executed_record_ids(demo_orders))
        execution_ids.update(executed_record_ids(live_requests))
        execution_ids.update(executed_record_ids(executed_decisions, legacy_count_missing_status=True))
        executed_count = len(execution_ids)
        directional = sum(1 for item in signals if item.get("direction") in {"long", "short"})
        status = "effective" if executed_count >= self.min_daily_executed_trades else ("low_volume" if executed_count == 1 else "inactive")
        return {
            "run_date": run_date,
            "status": status,
            "effective_today": status == "effective",
            "min_daily_executed_trades": self.min_daily_executed_trades,
            "executed_trade_count": executed_count,
            "signal_count": len(signals),
            "directional_signal_count": directional,
            "ticket_count": len(tickets),
            "paper_order_count": len(paper_orders),
            "demo_order_count": len(demo_orders),
            "live_request_count": len(live_requests),
            "executed_decision_count": len(executed_decisions),
        }

    def _compact_equity_points(self, points: list) -> list[dict]:
        compact = []
        for point in points:
            if not isinstance(point, dict) or point.get("timestamp") is None or point.get("equity") is None:
                continue
            compact.append({"timestamp": point["timestamp"], "equity": point["equity"]})
        return compact

    def _position_summary(self, namespace: Path) -> dict:
        positions = load_json(namespace / "paper_positions" / "current.json")
        if not isinstance(positions, dict) or not positions:
            return {
                "status": "flat",
                "summary": "flat",
                "side": "flat",
                "gross_quantity": 0.0,
                "net_side": "flat",
                "net_quantity": 0.0,
                "unrealized_pnl": 0.0,
                "legs": {},
            }
        item = positions.get("GOLD") or next(iter(positions.values()))
        side = str(item.get("side", "n/a"))
        gross = float(item.get("quantity", 0) or 0)
        net_side = str(item.get("net_side", side))
        net_quantity = float(item.get("net_quantity", gross) or 0)
        legs = item.get("legs") or {}
        summary = "flat" if gross == 0 else f"{side} gross {gross:.4f} / net {net_side} {net_quantity:.4f}"
        return {
            "status": "open" if gross else "flat",
            "summary": summary,
            "side": side,
            "gross_quantity": round(gross, 6),
            "net_side": net_side,
            "net_quantity": round(net_quantity, 6),
            "unrealized_pnl": item.get("unrealized_pnl", 0.0),
            "legs": legs,
        }

    def _gold_return(self, namespace: Path, run_date: str) -> float | None:
        bars = load_json(namespace / "clean_bars" / run_date / "GOLD_5m.json")
        closes = [float(b["close"]) for b in bars if isinstance(b, dict) and b.get("close") is not None]
        if len(closes) < 2 or not closes[0]:
            return None
        return (closes[-1] / closes[0] - 1) * 100

    def _sharpe(self, points: list) -> float | None:
        equities = [float(p["equity"]) for p in points if isinstance(p, dict) and p.get("equity") is not None]
        if len(equities) < 3:
            return None
        returns = [(equities[i] / equities[i - 1] - 1) for i in range(1, len(equities)) if equities[i - 1]]
        if len(returns) < 2:
            return None
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        std = math.sqrt(variance)
        return round(mean / std, 4) if std else None


def build_strategy_leaderboard(run_date: str, output_root: Path | None = None) -> dict:
    return StrategyLeaderboard(output_root).build(run_date)
