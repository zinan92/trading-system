import unittest
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from standard_broker.capabilities import PORT_NAMES, CapabilityDescriptor
from standard_broker.account import PositionFact, PositionSide
from standard_broker.evidence import (
    ReconciliationEvidence,
    EvidenceClass,
    EvidenceIdentity,
    run_paper_runtime_readiness,
)
from standard_broker.host import (
    BrokerRegistry,
    BrokerSelection,
    CanonicalHostReceipt,
    HostRole,
    HostSafetyPolicy,
    RecordingEvent,
    RecordingTrack,
)
from standard_broker.models import AccountScope, BrokerEnvironment, BrokerIdentity, Provenance, SignerKind
from standard_broker.fees import FeeEvent, FeeKind, FeeSource, FeeState, FillFact
from standard_broker.orders import OrderFill, OrderReceipt, OrderSide, OrderState
from standard_broker.paper import PaperBrokerAdapter
from standard_broker.runtime import AccountReference, BrokerRuntimeSession, SignerReference


class RuntimeReadinessTests(unittest.TestCase):
    def binding(self):
        adapter = PaperBrokerAdapter(
            identity=BrokerIdentity(
                broker_id="hyperliquid",
                environment=BrokerEnvironment.PAPER,
                account_scope=AccountScope.MASTER,
                account_address="0xaccount",
                signer_kind=SignerKind.NONE,
                execution_scope="hypercore:default",
            ),
            capabilities=CapabilityDescriptor(
                broker_id="hyperliquid",
                environment=BrokerEnvironment.PAPER,
                operations={
                    "preflight": frozenset({"read"}),
                    **{port: frozenset({"read"}) for port in PORT_NAMES},
                },
                revision="readiness-v1",
            ),
        )
        registry = BrokerRegistry()
        selection = BrokerSelection("hyperliquid", BrokerEnvironment.PAPER, HostRole.AUTHORITATIVE)
        registry.register(selection, factory=lambda: adapter, capabilities=adapter.capabilities)
        registry.freeze()
        return registry.resolve(selection, policy=HostSafetyPolicy())

    def runtime(self):
        binding = self.binding()
        return SimpleNamespace(
            session=BrokerRuntimeSession(
                broker_id="hyperliquid",
                environment=BrokerEnvironment.PAPER,
                account=AccountReference(AccountScope.MASTER, "0xaccount"),
                signer=SignerReference.paper(),
                signer_provider=None,
                capabilities=binding.capabilities,
                execution_scope="hypercore:default",
                lifecycle_id="readiness-runtime-1",
            ),
            health=SimpleNamespace(
                external_network=False,
                real_money_eligible=False,
                invocation_performed=False,
            ),
        )

    def recording(self) -> RecordingTrack:
        provenance = Provenance(
            source="paper.fixture",
            execution_scope="hypercore:default",
            transport_state="local_fixture",
            mapping_revision="readiness-v1",
        )
        track = RecordingTrack()
        track.record(
            RecordingEvent(
                event_id="host-receipt-1",
                kind="receipt",
                payload=CanonicalHostReceipt(
                    broker_id="hyperliquid",
                    environment=BrokerEnvironment.PAPER,
                    port="market_data",
                    operation="read",
                    accepted=True,
                    network_io=False,
                    real_money_eligible=False,
                    provenance=provenance,
                ),
                provenance=provenance,
            )
        )
        fee = FeeEvent(
            fee_id="fee-1",
            broker_id="hyperliquid",
            kind=FeeKind.TAKER,
            amount=Decimal("0.1"),
            currency="USDC",
            occurred_at=datetime(2026, 8, 22, tzinfo=UTC),
            source=FeeSource.ACTUAL_FILL,
            state=FeeState.ACTUAL,
            provenance=provenance,
            instrument_id="BTC-USD-PERP",
            fill_id="fill-1",
            order_id="order-1",
        )
        track.record(
            RecordingEvent(
                "order-1",
                "order",
                OrderReceipt(
                    order_id="order-1",
                    broker_id="hyperliquid",
                    environment=BrokerEnvironment.PAPER,
                    client_order_id="client-1",
                    state=OrderState.RESTING,
                    original_quantity=Decimal("0.1"),
                    filled_quantity=Decimal(0),
                    remaining_quantity=Decimal("0.1"),
                    broker_order_id="101",
                    average_fill_price=None,
                    reason=None,
                    provenance=provenance,
                    updated_at=provenance.received_at,
                    broker_order_lineage=("101",),
                    client_order_lineage=("client-1",),
                ),
                provenance,
            )
        )
        track.record(
            RecordingEvent(
                "fill-1",
                "fill",
                FillFact(
                    fill_id="fill-1",
                    broker_id="hyperliquid",
                    instrument_id="BTC-USD-PERP",
                    side="buy",
                    price=Decimal("65000"),
                    quantity=Decimal("0.1"),
                    occurred_at=provenance.received_at,
                    closed_pnl=Decimal(0),
                    fee=fee,
                    provenance=provenance,
                ),
                provenance,
            )
        )
        track.record(
            RecordingEvent(
                "order-fill-1",
                "order_fill",
                OrderFill(
                    fill_id="fill-1",
                    order_id="order-1",
                    broker_order_id="101",
                    client_order_id="client-1",
                    instrument_id="BTC-USD-PERP",
                    side=OrderSide.BUY,
                    price=Decimal("65000"),
                    quantity=Decimal("0.1"),
                    occurred_at=provenance.received_at,
                    environment=BrokerEnvironment.PAPER,
                    account_address="0xaccount",
                    lifecycle_id="readiness-runtime-1",
                ),
                provenance,
            )
        )
        track.record(
            RecordingEvent(
                "position-1",
                "position",
                PositionFact(
                    instrument_id="BTC-USD-PERP",
                    signed_quantity=Decimal("0.1"),
                    side=PositionSide.LONG,
                    entry_price=Decimal("65000"),
                    leverage=Decimal(5),
                    margin_mode=None,
                    liquidation_price=None,
                    margin_used=Decimal(100),
                    position_value=Decimal(6500),
                    unrealized_pnl=Decimal(0),
                    broker_id="hyperliquid",
                    environment=BrokerEnvironment.PAPER,
                    observation_id="position-1",
                    provenance=provenance,
                ),
                provenance,
            )
        )
        return track

    def test_paper_runtime_readiness_is_distinct_and_requires_external_gate(self) -> None:
        provenance = Provenance(
            source="paper.fixture",
            execution_scope="hypercore:default",
            transport_state="local_fixture",
            mapping_revision="readiness-v1",
        )
        report = run_paper_runtime_readiness(
            runtime=self.runtime(),
            binding=self.binding(),
            recording=self.recording(),
            reconciliation_events=(
                ReconciliationEvidence(
                    reconciliation_id="reconciliation-1",
                    broker_id="hyperliquid",
                    environment=BrokerEnvironment.PAPER,
                    account_scope=AccountScope.MASTER,
                    account_address="0xaccount",
                    execution_scope="hypercore:default",
                    lifecycle_id="readiness-runtime-1",
                    watermark=1,
                    provenance=provenance,
                ),
            ),
        )

        self.assertTrue(report.passed)
        self.assertEqual(report.identity.evidence_class, EvidenceClass.PAPER)
        self.assertFalse(report.network_io)
        self.assertFalse(report.credential_required)
        self.assertFalse(report.real_money_eligible)
        self.assertTrue(report.external_activation_required)
        self.assertFalse(report.external_gate_verified)
        self.assertIn("testnet_human_gate_required", report.checks)
        self.assertIn("order_ids", report.required_evidence_fields)

    def test_readiness_fails_if_runtime_claims_network(self) -> None:
        runtime = self.runtime()
        runtime.health = SimpleNamespace(
            external_network=True,
            real_money_eligible=False,
            invocation_performed=False,
        )

        report = run_paper_runtime_readiness(
            runtime=runtime,
            binding=self.binding(),
            recording=self.recording(),
            reconciliation_events=(
                ReconciliationEvidence(
                    reconciliation_id="reconciliation-1",
                    broker_id="hyperliquid",
                    environment=BrokerEnvironment.PAPER,
                    account_scope=AccountScope.MASTER,
                    account_address="0xaccount",
                    execution_scope="hypercore:default",
                    lifecycle_id="readiness-runtime-1",
                    watermark=1,
                    provenance=Provenance(
                        source="paper.fixture",
                        execution_scope="hypercore:default",
                        transport_state="local_fixture",
                        mapping_revision="readiness-v1",
                    ),
                ),
            ),
        )

        self.assertFalse(report.passed)
        self.assertIn("paper_network_io", report.failures)

    def test_evidence_identity_does_not_allow_paper_to_claim_testnet(self) -> None:
        with self.assertRaises(ValueError):
            EvidenceIdentity(
                evidence_class=EvidenceClass.TESTNET,
                broker_id="hyperliquid",
                environment=BrokerEnvironment.PAPER,
                account_scope=AccountScope.MASTER,
                account_address="0xaccount",
                execution_scope="hypercore:default",
                lifecycle_id="runtime-1",
                release_sha="a" * 40,
            )

    def test_testnet_identity_requires_release_bound_evidence(self) -> None:
        with self.assertRaises(ValueError):
            EvidenceIdentity(
                evidence_class=EvidenceClass.TESTNET,
                broker_id="hyperliquid",
                environment=BrokerEnvironment.TESTNET,
                account_scope=AccountScope.MASTER,
                account_address="0xaccount",
                execution_scope="hypercore:default",
                lifecycle_id="runtime-1",
                release_sha=None,
            )


if __name__ == "__main__":
    unittest.main()
