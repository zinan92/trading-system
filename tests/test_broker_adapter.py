from pathlib import Path
import json
import urllib.parse
from datetime import datetime, timezone

import pytest

from services.broker_adapter import BrokerOrderRequest, LiveBrokerAdapter, PaperBrokerAdapter, broker_preflight, build_broker_adapter
from services.journal_store import load_json, write_json


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def _ticket() -> dict:
    return {
        "ticket_id": "ticket_gold_20260512_adapter",
        "signal_id": "sig_gold_adapter",
        "asset": "GOLD",
        "asset_class": "commodity",
        "action": "prepare_buy",
        "entry_zone": "4550-4590",
        "stop_loss": 4480,
        "targets": [4750],
        "position_size_pct": 8,
        "max_loss_pct": 0.5,
        "order_type": "limit",
        "time_in_force": "day",
        "paper_only": True,
    }


def _posted_order_response(body: dict, order_id: int, status: str, *, avg_price: str = "0") -> dict:
    quantity = body.get("quantity", ["0"])[0]
    response = {
        "orderId": order_id,
        "symbol": body.get("symbol", ["XAUUSDT"])[0],
        "clientOrderId": body.get("newClientOrderId", ["client"])[0],
        "side": body.get("side", ["BUY"])[0],
        "type": body.get("type", ["MARKET"])[0],
        "origQty": quantity,
        "executedQty": quantity,
        "avgPrice": avg_price,
        "status": status,
    }
    if "reduceOnly" in body:
        response["reduceOnly"] = body["reduceOnly"][0]
    return response


def test_paper_broker_adapter_submits_local_order(tmp_path: Path):
    root = tmp_path / "outputs"
    order = PaperBrokerAdapter(root).submit_order(BrokerOrderRequest("2026-05-12", _ticket(), latest_price=4570.0))

    assert order.status == "filled"
    assert load_json(root / "paper_orders" / "2026-05-12.json")[0]["order_id"] == order.order_id


def test_live_broker_adapter_is_guarded_by_default():
    with pytest.raises(RuntimeError, match="live trading is disabled"):
        LiveBrokerAdapter(Path("outputs"), False).submit_order(BrokerOrderRequest("2026-05-12", _ticket(), latest_price=4570.0))


def test_live_broker_adapter_records_dry_run_request(tmp_path: Path):
    root = tmp_path / "outputs"
    adapter = LiveBrokerAdapter(
        root,
        True,
        {
            "provider": "manual_gateway",
            "dry_run": True,
            "request_dir": "live_order_requests",
            "allowed_symbols": ["GOLD"],
        },
    )

    order = adapter.submit_order(BrokerOrderRequest("2026-05-12", _ticket(), latest_price=4570.0))
    requests = load_json(root / "live_order_requests" / "2026-05-12.json")

    assert order.status == "dry_run"
    assert requests[0]["receipt"]["order_id"] == order.order_id
    assert requests[0]["readiness"]["dry_run"] is True


def test_mt5_file_bridge_records_order_outbox_file(tmp_path: Path):
    root = tmp_path / "outputs"
    outbox = tmp_path / "mt5_outbox"
    inbox = tmp_path / "mt5_inbox"
    adapter = LiveBrokerAdapter(
        root,
        True,
        {
            "provider": "mt5_file_bridge",
            "dry_run": True,
            "request_dir": "live_order_requests",
            "allowed_symbols": ["GOLD"],
            "outbox_dir": str(outbox),
            "inbox_dir": str(inbox),
        },
    )

    order = adapter.submit_order(BrokerOrderRequest("2026-05-12", _ticket(), latest_price=4570.0))
    requests = load_json(root / "live_order_requests" / "2026-05-12.json")

    assert order.status == "bridge_dry_run"
    assert Path(requests[0]["outbox_file"]).exists()
    bridge_payload = load_json(Path(requests[0]["outbox_file"]))
    assert bridge_payload["order_id"] == order.order_id
    assert bridge_payload["dry_run"] is True
    readiness = requests[0]["readiness"]
    for key in ["outbox_readme", "inbox_readme", "request_template", "receipt_template"]:
        assert Path(readiness[key]).exists()
    assert "dry_run: true" in Path(readiness["outbox_readme"]).read_text(encoding="utf-8")
    assert "ORDER_RECEIPT.json.template" in Path(readiness["inbox_readme"]).read_text(encoding="utf-8")


