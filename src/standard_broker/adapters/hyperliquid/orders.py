"""Local-fixture Hyperliquid order lifecycle for Paper and approved Testnet."""

import hashlib
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from ...errors import OrderIdempotencyError
from ...models import BrokerEnvironment, Provenance
from ...orders import (
    InMemoryOrderTransport,
    OrderFill,
    OrderIntent,
    OrderReceipt,
    OrderSide,
    OrderState,
    OrderType,
)
from .bridge import NautilusHyperliquidRuntime
from .instruments import HyperliquidInstrumentAdapter
from ...runtime_facts import RuntimeFactLedger


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _timestamp(value: object) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC)


class HyperliquidOrderAdapter:
    """Maps Hyperliquid order responses into one idempotent local lifecycle."""

    name = "order_execution"

    def __init__(
        self,
        *,
        transport: InMemoryOrderTransport,
        environment: BrokerEnvironment = BrokerEnvironment.PAPER,
    ) -> None:
        if getattr(transport, "local_only", False) is not True:
            raise ValueError("Paper/Testnet order adapter requires a local-only transport")
        if environment not in {BrokerEnvironment.PAPER, BrokerEnvironment.TESTNET}:
            raise ValueError("fixture order adapter supports Paper and approved Testnet only")
        self._transport = transport
        self._environment = environment
        self._orders: dict[str, OrderReceipt] = {}
        self._by_key: dict[str, str] = {}
        self._intent_fingerprints: dict[str, tuple[object, ...]] = {}
        self._by_client: dict[str, str] = {}
        self._pending_modifies: dict[str, tuple[str, str]] = {}
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
            if self._intent_fingerprints[intent.idempotency_key] != self._intent_fingerprint(intent):
                raise OrderIdempotencyError(
                    f"idempotency key {intent.idempotency_key!r} was reused for a different intent"
                )
            return self._orders[existing_order_id]
        client_order_id = intent.client_order_id or self._client_order_id(intent.idempotency_key)
        receipt = OrderReceipt(
            order_id=intent.order_id,
            broker_id="hyperliquid",
            environment=self._environment,
            client_order_id=client_order_id,
            state=OrderState.SUBMITTING,
            original_quantity=intent.quantity,
            filled_quantity=Decimal(0),
            remaining_quantity=intent.quantity,
            broker_order_id=None,
            average_fill_price=None,
            reason=None,
            provenance=self._provenance(),
            updated_at=datetime.now(UTC),
            client_order_lineage=(client_order_id,),
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
        replacement_client_order_id = self._client_order_id(
            f"replace:{receipt.client_order_id}:{intent.idempotency_key}"
        )
        try:
            response = self._transport.modify(receipt, intent, replacement_client_order_id)
        except (TimeoutError, OSError) as exc:
            return self._replace(receipt, state=OrderState.UNKNOWN, reason=f"ambiguous_modify:{exc}")
        if isinstance(response, Mapping) and response.get("status") == "ok":
            if receipt.broker_order_id is not None:
                self._pending_modifies[order_id] = (
                    receipt.broker_order_id,
                    replacement_client_order_id,
                )
            return self._replace(
                receipt,
                client_order_id=replacement_client_order_id,
                state=OrderState.MODIFY_PENDING,
                reason=None,
            )
        return self._replace(receipt, state=OrderState.UNKNOWN, reason="modify_outcome_unknown")

    def query(self, order_id: str) -> OrderReceipt:
        """Query the Broker lifecycle and reconcile the returned canonical order state."""

        receipt = self._orders[order_id]
        try:
            response = self._transport.query(receipt)
        except (TimeoutError, OSError) as exc:
            return self._replace(receipt, state=OrderState.UNKNOWN, reason=f"ambiguous_query:{exc}")
        return self.reconcile(response)

    def open_orders(self, instrument_id: str | None = None) -> tuple[OrderReceipt, ...]:
        """Query open orders and reconcile every returned canonical order observation."""

        response = self._transport.open_orders(instrument_id)
        rows = response.get("orders", []) if isinstance(response, Mapping) else response
        if not isinstance(rows, list):
            raise ValueError("Hyperliquid open-orders response must contain a list")
        return tuple(self.reconcile(row) for row in rows if isinstance(row, Mapping))

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
            broker_updated_at=self._event_timestamp(raw),
        )

    def apply_order_update(self, raw: Mapping[str, object]) -> OrderReceipt:
        receipt = self._find_receipt(raw)
        status = str(raw.get("status") or "").lower()
        broker_order_id = str(raw["oid"]) if raw.get("oid") is not None else receipt.broker_order_id
        pending = self._pending_modifies.get(receipt.order_id)
        pending_old = pending[0] if pending is not None else None
        if status in {"accepted", "open", "resting"}:
            if pending_old is not None and broker_order_id != pending_old:
                return self._promote(receipt, broker_order_id)
            return self._replace(
                receipt,
                state=OrderState.RESTING,
                broker_order_id=broker_order_id,
                broker_updated_at=self._event_timestamp(raw),
            )
        if status in {"canceled", "cancelled"}:
            if receipt.broker_order_id is not None and broker_order_id != receipt.broker_order_id:
                return receipt
            if pending_old is not None and broker_order_id == pending_old:
                return receipt
            return self._replace(
                receipt,
                state=OrderState.CANCELED,
                broker_order_id=broker_order_id,
                broker_updated_at=self._event_timestamp(raw),
            )
        if status in {"partially_filled", "partial"}:
            if not self._has_fill_identity(raw):
                return self._replace(
                    receipt,
                    state=OrderState.UNKNOWN,
                    broker_order_id=broker_order_id,
                    reason="status_without_fill_identity",
                )
            return self.apply_fill(raw)
        if status == "filled":
            if not self._has_fill_identity(raw):
                return self._replace(
                    receipt,
                    state=OrderState.UNKNOWN,
                    broker_order_id=broker_order_id,
                    reason="status_without_fill_identity",
                )
            return self.apply_fill(raw)
        return receipt

    def reconcile(self, raw: Mapping[str, object]) -> OrderReceipt:
        return self.apply_order_update(self.normalize_reconcile_event(raw))

    def normalize_reconcile_event(self, raw: Mapping[str, object]) -> dict[str, object]:
        """Flatten a Broker reconciliation envelope into one lifecycle event."""

        outer = raw.get("order") if isinstance(raw.get("order"), Mapping) else raw
        if not isinstance(outer, Mapping):
            raise TypeError("Hyperliquid order status must be a mapping")
        details = outer.get("order") if isinstance(outer.get("order"), Mapping) else outer
        if not isinstance(details, Mapping):
            raise TypeError("Hyperliquid order details must be a mapping")
        event = dict(details)
        event["status"] = outer.get("status", event.get("status"))
        event["timestamp"] = outer.get(
            "statusTimestamp",
            outer.get("timestamp", details.get("statusTimestamp", details.get("timestamp"))),
        )
        return event

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
        broker_order_id = str(raw["oid"]) if raw.get("oid") is not None else None
        if client_order_id is not None and str(client_order_id) in self._by_client:
            receipt = self._orders[self._by_client[str(client_order_id)]]
            if (
                broker_order_id is not None
                and receipt.broker_order_id not in {None, broker_order_id}
                and broker_order_id not in receipt.broker_order_lineage
            ):
                if receipt.state is not OrderState.MODIFY_PENDING:
                    raise ValueError("Broker order identity conflicts with client order identity")
            return receipt
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
            updated = self._replace(
                receipt,
                state=OrderState.FILLED,
                broker_order_id=str(filled["oid"]),
                filled_quantity=quantity,
                remaining_quantity=receipt.original_quantity - quantity,
                average_fill_price=_decimal(filled["avgPx"]),
                broker_updated_at=self._event_timestamp(filled),
            )
            fill_id = filled.get("tid") or filled.get("hash")
            if fill_id is None or filled.get("side") is None or filled.get("time") is None:
                return self._replace(
                    updated,
                    state=OrderState.UNKNOWN,
                    reason="filled_without_fill_identity",
                )
            self._fills[str(fill_id)] = OrderFill(
                fill_id=str(fill_id),
                order_id=updated.order_id,
                broker_order_id=updated.broker_order_id,
                client_order_id=updated.client_order_id,
                instrument_id=self._instrument_ids[updated.order_id],
                side=self._side(filled["side"]),
                price=_decimal(filled["avgPx"]),
                quantity=quantity,
                occurred_at=_timestamp(filled["time"]),
            )
            return updated
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
        self._intent_fingerprints[intent.idempotency_key] = self._intent_fingerprint(intent)
        self._by_client[receipt.client_order_id] = intent.order_id
        self._instrument_ids[intent.order_id] = intent.instrument_id

    def _replace(self, receipt: OrderReceipt, **changes: object) -> OrderReceipt:
        next_changes = dict(changes)
        new_broker_order_id = next_changes.get("broker_order_id", receipt.broker_order_id)
        if new_broker_order_id is not None:
            broker_order_id = str(new_broker_order_id)
            if not receipt.broker_order_lineage or receipt.broker_order_lineage[-1] != broker_order_id:
                next_changes["broker_order_lineage"] = receipt.broker_order_lineage + (broker_order_id,)
        new_client_order_id = str(next_changes.get("client_order_id", receipt.client_order_id))
        if not receipt.client_order_lineage or receipt.client_order_lineage[-1] != new_client_order_id:
            next_changes["client_order_lineage"] = receipt.client_order_lineage + (new_client_order_id,)
        next_changes.setdefault("updated_at", datetime.now(UTC))
        updated = replace(receipt, **next_changes)
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

    @staticmethod
    def _intent_fingerprint(intent: OrderIntent) -> tuple[object, ...]:
        return (
            intent.order_id,
            intent.instrument_id,
            intent.side,
            intent.order_type,
            intent.quantity,
            intent.limit_price,
            intent.time_in_force,
            intent.reduce_only,
            intent.close_position,
            intent.client_order_id,
            intent.trigger_price,
            intent.expires_at,
        )

    @staticmethod
    def _has_fill_identity(raw: Mapping[str, object]) -> bool:
        return all(raw.get(key) is not None for key in ("tid", "side", "px", "sz", "time"))

    @staticmethod
    def _event_timestamp(raw: Mapping[str, object]) -> datetime | None:
        for key in ("statusTimestamp", "timestamp", "time"):
            value = raw.get(key)
            if value is not None:
                return _timestamp(value)
        return None


