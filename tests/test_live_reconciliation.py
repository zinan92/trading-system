from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

from services.live_reconciliation import LiveBrokerReconciliation
from services.journal_store import load_json
from services.live_money_guardrails import LiveMoneyGuardrails
from services.order_lifecycle import OrderLifecycleStore


class _FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def _protective_order(qty: float = 0.05, *, client_id: str = "known_sl", side: str = "SELL") -> dict:
    return {
        "symbol": "XAUUSDT",
        "orderId": 9001,
        "clientOrderId": client_id,
        "type": "STOP_MARKET",
        "side": side,
        "origQty": str(qty),
        "reduceOnly": "true",
        "status": "NEW",
    }


def _seed_known_protective(root: Path, run_date: str = "2026-06-03", client_id: str = "known_sl") -> None:
    path = root / "live_order_requests" / f"{run_date}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            [
                {
                    "order_id": "order_live",
                    "request": {"protective_orders": [{"newClientOrderId": client_id}]},
                    "broker_response": {"protective_orders": [{"clientOrderId": client_id}]},
                }
            ]
        ),
        encoding="utf-8",
    )


def test_known_protective_ids_ignore_request_only_payloads(tmp_path):
    root = tmp_path / "outputs"
    path = root / "live_order_requests" / "2026-06-03.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            [
                {
                    "order_id": "request_only",
                    "request": {"protective_orders": [{"newClientOrderId": "request_only_sl"}, {"clientAlgoId": "request_only_tp"}]},
                    "broker_response": {"protective_orders": []},
                },
                {
                    "order_id": "response_confirmed",
                    "request": {"protective_orders": [{"newClientOrderId": "confirmed_request_sl"}]},
                    "broker_response": {
                        "protective_orders": [
                            {"clientOrderId": "confirmed_response_sl"},
                            {"clientAlgoId": "confirmed_algo_tp"},
                            {"client_order_id": "confirmed_normalized_id"},
                        ]
                    },
                },
            ]
        ),
        encoding="utf-8",
    )

    ids = LiveBrokerReconciliation(root, _CFG)._known_protective_order_ids()

    assert "request_only_sl" not in ids
    assert "request_only_tp" not in ids
    assert "confirmed_request_sl" not in ids
    assert {"confirmed_response_sl", "confirmed_algo_tp", "confirmed_normalized_id"}.issubset(ids)


def _opener(position_amt: float, open_orders: list[dict] | None = None):
    def opener(request, timeout):
        url = request.full_url
        # signed GET must carry signature + the api key header
        assert "signature=" in url
        assert request.get_header("X-mbx-apikey")
        if "/fapi/v2/positionRisk" in url:
            return _FakeResponse([
                {"symbol": "XAUUSDT", "positionAmt": str(position_amt), "entryPrice": "4470.0", "unRealizedProfit": "1.5"},
                {"symbol": "BTCUSDT", "positionAmt": "0", "entryPrice": "0", "unRealizedProfit": "0"},
            ])
        if "/fapi/v2/balance" in url:
            return _FakeResponse([
                {"asset": "USDT", "balance": "100.0", "availableBalance": "95.0"},
                {"asset": "BNB", "balance": "0", "availableBalance": "0"},
            ])
        if "/fapi/v1/openOrders" in url:
            return _FakeResponse(open_orders or [])
        if "/fapi/v1/userTrades" in url:
            return _FakeResponse([])
        if "/fapi/v1/income" in url:
            return _FakeResponse([])
        raise AssertionError(f"unexpected url {url}")

    return opener


_CFG = {
    "provider": "binance_usdm",
    "environment": "testnet",
    "base_url": "https://testnet.binancefuture.com",
    "api_key_env": "BINANCE_API_KEY",
    "api_secret_env": "BINANCE_API_SECRET",
    "instrument_map": {"GOLD": "XAUUSDT"},
}

_RUN_DATE = "2026-06-09"
_DAY_START_MS = 1780963200000
_DAY_MID_MS = 1781006400000
_DAY_END_MS = 1781049599999
_PREVIOUS_DAY_MS = 1780963199000
_NEXT_DAY_MS = 1781049600000


def _query(url: str) -> dict[str, list[str]]:
    return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)


def _creds(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "testkey123")
    monkeypatch.setenv("BINANCE_API_SECRET", "testsecret123")


def _seed_local_position(root: Path, side: str, quantity: float) -> None:
    path = root / "paper_positions" / "current.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"GOLD": {"symbol": "GOLD", "side": side, "quantity": quantity, "avg_price": 4470.0}}), encoding="utf-8")


