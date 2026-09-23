"""Read-only Mainnet BTC sub-account profile (issue #139)."""

from datetime import UTC, datetime
import hashlib
from pathlib import Path
import tempfile

import pytest

from standard_broker.adapters.hyperliquid import (
    HYPERLIQUID_MAINNET_BTC_READONLY_PROFILE,
    HYPERLIQUID_TESTNET_PROFILE,
    MAINNET_BTC_READONLY_REVISION,
    HyperliquidMainnetReadOnlyBackendConfig,
    HyperliquidTestnetBackendConfig,
    LocalFileSecretProvider,
    NautilusHyperliquidMainnetReadOnlyBackend,
    NautilusHyperliquidRuntime,
    NautilusRuntimeConfig,
    build_hyperliquid_mainnet_readonly_host,
    mainnet_btc_readonly_capabilities,
    resolve_external_position_protection_profile,
    resolve_external_profile,
    resolve_mainnet_readonly_profile,
)
from standard_broker.adapters.hyperliquid.external import (
    NAUTILUS_HYPERLIQUID_COMMIT,
    NAUTILUS_HYPERLIQUID_VERSION,
    default_testnet_capabilities,
)
from standard_broker.adapters.hyperliquid.bridge import NautilusRuntimeError
from standard_broker.capabilities import CapabilityDescriptor
from standard_broker.errors import BrokerCapabilityError, RuntimeBoundaryError
from standard_broker.external_host import ExternalBrokerBuildContext, ExternalRuntimeIdentity
from standard_broker.models import AccountScope, BrokerEnvironment, SignerKind
from standard_broker.runtime import (
    AccountReference,
    BrokerRuntimeSession,
    ExternalEnvironmentApproval,
    RuntimeActivationPolicy,
    SignerReference,
    preflight_runtime_session,
)

from test_external_host import FakeRuntime, FakeSignerProvider, RELEASE_SHA
from test_hyperliquid_external_backend import AssetIndexInstrument, FakeClient

SUBACCOUNT = "0x" + "22" * 20
LIFECYCLE = "mainnet-dry-run-1"
SECRET_REF = "file-secret://hyperliquid-mainnet"


class BtcClient(FakeClient):
    async def load_instrument_definitions(self, **kwargs: object) -> list[object]:
        self.calls.append(("load_instrument_definitions", (), kwargs))
        return [AssetIndexInstrument("BTC", 0, size_precision=5), AssetIndexInstrument("ETH", 1, size_precision=4)]


def _session(
    *,
    scope: AccountScope = AccountScope.SUBACCOUNT,
    capabilities: CapabilityDescriptor | None = None,
    environment: BrokerEnvironment = BrokerEnvironment.MAINNET,
    signer_provider: object | None = None,
) -> BrokerRuntimeSession:
    return BrokerRuntimeSession(
        broker_id="hyperliquid",
        environment=environment,
        account=AccountReference(scope, SUBACCOUNT),
        signer=SignerReference(SignerKind.API_AGENT, "file", SECRET_REF),
        signer_provider=signer_provider or FakeSignerProvider(),
        capabilities=capabilities or mainnet_btc_readonly_capabilities(),
        execution_scope="hypercore:default",
        lifecycle_id=LIFECYCLE,
    )


def _approval(**overrides: object) -> ExternalEnvironmentApproval:
    values = dict(
        environment=BrokerEnvironment.MAINNET,
        approval_id="mainnet-readonly-approval-1",
        release_sha=RELEASE_SHA,
        approved_by="park",
        approved_at=datetime.now(UTC),
        account_address=SUBACCOUNT,
        lifecycle_id=LIFECYCLE,
    )
    values.update(overrides)
    return ExternalEnvironmentApproval(**values)  # type: ignore[arg-type]


