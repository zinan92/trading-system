from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from services.broker_adapter import BrokerOrderRequest, build_broker_adapter, broker_preflight, resolve_broker_config
from services.journal_store import load_json
from services.tiger_openapi_broker_adapter import TigerOpenApiPaperBrokerAdapter


class _FakeTigerSdk:
    def future_contract(self, symbol, currency, **kwargs):
        return {"symbol": symbol, "currency": currency, **kwargs}

    def order_leg(self, leg_type, price=None, time_in_force="DAY", **kwargs):
        return {"leg_type": leg_type, "price": price, "time_in_force": time_in_force, **kwargs}

    def limit_order_with_legs(self, account, contract, action, quantity, limit_price, order_legs=None, time_in_force="DAY"):
        return SimpleNamespace(
            account=account,
            contract=contract,
            action=action,
            order_type="LMT",
            quantity=quantity,
            limit_price=limit_price,
            order_legs=order_legs or [],
            time_in_force=time_in_force,
        )

    def limit_order(self, account, contract, action, quantity, limit_price, time_in_force="DAY"):
        return SimpleNamespace(
            account=account,
            contract=contract,
            action=action,
            order_type="LMT",
            quantity=quantity,
            limit_price=limit_price,
            order_legs=[],
            time_in_force=time_in_force,
        )

    def market_order(self, account, contract, action, quantity, time_in_force="DAY"):
        return SimpleNamespace(account=account, contract=contract, action=action, order_type="MKT", quantity=quantity, time_in_force=time_in_force)

    def stop_order(self, account, contract, action, quantity, aux_price, time_in_force="DAY"):
        return SimpleNamespace(account=account, contract=contract, action=action, order_type="STP", quantity=quantity, aux_price=aux_price, time_in_force=time_in_force)

    def stop_limit_order(self, account, contract, action, quantity, limit_price, aux_price, time_in_force="DAY"):
        return SimpleNamespace(
            account=account,
            contract=contract,
            action=action,
            order_type="STP_LMT",
            quantity=quantity,
            limit_price=limit_price,
            aux_price=aux_price,
            time_in_force=time_in_force,
        )


def _prime_assets(*, balance: float = 25000.0, realized_pl: float = 0.0) -> SimpleNamespace:
    return SimpleNamespace(
        segments={
            "C": SimpleNamespace(
                currency="USD",
                net_liquidation=balance,
                cash_available_for_trade=balance,
                realized_pl=realized_pl,
                unrealized_pl=0.0,
            )
        }
    )


class _FakeTigerTradeClient:
    def __init__(self, *, positions=None, open_orders=None, prime_assets=None, prime_assets_error: Exception | None = None) -> None:
        self._account = "DU123456"
        self.positions = positions if positions is not None else []
        self.open_orders = open_orders if open_orders is not None else []
        self.prime_assets = prime_assets if prime_assets is not None else _prime_assets()
        self.prime_assets_error = prime_assets_error
        self.calls: list[tuple[str, object]] = []

    def get_positions(self, **kwargs):
        self.calls.append(("get_positions", kwargs))
        return self.positions

    def get_open_orders(self, **kwargs):
        self.calls.append(("get_open_orders", kwargs))
        return self.open_orders

    def get_prime_assets(self, **kwargs):
        self.calls.append(("get_prime_assets", kwargs))
        if self.prime_assets_error:
            raise self.prime_assets_error
        return self.prime_assets

    def preview_order(self, order):
        self.calls.append(("preview_order", order))
        return {"ok": True, "margin_change": 2300}

    def place_order(self, order):
        self.calls.append(("place_order", order))
        order.id = 987654321
        order.sub_ids = [987654322, 987654323]
        order.orders = [{"id": 987654321}]
        return 987654321


def _ticket(**overrides) -> dict:
    ticket = {
        "ticket_id": "ticket_mgc_20260705_adapter",
        "signal_id": "sig_mgc_adapter",
        "asset": "MGC2608",
        "asset_class": "future",
        "action": "prepare_buy",
        "entry_zone": "4180-4190",
        "stop_loss": 4170.0,
        "targets": [4200.0],
        "position_size_pct": 8,
        "max_loss_pct": 0.5,
        "order_type": "limit",
        "time_in_force": "day",
        "paper_only": True,
    }
    ticket.update(overrides)
    return ticket


def _props(monkeypatch, tmp_path: Path) -> Path:
    props = tmp_path / "tiger_openapi_config.properties"
    props.write_text("tiger_id=placeholder\n", encoding="utf-8")
    props.chmod(0o600)
    monkeypatch.setenv("TIGER_OPENAPI_CONFIG_PATH", str(props))
    monkeypatch.setenv("TRADING_ORCHESTRATOR_LIVE_ENV", str(tmp_path / "missing-live.env"))
    return props