def test_exchange_accounting_uses_utc_run_date_window_for_daily_loss(tmp_path, monkeypatch):
    _creds(monkeypatch)
    root = tmp_path / "outputs"
    seen_queries = {}

    def opener(request, timeout):
        url = request.full_url
        assert "signature=" in url
        if "/fapi/v2/positionRisk" in url:
            return _FakeResponse([{"symbol": "XAUUSDT", "positionAmt": "0", "entryPrice": "0", "unRealizedProfit": "0"}])
        if "/fapi/v2/balance" in url:
            return _FakeResponse([{"asset": "USDT", "balance": "100.0", "availableBalance": "95.0"}])
        if "/fapi/v1/openOrders" in url:
            return _FakeResponse([])
        if "/fapi/v1/userTrades" in url:
            seen_queries["fills"] = _query(url)
            return _FakeResponse(
                [
                    {"symbol": "XAUUSDT", "id": 1, "orderId": 1001, "side": "SELL", "price": "4470", "qty": "0.01", "quoteQty": "44.7", "commission": "0.1", "commissionAsset": "USDT", "realizedPnl": "-100.0", "time": _PREVIOUS_DAY_MS},
                    {"symbol": "XAUUSDT", "id": 2, "orderId": 1002, "side": "SELL", "price": "4470", "qty": "0.01", "quoteQty": "44.7", "commission": "0.5", "commissionAsset": "USDT", "realizedPnl": "-2.0", "time": _DAY_MID_MS},
                    {"symbol": "XAUUSDT", "id": 3, "orderId": 1003, "side": "SELL", "price": "4470", "qty": "0.01", "quoteQty": "44.7", "commission": "0.1", "commissionAsset": "USDT", "realizedPnl": "-100.0", "time": _NEXT_DAY_MS},
                ]
            )
        if "/fapi/v1/income" in url:
            seen_queries["income"] = _query(url)
            return _FakeResponse(
                [
                    {"symbol": "XAUUSDT", "incomeType": "FUNDING_FEE", "income": "-50.0", "asset": "USDT", "time": _PREVIOUS_DAY_MS},
                    {"symbol": "XAUUSDT", "incomeType": "FUNDING_FEE", "income": "-1.0", "asset": "USDT", "time": _DAY_MID_MS},
                    {"symbol": "XAUUSDT", "incomeType": "FUNDING_FEE", "income": "-50.0", "asset": "USDT", "time": _NEXT_DAY_MS},
                ]
            )
        raise AssertionError(f"unexpected url {url}")

    report = LiveBrokerReconciliation(root, _CFG, opener=opener).run(_RUN_DATE)

    assert seen_queries["fills"]["startTime"] == [str(_DAY_START_MS)]
    assert seen_queries["fills"]["endTime"] == [str(_DAY_END_MS)]
    assert seen_queries["income"]["startTime"] == [str(_DAY_START_MS)]
    assert seen_queries["income"]["endTime"] == [str(_DAY_END_MS)]
    assert report["exchange_accounting"]["utc_trading_day"]["run_date"] == _RUN_DATE
    assert report["exchange_accounting"]["trade_count"] == 1
    assert report["exchange_accounting"]["income_count"] == 1
    assert report["exchange_accounting"]["net_realized_pnl_estimate"] == -3.5
    assert report["accounting_snapshot"]["schema_version"] == "accounting-snapshot-v1"
    assert report["accounting_snapshot"]["counts"]["fill_count"] == 1
    assert report["accounting_snapshot"]["counts"]["trade_count"] is None
    assert report["accounting_snapshot"]["pnl"]["net_realized_pnl"] == -3.5
    assert report["accounting_snapshot"]["reconciliation"]["status"] == "pass"

    guardrail = LiveMoneyGuardrails(root, broker_config={"request_dir": "testnet_order_requests"}).evaluate_order(
        _RUN_DATE,
        ticket={"ticket_id": "ticket_guard", "asset": "GOLD", "action": "prepare_buy"},
        symbol="XAUUSDT",
        side="BUY",
        requested_price=4500.0,
        quantity=0.002,
        source="binance_usdm:testnet",
        reconciliation=report,
    )
    assert guardrail["status"] == "BLOCKED_DAILY_LOSS_LIMIT"
    assert guardrail["daily_loss"]["net_realized_pnl_estimate"] == -3.5


