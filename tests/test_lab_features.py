from __future__ import annotations

from datetime import datetime, timedelta, timezone

from schemas.market_data import Bar
from services.lab_features import build_feature_rows, feature_names, lookahead_shift_audit
from services.lab_labels import TripleBarrierConfig, triple_barrier_labels, zigzag_swing_labels


def test_feature_rows_are_all_bars_and_point_in_time():
    bars = _bars([100 + index * 0.05 for index in range(80)])
    primary = triple_barrier_labels(bars, TripleBarrierConfig(horizon_minutes=30))
    zigzag = zigzag_swing_labels(bars)

    rows = build_feature_rows(bars, primary, zigzag)
    mutated = list(bars)
    mutated[-1] = Bar("GOLD", "5m", mutated[-1].timestamp, 999, 1000, 998, 999, 100, "test", [])
    mutated_rows = build_feature_rows(mutated, primary, zigzag)

    assert len(rows) == len(bars)
    assert 30 <= len(feature_names(rows)) <= 50
    assert rows[20] == mutated_rows[20]


def test_lookahead_shift_audit_detects_known_leaky_probe():
    rows = []
    labels = [0, 1] * 40
    for index, label in enumerate(labels):
        rows.append({
            "label_primary": "long_win" if label else "none",
            "legit": labels[index - 1] if index else 0,
            "leaky": label,
        })

    audit = lookahead_shift_audit(rows, label_field="label_primary", legit_feature="legit", leaky_feature="leaky")

    assert audit["leaky_original_auc"] == 1.0
    assert audit["leaky_shifted_auc"] < 0.1
    assert audit["legit_shifted_auc"] > 0.9


def _bars(closes: list[float]) -> list[Bar]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        Bar("GOLD", "5m", (start + timedelta(minutes=5 * index)).isoformat(), close, close + 0.2, close - 0.2, close, 100, "test", [])
        for index, close in enumerate(closes)
    ]
