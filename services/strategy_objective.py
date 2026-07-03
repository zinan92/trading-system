from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.edge_judgment import EdgeJudgment
from services.journal_store import write_json


STRATEGY_OBJECTIVE_VERSION = "strategy-objective-v1"


class StrategyObjective:
    """Daily single-number objective J for one strategy, plus a hard winner gate.

    J = expectancy_R * min(1, N_closed / min_closed_trades)
        - drawdown_weight * |max_drawdown_pct|

    Winner gate (all must hold): expectancy_R > 0, profit_factor > 1,
    N_closed >= min_closed_trades.

    Scoring reuses EdgeJudgment's closed-trades-only metrics so open/unrealized
    PnL can never leak into the score, and thin samples are damped by the
    sample fraction but never promoted past the gate.
    """

    def __init__(
        self,
        output_root: Path,
        *,
        strategy_id: str,
        min_closed_trades: int = 20,
        drawdown_weight: float = 0.1,
    ) -> None:
        self.output_root = Path(output_root)
        self.strategy_id = strategy_id or self.output_root.name
        self.min_closed_trades = int(min_closed_trades or 20)
        self.drawdown_weight = float(drawdown_weight)

    def build(self, run_date: str, edge_judgment: dict | None = None, persist: bool = True) -> dict:
        edge = edge_judgment if self._is_edge_payload(edge_judgment) else None
        if edge is None:
            edge = EdgeJudgment(
                self.output_root,
                strategy_id=self.strategy_id,
                min_closed_trades=self.min_closed_trades,
            ).build(run_date, persist=False)
        edge_metrics = edge.get("metrics", {}) if isinstance(edge.get("metrics"), dict) else {}
        edge_audit = edge.get("audit", {}) if isinstance(edge.get("audit"), dict) else {}

        expectancy_r = self._float(edge_metrics.get("avg_realized_r"), 0.0)
        profit_factor = self._float(edge_metrics.get("profit_factor"), 0.0)
        closed_count = int(self._float(edge_metrics.get("closed_trade_count"), 0.0))
        max_drawdown_pct = self._float(edge_metrics.get("max_drawdown_pct"), 0.0)
        sample_fraction = round(min(1.0, closed_count / self.min_closed_trades), 4) if self.min_closed_trades else 1.0
        drawdown_penalty = round(self.drawdown_weight * abs(max_drawdown_pct), 4)
        objective_score = round(expectancy_r * sample_fraction - drawdown_penalty, 4)

        gate = {
            "expectancy_r_positive": expectancy_r > 0,
            "profit_factor_above_1": profit_factor > 1.0,
            "sample_sufficient": closed_count >= self.min_closed_trades,
        }
        gate["passed"] = all(gate.values())

        read_errors = edge_audit.get("read_errors") if isinstance(edge_audit.get("read_errors"), list) else []
        payload = {
            "schema_version": STRATEGY_OBJECTIVE_VERSION,
            "run_date": run_date,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "strategy_id": self.strategy_id,
            "objective_score": objective_score,
            "winner_gate": gate,
            "rankable": gate["sample_sufficient"],
            "scoring_basis": "closed_trades_only",
            "metrics": {
                "expectancy_r": round(expectancy_r, 4),
                "profit_factor": round(profit_factor, 4),
                "closed_trade_count": closed_count,
                "min_closed_trades": self.min_closed_trades,
                "sample_fraction": sample_fraction,
                "max_drawdown_pct": round(max_drawdown_pct, 4),
                "drawdown_weight": round(self.drawdown_weight, 4),
                "drawdown_penalty": drawdown_penalty,
                "wins": int(self._float(edge_metrics.get("wins"), 0.0)),
                "losses": int(self._float(edge_metrics.get("losses"), 0.0)),
            },
            "audit": {
                "status": "pass" if not read_errors else "fail",
                "read_errors": read_errors,
                "open_pnl_used_for_score": False,
                "red_line": "objective score uses closed trades only; thin samples are damped, never promoted",
            },
        }
        if persist:
            write_json(self.output_root / "strategy_objective" / "current.json", [payload])
            write_json(self.output_root / "strategy_objective" / f"{run_date}.json", [payload])
        return payload

    def _is_edge_payload(self, value: Any) -> bool:
        return isinstance(value, dict) and isinstance(value.get("metrics"), dict)

    def _float(self, value: Any, default: float) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError):
            return float(default)
        if result != result or result in (float("inf"), float("-inf")):
            return float(default)
        return result
