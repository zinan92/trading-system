import json
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

from services.binance_demo_broker_adapter import BinanceDemoBrokerAdapter
from services.broker_adapter import BrokerOrderRequest
from services.journal_store import load_json
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
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                    {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5"},
                ],
            }
        ]
    }


def _flat_position() -> list[dict]:
    return [{"symbol": "XAUUSDT", "positionAmt": "0", "entryPrice": "0", "unRealizedProfit": "0"}]


def _short_position() -> list[dict]:
    return [{"symbol": "XAUUSDT", "positionAmt": "-0.002", "entryPrice": "4232.82", "unRealizedProfit": "-0.12"}]


def _balance() -> list[dict]:
    return [{"asset": "USDT", "balance": "4997.0", "availableBalance": "4996.5"}]


def _ticket() -> dict:
    return {
        "ticket_id": "ticket_gold_20260609_chan2",
        "signal_id": "sig_gold_chan2",
        "asset": "GOLD",
        "action": "prepare_buy",
        "entry_zone": "4520-4530",
        "stop_loss": 4500,
        "targets": [4560],
        "position_size_pct": 5,
        "max_loss_pct": 0.25,
        "order_type": "market",
        "time_in_force": "day",
    }


def _adapter(tmp_path: Path, monkeypatch, opener) -> BinanceDemoBrokerAdapter:
    monkeypatch.setenv("BINANCE_API_KEY", "key")
    monkeypatch.setenv("BINANCE_API_SECRET", "secret")
    return BinanceDemoBrokerAdapter(
        tmp_path / "outputs",
        {
            "provider": "binance_usdm",
            "base_url": "https://demo-fapi.binance.com",
            "api_key_env": "BINANCE_API_KEY",
            "api_secret_env": "BINANCE_API_SECRET",
            "instrument_map": {"GOLD": "XAUUSDT"},
            "timeout_seconds": 5,
        },
        {"request_dir": "demo_order_requests", "max_order_quantity": 0.002, "require_flat_before_entry": True},
        opener=opener,
    )


def test_binance_demo_adapter_caps_quantity_posts_demo_orders_and_mirrors_fill(tmp_path: Path, monkeypatch):
    posted = []

    def opener(request, timeout):
        if "/fapi/v1/exchangeInfo" in request.full_url:
            return _FakeResponse(_exchange_info())
        if "/fapi/v2/positionRisk" in request.full_url:
            return _FakeResponse(_flat_position())
        if "/fapi/v2/balance" in request.full_url:
            return _FakeResponse(_balance())
        if "/fapi/v1/openOrders" in request.full_url:
            return _FakeResponse([])
        if request.full_url.endswith("/fapi/v1/order"):
            body = urllib.parse.parse_qs(request.data.decode("utf-8"))
            posted.append(body)
            return _FakeResponse(
                {
                    "orderId": 1000 + len(posted),
                    "symbol": "XAUUSDT",
                    "clientOrderId": body["newClientOrderId"][0],
                    "side": body["side"][0],
                    "type": body["type"][0],
                    "origQty": body.get("quantity", [""])[0],
                    "executedQty": body.get("quantity", ["0"])[0],
                    "avgPrice": None,
                }
            )
        raise AssertionError(request.full_url)

    adapter = _adapter(tmp_path, monkeypatch, opener)
    order = adapter.submit_order(BrokerOrderRequest("2026-06-09", _ticket(), latest_price=4525.5, actual_size=0.25))

    assert order.status == "filled"
    assert order.quantity == 0.002
    assert order.fill_price == 4525.5
    assert len(posted) == 3
    assert posted[0]["symbol"] == ["XAUUSDT"]
    assert posted[0]["quantity"] == ["0.002"]
    assert posted[0]["side"] == ["BUY"]
    assert posted[1]["type"] == ["STOP_MARKET"]
    assert posted[2]["type"] == ["TAKE_PROFIT_MARKET"]
    assert posted[1]["quantity"] == ["0.002"]
    assert posted[2]["quantity"] == ["0.002"]
    assert posted[1]["reduceOnly"] == ["true"]
    assert posted[2]["reduceOnly"] == ["true"]

    requests = load_json(tmp_path / "outputs" / "demo_order_requests" / "2026-06-09.json")
    assert requests[0]["readiness"]["mode"] == "demo"
    assert requests[0]["broker_response"]["fill_price_source"] == "requested_price_fallback_after_executed_qty"
    lifecycle = load_json(tmp_path / "outputs" / "order_lifecycle" / "2026-06-09.json")[0]
    assert lifecycle["state"] == "protective_attached"
    assert [item["to"] for item in lifecycle["transitions"]] == ["entry", "submitting", "accepted", "filled", "protective_attached"]
    position = json.loads((tmp_path / "outputs" / "paper_positions" / "current.json").read_text())["GOLD"]
    assert position["side"] == "long"
    assert position["quantity"] == 0.002
    assert position["avg_price"] == 4525.5