def test_mt5_file_bridge_can_submit_to_bridge_when_not_dry_run(tmp_path: Path):
    root = tmp_path / "outputs"
    outbox = tmp_path / "mt5_outbox"
    inbox = tmp_path / "mt5_inbox"
    write_json(root / "live_activation" / "2026-05-12.json", [{"status": "real_money_ready", "real_money_ready": True, "approval": {"approved": True}}])
    adapter = LiveBrokerAdapter(
        root,
        True,
        {
            "provider": "mt5_file_bridge",
            "dry_run": False,
            "request_dir": "live_order_requests",
            "allowed_symbols": ["GOLD"],
            "outbox_dir": str(outbox),
            "inbox_dir": str(inbox),
        },
    )

    with pytest.raises(RuntimeError, match="source-bound Live activation/canary"):
        adapter.submit_order(BrokerOrderRequest("2026-05-12", _ticket(), latest_price=4570.0))
    assert not (outbox / "ORDER_REQUEST.json").exists()


def test_oanda_rest_records_dry_run_request_without_credentials(tmp_path: Path, monkeypatch):
    root = tmp_path / "outputs"
    monkeypatch.delenv("OANDA_API_TOKEN", raising=False)
    monkeypatch.delenv("OANDA_ACCOUNT_ID", raising=False)
    adapter = LiveBrokerAdapter(
        root,
        True,
        {
            "provider": "oanda_rest",
            "dry_run": True,
            "request_dir": "live_order_requests",
            "allowed_symbols": ["GOLD"],
        },
    )

    order = adapter.submit_order(BrokerOrderRequest("2026-05-12", _ticket(), latest_price=4570.0, actual_size=0.25))
    requests = load_json(root / "live_order_requests" / "2026-05-12.json")

    assert order.status == "oanda_dry_run"
    assert requests[0]["request"]["order"]["instrument"] == "XAU_USD"
    assert requests[0]["request"]["order"]["type"] == "LIMIT"
    assert requests[0]["request"]["order"]["units"] == "0.25"
    assert requests[0]["request"]["order"]["timeInForce"] == "GFD"
    assert requests[0]["request"]["order"]["stopLossOnFill"]["price"] == "4480"
    assert requests[0]["readiness"]["missing_env"] == ["OANDA_API_TOKEN", "OANDA_ACCOUNT_ID"]


def test_oanda_rest_blocks_real_submit_when_credentials_missing(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("OANDA_API_TOKEN", raising=False)
    monkeypatch.delenv("OANDA_ACCOUNT_ID", raising=False)
    adapter = LiveBrokerAdapter(
        tmp_path / "outputs",
        True,
        {
            "provider": "oanda_rest",
            "dry_run": False,
            "allowed_symbols": ["GOLD"],
        },
    )

    with pytest.raises(RuntimeError, match="missing OANDA environment variables"):
        adapter.submit_order(BrokerOrderRequest("2026-05-12", _ticket(), latest_price=4570.0, actual_size=0.25))


def test_tiger_openapi_preflight_checks_owner_only_props(tmp_path: Path, monkeypatch):
    props = tmp_path / "tiger_openapi_config.properties"
    props.write_text("tiger_id=placeholder\n", encoding="utf-8")
    props.chmod(0o600)
    monkeypatch.setenv("TIGER_OPENAPI_CONFIG_PATH", str(props))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(tmp_path / "missing-live.env"))
    adapter = LiveBrokerAdapter(
        tmp_path / "outputs",
        True,
        {
            "provider": "tiger_openapi",
            "environment": "paper",
            "dry_run": True,
            "props_path_env": "TIGER_OPENAPI_CONFIG_PATH",
            "allowed_symbols": ["MGC2608"],
            "contract_map": {"MGC2608": "MGC2608"},
        },
    )

    readiness = adapter.preflight()

    assert readiness["provider"] == "tiger_openapi"
    assert readiness["ready"] is True
    assert readiness["props_path_owner_only"] is True
    assert readiness["network_order_submission"] == "not_implemented"


