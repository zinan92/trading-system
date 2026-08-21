"""Paper-safe Hyperliquid order lifecycle and reconciliation mapping."""

import hashlib
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from ...models import Provenance
from ...orders import (
    InMemoryOrderTransport,
    OrderFill,
    OrderIntent,
    OrderReceipt,
    OrderSide,
    OrderState,
)


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _timestamp(value: object) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC)


class HyperliquidOrderAdapter:
    """Maps Hyperliquid order responses into one idempotent local lifecycle."""

    name = "order_execution"

    def __init__(self, *, transport: InMemoryOrderTransport) -> None:
        if getattr(transport, "local_only", False) is not True:
            raise ValueError("T04 Paper order adapter requires a local-only transport")
        self._transport = transport
        self._orders: dict[str, OrderReceipt] = {}
        self._by_key: dict[str, str] = {}
        self._by_client: dict[str, str] = {}
        self._pending_modifies: dict[str, str] = {}
        self._fills: dict[str, OrderFill] = {}
        self._instrument_ids: dict[str, str] = {}

    @staticmethod
    def _client_order_id(idempotency_key: str) -> str:
        return "0x" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _provenance() -> Provenance:
        return Provenance(
            source="hyperliquid.exchange.fixture",
            execution_scope="hypercore:default",
            transport_state="local_fixture",
            mapping_revision="order-lifecycle-v1",
        )

    def submit(self, intent: OrderIntent) -> OrderReceipt:
        existing_order_id = self._by_key.get(intent.idempotency_key)
        if existing_order_id is not None:
            return self._orders[existing_order_id]
        client_order_id = intent.client_order_id or self._client_order_id(intent.idempotency_key)
        receipt = OrderReceipt(
            order_id=intent.order_id,
            broker_id="hyperliquid",
            client_order_id=client_order_id,
            state=OrderState.SUBMITTING,
            original_quantity=intent.quantity,
            filled_quantity=Decimal(0),
            remaining_quantity=intent.quantity,
            broker_order_id=None,
            average_fill_price=None,
            reason=None,
            provenance=self._provenance(),
        )
        self._remember(intent, receipt)
        try:
            response = self._transport.submit(intent, client_order_id)
        except (TimeoutError, OSError) as exc:
            return self._replace(receipt, state=OrderState.UNKNOWN, reason=f"ambiguous_submit:{exc}")
        return self._apply_submit_response(receipt, response)

    def cancel(self, order_id: str) -> OrderReceipt:
        receipt = self._orders[order_id]
        try:
            response = self._transport.cancel(receipt)
        except (TimeoutError, OSError) as exc:
            return self._replace(receipt, state=OrderState.UNKNOWN, reason=f"ambiguous_cancel:{exc}")
        if isinstance(response, Mapping) and response.get("status") == "ok":
            return self._replace(receipt, state=OrderState.CANCEL_PENDING, reason=None)
        return self._replace(receipt, state=OrderState.UNKNOWN, reason="cancel_outcome_unknown")

    def modify(self, order_id: str, intent: OrderIntent) -> OrderReceipt:
        receipt = self._orders[order_id]
        try:
            response = self._transport.modify(receipt, intent)
        except (TimeoutError, OSError) as exc:
            return self._replace(receipt, state=OrderState.UNKNOWN, reason=f"ambiguous_modify:{exc}")
        if isinstance(response, Mapping) and response.get("status") == "ok":
            if receipt.broker_order_id is not None:
                self._pending_modifies[order_id] = receipt.broker_order_id
            return self._replace(receipt, state=OrderState.MODIFY_PENDING, reason=None)
        return self._replace(receipt, state=OrderState.UNKNOWN, reason="modify_outcome_unknown")

    def apply_fill(self, raw: Mapping[str, object]) -> OrderReceipt:
        receipt = self._find_receipt(raw)
        fill_id = str(raw.get("tid") or raw.get("hash") or "")
        if not fill_id:
            raise ValueError("Hyperliquid fill requires tid or hash")
        if fill_id in self._fills:
            return receipt

        broker_order_id = str(raw["oid"]) if raw.get("oid") is not None else receipt.broker_order_id
        if receipt.order_id in self._pending_modifies and broker_order_id != receipt.broker_order_id:
            receipt = self._promote(receipt, broker_order_id)
        quantity = _decimal(raw["sz"])
        price = _decimal(raw["px"])
        new_filled = receipt.filled_quantity + quantity
        if new_filled > receipt.original_quantity:
            raise ValueError("fill quantity exceeds original order quantity")
        average = (
            price
            if receipt.average_fill_price is None or receipt.filled_quantity == 0
            else (
                receipt.average_fill_price * receipt.filled_quantity + price * quantity
            )
            / new_filled
        )
        fill = OrderFill(
            fill_id=fill_id,
            order_id=receipt.order_id,
            broker_order_id=broker_order_id,
            client_order_id=receipt.client_order_id,
            instrument_id=self._instrument_ids[receipt.order_id],
            side=self._side(raw.get("side")),
            price=price,
            quantity=quantity,
            occurred_at=_timestamp(raw["time"]),
        )
        self._fills[fill_id] = fill
        state = OrderState.FILLED if new_filled == receipt.original_quantity else OrderState.PARTIALLY_FILLED
        return self._replace(
            receipt,
            broker_order_id=broker_order_id,
            state=state,
            filled_quantity=new_filled,
            remaining_quantity=receipt.original_quantity - new_filled,
            average_fill_price=average,
        )

    def apply_order_update(self, raw: Mapping[str, object]) -> OrderReceipt:
        receipt = self._find_receipt(raw)
        status = str(raw.get("status") or "").lower()
        broker_order_id = str(raw["oid"]) if raw.get("oid") is not None else receipt.broker_order_id
        pending_old = self._pending_modifies.get(receipt.order_id)
        if status in {"accepted", "open", "resting"}:
            if pending_old is not None and broker_order_id != pending_old:
                return self._promote(receipt, broker_order_id)
            return self._replace(receipt, state=OrderState.RESTING, broker_order_id=broker_order_id)
        if status in {"canceled", "cancelled"}:
            if receipt.broker_order_id is not None and broker_order_id != receipt.broker_order_id:
                return receipt
            if pending_old is not None and broker_order_id == pending_old:
                return receipt
            return self._replace(receipt, state=OrderState.CANCELED, broker_order_id=broker_order_id)
        if status in {"partially_filled", "partial"}:
            return self._replace(receipt, state=OrderState.PARTIALLY_FILLED, broker_order_id=broker_order_id)
        if status == "filled":
            return self._replace(receipt, state=OrderState.FILLED, broker_order_id=broker_order_id)
        return receipt

    def reconcile(self, raw: Mapping[str, object]) -> OrderReceipt:
        outer = raw.get("order") if isinstance(raw.get("order"), Mapping) else raw
        if not isinstance(outer, Mapping):
            raise TypeError("Hyperliquid order status must be a mapping")
        details = outer.get("order") if isinstance(outer.get("order"), Mapping) else outer
        if not isinstance(details, Mapping):
            raise TypeError("Hyperliquid order details must be a mapping")
        event = dict(details)
        event["status"] = outer.get("status", event.get("status"))
        return self.apply_order_update(event)

    def get(self, order_id: str) -> OrderReceipt:
        return self._orders[order_id]

    @property
    def fills(self) -> Mapping[str, OrderFill]:
        return dict(self._fills)

    @staticmethod
    def _side(value: object) -> OrderSide:
        if value == "B":
            return OrderSide.BUY
        if value == "A":
            return OrderSide.SELL
        raise ValueError(f"unsupported Hyperliquid side: {value}")

    def _find_receipt(self, raw: Mapping[str, object]) -> OrderReceipt:
        client_order_id = raw.get("cloid") or raw.get("client_order_id")
        if client_order_id is not None and str(client_order_id) in self._by_client:
            return self._orders[self._by_client[str(client_order_id)]]
        broker_order_id = str(raw["oid"]) if raw.get("oid") is not None else None
        for receipt in self._orders.values():
            if receipt.broker_order_id == broker_order_id:
                return receipt
        raise KeyError("unable to resolve Hyperliquid lifecycle event to a canonical order")

    def _apply_submit_response(self, receipt: OrderReceipt, response: object) -> OrderReceipt:
        if not isinstance(response, Mapping):
            return self._replace(receipt, state=OrderState.UNKNOWN, reason="invalid_submit_response")
        statuses = (
            response.get("response", {})
            .get("data", {})
            .get("statuses", [])
            if isinstance(response.get("response"), Mapping)
            else []
        )
        if not isinstance(statuses, list) or not statuses:
            return self._replace(receipt, state=OrderState.UNKNOWN, reason="missing_submit_status")
        status = statuses[0]
        if isinstance(status, str):
            state = {
                "waitingForFill": OrderState.WAITING_FOR_FILL,
                "waitingForTrigger": OrderState.WAITING_FOR_TRIGGER,
            }.get(status, OrderState.UNKNOWN)
            return self._replace(receipt, state=state, reason=None if state is not OrderState.UNKNOWN else status)
        if not isinstance(status, Mapping):
            return self._replace(receipt, state=OrderState.UNKNOWN, reason="invalid_submit_status")
        if isinstance(status.get("resting"), Mapping):
            resting = status["resting"]
            return self._replace(
                receipt,
                state=OrderState.RESTING,
                broker_order_id=str(resting["oid"]),
            )
        if isinstance(status.get("filled"), Mapping):
            filled = status["filled"]
            quantity = _decimal(filled["totalSz"])
            return self._replace(
                receipt,
                state=OrderState.FILLED,
                broker_order_id=str(filled["oid"]),
                filled_quantity=quantity,
                remaining_quantity=receipt.original_quantity - quantity,
                average_fill_price=_decimal(filled["avgPx"]),
            )
        if status.get("error") is not None:
            return self._replace(
                receipt,
                state=OrderState.REJECTED,
                reason=str(status["error"]),
            )
        return self._replace(receipt, state=OrderState.UNKNOWN, reason="unrecognized_submit_status")

    def _remember(self, intent: OrderIntent, receipt: OrderReceipt) -> None:
        self._orders[intent.order_id] = receipt
        self._by_key[intent.idempotency_key] = intent.order_id
        self._by_client[receipt.client_order_id] = intent.order_id
        self._instrument_ids[intent.order_id] = intent.instrument_id

    def _replace(self, receipt: OrderReceipt, **changes: object) -> OrderReceipt:
        updated = replace(receipt, **changes)
        self._orders[receipt.order_id] = updated
        self._by_client[updated.client_order_id] = updated.order_id
        return updated

    def _promote(self, receipt: OrderReceipt, broker_order_id: str | None) -> OrderReceipt:
        self._pending_modifies.pop(receipt.order_id, None)
        return self._replace(
            receipt,
            state=OrderState.RESTING,
            broker_order_id=broker_order_id,
            reason=None,
        )
