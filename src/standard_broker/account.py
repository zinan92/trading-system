"""Canonical account, position, and liquidation facts."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

from .instruments import MarginMode
from .models import Provenance


class PositionSide(str, Enum):
    LONG = "long"
    SHORT = "short"


@dataclass(frozen=True)
class PositionFact:
    instrument_id: str
    signed_quantity: Decimal
    side: PositionSide | None
    entry_price: Decimal | None
    leverage: Decimal | None
    margin_mode: MarginMode | None
    liquidation_price: Decimal | None
    margin_used: Decimal | None
    position_value: Decimal | None
    unrealized_pnl: Decimal | None
    provenance: Provenance


@dataclass(frozen=True)
class AccountSnapshot:
    broker_id: str
    account_address: str
    equity: Decimal | None
    balance: Decimal | None
    withdrawable: Decimal | None
    margin_used: Decimal | None
    exposure: Decimal | None
    realized_pnl: Decimal | None
    unrealized_pnl: Decimal | None
    positions: tuple[PositionFact, ...]
    provenance: Provenance


@dataclass(frozen=True)
class LiquidationFact:
    liquidation_id: str
    broker_id: str
    account_address: str
    liquidator: str | None
    notional: Decimal | None
    account_value: Decimal | None
    method: str | None
    liquidation_fee: Decimal | None
    occurred_at: datetime
    provenance: Provenance
