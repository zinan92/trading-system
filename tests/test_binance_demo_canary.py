import json
import urllib.parse
from pathlib import Path

from services.binance_demo_canary import BinanceDemoCanary
from services.journal_store import load_json


class _FakeResponse:
    status = 200

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def _exchange_info() -> dict:
    return {
        "symbols": [
            {"symbol": "BTCUSDT", "status": "TRADING", "filters": []},
            {
                "symbol": "XAUUSDT",
                "status": "TRADING",
                "contractType": "TRADIFI_PERPETUAL",
                "marginAsset": "USDT",
                "filters": [
                    {"filterType": "LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"},
                    {"filterType": "MARKET_LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5"},
                ],
            },
        ]
    }


def _account() -> dict:
    return {
        "totalWalletBalance": "4998.0",
        "availableBalance": "4747.0",
        "assets": [{"asset": "USDT", "walletBalance": "4998.0", "availableBalance": "4747.0", "maxWithdrawAmount": "4747.0"}],
    }


def _flat_position() -> list[dict]:
    return [{"symbol": "XAUUSDT", "positionAmt": "0", "entryPrice": "0", "unRealizedProfit": "0", "leverage": "20"}]


def _canary(tmp_path: Path, monkeypatch, opener):
    env = tmp_path / "live.env"
    env.write_text("BINANCE_API_KEY=key\nBINANCE_API_SECRET=secret\n", encoding="utf-8")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(env))
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    service = BinanceDemoCanary(tmp_path / "outputs", opener=opener, clock=lambda: 1_800_000_000_000)
    service.broker_config = {
        "provider": "binance_usdm",
        "base_url": "https://demo-fapi.binance.com",
        "api_key_env": "BINANCE_API_KEY",
        "api_secret_env": "BINANCE_API_SECRET",
        "instrument_map": {"GOLD": "XAUUSDT"},
    }
    return service


def test_binance_demo_canary_validation_only_does_not_create_order(tmp_path: Path, monkeypatch):
    seen = []

    def opener(request, timeout):
        seen.append((request.get_method(), request.full_url, request.data.decode("utf-8") if request.data else ""))
        if request.full_url.endswith("/fapi/v1/exchangeInfo"):
            return _FakeResponse(_exchange_info())
        if "/fapi/v2/account" in request.full_url:
            return _FakeResponse(_account())
        if "/fapi/v2/positionRisk" in request.full_url:
            return _FakeResponse(_flat_position())
        if request.full_url.endswith("/fapi/v1/order/test"):
            return _FakeResponse({})
        raise AssertionError(request.full_url)

    result = _canary(tmp_path, monkeypatch, opener).run("2026-06-09", execute_demo=False, quantity=0.002)

    assert result["status"] == "ready_for_demo_execution"
    assert result["network_order_created"] is False
    assert [item["name"] for item in result["checks"]][-1] == "demo_execution"
    assert result["checks"][-1]["status"] == "skip"
    assert not any(url.endswith("/fapi/v1/order") for _method, url, _body in seen)
    stored = load_json(tmp_path / "outputs" / "binance_demo_canary" / "current.json")[0]
    assert stored["order_test"]["ok"] is True
    assert "key" not in json.dumps(stored)
    assert "secret" not in json.dumps(stored)


def test_binance_demo_canary_execute_demo_opens_and_reduce_only_closes(tmp_path: Path, monkeypatch):
    posted = []

    def opener(request, timeout):
        if request.full_url.endswith("/fapi/v1/exchangeInfo"):
            return _FakeResponse(_exchange_info())
        if "/fapi/v2/account" in request.full_url:
            return _FakeResponse(_account())
        if "/fapi/v2/positionRisk" in request.full_url:
            return _FakeResponse(_flat_position())
        if request.full_url.endswith("/fapi/v1/order/test"):
            return _FakeResponse({})
        if request.full_url.endswith("/fapi/v1/order"):
            body = urllib.parse.parse_qs(request.data.decode("utf-8"))
            posted.append(body)
            return _FakeResponse(
                {
                    "orderId": 100 + len(posted),
                    "symbol": "XAUUSDT",
                    "clientOrderId": body["newClientOrderId"][0],
                    "side": body["side"][0],
                    "type": body["type"][0],
                    "origQty": body["quantity"][0],
                    "executedQty": body["quantity"][0],
                    "avgPrice": "4325.10",
                    "reduceOnly": body.get("reduceOnly", ["false"])[0] == "true",
                    "updateTime": 1800000000000,
                }
            )
        raise AssertionError(request.full_url)

    result = _canary(tmp_path, monkeypatch, opener).run("2026-06-09", execute_demo=True, quantity=0.002)

    assert result["status"] == "pass"
    assert result["network_order_created"] is True
    assert result["network_close_created"] is True
    assert len(posted) == 2
    assert posted[0]["side"] == ["BUY"]
    assert posted[1]["side"] == ["SELL"]
    assert posted[1]["reduceOnly"] == ["true"]
    assert result["demo_order"]["entry"]["orderId"] == 101
    assert result["demo_order"]["close"]["orderId"] == 102


def test_binance_demo_canary_blocks_non_demo_endpoint(tmp_path: Path, monkeypatch):
    def opener(request, timeout):
        raise AssertionError("network should not be reached after static guard failure")

    service = _canary(tmp_path, monkeypatch, opener)
    service.broker_config["base_url"] = "https://fapi.binance.com"

    result = service.run("2026-06-09", execute_demo=True, quantity=0.002)

    assert result["status"] == "fail"
    assert result["checks"][0]["name"] == "static_guard"
    assert result["checks"][0]["status"] == "fail"
    assert result["network_order_created"] is False


def test_binance_demo_canary_blocks_existing_xau_position(tmp_path: Path, monkeypatch):
    def opener(request, timeout):
        if request.full_url.endswith("/fapi/v1/exchangeInfo"):
            return _FakeResponse(_exchange_info())
        if "/fapi/v2/account" in request.full_url:
            return _FakeResponse(_account())
        if "/fapi/v2/positionRisk" in request.full_url:
            return _FakeResponse([{"symbol": "XAUUSDT", "positionAmt": "0.25", "entryPrice": "4300", "unRealizedProfit": "1"}])
        if request.full_url.endswith("/fapi/v1/order/test"):
            return _FakeResponse({})
        raise AssertionError("order endpoints should not be reached")

    result = _canary(tmp_path, monkeypatch, opener).run("2026-06-09", execute_demo=True, quantity=0.002)

    assert result["status"] == "fail"
    position_check = next(item for item in result["checks"] if item["name"] == "position_precheck")
    assert position_check["status"] == "fail"
    assert result["network_order_created"] is False
