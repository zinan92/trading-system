"""Exact Hyperliquid external transport profiles."""

from ...external_host import (
    ExternalBrokerBuildContext,
    ExternalBrokerHost,
    ExternalTransportProfile,
    _ExternalRuntimePort,
)
from ...errors import RuntimeBoundaryError
from ...models import BrokerEnvironment, SignerKind
from .external import (
    NAUTILUS_HYPERLIQUID_COMMIT,
    NAUTILUS_HYPERLIQUID_VERSION,
    default_testnet_capabilities,
)
from .protection import default_external_testnet_protection_capabilities


HYPERLIQUID_TESTNET_PROFILE = ExternalTransportProfile(
    profile_id="hyperliquid-testnet-default",
    broker_id="hyperliquid",
    environment=BrokerEnvironment.TESTNET,
    execution_scope="hypercore:default",
    adapter_id="nautilus-hyperliquid",
    version=NAUTILUS_HYPERLIQUID_VERSION,
    commit=NAUTILUS_HYPERLIQUID_COMMIT,
    mapping_revision="hyperliquid-testnet-runtime-v1",
    transport_state="external_testnet",
    signer_kind=SignerKind.API_AGENT,
    capabilities=default_testnet_capabilities(),
    protection_capabilities=default_external_testnet_protection_capabilities(),
)


def resolve_external_profile(profile_id: str) -> ExternalTransportProfile:
    """Resolve one exact Hyperliquid profile without wildcard fallback."""

    if profile_id != HYPERLIQUID_TESTNET_PROFILE.profile_id:
        raise RuntimeBoundaryError(
            "external_profile_unsupported",
            f"no exact external profile is registered for {profile_id!r}",
        )
    return HYPERLIQUID_TESTNET_PROFILE


def build_hyperliquid_testnet_host(
    *,
    context: ExternalBrokerBuildContext,
    runtime: _ExternalRuntimePort,
) -> ExternalBrokerHost:
    """Build the exact registered Hyperliquid Testnet host profile."""

    profile = resolve_external_profile(HYPERLIQUID_TESTNET_PROFILE.profile_id)
    profile.validate(context=context, runtime=runtime, require_approval=False)
    return ExternalBrokerHost(context=context, runtime=runtime, profile=profile)
