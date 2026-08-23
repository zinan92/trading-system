from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from standard_broker.external_canary import (
    ExternalCanaryBinding,
    ExternalCanaryFactBundle,
)
from standard_broker.models import BrokerEnvironment
from standard_broker.orders import OrderState
from standard_broker.adapters.hyperliquid import (
    HyperliquidInstrumentAdapter,
    NautilusHyperliquidRuntime,
    NautilusRuntimeConfig,
    build_hyperliquid_testnet_canary_binding,
)
from standard_broker.adapters.hyperliquid.external import (
    NAUTILUS_HYPERLIQUID_COMMIT,
    NAUTILUS_HYPERLIQUID_VERSION,
)
from standard_broker.runtime import ExternalEnvironmentApproval, RuntimeActivationPolicy
from standard_broker.models import BrokerEnvironment
from standard_broker.runtime_facts import RuntimeFactLedger
from standard_broker.orders import OrderFill

from test_external_order_lifecycle import ExternalOrderBackend, LifecycleStub, META, NOW, _intent
from test_external_host import RELEASE_SHA
from test_external_profile import _profile_context, _profile_runtime, _profile_session


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


def test_binding_rejects_unknown_idempotency_without_fallback() -> None:
    binding, lifecycle, _, _ = _binding()

    with pytest.raises(Exception, match="idempotency"):
        binding.query_by_idempotency_key("missing")
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
    )

    preflight = binding.preflight()
    assert preflight["canary_ready"] is True
    assert backend.calls == []
