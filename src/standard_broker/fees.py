"""Canonical fee, fill, and funding facts."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

from .models import BrokerEnvironment, Provenance


class FeeKind(str, Enum):
    MAKER = "maker"
    TAKER = "taker"
    REBATE = "rebate"
    FUNDING = "funding"
    BUILDER = "builder"
    LIQUIDATION = "liquidation"
    OTHER = "other"


class FeeSource(str, Enum):
    ACTUAL_FILL = "actual_fill"
    FUNDING_EVENT = "funding_event"
    LEDGER = "ledger"
    SCHEDULE = "schedule"
    ESTIMATE = "estimate"


class FeeState(str, Enum):
    ACTUAL = "actual"
    ESTIMATED = "estimated"


@dataclass(frozen=True)
class FeeEvent:
    fee_id: str
    broker_id: str
    kind: FeeKind
    amount: Decimal
    currency: str
    occurred_at: datetime
    source: FeeSource
    state: FeeState
    provenance: Provenance
    instrument_id: str | None = None
    fill_id: str | None = None
    order_id: str | None = None
    liquidity: str | None = None
    environment: BrokerEnvironment = BrokerEnvironment.PAPER


@dataclass(frozen=True)
class FillFact:
    fill_id: str
    broker_id: str
    instrument_id: str
    side: str
    price: Decimal
    quantity: Decimal
    occurred_at: datetime
    closed_pnl: Decimal
    fee: FeeEvent
    provenance: Provenance
    builder_fee: FeeEvent | None = None
    environment: BrokerEnvironment = BrokerEnvironment.PAPER


@dataclass(frozen=True)
class FundingPayment:
    funding_id: str
    broker_id: str
    instrument_id: str
    amount: Decimal
    currency: str
    funding_rate: Decimal
    signed_quantity: Decimal
    occurred_at: datetime
    fee: FeeEvent
    provenance: Provenance
    environment: BrokerEnvironment = BrokerEnvironment.PAPER


@dataclass(frozen=True)
class FeeScheduleSnapshot:
    broker_id: str
    maker_rate: Decimal
    taker_rate: Decimal
    active_referral_discount: Decimal | None
    schedule_identity: str
    retrieved_at: datetime
    source: FeeSource
    state: FeeState
    provenance: Provenance
    environment: BrokerEnvironment = BrokerEnvironment.PAPER