def test_account_history_non_list_response_is_not_observed_and_blocks_daily_loss(tmp_path, monkeypatch):
    _creds(monkeypatch)
    root = tmp_path / "outputs"

    def opener(request, timeout):
        url = request.full_url
        if "/fapi/v2/positionRisk" in url:
            return _FakeResponse([{"symbol": "XAUUSDT", "positionAmt": "0", "entryPrice": "0", "unRealizedProfit": "0"}])
        if "/fapi/v2/balance" in url:
            return _FakeResponse([{"asset": "USDT", "balance": "100.0", "availableBalance": "95.0"}])
        if "/fapi/v1/openOrders" in url:
            return _FakeResponse([])
        if "/fapi/v1/userTrades" in url:
            return _FakeResponse({"code": -1021, "msg": "Timestamp outside recvWindow"})
        if "/fapi/v1/income" in url:
            return _FakeResponse([])
        raise AssertionError(f"unexpected url {url}")

    report = LiveBrokerReconciliation(root, _CFG, opener=opener).run(_RUN_DATE)

    assert report["confirmation_status"] == "cannot_confirm"
    assert report["account_observation"]["account_observed"] is False
    assert report["account_observation"]["balance_present"] is True
    assert "response was not a list" in report["error"]
    assert all(value is None for value in report["accounting_snapshot"]["counts"].values())
    assert report["accounting_snapshot"]["pnl"]["net_realized_pnl"] is None
    assert report["accounting_snapshot"]["reconciliation"]["status"] == "blocked"
    guardrail = LiveMoneyGuardrails(root, broker_config={"request_dir": "testnet_order_requests"}).evaluate_order(
        _RUN_DATE,
        ticket={"ticket_id": "ticket_guard", "asset": "GOLD", "action": "prepare_buy"},
        symbol="XAUUSDT",
        side="BUY",
        requested_price=4500.0,
        quantity=0.002,
        source="binance_usdm:testnet",
        reconciliation=report,
    )
    assert guardrail["status"] == "BLOCKED_MONEY_GUARDRAIL_UNKNOWN"
    assert guardrail["primary_blocker"]["code"] == "daily_loss_unknown"


def test_income_non_list_response_is_not_observed_and_blocks_daily_loss(tmp_path, monkeypatch):
    _creds(monkeypatch)
    root = tmp_path / "outputs"

    def opener(request, timeout):
        url = request.full_url
        if "/fapi/v2/positionRisk" in url:
            return _FakeResponse([{"symbol": "XAUUSDT", "positionAmt": "0", "entryPrice": "0", "unRealizedProfit": "0"}])
        if "/fapi/v2/balance" in url:
            return _FakeResponse([{"asset": "USDT", "balance": "100.0", "availableBalance": "95.0"}])
        if "/fapi/v1/openOrders" in url:
            return _FakeResponse([])
        if "/fapi/v1/userTrades" in url:
            return _FakeResponse([])
        if "/fapi/v1/income" in url:
            return _FakeResponse({"code": -1022, "msg": "Signature invalid"})
        raise AssertionError(f"unexpected url {url}")

    report = LiveBrokerReconciliation(root, _CFG, opener=opener).run(_RUN_DATE)

    assert report["confirmation_status"] == "cannot_confirm"
    assert report["account_observation"]["fills_observed"] is True
    assert report["account_observation"]["income_observed"] is False
    assert report["account_observation"]["account_observed"] is False
    assert "response was not a list" in report["error"]
    guardrail = LiveMoneyGuardrails(root, broker_config={"request_dir": "testnet_order_requests"}).evaluate_order(
        _RUN_DATE,
        ticket={"ticket_id": "ticket_guard", "asset": "GOLD", "action": "prepare_buy"},
        symbol="XAUUSDT",
        side="BUY",
        requested_price=4500.0,
        quantity=0.002,
        source="binance_usdm:testnet",
        reconciliation=report,
    )
    assert guardrail["status"] == "BLOCKED_MONEY_GUARDRAIL_UNKNOWN"


