from __future__ import annotations

from pathlib import Path

import pytest

from services.dualtrack_execution_contract import canonical_market_event
from services.legacy_paper_execution_adapter import LegacyPaperExecutionAdapter
from tests.test_dualtrack_dt2_machine_runner import TEST_CONFIG


ROOT = Path(__file__).resolve().parents[1]


def _three_level_bar() -> dict:
    return {
        "schema_version": "dualtrack-market-event-v1",
        "event_id": "bar-4008-3994",
        "cycle_id": "2026-07-24_DAY",
        "ts_event": "2026-07-24T01:01:00+00:00",
        "event_started_at": "2026-07-24T01:00:00+00:00",
        "price": 4003.0,
        "open": 4008.0,
        "high": 4009.0,
        "low": 3994.0,
        "fresh": True,
        "is_synthetic": False,
        "source": "market_db:binance_usdm_futures",
        "provider": "binance_usdm_futures",
        "instrument_id": "XAUUSDT",
    }


def test_multi_level_bar_remains_one_canonical_market_event() -> None:
    event = canonical_market_event(_three_level_bar())

    assert event["event_id"] == "bar-4008-3994"
    assert event["ts_event"] == "2026-07-24T01:01:00+00:00"
    assert (event["open"], event["high"], event["low"], event["price"]) == (
        4008.0,
        4009.0,
        3994.0,
        4003.0,
    )
    assert not {"intrabar_events", "tick_sequence", "assumed_fill_levels"} & set(event)


def test_multi_level_bar_rejects_partial_ohlc_that_could_invent_a_path() -> None:
    event = _three_level_bar()
    event.pop("low")

    with pytest.raises(ValueError, match="OHLC must include open, high, and low together"):
        canonical_market_event(event)


def test_paper_replay_records_each_preexisting_touched_level_once(tmp_path: Path) -> None:
    adapter = LegacyPaperExecutionAdapter(tmp_path / "outputs", config=TEST_CONFIG)
    cycle_id = "2026-07-24_DAY"
    for index, price in enumerate((4004.0, 4000.0, 3996.0), start=1):
        adapter.submit_order({
            "cycle_id": cycle_id,
            "ts": "2026-07-24T00:59:00+00:00",
            "side": "buy",
            "event": "entry",
            "order_type": "limit",
            "price": price,
            "notional": 1_000.0,
            "sl": 3900.0,
            "tp": price + 4.0,
            "source": "grid-traversal-contract",
            "source_fill_id": f"grid-level-{index}",
        })

    first = adapter.process_market_event(_three_level_bar())
    repeat = adapter.process_market_event(_three_level_bar())
    snapshot = adapter.snapshot(cycle_id)

    assert first["accepted_limit_fill_count"] == 3
    assert [row["price"] for row in first["accepted_limit_fills"]] == [4004.0, 4000.0, 3996.0]
    assert repeat["accepted_limit_fill_count"] == 0
    assert [row["price"] for row in snapshot["fills"] if row["event"] == "entry"] == [
        4004.0,
        4000.0,
        3996.0,
    ]


def test_multi_level_contract_explains_receipt_and_precision_boundaries() -> None:
    contract = (ROOT / "docs/contracts/grid-multi-level-traversal-v1.md").read_text(
        encoding="utf-8"
    )

    for requirement in (
        "one immutable",
        "only completed bars",
        "does not manufacture",
        "Paper model fill",
        "execution fill receipt",
        "modelled_at_bar_granularity",
        "unsupported_precision",
        "completed_rearmed",
    ):
        assert requirement in contract
