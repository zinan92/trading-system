import unittest
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from standard_broker.account import AccountSnapshot, PositionFact, PositionSide
from standard_broker.adapters.hyperliquid.resilience import (
    HyperliquidRuntimeReconciliationAdapter,
    ObservationSource,
    RateLimitError,
    ReconciliationSnapshot,
    RetryPolicy,
    RuntimeConnectionState,
    RuntimeRecoveryError,
    TransportDisposition,
    classify_transport_error,
    plan_retry,
)
from standard_broker.capabilities import CapabilityDescriptor
from standard_broker.fees import FeeEvent, FeeKind, FeeSource, FeeState, FillFact
from standard_broker.models import AccountScope, BrokerEnvironment, Provenance
from standard_broker.orders import OrderReceipt, OrderState
from standard_broker.runtime import (
    AccountReference,
    BrokerRuntimeSession,
    SignerReference,
)

PROVENANCE = Provenance(
    source="resilience.fixture",
    execution_scope="hypercore:default",
    transport_state="local_fixture",
    mapping_revision="resilience-v1",
    received_at=datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
)


def canonical_facts() -> tuple[list[FillFact], list[PositionFact], AccountSnapshot]:
    fee = FeeEvent(
        fee_id="fee-1",
        broker_id="hyperliquid",
        kind=FeeKind.TAKER,
        amount=Decimal("0.1"),
        currency="USDC",
        occurred_at=PROVENANCE.received_at,
        source=FeeSource.ACTUAL_FILL,
        state=FeeState.ACTUAL,
        instrument_id="BTC-USD-PERP",
        fill_id="fill-1",
        order_id="order-1",
        provenance=PROVENANCE,
        environment=BrokerEnvironment.PAPER,
    )
    fill = FillFact(
        fill_id="fill-1",
        broker_id="hyperliquid",
        instrument_id="BTC-USD-PERP",
        side="buy",
        price=Decimal(65000),
        quantity=Decimal("0.1"),
        occurred_at=PROVENANCE.received_at,
        closed_pnl=Decimal(0),
        fee=fee,
        provenance=PROVENANCE,
        environment=BrokerEnvironment.PAPER,
    )
    position = PositionFact(
        instrument_id="BTC-USD-PERP",
        signed_quantity=Decimal("0.1"),
        side=PositionSide.LONG,
        entry_price=Decimal(65000),
        leverage=Decimal(5),
        margin_mode=None,
        liquidation_price=None,
        margin_used=Decimal(100),
        position_value=Decimal(6500),
        unrealized_pnl=Decimal(0),
        broker_id="hyperliquid",
        environment=BrokerEnvironment.PAPER,
        observation_id="position-1",
        provenance=PROVENANCE,
    )
    account = AccountSnapshot(
        broker_id="hyperliquid",
        account_address="0xaccount",
        equity=Decimal(1000),
        balance=Decimal(900),
        withdrawable=Decimal(800),
        margin_used=Decimal(100),
        exposure=Decimal(6500),
        realized_pnl=Decimal(0),
        unrealized_pnl=Decimal(0),
        positions=(position,),
        provenance=PROVENANCE,
        environment=BrokerEnvironment.PAPER,
        observation_id="account-1",
    )
    return [fill], [position], account


def receipt(
    state: OrderState,
    *,
    order_id: str = "order-1",
    filled_quantity: Decimal = Decimal(0),
    original_quantity: Decimal = Decimal("0.1"),
    environment: BrokerEnvironment = BrokerEnvironment.PAPER,
    broker_id: str = "hyperliquid",
) -> OrderReceipt:
    return OrderReceipt(
        order_id=order_id,
        broker_id=broker_id,
        environment=environment,
        client_order_id="client-1",
        state=state,
        original_quantity=original_quantity,
        filled_quantity=filled_quantity,
        remaining_quantity=original_quantity - filled_quantity,
        broker_order_id="11",
        average_fill_price=Decimal(65000) if filled_quantity else None,
        reason=None,
        provenance=PROVENANCE,
        updated_at=PROVENANCE.received_at,
        broker_updated_at=PROVENANCE.received_at,
        broker_order_lineage=("11",),
        client_order_lineage=("client-1",),
    )