class _RuntimeOrderTransport:
    """Adapter-internal transport that keeps native order payloads off canonical receipts."""

    local_only = True

    def __init__(self, *, runtime: NautilusHyperliquidRuntime, instruments: HyperliquidInstrumentAdapter) -> None:
        self._runtime = runtime
        self._instruments = instruments

    def submit(self, intent: OrderIntent, client_order_id: str) -> object:
        return self._runtime._invoke_native(
            "order_execution",
            "submit",
            self._native_order(intent, client_order_id),
        )

    def cancel(self, receipt: OrderReceipt) -> object:
        if receipt.broker_order_id is None:
            raise ValueError("cancel requires a Broker order identity")
        return self._runtime._invoke_native(
            "order_execution",
            "cancel",
            {"oid": receipt.broker_order_id, "cloid": receipt.client_order_id},
        )

    def modify(
        self,
        receipt: OrderReceipt,
        intent: OrderIntent,
        replacement_client_order_id: str,
    ) -> object:
        if receipt.broker_order_id is None:
            raise ValueError("replace requires a Broker order identity")
        request = self._native_order(intent, replacement_client_order_id)
        request["oid"] = receipt.broker_order_id
        return self._runtime._invoke_native("order_execution", "replace", request)

    def query(self, receipt: OrderReceipt) -> object:
        request: dict[str, object] = {"cloid": receipt.client_order_id}
        if receipt.broker_order_id is not None:
            request["oid"] = receipt.broker_order_id
        return self._runtime._invoke_native(
            "order_execution",
            "query",
            request,
        )

    def open_orders(self, instrument_id: str | None = None) -> object:
        request: dict[str, object] = {}
        if instrument_id is not None:
            request["instrument_id"] = instrument_id
        return self._runtime._invoke_native("order_execution", "open_orders", request)

    def _native_order(self, intent: OrderIntent, client_order_id: str) -> dict[str, object]:
        if intent.order_type is not OrderType.LIMIT or intent.limit_price is None:
            raise ValueError("Hyperliquid runtime order transport requires a canonical limit intent")
        instrument = self._instruments.get(intent.instrument_id)
        if not instrument.supports_order_type(OrderType.LIMIT):
            raise ValueError("instrument does not support canonical limit orders")
        if not instrument.price_rule.is_valid(intent.limit_price):
            raise ValueError("limit price does not satisfy instrument precision")
        return {
            "coin": instrument.broker_symbol,
            "side": "B" if intent.side is OrderSide.BUY else "A",
            "sz": str(intent.quantity),
            "limitPx": str(intent.limit_price),
            "tif": intent.time_in_force.value.upper(),
            "reduceOnly": intent.reduce_only,
            "cloid": client_order_id,
        }


