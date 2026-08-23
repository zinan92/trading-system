from decimal import Decimal

import pytest

from standard_broker.adapters.hyperliquid.profile import build_hyperliquid_testnet_host
from standard_broker.adapters.hyperliquid.protection import (
    default_external_testnet_protection_capabilities,
)
from standard_broker.errors import BrokerCapabilityError
from standard_broker.external_host import ExternalHostRequest
from standard_broker.host import CanonicalHostRequest
from standard_broker.models import BrokerEnvironment, AccountScope
from standard_broker.orders import OrderSide
from standard_broker.protection import (
    ProtectionExecution,
    ProtectionGroup,
    ProtectionLeg,
    ProtectionQuantityPolicy,
    ProtectionType,
    TriggerReference,
)

from test_external_profile import _profile_context, _profile_runtime, _profile_session


def _group() -> ProtectionGroup:
    return ProtectionGroup(
        protection_id="protection-1",
        parent_order_id="order-1",
        instrument_id="SOL-USD-PERP",
        entry_side=OrderSide.BUY,
        entry_price=Decimal("100"),
        quantity=Decimal("0.2"),
        quantity_policy=ProtectionQuantityPolicy.FIXED_SIZE,
        take_profit=ProtectionLeg(
            protection_type=ProtectionType.TAKE_PROFIT,
            execution=ProtectionExecution.MARKET,
            trigger_price=Decimal("105"),
            trigger_reference=TriggerReference.MARK,
        ),
        stop_loss=ProtectionLeg(
            protection_type=ProtectionType.STOP_LOSS,
            execution=ProtectionExecution.MARKET,
            trigger_price=Decimal("95"),
            trigger_reference=TriggerReference.MARK,
        ),
    )


def test_external_testnet_protection_profile_declares_gaps_without_emulation() -> None:
    matrix = default_external_testnet_protection_capabilities()

    assert matrix.supports("reduce_only_close") is True
    assert matrix.supports("reduce_only") is False
    assert matrix.supports("take_profit_market") is False
    assert matrix.supports("stop_loss_market") is False
    assert matrix.supports("grouped_tp_sl") is False
    assert matrix.supports("position_following") is False
    assert matrix.supports("partial_fill_repair") is False
    assert "reduce_only" in matrix.missing_for(_group(), operation="submit")


def test_external_host_blocks_protection_request_before_runtime_preflight_or_invoke() -> None:
    session = _profile_session()
    runtime = _profile_runtime(session)
    host = build_hyperliquid_testnet_host(
        context=_profile_context(session),
        runtime=runtime,
    )

    with pytest.raises(BrokerCapabilityError, match="capability_gap"):
        host.request(
            ExternalHostRequest(
                request_id="protection-request-1",
                request=CanonicalHostRequest(
                    port="protection_order",
                    operation="submit",
                    payload=_group(),
                ),
            )
        )

    assert runtime.preflight_calls == []
    assert runtime.invoke_calls == []