def _context(session: BrokerRuntimeSession, *, approval: ExternalEnvironmentApproval | None = None,
             transport_state: str = "external_mainnet") -> ExternalBrokerBuildContext:
    return ExternalBrokerBuildContext(
        session=session,
        runtime_identity=ExternalRuntimeIdentity(
            adapter_id="nautilus-hyperliquid",
            version=NAUTILUS_HYPERLIQUID_VERSION,
            commit=NAUTILUS_HYPERLIQUID_COMMIT,
            mapping_revision=session.capabilities.revision,
            transport_state=transport_state,
        ),
        release_sha=RELEASE_SHA,
        approval=approval,
    )


def _provider(directory: str) -> LocalFileSecretProvider:
    path = Path(directory) / "key"
    path.write_text("HYPERLIQUID_PK=0x" + hashlib.sha256(b"mainnet-fixture").hexdigest(), encoding="utf-8")
    path.chmod(0o600)
    return LocalFileSecretProvider({SECRET_REF: path})


def _backend(directory: str, client: FakeClient, *, session: BrokerRuntimeSession | None = None):
    selected = session or _session(signer_provider=_provider(directory))
    backend = NautilusHyperliquidMainnetReadOnlyBackend(
        session=selected,
        config=HyperliquidMainnetReadOnlyBackendConfig(account_address=SUBACCOUNT),
        secrets=_provider(directory),
        client_factory=lambda private_key, account: client,
    )
    return selected, backend


# ---- 1. exact profile resolution ------------------------------------------------------

def test_mainnet_profile_resolves_only_through_its_own_exact_resolver() -> None:
    profile_id = HYPERLIQUID_MAINNET_BTC_READONLY_PROFILE.profile_id
    assert profile_id == "hyperliquid-mainnet-btc-readonly"
    assert resolve_mainnet_readonly_profile(profile_id) is HYPERLIQUID_MAINNET_BTC_READONLY_PROFILE
    with pytest.raises(RuntimeBoundaryError, match="external_profile_unsupported"):
        resolve_external_profile(profile_id)
    with pytest.raises(RuntimeBoundaryError, match="external_profile_unsupported"):
        resolve_external_position_protection_profile(profile_id)
    with pytest.raises(RuntimeBoundaryError, match="external_profile_unsupported"):
        resolve_mainnet_readonly_profile(HYPERLIQUID_TESTNET_PROFILE.profile_id)
    assert resolve_external_profile(HYPERLIQUID_TESTNET_PROFILE.profile_id) is HYPERLIQUID_TESTNET_PROFILE


# ---- 2. approval-bound Mainnet admission ----------------------------------------------

def test_mainnet_host_preflights_reads_with_a_bound_approval_and_no_real_money() -> None:
    session = _session()
    runtime = FakeRuntime(session, transport_state="external_mainnet",
                          adapter_version=NAUTILUS_HYPERLIQUID_VERSION, adapter_commit=NAUTILUS_HYPERLIQUID_COMMIT)
    host = build_hyperliquid_mainnet_readonly_host(context=_context(session, approval=_approval()), runtime=runtime)

    receipt = host.preflight(request_id="mainnet-read-1", required_operations={"account": {"read"}})

    assert receipt.accepted is True
    assert receipt.environment is BrokerEnvironment.MAINNET
    assert receipt.network_io is True
    assert receipt.real_money_eligible is False
    assert runtime.invoke_calls == []
    with pytest.raises(BrokerCapabilityError, match="capability_gap"):
        host.preflight(request_id="mainnet-write-1", required_operations={"order_execution": {"submit"}})


def test_mainnet_context_requires_an_approval() -> None:
    with pytest.raises(RuntimeBoundaryError, match="external_approval_required"):
        _context(_session())


@pytest.mark.parametrize("override", [
    {"account_address": "0x" + "33" * 20},
    {"lifecycle_id": "another-lifecycle"},
    {"release_sha": "b" * 40},
])
def test_mainnet_context_rejects_an_approval_for_another_identity(override: dict[str, object]) -> None:
    with pytest.raises(RuntimeBoundaryError, match="external_approval_identity_mismatch"):
        _context(_session(), approval=_approval(**override))


