import unittest
from datetime import UTC, datetime

from standard_broker.adapters.hyperliquid.bridge import (
    NautilusAdapterMetadata,
    NautilusHyperliquidRuntime,
    NautilusRuntimeConfig,
    NautilusRuntimeError,
    NautilusRuntimeState,
)
from standard_broker.capabilities import CapabilityDescriptor
from standard_broker.models import AccountScope, BrokerEnvironment, SignerKind
from standard_broker.runtime import (
    AccountReference,
    BrokerRuntimeSession,
    ExternalEnvironmentApproval,
    RuntimeActivationPolicy,
    SignerReference,
)


def capabilities(environment: BrokerEnvironment, revision: str = "nautilus-runtime-v1") -> CapabilityDescriptor:
    return CapabilityDescriptor(
        broker_id="hyperliquid",
        environment=environment,
        operations={
            "market_data": frozenset({"read"}),
            "order_execution": frozenset({"submit"}),
        },
        revision=revision,
    )


class FakeSignerProvider:
    def sign(self, signer: SignerReference, payload: bytes) -> bytes:
        return b"signed-by-fixture"


class FakeNautilusBackend:
    local_only = True

    def __init__(self, metadata: NautilusAdapterMetadata) -> None:
        self.metadata = metadata
        self.calls: list[tuple[str, str, object]] = []

    def invoke(self, port: str, operation: str, request: object) -> object:
        self.calls.append((port, operation, request))
        return {"native": True, "port": port, "operation": operation}


class NautilusRuntimeTests(unittest.TestCase):
    def metadata(
        self,
        *,
        environment: BrokerEnvironment = BrokerEnvironment.PAPER,
        version: str = "1.230.0",
        commit: str = "nautilus-commit",
        revision: str = "nautilus-runtime-v1",
    ) -> NautilusAdapterMetadata:
        return NautilusAdapterMetadata(
            package="nautilus-hyperliquid",
            version=version,
            commit=commit,
            capabilities=capabilities(environment, revision),
        )

    def session(self, environment: BrokerEnvironment = BrokerEnvironment.PAPER) -> BrokerRuntimeSession:
        is_paper = environment is BrokerEnvironment.PAPER
        return BrokerRuntimeSession(
            broker_id="hyperliquid",
            environment=environment,
            account=AccountReference(AccountScope.MASTER, "0xmaster"),
            signer=(
                SignerReference.paper()
                if is_paper
                else SignerReference(SignerKind.API_AGENT, "keychain", "keychain://hl-testnet")
            ),
            signer_provider=None if is_paper else FakeSignerProvider(),
            capabilities=capabilities(environment),
            execution_scope="hypercore:default",
            lifecycle_id="runtime-1",
        )

    def runtime(
        self,
        *,
        environment: BrokerEnvironment = BrokerEnvironment.PAPER,
        backend: FakeNautilusBackend | None = None,
        policy: RuntimeActivationPolicy | None = None,
    ) -> tuple[NautilusHyperliquidRuntime, FakeNautilusBackend]:
        selected_backend = backend or FakeNautilusBackend(self.metadata(environment=environment))
        runtime = NautilusHyperliquidRuntime(
            session=self.session(environment),
            backend=selected_backend,
            config=NautilusRuntimeConfig(
                expected_version="1.230.0",
                expected_commit="nautilus-commit",
                policy=policy or RuntimeActivationPolicy(),
            ),
        )
        return runtime, selected_backend

    def test_runtime_starts_ready_and_invokes_only_after_start(self) -> None:
        runtime, backend = self.runtime()

        with self.assertRaises(NautilusRuntimeError):
            runtime.invoke("market_data", "read", {"instrument_id": "BTC-USD-PERP"})
        self.assertEqual(backend.calls, [])

        health = runtime.start()
        self.assertEqual(health.state, NautilusRuntimeState.READY)
        self.assertEqual(backend.calls, [])

        receipt = runtime.invoke("market_data", "read", {"instrument_id": "BTC-USD-PERP"})

        self.assertTrue(receipt.accepted)
        self.assertEqual(receipt.port, "market_data")
        self.assertEqual(receipt.operation, "read")
        self.assertEqual(len(backend.calls), 1)

    def test_close_prevents_future_invocation(self) -> None:
        runtime, backend = self.runtime()
        runtime.start()

        health = runtime.close()

        self.assertEqual(health.state, NautilusRuntimeState.CLOSED)
        with self.assertRaises(NautilusRuntimeError):
            runtime.invoke("market_data", "read", {})
        self.assertEqual(len(backend.calls), 0)

    def test_version_mismatch_fails_before_backend_invocation(self) -> None:
        backend = FakeNautilusBackend(self.metadata(version="1.231.0"))

        with self.assertRaises(NautilusRuntimeError):
            NautilusHyperliquidRuntime(
                session=self.session(),
                backend=backend,
                config=NautilusRuntimeConfig("1.230.0", "nautilus-commit"),
            )

        self.assertEqual(backend.calls, [])

    def test_capability_mismatch_fails_before_backend_invocation(self) -> None:
        backend = FakeNautilusBackend(self.metadata(revision="different"))

        with self.assertRaises(NautilusRuntimeError):
            NautilusHyperliquidRuntime(
                session=self.session(),
                backend=backend,
                config=NautilusRuntimeConfig("1.230.0", "nautilus-commit"),
            )

        self.assertEqual(backend.calls, [])

    def test_paper_runtime_rejects_non_local_backend(self) -> None:
        backend = FakeNautilusBackend(self.metadata())
        backend.local_only = False

        with self.assertRaises(NautilusRuntimeError):
            NautilusHyperliquidRuntime(
                session=self.session(),
                backend=backend,
                config=NautilusRuntimeConfig("1.230.0", "nautilus-commit"),
            )

    def test_testnet_requires_human_approval_and_has_no_network_call_by_start(self) -> None:
        runtime, backend = self.runtime(environment=BrokerEnvironment.TESTNET)

        with self.assertRaises(NautilusRuntimeError):
            runtime.start()
        self.assertEqual(backend.calls, [])

        runtime, backend = self.runtime(
            environment=BrokerEnvironment.TESTNET,
            policy=RuntimeActivationPolicy(
                testnet_approval=ExternalEnvironmentApproval(
                    environment=BrokerEnvironment.TESTNET,
                    approval_id="approval-runtime-1",
                    release_sha="release-sha-1",
                    approved_by="park",
                    approved_at=datetime.now(UTC),
                )
            ),
        )
        health = runtime.start()

        self.assertEqual(health.environment, BrokerEnvironment.TESTNET)
        self.assertEqual(runtime.state, NautilusRuntimeState.READY)
        self.assertEqual(backend.calls, [])


if __name__ == "__main__":
    unittest.main()
