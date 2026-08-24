from pathlib import Path
from types import SimpleNamespace

import pytest

from standard_broker.adapters.hyperliquid.external import (
    HyperliquidTestnetBackendConfig,
    NautilusHyperliquidTestnetBackend,
    default_testnet_capabilities,
)
from standard_broker.adapters.hyperliquid.credentials import LocalFileSecretProvider
from standard_broker.capabilities import CapabilityDescriptor
from standard_broker.errors import RuntimeBoundaryError
from standard_broker.models import AccountScope, BrokerEnvironment, SignerKind
from standard_broker.runtime import AccountReference, BrokerRuntimeSession, SignerReference


ACCOUNT = "0x" + "11" * 20
REFERENCE = "file-secret://hyperliquid-testnet"


class FakeSignerProvider:
    def sign(self, signer: SignerReference, payload: bytes) -> bytes:
        return payload


def _session() -> BrokerRuntimeSession:
    base = default_testnet_capabilities()
    operations = dict(base.operations)
    operations["protection_order"] = frozenset(
        {"submit", "cancel", "replace", "query"}
    )
    capabilities = CapabilityDescriptor(
        broker_id=base.broker_id,
        environment=base.environment,
        operations=operations,
        revision="hyperliquid-testnet-protection-runtime-v1",
    )
    return BrokerRuntimeSession(
        broker_id="hyperliquid",
        environment=BrokerEnvironment.TESTNET,
        account=AccountReference(AccountScope.MASTER, ACCOUNT),
        signer=SignerReference(SignerKind.API_AGENT, "fixture", REFERENCE),
        signer_provider=FakeSignerProvider(),
        capabilities=capabilities,
        execution_scope="hypercore:default",
        lifecycle_id="protection-runtime-1",
    )


def _backend(tmp_path: Path) -> NautilusHyperliquidTestnetBackend:
    session = _session()
    return NautilusHyperliquidTestnetBackend(
        session=session,
        config=HyperliquidTestnetBackendConfig(
            account_address=ACCOUNT,
            capability_revision=session.capabilities.revision,
            capabilities=session.capabilities,
        ),
        secrets=LocalFileSecretProvider({REFERENCE: tmp_path / "key"}),
    )


def _request() -> dict[str, object]:
    return {
        "protectionId": "dca-protection:1",
        "instrumentId": "BTC-USD-PERP",
        "grouping": "positionTpsl",
        "quantity": "0.001",
        "quantityPolicy": "position_following",
        "legs": [
            {
                "side": "A",
                "tpsl": "tp",
                "execution": "market",
                "triggerPx": "61000",
                "reduceOnly": True,
                "triggerReference": "mark",
            },
            {
                "side": "A",
                "tpsl": "sl",
                "execution": "market",
                "triggerPx": "59000",
                "reduceOnly": True,
                "triggerReference": "mark",
            },
        ],
    }


def _supported_request() -> dict[str, object]:
    request = _request()
    request["legs"] = [
        {
            "side": "A",
            "tpsl": "tp",
            "execution": "limit",
            "triggerPx": "61000",
            "limitPx": "60950",
            "reduceOnly": True,
            "triggerReference": "mark",
        },
        {
            "side": "A",
            "tpsl": "sl",
            "execution": "market",
            "triggerPx": "59000",
            "reduceOnly": True,
            "triggerReference": "mark",
        },
    ]
    return request


def test_pinned_nautilus_public_protection_submit_accepts_tp_limit_and_sl_market(
    tmp_path: Path,
) -> None:
    pytest.importorskip("nautilus_trader")
    backend = _backend(tmp_path)
    backend.activate(release_sha="a" * 40)

    captured: dict[str, object] = {}

    def call(method: str, *args, **kwargs):
        del kwargs
        if method == "submit_orders":
            captured["orders"] = args[0]
            return [{"order_status": "OPEN", "venue_order_id": "group-1"}]
        raise AssertionError(method)

    backend._call = call
    submitted = backend.invoke("protection_order", "submit", _supported_request())
    orders = captured["orders"]

    assert submitted["state"] == "submitted"
    assert isinstance(orders, list)
    assert len(orders) == 2
    assert all(type(order).__module__.startswith("nautilus_trader") for order in orders)
    assert [str(order.trigger_type) for order in orders] == ["MARK_PRICE", "MARK_PRICE"]
    assert all(order.is_reduce_only for order in orders)
    assert orders[0].linked_order_ids == [orders[1].client_order_id]
    assert orders[1].linked_order_ids == [orders[0].client_order_id]


