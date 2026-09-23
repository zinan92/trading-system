from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("nautilus_trader", reason="Testnet backend tests need the optional nautilus extra")

from standard_broker.adapters.hyperliquid.external import (
    HyperliquidTestnetBackendConfig,
    NautilusHyperliquidTestnetBackend,
    default_testnet_capabilities,
)
import standard_broker.adapters.hyperliquid.external as external_module
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
        del args
        if method == "submit_orders":
            return [
                {"order_status": "OPEN", "venue_order_id": "1001", "client_order_id": "tp"},
                {"order_status": "OPEN", "venue_order_id": "1002", "client_order_id": "sl"},
            ]
        if method == "request_order_status_report":
            return {
                "order_status": "OPEN",
                "venue_order_id": str(kwargs.get("venue_order_id") or ""),
                "client_order_id": str(kwargs.get("client_order_id") or ""),
                "quantity": "0.001",
            }
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


def _recovered_orders() -> list[object]:
    from nautilus_trader.core import nautilus_pyo3

    request = _supported_request()
    reports = []
    for index, leg in enumerate(request["legs"]):
        assert isinstance(leg, dict)
        sbp_id = NautilusHyperliquidTestnetBackend._protection_client_id(
            request["protectionId"], index, leg, quantity=request["quantity"]
        )
        reports.append(
            nautilus_pyo3.OrderStatusReport(
                nautilus_pyo3.AccountId("HYPERLIQUID-001"),
                nautilus_pyo3.InstrumentId.from_str("BTC-USD-PERP.HYPERLIQUID"),
                nautilus_pyo3.VenueOrderId(str(59821879831 + index)),
                nautilus_pyo3.OrderSide.SELL,
                nautilus_pyo3.OrderType.MARKET,
                nautilus_pyo3.TimeInForce.GTC,
                nautilus_pyo3.OrderStatus.ACCEPTED,
                nautilus_pyo3.Quantity.from_str("0.00024"),
                nautilus_pyo3.Quantity.from_str("0"),
                1,
                2,
                3,
                client_order_id=nautilus_pyo3.ClientOrderId(
                    NautilusHyperliquidTestnetBackend._native_protection_cloid(sbp_id)
                ),
                trigger_price=nautilus_pyo3.Price.from_str(str(leg["triggerPx"])),
                trigger_type=nautilus_pyo3.TriggerType.MARK_PRICE,
                reduce_only=True,
            )
        )
    return reports


@pytest.mark.parametrize(
    ("orders", "reason"),
    [(_recovered_orders(), None), (_recovered_orders()[:1], "protection_submit_partial"), ([], "protection_submit_unconfirmed")],
)
def test_empty_protection_submit_recovers_or_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    orders: list[object],
    reason: str | None,
) -> None:
    backend = _backend(tmp_path)
    backend.activate(release_sha="a" * 40)
    backend._build_protection_orders = lambda request: ["tp-order", "sl-order"]
    backend._instrument = lambda request: SimpleNamespace(id="BTC-USD-PERP.HYPERLIQUID")
    monkeypatch.setattr(external_module, "sleep", lambda seconds: None)
    calls: list[tuple[str, dict[str, object]]] = []

    def call(method: str, *args, **kwargs):
        calls.append((method, dict(kwargs)))
        del args
        if method == "submit_orders":
            return []
        if method == "request_order_status_reports":
            return orders
        if method == "request_order_status_report" and kwargs.get("venue_order_id") is None:
            return None
        if method == "cancel_order":
            return {"status": "ok"}
        if method == "request_order_status_report":
            return {
                "order_status": "CANCELED",
                "venue_order_id": kwargs.get("venue_order_id"),
                "client_order_id": kwargs.get("client_order_id"),
                "quantity": "0.001",
            }
        raise AssertionError(method)

    backend._call = call
    if reason is None:
        submitted = backend.invoke("protection_order", "submit", _supported_request())
        assert submitted["state"] == "submitted"
        assert submitted["order_ids"] == ["59821879831", "59821879832"]
        assert backend._protection_orders["dca-protection:1"]["rows"][0]["cloid"].startswith("0x")
        backend.invoke("protection_order", "cancel", {"protectionId": "dca-protection:1"})
        cancel_calls = [kwargs for method, kwargs in calls if method == "cancel_order"]
        assert {str(kwargs["client_order_id"]) for kwargs in cancel_calls} == {
            NautilusHyperliquidTestnetBackend._native_protection_cloid(
                NautilusHyperliquidTestnetBackend._protection_client_id(
                    "dca-protection:1", 0, _supported_request()["legs"][0], quantity="0.001"
                )
            ),
            NautilusHyperliquidTestnetBackend._native_protection_cloid(
                NautilusHyperliquidTestnetBackend._protection_client_id(
                    "dca-protection:1", 1, _supported_request()["legs"][1], quantity="0.001"
                )
            ),
        }
    else:
        with pytest.raises(RuntimeBoundaryError) as raised:
            backend.invoke("protection_order", "submit", _supported_request())
        assert raised.value.reason_code == reason
        assert "recovery evidence" in str(raised.value)
        if orders:
            assert "59821879831" in str(raised.value)


