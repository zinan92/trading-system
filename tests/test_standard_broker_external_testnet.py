from dataclasses import replace
from datetime import UTC, datetime
import json
import pytest

from standard_broker import (
    AccountReference,
    AccountScope,
    BrokerEnvironment,
    BrokerRuntimeSession,
    CapabilityDescriptor,
    ExternalBrokerBuildContext,
    ExternalBrokerHost,
    ExternalEnvironmentApproval,
    ExternalRuntimeIdentity,
    ProtectionCapabilityMatrix,
    RuntimePreflight,
    SignerKind,
    SignerReference,
)
from standard_broker.adapters.hyperliquid import (
    HYPERLIQUID_TESTNET_PROFILE,
    build_hyperliquid_testnet_host,
)

from services.broker_composition import BrokerBuildContext, build_broker_execution_port
from services.broker_port import BrokerOrderRequest, UnsupportedBrokerCapability


ACCOUNT = "0x" + "12" * 20
RELEASE_SHA = "a" * 40
STANDARD_BROKER_SHA = "7a23054d3f8bcf4e3a17537dc3b8d3ebd361a70b"


class _SignerProvider:
    def sign(self, signer: SignerReference, payload: bytes) -> bytes:
        del signer, payload
        return b"fixture-signature"


class _ExternalRuntime:
    transport_state = "external_testnet"

    def __init__(self, session: BrokerRuntimeSession) -> None:
        self.session = session
        self.preflight_calls: list[dict[str, set[str]]] = []
        self.invoke_calls: list[tuple[str, str, object]] = []

    def preflight(
        self,
        *,
        required_operations: dict[str, set[str]] | None = None,
    ) -> RuntimePreflight:
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
            external_network=True,
            credential_required=needs_credential,
            real_money_eligible=False,
            release_sha=RELEASE_SHA,
        )

    def invoke(self, port: str, operation: str, request: object) -> object:
        self.invoke_calls.append((port, operation, request))
        raise AssertionError("external bridge preflight must not invoke a Broker operation")


def _host():
    profile = HYPERLIQUID_TESTNET_PROFILE
    session = BrokerRuntimeSession(
        broker_id="hyperliquid",
        environment=BrokerEnvironment.TESTNET,
        account=AccountReference(AccountScope.MASTER, ACCOUNT),
        signer=SignerReference(SignerKind.API_AGENT, "fixture", "fixture://api-agent"),
        signer_provider=_SignerProvider(),
        capabilities=profile.capabilities,
        execution_scope=profile.execution_scope,
        lifecycle_id="trading-system-external-host-1",
    )
    approval = ExternalEnvironmentApproval(
        environment=BrokerEnvironment.TESTNET,
        approval_id="trading-system-external-approval-1",
        release_sha=RELEASE_SHA,
        approved_by="park",
        approved_at=datetime.now(UTC),
        account_address=ACCOUNT,
        lifecycle_id=session.lifecycle_id,
    )
    context = ExternalBrokerBuildContext(
        session=session,
        runtime_identity=ExternalRuntimeIdentity(
            adapter_id=profile.adapter_id,
            version=profile.version,
            commit=profile.commit,
            mapping_revision=profile.mapping_revision,
            transport_state=profile.transport_state,
        ),
        release_sha=RELEASE_SHA,
        approval=approval,
    )
    runtime = _ExternalRuntime(session)
    return build_hyperliquid_testnet_host(context=context, runtime=runtime), runtime


