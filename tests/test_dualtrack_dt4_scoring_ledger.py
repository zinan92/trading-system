from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from schemas.market_data import Bar
from services.dualtrack_human import DualTrackHumanEngine
from services.dualtrack_machine import DualTrackMachineRunner
from services.dualtrack_scoring import DualTrackScorer
from services.dualtrack_store import DualTrackPlanStore
from services.journal_store import load_json, write_json
from services.strategy_cycle_package import _hash_payload
from tests.test_dualtrack_dt2_machine_runner import TEST_CONFIG


def _bar(ts: datetime, o: float, h: float, low: float, c: float) -> Bar:
    return Bar(symbol="GOLD", timeframe="1m", timestamp=ts.isoformat(), open=o, high=h, low=low, close=c, volume=1, provider="test")


def _bars(start: datetime, closes: list[float]) -> list[Bar]:
    rows = []
    prev = closes[0]
    for i, close in enumerate(closes):
        rows.append(_bar(start + timedelta(minutes=i), prev, max(prev, close), min(prev, close), close))
        prev = close
    return rows


def _plan(cycle_id: str, direction: str) -> dict:
    plan_range = {"low": 3950.0, "high": 4050.0}
    invalidation_price = 3940.0
    if direction == "long":
        plan_range = {"low": 3940.0, "high": 4050.0}
    elif direction == "short":
        plan_range = {"low": 3950.0, "high": 4060.0}
        invalidation_price = 4060.0
    return {
        "cycle_id": cycle_id,
        "direction": direction,
        "range": plan_range,
        "key_levels": [3992.0],
        "invalidation": [{"side": "below" if direction != "short" else "above", "price": invalidation_price, "confirm": "touch"}],
        "confidence": 7,
    }


