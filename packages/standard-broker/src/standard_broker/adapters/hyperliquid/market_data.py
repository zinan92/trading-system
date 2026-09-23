"""Hyperliquid default-perps market-data normalizers."""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from dataclasses import replace
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from ...market_data import (
    BookLevel,
    FreshnessPolicy,
    FreshnessState,
    MarketDataEnvelope,
    OrderBookSnapshot,
    StandardKLine,
    Ticker,
    TradeTick,
)
from ...errors import BrokerCapabilityError, MarketDataGateError
from ...models import Provenance
from ...orders import OrderIntent, OrderSide, OrderType, SlippagePolicy, TimeInForce
from .bridge import NautilusHyperliquidRuntime
from .instruments import HyperliquidInstrumentAdapter


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _timestamp(milliseconds: object) -> datetime:
    return datetime.fromtimestamp(int(milliseconds) / 1000, tz=UTC)


def _side(value: object) -> str:
    if value == "B":
        return "buy"
    if value == "A":
        return "sell"
    raise ValueError(f"unsupported Hyperliquid side: {value}")


def _require_matching_symbol(raw: Mapping[str, object], broker_symbol: str, label: str) -> None:
    raw_symbol = raw.get("coin", raw.get("s"))
    if raw_symbol is None:
        raise ValueError(f"Hyperliquid {label} requires broker symbol identity")
    if str(raw_symbol) != broker_symbol:
        raise ValueError(f"Hyperliquid {label} symbol does not match requested symbol")


class HyperliquidMarketDataAdapter:
    """Maps official Hyperliquid read payloads to canonical market observations."""

    name = "market_data"

    def __init__(self, instruments: HyperliquidInstrumentAdapter) -> None:
        self._instruments = instruments

    def map_candles(
        self,
        broker_symbol: str,
        interval: str,
        rows: Sequence[Mapping[str, object]],
        provenance: Provenance,
    ) -> tuple[StandardKLine, ...]:
        instrument = self._instruments.get_by_broker_symbol(broker_symbol)
        mapped: list[StandardKLine] = []
        for row in rows:
            _require_matching_symbol(row, broker_symbol, "candle")
            mapped.append(
                StandardKLine(
                    instrument_id=instrument.canonical_symbol,
                    interval=interval,
                    open_time=_timestamp(row["t"]),
                    close_time=_timestamp(row["T"]),
                    open=_decimal(row["o"]),
                    high=_decimal(row["h"]),
                    low=_decimal(row["l"]),
                    close=_decimal(row["c"]),
                    volume=_decimal(row["v"]),
                    trade_count=int(row["n"]) if row.get("n") is not None else None,
                    provenance=provenance,
                )
            )
        return tuple(mapped)

    def map_trades(
        self,
        broker_symbol: str,
        rows: Sequence[Mapping[str, object]],
        provenance: Provenance,
    ) -> tuple[TradeTick, ...]:
        instrument = self._instruments.get_by_broker_symbol(broker_symbol)
        mapped: list[TradeTick] = []
        for row in rows:
            _require_matching_symbol(row, broker_symbol, "trade")
            trade_id = row.get("tid", row.get("hash"))
            if trade_id is None:
                raise ValueError("Hyperliquid trade requires tid or hash")
            mapped.append(
                TradeTick(
                    instrument_id=instrument.canonical_symbol,
                    side=_side(row["side"]),
                    price=_decimal(row["px"]),
                    quantity=_decimal(row["sz"]),
                    trade_id=str(trade_id),
                    venue_timestamp=_timestamp(row["time"]),
                    provenance=provenance,
                )
            )
        return tuple(mapped)

    def map_l2_book(
        self,
        broker_symbol: str,
        row: Mapping[str, object],
        provenance: Provenance,
    ) -> OrderBookSnapshot:
        instrument = self._instruments.get_by_broker_symbol(broker_symbol)
        _require_matching_symbol(row, broker_symbol, "order book")
        levels = row.get("levels")
        if not isinstance(levels, list) or len(levels) != 2:
            raise ValueError("Hyperliquid l2Book must contain bid and ask levels")
        bids = tuple(self._map_levels(levels[0]))
        asks = tuple(self._map_levels(levels[1]))
        return OrderBookSnapshot(
            instrument_id=instrument.canonical_symbol,
            bids=bids,
            asks=asks,
            depth=max(len(bids), len(asks)),
            venue_timestamp=_timestamp(row["time"]),
            provenance=provenance,
        )

    @staticmethod
    def _map_levels(raw_levels: object) -> tuple[BookLevel, ...]:
        if not isinstance(raw_levels, list):
            raise TypeError("Hyperliquid book side must be a list")
        return tuple(
            BookLevel(
                price=_decimal(level["px"]),
                quantity=_decimal(level["sz"]),
                order_count=int(level["n"]),
            )
            for level in raw_levels
        )

    def map_ticker(
        self,
        broker_symbol: str,
        *,
        mid: object | None,
        bbo: Mapping[str, object] | None,
        provenance: Provenance,
    ) -> Ticker:
        instrument = self._instruments.get_by_broker_symbol(broker_symbol)
        bid: Decimal | None = None
        ask: Decimal | None = None
        venue_timestamp: datetime | None = None
        if bbo is not None:
            _require_matching_symbol(bbo, broker_symbol, "BBO")
            raw_bbo = bbo.get("bbo")
            if not isinstance(raw_bbo, list) or len(raw_bbo) != 2:
                raise ValueError("Hyperliquid BBO must contain bid and ask")
            bid = _decimal(raw_bbo[0]["px"]) if raw_bbo[0] else None
            ask = _decimal(raw_bbo[1]["px"]) if raw_bbo[1] else None
            if bbo.get("time") is not None:
                venue_timestamp = _timestamp(bbo["time"])
        return Ticker(
            instrument_id=instrument.canonical_symbol,
            mid=_decimal(mid) if mid is not None else None,
            bid=bid,
            ask=ask,
            venue_timestamp=venue_timestamp,
            provenance=provenance,
        )

    @staticmethod
    def envelope(
        data: StandardKLine | TradeTick | OrderBookSnapshot | Ticker,
        policy: FreshnessPolicy,
        *,
        now: datetime,
    ) -> MarketDataEnvelope[StandardKLine | TradeTick | OrderBookSnapshot | Ticker]:
        """Attach freshness to a canonical observation without changing its payload."""

        return MarketDataEnvelope(
            data=data,
            freshness=policy.classify(
                data.provenance.received_at,
                now,
                transport_state=data.provenance.transport_state,
            ),
            provenance=data.provenance,
        )