def _host_for_profile(profile):
    session = BrokerRuntimeSession(
        broker_id="hyperliquid",
        environment=BrokerEnvironment.TESTNET,
        account=AccountReference(AccountScope.MASTER, ACCOUNT),
        signer=SignerReference(SignerKind.API_AGENT, "fixture", "fixture://api-agent"),
        signer_provider=_SignerProvider(),
        capabilities=profile.capabilities,
        execution_scope=profile.execution_scope,
        lifecycle_id="trading-system-external-host-1",
    )
    approval = ExternalEnvironmentApproval(
        environment=BrokerEnvironment.TESTNET,
        approval_id="trading-system-external-approval-1",
        release_sha=RELEASE_SHA,
        approved_by="park",
        approved_at=datetime.now(UTC),
        account_address=ACCOUNT,
        lifecycle_id=session.lifecycle_id,
    )
    context = ExternalBrokerBuildContext(
        session=session,
        runtime_identity=ExternalRuntimeIdentity(
            adapter_id=profile.adapter_id,
            version=profile.version,
            commit=profile.commit,
            mapping_revision=profile.mapping_revision,
            transport_state=profile.transport_state,
        ),
        release_sha=RELEASE_SHA,
        approval=approval,
    )
    runtime = _ExternalRuntime(session)
    return ExternalBrokerHost(context=context, runtime=runtime, profile=profile), runtime


def _context(tmp_path, host) -> BrokerBuildContext:
    return BrokerBuildContext(
        output_root=tmp_path / "outputs",
        execution_mode="live",
        live_trading_enabled=False,
        broker_config={
            "provider": "standard_broker",
            "broker_id": "hyperliquid",
            "environment": "testnet",
            "transport_profile": "hyperliquid-testnet-default",
            "external_host": host,
            "account_id": ACCOUNT,
            "runtime_id": "trading-system-external-host-1",
            "release_sha": RELEASE_SHA,
            "execution_scope": "hypercore:default",
            "standard_broker_release_sha": STANDARD_BROKER_SHA,
            "instrument_binding": {
                "instrument_id": "PAXG-USD-PERP",
                "broker_symbol": "PAXG",
                "price_tick": "0.001",
                "quantity_step": "0.001",
                "contract_multiplier": "1",
                "minimum_quantity": "0.001",
                "mapping_revision": "hyperliquid-testnet-runtime-v1",
                "supported_order_types": ["limit"],
            },
            "market_source": {
                "source_id": "hyperliquid.external_testnet",
                "broker_id": "hyperliquid",
                "environment": "testnet",
                "instrument_id": "PAXG-USD-PERP",
                "execution_venue": True,
            },
        },
    )


def test_external_testnet_bridge_preflights_public_host_without_broker_invocation(tmp_path) -> None:
    host, runtime = _host()

    adapter = build_broker_execution_port(_context(tmp_path, host))
    preflight = adapter.preflight()

    assert adapter.name == "standard_broker_external_testnet"
    assert preflight["host_ready"] is True
    assert preflight["strategy_ready"] is False
    assert preflight["protection_ready"] is False
    assert preflight["account_read_ready"] is False
    assert preflight["order_execution_ready"] is False
    assert preflight["upstream_account_read_ready"] is True
    assert preflight["upstream_instrument_read_ready"] is True
    assert preflight["upstream_order_execution_ready"] is True
    assert preflight["broker_operation_invoked"] is False
    assert preflight["transport_profile"] == "hyperliquid-testnet-default"
    assert preflight["transport_state"] == "external_testnet"
    assert preflight["instrument_id"] == "PAXG-USD-PERP"
    assert preflight["instrument_binding"]["broker_symbol"] == "PAXG"
    assert preflight["instrument_read_ready"] is True
    assert preflight["market_source"]["source_id"] == "hyperliquid.external_testnet"
    assert preflight["market_source_ready"] is True
    assert preflight["network_io"] is True
    assert preflight["real_money_eligible"] is False
    assert preflight["standard_broker_release_sha"] == STANDARD_BROKER_SHA
    assert preflight["upstream_receipt_digest"].startswith("sha256:")
    assert runtime.invoke_calls == []


def test_external_testnet_bridge_descriptor_exposes_exact_nonsecret_profile(tmp_path) -> None:
    host, _ = _host()

    descriptor = build_broker_execution_port(_context(tmp_path, host)).descriptor.to_dict()

    assert descriptor["broker_id"] == "hyperliquid"
    assert descriptor["transport_profile"] == "hyperliquid-testnet-default"
    assert descriptor["transport_state"] == "external_testnet"
    assert descriptor["instrument_binding"]["instrument_id"] == "PAXG-USD-PERP"
    assert descriptor["market_source"]["source_id"] == "hyperliquid.external_testnet"
    assert descriptor["credential_env_names"] == []
    assert "fixture://api-agent" not in json.dumps(descriptor, sort_keys=True)


