import unittest
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal

from standard_broker.adapters.hyperliquid import (
    HyperliquidInstrumentAdapter,
    HyperliquidRuntimeOrderAdapter,
    NautilusAdapterMetadata,
    NautilusHyperliquidRuntime,
    NautilusRuntimeConfig,
    NautilusRuntimeError,
    NautilusRuntimeState,
)
from standard_broker.capabilities import CapabilityDescriptor
from standard_broker.errors import OrderIdempotencyError
from standard_broker.models import AccountScope, BrokerEnvironment, SignerKind
from standard_broker.orders import OrderIntent, OrderSide, OrderState, OrderType, TimeInForce
from standard_broker.runtime import (
    AccountReference,
    BrokerRuntimeSession,
    ExternalEnvironmentApproval,
    RuntimeActivationPolicy,
    SignerReference,
    SignerProvider,
)
from standard_broker.runtime_facts import RuntimeFactLedger


REVISION = "hyperliquid-runtime-order-v1"


def capabilities(*operations: str) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        broker_id="hyperliquid",
        environment=BrokerEnvironment.PAPER,
        operations={"order_execution": frozenset(operations)},
        revision=REVISION,
    )


def capabilities_for(environment: BrokerEnvironment, *operations: str) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        broker_id="hyperliquid",
        environment=environment,
        operations={"order_execution": frozenset(operations)},
        revision=REVISION,
    )


class FixtureSignerProvider:
    def sign(self, signer: SignerReference, payload: bytes) -> bytes:
        assert signer.reference == "fixture://testnet-signer"
        return payload


class FakeOrderBackend:
    local_only = True

    def __init__(self, profile: CapabilityDescriptor) -> None:
        self.calls: list[tuple[str, str, object]] = []
        self.last_client_order_id: str | None = None
        self.last_broker_order_id = 100
        self.submit_timeout = False
        self.inline_filled = False
        self.responses: dict[str, object] = {}
        self.metadata = NautilusAdapterMetadata(
            package="nautilus-hyperliquid",
            version="1.230.0",
            commit="order-lifecycle-commit",
            capabilities=profile,
        )

    def invoke(self, port: str, operation: str, request: object) -> object:
        self.calls.append((port, operation, request))
        if operation == "submit":
            self.last_client_order_id = str(request["cloid"])
            self.last_broker_order_id += 1
            if self.submit_timeout:
                raise TimeoutError("ambiguous submit")
            if self.inline_filled:
                return {
                    "status": "ok",
                    "response": {
                        "type": "order",
                        "data": {
                            "statuses": [
                                {"filled": {"oid": self.last_broker_order_id, "totalSz": "0.1", "avgPx": "65000"}}
                            ]
                        },
                    },
                }
            return {
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {"statuses": [{"resting": {"oid": self.last_broker_order_id}}]},
                },
            }
        if operation in {"cancel", "replace"}:
            return {"status": "ok"}
        if operation == "query":
            return {
                "status": "open",
                "oid": self.last_broker_order_id,
                "cloid": self.last_client_order_id,
                "timestamp": 1787313661000,
            }
        if operation == "open_orders":
            return self.responses.get(
                "open_orders",
                {"orders": [{"status": "open", "oid": self.last_broker_order_id, "cloid": self.last_client_order_id}]},
            )
        return {"status": "unknown"}


