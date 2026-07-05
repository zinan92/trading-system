from __future__ import annotations

from datetime import datetime, timedelta, timezone

from schemas.market_data import Bar
from services.lab_labels import TripleBarrierConfig, triple_barrier_labels, zigzag_swing_labels


def test_triple_barrier_labels_are_deterministic_and_directional():
    bars = _bars([100, 100.1, 100.2, 101.4, 101.5, 101.6])
    cfg = TripleBarrierConfig(horizon_minutes=20, target_atr=1.0, stop_atr=1.0, atr_period=2)

    first = triple_barrier_labels(bars, cfg)
    second = triple_barrier_labels(bars, cfg)

    assert first == second
    assert any(row["label"] == "long_win" for row in first)


def test_zigzag_labels_include_future_swing_membership():
    bars = _bars([100, 100.5, 101.0, 100.2, 99.5, 99.0])

    labels = zigzag_swing_labels(bars, threshold_pct=0.4)

    assert {row["label"] for row in labels} >= {"long_win", "short_win"}


def _bars(closes: list[float]) -> list[Bar]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        Bar("GOLD", "5m", (start + timedelta(minutes=5 * index)).isoformat(), close, close + 0.2, close - 0.2, close, 100, "test", [])
        for index, close in enumerate(closes)
    ]
