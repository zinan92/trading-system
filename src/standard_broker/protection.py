"""Canonical protective-order intent vocabulary."""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from .models import BrokerEnvironment, Provenance
from .orders import OrderSide


class ProtectionType(str, Enum):
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"


class ProtectionExecution(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class ProtectionQuantityPolicy(str, Enum):
    FIXED_SIZE = "fixed_size"
    POSITION_FOLLOWING = "position_following"


class TriggerReference(str, Enum):
    MARK = "mark"


class ProtectionLifecycleState(str, Enum):
    UNKNOWN = "unknown"
    SUBMITTED = "submitted"
    ACTIVE = "active"
    FROZEN = "frozen"
    CANCELED = "canceled"


@dataclass(frozen=True)
class ProtectionLifecycleStatus:
    """Canonical protection status; ACTIVE requires Broker/ledger evidence."""

    protection_id: str
    state: ProtectionLifecycleState
    reason: str | None
    attempts: int


@dataclass(frozen=True)
class ProtectionReceipt:
    protection_id: str
    parent_order_id: str
    operation: str
    accepted: bool
    broker_id: str
    environment: BrokerEnvironment
    provenance: Provenance
    account_address: str | None = None
    lifecycle_id: str | None = None
    release_sha: str | None = None

    def __post_init__(self) -> None:
        for name in ("protection_id", "parent_order_id", "operation", "broker_id"):
            value = getattr(self, name)
            if not value or value != value.strip():
                raise ValueError(f"{name} must be a non-empty trimmed string")
        if not isinstance(self.environment, BrokerEnvironment):
            raise TypeError("environment must be a BrokerEnvironment")
        if not isinstance(self.accepted, bool):
            raise TypeError("accepted must be a bool")
        if not isinstance(self.provenance, Provenance):
            raise TypeError("provenance must be a Provenance")


@dataclass(frozen=True)
class ProtectionRetryPlan:
    protection_id: str
    operation: str
    attempt: int
    retry_allowed: bool
    delay_seconds: float
    disposition: str


@dataclass(frozen=True)
class ProtectionLeg:
    protection_type: ProtectionType
    execution: ProtectionExecution
    trigger_price: Decimal
    limit_price: Decimal | None = None
    trigger_reference: TriggerReference = TriggerReference.MARK

    def __post_init__(self) -> None:
        if self.trigger_price <= 0:
            raise ValueError("protection trigger_price must be positive")
        if self.execution is ProtectionExecution.MARKET and self.limit_price is not None:
            raise ValueError("market protection cannot carry a limit_price")
        if self.execution is ProtectionExecution.LIMIT and self.limit_price is None:
            raise ValueError("limit protection requires a limit_price")
        if self.limit_price is not None and self.limit_price <= 0:
            raise ValueError("protection limit_price must be positive")


@dataclass(frozen=True)
class ProtectionGroup:
    protection_id: str
    parent_order_id: str
    instrument_id: str
    entry_side: OrderSide
    entry_price: Decimal
    quantity: Decimal
    quantity_policy: ProtectionQuantityPolicy
    take_profit: ProtectionLeg | None
    stop_loss: ProtectionLeg | None

    def __post_init__(self) -> None:
        for name in ("protection_id", "parent_order_id", "instrument_id"):
            value = getattr(self, name)
            if not value or value != value.strip():
                raise ValueError(f"{name} must be a non-empty trimmed string")
        if self.entry_price <= 0 or self.quantity <= 0:
            raise ValueError("protection entry_price and quantity must be positive")
        if self.take_profit is None and self.stop_loss is None:
            raise ValueError("protection group requires a take-profit or stop-loss leg")
