import unittest
from dataclasses import dataclass

from standard_broker.adapters.hyperliquid.bridge import (
    NautilusAdapterMetadata,
    NautilusBridgeConfig,
    NautilusCompatibilityError,
    NautilusHyperliquidBridge,
)
from standard_broker.capabilities import CapabilityDescriptor
from standard_broker.models import BrokerEnvironment, SignerKind


def capabilities() -> CapabilityDescriptor:
    return CapabilityDescriptor(
        broker_id="hyperliquid",
        environment=BrokerEnvironment.PAPER,
        operations={
            "preflight": frozenset({"read"}),
            "market_data": frozenset({"read"}),
        },
        revision="bridge-contract-v1",
    )


@dataclass
class FakeNautilusBackend:
    metadata: NautilusAdapterMetadata
    local_only: bool = True

    def __post_init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []

    def invoke(self, port: str, operation: str, request: object) -> object:
        self.calls.append((port, operation, request))
        return {"canonical": True, "port": port, "operation": operation}


class NautilusBridgeTests(unittest.TestCase):
    def metadata(self, *, version: str = "1.230.0", commit: str = "nautilus-commit") -> NautilusAdapterMetadata:
        return NautilusAdapterMetadata(
            package="nautilus-hyperliquid",
            version=version,
            commit=commit,
            capabilities=capabilities(),
        )

    def config(self, *, expected_version: str = "1.230.0") -> NautilusBridgeConfig:
        return NautilusBridgeConfig(
            expected_version=expected_version,
            expected_commit="nautilus-commit",
            account_address="0xmaster",
            signer_kind=SignerKind.NONE,
        )

    def test_matching_local_backend_returns_canonical_bridge_receipt(self) -> None:
        backend = FakeNautilusBackend(self.metadata())
        bridge = NautilusHyperliquidBridge(
            backend=backend,
            config=self.config(),
            declared_capabilities=capabilities(),
        )

        receipt = bridge.request("market_data", "read", {"instrument_id": "BTC-USD-PERP"})

        self.assertTrue(receipt.accepted)
        self.assertFalse(receipt.network_io)
        self.assertFalse(receipt.real_money_eligible)
        self.assertEqual(receipt.account_address, "0xmaster")
        self.assertEqual(receipt.adapter_version, "1.230.0")
        self.assertEqual(backend.calls[0][0:2], ("market_data", "read"))

    def test_version_mismatch_fails_before_backend_invocation(self) -> None:
        backend = FakeNautilusBackend(self.metadata(version="1.231.0"))

        with self.assertRaises(NautilusCompatibilityError):
            NautilusHyperliquidBridge(
                backend=backend,
                config=self.config(),
                declared_capabilities=capabilities(),
            )

        self.assertEqual(backend.calls, [])

    def test_commit_mismatch_fails_before_backend_invocation(self) -> None:
        backend = FakeNautilusBackend(self.metadata(commit="different-commit"))

        with self.assertRaises(NautilusCompatibilityError):
            NautilusHyperliquidBridge(
                backend=backend,
                config=self.config(),
                declared_capabilities=capabilities(),
            )

        self.assertEqual(backend.calls, [])

    def test_capability_mismatch_fails_before_backend_invocation(self) -> None:
        backend = FakeNautilusBackend(self.metadata())
        declared = CapabilityDescriptor(
            broker_id="hyperliquid",
            environment=BrokerEnvironment.PAPER,
            operations={"preflight": frozenset({"read"})},
            revision="different-contract",
        )

        with self.assertRaises(NautilusCompatibilityError):
            NautilusHyperliquidBridge(
                backend=backend,
                config=self.config(),
                declared_capabilities=declared,
            )

        self.assertEqual(backend.calls, [])

    def test_non_local_backend_is_rejected_for_paper(self) -> None:
        backend = FakeNautilusBackend(self.metadata(), local_only=False)

        with self.assertRaises(NautilusCompatibilityError):
            NautilusHyperliquidBridge(
                backend=backend,
                config=self.config(),
                declared_capabilities=capabilities(),
            )

    def test_paper_bridge_rejects_api_agent_signer(self) -> None:
        with self.assertRaises(NautilusCompatibilityError):
            NautilusBridgeConfig(
                expected_version="1.230.0",
                expected_commit="nautilus-commit",
                account_address="0xmaster",
                signer_kind=SignerKind.API_AGENT,
            )

    def test_unsupported_operation_fails_before_backend_invocation(self) -> None:
        backend = FakeNautilusBackend(self.metadata())
        bridge = NautilusHyperliquidBridge(
            backend=backend,
            config=self.config(),
            declared_capabilities=capabilities(),
        )

        with self.assertRaises(NautilusCompatibilityError):
            bridge.request("order_execution", "submit", {"order_id": "o-1"})

        self.assertEqual(backend.calls, [])


if __name__ == "__main__":
    unittest.main()
