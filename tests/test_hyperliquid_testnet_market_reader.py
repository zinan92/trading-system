from __future__ import annotations

import json

import pytest

from services.hyperliquid_testnet_market_reader import (
    HyperliquidTestnetMarketError,
    HyperliquidTestnetMarketReader,
)


class Response:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


def test_reads_btc_mid_and_l2_from_hyperliquid_testnet_without_credentials() -> None:
    calls: list[dict] = []
    payloads = {
        "allMids": {"BTC": "79665.5"},
        "l2Book": {
            "coin": "BTC",
            "levels": [
                [{"px": "79660.0", "sz": "0.25", "n": 1}],
                [{"px": "79670.0", "sz": "0.20", "n": 1}],
            ],
        },
    }

    def opener(request, timeout):
        body = json.loads(request.data.decode())
        calls.append({"body": body, "timeout": timeout, "url": request.full_url})
        return Response(payloads[body["type"]])

    reader = HyperliquidTestnetMarketReader(opener=opener, clock=lambda: 1787827000.0)

    result = reader.read("BTC-USD-PERP")

    assert result["price"] == 79665.5
    assert result["mid"] == 79665.5
    assert result["bid"] == 79660.0
    assert result["ask"] == 79670.0
    assert result["trusted"] is True
    assert result["fresh"] is True
    assert result["provider"] == "hyperliquid"
    assert result["source"] == "hyperliquid.external_testnet"
    assert result["environment"] == "testnet"
    assert result["instrument_id"] == "BTC-USD-PERP"
    assert result["symbol"] == "BTC"
    assert len(result["bids"]) == 1
    assert len(result["asks"]) == 1
    assert [call["body"]["type"] for call in calls] == ["allMids", "l2Book"]
    assert all(call["timeout"] == 5.0 for call in calls)
    assert all("Authorization" not in call["body"] for call in calls)


def test_rejects_non_btc_instrument_instead_of_silently_aliasing_market() -> None:
    reader = HyperliquidTestnetMarketReader(opener=lambda *_args, **_kwargs: Response({}))

    try:
        reader.read("ETH-USD-PERP")
    except ValueError as exc:
        assert str(exc) == "unsupported_testnet_instrument"
    else:
        raise AssertionError("reader must reject non-BTC until an explicit mapping is added")


def test_reads_complete_public_default_perp_catalog_without_credentials() -> None:
    calls: list[dict] = []
    payloads = {
        "meta": {
            "universe": [
                {"name": "BTC", "index": 0, "szDecimals": 5, "maxLeverage": 50},
                {"name": "ZEC", "index": 9, "szDecimals": 3, "maxLeverage": 10},
            ]
        }
    }

    def opener(request, timeout):
        body = json.loads(request.data.decode())
        calls.append({"body": body, "timeout": timeout})
        return Response(payloads[body["type"]])

    reader = HyperliquidTestnetMarketReader(opener=opener, clock=lambda: 1787827000.0)

    result = reader.read_catalog()

    assert [row["instrument_id"] for row in result["instruments"]] == [
        "BTC-USD-PERP",
        "ZEC-USD-PERP",
    ]
    assert result["instrument_scope"] == "default_perpetuals"
    assert all(row["eligibility"] == "unknown" for row in result["instruments"])
    assert calls == [{"body": {"type": "meta"}, "timeout": 5.0}]


def test_catalog_failure_is_typed_without_transport_details() -> None:
    def opener(*_args, **_kwargs):
        raise TimeoutError("private transport detail")

    reader = HyperliquidTestnetMarketReader(opener=opener)

    with pytest.raises(HyperliquidTestnetMarketError, match="testnet_market_unavailable"):
        reader.read_catalog()


def test_public_failure_is_typed_without_exposing_transport_exception() -> None:
    def opener(*_args, **_kwargs):
        raise TimeoutError("private transport detail")

    reader = HyperliquidTestnetMarketReader(opener=opener)

    with pytest.raises(HyperliquidTestnetMarketError, match="testnet_market_unavailable") as raised:
        reader.read("BTC-USD-PERP")

    assert "private transport detail" not in str(raised.value)
