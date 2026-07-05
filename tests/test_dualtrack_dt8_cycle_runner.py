from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from pipelines.dualtrack_cycle_runner import DualTrackCycleRunner
from schemas.market_data import Bar
from services.dualtrack_config import base_rung_notional
from services.dualtrack_grid_core import GridStop, simulate_conditional_grid
from services.dualtrack_machine import DualTrackMachineRunner
from services.dualtrack_store import DualTrackPlanStore, validate_plan
from services.journal_store import load_json, write_json
from services.market_store import MarketStore
from tests.test_dualtrack_dt2_machine_runner import TEST_CONFIG


def _bar(ts: datetime, open_: float, close: float) -> Bar:
    return Bar(
        symbol="GOLD",
        timeframe="1m",
        timestamp=ts.isoformat(),
        open=open_,
        high=max(open_, close),
        low=min(open_, close),
        close=close,
        volume=1,
        provider="test",
    )


def _seed_bars(store: MarketStore, start: datetime, closes: list[float]) -> list[Bar]:
    rows = []
    previous = closes[0]
    for index, close in enumerate(closes):
        rows.append(_bar(start + timedelta(minutes=index), previous, close))
        previous = close
    store.upsert_bars(rows)
    return rows


def _plan(cycle_id: str, direction: str = "long") -> dict:
    if direction == "flat":
        return {"cycle_id": cycle_id, "direction": "flat", "confidence": 5}
    if direction == "short":
        return {
            "cycle_id": cycle_id,
            "direction": "short",
            "range": {"high": 4040.0},
            "key_levels": [3992.0],
            "invalidation": [{"side": "above", "price": 4040.0, "confirm": "touch"}],
            "confidence": 7,
        }
    return {
        "cycle_id": cycle_id,
        "direction": "long",
        "range": {"low": 3960.0, "high": None},
        "key_levels": [3992.0],
        "invalidation": [{"side": "below", "price": 3960.0, "confirm": "touch"}],
        "confidence": 7,
    }


def _write_market_view(output: Path, date: str = "2026-07-05", *, expire_below: float = 3960.0) -> None:
    write_json(
        output / "market_views" / f"{date}.json",
        [
            {
                "run_date": date,
                "generated_at": f"{date}T00:30:00+00:00",
                "direction_score": 80,
                "direction_bias": "strong_long",
                "key_levels": ["3992"],
                "expiry": {"expire_below": expire_below},
            }
        ],
    )


def _seed_previous_and_day(db: Path) -> MarketStore:
    store = MarketStore(db)
    _seed_bars(store, datetime(2026, 7, 4, 13, 0, tzinfo=timezone.utc), [4000.0, 3960.0, 4040.0])
    _seed_bars(store, datetime(2026, 7, 5, 1, 0, tzinfo=timezone.utc), [4000.0, 3990.0, 4002.0, 3990.0, 4002.0])
    return store