def test_tiger_openapi_dry_run_records_local_request_only(tmp_path: Path, monkeypatch):
    props = tmp_path / "tiger_openapi_config.properties"
    props.write_text("tiger_id=placeholder\n", encoding="utf-8")
    props.chmod(0o600)
    monkeypatch.setenv("TIGER_OPENAPI_CONFIG_PATH", str(props))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(tmp_path / "missing-live.env"))
    root = tmp_path / "outputs"
    ticket = {**_ticket(), "asset": "MGC2608", "entry_zone": "4180-4190"}
    adapter = LiveBrokerAdapter(
        root,
        True,
        {
            "provider": "tiger_openapi",
            "environment": "paper",
            "dry_run": True,
            "request_dir": "tiger_order_requests",
            "props_path_env": "TIGER_OPENAPI_CONFIG_PATH",
            "allowed_symbols": ["MGC2608"],
            "contract_map": {"MGC2608": "MGC2608"},
        },
    )

    order = adapter.submit_order(BrokerOrderRequest("2026-07-05", ticket, latest_price=4186.0, actual_size=1))
    requests = load_json(root / "tiger_order_requests" / "2026-07-05.json")

    assert order.status == "dry_run"
    assert requests[0]["provider"] == "tiger_openapi"
    assert requests[0]["readiness"]["network_order_submission"] == "not_implemented"
    assert requests[0]["request"]["quantity"] == 1
    assert requests[0]["request"]["submission_intent"] == "tiger_tradeclient_future_order"
    assert requests[0]["request"]["network_order_created"] is False
    assert requests[0]["request"]["environment"] == "paper"
    assert requests[0]["request"]["contract"] == "MGC2608"
    assert requests[0]["request"]["sec_type"] == "FUT"
    assert requests[0]["request"]["exchange"] == "COMEX"
    assert requests[0]["request"]["side"] == "BUY"
    assert requests[0]["request"]["order_type"] == "LIMIT"
    assert requests[0]["request"]["limit_price"] == 4186.0


def test_tiger_openapi_dry_run_requires_whole_contracts(tmp_path: Path, monkeypatch):
    props = tmp_path / "tiger_openapi_config.properties"
    props.write_text("tiger_id=placeholder\n", encoding="utf-8")
    props.chmod(0o600)
    monkeypatch.setenv("TIGER_OPENAPI_CONFIG_PATH", str(props))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(tmp_path / "missing-live.env"))
    adapter = LiveBrokerAdapter(
        tmp_path / "outputs",
        True,
        {
            "provider": "tiger_openapi",
            "environment": "paper",
            "dry_run": True,
            "props_path_env": "TIGER_OPENAPI_CONFIG_PATH",
            "allowed_symbols": ["MGC2608"],
            "contract_map": {"MGC2608": "MGC2608"},
        },
    )

    with pytest.raises(RuntimeError, match="positive whole-contract integer"):
        adapter.submit_order(BrokerOrderRequest("2026-07-05", {**_ticket(), "asset": "MGC2608"}, latest_price=4186.0, actual_size=0.5))


