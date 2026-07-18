from __future__ import annotations

import hashlib
import hmac
import json
import urllib.error
from pathlib import Path

import pytest

from services.broker_adapter import LiveBrokerAdapter
from services.venues.binance_usdm_transport import BinanceUsdmTransport


ROOT = Path(__file__).resolve().parents[1]


class _FakeResponse:
    def __init__(self, payload, *, status: int = 200) -> None:
        self.payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        if isinstance(self.payload, bytes):
            return self.payload
        return json.dumps(self.payload).encode("utf-8")

    def close(self) -> None:
        return None


def _credentials(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(tmp_path / "missing-live.env"))
    monkeypatch.setenv("BINANCE_API_KEY", "wire-key")
    monkeypatch.setenv("BINANCE_API_SECRET", "wire-secret")


def test_signed_write_preserves_exact_a12_wire_shape(tmp_path: Path, monkeypatch) -> None:
    _credentials(monkeypatch, tmp_path)
    seen = {}

    def opener(request, timeout):
        seen.update(
            url=request.full_url,
            method=request.get_method(),
            body=request.data,
            api_key=request.get_header("X-mbx-apikey"),
            content_type=request.get_header("Content-type"),
            user_agent=request.get_header("User-agent"),
            timeout=timeout,
        )
        return _FakeResponse({"orderId": 42})

    transport = BinanceUsdmTransport(
        {
            "environment": "live",
            "recv_window_ms": 7000,
            "timeout_seconds": 13,
        },
        opener=opener,
        clock_ms=lambda: 1_721_234_567_890,
    )

    result = transport.signed_request(
        "POST",
        transport.endpoints.order,
        {"symbol": "XAUUSDT", "side": "BUY", "quantity": "0.002"},
    )

    query = "symbol=XAUUSDT&side=BUY&quantity=0.002&timestamp=1721234567890&recvWindow=7000"
    signature = hmac.new(b"wire-secret", query.encode("utf-8"), hashlib.sha256).hexdigest()
    assert result == {"orderId": 42}
    assert seen == {
        "url": "https://fapi.binance.com/fapi/v1/order",
        "method": "POST",
        "body": f"{query}&signature={signature}".encode("utf-8"),
        "api_key": "wire-key",
        "content_type": "application/x-www-form-urlencoded",
        "user_agent": "TradingOrchestrator/1.0",
        "timeout": 13,
    }


def test_signed_get_preserves_query_order_and_url_placement(tmp_path: Path, monkeypatch) -> None:
    _credentials(monkeypatch, tmp_path)
    seen = {}

    def opener(request, timeout):
        seen.update(
            url=request.full_url,
            method=request.get_method(),
            body=request.data,
            api_key=request.get_header("X-mbx-apikey"),
            timeout=timeout,
        )
        return _FakeResponse([{"positionAmt": "0"}])

    transport = BinanceUsdmTransport(
        {"base_url": "https://demo-fapi.binance.com/"},
        opener=opener,
        clock_ms=lambda: 1_721_234_567_890,
    )

    result = transport.signed_get("/fapi/v2/positionRisk", {"symbol": "XAUUSDT"})

    query = "symbol=XAUUSDT&timestamp=1721234567890&recvWindow=5000"
    signature = hmac.new(b"wire-secret", query.encode("utf-8"), hashlib.sha256).hexdigest()
    assert result == [{"positionAmt": "0"}]
    assert seen == {
        "url": f"https://demo-fapi.binance.com/fapi/v2/positionRisk?{query}&signature={signature}",
        "method": "GET",
        "body": None,
        "api_key": "wire-key",
        "timeout": 10,
    }


def test_missing_credentials_fail_before_network_io(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(tmp_path / "missing-live.env"))
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    calls = []
    transport = BinanceUsdmTransport({}, opener=lambda *args, **kwargs: calls.append((args, kwargs)))

    with pytest.raises(RuntimeError, match="missing Binance environment variables"):
        transport.signed_request("DELETE", transport.endpoints.all_open_orders, {"symbol": "XAUUSDT"})

    assert calls == []
    assert transport.signed_get_envelope("/fapi/v2/positionRisk", {"symbol": "XAUUSDT"}) == {
        "ok": False,
        "status": None,
        "error": {"message": "missing Binance environment variables: BINANCE_API_KEY, BINANCE_API_SECRET"},
    }


def test_transport_repr_never_exposes_config_or_injected_clients() -> None:
    transport = BinanceUsdmTransport(
        {"api_key": "must-not-leak", "api_secret": "must-not-leak"},
        opener=lambda request, timeout: _FakeResponse({}),
        clock_ms=lambda: 1,
    )

    rendered = repr(transport)
    assert "must-not-leak" not in rendered
    assert "opener" not in rendered
    assert "clock_ms" not in rendered


