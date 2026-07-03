import json
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from services.binance_usdm_testnet_broker_adapter import BinanceUsdmTestnetBrokerAdapter
from services.broker_adapter import BrokerOrderRequest
from services.binance_usdm_testnet_canary import BinanceUsdmTestnetCanary
from services.journal_store import load_json
from services.order_lifecycle import OrderLifecycleStore
from services.run_date import utc_run_date


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


def _canary(tmp_path: Path, monkeypatch, opener):
    env = tmp_path / "live.env"
    env.write_text("BINANCE_API_KEY=ALPHA123\nBINANCE_API_SECRET=OMEGA456\n", encoding="utf-8")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(env))
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    service = BinanceUsdmTestnetCanary(tmp_path / "outputs", opener=opener)
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
    return service


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


def _adapter(tmp_path: Path, monkeypatch, opener) -> BinanceUsdmTestnetBrokerAdapter:
    env = tmp_path / "live.env"
    env.write_text("BINANCE_API_KEY=ALPHA123\nBINANCE_API_SECRET=OMEGA456\n", encoding="utf-8")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(env))
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


def _common_testnet_gets(url: str):
    if url.endswith("/fapi/v1/exchangeInfo?symbol=XAUUSDT"):
        return _FakeResponse(_exchange_info())
    if "/fapi/v2/positionRisk" in url:
        return _FakeResponse([{"symbol": "XAUUSDT", "positionAmt": "0", "entryPrice": "0", "unRealizedProfit": "0"}])
    if "/fapi/v2/balance" in url:
        return _FakeResponse([{"asset": "USDT", "balance": "5000.0", "availableBalance": "4990.0"}])
    if "/fapi/v1/openOrders" in url or "/fapi/v1/openAlgoOrders" in url or "/fapi/v1/userTrades" in url or "/fapi/v1/income" in url:
        return _FakeResponse([])
    return None


def test_binance_usdm_testnet_adapter_hard_blocks_daily_loss_before_entry_post(tmp_path: Path, monkeypatch):
    posted = []

    def opener(request, timeout):
        url = request.full_url
        if url.endswith("/fapi/v1/exchangeInfo?symbol=XAUUSDT"):
            return _FakeResponse(_exchange_info())
        if "/fapi/v2/positionRisk" in url:
            return _FakeResponse([{"symbol": "XAUUSDT", "positionAmt": "0", "entryPrice": "0", "unRealizedProfit": "0"}])
        if "/fapi/v2/balance" in url:
            return _FakeResponse([{"asset": "USDT", "balance": "1000.0", "availableBalance": "990.0"}])
        if "/fapi/v1/openOrders" in url or "/fapi/v1/openAlgoOrders" in url:
            return _FakeResponse([])
        if "/fapi/v1/userTrades" in url:
            return _FakeResponse(
                [
                    {
                        "symbol": "XAUUSDT",
                        "id": 1,
                        "orderId": 1001,
                        "side": "SELL",
                        "price": "4325.10",
                        "qty": "0.002",
                        "quoteQty": "8.6502",
                        "commission": "0",
                        "commissionAsset": "USDT",
                        "realizedPnl": "-8.0",
                        "time": 1781006400000,
                    }
                ]
            )
        if "/fapi/v1/income" in url:
            return _FakeResponse([{"symbol": "XAUUSDT", "incomeType": "FUNDING_FEE", "income": "-5.0", "asset": "USDT", "time": 1781006402000}])
        if url.endswith("/fapi/v1/order") or url.endswith("/fapi/v1/algoOrder"):
            posted.append(url)
            raise AssertionError("daily loss hard guardrail must block before entry/protective POST")
        raise AssertionError(url)

    adapter = _adapter(tmp_path, monkeypatch, opener)
    try:
        adapter.submit_order(BrokerOrderRequest("2026-06-09", _ticket(), latest_price=4325.10, actual_size=0.002))
    except RuntimeError as exc:
        assert "daily live/testnet loss" in str(exc)
    else:
        raise AssertionError("expected daily loss guardrail to block")

    assert posted == []
    guardrail = load_json(tmp_path / "outputs" / "live_money_guardrails" / "current.json")[0]
    assert guardrail["status"] == "BLOCKED_DAILY_LOSS_LIMIT"
    request = load_json(tmp_path / "outputs" / "testnet_order_requests" / "2026-06-09.json")[0]
    assert request["receipt"]["status"] == "blocked"
    assert request["broker_response"]["live_money_guardrails"]["status"] == "BLOCKED_DAILY_LOSS_LIMIT"