def test_account_level_income_empty_symbol_is_skipped_without_blocking_daily_loss(tmp_path, monkeypatch):
    _creds(monkeypatch)
    root = tmp_path / "outputs"

    def opener(request, timeout):
        url = request.full_url
        if "/fapi/v2/positionRisk" in url:
            return _FakeResponse([{"symbol": "XAUUSDT", "positionAmt": "0", "entryPrice": "0", "unRealizedProfit": "0"}])
        if "/fapi/v2/balance" in url:
            return _FakeResponse([{"asset": "USDT", "balance": "1000.0", "availableBalance": "995.0"}])
        if "/fapi/v1/openOrders" in url:
            return _FakeResponse([])
        if "/fapi/v1/userTrades" in url:
            return _FakeResponse(
                [
                    {"symbol": "XAUUSDT", "id": 1, "orderId": 1001, "side": "SELL", "price": "4470", "qty": "0.01", "quoteQty": "44.7", "commission": "0.5", "commissionAsset": "USDT", "realizedPnl": "-2.0", "time": _DAY_MID_MS}
                ]
            )
        if "/fapi/v1/income" in url:
            return _FakeResponse(
                [
                    {"symbol": "", "incomeType": "TRANSFER", "income": "100.0", "asset": "USDT", "time": _DAY_MID_MS},
                    {"symbol": "XAUUSDT", "incomeType": "FUNDING_FEE", "income": "-1.0", "asset": "USDT", "time": _DAY_MID_MS},
                ]
            )
        raise AssertionError(f"unexpected url {url}")

    report = LiveBrokerReconciliation(root, _CFG, opener=opener).run(_RUN_DATE)

    assert report["error"] == ""
    assert report["account_observation"]["account_observed"] is True
    assert report["exchange_accounting"]["income_count"] == 1
    assert report["exchange_accounting"]["income_by_type"] == {"FUNDING_FEE": -1.0}
    assert report["exchange_accounting"]["net_realized_pnl_estimate"] == -3.5
    guardrail = LiveMoneyGuardrails(root, broker_config={"request_dir": "testnet_order_requests"}).evaluate_order(
        _RUN_DATE,
        ticket={"ticket_id": "ticket_guard", "asset": "GOLD", "action": "prepare_buy"},
        symbol="XAUUSDT",
        side="BUY",
        requested_price=4500.0,
        quantity=0.002,
        source="binance_usdm:testnet",
        reconciliation=report,
    )
    assert guardrail["status"] == "READY"
    assert guardrail["daily_loss"]["known"] is True
    assert guardrail["daily_loss"]["loss_pct"] == 0.35


def test_user_trades_list_with_non_dict_row_is_not_observed(tmp_path, monkeypatch):
    _creds(monkeypatch)
    root = tmp_path / "outputs"

    def opener(request, timeout):
        url = request.full_url
        if "/fapi/v2/positionRisk" in url:
            return _FakeResponse([{"symbol": "XAUUSDT", "positionAmt": "0", "entryPrice": "0", "unRealizedProfit": "0"}])
        if "/fapi/v2/balance" in url:
            return _FakeResponse([{"asset": "USDT", "balance": "100.0", "availableBalance": "95.0"}])
        if "/fapi/v1/openOrders" in url:
            return _FakeResponse([])
        if "/fapi/v1/userTrades" in url:
            return _FakeResponse([1, "junk", None])
        if "/fapi/v1/income" in url:
            return _FakeResponse([])
        raise AssertionError(f"unexpected url {url}")

    report = LiveBrokerReconciliation(root, _CFG, opener=opener).run(_RUN_DATE)

    assert report["confirmation_status"] == "cannot_confirm"
    assert report["account_observation"]["account_observed"] is False
    assert "row was not an object" in report["error"]
    guardrail = LiveMoneyGuardrails(root, broker_config={"request_dir": "testnet_order_requests"}).evaluate_order(
        _RUN_DATE,
        ticket={"ticket_id": "ticket_guard", "asset": "GOLD", "action": "prepare_buy"},
        symbol="XAUUSDT",
        side="BUY",
        requested_price=4500.0,
        quantity=0.002,
        source="binance_usdm:testnet",
        reconciliation=report,
    )
    assert guardrail["status"] == "BLOCKED_MONEY_GUARDRAIL_UNKNOWN"


