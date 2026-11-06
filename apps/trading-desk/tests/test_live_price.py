"""#1372: the desk's BTC price is the venue's live mid, never an old grid record's last price."""
from __future__ import annotations

import json

from trading_desk.app import current_state
from tests.test_desk import client_for

OLD = 76253.0  # what a terminal grid record last saw, weeks ago


def _terminal_grid(paper_output) -> None:
    folder = paper_output / "dualtrack" / "grid_testnet_lifecycle"
    for path in folder.glob("*.json"):
        path.unlink()
    (folder / "dashboard-plan:old.json").write_text(json.dumps([{
        "status": "terminal", "sealed": True, "direction": "long", "lower_boundary": 74000.0, "upper_boundary": 77000.0,
        "last_market_price": OLD, "updated_at": "2026-09-15T14:40:59+00:00", "instrument_id": "BTC-USD-PERP", "rungs": [], "orders": [], "fills": [],
    }]))


def test_the_price_is_the_venue_mid_not_the_grid_record(sources, btc, fetch, paper_output) -> None:
    _terminal_grid(paper_output)
    fetch.routes["allMids"] = {"BTC": "76149.5"}
    state = current_state(sources, btc)
    assert state["price"] == 76149.5 and state["price_source"] == "venue"
    assert state["grid"]["price"] == OLD  # the grid card still says what the grid last saw


def test_without_the_mid_the_last_hourly_close_is_used_and_named(sources, btc, fetch, paper_output) -> None:
    _terminal_grid(paper_output)
    fetch.routes["allMids"] = ConnectionError("down")
    state = current_state(sources, btc)
    assert state["price_source"] == "bars_1h" and state["price"] != OLD
    assert state["price"] == float(fetch.routes["candleSnapshot"][-1]["c"])


def test_with_the_feed_down_there_is_no_price_and_no_approval(config, sources, store, btc, fetch, paper_output) -> None:
    _terminal_grid(paper_output)
    fetch.routes["allMids"] = ConnectionError("down")
    fetch.routes["candleSnapshot"] = ConnectionError("down")
    assert current_state(sources, btc) == {**current_state(sources, btc), "price": None, "price_source": "none"}
    client, _ = client_for(config, sources, store, fetch)
    refused = client.post("/api/judgments", json={"asset": "BTC", "direction": "long", "confidence": 3, "action": "approved"})
    assert refused.status_code == 409 and "现价读不到" in refused.json()["detail"]


def test_a_mid_that_is_not_a_number_is_no_price(sources, btc, fetch, paper_output) -> None:
    _terminal_grid(paper_output)
    fetch.routes["allMids"] = {"BTC": "n/a"}
    fetch.routes["candleSnapshot"] = ConnectionError("down")
    assert current_state(sources, btc)["price"] is None