def test_testnet_approval_cannot_open_mainnet_and_mainnet_approval_must_be_bound() -> None:
    with pytest.raises(RuntimeBoundaryError, match="external_approval_invalid"):
        _context(_session(), approval=_approval(environment=BrokerEnvironment.TESTNET))
    with pytest.raises(RuntimeBoundaryError, match="external_approval_invalid"):
        _approval(account_address=None)


def test_write_capable_or_unregistered_mainnet_sessions_stay_closed() -> None:
    write_capable = CapabilityDescriptor(
        broker_id="hyperliquid",
        environment=BrokerEnvironment.MAINNET,
        operations=dict(mainnet_btc_readonly_capabilities().operations, order_execution=frozenset({"submit", "query"})),
        revision=MAINNET_BTC_READONLY_REVISION,
    )
    unregistered = CapabilityDescriptor(
        broker_id="hyperliquid",
        environment=BrokerEnvironment.MAINNET,
        operations={"account": frozenset({"read"})},
        revision="some-other-readonly-v1",
    )
    for capabilities in (write_capable, unregistered):
        with pytest.raises(RuntimeBoundaryError, match="mainnet_not_in_external_host"):
            _context(_session(capabilities=capabilities), approval=_approval())
        with pytest.raises(RuntimeBoundaryError, match="mainnet_not_in_runtime_v1"):
            preflight_runtime_session(
                _session(capabilities=capabilities),
                required_operations={},
                policy=RuntimeActivationPolicy(mainnet_approval=_approval()),
            )


def test_mainnet_context_rejects_a_testnet_transport_label() -> None:
    with pytest.raises(RuntimeBoundaryError, match="external_transport_environment_mismatch"):
        _context(_session(), approval=_approval(), transport_state="external_testnet")


def test_runtime_preflight_needs_the_mainnet_approval_for_this_account() -> None:
    session = _session()
    with pytest.raises(RuntimeBoundaryError, match="external_environment_denied"):
        preflight_runtime_session(session, required_operations={"account": {"read"}})
    with pytest.raises(RuntimeBoundaryError, match="external_environment_denied"):
        preflight_runtime_session(
            session,
            required_operations={"account": {"read"}},
            policy=RuntimeActivationPolicy(mainnet_approval=_approval(account_address="0x" + "44" * 20)),
        )
    result = preflight_runtime_session(
        session,
        required_operations={"account": {"read"}},
        policy=RuntimeActivationPolicy(mainnet_approval=_approval()),
    )
    assert result.accepted is True and result.real_money_eligible is False


# ---- 3–6. backend: sub-account, no writes, BTC only, secret handling --------------------

def test_backend_requires_mainnet_subaccount_and_the_exact_readonly_config() -> None:
    with tempfile.TemporaryDirectory() as directory:
        with pytest.raises(RuntimeBoundaryError, match="mainnet_subaccount_required"):
            _backend(directory, BtcClient(), session=_session(scope=AccountScope.MASTER))
        testnet_session = _session(environment=BrokerEnvironment.TESTNET, capabilities=default_testnet_capabilities())
        with pytest.raises(RuntimeBoundaryError, match="external_environment_invalid"):
            _backend(directory, BtcClient(), session=testnet_session)
        with pytest.raises(RuntimeBoundaryError, match="mainnet_config_required"):
            NautilusHyperliquidMainnetReadOnlyBackend(
                session=_session(),
                config=HyperliquidTestnetBackendConfig(account_address=SUBACCOUNT),  # type: ignore[arg-type]
                secrets=_provider(directory),
            )
        with pytest.raises(RuntimeBoundaryError, match="mainnet_capabilities_invalid"):
            HyperliquidMainnetReadOnlyBackendConfig(
                account_address=SUBACCOUNT,
                capabilities=CapabilityDescriptor(
                    broker_id="hyperliquid",
                    environment=BrokerEnvironment.MAINNET,
                    operations={"order_execution": frozenset({"submit"})},
                    revision=MAINNET_BTC_READONLY_REVISION,
                ),
            )


