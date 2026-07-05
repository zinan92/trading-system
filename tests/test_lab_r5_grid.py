"""Sanity battery for the R5-B conditional grid simulator."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from schemas.market_data import Bar
from services.lab_r5_grid import (
    R5GridConfig,
    breakeven_hit_rate,
    run_r5_grid,
    segment_cycles,
    simulate_cycle,
)
from services.lab_registry import LabRegistry

CFG = R5GridConfig(spacing_bp=(20.0,), range_k=(1.0,), cost_grid_bp=(0.0, 0.5, 5.0), min_cycles=1)


def _bar(ts: datetime, o: float, h: float, low: float, c: float) -> Bar:
    return Bar(
        symbol="GOLD", timeframe="1m", timestamp=ts.isoformat(),
        open=o, high=h, low=low, close=c, volume=1.0, provider="test",
    )


def _cycle_bars(start: datetime, closes: list[float]) -> list[Bar]:
    bars = []
    prev = closes[0]
    for i, close in enumerate(closes):
        o = prev
        bars.append(_bar(start + timedelta(minutes=i), o, max(o, close), min(o, close), close))
        prev = close
    return bars


def _make_cycle(closes: list[float], prev_range: float = 40.0):
    from services.lab_r5_grid import Cycle

    start = datetime(2026, 1, 5, 1, 0, tzinfo=timezone.utc)
    return Cycle(cycle_id="t_DAY", kind="DAY", bars=tuple(_cycle_bars(start, closes)), prev_range=prev_range)


def test_grid_collects_oscillation_profit() -> None:
    # anchor 4000, spacing 20bp = 8.0; sawtooth dips to 3990 then recovers repeatedly
    closes = [4000.0]
    for _ in range(10):
        closes += [3990.0, 4001.0]
    cycle = _make_cycle(closes)
    result = simulate_cycle(cycle, 1, spacing_bp=20.0, range_k=1.0, config=CFG)
    assert result["round_trips"] >= 5
    assert result["net_by_cost"]["0bp"] > 0


def test_stop_loss_realized_and_bounded() -> None:
    # crash straight through the range: rungs fill, stop closes everything
    closes = [4000.0, 3995.0, 3985.0, 3970.0, 3950.0, 3940.0]
    cycle = _make_cycle(closes, prev_range=40.0)
    result = simulate_cycle(cycle, 1, spacing_bp=20.0, range_k=1.0, config=CFG)
    assert result["stop_hit"] is True
    assert result["net_by_cost"]["0bp"] < 0
    # max loss bounded by rungs x distance-to-stop on $1k rungs
    assert result["net_by_cost"]["0bp"] > -(CFG.max_rungs * CFG.rung_notional * 40.0 / 4000.0)


def test_no_same_bar_round_trip() -> None:
    # one giant bar spans rung and target; buy must not TP in the same bar
    start = datetime(2026, 1, 5, 1, 0, tzinfo=timezone.utc)
    bars = [_bar(start, 4000.0, 4000.0, 4000.0, 4000.0), _bar(start + timedelta(minutes=1), 4000.0, 4005.0, 3991.0, 4004.0)]
    from services.lab_r5_grid import Cycle

    cycle = Cycle(cycle_id="t_DAY", kind="DAY", bars=tuple(bars), prev_range=40.0)
    result = simulate_cycle(cycle, 1, spacing_bp=20.0, range_k=1.0, config=CFG)
    assert result["round_trips"] == 0


def test_cost_monotonicity() -> None:
    closes = [4000.0] + [3990.0, 4001.0] * 8
    cycle = _make_cycle(closes)
    result = simulate_cycle(cycle, 1, spacing_bp=20.0, range_k=1.0, config=CFG)
    assert result["net_by_cost"]["0bp"] > result["net_by_cost"]["0.5bp"] > result["net_by_cost"]["5bp"]


def test_cycle_segmentation_boundaries() -> None:
    bars: list[Bar] = []
    t0 = datetime(2026, 1, 5, 1, 0, tzinfo=timezone.utc)
    for i in range(24 * 60 * 2):  # two full days of 1m bars
        ts = t0 + timedelta(minutes=i)
        bars.append(_bar(ts, 4000.0, 4001.0, 3999.0, 4000.0))
    cycles = segment_cycles(bars)
    assert all(cycle.bars for cycle in cycles)
    kinds = {cycle.cycle_id.split("_")[-1] for cycle in cycles}
    assert kinds == {"DAY", "NIGHT"}
    for cycle in cycles:
        hours = {datetime.fromisoformat(bar.timestamp).hour for bar in cycle.bars}
        if cycle.kind == "DAY":
            assert hours <= set(range(1, 13))
        else:
            assert hours <= (set(range(13, 24)) | {0})


def test_oracle_beats_anti_and_determinism(tmp_path: Path) -> None:
    bars: list[Bar] = []
    t0 = datetime(2026, 1, 5, 13, 0, tzinfo=timezone.utc)
    price = 4000.0
    minute = 0
    for _ in range(8):  # 8 cycles: trending up with dips (long-favourable)
        for _ in range(720):
            dip = 0.35 if minute % 7 == 3 else -0.15
            new = price - dip
            bars.append(_bar(t0 + timedelta(minutes=minute), price, max(price, new), min(price, new), new))
            price = new
            minute += 1
    registry = LabRegistry(tmp_path)
    report1 = run_r5_grid(tmp_path, registry, bars, config=CFG)
    report2 = run_r5_grid(tmp_path, registry, bars, config=CFG)
    cell = lambda rep, arm: next(c for c in rep["cells"] if c["arm"] == arm)  # noqa: E731
    oracle, anti = cell(report1, "oracle"), cell(report1, "anti")
    assert oracle["cost_grid_results"]["0.5bp"]["mean_net_per_cycle"] > anti["cost_grid_results"]["0.5bp"]["mean_net_per_cycle"]
    assert report1["cells"] == report2["cells"]  # determinism (timestamps excluded)
    assert registry.trial_count("r5_grid_direction_gold_1m") >= 4


def test_breakeven_hit_rate_math() -> None:
    assert breakeven_hit_rate(10.0, -10.0) == 0.5
    assert breakeven_hit_rate(30.0, -10.0) == 0.25
    assert breakeven_hit_rate(-1.0, -10.0) is None
    assert breakeven_hit_rate(10.0, 5.0) is None


def test_budget_sizing_scales_rung_notional() -> None:
    # spacing 100bp on a 2%-wide range -> 2 rungs of $5k instead of $1k each
    closes = [4000.0, 3958.0, 4000.0, 3958.0, 4001.0]
    cycle = _make_cycle(closes, prev_range=80.0)
    small = simulate_cycle(cycle, 1, spacing_bp=100.0, range_k=1.0, config=CFG)
    big = simulate_cycle(cycle, 1, spacing_bp=100.0, range_k=1.0, config=CFG, budget_sizing=True)
    assert big["round_trips"] == small["round_trips"] >= 1
    assert big["net_by_cost"]["0bp"] > small["net_by_cost"]["0bp"] * 4  # 5x notional per rung


def test_tp_mult_requires_bigger_move() -> None:
    # dip fills the rung; +1 spacing recovery satisfies tp1 but not tp2
    closes = [4000.0, 3991.0, 4000.5, 4000.5, 4000.5]
    cycle = _make_cycle(closes)
    tp1 = simulate_cycle(cycle, 1, spacing_bp=20.0, range_k=1.0, config=CFG, tp_mult=1.0)
    tp2 = simulate_cycle(cycle, 1, spacing_bp=20.0, range_k=1.0, config=CFG, tp_mult=2.0)
    assert tp1["round_trips"] == 1
    assert tp2["round_trips"] == 0


def test_re_arm_trades_after_stop() -> None:
    # crash through the range, then oscillate around the new anchor
    closes = [4000.0, 3985.0, 3955.0]  # breach (stop at 3960); re-anchor 3955, first rung 3947
    closes += [3946.0, 3956.0, 3946.0, 3956.0, 3946.0, 3956.0]
    cycle = _make_cycle(closes, prev_range=40.0)
    plain = simulate_cycle(cycle, 1, spacing_bp=20.0, range_k=1.0, config=CFG)
    rearmed = simulate_cycle(cycle, 1, spacing_bp=20.0, range_k=1.0, config=CFG, re_arm=True)
    assert plain["stop_hit"] and plain["round_trips"] == 0
    assert rearmed["rearms"] == 1
    assert rearmed["round_trips"] >= 1
