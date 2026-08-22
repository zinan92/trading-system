import unittest
from decimal import Decimal
from dataclasses import replace

from standard_broker.adapters.hyperliquid import (
    HyperliquidRuntimeProtectionAdapter,
    NautilusAdapterMetadata,
    NautilusHyperliquidRuntime,
    NautilusRuntimeConfig,
    ProtectionLifecycleState,
    RateLimitError,
)
from standard_broker.capabilities import CapabilityDescriptor
from standard_broker.errors import BrokerCapabilityError
from standard_broker.models import AccountScope, BrokerEnvironment
from standard_broker.orders import OrderSide
from standard_broker.ports import ProtectionOrderPort
from standard_broker.protection import (
    ProtectionExecution,
    ProtectionGroup,
    ProtectionLeg,
    ProtectionQuantityPolicy,
    ProtectionType,
)
from standard_broker.runtime import AccountReference, BrokerRuntimeSession, SignerReference


REVISION = "hyperliquid-protection-runtime-v1"


class FakeProtectionBackend:
    local_only = True

    def __init__(self, profile: CapabilityDescriptor) -> None:
        self.calls: list[tuple[str, str, object]] = []
        self.fail_with: BaseException | None = None
        self.metadata = NautilusAdapterMetadata(
            package="nautilus-hyperliquid",
            version="1.230.0",
            commit="protection-runtime-commit",
            capabilities=profile,
        )

    def invoke(self, port: str, operation: str, request: object) -> object:
        self.calls.append((port, operation, request))
        if self.fail_with is not None:
            raise self.fail_with
        return {"status": "ok"}


