import unittest
from datetime import UTC, datetime
from decimal import Decimal

from standard_broker.adapters.hyperliquid import (
    HyperliquidAccountAdapter,
    HyperliquidFeeAdapter,
    UnknownInstrumentError,
)
from standard_broker.adapters.hyperliquid.instruments import (
    HyperliquidInstrumentAdapter,
)
from standard_broker.fees import FeeKind, FeeSource, FeeState
from standard_broker.models import Provenance


class HyperliquidAccountFeesTests(unittest.TestCase):
    NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)

    def make_instruments(self) -> HyperliquidInstrumentAdapter:
        return HyperliquidInstrumentAdapter.from_meta(
            {
                "universe": [
                    {"name": "BTC", "szDecimals": 5, "maxLeverage": 50},
                ]
            },
            revision="account-fixture-v1",
        )

    def provenance(self) -> Provenance:
        return Provenance(
            source="hyperliquid.account.fixture",
            execution_scope="hypercore:default",
            transport_state="local_fixture",
            mapping_revision="account-fixture-v1",
            received_at=self.NOW,
        )

    def test_clearinghouse_maps_account_and_position(self) -> None:
        adapter = HyperliquidAccountAdapter(self.make_instruments())
        snapshot = adapter.map_clearinghouse(
            account_address="0xaccount",
            raw={
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
                            "returnOnEquity": "0.1",
                            "unrealizedPnl": "20",
                        },
                        "type": "oneWay",
                    }
                ],
                "crossMarginSummary": {
                    "accountValue": "1000",
                    "totalMarginUsed": "100",
                    "totalNtlPos": "6500",
                    "totalRawUsd": "900",
                },
                "marginSummary": {
                    "accountValue": "1000",
                    "totalMarginUsed": "100",
                    "totalNtlPos": "6500",
                    "totalRawUsd": "900",
                },
                "withdrawable": "800",
            },
            provenance=self.provenance(),
            realized_pnl=Decimal(30),
        )

        self.assertEqual(snapshot.equity, Decimal(1000))
        self.assertEqual(snapshot.balance, Decimal(900))
        self.assertEqual(snapshot.withdrawable, Decimal(800))
        self.assertEqual(snapshot.margin_used, Decimal(100))
        self.assertEqual(snapshot.exposure, Decimal(6500))
        self.assertEqual(snapshot.realized_pnl, Decimal(30))
        self.assertEqual(snapshot.unrealized_pnl, Decimal(20))
        self.assertEqual(snapshot.positions[0].instrument_id, "BTC-USD-PERP")
        self.assertEqual(snapshot.positions[0].signed_quantity, Decimal("0.1"))

    def test_missing_account_facts_remain_unknown(self) -> None:
        snapshot = HyperliquidAccountAdapter(self.make_instruments()).map_clearinghouse(
            account_address="0xaccount",
            raw={"assetPositions": [], "marginSummary": {"accountValue": "1000"}},
            provenance=self.provenance(),
        )

        self.assertEqual(snapshot.equity, Decimal(1000))
        self.assertIsNone(snapshot.balance)
        self.assertIsNone(snapshot.withdrawable)
        self.assertIsNone(snapshot.realized_pnl)
        self.assertEqual(snapshot.unrealized_pnl, Decimal(0))

    def test_taker_fill_maps_actual_fee_and_realized_pnl(self) -> None:
        fill = HyperliquidFeeAdapter(self.make_instruments()).map_fill(
            {
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
            self.provenance(),
        )

        self.assertEqual(fill.instrument_id, "BTC-USD-PERP")
        self.assertEqual(fill.closed_pnl, Decimal("12.5"))
        self.assertEqual(fill.fee.kind, FeeKind.TAKER)
        self.assertEqual(fill.fee.source, FeeSource.ACTUAL_FILL)
        self.assertEqual(fill.fee.state, FeeState.ACTUAL)
        self.assertEqual(fill.fee.amount, Decimal("0.45"))
        self.assertEqual(fill.fee.currency, "USDC")
        self.assertIsNotNone(fill.builder_fee)
        self.assertEqual(fill.builder_fee.kind, FeeKind.BUILDER)
        self.assertEqual(fill.builder_fee.amount, Decimal("0.05"))

    def test_negative_maker_fee_maps_rebate_and_keeps_liquidity_side(self) -> None:
        fill = HyperliquidFeeAdapter(self.make_instruments()).map_fill(
            {
                "coin": "BTC",
                "px": "65000",
                "sz": "0.1",
                "side": "B",
                "time": 1787313659000,
                "closedPnl": "0",
                "hash": "0xrebate",
                "oid": 12,
                "tid": 78,
                "crossed": False,
                "fee": "-0.01",
                "feeToken": "USDC",
            },
            self.provenance(),
        )

        self.assertEqual(fill.fee.kind, FeeKind.REBATE)
        self.assertEqual(fill.fee.liquidity, "maker")
        self.assertEqual(fill.fee.amount, Decimal("-0.01"))

    def test_funding_is_a_separate_actual_payment(self) -> None:
        payment = HyperliquidFeeAdapter(self.make_instruments()).map_funding(
            {
                "time": 1787313600000,
                "coin": "BTC",
                "usdc": "-0.2",
                "szi": "0.1",
                "fundingRate": "0.0001",
            },
            self.provenance(),
        )

        self.assertEqual(payment.instrument_id, "BTC-USD-PERP")
        self.assertEqual(payment.amount, Decimal("-0.2"))
        self.assertEqual(payment.fee.kind, FeeKind.FUNDING)
        self.assertEqual(payment.fee.source, FeeSource.FUNDING_EVENT)
        self.assertEqual(payment.fee.state, FeeState.ACTUAL)
        self.assertEqual(payment.funding_rate, Decimal("0.0001"))

    def test_fee_schedule_is_estimated_and_has_provenance_identity(self) -> None:
        schedule = HyperliquidFeeAdapter(self.make_instruments()).map_fee_schedule(
            {
                "userAddRate": "0.00015",
                "userCrossRate": "0.00045",
                "activeReferralDiscount": "0.04",
                "feeSchedule": {"add": "0.00015", "cross": "0.00045"},
            },
            self.provenance(),
        )

        self.assertEqual(schedule.maker_rate, Decimal("0.00015"))
        self.assertEqual(schedule.taker_rate, Decimal("0.00045"))
        self.assertEqual(schedule.state, FeeState.ESTIMATED)
        self.assertEqual(schedule.source, FeeSource.SCHEDULE)
        self.assertIn("account-fixture-v1:", schedule.schedule_identity)

    def test_realized_pnl_can_be_aggregated_from_fill_facts(self) -> None:
        fee_adapter = HyperliquidFeeAdapter(self.make_instruments())
        fills = [
            fee_adapter.map_fill(
                {
                    "coin": "BTC",
                    "px": "65000",
                    "sz": "0.1",
                    "side": "A",
                    "time": 1787313659000,
                    "closedPnl": "12.5",
                    "hash": "0xfill-a",
                    "tid": 80,
                    "crossed": True,
                    "fee": "0.45",
                    "feeToken": "USDC",
                },
                self.provenance(),
            ),
            fee_adapter.map_fill(
                {
                    "coin": "BTC",
                    "px": "65000",
                    "sz": "0.1",
                    "side": "B",
                    "time": 1787313659000,
                    "closedPnl": "-2.5",
                    "hash": "0xfill-b",
                    "tid": 81,
                    "crossed": True,
                    "fee": "0.45",
                    "feeToken": "USDC",
                },
                self.provenance(),
            ),
        ]

        realized = HyperliquidAccountAdapter(self.make_instruments()).realized_pnl_from_fills(
            fills + [fills[0]]
        )

        self.assertEqual(realized, Decimal("10.0"))

    def test_liquidation_fact_does_not_invent_a_fee(self) -> None:
        fact = HyperliquidAccountAdapter(self.make_instruments()).map_liquidation(
            {
                "lid": 9,
                "liquidator": "0xliquidator",
                "liquidated_user": "0xaccount",
                "liquidated_ntl_pos": "1000",
                "liquidated_account_value": "50",
            },
            self.provenance(),
        )

        self.assertEqual(fact.liquidation_id, "9")
        self.assertEqual(fact.notional, Decimal(1000))
        self.assertIsNone(fact.liquidation_fee)

    def test_unknown_fill_instrument_fails_closed(self) -> None:
        with self.assertRaises(UnknownInstrumentError):
            HyperliquidFeeAdapter(self.make_instruments()).map_fill(
                {
                    "coin": "ETH",
                    "px": "3000",
                    "sz": "1",
                    "side": "A",
                    "time": 1787313659000,
                    "closedPnl": "0",
                    "hash": "0xunknown",
                    "oid": 13,
                    "tid": 79,
                    "crossed": True,
                    "fee": "0.1",
                    "feeToken": "USDC",
                },
                self.provenance(),
            )

    def test_unknown_fill_side_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            HyperliquidFeeAdapter(self.make_instruments()).map_fill(
                {
                    "coin": "BTC",
                    "px": "65000",
                    "sz": "1",
                    "side": "unknown",
                    "time": 1787313659000,
                    "closedPnl": "0",
                    "hash": "0xbad-side",
                    "tid": 82,
                    "crossed": True,
                    "fee": "0.1",
                    "feeToken": "USDC",
                },
                self.provenance(),
            )


if __name__ == "__main__":
    unittest.main()