class HyperliquidRuntimeOrderLifecycleTests(unittest.TestCase):
    def instruments(self) -> HyperliquidInstrumentAdapter:
        return HyperliquidInstrumentAdapter.from_meta(
            {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 50}]},
            revision=REVISION,
        )

    def runtime(
        self,
        *,
        profile: CapabilityDescriptor | None = None,
        start: bool = True,
    ) -> tuple[NautilusHyperliquidRuntime, FakeOrderBackend]:
        selected_profile = profile or capabilities("submit", "cancel", "replace", "query", "open_orders")
        backend = FakeOrderBackend(selected_profile)
        runtime = NautilusHyperliquidRuntime(
            session=BrokerRuntimeSession(
                broker_id="hyperliquid",
                environment=BrokerEnvironment.PAPER,
                account=AccountReference(AccountScope.MASTER, "0xmaster"),
                signer=SignerReference.paper(),
                signer_provider=None,
                capabilities=selected_profile,
                execution_scope="hypercore:default",
                lifecycle_id="order-runtime-1",
            ),
            backend=backend,
            config=NautilusRuntimeConfig("1.230.0", "order-lifecycle-commit"),
        )
        if start:
            runtime.start()
        return runtime, backend

    def intent(self, *, order_id: str = "o-1", key: str = "cycle:o-1") -> OrderIntent:
        return OrderIntent(
            order_id=order_id,
            instrument_id="BTC-USD-PERP",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Decimal("0.1"),
            limit_price=Decimal("65000"),
            time_in_force=TimeInForce.GTC,
            idempotency_key=key,
        )

    def adapter(self, *, profile: CapabilityDescriptor | None = None):
        runtime, backend = self.runtime(profile=profile)
        return (
            HyperliquidRuntimeOrderAdapter(
                runtime=runtime,
                instruments=self.instruments(),
                ledger=RuntimeFactLedger(),
            ),
            backend,
        )

    def test_testnet_runtime_requires_approval_and_supports_fixture_order_lifecycle(self) -> None:
        profile = capabilities_for(
            BrokerEnvironment.TESTNET,
            "submit",
            "cancel",
            "replace",
            "query",
            "open_orders",
        )
        backend = FakeOrderBackend(profile)
        approval = ExternalEnvironmentApproval(
            environment=BrokerEnvironment.TESTNET,
            approval_id="testnet-approval-1",
            release_sha="a" * 40,
            approved_by="park",
            approved_at=datetime.now(UTC),
            account_address="testnet-account",
            lifecycle_id="testnet-order-runtime-1",
        )
        runtime = NautilusHyperliquidRuntime(
            session=BrokerRuntimeSession(
                broker_id="hyperliquid",
                environment=BrokerEnvironment.TESTNET,
                account=AccountReference(AccountScope.MASTER, "testnet-account"),
                signer=SignerReference(
                    SignerKind.API_AGENT,
                    "fixture",
                    "fixture://testnet-signer",
                ),
                signer_provider=FixtureSignerProvider(),
                capabilities=profile,
                execution_scope="hypercore:default",
                lifecycle_id="testnet-order-runtime-1",
            ),
            backend=backend,
            config=NautilusRuntimeConfig(
                "1.230.0",
                "order-lifecycle-commit",
                RuntimeActivationPolicy(testnet_approval=approval),
                expected_release_sha="a" * 40,
            ),
        )

        health = runtime.start()
        self.assertEqual(health.environment, BrokerEnvironment.TESTNET)
        self.assertEqual(health.state.value, "ready")
        adapter = HyperliquidRuntimeOrderAdapter(
            runtime=runtime,
            instruments=self.instruments(),
            ledger=RuntimeFactLedger(),
        )

        receipt = adapter.submit(self.intent())

        self.assertEqual(receipt.environment, BrokerEnvironment.TESTNET)
        self.assertEqual(receipt.state, OrderState.RESTING)
        self.assertEqual(receipt.account_address, "testnet-account")
        self.assertEqual(receipt.lifecycle_id, "testnet-order-runtime-1")
        self.assertEqual(receipt.release_sha, "a" * 40)
        self.assertTrue(adapter.local_only)
        self.assertEqual(adapter.submit(self.intent()), receipt)
        partial = adapter.apply_fill(
            {
                "coin": "BTC",
                "px": "65000",
                "sz": "0.04",
                "side": "B",
                "time": 1787313659000,
                "oid": 101,
                "cloid": receipt.client_order_id,
                "tid": 601,
            }
        )
        self.assertEqual(partial.state, OrderState.PARTIALLY_FILLED)
        self.assertEqual(partial.account_address, "testnet-account")
        self.assertEqual(partial.lifecycle_id, "testnet-order-runtime-1")
        bound_fill = next(iter(adapter.fills.values()))
        self.assertEqual(bound_fill.environment, BrokerEnvironment.TESTNET)
        self.assertEqual(bound_fill.account_address, "testnet-account")
        self.assertEqual(bound_fill.lifecycle_id, "testnet-order-runtime-1")
        self.assertEqual(bound_fill.release_sha, "a" * 40)
        self.assertEqual(adapter.apply_fill(
            {
                "coin": "BTC",
                "px": "65000",
                "sz": "0.04",
                "side": "B",
                "time": 1787313659001,
                "oid": 101,
                "cloid": receipt.client_order_id,
                "tid": 601,
            }
        ), partial)

        queried = adapter.query(receipt.order_id)
        self.assertEqual(queried.state, OrderState.RESTING)
        self.assertEqual(adapter.open_orders("BTC-USD-PERP")[0].order_id, receipt.order_id)
        pending_replace = adapter.modify(
            receipt.order_id,
            self.intent(order_id=receipt.order_id, key="testnet-replace"),
        )
        self.assertEqual(pending_replace.state, OrderState.MODIFY_PENDING)
        promoted = adapter.apply_order_update(
            {
                "status": "accepted",
                "oid": 202,
                "cloid": pending_replace.client_order_id,
                "timestamp": 1787313661000,
            }
        )
        self.assertEqual(promoted.broker_order_id, "202")
        pending_cancel = adapter.cancel(receipt.order_id)
        self.assertEqual(pending_cancel.state, OrderState.CANCEL_PENDING)
        canceled = adapter.reconcile(
            {
                "status": "canceled",
                "oid": 202,
                "cloid": pending_cancel.client_order_id,
                "timestamp": 1787313661000,
            }
        )
        self.assertEqual(canceled.state, OrderState.CANCELED)
        self.assertEqual(
            [call[1] for call in backend.calls],
            ["submit", "query", "open_orders", "replace", "cancel"],
        )

        second = adapter.submit(self.intent(order_id="testnet-cancel", key="testnet-cancel"))
        client_cancel = adapter.cancel(second.client_order_id)
        self.assertEqual(client_cancel.state, OrderState.CANCEL_PENDING)
        third = adapter.submit(
            self.intent(order_id="testnet-broker-cancel", key="testnet-broker-cancel")
        )
        broker_cancel = adapter.cancel("103")
        self.assertEqual(broker_cancel.order_id, third.order_id)
        self.assertEqual(broker_cancel.state, OrderState.CANCEL_PENDING)
        before_unknown = len(backend.calls)
        with self.assertRaises(KeyError):
            adapter.cancel("unknown-testnet-order")
        self.assertEqual(len(backend.calls), before_unknown)

        preflight = runtime.preflight(
            required_operations={
                "order_execution": {"submit", "cancel", "replace", "query", "open_orders"}
            }
        )
        self.assertEqual(preflight.release_sha, "a" * 40)

    def test_testnet_runtime_rejects_empty_capability_profile_before_ready(self) -> None:
        profile = capabilities_for(BrokerEnvironment.TESTNET)
        backend = FakeOrderBackend(profile)
        approval = ExternalEnvironmentApproval(
            environment=BrokerEnvironment.TESTNET,
            approval_id="testnet-approval-empty-profile",
            release_sha="a" * 40,
            approved_by="park",
            approved_at=datetime.now(UTC),
            account_address="testnet-account",
            lifecycle_id="testnet-order-runtime-empty",
        )
        runtime = NautilusHyperliquidRuntime(
            session=BrokerRuntimeSession(
                broker_id="hyperliquid",
                environment=BrokerEnvironment.TESTNET,
                account=AccountReference(AccountScope.MASTER, "testnet-account"),
                signer=SignerReference(
                    SignerKind.API_AGENT,
                    "fixture",
                    "fixture://testnet-signer",
                ),
                signer_provider=FixtureSignerProvider(),
                capabilities=profile,
                execution_scope="hypercore:default",
                lifecycle_id="testnet-order-runtime-empty",
            ),
            backend=backend,
            config=NautilusRuntimeConfig(
                "1.230.0",
                "order-lifecycle-commit",
                RuntimeActivationPolicy(testnet_approval=approval),
                expected_release_sha="a" * 40,
            ),
        )

        with self.assertRaises(NautilusRuntimeError) as raised:
            runtime.start()

        self.assertEqual(raised.exception.reason_code, "capability_gap")
        self.assertEqual(runtime.state, NautilusRuntimeState.FAULTED)
        self.assertEqual(backend.calls, [])

    def test_testnet_runtime_without_approval_fails_before_backend_invocation(self) -> None:
        profile = capabilities_for(
            BrokerEnvironment.TESTNET,
            "submit",
            "cancel",
            "replace",
            "query",
            "open_orders",
        )
        backend = FakeOrderBackend(profile)
        runtime = NautilusHyperliquidRuntime(
            session=BrokerRuntimeSession(
                broker_id="hyperliquid",
                environment=BrokerEnvironment.TESTNET,
                account=AccountReference(AccountScope.MASTER, "testnet-account"),
                signer=SignerReference(
                    SignerKind.API_AGENT,
                    "fixture",
                    "fixture://testnet-signer",
                ),
                signer_provider=FixtureSignerProvider(),
                capabilities=profile,
                execution_scope="hypercore:default",
                lifecycle_id="testnet-order-runtime-2",
            ),
            backend=backend,
            config=NautilusRuntimeConfig("1.230.0", "order-lifecycle-commit"),
        )

        with self.assertRaises(NautilusRuntimeError) as raised:
            runtime.start()

        self.assertEqual(raised.exception.reason_code, "external_environment_denied")
        self.assertEqual(backend.calls, [])

    def test_testnet_runtime_rejects_mismatched_release_approval(self) -> None:
        profile = capabilities_for(
            BrokerEnvironment.TESTNET,
            "submit",
            "cancel",
            "replace",
            "query",
            "open_orders",
        )
        backend = FakeOrderBackend(profile)
        approval = ExternalEnvironmentApproval(
            environment=BrokerEnvironment.TESTNET,
            approval_id="testnet-approval-mismatch",
            release_sha="b" * 40,
            approved_by="park",
            approved_at=datetime.now(UTC),
            account_address="testnet-account",
            lifecycle_id="testnet-order-runtime-3",
        )
        runtime = NautilusHyperliquidRuntime(
            session=BrokerRuntimeSession(
                broker_id="hyperliquid",
                environment=BrokerEnvironment.TESTNET,
                account=AccountReference(AccountScope.MASTER, "testnet-account"),
                signer=SignerReference(
                    SignerKind.API_AGENT,
                    "fixture",
                    "fixture://testnet-signer",
                ),
                signer_provider=FixtureSignerProvider(),
                capabilities=profile,
                execution_scope="hypercore:default",
                lifecycle_id="testnet-order-runtime-3",
            ),
            backend=backend,
            config=NautilusRuntimeConfig(
                "1.230.0",
                "order-lifecycle-commit",
                RuntimeActivationPolicy(testnet_approval=approval),
                expected_release_sha="a" * 40,
            ),
        )

        with self.assertRaises(NautilusRuntimeError) as raised:
            runtime.start()

        self.assertEqual(raised.exception.reason_code, "testnet_release_mismatch")
        self.assertEqual(backend.calls, [])

    def test_submit_returns_canonical_receipt_and_native_request_stays_internal(self) -> None:
        adapter, backend = self.adapter()

        receipt = adapter.submit(self.intent())

        self.assertEqual(receipt.state, OrderState.RESTING)
        self.assertEqual(receipt.environment, BrokerEnvironment.PAPER)
        self.assertEqual(receipt.broker_order_id, "101")
        self.assertTrue(receipt.client_order_id.startswith("0x"))
        self.assertIsNotNone(receipt.updated_at)
        request = backend.calls[0][2]
        self.assertEqual(request["coin"], "BTC")
        self.assertEqual(request["cloid"], receipt.client_order_id)
        self.assertNotIn("coin", receipt.__dict__)

    def test_idempotency_and_partial_fill_are_canonical(self) -> None:
        adapter, backend = self.adapter()
        first = adapter.submit(self.intent())
        second = adapter.submit(self.intent())

        self.assertEqual(first, second)
        self.assertEqual(len([call for call in backend.calls if call[1] == "submit"]), 1)

        fill = {
            "coin": "BTC",
            "px": "65000",
            "sz": "0.04",
            "side": "B",
            "time": 1787313659000,
            "oid": 101,
            "cloid": first.client_order_id,
            "tid": 501,
        }
        partial = adapter.apply_fill(fill)
        duplicate = adapter.apply_fill(fill)

        self.assertEqual(partial.state, OrderState.PARTIALLY_FILLED)
        self.assertEqual(partial.remaining_quantity, Decimal("0.06"))
        self.assertEqual(duplicate, partial)

        full = adapter.apply_fill(
            {
                "coin": "BTC",
                "px": "65010",
                "sz": "0.06",
                "side": "B",
                "time": 1787313660000,
                "oid": 101,
                "cloid": first.client_order_id,
                "tid": 502,
            }
        )
        self.assertEqual(full.state, OrderState.FILLED)
        self.assertEqual(full.remaining_quantity, Decimal("0"))

    def test_cancel_and_replace_preserve_order_lineage(self) -> None:
        adapter, _ = self.adapter()
        submitted = adapter.submit(self.intent())
        pending_cancel = adapter.cancel(submitted.order_id)
        canceled = adapter.apply_order_update(
            {"status": "canceled", "oid": 101, "cloid": submitted.client_order_id, "timestamp": 1}
        )

        self.assertEqual(pending_cancel.state, OrderState.CANCEL_PENDING)
        self.assertEqual(canceled.state, OrderState.CANCELED)

        adapter, _ = self.adapter()
        submitted = adapter.submit(self.intent())
        pending_replace = adapter.modify(submitted.order_id, self.intent())
        promoted = adapter.apply_order_update(
            {"status": "accepted", "oid": 202, "cloid": submitted.client_order_id, "timestamp": 2}
        )
        stale_cancel = adapter.apply_order_update(
            {"status": "canceled", "oid": 101, "cloid": submitted.client_order_id, "timestamp": 3}
        )

        self.assertEqual(pending_replace.state, OrderState.MODIFY_PENDING)
        self.assertEqual(promoted.broker_order_id, "202")
        self.assertEqual(promoted.broker_order_lineage, ("101", "202"))
        self.assertNotEqual(promoted.client_order_lineage[0], promoted.client_order_lineage[-1])
        self.assertEqual(stale_cancel.broker_order_id, "202")

    def test_cancel_accepts_client_order_identity(self) -> None:
        adapter, _ = self.adapter()
        submitted = adapter.submit(self.intent())

        pending_cancel = adapter.cancel(submitted.client_order_id)

        self.assertEqual(pending_cancel.state, OrderState.CANCEL_PENDING)

    def test_cancel_accepts_broker_order_lineage_and_rejects_unknown_identity(self) -> None:
        adapter, backend = self.adapter()
        submitted = adapter.submit(self.intent())

        pending_cancel = adapter.cancel("101")

        self.assertEqual(pending_cancel.state, OrderState.CANCEL_PENDING)
        with self.assertRaises(KeyError):
            adapter.cancel("unknown-order-reference")
        self.assertEqual([call[1] for call in backend.calls], ["submit", "cancel"])

    def test_query_and_open_orders_use_runtime_operations(self) -> None:
        adapter, backend = self.adapter()
        submitted = adapter.submit(self.intent())

        queried = adapter.query(submitted.order_id)
        open_orders = adapter.open_orders("BTC-USD-PERP")

        self.assertEqual(queried.state, OrderState.RESTING)
        self.assertEqual(queried.broker_updated_at, datetime.fromtimestamp(1787313661000 / 1000, tz=UTC))
        self.assertNotEqual(queried.updated_at, queried.broker_updated_at)
        self.assertEqual(len(open_orders), 1)
        self.assertEqual(open_orders[0].broker_order_id, "101")
        self.assertEqual([call[1] for call in backend.calls], ["submit", "query", "open_orders"])

        backend.responses["open_orders"] = {"orders": [{"status": "open", "cloid": submitted.client_order_id}]}
        client_only_open_orders = adapter.open_orders("BTC-USD-PERP")
        self.assertEqual(client_only_open_orders[0].order_id, submitted.order_id)

    def test_capability_gap_blocks_cancel_before_runtime_backend(self) -> None:
        adapter, backend = self.adapter(profile=capabilities("submit"))
        submitted = adapter.submit(self.intent())

        with self.assertRaises(NautilusRuntimeError):
            adapter.cancel(submitted.order_id)

        self.assertEqual([call[1] for call in backend.calls], ["submit"])

    def test_close_position_requires_reduce_only(self) -> None:
        with self.assertRaises(ValueError):
            self.intent().__class__(
                order_id="close-unsafe",
                instrument_id="BTC-USD-PERP",
                side=OrderSide.SELL,
                order_type=OrderType.LIMIT,
                quantity=Decimal("0.1"),
                limit_price=Decimal("65000"),
                time_in_force=TimeInForce.GTC,
                idempotency_key="close-unsafe",
                close_position=True,
                reduce_only=False,
            )

    def test_invalid_price_precision_fails_before_backend(self) -> None:
        adapter, backend = self.adapter()

        with self.assertRaises(ValueError):
            adapter.submit(
                self.intent(order_id="bad-price", key="bad-price").__class__(
                    order_id="bad-price",
                    instrument_id="BTC-USD-PERP",
                    side=OrderSide.BUY,
                    order_type=OrderType.LIMIT,
                    quantity=Decimal("0.1"),
                    limit_price=Decimal("65000.01"),
                    time_in_force=TimeInForce.GTC,
                    idempotency_key="bad-price",
                )
            )
        self.assertEqual(backend.calls, [])

    def test_idempotency_collision_fails_closed(self) -> None:
        adapter, backend = self.adapter()
        adapter.submit(self.intent())

        with self.assertRaises(OrderIdempotencyError) as raised:
            adapter.submit(self.intent(order_id="o-2", key="cycle:o-1"))

        self.assertEqual(raised.exception.reason_code, "idempotency_collision")
        self.assertEqual([call[1] for call in backend.calls], ["submit"])

    def test_ambiguous_submit_can_query_by_client_id(self) -> None:
        adapter, backend = self.adapter()
        backend.submit_timeout = True
        unknown = adapter.submit(self.intent())

        self.assertEqual(unknown.state, OrderState.UNKNOWN)
        queried = adapter.query(unknown.order_id)

        self.assertEqual(queried.state, OrderState.RESTING)
        self.assertEqual(backend.calls[-1][1], "query")
        self.assertNotIn("oid", backend.calls[-1][2])

    def test_inline_filled_without_fill_identity_fails_closed(self) -> None:
        adapter, backend = self.adapter()
        backend.inline_filled = True

        receipt = adapter.submit(self.intent())

        self.assertEqual(receipt.state, OrderState.UNKNOWN)
        self.assertEqual(receipt.reason, "filled_without_fill_identity")
        self.assertEqual(adapter.fills, {})

    def test_conflicting_broker_and_client_identities_fail_closed(self) -> None:
        adapter, _ = self.adapter()
        submitted = adapter.submit(self.intent())

        with self.assertRaises(ValueError):
            adapter.apply_order_update(
                {"status": "open", "oid": 999, "cloid": submitted.client_order_id, "timestamp": 4}
            )

    def test_status_only_fill_event_fails_closed_without_fill_identity(self) -> None:
        adapter, _ = self.adapter()
        submitted = adapter.submit(self.intent())

        result = adapter.apply_order_update(
            {"status": "filled", "oid": 101, "cloid": submitted.client_order_id, "timestamp": 5}
        )

        self.assertEqual(result.state, OrderState.UNKNOWN)
        self.assertEqual(result.reason, "status_without_fill_identity")
        self.assertEqual(result.filled_quantity, Decimal("0"))

    def test_status_with_fill_identity_updates_quantity_and_records_fill(self) -> None:
        adapter, _ = self.adapter()
        submitted = adapter.submit(self.intent())

        result = adapter.apply_order_update(
            {
                "status": "filled",
                "oid": 101,
                "cloid": submitted.client_order_id,
                "coin": "BTC",
                "tid": 503,
                "side": "B",
                "px": "65000",
                "sz": "0.1",
                "time": 1787313662000,
            }
        )

        self.assertEqual(result.state, OrderState.FILLED)
        self.assertEqual(result.filled_quantity, Decimal("0.1"))
        self.assertEqual(result.remaining_quantity, Decimal("0"))
        self.assertIn("503", adapter.fills)

    def test_order_fill_is_written_to_shared_runtime_fact_ledger(self) -> None:
        runtime, _ = self.runtime()
        ledger = RuntimeFactLedger()
        adapter = HyperliquidRuntimeOrderAdapter(
            runtime=runtime,
            instruments=self.instruments(),
            ledger=ledger,
        )
        submitted = adapter.submit(self.intent())

        adapter.apply_fill(
            {
                "coin": "BTC",
                "px": "65000",
                "sz": "0.1",
                "side": "B",
                "time": 1787313663000,
                "oid": 101,
                "cloid": submitted.client_order_id,
                "tid": 504,
            }
        )

        self.assertEqual(len(ledger.order_fills), 1)

    def test_runtime_transaction_preserves_shared_fee_enricher_identity_on_rollback(self) -> None:
        runtime, _ = self.runtime()
        ledger = RuntimeFactLedger()
        adapter = HyperliquidRuntimeOrderAdapter(
            runtime=runtime,
            instruments=self.instruments(),
            ledger=ledger,
        )
        submitted = adapter.submit(self.intent())

        class Enricher:
            def __init__(self) -> None:
                self.calls: list[Mapping[str, object]] = []

            def enrich(self, fill, raw: Mapping[str, object]):
                self.calls.append(raw)
                return None

        enricher = Enricher()
        ledger.register_fill_enricher(enricher.enrich)
        original_callback = ledger.fill_enricher

        with self.assertRaises(ValueError):
            with adapter.transaction():
                adapter.apply_fill(
                    {
                        "coin": "BTC",
                        "px": "65000",
                        "sz": "0.1",
                        "side": "B",
                        "time": 1787313663000,
                        "oid": 101,
                        "cloid": submitted.client_order_id,
                        "tid": 504,
                    }
                )
                raise ValueError("rollback")

        self.assertIs(ledger.fill_enricher, original_callback)
        self.assertIs(ledger.fill_enricher.__self__, enricher)
        self.assertEqual(ledger.order_fills, {})
        self.assertEqual(ledger.order_fill_raw, {})

    def test_quantity_step_and_minimum_notional_are_checked_before_backend(self) -> None:
        adapter, backend = self.adapter()

        with self.assertRaises(ValueError):
            adapter.submit(
                OrderIntent(
                    order_id="bad-step",
                    instrument_id="BTC-USD-PERP",
                    side=OrderSide.BUY,
                    order_type=OrderType.LIMIT,
                    quantity=Decimal("0.000011"),
                    limit_price=Decimal("65000"),
                    time_in_force=TimeInForce.GTC,
                    idempotency_key="bad-step",
                )
            )
        with self.assertRaises(ValueError):
            adapter.submit(
                OrderIntent(
                    order_id="bad-notional",
                    instrument_id="BTC-USD-PERP",
                    side=OrderSide.BUY,
                    order_type=OrderType.LIMIT,
                    quantity=Decimal("0.00001"),
                    limit_price=Decimal("65000"),
                    time_in_force=TimeInForce.GTC,
                    idempotency_key="bad-notional",
                )
            )
        self.assertEqual(backend.calls, [])

    def test_runtime_backend_is_not_invoked_before_runtime_start(self) -> None:
        runtime, backend = self.runtime(start=False)
        adapter = HyperliquidRuntimeOrderAdapter(
            runtime=runtime,
            instruments=self.instruments(),
            ledger=RuntimeFactLedger(),
        )

        with self.assertRaises(NautilusRuntimeError):
            adapter.submit(self.intent())

        self.assertEqual(backend.calls, [])


if __name__ == "__main__":
    unittest.main()