def _armed_config() -> dict:
    return {
        "provider": "tiger_openapi",
        "environment": "paper",
        "exchange": "COMEX",
        "dry_run": False,
        "props_path_env": "TIGER_OPENAPI_CONFIG_PATH",
        "base_currency": "USD",
        "account_segment_priority": ["C", "F", "S"],
        "network_order_submission": "paper_tradeclient",
        "confirm_tiger_paper_orders": True,
        "preview_before_place": True,
        "require_flat_before_entry": True,
        "require_no_open_orders_before_entry": True,
        "require_attached_protection_before_entry": True,
        "require_live_money_guardrails_before_entry": False,
        "allowed_symbols": ["MGC2608"],
        "execution_contract_map": {"MGCmain": "MGC2608", "MGC": "MGC2608"},
        "rollover_policy": {"rollover_days_before_contract_month": 14},
        "contract_specs": {
            "MGC2608": {
                "symbol": "MGC",
                "currency": "USD",
                "exchange": "COMEX",
                "contract_month": "202608",
                "multiplier": 10,
                "local_symbol": "MGC2608",
            }
        },
        "request_dir": "tiger_order_requests",
    }


def test_tiger_paper_tradeclient_places_limit_with_attached_legs(tmp_path: Path, monkeypatch):
    _props(monkeypatch, tmp_path)
    root = tmp_path / "outputs"
    trade_client = _FakeTigerTradeClient()
    adapter = TigerOpenApiPaperBrokerAdapter(root, _armed_config(), trade_client=trade_client, sdk=_FakeTigerSdk())

    order = adapter.submit_order(BrokerOrderRequest("2026-07-05", _ticket(), latest_price=4186.0, actual_size=1))

    assert order.status == "submitted_to_tiger_paper"
    assert [name for name, _ in trade_client.calls] == ["get_positions", "get_open_orders", "preview_order", "place_order"]
    placed = trade_client.calls[-1][1]
    assert placed.contract["symbol"] == "MGC"
    assert placed.contract["contract_month"] == "202608"
    assert placed.quantity == 1
    assert placed.order_type == "LMT"
    assert [leg["leg_type"] for leg in placed.order_legs] == ["PROFIT", "LOSS"]

    requests = load_json(root / "tiger_order_requests" / "2026-07-05.json")
    request = requests[-1]
    assert request["request"]["network_order_created"] is True
    assert request["request"]["submission_intent"] == "tiger_tradeclient_future_order"
    assert request["request"]["protective_status"] == "attached_in_parent_order"
    assert request["request"]["protective_leg_count"] == 2
    assert request["broker_response"]["place_order"]["order_id"] == 987654321

    lifecycle = load_json(root / "order_lifecycle" / "2026-07-05.json")[-1]
    assert lifecycle["source"] == "tiger_openapi:paper"
    assert lifecycle["state"] == "accepted"


def test_tiger_paper_tradeclient_maps_continuous_ticket_to_dated_execution_contract(tmp_path: Path, monkeypatch):
    _props(monkeypatch, tmp_path)
    root = tmp_path / "outputs"
    trade_client = _FakeTigerTradeClient()
    adapter = TigerOpenApiPaperBrokerAdapter(root, _armed_config(), trade_client=trade_client, sdk=_FakeTigerSdk())

    order = adapter.submit_order(BrokerOrderRequest("2026-07-05", _ticket(asset="MGCmain"), latest_price=4186.0, actual_size=1))

    assert order.status == "submitted_to_tiger_paper"
    placed = trade_client.calls[-1][1]
    assert placed.contract["local_symbol"] == "MGC2608"
    request = load_json(root / "tiger_order_requests" / "2026-07-05.json")[-1]
    assert request["readiness"]["tiger_contract"]["requested_symbol"] == "MGCmain"
    assert request["readiness"]["tiger_contract"]["execution_symbol"] == "MGC2608"
    assert request["request"]["symbol"] == "MGC2608"


def test_tiger_paper_tradeclient_requires_explicit_network_confirmation(tmp_path: Path, monkeypatch):
    _props(monkeypatch, tmp_path)
    root = tmp_path / "outputs"
    config = {**_armed_config(), "confirm_tiger_paper_orders": False}
    trade_client = _FakeTigerTradeClient()
    adapter = TigerOpenApiPaperBrokerAdapter(root, config, trade_client=trade_client, sdk=_FakeTigerSdk())

    with pytest.raises(RuntimeError, match="confirm_tiger_paper_orders"):
        adapter.submit_order(BrokerOrderRequest("2026-07-05", _ticket(), latest_price=4186.0, actual_size=1))

    assert trade_client.calls == []
    assert load_json(root / "tiger_order_requests" / "2026-07-05.json")[-1]["receipt"]["status"] == "blocked"


