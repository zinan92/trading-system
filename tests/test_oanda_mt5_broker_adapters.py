from __future__ import annotations

import ast
import hashlib
import json
import urllib.parse
from pathlib import Path

import pytest

from services.broker_adapter import LiveBrokerAdapter
from services.broker_composition import BrokerBuildContext, build_broker_execution_port
from services.broker_port import BrokerExecutionPort, BrokerOrderRequest
from services.journal_store import load_json, write_json
from services.mt5_file_bridge_broker_adapter import Mt5FileBridgeBrokerAdapter
from services.oanda_rest_broker_adapter import OandaRestBrokerAdapter


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def _ticket(ticket_id: str = "ticket-gold-a14") -> dict:
    return {
        "ticket_id": ticket_id,
        "asset": "GOLD",
        "action": "prepare_buy",
        "entry_zone": "3999-4001",
        "stop_loss": 3990,
        "targets": [4010],
        "position_size_pct": 1,
        "order_type": "limit",
        "time_in_force": "day",
        "manual_execution_required": True,
    }


def _request(ticket_id: str = "ticket-gold-a14") -> BrokerOrderRequest:
    return BrokerOrderRequest(
        "2026-07-18",
        _ticket(ticket_id),
        latest_price=4000,
        actual_size=0.25,
    )


def _context(
    tmp_path: Path,
    *,
    provider: str,
    broker_config: dict | None = None,
    opener=None,
) -> BrokerBuildContext:
    return BrokerBuildContext(
        output_root=tmp_path / "outputs",
        execution_mode="live",
        live_trading_enabled=True,
        broker_config={
            "provider": provider,
            "dry_run": True,
            **(broker_config or {}),
        },
        opener=opener,
    )


@pytest.mark.parametrize(
    ("provider", "expected_type", "expected_name"),
    [
        ("oanda_rest", OandaRestBrokerAdapter, "oanda_rest"),
        ("mt5_file_bridge", Mt5FileBridgeBrokerAdapter, "mt5_file_bridge"),
        ("manual_gateway", LiveBrokerAdapter, "live"),
    ],
)
def test_registry_builds_concrete_venue_adapters(
    tmp_path: Path,
    provider: str,
    expected_type: type,
    expected_name: str,
):
    adapter = build_broker_execution_port(_context(tmp_path, provider=provider))

    assert type(adapter) is expected_type
    assert isinstance(adapter, BrokerExecutionPort)
    assert adapter.name == expected_name
    assert adapter.capabilities.names == ("preflight", "submit_order")
    assert adapter.descriptor.provider == provider


def test_oanda_real_submission_preserves_wire_contract_and_redacts_bearer_token(
    tmp_path: Path,
    monkeypatch,
):
    output_root = tmp_path / "outputs"
    token = "super-secret-oanda-token"
    account_id = "acct /#?"
    monkeypatch.setenv("A14_OANDA_TOKEN", token)
    monkeypatch.setenv("A14_OANDA_ACCOUNT", account_id)
    write_json(
        output_root / "live_activation" / "2026-07-18.json",
        [{"status": "real_money_ready", "real_money_ready": True}],
    )
    seen: dict = {}

    def opener(request, timeout):
        seen.update(
            {
                "method": request.get_method(),
                "url": request.full_url,
                "headers": dict(request.header_items()),
                "body": json.loads(request.data.decode("utf-8")),
                "timeout": timeout,
            }
        )
        return _FakeResponse(
            {
                "orderCreateTransaction": {
                    "id": "broker-order-42",
                    "accountID": account_id,
                },
                "lastTransactionID": "42",
            }
        )

    adapter = OandaRestBrokerAdapter(
        output_root,
        True,
        {
            "provider": "oanda_rest",
            "environment": "practice",
            "dry_run": False,
            "api_key_env": "A14_OANDA_TOKEN",
            "account_id_env": "A14_OANDA_ACCOUNT",
            "request_dir": "oanda_requests",
            "timeout_seconds": 17,
        },
        opener=opener,
    )

    receipt = adapter.submit_order(_request())

    expected_hash = hashlib.sha256(
        b"ticket-gold-a14:2026-07-18:oanda_rest:entry"
    ).hexdigest()[:10]
    assert receipt.order_id == f"live_dryrun_{expected_hash}"
    assert receipt.status == "submitted_to_oanda"
    assert receipt.rejection_reason == "broker-order-42"
    assert seen == {
        "method": "POST",
        "url": (
            "https://api-fxpractice.oanda.com/v3/accounts/"
            f"{urllib.parse.quote(account_id, safe='')}/orders"
        ),
        "headers": {
            "Authorization": f"Bearer {token}",
            "Accept-datetime-format": "RFC3339",
            "Content-type": "application/json",
            "User-agent": "TradingOrchestrator/1.0",
        },
        "body": {
            "order": {
                "type": "LIMIT",
                "instrument": "XAU_USD",
                "units": "0.25",
                "timeInForce": "GFD",
                "positionFill": "DEFAULT",
                "clientExtensions": {
                    "id": receipt.order_id,
                    "tag": "trading_orchestrator",
                    "comment": "ticket-gold-a14",
                },
                "price": "4000",
                "stopLossOnFill": {"price": "3990"},
                "takeProfitOnFill": {"price": "4010"},
            }
        },
        "timeout": 17,
    }
    durable_text = (
        output_root / "oanda_requests" / "2026-07-18.json"
    ).read_text(encoding="utf-8")
    assert token not in durable_text
    assert token not in json.dumps(adapter.descriptor.to_dict(), sort_keys=True)
    durable_record = load_json(
        output_root / "oanda_requests" / "2026-07-18.json"
    )[0]
    assert durable_record["broker_response"]["orderCreateTransaction"][
        "accountID"
    ] == account_id


