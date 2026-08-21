import unittest
from decimal import Decimal

from standard_broker.adapters.hyperliquid.bridge import (
    NautilusAdapterMetadata,
    NautilusBridgeConfig,
    NautilusHyperliquidBridge,
)
from standard_broker.capabilities import PORT_NAMES, CapabilityDescriptor
from standard_broker.errors import BrokerError
from standard_broker.fees import FeeEvent, FeeKind, FeeSource, FeeState
from standard_broker.host import (
    BrokerRegistry,
    BrokerSelection,
    HostRole,
    HostSafetyPolicy,
    RecordingEvent,
    RecordingTrack,
)
from standard_broker.models import (
    BrokerEnvironment,
    BrokerIdentity,
    Provenance,
    SignerKind,
)
from standard_broker.paper import PaperBrokerAdapter


class HostContractTests(unittest.TestCase):
    def adapter(self, broker_id: str = "hyperliquid") -> PaperBrokerAdapter:
        return PaperBrokerAdapter(
            identity=BrokerIdentity(
                broker_id=broker_id,
                environment=BrokerEnvironment.PAPER,
                signer_kind=SignerKind.NONE,
            ),
            capabilities=CapabilityDescriptor(
                broker_id=broker_id,
                environment=BrokerEnvironment.PAPER,
                operations={
                    "preflight": frozenset({"read"}),
                    **{port: frozenset({"read"}) for port in PORT_NAMES},
                },
                revision="host-contract-v1",
            ),
        )

    def test_registry_resolves_explicit_paper_broker_role(self) -> None:
        registry = BrokerRegistry()
        adapter = self.adapter()
        registry.register(
            BrokerSelection("hyperliquid", BrokerEnvironment.PAPER, HostRole.AUTHORITATIVE),
            factory=lambda: adapter,
            capabilities=adapter.capabilities,
        )
        registry.freeze()

        binding = registry.resolve(
            BrokerSelection("hyperliquid", BrokerEnvironment.PAPER, HostRole.AUTHORITATIVE),
            policy=HostSafetyPolicy(),
        )

        self.assertEqual(binding.identity.broker_id, "hyperliquid")
        self.assertEqual(binding.identity.environment, BrokerEnvironment.PAPER)
        self.assertFalse(binding.real_money_eligible)
        self.assertEqual(binding.control_plane, "telegram")

    def test_registry_composes_the_paper_nautilus_bridge(self) -> None:
        capabilities = CapabilityDescriptor(
            broker_id="hyperliquid",
            environment=BrokerEnvironment.PAPER,
            operations={"preflight": frozenset({"read"}), "market_data": frozenset({"read"})},
            revision="host-bridge-v1",
        )
        backend = type(
            "LocalNautilusBackend",
            (),
            {
                "local_only": True,
                "metadata": NautilusAdapterMetadata(
                    package="nautilus-hyperliquid",
                    version="1.230.0",
                    commit="host-bridge-commit",
                    capabilities=capabilities,
                ),
                "invoke": lambda self, port, operation, request: {"ok": True},
            },
        )()
        bridge = NautilusHyperliquidBridge(
            backend=backend,
            config=NautilusBridgeConfig(
                expected_version="1.230.0",
                expected_commit="host-bridge-commit",
                account_address="0xmaster",
            ),
            declared_capabilities=capabilities,
        )
        registry = BrokerRegistry()
        selection = BrokerSelection("hyperliquid", BrokerEnvironment.PAPER, HostRole.AUTHORITATIVE)
        registry.register(selection, factory=lambda: bridge, capabilities=capabilities)
        registry.freeze()

        binding = registry.resolve(selection, policy=HostSafetyPolicy())

        self.assertIs(binding.adapter, bridge)
        self.assertFalse(binding.real_money_eligible)

    def test_unknown_selection_has_no_fallback_authority(self) -> None:
        registry = BrokerRegistry()
        adapter = self.adapter()
        registry.register(
            BrokerSelection("hyperliquid", BrokerEnvironment.PAPER, HostRole.AUTHORITATIVE),
            factory=lambda: adapter,
            capabilities=adapter.capabilities,
        )
        registry.freeze()

        with self.assertRaises(BrokerError):
            registry.resolve(
                BrokerSelection("binance", BrokerEnvironment.PAPER, HostRole.AUTHORITATIVE),
                policy=HostSafetyPolicy(),
            )
        with self.assertRaises(BrokerError):
            registry.resolve(
                BrokerSelection("hyperliquid", BrokerEnvironment.MAINNET, HostRole.AUTHORITATIVE),
                policy=HostSafetyPolicy(),
            )

    def test_registry_rejects_mutation_after_freeze(self) -> None:
        registry = BrokerRegistry()
        adapter = self.adapter()
        registry.register(
            BrokerSelection("hyperliquid", BrokerEnvironment.PAPER, HostRole.AUTHORITATIVE),
            factory=lambda: adapter,
            capabilities=adapter.capabilities,
        )
        registry.freeze()

        with self.assertRaises(BrokerError):
            registry.register(
                BrokerSelection("binance", BrokerEnvironment.PAPER, HostRole.AUTHORITATIVE),
                factory=lambda: adapter,
                capabilities=adapter.capabilities,
            )

    def test_safety_policy_rejects_non_telegram_or_live_authority(self) -> None:
        with self.assertRaises(BrokerError):
            HostSafetyPolicy(control_plane="mcp")
        with self.assertRaises(BrokerError):
            HostSafetyPolicy(paper_only=False)
        with self.assertRaises(BrokerError):
            HostSafetyPolicy(live_enabled=True)

    def test_recording_track_accepts_canonical_fee_event(self) -> None:
        provenance = Provenance(
            source="paper.fixture",
            execution_scope="hypercore:default",
            transport_state="local_fixture",
            mapping_revision="host-contract-v1",
        )
        fee = FeeEvent(
            fee_id="fee-1",
            broker_id="hyperliquid",
            kind=FeeKind.TAKER,
            amount=Decimal("0.1"),
            currency="USDC",
            occurred_at=provenance.received_at,
            source=FeeSource.ACTUAL_FILL,
            state=FeeState.ACTUAL,
            provenance=provenance,
        )
        track = RecordingTrack()

        track.record(RecordingEvent("event-1", "fee", fee, provenance))

        self.assertEqual(track.events[0].event_id, "event-1")
        self.assertEqual(track.events[0].kind, "fee")

    def test_recording_track_rejects_raw_provider_payload(self) -> None:
        provenance = Provenance(
            source="paper.fixture",
            execution_scope="hypercore:default",
            transport_state="local_fixture",
            mapping_revision="host-contract-v1",
        )
        track = RecordingTrack()

        with self.assertRaises(BrokerError):
            track.record(RecordingEvent("event-raw", "raw", {"coin": "BTC", "oid": 1}, provenance))


if __name__ == "__main__":
    unittest.main()
