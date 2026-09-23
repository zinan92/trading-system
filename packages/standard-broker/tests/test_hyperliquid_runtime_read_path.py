import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from standard_broker.adapters.hyperliquid import (
    HyperliquidInstrumentAdapter,
    HyperliquidRuntimeReadAdapter,
    NautilusAdapterMetadata,
    NautilusHyperliquidRuntime,
    NautilusRuntimeConfig,
)
from standard_broker.capabilities import CapabilityDescriptor
from standard_broker.errors import MarketDataGateError
from standard_broker.market_data import FreshnessPolicy, FreshnessState
from standard_broker.models import AccountScope, BrokerEnvironment, Provenance
from standard_broker.orders import OrderIntent, OrderSide, OrderType, SlippagePolicy, TimeInForce
from standard_broker.runtime import AccountReference, BrokerRuntimeSession, SignerReference


NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
REVISION = "hyperliquid-runtime-read-v1"


def capabilities() -> CapabilityDescriptor:
    return CapabilityDescriptor(
        broker_id="hyperliquid",
        environment=BrokerEnvironment.PAPER,
        operations={
            "market_data": frozenset({"candles", "trades", "order_book", "ticker"}),
            "instrument": frozenset({"read"}),
            "order_execution": frozenset({"submit_ioc_limit"}),
        },
        revision=REVISION,
    )


def provenance(received_at: datetime = NOW, transport_state: str = "local_fixture") -> Provenance:
    return Provenance(
        source="hyperliquid.info.fixture",
        execution_scope="hypercore:default",
        transport_state=transport_state,
        mapping_revision=REVISION,
        received_at=received_at,
    )


class FakeReadBackend:
    local_only = True

    def __init__(
        self,
        responses: dict[str, object],
        capability_profile: CapabilityDescriptor | None = None,
    ) -> None:
        self.calls: list[tuple[str, str, object]] = []
        self.responses = responses
        self.metadata = NautilusAdapterMetadata(
            package="nautilus-hyperliquid",
            version="1.230.0",
            commit="read-path-commit",
            capabilities=capability_profile or capabilities(),
        )

    def invoke(self, port: str, operation: str, request: object) -> object:
        self.calls.append((port, operation, request))
        return self.responses[operation]