def test_binance_demo_adapter_partial_fill_protects_actual_fill_quantity(tmp_path: Path, monkeypatch):
    posted = []

    def opener(request, timeout):
        if "/fapi/v1/exchangeInfo" in request.full_url:
            return _FakeResponse(_exchange_info())
        if "/fapi/v2/positionRisk" in request.full_url:
            return _FakeResponse(_flat_position())
        if "/fapi/v2/balance" in request.full_url:
            return _FakeResponse(_balance())
        if "/fapi/v1/openOrders" in request.full_url:
            return _FakeResponse([])
        if request.full_url.endswith("/fapi/v1/order"):
            body = urllib.parse.parse_qs(request.data.decode("utf-8"))
            posted.append(body)
            executed = "0.001" if len(posted) == 1 else body.get("quantity", ["0"])[0]
            return _FakeResponse(
                {
                    "orderId": 1100 + len(posted),
                    "symbol": "XAUUSDT",
                    "clientOrderId": body["newClientOrderId"][0],
                    "side": body["side"][0],
                    "type": body["type"][0],
                    "origQty": body.get("quantity", [""])[0],
                    "executedQty": executed,
                    "avgPrice": "4525.5" if len(posted) == 1 else None,
                    "status": "PARTIALLY_FILLED" if len(posted) == 1 else "NEW",
                }
            )
        raise AssertionError(request.full_url)

    adapter = _adapter(tmp_path, monkeypatch, opener)
    order = adapter.submit_order(BrokerOrderRequest("2026-06-09", _ticket(), latest_price=4525.5, actual_size=0.002))

    assert order.status == "filled"
    assert order.quantity == 0.001
    assert len(posted) == 3
    assert posted[0]["quantity"] == ["0.002"]
    assert posted[1]["quantity"] == ["0.001"]
    assert posted[2]["quantity"] == ["0.001"]
    lifecycle = load_json(tmp_path / "outputs" / "order_lifecycle" / "2026-06-09.json")[0]
    assert lifecycle["state"] == "protective_attached"
    assert lifecycle["filled_quantity"] == 0.001
    assert lifecycle["protective_quantity"] == 0.001
    assert [item["to"] for item in lifecycle["transitions"]] == ["entry", "submitting", "accepted", "partially_filled", "protective_attached"]


def test_binance_demo_adapter_rejected_entry_creates_no_phantom_position(tmp_path: Path, monkeypatch):
    def opener(request, timeout):
        if "/fapi/v1/exchangeInfo" in request.full_url:
            return _FakeResponse(_exchange_info())
        if "/fapi/v2/positionRisk" in request.full_url:
            return _FakeResponse(_flat_position())
        if "/fapi/v2/balance" in request.full_url:
            return _FakeResponse(_balance())
        if "/fapi/v1/openOrders" in request.full_url:
            return _FakeResponse([])
        if request.full_url.endswith("/fapi/v1/order"):
            raise OSError("venue rejected: insufficient margin")
        raise AssertionError(request.full_url)

    adapter = _adapter(tmp_path, monkeypatch, opener)
    order = adapter.submit_order(BrokerOrderRequest("2026-06-09", _ticket(), latest_price=4525.5, actual_size=0.002))

    assert order.status == "rejected"
    assert "insufficient margin" in order.rejection_reason
    assert load_json(tmp_path / "outputs" / "paper_trades" / "current.json") == []
    assert not (tmp_path / "outputs" / "paper_positions" / "current.json").exists()
    request = load_json(tmp_path / "outputs" / "demo_order_requests" / "2026-06-09.json")[0]
    assert request["receipt"]["status"] == "rejected"
    lifecycle = load_json(tmp_path / "outputs" / "order_lifecycle" / "2026-06-09.json")[0]
    assert lifecycle["state"] == "rejected"
    assert [item["to"] for item in lifecycle["transitions"]] == ["entry", "submitting", "rejected"]