def test_backend_rejects_tp_market_before_conversion(tmp_path: Path) -> None:
    pytest.importorskip("nautilus_trader")
    backend = _backend(tmp_path)

    with pytest.raises(RuntimeBoundaryError, match="take-profit market"):
        backend._build_protection_orders(_request())


def test_external_protection_submit_and_query_are_redacted_and_cursorable(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    backend.activate(release_sha="a" * 40)
    backend._build_protection_orders = lambda request: ["tp-order", "sl-order"]
    backend._instrument = lambda request: SimpleNamespace(id="BTC-USD-PERP.HYPERLIQUID")

    def call(method: str, *args, **kwargs):
        del args, kwargs
        if method == "submit_orders":
            return [
                {"order_status": "OPEN", "venue_order_id": "1001", "client_order_id": "tp"},
                {"order_status": "OPEN", "venue_order_id": "1002", "client_order_id": "sl"},
            ]
        if method == "request_order_status_report":
            return {"order_status": "OPEN", "venue_order_id": "1001", "client_order_id": "tp"}
        raise AssertionError(method)

    backend._call = call
    submitted = backend.invoke("protection_order", "submit", _request())
    queried = backend.invoke(
        "protection_order",
        "query",
        {"protectionId": "dca-protection:1"},
    )

    assert submitted["state"] == "submitted"
    assert submitted["covered_quantity"] == "0"
    assert submitted["observation_digest"].startswith("sha256:")
    assert queried["state"] == "active"
    assert queried["covered_quantity"] == "0.001"
    assert "reduceOnly" not in str(submitted)


def test_external_protection_accepts_group_level_submit_report_and_queries_child_cloids(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    backend.activate(release_sha="a" * 40)
    backend._build_protection_orders = lambda request: ["tp-order", "sl-order"]
    backend._instrument = lambda request: SimpleNamespace(id="BTC-USD-PERP.HYPERLIQUID")

    def call(method: str, *args, **kwargs):
        del args, kwargs
        if method == "submit_orders":
            return [{"order_status": "OPEN", "venue_order_id": "group-1"}]
        if method == "request_order_status_report":
            return {"order_status": "OPEN", "venue_order_id": "child-1", "client_order_id": "child"}
        raise AssertionError(method)

    backend._call = call
    submitted = backend.invoke("protection_order", "submit", _request())
    queried = backend.invoke(
        "protection_order",
        "query",
        {"protectionId": "dca-protection:1"},
    )

    assert submitted["state"] == "submitted"
    assert queried["state"] == "active"
    assert queried["covered_quantity"] == "0.001"


def test_external_protection_rejects_group_level_rejected_report(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    backend.activate(release_sha="a" * 40)
    backend._build_protection_orders = lambda request: ["tp-order", "sl-order"]

    backend._call = lambda method, *args, **kwargs: (
        [{"order_status": "REJECTED", "cancel_reason": "bad trigger"}]
        if method == "submit_orders"
        else None
    )

    with pytest.raises(RuntimeBoundaryError, match="group_submit_rejected"):
        backend.invoke("protection_order", "submit", _request())


def test_external_protection_query_transport_error_is_unknown_and_uncovered(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    backend.activate(release_sha="a" * 40)
    backend._build_protection_orders = lambda request: ["tp-order", "sl-order"]
    backend._instrument = lambda request: SimpleNamespace(id="BTC-USD-PERP.HYPERLIQUID")
    calls = {"status": 0}

    def call(method: str, *args, **kwargs):
        del args, kwargs
        if method == "submit_orders":
            return [
                {"order_status": "OPEN", "venue_order_id": "1001", "client_order_id": "tp"},
                {"order_status": "OPEN", "venue_order_id": "1002", "client_order_id": "sl"},
            ]
        if method == "request_order_status_report":
            calls["status"] += 1
            raise TimeoutError("status unavailable")
        raise AssertionError(method)

    backend._call = call
    backend.invoke("protection_order", "submit", _request())
    queried = backend.invoke(
        "protection_order",
        "query",
        {"protectionId": "dca-protection:1"},
    )

    assert calls["status"] == 2
    assert queried["state"] == "unknown"
    assert queried["covered_quantity"] == "0"
