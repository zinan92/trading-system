from __future__ import annotations

import json
import os

from services.dualtrack_binance_account_costs import BinanceAccountCostObserver


class _Response:
    def __init__(self, payload) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def test_observes_account_fee_and_public_funding_without_exposing_credentials(monkeypatch) -> None:
    monkeypatch.setenv("BINANCE_TEST_KEY", "key-value")
    monkeypatch.setenv("BINANCE_TEST_SECRET", "secret-value")
    requests = []

    def opener(request, timeout):
        requests.append((request, timeout))
        if "/commissionRate" in request.full_url:
            return _Response({
                "symbol": "XAUUSDT",
                "makerCommissionRate": "0.000020",
                "takerCommissionRate": "0.000050",
            })
        return _Response([{"symbol": "XAUUSDT", "fundingRate": "0.000100", "fundingTime": 1234}])

    observer = BinanceAccountCostObserver(
        config={
            "broker": {
                "environment": "demo",
                "base_url": "https://demo.example",
                "api_key_env": "BINANCE_TEST_KEY",
                "api_secret_env": "BINANCE_TEST_SECRET",
            }
        },
        opener=opener,
        clock_ms=lambda: 1000,
    )
    result = observer.observe()

    assert result["maker_fee_rate"] == "0.000020"
    assert result["taker_fee_rate"] == "0.000050"
    assert result["funding_rate"] == "0.000100"
    assert result["environment"] == "demo"
    assert result["read_only"] is True
    assert result["real_money_eligible"] is False
    rendered = json.dumps(result)
    assert os.environ["BINANCE_TEST_KEY"] not in rendered
    assert os.environ["BINANCE_TEST_SECRET"] not in rendered
    assert requests[0][0].get_header("X-mbx-apikey") == "key-value"


def test_observer_fails_closed_when_funding_evidence_is_missing(monkeypatch) -> None:
    monkeypatch.setenv("BINANCE_TEST_KEY", "key-value")
    monkeypatch.setenv("BINANCE_TEST_SECRET", "secret-value")

    def opener(request, timeout):
        if "/commissionRate" in request.full_url:
            return _Response({
                "makerCommissionRate": "0.000020",
                "takerCommissionRate": "0.000050",
            })
        return _Response([])

    observer = BinanceAccountCostObserver(
        config={"broker": {
            "base_url": "https://demo.example",
            "api_key_env": "BINANCE_TEST_KEY",
            "api_secret_env": "BINANCE_TEST_SECRET",
        }},
        opener=opener,
    )

    try:
        observer.observe()
    except RuntimeError as exc:
        assert "funding" in str(exc)
    else:
        raise AssertionError("missing funding evidence must fail closed")