class HyperliquidRuntimeOrderAdapter:
    """Canonical order lifecycle backed by a local-fixture Nautilus runtime."""

    name = "hyperliquid_runtime_order_execution"

    def __init__(
        self,
        *,
        runtime: NautilusHyperliquidRuntime,
        instruments: HyperliquidInstrumentAdapter,
        ledger: RuntimeFactLedger,
    ) -> None:
        self._runtime = runtime
        self._lifecycle = HyperliquidOrderAdapter(
            transport=_RuntimeOrderTransport(runtime=runtime, instruments=instruments),
            environment=runtime.session.environment,
        )
        self._instruments = instruments
        self._ledger = ledger
        self._ledger.bind_session(
            broker_id=runtime.session.broker_id,
            environment=runtime.session.environment.value,
            account_address=runtime.session.account.address,
        )

    @property
    def local_only(self) -> bool:
        """Expose whether the injected runtime backend is local-only."""

        return bool(getattr(self._runtime._backend, "local_only", False))

    def submit(self, intent: OrderIntent) -> OrderReceipt:
        self._validate_intent(intent)
        return self._bind_receipt(self._lifecycle.submit(intent))

    def cancel(self, order_id: str) -> OrderReceipt:
        return self._bind_receipt(self._lifecycle.cancel(order_id))

    def modify(self, order_id: str, intent: OrderIntent) -> OrderReceipt:
        self._validate_intent(intent)
        return self._bind_receipt(self._lifecycle.modify(order_id, intent))

    def query(self, order_id: str) -> OrderReceipt:
        return self._bind_receipt(self._lifecycle.query(order_id))

    def open_orders(self, instrument_id: str | None = None) -> tuple[OrderReceipt, ...]:
        return tuple(self._bind_receipt(item) for item in self._lifecycle.open_orders(instrument_id))

    def apply_fill(self, raw: Mapping[str, object]) -> OrderReceipt:
        result = self._bind_receipt(self._lifecycle.apply_fill(raw))
        self._sync_order_fills(raw)
        return result

    def apply_order_update(self, raw: Mapping[str, object]) -> OrderReceipt:
        result = self._bind_receipt(self._lifecycle.apply_order_update(raw))
        self._sync_order_fills(raw)
        return result

    def reconcile(self, raw: Mapping[str, object]) -> OrderReceipt:
        normalized = self._lifecycle.normalize_reconcile_event(raw)
        result = self._bind_receipt(self._lifecycle.apply_order_update(normalized))
        self._sync_order_fills(normalized)
        return result

    def get(self, order_id: str) -> OrderReceipt:
        return self._bind_receipt(self._lifecycle.get(order_id))

    def _bind_receipt(self, receipt: OrderReceipt) -> OrderReceipt:
        return replace(
            receipt,
            account_address=self._runtime.session.account.address,
            lifecycle_id=self._runtime.session.lifecycle_id,
        )

    @property
    def fills(self) -> Mapping[str, OrderFill]:
        return {
            fill_id: self._bind_fill(fill)
            for fill_id, fill in self._lifecycle.fills.items()
        }

    def _bind_fill(self, fill: OrderFill) -> OrderFill:
        return replace(
            fill,
            environment=self._runtime.session.environment,
            account_address=self._runtime.session.account.address,
            lifecycle_id=self._runtime.session.lifecycle_id,
            release_sha=self._runtime._config.expected_release_sha,
        )

    def _validate_intent(self, intent: OrderIntent) -> None:
        instrument = self._instruments.get(intent.instrument_id)
        if not instrument.supports_order_type(intent.order_type):
            raise ValueError(f"unsupported order type for {intent.instrument_id}: {intent.order_type.value}")
        if intent.quantity < (instrument.minimum_quantity or instrument.quantity_step):
            raise ValueError("order quantity is below instrument minimum quantity")
        if intent.quantity % instrument.quantity_step != 0:
            raise ValueError("order quantity does not satisfy instrument quantity step")
        if intent.order_type is OrderType.LIMIT and intent.limit_price is not None:
            if not instrument.price_rule.is_valid(intent.limit_price):
                raise ValueError("order price does not satisfy instrument precision")
            if intent.quantity * intent.limit_price < instrument.minimum_notional:
                raise ValueError("order notional is below instrument minimum notional")

    def _sync_order_fills(self, raw: Mapping[str, object]) -> None:
        namespace = ":".join(
            (
                self._runtime.session.broker_id,
                self._runtime.session.environment.value,
                self._runtime.session.account.address,
            )
        ) + ":"
        for fill_id, raw_fill in self._lifecycle.fills.items():
            fill = self._bind_fill(raw_fill)
            if str(raw.get("tid") or raw.get("hash") or "") == fill_id:
                self._ledger.record_order_fill(fill, raw)
            else:
                self._ledger.order_fills[namespace + fill_id] = fill
