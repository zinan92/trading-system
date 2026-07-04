"""Pure Strategy Lab objective functions: hard constraints first, scalar second."""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import mean, pstdev


@dataclass(frozen=True)
class ObjectiveConfig:
    min_oos_trades: int = 100
    max_drawdown_pct: float = 8.0
    min_positive_regime_slices: int = 2
    required_regime_slices: int = 3


def evaluate_objective(
    trades: list[dict],
    equity: list[dict],
    config: ObjectiveConfig | None = None,
    *,
    stress_expectancy_per_trade: float | None = None,
    regime_slice_results: dict[str, dict] | None = None,
) -> dict:
    cfg = config or ObjectiveConfig()
    metrics = objective_metrics(trades, equity)
    invalid_reason = _invalid_reason(trades, equity, metrics)
    if invalid_reason:
        return {"status": "invalid", "reason": invalid_reason, "gates": {}, "metrics": metrics, "passed": False, "rank_score": None}

    regime_slice_results = regime_slice_results or {}
    non_negative_slices = sum(1 for item in regime_slice_results.values() if float(item.get("expectancy_per_trade", item.get("net_pnl", -1))) >= 0)
    gates = {
        "min_oos_trades": {
            "passed": metrics["trade_count"] >= cfg.min_oos_trades,
            "value": metrics["trade_count"],
            "threshold": cfg.min_oos_trades,
        },
        "max_drawdown": {
            "passed": metrics["max_drawdown_pct"] <= cfg.max_drawdown_pct,
            "value": metrics["max_drawdown_pct"],
            "threshold": cfg.max_drawdown_pct,
        },
        "positive_at_cost_plus_50pct": {
            "passed": stress_expectancy_per_trade is not None and stress_expectancy_per_trade > 0,
            "value": stress_expectancy_per_trade,
            "threshold": 0,
        },
        "regime_slices": {
            "passed": len(regime_slice_results) >= cfg.required_regime_slices and non_negative_slices >= cfg.min_positive_regime_slices,
            "value": non_negative_slices,
            "threshold": cfg.min_positive_regime_slices,
            "slice_count": len(regime_slice_results),
        },
    }
    passed = all(item["passed"] for item in gates.values())
    return {
        "status": "valid",
        "reason": "pass" if passed else "gate_failed",
        "gates": gates,
        "metrics": metrics,
        "passed": passed,
        "rank_score": metrics["sortino"] if passed else None,
    }


def objective_metrics(trades: list[dict], equity: list[dict]) -> dict:
    net = [float(item.get("net_pnl", 0)) for item in trades]
    returns = _returns(trades, equity)
    downside = [value for value in returns if value < 0]
    final_equity = float(equity[-1]["equity"]) if equity else 0.0
    starting_equity = float(equity[0]["equity"]) if equity else 0.0
    log_wealth = math.log(final_equity / starting_equity) if starting_equity > 0 and final_equity > 0 else float("nan")
    return {
        "trade_count": len(trades),
        "total_net_pnl": round(sum(net), 6),
        "expectancy_per_trade": round(sum(net) / len(net), 6) if net else float("nan"),
        "max_drawdown_pct": round(_max_drawdown_pct(equity), 6) if equity else float("nan"),
        "sharpe": round(_ratio(returns, returns), 6),
        "sortino": round(_ratio(returns, downside), 6),
        "log_wealth": round(log_wealth, 6),
        "turnover": round(sum(float(item.get("fixed_notional", 0)) for item in trades), 6),
    }


def _invalid_reason(trades: list[dict], equity: list[dict], metrics: dict) -> str:
    if not trades:
        return "zero_trades"
    if not equity:
        return "missing_equity"
    for value in metrics.values():
        if isinstance(value, (int, float)) and not math.isfinite(float(value)):
            return "non_finite_metrics"
    return ""


def _returns(trades: list[dict], equity: list[dict]) -> list[float]:
    base = float(equity[0]["equity"]) if equity else 0.0
    if base <= 0:
        return []
    return [float(item.get("net_pnl", 0)) / base for item in trades]


def _ratio(returns: list[float], risk_values: list[float]) -> float:
    if len(returns) < 2 or len(risk_values) < 2:
        return 0.0
    risk = pstdev(risk_values)
    return mean(returns) / risk if risk else 0.0


def _max_drawdown_pct(equity: list[dict]) -> float:
    peak = float(equity[0]["equity"])
    max_dd = 0.0
    for point in equity:
        value = float(point["equity"])
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak * 100)
    return max_dd