def test_binance_demo_adapter_recovers_submitting_intent_without_duplicate_entry(tmp_path: Path, monkeypatch):
    posted = []
    adapter = _adapter(tmp_path, monkeypatch, lambda request, timeout: _FakeResponse({}))
    order_id = adapter._order_id(_ticket()["ticket_id"], "2026-06-09", 4525.5)
    moved_price_order_id = adapter._order_id(_ticket()["ticket_id"], "2026-06-09", 4533.3)
    assert moved_price_order_id == order_id
    OrderLifecycleStore(tmp_path / "outputs").write_intent(
        "2026-06-09",
        order_id=order_id,
        ticket_id=_ticket()["ticket_id"],
        idempotency_key=order_id[:36],
        requested_quantity=0.002,
        requested_price=4525.5,
        source="binance_usdm:demo",
    )
    OrderLifecycleStore(tmp_path / "outputs").transition("2026-06-09", order_id, "submitting", reason="crash_after_submit_started")

    def opener(request, timeout):
        if "/fapi/v1/exchangeInfo" in request.full_url:
            return _FakeResponse(_exchange_info())
        if "/fapi/v2/positionRisk" in request.full_url:
            return _FakeResponse(_flat_position())
        if "/fapi/v2/balance" in request.full_url:
            return _FakeResponse(_balance())
        if "/fapi/v1/openOrders" in request.full_url:
            return _FakeResponse([])
        if "/fapi/v1/order?" in request.full_url:
            return _FakeResponse(
                {
                    "orderId": 1201,
                    "symbol": "XAUUSDT",
                    "clientOrderId": order_id[:36],
                    "side": "BUY",
                    "type": "MARKET",
                    "origQty": "0.002",
                    "executedQty": "0.002",
                    "avgPrice": "4525.5",
                    "status": "FILLED",
                }
            )
        if request.full_url.endswith("/fapi/v1/order"):
            body = urllib.parse.parse_qs(request.data.decode("utf-8"))
            if body["newClientOrderId"][0] == order_id[:36]:
                raise AssertionError("duplicate entry order should not be posted after recovery")
            posted.append(body)
            return _FakeResponse(
                {
                    "orderId": 1300 + len(posted),
                    "symbol": "XAUUSDT",
                    "clientOrderId": body["newClientOrderId"][0],
                    "side": body["side"][0],
                    "type": body["type"][0],
                    "origQty": body.get("quantity", ["0"])[0],
                    "executedQty": body.get("quantity", ["0"])[0],
                    "avgPrice": None,
                    "status": "NEW",
                }
            )
        raise AssertionError(request.full_url)

    adapter = _adapter(tmp_path, monkeypatch, opener)
    order = adapter.submit_order(BrokerOrderRequest("2026-06-09", _ticket(), latest_price=4533.3, actual_size=0.002))

    assert order.status == "filled"
    assert len(posted) == 2
    request = load_json(tmp_path / "outputs" / "demo_order_requests" / "2026-06-09.json")[0]
    assert request["broker_response"]["entry_recovered_from_idempotency_key"] is True
    assert request["broker_response"]["entry_recovery"]["status"] == "found"
    assert request["request"]["entry"]["newClientOrderId"] == order_id[:36]
    lifecycle = load_json(tmp_path / "outputs" / "order_lifecycle" / "2026-06-09.json")[0]
    assert lifecycle["state"] == "protective_attached"
    assert lifecycle["metadata"]["ticket"]["ticket_id"] == _ticket()["ticket_id"]


