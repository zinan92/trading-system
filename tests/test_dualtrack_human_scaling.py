from __future__ import annotations

from pathlib import Path

import pytest

from services.dualtrack_human import DualTrackHumanEngine
from tests.test_dualtrack_dt2_machine_runner import TEST_CONFIG


def test_same_trade_scale_in_aggregates_quantity_and_weighted_entry_price(tmp_path: Path) -> None:
    engine = DualTrackHumanEngine(tmp_path / "outputs", config=TEST_CONFIG)
    for index, price in enumerate((100.0, 98.0)):
        engine.submit_order({
            "cycle_id": "2026-07-05_DAY",
            "ts": f"2026-07-05T01:0{index}:00+00:00",
            "side": "buy",
            "event": "entry",
            "order_type": "market",
            "price": price,
            "notional": price,
            "contracts": 1.0,
            "trade_id": "scaled-long",
            "position_id": "scaled",
            "source": "test",
        })

    trade = engine.human_payload("2026-07-05_DAY")["trades"][0]

    assert trade["units"] == 2.0
    assert trade["remaining_units"] == 2.0
    assert trade["entry_price"] == pytest.approx(99.0)
    assert len(trade["entry_fills"]) == 2