@pytest.mark.parametrize("with_credentials", [False, True])
def test_oanda_fail_closed_gates_run_before_network(
    tmp_path: Path,
    monkeypatch,
    with_credentials: bool,
):
    calls = 0

    def forbidden_opener(request, timeout):
        nonlocal calls
        calls += 1
        raise AssertionError("network must remain blocked")

    if with_credentials:
        monkeypatch.setenv("A14_OANDA_TOKEN", "token")
        monkeypatch.setenv("A14_OANDA_ACCOUNT", "account")
    else:
        monkeypatch.delenv("A14_OANDA_TOKEN", raising=False)
        monkeypatch.delenv("A14_OANDA_ACCOUNT", raising=False)
    adapter = OandaRestBrokerAdapter(
        tmp_path / "outputs",
        True,
        {
            "provider": "oanda_rest",
            "dry_run": False,
            "api_key_env": "A14_OANDA_TOKEN",
            "account_id_env": "A14_OANDA_ACCOUNT",
        },
        opener=forbidden_opener,
    )

    expected = (
        "live activation gate is not real_money_ready"
        if with_credentials
        else "missing OANDA environment variables"
    )
    with pytest.raises(RuntimeError, match=expected):
        adapter.submit_order(_request())

    assert calls == 0


def test_mt5_activation_gate_never_writes_an_executable_order(
    tmp_path: Path,
):
    output_root = tmp_path / "outputs"
    outbox = tmp_path / "outbox"
    adapter = Mt5FileBridgeBrokerAdapter(
        output_root,
        True,
        {
            "provider": "mt5_file_bridge",
            "dry_run": False,
            "outbox_dir": str(outbox),
            "inbox_dir": str(tmp_path / "inbox"),
            "request_dir": "mt5_requests",
        },
    )

    with pytest.raises(
        RuntimeError,
        match="live activation gate is not real_money_ready",
    ):
        adapter.submit_order(_request())

    assert list(outbox.glob("*.json")) == []
    assert not (output_root / "mt5_requests" / "2026-07-18.json").exists()


def test_mt5_dry_run_writes_explicit_non_executable_artifact(tmp_path: Path):
    output_root = tmp_path / "outputs"
    outbox = tmp_path / "outbox"
    adapter = Mt5FileBridgeBrokerAdapter(
        output_root,
        True,
        {
            "provider": "mt5_file_bridge",
            "dry_run": True,
            "outbox_dir": str(outbox),
            "inbox_dir": str(tmp_path / "inbox"),
            "request_dir": "mt5_requests",
        },
    )

    receipt = adapter.submit_order(_request("ticket-mt5-a14"))

    files = list(outbox.glob("*.json"))
    assert len(files) == 1
    assert receipt.status == "bridge_dry_run"
    assert load_json(files[0])["dry_run"] is True
    assert load_json(files[0])["order_id"] == receipt.order_id


def test_legacy_facade_rebuilds_delegate_when_runtime_wiring_changes(
    tmp_path: Path,
):
    config = {"provider": "oanda_rest", "dry_run": True}
    facade = LiveBrokerAdapter(tmp_path / "outputs", True, config)

    first = facade._oanda_adapter()
    facade.live_trading_enabled = False
    second = facade._oanda_adapter()
    facade.opener = object()
    third = facade._oanda_adapter()
    facade.broker_config = {**config, "environment": "live"}
    fourth = facade._oanda_adapter()

    assert first is not second
    assert second.live_trading_enabled is False
    assert second is not third
    assert third.opener is facade.opener
    assert third is not fourth
    assert fourth.broker_config is facade.broker_config
    assert fourth._base_url() == "https://api-fxtrade.oanda.com"


