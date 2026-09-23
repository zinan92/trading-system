import unittest
from datetime import UTC, datetime
from decimal import Decimal

from standard_broker.adapters.hyperliquid import (
    HyperliquidInstrumentAdapter,
    HyperliquidRuntimeAccountAdapter,
    HyperliquidRuntimeFeeAdapter,
    NautilusAdapterMetadata,
    NautilusHyperliquidRuntime,
    NautilusRuntimeConfig,
)
from standard_broker.capabilities import CapabilityDescriptor
from standard_broker.models import AccountScope, BrokerEnvironment, Provenance
from standard_broker.orders import OrderFill, OrderSide
from standard_broker.runtime import AccountReference, BrokerRuntimeSession, SignerReference
from standard_broker.runtime_facts import RuntimeFactLedger
from standard_broker.fees import FeeKind, FeeState


REVISION = "hyperliquid-runtime-account-fee-v1"
NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


def capabilities() -> CapabilityDescriptor:
    return CapabilityDescriptor(
        broker_id="hyperliquid",
        environment=BrokerEnvironment.PAPER,
        operations={
            "account": frozenset({"read", "liquidation"}),
            "fee": frozenset({"fill", "funding", "schedule"}),
            "order_execution": frozenset({"submit"}),
        },
        revision=REVISION,
    )


def provenance() -> Provenance:
    return Provenance(
        source="hyperliquid.account-fee.fixture",
        execution_scope="hypercore:default",
        transport_state="local_fixture",
        mapping_revision=REVISION,
        received_at=NOW,
    )


class FakeAccountFeeBackend:
    local_only = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []
        profile = capabilities()
        self.metadata = NautilusAdapterMetadata(
            package="nautilus-hyperliquid",
            version="1.230.0",
            commit="account-fee-commit",
            capabilities=profile,
        )
        self.responses: dict[str, object] = {
            "read": {
                "data": {
                    "assetPositions": [
                        {
                            "position": {
                                "coin": "BTC",
                                "positionId": "position-btc-1",
                                "szi": "0.1",
                                "entryPx": "65000",
                                "leverage": {"type": "cross", "value": 5},
                                "liquidationPx": "50000",
                                "marginUsed": "100",
                                "positionValue": "6500",
                                "unrealizedPnl": "20",
                            }
                        }
                    ],
                    "snapshotId": "snapshot-1",
                    "marginSummary": {
                        "accountValue": "1000",
                        "totalMarginUsed": "100",
                        "totalNtlPos": "6500",
                        "totalRawUsd": "900",
                    },
                    "withdrawable": "800",
                },
                "provenance": provenance(),
            },
            "liquidation": {
                "data": {
                    "lid": 9,
                    "liquidator": "0xliquidator",
                    "liquidated_user": "0xaccount",
                    "liquidated_ntl_pos": "1000",
                    "liquidated_account_value": "50",
                },
                "provenance": provenance(),
            },
            "fill": {
                "data": {
                    "coin": "BTC",
                    "px": "65000",
                    "sz": "0.1",
                    "side": "A",
                    "time": 1787313659000,
                    "closedPnl": "12.5",
                    "hash": "0xfill",
                    "oid": 11,
                    "tid": 77,
                    "crossed": True,
                    "fee": "0.45",
                    "feeToken": "USDC",
                    "builderFee": "0.05",
                },
                "provenance": provenance(),
            },
            "funding": {
                "data": {
                    "time": 1787313600000,
                    "coin": "BTC",
                    "usdc": "-0.2",
                    "szi": "0.1",
                    "fundingRate": "0.0001",
                },
                "provenance": provenance(),
            },
            "schedule": {
                "data": {
                    "userAddRate": "0.00015",
                    "userCrossRate": "0.00045",
                    "activeReferralDiscount": "0.04",
                },
                "provenance": provenance(),
            },
        }

    def invoke(self, port: str, operation: str, request: object) -> object:
        self.calls.append((port, operation, request))
        if port == "order_execution" and operation == "submit":
            return {
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {"statuses": [{"resting": {"oid": 11}}]},
                },
            }
        return self.responses[operation]