def test_external_testnet_bridge_blocks_order_submission_before_broker_invocation(tmp_path) -> None:
    host, runtime = _host()
    adapter = build_broker_execution_port(_context(tmp_path, host))

    try:
        adapter.submit_order(
            BrokerOrderRequest(
                run_date="2026-08-23",
                ticket={"ticket_id": "external-order-forbidden"},
            )
        )
    except UnsupportedBrokerCapability as exc:
        assert "preflight-only" in str(exc)
    else:
        raise AssertionError("external host bridge must keep order submission blocked")

    assert runtime.invoke_calls == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("account_id", "0x" + "34" * 20),
        ("runtime_id", "wrong-runtime"),
        ("release_sha", "b" * 40),
        ("standard_broker_release_sha", "c" * 40),
    ],
)
def test_external_testnet_bridge_rejects_identity_drift_before_broker_invocation(
    tmp_path,
    field: str,
    value: str,
) -> None:
    host, runtime = _host()
    config = dict(_context(tmp_path, host).broker_config)
    config[field] = value

    with pytest.raises(RuntimeError, match="external|dependency SHA|identity"):
        build_broker_execution_port(
            BrokerBuildContext(
                output_root=tmp_path / "outputs",
                execution_mode="live",
                live_trading_enabled=False,
                broker_config=config,
            )
        )

    assert runtime.invoke_calls == []


def test_external_testnet_profile_never_falls_back_to_local_fixture(tmp_path) -> None:
    host, runtime = _host()
    config = dict(_context(tmp_path, host).broker_config)
    config["transport_profile"] = "local_fixture_v1"

    with pytest.raises(ValueError, match="external host markers require.*external profile"):
        build_broker_execution_port(
            BrokerBuildContext(
                output_root=tmp_path / "outputs",
                execution_mode="live",
                live_trading_enabled=False,
                broker_config=config,
            )
        )

    assert runtime.invoke_calls == []


def test_external_testnet_bridge_rejects_credential_configuration(tmp_path) -> None:
    host, runtime = _host()
    config = dict(_context(tmp_path, host).broker_config)
    config["credential_source"] = "HL_TESTNET_CREDENTIAL"

    with pytest.raises(RuntimeError, match="forbids credential"):
        build_broker_execution_port(
            BrokerBuildContext(
                output_root=tmp_path / "outputs",
                execution_mode="live",
                live_trading_enabled=False,
                broker_config=config,
            )
        )

    assert runtime.invoke_calls == []


@pytest.mark.parametrize("field", ["instrument_binding", "market_source"])
def test_external_testnet_bridge_requires_explicit_market_binding(tmp_path, field: str) -> None:
    host, runtime = _host()
    config = dict(_context(tmp_path, host).broker_config)
    config.pop(field)

    with pytest.raises(RuntimeError, match="missing|market_source"):
        build_broker_execution_port(
            BrokerBuildContext(
                output_root=tmp_path / "outputs",
                execution_mode="live",
                live_trading_enabled=False,
                broker_config=config,
            )
        )

    assert runtime.invoke_calls == []


def test_external_testnet_bridge_rejects_market_binding_drift(tmp_path) -> None:
    host, runtime = _host()
    config = dict(_context(tmp_path, host).broker_config)
    config["market_source"] = {
        **config["market_source"],
        "instrument_id": "BTC-USD-PERP",
    }

    with pytest.raises(RuntimeError, match="market_source_binding_mismatch"):
        build_broker_execution_port(
            BrokerBuildContext(
                output_root=tmp_path / "outputs",
                execution_mode="live",
                live_trading_enabled=False,
                broker_config=config,
            )
        )

    assert runtime.invoke_calls == []