class FakeOrders:
    local_only = True

    def __init__(self) -> None:
        self.runtime_session = BrokerRuntimeSession(
            broker_id="hyperliquid",
            environment=BrokerEnvironment.PAPER,
            account=AccountReference(AccountScope.MASTER, "0xaccount"),
            signer=SignerReference.paper(),
            signer_provider=None,
            capabilities=CapabilityDescriptor(
                broker_id="hyperliquid",
                environment=BrokerEnvironment.PAPER,
                operations={},
                revision="resilience-v1",
            ),
            execution_scope="hypercore:default",
            lifecycle_id="resilience-runtime-1",
        )
        self.order_events: list[dict] = []
        self.fill_events: list[dict] = []
        self.fill_count = 0
        self.query_calls: list[str] = []
        self.query_result = receipt(OrderState.UNKNOWN)
        self.query_error: BaseException | None = None
        self.fact_calls: list[str] = []
        self.snapshot_watermark = 1
        self.fill_facts, self.position_facts, self.account_fact = canonical_facts()

    def apply_order_update(self, raw: dict) -> OrderReceipt:
        self.order_events.append(raw)
        status = str(raw.get("status") or "").lower()
        state = {
            "canceled": OrderState.CANCELED,
            "cancelled": OrderState.CANCELED,
            "rejected": OrderState.REJECTED,
            "filled": OrderState.FILLED,
        }.get(status, OrderState.RESTING)
        return receipt(
            state,
            filled_quantity=Decimal(1) if state is OrderState.FILLED else Decimal(0),
            original_quantity=Decimal(1),
        )

    def apply_fill(self, raw: dict) -> OrderReceipt:
        self.fill_events.append(raw)
        self.fill_count += 1
        filled_quantity = Decimal("0.1") * self.fill_count
        return receipt(
            OrderState.FILLED if filled_quantity == Decimal(1) else OrderState.PARTIALLY_FILLED,
            filled_quantity=filled_quantity,
            original_quantity=Decimal(1),
        )

    def validate_fill(self, raw: dict) -> None:
        return None

    def validate_receipt(self, candidate: OrderReceipt) -> None:
        if candidate != self.query_result:
            raise ValueError("snapshot receipt is not current")

    @contextmanager
    def transaction(self):
        checkpoint = (
            list(self.order_events),
            list(self.fill_events),
            self.fill_count,
        )
        try:
            yield
        except Exception:
            self.order_events, self.fill_events, self.fill_count = checkpoint
            raise

    def query(self, order_id: str) -> OrderReceipt:
        self.query_calls.append(order_id)
        if self.query_error is not None:
            raise self.query_error
        return self.query_result

    def query_snapshot(self, order_id: str) -> ReconciliationSnapshot:
        self.fact_calls.append("snapshot")
        if self.query_error is not None:
            raise self.query_error
        return ReconciliationSnapshot(
            order_id=order_id,
            receipt=self.query_result,
            fills=tuple(self.fill_facts),
            positions=tuple(self.position_facts),
            account=self.account_fact,
            watermark=self.snapshot_watermark,
        )


class LocalCallback:
    local_only = True

    def __init__(self, result: object) -> None:
        self._result = result

    def __call__(self) -> object:
        return self._result() if callable(self._result) else self._result


