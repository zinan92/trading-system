"""Hyperliquid default-perps market-data normalizers."""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal

from ...market_data import (
    BookLevel,
    FreshnessPolicy,
    MarketDataEnvelope,
    OrderBookSnapshot,
    StandardKLine,
    Ticker,
    TradeTick,
)
from ...models import Provenance
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
        *,
        n_sig_figs: int | None = None,
        mantissa: int | None = None,
    ) -> OrderBookSnapshot:
        instrument = self._instruments.get_by_broker_symbol(broker_symbol)
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
            n_sig_figs=n_sig_figs,
            mantissa=mantissa,
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
