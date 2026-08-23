from datetime import UTC, datetime
from decimal import Decimal
from types import MethodType

import pytest

from standard_broker import (
    ExternalProtectionBinding,
    ExternalProtectionObservation,
    ExternalProtectionReceipt,
)
from standard_broker.adapters.hyperliquid.external import (
    NAUTILUS_HYPERLIQUID_COMMIT,
    NAUTILUS_HYPERLIQUID_VERSION,
    default_testnet_capabilities,
)
from standard_broker.adapters.hyperliquid.profile import build_hyperliquid_testnet_host
from standard_broker.adapters.hyperliquid.profile import (
    build_hyperliquid_testnet_position_protection_binding_from_runtime,
)
from standard_broker.adapters.hyperliquid.external import enabled_testnet_position_protection_capabilities
from standard_broker.capabilities import CapabilityDescriptor
from standard_broker.errors import BrokerCapabilityError, RuntimeBoundaryError
from standard_broker.external_host import (
    ExternalBrokerBuildContext,
    ExternalBrokerHost,
    ExternalRuntimeIdentity,
    digest_canonical,
)
from standard_broker.models import BrokerEnvironment, Provenance
from standard_broker.protection import (
    ProtectionExecution,
    ProtectionGroup,
    ProtectionLeg,
    ProtectionQuantityPolicy,
    ProtectionType,
)

from test_external_profile import ACCOUNT, RELEASE_SHA, _profile_context, _profile_runtime, _profile_session


def _group() -> ProtectionGroup:
    return ProtectionGroup(
        protection_id="dca-protection:1",
        parent_order_id="entry:1",
        instrument_id="BTC-USD-PERP",
        entry_side=__import__("standard_broker").OrderSide.BUY,
        entry_price=Decimal("60000"),
        quantity=Decimal("0.001"),
        quantity_policy=ProtectionQuantityPolicy.POSITION_FOLLOWING,
        take_profit=ProtectionLeg(
            protection_type=ProtectionType.TAKE_PROFIT,
            execution=ProtectionExecution.MARKET,
            trigger_price=Decimal("61000"),
        ),
        stop_loss=ProtectionLeg(
            protection_type=ProtectionType.STOP_LOSS,
            execution=ProtectionExecution.MARKET,
            trigger_price=Decimal("59000"),
        ),
    )


def _enabled_host() -> ExternalBrokerHost:
    base = default_testnet_capabilities()
    operations = dict(base.operations)
    operations["protection_order"] = frozenset(
        {
            "submit",
            "cancel",
            "replace",
            "query",
            "reduce_only",
            "mark_price_trigger",
            "grouped_tp_sl",
            "sibling_cancellation",
            "position_following",
            "position_level_tpsl",
            "take_profit_market",
            "stop_loss_market",
            "position_coverage",
            "partial_fill_repair",
            "cancel_replace",
        }
    )
    capabilities = CapabilityDescriptor(
        broker_id=base.broker_id,
        environment=base.environment,
        operations=operations,
        revision="hyperliquid-testnet-protection-binding-v1",
    )
    session = _profile_session(capabilities=capabilities)
    context = ExternalBrokerBuildContext(
        session=session,
        runtime_identity=ExternalRuntimeIdentity(
            adapter_id="nautilus-hyperliquid",
            version=NAUTILUS_HYPERLIQUID_VERSION,
            commit=NAUTILUS_HYPERLIQUID_COMMIT,
            mapping_revision=capabilities.revision,
            transport_state="external_testnet",
        ),
        release_sha=RELEASE_SHA,
        approval=_profile_context(session).approval,
    )
    from standard_broker.external_host import ExternalTransportProfile
    from standard_broker.protection import ProtectionCapabilityMatrix
    from standard_broker.models import SignerKind

    profile = ExternalTransportProfile(
        profile_id="hyperliquid-testnet-protection-binding",
        broker_id="hyperliquid",
        environment=BrokerEnvironment.TESTNET,
        execution_scope="hypercore:default",
        adapter_id="nautilus-hyperliquid",
        version=NAUTILUS_HYPERLIQUID_VERSION,
        commit=NAUTILUS_HYPERLIQUID_COMMIT,
        mapping_revision=capabilities.revision,
        transport_state="external_testnet",
        signer_kind=SignerKind.API_AGENT,
        capabilities=capabilities,
        protection_capabilities=ProtectionCapabilityMatrix(
            profile_id="hyperliquid-testnet-protection-binding-v1",
            values={
                key: True
                for key in (
                    "submit",
                    "cancel",
                    "replace",
                    "query",
                    "reduce_only",
                    "mark_price_trigger",
                    "grouped_tp_sl",
                    "sibling_cancellation",
                    "position_following",
                    "position_level_tpsl",
                    "take_profit_market",
                    "stop_loss_market",
                    "position_coverage",
                    "partial_fill_repair",
                    "cancel_replace",
                )
            },
        ),
    )
    return ExternalBrokerHost(
        context=context,
        runtime=_profile_runtime(session),
        profile=profile,
    )


