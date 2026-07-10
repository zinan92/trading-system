from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from schemas.market_data import Bar
from services.dualtrack_human import DualTrackHumanEngine
from services.dualtrack_machine import DualTrackMachineRunner
from services.dualtrack_store import DualTrackPlanStore
from services.journal_store import load_json
from tests.test_dualtrack_dt2_machine_runner import TEST_CONFIG, _mgc_config


def _bar(ts: datetime, o: float, h: float, low: float, c: float) -> Bar:
    return Bar(symbol="GOLD", timeframe="1m", timestamp=ts.isoformat(), open=o, high=h, low=low, close=c, volume=1, provider="test")


def _bars() -> list[Bar]:
    start = datetime(2026, 7, 5, 1, 0, tzinfo=timezone.utc)
    closes = [4000.0, 3990.0, 4001.0]
    rows = []
    prev = closes[0]
    for i, close in enumerate(closes):
        rows.append(_bar(start + timedelta(minutes=i), prev, max(prev, close), min(prev, close), close))
        prev = close
    return rows


def _plan(cycle_id: str = "2026-07-05_DAY") -> dict:
    return {
        "cycle_id": cycle_id,
        "direction": "long",
        "range": {"low": 3940.0, "high": 4050.0},
        "key_levels": [3992.0],
        "invalidation": [{"side": "below", "price": 3940.0, "confirm": "touch"}],
        "confidence": 7,
    }


def test_invariant_6_track_isolation_and_identical_cost_model(tmp_path: Path) -> None:
    cycle_id = "2026-07-05_DAY"
    store = DualTrackPlanStore(tmp_path / "outputs", config=TEST_CONFIG)
    store.save_human_plan(_plan(cycle_id), now="2026-07-05T00:59:00+00:00")
    machine = DualTrackMachineRunner(tmp_path / "outputs", config=TEST_CONFIG)
    human = DualTrackHumanEngine(tmp_path / "outputs", config=TEST_CONFIG)

    machine.run_plan(cycle_id, store.effective_plan(cycle_id, as_of="2026-07-05T01:00:00+00:00"), _bars(), prev_range=40.0, trend_gate_armed=False)
    human.submit_order({
        "cycle_id": cycle_id,
        "ts": "2026-07-05T01:01:00+00:00",
        "side": "buy",
        "order_type": "market",
        "price": 4000.0,
        "notional": 1000.0,
        "sl": 3980.0,
        "tp": 4020.0,
    })

    machine_fills = load_json(tmp_path / "outputs" / "dualtrack" / "fills" / f"{cycle_id}_machine.json")
    human_fills = load_json(tmp_path / "outputs" / "dualtrack" / "fills" / f"{cycle_id}_human.json")
    machine_account = load_json(tmp_path / "outputs" / "dualtrack" / "accounts" / f"{cycle_id}_machine.json")[0]
    human_account = load_json(tmp_path / "outputs" / "dualtrack" / "accounts" / f"{cycle_id}_human.json")[0]

    assert machine_fills and all(fill["track"] == "machine" for fill in machine_fills)
    assert human_fills and all(fill["track"] == "human" for fill in human_fills)
    assert machine_account["track"] == "machine"
    assert human_account["track"] == "human"
    assert machine_account["starting_cash"] == human_account["starting_cash"] == 10_000
    assert machine_account["cost_model"] == human_account["cost_model"]
    assert machine_fills[0]["cost_model"] == human_fills[0]["cost_model"]


def test_invariant_7_out_of_plan_human_order_executes_but_is_flagged(tmp_path: Path) -> None:
    cycle_id = "2026-07-05_DAY"
    store = DualTrackPlanStore(tmp_path / "outputs", config=TEST_CONFIG)
    store.save_human_plan(_plan(cycle_id), now="2026-07-05T00:59:00+00:00")
    human = DualTrackHumanEngine(tmp_path / "outputs", config=TEST_CONFIG)

    fill = human.submit_order({
        "cycle_id": cycle_id,
        "ts": "2026-07-05T01:02:00+00:00",
        "side": "sell",
        "order_type": "limit",
        "price": 4100.0,
        "notional": 1000.0,
        "sl": 4120.0,
        "tp": 4060.0,
    })

    assert fill["out_of_plan"] is True
    assert fill["side"] == "sell"
    assert fill["realized_pnl"] < 0
    saved = load_json(tmp_path / "outputs" / "dualtrack" / "fills" / f"{cycle_id}_human.json")
    assert saved[-1]["fill_id"] == fill["fill_id"]


