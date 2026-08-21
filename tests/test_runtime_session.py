import unittest

from standard_broker.capabilities import CapabilityDescriptor
from standard_broker.errors import BrokerCapabilityError
from standard_broker.models import AccountScope, BrokerEnvironment, SignerKind
from standard_broker.runtime import (
    AccountReference,
    BrokerRuntimeSession,
    RuntimeActivationPolicy,
    RuntimeBoundaryError,
    RuntimeOperationGuard,
    SignerReference,
    preflight_runtime_session,
)


def capabilities(environment: BrokerEnvironment, revision: str = "runtime-v1") -> CapabilityDescriptor:
    return CapabilityDescriptor(
        broker_id="hyperliquid",
        environment=environment,
        operations={
            "market_data": frozenset({"read"}),
            "order_execution": frozenset({"submit", "query", "cancel", "replace"}),
        },
        revision=revision,
    )


class FakeSignerProvider:
    def sign(self, signer: SignerReference, payload: bytes) -> bytes:
        return b"signed-by-fixture"


class RuntimeSessionTests(unittest.TestCase):
    def session(
        self,
        *,
        environment: BrokerEnvironment = BrokerEnvironment.TESTNET,
        signer: SignerReference | None = None,
        with_signer: bool = True,
        with_provider: bool = True,
        capability_profile: CapabilityDescriptor | None = None,
    ) -> BrokerRuntimeSession:
        if environment is not BrokerEnvironment.PAPER and signer is None and with_signer:
            signer = SignerReference(SignerKind.API_AGENT, "keychain", "keychain://hl-testnet")
        selected_signer = signer or SignerReference.paper()
        profile = capability_profile or capabilities(environment)
        return BrokerRuntimeSession(
            broker_id="hyperliquid",
            environment=environment,
            account=AccountReference(AccountScope.MASTER, "0xmaster"),
            signer=selected_signer,
            signer_provider=FakeSignerProvider()
            if environment is not BrokerEnvironment.PAPER and with_provider
            else None,
            capabilities=profile,
            execution_scope="hypercore:default",
            lifecycle_id="session-1",
        )

    def test_session_requires_explicit_identity_and_exposes_canonical_identity(self) -> None:
        session = self.session()

        self.assertEqual(session.broker_id, "hyperliquid")
        self.assertEqual(session.environment, BrokerEnvironment.TESTNET)
        self.assertEqual(session.account.address, "0xmaster")
        self.assertEqual(session.identity.account_address, "0xmaster")
        self.assertEqual(session.identity.execution_scope, "hypercore:default")

    def test_paper_session_rejects_signer(self) -> None:
        with self.assertRaises(RuntimeBoundaryError) as raised:
            self.session(
                environment=BrokerEnvironment.PAPER,
                signer=SignerReference(SignerKind.API_AGENT, "keychain", "keychain://hl-paper"),
            )

        self.assertEqual(raised.exception.reason_code, "paper_signer_forbidden")

    def test_paper_session_has_explicit_no_signer_binding(self) -> None:
        session = self.session(environment=BrokerEnvironment.PAPER)

        self.assertEqual(session.signer.kind, SignerKind.NONE)
        self.assertIsNone(session.signer_provider)

    def test_signer_reference_rejects_raw_private_key_material(self) -> None:
        with self.assertRaises(RuntimeBoundaryError) as raised:
            SignerReference(SignerKind.API_AGENT, "inline", "a" * 64)

        self.assertEqual(raised.exception.reason_code, "raw_credential_forbidden")

        with self.assertRaises(RuntimeBoundaryError) as raised:
            SignerReference(SignerKind.API_AGENT, "inline", "not-an-opaque-reference")

        self.assertEqual(raised.exception.reason_code, "raw_credential_forbidden")

    def test_testnet_preflight_is_denied_without_external_activation_policy(self) -> None:
        session = self.session(
            signer=SignerReference(SignerKind.API_AGENT, "keychain", "keychain://hl-testnet"),
        )

        with self.assertRaises(RuntimeBoundaryError) as raised:
            preflight_runtime_session(
                session,
                required_operations={"market_data": {"read"}},
            )

        self.assertEqual(raised.exception.reason_code, "external_environment_denied")

    def test_testnet_preflight_requires_explicit_policy_and_has_no_paper_claim(self) -> None:
        session = self.session(
            signer=SignerReference(SignerKind.API_AGENT, "keychain", "keychain://hl-testnet"),
        )

        result = preflight_runtime_session(
            session,
            required_operations={"market_data": {"read"}},
            policy=RuntimeActivationPolicy(allow_testnet=True),
        )

        self.assertTrue(result.accepted)
        self.assertTrue(result.external_network)
        self.assertFalse(result.credential_required)
        self.assertNotEqual(result.environment, BrokerEnvironment.PAPER)
        self.assertFalse(result.real_money_eligible)

    def test_write_preflight_requires_signer(self) -> None:
        with self.assertRaises(RuntimeBoundaryError) as raised:
            self.session(with_signer=False)

        self.assertEqual(raised.exception.reason_code, "signer_reference_required")

    def test_external_session_requires_signer_provider(self) -> None:
        with self.assertRaises(RuntimeBoundaryError) as raised:
            self.session(with_provider=False)

        self.assertEqual(raised.exception.reason_code, "signer_provider_required")

    def test_external_write_preflight_marks_credential_requirement(self) -> None:
        session = self.session(
            signer=SignerReference(SignerKind.API_AGENT, "keychain", "keychain://hl-testnet"),
        )

        result = preflight_runtime_session(
            session,
            required_operations={"order_execution": {"submit"}},
            policy=RuntimeActivationPolicy(allow_testnet=True),
        )

        self.assertTrue(result.credential_required)

    def test_mainnet_is_not_an_activation_option_in_runtime_v1(self) -> None:
        session = self.session(
            environment=BrokerEnvironment.MAINNET,
            signer=SignerReference(SignerKind.API_AGENT, "keychain", "keychain://hl-mainnet"),
        )

        with self.assertRaises(RuntimeBoundaryError) as raised:
            preflight_runtime_session(
                session,
                required_operations={"market_data": {"read"}},
                policy=RuntimeActivationPolicy(allow_testnet=True),
            )

        self.assertEqual(raised.exception.reason_code, "mainnet_not_in_runtime_v1")

    def test_preflight_blocks_backend_before_invocation(self) -> None:
        class Backend:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str, object]] = []

            def invoke(self, port: str, operation: str, request: object) -> object:
                self.calls.append((port, operation, request))
                return {"accepted": True}

        backend = Backend()
        guard = RuntimeOperationGuard(
            session=self.session(),
            backend=backend,
        )

        with self.assertRaises(RuntimeBoundaryError):
            guard.invoke("order_execution", "submit", {"request_id": "r-1"})

        self.assertEqual(backend.calls, [])

    def test_capability_gap_blocks_backend_before_invocation(self) -> None:
        class Backend:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str, object]] = []

            def invoke(self, port: str, operation: str, request: object) -> object:
                self.calls.append((port, operation, request))
                return {"accepted": True}

        backend = Backend()
        guard = RuntimeOperationGuard(
            session=self.session(
                capability_profile=CapabilityDescriptor(
                    broker_id="hyperliquid",
                    environment=BrokerEnvironment.TESTNET,
                    operations={"market_data": frozenset({"read"})},
                    revision="runtime-v1",
                )
            ),
            backend=backend,
            policy=RuntimeActivationPolicy(allow_testnet=True),
        )

        with self.assertRaises(BrokerCapabilityError):
            guard.invoke("order_execution", "submit", {"request_id": "r-2"})

        self.assertEqual(backend.calls, [])

    def test_raw_signed_payload_is_rejected_before_backend_invocation(self) -> None:
        class Backend:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str, object]] = []

            def invoke(self, port: str, operation: str, request: object) -> object:
                self.calls.append((port, operation, request))
                return {"accepted": True}

        backend = Backend()
        guard = RuntimeOperationGuard(
            session=self.session(environment=BrokerEnvironment.PAPER),
            backend=backend,
        )

        with self.assertRaises(RuntimeBoundaryError):
            guard.invoke("market_data", "read", {"signed_payload": b"secret"})

        self.assertEqual(backend.calls, [])

    def test_capability_profile_is_bound_to_session(self) -> None:
        profile = capabilities(BrokerEnvironment.TESTNET, revision="bound-profile")
        session = self.session(capability_profile=profile)

        self.assertIs(session.capabilities, profile)
        self.assertEqual(session.capability_revision, "bound-profile")


if __name__ == "__main__":
    unittest.main()
