import unittest

from standard_broker.capabilities import PORT_NAMES, CapabilityDescriptor
from standard_broker.errors import BrokerCapabilityError, PaperBoundaryError
from standard_broker.models import (
    AccountScope,
    BrokerEnvironment,
    BrokerIdentity,
    SignerKind,
)
from standard_broker.paper import InMemoryPaperTransport, PaperBrokerAdapter


class PaperBrokerContractTests(unittest.TestCase):
    def make_identity(self) -> BrokerIdentity:
        return BrokerIdentity(
            broker_id="hyperliquid",
            environment=BrokerEnvironment.PAPER,
            account_scope=AccountScope.MASTER,
            signer_kind=SignerKind.NONE,
        )

    def make_capabilities(self, operations=None) -> CapabilityDescriptor:
        return CapabilityDescriptor(
            broker_id="hyperliquid",
            environment=BrokerEnvironment.PAPER,
            operations=operations or {"preflight": frozenset({"read"})},
            revision="paper-contract-v1",
        )

    def test_default_paper_adapter_exposes_all_six_canonical_ports(self) -> None:
        adapter = PaperBrokerAdapter(
            identity=self.make_identity(),
            capabilities=self.make_capabilities(),
        )

        self.assertEqual(tuple(adapter.ports), PORT_NAMES)
        self.assertEqual(set(adapter.ports), set(PORT_NAMES))

    def test_paper_preflight_is_local_and_not_real_money_eligible(self) -> None:
        adapter = PaperBrokerAdapter(
            identity=self.make_identity(),
            capabilities=self.make_capabilities(),
        )

        preflight = adapter.preflight()

        self.assertFalse(preflight.network_io)
        self.assertFalse(preflight.real_money_eligible)
        self.assertFalse(preflight.credential_required)
        self.assertEqual(preflight.environment, BrokerEnvironment.PAPER)

    def test_unsupported_capability_fails_before_transport(self) -> None:
        transport = InMemoryPaperTransport()
        adapter = PaperBrokerAdapter(
            identity=self.make_identity(),
            capabilities=self.make_capabilities(),
            transport=transport,
        )

        with self.assertRaises(BrokerCapabilityError) as raised:
            adapter.request("order_execution", "submit", {"order_id": "paper-1"})

        self.assertEqual(raised.exception.reason_code, "capability_gap")
        self.assertEqual(raised.exception.port, "order_execution")
        self.assertEqual(transport.calls, [])

    def test_supported_request_returns_local_normalized_receipt(self) -> None:
        transport = InMemoryPaperTransport()
        adapter = PaperBrokerAdapter(
            identity=self.make_identity(),
            capabilities=self.make_capabilities({"preflight": frozenset({"read"})}),
            transport=transport,
        )

        receipt = adapter.request("preflight", "read", {"request_id": "p-1"})

        self.assertEqual(receipt.broker_id, "hyperliquid")
        self.assertEqual(receipt.port, "preflight")
        self.assertEqual(receipt.operation, "read")
        self.assertTrue(receipt.accepted)
        self.assertFalse(receipt.network_io)
        self.assertFalse(receipt.real_money_eligible)
        self.assertEqual(len(transport.calls), 1)

    def test_non_paper_identity_is_rejected(self) -> None:
        with self.assertRaises(PaperBoundaryError):
            PaperBrokerAdapter(
                identity=BrokerIdentity(
                    broker_id="hyperliquid",
                    environment=BrokerEnvironment.TESTNET,
                    signer_kind=SignerKind.NONE,
                ),
                capabilities=CapabilityDescriptor(
                    broker_id="hyperliquid",
                    environment=BrokerEnvironment.TESTNET,
                    operations={},
                    revision="testnet-contract-v1",
                ),
            )

    def test_signer_is_rejected_even_when_environment_is_paper(self) -> None:
        with self.assertRaises(PaperBoundaryError):
            PaperBrokerAdapter(
                identity=BrokerIdentity(
                    broker_id="hyperliquid",
                    environment=BrokerEnvironment.PAPER,
                    signer_kind=SignerKind.API_AGENT,
                ),
                capabilities=self.make_capabilities(),
            )

    def test_capability_descriptor_is_immutable(self) -> None:
        descriptor = self.make_capabilities()

        with self.assertRaises((AttributeError, TypeError)):
            descriptor.revision = "changed"  # type: ignore[misc]
        with self.assertRaises(TypeError):
            descriptor.operations["preflight"] = frozenset({"write"})  # type: ignore[index]

    def test_non_local_transport_is_rejected(self) -> None:
        class NonLocalTransport:
            local_only = False

            def request(self, port, operation, payload):
                return payload

        with self.assertRaises(PaperBoundaryError):
            PaperBrokerAdapter(
                identity=self.make_identity(),
                capabilities=self.make_capabilities(),
                transport=NonLocalTransport(),
            )


if __name__ == "__main__":
    unittest.main()