def test_in_plan_human_order_is_not_flagged(tmp_path: Path) -> None:
    cycle_id = "2026-07-05_DAY"
    store = DualTrackPlanStore(tmp_path / "outputs", config=TEST_CONFIG)
    store.save_human_plan(_plan(cycle_id), now="2026-07-05T00:59:00+00:00")
    human = DualTrackHumanEngine(tmp_path / "outputs", config=TEST_CONFIG)

    fill = human.submit_order({
        "cycle_id": cycle_id,
        "ts": "2026-07-05T01:02:00+00:00",
        "side": "buy",
        "order_type": "market",
        "price": 4000.0,
        "notional": 1000.0,
    })

    assert fill["out_of_plan"] is False


@pytest.mark.parametrize(
    ("side", "sl", "tp", "message"),
    [
        ("buy", 4000.0, 4020.0, "long stop must be below entry"),
        ("buy", 3980.0, 4000.0, "long target must be above entry"),
        ("sell", 4000.0, 3980.0, "short stop must be above entry"),
        ("sell", 4020.0, 4000.0, "short target must be below entry"),
    ],
)
def test_human_entry_rejects_invalid_protective_geometry(
    tmp_path: Path,
    side: str,
    sl: float,
    tp: float,
    message: str,
) -> None:
    human = DualTrackHumanEngine(tmp_path / "outputs", config=TEST_CONFIG)

    with pytest.raises(ValueError, match=message):
        human.submit_order({
            "cycle_id": "2026-07-05_DAY",
            "ts": "2026-07-05T01:02:00+00:00",
            "side": side,
            "event": "entry",
            "order_type": "limit",
            "price": 4000.0,
            "notional": 1000.0,
            "sl": sl,
            "tp": tp,
        })


def test_human_order_rejects_timestamp_outside_requested_cycle(tmp_path: Path) -> None:
    human = DualTrackHumanEngine(tmp_path / "outputs", config=TEST_CONFIG)

    with pytest.raises(ValueError, match="order timestamp does not belong to cycle"):
        human.submit_order({
            "cycle_id": "2026-07-05_DAY",
            "ts": "2026-07-05T13:02:00+00:00",
            "side": "buy",
            "event": "entry",
            "order_type": "market",
            "price": 4000.0,
            "notional": 1000.0,
        })


def test_human_entry_exit_pair_realizes_price_pnl_and_writes_trade(tmp_path: Path) -> None:
    cycle_id = "2026-07-05_DAY"
    store = DualTrackPlanStore(tmp_path / "outputs", config=TEST_CONFIG)
    store.save_human_plan(_plan(cycle_id), now="2026-07-05T00:59:00+00:00")
    human = DualTrackHumanEngine(tmp_path / "outputs", config=TEST_CONFIG)

    entry = human.submit_order({
        "cycle_id": cycle_id,
        "ts": "2026-07-05T01:02:00+00:00",
        "side": "buy",
        "order_type": "limit",
        "price": 4000.0,
        "notional": 1000.0,
        "position_id": "manual-a",
    })
    exit_fill = human.submit_order({
        "cycle_id": cycle_id,
        "ts": "2026-07-05T03:30:00+00:00",
        "side": "sell",
        "order_type": "market",
        "price": 4020.0,
        "position_id": "manual-a",
    })

    fills = load_json(tmp_path / "outputs" / "dualtrack" / "fills" / f"{cycle_id}_human.json")
    trades = load_json(tmp_path / "outputs" / "dualtrack" / "trades" / f"{cycle_id}_human.json")
    account = load_json(tmp_path / "outputs" / "dualtrack" / "accounts" / f"{cycle_id}_human.json")[0]

    assert entry["event"] == "entry"
    assert exit_fill["event"] == "exit"
    assert exit_fill["out_of_plan"] is False
    assert exit_fill["gross_pnl"] == pytest.approx(5.0)
    assert account["realized_pnl"] == pytest.approx(4.89975)
    assert fills[0]["position_status"] == "closed"
    assert trades[0]["status"] == "closed"
    assert trades[0]["realized_pnl"] == pytest.approx(4.89975)