class HyperliquidRuntimeReadAdapter:
    """Connect the canonical read path to a Paper-safe Nautilus runtime."""

    name = "hyperliquid_runtime_read"

    def __init__(
        self,
        *,
        runtime: NautilusHyperliquidRuntime,
        instruments: HyperliquidInstrumentAdapter,
        freshness_policy: FreshnessPolicy,
    ) -> None:
        self._runtime = runtime
        self._instruments = instruments
        self._normalizer = HyperliquidMarketDataAdapter(instruments)
        self._freshness_policy = freshness_policy

    def get_instrument(self, canonical_symbol: str):
        """Return canonical instrument rules by canonical identity."""

        try:
            return self._instruments.get(canonical_symbol)
        except KeyError as exc:
            raise MarketDataGateError("unsupported_instrument", canonical_symbol) from exc

    def minimum_quantity_for_price(self, canonical_symbol: str, price: Decimal) -> Decimal:
        """Return the explicit canonical minimum quantity at one valid price."""

        return self.get_instrument(canonical_symbol).minimum_quantity_for_price(price)

    def _invoke(self, operation: str, request: Mapping[str, object]) -> Mapping[str, object]:
        raw = self._runtime._invoke_native("market_data", operation, request)
        if not isinstance(raw, Mapping):
            raise ValueError(f"Hyperliquid {operation} response must be a mapping")
        return raw

    def _provenance(self, response: Mapping[str, object]) -> Provenance:
        provided = response.get("provenance")
        if isinstance(provided, Provenance):
            return provided
        raise MarketDataGateError("provenance_required", "Broker market data must carry source provenance")

    def read_candles(
        self,
        broker_symbol: str,
        interval: str,
        *,
        now: datetime,
    ) -> tuple[MarketDataEnvelope[StandardKLine], ...]:
        response = self._invoke("candles", {"broker_symbol": broker_symbol, "interval": interval})
        rows = response.get("rows")
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
            raise ValueError("Hyperliquid candles response must contain rows")
        mapped = self._normalizer.map_candles(broker_symbol, interval, rows, self._provenance(response))
        return tuple(self._normalizer.envelope(item, self._freshness_policy, now=now) for item in mapped)

    def read_trades(
        self,
        broker_symbol: str,
        *,
        now: datetime,
    ) -> tuple[MarketDataEnvelope[TradeTick], ...]:
        response = self._invoke("trades", {"broker_symbol": broker_symbol})
        rows = response.get("rows")
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
            raise ValueError("Hyperliquid trades response must contain rows")
        mapped = self._normalizer.map_trades(broker_symbol, rows, self._provenance(response))
        return tuple(self._normalizer.envelope(item, self._freshness_policy, now=now) for item in mapped)

    def read_order_book(
        self,
        broker_symbol: str,
        *,
        now: datetime,
    ) -> MarketDataEnvelope[OrderBookSnapshot]:
        response = self._invoke("order_book", {"broker_symbol": broker_symbol})
        row = response.get("row")
        if not isinstance(row, Mapping):
            raise ValueError("Hyperliquid order book response must contain row")
        mapped = self._normalizer.map_l2_book(
            broker_symbol,
            row,
            self._provenance(response),
        )
        return self._normalizer.envelope(mapped, self._freshness_policy, now=now)

    def read_ticker(self, broker_symbol: str, *, now: datetime) -> MarketDataEnvelope[Ticker]:
        response = self._invoke("ticker", {"broker_symbol": broker_symbol})
        bbo = response.get("bbo")
        if bbo is not None and not isinstance(bbo, Mapping):
            raise ValueError("Hyperliquid ticker bbo must be a mapping")
        mapped = self._normalizer.map_ticker(
            broker_symbol,
            mid=response.get("mid"),
            bbo=bbo,
            provenance=self._provenance(response),
        )
        return self._normalizer.envelope(mapped, self._freshness_policy, now=now)

    def market_as_ioc_limit(
        self,
        intent: OrderIntent,
        ticker: MarketDataEnvelope[Ticker],
        policy: SlippagePolicy,
    ) -> OrderIntent:
        """Convert a canonical Market intent only when the BBO is fresh and complete."""

        if intent.order_type is not OrderType.MARKET:
            raise ValueError("market_as_ioc_limit requires a Market intent")
        if ticker.data.instrument_id != intent.instrument_id:
            raise MarketDataGateError("instrument_mismatch", "BBO instrument does not match the order intent")
        try:
            self._runtime.session.capabilities.require("order_execution", "submit_ioc_limit")
        except BrokerCapabilityError as exc:
            raise MarketDataGateError("capability_gap", str(exc)) from exc
        if ticker.freshness is FreshnessState.STALE:
            raise MarketDataGateError("stale_market_data", "fresh BBO is required for market-like execution")
        if ticker.freshness is not FreshnessState.FRESH:
            raise MarketDataGateError("unknown_market_data", "BBO freshness is not authoritative")

        reference = ticker.data.ask if intent.side is OrderSide.BUY else ticker.data.bid
        if reference is None:
            raise MarketDataGateError("bbo_required", "both market direction and fresh BBO are required")
        instrument = self.get_instrument(intent.instrument_id)
        bps = policy.max_bps / Decimal(10000)
        raw_limit = reference * (Decimal(1) + bps if intent.side is OrderSide.BUY else Decimal(1) - bps)
        rounding = ROUND_CEILING if intent.side is OrderSide.BUY else ROUND_FLOOR
        limit_price = None
        for decimal_places in range(instrument.price_rule.max_decimal_places, -1, -1):
            quantum = Decimal(1).scaleb(-decimal_places)
            candidate = raw_limit.quantize(quantum, rounding=rounding)
            if instrument.price_rule.is_valid(candidate):
                limit_price = candidate
                break
        if limit_price is None:
            raise MarketDataGateError("price_precision_invalid", "slippage-adjusted limit violates instrument precision")
        return replace(
            intent,
            order_type=OrderType.LIMIT,
            limit_price=limit_price,
            time_in_force=TimeInForce.IOC,
        )