def test_demo_envelope_preserves_http_error_body(tmp_path: Path, monkeypatch) -> None:
    _credentials(monkeypatch, tmp_path)

    def opener(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            400,
            "Bad Request",
            {},
            _FakeResponse({"code": -2013, "msg": "Order does not exist"}),
        )

    transport = BinanceUsdmTransport({}, opener=opener, clock_ms=lambda: 1)

    assert transport.signed_get_envelope(transport.endpoints.order, {"symbol": "XAUUSDT"}) == {
        "ok": False,
        "status": 400,
        "error": {"code": -2013, "msg": "Order does not exist"},
    }


def test_exchange_info_normalization_and_failure_are_unchanged() -> None:
    payload = {
        "symbols": [
            {
                "symbol": "XAUUSDT",
                "status": "TRADING",
                "contractType": "TRADIFI_PERPETUAL",
                "underlyingType": "COMMODITY",
                "marginAsset": "USDT",
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                    {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01"},
                    {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001", "minQty": "0.002"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5"},
                ],
            }
        ]
    }
    seen = {}

    def public_opener(request, timeout):
        seen.update(
            url=request.full_url,
            method=request.get_method(),
            body=request.data,
            api_key=request.get_header("X-mbx-apikey"),
            authorization=request.get_header("Authorization"),
            timeout=timeout,
        )
        return _FakeResponse(payload)

    transport = BinanceUsdmTransport({}, opener=public_opener)

    assert transport.symbol_status("XAUUSDT") == {
        "symbol": "XAUUSDT",
        "status": "TRADING",
        "contract_type": "TRADIFI_PERPETUAL",
        "underlying_type": "COMMODITY",
        "margin_asset": "USDT",
        "filters": {
            "tick_size": "0.01",
            "step_size": "0.001",
            "min_qty": "0.002",
            "min_notional": "5",
        },
        "truth_level": "official_binance_exchange_info",
    }
    assert seen == {
        "url": "https://fapi.binance.com/fapi/v1/exchangeInfo?symbol=XAUUSDT",
        "method": "GET",
        "body": None,
        "api_key": None,
        "authorization": None,
        "timeout": 10,
    }
    unavailable = BinanceUsdmTransport({}, opener=lambda request, timeout: (_ for _ in ()).throw(TimeoutError("late")))
    assert unavailable.symbol_status("XAUUSDT") == {
        "symbol": "XAUUSDT",
        "status": "UNKNOWN",
        "filters": {},
        "truth_level": "exchange_info_unavailable",
    }


def test_compatibility_adapter_reuses_one_transport_and_observes_config_mutation(tmp_path: Path) -> None:
    adapter = LiveBrokerAdapter(
        tmp_path / "outputs",
        True,
        {
            "provider": "binance_usdm",
            "environment": "demo",
            "base_url": "https://demo-fapi.binance.com",
            "instrument_map": {"GOLD": "XAUUSDT"},
        },
    )

    first = adapter._binance_transport()
    adapter.broker_config["base_url"] = "https://fapi.binance.com/"

    assert adapter._binance_transport() is first
    assert adapter._binance_base_url() == "https://fapi.binance.com"
    assert adapter._binance_symbol("GOLD") == "XAUUSDT"

    def replacement_opener(request, timeout):
        return _FakeResponse({})

    adapter.opener = replacement_opener
    second = adapter._binance_transport()
    assert second is not first
    assert second.opener is replacement_opener

    adapter.broker_config = {**adapter.broker_config, "base_url": "https://demo-fapi.binance.com"}
    third = adapter._binance_transport()
    assert third is not second
    assert third.broker_config is adapter.broker_config


def test_binance_wire_code_is_absent_from_cross_venue_compatibility_adapter() -> None:
    source = (ROOT / "services" / "broker_adapter.py").read_text(encoding="utf-8")

    assert "import hmac" not in source
    assert "time.time()" not in source
    assert "signature=" not in source
    assert "https://fapi.binance.com" not in source
    assert "https://testnet.binancefuture.com" not in source
    for endpoint in (
        "/fapi/v1/order",
        "/fapi/v1/allOpenOrders",
        "/fapi/v1/openOrders",
        "/fapi/v1/algoOrder",
        "/fapi/v1/algoOpenOrders",
        "/fapi/v1/openAlgoOrders",
        "/fapi/v1/exchangeInfo",
        "/fapi/v2/positionRisk",
    ):
        assert endpoint not in source

    demo_source = (ROOT / "services" / "binance_demo_broker_adapter.py").read_text(encoding="utf-8")
    assert "import hmac" not in demo_source
    assert "signature=" not in demo_source