def test_d8_1_prefix_replay_matches_batch_runner_on_same_prefix(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    _seed_previous_and_day(db)
    cycle_id = "2026-07-05_DAY"
    output = tmp_path / "outputs"
    expected_output = tmp_path / "expected_outputs"
    DualTrackPlanStore(output, config=TEST_CONFIG).save_human_plan(_plan(cycle_id), now="2026-07-05T00:59:00+00:00")
    DualTrackPlanStore(expected_output, config=TEST_CONFIG).save_human_plan(_plan(cycle_id), now="2026-07-05T00:59:00+00:00")
    runner = DualTrackCycleRunner(output_root=output, market_db=db, config=TEST_CONFIG)

    runner.intraday_tick(cycle_id, as_of="2026-07-05T01:04:00+00:00")
    actual = load_json(output / "dualtrack" / "fills" / f"{cycle_id}_machine.json")

    prefix = MarketStore(db).load_bars_between("GOLD", "1m", "2026-07-05T01:00:00+00:00", "2026-07-05T01:04:00+00:00")
    DualTrackMachineRunner(expected_output, config=TEST_CONFIG).run_effective_plan(
        cycle_id,
        prefix,
        prev_range=runner.previous_cycle_range(cycle_id),
        as_of="2026-07-05T01:04:00+00:00",
    )
    expected = load_json(expected_output / "dualtrack" / "fills" / f"{cycle_id}_machine.json")

    assert actual == expected


def test_d8_2_missing_market_view_fails_closed_and_machine_stands_down(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    _seed_previous_and_day(db)
    output = tmp_path / "outputs"
    runner = DualTrackCycleRunner(output_root=output, market_db=db, config=TEST_CONFIG)

    pre = runner.pre_cycle("2026-07-05_DAY", as_of="2026-07-05T01:00:00+00:00")
    tick = runner.intraday_tick("2026-07-05_DAY", as_of="2026-07-05T01:04:00+00:00")

    assert pre["status"] == "fail_closed_no_ai_plan"
    assert tick["state"]["machine_stood_down"] is True
    assert load_json(output / "dualtrack" / "fills" / "2026-07-05_DAY_machine.json") == []
    audit_events = [row["event"] for row in load_json(output / "dualtrack" / "audit" / "2026-07-05_DAY.json")]
    assert "ai_plan_absent" in audit_events


def test_d8_3_intraday_tick_is_idempotent_for_same_bar_set(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    _seed_previous_and_day(db)
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    DualTrackPlanStore(output, config=TEST_CONFIG).save_human_plan(_plan(cycle_id), now="2026-07-05T00:59:00+00:00")
    runner = DualTrackCycleRunner(output_root=output, market_db=db, config=TEST_CONFIG)

    runner.intraday_tick(cycle_id, as_of="2026-07-05T01:04:00+00:00")
    first = load_json(output / "dualtrack" / "fills" / f"{cycle_id}_machine.json")
    runner.intraday_tick(cycle_id, as_of="2026-07-05T01:04:00+00:00")
    second = load_json(output / "dualtrack" / "fills" / f"{cycle_id}_machine.json")

    assert second == first


def test_d8_4_close_cycle_is_single_shot(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    _seed_previous_and_day(db)
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    DualTrackPlanStore(output, config=TEST_CONFIG).save_human_plan(_plan(cycle_id), now="2026-07-05T00:59:00+00:00")
    runner = DualTrackCycleRunner(output_root=output, market_db=db, config=TEST_CONFIG)

    first = runner.close_cycle(cycle_id, as_of="2026-07-05T13:00:00+00:00")
    second = runner.close_cycle(cycle_id, as_of="2026-07-05T13:00:00+00:00")

    assert first["status"] == "closed"
    assert second["status"] == "already_closed"
    assert second["attribution"] == first["attribution"]
    scoreboard = load_json(output / "dualtrack" / "scoreboard.json")[-1]
    assert [row["cycle_id"] for row in scoreboard["history"]["human"]] == [cycle_id]
    daily = load_json(output / "dualtrack" / "ledger" / "daily" / "2026-07-05.json")[-1]
    assert list(daily["cycles"]) == [cycle_id]


def test_d8_5_orchestrated_intraday_machine_payload_stays_pnl_only(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    _seed_previous_and_day(db)
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    DualTrackPlanStore(output, config=TEST_CONFIG).save_human_plan(_plan(cycle_id), now="2026-07-05T00:59:00+00:00")
    DualTrackCycleRunner(output_root=output, market_db=db, config=TEST_CONFIG).intraday_tick(cycle_id, as_of="2026-07-05T01:04:00+00:00")

    payload = DualTrackMachineRunner(output, config=TEST_CONFIG).machine_payload(cycle_id, as_of="2026-07-05T01:04:00+00:00")

    assert set(payload) == {"realized_pnl", "unrealized_pnl", "layers"}
    assert {"fills", "orders", "entries", "inventory", "rungs", "price", "notional", "sl", "tp"}.isdisjoint(payload)


def test_d8_6_open_ended_directional_schema_and_flat_stand_down(tmp_path: Path) -> None:
    normalized = validate_plan(
        {
            "cycle_id": "2026-07-05_DAY",
            "direction": "long",
            "range": {},
            "key_levels": [4210.0],
            "invalidation": [{"side": "below", "price": 4160.0, "confirm": "close_1m"}],
        },
        author="human",
        status="locked",
    )
    assert normalized["range"] == {"low": 4160.0, "high": None}

    flat = validate_plan({"cycle_id": "2026-07-05_DAY", "direction": "flat"}, author="human", status="locked")
    assert flat["range"] == {"low": None, "high": None}
    assert flat["key_levels"] == []

    db = tmp_path / "market_data.db"
    _seed_previous_and_day(db)
    output = tmp_path / "outputs"
    cycle_id = "2026-07-05_DAY"
    DualTrackPlanStore(output, config=TEST_CONFIG).save_human_plan(_plan(cycle_id, "flat"), now="2026-07-05T00:59:00+00:00")

    state = DualTrackCycleRunner(output_root=output, market_db=db, config=TEST_CONFIG).intraday_tick(
        cycle_id,
        as_of="2026-07-05T01:04:00+00:00",
    )["state"]

    assert state["machine_stood_down"] is True
    assert state["layers"] == ["grid:stand_down:flat_plan", "trend:stand_down:flat_plan"]


def test_dt8_acceptance_fast_forward_day_produces_two_unattended_cycles(tmp_path: Path) -> None:
    db = tmp_path / "market_data.db"
    store = _seed_previous_and_day(db)
    _seed_bars(store, datetime(2026, 7, 5, 13, 0, tzinfo=timezone.utc), [4010.0, 4000.0, 4012.0, 4000.0, 4012.0])
    output = tmp_path / "outputs"
    tight_floor = 3980.0
    _write_market_view(output, expire_below=tight_floor)

    runner = DualTrackCycleRunner(output_root=output, market_db=db, config=TEST_CONFIG)
    result = runner.fast_forward_day("2026-07-05")

    closed = [row for row in result["results"] if row["event"] == "close" and row["status"] == "closed"]
    assert [row["cycle_id"] for row in closed] == ["2026-07-05_DAY", "2026-07-05_NIGHT"]
    assert load_json(output / "dualtrack" / "plans" / "2026-07-05_DAY_ai.json")
    assert load_json(output / "dualtrack" / "plans" / "2026-07-05_NIGHT_ai.json")
    assert load_json(output / "dualtrack" / "fills" / "2026-07-05_DAY_machine.json")
    ledger = load_json(output / "dualtrack" / "ledger" / "daily" / "2026-07-05.json")[-1]
    assert set(ledger["cycles"]) == {"2026-07-05_DAY", "2026-07-05_NIGHT"}
    assert abs(float(ledger["tracks"]["machine"]["realized_pnl"])) < 100.0
    day_fills = load_json(output / "dualtrack" / "fills" / "2026-07-05_DAY_machine.json")
    day_pnl = sum(float(fill["realized_pnl"]) for fill in day_fills)
    day_budgeted = simulate_conditional_grid(
        cycle_id="2026-07-05_DAY",
        bars=store.load_bars_between("GOLD", "1m", "2026-07-05T01:00:00+00:00", "2026-07-05T01:04:00+00:00"),
        direction=1,
        prev_range=runner.previous_cycle_range("2026-07-05_DAY"),
        spacing_bp=float(TEST_CONFIG["grid"]["spacing_bp"]),
        range_k=float(TEST_CONFIG["grid"]["range_k"]),
        rung_notional=base_rung_notional(TEST_CONFIG),
        max_rungs=10,
        cost_per_side_bp=float(TEST_CONFIG["cost_per_side_bp"]),
        tp_mult=float(TEST_CONFIG["grid"]["tp_mult_base"]),
        re_arm_max=int(TEST_CONFIG["grid"]["re_arm_max"]),
        budget_sizing=True,
        stop=GridStop(side="below", price=tight_floor),
    )
    day_budgeted_pnl = sum(float(fill["realized_pnl"]) for fill in day_budgeted.fills)
    assert abs(day_pnl) < abs(day_budgeted_pnl)
    stop_fills = [
        fill
        for cycle_id in ("2026-07-05_DAY", "2026-07-05_NIGHT")
        for fill in load_json(output / "dualtrack" / "fills" / f"{cycle_id}_machine.json")
        if fill["event"] == "stop"
    ]
    assert all(float(fill["realized_pnl"]) <= 0 for fill in stop_fills)


def test_dt8_empty_market_db_skip_paths_and_auto_are_safe(tmp_path: Path) -> None:
    runner = DualTrackCycleRunner(output_root=tmp_path / "outputs", market_db=tmp_path / "empty.db", config=TEST_CONFIG)
    cycle_id = "2026-07-05_DAY"

    assert runner.previous_cycle_range(cycle_id) == 0.0
    assert runner.pre_cycle(cycle_id, as_of="2026-07-05T01:00:00+00:00")["status"] == "skipped"
    assert runner.intraday_tick(cycle_id, as_of="2026-07-04T23:00:00+00:00")["status"] == "skipped"
    assert runner.close_cycle(cycle_id, as_of="2026-07-05T13:00:00+00:00")["status"] == "skipped"

    auto = runner.auto(as_of="2026-07-05T01:00:00+00:00")

    assert auto["event"] == "auto"
    assert [row["status"] for row in auto["results"]] == ["skipped", "skipped", "skipped"]
