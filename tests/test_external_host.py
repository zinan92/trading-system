from datetime import UTC, datetime
from types import SimpleNamespace
import pytest

from standard_broker.capabilities import CapabilityDescriptor
from standard_broker.errors import RuntimeBoundaryError
from standard_broker.external_host import (
    ExternalBrokerBuildContext,
    ExternalBrokerHost,
    ExternalHostRequest,
    ExternalRuntimeIdentity,
)
from standard_broker.host import CanonicalHostRequest, CanonicalPortQuery
from standard_broker.models import AccountScope, BrokerEnvironment, SignerKind, Provenance
from standard_broker.runtime import (
    AccountReference,
    ExternalEnvironmentApproval,
    BrokerRuntimeSession,
    RuntimePreflight,
    SignerReference,
)
from standard_broker.adapters.hyperliquid.bridge import NautilusRuntimeReceipt


ACCOUNT = "0x" + "11" * 20
RELEASE_SHA = "a" * 40
ADAPTER_COMMIT = "b" * 40


class FakeSignerProvider:
    def sign(self, signer: SignerReference, payload: bytes) -> bytes:
        return b"fixture-signature"


class FakeRuntime:
    def __init__(
        self,
        session: BrokerRuntimeSession,
        *,
        transport_state: str = "local_fixture",
        adapter_version: str = "1.230.0",
        adapter_commit: str = ADAPTER_COMMIT,
    ) -> None:
        self.session = session
        self.transport_state = transport_state
        self.adapter_version = adapter_version
        self.adapter_commit = adapter_commit
        self.adapter_metadata = SimpleNamespace(
            package="nautilus-hyperliquid",
            version=adapter_version,
            commit=adapter_commit,
            capabilities=session.capabilities,
        )
        self.preflight_calls: list[dict[str, set[str]]] = []
        self.invoke_calls: list[tuple[str, str, object]] = []

    def preflight(self, *, required_operations: dict[str, set[str]] | None = None) -> RuntimePreflight:
        operations = required_operations or {}
        self.preflight_calls.append(operations)
        needs_credential = any(
            operation in {"submit", "cancel", "replace", "cancel_replace", "modify"}
            for values in operations.values()
            for operation in values
        )
        return RuntimePreflight(
            broker_id=self.session.broker_id,
            environment=self.session.environment,
            account_address=self.session.account.address,
            capability_revision=self.session.capabilities.revision,
            lifecycle_id=self.session.lifecycle_id,
            accepted=True,
            external_network=self.session.environment is not BrokerEnvironment.PAPER,
            credential_required=needs_credential,
            real_money_eligible=False,
            release_sha=RELEASE_SHA,
        )

    def invoke(self, port: str, operation: str, request: object) -> NautilusRuntimeReceipt:
        self.invoke_calls.append((port, operation, request))
        return NautilusRuntimeReceipt(
            broker_id=self.session.broker_id,
            environment=self.session.environment,
            port=port,
            operation=operation,
            accepted=True,
            adapter_version=self.adapter_version,
            adapter_commit=self.adapter_commit,
            invocation_performed=True,
            account_address=self.session.account.address,
            lifecycle_id=self.session.lifecycle_id,
            release_sha=RELEASE_SHA,
            provenance=Provenance(
                source="nautilus-hyperliquid.fixture",
                execution_scope=self.session.execution_scope,
                transport_state=self.transport_state,
                mapping_revision=self.session.capabilities.revision,
            ),
        )


def _session(
    *,
    environment: BrokerEnvironment = BrokerEnvironment.TESTNET,
    address: str = ACCOUNT,
    lifecycle_id: str = "lifecycle-1",
) -> BrokerRuntimeSession:
    capabilities = CapabilityDescriptor(
        broker_id="hyperliquid",
        environment=environment,
        operations={"market_data": frozenset({"ticker"})},
        revision="external-host-v1",
    )
    signer = (
        SignerReference.paper()
        if environment is BrokerEnvironment.PAPER
        else SignerReference(SignerKind.API_AGENT, "fixture", "fixture://api-agent")
    )
    return BrokerRuntimeSession(
        broker_id="hyperliquid",
        environment=environment,
        account=AccountReference(AccountScope.MASTER, address),
        signer=signer,
        signer_provider=None if environment is BrokerEnvironment.PAPER else FakeSignerProvider(),
        capabilities=capabilities,
        execution_scope="hypercore:default",
        lifecycle_id=lifecycle_id,
    )