class HyperliquidRuntimeReadPathTests(unittest.TestCase):
    def instruments(self) -> HyperliquidInstrumentAdapter:
        return HyperliquidInstrumentAdapter.from_meta(
            {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 50}]},
            revision=REVISION,
        )

    def session(self) -> BrokerRuntimeSession:
        return BrokerRuntimeSession(
            broker_id="hyperliquid",
            environment=BrokerEnvironment.PAPER,
            account=AccountReference(AccountScope.MASTER, "0xmaster"),
            signer=SignerReference.paper(),
            signer_provider=None,
            capabilities=capabilities(),
            execution_scope="hypercore:default",
            lifecycle_id="read-path-1",
        )

    def adapter(
        self,
        *,
        received_at: datetime = NOW,
        transport_state: str = "local_fixture",
        bbo: list[dict[str, object] | None] | None = None,
        bbo_coin: str | None = "BTC",
    ) -> tuple[HyperliquidRuntimeReadAdapter, FakeReadBackend]:
        selected_bbo = bbo or [
            {"px": "64999.0", "sz": "1.0", "n": 1},
            {"px": "65001.0", "sz": "0.9", "n": 2},
        ]
        rows = {
            "candles": {
                "rows": [
                    {
                        "T": 1787313659999,
                        "c": "65001.2",
                        "h": "65010.0",
                        "l": "64990.0",
                        "n": 42,
                        "o": "65000.0",
                        "s": "BTC",
                        "t": 1787313600000,
                        "v": "12.5",
                    }
                ],
                "provenance": provenance(received_at, transport_state),
            },
            "trades": {
                "rows": [
                    {
                        "coin": "BTC",
                        "side": "B",
                        "px": "65000.1",
                        "sz": "0.01",
                        "tid": 77,
                        "time": 1787313659000,
                    }
                ],
                "provenance": provenance(received_at, transport_state),
            },
            "order_book": {
                "row": {
                    "coin": "BTC",
                    "levels": [[{"px": "64999.0", "sz": "1.2", "n": 3}], [{"px": "65001.0", "sz": "0.8", "n": 2}]],
                    "time": 1787313659000,
                },
                "provenance": provenance(received_at, transport_state),
            },
            "ticker": {
                "mid": "65000.0",
                "bbo": {
                    **({"coin": bbo_coin} if bbo_coin is not None else {}),
                    "time": 1787313659000,
                    "bbo": selected_bbo,
                },
                "provenance": provenance(received_at, transport_state),
            },
        }
        backend = FakeReadBackend(rows)
        runtime = NautilusHyperliquidRuntime(
            session=self.session(),
            backend=backend,
            config=NautilusRuntimeConfig("1.230.0", "read-path-commit"),
        )
        runtime.start()
        return (
            HyperliquidRuntimeReadAdapter(
                runtime=runtime,
                instruments=self.instruments(),
                freshness_policy=FreshnessPolicy(max_age=timedelta(seconds=5)),
            ),
            backend,
        )

    def test_runtime_reads_canonical_market_data_and_instrument(self) -> None:
        adapter, backend = self.adapter()

        candles = adapter.read_candles("BTC", "1m", now=NOW)
        trades = adapter.read_trades("BTC", now=NOW)
        book = adapter.read_order_book("BTC", now=NOW)
        ticker = adapter.read_ticker("BTC", now=NOW)
        instrument = adapter.get_instrument("BTC-USD-PERP")

        self.assertEqual(candles[0].data.instrument_id, "BTC-USD-PERP")
        self.assertEqual(candles[0].data.trade_count, 42)
        self.assertEqual(trades[0].data.trade_id, "77")
        self.assertEqual(book.data.bids[0].price, Decimal("64999.0"))
        self.assertEqual(ticker.data.bid, Decimal("64999.0"))
        self.assertEqual(instrument.quantity_step, Decimal("0.00001"))
        self.assertEqual(instrument.supported_order_types, (OrderType.LIMIT, OrderType.MARKET))
        self.assertEqual(instrument.minimum_quantity, Decimal("0.00001"))
        self.assertEqual(instrument.minimum_quantity_for_price(Decimal("65000.0")), Decimal("0.00016"))
        self.assertEqual(
            adapter.minimum_quantity_for_price("BTC-USD-PERP", Decimal("65000.0")),
            Decimal("0.00016"),
        )
        self.assertEqual([call[1] for call in backend.calls], ["candles", "trades", "order_book", "ticker"])

    def test_read_path_preserves_freshness_and_provenance(self) -> None:
        adapter, _ = self.adapter()

        ticker = adapter.read_ticker("BTC", now=NOW)

        self.assertEqual(ticker.freshness, FreshnessState.FRESH)
        self.assertEqual(ticker.provenance.source, "hyperliquid.info.fixture")
        self.assertEqual(ticker.provenance.transport_state, "local_fixture")
        self.assertEqual(ticker.provenance.mapping_revision, REVISION)

    def test_market_intent_requires_fresh_bbo_and_becomes_ioc_limit(self) -> None:
        adapter, _ = self.adapter()
        ticker = adapter.read_ticker("BTC", now=NOW)
        intent = OrderIntent(
            order_id="order-1",
            instrument_id="BTC-USD-PERP",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("0.01"),
            limit_price=None,
            time_in_force=TimeInForce.GTC,
            idempotency_key="idem-1",
        )

        mapped = adapter.market_as_ioc_limit(intent, ticker, SlippagePolicy(max_bps=Decimal("5")))

        self.assertEqual(mapped.order_type, OrderType.LIMIT)
        self.assertEqual(mapped.time_in_force, TimeInForce.IOC)
        self.assertEqual(mapped.limit_price, Decimal("65034.0"))

    def test_market_intent_rejects_stale_or_unknown_bbo(self) -> None:
        stale_adapter, _ = self.adapter(received_at=NOW - timedelta(seconds=6))
        stale_ticker = stale_adapter.read_ticker("BTC", now=NOW)
        intent = OrderIntent(
            order_id="order-2",
            instrument_id="BTC-USD-PERP",
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=Decimal("0.01"),
            limit_price=None,
            time_in_force=TimeInForce.GTC,
            idempotency_key="idem-2",
        )

        with self.assertRaises(MarketDataGateError) as raised:
            stale_adapter.market_as_ioc_limit(intent, stale_ticker, SlippagePolicy(max_bps=Decimal("5")))

        self.assertEqual(raised.exception.reason_code, "stale_market_data")

        unknown_adapter, _ = self.adapter(transport_state="reconnecting")
        unknown_ticker = unknown_adapter.read_ticker("BTC", now=NOW)
        with self.assertRaises(MarketDataGateError) as raised:
            unknown_adapter.market_as_ioc_limit(intent, unknown_ticker, SlippagePolicy(max_bps=Decimal("5")))

        self.assertEqual(raised.exception.reason_code, "unknown_market_data")

    def test_market_intent_rejects_missing_bbo_or_invalid_slippage(self) -> None:
        adapter, _ = self.adapter(bbo=[None, None])
        ticker = adapter.read_ticker("BTC", now=NOW)
        intent = OrderIntent(
            order_id="order-3",
            instrument_id="BTC-USD-PERP",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("0.01"),
            limit_price=None,
            time_in_force=TimeInForce.GTC,
            idempotency_key="idem-3",
        )

        with self.assertRaises(MarketDataGateError) as raised:
            adapter.market_as_ioc_limit(intent, ticker, SlippagePolicy(max_bps=Decimal("5")))
        self.assertEqual(raised.exception.reason_code, "bbo_required")

        with self.assertRaises(ValueError):
            SlippagePolicy(max_bps=Decimal("0"))

        with self.assertRaises(MarketDataGateError) as raised:
            adapter.get_instrument("ETH-USD-PERP")
        self.assertEqual(raised.exception.reason_code, "unsupported_instrument")

    def test_market_intent_rejects_bbo_for_a_different_instrument(self) -> None:
        adapter, _ = self.adapter()
        ticker = adapter.read_ticker("BTC", now=NOW)
        intent = OrderIntent(
            order_id="order-instrument-mismatch",
            instrument_id="ETH-USD-PERP",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("0.01"),
            limit_price=None,
            time_in_force=TimeInForce.GTC,
            idempotency_key="idem-instrument-mismatch",
        )

        with self.assertRaises(MarketDataGateError) as raised:
            adapter.market_as_ioc_limit(intent, ticker, SlippagePolicy(max_bps=Decimal("5")))

        self.assertEqual(raised.exception.reason_code, "instrument_mismatch")

    def test_missing_provenance_fails_closed(self) -> None:
        adapter, backend = self.adapter()
        backend.responses["ticker"].pop("provenance")

        with self.assertRaises(MarketDataGateError) as raised:
            adapter.read_ticker("BTC", now=NOW)

        self.assertEqual(raised.exception.reason_code, "provenance_required")

    def test_invalid_slippage_adjusted_price_fails_closed(self) -> None:
        adapter, _ = self.adapter()
        ticker = adapter.read_ticker("BTC", now=NOW)
        intent = OrderIntent(
            order_id="order-invalid-slippage",
            instrument_id="BTC-USD-PERP",
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=Decimal("0.01"),
            limit_price=None,
            time_in_force=TimeInForce.GTC,
            idempotency_key="idem-invalid-slippage",
        )

        with self.assertRaises(MarketDataGateError) as raised:
            adapter.market_as_ioc_limit(intent, ticker, SlippagePolicy(max_bps=Decimal("20000")))

        self.assertEqual(raised.exception.reason_code, "price_precision_invalid")

    def test_bbo_native_coin_mismatch_fails_before_canonicalization(self) -> None:
        adapter, _ = self.adapter(bbo_coin="ETH")

        with self.assertRaises(ValueError):
            adapter.read_ticker("BTC", now=NOW)

        adapter, backend = self.adapter()
        backend.responses["order_book"]["row"]["coin"] = "ETH"
        with self.assertRaises(ValueError):
            adapter.read_order_book("BTC", now=NOW)

        adapter, _ = self.adapter(bbo_coin=None)
        with self.assertRaises(ValueError):
            adapter.read_ticker("BTC", now=NOW)

    def test_market_intent_requires_ioc_capability(self) -> None:
        reduced_capabilities = CapabilityDescriptor(
            broker_id="hyperliquid",
            environment=BrokerEnvironment.PAPER,
            operations={"market_data": frozenset({"ticker"})},
            revision=REVISION,
        )
        backend = FakeReadBackend(
            {
                "ticker": {
                    "mid": "65000.0",
                    "bbo": {
                        "coin": "BTC",
                        "time": 1787313659000,
                        "bbo": [{"px": "64999.0"}, {"px": "65001.0"}],
                    },
                    "provenance": provenance(),
                }
            },
            capability_profile=reduced_capabilities,
        )
        runtime = NautilusHyperliquidRuntime(
            session=BrokerRuntimeSession(
                broker_id="hyperliquid",
                environment=BrokerEnvironment.PAPER,
                account=AccountReference(AccountScope.MASTER, "0xmaster"),
                signer=SignerReference.paper(),
                signer_provider=None,
                capabilities=reduced_capabilities,
                execution_scope="hypercore:default",
                lifecycle_id="read-path-capability-gap",
            ),
            backend=backend,
            config=NautilusRuntimeConfig("1.230.0", "read-path-commit"),
        )
        runtime.start()
        adapter = HyperliquidRuntimeReadAdapter(
            runtime=runtime,
            instruments=self.instruments(),
            freshness_policy=FreshnessPolicy(max_age=timedelta(seconds=5)),
        )
        ticker = adapter.read_ticker("BTC", now=NOW)
        intent = OrderIntent(
            order_id="order-capability-gap",
            instrument_id="BTC-USD-PERP",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("0.01"),
            limit_price=None,
            time_in_force=TimeInForce.GTC,
            idempotency_key="idem-capability-gap",
        )

        with self.assertRaises(MarketDataGateError) as raised:
            adapter.market_as_ioc_limit(intent, ticker, SlippagePolicy(max_bps=Decimal("5")))

        self.assertEqual(raised.exception.reason_code, "capability_gap")


if __name__ == "__main__":
    unittest.main()
