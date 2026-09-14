import unittest
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from standard_broker.adapters.hyperliquid import (
    HyperliquidProtectionAdapter,
    HyperliquidRuntimeProtectionAdapter,
    NautilusAdapterMetadata,
    NautilusHyperliquidRuntime,
    NautilusRuntimeConfig,
)
from standard_broker.adapters.hyperliquid.external import (
    enabled_testnet_position_protection_capabilities,
)
from standard_broker.capabilities import CapabilityDescriptor
from standard_broker.errors import BrokerCapabilityError
from standard_broker.models import BrokerEnvironment
from standard_broker.orders import OrderSide
from standard_broker.protection import (
    ProtectionExecution,
    ProtectionGroup,
    ProtectionLeg,
    ProtectionLifecycleState,
    ProtectionQuantityPolicy,
    ProtectionType,
    TriggerReference,
)
from standard_broker.models import AccountScope, Provenance, SignerKind
from standard_broker.runtime import (
    AccountReference,
    BrokerRuntimeSession,
    ExternalEnvironmentApproval,
    RuntimeActivationPolicy,
    SignerReference,
)


class FixtureSignerProvider:
    def sign(self, signer: SignerReference, payload: bytes) -> bytes:
        return payload


def _approved_testnet_runtime(capabilities, calls, *, accepted: bool):
    class Backend:
        local_only = True

        def __init__(self) -> None:
            self.metadata = NautilusAdapterMetadata(
                package="nautilus-hyperliquid",
                version="1.230.0",
                commit="testnet-protection-commit",
                capabilities=capabilities,
            )

        def invoke(self, port: str, operation: str, request: object):
            calls.append((port, operation, request))
            state = {"query": "active", "cancel": "canceled"}.get(operation, "submitted")
            return {
                "protection_id": "protect-1",
                "operation": operation,
                "state": state,
                "accepted": accepted,
                "covered_quantity": "0.1" if operation == "query" else "0",
                "order_ids": ["protection-order-1", "protection-order-2"],
                "observation_digest": "sha256:" + "d" * 64,
            }

    approval = ExternalEnvironmentApproval(
        environment=BrokerEnvironment.TESTNET,
        approval_id="protection-approval",
        release_sha="a" * 40,
        approved_by="park",
        approved_at=datetime.now(UTC),
        account_address="testnet-account",
        lifecycle_id="protection-runtime",
    )
    runtime = NautilusHyperliquidRuntime(
        session=BrokerRuntimeSession(
            broker_id="hyperliquid",
            environment=BrokerEnvironment.TESTNET,
            account=AccountReference(AccountScope.MASTER, "testnet-account"),
            signer=SignerReference(SignerKind.API_AGENT, "fixture", "fixture://signer"),
            signer_provider=FixtureSignerProvider(),
            capabilities=capabilities,
            execution_scope="hypercore:default",
            lifecycle_id="protection-runtime",
        ),
        backend=Backend(),
        config=NautilusRuntimeConfig(
            "1.230.0",
            "testnet-protection-commit",
            RuntimeActivationPolicy(testnet_approval=approval),
            expected_release_sha="a" * 40,
        ),
    )
    runtime.start()
    if not accepted:
        def rejected_invoke(port: str, operation: str, request: object):
            return SimpleNamespace(
                accepted=False,
                broker_id="hyperliquid",
                environment=BrokerEnvironment.TESTNET,
                provenance=Provenance(
                    source="testnet.fixture",
                    execution_scope="hypercore:default",
                    transport_state="local_fixture",
                    mapping_revision="testnet-protection-v1",
                    received_at=datetime.now(UTC),
                ),
            )
        runtime.invoke = rejected_invoke
    return runtime