def test_tiger_paper_tradeclient_blocks_inside_rollover_window_before_tradeclient_calls(tmp_path: Path, monkeypatch):
    _props(monkeypatch, tmp_path)
    root = tmp_path / "outputs"
    trade_client = _FakeTigerTradeClient()
    adapter = TigerOpenApiPaperBrokerAdapter(root, _armed_config(), trade_client=trade_client, sdk=_FakeTigerSdk())

    with pytest.raises(RuntimeError, match="contract resolution blocks"):
        adapter.submit_order(BrokerOrderRequest("2026-07-25", _ticket(), latest_price=4186.0, actual_size=1))

    assert trade_client.calls == []
    request = load_json(root / "tiger_order_requests" / "2026-07-25.json")[-1]
    assert request["receipt"]["status"] == "blocked"
    assert request["readiness"]["tiger_contract"]["status"] == "rollover_required"
    assert "inside rollover window" in request["receipt"]["rejection_reason"]


def test_tiger_paper_tradeclient_money_guardrail_fails_closed_before_preview_or_place(tmp_path: Path, monkeypatch):
    _props(monkeypatch, tmp_path)
    root = tmp_path / "outputs"
    trade_client = _FakeTigerTradeClient(prime_assets_error=RuntimeError("account sync unavailable"))
    config = {**_armed_config(), "require_live_money_guardrails_before_entry": True}
    adapter = TigerOpenApiPaperBrokerAdapter(root, config, trade_client=trade_client, sdk=_FakeTigerSdk())

    with pytest.raises(RuntimeError, match="live money guardrails block"):
        adapter.submit_order(BrokerOrderRequest("2026-07-05", _ticket(), latest_price=4186.0, actual_size=1))

    assert [name for name, _ in trade_client.calls] == ["get_positions", "get_open_orders", "get_prime_assets"]
    request = load_json(root / "tiger_order_requests" / "2026-07-05.json")[-1]
    assert request["receipt"]["status"] == "blocked"
    assert request["readiness"]["tiger_account_sync"]["sync_status"] == "cannot_sync"
    assert request["readiness"]["live_money_guardrails"]["status"] == "BLOCKED_MONEY_GUARDRAIL_UNKNOWN"
    assert request["readiness"]["live_money_guardrails"]["risk_decision"]["allow_exposure_increase"] is False
    assert "account history was not observed" in request["receipt"]["rejection_reason"]
    assert not any(name in {"preview_order", "place_order"} for name, _ in trade_client.calls)


def test_tiger_paper_tradeclient_uses_account_sync_before_money_guardrails(tmp_path: Path, monkeypatch):
    _props(monkeypatch, tmp_path)
    root = tmp_path / "outputs"
    trade_client = _FakeTigerTradeClient(prime_assets=_prime_assets(balance=25000.0, realized_pl=0.0))
    config = {**_armed_config(), "require_live_money_guardrails_before_entry": True}
    adapter = TigerOpenApiPaperBrokerAdapter(root, config, trade_client=trade_client, sdk=_FakeTigerSdk())

    with pytest.raises(RuntimeError, match="live money guardrails block"):
        adapter.submit_order(BrokerOrderRequest("2026-07-05", _ticket(), latest_price=4186.0, actual_size=1))

    assert [name for name, _ in trade_client.calls] == ["get_positions", "get_open_orders", "get_prime_assets"]
    request = load_json(root / "tiger_order_requests" / "2026-07-05.json")[-1]
    assert request["readiness"]["tiger_account_sync"]["sync_status"] == "synced"
    assert request["readiness"]["tiger_reconciliation_raw"]["exchange_balance"]["balance"] == 25000.0
    assert request["readiness"]["live_money_guardrails"]["daily_loss"]["known"] is True
    assert request["readiness"]["live_money_guardrails"]["status"] == "BLOCKED_SINGLE_ORDER_NOTIONAL_LIMIT"
    assert request["readiness"]["live_money_guardrails"]["risk_decision"]["allow_exposure_increase"] is False
    assert not any(name in {"preview_order", "place_order"} for name, _ in trade_client.calls)


def test_tiger_paper_tradeclient_blocks_non_limit_orders_with_attached_protection(tmp_path: Path, monkeypatch):
    _props(monkeypatch, tmp_path)
    root = tmp_path / "outputs"
    trade_client = _FakeTigerTradeClient()
    adapter = TigerOpenApiPaperBrokerAdapter(root, _armed_config(), trade_client=trade_client, sdk=_FakeTigerSdk())

    with pytest.raises(RuntimeError, match="require LIMIT orders"):
        adapter.submit_order(
            BrokerOrderRequest(
                "2026-07-05",
                _ticket(order_type="market"),
                latest_price=4186.0,
                actual_size=1,
            )
        )

    assert trade_client.calls == []
    request = load_json(root / "tiger_order_requests" / "2026-07-05.json")[-1]
    assert request["receipt"]["status"] == "blocked"
    assert request["readiness"]["protective_order_precheck"]["ready"] is False
    assert "require LIMIT orders" in request["receipt"]["rejection_reason"]


