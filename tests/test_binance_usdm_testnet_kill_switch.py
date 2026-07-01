import json
import urllib.parse
from pathlib import Path

from services.binance_usdm_testnet_broker_adapter import BinanceUsdmTestnetBrokerAdapter
from services.binance_usdm_testnet_kill_switch import BinanceUsdmTestnetKillSwitch
from services.broker_adapter import BrokerOrderRequest
from services.journal_store import load_json, write_json
from services.live_money_guardrails import LiveMoneyGuardrails
from services.order_lifecycle import OrderLifecycleStore


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


def _ticket() -> dict:
    return {
        "ticket_id": "testnet_ticket_gold_20260609",
        "signal_id": "testnet_signal",
        "asset": "GOLD",
        "action": "prepare_buy",
        "entry_zone": "4325.10-4325.10",
        "stop_loss": 4312.12,
        "targets": [4338.08],
        "position_size_pct": 0.01,
        "max_loss_pct": 0.01,
        "order_type": "market",
        "time_in_force": "day",
    }


def _env(tmp_path: Path, monkeypatch) -> None:
    env = tmp_path / "live.env"
    env.write_text("BINANCE_API_KEY=ALPHA123\nBINANCE_API_SECRET=OMEGA456\n", encoding="utf-8")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(env))
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)


def _adapter(tmp_path: Path, opener) -> BinanceUsdmTestnetBrokerAdapter:
    broker_config = {
        "provider": "binance_usdm",
        "environment": "testnet",
        "base_url": "https://demo-fapi.binance.com",
        "dry_run": False,
        "api_key_env": "BINANCE_API_KEY",
        "api_secret_env": "BINANCE_API_SECRET",
        "instrument_map": {"GOLD": "XAUUSDT"},
        "request_dir": "testnet_order_requests",
        "protective_order_endpoint": "algoOrder",
        "reconcile_account_history": True,
    }
    testnet_config = {"request_dir": "testnet_order_requests", "max_order_quantity": 0.002, "require_flat_before_entry": True}
    return BinanceUsdmTestnetBrokerAdapter(tmp_path / "outputs", broker_config, testnet_config, opener=opener)


def _seed_local_exchange_managed_trade(root: Path, run_date: str) -> None:
    write_json(root / "clean_bars" / run_date / "GOLD_5m.json", [{"timestamp": "2026-06-09T00:00:00+00:00", "high": 4328, "low": 4320, "close": 4327.1}])
    write_json(
        root / "paper_trades" / "current.json",
        [
            {
                "trade_id": "trade_testnet_order",
                "order_id": "testnet_order_1",
                "ticket_id": "testnet_ticket_1",
                "symbol": "GOLD",
                "side": "long",
                "status": "open",
                "quantity": 0.002,
                "entry_price": 4325.1,
                "entry_total_cost": 0.0,
                "quality_flags": ["exchange_managed"],
            }
        ],
    )
    store = OrderLifecycleStore(root)
    store.write_intent(
        run_date,
        order_id="testnet_order_1",
        ticket_id="testnet_ticket_1",
        idempotency_key="testnet_order_1",
        requested_quantity=0.002,
        requested_price=4325.1,
        source="binance_usdm:testnet",
        metadata={"symbol": "XAUUSDT", "ticket": _ticket()},
    )
    for state in ["submitting", "accepted", "filled", "protective_attached"]:
        store.transition(run_date, "testnet_order_1", state, reason=f"test_{state}")


def test_testnet_kill_switch_dry_run_does_not_activate_halt(tmp_path: Path, monkeypatch):
    _env(tmp_path, monkeypatch)

    def opener(request, timeout):
        if request.full_url.endswith("/fapi/v1/exchangeInfo?symbol=XAUUSDT"):
            return _FakeResponse(_exchange_info())
        raise AssertionError(request.full_url)

    service = BinanceUsdmTestnetKillSwitch(tmp_path / "outputs", opener=opener)
    service.broker_config = {
        "provider": "binance_usdm",
        "environment": "testnet",
        "base_url": "https://demo-fapi.binance.com",
        "dry_run": False,
        "api_key_env": "BINANCE_API_KEY",
        "api_secret_env": "BINANCE_API_SECRET",
        "instrument_map": {"GOLD": "XAUUSDT"},
        "request_dir": "testnet_order_requests",
        "protective_order_endpoint": "algoOrder",
        "reconcile_account_history": True,
    }

    result = service.run("2026-06-09")

    assert result["status"] == "dry_run"
    assert result["halt"] == {}
    assert not (tmp_path / "outputs" / "live_halt" / "current.json").exists()


def test_testnet_kill_switch_flattens_cancels_confirms_flat_and_persists_halt(tmp_path: Path, monkeypatch):
    _env(tmp_path, monkeypatch)
    run_date = "2026-06-09"
    root = tmp_path / "outputs"
    _seed_local_exchange_managed_trade(root, run_date)
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
            return _FakeResponse([{"asset": "USDT", "balance": "5000.0", "availableBalance": "4990.0"}])
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

    service = BinanceUsdmTestnetKillSwitch(root, opener=opener)
    service.broker_config = {
        "provider": "binance_usdm",
        "environment": "testnet",
        "base_url": "https://demo-fapi.binance.com",
        "dry_run": False,
        "api_key_env": "BINANCE_API_KEY",
        "api_secret_env": "BINANCE_API_SECRET",
        "instrument_map": {"GOLD": "XAUUSDT"},
        "request_dir": "testnet_order_requests",
        "protective_order_endpoint": "algoOrder",
        "reconcile_account_history": True,
    }
    service.testnet_config = {"request_dir": "testnet_order_requests", "max_order_quantity": 0.002, "require_flat_before_entry": True}

    result = service.run(run_date, confirm_testnet_kill=True)

    assert result["status"] == "halted_flat_confirmed"
    assert result["network_order_created"] is True
    assert result["confirmed_flat"] is True
    assert state["deleted_open"] is True
    assert state["deleted_algo"] is True
    assert len(state["posts"]) == 1
    assert state["posts"][0]["side"] == ["SELL"]
    assert result["reconciliation_after"]["confirmation_status"] == "confirmed_flat"
    assert result["reconciliation_after"]["exchange_open_orders"] == []
    assert result["reconciliation_after"]["suspected_naked_position"] is False
    assert load_json(root / "live_halt" / "current.json")[0]["active"] is True
    assert load_json(root / "paper_trades" / "current.json") == []
    assert load_json(root / "testnet_kill_switch" / "current.json")[0]["status"] == "halted_flat_confirmed"

    restarted_adapter = _adapter(tmp_path, opener)
    try:
        restarted_adapter.submit_order(BrokerOrderRequest(run_date, _ticket(), latest_price=4325.10, actual_size=0.002))
    except RuntimeError as exc:
        assert "operator HALT is active" in str(exc)
    else:
        raise AssertionError("persistent HALT must block restarted adapter")

    cleared = service.clear_halt(run_date, confirm_clear=True)
    assert cleared["status"] == "halt_cleared"
    guardrail = LiveMoneyGuardrails(root, broker_config={"request_dir": "testnet_order_requests"}).evaluate_order(
        run_date,
        ticket=_ticket(),
        symbol="XAUUSDT",
        side="BUY",
        requested_price=4325.10,
        quantity=0.002,
        source="binance_usdm:testnet",
    )
    assert guardrail["status"] == "READY"
    assert guardrail["allows_new_order"] is True
