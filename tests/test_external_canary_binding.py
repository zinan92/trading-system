from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from standard_broker.external_canary import (
    ExternalCanaryBinding,
    ExternalCanaryFactBundle,
    HyperliquidExternalSnapshotReader,
)
from standard_broker.models import BrokerEnvironment
from standard_broker.models import Provenance
from standard_broker.external_host import ExternalBrokerHost, ExternalFactEnvelope, ExternalHostRequest
from standard_broker.host import CanonicalHostRequest, CanonicalPortQuery
from standard_broker.orders import OrderState
from standard_broker.adapters.hyperliquid import (
    HyperliquidInstrumentAdapter,
    NautilusHyperliquidRuntime,
    NautilusRuntimeConfig,
    build_hyperliquid_testnet_canary_binding,
    build_hyperliquid_testnet_canary_binding_from_runtime,
    build_hyperliquid_testnet_protected_canary_binding_from_runtime,
)
from standard_broker.adapters.hyperliquid.external import (
    NAUTILUS_HYPERLIQUID_COMMIT,
    NAUTILUS_HYPERLIQUID_VERSION,
    default_testnet_capabilities,
    enabled_testnet_position_protection_capabilities,
)
from standard_broker.runtime import ExternalEnvironmentApproval, RuntimeActivationPolicy
from standard_broker.models import BrokerEnvironment
from standard_broker.runtime_facts import RuntimeFactLedger
from standard_broker.orders import OrderFill

from test_external_order_lifecycle import ExternalOrderBackend, LifecycleStub, META, NOW, _intent
from test_external_host import RELEASE_SHA
from test_external_profile import _profile_context, _profile_runtime, _profile_session
from test_external_reconciliation import _observations


class FakeFacts:
    def __init__(self, session) -> None:
        self.session = session
        self.calls: list[tuple[str, str]] = []

    def read(self, *, order_id: str, instrument_id: str, now: datetime) -> ExternalCanaryFactBundle:
        self.calls.append((order_id, instrument_id))
        return ExternalCanaryFactBundle(
            fills=(),
            fees=(),
            account=None,
            positions=(),
            open_orders=(),
            reconciliation=None,
        )

    def market_fact(self, *, instrument_id: str, now: datetime) -> dict[str, object]:
        return {"instrument_id": instrument_id, "freshness": "fresh"}


class FakeSnapshotReader:
    def read_reconciliation(self, *, order_id: str, instrument_id: str, now: datetime):
        raise RuntimeError("fixture snapshot reader is not exercised by preflight")


def _binding():
    session = _profile_session()
    runtime = _profile_runtime(session)
    host = __import__(
        "standard_broker.adapters.hyperliquid.profile",
        fromlist=["build_hyperliquid_testnet_host"],
    ).build_hyperliquid_testnet_host(
        context=_profile_context(session),
        runtime=runtime,
    )
    lifecycle = LifecycleStub(session)
    from standard_broker.adapters.hyperliquid.orders import HyperliquidExternalOrderAdapter

    order = HyperliquidExternalOrderAdapter(host=host, lifecycle=lifecycle)
    facts = FakeFacts(session)
    return ExternalCanaryBinding(host=host, order=order, facts=facts), lifecycle, runtime, facts


def test_binding_preflight_is_exact_and_order_receipts_are_enriched() -> None:
    binding, lifecycle, runtime, facts = _binding()

    preflight = binding.preflight()
    assert preflight["canary_ready"] is True
    assert preflight["environment"] == "testnet"
    assert preflight["transport_profile"] == "hyperliquid-testnet-default"
    assert preflight["broker_operation_invoked"] is False
    assert runtime.invoke_calls == []

    receipt = binding.submit(_intent())
    assert receipt.instrument_id == "SOL-USD-PERP"
    assert receipt.side == "buy"
    assert receipt.quantity == Decimal("0.1")
    assert receipt.state is OrderState.RESTING
    assert receipt.account_fingerprint.startswith("sha256:")
    assert [name for name, _ in lifecycle.calls] == ["submit"]


def test_binding_idempotency_recovery_uses_canonical_order_identity() -> None:
    binding, lifecycle, _, _ = _binding()
    receipt = binding.submit(_intent())
    recovered = binding.query_by_idempotency_key("cycle-1")
    assert recovered.order_id == receipt.order_id
    assert [name for name, _ in lifecycle.calls] == ["submit", "query"]