def test_income_list_with_non_dict_row_is_not_observed(tmp_path, monkeypatch):
    _creds(monkeypatch)
    root = tmp_path / "outputs"

    def opener(request, timeout):
        url = request.full_url
        if "/fapi/v2/positionRisk" in url:
            return _FakeResponse([{"symbol": "XAUUSDT", "positionAmt": "0", "entryPrice": "0", "unRealizedProfit": "0"}])
        if "/fapi/v2/balance" in url:
            return _FakeResponse([{"asset": "USDT", "balance": "100.0", "availableBalance": "95.0"}])
        if "/fapi/v1/openOrders" in url:
            return _FakeResponse([])
        if "/fapi/v1/userTrades" in url:
            return _FakeResponse([])
        if "/fapi/v1/income" in url:
            return _FakeResponse([1, "junk", None])
        raise AssertionError(f"unexpected url {url}")

    report = LiveBrokerReconciliation(root, _CFG, opener=opener).run(_RUN_DATE)

    assert report["confirmation_status"] == "cannot_confirm"
    assert report["account_observation"]["fills_observed"] is True
    assert report["account_observation"]["income_observed"] is False
    assert report["account_observation"]["account_observed"] is False
    assert "row was not an object" in report["error"]
    guardrail = LiveMoneyGuardrails(root, broker_config={"request_dir": "testnet_order_requests"}).evaluate_order(
        _RUN_DATE,
        ticket={"ticket_id": "ticket_guard", "asset": "GOLD", "action": "prepare_buy"},
        symbol="XAUUSDT",
        side="BUY",
        requested_price=4500.0,
        quantity=0.002,
        source="binance_usdm:testnet",
        reconciliation=report,
    )
    assert guardrail["status"] == "BLOCKED_MONEY_GUARDRAIL_UNKNOWN"


def test_reconcile_account_history_disabled_is_not_observed_and_blocks_daily_loss(tmp_path, monkeypatch):
    _creds(monkeypatch)
    root = tmp_path / "outputs"
    cfg = {**_CFG, "reconcile_account_history": False}

    report = LiveBrokerReconciliation(root, cfg, opener=_opener(0.0)).run(_RUN_DATE)

    assert report["error"] == ""
    assert report["account_observation"]["history_requested"] is False
    assert report["account_observation"]["account_observed"] is False
    guardrail = LiveMoneyGuardrails(root, broker_config={"request_dir": "testnet_order_requests"}).evaluate_order(
        _RUN_DATE,
        ticket={"ticket_id": "ticket_guard", "asset": "GOLD", "action": "prepare_buy"},
        symbol="XAUUSDT",
        side="BUY",
        requested_price=4500.0,
        quantity=0.002,
        source="binance_usdm:testnet",
        reconciliation=report,
    )
    assert guardrail["status"] == "BLOCKED_MONEY_GUARDRAIL_UNKNOWN"
    assert "account history was not observed" in guardrail["daily_loss"]["reason"]


def test_missing_balance_row_is_not_present_and_blocks_daily_loss(tmp_path, monkeypatch):
    _creds(monkeypatch)
    root = tmp_path / "outputs"

    def opener(request, timeout):
        url = request.full_url
        if "/fapi/v2/positionRisk" in url:
            return _FakeResponse([{"symbol": "XAUUSDT", "positionAmt": "0", "entryPrice": "0", "unRealizedProfit": "0"}])
        if "/fapi/v2/balance" in url:
            return _FakeResponse([{"asset": "BNB", "balance": "100.0", "availableBalance": "95.0"}])
        if "/fapi/v1/openOrders" in url:
            return _FakeResponse([])
        if "/fapi/v1/userTrades" in url:
            return _FakeResponse([])
        if "/fapi/v1/income" in url:
            return _FakeResponse([])
        raise AssertionError(f"unexpected url {url}")

    report = LiveBrokerReconciliation(root, _CFG, opener=opener).run(_RUN_DATE)

    assert report["error"] == ""
    assert report["exchange_balance"]["balance_present"] is False
    assert report["account_observation"]["account_observed"] is True
    assert report["account_observation"]["balance_present"] is False
    guardrail = LiveMoneyGuardrails(root, broker_config={"request_dir": "testnet_order_requests"}).evaluate_order(
        _RUN_DATE,
        ticket={"ticket_id": "ticket_guard", "asset": "GOLD", "action": "prepare_buy"},
        symbol="XAUUSDT",
        side="BUY",
        requested_price=4500.0,
        quantity=0.002,
        source="binance_usdm:testnet",
        reconciliation=report,
    )
    assert guardrail["status"] == "BLOCKED_MONEY_GUARDRAIL_UNKNOWN"
    assert "exchange balance was not observed" in guardrail["daily_loss"]["reason"]


