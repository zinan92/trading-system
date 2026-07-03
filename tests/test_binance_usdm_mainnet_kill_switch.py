import json
import urllib.parse
from pathlib import Path

from services.binance_usdm_mainnet_kill_switch import BinanceUsdmMainnetKillSwitch
from services.journal_store import load_json


class _FakeResponse:
    status = 200

    def __init__(self, payload) -> None:
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
            {
                "symbol": "XAUUSDT",
                "status": "TRADING",
                "contractType": "TRADIFI_PERPETUAL",
                "marginAsset": "USDT",
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                    {"filterType": "LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"},
                    {"filterType": "MARKET_LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5"},
                ],
            }
        ]
    }


def _env(tmp_path: Path, monkeypatch) -> None:
    env = tmp_path / "live.env"
    env.write_text("BINANCE_API_KEY=ALPHA123\nBINANCE_API_SECRET=OMEGA456\n", encoding="utf-8")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(env))
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)


def _service(root: Path, opener) -> BinanceUsdmMainnetKillSwitch:
    service = BinanceUsdmMainnetKillSwitch(root, opener=opener)
    service.broker_config = {
        "provider": "binance_usdm",
        "environment": "live",
        "base_url": "https://fapi.binance.com",
        "dry_run": False,
        "api_key_env": "BINANCE_API_KEY",
        "api_secret_env": "BINANCE_API_SECRET",
        "instrument_map": {"GOLD": "XAUUSDT"},
        "request_dir": "live_order_requests",
        "protective_order_endpoint": "algoOrder",
        "reconcile_account_history": True,
    }
    return service


def test_mainnet_kill_switch_dry_run_does_not_activate_halt(tmp_path: Path, monkeypatch):
    _env(tmp_path, monkeypatch)

    def opener(request, timeout):
        assert request.full_url.endswith("/fapi/v1/exchangeInfo?symbol=XAUUSDT")
        return _FakeResponse(_exchange_info())

    root = tmp_path / "outputs"
    result = _service(root, opener).run("2026-07-03")

    assert result["status"] == "dry_run"
    assert result["base_url"] == "https://fapi.binance.com"
    assert result["halt"] == {}
    assert not (root / "live_halt" / "current.json").exists()


def test_mainnet_kill_switch_flattens_cancels_confirms_flat_and_persists_halt(tmp_path: Path, monkeypatch):
    _env(tmp_path, monkeypatch)
    root = tmp_path / "outputs"
    state = {
        "position_amt": 0.002,
        "open_orders": [{"symbol": "XAUUSDT", "orderId": 11, "clientOrderId": "manual_limit", "type": "LIMIT", "side": "BUY", "origQty": "0.002", "status": "NEW"}],
        "algo_orders": [{"symbol": "XAUUSDT", "algoId": 21, "clientAlgoId": "protect_sl", "orderType": "STOP_MARKET", "side": "SELL", "origQty": "0.002", "reduceOnly": "true", "status": "NEW"}],
        "posts": [],
        "deleted_open": False,
        "deleted_algo": False,
    }

    def opener(request, timeout):
        url = request.full_url
        if url.endswith("/fapi/v1/exchangeInfo?symbol=XAUUSDT"):
            return _FakeResponse(_exchange_info())
        if "/fapi/v2/positionRisk" in url:
            return _FakeResponse([{"symbol": "XAUUSDT", "positionAmt": str(state["position_amt"]), "entryPrice": "4325.10", "unRealizedProfit": "0"}])
        if "/fapi/v2/balance" in url:
            return _FakeResponse([{"asset": "USDT", "balance": "100.0", "availableBalance": "90.0"}])
        if "/fapi/v1/openOrders" in url:
            return _FakeResponse(state["open_orders"])
        if "/fapi/v1/openAlgoOrders" in url:
            return _FakeResponse(state["algo_orders"])
        if "/fapi/v1/userTrades" in url:
            return _FakeResponse([])
        if "/fapi/v1/income" in url:
            return _FakeResponse([])
        if url.endswith("/fapi/v1/allOpenOrders"):
            state["deleted_open"] = True
            state["open_orders"] = []
            return _FakeResponse({"code": 200, "msg": "success"})
        if url.endswith("/fapi/v1/algoOpenOrders"):
            state["deleted_algo"] = True
            state["algo_orders"] = []
            return _FakeResponse({"code": 200, "msg": "success"})
        if url.endswith("/fapi/v1/order"):
            body = urllib.parse.parse_qs(request.data.decode("utf-8"))
            state["posts"].append(body)
            assert body["reduceOnly"] == ["true"]
            state["position_amt"] = 0.0
            return _FakeResponse(
                {
                    "orderId": 4001,
                    "symbol": "XAUUSDT",
                    "clientOrderId": body["newClientOrderId"][0],
                    "side": body["side"][0],
                    "type": body["type"][0],
                    "origQty": body["quantity"][0],
                    "executedQty": body["quantity"][0],
                    "avgPrice": "4327.10",
                    "status": "FILLED",
                    "reduceOnly": True,
                }
            )
        raise AssertionError(url)

    result = _service(root, opener).run("2026-07-03", confirm_mainnet_kill=True, mainnet_approved=True)

    assert result["status"] == "halted_flat_confirmed"
    assert result["network_order_created"] is True
    assert result["confirmed_flat"] is True
    assert state["deleted_open"] is True
    assert state["deleted_algo"] is True
    assert len(state["posts"]) == 1
    assert state["posts"][0]["side"] == ["SELL"]
    assert load_json(root / "live_halt" / "current.json")[0]["active"] is True


def test_mainnet_kill_switch_does_not_treat_200_body_error_as_success(tmp_path: Path, monkeypatch):
    _env(tmp_path, monkeypatch)
    root = tmp_path / "outputs"
    state = {"position_amt": 0.002, "open_orders": [], "algo_orders": [], "posts": []}

    def opener(request, timeout):
        url = request.full_url
        if url.endswith("/fapi/v1/exchangeInfo?symbol=XAUUSDT"):
            return _FakeResponse(_exchange_info())
        if "/fapi/v2/positionRisk" in url:
            return _FakeResponse([{"symbol": "XAUUSDT", "positionAmt": str(state["position_amt"]), "entryPrice": "4325.10", "unRealizedProfit": "0"}])
        if "/fapi/v2/balance" in url:
            return _FakeResponse([{"asset": "USDT", "balance": "100.0", "availableBalance": "90.0"}])
        if "/fapi/v1/openOrders" in url:
            return _FakeResponse(state["open_orders"])
        if "/fapi/v1/openAlgoOrders" in url:
            return _FakeResponse(state["algo_orders"])
        if "/fapi/v1/userTrades" in url:
            return _FakeResponse([])
        if "/fapi/v1/income" in url:
            return _FakeResponse([])
        if url.endswith("/fapi/v1/allOpenOrders") or url.endswith("/fapi/v1/algoOpenOrders"):
            return _FakeResponse({"code": 200, "msg": "success"})
        if url.endswith("/fapi/v1/order"):
            state["posts"].append(urllib.parse.parse_qs(request.data.decode("utf-8")))
            return _FakeResponse({"code": -2022, "msg": "ReduceOnly Order is rejected."})
        raise AssertionError(url)

    result = _service(root, opener).run("2026-07-03", confirm_mainnet_kill=True, mainnet_approved=True)

    assert result["status"] == "halt_active_not_flat"
    assert result["confirmed_flat"] is False
    assert result["network_order_created"] is False
    assert result["close_orders"][0]["status"] == "failed"
    assert result["close_orders"][0]["error"]["error_type"] == "BinanceCloseBodyError"
