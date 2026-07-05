from __future__ import annotations

from datetime import datetime, timedelta, timezone

from schemas.market_data import Bar
from services.lab_registry import LabRegistry
from services.lab_r3_sweep import R3_SUBJECTS, R3SweepConfig, _anchor_divergence, recommend_label_horizon, run_r3_sweep


def test_r3_recommends_best_gross_per_risk_horizon():
    rows = [
        {"status": "valid", "oos_trades": 120, "gross_bp_per_trip": 4.0, "stop_target_scale": 2.0, "maker_2bp_expectancy_per_trade": 0.1, "hold_minutes": 120, "strategy": "a", "hold_multiplier": 1},
        {"status": "valid", "oos_trades": 120, "gross_bp_per_trip": 3.0, "stop_target_scale": 1.0, "maker_2bp_expectancy_per_trade": -0.1, "hold_minutes": 240, "strategy": "b", "hold_multiplier": 2},
    ]

    rec = recommend_label_horizon(rows)

    assert rec["horizon_minutes"] == 240
    assert rec["strategy"] == "b"


def test_run_r3_sweep_registers_all_cells(monkeypatch, tmp_path):
    strategies = [_Strategy(strategy_id) for strategy_id in R3_SUBJECTS]

    class FakeStrategyRegistry:
        def strategies(self):
            return strategies

    def fake_replay(strategy, bars, walkforward):
        return {"status": "valid", "windows": [{"bars": bars, "signals": [{"index": 0, "direction": "long"}]}]}

    def fake_eval(windows, bp, simulation=None):
        expectancy = round(1.0 - float(bp) * 0.1, 6)
        return {
            "status": "valid",
            "reason": "pass",
            "window_count": 1,
            "metrics": {
                "trade_count": 150,
                "expectancy_per_trade": expectancy,
                "expectancy_bp_on_notional": expectancy * 10,
                "win_rate": 0.5,
                "max_drawdown_pct": 0.1,
                "sortino": 1.0,
            },
            "trades": [{}] * 150,
        }

    monkeypatch.setattr("services.lab_r3_sweep.StrategyRegistry", FakeStrategyRegistry)
    monkeypatch.setattr("services.lab_r3_sweep._replay_strategy_windows", fake_replay)
    monkeypatch.setattr("services.lab_r3_sweep._evaluate_replayed_windows", fake_eval)

    result = run_r3_sweep(tmp_path, LabRegistry(tmp_path), _bars(3), config=R3SweepConfig())

    assert result["total_trials"] == 60
    assert len(list((tmp_path / "lab" / "experiments").glob("R3_*.json"))) == 60
    assert result["recommendation"]["status"] == "selected"


def test_r3_anchor_divergence_reports_metric_mismatch():
    divergence = _anchor_divergence(
        {
            "r3_summary": {"break_even_bp": 0.1, "oos_trades": 2},
            "cost_grid_results": {"0bp": {"metrics": {"expectancy_per_trade": 0.2}}},
        },
        {"break_even_bp": 0.3, "trade_count": 4, "expectancy_per_trade": {"0bp": 0.5}},
    )

    assert set(divergence) == {"break_even_bp", "trade_count", "expectancy_per_trade"}


class _Strategy:
    symbol = "GOLD"
    timeframe = "1m"
    params = {"engine": "fake"}

    def __init__(self, strategy_id: str) -> None:
        self.strategy_id = strategy_id


def _bars(count: int) -> list[Bar]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        Bar("GOLD", "1m", (start + timedelta(minutes=index)).isoformat(), 100, 101, 99, 100, 1, "test", [])
        for index in range(count)
    ]
