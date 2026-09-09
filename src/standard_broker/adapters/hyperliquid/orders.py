"""Local-fixture Hyperliquid order lifecycle for Paper and approved Testnet."""

from contextlib import contextmanager
from copy import deepcopy
import hashlib
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol, runtime_checkable

from ...errors import BrokerCapabilityError, OrderIdempotencyError, RuntimeBoundaryError
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
from ...runtime import BrokerRuntimeSession
from ...runtime_facts import RuntimeFactLedger
from .bridge import NautilusHyperliquidRuntime
from .instruments import HyperliquidInstrumentAdapter


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _timestamp(value: object) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC)


_TERMINAL_STATES = frozenset({OrderState.FILLED, OrderState.CANCELED, OrderState.REJECTED})


class HyperliquidOrderAdapter:
    """Maps Hyperliquid order responses into one idempotent local lifecycle."""

    name = "order_execution"

    def __init__(
        self,
        *,
        transport: InMemoryOrderTransport,
        environment: BrokerEnvironment = BrokerEnvironment.PAPER,
        execution_scope: str = "hypercore:default",
        mapping_revision: str = "order-lifecycle-v1",
        transport_state: str | None = None,
    ) -> None:
        if environment not in {BrokerEnvironment.PAPER, BrokerEnvironment.TESTNET}:
            raise ValueError("fixture order adapter supports Paper and approved Testnet only")
        local_only = getattr(transport, "local_only", False) is True
        external_network = getattr(transport, "external_network", False) is True
        if environment is BrokerEnvironment.PAPER and not local_only:
            raise ValueError("Paper order adapter requires a local-only transport")
        if environment is BrokerEnvironment.TESTNET and not (local_only or external_network):
            raise ValueError("Testnet order adapter requires a local fixture or external Testnet transport")
        self._transport = transport
        self._environment = environment
        self._execution_scope = execution_scope
        self._mapping_revision = mapping_revision
        derived_transport_state = "local_fixture" if local_only else "external_testnet"
        if transport_state is not None and transport_state != derived_transport_state:
            raise ValueError("transport_state does not match transport boundary")
        self._transport_state = transport_state or derived_transport_state
        self._source = (
            "hyperliquid.exchange.fixture"
            if local_only
            else "nautilus-hyperliquid.testnet"
        )
        self._orders: dict[str, OrderReceipt] = {}
        self._by_key: dict[str, str] = {}
        self._intent_fingerprints: dict[str, tuple[object, ...]] = {}
        self._by_client: dict[str, str] = {}
        self._pending_modifies: dict[str, tuple[str, str]] = {}
        self._fills: dict[str, OrderFill] = {}
        self._fill_raws: dict[str, Mapping[str, object]] = {}
        self._fill_aliases: dict[str, str] = {}
        self._hash_fill_aliases: dict[str, set[str]] = {}
        self._instrument_ids: dict[str, str] = {}
        self._sides: dict[str, OrderSide] = {}
        self._intents: dict[str, OrderIntent] = {}

    @contextmanager
    def transaction(self):
        """Rollback canonical lifecycle mutations if one snapshot event fails."""

        state_fields = (
            "_orders",
            "_by_key",
            "_intent_fingerprints",
            "_by_client",
            "_pending_modifies",
            "_fills",
            "_fill_raws",
            "_fill_aliases",
            "_hash_fill_aliases",
            "_instrument_ids",
            "_sides",
            "_intents",
        )
        checkpoint = {name: deepcopy(getattr(self, name)) for name in state_fields}
        try:
            yield
        except Exception:
            for name, value in checkpoint.items():
                setattr(self, name, value)
            raise

    @staticmethod
    def _client_order_id(idempotency_key: str) -> str:
        return "0x" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:32]

    def _provenance(self) -> Provenance:
        return Provenance(
            source=self._source,
            execution_scope=self._execution_scope,
            transport_state=self._transport_state,
            mapping_revision=self._mapping_revision,
        )

    def submit(self, intent: OrderIntent) -> OrderReceipt:
        existing_by_order = self._orders.get(intent.order_id)
        if existing_by_order is not None:
            if self._intent_fingerprint(intent) != self._intent_fingerprint(self._intents[intent.order_id]):
                raise OrderIdempotencyError("canonical order_id was reused for a different intent")
            return existing_by_order
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

    def recover(
        self,
        intent: OrderIntent,
        *,
        broker_order_id: str,
        state: str | OrderState,
    ) -> OrderReceipt:
        """Restore one persisted order identity without invoking transport."""

        broker_id = str(broker_order_id or "").strip()
        if not broker_id:
            raise RuntimeBoundaryError(
                "broker_order_identity_required",
                "persisted order recovery requires a Broker order identity",
            )
        try:
            order_state = state if isinstance(state, OrderState) else OrderState(str(state).lower())
        except ValueError as exc:
            raise RuntimeBoundaryError(
                "recovery_state_invalid",
                "persisted order recovery state is not canonical",
            ) from exc
        client_order_id = intent.client_order_id or self._client_order_id(intent.idempotency_key)
        receipt = OrderReceipt(
            order_id=intent.order_id,
            broker_id="hyperliquid",
            environment=self._environment,
            client_order_id=client_order_id,
            state=order_state,
            original_quantity=intent.quantity,
            filled_quantity=intent.quantity if order_state is OrderState.FILLED else Decimal("0"),
            remaining_quantity=Decimal("0") if order_state is OrderState.FILLED else intent.quantity,
            broker_order_id=broker_id,
            average_fill_price=None,
            reason="recovered_persisted_identity",
            provenance=self._provenance(),
            updated_at=datetime.now(UTC),
            broker_order_lineage=(broker_id,),
            client_order_lineage=(client_order_id,),
        )
        self._remember(intent, receipt)
        return receipt

    def recover_client_order(
        self,
        intent: OrderIntent,
        *,
        client_order_id: str,
        state: str | OrderState,
    ) -> OrderReceipt:
        """Restore one intent when only the persisted client identity exists."""

        client_id = str(client_order_id or "").strip()
        if not client_id:
            raise RuntimeBoundaryError(
                "client_order_identity_required",
                "client-order recovery requires a persisted client identity",
            )
        try:
            order_state = state if isinstance(state, OrderState) else OrderState(str(state).lower())
        except ValueError as exc:
            raise RuntimeBoundaryError(
                "recovery_state_invalid",
                "client recovery state is not canonical",
            ) from exc
        receipt = OrderReceipt(
            order_id=intent.order_id,
            broker_id="hyperliquid",
            environment=self._environment,
            client_order_id=client_id,
            state=order_state,
            original_quantity=intent.quantity,
            filled_quantity=intent.quantity if order_state is OrderState.FILLED else Decimal("0"),
            remaining_quantity=Decimal("0") if order_state is OrderState.FILLED else intent.quantity,
            broker_order_id=None,
            average_fill_price=None,
            reason="recovered_persisted_client_identity",
            provenance=self._provenance(),
            updated_at=datetime.now(UTC),
            client_order_lineage=(client_id,),
        )
        self._remember(intent, receipt)
        return receipt

    def cancel(self, order_id: str) -> OrderReceipt:
        order_id = self.resolve_order_id(order_id)
        receipt = self._orders[order_id]
        if receipt.state is OrderState.UNKNOWN:
            raise RuntimeBoundaryError(
                "reconciliation_required",
                "an ambiguous order outcome must be queried or reconciled before cancel retry",
            )
        try:
            response = self._transport.cancel(receipt)
        except (TimeoutError, OSError) as exc:
            return self._replace(receipt, state=OrderState.UNKNOWN, reason=f"ambiguous_cancel:{exc}")
        if isinstance(response, Mapping) and response.get("status") == "ok":
            return self._replace(receipt, state=OrderState.CANCEL_PENDING, reason=None)
        return self._replace(receipt, state=OrderState.UNKNOWN, reason="cancel_outcome_unknown")

    def resolve_order_id(self, reference: str) -> str:
        """Resolve canonical, client, or broker order identity to one local order id."""

        reference = str(reference or "").strip()
        if reference in self._orders:
            return reference
        canonical = self._by_client.get(reference)
        if canonical is not None:
            return canonical
        for receipt in self._orders.values():
            if reference in receipt.broker_order_lineage:
                return receipt.order_id
        raise KeyError(f"unknown Hyperliquid order identity: {reference}")

    def instrument_id_for_order(self, reference: str) -> str:
        """Return the canonical instrument for one resolved order identity."""

        return self._instrument_ids[self.resolve_order_id(reference)]

    def modify(self, order_id: str, intent: OrderIntent) -> OrderReceipt:
        order_id = self.resolve_order_id(order_id)
        receipt = self._orders[order_id]
        if intent.order_id != order_id:
            raise ValueError("replacement intent order_id must match the canonical order")
        if receipt.state is OrderState.UNKNOWN:
            raise RuntimeBoundaryError(
                "reconciliation_required",
                "an ambiguous order outcome must be queried or reconciled before replace retry",
            )
        if receipt.state in {OrderState.FILLED, OrderState.CANCELED, OrderState.REJECTED, OrderState.UNKNOWN}:
            raise ValueError("terminal orders cannot be replaced")
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

        order_id = self.resolve_order_id(order_id)
        receipt = self._orders[order_id]
        try:
            response = self._transport.query(receipt)
        except (TimeoutError, OSError) as exc:
            return self._replace(receipt, state=OrderState.UNKNOWN, reason=f"ambiguous_query:{exc}")
        return self.reconcile(response)

    def open_orders(self, instrument_id: str | None = None) -> tuple[OrderReceipt, ...]:
        """Query open orders without assigning unregistered Broker orders locally."""

        response = self._transport.open_orders(instrument_id)
        rows = response.get("orders", []) if isinstance(response, Mapping) else response
        if not isinstance(rows, list):
            raise ValueError("Hyperliquid open-orders response must contain a list")
        receipts: list[OrderReceipt] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            event = self.normalize_reconcile_event(row)
            if self._registered_receipt_for_open_order(event) is None:
                receipts.append(self._external_open_order(event))
            else:
                receipts.append(self.apply_order_update(event))
        return tuple(receipts)

    def _registered_receipt_for_open_order(
        self,
        raw: Mapping[str, object],
    ) -> OrderReceipt | None:
        client_order_id = raw.get("cloid") or raw.get("client_order_id")
        if client_order_id is not None:
            order_id = self._by_client.get(str(client_order_id))
            return self._orders.get(order_id) if order_id is not None else None
        broker_order_id = str(raw["oid"]) if raw.get("oid") is not None else None
        return self._receipt_for_broker_id(broker_order_id)

    def _external_open_order(self, raw: Mapping[str, object]) -> OrderReceipt:
        broker_order_id = str(raw["oid"]) if raw.get("oid") is not None else ""
        if not broker_order_id:
            raise ValueError("unregistered Hyperliquid open order requires a Broker order identity")
        client_order_id = str(raw.get("cloid") or raw.get("client_order_id") or "")
        try:
            remaining_quantity = _decimal(raw["sz"])
            original_quantity = _decimal(raw.get("origSz", remaining_quantity))
        except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("unregistered Hyperliquid open order quantity is invalid") from exc
        if (
            not remaining_quantity.is_finite()
            or not original_quantity.is_finite()
            or remaining_quantity < 0
            or original_quantity <= 0
            or remaining_quantity > original_quantity
        ):
            raise ValueError("unregistered Hyperliquid open order quantity is invalid")
        broker_updated_at = self._event_timestamp(raw)
        return OrderReceipt(
            order_id=f"external:{broker_order_id}",
            broker_id="hyperliquid",
            environment=self._environment,
            client_order_id=client_order_id,
            state=OrderState.UNKNOWN,
            original_quantity=original_quantity,
            filled_quantity=original_quantity - remaining_quantity,
            remaining_quantity=remaining_quantity,
            broker_order_id=broker_order_id,
            average_fill_price=None,
            reason="unregistered_exchange_order",
            provenance=self._provenance(),
            updated_at=broker_updated_at or datetime.now(UTC),
            broker_updated_at=broker_updated_at,
            broker_order_lineage=(broker_order_id,),
            client_order_lineage=(client_order_id,) if client_order_id else (),
        )

    @staticmethod
    def _canonical_trade_id(raw_tid: object) -> str | None:
        if raw_tid is None or not str(raw_tid).strip():
            return None
        text = str(raw_tid).strip()
        return str(int(raw_tid)) if type(raw_tid) is int or text.isdecimal() else text

    def _prepare_fill_identity(
        self,
        raw: Mapping[str, object],
    ) -> tuple[str, set[str], str | None, bool, set[str]]:
        canonical_tid = self._canonical_trade_id(raw.get("tid"))
        raw_hash = raw.get("hash")
        hash_key = str(raw_hash).strip() if raw_hash is not None and str(raw_hash).strip() else None
        if canonical_tid is not None:
            fill_id = canonical_tid
            identity_keys = {fill_id, f"tid:{canonical_tid}"}
            existing = {self._fill_aliases[key] for key in identity_keys if key in self._fill_aliases}
            if existing:
                return fill_id, identity_keys, hash_key, True, existing
            if hash_key and hash_key in self._hash_fill_aliases.get(hash_key, set()):
                owners = self._hash_fill_aliases[hash_key]
                if self._transport_state != "local_fixture" or len(owners) != 1:
                    raise ValueError("fill_identity_ambiguous")
                fill_id = next(iter(owners))
                identity_keys.update({fill_id, f"hash:{hash_key}"})
                existing.add(fill_id)
            return fill_id, identity_keys, hash_key, True, existing
        if hash_key is None:
            raise ValueError("Hyperliquid fill requires tid or hash")
        owners = self._hash_fill_aliases.get(hash_key, set())
        if len(owners) > 1:
            raise ValueError("fill_identity_ambiguous")
        fill_id = next(iter(owners), hash_key)
        identity_keys = {fill_id, f"hash:{hash_key}"}
        existing = {self._fill_aliases[key] for key in identity_keys if key in self._fill_aliases}
        existing.update(owners)
        return fill_id, identity_keys, hash_key, False, existing

    def _register_hash_alias(self, hash_key: str | None, fill_id: str) -> None:
        if hash_key is None:
            return
        owners = self._hash_fill_aliases.setdefault(hash_key, set())
        owners.add(fill_id)
        alias_key = f"hash:{hash_key}"
        if len(owners) == 1:
            self._fill_aliases[alias_key] = fill_id
        else:
            self._fill_aliases.pop(alias_key, None)

    def apply_fill(self, raw: Mapping[str, object]) -> OrderReceipt:
        self._validate_basic_fill(raw)
        receipt = self._find_receipt(raw)
        expected_side = self._sides.get(receipt.order_id)
        if expected_side is not None and raw.get("side") != ("B" if expected_side is OrderSide.BUY else "A"):
            raise ValueError("fill side conflicts with the canonical order side")
        fill_id, identity_keys, hash_key, has_trade_id, existing_fill_ids = self._prepare_fill_identity(raw)
        if not has_trade_id and hash_key:
            existing_fill_ids.update(self._hash_fill_aliases.get(hash_key, set()))
        if len(existing_fill_ids) > 1:
            raise ValueError("fill aliases resolve to different canonical fills")
        quantity = _decimal(raw["sz"])
        price = _decimal(raw["px"])
        existing_fill_id = next(iter(existing_fill_ids), None)
        if existing_fill_id is not None:
            existing_fill = self._fills[existing_fill_id]
            if (
                existing_fill.order_id != receipt.order_id
                or existing_fill.price != price
                or existing_fill.quantity != quantity
                or existing_fill.side is not self._side(raw.get("side"))
            ):
                if has_trade_id and self._transport_state == "local_fixture":
                    canonical_tid = self._canonical_trade_id(raw.get("tid"))
                    if canonical_tid is None:
                        raise ValueError("fill_identity_ambiguous")
                    fill_id = canonical_tid
                    identity_keys = {fill_id, f"tid:{canonical_tid}"}
                    existing_fill_id = None
                else:
                    raise ValueError("fill identity was reused with different canonical facts")
            if existing_fill_id is None:
                existing_fill_ids = set()
            else:
                for identity_key in identity_keys:
                    self._fill_aliases[identity_key] = existing_fill_id
                self._register_hash_alias(hash_key, existing_fill_id)
                return receipt

        broker_order_id = str(raw["oid"]) if raw.get("oid") is not None else receipt.broker_order_id
        pending_modify = receipt.order_id in self._pending_modifies
        if broker_order_id != receipt.broker_order_id:
            if pending_modify:
                receipt = self._promote(receipt, broker_order_id)
            else:
                raise ValueError("stale Broker order identity cannot mutate the active order")
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
        self._fill_raws[fill_id] = dict(raw)
        for identity_key in identity_keys:
            self._fill_aliases[identity_key] = fill_id
        self._register_hash_alias(hash_key, fill_id)
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

    def validate_fill(
        self,
        raw: Mapping[str, object],
        *,
        expected_broker_symbol: str | None = None,
    ) -> None:
        receipt = self._find_receipt(raw)
        instrument_id = self._instrument_ids[receipt.order_id]
        expected_side = self._sides[receipt.order_id]
        if raw.get("side") != ("B" if expected_side is OrderSide.BUY else "A"):
            raise ValueError("fill side conflicts with the canonical order side")
        coin = raw.get("coin")
        if coin is None:
            raise ValueError("fill coin is required")
        # Runtime adapter validates the provider symbol before delegating to this lifecycle.
        if not isinstance(coin, str) or not coin.strip():
            raise ValueError("fill coin must be a non-empty string")
        if expected_broker_symbol is not None and coin != expected_broker_symbol:
            raise ValueError("fill coin conflicts with the canonical instrument")
        if instrument_id not in self._instrument_ids.values():
            raise ValueError("fill instrument is not bound to the canonical order")

    def instrument_id_for_fill(self, raw: Mapping[str, object]) -> str:
        """Return the canonical instrument identity for one resolved fill."""

        receipt = self._find_receipt(raw)
        return self._instrument_ids[receipt.order_id]

    def validate_receipt(self, candidate: OrderReceipt) -> None:
        """Verify that a snapshot receipt is the current lifecycle-owner receipt."""

        current = self._orders.get(candidate.order_id)
        if current != candidate:
            raise ValueError("snapshot receipt is not the current canonical lifecycle receipt")

    def apply_order_update(self, raw: Mapping[str, object]) -> OrderReceipt:
        receipt = self._find_receipt(raw)
        status = str(raw.get("status") or "").lower()
        event_timestamp = self._event_timestamp(raw)
        if receipt.state in _TERMINAL_STATES:
            return receipt
        if (
            event_timestamp is not None
            and receipt.broker_updated_at is not None
            and event_timestamp < receipt.broker_updated_at
        ):
            return receipt
        broker_order_id = str(raw["oid"]) if raw.get("oid") is not None else receipt.broker_order_id
        pending = self._pending_modifies.get(receipt.order_id)
        pending_old = pending[0] if pending is not None else None
        if status == "waitingforfill":
            return self._replace(receipt, state=OrderState.WAITING_FOR_FILL, broker_order_id=broker_order_id)
        if status == "waitingfortrigger":
            return self._replace(receipt, state=OrderState.WAITING_FOR_TRIGGER, broker_order_id=broker_order_id)
        if status in {"accepted", "open", "resting"}:
            if pending_old is not None and broker_order_id != pending_old:
                return self._promote(receipt, broker_order_id)
            if receipt.broker_order_id is not None and broker_order_id != receipt.broker_order_id:
                return receipt
            return self._replace(
                receipt,
                state=OrderState.RESTING,
                broker_order_id=broker_order_id,
                broker_updated_at=self._event_timestamp(raw),
            )
        if status in {"canceled", "cancelled"}:
            if pending_old is not None and broker_order_id != pending_old:
                promoted = self._promote(receipt, broker_order_id)
                return self._replace(
                    promoted,
                    state=OrderState.CANCELED,
                    broker_updated_at=self._event_timestamp(raw),
                )
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
        if status == "rejected":
            if pending_old is not None and broker_order_id != pending_old:
                promoted = self._promote(receipt, broker_order_id)
                return self._replace(
                    promoted,
                    state=OrderState.REJECTED,
                    broker_updated_at=self._event_timestamp(raw),
                )
            if receipt.broker_order_id is not None and broker_order_id != receipt.broker_order_id:
                return receipt
            return self._replace(
                receipt,
                state=OrderState.REJECTED,
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
        changes: dict[str, object] = {
            "state": OrderState.UNKNOWN,
            "broker_order_id": broker_order_id,
            "reason": f"unrecognized_order_status:{status or 'missing'}",
        }
        if event_timestamp is not None:
            changes["broker_updated_at"] = event_timestamp
        return self._replace(receipt, **changes)

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
        return self._orders[self.resolve_order_id(order_id)]

    @property
    def fills(self) -> Mapping[str, OrderFill]:
        return dict(self._fills)

    @property
    def fill_raws(self) -> Mapping[str, Mapping[str, object]]:
        """Return the provider facts retained for each canonical fill."""

        return {fill_id: dict(raw) for fill_id, raw in self._fill_raws.items()}

    @staticmethod
    def _side(value: object) -> OrderSide:
        if value == "B":
            return OrderSide.BUY
        if value == "A":
            return OrderSide.SELL
        raise ValueError(f"unsupported Hyperliquid side: {value}")

    @staticmethod
    def _validate_basic_fill(raw: Mapping[str, object]) -> None:
        if raw.get("coin") is not None and (not isinstance(raw.get("coin"), str) or not str(raw["coin"]).strip()):
            raise ValueError("fill coin is required")
        if raw.get("side") not in {"B", "A"}:
            raise ValueError("fill side is invalid")
        try:
            price = _decimal(raw["px"])
            quantity = _decimal(raw["sz"])
        except (KeyError, InvalidOperation, TypeError, ValueError) as error:
            raise ValueError("fill price or size is invalid") from error
        if not price.is_finite() or price <= 0 or not quantity.is_finite() or quantity <= 0:
            raise ValueError("fill price and size must be positive finite values")

    def _find_receipt(self, raw: Mapping[str, object]) -> OrderReceipt:
        client_order_id = raw.get("cloid") or raw.get("client_order_id")
        broker_order_id = str(raw["oid"]) if raw.get("oid") is not None else None
        if client_order_id is not None and str(client_order_id) in self._by_client:
            receipt = self._orders[self._by_client[str(client_order_id)]]
            broker_owner = self._receipt_for_broker_id(broker_order_id)
            if broker_owner is not None and broker_owner.order_id != receipt.order_id:
                raise ValueError("Broker order identity belongs to a different canonical order")
            if (
                broker_order_id is not None
                and receipt.broker_order_id not in {None, broker_order_id}
                and broker_order_id not in receipt.broker_order_lineage
            ):
                if receipt.state is not OrderState.MODIFY_PENDING:
                    raise ValueError("Broker order identity conflicts with client order identity")
            return receipt
        if client_order_id is not None:
            raise ValueError("unknown client order identity cannot fall back to Broker order identity")
        for receipt in self._orders.values():
            if broker_order_id in receipt.broker_order_lineage:
                return receipt
        raise KeyError("unable to resolve Hyperliquid lifecycle event to a canonical order")

    def _receipt_for_broker_id(self, broker_order_id: str | None) -> OrderReceipt | None:
        if broker_order_id is None:
            return None
        for receipt in self._orders.values():
            if broker_order_id in receipt.broker_order_lineage:
                return receipt
        return None

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
            filled_side = filled.get("side")
            expected_side = self._sides.get(receipt.order_id)
            if filled_side is None:
                try:
                    partial_quantity = _decimal(filled["totalSz"])
                    partial_average = _decimal(filled["avgPx"])
                except (KeyError, InvalidOperation, TypeError, ValueError):
                    return self._replace(receipt, state=OrderState.UNKNOWN, reason="filled_without_fill_identity")
                if (
                    not partial_quantity.is_finite()
                    or partial_quantity < 0
                    or partial_quantity > receipt.original_quantity
                    or not partial_average.is_finite()
                    or partial_average <= 0
                ):
                    return self._replace(receipt, state=OrderState.UNKNOWN, reason="invalid_filled_response")
                return self._replace(
                    receipt,
                    state=OrderState.UNKNOWN,
                    broker_order_id=str(filled.get("oid")) if filled.get("oid") is not None else receipt.broker_order_id,
                    filled_quantity=partial_quantity,
                    remaining_quantity=receipt.original_quantity - partial_quantity,
                    average_fill_price=partial_average,
                    reason="filled_without_fill_identity",
                )
            if filled_side not in {"B", "A"} or (
                expected_side is not None
                and filled_side != ("B" if expected_side is OrderSide.BUY else "A")
            ):
                return self._replace(receipt, state=OrderState.UNKNOWN, reason="invalid_filled_side")
            try:
                quantity = _decimal(filled["totalSz"])
                average_fill_price = _decimal(filled["avgPx"])
                broker_order_id = str(filled["oid"])
            except (KeyError, InvalidOperation, TypeError, ValueError):
                return self._replace(receipt, state=OrderState.UNKNOWN, reason="invalid_filled_response")
            if (
                not quantity.is_finite()
                or quantity != receipt.original_quantity
                or not average_fill_price.is_finite()
                or average_fill_price <= 0
                or not broker_order_id
            ):
                return self._replace(receipt, state=OrderState.UNKNOWN, reason="invalid_filled_response")
            updated = self._replace(
                receipt,
                state=OrderState.FILLED,
                broker_order_id=broker_order_id,
                filled_quantity=quantity,
                remaining_quantity=receipt.original_quantity - quantity,
                average_fill_price=average_fill_price,
                broker_updated_at=self._event_timestamp(filled),
            )
            if (
                (filled.get("tid") is None and filled.get("hash") is None)
                or filled.get("side") is None
                or filled.get("time") is None
            ):
                return self._replace(
                    updated,
                    state=OrderState.UNKNOWN,
                    reason="filled_without_fill_identity",
                )
            fill_id, identity_keys, hash_key, has_trade_id, existing_fill_ids = self._prepare_fill_identity(filled)
            if not has_trade_id and hash_key:
                existing_fill_ids.update(self._hash_fill_aliases.get(hash_key, set()))
            if len(existing_fill_ids) > 1:
                raise ValueError("fill aliases resolve to different canonical fills")
            existing_fill_id = next(iter(existing_fill_ids), None)
            if existing_fill_id is not None:
                existing_fill = self._fills[existing_fill_id]
                if (
                    existing_fill.order_id != updated.order_id
                    or existing_fill.price != _decimal(filled["avgPx"])
                    or existing_fill.quantity != quantity
                    or existing_fill.side is not self._side(filled["side"])
                ):
                    if has_trade_id and self._transport_state == "local_fixture":
                        canonical_tid = self._canonical_trade_id(filled.get("tid"))
                        if canonical_tid is None:
                            raise ValueError("fill_identity_ambiguous")
                        fill_id = canonical_tid
                        identity_keys = {fill_id, f"tid:{canonical_tid}"}
                        existing_fill_id = None
                    else:
                        raise ValueError("fill identity was reused with different canonical facts")
                if existing_fill_id is not None:
                    for identity_key in identity_keys:
                        self._fill_aliases[identity_key] = existing_fill_id
                    self._register_hash_alias(hash_key, existing_fill_id)
                    return updated
            self._fills[fill_id] = OrderFill(
                fill_id=fill_id,
                order_id=updated.order_id,
                broker_order_id=updated.broker_order_id,
                client_order_id=updated.client_order_id,
                instrument_id=self._instrument_ids[updated.order_id],
                side=self._side(filled["side"]),
                price=_decimal(filled["avgPx"]),
                quantity=quantity,
                occurred_at=_timestamp(filled["time"]),
            )
            raw_fill = dict(filled)
            raw_fill.setdefault("px", filled.get("avgPx"))
            raw_fill.setdefault("sz", filled.get("totalSz"))
            self._fill_raws[fill_id] = raw_fill
            for identity_key in identity_keys:
                self._fill_aliases[identity_key] = fill_id
            self._register_hash_alias(hash_key, fill_id)
            return updated
        if status.get("error") is not None:
            return self._replace(
                receipt,
                state=OrderState.REJECTED,
                reason=str(status["error"]),
            )
        return self._replace(receipt, state=OrderState.UNKNOWN, reason="unrecognized_submit_status")

    def _remember(self, intent: OrderIntent, receipt: OrderReceipt) -> None:
        existing_order = self._orders.get(intent.order_id)
        if existing_order is not None:
            if self._intent_fingerprint(intent) != self._intent_fingerprint(self._intents[intent.order_id]):
                raise OrderIdempotencyError("canonical order_id was reused for a different intent")
            return
        existing_client_owner = self._by_client.get(receipt.client_order_id)
        if existing_client_owner is not None and existing_client_owner != intent.order_id:
            raise OrderIdempotencyError(
                f"client order ID {receipt.client_order_id!r} was reused by another canonical order"
            )
        self._orders[intent.order_id] = receipt
        self._by_key[intent.idempotency_key] = intent.order_id
        self._intent_fingerprints[intent.idempotency_key] = self._intent_fingerprint(intent)
        self._by_client[receipt.client_order_id] = intent.order_id
        self._instrument_ids[intent.order_id] = intent.instrument_id
        remember_instrument = getattr(self._transport, "remember_instrument", None)
        if callable(remember_instrument):
            remember_instrument(intent.order_id, intent.instrument_id)
        self._sides[intent.order_id] = intent.side
        self._intents[intent.order_id] = intent

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
        if (
            broker_order_id is None
            or broker_order_id == receipt.broker_order_id
            or broker_order_id in receipt.broker_order_lineage
        ):
            raise ValueError("replacement promotion requires a new Broker order identity")
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

    def __init__(self, *, runtime: NautilusHyperliquidRuntime, instruments: HyperliquidInstrumentAdapter) -> None:
        self._runtime = runtime
        self._instruments = instruments
        self._native_cloids: dict[str, str] = {}
        self._instrument_ids: dict[str, str] = {}

    @property
    def local_only(self) -> bool:
        return bool(getattr(self._runtime._backend, "local_only", False))

    @property
    def external_network(self) -> bool:
        return bool(getattr(self._runtime._backend, "external_network", False))

    def submit(self, intent: OrderIntent, client_order_id: str) -> object:
        self._native_cloids[intent.order_id] = client_order_id
        self._instrument_ids[intent.order_id] = intent.instrument_id
        return self._runtime._invoke_native(
            "order_execution",
            "submit",
            self._native_order(intent, client_order_id),
        )

    def remember_instrument(self, order_id: str, instrument_id: str) -> None:
        """Keep recovered canonical orders usable before a submit call occurs."""

        self._instrument_ids[order_id] = instrument_id

    def cancel(self, receipt: OrderReceipt) -> object:
        if receipt.broker_order_id is None:
            raise ValueError("cancel requires a Broker order identity")
        return self._runtime._invoke_native(
            "order_execution",
            "cancel",
            {
                "instrument_id": self._instrument_ids.get(receipt.order_id),
                "oid": receipt.broker_order_id,
                "cloid": self._native_client_order_id(receipt),
            },
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
        request["instrument_id"] = self._instrument_ids.get(receipt.order_id, intent.instrument_id)
        request["oid"] = receipt.broker_order_id
        request["cloid"] = self._native_client_order_id(receipt)
        request["replacement_cloid"] = replacement_client_order_id
        result = self._runtime._invoke_native("order_execution", "replace", request)
        if isinstance(result, Mapping) and result.get("native_cloid"):
            self._native_cloids[receipt.order_id] = str(result["native_cloid"])
        return result

    def query(self, receipt: OrderReceipt) -> object:
        request: dict[str, object] = {
            "instrument_id": self._instrument_ids.get(receipt.order_id),
            "cloid": self._native_client_order_id(receipt),
        }
        if receipt.broker_order_id is not None:
            request["oid"] = receipt.broker_order_id
        return self._runtime._invoke_native(
            "order_execution",
            "query",
            request,
        )

    def _native_client_order_id(self, receipt: OrderReceipt) -> str:
        """Hyperliquid modify keeps the original CLOID across both OIDs."""

        return self._native_cloids.get(
            receipt.order_id,
            receipt.client_order_lineage[0] if receipt.client_order_lineage else receipt.client_order_id,
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
            execution_scope=runtime.session.execution_scope,
            mapping_revision=runtime.session.capabilities.revision,
            transport_state=(
                "local_fixture"
                if self._lifecycle_transport_is_local(runtime)
                else "external_testnet"
            ),
        )
        self._instruments = instruments
        self._ledger = ledger
        self._ledger.bind_session(
            broker_id=runtime.session.broker_id,
            environment=runtime.session.environment.value,
            account_address=runtime.session.account.address,
        )

    @staticmethod
    def _lifecycle_transport_is_local(runtime: NautilusHyperliquidRuntime) -> bool:
        return bool(getattr(runtime._backend, "local_only", False))

    @contextmanager
    def transaction(self):
        """Rollback lifecycle and staged ledger mutations for one local-fixture snapshot."""

        dictionary_fields = (
            "fills",
            "funding",
            "order_fills",
            "order_fill_raw",
            "accounts",
            "liquidations",
        )
        ledger_checkpoint = {
            name: deepcopy(getattr(self._ledger, name))
            for name in dictionary_fields
        }
        ledger_checkpoint["session_key"] = self._ledger.session_key
        # Preserve the original bound FeePort callback; deepcopying it would
        # bind the callback to a cloned owner after rollback.
        ledger_checkpoint["fill_enricher"] = self._ledger.fill_enricher
        try:
            with self._lifecycle.transaction():
                yield
        except Exception:
            for name in dictionary_fields:
                current = getattr(self._ledger, name)
                current.clear()
                current.update(ledger_checkpoint[name])
            self._ledger.session_key = ledger_checkpoint["session_key"]
            self._ledger.fill_enricher = ledger_checkpoint["fill_enricher"]
            raise

    @property
    def local_only(self) -> bool:
        """Expose whether the injected runtime backend is local-only."""

        return bool(getattr(self._runtime._backend, "local_only", False))

    @property
    def runtime_session(self) -> BrokerRuntimeSession:
        """Expose the immutable session used by the reconciliation boundary."""

        return self._runtime.session

    @property
    def transport_state(self) -> str:
        """Expose the runtime transport identity to external compositions."""

        return self._runtime.transport_state

    def resolve_order_id(self, reference: str) -> str:
        """Resolve one canonical, client, or broker order identity."""

        return self._lifecycle.resolve_order_id(reference)

    def submit(self, intent: OrderIntent) -> OrderReceipt:
        self._validate_intent(intent)
        receipt = self._bind_receipt(self._lifecycle.submit(intent))
        self._sync_inline_fills()
        return receipt

    def recover(
        self,
        intent: OrderIntent,
        *,
        broker_order_id: str,
        state: str | OrderState,
    ) -> OrderReceipt:
        self._validate_intent(intent)
        return self._bind_receipt(
            self._lifecycle.recover(
                intent,
                broker_order_id=broker_order_id,
                state=state,
            )
        )

    def recover_client_order(
        self,
        intent: OrderIntent,
        *,
        client_order_id: str,
        state: str | OrderState,
    ) -> OrderReceipt:
        self._validate_intent(intent)
        return self._bind_receipt(
            self._lifecycle.recover_client_order(
                intent,
                client_order_id=client_order_id,
                state=state,
            )
        )

    def cancel(self, order_id: str) -> OrderReceipt:
        return self._bind_receipt(
            self._lifecycle.cancel(self._lifecycle.resolve_order_id(order_id))
        )

    def modify(self, order_id: str, intent: OrderIntent) -> OrderReceipt:
        self._validate_intent(intent)
        return self._bind_receipt(self._lifecycle.modify(order_id, intent))

    def query(self, order_id: str) -> OrderReceipt:
        result = self._bind_receipt(self._lifecycle.query(order_id))
        self._sync_inline_fills()
        return result

    def open_orders(self, instrument_id: str | None = None) -> tuple[OrderReceipt, ...]:
        result = tuple(
            self._bind_receipt(item)
            for item in self._lifecycle.open_orders(instrument_id)
        )
        self._sync_inline_fills()
        return result

    def query_fills(
        self,
        *,
        order_id: str | None = None,
        instrument_id: str | None = None,
        client_order_id: str | None = None,
    ) -> tuple[OrderFill, ...]:
        """Query external fill observations and merge them idempotently."""

        if order_id and instrument_id:
            raise ValueError("query_fills accepts either order_id or instrument_id, not both")
        request: dict[str, object] = {}
        resolved_order_id = None
        if order_id is not None:
            resolved_order_id = self._lifecycle.resolve_order_id(order_id)
            receipt = self._lifecycle.get(resolved_order_id)
            request["instrument_id"] = self._lifecycle.instrument_id_for_order(resolved_order_id)
            request["oid"] = receipt.broker_order_id
            request["cloid"] = receipt.client_order_id
        elif instrument_id is not None:
            request["instrument_id"] = instrument_id
        else:
            raise BrokerCapabilityError(
                "order_execution",
                "fills",
                "an instrument or order scope is required by the external fill transport",
            )
        if client_order_id is not None:
            request["cloid"] = client_order_id
        response = self._runtime._invoke_native("order_execution", "fills", request)
        rows = response.get("fills") if isinstance(response, Mapping) else None
        if not isinstance(rows, list):
            raise RuntimeBoundaryError(
                "external_fill_response_invalid",
                "external fill query did not return a canonical fill list",
            )
        if client_order_id:
            rows = [
                row
                for row in rows
                if isinstance(row, Mapping)
                and str(row.get("cloid") or row.get("client_order_id") or "") == client_order_id
            ]
        for row in rows:
            if not isinstance(row, Mapping):
                raise RuntimeBoundaryError(
                    "external_fill_response_invalid",
                    "external fill query contained a non-mapping observation",
                )
            self.apply_fill(row)
        values = tuple(self.fills.values())
        if resolved_order_id is not None:
            values = tuple(item for item in values if item.order_id == resolved_order_id)
        if instrument_id is not None:
            values = tuple(item for item in values if item.instrument_id == instrument_id)
        if client_order_id is not None:
            values = tuple(item for item in values if item.client_order_id == client_order_id)
        return values

    def fills_by_client_order_id(
        self,
        *,
        client_order_id: str,
        instrument_id: str,
    ) -> tuple[OrderFill, ...]:
        return self.query_fills(
            instrument_id=instrument_id,
            client_order_id=client_order_id,
        )

    def apply_fill(self, raw: Mapping[str, object]) -> OrderReceipt:
        self.validate_fill(raw)
        result = self._bind_receipt(self._lifecycle.apply_fill(raw))
        self._sync_order_fills(raw)
        return result

    def validate_fill(self, raw: Mapping[str, object]) -> None:
        instrument_id = self._lifecycle.instrument_id_for_fill(raw)
        instrument = self._instruments.get(instrument_id)
        self._lifecycle.validate_fill(raw, expected_broker_symbol=instrument.broker_symbol)

    def validate_receipt(self, candidate: OrderReceipt) -> None:
        self._lifecycle.validate_receipt(candidate)

    def apply_order_update(self, raw: Mapping[str, object]) -> OrderReceipt:
        status = str(raw.get("status") or "").lower()
        if status in {"filled", "partially_filled", "partial"} and any(
            raw.get(field) is not None for field in ("coin", "tid", "side", "px", "sz", "time")
        ):
            self.validate_fill(raw)
        result = self._bind_receipt(self._lifecycle.apply_order_update(raw))
        self._sync_order_fills(raw)
        return result

    def reconcile(self, raw: Mapping[str, object]) -> OrderReceipt:
        normalized = self._lifecycle.normalize_reconcile_event(raw)
        status = str(normalized.get("status") or "").lower()
        if status in {"filled", "partially_filled", "partial"} and any(
            normalized.get(field) is not None for field in ("coin", "tid", "side", "px", "sz", "time")
        ):
            self.validate_fill(normalized)
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
            release_sha=self._runtime._config.expected_release_sha,
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
        for fill_id, raw_fill in self._lifecycle.fills.items():
            fill = self._bind_fill(raw_fill)
            if str(raw.get("tid") or raw.get("hash") or "") == fill_id:
                raw_payload = raw
            else:
                raw_payload = self._fill_raw(fill)
            self._ledger.record_order_fill(fill, raw_payload)

    def _fill_raw(self, fill: OrderFill) -> dict[str, object]:
        return {
            "tid": fill.fill_id,
            "oid": fill.broker_order_id,
            "cloid": fill.client_order_id,
            "coin": self._instruments.get(fill.instrument_id).broker_symbol,
            "side": "B" if fill.side is OrderSide.BUY else "A",
            "px": str(fill.price),
            "sz": str(fill.quantity),
            "time": int(fill.occurred_at.timestamp() * 1000),
        }

    def _sync_inline_fills(self) -> None:
        for fill in self._lifecycle.fills.values():
            bound_fill = self._bind_fill(fill)
            raw = self._lifecycle.fill_raws.get(fill.fill_id, self._fill_raw(bound_fill))
            if "provenance" not in raw:
                raw = dict(raw)
                raw["provenance"] = self._lifecycle.get(fill.order_id).provenance
            self._ledger.record_order_fill(bound_fill, raw)


@runtime_checkable
class ExternalOrderLifecyclePort(Protocol):
    """Typed lifecycle dependency required by the external facade."""

    @property
    def runtime_session(self) -> BrokerRuntimeSession:
        ...

    @property
    def local_only(self) -> bool:
        ...

    @property
    def transport_state(self) -> str:
        ...

    def submit(self, intent: OrderIntent) -> OrderReceipt:
        ...

    def recover(
        self,
        intent: OrderIntent,
        *,
        broker_order_id: str,
        state: str | OrderState,
    ) -> OrderReceipt:
        ...

    def cancel(self, order_id: str) -> OrderReceipt:
        ...

    def modify(self, order_id: str, intent: OrderIntent) -> OrderReceipt:
        ...

    def query(self, order_id: str) -> OrderReceipt:
        ...

    def open_orders(self, instrument_id: str | None = None) -> tuple[OrderReceipt, ...]:
        ...

    def query_fills(
        self,
        *,
        order_id: str | None = None,
        instrument_id: str | None = None,
    ) -> tuple[OrderFill, ...]:
        ...

    @property
    def fills(self) -> Mapping[str, OrderFill]:
        ...


class HyperliquidExternalOrderAdapter:
    """Public external OrderExecutionPort facade for one exact host binding.

    The host owns authorization and capability gates.  The injected runtime
    lifecycle owns canonical order state and native transport translation.
    No provider-native request or response crosses this facade.
    """

    name = "hyperliquid_external_order_execution"

    def __init__(self, *, host: object, lifecycle: ExternalOrderLifecyclePort) -> None:
        context = getattr(host, "context", None)
        runtime_session = getattr(lifecycle, "runtime_session", None)
        if context is None or runtime_session is None:
            raise TypeError("external order adapter requires a public host and runtime lifecycle")
        if runtime_session != context.session:
            raise RuntimeBoundaryError(
                "external_order_identity_mismatch",
                "order lifecycle session does not match the external host context",
            )
        if context.session.environment is not BrokerEnvironment.TESTNET:
            raise RuntimeBoundaryError(
                "external_order_environment_invalid",
                "external order facade is restricted to the exact Testnet binding",
            )
        if context.runtime_identity.transport_state != "external_testnet":
            raise RuntimeBoundaryError(
                "external_order_transport_invalid",
                "external order facade requires external_testnet transport",
            )
        if not callable(getattr(host, "authorize", None)):
            raise TypeError("external order adapter requires the public host authorization seam")
        if not isinstance(lifecycle, ExternalOrderLifecyclePort):
            raise TypeError("external order adapter requires a typed order lifecycle")
        if lifecycle.local_only is not False or lifecycle.transport_state != "external_testnet":
            raise RuntimeBoundaryError(
                "external_order_fixture_forbidden",
                "external order facade requires a non-local external_testnet lifecycle",
            )
        self._host = host
        self._lifecycle = lifecycle
        self._context = context

    @property
    def runtime_session(self) -> BrokerRuntimeSession:
        return self._lifecycle.runtime_session

    @property
    def local_only(self) -> bool:
        return self._lifecycle.local_only

    @property
    def transport_state(self) -> str:
        return self._lifecycle.transport_state

    def submit(self, intent: OrderIntent) -> OrderReceipt:
        self._authorize(intent.order_id, "submit", intent)
        return self._validate_receipt(self._lifecycle.submit(intent))

    def recover(
        self,
        intent: OrderIntent,
        *,
        broker_order_id: str,
        state: str | OrderState,
    ) -> OrderReceipt:
        self._authorize(intent.order_id, "query", intent)
        return self._validate_receipt(
            self._lifecycle.recover(
                intent,
                broker_order_id=broker_order_id,
                state=state,
            )
        )

    def recover_client_order(
        self,
        intent: OrderIntent,
        *,
        client_order_id: str,
        state: str | OrderState,
    ) -> OrderReceipt:
        self._authorize(intent.order_id, "query", intent)
        recover = getattr(self._lifecycle, "recover_client_order", None)
        if not callable(recover):
            raise RuntimeBoundaryError(
                "client_order_recovery_unavailable",
                "external order lifecycle does not support persisted client identity recovery",
            )
        return self._validate_receipt(
            recover(
                intent,
                client_order_id=client_order_id,
                state=state,
            )
        )

    def cancel(self, order_id: str) -> OrderReceipt:
        reference = self._reference(order_id)
        self._authorize(reference, "cancel", reference)
        return self._validate_receipt(self._lifecycle.cancel(reference))

    def replace(self, order_id: str, intent: OrderIntent) -> OrderReceipt:
        reference = self._reference(order_id)
        self._authorize(reference, "replace", intent)
        return self._validate_receipt(self._lifecycle.modify(reference, intent))

    def modify(self, order_id: str, intent: OrderIntent) -> OrderReceipt:
        """Compatibility spelling for callers using the runtime adapter name."""

        return self.replace(order_id, intent)

    def query(self, order_id: str) -> OrderReceipt:
        reference = self._reference(order_id)
        self._authorize(reference, "query", reference)
        return self._validate_receipt(self._lifecycle.query(reference))

    def open_orders(self, instrument_id: str | None = None) -> tuple[OrderReceipt, ...]:
        self._authorize(
            f"open-orders:{instrument_id or 'all'}",
            "open_orders",
            self._query(subject=instrument_id, kind="instrument" if instrument_id else "all"),
        )
        result = self._lifecycle.open_orders(instrument_id)
        if not isinstance(result, tuple):
            result = tuple(result)
        return tuple(self._validate_receipt(item) for item in result)

    def fills(
        self,
        *,
        order_id: str | None = None,
        instrument_id: str | None = None,
    ) -> tuple[OrderFill, ...]:
        if order_id and instrument_id:
            raise ValueError("fills accepts either order_id or instrument_id, not both")
        self._authorize(
            f"fills:{order_id or instrument_id or 'all'}",
            "fills",
            self._query(
                subject=order_id or instrument_id,
                kind="order" if order_id else "instrument" if instrument_id else "all",
            ),
        )
        values = self._lifecycle.query_fills(order_id=order_id, instrument_id=instrument_id)
        if not isinstance(values, tuple):
            values = tuple(values)
        return tuple(self._validate_fill(item) for item in values)

    def fills_by_client_order_id(
        self,
        *,
        client_order_id: str,
        instrument_id: str,
    ) -> tuple[OrderFill, ...]:
        self._authorize(
            f"fills-client:{client_order_id}",
            "fills",
            self._query(subject=instrument_id, kind="instrument"),
        )
        reader = getattr(self._lifecycle, "fills_by_client_order_id", None)
        if not callable(reader):
            raise RuntimeBoundaryError(
                "client_fill_recovery_unavailable",
                "external order lifecycle lacks client-scoped fill recovery",
            )
        values = reader(
            client_order_id=client_order_id,
            instrument_id=instrument_id,
        )
        if not isinstance(values, tuple):
            values = tuple(values)
        return tuple(self._validate_fill(item) for item in values)

    def _authorize(self, request_id: str, operation: str, payload: object) -> None:
        from ...external_host import ExternalHostRequest
        from ...host import CanonicalHostRequest

        self._host.authorize(
            ExternalHostRequest(
                request_id=request_id,
                request=CanonicalHostRequest(
                    port="order_execution",
                    operation=operation,
                    payload=payload,
                ),
            )
        )

    @staticmethod
    def _query(*, subject: str | None, kind: str):
        from ...host import CanonicalPortQuery

        return CanonicalPortQuery(subject=subject, kind=kind)

    @staticmethod
    def _reference(value: str) -> str:
        reference = str(value or "").strip()
        if not reference:
            raise ValueError("order reference is required")
        return reference

    def _validate_receipt(self, value: object) -> OrderReceipt:
        if not isinstance(value, OrderReceipt):
            raise RuntimeBoundaryError(
                "external_order_receipt_invalid",
                "external order lifecycle returned a non-canonical receipt",
            )
        if not isinstance(value.provenance, Provenance):
            raise RuntimeBoundaryError(
                "external_order_receipt_provenance_missing",
                "external order receipt must carry canonical provenance",
            )
        if (
            value.broker_id != self._context.identity.broker_id
            or value.environment is not self._context.identity.environment
            or value.account_address != self._context.identity.account_address
            or value.lifecycle_id != self._context.session.lifecycle_id
            or value.release_sha != self._context.release_sha
            or value.provenance.transport_state != self._context.runtime_identity.transport_state
            or value.provenance.execution_scope != self._context.identity.execution_scope
            or value.provenance.mapping_revision != self._context.capabilities.revision
            or self._context.runtime_identity.adapter_id not in value.provenance.source
        ):
            raise RuntimeBoundaryError(
                "external_order_receipt_identity_mismatch",
                "external order receipt does not match the bound host context",
            )
        return value

    def _validate_fill(self, value: object) -> OrderFill:
        if not isinstance(value, OrderFill):
            raise RuntimeBoundaryError(
                "external_order_fill_invalid",
                "external order lifecycle returned a non-canonical fill",
            )
        if (
            value.environment is not self._context.identity.environment
            or value.account_address != self._context.identity.account_address
            or value.lifecycle_id != self._context.session.lifecycle_id
            or value.release_sha != self._context.release_sha
        ):
            raise RuntimeBoundaryError(
                "external_order_fill_identity_mismatch",
                "external order fill does not match the bound host context",
            )
        return value