def test_human_protective_sweep_executes_short_stop_once_at_sl_price(tmp_path: Path) -> None:
    cycle_id = "2026-07-05_DAY"
    human = DualTrackHumanEngine(tmp_path / "outputs", config=TEST_CONFIG)

    entry = human.submit_order({
        "cycle_id": cycle_id,
        "ts": "2026-07-05T01:02:00+00:00",
        "side": "sell",
        "order_type": "limit",
        "price": 100.0,
        "notional": 1000.0,
        "sl": 105.0,
        "tp": 90.0,
        "position_id": "manual-short",
    })
    sweep = human.sweep_protective_exits(
        cycle_id,
        mark_price=106.0,
        ts="2026-07-05T01:05:00+00:00",
        source="test_websocket",
    )
    repeat = human.sweep_protective_exits(
        cycle_id,
        mark_price=106.0,
        ts="2026-07-05T01:06:00+00:00",
        source="test_websocket",
    )

    fills = load_json(tmp_path / "outputs" / "dualtrack" / "fills" / f"{cycle_id}_human.json")
    trades = load_json(tmp_path / "outputs" / "dualtrack" / "trades" / f"{cycle_id}_human.json")

    assert entry["trade_id"]
    assert sweep["status"] == "triggered"
    assert sweep["triggered"][0]["event"] == "stop"
    assert repeat["triggered"] == []
    assert len(fills) == 2
    assert fills[-1]["event"] == "stop"
    assert fills[-1]["side"] == "buy"
    assert fills[-1]["price"] == 105.0
    assert fills[-1]["trigger_mark_price"] == 106.0
    assert trades[0]["status"] == "closed"
    assert trades[0]["remaining_units"] == 0.0
    assert trades[0]["realized_pnl"] == pytest.approx(-50.1025)


def test_human_paper_fee_model_charges_taker_entry_and_maker_target(tmp_path: Path) -> None:
    cycle_id = "2026-07-05_DAY"
    config = {
        **TEST_CONFIG,
        "paper_fee_model": {
            "maker_fee_rate": "0",
            "taker_fee_rate": "0.0004",
            "source": "account_observed_test",
            "real_money_eligible": False,
        },
    }
    human = DualTrackHumanEngine(tmp_path / "outputs", config=config)

    entry = human.submit_order({
        "cycle_id": cycle_id,
        "ts": "2026-07-05T01:02:00+00:00",
        "side": "buy",
        "order_type": "market",
        "price": 100.0,
        "notional": 100.0,
        "sl": 95.0,
        "tp": 105.0,
    })
    human.sweep_protective_exits(
        cycle_id,
        mark_price=106.0,
        mark_open=100.0,
        mark_high=106.0,
        mark_low=99.0,
        event_started_at="2026-07-05T01:03:00+00:00",
        ts="2026-07-05T01:03:00+00:00",
    )
    target = load_json(tmp_path / "outputs" / "dualtrack" / "fills" / f"{cycle_id}_human.json")[-1]

    assert entry["cost"] == pytest.approx(0.04)
    assert entry["cost_model"]["liquidity"] == "taker"
    assert target["event"] == "target"
    assert target["order_type"] == "limit"
    assert target["cost"] == 0.0
    assert target["cost_model"]["liquidity"] == "maker"
    assert target["realized_pnl"] == pytest.approx(5.0)