class HyperliquidRuntimeProtectionTests(unittest.TestCase):
    def profile(self, *operations: str) -> CapabilityDescriptor:
        return CapabilityDescriptor(
            broker_id="hyperliquid",
            environment=BrokerEnvironment.PAPER,
            operations={"protection_order": frozenset(operations)},
            revision=REVISION,
        )

    def runtime(
        self,
        *operations: str,
    ) -> tuple[NautilusHyperliquidRuntime, FakeProtectionBackend]:
        profile = self.profile(*operations)
        backend = FakeProtectionBackend(profile)
        runtime = NautilusHyperliquidRuntime(
            session=BrokerRuntimeSession(
                broker_id="hyperliquid",
                environment=BrokerEnvironment.PAPER,
                account=AccountReference(AccountScope.MASTER, "0xmaster"),
                signer=SignerReference.paper(),
                signer_provider=None,
                capabilities=profile,
                execution_scope="hypercore:default",
                lifecycle_id="protection-runtime-1",
            ),
            backend=backend,
            config=NautilusRuntimeConfig("1.230.0", "protection-runtime-commit"),
        )
        runtime.start()
        return runtime, backend

    def group(
        self,
        *,
        quantity_policy: ProtectionQuantityPolicy = ProtectionQuantityPolicy.FIXED_SIZE,
    ) -> ProtectionGroup:
        return ProtectionGroup(
            protection_id="protect-1",
            parent_order_id="order-1",
            instrument_id="BTC-USD-PERP",
            entry_side=OrderSide.BUY,
            entry_price=Decimal("65000"),
            quantity=Decimal("0.1"),
            quantity_policy=quantity_policy,
            take_profit=ProtectionLeg(
                protection_type=ProtectionType.TAKE_PROFIT,
                execution=ProtectionExecution.MARKET,
                trigger_price=Decimal("66000"),
            ),
            stop_loss=ProtectionLeg(
                protection_type=ProtectionType.STOP_LOSS,
                execution=ProtectionExecution.LIMIT,
                trigger_price=Decimal("64000"),
                limit_price=Decimal("63900"),
            ),
        )

    def full_operations(self) -> tuple[str, ...]:
        return (
            "submit",
            "cancel",
            "replace",
            "cancel_replace",
            "reduce_only",
            "mark_price_trigger",
            "take_profit_market",
            "stop_loss_limit",
            "grouped_tp_sl",
            "sibling_cancellation",
            "fixed_size",
            "position_following",
            "position_level_tpsl",
            "partial_fill_repair_position_following",
        )

    def test_submit_serializes_reduce_only_group_and_siblings(self) -> None:
        runtime, backend = self.runtime(*self.full_operations())
        adapter = HyperliquidRuntimeProtectionAdapter(runtime=runtime)

        self.assertIsInstance(adapter, ProtectionOrderPort)
        receipt = adapter.submit(self.group())

        self.assertTrue(receipt.accepted)
        self.assertEqual(receipt.operation, "submit")
        request = backend.calls[0][2]
        self.assertEqual(request["protectionId"], "protect-1")
        self.assertEqual(request["parentOrderId"], "order-1")
        self.assertEqual(request["instrumentId"], "BTC-USD-PERP")
        self.assertEqual(request["grouping"], "normalTpsl")
        self.assertEqual(request["quantity"], "0.1")
        self.assertTrue(all(leg["reduceOnly"] for leg in request["legs"]))
        self.assertEqual(request["legs"][0]["siblingId"], request["legs"][1]["siblingId"])

    def test_missing_reduce_only_capability_fails_before_backend_and_freezes(self) -> None:
        runtime, backend = self.runtime("submit", "grouped_tp_sl", "fixed_size")
        adapter = HyperliquidRuntimeProtectionAdapter(runtime=runtime)

        with self.assertRaises(BrokerCapabilityError) as raised:
            adapter.submit(self.group())

        self.assertEqual(raised.exception.reason_code, "capability_gap")
        self.assertEqual(backend.calls, [])
        self.assertEqual(adapter.status("protect-1").state, ProtectionLifecycleState.FROZEN)

    def test_missing_mark_trigger_capability_fails_before_backend(self) -> None:
        runtime, backend = self.runtime(
            "submit",
            "reduce_only",
            "grouped_tp_sl",
            "sibling_cancellation",
            "fixed_size",
            "take_profit_market",
            "stop_loss_limit",
        )
        adapter = HyperliquidRuntimeProtectionAdapter(runtime=runtime)

        with self.assertRaises(BrokerCapabilityError) as raised:
            adapter.submit(self.group())

        self.assertEqual(raised.exception.operation, "mark_price_trigger")
        self.assertEqual(backend.calls, [])

    def test_fixed_size_partial_fill_is_explicit_gap_and_freezes(self) -> None:
        runtime, backend = self.runtime(*self.full_operations())
        adapter = HyperliquidRuntimeProtectionAdapter(runtime=runtime)

        with self.assertRaises(BrokerCapabilityError) as raised:
            adapter.repair_after_partial_fill(self.group(), filled_quantity=Decimal("0.04"))

        self.assertEqual(raised.exception.operation, "partial_fill_repair")
        self.assertIn("partial_fill_protection_gap", str(raised.exception))
        self.assertEqual(backend.calls, [])
        self.assertEqual(adapter.status("protect-1").state, ProtectionLifecycleState.FROZEN)
        retry_plan = adapter.retry_plan(self.group(), attempt=1)
        self.assertEqual(retry_plan.operation, "replace")
        self.assertFalse(retry_plan.retry_allowed)

    def test_position_following_partial_fill_uses_replace_with_new_quantity(self) -> None:
        runtime, backend = self.runtime(*self.full_operations())
        adapter = HyperliquidRuntimeProtectionAdapter(runtime=runtime)
        group = self.group(quantity_policy=ProtectionQuantityPolicy.POSITION_FOLLOWING)

        adapter.submit(group)
        adapter.repair_after_partial_fill(group, filled_quantity=Decimal("0.04"))

        self.assertEqual([call[1] for call in backend.calls], ["submit", "replace"])
        self.assertEqual(backend.calls[-1][2]["quantity"], "0.04")
        self.assertEqual(adapter.status("protect-1").state, ProtectionLifecycleState.SUBMITTED)
        confirmed = adapter.reconcile_position_coverage(
            replace(group, quantity=Decimal("0.04")),
            owned_quantity=Decimal("0.04"),
        )
        self.assertEqual(confirmed.state, ProtectionLifecycleState.SUBMITTED)

    def test_position_coverage_reconciliation_tracks_owned_quantity(self) -> None:
        runtime, backend = self.runtime(*self.full_operations())
        adapter = HyperliquidRuntimeProtectionAdapter(runtime=runtime)
        group = self.group(quantity_policy=ProtectionQuantityPolicy.POSITION_FOLLOWING)

        adapter.submit(group)
        adapter.reconcile_position_coverage(group, owned_quantity=Decimal("0.2"))

        self.assertEqual([call[1] for call in backend.calls], ["submit", "replace"])
        self.assertEqual(backend.calls[-1][2]["quantity"], "0.2")

    def test_fixed_size_coverage_gap_freezes_residual_path(self) -> None:
        runtime, backend = self.runtime(*self.full_operations())
        adapter = HyperliquidRuntimeProtectionAdapter(runtime=runtime)

        with self.assertRaises(BrokerCapabilityError) as raised:
            adapter.reconcile_position_coverage(self.group(), owned_quantity=Decimal("0.03"))

        self.assertEqual(raised.exception.operation, "position_coverage")
        self.assertEqual(backend.calls, [])
        self.assertEqual(adapter.status("protect-1").state, ProtectionLifecycleState.FROZEN)
        self.assertEqual(
            adapter.retry_plan(self.group(), attempt=1).operation,
            "replace",
        )

    def test_update_failure_freezes_and_retry_requires_bounded_policy(self) -> None:
        runtime, backend = self.runtime(*self.full_operations())
        adapter = HyperliquidRuntimeProtectionAdapter(runtime=runtime)
        group = self.group()
        adapter.submit(group)
        backend.fail_with = RateLimitError(retry_after_seconds=0.1, side_effect_free=True)

        with self.assertRaises(RateLimitError):
            adapter.replace(group)

        self.assertEqual(adapter.status("protect-1").state, ProtectionLifecycleState.FROZEN)
        retry_plan = adapter.retry_plan(group, attempt=1)
        self.assertEqual(retry_plan.operation, "replace")
        self.assertEqual(retry_plan.attempt, 2)
        self.assertGreater(retry_plan.delay_seconds, 0)
        with self.assertRaises(BrokerCapabilityError) as raised:
            adapter.retry(group, attempt=1)
        self.assertIn("retry_wait_required", str(raised.exception))
        backend.fail_with = None

        with self.assertRaises(BrokerCapabilityError):
            adapter.replace(group)

        adapter.retry(group, attempt=1, delay_elapsed=True)

        self.assertEqual([call[1] for call in backend.calls], ["submit", "replace", "replace"])
        self.assertEqual(adapter.status("protect-1").state, ProtectionLifecycleState.SUBMITTED)

    def test_retry_repeats_original_submit_and_is_bounded(self) -> None:
        runtime, backend = self.runtime(*self.full_operations())
        adapter = HyperliquidRuntimeProtectionAdapter(runtime=runtime)
        group = self.group()
        backend.fail_with = RateLimitError(retry_after_seconds=0.1, side_effect_free=True)

        with self.assertRaises(RateLimitError):
            adapter.submit(group)
        with self.assertRaises(RateLimitError):
            adapter.retry(group, attempt=1, delay_elapsed=True)

        with self.assertRaises(BrokerCapabilityError) as raised:
            adapter.retry(group, attempt=1)

        self.assertEqual(raised.exception.operation, "retry")
        self.assertEqual([call[1] for call in backend.calls], ["submit", "submit"])

    def test_unknown_operation_capability_fails_before_backend(self) -> None:
        runtime, backend = self.runtime("submit", "reduce_only")
        adapter = HyperliquidRuntimeProtectionAdapter(runtime=runtime)

        with self.assertRaises(BrokerCapabilityError):
            adapter.cancel(self.group())

        self.assertEqual(backend.calls, [])


if __name__ == "__main__":
    unittest.main()
