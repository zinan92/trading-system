"""The Paper instrument table (ADR 0009, #1405): specs come from the datafeed; anything doubtful blocks."""

from __future__ import annotations

import pytest

from services.paper_gold_venue import PROFILES, PaperGoldVenueError, apply_to_dualtrack, describe
from services.paper_instruments import contract_from_definition, instrument_keys, resolve_paper_instrument
from tests.test_paper_gold_venue import _okx_definition

CONFIG = apply_to_dualtrack({"paper_gold_venue": {"active": "okx"}})
ENDPOINT = "http://127.0.0.1:8100/api/instruments/commodity/XAUUSDT"


def _definition(wanted: str, **overrides) -> dict:
    return {**_okx_definition(), "instrument_id": f"{wanted}.OKX", "symbol": wanted, "price_precision": 2, "price_increment": "0.01",
            "size_precision": 2, "size_increment": "0.01", "status": "TRADING", "contract_multiplier": "1", **overrides}


def _fetch(definition: dict, calls: list | None = None):
    def fetch(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        return definition
    return fetch


def test_the_table_is_gold_silver_crude() -> None:
    assert instrument_keys() == ("gold", "silver", "crude")


def test_gold_is_the_active_venue_and_reads_nothing() -> None:
    def fetch(**_):
        raise AssertionError("gold's contract is its profile")
    assert resolve_paper_instrument("gold", CONFIG, fetch=fetch) is PROFILES["okx"]


@pytest.mark.parametrize(("key", "symbol"), [("silver", "XAG-USDT-SWAP"), ("crude", "CL-USDT-SWAP")])
def test_silver_and_crude_read_their_contract_from_the_datafeed(key: str, symbol: str) -> None:
    calls: list = []
    profile = resolve_paper_instrument(key, CONFIG, fetch=_fetch(_definition(symbol), calls))
    assert calls == [{"endpoint": f"http://127.0.0.1:8100/api/instruments/commodity/{symbol}", "source": "okx_swap_public"}]
    assert profile.contract == {"price_precision": 2, "price_increment": "0.01", "quantity_precision": 2, "quantity_increment": "0.01"}
    assert (profile.venue_profile_id, profile.execution_market_instrument_id) == ("okx.paper", f"{symbol}.OKX")
    assert describe(profile)["fee_verified"] is False


def test_the_contract_follows_the_definition_not_a_constant() -> None:
    assert contract_from_definition(_definition("CL-USDT-SWAP", size_precision=1, size_increment="0.1"))["quantity_increment"] == "0.1"


@pytest.mark.parametrize(("change", "code"), [
    ({"status": "SUSPENDED"}, "paper_instrument_not_trading"),
    ({"contract_multiplier": "10"}, "paper_instrument_multiplier"),
    ({"symbol": "XAU-USDT-SWAP"}, "paper_gold_venue_mismatch"),
    ({"venue": "BINANCE"}, "paper_gold_venue_mismatch"),
    ({"source_mode": "binance_usdm_futures", "provider": "binance_usdm_futures"}, "paper_gold_venue_mismatch"),
])
def test_a_doubtful_definition_blocks_the_instrument(change: dict, code: str) -> None:
    with pytest.raises(PaperGoldVenueError) as caught:
        resolve_paper_instrument("silver", CONFIG, fetch=_fetch(_definition("XAG-USDT-SWAP", **change)))
    assert caught.value.code == code


def test_an_unreadable_definition_blocks_the_instrument() -> None:
    def fetch(**_):
        raise RuntimeError("datafeed down")
    with pytest.raises(PaperGoldVenueError) as caught:
        resolve_paper_instrument("crude", CONFIG, fetch=fetch)
    assert caught.value.code == "paper_instrument_unavailable"


def test_an_unknown_instrument_is_refused() -> None:
    with pytest.raises(PaperGoldVenueError) as caught:
        resolve_paper_instrument("copper", CONFIG)
    assert caught.value.code == "paper_instrument_unknown"