def test_tiger_openapi_non_dry_run_is_fail_closed(tmp_path: Path, monkeypatch):
    props = tmp_path / "tiger_openapi_config.properties"
    props.write_text("tiger_id=placeholder\n", encoding="utf-8")
    props.chmod(0o600)
    monkeypatch.setenv("TIGER_OPENAPI_CONFIG_PATH", str(props))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(tmp_path / "missing-live.env"))
    adapter = LiveBrokerAdapter(
        tmp_path / "outputs",
        True,
        {
            "provider": "tiger_openapi",
            "environment": "paper",
            "dry_run": False,
            "props_path_env": "TIGER_OPENAPI_CONFIG_PATH",
            "allowed_symbols": ["MGC2608"],
            "contract_map": {"MGC2608": "MGC2608"},
        },
    )

    with pytest.raises(RuntimeError, match="Tiger OpenAPI network order submission is not implemented"):
        adapter.submit_order(BrokerOrderRequest("2026-07-05", {**_ticket(), "asset": "MGC2608"}, latest_price=4186.0, actual_size=1))


def test_oanda_rest_preflight_treats_placeholder_credentials_as_missing(tmp_path: Path, monkeypatch):
    env = tmp_path / "live.env"
    env.write_text("OANDA_API_TOKEN=CHANGE_ME\nOANDA_ACCOUNT_ID=your_account_id\n", encoding="utf-8")
    monkeypatch.setenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(env))
    monkeypatch.delenv("OANDA_API_TOKEN", raising=False)
    monkeypatch.delenv("OANDA_ACCOUNT_ID", raising=False)
    adapter = LiveBrokerAdapter(
        tmp_path / "outputs",
        True,
        {
            "provider": "oanda_rest",
            "dry_run": False,
            "allowed_symbols": ["GOLD"],
        },
    )

    readiness = adapter.preflight()

    assert readiness["ready"] is False
    assert readiness["missing_env"] == ["OANDA_API_TOKEN", "OANDA_ACCOUNT_ID"]
    assert readiness["account_id_present"] is False