def test_account_history_symbol_mismatch_is_not_observed(tmp_path, monkeypatch):
    _creds(monkeypatch)
    root = tmp_path / "outputs"

    def opener(request, timeout):
        url = request.full_url
        if "/fapi/v2/positionRisk" in url:
            return _FakeResponse([{"symbol": "XAUUSDT", "positionAmt": "0", "entryPrice": "0", "unRealizedProfit": "0"}])
        if "/fapi/v2/balance" in url:
            return _FakeResponse([{"asset": "USDT", "balance": "100.0", "availableBalance": "95.0"}])
        if "/fapi/v1/openOrders" in url:
            return _FakeResponse([])
        if "/fapi/v1/userTrades" in url:
            return _FakeResponse([{"symbol": "BTCUSDT", "id": 1, "orderId": 1001, "side": "SELL", "price": "1", "qty": "1", "quoteQty": "1", "commission": "0", "commissionAsset": "USDT", "realizedPnl": "-50", "time": _DAY_MID_MS}])
        if "/fapi/v1/income" in url:
            return _FakeResponse([])
        raise AssertionError(f"unexpected url {url}")

    report = LiveBrokerReconciliation(root, _CFG, opener=opener).run(_RUN_DATE)

    assert report["confirmation_status"] == "cannot_confirm"
    assert report["account_observation"]["account_observed"] is False
    assert "symbol mismatch" in report["error"]


def test_reconciled_when_exchange_matches_local(tmp_path, monkeypatch):
    _creds(monkeypatch)
    root = tmp_path / "outputs"
    _seed_local_position(root, "long", 0.05)
    _seed_known_protective(root)

    report = LiveBrokerReconciliation(root, _CFG, opener=_opener(0.05, [_protective_order()])).run("2026-06-03")

    assert report["reconciled"] is True
    assert report["drifts"] == []
    assert report["position_protection"][0]["covered"] is True
    assert report["exchange_balance"]["available"] == 95.0
    assert any(p["symbol"] == "XAUUSDT" for p in report["exchange_positions"])
    saved = load_json(root / "live_reconciliation" / "2026-06-03.json")[0]
    assert saved["reconciled"] is True


def test_drift_when_quantity_mismatch(tmp_path, monkeypatch):
    _creds(monkeypatch)
    root = tmp_path / "outputs"
    _seed_local_position(root, "long", 0.05)
    _seed_known_protective(root)

    report = LiveBrokerReconciliation(root, _CFG, opener=_opener(0.08, [_protective_order(0.08)])).run("2026-06-03")

    assert report["reconciled"] is False
    assert len(report["drifts"]) == 1
    assert report["drifts"][0]["exchange_qty"] == 0.08
    assert report["drifts"][0]["local_qty"] == 0.05


def test_reconciles_mixed_local_position_by_net_quantity(tmp_path, monkeypatch):
    _creds(monkeypatch)
    root = tmp_path / "outputs"
    path = root / "paper_positions" / "current.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"GOLD": {"symbol": "GOLD", "side": "mixed", "quantity": 0.15, "net_quantity": 0.05, "avg_price": 4470.0}}),
        encoding="utf-8",
    )
    _seed_known_protective(root)

    report = LiveBrokerReconciliation(root, _CFG, opener=_opener(0.05, [_protective_order()])).run("2026-06-03")

    assert report["reconciled"] is True
    assert report["drifts"] == []


def test_orphan_exchange_position_flagged(tmp_path, monkeypatch):
    _creds(monkeypatch)
    root = tmp_path / "outputs"  # no local positions at all

    report = LiveBrokerReconciliation(root, _CFG, opener=_opener(0.05)).run("2026-06-03")

    assert report["reconciled"] is False
    assert report["suspected_naked_position"] is True
    assert report["system_state"] == "BLOCKED_NAKED_POSITION_SUSPECTED"
    assert any("no local record" in d["reason"] and d.get("missing_protective_order") for d in report["drifts"])


def test_local_exchange_position_without_resting_stop_is_suspected_naked(tmp_path, monkeypatch):
    _creds(monkeypatch)
    root = tmp_path / "outputs"
    _seed_local_position(root, "long", 0.05)

    report = LiveBrokerReconciliation(root, _CFG, opener=_opener(0.05)).run("2026-06-03")

    assert report["reconciled"] is False
    assert report["confirmation_status"] == "confirmed_drift"
    assert report["system_state"] == "BLOCKED_NAKED_POSITION_SUSPECTED"
    assert report["reason_code"] == "naked_position_suspected"
    assert report["suspected_naked_position"] is True
    assert report["position_protection"][0]["covered"] is False
    assert report["drifts"][0]["missing_protective_order"] is True


