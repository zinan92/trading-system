from __future__ import annotations

import ast
from pathlib import Path

from services.broker_adapter import LiveBrokerAdapter
from services.broker_composition import BrokerBuildContext, build_broker_execution_port
from services.broker_port import BrokerExecutionPort, BrokerOrderRequest
from services.journal_store import load_json
from services.tiger_openapi_broker_adapter import TigerOpenApiPaperBrokerAdapter


def _config(tmp_path: Path) -> dict:
    return {
        "provider": "tiger_openapi",
        "environment": "paper",
        "dry_run": True,
        "request_dir": "tiger_requests",
        "props_path_env": "A15_TIGER_PROPS",
        "allowed_symbols": ["MGC2608"],
        "contract_map": {"MGC2608": "MGC2608"},
    }


def _request() -> BrokerOrderRequest:
    return BrokerOrderRequest(
        "2026-07-18",
        {
            "ticket_id": "ticket-tiger-a15",
            "asset": "MGC2608",
            "action": "prepare_buy",
            "entry_zone": "4180-4190",
            "stop_loss": 4160,
            "targets": [4210],
            "position_size_pct": 1,
            "order_type": "limit",
            "time_in_force": "day",
        },
        latest_price=4186,
        actual_size=1,
    )


def _install_props(tmp_path: Path, monkeypatch) -> None:
    props = tmp_path / "tiger.properties"
    props.write_text("tiger_id=placeholder\n", encoding="utf-8")
    props.chmod(0o600)
    monkeypatch.setenv("A15_TIGER_PROPS", str(props))
    monkeypatch.setenv(
        "TRADING_ORCHESTRATOR_LIVE_ENV",
        str(tmp_path / "missing-live.env"),
    )


def test_tiger_adapter_is_a_standalone_broker_port(tmp_path: Path):
    adapter = TigerOpenApiPaperBrokerAdapter(
        tmp_path / "outputs",
        _config(tmp_path),
    )

    assert isinstance(adapter, BrokerExecutionPort)
    assert adapter.__class__.__bases__ == (object,)
    assert adapter.capabilities.names == ("preflight", "submit_order")
    assert adapter.descriptor.adapter_name == "tiger_openapi_paper"
    assert adapter.descriptor.provider == "tiger_openapi"


def test_registry_returns_the_standalone_tiger_adapter(tmp_path: Path):
    adapter = build_broker_execution_port(
        BrokerBuildContext(
            output_root=tmp_path / "outputs",
            execution_mode="live",
            live_trading_enabled=True,
            broker_config=_config(tmp_path),
        )
    )

    assert type(adapter) is TigerOpenApiPaperBrokerAdapter


def test_tiger_module_has_no_legacy_facade_dependency():
    source = (
        Path(__file__).parents[1]
        / "services"
        / "tiger_openapi_broker_adapter.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    adapter_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name == "TigerOpenApiPaperBrokerAdapter"
    )

    assert "services.broker_adapter" not in imports
    assert adapter_class.bases == []


def test_legacy_facade_tiger_methods_are_structural_delegates():
    source = (
        Path(__file__).parents[1] / "services" / "broker_adapter.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    facade = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "LiveBrokerAdapter"
    )
    methods = {
        node.name: node
        for node in facade.body
        if isinstance(node, ast.FunctionDef)
    }
    expected = {
        "_tiger_preflight": "self._tiger_adapter()._tiger_preflight()",
        "_submit_tiger_order": (
            "self._tiger_adapter()._submit_tiger_order(request, readiness)"
        ),
        "_tiger_contract_quantity": (
            "self._tiger_adapter()._tiger_contract_quantity(raw_quantity)"
        ),
        "_tiger_order_payload": (
            "self._tiger_adapter()._tiger_order_payload(ticket, order_id, "
            "requested_price, quantity)"
        ),
    }
    for method_name, expected_call in expected.items():
        body = methods[method_name].body
        assert len(body) == 1
        assert isinstance(body[0], ast.Return)
        assert ast.unparse(body[0].value) == expected_call

    forbidden = (
        "TIGER_OPENAPI_CONFIG_PATH",
        "tiger_tradeclient_future_order",
        "positive whole-contract integer",
        "chmod 600",
    )
    assert all(value not in source for value in forbidden)


def test_legacy_facade_preserves_tiger_dry_run_parity(
    tmp_path: Path,
    monkeypatch,
):
    _install_props(tmp_path, monkeypatch)
    config = _config(tmp_path)
    direct_root = tmp_path / "direct"
    facade_root = tmp_path / "facade"

    direct = TigerOpenApiPaperBrokerAdapter(
        direct_root,
        dict(config),
    ).submit_order(_request())
    facade = LiveBrokerAdapter(
        facade_root,
        True,
        dict(config),
    ).submit_order(_request())

    assert direct.to_dict() == facade.to_dict()
    direct_record = load_json(
        direct_root / "tiger_requests" / "2026-07-18.json"
    )[0]
    facade_record = load_json(
        facade_root / "tiger_requests" / "2026-07-18.json"
    )[0]
    assert direct_record["request"] == facade_record["request"]
    assert direct_record["receipt"] == facade_record["receipt"]


def test_legacy_facade_refreshes_tiger_delegate_after_top_level_config_mutation(
    tmp_path: Path,
):
    config = _config(tmp_path)
    facade = LiveBrokerAdapter(tmp_path / "outputs", True, config)

    first = facade._tiger_adapter()
    config["allowed_symbols"] = ["MGC2609"]
    second = facade._tiger_adapter()
    facade.live_trading_enabled = False
    third = facade._tiger_adapter()

    assert first is not second
    assert second.broker_config["allowed_symbols"] == ["MGC2609"]
    assert second is not third
    assert third.live_trading_enabled is False