class HyperliquidRuntimeAccountFeeTests(unittest.TestCase):
    def instruments(self) -> HyperliquidInstrumentAdapter:
        return HyperliquidInstrumentAdapter.from_meta(
            {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 50}]},
            revision=REVISION,
        )

    def adapters(self):
        backend = FakeAccountFeeBackend()
        profile = capabilities()
        runtime = NautilusHyperliquidRuntime(
            session=BrokerRuntimeSession(
                broker_id="hyperliquid",
                environment=BrokerEnvironment.PAPER,
                account=AccountReference(AccountScope.MASTER, "0xaccount"),
                signer=SignerReference.paper(),
                signer_provider=None,
                capabilities=profile,
                execution_scope="hypercore:default",
                lifecycle_id="account-fee-runtime-1",
            ),
            backend=backend,
            config=NautilusRuntimeConfig("1.230.0", "account-fee-commit"),
        )
        runtime.start()
        ledger = RuntimeFactLedger()
        self.ledger = ledger
        self.runtime = runtime
        return (
            HyperliquidRuntimeAccountAdapter(
                runtime=runtime,
                instruments=self.instruments(),
                ledger=ledger,
            ),
            HyperliquidRuntimeFeeAdapter(
                runtime=runtime,
                instruments=self.instruments(),
                ledger=ledger,
            ),
            backend,
        )

    def test_account_and_position_facts_are_canonical_and_environment_bound(self) -> None:
        account, _, _ = self.adapters()

        snapshot = account.read_account("0xaccount")

        self.assertEqual(snapshot.environment, BrokerEnvironment.PAPER)
        self.assertEqual(snapshot.equity, Decimal("1000"))
        self.assertEqual(snapshot.positions[0].instrument_id, "BTC-USD-PERP")
        self.assertEqual(snapshot.positions[0].environment, BrokerEnvironment.PAPER)
        self.assertEqual(snapshot.observation_id, "snapshot-1")
        self.assertEqual(snapshot.unrealized_pnl, Decimal("20"))
        self.assertFalse(hasattr(snapshot, "assetPositions"))

    def test_fill_fee_is_actual_and_duplicate_identity_is_deduplicated(self) -> None:
        account, fees, _ = self.adapters()
        before = account.read_account("0xaccount")

        first = fees.read_fill({"tid": 77})
        duplicate = fees.read_fill({"tid": 77})

        self.assertIs(first, duplicate)
        self.assertEqual(len(fees.fill_facts), 1)
        self.assertEqual(first.environment, BrokerEnvironment.PAPER)
        self.assertEqual(first.fee.kind, FeeKind.TAKER)
        self.assertEqual(first.fee.state, FeeState.ACTUAL)
        self.assertEqual(first.fee.environment, BrokerEnvironment.PAPER)

        snapshot = account.read_account("0xaccount")
        self.assertNotEqual(before, snapshot)
        self.assertEqual(snapshot.realized_pnl, Decimal("12.5"))
        self.assertIs(snapshot, account.read_account("0xaccount"))

    def test_funding_is_actual_separate_and_duplicate_safe(self) -> None:
        _, fees, _ = self.adapters()

        first = fees.read_funding({"coin": "BTC", "time": 1787313600000})
        duplicate = fees.read_funding({"coin": "BTC", "time": 1787313600000})

        self.assertIs(first, duplicate)
        self.assertEqual(first.amount, Decimal("-0.2"))
        self.assertEqual(first.environment, BrokerEnvironment.PAPER)
        self.assertEqual(first.fee.kind, FeeKind.FUNDING)
        self.assertEqual(len(fees.funding_payments), 1)

    def test_fee_schedule_is_estimated_and_has_environment(self) -> None:
        _, fees, _ = self.adapters()

        schedule = fees.read_schedule({"account_address": "0xaccount"})

        self.assertEqual(schedule.state, FeeState.ESTIMATED)
        self.assertEqual(schedule.environment, BrokerEnvironment.PAPER)
        self.assertIn(f"{REVISION}:", schedule.schedule_identity)

    def test_liquidation_fact_does_not_invent_fee(self) -> None:
        account, _, _ = self.adapters()

        fact = account.read_liquidation({"account_address": "0xaccount"})

        self.assertEqual(fact.environment, BrokerEnvironment.PAPER)
        self.assertEqual(fact.liquidation_id, "9")
        self.assertIsNone(fact.liquidation_fee)
        self.assertIs(fact, account.read_liquidation({"account_address": "0xaccount"}))

    def test_missing_provenance_fails_closed(self) -> None:
        account, fees, backend = self.adapters()
        backend.responses["fill"] = {"data": backend.responses["fill"]["data"]}

        with self.assertRaises(ValueError):
            fees.read_fill({"tid": 77})

        backend.responses["read"] = {"data": backend.responses["read"]["data"]}
        with self.assertRaises(ValueError):
            account.read_account("0xaccount")

    def test_missing_observation_identity_fails_closed(self) -> None:
        account, _, backend = self.adapters()
        account_data = dict(backend.responses["read"]["data"])
        account_data.pop("snapshotId")
        backend.responses["read"] = {"data": account_data, "provenance": provenance()}
        with self.assertRaises(ValueError):
            account.read_account("0xaccount")

        liquidation_data = dict(backend.responses["liquidation"]["data"])
        liquidation_data.pop("lid")
        backend.responses["liquidation"] = {"data": liquidation_data, "provenance": provenance()}
        with self.assertRaises(ValueError):
            account.read_liquidation({"account_address": "0xaccount"})

    def test_incomplete_actual_fee_facts_fail_closed(self) -> None:
        _, fees, backend = self.adapters()
        fill_data = dict(backend.responses["fill"]["data"])
        fill_data.pop("fee")
        backend.responses["fill"] = {"data": fill_data, "provenance": provenance()}

        with self.assertRaises(ValueError):
            fees.read_fill({"tid": 77})

    def test_order_fill_to_fee_enrichment_to_account_pnl_is_connected(self) -> None:
        account, fees, _ = self.adapters()
        from standard_broker.adapters.hyperliquid import HyperliquidRuntimeOrderAdapter
        from standard_broker.orders import OrderIntent, OrderSide, OrderType, TimeInForce

        orders = HyperliquidRuntimeOrderAdapter(
            runtime=self.runtime,
            instruments=self.instruments(),
            ledger=self.ledger,
        )
        submitted = orders.submit(
            OrderIntent(
                order_id="order-linked",
                instrument_id="BTC-USD-PERP",
                side=OrderSide.SELL,
                order_type=OrderType.LIMIT,
                quantity=Decimal("0.1"),
                limit_price=Decimal("65000"),
                time_in_force=TimeInForce.GTC,
                idempotency_key="order-linked",
            )
        )
        orders.apply_fill(
            {
                "coin": "BTC",
                "px": "65000",
                "sz": "0.1",
                "side": "A",
                "time": 1787313659000,
                "oid": 11,
                "cloid": submitted.client_order_id,
                "tid": 77,
                "closedPnl": "12.5",
                "fee": "0.45",
                "feeToken": "USDC",
                "crossed": True,
                "provenance": provenance(),
            }
        )

        fact = fees.fill_facts[0]
        snapshot = account.read_account("0xaccount")

        self.assertEqual(fact.fill_id, "77")
        self.assertEqual(snapshot.realized_pnl, Decimal("12.5"))

    def test_nested_reconcile_fill_enriches_fee_and_account_pnl(self) -> None:
        account, fees, _ = self.adapters()
        from standard_broker.adapters.hyperliquid import HyperliquidRuntimeOrderAdapter
        from standard_broker.orders import OrderIntent, OrderSide, OrderType, TimeInForce

        orders = HyperliquidRuntimeOrderAdapter(
            runtime=self.runtime,
            instruments=self.instruments(),
            ledger=self.ledger,
        )
        submitted = orders.submit(
            OrderIntent(
                order_id="order-reconcile-linked",
                instrument_id="BTC-USD-PERP",
                side=OrderSide.SELL,
                order_type=OrderType.LIMIT,
                quantity=Decimal("0.1"),
                limit_price=Decimal("65000"),
                time_in_force=TimeInForce.GTC,
                idempotency_key="order-reconcile-linked",
            )
        )
        orders.reconcile(
            {
                "status": "order",
                "order": {
                    "order": {
                        "oid": 11,
                        "coin": "BTC",
                        "cloid": submitted.client_order_id,
                        "tid": 78,
                        "side": "A",
                        "px": "65000",
                        "sz": "0.1",
                        "time": 1787313659000,
                        "fee": "0.45",
                        "feeToken": "USDC",
                        "crossed": True,
                        "closedPnl": "12.5",
                        "provenance": provenance(),
                    },
                    "status": "filled",
                    "statusTimestamp": 1787313664000,
                },
            }
        )

        self.assertEqual(fees.fill_facts[0].fill_id, "78")
        self.assertEqual(account.read_account("0xaccount").realized_pnl, Decimal("12.5"))

    def test_snapshot_and_liquidation_identity_conflicts_fail_closed(self) -> None:
        account, _, backend = self.adapters()
        first = account.read_account("0xaccount")
        conflicting = dict(backend.responses["read"]["data"])
        conflicting["marginSummary"] = {"accountValue": "2000"}
        backend.responses["read"] = {"data": conflicting, "provenance": provenance()}

        with self.assertRaises(ValueError):
            account.read_account("0xaccount")

        first_liquidation = account.read_liquidation({"account_address": "0xaccount"})
        conflicting_liquidation = dict(backend.responses["liquidation"]["data"])
        conflicting_liquidation["liquidated_ntl_pos"] = "2000"
        backend.responses["liquidation"] = {
            "data": conflicting_liquidation,
            "provenance": provenance(),
        }

        self.assertIsNotNone(first)
        with self.assertRaises(ValueError):
            account.read_liquidation({"account_address": "0xaccount"})
        self.assertEqual(first_liquidation.liquidation_id, "9")

    def test_fee_fact_enriches_matching_order_fill_in_shared_ledger(self) -> None:
        _, fees, _ = self.adapters()
        self.ledger.order_fills["hyperliquid:paper:0xaccount:77"] = OrderFill(
            fill_id="77",
            order_id="order-1",
            broker_order_id="11",
            client_order_id="client-1",
            instrument_id="BTC-USD-PERP",
            side=OrderSide.SELL,
            price=Decimal("65000"),
            quantity=Decimal("0.1"),
            occurred_at=NOW,
        )

        fact = fees.read_fill({"tid": 77})

        self.assertEqual(fact.fill_id, "77")
        self.assertIn("hyperliquid:paper:0xaccount:77", self.ledger.fills)

    def test_account_binding_rejects_mismatched_account(self) -> None:
        account, _, _ = self.adapters()

        with self.assertRaises(ValueError):
            account.read_account("0xother")

        with self.assertRaises(ValueError):
            account.read_liquidation({"account_address": "0xother"})


if __name__ == "__main__":
    unittest.main()