class HyperliquidRuntimeResilienceTests(unittest.TestCase):
    def adapter(self) -> tuple[HyperliquidRuntimeReconciliationAdapter, FakeOrders]:
        orders = FakeOrders()
        return HyperliquidRuntimeReconciliationAdapter(orders=orders, session=orders.runtime_session), orders

    def test_direct_snapshot_requires_authoritative_facts_and_advances_cursor(self) -> None:
        adapter, orders = self.adapter()

        adapter.reconcile_snapshot(
            order_events=[("order-1", {"status": "open", "oid": 11, "timestamp": 10})],
            fill_events=[],
            positions=orders.position_facts,
            account=orders.account_fact,
            watermark=1,
        )

        self.assertEqual(adapter.latest_watermark, 1)
        self.assertEqual(adapter.latest_account, orders.account_fact)
        self.assertEqual(adapter.latest_positions, tuple(orders.position_facts))

    def test_direct_snapshot_rejects_changed_facts_at_same_cursor(self) -> None:
        adapter, orders = self.adapter()
        adapter.reconcile_snapshot(
            order_events=[],
            fill_events=[],
            positions=orders.position_facts,
            account=orders.account_fact,
            watermark=1,
        )
        changed_account = replace(orders.account_fact, observation_id="account-2")

        with self.assertRaises(RuntimeRecoveryError) as raised:
            adapter.reconcile_snapshot(
                order_events=[],
                fill_events=[],
                positions=orders.position_facts,
                account=changed_account,
                watermark=1,
            )

        self.assertEqual(raised.exception.reason_code, "snapshot_cursor_conflict")
        self.assertEqual(adapter.state, RuntimeConnectionState.DEGRADED)

    def test_snapshot_failure_rolls_back_lifecycle_and_observation_state(self) -> None:
        adapter, orders = self.adapter()
        original_apply = orders.apply_order_update

        def fail_on_second_event(raw: dict) -> OrderReceipt:
            if orders.order_events:
                raise ValueError("second event failed")
            return original_apply(raw)

        orders.apply_order_update = fail_on_second_event

        with self.assertRaises(RuntimeRecoveryError) as raised:
            adapter.reconcile_snapshot(
                order_events=[
                    ("order-1", {"status": "open", "oid": 11, "timestamp": 10}),
                    ("order-2", {"status": "canceled", "oid": 11, "timestamp": 11}),
                ],
                fill_events=[],
                positions=orders.position_facts,
                account=orders.account_fact,
                watermark=1,
            )

        self.assertEqual(raised.exception.reason_code, "order_event_apply_failed")
        self.assertEqual(orders.order_events, [])
        self.assertIsNone(adapter.latest_watermark)
        self.assertEqual(adapter.state, RuntimeConnectionState.DEGRADED)

    def test_reconnect_state_and_snapshot_order_then_fill_are_deduplicated(self) -> None:
        adapter, orders = self.adapter()

        adapter.disconnect("websocket reconnect")
        self.assertEqual(adapter.state, RuntimeConnectionState.RECONNECTING)
        resubscribed: list[bool] = []
        adapter.reconnect(
            resubscribe=LocalCallback(lambda: resubscribed.append(True) or True),
            snapshot_provider=LocalCallback(
                lambda: {
                    "order_events": [],
                    "fill_events": [],
                    "positions": orders.position_facts,
                    "account": orders.account_fact,
                    "watermark": 1,
                }
            ),
        )
        results = adapter.reconcile_snapshot(
            order_events=[("order-1", {"status": "open", "oid": 11, "timestamp": 10})],
            fill_events=[("fill-1", {"tid": 1, "oid": 11, "coin": "BTC", "side": "B", "px": "65000", "sz": "0.1", "time": 10})],
            positions=orders.position_facts,
            account=orders.account_fact,
            watermark=2,
        )
        duplicate = adapter.reconcile_snapshot(
            order_events=[("order-2", {"status": "open", "oid": 11, "timestamp": 10})],
            fill_events=[("fill-2", {"tid": 1, "oid": 11, "coin": "BTC", "side": "B", "px": "65000", "sz": "0.1", "time": 10})],
            positions=orders.position_facts,
            account=orders.account_fact,
            watermark=3,
        )

        self.assertEqual(adapter.state, RuntimeConnectionState.CONNECTED)
        self.assertEqual(len(results), 2)
        self.assertEqual(duplicate, ())
        self.assertEqual(len(orders.order_events), 1)
        self.assertEqual(len(orders.fill_events), 1)
        self.assertEqual(resubscribed, [True])

        self.assertIsNone(
            adapter.ingest_order_event(
                "order-3",
                {"status": "open", "oid": 11, "timestamp": 10},
                source=ObservationSource.WEBSOCKET,
            )
        )
        with self.assertRaises(RuntimeRecoveryError) as raised:
            adapter.ingest_order_event(
                "order-4",
                {"status": "filled", "oid": 11, "timestamp": 11},
                source=ObservationSource.WEBSOCKET,
            )
        self.assertEqual(raised.exception.reason_code, "fill_payload_incomplete")

    def test_equal_timestamp_state_transitions_are_not_deduplicated(self) -> None:
        adapter, orders = self.adapter()

        adapter.ingest_order_event("open", {"status": "open", "oid": 11, "timestamp": 10})
        adapter.ingest_order_event("cancel", {"status": "canceled", "oid": 11, "timestamp": 10})

        self.assertEqual(len(orders.order_events), 2)

        with self.assertRaises(RuntimeRecoveryError) as raised:
            adapter.ingest_order_event("reject", {"status": "rejected", "oid": 11, "timestamp": 10})
        self.assertEqual(raised.exception.reason_code, "order_terminal_conflict")

    def test_older_cancel_after_fill_is_ignored(self) -> None:
        adapter, orders = self.adapter()

        adapter.ingest_order_event("open", {"status": "open", "oid": 11, "timestamp": 10})
        adapter.ingest_fill_event(
            "fill-1",
            {"tid": 1, "oid": 11, "coin": "BTC", "side": "B", "px": "65000", "sz": "0.1", "time": 20},
        )
        adapter.ingest_order_event("old-cancel", {"status": "canceled", "oid": 11, "timestamp": 15})

        self.assertEqual(len(orders.order_events), 1)

    def test_terminal_fill_and_cancel_conflicts_fail_closed_in_both_orders(self) -> None:
        adapter, orders = self.adapter()
        orders.apply_fill = lambda raw: receipt(OrderState.FILLED, filled_quantity=Decimal("0.1"))
        adapter.ingest_fill_event(
            "fill-terminal",
            {"tid": 1, "oid": 11, "coin": "BTC", "side": "B", "px": "65000", "sz": "0.1", "time": 20},
        )
        with self.assertRaises(RuntimeRecoveryError) as raised:
            adapter.ingest_order_event("cancel-after-fill", {"status": "canceled", "oid": 11, "timestamp": 20})
        self.assertEqual(raised.exception.reason_code, "order_terminal_conflict")

        adapter, _ = self.adapter()
        adapter.ingest_order_event("cancel-first", {"status": "canceled", "oid": 11, "timestamp": 20})
        with self.assertRaises(RuntimeRecoveryError) as raised:
            adapter.ingest_fill_event(
                "fill-after-cancel",
                {"tid": 1, "oid": 11, "coin": "BTC", "side": "B", "px": "65000", "sz": "0.1", "time": 20},
            )
        self.assertEqual(raised.exception.reason_code, "fill_lifecycle_conflict")

    def test_malformed_filled_status_keeps_unresolved_terminal_watermark(self) -> None:
        adapter, orders = self.adapter()
        orders.apply_order_update = lambda raw: receipt(OrderState.UNKNOWN)

        with self.assertRaises(RuntimeRecoveryError) as raised:
            adapter.ingest_order_event("filled-without-fill", {"status": "filled", "oid": 11, "timestamp": 20})

        self.assertEqual(raised.exception.reason_code, "fill_payload_incomplete")

    def test_event_id_reuse_with_different_payload_fails_closed(self) -> None:
        adapter, _ = self.adapter()
        adapter.ingest_order_event("same-event", {"status": "open", "oid": 11, "timestamp": 10})

        with self.assertRaises(RuntimeRecoveryError) as raised:
            adapter.ingest_order_event("same-event", {"status": "canceled", "oid": 11, "timestamp": 11})

        self.assertEqual(raised.exception.reason_code, "order_event_identity_conflict")

    def test_non_paper_receipt_fails_closed(self) -> None:
        adapter, orders = self.adapter()
        orders.apply_order_update = lambda raw: receipt(
            OrderState.RESTING,
            environment=BrokerEnvironment.TESTNET,
        )

        with self.assertRaises(RuntimeRecoveryError) as raised:
            adapter.ingest_order_event("testnet-receipt", {"status": "open", "oid": 11, "timestamp": 10})

        self.assertEqual(raised.exception.reason_code, "paper_receipt_required")

    def test_non_fixture_receipt_provenance_fails_closed(self) -> None:
        adapter, orders = self.adapter()
        orders.apply_order_update = lambda raw: replace(
            receipt(OrderState.RESTING),
            provenance=replace(PROVENANCE, transport_state="network"),
        )

        with self.assertRaises(RuntimeRecoveryError) as raised:
            adapter.ingest_order_event("network-receipt", {"status": "open", "oid": 11, "timestamp": 10})

        self.assertEqual(raised.exception.reason_code, "paper_receipt_required")

    def test_rest_and_websocket_fill_aliases_converge_to_one_canonical_event(self) -> None:
        adapter, orders = self.adapter()
        base = {
            "oid": 11,
            "coin": "BTC",
            "side": "B",
            "px": "65000",
            "sz": "0.1",
            "time": 20,
            "hash": "0xsame-fill",
        }

        first = adapter.ingest_fill_event(
            "rest-event",
            {**base, "tid": 1},
            source=ObservationSource.REST,
        )
        duplicate = adapter.ingest_fill_event(
            "ws-event",
            {key: value for key, value in base.items() if key != "hash"} | {"hash": "0xsame-fill"},
            source=ObservationSource.WEBSOCKET,
        )

        self.assertIsNotNone(first)
        self.assertIsNone(duplicate)
        self.assertEqual(len(orders.fill_events), 1)
        self.assertEqual(
            adapter.event_sources("fill:hash:0xsame-fill:11:BTC:B:65000:0.1:20"),
            frozenset({ObservationSource.REST, ObservationSource.WEBSOCKET}),
        )

    def test_tid_only_then_hash_only_observation_fails_closed_on_ambiguity(self) -> None:
        adapter, orders = self.adapter()
        base = {"oid": 11, "coin": "BTC", "side": "B", "px": "65000", "sz": "0.1", "time": 20}

        adapter.ingest_fill_event("tid-only", {**base, "tid": 1})
        with self.assertRaises(RuntimeRecoveryError) as raised:
            adapter.ingest_fill_event("hash-only", {**base, "hash": "0xhash"})

        self.assertEqual(raised.exception.reason_code, "fill_identity_ambiguous")
        self.assertEqual(len(orders.fill_events), 1)

    def test_tid_enrichment_with_hash_is_idempotent(self) -> None:
        adapter, orders = self.adapter()
        base = {"oid": 11, "coin": "BTC", "side": "B", "px": "65000", "sz": "0.1", "time": 20, "tid": 1}

        adapter.ingest_fill_event("tid-only", base)
        duplicate = adapter.ingest_fill_event("tid-with-hash", {**base, "hash": "0xhash"})

        self.assertIsNone(duplicate)
        self.assertEqual(len(orders.fill_events), 1)

    def test_distinct_native_fill_ids_are_not_collapsed_by_same_price_shape(self) -> None:
        adapter, orders = self.adapter()
        base = {"oid": 11, "coin": "BTC", "side": "B", "px": "65000", "sz": "0.1", "time": 20}

        adapter.ingest_fill_event("fill-1", {**base, "tid": 1})
        adapter.ingest_fill_event("fill-2", {**base, "tid": 2})

        self.assertEqual(len(orders.fill_events), 2)

    def test_hash_only_then_tid_observation_converges_without_collapsing_next_tid(self) -> None:
        adapter, orders = self.adapter()
        base = {
            "oid": 11,
            "coin": "BTC",
            "side": "B",
            "px": "65000",
            "sz": "0.1",
            "time": 20,
            "hash": "0xhash",
        }

        adapter.ingest_fill_event("hash-only", base)
        with self.assertRaises(RuntimeRecoveryError) as raised:
            adapter.ingest_fill_event("tid-1", {**base, "tid": 1})

        self.assertEqual(raised.exception.reason_code, "fill_identity_ambiguous")
        self.assertEqual(len(orders.fill_events), 1)

    def test_empty_fill_identity_fails_closed(self) -> None:
        adapter, _ = self.adapter()

        with self.assertRaises(RuntimeRecoveryError) as raised:
            adapter.ingest_fill_event("empty-tid", {"tid": "", "oid": 11, "time": 20})

        self.assertEqual(raised.exception.reason_code, "fill_identity_required")

    def test_shape_only_fill_is_rejected_before_the_order_port(self) -> None:
        adapter, orders = self.adapter()

        with self.assertRaises(RuntimeRecoveryError) as raised:
            adapter.ingest_fill_event(
                "shape-only",
                {"oid": 11, "coin": "BTC", "side": "B", "px": "65000", "sz": "0.1", "time": 20},
            )

        self.assertEqual(raised.exception.reason_code, "fill_identity_required")
        self.assertEqual(orders.fill_events, [])

    def test_fill_identity_mutation_fails_closed(self) -> None:
        adapter, _ = self.adapter()
        base = {"tid": 7, "oid": 11, "coin": "BTC", "side": "B", "px": "65000", "sz": "0.1", "time": 20}
        adapter.ingest_fill_event("same-fill", base)

        with self.assertRaises(RuntimeRecoveryError) as raised:
            adapter.ingest_fill_event("same-fill", {**base, "px": "65001"})

        self.assertEqual(raised.exception.reason_code, "fill_identity_conflict")

    def test_ingest_is_blocked_while_disconnected_and_closes(self) -> None:
        adapter, _ = self.adapter()
        adapter.disconnect("transport down")

        with self.assertRaises(RuntimeRecoveryError) as raised:
            adapter.ingest_order_event("order-1", {"status": "open"})
        self.assertEqual(raised.exception.reason_code, "runtime_not_connected")

        adapter.close()
        self.assertEqual(adapter.state, RuntimeConnectionState.CLOSED)
        with self.assertRaises(RuntimeRecoveryError):
            adapter.reconnect()

    def test_reconnect_can_automatically_reconcile_provider_snapshot(self) -> None:
        adapter, orders = self.adapter()
        adapter.disconnect("replay")

        adapter.reconnect(
            snapshot_provider=LocalCallback(
                lambda: {
                    "order_events": [("order-snapshot", {"status": "open", "oid": 11, "timestamp": 20})],
                    "fill_events": [("fill-snapshot", {"tid": 2, "oid": 11, "coin": "BTC", "side": "B", "px": "65000", "sz": "0.1", "time": 20})],
                    "positions": orders.position_facts,
                    "account": orders.account_fact,
                    "watermark": 2,
                }
            )
        )

        self.assertEqual(len(orders.order_events), 1)
        self.assertEqual(len(orders.fill_events), 1)
        self.assertEqual(adapter.state, RuntimeConnectionState.CONNECTED)
        self.assertEqual(adapter.latest_account, orders.account_fact)
        self.assertEqual(adapter.latest_positions, tuple(orders.position_facts))
        self.assertEqual(adapter.latest_watermark, 2)

    def test_reconnect_requires_acknowledged_local_snapshot(self) -> None:
        adapter, orders = self.adapter()
        adapter.disconnect("missing snapshot")

        with self.assertRaises(RuntimeRecoveryError) as raised:
            adapter.reconnect()
        self.assertEqual(raised.exception.reason_code, "reconciliation_snapshot_required")
        self.assertEqual(adapter.state, RuntimeConnectionState.RECONNECTING)

        with self.assertRaises(RuntimeRecoveryError) as raised:
            adapter.reconnect(
                resubscribe=lambda: True,
                snapshot_provider=LocalCallback(
                    lambda: {
                        "order_events": [],
                        "fill_events": [],
                        "positions": orders.position_facts,
                        "account": orders.account_fact,
                        "watermark": 3,
                    }
                ),
            )
        self.assertEqual(raised.exception.reason_code, "non_local_callback")

        with self.assertRaises(RuntimeRecoveryError) as raised:
            adapter.reconnect(
                resubscribe=LocalCallback(lambda: True),
                snapshot_provider=LocalCallback(lambda: {"order_events": [], "fill_events": []}),
            )
        self.assertEqual(raised.exception.reason_code, "reconciliation_snapshot_incomplete")
        self.assertEqual(adapter.state, RuntimeConnectionState.DEGRADED)

        adapter.reconnect(
            resubscribe=LocalCallback(lambda: True),
            snapshot_provider=LocalCallback(
                lambda: {
                    "order_events": [],
                    "fill_events": [],
                    "positions": orders.position_facts,
                    "account": orders.account_fact,
                    "watermark": 4,
                }
            ),
        )
        self.assertEqual(adapter.state, RuntimeConnectionState.CONNECTED)

    def test_reconnect_rejects_mismatched_account_snapshot(self) -> None:
        adapter, orders = self.adapter()
        adapter.disconnect("account mismatch")
        mismatched_account = replace(orders.account_fact, account_address="0xother")

        with self.assertRaises(RuntimeRecoveryError) as raised:
            adapter.reconnect(
                snapshot_provider=LocalCallback(
                    lambda: {
                        "order_events": [],
                        "fill_events": [],
                        "positions": orders.position_facts,
                        "account": mismatched_account,
                        "watermark": 1,
                    }
                )
            )

        self.assertEqual(raised.exception.reason_code, "reconciliation_session_mismatch")

    def test_reconnect_rejects_regressing_numeric_watermark(self) -> None:
        adapter, orders = self.adapter()
        snapshot = lambda watermark: {
            "order_events": [],
            "fill_events": [],
            "positions": orders.position_facts,
            "account": orders.account_fact,
            "watermark": watermark,
        }

        adapter.disconnect("first snapshot")
        adapter.reconnect(snapshot_provider=LocalCallback(lambda: snapshot(20)))
        adapter.disconnect("stale snapshot")
        with self.assertRaises(RuntimeRecoveryError) as raised:
            adapter.reconnect(snapshot_provider=LocalCallback(lambda: snapshot(10)))

        self.assertEqual(raised.exception.reason_code, "stale_reconciliation_snapshot")
        self.assertEqual(adapter.latest_watermark, 20)

    def test_reconciliation_requires_the_order_port_runtime_session(self) -> None:
        orders = FakeOrders()
        other_session = replace(
            orders.runtime_session,
            account=AccountReference(AccountScope.MASTER, "0xother"),
        )

        with self.assertRaises(RuntimeRecoveryError) as raised:
            HyperliquidRuntimeReconciliationAdapter(orders=orders, session=other_session)

        self.assertEqual(raised.exception.reason_code, "runtime_session_mismatch")

    def test_query_before_retry_returns_explicit_recovery_decision(self) -> None:
        adapter, orders = self.adapter()
        orders.query_result = replace(receipt(OrderState.CANCELED), reason="ambiguous_submit")
        orders.fill_facts = []
        orders.position_facts = []
        orders.account_fact = replace(orders.account_fact, positions=(), exposure=Decimal(0))

        decision = adapter.reconcile_before_retry("order-1", facts=orders)

        self.assertTrue(decision.retry_allowed)
        self.assertEqual(decision.receipt.state, OrderState.CANCELED)
        self.assertEqual(orders.query_calls, [])

        orders.query_result = receipt(OrderState.UNKNOWN)
        orders.snapshot_watermark = 2
        blocked = adapter.reconcile_before_retry("order-1", facts=orders)
        self.assertFalse(blocked.retry_allowed)
        self.assertEqual(
            orders.fact_calls,
            ["snapshot", "snapshot"],
        )

    def test_rejected_order_without_explicit_retryable_reason_fails_closed(self) -> None:
        adapter, orders = self.adapter()
        orders.fill_facts = []
        orders.position_facts = []
        orders.account_fact = replace(orders.account_fact, positions=(), exposure=Decimal(0))
        orders.query_result = replace(receipt(OrderState.REJECTED), reason=None)

        decision = adapter.reconcile_before_retry("order-1", facts=orders)

        self.assertFalse(decision.retry_allowed)
        self.assertEqual(decision.reason, "retry_blocked_non_retryable_rejection")

    def test_canceled_order_without_explicit_retryable_reason_fails_closed(self) -> None:
        adapter, orders = self.adapter()
        orders.fill_facts = []
        orders.position_facts = []
        orders.account_fact = replace(orders.account_fact, positions=(), exposure=Decimal(0))
        orders.query_result = receipt(OrderState.CANCELED)

        decision = adapter.reconcile_before_retry("order-1", facts=orders)

        self.assertFalse(decision.retry_allowed)
        self.assertEqual(decision.reason, "retry_blocked_non_retryable_rejection")

    def test_retry_rejects_a_regressing_snapshot_watermark(self) -> None:
        adapter, orders = self.adapter()
        adapter.reconcile_snapshot(
            order_events=[],
            fill_events=[],
            positions=orders.position_facts,
            account=orders.account_fact,
            watermark=20,
        )
        orders.fill_facts = []
        orders.position_facts = []
        orders.account_fact = replace(orders.account_fact, positions=(), exposure=Decimal(0))
        orders.query_result = receipt(OrderState.CANCELED)
        orders.snapshot_watermark = 10

        with self.assertRaises(RuntimeRecoveryError) as raised:
            adapter.reconcile_before_retry("order-1", facts=orders)

        self.assertEqual(raised.exception.reason_code, "stale_reconciliation_snapshot")
        self.assertEqual(adapter.state, RuntimeConnectionState.DEGRADED)

    def test_retry_rejects_changed_facts_at_same_snapshot_watermark(self) -> None:
        adapter, orders = self.adapter()
        orders.fill_facts = []
        orders.position_facts = []
        orders.account_fact = replace(orders.account_fact, positions=(), exposure=Decimal(0))
        orders.query_result = receipt(OrderState.CANCELED)

        adapter.reconcile_before_retry("order-1", facts=orders)
        orders.account_fact = replace(orders.account_fact, observation_id="account-2")

        with self.assertRaises(RuntimeRecoveryError) as raised:
            adapter.reconcile_before_retry("order-1", facts=orders)

        self.assertEqual(raised.exception.reason_code, "snapshot_cursor_conflict")
        self.assertEqual(adapter.state, RuntimeConnectionState.DEGRADED)

    def test_retry_without_snapshot_capability_fails_closed(self) -> None:
        adapter, orders = self.adapter()
        orders.query_snapshot = None

        with self.assertRaises(RuntimeRecoveryError) as raised:
            adapter.reconcile_before_retry("order-1", facts=orders)

        self.assertEqual(raised.exception.reason_code, "runtime_contract_missing")

    def test_retry_rejects_snapshot_receipt_not_owned_by_lifecycle(self) -> None:
        adapter, orders = self.adapter()

        def reject_receipt(candidate: OrderReceipt) -> None:
            raise ValueError("stale receipt")

        orders.validate_receipt = reject_receipt

        with self.assertRaises(RuntimeRecoveryError) as raised:
            adapter.reconcile_before_retry("order-1", facts=orders)

        self.assertEqual(raised.exception.reason_code, "reconciliation_order_identity_mismatch")

    def test_existing_fills_and_exposure_block_retry(self) -> None:
        adapter, orders = self.adapter()
        orders.query_result = receipt(OrderState.CANCELED)

        decision = adapter.reconcile_before_retry("order-1", facts=orders)

        self.assertFalse(decision.retry_allowed)
        self.assertEqual(decision.reason, "retry_blocked_existing_fills")

    def test_receipt_execution_and_account_positions_block_retry(self) -> None:
        adapter, orders = self.adapter()
        orders.query_result = receipt(OrderState.CANCELED, filled_quantity=Decimal("0.1"))
        orders.fill_facts = []
        orders.position_facts = []
        orders.account_fact = replace(orders.account_fact, positions=(), exposure=Decimal(0))

        decision = adapter.reconcile_before_retry("order-1", facts=orders)
        self.assertFalse(decision.retry_allowed)
        self.assertEqual(decision.reason, "retry_blocked_receipt_has_execution")

        orders.query_result = receipt(OrderState.CANCELED)
        orders.account_fact = replace(orders.account_fact, positions=(canonical_facts()[1][0],), exposure=Decimal(0))
        with self.assertRaises(RuntimeRecoveryError) as raised:
            adapter.reconcile_before_retry("order-1", facts=orders)
        self.assertEqual(raised.exception.reason_code, "reconciliation_session_mismatch")

    def test_unknown_account_exposure_blocks_retry(self) -> None:
        adapter, orders = self.adapter()
        orders.query_result = receipt(OrderState.CANCELED)
        orders.fill_facts = []
        orders.position_facts = []
        orders.account_fact = replace(orders.account_fact, positions=(), exposure=None)

        decision = adapter.reconcile_before_retry("order-1", facts=orders)
        self.assertFalse(decision.retry_allowed)
        self.assertEqual(decision.reason, "retry_blocked_incomplete_facts")

    def test_retry_requires_matching_canonical_order_and_environment(self) -> None:
        adapter, orders = self.adapter()
        orders.fill_facts = []
        orders.position_facts = []
        orders.account_fact = replace(orders.account_fact, positions=(), exposure=Decimal(0))
        orders.query_result = receipt(OrderState.CANCELED, order_id="other-order")

        with self.assertRaises(RuntimeRecoveryError) as raised:
            adapter.reconcile_before_retry("order-1", facts=orders)
        self.assertEqual(raised.exception.reason_code, "reconciliation_order_identity_mismatch")

        adapter, orders = self.adapter()
        orders.fill_facts = []
        orders.position_facts = []
        orders.account_fact = replace(orders.account_fact, positions=(), exposure=Decimal(0))
        orders.query_result = receipt(OrderState.CANCELED, environment=BrokerEnvironment.TESTNET)
        with self.assertRaises(RuntimeRecoveryError) as raised:
            adapter.reconcile_before_retry("order-1", facts=orders)
        self.assertEqual(raised.exception.reason_code, "paper_receipt_required")

    def test_incomplete_reconciled_facts_block_retry(self) -> None:
        adapter, orders = self.adapter()
        orders.query_result = receipt(OrderState.CANCELED)
        orders.fill_facts = []
        orders.position_facts = []
        orders.account_fact = replace(orders.account_fact, positions=(), exposure=Decimal(0))
        orders.account_fact = None

        with self.assertRaises(RuntimeRecoveryError) as raised:
            adapter.reconcile_before_retry("order-1", facts=orders)

        self.assertEqual(raised.exception.reason_code, "fact_payload_invalid")
        self.assertEqual(adapter.state, RuntimeConnectionState.DEGRADED)

    def test_query_failure_becomes_degraded_and_never_blind_retries(self) -> None:
        adapter, orders = self.adapter()
        orders.query_error = TimeoutError("ambiguous")

        with self.assertRaises(RuntimeRecoveryError) as raised:
            adapter.reconcile_before_retry("order-1", facts=orders)

        self.assertEqual(raised.exception.reason_code, "reconciliation_ambiguous")
        self.assertEqual(adapter.state, RuntimeConnectionState.DEGRADED)

    def test_transport_error_disposition_is_explicit(self) -> None:
        adapter, _ = self.adapter()
        self.assertEqual(
            classify_transport_error(RateLimitError(retry_after_seconds=2)),
            TransportDisposition.AMBIGUOUS,
        )
        self.assertEqual(
            classify_transport_error(RateLimitError(retry_after_seconds=2, side_effect_free=True)),
            TransportDisposition.RETRYABLE,
        )
        self.assertEqual(classify_transport_error(TimeoutError()), TransportDisposition.AMBIGUOUS)
        self.assertEqual(classify_transport_error(ValueError("bad order")), TransportDisposition.NON_RETRYABLE)
        self.assertFalse(plan_retry(RateLimitError(retry_after_seconds=2), attempt=1).retry_allowed)
        self.assertTrue(
            plan_retry(
                RateLimitError(retry_after_seconds=2, side_effect_free=True),
                attempt=1,
            ).retry_allowed
        )
        self.assertFalse(plan_retry(TimeoutError(), attempt=1).retry_allowed)
        self.assertFalse(
            plan_retry(
                RateLimitError(retry_after_seconds=2, side_effect_free=True),
                attempt=3,
                policy=RetryPolicy(max_attempts=3),
            ).retry_allowed
        )
        self.assertEqual(
            adapter.retry_plan(
                RateLimitError(retry_after_seconds=2, side_effect_free=True),
                attempt=1,
            ).disposition,
            TransportDisposition.RETRYABLE,
        )
        deferred = plan_retry(
            RateLimitError(retry_after_seconds=120, side_effect_free=True),
            attempt=1,
            policy=RetryPolicy(max_delay_seconds=30),
        )
        self.assertFalse(deferred.retry_allowed)
        self.assertEqual(deferred.delay_seconds, 0)

    def test_retry_bounds_are_finite_and_explicit(self) -> None:
        with self.assertRaises(ValueError):
            RetryPolicy(max_delay_seconds=float("inf"))
        with self.assertRaises(ValueError):
            plan_retry(RateLimitError(retry_after_seconds=1, side_effect_free=True), attempt=0)


if __name__ == "__main__":
    unittest.main()