def test_external_testnet_bridge_rejects_instrument_mapping_revision_drift(tmp_path) -> None:
    host, runtime = _host()
    config = dict(_context(tmp_path, host).broker_config)
    config["instrument_binding"] = {
        **config["instrument_binding"],
        "mapping_revision": "stale-revision",
    }

    with pytest.raises(RuntimeError, match="instrument_mapping_revision_mismatch"):
        build_broker_execution_port(
            BrokerBuildContext(
                output_root=tmp_path / "outputs",
                execution_mode="live",
                live_trading_enabled=False,
                broker_config=config,
            )
        )

    assert runtime.invoke_calls == []


def test_external_testnet_bridge_rejects_unsupported_market_source(tmp_path) -> None:
    host, runtime = _host()
    config = dict(_context(tmp_path, host).broker_config)
    config["market_source"] = {
        **config["market_source"],
        "source_id": "binance_usdm_futures",
    }

    with pytest.raises(RuntimeError, match="unsupported_market_source"):
        build_broker_execution_port(
            BrokerBuildContext(
                output_root=tmp_path / "outputs",
                execution_mode="live",
                live_trading_enabled=False,
                broker_config=config,
            )
        )

    assert runtime.invoke_calls == []


@pytest.mark.parametrize(
    "profile",
    [
        replace(
            HYPERLIQUID_TESTNET_PROFILE,
            adapter_id="different-adapter",
        ),
        replace(
            HYPERLIQUID_TESTNET_PROFILE,
            version="9.9.9",
        ),
        replace(
            HYPERLIQUID_TESTNET_PROFILE,
            commit="d" * 40,
        ),
        replace(
            HYPERLIQUID_TESTNET_PROFILE,
            mapping_revision="drift-v2",
            capabilities=CapabilityDescriptor(
                broker_id="hyperliquid",
                environment=BrokerEnvironment.TESTNET,
                operations=HYPERLIQUID_TESTNET_PROFILE.capabilities.operations,
                revision="drift-v2",
            ),
        ),
        replace(
            HYPERLIQUID_TESTNET_PROFILE,
            capabilities=CapabilityDescriptor(
                broker_id="hyperliquid",
                environment=BrokerEnvironment.TESTNET,
                operations={"market_data": frozenset({"ticker"})},
                revision=HYPERLIQUID_TESTNET_PROFILE.capabilities.revision,
            ),
        ),
    ],
)
def test_external_testnet_bridge_rejects_runtime_or_capability_profile_drift(
    tmp_path,
    profile,
) -> None:
    host, runtime = _host_for_profile(profile)

    with pytest.raises(RuntimeError, match="runtime|capability|identity"):
        build_broker_execution_port(_context(tmp_path, host))

    assert runtime.invoke_calls == []


@pytest.mark.parametrize(
    "values",
    [
        {"reduce_only_close": True},
        {
            **dict(HYPERLIQUID_TESTNET_PROFILE.protection_capabilities.values),
            "future_false_capability": False,
        },
    ],
)
def test_external_testnet_bridge_rejects_incomplete_or_extra_protection_gap_matrix(
    tmp_path,
    values: dict[str, bool],
) -> None:
    accepted = HYPERLIQUID_TESTNET_PROFILE.protection_capabilities
    profile = replace(
        HYPERLIQUID_TESTNET_PROFILE,
        protection_capabilities=ProtectionCapabilityMatrix(
            profile_id=accepted.profile_id,
            values=values,
        ),
    )
    host, runtime = _host_for_profile(profile)

    with pytest.raises(RuntimeError, match="protection capability identity"):
        build_broker_execution_port(_context(tmp_path, host))

    assert runtime.invoke_calls == []


def test_external_testnet_bridge_rejects_protection_profile_id_drift(tmp_path) -> None:
    accepted = HYPERLIQUID_TESTNET_PROFILE.protection_capabilities
    profile = replace(
        HYPERLIQUID_TESTNET_PROFILE,
        protection_capabilities=ProtectionCapabilityMatrix(
            profile_id="drift-v2",
            values=accepted.values,
        ),
    )
    host, runtime = _host_for_profile(profile)

    with pytest.raises(RuntimeError, match="protection capability identity"):
        build_broker_execution_port(_context(tmp_path, host))

    assert runtime.invoke_calls == []