def test_duplicate_nonzero_position_rows_are_blocked_as_unsupported_dual_side_shape(tmp_path, monkeypatch):
    _creds(monkeypatch)
    root = tmp_path / "outputs"
    _seed_local_position(root, "long", 0.05)
    _seed_known_protective(root)

    def opener(request, timeout):
        url = request.full_url
        assert "signature=" in url
        if "/fapi/v2/positionRisk" in url:
            return _FakeResponse(
                [
                    {"symbol": "XAUUSDT", "positionAmt": "0.05", "entryPrice": "4470.0", "unRealizedProfit": "1.5"},
                    {"symbol": "XAUUSDT", "positionAmt": "-0.02", "entryPrice": "4475.0", "unRealizedProfit": "-0.4"},
                ]
            )
        if "/fapi/v2/balance" in url:
            return _FakeResponse([{"asset": "USDT", "balance": "100.0", "availableBalance": "95.0"}])
        if "/fapi/v1/openOrders" in url:
            return _FakeResponse([_protective_order(0.05), _protective_order(0.02, client_id="known_sl_short", side="BUY")])
        if "/fapi/v1/userTrades" in url:
            return _FakeResponse([])
        if "/fapi/v1/income" in url:
            return _FakeResponse([])
        raise AssertionError(f"unexpected url {url}")

    report = LiveBrokerReconciliation(root, _CFG, opener=opener).run("2026-06-03")

    assert report["reconciled"] is False
    assert report["confirmation_status"] == "confirmed_drift"
    assert any(drift.get("reason_code") == "unsupported_dual_side_position_shape" for drift in report["drifts"])


def test_orphan_protective_order_flagged_from_exchange_open_orders(tmp_path, monkeypatch):
    _creds(monkeypatch)
    root = tmp_path / "outputs"

    def opener(request, timeout):
        url = request.full_url
        assert "signature=" in url
        if "/fapi/v2/positionRisk" in url:
            return _FakeResponse([{"symbol": "XAUUSDT", "positionAmt": "0", "entryPrice": "0", "unRealizedProfit": "0"}])
        if "/fapi/v2/balance" in url:
            return _FakeResponse([{"asset": "USDT", "balance": "100.0", "availableBalance": "95.0"}])
        if "/fapi/v1/openOrders" in url:
            return _FakeResponse(
                [
                    {
                        "symbol": "XAUUSDT",
                        "orderId": 9001,
                        "clientOrderId": "orphan_sl",
                        "type": "STOP_MARKET",
                        "side": "SELL",
                        "origQty": "0.002",
                        "reduceOnly": "true",
                        "status": "NEW",
                    }
                    ]
                )
        if "/fapi/v1/userTrades" in url:
            return _FakeResponse([])
        if "/fapi/v1/income" in url:
            return _FakeResponse([])
        raise AssertionError(f"unexpected url {url}")

    report = LiveBrokerReconciliation(root, _CFG, opener=opener).run("2026-06-03")

    assert report["reconciled"] is False
    assert report["drift_count"] == 1
    assert report["drifts"][0]["reason"] == "orphan protective order has no local position"
    assert report["exchange_open_orders"][0]["client_order_id"] == "orphan_sl"


def test_flat_exchange_reconciliation_marks_closed_order_reconciled(tmp_path, monkeypatch):
    _creds(monkeypatch)
    root = tmp_path / "outputs"
    store = OrderLifecycleStore(root)
    store.write_intent(
        "2026-06-03",
        order_id="order_closed",
        ticket_id="ticket_closed",
        idempotency_key="order_closed",
        requested_quantity=0.002,
        requested_price=4525.5,
        source="test",
    )
    for state in ["submitting", "accepted", "filled", "protective_attached", "closed"]:
        store.transition("2026-06-03", "order_closed", state, reason=f"test_{state}")

    report = LiveBrokerReconciliation(root, _CFG, opener=_opener(0.0)).run("2026-06-03")

    assert report["reconciled"] is True
    lifecycle = load_json(root / "order_lifecycle" / "2026-06-03.json")[0]
    assert lifecycle["state"] == "reconciled"
    assert [item["to"] for item in lifecycle["transitions"]][-1] == "reconciled"


def test_accounting_projection_failure_cannot_block_reconciliation_writes(tmp_path, monkeypatch):
    _creds(monkeypatch)
    root = tmp_path / "outputs"
    store = OrderLifecycleStore(root)
    store.write_intent(
        "2026-06-03",
        order_id="order_closed",
        ticket_id="ticket_closed",
        idempotency_key="order_closed",
        requested_quantity=0.002,
        requested_price=4525.5,
        source="test",
    )
    for state in ["submitting", "accepted", "filled", "protective_attached", "closed"]:
        store.transition("2026-06-03", "order_closed", state, reason=f"test_{state}")
    monkeypatch.setattr(
        "services.accounting_projection.build_accounting_snapshot",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("snapshot builder failed")),
    )

    report = LiveBrokerReconciliation(
        root,
        _CFG,
        opener=_opener(0.0),
    ).run("2026-06-03")

    assert report["reconciled"] is True
    assert report["accounting_snapshot"]["schema_version"] == "accounting-projection-unavailable-v1"
    assert report["accounting_snapshot"]["reconciliation"]["status"] == "blocked"
    assert report["accounting_snapshot"]["reconciliation"]["issues"][0]["code"] == "accounting_projection_unavailable"
    assert load_json(root / "live_reconciliation" / "current.json")[0] == report
    assert OrderLifecycleStore(root).current("2026-06-03", "order_closed")["state"] == "reconciled"


