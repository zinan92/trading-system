from __future__ import annotations

import pytest

from services.dualtrack_nautilus_instrument import (
    build_nautilus_instrument,
    validate_instrument_definition,
)


def _definition() -> dict:
    return {
        "schema_version": "instrument-definition-v1",
        "instrument_id": "XAUUSDT.BINANCE",
        "venue": "BINANCE",
        "symbol": "XAUUSDT",
        "asset_class": "commodity",
        "market_type": "usd_m_futures",
        "contract_type": "TRADIFI_PERPETUAL",
        "status": "TRADING",
        "base_currency": "XAU",
        "quote_currency": "USDT",
        "settlement_currency": "USDT",
        "margin_currency": "USDT",
        "is_inverse": False,
        "price_precision": 2,
        "price_increment": "0.01",
        "min_price": "0.01",
        "max_price": "200000",
        "size_precision": 3,
        "size_increment": "0.001",
        "min_quantity": "0.001",
        "max_quantity": "10000",
        "min_notional": "5",
        "margin_init_rate": "0.0500",
        "margin_maint_rate": "0.0250",
        "contract_multiplier": "1",
        "provider": "binance_usdm_futures",
        "source_mode": "binance_usdm_futures",
        "served_from": "upstream",
        "execution_venue": True,
        "is_synthetic": False,
        "upstream_server_time": 1783667819466,
        "derived_fields": {
            "contract_multiplier": "usd_m_notional_equals_price_times_quantity",
        },
    }


def test_validates_upstream_execution_definition() -> None:
    assert validate_instrument_definition(_definition())["symbol"] == "XAUUSDT"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("is_synthetic", True, "synthetic"),
        ("execution_venue", False, "execution venue"),
        ("status", "BREAK", "TRADING"),
        ("served_from", "cache", "upstream"),
    ],
)
def test_rejects_untrusted_or_untradeable_definition(field: str, value, message: str) -> None:
    definition = _definition()
    definition[field] = value
    with pytest.raises(ValueError, match=message):
        validate_instrument_definition(definition)


def test_rejects_unexplained_contract_multiplier() -> None:
    definition = _definition()
    definition["derived_fields"] = {}
    with pytest.raises(ValueError, match="multiplier provenance"):
        validate_instrument_definition(definition)


def test_builder_has_no_implicit_fee_default() -> None:
    with pytest.raises(ValueError, match="maker fee rate is required explicitly"):
        build_nautilus_instrument(_definition())


def test_paper_keeps_commodity_only_and_only_the_backtest_entry_widens_it() -> None:
    """#1337: a crypto perpetual is accepted only where the caller asks for the backtest set."""
    from services.dualtrack_nautilus_instrument import BACKTEST_ASSET_CLASSES

    crypto = {**_definition(), "asset_class": "crypto", "symbol": "BTC-USDT-SWAP"}
    with pytest.raises(ValueError, match="asset class must be one of commodity"):
        validate_instrument_definition(crypto)
    assert validate_instrument_definition(crypto, asset_classes=BACKTEST_ASSET_CLASSES)["symbol"] == "BTC-USDT-SWAP"