def test_oanda_rest_blocks_real_submit_without_live_activation(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OANDA_API_TOKEN", "token")
    monkeypatch.setenv("OANDA_ACCOUNT_ID", "acct")
    adapter = LiveBrokerAdapter(
        tmp_path / "outputs",
        True,
        {
            "provider": "oanda_rest",
            "dry_run": False,
            "allowed_symbols": ["GOLD"],
        },
    )

    with pytest.raises(RuntimeError, match="source-bound Live activation/canary"):
        adapter.submit_order(BrokerOrderRequest("2026-05-12", _ticket(), latest_price=4570.0, actual_size=0.25))


def test_oanda_rest_posts_real_order_when_enabled_with_credentials(tmp_path: Path, monkeypatch):
    root = tmp_path / "outputs"
    monkeypatch.setenv("OANDA_API_TOKEN", "token")
    monkeypatch.setenv("OANDA_ACCOUNT_ID", "acct")
    write_json(root / "live_activation" / "2026-05-12.json", [{"status": "real_money_ready", "real_money_ready": True, "approval": {"approved": True}}])
    seen = {}

    def opener(request, timeout):
        seen["url"] = request.full_url
        seen["method"] = request.get_method()
        seen["auth"] = request.headers["Authorization"]
        seen["body"] = json.loads(request.data.decode("utf-8"))
        seen["timeout"] = timeout
        return _FakeResponse(
            {
                "orderCreateTransaction": {"id": "101", "time": "2026-05-12T01:00:00Z"},
                "orderFillTransaction": {"id": "102", "price": "4570.2", "time": "2026-05-12T01:00:01Z"},
                "lastTransactionID": "102",
            }
        )

    ticket = {**_ticket(), "order_type": "market", "time_in_force": "ioc"}
    adapter = LiveBrokerAdapter(
        root,
        True,
        {
            "provider": "oanda_rest",
            "dry_run": False,
            "request_dir": "live_order_requests",
            "allowed_symbols": ["GOLD"],
        },
        opener=opener,
    )

    with pytest.raises(RuntimeError, match="source-bound Live activation/canary"):
        adapter.submit_order(BrokerOrderRequest("2026-05-12", ticket, latest_price=4570.0, actual_size=0.25))
    assert seen == {}


def test_binance_usdm_records_dry_run_request_without_credentials(tmp_path: Path, monkeypatch):
    root = tmp_path / "outputs"
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    monkeypatch.setenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(tmp_path / "absent.env"))

    def opener(request, timeout):
        assert request.full_url.startswith("https://fapi.binance.com/fapi/v1/exchangeInfo")
        return _FakeResponse(
            {
                "symbols": [
                    {
                        "symbol": "XAUUSDT",
                        "status": "TRADING",
                        "contractType": "TRADIFI_PERPETUAL",
                        "underlyingType": "COMMODITY",
                        "marginAsset": "USDT",
                        "filters": [
                            {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                            {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                            {"filterType": "MIN_NOTIONAL", "notional": "5"},
                        ],
                    }
                ]
            }
        )

    adapter = LiveBrokerAdapter(
        root,
        True,
        {
            "provider": "binance_usdm",
            "dry_run": True,
            "request_dir": "live_order_requests",
            "allowed_symbols": ["GOLD"],
            "instrument_map": {"GOLD": "XAUUSDT"},
        },
        opener=opener,
    )

    ticket = {**_ticket(), "order_type": "market"}
    order = adapter.submit_order(BrokerOrderRequest("2026-05-12", ticket, latest_price=4570.0, actual_size=0.2559))
    requests = load_json(root / "live_order_requests" / "2026-05-12.json")

    assert order.status == "binance_dry_run"
    assert order.quantity == 0.255
    assert requests[0]["request"]["entry"]["symbol"] == "XAUUSDT"
    assert requests[0]["request"]["entry"]["type"] == "MARKET"
    assert requests[0]["request"]["entry"]["quantity"] == "0.255"
    assert requests[0]["request"]["protective_orders"][0]["type"] == "STOP_MARKET"
    assert requests[0]["readiness"]["missing_env"] == ["BINANCE_API_KEY", "BINANCE_API_SECRET"]


def test_binance_usdm_blocks_real_submit_without_credentials(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    monkeypatch.setenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(tmp_path / "absent.env"))

    def opener(request, timeout):
        return _FakeResponse({"symbols": [{"symbol": "XAUUSDT", "status": "TRADING", "filters": []}]})

    adapter = LiveBrokerAdapter(
        tmp_path / "outputs",
        True,
        {
            "provider": "binance_usdm",
            "dry_run": False,
            "allowed_symbols": ["GOLD"],
            "instrument_map": {"GOLD": "XAUUSDT"},
        },
        opener=opener,
    )

    with pytest.raises(RuntimeError, match="missing Binance environment variables"):
        adapter.submit_order(BrokerOrderRequest("2026-05-12", {**_ticket(), "order_type": "market"}, latest_price=4570.0, actual_size=0.25))


def test_binance_usdm_posts_real_market_order_when_all_gates_pass(tmp_path: Path, monkeypatch):
    root = tmp_path / "outputs"
    monkeypatch.setenv("BINANCE_API_KEY", "key")
    monkeypatch.setenv("BINANCE_API_SECRET", "secret")
    write_json(root / "live_activation" / "2026-05-12.json", [{"status": "real_money_ready", "real_money_ready": True, "approval": {"approved": True}}])
    write_json(
        root / "live_reconciliation" / "2026-05-12.json",
        [
            {
                "run_date": "2026-05-12",
                "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "error": "",
                "confirmation_status": "confirmed_flat",
                "exchange_balance": {"asset": "USDT", "balance": 5000.0, "available": 4990.0, "balance_present": True},
                "account_observation": {
                    "account_observed": True,
                    "balance_present": True,
                    "history_requested": True,
                    "fills_observed": True,
                    "income_observed": True,
                    "fills_count": 0,
                    "income_count": 0,
                    "symbol": "XAUUSDT",
                    "reason": "test account observation",
                },
                "exchange_positions": [],
                "exchange_accounting": {
                    "net_realized_pnl_estimate": 0.0,
                    "utc_trading_day": {"run_date": "2026-05-12", "start_time_ms": 1778544000000, "end_time_ms": 1778630399999},
                },
            }
        ],
    )
    seen = []

    def opener(request, timeout):
        url = request.full_url
        seen.append({"url": url, "method": request.get_method(), "body": request.data.decode("utf-8") if request.data else ""})
        if url.startswith("https://fapi.binance.com/fapi/v1/exchangeInfo"):
            return _FakeResponse(
                {
                    "symbols": [
                        {
                            "symbol": "XAUUSDT",
                            "status": "TRADING",
                            "filters": [
                                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                                {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                                {"filterType": "MIN_NOTIONAL", "notional": "5"},
                            ],
                        }
                    ]
                }
            )
        # inline reconciliation refresh endpoints (adapter refreshes before ordering)
        if "/fapi/v2/positionRisk" in url:
            return _FakeResponse([{"symbol": "XAUUSDT", "positionAmt": "0.000", "entryPrice": "0.0", "unRealizedProfit": "0.0"}])
        if "/fapi/v2/balance" in url:
            return _FakeResponse([{"asset": "USDT", "balance": "5000.0", "availableBalance": "4990.0"}])
        if "/fapi/v1/openOrders" in url or "/fapi/v1/openAlgoOrders" in url:
            return _FakeResponse([])
        if "/fapi/v1/userTrades" in url or "/fapi/v1/income" in url:
            return _FakeResponse([])
        body = urllib.parse.parse_qs(request.data.decode("utf-8")) if request.data else {}
        is_protective = body.get("reduceOnly", ["false"])[0] == "true"
        return _FakeResponse(_posted_order_response(body, 42, "NEW" if is_protective else "FILLED", avg_price="0" if is_protective else "4570.25"))

    adapter = LiveBrokerAdapter(
        root,
        True,
        {
            "provider": "binance_usdm",
            "dry_run": False,
            "request_dir": "live_order_requests",
            "allowed_symbols": ["GOLD"],
            "instrument_map": {"GOLD": "XAUUSDT"},
            "reconcile_account_history": True,
        },
        opener=opener,
    )

    with pytest.raises(RuntimeError, match="source-bound Live activation/canary"):
        adapter.submit_order(BrokerOrderRequest("2026-05-12", {**_ticket(), "order_type": "market"}, latest_price=4570.0, actual_size=0.002))
    assert not [item for item in seen if item["method"] == "POST"]


def test_broker_preflight_writes_paper_mode_status(tmp_path: Path):
    result = broker_preflight(tmp_path / "outputs")

    assert result["mode"] == "paper"
    assert result["ready"] is True
    assert load_json(tmp_path / "outputs" / "broker_preflight" / "current.json")[0]["mode"] == "paper"


def test_build_broker_adapter_defaults_to_paper(tmp_path: Path):
    assert build_broker_adapter(tmp_path / "outputs").name == "paper"


def _mainnet_recon_opener(seen: list, position_amt: str = "0.000"):
    """Fake mainnet opener serving preflight + reconciliation + order endpoints."""

    def opener(request, timeout):
        url = request.full_url
        seen.append({"url": url, "method": request.get_method(), "body": request.data.decode("utf-8") if request.data else ""})
        if "/fapi/v1/exchangeInfo" in url:
            return _FakeResponse(
                {
                    "symbols": [
                        {
                            "symbol": "XAUUSDT",
                            "status": "TRADING",
                            "filters": [
                                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                                {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                                {"filterType": "MIN_NOTIONAL", "notional": "5"},
                            ],
                        }
                    ]
                }
            )
        if "/fapi/v2/positionRisk" in url:
            return _FakeResponse([{"symbol": "XAUUSDT", "positionAmt": position_amt, "entryPrice": "0.0", "unRealizedProfit": "0.0"}])
        if "/fapi/v2/balance" in url:
            return _FakeResponse([{"asset": "USDT", "balance": "5000.0", "availableBalance": "4990.0"}])
        if "/fapi/v1/openOrders" in url or "/fapi/v1/openAlgoOrders" in url:
            return _FakeResponse([])
        if "/fapi/v1/userTrades" in url or "/fapi/v1/income" in url:
            return _FakeResponse([])
        body = urllib.parse.parse_qs(request.data.decode("utf-8")) if request.data else {}
        is_protective = body.get("reduceOnly", ["false"])[0] == "true"
        return _FakeResponse(_posted_order_response(body, 77, "NEW" if is_protective else "FILLED", avg_price="0" if is_protective else "4570.25"))

    return opener


def _mainnet_adapter(root: Path, opener) -> LiveBrokerAdapter:
    return LiveBrokerAdapter(
        root,
        True,
        {
            "provider": "binance_usdm",
            "environment": "live",
            "base_url": "https://fapi.binance.com",
            "dry_run": False,
            "request_dir": "live_order_requests",
            "allowed_symbols": ["GOLD"],
            "instrument_map": {"GOLD": "XAUUSDT"},
            "reconcile_account_history": True,
        },
        opener=opener,
    )


def test_binance_usdm_refreshes_reconciliation_inline_before_real_order(tmp_path: Path, monkeypatch):
    """No pre-seeded snapshot: the live path must refresh reconciliation itself,
    so money guardrails see a fresh same-day snapshot instead of failing UNKNOWN."""
    from services.run_date import utc_run_date

    root = tmp_path / "outputs"
    run_date = utc_run_date()
    monkeypatch.setenv("BINANCE_API_KEY", "key")
    monkeypatch.setenv("BINANCE_API_SECRET", "secret")
    write_json(root / "live_activation" / f"{run_date}.json", [{"status": "real_money_ready", "real_money_ready": True, "approval": {"approved": True}}])
    seen: list = []
    adapter = _mainnet_adapter(root, _mainnet_recon_opener(seen, position_amt="0.000"))

    with pytest.raises(RuntimeError, match="source-bound Live activation/canary"):
        adapter.submit_order(BrokerOrderRequest(run_date, {**_ticket(), "order_type": "market"}, latest_price=4570.0, actual_size=0.002))
    assert not [item for item in seen if item["method"] == "POST"]


def test_binance_usdm_blocks_real_order_on_reconciliation_drift(tmp_path: Path, monkeypatch):
    """Venue holds a position the local book does not know: even with a fresh-looking
    seeded snapshot, the inline refresh must detect drift and refuse to POST."""
    from services.run_date import utc_run_date

    root = tmp_path / "outputs"
    run_date = utc_run_date()
    monkeypatch.setenv("BINANCE_API_KEY", "key")
    monkeypatch.setenv("BINANCE_API_SECRET", "secret")
    write_json(root / "live_activation" / f"{run_date}.json", [{"status": "real_money_ready", "real_money_ready": True, "approval": {"approved": True}}])
    # a stale-but-plausible snapshot claims flat; the venue actually holds 0.5
    write_json(
        root / "live_reconciliation" / f"{run_date}.json",
        [
            {
                "run_date": run_date,
                "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "error": "",
                "confirmation_status": "confirmed_flat",
                "exchange_balance": {"asset": "USDT", "balance": 5000.0, "available": 4990.0, "balance_present": True},
                "account_observation": {"account_observed": True, "balance_present": True, "history_requested": True, "fills_observed": True, "income_observed": True, "fills_count": 0, "income_count": 0, "symbol": "XAUUSDT", "reason": "seeded"},
                "exchange_positions": [],
                "exchange_accounting": {"net_realized_pnl_estimate": 0.0, "utc_trading_day": {"run_date": run_date}},
            }
        ],
    )
    seen: list = []
    adapter = _mainnet_adapter(root, _mainnet_recon_opener(seen, position_amt="0.500"))

    with pytest.raises(RuntimeError, match="source-bound Live activation/canary"):
        adapter.submit_order(BrokerOrderRequest(run_date, {**_ticket(), "order_type": "market"}, latest_price=4570.0, actual_size=0.002))

    assert not [item for item in seen if item["method"] == "POST"]
