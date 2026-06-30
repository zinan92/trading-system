import json
from pathlib import Path

from services.live_reconciliation import LiveBrokerReconciliation
from services.journal_store import load_json
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


def _opener(position_amt: float):
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


def _creds(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "testkey123")
    monkeypatch.setenv("BINANCE_API_SECRET", "testsecret123")


def _seed_local_position(root: Path, side: str, quantity: float) -> None:
    path = root / "paper_positions" / "current.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"GOLD": {"symbol": "GOLD", "side": side, "quantity": quantity, "avg_price": 4470.0}}), encoding="utf-8")


def test_reconciled_when_exchange_matches_local(tmp_path, monkeypatch):
    _creds(monkeypatch)
    root = tmp_path / "outputs"
    _seed_local_position(root, "long", 0.05)

    report = LiveBrokerReconciliation(root, _CFG, opener=_opener(0.05)).run("2026-06-03")

    assert report["reconciled"] is True
    assert report["drifts"] == []
    assert report["exchange_balance"]["available"] == 95.0
    assert any(p["symbol"] == "XAUUSDT" for p in report["exchange_positions"])
    saved = load_json(root / "live_reconciliation" / "2026-06-03.json")[0]
    assert saved["reconciled"] is True


def test_drift_when_quantity_mismatch(tmp_path, monkeypatch):
    _creds(monkeypatch)
    root = tmp_path / "outputs"
    _seed_local_position(root, "long", 0.05)

    report = LiveBrokerReconciliation(root, _CFG, opener=_opener(0.08)).run("2026-06-03")

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

    report = LiveBrokerReconciliation(root, _CFG, opener=_opener(0.05)).run("2026-06-03")

    assert report["reconciled"] is True
    assert report["drifts"] == []


def test_orphan_exchange_position_flagged(tmp_path, monkeypatch):
    _creds(monkeypatch)
    root = tmp_path / "outputs"  # no local positions at all

    report = LiveBrokerReconciliation(root, _CFG, opener=_opener(0.05)).run("2026-06-03")

    assert report["reconciled"] is False
    assert any("no local record" in d["reason"] for d in report["drifts"])


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
