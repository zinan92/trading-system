import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from standard_broker.adapters.hyperliquid import (
    HyperliquidInstrumentAdapter,
    HyperliquidMarketDataAdapter,
    UnsupportedProductError,
)
from standard_broker.instruments import ContractType, MarginMode
from standard_broker.market_data import (
    FreshnessPolicy,
    FreshnessState,
    MarketDataEnvelope,
)
from standard_broker.models import Provenance


class HyperliquidMarketDataTests(unittest.TestCase):
    REVISION = "hyperliquid-fixture-v1"
    NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)

    def make_instruments(self) -> HyperliquidInstrumentAdapter:
        return HyperliquidInstrumentAdapter.from_meta(
            {
                "universe": [
                    {"name": "BTC", "szDecimals": 5, "maxLeverage": 50},
                    {"name": "HPOS", "szDecimals": 0, "maxLeverage": 3, "onlyIsolated": True},
                ]
            },
            revision=self.REVISION,
        )

    def make_provenance(self, received_at: datetime | None = None) -> Provenance:
        return Provenance(
            source="hyperliquid.info.fixture",
            execution_scope="hypercore:default",
            transport_state="local_fixture",
            mapping_revision=self.REVISION,
            received_at=received_at or self.NOW,
        )

    def test_meta_maps_default_perp_and_precision_rules(self) -> None:
        instruments = self.make_instruments()

        btc = instruments.get("BTC-USD-PERP")
        hpos = instruments.get("HPOS-USD-PERP")

        self.assertEqual(btc.broker_symbol, "BTC")
        self.assertEqual(btc.asset_index, 0)
        self.assertEqual(btc.contract_type, ContractType.LINEAR_PERPETUAL)
        self.assertEqual(btc.quantity_step, Decimal("0.00001"))
        self.assertEqual(btc.price_rule.max_decimal_places, 1)
        self.assertEqual(btc.minimum_notional, Decimal(10))
        self.assertEqual(btc.margin_mode, MarginMode.CROSS)
        self.assertEqual(hpos.margin_mode, MarginMode.ISOLATED)

    def test_price_rule_enforces_significant_figures_and_decimal_limit(self) -> None:
        btc = self.make_instruments().get("BTC-USD-PERP")

        self.assertTrue(btc.price_rule.is_valid(Decimal("1234.5")))
        self.assertFalse(btc.price_rule.is_valid(Decimal("1234.56")))
        self.assertFalse(btc.price_rule.is_valid(Decimal("0.00123")))
        self.assertFalse(btc.price_rule.is_valid(Decimal(0)))

    def test_candles_map_to_standard_kline_and_keep_provenance(self) -> None:
        adapter = HyperliquidMarketDataAdapter(self.make_instruments())
        candles = adapter.map_candles(
            "BTC",
            "1m",
            [
                {
                    "T": 1787313659999,
                    "c": "65001.2",
                    "h": "65010.0",
                    "i": "1m",
                    "l": "64990.0",
                    "n": 42,
                    "o": "65000.0",
                    "s": "BTC",
                    "t": 1787313600000,
                    "v": "12.5",
                }
            ],
            self.make_provenance(),
        )

        candle = candles[0]
        self.assertEqual(candle.instrument_id, "BTC-USD-PERP")
        self.assertEqual(candle.interval, "1m")
        self.assertEqual(candle.open, Decimal("65000.0"))
        self.assertEqual(candle.close, Decimal("65001.2"))
        self.assertEqual(candle.trade_count, 42)
        self.assertEqual(candle.provenance.source, "hyperliquid.info.fixture")
        self.assertFalse(hasattr(candle, "coin"))

    def test_trades_and_order_book_map_to_canonical_models(self) -> None:
        adapter = HyperliquidMarketDataAdapter(self.make_instruments())
        trades = adapter.map_trades(
            "BTC",
            [
                {
                    "coin": "BTC",
                    "side": "B",
                    "px": "65000.1",
                    "sz": "0.01",
                    "hash": "0xtrade",
                    "tid": 77,
                    "time": 1787313659000,
                }
            ],
            self.make_provenance(),
        )
        book = adapter.map_l2_book(
            "BTC",
            {
                "coin": "BTC",
                "levels": [
                    [{"px": "64999.0", "sz": "1.2", "n": 3}],
                    [{"px": "65001.0", "sz": "0.8", "n": 2}],
                ],
                "time": 1787313659000,
            },
            self.make_provenance(),
            n_sig_figs=5,
            mantissa=2,
        )

        self.assertEqual(trades[0].instrument_id, "BTC-USD-PERP")
        self.assertEqual(trades[0].side, "buy")
        self.assertEqual(trades[0].trade_id, "77")
        self.assertEqual(book.instrument_id, "BTC-USD-PERP")
        self.assertEqual(book.bids[0].price, Decimal("64999.0"))
        self.assertEqual(book.asks[0].order_count, 2)
        self.assertEqual(book.depth, 1)
        self.assertEqual(book.n_sig_figs, 5)
        self.assertEqual(book.mantissa, 2)

    def test_mid_and_bbo_remain_distinct_ticker_facts(self) -> None:
        adapter = HyperliquidMarketDataAdapter(self.make_instruments())
        ticker = adapter.map_ticker(
            "BTC",
            mid="65000.0",
            bbo={
                "coin": "BTC",
                "time": 1787313659000,
                "bbo": [
                    {"px": "64999.0", "sz": "1.0", "n": 1},
                    {"px": "65001.0", "sz": "0.9", "n": 2},
                ],
            },
            provenance=self.make_provenance(),
        )

        self.assertEqual(ticker.mid, Decimal("65000.0"))
        self.assertEqual(ticker.bid, Decimal("64999.0"))
        self.assertEqual(ticker.ask, Decimal("65001.0"))
        self.assertNotEqual(ticker.mid, ticker.bid)

    def test_freshness_never_reports_stale_data_as_fresh(self) -> None:
        policy = FreshnessPolicy(max_age=timedelta(seconds=5))

        fresh = policy.classify(self.NOW - timedelta(seconds=4), self.NOW)
        stale = policy.classify(self.NOW - timedelta(seconds=6), self.NOW)
        reconnecting = policy.classify(
            self.NOW - timedelta(seconds=1),
            self.NOW,
            transport_state="reconnecting",
        )

        self.assertEqual(fresh, FreshnessState.FRESH)
        self.assertEqual(stale, FreshnessState.STALE)
        self.assertEqual(reconnecting, FreshnessState.UNKNOWN)

    def test_observation_envelope_carries_stale_state(self) -> None:
        adapter = HyperliquidMarketDataAdapter(self.make_instruments())
        book = adapter.map_l2_book(
            "BTC",
            {
                "coin": "BTC",
                "levels": [[], []],
                "time": 1787313659000,
            },
            self.make_provenance(self.NOW - timedelta(seconds=6)),
        )

        envelope = adapter.envelope(
            book,
            FreshnessPolicy(max_age=timedelta(seconds=5)),
            now=self.NOW,
        )

        self.assertIsInstance(envelope, MarketDataEnvelope)
        self.assertEqual(envelope.freshness, FreshnessState.STALE)
        self.assertEqual(envelope.data.instrument_id, "BTC-USD-PERP")

    def test_unsupported_product_scope_fails_closed(self) -> None:
        with self.assertRaises(UnsupportedProductError):
            HyperliquidInstrumentAdapter.from_meta(
                {"universe": [{"name": "xyz:TSLA", "szDecimals": 2, "maxLeverage": 5}]},
                revision=self.REVISION,
            )


if __name__ == "__main__":
    unittest.main()
