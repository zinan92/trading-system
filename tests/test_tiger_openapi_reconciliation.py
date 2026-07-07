from __future__ import annotations

from pathlib import Path

import pytest

from services.broker_adapter import BrokerOrderRequest
from services.journal_store import load_json, write_json
from services.tiger_openapi_broker_adapter import TigerOpenApiPaperBrokerAdapter
from services.tiger_openapi_kill_switch import TigerOpenApiPaperKillSwitch
from services.tiger_openapi_order_sync import TigerOpenApiOrderSync
from services.tiger_openapi_reconciliation import TigerOpenApiPaperReconciliation
from tests.test_tiger_openapi_broker_adapter import _FakeTigerSdk, _armed_config, _props, _ticket


class _FakeTigerTradeClient:
    def __init__(self, *, positions=None, open_orders=None, filled_orders=None) -> None:
        self._account = "DU123456"
        self.positions = positions if positions is not None else []
        self.open_orders = open_orders if open_orders is not None else []
        self.filled_orders = filled_orders if filled_orders is not None else []
        self.calls: list[tuple[str, object]] = []

    def get_positions(self, **kwargs):
        self.calls.append(("get_positions", kwargs))
        return self.positions

    def get_open_orders(self, **kwargs):
        self.calls.append(("get_open_orders", kwargs))
        return self.open_orders

    def get_filled_orders(self, **kwargs):
        self.calls.append(("get_filled_orders", kwargs))
        return self.filled_orders

    def preview_order(self, order):
        self.calls.append(("preview_order", order))
        return {"ok": True}

    def place_order(self, order):
        self.calls.append(("place_order", order))
        return 123

    def cancel_order(self, *args, **kwargs):
        raise AssertionError("Tiger kill-switch dry-run must not cancel orders")


class _NetworkTigerTradeClient(_FakeTigerTradeClient):
    def cancel_order(self, *args, **kwargs):
        self.calls.append(("cancel_order", kwargs))
        return kwargs.get("id")


def _position(symbol: str = "MGC2608", quantity: float = 1.0) -> dict:
    return {
        "contract": {"local_symbol": symbol, "symbol": "MGC"},
        "quantity": quantity,
        "average_cost": 4180.0,
        "market_price": 4186.0,
        "unrealized_pnl": 60.0,
    }


def _stop_order(symbol: str = "MGC2608", quantity: float = 1.0) -> dict:
    return {
        "contract": {"local_symbol": symbol, "symbol": "MGC"},
        "id": 9001,
        "order_type": "STP",
        "action": "SELL",
        "quantity": quantity,
        "aux_price": 4170.0,
        "status": "NEW",
    }


def _filled_order(symbol: str = "MGC2608", quantity: float = 1.0) -> dict:
    return {
        "contract": {"local_symbol": symbol, "symbol": "MGC", "currency": "USD"},
        "id": 8001,
        "order_type": "LMT",
        "action": "BUY",
        "quantity": quantity,
        "filled_quantity": quantity,
        "avg_fill_price": 4186.5,
        "status": "FILLED",
        "filled_time": "2026-07-05T01:02:03+00:00",
        "account": "DU123456",
    }


def test_tiger_reconciliation_confirms_flat_and_writes_artifacts(tmp_path: Path):
    root = tmp_path / "outputs"
    client = _FakeTigerTradeClient()

    report = TigerOpenApiPaperReconciliation(root, _armed_config(), trade_client=client).run("2026-07-05")

    assert report["confirmation_status"] == "confirmed_flat"
    assert report["can_open_new_orders"] is True
    assert report["exchange_positions"] == []
    assert report["exchange_open_orders"] == []
    assert load_json(root / "tiger_reconciliation" / "current.json")[-1]["confirmation_status"] == "confirmed_flat"
    assert [name for name, _ in client.calls] == ["get_positions", "get_open_orders"]