def test_binance_demo_adapter_not_found_recovery_posts_once(tmp_path: Path, monkeypatch):
    posted = []
    adapter = _adapter(tmp_path, monkeypatch, lambda request, timeout: _FakeResponse({}))
    order_id = adapter._order_id(_ticket()["ticket_id"], "2026-06-09", 4525.5)
    store = OrderLifecycleStore(tmp_path / "outputs")
    store.write_intent(
        "2026-06-09",
        order_id=order_id,
        ticket_id=_ticket()["ticket_id"],
        idempotency_key=order_id[:36],
        requested_quantity=0.002,
        requested_price=4525.5,
        source="binance_usdm:demo",
    )
    store.transition("2026-06-09", order_id, "submitting", reason="crash_after_submit_started")

    def opener(request, timeout):
        if "/fapi/v1/exchangeInfo" in request.full_url:
            return _FakeResponse(_exchange_info())
        if "/fapi/v2/positionRisk" in request.full_url:
            return _FakeResponse(_flat_position())
        if "/fapi/v2/balance" in request.full_url:
            return _FakeResponse(_balance())
        if "/fapi/v1/openOrders" in request.full_url:
            return _FakeResponse([])
        if "/fapi/v1/order?" in request.full_url:
            return _FakeResponse({"code": -2013, "msg": "Order does not exist."})
        if request.full_url.endswith("/fapi/v1/order"):
            body = urllib.parse.parse_qs(request.data.decode("utf-8"))
            posted.append(body)
            return _FakeResponse(
                {
                    "orderId": 1400 + len(posted),
                    "symbol": "XAUUSDT",
                    "clientOrderId": body["newClientOrderId"][0],
                    "side": body["side"][0],
                    "type": body["type"][0],
                    "origQty": body.get("quantity", ["0"])[0],
                    "executedQty": body.get("quantity", ["0"])[0],
                    "avgPrice": "4525.5" if len(posted) == 1 else None,
                    "status": "FILLED" if len(posted) == 1 else "NEW",
                }
            )
        raise AssertionError(request.full_url)

    adapter = _adapter(tmp_path, monkeypatch, opener)
    order = adapter.submit_order(BrokerOrderRequest("2026-06-09", _ticket(), latest_price=4525.5, actual_size=0.002))

    assert order.status == "filled"
    assert len([item for item in posted if item["newClientOrderId"][0] == order_id[:36]]) == 1
    request = load_json(tmp_path / "outputs" / "demo_order_requests" / "2026-06-09.json")[0]
    assert request["broker_response"]["entry_recovery"]["status"] == "not_found"
    lifecycle = load_json(tmp_path / "outputs" / "order_lifecycle" / "2026-06-09.json")[0]
    assert lifecycle["state"] == "protective_attached"
    assert [item["to"] for item in lifecycle["transitions"]][:3] == ["entry", "submitting", "accepted"]