def _patch_fact_reader(host: ExternalBrokerHost, *, state: str) -> None:
    provenance = Provenance(
        source="nautilus-hyperliquid.testnet",
        execution_scope="hypercore:default",
        transport_state="external_testnet",
        mapping_revision=host.capabilities.revision,
        received_at=datetime.now(UTC),
    )

    def read_fact(self, *, request, mapper):
        del request
        raw = {
            "protection_id": "dca-protection:1",
            "operation": "query" if state == "active" else "submit",
            "state": state,
            "accepted": state in {"active", "submitted"},
            "covered_quantity": "0.001" if state in {"active", "submitted"} else "0",
            "order_ids": ["protect-tp-1", "protect-sl-1"],
            "provenance": provenance,
        }
        raw["observation_digest"] = digest_canonical(raw)
        return mapper(raw)

    host.read_fact = MethodType(read_fact, host)  # type: ignore[method-assign]


def test_external_protection_binding_requires_remote_active_query() -> None:
    host = _enabled_host()
    _patch_fact_reader(host, state="active")
    binding = ExternalProtectionBinding(host=host)

    receipt = binding.reconcile(_group())

    assert isinstance(receipt, ExternalProtectionReceipt)
    assert receipt.observation.state.value == "active"
    assert receipt.observation.covered_quantity == Decimal("0.001")
    assert binding.status(_group().protection_id).state.value == "active"


def test_external_protection_binding_freezes_unknown_submit() -> None:
    host = _enabled_host()
    _patch_fact_reader(host, state="unknown")
    binding = ExternalProtectionBinding(host=host)

    with pytest.raises(BrokerCapabilityError, match="not_accepted"):
        binding.submit(_group())

    assert binding.status(_group().protection_id).state.value == "frozen"


def test_external_protection_binding_rejects_tampered_observation_digest() -> None:
    host = _enabled_host()

    def read_fact(self, *, request, mapper):
        del request
        provenance = Provenance(
            source="nautilus-hyperliquid.testnet",
            execution_scope="hypercore:default",
            transport_state="external_testnet",
            mapping_revision=host.capabilities.revision,
            received_at=datetime.now(UTC),
        )
        raw = {
            "protection_id": "dca-protection:1",
            "operation": "query",
            "state": "active",
            "accepted": True,
            "covered_quantity": "0.001",
            "order_ids": ["protect-tp-1", "protect-sl-1"],
            "provenance": provenance,
            "observation_digest": "sha256:" + "0" * 64,
        }
        return mapper(raw)

    host.read_fact = MethodType(read_fact, host)  # type: ignore[method-assign]
    binding = ExternalProtectionBinding(host=host)

    with pytest.raises(RuntimeBoundaryError, match="digest"):
        binding.reconcile(_group())


def test_default_external_profile_still_blocks_protection_before_runtime() -> None:
    session = _profile_session()
    runtime = _profile_runtime(session)
    host = build_hyperliquid_testnet_host(
        context=_profile_context(session),
        runtime=runtime,
    )
    binding = ExternalProtectionBinding(host=host)

    with pytest.raises(BrokerCapabilityError, match="capability_gap"):
        binding.submit(_group())

    assert runtime.preflight_calls == []
    assert runtime.invoke_calls == []


def test_position_protection_profile_is_opt_in_and_exact() -> None:
    capabilities = enabled_testnet_position_protection_capabilities()
    session = _profile_session(capabilities=capabilities)
    runtime = _profile_runtime(session)
    binding = build_hyperliquid_testnet_position_protection_binding_from_runtime(
        context=_profile_context(session),
        runtime=runtime,
    )

    assert binding.local_only is False
    assert binding.transport_state == "external_testnet"
    assert binding.protection_capabilities is not None
    assert binding.protection_capabilities.supports("position_following") is True