def test_legacy_facade_and_concrete_adapters_keep_dry_run_receipt_parity(
    tmp_path: Path,
):
    request = _request()
    oanda_config = {
        "provider": "oanda_rest",
        "dry_run": True,
        "request_dir": "requests",
    }
    direct_oanda = OandaRestBrokerAdapter(
        tmp_path / "direct_oanda",
        True,
        oanda_config,
    ).submit_order(request)
    facade_oanda = LiveBrokerAdapter(
        tmp_path / "facade_oanda",
        True,
        dict(oanda_config),
    ).submit_order(request)
    mt5_direct_outbox = tmp_path / "direct_mt5_outbox"
    mt5_facade_outbox = tmp_path / "facade_mt5_outbox"
    direct_mt5 = Mt5FileBridgeBrokerAdapter(
        tmp_path / "direct_mt5",
        True,
        {
            "provider": "mt5_file_bridge",
            "dry_run": True,
            "outbox_dir": str(mt5_direct_outbox),
            "inbox_dir": str(tmp_path / "direct_mt5_inbox"),
        },
    ).submit_order(request)
    facade_mt5 = LiveBrokerAdapter(
        tmp_path / "facade_mt5",
        True,
        {
            "provider": "mt5_file_bridge",
            "dry_run": True,
            "outbox_dir": str(mt5_facade_outbox),
            "inbox_dir": str(tmp_path / "facade_mt5_inbox"),
        },
    ).submit_order(request)

    assert direct_oanda.to_dict() == facade_oanda.to_dict()
    assert direct_mt5.to_dict() == facade_mt5.to_dict()


def test_legacy_facade_contains_no_oanda_or_mt5_provider_io_implementation():
    facade_source = (
        Path(__file__).parents[1] / "services" / "broker_adapter.py"
    ).read_text(encoding="utf-8")
    forbidden = (
        "OANDA_API_TOKEN",
        "OANDA_ACCOUNT_ID",
        "api-fxtrade.oanda.com",
        "api-fxpractice.oanda.com",
        "/v3/accounts/",
        "ORDER_REQUEST.json.template",
        "ORDER_RECEIPT.json.template",
        "data/broker_outbox/mt5",
        "data/broker_inbox/mt5",
    )

    assert all(value not in facade_source for value in forbidden)
    tree = ast.parse(facade_source)
    facade = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "LiveBrokerAdapter"
    )
    methods = {
        node.name: node
        for node in facade.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    thin_delegates = {
        "_oanda_preflight": "self._oanda_adapter().preflight()",
        "_mt5_file_bridge_preflight": "self._mt5_adapter().preflight()",
        "_record_mt5_file_bridge_request": (
            "self._mt5_adapter()._submit_with_readiness(request, readiness)"
        ),
        "_submit_oanda_order": (
            "self._oanda_adapter()._submit_with_readiness(request, readiness)"
        ),
        "_oanda_order_payload": (
            "self._oanda_adapter()._order_payload(ticket, order_id, "
            "requested_price, quantity)"
        ),
        "_post_oanda_order": "self._oanda_adapter()._post_order(payload)",
        "_oanda_base_url": "self._oanda_adapter()._base_url()",
        "_oanda_instrument": "self._oanda_adapter()._instrument(asset)",
        "_oanda_time_in_force": (
            "self._oanda_adapter()._time_in_force(order_type, raw)"
        ),
        "_format_price": "self._oanda_adapter()._format_price(value)",
        "_mt5_outbox_dir": "self._mt5_adapter()._outbox_dir()",
        "_mt5_inbox_dir": "self._mt5_adapter()._inbox_dir()",
        "_ensure_mt5_bridge_docs": (
            "self._mt5_adapter()._ensure_bridge_docs(outbox_dir, inbox_dir)"
        ),
    }
    for method_name, expected_call in thin_delegates.items():
        body = methods[method_name].body
        assert len(body) == 1
        assert isinstance(body[0], ast.Return)
        assert ast.unparse(body[0].value) == expected_call


def test_operational_consumers_resolve_adapters_through_composition():
    service_root = Path(__file__).parents[1] / "services"
    for filename in (
        "mt5_bridge_smoke.py",
        "live_submission_safety.py",
        "completion_audit.py",
    ):
        source = (service_root / filename).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        imported_names = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        assert "services.broker_adapter" not in imports
        assert "build_broker_execution_port" in imported_names