def _context(
    session: BrokerRuntimeSession,
    *,
    approval: bool = True,
    transport_state: str = "local_fixture",
) -> ExternalBrokerBuildContext:
    approval_artifact = (
        ExternalEnvironmentApproval(
            environment=BrokerEnvironment.TESTNET,
            approval_id="approval-1",
            release_sha=RELEASE_SHA,
            approved_by="park",
            approved_at=datetime.now(UTC),
            account_address=session.account.address,
            lifecycle_id=session.lifecycle_id,
        )
        if approval and session.environment is BrokerEnvironment.TESTNET
        else None
    )
    return ExternalBrokerBuildContext(
        session=session,
        runtime_identity=ExternalRuntimeIdentity(
            adapter_id="nautilus-hyperliquid",
            version="1.230.0",
            commit=ADAPTER_COMMIT,
            mapping_revision=session.capabilities.revision,
            transport_state=transport_state,
        ),
        release_sha=RELEASE_SHA,
        approval=approval_artifact,
    )


def test_external_host_returns_canonical_identity_receipt_at_public_seam() -> None:
    session = _session(environment=BrokerEnvironment.PAPER)
    runtime = FakeRuntime(session)
    host = ExternalBrokerHost(context=_context(session), runtime=runtime)

    receipt = host.request(
        ExternalHostRequest(
            request_id="request-1",
            request=CanonicalHostRequest(
                port="market_data",
                operation="ticker",
                payload=CanonicalPortQuery(subject="SOL-USD-PERP"),
            ),
        )
    )

    assert receipt.broker_id == "hyperliquid"
    assert receipt.environment is BrokerEnvironment.PAPER
    assert receipt.account_scope is AccountScope.MASTER
    assert receipt.account_address == ACCOUNT
    assert receipt.lifecycle_id == "lifecycle-1"
    assert receipt.release_sha == RELEASE_SHA
    assert receipt.runtime_identity.adapter_id == "nautilus-hyperliquid"
    assert receipt.accepted is True
    assert receipt.network_io is False
    assert receipt.real_money_eligible is False
    assert receipt.provenance.transport_state == "local_fixture"
    assert receipt.request_digest.startswith("sha256:")
    assert receipt.receipt_digest.startswith("sha256:")
    assert len(runtime.invoke_calls) == 1


def test_paper_fixture_has_no_signer_or_network_eligibility() -> None:
    session = _session(environment=BrokerEnvironment.PAPER)
    runtime = FakeRuntime(session)
    host = ExternalBrokerHost(context=_context(session), runtime=runtime)

    receipt = host.request(
        ExternalHostRequest(
            request_id="request-paper",
            request=CanonicalHostRequest(port="market_data", operation="ticker"),
        )
    )

    assert receipt.environment is BrokerEnvironment.PAPER
    assert receipt.signer_kind is SignerKind.NONE
    assert receipt.network_io is False
    assert receipt.real_money_eligible is False


def test_external_host_rejects_secret_like_payload_before_runtime_invocation() -> None:
    session = _session(environment=BrokerEnvironment.PAPER)
    runtime = FakeRuntime(session)
    host = ExternalBrokerHost(context=_context(session), runtime=runtime)

    with pytest.raises(RuntimeBoundaryError, match="raw_payload_forbidden"):
        host.request(
            ExternalHostRequest(
                request_id="request-secret",
                request=CanonicalHostRequest(
                    port="market_data",
                    operation="ticker",
                    payload="0x" + "c" * 64,
                ),
            )
        )

    assert runtime.invoke_calls == []


def test_external_host_rejects_mainnet_context_before_runtime_creation() -> None:
    with pytest.raises(RuntimeBoundaryError, match="mainnet_not_in_external_host"):
        _context(_session(environment=BrokerEnvironment.MAINNET))


def test_external_host_rejects_runtime_session_identity_mismatch() -> None:
    context_session = _session(environment=BrokerEnvironment.PAPER, lifecycle_id="context-lifecycle")
    runtime = FakeRuntime(_session(environment=BrokerEnvironment.PAPER, lifecycle_id="runtime-lifecycle"))

    with pytest.raises(RuntimeBoundaryError, match="runtime_identity_mismatch"):
        ExternalBrokerHost(context=_context(context_session), runtime=runtime)
