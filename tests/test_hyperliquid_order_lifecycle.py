import unittest
from datetime import UTC, datetime
from decimal import Decimal

from standard_broker.adapters.hyperliquid import HyperliquidOrderAdapter
from standard_broker.orders import (
    InMemoryOrderTransport,
    OrderIntent,
    OrderSide,
    OrderState,
    OrderType,
    TimeInForce,
)


class HyperliquidOrderLifecycleTests(unittest.TestCase):
    NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)

    def intent(self, *, order_id: str = "o-1", key: str = "cycle:o-1") -> OrderIntent:
        return OrderIntent(
            order_id=order_id,
            instrument_id="BTC-USD-PERP",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Decimal("0.1"),
            limit_price=Decimal(65000),
            time_in_force=TimeInForce.GTC,
            idempotency_key=key,
        )

    def resting_response(self, oid: int = 101) -> dict:
        return {
            "status": "ok",
            "response": {
                "type": "order",
                "data": {"statuses": [{"resting": {"oid": oid}}]},
            },
        }

    def test_submit_resting_order_returns_normalized_receipt(self) -> None:
        transport = InMemoryOrderTransport(submit_response=self.resting_response())
        adapter = HyperliquidOrderAdapter(transport=transport)

        receipt = adapter.submit(self.intent())

        self.assertEqual(receipt.state, OrderState.RESTING)
        self.assertEqual(receipt.broker_order_id, "101")
        self.assertTrue(receipt.client_order_id.startswith("0x"))
        self.assertEqual(len(receipt.client_order_id), 34)
        self.assertEqual(receipt.remaining_quantity, Decimal("0.1"))
        self.assertEqual(len(transport.submit_calls), 1)

    def test_same_idempotency_key_does_not_submit_twice(self) -> None:
        transport = InMemoryOrderTransport(submit_response=self.resting_response())
        adapter = HyperliquidOrderAdapter(transport=transport)
        first = adapter.submit(self.intent())
        second = adapter.submit(self.intent())

        self.assertEqual(first, second)
        self.assertEqual(len(transport.submit_calls), 1)

    def test_filled_submit_response_preserves_quantity_and_average_price(self) -> None:
        transport = InMemoryOrderTransport(
            submit_response={
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {
                        "statuses": [
                            {
                                "filled": {
                                    "totalSz": "0.1",
                                    "avgPx": "65001.2",
                                    "oid": 102,
                                }
                            }
                        ]
                    },
                },
            }
        )
        receipt = HyperliquidOrderAdapter(transport=transport).submit(self.intent())

        self.assertEqual(receipt.state, OrderState.UNKNOWN)
        self.assertEqual(receipt.reason, "filled_without_fill_identity")
        self.assertEqual(receipt.filled_quantity, Decimal("0.1"))
        self.assertEqual(receipt.remaining_quantity, Decimal(0))
        self.assertEqual(receipt.average_fill_price, Decimal("65001.2"))

    def test_partial_fills_are_idempotent_and_preserve_remaining(self) -> None:
        adapter = HyperliquidOrderAdapter(
            transport=InMemoryOrderTransport(submit_response=self.resting_response())
        )
        submitted = adapter.submit(self.intent())
        fill = {
            "coin": "BTC",
            "px": "65000",
            "sz": "0.04",
            "side": "B",
            "time": 1787313659000,
            "oid": 101,
            "cloid": submitted.client_order_id,
            "tid": 501,
        }

        first = adapter.apply_fill(fill)
        duplicate = adapter.apply_fill(fill)

        self.assertEqual(first.state, OrderState.PARTIALLY_FILLED)
        self.assertEqual(first.filled_quantity, Decimal("0.04"))
        self.assertEqual(first.remaining_quantity, Decimal("0.06"))
        self.assertEqual(duplicate, first)
        self.assertEqual(adapter.fills["501"].instrument_id, "BTC-USD-PERP")

    def test_timeout_is_unknown_and_same_intent_is_not_retried(self) -> None:
        transport = InMemoryOrderTransport(submit_response=TimeoutError("transport timeout"))
        adapter = HyperliquidOrderAdapter(transport=transport)

        first = adapter.submit(self.intent())
        second = adapter.submit(self.intent())

        self.assertEqual(first.state, OrderState.UNKNOWN)
        self.assertEqual(second.state, OrderState.UNKNOWN)
        self.assertEqual(len(transport.submit_calls), 1)

    def test_cancel_waits_for_authoritative_cancel_event(self) -> None:
        transport = InMemoryOrderTransport(
            submit_response=self.resting_response(),
            cancel_response={"status": "ok"},
        )
        adapter = HyperliquidOrderAdapter(transport=transport)
        submitted = adapter.submit(self.intent())
        pending = adapter.cancel(submitted.order_id)

        self.assertEqual(pending.state, OrderState.CANCEL_PENDING)
        canceled = adapter.apply_order_update(
            {
                "status": "canceled",
                "oid": 101,
                "cloid": submitted.client_order_id,
                "timestamp": 1787313660000,
            }
        )
        self.assertEqual(canceled.state, OrderState.CANCELED)

    def test_modify_promotes_new_oid_and_suppresses_old_cancel(self) -> None:
        transport = InMemoryOrderTransport(
            submit_response=self.resting_response(oid=201),
            modify_response={"status": "ok"},
        )
        adapter = HyperliquidOrderAdapter(transport=transport)
        submitted = adapter.submit(self.intent())
        modified = adapter.modify(submitted.order_id, self.intent(order_id="o-1", key="cycle:o-1"))
        self.assertEqual(modified.state, OrderState.MODIFY_PENDING)

        promoted = adapter.apply_order_update(
            {
                "status": "accepted",
                "oid": 202,
                "cloid": submitted.client_order_id,
                "origSz": "0.1",
                "sz": "0.1",
                "limitPx": "65010",
                "timestamp": 1787313660000,
            }
        )
        stale_cancel = adapter.apply_order_update(
            {
                "status": "canceled",
                "oid": 201,
                "cloid": submitted.client_order_id,
                "timestamp": 1787313660001,
            }
        )

        self.assertEqual(promoted.state, OrderState.RESTING)
        self.assertEqual(promoted.broker_order_id, "202")
        self.assertEqual(stale_cancel.state, OrderState.RESTING)
        self.assertEqual(stale_cancel.broker_order_id, "202")

    def test_reconciliation_promotes_replacement_after_missing_websocket_event(self) -> None:
        transport = InMemoryOrderTransport(
            submit_response=self.resting_response(oid=301),
            modify_response={"status": "ok"},
        )
        adapter = HyperliquidOrderAdapter(transport=transport)
        submitted = adapter.submit(self.intent())
        adapter.modify(submitted.order_id, self.intent())

        reconciled = adapter.reconcile(
            {
                "status": "order",
                "order": {
                    "order": {
                        "oid": 302,
                        "cloid": submitted.client_order_id,
                        "sz": "0.1",
                        "origSz": "0.1",
                        "limitPx": "65020",
                    },
                    "status": "open",
                    "statusTimestamp": 1787313661000,
                },
            }
        )

        self.assertEqual(reconciled.state, OrderState.RESTING)
        self.assertEqual(reconciled.broker_order_id, "302")

    def test_rejected_submit_is_not_reported_as_accepted(self) -> None:
        transport = InMemoryOrderTransport(
            submit_response={
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {"statuses": [{"error": "tickRejected"}]},
                },
            }
        )
        receipt = HyperliquidOrderAdapter(transport=transport).submit(self.intent())

        self.assertEqual(receipt.state, OrderState.REJECTED)
        self.assertEqual(receipt.reason, "tickRejected")

    def test_unknown_fill_side_fails_closed(self) -> None:
        transport = InMemoryOrderTransport(submit_response=self.resting_response())
        adapter = HyperliquidOrderAdapter(transport=transport)
        submitted = adapter.submit(self.intent())

        with self.assertRaises(ValueError):
            adapter.apply_fill(
                {
                    "coin": "BTC",
                    "px": "65000",
                    "sz": "0.01",
                    "side": "unknown",
                    "time": 1787313659000,
                    "oid": 101,
                    "cloid": submitted.client_order_id,
                    "tid": 502,
                }
            )


if __name__ == "__main__":
    unittest.main()
