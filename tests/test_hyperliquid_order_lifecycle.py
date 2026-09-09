import unittest
from datetime import UTC, datetime
from decimal import Decimal

from standard_broker.adapters.hyperliquid import HyperliquidOrderAdapter
from standard_broker.errors import RuntimeBoundaryError
from standard_broker.models import BrokerEnvironment
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

    def intent(
        self,
        *,
        order_id: str = "o-1",
        key: str = "cycle:o-1",
        client_order_id: str | None = None,
    ) -> OrderIntent:
        return OrderIntent(
            order_id=order_id,
            instrument_id="BTC-USD-PERP",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Decimal("0.1"),
            limit_price=Decimal(65000),
            time_in_force=TimeInForce.GTC,
            idempotency_key=key,
            client_order_id=client_order_id,
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

    def test_external_testnet_hash_only_to_tid_promotion_fails_closed(self) -> None:
        class ExternalFixtureTransport(InMemoryOrderTransport):
            local_only = False
            external_network = True

        transport = ExternalFixtureTransport(submit_response=self.resting_response())
        adapter = HyperliquidOrderAdapter(
            transport=transport,
            environment=BrokerEnvironment.TESTNET,
            transport_state="external_testnet",
        )
        submitted = adapter.submit(self.intent())
        hash_only = {
            "oid": 101,
            "coin": "BTC",
            "side": "B",
            "px": "65000",
            "sz": "0.1",
            "time": 1787313659000,
            "hash": "0xexternal-hash-only",
        }

        adapter.apply_fill(hash_only)

        with self.assertRaises(ValueError):
            adapter.apply_fill({**hash_only, "cloid": submitted.client_order_id, "tid": "trade-promoted"})

        with self.assertRaises(ValueError):
            HyperliquidOrderAdapter(
                transport=transport,
                environment=BrokerEnvironment.TESTNET,
                transport_state="local_fixture",
            )

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

    def test_unknown_order_cannot_blind_retry_cancel_before_reconciliation(self) -> None:
        transport = InMemoryOrderTransport(submit_response=TimeoutError("transport timeout"))
        adapter = HyperliquidOrderAdapter(transport=transport)
        unknown = adapter.submit(self.intent())

        with self.assertRaises(RuntimeBoundaryError) as raised:
            adapter.cancel(unknown.order_id)

        self.assertEqual(raised.exception.reason_code, "reconciliation_required")
        self.assertEqual(len(transport.cancel_calls), 0)

    def test_unrecognized_order_status_enters_unknown_until_reconciliation(self) -> None:
        transport = InMemoryOrderTransport(submit_response=self.resting_response())
        adapter = HyperliquidOrderAdapter(transport=transport)
        submitted = adapter.submit(self.intent())

        unknown = adapter.apply_order_update(
            {
                "status": "broker_added_state",
                "oid": 101,
                "cloid": submitted.client_order_id,
                "timestamp": 1787313660000,
            }
        )
        reconciled = adapter.reconcile(
            {
                "status": "open",
                "oid": 101,
                "cloid": submitted.client_order_id,
                "timestamp": 1787313661000,
            }
        )

        self.assertEqual(unknown.state, OrderState.UNKNOWN)
        self.assertEqual(unknown.reason, "unrecognized_order_status:broker_added_state")
        self.assertEqual(reconciled.state, OrderState.RESTING)

    def test_open_orders_projects_unregistered_exchange_orders_without_mutating_registry(self) -> None:
        transport = InMemoryOrderTransport(
            submit_response=self.resting_response(),
            open_orders_response={
                "orders": [
                    {
                        "status": "open",
                        "oid": 201,
                        "cloid": "0xunregistered-1",
                        "sz": "0.2",
                        "origSz": "0.2",
                        "timestamp": 1787313661000,
                    },
                    {
                        "status": "open",
                        "oid": 202,
                        "cloid": "0xunregistered-2",
                        "sz": "0.3",
                        "origSz": "0.3",
                        "timestamp": 1787313662000,
                    },
                ]
            },
        )
        adapter = HyperliquidOrderAdapter(transport=transport)

        open_orders = adapter.open_orders("BTC-USD-PERP")

        self.assertEqual([receipt.order_id for receipt in open_orders], ["external:201", "external:202"])
        self.assertTrue(all(receipt.state is OrderState.UNKNOWN for receipt in open_orders))
        self.assertTrue(all(receipt.reason == "unregistered_exchange_order" for receipt in open_orders))
        with self.assertRaises(KeyError):
            adapter.get("external:201")

    def test_open_orders_keeps_known_identity_and_projects_only_unregistered_order(self) -> None:
        transport = InMemoryOrderTransport(submit_response=self.resting_response())
        adapter = HyperliquidOrderAdapter(transport=transport)
        submitted = adapter.submit(self.intent())
        transport.open_orders_response = {
            "orders": [
                {
                    "status": "open",
                    "oid": 101,
                    "cloid": submitted.client_order_id,
                    "sz": "0.1",
                    "origSz": "0.1",
                    "timestamp": 1787313661000,
                },
                {
                    "status": "open",
                    "oid": 202,
                    "cloid": "0xunregistered-2",
                    "sz": "0.3",
                    "origSz": "0.3",
                    "timestamp": 1787313662000,
                },
            ]
        }

        known, external = adapter.open_orders("BTC-USD-PERP")

        self.assertEqual(known.order_id, submitted.order_id)
        self.assertEqual(known.state, OrderState.RESTING)
        self.assertEqual(external.order_id, "external:202")
        self.assertTrue(external.is_unregistered_broker_order)

    def test_recover_then_open_orders_resolves_all_orders_to_canonical_identity(self) -> None:
        rows = [
            {
                "status": "open",
                "oid": 300 + index,
                "cloid": f"0xrecovered-{index}",
                "sz": "0.1",
                "origSz": "0.1",
                "timestamp": 1787313661000 + index,
            }
            for index in range(5)
        ]
        transport = InMemoryOrderTransport(
            submit_response=self.resting_response(),
            open_orders_response={"orders": rows},
        )
        adapter = HyperliquidOrderAdapter(transport=transport)
        for index, row in enumerate(rows):
            adapter.recover(
                self.intent(
                    order_id=f"recovered-{index}",
                    key=f"cycle:recovered-{index}",
                    client_order_id=str(row["cloid"]),
                ),
                broker_order_id=str(row["oid"]),
                state=OrderState.RESTING,
            )

        open_orders = adapter.open_orders("BTC-USD-PERP")

        self.assertEqual(
            [receipt.order_id for receipt in open_orders],
            [f"recovered-{index}" for index in range(5)],
        )
        self.assertFalse(any(receipt.is_unregistered_broker_order for receipt in open_orders))

    def test_strict_reconcile_rejects_unregistered_client_identity(self) -> None:
        adapter = HyperliquidOrderAdapter(
            transport=InMemoryOrderTransport(submit_response=self.resting_response())
        )

        with self.assertRaisesRegex(
            ValueError,
            "unknown client order identity cannot fall back to Broker order identity",
        ):
            adapter.reconcile(
                {
                    "status": "open",
                    "oid": 201,
                    "cloid": "0xunregistered-1",
                    "sz": "0.2",
                    "origSz": "0.2",
                    "timestamp": 1787313661000,
                }
            )

    def test_strict_fill_path_rejects_unregistered_client_identity(self) -> None:
        adapter = HyperliquidOrderAdapter(
            transport=InMemoryOrderTransport(submit_response=self.resting_response())
        )

        with self.assertRaisesRegex(
            ValueError,
            "unknown client order identity cannot fall back to Broker order identity",
        ):
            adapter.apply_fill(
                {
                    "coin": "BTC",
                    "side": "B",
                    "px": "65000",
                    "sz": "0.1",
                    "time": 1787313661000,
                    "tid": 501,
                    "oid": 201,
                    "cloid": "0xunregistered-1",
                }
            )

    def test_terminal_order_ignores_late_non_terminal_event(self) -> None:
        transport = InMemoryOrderTransport(submit_response=self.resting_response())
        adapter = HyperliquidOrderAdapter(transport=transport)
        submitted = adapter.submit(self.intent())

        filled = adapter.apply_order_update(
            {
                "status": "filled",
                "oid": 101,
                "cloid": submitted.client_order_id,
                "coin": "BTC",
                "tid": 501,
                "side": "B",
                "px": "65000",
                "sz": "0.1",
                "time": 1787313661000,
            }
        )
        late = adapter.apply_order_update(
            {
                "status": "open",
                "oid": 101,
                "cloid": submitted.client_order_id,
                "timestamp": 1787313662000,
            }
        )

        self.assertEqual(filled.state, OrderState.FILLED)
        self.assertEqual(late, filled)

    def test_filled_submit_observation_registers_aliases_for_duplicate_fill(self) -> None:
        transport = InMemoryOrderTransport(
            submit_response={
                "status": "ok",
                "response": {
                    "data": {
                        "statuses": [
                            {
                                "filled": {
                                    "oid": 101,
                                    "totalSz": "0.1",
                                    "avgPx": "65000",
                                    "side": "B",
                                    "tid": 501,
                                    "time": 1787313661000,
                                }
                            }
                        ]
                    }
                },
            }
        )
        adapter = HyperliquidOrderAdapter(transport=transport)
        submitted = adapter.submit(self.intent())

        duplicate = adapter.apply_fill(
            {
                "oid": 101,
                "cloid": submitted.client_order_id,
                "coin": "BTC",
                "tid": "501",
                "side": "B",
                "px": "65000",
                "sz": "0.1",
                "time": 1787313661000,
            }
        )

        self.assertEqual(duplicate.state, OrderState.FILLED)
        self.assertEqual(len(adapter.fills), 1)

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