def test_binding_recovers_persisted_intent_without_transport() -> None:
    binding, lifecycle, _, _ = _binding()
    intent = _intent()

    binding.recover(intent, broker_order_id="101", state="resting")

    assert [name for name, _ in lifecycle.calls] == ["recover"]
    recovered = binding.query_by_idempotency_key("cycle-1")
    assert recovered.order_id == "order-1"
    assert [name for name, _ in lifecycle.calls] == ["recover", "query"]


def test_binding_rejects_unknown_idempotency_without_fallback() -> None:
    binding, lifecycle, _, _ = _binding()

    with pytest.raises(Exception, match="idempotency"):
        binding.query_by_idempotency_key("missing")
    assert lifecycle.calls == []


def test_protected_canary_binding_composes_order_facts_and_protection_under_one_profile() -> None:
    from standard_broker.external_canary import ExternalCanaryBinding
    from standard_broker.external_protection import ExternalProtectionBinding
    from test_external_protection_binding import _enabled_host

    host = _enabled_host()
    session = host.context.session
    lifecycle = LifecycleStub(session)
    from standard_broker.adapters.hyperliquid.orders import HyperliquidExternalOrderAdapter

    order = HyperliquidExternalOrderAdapter(host=host, lifecycle=lifecycle)
    binding = ExternalCanaryBinding(
        host=host,
        order=order,
        facts=FakeFacts(session),
        expected_profile_id="hyperliquid-testnet-position-protection",
        protection=ExternalProtectionBinding(host=host),
    )

    preflight = binding.preflight()
    assert binding.protection is not None
    assert binding.profile_id == "hyperliquid-testnet-position-protection"
    assert binding.runtime_session is session
    assert binding.protection_capabilities is host.protection_capabilities
    assert preflight["protection_ready"] is True
    assert preflight["broker_operation_invoked"] is False
    assert lifecycle.calls == []


def test_binding_read_facts_is_typed_and_does_not_accept_raw_mapping() -> None:
    binding, _, _, facts = _binding()
    result = binding.read_facts(
        order_id="order-1",
        instrument_id="SOL-USD-PERP",
        now=datetime(2026, 8, 23, 5, 0, tzinfo=UTC),
    )
    assert isinstance(result, ExternalCanaryFactBundle)
    assert facts.calls == [("order-1", "SOL-USD-PERP")]


def test_binding_read_facts_returns_complete_bundle_with_unregistered_open_orders() -> None:
    session = _profile_session()
    provenance = Provenance(
        source="nautilus-hyperliquid.testnet",
        execution_scope=session.execution_scope,
        transport_state="external_testnet",
        mapping_revision=session.capabilities.revision,
        received_at=NOW,
    )

    class UnregisteredOrderBackend(ExternalOrderBackend):
        def invoke(self, port: str, operation: str, request: object) -> object:
            if port == "account" and operation == "read":
                self.calls.append((port, operation, request))
                return {
                    "data": {
                        "accountAddress": session.account.address,
                        "assetPositions": [],
                        "snapshotId": "unregistered-orders-snapshot",
                        "marginSummary": {
                            "accountValue": "100",
                            "totalMarginUsed": "0",
                            "totalNtlPos": "0",
                            "totalRawUsd": "100",
                        },
                        "withdrawable": "100",
                    },
                    "provenance": provenance,
                }
            if port == "account" and operation == "positions":
                self.calls.append((port, operation, request))
                return {"data": {"positions": []}, "provenance": provenance}
            return super().invoke(port, operation, request)

    backend = UnregisteredOrderBackend(session.capabilities)
    backend.responses["open_orders"] = {
        "orders": [
            {
                "status": "open",
                "oid": oid,
                "cloid": f"0xunregistered-{oid}",
                "sz": "0.1",
                "origSz": "0.1",
                "timestamp": 1787313661000 + oid,
            }
            for oid in (201, 202)
        ]
    }
    runtime = NautilusHyperliquidRuntime(
        session=session,
        backend=backend,
        config=NautilusRuntimeConfig(
            expected_version=NAUTILUS_HYPERLIQUID_VERSION,
            expected_commit=NAUTILUS_HYPERLIQUID_COMMIT,
            policy=RuntimeActivationPolicy(
                testnet_approval=ExternalEnvironmentApproval(
                    environment=BrokerEnvironment.TESTNET,
                    approval_id="unregistered-orders-approval",
                    release_sha=RELEASE_SHA,
                    approved_by="park",
                    approved_at=NOW,
                    account_address=session.account.address,
                    lifecycle_id=session.lifecycle_id,
                )
            ),
            expected_release_sha=RELEASE_SHA,
        ),
    )
    runtime.start()
    binding = build_hyperliquid_testnet_canary_binding(
        context=_profile_context(session),
        runtime=runtime,
        instruments=HyperliquidInstrumentAdapter.from_meta(
            META,
            revision=session.capabilities.revision,
        ),
        ledger=RuntimeFactLedger(),
    )

    bundle = binding.read_facts(
        order_id="",
        instrument_id="SOL-USD-PERP",
        now=NOW,
    )

    assert tuple(receipt.order_id for receipt in bundle.open_orders) == ("external:201", "external:202")
    assert all(receipt.is_unregistered_broker_order for receipt in bundle.open_orders)
    assert bundle.reconciliation.unregistered_open_order_count == 2
    assert bundle.reconciliation.unregistered_open_orders == bundle.open_orders
    assert bundle.reconciliation.passed is False


