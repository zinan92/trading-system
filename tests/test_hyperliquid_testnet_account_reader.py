from __future__ import annotations

import json

import pytest

from services.hyperliquid_testnet_account_reader import (
    HyperliquidTestnetAccountError,
    HyperliquidTestnetAccountReader,
)
from services.account_identity import ACCOUNT_FINGERPRINT_SCHEME, account_fingerprint


class Response:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


ACCOUNT = "0x" + "a" * 40


def test_reads_public_account_state_and_projects_non_secret_facts() -> None:
    payloads = {
        "clearinghouseState": {
            "marginSummary": {"accountValue": "995.46", "totalMarginUsed": "0"},
            "assetPositions": [
                {"position": {"coin": "BTC", "szi": "0.010", "entryPx": "79000"}},
                {"position": {"coin": "SOL", "szi": "0"}},
            ],
        },
        "openOrders": [{"coin": "BTC", "oid": 12, "sz": "0.01", "side": "B"}],
        "frontendOpenOrders": [{"coin": "BTC", "oid": 12, "sz": "0.01", "side": "B"}],
        "userFills": [{"coin": "BTC", "oid": 11, "px": "79000", "sz": "0.01", "fee": "0.12"}],
    }
    calls: list[dict] = []

    def opener(request, timeout):
        body = json.loads(request.data.decode())
        calls.append({"body": body, "timeout": timeout})
        return Response(payloads[body["type"]])

    reader = HyperliquidTestnetAccountReader(
        ACCOUNT,
        opener=opener,
        clock=lambda: 1788000000.0,
    )

    result = reader.read()

    assert result["broker_id"] == "hyperliquid"
    assert result["environment"] == "testnet"
    assert result["account_fingerprint"].startswith("sha256:")
    assert result["account_fingerprint"] == account_fingerprint(ACCOUNT.upper())
    assert result["fingerprint_scheme"] == ACCOUNT_FINGERPRINT_SCHEME
    assert result["equity"] == 995.46
    assert result["positions"][0]["instrument_id"] == "BTC-USD-PERP"
    assert result["positions"][0]["signed_quantity"] == "0.010"
    assert result["open_orders"][0]["instrument_id"] == "BTC-USD-PERP"
    assert result["fills"][0]["instrument_id"] == "BTC-USD-PERP"
    assert result["fees"][0]["fee"] == "0.12"
    assert result["fresh"] is True
    assert result["coherent"] is True
    assert result["source_cursor"].startswith("sha256:")
    assert all(call["body"]["user"] == ACCOUNT for call in calls)
    assert all("private" not in json.dumps(call).lower() for call in calls)


def test_rejects_invalid_public_account_address_without_network_call() -> None:
    with pytest.raises(HyperliquidTestnetAccountError, match="testnet_account_address_invalid"):
        HyperliquidTestnetAccountReader("not-an-address")


def test_account_transport_failure_is_typed_and_redacted() -> None:
    def opener(*_args, **_kwargs):
        raise TimeoutError("secret transport detail")

    reader = HyperliquidTestnetAccountReader(ACCOUNT, opener=opener)

    with pytest.raises(HyperliquidTestnetAccountError, match="testnet_account_unavailable") as raised:
        reader.read()

    assert "secret transport detail" not in str(raised.value)


def test_account_freshness_is_derived_from_fact_timestamp() -> None:
    payloads = {
        "clearinghouseState": {
            "time": 1787999000,
            "marginSummary": {"accountValue": "995.46", "totalMarginUsed": "0"},
            "assetPositions": [],
        },
        "openOrders": [],
        "frontendOpenOrders": [],
        "userFills": [],
    }

    def opener(request, timeout):
        body = json.loads(request.data.decode())
        return Response(payloads[body["type"]])

    reader = HyperliquidTestnetAccountReader(
        ACCOUNT,
        opener=opener,
        clock=lambda: 1788000000.0,
    )

    result = reader.read()

    assert result["fresh"] is False
    assert result["fact_age_seconds"] == 1000.0
    assert result["fact_max_age_seconds"] == 120.0


def test_account_coherence_is_derived_from_open_order_views() -> None:
    payloads = {
        "clearinghouseState": {
            "marginSummary": {"accountValue": "995.46", "totalMarginUsed": "0"},
            "assetPositions": [],
        },
        "openOrders": [{"coin": "BTC", "oid": 12, "sz": "0.01", "side": "B"}],
        "frontendOpenOrders": [],
        "userFills": [],
    }

    def opener(request, timeout):
        body = json.loads(request.data.decode())
        return Response(payloads[body["type"]])

    result = HyperliquidTestnetAccountReader(
        ACCOUNT,
        opener=opener,
        clock=lambda: 1788000000.0,
    ).read()

    assert result["coherent"] is False
    assert result["coherence_issues"] == ["open_order_views_mismatch"]