@pytest.mark.parametrize("port,operation", [
    ("order_execution", "submit"),
    ("order_execution", "cancel"),
    ("order_execution", "replace"),
    ("protection_order", "submit"),
    ("protection_order", "cancel"),
])
def test_backend_refuses_every_write_even_when_called_directly(port: str, operation: str) -> None:
    with tempfile.TemporaryDirectory() as directory:
        client = BtcClient()
        _, backend = _backend(directory, client)
        backend.activate(release_sha=RELEASE_SHA)
        with pytest.raises(RuntimeBoundaryError, match="mainnet_read_only"):
            backend.invoke(port, operation, {"instrument_id": "BTC-USD-PERP", "protectionId": "p-1"})
        with pytest.raises(RuntimeBoundaryError, match="mainnet_read_only"):
            backend._submit({"instrument_id": "BTC-USD-PERP"})
        assert not any(name in {"submit_order", "cancel_order", "modify_order"} for name, _, _ in client.calls)


def test_backend_reads_btc_for_the_subaccount_with_mainnet_provenance() -> None:
    with tempfile.TemporaryDirectory() as directory:
        client = BtcClient()
        seen: list[str] = []
        session = _session(signer_provider=_provider(directory))
        backend = NautilusHyperliquidMainnetReadOnlyBackend(
            session=session,
            config=HyperliquidMainnetReadOnlyBackendConfig(account_address=SUBACCOUNT),
            secrets=_provider(directory),
            client_factory=lambda private_key, account: seen.append(account) or client,
        )
        backend.activate(release_sha=RELEASE_SHA)

        positions = backend.invoke("account", "positions", {"instrument_id": "BTC-USD-PERP"})
        account = backend.invoke("account", "read", {})

        assert seen == [SUBACCOUNT]
        assert ("request_position_status_reports", ("BTC-USD-PERP.HYPERLIQUID",), {}) in client.calls
        for result in (positions, account):
            assert result["provenance"].transport_state == "external_mainnet"
            assert result["provenance"].source == "nautilus-hyperliquid.mainnet"
        with pytest.raises(RuntimeBoundaryError, match="mainnet_instrument_not_allowed"):
            backend.invoke("account", "positions", {"instrument_id": "ETH-USD-PERP"})


def test_secret_never_appears_in_backend_errors_or_repr() -> None:
    with tempfile.TemporaryDirectory() as directory:
        secret = hashlib.sha256(b"mainnet-fixture").hexdigest()
        _, backend = _backend(directory, BtcClient())
        backend.activate(release_sha=RELEASE_SHA)
        with pytest.raises(RuntimeBoundaryError) as raised:
            backend.invoke("order_execution", "submit", {})
        assert secret not in str(raised.value)
        assert secret not in repr(backend)


# ---- 7. runtime carries backend-derived Mainnet transport ------------------------------

def test_runtime_labels_mainnet_transport_and_refuses_testnet_backends_on_mainnet() -> None:
    with tempfile.TemporaryDirectory() as directory:
        session, backend = _backend(directory, BtcClient())
        config = NautilusRuntimeConfig(
            expected_version=NAUTILUS_HYPERLIQUID_VERSION,
            expected_commit=NAUTILUS_HYPERLIQUID_COMMIT,
            policy=RuntimeActivationPolicy(mainnet_approval=_approval()),
            expected_release_sha=RELEASE_SHA,
        )
        runtime = NautilusHyperliquidRuntime(session=session, backend=backend, config=config)
        assert runtime.transport_state == "external_mainnet"
        health = runtime.start()
        assert health.environment is BrokerEnvironment.MAINNET
        host = build_hyperliquid_mainnet_readonly_host(context=_context(session, approval=_approval()), runtime=runtime)
        assert host.runtime_identity.transport_state == "external_mainnet"

        class LooksLikeTestnet:
            local_only = False
            external_network = True
            metadata = backend.metadata

            def invoke(self, port: str, operation: str, request: object) -> object:
                raise AssertionError("must not be reached")

        with pytest.raises(NautilusRuntimeError, match="mainnet_backend_boundary_invalid"):
            NautilusHyperliquidRuntime(session=session, backend=LooksLikeTestnet(), config=config)