def test_account_wide_snapshot_queries_instrument_fills_and_retains_unregistered_open_orders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _profile_session()
    context = _profile_context(session)
    provenance = Provenance(
        source="nautilus-hyperliquid.testnet",
        execution_scope=session.execution_scope,
        transport_state="external_testnet",
        mapping_revision=session.capabilities.revision,
        received_at=NOW,
    )
    template = _observations()["open_orders"].fact.data[0]
    unregistered = replace(
        template,
        order_id="external:201",
        client_order_id="0xunregistered-201",
        state=OrderState.UNKNOWN,
        broker_order_id="201",
        reason="unregistered_exchange_order",
        provenance=replace(template.provenance, received_at=datetime(2026, 8, 23, 4, 59, tzinfo=UTC)),
        broker_order_lineage=("201",),
        client_order_lineage=("0xunregistered-201",),
    )

    class FakeHost:
        def read_fact(self, *, request, mapper):
            del request
            return mapper({"data": {}, "provenance": provenance})

    class FakeMapper:
        def _envelope(self, fact_type: str, data: object, request_id: str):
            return ExternalFactEnvelope.create(
                context=context,
                fact_type=fact_type,
                data=data,
                request_id=request_id,
                provenance=provenance,
            )

        def map_account(self, *, request_id: str, raw):
            return self._envelope("account.snapshot", {}, request_id)

        def map_positions(self, *, request_id: str, broker_symbol: str, raw):
            del broker_symbol, raw
            return self._envelope("account.positions", (), request_id)

        def map_fill(self, *, request_id: str, raw):
            del raw
            return self._envelope("fee.fill", {}, request_id)

    class FakeOrder:
        def query_fills(self, *, instrument_id: str):
            assert instrument_id == "PAXG-USD-PERP"
            return ()

        def open_orders(self, instrument_id: str):
            assert instrument_id == "PAXG-USD-PERP"
            return (unregistered,)

    class FakeInstruments:
        def get(self, instrument_id: str):
            assert instrument_id == "PAXG-USD-PERP"
            return SimpleNamespace(canonical_symbol="PAXG-USD-PERP", broker_symbol="PAXG")

    captured: dict[str, object] = {}

    def fake_assemble(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(
        "standard_broker.external_canary.ExternalReconciliationSnapshot.assemble",
        fake_assemble,
    )
    reader = HyperliquidExternalSnapshotReader(
        context=context,
        host=FakeHost(),
        order=FakeOrder(),
        facts_mapper=FakeMapper(),
        instruments=FakeInstruments(),
    )

    reader.read_reconciliation(
        order_id="",
        instrument_id="PAXG-USD-PERP",
        now=NOW,
    )

    assert captured["fills"].fact.data == ()
    assert captured["fees"].fact.data == ()
    assert captured["open_orders"].fact.data[0].is_unregistered_broker_order
    assert captured["open_orders"].fact.data[0].provenance == provenance


def test_order_snapshot_falls_back_to_instrument_fills_and_filters_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _profile_session()
    context = _profile_context(session)
    provenance = Provenance(
        source="nautilus-hyperliquid.testnet",
        execution_scope=session.execution_scope,
        transport_state="external_testnet",
        mapping_revision=session.capabilities.revision,
        received_at=NOW,
    )
    @dataclass(frozen=True)
    class _Fill:
        fill_id: str = "fill-close"
        order_id: str = "order-close"
        occurred_at: datetime = NOW
        client_order_id: str = "client-close"
    @dataclass(frozen=True)
    class _OtherFill:
        fill_id: str = "fill-other"
        order_id: str = "order-other"
        occurred_at: datetime = NOW
        client_order_id: str = "client-other"
    @dataclass(frozen=True)
    class _Fee:
        fee_id: str
        occurred_at: datetime
    @dataclass(frozen=True)
    class _MappedFee:
        fill_id: str
        order_id: str
        fee: _Fee

    matching = _Fill()

    unrelated = _OtherFill()

    class FakeOrder:
        def query_fills(self, *, order_id=None, instrument_id=None):
            if order_id is not None:
                assert order_id == "order-close"
                return ()
            assert instrument_id == "PAXG-USD-PERP"
            return (matching, unrelated)

        def open_orders(self, instrument_id: str):
            del instrument_id
            return ()

    class FakeInstruments:
        def get(self, instrument_id: str):
            assert instrument_id == "PAXG-USD-PERP"
            return SimpleNamespace(canonical_symbol="PAXG-USD-PERP", broker_symbol="PAXG")

    class FakeMapper:
        map_account = staticmethod(lambda **_kwargs: None)
        map_positions = staticmethod(lambda **_kwargs: None)
        map_fill = staticmethod(lambda **_kwargs: None)

    reader = HyperliquidExternalSnapshotReader(
        context=context,
        host=SimpleNamespace(),
        order=FakeOrder(),
        facts_mapper=FakeMapper(),
        instruments=FakeInstruments(),
    )
    def fake_read_fact(**kwargs):
        data = (
            _MappedFee("fill-close", "order-close", _Fee("fee-close", NOW))
            if kwargs["port"] == "fee"
            else ()
        )
        return ExternalFactEnvelope.create(
            context=context,
            fact_type=kwargs["port"],
            data=data,
            request_id=kwargs["request_id"],
            provenance=provenance,
        )

    reader._read_fact = fake_read_fact
    reader._envelope = lambda *, fact_type, data, provenance, request_id: ExternalFactEnvelope.create(
        context=context,
        fact_type=fact_type,
        data=data,
        request_id=request_id,
        provenance=provenance,
    )
    captured: dict[str, object] = {}

    def fake_assemble(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(
        "standard_broker.external_canary.ExternalReconciliationSnapshot.assemble",
        fake_assemble,
    )
    reader.read_reconciliation(order_id="order-close", instrument_id="PAXG-USD-PERP", now=NOW)

    assert captured["fills"].fact.data == (matching,)


def test_order_snapshot_uses_client_scoped_fill_recovery_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _profile_session()
    context = _profile_context(session)
    provenance = Provenance(
        source="nautilus-hyperliquid.testnet",
        execution_scope=session.execution_scope,
        transport_state="external_testnet",
        mapping_revision=session.capabilities.revision,
        received_at=NOW,
    )

    @dataclass(frozen=True)
    class _Fill:
        fill_id: str = "fill-close"
        order_id: str = "order-close"
        occurred_at: datetime = NOW
        client_order_id: str = "client-close"

    matching = _Fill()

    @dataclass(frozen=True)
    class _Fee:
        fee_id: str = "fee-close"
        occurred_at: datetime = NOW

    @dataclass(frozen=True)
    class _MappedFee:
        fill_id: str = "fill-close"
        order_id: str = "order-close"
        fee: _Fee = _Fee()

    class FakeOrder:
        def query_fills(self, **_kwargs):
            return ()

        def fills_by_client_order_id(self, *, client_order_id: str, instrument_id: str):
            assert client_order_id == "client-close"
            assert instrument_id == "PAXG-USD-PERP"
            return (matching,)

        def open_orders(self, instrument_id: str):
            del instrument_id
            return ()

    class FakeInstruments:
        def get(self, instrument_id: str):
            assert instrument_id == "PAXG-USD-PERP"
            return SimpleNamespace(canonical_symbol="PAXG-USD-PERP", broker_symbol="PAXG")

    class FakeMapper:
        map_account = staticmethod(lambda **_kwargs: None)
        map_positions = staticmethod(lambda **_kwargs: None)
        map_fill = staticmethod(lambda **_kwargs: None)

    reader = HyperliquidExternalSnapshotReader(
        context=context,
        host=SimpleNamespace(),
        order=FakeOrder(),
        facts_mapper=FakeMapper(),
        instruments=FakeInstruments(),
    )
    def fake_read_fact(**kwargs):
        if kwargs["port"] == "fee":
            assert kwargs["instrument_id"] == "PAXG-USD-PERP"
        data = _MappedFee() if kwargs["port"] == "fee" else ()
        return ExternalFactEnvelope.create(
            context=context,
            fact_type=kwargs["port"],
            data=data,
            request_id=kwargs["request_id"],
            provenance=provenance,
        )

    reader._read_fact = fake_read_fact
    reader._envelope = lambda *, fact_type, data, provenance, request_id: ExternalFactEnvelope.create(
        context=context,
        fact_type=fact_type,
        data=data,
        request_id=request_id,
        provenance=provenance,
    )
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "standard_broker.external_canary.ExternalReconciliationSnapshot.assemble",
        lambda **kwargs: captured.update(kwargs) or SimpleNamespace(),
    )

    reader.read_reconciliation(
        order_id="order-close",
        instrument_id="PAXG-USD-PERP",
        now=NOW,
        client_order_id="client-close",
    )

    assert captured["fills"].fact.data == (matching,)


def test_public_factory_composes_external_canary_binding_without_network() -> None:
    session = _profile_session()
    backend = ExternalOrderBackend(session.capabilities)
    runtime = NautilusHyperliquidRuntime(
        session=session,
        backend=backend,
        config=NautilusRuntimeConfig(
            expected_version=NAUTILUS_HYPERLIQUID_VERSION,
            expected_commit=NAUTILUS_HYPERLIQUID_COMMIT,
            policy=RuntimeActivationPolicy(
                testnet_approval=ExternalEnvironmentApproval(
                    environment=BrokerEnvironment.TESTNET,
                    approval_id="canary-binding-approval",
                    release_sha=RELEASE_SHA,
                    approved_by="park",
                    approved_at=NOW,
                    account_address=session.account.address,
                    lifecycle_id=session.lifecycle_id,
                )
            ),
            expected_release_sha=RELEASE_SHA,
        ),
    )
    runtime.start()
    instruments = HyperliquidInstrumentAdapter.from_meta(META, revision=session.capabilities.revision)
    binding = build_hyperliquid_testnet_canary_binding(
        context=_profile_context(session),
        runtime=runtime,
        instruments=instruments,
        ledger=RuntimeFactLedger(),
        snapshot_reader=FakeSnapshotReader(),
    )

    preflight = binding.preflight()
    assert preflight["canary_ready"] is True
    assert backend.calls == []


@pytest.mark.parametrize("protected", [False, True])
def test_runtime_fact_factories_send_typed_instrument_query(
    monkeypatch: pytest.MonkeyPatch,
    protected: bool,
) -> None:
    capabilities = (
        enabled_testnet_position_protection_capabilities()
        if protected
        else default_testnet_capabilities()
    )
    session = _profile_session(capabilities=capabilities)
    backend = ExternalOrderBackend(capabilities)
    runtime = NautilusHyperliquidRuntime(
        session=session,
        backend=backend,
        config=NautilusRuntimeConfig(
            expected_version=NAUTILUS_HYPERLIQUID_VERSION,
            expected_commit=NAUTILUS_HYPERLIQUID_COMMIT,
            policy=RuntimeActivationPolicy(
                testnet_approval=ExternalEnvironmentApproval(
                    environment=BrokerEnvironment.TESTNET,
                    approval_id="typed-instrument-read-approval",
                    release_sha=RELEASE_SHA,
                    approved_by="park",
                    approved_at=NOW,
                    account_address=session.account.address,
                    lifecycle_id=session.lifecycle_id,
                )
            ),
            expected_release_sha=RELEASE_SHA,
        ),
    )
    runtime.start()
    context = _profile_context(session)
    captured: list[object] = []
    provenance = Provenance(
        source="nautilus-hyperliquid.testnet",
        execution_scope=session.execution_scope,
        transport_state="external_testnet",
        mapping_revision=capabilities.revision,
    )

    def fake_read_fact(self, *, request, mapper):
        del self
        captured.append(request.request.payload)
        return mapper(
            {
                "meta": {
                    "universe": [
                        {"name": "BTC", "index": 0, "szDecimals": 5, "maxLeverage": 40}
                    ]
                },
                "provenance": provenance,
            }
        )

    monkeypatch.setattr(ExternalBrokerHost, "read_fact", fake_read_fact)
    factory = (
        build_hyperliquid_testnet_protected_canary_binding_from_runtime
        if protected
        else build_hyperliquid_testnet_canary_binding_from_runtime
    )
    binding = factory(context=context, runtime=runtime, ledger=RuntimeFactLedger())

    assert captured
    assert isinstance(captured[0], CanonicalPortQuery)
    assert captured[0].kind == "instruments"
    assert binding.profile_id == (
        "hyperliquid-testnet-position-protection"
        if protected
        else "hyperliquid-testnet-default"
    )


def test_market_fact_freshness_uses_read_completion_time() -> None:
    from standard_broker.external_canary import ExternalCanaryRuntimeFactsReader
    from standard_broker.market_data import FreshnessState

    session = _profile_session()
    context = _profile_context(session)
    received_at = datetime.now(UTC)
    captured: list[datetime] = []
    provenance = Provenance(
        source="nautilus-hyperliquid.testnet",
        execution_scope=session.execution_scope,
        transport_state="external_testnet",
        mapping_revision=session.capabilities.revision,
        received_at=received_at,
    )

    class FakeHost:
        def read_fact(self, *, request, mapper):
            del request
            return mapper({"provenance": provenance, "data": {"bbo": {"bbo": [{"px": "4610"}, {"px": "4611"}]}, "mid": "4610.5"}})

    class FakeMarket:
        def map_ticker(self, *, now, **_kwargs):
            captured.append(now)
            return SimpleNamespace(
                data=SimpleNamespace(
                    data=SimpleNamespace(mid=Decimal("4610.5"), bid=None, ask=None, venue_timestamp=None),
                    freshness=FreshnessState.FRESH,
                ),
                provenance=provenance,
            )

    class FakeInstruments:
        def get(self, _instrument_id):
            return SimpleNamespace(canonical_symbol="PAXG-USD-PERP", broker_symbol="PAXG")

    reader = ExternalCanaryRuntimeFactsReader(
        context=context,
        host=FakeHost(),
        order=SimpleNamespace(query_fills=lambda **_kwargs: ()),
        snapshot_reader=SimpleNamespace(read_reconciliation=lambda **_kwargs: None),
        instruments=FakeInstruments(),
        market=FakeMarket(),
    )
    caller_now = received_at.replace(microsecond=0) - __import__("datetime").timedelta(seconds=1)
    result = reader.market_fact(instrument_id="PAXG-USD-PERP", now=caller_now)

    assert result["freshness"] == "fresh"
    assert captured and captured[0] >= received_at


def test_host_typed_fact_read_authorizes_without_exposing_internal_payload() -> None:
    binding, _, runtime, _ = _binding()
    calls = []
    provenance = Provenance(
        source="nautilus-hyperliquid.testnet",
        execution_scope=binding.context.identity.execution_scope,
        transport_state="external_testnet",
        mapping_revision=binding.context.capabilities.revision,
    )

    def invoke_fact(port, operation, request):
        calls.append((port, operation, request))
        return {"data": {"ok": True}, "provenance": provenance}

    runtime.invoke_fact = invoke_fact
    request = ExternalHostRequest(
        request_id="typed-fact-1",
        request=CanonicalHostRequest(
            port="account",
            operation="read",
            payload=CanonicalPortQuery(subject=binding.context.identity.account_address, kind="account"),
        ),
    )
    envelope = binding._host.read_fact(
        request=request,
        mapper=lambda raw: ExternalFactEnvelope.create(
            context=binding.context,
            fact_type="test.account",
            data=raw["data"],
            request_id=request.request_id,
            provenance=raw["provenance"],
        ),
    )
    assert envelope.data == {"ok": True}
    assert calls and calls[0][0:2] == ("account", "read")
    assert runtime.invoke_calls == []
