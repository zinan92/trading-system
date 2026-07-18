from __future__ import annotations

import ast
import json
from pathlib import Path

from services.binance_demo_broker_adapter import BinanceDemoBrokerAdapter
from services.binance_usdm_broker_adapter import BinanceUsdmBrokerAdapter
from services.binance_usdm_testnet_broker_adapter import (
    BinanceUsdmTestnetBrokerAdapter,
)
from services.broker_adapter import LiveBrokerAdapter
from services.broker_composition import BrokerBuildContext, build_broker_execution_port
from services.broker_port import BrokerExecutionPort, BrokerOrderRequest
from services.journal_store import load_json


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def _opener(request, timeout):
    assert timeout == 10
    assert request.get_method() == "GET"
    assert request.full_url.split("?", 1)[0].endswith(
        "/fapi/v1/exchangeInfo"
    )
    return _FakeResponse(
        {
            "symbols": [
                {
                    "symbol": "XAUUSDT",
                    "status": "TRADING",
                    "filters": [
                        {
                            "filterType": "PRICE_FILTER",
                            "tickSize": "0.01",
                        },
                        {
                            "filterType": "MARKET_LOT_SIZE",
                            "stepSize": "0.001",
                            "minQty": "0.001",
                        },
                        {
                            "filterType": "MIN_NOTIONAL",
                            "notional": "5",
                        },
                    ],
                }
            ]
        }
    )


def _config() -> dict:
    return {
        "provider": "binance_usdm",
        "environment": "live",
        "dry_run": True,
        "request_dir": "binance_requests",
        "allowed_symbols": ["GOLD"],
        "instrument_map": {"GOLD": "XAUUSDT"},
    }


def _request() -> BrokerOrderRequest:
    return BrokerOrderRequest(
        "2026-07-18",
        {
            "ticket_id": "ticket-binance-a15",
            "asset": "GOLD",
            "action": "prepare_buy",
            "entry_zone": "3999-4001",
            "stop_loss": 3990,
            "targets": [4010],
            "position_size_pct": 1,
            "order_type": "limit",
            "time_in_force": "day",
        },
        latest_price=4000,
        actual_size=0.01,
    )


def test_mainnet_registry_returns_concrete_binance_adapter(tmp_path: Path):
    adapter = build_broker_execution_port(
        BrokerBuildContext(
            output_root=tmp_path / "outputs",
            execution_mode="live",
            live_trading_enabled=True,
            broker_config=_config(),
            opener=_opener,
        )
    )

    assert type(adapter) is BinanceUsdmBrokerAdapter
    assert isinstance(adapter, BrokerExecutionPort)
    assert adapter.provider == "binance_usdm"
    assert adapter.capabilities.names == (
        "cancel_order",
        "preflight",
        "protective_recovery",
        "submit_order",
    )


def test_all_binance_variants_share_only_the_venue_owned_base():
    assert BinanceDemoBrokerAdapter.__bases__ == (BinanceUsdmBrokerAdapter,)
    assert BinanceUsdmTestnetBrokerAdapter.__bases__ == (
        BinanceUsdmBrokerAdapter,
    )
    assert LiveBrokerAdapter.__bases__ == (BinanceUsdmBrokerAdapter,)


def test_binance_adapter_has_no_cross_venue_or_facade_dependency():
    source = (
        Path(__file__).parents[1]
        / "services"
        / "binance_usdm_broker_adapter.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    forbidden_text = (
        "oanda",
        "mt5",
        "tiger",
        "manual_gateway",
        "BROKER_API_KEY",
        "BROKER_ACCOUNT_ID",
    )

    assert "services.broker_adapter" not in imports
    assert all(value not in source for value in forbidden_text)


def test_legacy_facade_contains_no_binance_lifecycle_implementation():
    source = (
        Path(__file__).parents[1] / "services" / "broker_adapter.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    facade = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "LiveBrokerAdapter"
    )
    method_names = {
        node.name for node in facade.body if isinstance(node, ast.FunctionDef)
    }
    forbidden_methods = {
        "_binance_preflight",
        "_submit_binance_order",
        "_live_reconciliation_check",
        "_live_money_guardrails_for_order",
        "_recover_binance_entry",
        "recover_missing_protective_orders",
        "_post_binance_order",
        "cancel_binance_order",
        "cancel_order",
        "recover_protective_orders",
        "_binance_order_payloads",
        "_binance_protective_payloads",
    }
    forbidden_text = (
        "OrderLifecycleStore",
        "canonical_live_risk_allows_exposure",
        "protective_order_response",
        "newClientOrderId",
        "reduceOnly",
    )

    assert method_names.isdisjoint(forbidden_methods)
    assert all(value not in source for value in forbidden_text)


def test_operational_binance_modules_do_not_import_legacy_facade():
    service_root = Path(__file__).parents[1] / "services"
    for filename in (
        "binance_demo_broker_adapter.py",
        "binance_usdm_testnet_broker_adapter.py",
        "binance_usdm_mainnet_kill_switch.py",
    ):
        tree = ast.parse((service_root / filename).read_text(encoding="utf-8"))
        imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        assert "services.broker_adapter" not in imports
        assert "services.binance_usdm_broker_adapter" in imports

    for filename in (
        "binance_usdm_testnet_canary.py",
        "tiger_openapi_account_sync.py",
        "tiger_openapi_broker_adapter.py",
        "tiger_openapi_kill_switch.py",
        "tiger_openapi_order_sync.py",
        "tiger_openapi_paper_order_canary.py",
        "tiger_openapi_paper_order_drill.py",
        "tiger_openapi_reconciliation.py",
    ):
        tree = ast.parse((service_root / filename).read_text(encoding="utf-8"))
        imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        assert "services.broker_adapter" not in imports


def test_legacy_facade_preserves_binance_dry_run_parity(tmp_path: Path):
    direct_root = tmp_path / "direct"
    facade_root = tmp_path / "facade"
    direct = BinanceUsdmBrokerAdapter(
        direct_root,
        True,
        dict(_config()),
        opener=_opener,
    ).submit_order(_request())
    facade = LiveBrokerAdapter(
        facade_root,
        True,
        dict(_config()),
        opener=_opener,
    ).submit_order(_request())

    assert direct.to_dict() == facade.to_dict()
    direct_record = load_json(
        direct_root / "binance_requests" / "2026-07-18.json"
    )[0]
    facade_record = load_json(
        facade_root / "binance_requests" / "2026-07-18.json"
    )[0]
    assert direct_record["request"] == facade_record["request"]
    assert direct_record["receipt"] == facade_record["receipt"]