def test_binance_demo_adapter_ambiguous_recovery_does_not_post_and_watchdog_blocks(tmp_path: Path, monkeypatch):
    posted = []
    adapter = _adapter(tmp_path, monkeypatch, lambda request, timeout: _FakeResponse({}))
    order_id = adapter._order_id(_ticket()["ticket_id"], "2026-06-09", 4525.5)
    store = OrderLifecycleStore(tmp_path / "outputs")
    store.write_intent(
        "2026-06-09",
        order_id=order_id,
        ticket_id=_ticket()["ticket_id"],
        idempotency_key=order_id[:36],
        requested_quantity=0.002,
        requested_price=4525.5,
        source="binance_usdm:demo",
    )
    store.transition("2026-06-09", order_id, "submitting", reason="crash_after_submit_started")

    def opener(request, timeout):
        if "/fapi/v1/exchangeInfo" in request.full_url:
            return _FakeResponse(_exchange_info())
        if "/fapi/v2/positionRisk" in request.full_url:
            return _FakeResponse(_flat_position())
        if "/fapi/v2/balance" in request.full_url:
            return _FakeResponse(_balance())
        if "/fapi/v1/openOrders" in request.full_url:
            return _FakeResponse([])
        if "/fapi/v1/order?" in request.full_url:
            raise TimeoutError("exchange order lookup timed out")
        if request.full_url.endswith("/fapi/v1/order"):
            posted.append(urllib.parse.parse_qs(request.data.decode("utf-8")))
            raise AssertionError("ambiguous recovery must not post a duplicate entry")
        raise AssertionError(request.full_url)

    adapter = _adapter(tmp_path, monkeypatch, opener)
    order = adapter.submit_order(BrokerOrderRequest("2026-06-09", _ticket(), latest_price=4525.5, actual_size=0.002))

    assert order.status == "submitted_to_binance"
    assert posted == []
    request = load_json(tmp_path / "outputs" / "demo_order_requests" / "2026-06-09.json")[0]
    assert request["broker_response"]["entry_recovery"]["status"] == "ambiguous"
    lifecycle = load_json(tmp_path / "outputs" / "order_lifecycle" / "2026-06-09.json")[0]
    assert lifecycle["state"] == "submitting"

    blockers = OrderLifecycleStore(tmp_path / "outputs").watchdog_tick("2026-06-09", max_cycles=0)

    assert blockers[0]["source"] == "order_lifecycle_watchdog"
    lifecycle = load_json(tmp_path / "outputs" / "order_lifecycle" / "2026-06-09.json")[0]
    assert lifecycle["state"] == "submitting"
    assert lifecycle["blocked"] is True


def test_binance_demo_adapter_blocks_non_demo_endpoint(tmp_path: Path, monkeypatch):
    def opener(request, timeout):
        if "/fapi/v1/exchangeInfo" in request.full_url:
            return _FakeResponse(_exchange_info())
        raise AssertionError("position/order endpoints should not be reached")

    adapter = _adapter(tmp_path, monkeypatch, opener)
    adapter.broker_config["base_url"] = "https://fapi.binance.com"

    with pytest.raises(RuntimeError, match="expected https://demo-fapi.binance.com"):
        adapter.submit_order(BrokerOrderRequest("2026-06-09", _ticket(), latest_price=4525.5, actual_size=0.002))

    block = load_json(tmp_path / "outputs" / "demo_order_requests" / "2026-06-09.json")[0]
    assert block["status"] == "blocked"
    assert block["mode"] == "demo"


def test_binance_demo_adapter_emergency_closes_when_protective_orders_fail(tmp_path: Path, monkeypatch):
    posted = []

    def opener(request, timeout):
        if "/fapi/v1/exchangeInfo" in request.full_url:
            return _FakeResponse(_exchange_info())
        if "/fapi/v2/positionRisk" in request.full_url:
            return _FakeResponse(_flat_position())
        if "/fapi/v2/balance" in request.full_url:
            return _FakeResponse(_balance())
        if "/fapi/v1/openOrders" in request.full_url:
            return _FakeResponse([])
        if request.full_url.endswith("/fapi/v1/order"):
            body = urllib.parse.parse_qs(request.data.decode("utf-8"))
            posted.append(body)
            if body["type"][0] != "MARKET":
                raise OSError("protective post failed")
            return _FakeResponse(
                {
                    "orderId": 2001,
                    "symbol": "XAUUSDT",
                    "clientOrderId": body["newClientOrderId"][0],
                    "side": body["side"][0],
                    "type": body["type"][0],
                    "origQty": body.get("quantity", ["0"])[0],
                    "executedQty": body.get("quantity", ["0"])[0],
                    "avgPrice": "4525.75",
                }
            )
        raise AssertionError(request.full_url)

    adapter = _adapter(tmp_path, monkeypatch, opener)
    order = adapter.submit_order(BrokerOrderRequest("2026-06-09", _ticket(), latest_price=4525.5, actual_size=0.002))

    assert order.status == "protective_order_missing_closed"
    assert order.fill_price == 4525.75
    assert len(posted) == 4
    assert posted[-1]["type"] == ["MARKET"]
    assert posted[-1]["side"] == ["SELL"]
    assert posted[-1]["reduceOnly"] == ["true"]
    requests = load_json(tmp_path / "outputs" / "demo_order_requests" / "2026-06-09.json")
    assert requests[0]["receipt"]["status"] == "protective_order_missing_closed"
    assert requests[0]["broker_response"]["protective_status"] == "failed"
    assert len(requests[0]["broker_response"]["protective_errors"]) == 2
    assert requests[0]["broker_response"]["local_mirror"]["mirrored"] is True
    assert requests[0]["broker_response"]["emergency_close"]["status"] == "closed"
    assert requests[0]["broker_response"]["emergency_close"]["local_mirror"]["closed"] is True
    lifecycle = load_json(tmp_path / "outputs" / "order_lifecycle" / "2026-06-09.json")[0]
    assert lifecycle["state"] == "closed"
    assert lifecycle["blocked"] is False
    assert lifecycle["blocker"] == {}
    assert lifecycle["resolved_blockers"][0]["blocker"]["source"] == "binance_demo_protective_orders"
    blocks = load_json(tmp_path / "outputs" / "paper_execution_blocks" / "2026-06-09.json")
    assert blocks[0]["source"] == "binance_demo_protective_orders"
    position = json.loads((tmp_path / "outputs" / "paper_positions" / "current.json").read_text())
    assert position == {}
    assert load_json(tmp_path / "outputs" / "paper_trades" / "current.json") == []
    closed = load_json(tmp_path / "outputs" / "paper_trades" / "closed" / "2026-06-09.json")
    assert closed[0]["exit_reason"] == "protective_order_missing_emergency_close"
    assert "exchange_emergency_close" in closed[0]["quality_flags"]


