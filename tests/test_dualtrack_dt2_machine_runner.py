from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from schemas.market_data import Bar
from services.dualtrack_machine import DualTrackMachineRunner
from services.dualtrack_store import DualTrackPlanStore
from services.lab_r5_grid import Cycle


TEST_CONFIG = {
    "capital_per_track_usd": 10_000,
    "max_leverage": 1,
    "plan_lock_deadline_min_before_cycle": 0,
    "grid": {
        "spacing_bp": 20.0,
        "range_k": 1.0,
        "tp_mult_base": 1.0,
        "tp_mult_trend": 2.0,
        "re_arm_max": 1,
        "trend_leg_budget_pct": 20.0,
    },
    "cost_per_side_bp": 0.5,
    "scoreboard": {"gate_threshold": 0.60, "window_cycles": 30},
    "census": {"reversal_bp": 10, "min_run_pct": 0.3},
    "weekly_target_usd": [1000, 1500],
}
def _bar(ts: datetime, o: float, h: float, low: float, c: float) -> Bar:
    return Bar(symbol="GOLD", timeframe="1m", timestamp=ts.isoformat(), open=o, high=h, low=low, close=c, volume=1, provider="test")


def _cycle(cycle_id: str, closes: list[float], prev_range: float = 40.0) -> Cycle:
    start = datetime(2026, 7, 5, 1, 0, tzinfo=timezone.utc)
    bars = []
    prev = closes[0]
    for i, close in enumerate(closes):
        bars.append(_bar(start + timedelta(minutes=i), prev, max(prev, close), min(prev, close), close))
        prev = close
    return Cycle(cycle_id=cycle_id, kind="DAY", bars=tuple(bars), prev_range=prev_range)


def _plan(cycle_id: str, direction: str = "long") -> dict:
    return {
        "cycle_id": cycle_id,
        "direction": direction,
        "range": {"low": 3950.0, "high": 4050.0},
        "key_levels": [3992.0],
        "invalidation": [{"side": "below", "price": 3960.0, "confirm": "touch"}],
        "confidence": 7,
    }


def test_acceptance_7_5_runner_matches_frozen_golden_on_three_cycles(tmp_path: Path) -> None:
    runner = DualTrackMachineRunner(tmp_path / "outputs", config=TEST_CONFIG)
    cycles = [
        _cycle("golden_oscillation_DAY", [4000.0] + [3990.0, 4001.0] * 4),
        _cycle("golden_stop_DAY", [4000.0, 3995.0, 3985.0, 3970.0, 3950.0, 3940.0]),
        _cycle("golden_rearm_DAY", [4000.0, 3985.0, 3955.0, 3946.0, 3956.0, 3946.0, 3956.0, 3946.0, 3956.0]),
    ]
    fixture = json.loads((Path(__file__).parent / "fixtures" / "dualtrack_machine_golden.json").read_text(encoding="utf-8"))

    saw_stop = False
    saw_rearm = False
    for cycle in cycles:
        state = runner.run_plan(
            cycle.cycle_id,
            _plan(cycle.cycle_id),
            cycle.bars,
            prev_range=cycle.prev_range,
            trend_gate_armed=False,
        )
        fills = json.loads((tmp_path / "outputs" / "dualtrack" / "fills" / f"{cycle.cycle_id}_machine.json").read_text(encoding="utf-8"))
        expected = fixture[cycle.cycle_id]
        actual_events = [
            {
                "event": fill["event"],
                "side": fill["side"],
                "price": round(float(fill["price"]), 8),
                "notional": round(float(fill["notional"]), 8),
                "rung": fill["rung"],
                "realized_pnl": round(float(fill["realized_pnl"]), 8),
            }
            for fill in fills
        ]
        assert actual_events == expected["events"]
        assert round(state["machine_realized_pnl"], 8) == expected["machine_realized_pnl"]
        assert state["stop_hit"] is expected["stop_hit"]
        assert state["rearms"] == expected["rearms"]
        saw_stop = saw_stop or state["stop_hit"]
        saw_rearm = saw_rearm or state["rearms"] == 1
    assert saw_stop is True
    assert saw_rearm is True


def test_invariant_3_no_effective_plan_fail_closed_machine_stands_down(tmp_path: Path) -> None:
    runner = DualTrackMachineRunner(tmp_path / "outputs", config=TEST_CONFIG)
    cycle = _cycle("2026-07-05_DAY", [4000.0, 3990.0, 4001.0])

    state = runner.run_effective_plan(cycle.cycle_id, cycle.bars, prev_range=cycle.prev_range, as_of="2026-07-05T01:00:00+00:00")

    assert state["machine_stood_down"] is True
    assert state["machine_realized_pnl"] == 0
    assert (tmp_path / "outputs" / "dualtrack" / "fills" / f"{cycle.cycle_id}_machine.json").read_text() == "[]\n"


def test_invariant_4_intraday_machine_payload_is_pnl_only(tmp_path: Path) -> None:
    runner = DualTrackMachineRunner(tmp_path / "outputs", config=TEST_CONFIG)
    cycle = _cycle("2026-07-05_DAY", [4000.0] + [3990.0, 4001.0] * 2)
    runner.run_plan(cycle.cycle_id, _plan(cycle.cycle_id), cycle.bars, prev_range=cycle.prev_range, trend_gate_armed=False)

    payload = runner.machine_payload(cycle.cycle_id, as_of="2026-07-05T02:00:00+00:00")

    assert set(payload) == {"realized_pnl", "unrealized_pnl", "layers"}
    assert all(isinstance(item, str) for item in payload["layers"])
    forbidden = {"fills", "orders", "entries", "inventory", "rungs", "price", "notional", "sl", "tp"}
    assert forbidden.isdisjoint(payload)


def test_machine_runner_uses_effective_human_plan_when_locked_before_deadline(tmp_path: Path) -> None:
    store = DualTrackPlanStore(tmp_path / "outputs", config=TEST_CONFIG)
    cycle = _cycle("2026-07-05_DAY", [4000.0] + [3990.0, 4001.0] * 2)
    store.save_human_plan(_plan(cycle.cycle_id), now="2026-07-05T00:59:00+00:00")
    runner = DualTrackMachineRunner(tmp_path / "outputs", config=TEST_CONFIG)

    state = runner.run_effective_plan(cycle.cycle_id, cycle.bars, prev_range=cycle.prev_range, as_of="2026-07-05T01:00:00+00:00")

    assert state["machine_stood_down"] is False
    assert state["effective_plan_author"] == "human"