def _clone_status_report(
    report: object,
    *,
    trigger: str | None = None,
    cloid: str | None = None,
    reduce_only: bool | None = None,
) -> object:
    from nautilus_trader.core import nautilus_pyo3

    return nautilus_pyo3.OrderStatusReport(
        report.account_id,
        report.instrument_id,
        report.venue_order_id,
        report.order_side,
        report.order_type,
        report.time_in_force,
        report.order_status,
        report.quantity,
        report.filled_qty,
        report.ts_accepted,
        report.ts_last,
        report.ts_init,
        client_order_id=nautilus_pyo3.ClientOrderId(cloid) if cloid else report.client_order_id,
        trigger_price=nautilus_pyo3.Price.from_str(trigger) if trigger else report.trigger_price,
        trigger_type=report.trigger_type,
        reduce_only=report.reduce_only if reduce_only is None else reduce_only,
    )


def test_protection_recovery_uses_sbp_then_venue_identity_and_ignores_foreign_quantity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _backend(tmp_path)
    backend.activate(release_sha="a" * 40)
    backend._build_protection_orders = lambda request: ["tp-order", "sl-order"]
    backend._instrument = lambda request: SimpleNamespace(id="BTC-USD-PERP.HYPERLIQUID")
    monkeypatch.setattr(external_module, "sleep", lambda seconds: None)
    reports = _recovered_orders()
    request = _supported_request()
    sbp_ids = [
        backend._protection_client_id("dca-protection:1", i, leg, quantity="0.001")
        for i, leg in enumerate(request["legs"])
    ]
    venue_ids = [backend._native_protection_cloid(value) for value in sbp_ids]
    foreign = _clone_status_report(reports[0], cloid="0x" + "f" * 32)

    def call(method: str, *args, **kwargs):
        del args
        if method == "submit_orders":
            return []
        if method == "request_order_status_report":
            lookup = str(kwargs.get("client_order_id") or "")
            if lookup == sbp_ids[0]:
                return reports[0]
            if lookup == venue_ids[1]:
                return reports[1]
            return None
        if method == "request_order_status_reports":
            return [foreign]
        raise AssertionError(method)

    backend._call = call
    submitted = backend.invoke("protection_order", "submit", request)

    assert submitted["state"] == "submitted"
    assert submitted["order_ids"] == ["59821879831", "59821879832"]
    assert [row["cloid"] for row in backend._protection_orders["dca-protection:1"]["rows"]] == venue_ids


@pytest.mark.parametrize("bad_kind", ["duplicate", "trigger", "reduce_only"])
def test_protection_recovery_identity_conflicts_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_kind: str
) -> None:
    backend = _backend(tmp_path)
    backend.activate(release_sha="a" * 40)
    backend._build_protection_orders = lambda request: ["tp-order", "sl-order"]
    backend._instrument = lambda request: SimpleNamespace(id="BTC-USD-PERP.HYPERLIQUID")
    monkeypatch.setattr(external_module, "sleep", lambda seconds: None)
    reports = _recovered_orders()
    listed = list(reports)
    if bad_kind == "duplicate":
        listed.append(reports[0])
    elif bad_kind == "trigger":
        listed[0] = _clone_status_report(reports[0], trigger="62000")
    else:
        listed[0] = _clone_status_report(reports[0], reduce_only=False)

    def call(method: str, *args, **kwargs):
        del args, kwargs
        if method == "submit_orders":
            return []
        if method == "request_order_status_report":
            return None
        if method == "request_order_status_reports":
            return listed
        raise AssertionError(method)

    backend._call = call
    with pytest.raises(RuntimeBoundaryError) as raised:
        backend.invoke("protection_order", "submit", _supported_request())

    assert raised.value.reason_code == "protection_recovery_conflict"
    assert (
        "trigger_px" in str(raised.value)
        or "reduce_only" in str(raised.value)
        or "multiple_list_reports" in str(raised.value)
    )


def test_external_protection_accepts_group_level_submit_report_and_queries_child_cloids(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    backend.activate(release_sha="a" * 40)
    backend._build_protection_orders = lambda request: ["tp-order", "sl-order"]
    backend._instrument = lambda request: SimpleNamespace(id="BTC-USD-PERP.HYPERLIQUID")

    def call(method: str, *args, **kwargs):
        del args
        if method == "submit_orders":
            return [{"order_status": "OPEN", "venue_order_id": "group-1"}]
        if method == "request_order_status_report":
            return {
                "order_status": "OPEN",
                "venue_order_id": "child-1",
                "client_order_id": str(kwargs.get("client_order_id") or ""),
                "quantity": "0.001",
            }
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


def test_external_protection_query_identity_conflict_is_unknown_and_uncovered(tmp_path: Path) -> None:
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
            return {
                "order_status": "OPEN",
                "venue_order_id": "wrong-order",
                "client_order_id": "wrong-cloid",
                "quantity": "0.001",
            }
        raise AssertionError(method)

    backend._call = call
    backend.invoke("protection_order", "submit", _request())
    queried = backend.invoke(
        "protection_order",
        "query",
        {"protectionId": "dca-protection:1"},
    )

    assert queried["state"] == "unknown"
    assert queried["accepted"] is False
    assert queried["covered_quantity"] == "0"


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