def test_tiger_paper_tradeclient_requires_attached_stop_and_target_before_client_calls(tmp_path: Path, monkeypatch):
    _props(monkeypatch, tmp_path)
    root = tmp_path / "outputs"
    trade_client = _FakeTigerTradeClient()
    adapter = TigerOpenApiPaperBrokerAdapter(root, _armed_config(), trade_client=trade_client, sdk=_FakeTigerSdk())

    with pytest.raises(RuntimeError, match="require attached stop_loss and take-profit"):
        adapter.submit_order(
            BrokerOrderRequest(
                "2026-07-05",
                _ticket(stop_loss=None, targets=[]),
                latest_price=4186.0,
                actual_size=1,
            )
        )

    assert trade_client.calls == []
    request = load_json(root / "tiger_order_requests" / "2026-07-05.json")[-1]
    assert request["receipt"]["status"] == "blocked"
    assert request["readiness"]["protective_order_precheck"]["expected_leg_count"] == 0
    assert "missing stop_loss, targets[0]" in request["receipt"]["rejection_reason"]


def test_tiger_paper_tradeclient_can_disable_attached_protection_requirement_for_low_level_drills(tmp_path: Path, monkeypatch):
    _props(monkeypatch, tmp_path)
    root = tmp_path / "outputs"
    trade_client = _FakeTigerTradeClient()
    config = {**_armed_config(), "require_attached_protection_before_entry": False}
    adapter = TigerOpenApiPaperBrokerAdapter(root, config, trade_client=trade_client, sdk=_FakeTigerSdk())

    order = adapter.submit_order(
        BrokerOrderRequest(
            "2026-07-05",
            _ticket(stop_loss=None, targets=[]),
            latest_price=4186.0,
            actual_size=1,
        )
    )

    assert order.status == "submitted_to_tiger_paper"
    assert [name for name, _ in trade_client.calls] == ["get_positions", "get_open_orders", "preview_order", "place_order"]
    request = load_json(root / "tiger_order_requests" / "2026-07-05.json")[-1]
    assert request["readiness"]["protective_order_precheck"]["required"] is False
    assert request["request"]["protective_status"] == "not_requested"


def test_tiger_paper_tradeclient_blocks_nonflat_account_before_place(tmp_path: Path, monkeypatch):
    _props(monkeypatch, tmp_path)
    root = tmp_path / "outputs"
    trade_client = _FakeTigerTradeClient(positions=[{"symbol": "MGC", "quantity": 1}])
    adapter = TigerOpenApiPaperBrokerAdapter(root, _armed_config(), trade_client=trade_client, sdk=_FakeTigerSdk())

    with pytest.raises(RuntimeError, match="Tiger reconciliation blocks"):
        adapter.submit_order(BrokerOrderRequest("2026-07-05", _ticket(), latest_price=4186.0, actual_size=1))

    assert [name for name, _ in trade_client.calls] == ["get_positions", "get_open_orders"]
    request = load_json(root / "tiger_order_requests" / "2026-07-05.json")[-1]
    assert request["receipt"]["status"] == "blocked"
    assert "naked_position" in request["receipt"]["rejection_reason"]


def test_resolve_broker_profile_can_select_tiger_without_changing_default(monkeypatch, tmp_path: Path):
    _props(monkeypatch, tmp_path)
    config = {
        "execution_mode": "live",
        "live_trading_enabled": True,
        "broker": {"profile": "tiger_openapi_paper"},
        "broker_profiles": {"tiger_openapi_paper": _armed_config()},
    }

    resolved = resolve_broker_config(config)

    assert resolved["provider"] == "tiger_openapi"
    assert resolved["profile"] == "tiger_openapi_paper"


def test_build_broker_adapter_can_construct_tiger_profile(monkeypatch, tmp_path: Path):
    from services import broker_adapter as broker_adapter_module

    _props(monkeypatch, tmp_path)
    monkeypatch.setattr(
        broker_adapter_module,
        "load_pipeline_config",
        lambda: {
            "execution_mode": "live",
            "live_trading_enabled": True,
            "broker": {"profile": "tiger_openapi_paper"},
            "broker_profiles": {"tiger_openapi_paper": {**_armed_config(), "dry_run": True}},
        },
    )

    adapter = build_broker_adapter(tmp_path / "outputs")
    preflight = broker_preflight(tmp_path / "outputs")

    assert adapter.name == "tiger_openapi_paper"
    assert preflight["provider"] == "tiger_openapi"
    assert preflight["dry_run"] is True
