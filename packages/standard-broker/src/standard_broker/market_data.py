"""Canonical market observations and freshness rules."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Generic, TypeVar

from .models import Provenance


class FreshnessState(str, Enum):
    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FreshnessPolicy:
    """Classifies a received observation without mutating it."""

    max_age: timedelta

    def __post_init__(self) -> None:
        if self.max_age < timedelta(0):
            raise ValueError("max_age cannot be negative")

    def classify(
        self,
        received_at: datetime,
        now: datetime,
        *,
        transport_state: str = "connected",
    ) -> FreshnessState:
        if transport_state not in {"connected", "local_fixture", "external_testnet"}:
            return FreshnessState.UNKNOWN
        age = now - received_at
        if age < timedelta(0):
            return FreshnessState.UNKNOWN
        return FreshnessState.FRESH if age <= self.max_age else FreshnessState.STALE


@dataclass(frozen=True)
class StandardKLine:
    instrument_id: str
    interval: str
    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    trade_count: int | None
    provenance: Provenance


@dataclass(frozen=True)
class TradeTick:
    instrument_id: str
    side: str
    price: Decimal
    quantity: Decimal
    trade_id: str
    venue_timestamp: datetime
    provenance: Provenance


@dataclass(frozen=True)
class BookLevel:
    price: Decimal
    quantity: Decimal
    order_count: int


@dataclass(frozen=True)
class OrderBookSnapshot:
    instrument_id: str
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    depth: int
    venue_timestamp: datetime
    provenance: Provenance


@dataclass(frozen=True)
class Ticker:
    instrument_id: str
    mid: Decimal | None
    bid: Decimal | None
    ask: Decimal | None
    venue_timestamp: datetime | None
    provenance: Provenance


T = TypeVar("T")


@dataclass(frozen=True)
class MarketDataEnvelope(Generic[T]):
    """Canonical payload plus its freshness and provenance state."""

    data: T
    freshness: FreshnessState
    provenance: Provenance