def test_missing_credentials_records_error_not_crash(tmp_path, monkeypatch):
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    monkeypatch.setenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(tmp_path / "absent.env"))
    root = tmp_path / "outputs"

    report = LiveBrokerReconciliation(root, _CFG, opener=_opener(0.05)).run("2026-06-03")

    assert report["reconciled"] is False
    assert "BINANCE_API_KEY" in report["error"]
    assert report["confirmation_status"] == "cannot_confirm"
    assert report["system_state"] == "BLOCKED_RECONCILIATION_UNKNOWN"
    assert report["can_open_new_orders"] is False


def test_fetch_error_is_cannot_confirm_not_flat(tmp_path, monkeypatch):
    _creds(monkeypatch)
    root = tmp_path / "outputs"

    def opener(_request, timeout):
        raise TimeoutError("venue read timed out")

    report = LiveBrokerReconciliation(root, _CFG, opener=opener).run("2026-06-03")

    assert report["reconciled"] is False
    assert report["confirmation_status"] == "cannot_confirm"
    assert report["system_state"] == "BLOCKED_RECONCILIATION_UNKNOWN"
    assert report["reason_code"] == "venue_state_unknown"
    assert report["flat_confirmed"] is False
    assert report["can_open_new_orders"] is False
    assert report["drift_count"] == 0
    assert report["exchange_positions"] == []
    assert "TimeoutError" in report["error"]


def test_cannot_confirm_with_submitting_intent_surfaces_suspected_naked_position(tmp_path, monkeypatch):
    _creds(monkeypatch)
    root = tmp_path / "outputs"
    store = OrderLifecycleStore(root)
    store.write_intent(
        "2026-06-03",
        order_id="order_submit",
        ticket_id="ticket_submit",
        idempotency_key="order_submit",
        requested_quantity=0.002,
        requested_price=4525.5,
        source="test",
        metadata={"symbol": "XAUUSDT", "ticket": {"ticket_id": "ticket_submit", "asset": "GOLD"}},
    )
    store.transition("2026-06-03", "order_submit", "submitting", reason="submit_started")

    def opener(_request, timeout):
        raise TimeoutError("venue read timed out")

    report = LiveBrokerReconciliation(root, _CFG, opener=opener).run("2026-06-03")

    assert report["confirmation_status"] == "cannot_confirm"
    assert report["system_state"] == "BLOCKED_RECONCILIATION_UNKNOWN"
    assert report["reason_code"] == "naked_position_suspected"
    assert report["suspected_naked_position"] is True
    assert "naked" in report["escalation_action"]
    assert report["naked_position_risks"][0]["order_id"] == "order_submit"


def test_exchange_position_with_local_submitting_is_suspected_naked_position(tmp_path, monkeypatch):
    _creds(monkeypatch)
    root = tmp_path / "outputs"
    store = OrderLifecycleStore(root)
    store.write_intent(
        "2026-06-03",
        order_id="order_submit",
        ticket_id="ticket_submit",
        idempotency_key="order_submit",
        requested_quantity=0.002,
        requested_price=4525.5,
        source="test",
        metadata={"symbol": "XAUUSDT", "ticket": {"ticket_id": "ticket_submit", "asset": "GOLD"}},
    )
    store.transition("2026-06-03", "order_submit", "submitting", reason="submit_started")

    report = LiveBrokerReconciliation(root, _CFG, opener=_opener(0.002)).run("2026-06-03")

    assert report["reconciled"] is False
    assert report["confirmation_status"] == "confirmed_drift"
    assert report["system_state"] == "BLOCKED_NAKED_POSITION_SUSPECTED"
    assert report["reason_code"] == "naked_position_suspected"
    assert report["suspected_naked_position"] is True
    assert report["drifts"][0]["reason_code"] == "naked_position_suspected"
    assert "suspected naked position" in report["drifts"][0]["reason"]
