"""Exact Hyperliquid external transport profiles."""

from datetime import timedelta

from ...external_host import (
    ExternalBrokerBuildContext,
    ExternalBrokerHost,
    ExternalTransportProfile,
    _ExternalRuntimePort,
)
from ...errors import RuntimeBoundaryError
from ...models import BrokerEnvironment, SignerKind
from ...runtime_facts import RuntimeFactLedger
from .external import (
    NAUTILUS_HYPERLIQUID_COMMIT,
    NAUTILUS_HYPERLIQUID_VERSION,
    default_testnet_capabilities,
)
from .bridge import NautilusHyperliquidRuntime
from .instruments import HyperliquidInstrumentAdapter
from .orders import HyperliquidExternalOrderAdapter, HyperliquidRuntimeOrderAdapter
from .protection import default_external_testnet_protection_capabilities
from ...external_canary import (
    ExternalCanaryBinding,
    ExternalCanaryRuntimeFactsReader,
    ExternalCanarySnapshotReader,
)
from ...market_data import FreshnessPolicy
from .read_facts import HyperliquidExternalFactAdapter


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


def build_hyperliquid_testnet_order_adapter(
    *,
    context: ExternalBrokerBuildContext,
    runtime: NautilusHyperliquidRuntime,
    instruments: HyperliquidInstrumentAdapter,
    ledger: RuntimeFactLedger,
) -> HyperliquidExternalOrderAdapter:
    """Bind one exact external host to the canonical Hyperliquid lifecycle."""

    host = build_hyperliquid_testnet_host(context=context, runtime=runtime)
    lifecycle = HyperliquidRuntimeOrderAdapter(
        runtime=runtime,
        instruments=instruments,
        ledger=ledger,
    )
    return HyperliquidExternalOrderAdapter(host=host, lifecycle=lifecycle)


def build_hyperliquid_testnet_canary_binding(
    *,
    context: ExternalBrokerBuildContext,
    runtime: NautilusHyperliquidRuntime,
    instruments: HyperliquidInstrumentAdapter,
    ledger: RuntimeFactLedger,
    snapshot_reader: ExternalCanarySnapshotReader,
) -> ExternalCanaryBinding:
    """Build the public typed order/facts binding consumed by a canary host."""

    host = build_hyperliquid_testnet_host(context=context, runtime=runtime)
    lifecycle = HyperliquidRuntimeOrderAdapter(
        runtime=runtime,
        instruments=instruments,
        ledger=ledger,
    )
    order = HyperliquidExternalOrderAdapter(host=host, lifecycle=lifecycle)
    facts = ExternalCanaryRuntimeFactsReader(
        context=context,
        host=host,
        order=lifecycle,
        snapshot_reader=snapshot_reader,
        instruments=instruments,
        market=HyperliquidExternalFactAdapter(
            context=context,
            instruments=instruments,
            freshness_policy=FreshnessPolicy(timedelta(minutes=2)),
        ),
    )
    return ExternalCanaryBinding(host=host, order=order, facts=facts)
