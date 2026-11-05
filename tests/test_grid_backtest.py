"""The pre-start backtest (#1308): Paper's rules, no look-ahead, and it says when it cannot answer."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

import pytest

from services import grid_backtest as gb
from services.datafeed_market_client import DatafeedUnavailable
from services.dualtrack_config import dualtrack_config

START = datetime(2026, 9, 1, tzinfo=timezone.utc)
CONFIG = dualtrack_config(paper_gold_venue="okx")
INSTRUMENT = "XAU-USDT-SWAP"
SOURCE = "okx_swap_public"
SHAPE = {
    "direction": "long", "market_price": 100.0, "lower": 90.0, "upper": 110.0, "stop": 85.0, "target": None,
    "quantity": 2.0, "account_equity": 1000.0,
    "rungs": [{"rung": 1, "side": "buy", "price": 95.0, "take_profit": 100.0}, {"rung": 2, "side": "buy", "price": 100.0, "take_profit": 105.0}],
}


def bars(closes: list[float], start: datetime = START) -> list[gb.Bar]:
    return [gb.Bar(start + timedelta(minutes=i), closes[i - 1] if i else closes[0], price) for i, price in enumerate(closes)]


def test_shape_comes_from_the_plan_and_only_a_grid_has_one() -> None:
    plan = {"normalized_input": {"strategy_type": "grid", "direction": "long", "lower_price_boundary": 90, "upper_price_boundary": 110, "stop_price": 85},
            "market": {"price": 100}, "risk": {"per_order_quantity": 2, "account_equity": 1000, "grid_rungs": [{"rung": 1, "side": "buy", "price": 95, "take_profit": 100}]}}
    assert gb.shape_from_plan(plan)["rungs"] == [{"rung": 1, "side": "buy", "price": 95.0, "take_profit": 100.0}]
    with pytest.raises(gb.BacktestUnavailable):
        gb.shape_from_plan({"normalized_input": {"strategy_type": "dca"}})


def test_moving_the_plan_keeps_percent_distances_and_notional() -> None:
    laid = gb.scaled(SHAPE, 200.0, price_step="0.1", quantity_step="0.001")
    assert (laid["lower"], laid["upper"], laid["stop"]) == (180.0, 220.0, 170.0)
    assert laid["rungs"][0]["price"] == 190.0 and laid["rungs"][0]["take_profit"] == 200.0
    assert laid["quantity"] == 1.0 and laid["market_price"] == 200.0


def test_events_never_show_a_close_before_the_bar_ended() -> None:
    series = bars([100, 101, 102])
    events = gb.market_events(series, cycle_id="c", instrument=INSTRUMENT, source=SOURCE)
    assert [(e["ts_event"], e["price"]) for e in events] == [
        (START.isoformat(), 100), ((START + timedelta(minutes=1)).isoformat(), 100),
        ((START + timedelta(minutes=2)).isoformat(), 101), ((START + timedelta(minutes=3)).isoformat(), 102),
    ]


@pytest.mark.parametrize(("price", "reason"), [(110, "upper"), (90, "lower"), (100, None)])
def test_boundaries_end_a_run_as_they_do_in_paper(price: float, reason: str | None) -> None:
    assert gb.terminal_reason(SHAPE, price) == reason


def test_windows_step_through_the_history_and_only_full_ones_count() -> None:
    series = bars([100.0] * (9 * 1440))
    spans = gb.windows(series)
    assert [span[0].opened_at for span in spans] == [START + timedelta(hours=12 * i) for i in range(5)]
    assert all(len(span) == 7 * 1440 for span in spans)


def test_funding_charges_a_long_and_pays_a_short() -> None:
    fills = [{"ts": "2026-09-01T00:00:00+00:00", "side": "buy", "quantity": 2.0}]
    funding = [{"funding_time": "2026-09-01T08:00:00+00:00", "funding_rate": 0.001, "realized_rate": None}]
    prices = {"2026-09-01T08:00:00+00:00": 100.0}
    assert gb.funding_cost(fills, funding, prices) == pytest.approx(0.2)
    assert gb.funding_cost([{**fills[0], "side": "sell"}], funding, prices) == pytest.approx(-0.2)


class ScriptedReplay:
    """Stands in for Nautilus: fills every entry, then the first generation's target once."""

    def __init__(self) -> None:
        self.bundles: list[dict] = []

    def __call__(self, bundle: dict) -> dict:
        self.bundles.append(bundle)
        fills, positions = [], []
        for row in bundle["commands"]:
            command = row["command"]
            if command["event"] == "entry":
                fills.append({"trade_id": row["command_id"], "event": "entry", "side": command["side"], "price": command["price"], "quantity": command["quantity"], "ts": command["ts"], "cost": 0.1})
                if command["grid_generation"] == 1:
                    fills.append({"trade_id": row["command_id"], "event": "target", "side": "sell", "price": command["tp"], "quantity": command["quantity"], "ts": command["ts"], "cost": 0.1})
                else:
                    positions.append({"trade_id": row["command_id"], "position_id": f"POS-{row['command_id']}", "status": "open", "side": "long", "remaining_units": command["quantity"]})
            else:
                fills.append({"trade_id": command["trade_id"], "event": "stop", "side": command["side"], "price": command["price"], "quantity": command["quantity"], "ts": command["ts"], "cost": 0.1})
                positions = [p for p in positions if p["position_id"] != command["position_id"]]
        return {"fills": fills, "positions": positions}


def window(closes: list[float], **kwargs) -> tuple[dict, ScriptedReplay]:
    replay = ScriptedReplay()
    result = gb.run_window(SHAPE, bars(closes), replay=replay, config=CONFIG, instrument=INSTRUMENT, source=SOURCE, price_step="0.1", quantity_step="0.001", **kwargs)
    return result, replay