def test_runtime_start_without_mainnet_approval_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        session, backend = _backend(directory, BtcClient())
        runtime = NautilusHyperliquidRuntime(
            session=session,
            backend=backend,
            config=NautilusRuntimeConfig(
                expected_version=NAUTILUS_HYPERLIQUID_VERSION,
                expected_commit=NAUTILUS_HYPERLIQUID_COMMIT,
                expected_release_sha=RELEASE_SHA,
            ),
        )
        with pytest.raises(NautilusRuntimeError, match="external_environment_denied"):
            runtime.start()


# ---- host -> real bridge -> Mainnet backend -> canonical fact ---------------------------

class AccountClient(BtcClient):
    async def request_account_state(self) -> object:
        self.calls.append(("request_account_state", (), {}))
        return {
            "type": "AccountState",
            "account_id": f"{SUBACCOUNT}-HYPERLIQUID",
            "event_id": "account-event-1",
            "reported": True,
            "base_currency": "USDC",
            "balances": [{"currency": "USDC", "total": "500.00", "free": "480.00", "locked": "20.00"}],
            "margins": [{"currency": "USDC", "initial": "20.00", "maintenance": "10.00"}],
        }


def test_account_fact_crosses_host_bridge_and_backend_labelled_mainnet() -> None:
    from datetime import timedelta

    from standard_broker.adapters.hyperliquid.read_facts import HyperliquidExternalFactAdapter
    from standard_broker.external_host import ExternalHostRequest
    from standard_broker.host import CanonicalHostRequest, CanonicalPortQuery
    from standard_broker.market_data import FreshnessPolicy

    with tempfile.TemporaryDirectory() as directory:
        client = AccountClient()
        session, backend = _backend(directory, client)
        runtime = NautilusHyperliquidRuntime(
            session=session,
            backend=backend,
            config=NautilusRuntimeConfig(
                expected_version=NAUTILUS_HYPERLIQUID_VERSION,
                expected_commit=NAUTILUS_HYPERLIQUID_COMMIT,
                policy=RuntimeActivationPolicy(mainnet_approval=_approval()),
                expected_release_sha=RELEASE_SHA,
            ),
        )
        runtime.start()
        context = _context(session, approval=_approval())
        host = build_hyperliquid_mainnet_readonly_host(context=context, runtime=runtime)
        mapper = HyperliquidExternalFactAdapter(context=context, instruments=None,
                                                freshness_policy=FreshnessPolicy(timedelta(minutes=2)))

        envelope = host.read_fact(
            request=ExternalHostRequest(
                request_id="mainnet-account-1",
                request=CanonicalHostRequest(port="account", operation="read", payload=CanonicalPortQuery(kind="account")),
            ),
            mapper=lambda raw: mapper.map_account(request_id="mainnet-account-1", raw=raw),
        )

        assert envelope.data.environment is BrokerEnvironment.MAINNET
        assert envelope.data.equity == __import__("decimal").Decimal("500.00")
        assert envelope.provenance.transport_state == "external_mainnet"
        assert ("request_account_state", (), {}) in client.calls

        with pytest.raises(BrokerCapabilityError, match="capability_gap"):
            host.request(
                ExternalHostRequest(
                    request_id="mainnet-submit-1",
                    request=CanonicalHostRequest(port="order_execution", operation="submit"),
                )
            )
        assert not any(name == "submit_order" for name, _, _ in client.calls)