def test_tiger_reconciliation_ignores_non_tiger_local_gold_position(tmp_path: Path):
    root = tmp_path / "outputs"
    write_json(root / "paper_positions" / "current.json", {"GOLD": {"side": "long", "quantity": 0.5}})
    client = _FakeTigerTradeClient()

    report = TigerOpenApiPaperReconciliation(root, _armed_config(), trade_client=client).run("2026-07-05")

    assert report["confirmation_status"] == "confirmed_flat"
    assert report["local_positions"] == {}


def test_tiger_reconciliation_defaults_to_tiger_profile_when_top_level_broker_differs(monkeypatch, tmp_path: Path):
    from services import tiger_openapi_reconciliation as reconciliation_module

    root = tmp_path / "outputs"
    write_json(root / "paper_positions" / "current.json", {"GOLD": {"side": "long", "quantity": 0.5}})
    monkeypatch.setattr(
        reconciliation_module,
        "load_pipeline_config",
        lambda: {
            "output_root": str(root),
            "broker": {"provider": "binance_usdm"},
            "broker_profiles": {"tiger_openapi_paper": _armed_config()},
        },
    )

    report = reconciliation_module.TigerOpenApiPaperReconciliation(root, trade_client=_FakeTigerTradeClient()).run("2026-07-05")

    assert report["confirmation_status"] == "confirmed_flat"
    assert report["local_positions"] == {}


def test_tiger_order_sync_reads_open_and_filled_orders_without_network_modification(tmp_path: Path):
    root = tmp_path / "outputs"
    client = _FakeTigerTradeClient(open_orders=[_stop_order()], filled_orders=[_filled_order()])

    report = TigerOpenApiOrderSync(root, _armed_config(), trade_client=client).run(
        "2026-07-05",
        start_time="2026-07-05 00:00:00",
        end_time="2026-07-05 23:59:59",
    )

    assert report["sync_status"] == "synced"
    assert report["open_order_count"] == 1
    assert report["filled_order_count"] == 1
    assert report["exchange_open_orders"][0]["source"] == "tiger_open_orders"
    assert report["exchange_filled_orders"][0]["source"] == "tiger_filled_orders"
    assert report["exchange_filled_orders"][0]["average_fill_price"] == 4186.5
    assert "account" not in report["exchange_filled_orders"][0]["raw"]
    assert [name for name, _ in client.calls] == ["get_open_orders", "get_filled_orders"]
    assert load_json(root / "tiger_order_sync" / "current.json")[-1]["sync_status"] == "synced"


def test_tiger_order_sync_defaults_filled_window_to_run_date(tmp_path: Path):
    root = tmp_path / "outputs"
    client = _FakeTigerTradeClient()

    report = TigerOpenApiOrderSync(root, _armed_config(), trade_client=client).run("2026-07-05")

    assert report["query"]["start_time"] == "2026-07-05 00:00:00"
    assert report["query"]["end_time"] == "2026-07-05 23:59:59"
    assert client.calls[-1] == (
        "get_filled_orders",
        {"sec_type": "FUT", "start_time": "2026-07-05 00:00:00", "end_time": "2026-07-05 23:59:59"},
    )


def test_tiger_reconciliation_flags_naked_position_without_local_or_stop(tmp_path: Path):
    root = tmp_path / "outputs"
    client = _FakeTigerTradeClient(positions=[_position()])

    report = TigerOpenApiPaperReconciliation(root, _armed_config(), trade_client=client).run("2026-07-05")

    assert report["confirmation_status"] == "confirmed_drift"
    assert report["suspected_naked_position"] is True
    assert report["reason_code"] == "naked_position_suspected"
    assert report["can_open_new_orders"] is False
    assert report["naked_position_risks"][0]["missing_protective_order"] is True