def test_a_line_whose_target_filled_is_placed_again_and_the_end_is_flat() -> None:
    result, replay = window([100.0] * 30)
    assert result["ended"] == "time" and result["open_after_close"] == 0
    commands = replay.bundles[-1]["commands"]
    assert sorted({c["command"].get("grid_generation") for c in commands if c["command"]["event"] == "entry"}) == [1, 2]
    assert sum(c["command"]["event"] == "stop" for c in commands) == 2


def test_a_run_stops_at_the_boundary_and_sees_nothing_after_it() -> None:
    result, replay = window([100.0, 95.0, 89.0, 200.0, 200.0])
    assert result["ended"] == "lower"
    assert max(e["price"] for e in replay.bundles[-1]["market_events"]) == 100.0
    assert result["ended_at"] == (START + timedelta(minutes=3)).isoformat()


def test_funding_older_than_the_record_is_left_out_and_said_so() -> None:
    settled = [{"funding_time": (START + timedelta(minutes=5)).isoformat(), "funding_rate": 0.1, "realized_rate": None}]
    covered, _ = window([100.0] * 30, funding=settled, funding_from=START.isoformat())
    assert covered["funding"] is not None
    older, _ = window([100.0] * 30, funding=settled, funding_from=(START + timedelta(days=1)).isoformat())
    assert older["funding"] is None and older["net"] == older["gross"] - older["fees"]


def test_summary_is_numbers_only() -> None:
    rows = [{"return_pct": value, "ended": ended, "fill_count": 10, "funding": None} for value, ended in ((1.0, "time"), (-2.0, "lower"), (3.0, "time"))]
    summary = gb.summarize(rows, days=180)
    assert (summary["runs"], summary["independent_windows"], summary["median_return_pct"], summary["worst_return_pct"]) == (3, 25, 1.0, -2.0)
    assert summary["stopped_pct"] == 33.3 and sum(summary["histogram"]["counts"]) == 3
    assert not {"grade", "score", "verdict", "healthy"} & set(summary)


class FakeHistory:
    def __init__(self, **report) -> None:
        end = datetime(2026, 11, 5, tzinfo=timezone.utc)
        self.start = end - timedelta(days=8)
        self.report = {"requested_start": self.start.isoformat(), "requested_end": end.isoformat(), "expected_bars": 8 * 1440, "gaps": [], "fetch_failures": {}, "missing_minutes": 0, **report}
        self.end = end

    def history_report(self, **_kwargs) -> dict:
        if self.report.get("down"):
            raise DatafeedUnavailable("down")
        return self.report

    def history_candles(self, *, start: str, end: str, **_kwargs) -> dict:
        first, last = datetime.fromisoformat(start), datetime.fromisoformat(end)
        rows, moment = [], first
        while moment <= last:
            rows.append({"timestamp": moment.isoformat(), "open": 100.0, "close": 100.0})
            moment += timedelta(minutes=1)
        return {"candles": rows}

    def history_funding(self, **_kwargs) -> dict:
        return {"funding": [{"funding_time": "2026-11-01T00:00:00+00:00", "funding_rate": 0.0, "realized_rate": 0.0}]}


def test_history_loads_whole_and_names_where_funding_starts() -> None:
    client = FakeHistory()
    loaded = gb.load_history(client, source=SOURCE, instrument=INSTRUMENT, now=client.end)
    assert len(loaded["bars"]) == 8 * 1440 and loaded["funding_from"] == "2026-11-01T00:00:00+00:00"


@pytest.mark.parametrize(("report", "words"), [
    ({"gaps": [{"minutes": 3}], "missing_minutes": 3}, "缺口"),
    ({"fetch_failures": {"2026-11-01": "x"}}, "缺口"),
    ({"expected_bars": 9 * 1440}, "不完整"),
    ({"down": True}, "读不到"),
])
def test_history_with_a_hole_is_refused_never_stepped_over(report: dict, words: str) -> None:
    client = FakeHistory(**report)
    with pytest.raises(gb.BacktestUnavailable, match=words):
        gb.load_history(client, source=SOURCE, instrument=INSTRUMENT, now=client.end)


def test_old_history_is_refused() -> None:
    client = FakeHistory()
    with pytest.raises(gb.BacktestUnavailable, match="太旧"):
        gb.load_history(client, source=SOURCE, instrument=INSTRUMENT, now=client.end + timedelta(days=4))


NAUTILUS = os.environ.get("TRADING_ORCHESTRATOR_NAUTILUS_TEST_PYTHON", "")


@pytest.mark.skipif(not NAUTILUS or not Path(NAUTILUS).exists(), reason="Nautilus runtime missing; set TRADING_ORCHESTRATOR_NAUTILUS_TEST_PYTHON")
def test_real_replay_rearms_a_line_and_ends_flat(tmp_path: Path) -> None:
    from tests.test_dualtrack_nautilus_runtime_integration import _definition

    preflight = tmp_path / "preflight.json"
    preflight.write_text(json.dumps([{
        "status": "ready_for_park_paper",
        "instrument": {**_definition(), "symbol": INSTRUMENT},
        "fee_model": {"mode": "paper_contract", "maker_fee_rate": "0", "taker_fee_rate": "0.000400", "funding_rate": "0", "real_money_eligible": False},
    }]))
    swing = ([100.0] + [94.0] * 3 + [106.0] * 3) * 12
    result = gb.run_window(
        SHAPE, bars(swing), replay=gb.subprocess_replay(NAUTILUS, preflight), config=CONFIG, instrument=INSTRUMENT, source=SOURCE,
        price_step="0.1", quantity_step="0.001",
    )
    assert result["open_after_close"] == 0
    assert result["fill_count"] > 4  # more than one pass over the two rungs
