from __future__ import annotations

import json

import pytest

from services.hyperliquid_testnet_market_reader import (
    HyperliquidTestnetMarketError,
    HyperliquidTestnetMarketReader,
)


class Response:
    def __init__(self, payload: object) -> None:
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
        "metaAndAssetCtxs": [
            {
                "universe": [
                    {"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
                ]
            },
            [
                {"oraclePx": "79665.0", "markPx": "79665.5"},
            ],
        ],
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
    assert result["asset_index"] == 0
    assert result["execution_ready"] is True
    assert result["max_oracle_deviation_bps"] == 50.0
    assert result["max_oracle_deviation_bps_source"] == "default"
    assert result["oracle"] == 79665.0
    assert result["mark"] == 79665.5
    assert result["depth_notional"] > 100
    assert len(result["bids"]) == 1
    assert len(result["asks"]) == 1
    assert [call["body"]["type"] for call in calls] == [
        "allMids",
        "l2Book",
        "metaAndAssetCtxs",
    ]
    assert all(call["timeout"] == 5.0 for call in calls)
    assert all("Authorization" not in call["body"] for call in calls)


def test_testnet_env_overrides_oracle_deviation_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HYPERLIQUID_TESTNET_MAX_ORACLE_DEVIATION_BPS", "100")

    reader = HyperliquidTestnetMarketReader(opener=lambda *_args, **_kwargs: Response({}))

    assert reader.max_oracle_deviation_bps == 100.0
    assert reader.max_oracle_deviation_bps_source == "env"


def test_non_testnet_environment_ignores_oracle_deviation_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.hyperliquid_testnet_market_reader import oracle_deviation_config

    monkeypatch.setenv("HYPERLIQUID_TESTNET_MAX_ORACLE_DEVIATION_BPS", "100")

    assert oracle_deviation_config(environment="mainnet") == (50.0, "default")


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


def test_reads_btc_candles_in_standard_kline_shape_without_credentials() -> None:
    calls: list[dict] = []
    candles = [
        {
            "T": 1787825399999,
            "c": "80020.0",
            "h": "80040.0",
            "i": "30m",
            "l": "79980.0",
            "n": 15,
            "o": "80000.0",
            "s": "BTC",
            "t": 1787823600000,
            "v": "12.5",
        },
        {
            "T": 1787827199999,
            "c": "80100.0",
            "h": "80120.0",
            "i": "30m",
            "l": "80010.0",
            "n": 21,
            "o": "80020.0",
            "s": "BTC",
            "t": 1787825400000,
            "v": "18.75",
        },
    ]

    def opener(request, timeout):
        body = json.loads(request.data.decode())
        calls.append({"body": body, "timeout": timeout})
        return Response(candles)

    reader = HyperliquidTestnetMarketReader(opener=opener, clock=lambda: 1787827000.0)

    result = reader.read_bars("BTC-USD-PERP", timeframe="30m", limit=2)

    assert result["provider"] == "hyperliquid"
    assert result["source_mode"] == "hyperliquid.external_testnet"
    assert result["instrument_id"] == "BTC-USD-PERP"
    assert result["symbol"] == "BTC"
    assert result["provider_symbol"] == "BTC"
    assert result["timeframe"] == "30m"
    assert result["bar_count"] == 2
    assert result["latest_close"] == 80100.0
    assert result["trusted"] is True
    assert result["fresh"] is True
    assert result["is_synthetic"] is False
    assert result["bars"][-1]["close"] == 80100.0
    assert result["bars"][-1]["provider"] == "hyperliquid"
    assert calls[0]["body"]["type"] == "candleSnapshot"
    assert calls[0]["body"]["req"]["coin"] == "BTC"
    assert calls[0]["body"]["req"]["interval"] == "30m"
    assert calls[0]["timeout"] == 5.0


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
