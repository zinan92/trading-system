from __future__ import annotations

from datetime import datetime, timedelta, timezone

from schemas.market_data import Bar
from services.lab_meta_model import MetaModelConfig, _calibration, evaluate_meta_rule
from services.lab_walkforward import WalkForwardConfig


def test_meta_model_training_is_seeded_and_reproducible():
    bars, rows = _dataset(days=35)
    cfg = MetaModelConfig(walkforward=WalkForwardConfig(train_days=2, validate_days=1, step_days=7, holdout_days=1), barriers=(0.55,))

    first = evaluate_meta_rule(rows, bars, scheme="primary", model_kind="logistic_l2", barrier=0.55, config=cfg)
    second = evaluate_meta_rule(rows, bars, scheme="primary", model_kind="logistic_l2", barrier=0.55, config=cfg)

    assert first["row"] == second["row"]
    assert first["calibration"] == second["calibration"]
    assert first["feature_importances"]


def test_calibration_bins_do_not_double_count_final_bucket():
    predictions = [
        {"probability": 0.35, "actual_positive": 0},
        {"probability": 0.95, "actual_positive": 1},
    ]

    calibration = _calibration(predictions, "primary")

    assert sum(row["count"] for row in calibration["bins"]) == 2
    assert calibration["bins"][-1]["bin"] == "0.9-1.0"
    assert calibration["bins"][-1]["count"] == 1


def _dataset(days: int) -> tuple[list[Bar], list[dict]]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    bars = []
    rows = []
    for index in range(days * 24 * 12):
        price = 100 + (index % 24) * 0.01
        ts = (start + timedelta(minutes=5 * index)).isoformat()
        bars.append(Bar("GOLD", "5m", ts, price, price + 0.2, price - 0.2, price, 100, "test", []))
        positive = index % 10 in {0, 1, 2}
        rows.append({
            "timestamp": ts,
            "label_primary": "long_win" if positive else "none",
            "label_zigzag": "long_win" if positive else "none",
            "atr": 0.2,
            "target_atr": 1.0,
            "stop_atr": 1.0,
            "horizon_bars": 6,
            "x_signal": 1.0 if positive else 0.0,
            "x_noise": (index % 7) / 7,
        })
    return bars, rows
