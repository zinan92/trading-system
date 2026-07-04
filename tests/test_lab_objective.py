from __future__ import annotations

from services.lab_objective import ObjectiveConfig, evaluate_objective


def _trades(count: int, pnl: float = 1.0) -> list[dict]:
    return [{"net_pnl": pnl, "fixed_notional": 1000.0} for _ in range(count)]


def _equity(trades: list[dict]) -> list[dict]:
    value = 10_000.0
    points = [{"equity": value}]
    for trade in trades:
        value += float(trade["net_pnl"])
        points.append({"equity": value})
    return points


def test_objective_constraints_then_sortino_score():
    trades = _trades(120, 1.0)
    equity = _equity(trades)
    regimes = {
        "low|directional": {"expectancy_per_trade": 1.0},
        "mid|directional": {"expectancy_per_trade": 0.5},
        "high|quiet": {"expectancy_per_trade": -0.2},
    }

    result = evaluate_objective(
        trades,
        equity,
        ObjectiveConfig(min_oos_trades=100, max_drawdown_pct=2),
        stress_expectancy_per_trade=0.5,
        regime_slice_results=regimes,
    )

    assert result["status"] == "valid"
    assert result["passed"] is True
    assert result["rank_score"] == result["metrics"]["sortino"]


def test_objective_fails_closed_on_zero_trades():
    result = evaluate_objective([], [{"equity": 10_000.0}])

    assert result["status"] == "invalid"
    assert result["reason"] == "zero_trades"
    assert result["passed"] is False


def test_objective_gate_failure_blocks_rank_score():
    trades = _trades(10, 1.0)
    result = evaluate_objective(
        trades,
        _equity(trades),
        ObjectiveConfig(min_oos_trades=100),
        stress_expectancy_per_trade=0.5,
        regime_slice_results={
            "a": {"expectancy_per_trade": 1},
            "b": {"expectancy_per_trade": 1},
            "c": {"expectancy_per_trade": 1},
        },
    )

    assert result["status"] == "valid"
    assert result["passed"] is False
    assert result["gates"]["min_oos_trades"]["passed"] is False
    assert result["rank_score"] is None
