"""Canonical order intent, lifecycle, fill, and local transport vocabulary."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

from .models import Provenance


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    LIMIT = "limit"
    MARKET = "market"


class TimeInForce(str, Enum):
    GTC = "gtc"
    IOC = "ioc"
    ALO = "alo"


class OrderState(str, Enum):
    SUBMITTING = "submitting"
    RESTING = "resting"
    WAITING_FOR_FILL = "waiting_for_fill"
    WAITING_FOR_TRIGGER = "waiting_for_trigger"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCEL_PENDING = "cancel_pending"
    CANCELED = "canceled"
    MODIFY_PENDING = "modify_pending"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class OrderIntent:
    order_id: str
    instrument_id: str
    side: OrderSide
    order_type: OrderType
    quantity: Decimal
    limit_price: Decimal | None
    time_in_force: TimeInForce
    idempotency_key: str
    reduce_only: bool = False
    client_order_id: str | None = None
    trigger_price: Decimal | None = None
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("order_id", "instrument_id", "idempotency_key"):
            value = getattr(self, name)
            if not value or value != value.strip():
                raise ValueError(f"{name} must be a non-empty trimmed string")
        if not isinstance(self.side, OrderSide):
            raise TypeError("side must be an OrderSide")
        if not isinstance(self.order_type, OrderType):
            raise TypeError("order_type must be an OrderType")
        if not isinstance(self.time_in_force, TimeInForce):
            raise TypeError("time_in_force must be a TimeInForce")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise ValueError("limit orders require limit_price")
        if self.limit_price is not None and self.limit_price <= 0:
            raise ValueError("limit_price must be positive")


@dataclass(frozen=True)
class OrderFill:
    fill_id: str
    order_id: str
    broker_order_id: str | None
    client_order_id: str
    instrument_id: str
    side: OrderSide
    price: Decimal
    quantity: Decimal
    occurred_at: datetime


@dataclass(frozen=True)
class OrderReceipt:
    order_id: str
    broker_id: str
    client_order_id: str
    state: OrderState
    original_quantity: Decimal
    filled_quantity: Decimal
    remaining_quantity: Decimal
    broker_order_id: str | None
    average_fill_price: Decimal | None
    reason: str | None
    provenance: Provenance


@dataclass(frozen=True)
class OrderTransportCall:
    operation: str
    order_id: str
    client_order_id: str


class InMemoryOrderTransport:
    """A local-only transport for lifecycle tests and Paper composition."""

    local_only = True

    def __init__(
        self,
        *,
        submit_response: object,
        cancel_response: object | None = None,
        modify_response: object | None = None,
    ) -> None:
        self.submit_response = submit_response
        self.cancel_response = cancel_response if cancel_response is not None else {"status": "ok"}
        self.modify_response = modify_response if modify_response is not None else {"status": "ok"}
        self.submit_calls: list[OrderTransportCall] = []
        self.cancel_calls: list[OrderTransportCall] = []
        self.modify_calls: list[OrderTransportCall] = []

    @staticmethod
    def _resolve(response: object, call: OrderTransportCall) -> object:
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            return response(call)
        return response

    def submit(self, intent: OrderIntent, client_order_id: str) -> object:
        call = OrderTransportCall("submit", intent.order_id, client_order_id)
        self.submit_calls.append(call)
        return self._resolve(self.submit_response, call)

    def cancel(self, receipt: OrderReceipt) -> object:
        call = OrderTransportCall("cancel", receipt.order_id, receipt.client_order_id)
        self.cancel_calls.append(call)
        return self._resolve(self.cancel_response, call)

    def modify(self, receipt: OrderReceipt, intent: OrderIntent) -> object:
        call = OrderTransportCall("modify", receipt.order_id, receipt.client_order_id)
        self.modify_calls.append(call)
        return self._resolve(self.modify_response, call)