def test_binance_demo_adapter_keeps_blocker_when_protective_and_emergency_close_fail(tmp_path: Path, monkeypatch):
    posted = []

    def opener(request, timeout):
        if "/fapi/v1/exchangeInfo" in request.full_url:
            return _FakeResponse(_exchange_info())
        if "/fapi/v2/positionRisk" in request.full_url:
            return _FakeResponse(_flat_position())
        if "/fapi/v2/balance" in request.full_url:
            return _FakeResponse(_balance())
        if "/fapi/v1/openOrders" in request.full_url:
            return _FakeResponse([])
        if request.full_url.endswith("/fapi/v1/order"):
            body = urllib.parse.parse_qs(request.data.decode("utf-8"))
            posted.append(body)
            if body["type"][0] != "MARKET" or body.get("reduceOnly", ["false"])[0] == "true":
                raise OSError("order post failed")
            return _FakeResponse(
                {
                    "orderId": 2001,
                    "symbol": "XAUUSDT",
                    "clientOrderId": body["newClientOrderId"][0],
                    "side": body["side"][0],
                    "type": body["type"][0],
                    "origQty": body.get("quantity", ["0"])[0],
                    "executedQty": body.get("quantity", ["0"])[0],
                    "avgPrice": "4525.75",
                }
            )
        raise AssertionError(request.full_url)

    adapter = _adapter(tmp_path, monkeypatch, opener)
    order = adapter.submit_order(BrokerOrderRequest("2026-06-09", _ticket(), latest_price=4525.5, actual_size=0.002))

    assert order.status == "protective_order_missing"
    assert len(posted) == 4
    requests = load_json(tmp_path / "outputs" / "demo_order_requests" / "2026-06-09.json")
    assert requests[0]["broker_response"]["emergency_close"]["status"] == "failed"
    lifecycle = load_json(tmp_path / "outputs" / "order_lifecycle" / "2026-06-09.json")[0]
    assert lifecycle["state"] == "protective_failed"
    assert lifecycle["blocker"]["source"] == "binance_demo_protective_orders"
    position = json.loads((tmp_path / "outputs" / "paper_positions" / "current.json").read_text())["GOLD"]
    assert position["side"] == "long"
    assert position["quantity"] == 0.002