def test_human_protective_sweep_filters_by_trade_id_when_position_id_is_shared(tmp_path: Path) -> None:
    cycle_id = "2026-07-05_DAY"
    human = DualTrackHumanEngine(tmp_path / "outputs", config=TEST_CONFIG)

    first = human.submit_order({
        "cycle_id": cycle_id,
        "ts": "2026-07-05T01:02:00+00:00",
        "side": "sell",
        "order_type": "limit",
        "price": 100.0,
        "notional": 1000.0,
        "sl": 105.0,
        "tp": 90.0,
        "position_id": "manual",
    })
    second = human.submit_order({
        "cycle_id": cycle_id,
        "ts": "2026-07-05T01:03:00+00:00",
        "side": "sell",
        "order_type": "limit",
        "price": 100.0,
        "notional": 1000.0,
        "sl": 120.0,
        "tp": 90.0,
        "position_id": "manual",
    })

    human.sweep_protective_exits(cycle_id, mark_price=106.0, ts="2026-07-05T01:05:00+00:00")
    trades = {row["trade_id"]: row for row in load_json(tmp_path / "outputs" / "dualtrack" / "trades" / f"{cycle_id}_human.json")}

    assert trades[first["trade_id"]]["status"] == "closed"
    assert trades[second["trade_id"]]["status"] == "open"
    assert trades[second["trade_id"]]["remaining_units"] > 0


def test_human_tiger_mgc_mode_records_contracts_and_fixed_side_cost(tmp_path: Path) -> None:
    cycle_id = "2026-07-05_DAY"
    human = DualTrackHumanEngine(tmp_path / "outputs", config=_mgc_config())

    fill = human.submit_order({
        "cycle_id": cycle_id,
        "ts": "2026-07-05T01:02:00+00:00",
        "side": "buy",
        "order_type": "market",
        "price": 4183.2,
        "contracts": 2,
    })
    account = load_json(tmp_path / "outputs" / "dualtrack" / "accounts" / f"{cycle_id}_human.json")[0]

    assert fill["contracts"] == 2
    assert fill["quantity"] == 2
    assert fill["notional"] == pytest.approx(4183.2 * 2 * 10)
    assert fill["cost"] == pytest.approx(5.4)
    assert fill["realized_pnl"] == pytest.approx(-5.4)
    assert fill["cost_model"]["venue"] == "tiger_mgc"
    assert account["cost_model"]["venue"] == "tiger_mgc"


def test_human_tiger_mgc_mode_requires_explicit_contracts(tmp_path: Path) -> None:
    human = DualTrackHumanEngine(tmp_path / "outputs", config=_mgc_config())

    with pytest.raises(ValueError, match="contracts is required"):
        human.submit_order({
            "cycle_id": "2026-07-05_DAY",
            "ts": "2026-07-05T01:02:00+00:00",
            "side": "buy",
            "order_type": "market",
            "price": 4183.2,
            "notional": 41832.0,
        })


def test_open_ended_long_plan_skips_missing_high_but_enforces_floor(tmp_path: Path) -> None:
    cycle_id = "2026-07-05_DAY"
    plan = _plan(cycle_id)
    plan["range"] = {"low": 3940.0, "high": None}
    store = DualTrackPlanStore(tmp_path / "outputs", config=TEST_CONFIG)
    store.save_human_plan(plan, now="2026-07-05T00:59:00+00:00")
    human = DualTrackHumanEngine(tmp_path / "outputs", config=TEST_CONFIG)

    high_fill = human.submit_order({
        "cycle_id": cycle_id,
        "ts": "2026-07-05T01:02:00+00:00",
        "side": "buy",
        "order_type": "market",
        "price": 4100.0,
        "notional": 1000.0,
    })
    floor_break_fill = human.submit_order({
        "cycle_id": cycle_id,
        "ts": "2026-07-05T01:03:00+00:00",
        "side": "buy",
        "order_type": "market",
        "price": 3939.0,
        "notional": 1000.0,
    })

    assert high_fill["out_of_plan"] is False
    assert floor_break_fill["out_of_plan"] is True
