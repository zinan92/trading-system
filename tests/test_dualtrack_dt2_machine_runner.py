from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from schemas.market_data import Bar
from services.dualtrack_config import base_rung_notional
from services.dualtrack_grid_core import simulate_conditional_grid
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


def _plan(cycle_id: str, direction: str = "long", *, floor: float = 3960.0) -> dict:
    return {
        "cycle_id": cycle_id,
        "direction": direction,
        "range": {"low": floor, "high": 4050.0},
        "key_levels": [3992.0],
        "invalidation": [{"side": "below", "price": floor, "confirm": "touch"}],
        "confidence": 7,
    }


def test_acceptance_7_5_runner_matches_frozen_golden_on_three_cycles(tmp_path: Path) -> None:
    runner = DualTrackMachineRunner(tmp_path / "outputs", config=TEST_CONFIG)
    cycles = [
        (_cycle("golden_oscillation_DAY", [4000.0] + [3990.0, 4001.0] * 4), _plan("golden_oscillation_DAY")),
        (_cycle("golden_stop_DAY", [4000.0, 3995.0, 3985.0, 3970.0, 3950.0, 3940.0]), _plan("golden_stop_DAY")),
        (_cycle("golden_rearm_DAY", [4000.0, 3985.0, 3955.0, 3946.0, 3956.0, 3946.0, 3956.0, 3946.0, 3956.0]), _plan("golden_rearm_DAY")),
        (_cycle("golden_tight_floor_DAY", [4000.0, 3990.0, 3970.0], prev_range=100.0), _plan("golden_tight_floor_DAY", floor=3976.0)),
    ]
    fixture = json.loads((Path(__file__).parent / "fixtures" / "dualtrack_machine_golden.json").read_text(encoding="utf-8"))

    saw_stop = False
    saw_rearm = False
    for cycle, plan in cycles:
        state = runner.run_plan(
            cycle.cycle_id,
            plan,
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


def test_dt8_grid_floor_uses_plan_stop_as_bottom_and_stop_pnl_is_non_positive(tmp_path: Path) -> None:
    runner = DualTrackMachineRunner(tmp_path / "outputs", config=TEST_CONFIG)
    cycle = _cycle("2026-07-05_DAY", [4000.0, 3990.0, 3970.0], prev_range=100.0)
    floor = 3976.0

    state = runner.run_plan(
        cycle.cycle_id,
        _plan(cycle.cycle_id, floor=floor),
        cycle.bars,
        prev_range=cycle.prev_range,
        trend_gate_armed=False,
    )
    fills_path = tmp_path / "outputs" / "dualtrack" / "fills" / f"{cycle.cycle_id}_machine.json"
    fills = json.loads(fills_path.read_text(encoding="utf-8"))
    entries = [fill for fill in fills if fill["event"] == "entry"]
    stops = [fill for fill in fills if fill["event"] == "stop"]

    assert state["stop_hit"] is True
    assert entries
    assert stops
    assert min(float(fill["price"]) for fill in entries) >= floor
    assert any(float(fill["price"]) == floor for fill in entries)
    assert all(float(fill["realized_pnl"]) <= 0 for fill in stops)


def test_dt8_machine_fixed_per_rung_sizing_tight_floor_deploys_less(tmp_path: Path) -> None:
    runner = DualTrackMachineRunner(tmp_path / "outputs", config=TEST_CONFIG)
    base = base_rung_notional(TEST_CONFIG)
    tight = _cycle("2026-07-05_DAY", [4000.0, 3970.0], prev_range=100.0)
    wide = _cycle("2026-07-05_NIGHT", [4000.0, 3910.0], prev_range=100.0)

    runner.run_plan(
        tight.cycle_id,
        _plan(tight.cycle_id, floor=3980.0),
        tight.bars,
        prev_range=tight.prev_range,
        trend_gate_armed=False,
    )
    tight_fills_path = tmp_path / "outputs" / "dualtrack" / "fills" / f"{tight.cycle_id}_machine.json"
    tight_fills = json.loads(tight_fills_path.read_text(encoding="utf-8"))
    tight_entries = [fill for fill in tight_fills if fill["event"] == "entry"]

    runner.run_plan(
        wide.cycle_id,
        _plan(wide.cycle_id, floor=3920.0),
        wide.bars,
        prev_range=wide.prev_range,
        trend_gate_armed=False,
    )
    wide_fills_path = tmp_path / "outputs" / "dualtrack" / "fills" / f"{wide.cycle_id}_machine.json"
    wide_fills = json.loads(wide_fills_path.read_text(encoding="utf-8"))
    wide_entries = [fill for fill in wide_fills if fill["event"] == "entry"]

    assert len(tight_entries) < len(wide_entries)
    assert {float(fill["notional"]) for fill in tight_entries} == {base}
    assert {float(fill["notional"]) for fill in wide_entries} == {base}
    assert sum(float(fill["notional"]) for fill in tight_entries) < sum(float(fill["notional"]) for fill in wide_entries)

    runner.run_plan(
        tight.cycle_id,
        _plan(tight.cycle_id, floor=3980.0),
        tight.bars,
        prev_range=tight.prev_range,
        trend_gate_armed=True,
    )
    armed_fills = json.loads(tight_fills_path.read_text(encoding="utf-8"))
    grid_entries = [fill for fill in armed_fills if fill["event"] == "entry" and fill["layer"] == "grid"]
    trend_entries = [fill for fill in armed_fills if fill["event"] == "entry" and fill["layer"] == "trend"]

    assert {float(fill["notional"]) for fill in grid_entries} == {base}
    assert {float(fill["notional"]) for fill in trend_entries} == {
        base * float(TEST_CONFIG["grid"]["trend_leg_budget_pct"]) / 100.0
    }


def test_lab_stop_none_path_still_uses_range_k_prev_range_geometry() -> None:
    cycle = _cycle("lab_stop_none_DAY", [4000.0, 3920.0], prev_range=80.0)

    result = simulate_conditional_grid(
        cycle_id=cycle.cycle_id,
        bars=cycle.bars,
        direction=1,
        prev_range=cycle.prev_range,
        spacing_bp=20.0,
        range_k=1.0,
        rung_notional=1000.0,
        max_rungs=10,
        cost_per_side_bp=0.5,
        re_arm_max=0,
        budget_sizing=False,
        stop=None,
    )
    entry_prices = [fill["price"] for fill in result.fills if fill["event"] == "entry"]

    assert entry_prices == [
        3992.0,
        3984.0,
        3976.0,
        3968.0,
        3960.0,
        3952.0,
        3944.0,
        3936.0,
        3928.0,
        3920.0,
    ]


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