def test_dt4_full_simulated_day_scores_two_cycles_and_writes_ledgers(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    store = DualTrackPlanStore(output, config=TEST_CONFIG)
    machine = DualTrackMachineRunner(output, config=TEST_CONFIG)
    human = DualTrackHumanEngine(output, config=TEST_CONFIG)
    scorer = DualTrackScorer(output, config=TEST_CONFIG)

    day_cycle = "2026-07-05_DAY"
    day_bars = _bars(datetime(2026, 7, 5, 1, 0, tzinfo=timezone.utc), [4000.0, 3990.0, 4005.0, 4020.0])
    store.save_human_plan(_plan(day_cycle, "long"), now="2026-07-05T00:59:00+00:00")
    store.save_ai_plan(_plan(day_cycle, "short") | {"author": "ai"}, now="2026-07-05T00:50:00+00:00")
    machine.run_effective_plan(day_cycle, day_bars, prev_range=80.0, as_of="2026-07-05T01:00:00+00:00")
    human.submit_order({"cycle_id": day_cycle, "ts": "2026-07-05T01:01:00+00:00", "side": "buy", "order_type": "market", "price": 4000, "notional": 1000})
    day_attr = scorer.close_cycle(day_cycle, day_bars)

    night_cycle = "2026-07-05_NIGHT"
    night_bars = _bars(datetime(2026, 7, 5, 13, 0, tzinfo=timezone.utc), [4020.0, 4030.0, 4005.0, 3990.0])
    store.save_human_plan(_plan(night_cycle, "short"), now="2026-07-05T12:59:00+00:00")
    store.save_ai_plan(_plan(night_cycle, "long") | {"author": "ai"}, now="2026-07-05T12:50:00+00:00")
    machine.run_effective_plan(night_cycle, night_bars, prev_range=80.0, as_of="2026-07-05T13:00:00+00:00")
    human.submit_order({"cycle_id": night_cycle, "ts": "2026-07-05T13:01:00+00:00", "side": "sell", "order_type": "market", "price": 4020, "notional": 1000})
    night_attr = scorer.close_cycle(night_cycle, night_bars)

    scoreboard = load_json(output / "dualtrack" / "scoreboard.json")[0]
    daily = load_json(output / "dualtrack" / "ledger" / "daily" / "2026-07-05.json")[0]
    weekly = load_json(output / "dualtrack" / "ledger" / "weekly" / "2026-W27.json")[0]

    assert day_attr["status"] == night_attr["status"] == "closed"
    assert day_attr["plan_grades"]["human"]["hit"] is True
    assert day_attr["plan_grades"]["ai"]["hit"] is False
    assert night_attr["plan_grades"]["human"]["hit"] is True
    assert scoreboard["human"]["graded"] == 2
    assert scoreboard["human"]["hits"] == 2
    assert scoreboard["trend_leg_gate"]["armed"] is True
    assert set(daily["cycles"]) == {day_cycle, night_cycle}
    assert weekly["days"][0]["date"] == "2026-07-05"
    assert (output / "dualtrack" / "attribution" / f"{day_cycle}.json").exists()
    assert (output / "dualtrack" / "attribution" / f"{night_cycle}.json").exists()


def test_acceptance_6_ledger_arithmetic_sums_fills_cycles_daily_and_weekly(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    store = DualTrackPlanStore(output, config=TEST_CONFIG)
    machine = DualTrackMachineRunner(output, config=TEST_CONFIG)
    human = DualTrackHumanEngine(output, config=TEST_CONFIG)
    scorer = DualTrackScorer(output, config=TEST_CONFIG)
    cycle_id = "2026-07-05_DAY"
    bars = _bars(datetime(2026, 7, 5, 1, 0, tzinfo=timezone.utc), [4000.0, 3990.0, 4005.0, 4020.0])
    store.save_human_plan(_plan(cycle_id, "long"), now="2026-07-05T00:59:00+00:00")
    machine.run_effective_plan(cycle_id, bars, prev_range=80.0, as_of="2026-07-05T01:00:00+00:00")
    human.submit_order({"cycle_id": cycle_id, "ts": "2026-07-05T01:01:00+00:00", "side": "buy", "order_type": "market", "price": 4000, "notional": 1000})

    attr = scorer.close_cycle(cycle_id, bars)

    machine_sum = sum(fill["realized_pnl"] for fill in load_json(output / "dualtrack" / "fills" / f"{cycle_id}_machine.json"))
    human_sum = sum(fill["realized_pnl"] for fill in load_json(output / "dualtrack" / "fills" / f"{cycle_id}_human.json"))
    cycle = load_json(output / "dualtrack" / "cycles" / f"{cycle_id}.json")[0]
    daily = load_json(output / "dualtrack" / "ledger" / "daily" / "2026-07-05.json")[0]
    weekly = load_json(output / "dualtrack" / "ledger" / "weekly" / "2026-W27.json")[0]

    assert round(machine_sum, 8) == cycle["machine_realized_pnl"] == attr["tracks"]["machine"]["realized_pnl"]
    assert round(human_sum, 8) == cycle["human_realized_pnl"] == attr["tracks"]["human"]["realized_pnl"]
    assert daily["cycles"][cycle_id]["machine"] == cycle["machine_realized_pnl"]
    assert daily["cycles"][cycle_id]["human"] == cycle["human_realized_pnl"]
    assert daily["total_pnl"] == round(cycle["machine_realized_pnl"] + cycle["human_realized_pnl"], 8)
    assert weekly["total_pnl"] == daily["total_pnl"]


def test_rebuild_ledgers_skips_malformed_legacy_cycle_dates_and_records_diagnostic(
    tmp_path: Path,
) -> None:
    output = tmp_path / "outputs"
    scorer = DualTrackScorer(output, config=TEST_CONFIG)
    write_json(output / "dualtrack" / "fills" / "2026-07-05_DAY_machine.json", [])
    write_json(
        output / "dualtrack" / "fills" / "deploy-canary-635260f-20260721T0310Z_machine.json",
        [],
    )

    scorer.rebuild_ledgers()

    assert (output / "dualtrack" / "ledger" / "weekly" / "2026-W27.json").exists()
    diagnostics = load_json(
        output / "dualtrack" / "ledger" / "diagnostics" / "invalid_dates.json"
    )
    assert diagnostics == [{
        "value": "deploy-canary-635260f-20260721T0310Z",
        "source": "rebuild_ledgers.cycle_id",
        "reason": "non_iso_date",
        "recorded_at": diagnostics[0]["recorded_at"],
    }]


def test_rebuild_ledgers_projects_verified_terminal_production_trades(
    tmp_path: Path,
) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-28_DAY"
    scorer = DualTrackScorer(output, config=TEST_CONFIG)
    write_json(output / "dualtrack" / "fills" / f"{cycle_id}_machine.json", [])
    package = {
        "schema_version": "strategy-cycle-package-v1",
        "cycle_id": cycle_id,
        "status": "closed",
        "blockers": [],
        "execution": {
            "fills": [
                {"fill_id": "entry-a", "event": "entry"},
                {"fill_id": "exit-a", "event": "target"},
                {"fill_id": "entry-b", "event": "entry"},
                {"fill_id": "exit-b", "event": "flatten"},
                {"fill_id": "entry-c", "event": "entry"},
                {"fill_id": "exit-c", "event": "flatten"},
            ],
            "positions": [
                {"trade_id": "a", "status": "closed", "realized_pnl": 11.01387},
                {"trade_id": "b", "status": "closed", "realized_pnl": -14.71923648},
                {"trade_id": "c", "status": "closed", "realized_pnl": -3.767825},
            ],
            "pnl": {"realized": -7.47319148, "unrealized": 0.0},
            "reconciliation": {"status": "ok", "issues": []},
        },
    }
    package["package_hash"] = _hash_payload(package)
    write_json(
        output / "dualtrack" / "strategy_cycle_packages" / f"{cycle_id}.json",
        [package],
    )

    scorer.rebuild_ledgers()

    daily = load_json(
        output / "dualtrack" / "ledger" / "daily" / "2026-07-28.json"
    )[-1]
    assert daily["cycles"][cycle_id]["machine"] == pytest.approx(-7.47319148)
    assert daily["cycles"][cycle_id]["machine_trade_count"] == 3
    assert daily["cycles"][cycle_id]["machine_fill_count"] == 6
    assert daily["cycles"][cycle_id]["machine_source"] == (
        "verified_strategy_cycle_package.execution"
    )
    assert daily["tracks"]["machine"]["trade_count"] == 3
    assert daily["tracks"]["machine"]["fill_count"] == 6
    assert daily["tracks"]["machine"]["realized_pnl"] == pytest.approx(-7.47319148)
    assert daily["total_pnl"] == pytest.approx(-7.47319148)


def test_recovery_replay_is_preserved_but_excluded_from_paper_pnl(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    store = DualTrackPlanStore(output, config=TEST_CONFIG)
    machine = DualTrackMachineRunner(output, config=TEST_CONFIG)
    scorer = DualTrackScorer(output, config=TEST_CONFIG)
    cycle_id = "2026-07-05_DAY"
    bars = _bars(datetime(2026, 7, 5, 1, 0, tzinfo=timezone.utc), [4000.0, 3998.0, 4012.0, 4020.0])
    store.save_ai_plan(
        {
            **_plan(cycle_id, "long"),
            "author": "ai",
            "bracket": {"entry": 3998.0, "take_profit": 4010.0, "stop_loss": 3990.0, "notional": 1000.0},
        },
        now="2026-07-05T00:50:00+00:00",
    )
    machine.run_effective_plan(
        cycle_id,
        bars,
        prev_range=80.0,
        as_of="2026-07-05T13:00:00+00:00",
        execution_provenance={
            "origin": "recovery_replay",
            "classified_at": "2026-07-06T00:00:00+00:00",
            "live_observed_until": None,
            "reason": "runner_execution_evidence_missing",
        },
    )

    attr = scorer.close_cycle(cycle_id, bars)

    raw_fills = load_json(output / "dualtrack" / "fills" / f"{cycle_id}_machine.json")
    recovery_trades = load_json(output / "dualtrack" / "recovery_replay" / "trades" / f"{cycle_id}_machine.json")
    daily = load_json(output / "dualtrack" / "ledger" / "daily" / "2026-07-05.json")[0]
    assert raw_fills and all(fill["execution_origin"] == "recovery_replay" for fill in raw_fills)
    assert recovery_trades
    assert attr["fills"]["machine"] == []
    assert attr["tracks"]["machine"]["realized_pnl"] == 0.0
    assert attr["recovery_replay"]["eligible_for_paper_pnl"] is False
    assert attr["recovery_replay"]["fill_count"] == len(raw_fills)
    assert daily["cycles"][cycle_id]["machine"] == 0.0
    assert daily["cycles"][cycle_id]["recovery_replay"]["machine"] == pytest.approx(
        sum(fill["realized_pnl"] for fill in raw_fills)
    )
    assert daily["cycles"][cycle_id]["recovery_replay"]["eligible_for_recorded_pnl"] is True
    assert daily["tracks"]["machine"]["live_observed_realized_pnl"] == 0.0
    assert daily["tracks"]["machine"]["realized_pnl"] == pytest.approx(
        sum(fill["realized_pnl"] for fill in raw_fills)
    )


def test_12h_cycle_closes_human_manual_trade_and_ai_bracket_pnl(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    store = DualTrackPlanStore(output, config=TEST_CONFIG)
    machine = DualTrackMachineRunner(output, config=TEST_CONFIG)
    human = DualTrackHumanEngine(output, config=TEST_CONFIG)
    scorer = DualTrackScorer(output, config=TEST_CONFIG)
    cycle_id = "2026-07-05_DAY"
    bars = _bars(datetime(2026, 7, 5, 1, 0, tzinfo=timezone.utc), [4000.0, 3998.0, 4012.0, 4020.0])
    store.save_human_plan(_plan(cycle_id, "long"), now="2026-07-05T00:59:00+00:00")
    store.save_ai_plan(
        {
            **_plan(cycle_id, "long"),
            "author": "ai",
            "bracket": {"entry": 3998.0, "take_profit": 4010.0, "stop_loss": 3990.0, "notional": 1000.0},
        },
        now="2026-07-05T00:50:00+00:00",
    )
    machine.run_effective_plan(cycle_id, bars, prev_range=80.0, as_of="2026-07-05T01:00:00+00:00")
    human.submit_order({
        "cycle_id": cycle_id,
        "ts": "2026-07-05T01:01:00+00:00",
        "side": "buy",
        "order_type": "market",
        "price": 4000.0,
        "notional": 1000.0,
        "position_id": "manual-a",
    })
    human.submit_order({
        "cycle_id": cycle_id,
        "ts": "2026-07-05T12:59:00+00:00",
        "side": "sell",
        "order_type": "market",
        "price": 4020.0,
        "position_id": "manual-a",
    })

    attr = scorer.close_cycle(cycle_id, bars)

    assert attr["cycle"]["effective_plan_author"] == "ai"
    assert attr["tracks"]["human"]["trade_count"] == 1
    assert attr["tracks"]["machine"]["trade_count"] == 1
    assert attr["trades"]["human"][0]["status"] == "closed"
    assert attr["trades"]["machine"][0]["status"] == "closed"
    assert attr["fills"]["machine"][-1]["event"] == "target"
    assert attr["tracks"]["human"]["realized_pnl"] == pytest.approx(4.89975)
    assert attr["ledger"]["daily"]["cycles"][cycle_id]["delta_machine_minus_human"] == pytest.approx(
        attr["tracks"]["machine"]["realized_pnl"] - attr["tracks"]["human"]["realized_pnl"]
    )


def test_flat_plans_are_ungraded(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    store = DualTrackPlanStore(output, config=TEST_CONFIG)
    scorer = DualTrackScorer(output, config=TEST_CONFIG)
    cycle_id = "2026-07-05_DAY"
    bars = _bars(datetime(2026, 7, 5, 1, 0, tzinfo=timezone.utc), [4000.0, 4000.0, 4001.0])
    store.save_human_plan(_plan(cycle_id, "flat"), now="2026-07-05T00:59:00+00:00")

    attr = scorer.close_cycle(cycle_id, bars)

    assert attr["plan_grades"]["human"]["graded"] is False
    assert attr["scoreboard"]["human"]["graded"] == 0


def test_late_draft_human_plan_is_not_graded_as_blind_answer(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    store = DualTrackPlanStore(output, config=TEST_CONFIG)
    scorer = DualTrackScorer(output, config=TEST_CONFIG)
    cycle_id = "2026-07-05_DAY"
    bars = _bars(datetime(2026, 7, 5, 1, 0, tzinfo=timezone.utc), [4000.0, 4005.0, 4010.0])
    store.save_human_plan(_plan(cycle_id, "long"), now="2026-07-05T02:00:00+00:00", lock=False)

    attr = scorer.close_cycle(cycle_id, bars)

    assert attr["plan_grades"]["human"]["direction"] == "long"
    assert attr["plan_grades"]["human"]["eligible"] is False
    assert attr["plan_grades"]["human"]["graded"] is False
    assert attr["scoreboard"]["human"]["graded"] == 0