def test_tiger_reconciliation_accepts_local_position_with_protective_stop(tmp_path: Path):
    root = tmp_path / "outputs"
    write_json(root / "paper_positions" / "current.json", {"MGC2608": {"side": "long", "quantity": 1}})
    client = _FakeTigerTradeClient(positions=[_position()], open_orders=[_stop_order()])

    report = TigerOpenApiPaperReconciliation(root, _armed_config(), trade_client=client).run("2026-07-05")

    assert report["confirmation_status"] == "confirmed_open"
    assert report["suspected_naked_position"] is False
    assert report["drift_count"] == 0
    assert report["position_protection"][0]["covered"] is True
    assert report["can_open_new_orders"] is False


def test_tiger_adapter_blocks_place_when_reconciliation_is_not_flat(tmp_path: Path, monkeypatch):
    _props(monkeypatch, tmp_path)
    root = tmp_path / "outputs"
    client = _FakeTigerTradeClient(positions=[_position()])
    adapter = TigerOpenApiPaperBrokerAdapter(root, _armed_config(), trade_client=client, sdk=_FakeTigerSdk())

    with pytest.raises(RuntimeError, match="Tiger reconciliation blocks"):
        adapter.submit_order(BrokerOrderRequest("2026-07-05", _ticket(), latest_price=4186.0, actual_size=1))

    assert [name for name, _ in client.calls] == ["get_positions", "get_open_orders"]
    request = load_json(root / "tiger_order_requests" / "2026-07-05.json")[-1]
    assert request["receipt"]["status"] == "blocked"
    assert request["readiness"]["tiger_reconciliation"]["can_open_new_orders"] is False
    assert not any(name == "place_order" for name, _ in client.calls)


def test_tiger_kill_switch_dry_run_plans_actions_without_network_modification(tmp_path: Path):
    root = tmp_path / "outputs"
    client = _FakeTigerTradeClient(positions=[_position()], open_orders=[_stop_order()])

    report = TigerOpenApiPaperKillSwitch(root, _armed_config(), trade_client=client).run("2026-07-05")

    assert report["status"] == "dry_run"
    assert report["network_order_created"] is False
    assert report["network_cancel_created"] is False
    assert report["intended_cancel_orders"][0]["order_id"] == "9001"
    assert report["intended_close_positions"][0]["symbol"] == "MGC2608"
    assert load_json(root / "tiger_kill_switch" / "current.json")[-1]["status"] == "dry_run"


def test_tiger_kill_switch_confirm_activates_halt_but_does_not_cancel_or_close(tmp_path: Path):
    root = tmp_path / "outputs"
    client = _FakeTigerTradeClient(positions=[_position()], open_orders=[_stop_order()])

    report = TigerOpenApiPaperKillSwitch(root, _armed_config(), trade_client=client).run("2026-07-05", confirm_tiger_kill=True)

    assert report["status"] == "halt_active_manual_action_required"
    assert report["halt"]["active"] is True
    assert report["network_order_created"] is False
    assert report["network_cancel_created"] is False
    assert not any(name in {"place_order", "cancel_order"} for name, _ in client.calls)


def test_tiger_kill_switch_network_actions_require_explicit_config_and_confirmation(tmp_path: Path):
    root = tmp_path / "outputs"
    client = _NetworkTigerTradeClient(positions=[_position()], open_orders=[_stop_order()])
    config = {**_armed_config(), "enable_tiger_kill_switch_network_actions": True}

    report = TigerOpenApiPaperKillSwitch(root, config, trade_client=client, sdk=_FakeTigerSdk()).run(
        "2026-07-05",
        confirm_tiger_kill=True,
    )

    assert report["status"] == "halt_active_network_actions_sent"
    assert report["network_actions"]["ready"] is True
    assert report["network_cancel_created"] is True
    assert report["network_order_created"] is True
    assert report["intended_cancel_orders"][0]["network_cancel_created"] is True
    assert report["intended_close_positions"][0]["network_order_created"] is True
    assert [name for name, _ in client.calls] == ["get_positions", "get_open_orders", "cancel_order", "place_order"]
    placed = client.calls[-1][1]
    assert placed.action == "SELL"
    assert placed.quantity == 1
    assert placed.contract["local_symbol"] == "MGC2608"