def test_binance_demo_adapter_blocks_on_reconciliation_drift(tmp_path: Path, monkeypatch):
    def opener(request, timeout):
        if "/fapi/v1/exchangeInfo" in request.full_url:
            return _FakeResponse(_exchange_info())
        if "/fapi/v2/positionRisk" in request.full_url:
            return _FakeResponse(_short_position())
        if "/fapi/v2/balance" in request.full_url:
            return _FakeResponse(_balance())
        if "/fapi/v1/openOrders" in request.full_url:
            return _FakeResponse([])
        if request.full_url.endswith("/fapi/v1/order"):
            raise AssertionError("drifted demo account must not submit a new order")
        raise AssertionError(request.full_url)

    adapter = _adapter(tmp_path, monkeypatch, opener)
    with pytest.raises(RuntimeError, match="reconciliation drift"):
        adapter.submit_order(BrokerOrderRequest("2026-06-09", _ticket(), latest_price=4525.5, actual_size=0.002))

    report = load_json(tmp_path / "outputs" / "live_reconciliation" / "current.json")[0]
    assert report["reconciled"] is False
    assert report["drift_count"] == 1
    assert report["drifts"][0]["reason"] == "exchange position has no local record"
    block = load_json(tmp_path / "outputs" / "demo_order_requests" / "2026-06-09.json")[0]
    assert block["status"] == "blocked"
    assert "reconciliation drift" in block["guard"]["block_reason"]


def test_binance_demo_close_position_defaults_to_dry_run(tmp_path: Path, monkeypatch):
    posted = []

    def opener(request, timeout):
        if "/fapi/v1/exchangeInfo" in request.full_url:
            return _FakeResponse(_exchange_info())
        if "/fapi/v2/positionRisk" in request.full_url:
            return _FakeResponse(_short_position())
        if request.full_url.endswith("/fapi/v1/order"):
            posted.append(request)
            raise AssertionError("dry-run close must not submit an order")
        raise AssertionError(request.full_url)

    adapter = _adapter(tmp_path, monkeypatch, opener)
    report = adapter.close_demo_position("2026-06-09")

    assert report["status"] == "dry_run"
    assert report["network_order_created"] is False
    assert report["position"]["positionAmt"] == "-0.002"
    assert report["close_request"]["side"] == "BUY"
    assert report["close_request"]["quantity"] == "0.002"
    assert posted == []
    saved = load_json(tmp_path / "outputs" / "demo_position_closes" / "2026-06-09.json")[0]
    assert saved["status"] == "dry_run"


def test_binance_demo_close_position_confirm_submits_reduce_only_and_reconciles(tmp_path: Path, monkeypatch):
    posted = []
    position_reads = {"count": 0}

    def opener(request, timeout):
        if "/fapi/v1/exchangeInfo" in request.full_url:
            return _FakeResponse(_exchange_info())
        if "/fapi/v2/positionRisk" in request.full_url:
            position_reads["count"] += 1
            return _FakeResponse(_short_position() if position_reads["count"] == 1 else _flat_position())
        if "/fapi/v2/balance" in request.full_url:
            return _FakeResponse(_balance())
        if "/fapi/v1/openOrders" in request.full_url:
            return _FakeResponse([])
        if request.full_url.endswith("/fapi/v1/allOpenOrders"):
            return _FakeResponse({"code": 200, "msg": "success"})
        if request.full_url.endswith("/fapi/v1/order"):
            body = urllib.parse.parse_qs(request.data.decode("utf-8"))
            posted.append(body)
            return _FakeResponse(
                {
                    "orderId": 3001,
                    "symbol": "XAUUSDT",
                    "clientOrderId": body["newClientOrderId"][0],
                    "side": body["side"][0],
                    "type": body["type"][0],
                    "origQty": body.get("quantity", ["0"])[0],
                    "executedQty": body.get("quantity", ["0"])[0],
                    "avgPrice": "4230.0",
                }
            )
        raise AssertionError(request.full_url)

    adapter = _adapter(tmp_path, monkeypatch, opener)
    report = adapter.close_demo_position("2026-06-09", confirm_close_demo_position=True)

    assert report["status"] == "submitted"
    assert report["network_order_created"] is True
    assert len(posted) == 1
    assert posted[0]["side"] == ["BUY"]
    assert posted[0]["type"] == ["MARKET"]
    assert posted[0]["reduceOnly"] == ["true"]
    assert report["protective_cancel"]["status"] == "cancelled"
    assert report["reconciliation_after"]["reconciled"] is True
