from datetime import UTC, datetime

import pytest

from standard_broker.adapters.hyperliquid.external import (
    NAUTILUS_HYPERLIQUID_COMMIT,
    NAUTILUS_HYPERLIQUID_VERSION,
    default_testnet_capabilities,
)
from standard_broker.errors import BrokerCapabilityError, RuntimeBoundaryError
from standard_broker.external_host import (
    ExternalBrokerBuildContext,
    ExternalBrokerHost,
    ExternalHostRequest,
    ExternalRuntimeIdentity,
)
from standard_broker.adapters.hyperliquid.profile import (
    build_hyperliquid_testnet_host,
    resolve_external_profile,
)
from standard_broker.host import CanonicalHostRequest
from standard_broker.models import AccountScope, BrokerEnvironment, SignerKind
from standard_broker.runtime import (
    AccountReference,
    BrokerRuntimeSession,
    ExternalEnvironmentApproval,
    SignerReference,
)

from test_external_host import ACCOUNT, FakeRuntime, FakeSignerProvider, RELEASE_SHA


def _profile_session(*, capabilities=None, execution_scope: str = "hypercore:default") -> BrokerRuntimeSession:
    selected = capabilities or default_testnet_capabilities()
    return BrokerRuntimeSession(
        broker_id="hyperliquid",
        environment=BrokerEnvironment.TESTNET,
        account=AccountReference(AccountScope.MASTER, ACCOUNT),
        signer=SignerReference(SignerKind.API_AGENT, "fixture", "fixture://api-agent"),
        signer_provider=FakeSignerProvider(),
        capabilities=selected,
        execution_scope=execution_scope,
        lifecycle_id="profile-lifecycle-1",
    )


def _profile_context(session: BrokerRuntimeSession, *, approval: bool = True) -> ExternalBrokerBuildContext:
    artifact = (
        ExternalEnvironmentApproval(
            environment=BrokerEnvironment.TESTNET,
            approval_id="profile-approval-1",
            release_sha=RELEASE_SHA,
            approved_by="park",
            approved_at=datetime.now(UTC),
            account_address=ACCOUNT,
            lifecycle_id=session.lifecycle_id,
        )
        if approval
        else None
    )
    return ExternalBrokerBuildContext(
        session=session,
        runtime_identity=ExternalRuntimeIdentity(
            adapter_id="nautilus-hyperliquid",
            version=NAUTILUS_HYPERLIQUID_VERSION,
            commit=NAUTILUS_HYPERLIQUID_COMMIT,
            mapping_revision=session.capabilities.revision,
            transport_state="external_testnet",
        ),
        release_sha=RELEASE_SHA,
        approval=artifact,
    )


def _profile_runtime(session: BrokerRuntimeSession) -> FakeRuntime:
    return FakeRuntime(
        session,
        transport_state="external_testnet",
        adapter_version=NAUTILUS_HYPERLIQUID_VERSION,
        adapter_commit=NAUTILUS_HYPERLIQUID_COMMIT,
    )


def test_exact_testnet_profile_preflights_without_invoking_transport() -> None:
    session = _profile_session()
    runtime = _profile_runtime(session)
    host = build_hyperliquid_testnet_host(context=_profile_context(session), runtime=runtime)

    receipt = host.preflight(
        request_id="profile-preflight-1",
        required_operations={"market_data": {"ticker"}},
    )

    assert receipt.accepted is True
    assert receipt.environment is BrokerEnvironment.TESTNET
    assert receipt.network_io is True
    assert runtime.invoke_calls == []


def test_exact_testnet_profile_requires_matching_approval_before_transport() -> None:
    session = _profile_session()
    runtime = _profile_runtime(session)
    host = build_hyperliquid_testnet_host(
        context=_profile_context(session, approval=False),
        runtime=runtime,
    )

    with pytest.raises(RuntimeBoundaryError, match="external_approval_required"):
        host.request(
            ExternalHostRequest(
                request_id="profile-no-approval",
                request=CanonicalHostRequest(port="market_data", operation="ticker"),
            )
        )
    assert runtime.invoke_calls == []


def test_unknown_external_profile_does_not_fallback() -> None:
    with pytest.raises(RuntimeBoundaryError, match="external_profile_unsupported"):
        resolve_external_profile("hyperliquid-testnet-unknown")


def test_exact_profile_rejects_wrong_execution_scope() -> None:
    session = _profile_session(execution_scope="wrong-scope")
    runtime = _profile_runtime(session)

    with pytest.raises(RuntimeBoundaryError, match="external_profile_mismatch"):
        build_hyperliquid_testnet_host(context=_profile_context(session), runtime=runtime)


def test_exact_profile_rejects_missing_requested_capability_before_transport() -> None:
    session = _profile_session()
    runtime = _profile_runtime(session)
    host = build_hyperliquid_testnet_host(context=_profile_context(session), runtime=runtime)

    with pytest.raises(BrokerCapabilityError, match="capability_gap"):
        host.preflight(
            request_id="profile-capability-gap",
            required_operations={"protection_order": {"submit"}},
        )
    assert runtime.preflight_calls == []


def test_direct_testnet_host_construction_requires_exact_profile() -> None:
    session = _profile_session()

    with pytest.raises(RuntimeBoundaryError, match="external_profile_required"):
        ExternalBrokerHost(context=_profile_context(session), runtime=_profile_runtime(session))
