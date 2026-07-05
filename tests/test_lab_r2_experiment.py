from __future__ import annotations

from datetime import datetime, timedelta, timezone

from schemas.market_data import Bar
from services.lab_r2_experiment import _verdict, render_r2_report, run_r2_experiment
from services.lab_registry import LabRegistry


def test_run_r2_experiment_registers_all_model_trials(monkeypatch, tmp_path):
    bars = _bars(12)
    labels = [{"timestamp": bar.timestamp, "label": "none"} for bar in bars]
    rows = [
        {
            "timestamp": bar.timestamp,
            "label_primary": "none",
            "label_zigzag": "short_win",
            "atr": 0.2,
            "target_atr": 2.0,
            "stop_atr": 1.0,
            "horizon_bars": 12,
            "x": float(index),
        }
        for index, bar in enumerate(bars)
    ]

    def fake_eval(rows, bars, *, scheme, model_kind, barrier, config):
        row = {
            "scheme": scheme,
            "model": model_kind,
            "barrier": barrier,
            "oos_trades": 120,
            "win_rate": 0.5,
            "gross_bp_per_trip": 1.0,
            "break_even_bp": 0.5,
            "maker_2bp_expectancy_per_trade": -0.1,
            "max_drawdown_pct": 0.2,
            "sortino_at_2bp": -0.3,
            "status": "valid",
            "reason": "pass",
        }
        return {
            "status": "valid",
            "row": row,
            "cost_grid_results": {},
            "calibration": {"brier": 0.1, "bins": []},
            "feature_importances": [{"feature": "x", "importance": 1.0}],
        }

    monkeypatch.setattr("services.lab_r2_experiment.resample_bars", lambda bars_1m, timeframe: bars)
    monkeypatch.setattr("services.lab_r2_experiment.triple_barrier_labels", lambda bars_5m, config: labels)
    monkeypatch.setattr("services.lab_r2_experiment.zigzag_swing_labels", lambda bars_5m: labels)
    monkeypatch.setattr("services.lab_r2_experiment.build_feature_rows", lambda bars_5m, primary, zigzag: rows)
    monkeypatch.setattr("services.lab_r2_experiment.evaluate_meta_rule", fake_eval)
    monkeypatch.setattr("services.lab_r2_experiment.run_chan_addendum", lambda output_root, registry, bars_1m: {"status": "valid", "rows": [], "parity": {}})
    monkeypatch.setattr("services.lab_r2_experiment._r1_top_rows", lambda output_root: [{"strategy": "r1", "oos_trades": 200, "gross_bp_per_trip": 2.0, "maker_2bp_expectancy_per_trade": -0.2}])

    result = run_r2_experiment(tmp_path, LabRegistry(tmp_path), bars, horizon_minutes=960)

    assert len(result["results"]) == 12
    assert len(list((tmp_path / "lab" / "experiments").glob("R2_*.json"))) == 12
    assert result["verdict"]["beats_r1_top3"] is True


def test_r2_verdict_ignores_insufficient_sample_rows():
    results = [
        {"status": "insufficient_sample", "oos_trades": 6, "maker_2bp_expectancy_per_trade": 1.0},
        {"status": "valid", "oos_trades": 150, "maker_2bp_expectancy_per_trade": -0.4},
    ]
    r1_top = [{"strategy": "r1", "maker_2bp_expectancy_per_trade": -0.2}]

    verdict = _verdict(results, r1_top)

    assert verdict["best_r2"]["maker_2bp_expectancy_per_trade"] == -0.4
    assert verdict["beats_r1_top3"] is False


def test_render_r2_report_includes_caveat_and_best_rule():
    dataset = {
        "feature_rows": 10,
        "primary_base_rates": {"rates": {"long_win": 0.1}},
        "zigzag_base_rates": {"rates": {"short_win": 0.9}},
    }
    results = [{"scheme": "zigzag", "model": "logistic_l2", "barrier": 0.55, "oos_trades": 120, "gross_bp_per_trip": -1.0, "break_even_bp": 0.0, "maker_2bp_expectancy_per_trade": -0.5, "max_drawdown_pct": 1.0, "sortino_at_2bp": -0.2, "status": "valid", "calibration": {"brier": 0.1, "bins": []}, "feature_importances": []}]

    report = render_r2_report(dataset, results, [], {"sentence": "No beat."}, total_trials=1)

    assert "Best-of-N caveat" in report
    assert "Best Comparable R2 Rule" in report


def _bars(count: int) -> list[Bar]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        Bar("GOLD", "1m", (start + timedelta(minutes=index)).isoformat(), 100, 101, 99, 100, 1, "test", [])
        for index in range(count)
    ]