class HyperliquidProtectionTests(unittest.TestCase):
    def long_group(self, *, quantity_policy=ProtectionQuantityPolicy.FIXED_SIZE) -> ProtectionGroup:
        return ProtectionGroup(
            protection_id="protect-1",
            parent_order_id="order-1",
            instrument_id="BTC-USD-PERP",
            entry_side=OrderSide.BUY,
            entry_price=Decimal(65000),
            quantity=Decimal("0.1"),
            quantity_policy=quantity_policy,
            take_profit=ProtectionLeg(
                protection_type=ProtectionType.TAKE_PROFIT,
                execution=ProtectionExecution.MARKET,
                trigger_price=Decimal(66000),
                limit_price=None,
            ),
            stop_loss=ProtectionLeg(
                protection_type=ProtectionType.STOP_LOSS,
                execution=ProtectionExecution.LIMIT,
                trigger_price=Decimal(64000),
                limit_price=Decimal(63900),
            ),
        )

    def test_bracket_maps_to_normal_tpsl_with_reduce_only_siblings(self) -> None:
        request = HyperliquidProtectionAdapter().build_group(self.long_group())

        self.assertEqual(request.grouping, "normalTpsl")
        self.assertEqual(len(request.legs), 2)
        self.assertTrue(all(leg.reduce_only for leg in request.legs))
        self.assertEqual(request.legs[0].tpsl, "tp")
        self.assertEqual(request.legs[1].tpsl, "sl")
        self.assertEqual(request.legs[0].execution, "market")
        self.assertEqual(request.legs[1].execution, "limit")
        self.assertEqual(request.legs[0].sibling_id, request.legs[1].sibling_id)

    def test_position_following_maps_to_position_tpsl(self) -> None:
        request = HyperliquidProtectionAdapter().build_group(
            self.long_group(quantity_policy=ProtectionQuantityPolicy.POSITION_FOLLOWING)
        )

        self.assertEqual(request.grouping, "positionTpsl")
        self.assertEqual(request.quantity_policy, ProtectionQuantityPolicy.POSITION_FOLLOWING)

    def test_all_triggers_are_mark_price_triggers(self) -> None:
        request = HyperliquidProtectionAdapter().build_group(self.long_group())

        self.assertTrue(all(leg.trigger_reference is TriggerReference.MARK for leg in request.legs))

    def test_partial_fill_repair_fails_closed_for_fixed_parent_group(self) -> None:
        adapter = HyperliquidProtectionAdapter()

        with self.assertRaises(BrokerCapabilityError) as raised:
            adapter.repair_after_partial_fill(self.long_group(), filled_quantity=Decimal("0.04"))

        self.assertEqual(raised.exception.operation, "partial_fill_repair")
        self.assertIn("partial_fill_protection_gap", str(raised.exception))

    def test_position_following_group_can_reconcile_partial_quantity(self) -> None:
        group = self.long_group(quantity_policy=ProtectionQuantityPolicy.POSITION_FOLLOWING)

        repaired = HyperliquidProtectionAdapter().repair_after_partial_fill(
            group,
            filled_quantity=Decimal("0.04"),
        )

        self.assertEqual(repaired.grouping, "positionTpsl")
        self.assertEqual(repaired.quantity, Decimal("0.04"))

    def test_invalid_long_trigger_direction_fails_closed(self) -> None:
        group = self.long_group()
        invalid = replace(
            group,
            take_profit=ProtectionLeg(
                protection_type=ProtectionType.TAKE_PROFIT,
                execution=ProtectionExecution.MARKET,
                trigger_price=Decimal(64000),
            ),
        )

        with self.assertRaises(ValueError):
            HyperliquidProtectionAdapter().build_group(invalid)

    def test_short_trigger_direction_is_supported(self) -> None:
        group = ProtectionGroup(
            protection_id="protect-short",
            parent_order_id="order-short",
            instrument_id="BTC-USD-PERP",
            entry_side=OrderSide.SELL,
            entry_price=Decimal(65000),
            quantity=Decimal("0.1"),
            quantity_policy=ProtectionQuantityPolicy.FIXED_SIZE,
            take_profit=ProtectionLeg(
                protection_type=ProtectionType.TAKE_PROFIT,
                execution=ProtectionExecution.MARKET,
                trigger_price=Decimal(64000),
            ),
            stop_loss=ProtectionLeg(
                protection_type=ProtectionType.STOP_LOSS,
                execution=ProtectionExecution.MARKET,
                trigger_price=Decimal(66000),
            ),
        )

        request = HyperliquidProtectionAdapter().build_group(group)

        self.assertTrue(all(leg.side is OrderSide.BUY for leg in request.legs))

    def test_approved_testnet_runtime_protection_is_reduce_only_and_namespaced(self) -> None:
        capabilities = CapabilityDescriptor(
            broker_id="hyperliquid",
            environment=BrokerEnvironment.TESTNET,
            operations={
                "order_execution": frozenset(
                    {"submit", "cancel", "replace", "query", "open_orders"}
                ),
                "protection_order": frozenset(
                    {
                        "submit",
                        "cancel",
                        "replace",
                        "cancel_replace",
                        "query",
                        "reduce_only",
                        "mark_price_trigger",
                        "grouped_tp_sl",
                        "sibling_cancellation",
                        "fixed_size",
                        "take_profit_market",
                        "stop_loss_limit",
                    }
                )
            },
            revision="testnet-protection-v1",
        )
        calls: list[tuple[str, str, object]] = []
        adapter = HyperliquidRuntimeProtectionAdapter(
            runtime=_approved_testnet_runtime(capabilities, calls, accepted=True)
        )
        receipt = adapter.submit(self.long_group())
        confirmed = adapter.reconcile(self.long_group())
        replaced = adapter.replace(self.long_group())
        canceled = adapter.cancel(self.long_group())

        self.assertEqual(receipt.environment, BrokerEnvironment.TESTNET)
        self.assertEqual(confirmed.operation, "query")
        self.assertEqual(adapter.status("protect-1").state, ProtectionLifecycleState.CANCELED)
        self.assertEqual(calls[0][0:2], ("protection_order", "submit"))
        self.assertTrue(all(leg["reduceOnly"] for leg in calls[0][2]["legs"]))
        self.assertEqual([call[1] for call in calls], ["submit", "query", "replace", "cancel"])
        self.assertEqual(replaced.operation, "replace")

        adapter.reconcile(self.long_group())
        with self.assertRaises(BrokerCapabilityError) as raised:
            adapter.reconcile_position_coverage(
                self.long_group(),
                owned_quantity=Decimal("0.04"),
            )
        self.assertIn("partial_fill_protection_gap", str(raised.exception))

    def test_shipped_testnet_profile_admits_supported_position_protection(self) -> None:
        capabilities = enabled_testnet_position_protection_capabilities()
        group = replace(
            self.long_group(quantity_policy=ProtectionQuantityPolicy.POSITION_FOLLOWING),
            take_profit=ProtectionLeg(
                protection_type=ProtectionType.TAKE_PROFIT,
                execution=ProtectionExecution.LIMIT,
                trigger_price=Decimal(66000),
                limit_price=Decimal(65950),
            ),
            stop_loss=ProtectionLeg(
                protection_type=ProtectionType.STOP_LOSS,
                execution=ProtectionExecution.MARKET,
                trigger_price=Decimal(64000),
            ),
        )
        adapter = HyperliquidRuntimeProtectionAdapter(
            runtime=_approved_testnet_runtime(capabilities, [], accepted=True)
        )

        receipt = adapter.submit(group)

        self.assertTrue(receipt.accepted)
        self.assertEqual(adapter.status(group.protection_id).state, ProtectionLifecycleState.SUBMITTED)

    def test_unaccepted_testnet_protection_receipt_freezes_before_coverage(self) -> None:
        capabilities = CapabilityDescriptor(
            broker_id="hyperliquid",
            environment=BrokerEnvironment.TESTNET,
            operations={
                "order_execution": frozenset(
                    {"submit", "cancel", "replace", "query", "open_orders"}
                ),
                "protection_order": frozenset(
                    {
                        "submit",
                        "cancel",
                        "reduce_only",
                        "mark_price_trigger",
                        "grouped_tp_sl",
                        "sibling_cancellation",
                        "fixed_size",
                        "take_profit_market",
                        "stop_loss_limit",
                    }
                ),
            },
            revision="testnet-protection-unaccepted-v1",
        )
        adapter = HyperliquidRuntimeProtectionAdapter(
            runtime=_approved_testnet_runtime(capabilities, [], accepted=False)
        )
        with self.assertRaises(BrokerCapabilityError) as raised:
            adapter.submit(self.long_group())
        self.assertIn("not_accepted", str(raised.exception))
        self.assertEqual(adapter.status("protect-1").state, ProtectionLifecycleState.FROZEN)


if __name__ == "__main__":
    unittest.main()