def test_binance_usdm_testnet_adapter_uses_utc_run_date_during_utc_plus_8_next_day_window(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    if hasattr(time, "tzset"):
        time.tzset()
    run_date = utc_run_date(datetime(2026, 6, 9, 16, 30, tzinfo=timezone.utc))
    posted = []
    seen_history_queries = {}

    def opener(request, timeout):
        url = request.full_url
        if url.endswith("/fapi/v1/exchangeInfo?symbol=XAUUSDT"):
            return _FakeResponse(_exchange_info())
        if "/fapi/v2/positionRisk" in url:
            return _FakeResponse([{"symbol": "XAUUSDT", "positionAmt": "0", "entryPrice": "0", "unRealizedProfit": "0"}])
        if "/fapi/v2/balance" in url:
            return _FakeResponse([{"asset": "USDT", "balance": "5000.0", "availableBalance": "4990.0"}])
        if "/fapi/v1/openOrders" in url or "/fapi/v1/openAlgoOrders" in url:
            return _FakeResponse([])
        if "/fapi/v1/userTrades" in url:
            seen_history_queries["fills"] = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            return _FakeResponse(
                [
                    {
                        "symbol": "XAUUSDT",
                        "id": 1,
                        "orderId": 1001,
                        "side": "SELL",
                        "price": "4325.10",
                        "qty": "0.002",
                        "quoteQty": "8.6502",
                        "commission": "0",
                        "commissionAsset": "USDT",
                        "realizedPnl": "-500.0",
                        "time": 1781022600000,
                    }
                ]
            )
        if "/fapi/v1/income" in url:
            seen_history_queries["income"] = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            return _FakeResponse([])
        if url.endswith("/fapi/v1/order") or url.endswith("/fapi/v1/algoOrder"):
            posted.append(url)
            raise AssertionError("UTC-day daily loss must block before any order POST")
        raise AssertionError(url)

    try:
        adapter = _adapter(tmp_path, monkeypatch, opener)
        adapter.submit_order(BrokerOrderRequest(run_date, _ticket(), latest_price=4325.10, actual_size=0.002))
    except RuntimeError as exc:
        assert "daily live/testnet loss" in str(exc)
    else:
        raise AssertionError("expected UTC-day daily loss guardrail to block")
    finally:
        monkeypatch.delenv("TZ", raising=False)
        if hasattr(time, "tzset"):
            time.tzset()

    assert run_date == "2026-06-09"
    assert posted == []
    assert seen_history_queries["fills"]["startTime"] == ["1780963200000"]
    assert seen_history_queries["fills"]["endTime"] == ["1781049599999"]
    assert seen_history_queries["income"]["startTime"] == ["1780963200000"]
    assert seen_history_queries["income"]["endTime"] == ["1781049599999"]
    guardrail = load_json(tmp_path / "outputs" / "live_money_guardrails" / "current.json")[0]
    assert guardrail["status"] == "BLOCKED_DAILY_LOSS_LIMIT"


def test_binance_usdm_testnet_canary_runs_signed_order_protective_reconcile_close_loop(tmp_path: Path, monkeypatch):
    state = {"position": "flat", "orders": [], "algos": [], "deleted_algo": False}

    def opener(request, timeout):
        url = request.full_url
        if url.endswith("/fapi/v1/exchangeInfo?symbol=XAUUSDT"):
            return _FakeResponse(_exchange_info())
        if "/fapi/v1/ticker/price" in url:
            return _FakeResponse({"symbol": "XAUUSDT", "price": "4325.10"})
        if "/fapi/v2/positionRisk" in url:
            amount = "0.002" if state["position"] == "open" else "0"
            return _FakeResponse([{"symbol": "XAUUSDT", "positionAmt": amount, "entryPrice": "4325.10", "unRealizedProfit": "0.42"}])
        if "/fapi/v2/balance" in url:
            return _FakeResponse([{"asset": "USDT", "balance": "5000.0", "availableBalance": "4990.0"}])
        if "/fapi/v1/openAlgoOrders" in url:
            return _FakeResponse(state["algos"] if state["position"] == "open" else [])
        if "/fapi/v1/openOrders" in url:
            return _FakeResponse([])
        if "/fapi/v1/userTrades" in url:
            rows = []
            if state["orders"]:
                rows.append(
                    {
                        "symbol": "XAUUSDT",
                        "id": 1,
                        "orderId": 1001,
                        "side": "BUY",
                        "price": "4325.10",
                        "qty": "0.002",
                        "quoteQty": "8.6502",
                        "commission": "0.00346008",
                        "commissionAsset": "USDT",
                        "realizedPnl": "0",
                        "time": 1781006400000,
                    }
                )
            if len(state["orders"]) > 1:
                rows.append(
                    {
                        "symbol": "XAUUSDT",
                        "id": 2,
                        "orderId": 1002,
                        "side": "SELL",
                        "price": "4327.10",
                        "qty": "0.002",
                        "quoteQty": "8.6542",
                        "commission": "0.00346168",
                        "commissionAsset": "USDT",
                        "realizedPnl": "0.004",
                        "time": 1781006401000,
                    }
                )
            return _FakeResponse(rows)
        if "/fapi/v1/income" in url:
            return _FakeResponse([{"symbol": "XAUUSDT", "incomeType": "FUNDING_FEE", "income": "-0.0002", "asset": "USDT", "time": 1781006402000}])
        if url.endswith("/fapi/v1/algoOrder"):
            body = urllib.parse.parse_qs(request.data.decode("utf-8"))
            state["algos"].append(
                {
                    "algoId": 2000 + len(state["algos"]),
                    "clientAlgoId": body["clientAlgoId"][0],
                    "symbol": body["symbol"][0],
                    "side": body["side"][0],
                    "algoType": body["algoType"][0],
                    "orderType": body["type"][0],
                    "origQty": body["quantity"][0],
                    "reduceOnly": body["reduceOnly"][0],
                    "status": "NEW",
                }
            )
            return _FakeResponse(state["algos"][-1])
        if url.endswith("/fapi/v1/order"):
            body = urllib.parse.parse_qs(request.data.decode("utf-8"))
            state["orders"].append(body)
            reduce_only = body.get("reduceOnly", ["false"])[0] == "true"
            state["position"] = "flat" if reduce_only else "open"
            return _FakeResponse(
                {
                    "orderId": 1000 + len(state["orders"]),
                    "symbol": "XAUUSDT",
                    "clientOrderId": body["newClientOrderId"][0],
                    "side": body["side"][0],
                    "type": body["type"][0],
                    "origQty": body["quantity"][0],
                    "executedQty": body["quantity"][0],
                    "avgPrice": "4327.10" if reduce_only else "4325.10",
                    "status": "FILLED",
                    "reduceOnly": reduce_only,
                }
            )
        if url.endswith("/fapi/v1/allOpenOrders") or url.endswith("/fapi/v1/algoOpenOrders"):
            if url.endswith("/fapi/v1/algoOpenOrders"):
                state["deleted_algo"] = True
            return _FakeResponse({"code": 200, "msg": "success"})
        raise AssertionError(url)

    result = _canary(tmp_path, monkeypatch, opener).run("2026-06-09", execute_testnet=True, quantity=0.002)

    assert result["status"] == "pass"
    assert result["network_order_created"] is True
    assert result["network_close_created"] is True
    assert state["orders"][0]["newOrderRespType"] == ["RESULT"]
    assert len(state["algos"]) == 2
    assert {item["orderType"] for item in state["algos"]} == {"STOP_MARKET", "TAKE_PROFIT_MARKET"}
    assert all(item["reduceOnly"] == "true" and item["origQty"] == "0.002" for item in state["algos"])
    assert state["deleted_algo"] is True
    assert result["reconciliation_open"]["confirmation_status"] == "confirmed_open"
    assert result["reconciliation_open"]["exchange_accounting"]["commission_by_asset"]["USDT"] == 0.00346008
    final_accounting = result["close_report"]["reconciliation_after"]["exchange_accounting"]
    assert final_accounting["funding_by_asset"]["USDT"] == -0.0002
    assert final_accounting["net_realized_pnl_estimate"] == -0.00312176
    lifecycle = result["lifecycle"]
    assert lifecycle["state"] == "reconciled"
    assert [item["to"] for item in lifecycle["transitions"]][-3:] == ["protective_attached", "closed", "reconciled"]

    stored = load_json(tmp_path / "outputs" / "binance_usdm_testnet_canary" / "2026-06-09.json")[0]
    assert stored["status"] == "pass"
    assert "ALPHA123" not in json.dumps(stored)
    assert "OMEGA456" not in json.dumps(stored)


def test_binance_usdm_testnet_not_found_recovery_posts_once_with_stable_key(tmp_path: Path, monkeypatch):
    posted_entries = []
    posted_algos = []
    bootstrap = _adapter(tmp_path, monkeypatch, lambda request, timeout: _FakeResponse({}))
    order_id = bootstrap._order_id(_ticket()["ticket_id"], "2026-06-09", 4325.10)
    moved_price_order_id = bootstrap._order_id(_ticket()["ticket_id"], "2026-06-09", 4330.50)
    assert moved_price_order_id == order_id
    store = OrderLifecycleStore(tmp_path / "outputs")
    store.write_intent(
        "2026-06-09",
        order_id=order_id,
        ticket_id=_ticket()["ticket_id"],
        idempotency_key=order_id[:36],
        requested_quantity=0.002,
        requested_price=4325.10,
        source="binance_usdm:testnet",
    )
    store.transition("2026-06-09", order_id, "submitting", reason="crash_after_submit_started")

    def opener(request, timeout):
        common = _common_testnet_gets(request.full_url)
        if common is not None:
            return common
        if "/fapi/v1/order?" in request.full_url:
            return _FakeResponse({"code": -2013, "msg": "Order does not exist."})
        if request.full_url.endswith("/fapi/v1/algoOrder"):
            body = urllib.parse.parse_qs(request.data.decode("utf-8"))
            posted_algos.append(body)
            return _FakeResponse(
                {
                    "algoId": 3000 + len(posted_algos),
                    "clientAlgoId": body["clientAlgoId"][0],
                    "symbol": body["symbol"][0],
                    "side": body["side"][0],
                    "algoType": body["algoType"][0],
                    "orderType": body["type"][0],
                    "origQty": body["quantity"][0],
                    "reduceOnly": body["reduceOnly"][0],
                    "status": "NEW",
                }
            )
        if request.full_url.endswith("/fapi/v1/order"):
            body = urllib.parse.parse_qs(request.data.decode("utf-8"))
            posted_entries.append(body)
            return _FakeResponse(
                {
                    "orderId": 1001,
                    "symbol": "XAUUSDT",
                    "clientOrderId": body["newClientOrderId"][0],
                    "side": body["side"][0],
                    "type": body["type"][0],
                    "origQty": body["quantity"][0],
                    "executedQty": body["quantity"][0],
                    "avgPrice": "4330.50",
                    "status": "FILLED",
                }
            )
        raise AssertionError(request.full_url)

    order = _adapter(tmp_path, monkeypatch, opener).submit_order(BrokerOrderRequest("2026-06-09", _ticket(), latest_price=4330.50, actual_size=0.002))

    assert order.status == "filled"
    assert len(posted_entries) == 1
    assert posted_entries[0]["newClientOrderId"] == [order_id[:36]]
    assert len(posted_algos) == 2
    request = load_json(tmp_path / "outputs" / "testnet_order_requests" / "2026-06-09.json")[0]
    assert request["broker_response"]["entry_recovery"]["status"] == "not_found"
    assert request["request"]["entry"]["newClientOrderId"] == order_id[:36]


def test_binance_usdm_testnet_ambiguous_recovery_does_not_post_duplicate(tmp_path: Path, monkeypatch):
    posted = []
    bootstrap = _adapter(tmp_path, monkeypatch, lambda request, timeout: _FakeResponse({}))
    order_id = bootstrap._order_id(_ticket()["ticket_id"], "2026-06-09", 4325.10)
    store = OrderLifecycleStore(tmp_path / "outputs")
    store.write_intent(
        "2026-06-09",
        order_id=order_id,
        ticket_id=_ticket()["ticket_id"],
        idempotency_key=order_id[:36],
        requested_quantity=0.002,
        requested_price=4325.10,
        source="binance_usdm:testnet",
    )
    store.transition("2026-06-09", order_id, "submitting", reason="crash_after_submit_started")

    def opener(request, timeout):
        common = _common_testnet_gets(request.full_url)
        if common is not None:
            return common
        if "/fapi/v1/order?" in request.full_url:
            raise TimeoutError("testnet order lookup timed out")
        if request.full_url.endswith("/fapi/v1/order") or request.full_url.endswith("/fapi/v1/algoOrder"):
            posted.append(request.full_url)
            raise AssertionError("ambiguous recovery must not submit a duplicate order")
        raise AssertionError(request.full_url)

    order = _adapter(tmp_path, monkeypatch, opener).submit_order(BrokerOrderRequest("2026-06-09", _ticket(), latest_price=4325.10, actual_size=0.002))

    assert order.status == "submitted_to_binance"
    assert posted == []
    request = load_json(tmp_path / "outputs" / "testnet_order_requests" / "2026-06-09.json")[0]
    assert request["broker_response"]["entry_recovery"]["status"] == "ambiguous"
    lifecycle = load_json(tmp_path / "outputs" / "order_lifecycle" / "2026-06-09.json")[0]
    assert lifecycle["state"] == "submitting"
