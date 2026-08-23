"""Consumer-facing attended external Testnet canary binding.

The binding is intentionally narrower than the general Broker ports.  It
composes the already-reviewed public host/order/fact/reconciliation contracts
and exposes only the typed lifecycle needed by one attended canary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib
import json
from typing import Any, Protocol, runtime_checkable

from .account import AccountSnapshot, PositionFact
from .errors import BrokerCapabilityError, RuntimeBoundaryError
from .external_host import ExternalBrokerBuildContext, ExternalBrokerHost, ExternalFactEnvelope
from .external_reconciliation import (
    ExternalCursorKind,
    ExternalReconciliationCursor,
    ExternalReconciliationObservation,
    ExternalReconciliationSnapshot,
)
from .fees import FeeEvent, FillFact
from .models import BrokerEnvironment, Provenance
from .orders import OrderFill, OrderIntent, OrderReceipt, OrderSide, OrderState


_ACCOUNT_FINGERPRINT_PREFIX = "sha256:"
_CANARY_OPERATIONS = {
    "market_data": {"ticker"},
    "instrument": {"read"},
    "order_execution": {"submit", "cancel", "replace", "query", "open_orders", "fills"},
    "account": {"read", "positions"},
    "fee": {"fill"},
}


def account_fingerprint(account_address: str) -> str:
    return _ACCOUNT_FINGERPRINT_PREFIX + hashlib.sha256(
        account_address.encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class ExternalCanaryReceipt:
    """Redacted receipt with the request identity needed by trading-system."""

    canonical_receipt: OrderReceipt
    instrument_id: str
    side: str
    quantity: Decimal
    order_type: str
    time_in_force: str
    limit_price: Decimal
    account_fingerprint: str
    capability_revision: str

    def __getattr__(self, name: str) -> object:
        # Preserve the canonical receipt vocabulary without copying provider
        # fields into a second mutable lifecycle owner.
        return getattr(self.canonical_receipt, name)


@dataclass(frozen=True)
class ExternalCanaryFactBundle:
    fills: tuple[OrderFill, ...]
    fees: tuple[FeeEvent, ...]
    account: AccountSnapshot | None
    positions: tuple[PositionFact, ...]
    open_orders: tuple[OrderReceipt, ...]
    reconciliation: ExternalReconciliationSnapshot | None


@runtime_checkable
class ExternalCanaryFactsReader(Protocol):
    def read(
        self,
        *,
        order_id: str,
        instrument_id: str,
        now: datetime,
    ) -> ExternalCanaryFactBundle:
        ...

    def market_fact(self, *, instrument_id: str, now: datetime) -> dict[str, object]:
        ...


class ExternalCanaryRuntimeFactsReader:
    """Compose existing typed runtime adapters into one cursor-bound bundle."""

    def __init__(self, *, context: ExternalBrokerBuildContext, order: object, account: object, fees: object, runtime: object | None = None, instruments: object | None = None, market: object | None = None) -> None:
        self._context = context
        self._order = order
        self._account = account
        self._fees = fees
        self._runtime = runtime
        self._instruments = instruments
        self._market = market
        for owner, methods in (
            (order, ("query_fills", "open_orders")),
            (account, ("read_account",)),
        ):
            if any(not callable(getattr(owner, method, None)) for method in methods):
                raise TypeError("runtime fact reader requires typed account/order adapters")
        if not hasattr(fees, "fill_facts"):
            raise TypeError("runtime fact reader requires the typed fee adapter")

    def read(self, *, order_id: str, instrument_id: str, now: datetime) -> ExternalCanaryFactBundle:
        if now.tzinfo is None:
            raise ValueError("fact read now must include timezone")
        fills = tuple(self._order.query_fills(order_id=order_id))
        account = self._account.read_account(self._context.identity.account_address or "")
        positions = tuple(
            position
            for position in account.positions
            if position.instrument_id == instrument_id
        )
        open_orders = tuple(self._order.open_orders(instrument_id))
        fill_ids = {fill.fill_id for fill in fills}
        fee_facts = tuple(
            fact for fact in self._fees.fill_facts
            if fact.fill_id in fill_ids
        )
        provenance = account.provenance
        cursor_value = str(account.observation_id or "")
        if not cursor_value:
            raise RuntimeBoundaryError(
                "canary_reconciliation_cursor_missing",
                "account observation must provide a Broker cursor",
            )
        cursor = ExternalReconciliationCursor(
            kind=ExternalCursorKind.CURSOR,
            value=cursor_value,
            observed_at=provenance.received_at,
        )
        account_observation = self._observation(
            context=self._context,
            fact_type="canary.account",
            data=account,
            provenance=provenance,
            request_id=f"canary-account:{cursor_value}",
            cursor=cursor,
        )
        position_observation = self._observation(
            context=self._context,
            fact_type="canary.positions",
            data=positions,
            provenance=provenance,
            request_id=f"canary-positions:{cursor_value}",
            cursor=cursor,
        )
        order_observation = self._observation(
            context=self._context,
            fact_type="canary.open_orders",
            data=open_orders,
            provenance=provenance,
            request_id=f"canary-open-orders:{cursor_value}",
            cursor=cursor,
        )
        fill_observation = self._observation(
            context=self._context,
            fact_type="canary.fills",
            data=fills,
            provenance=provenance,
            request_id=f"canary-fills:{cursor_value}:{order_id}",
            cursor=cursor,
        )
        fee_observation = self._observation(
            context=self._context,
            fact_type="canary.fees",
            data=tuple(fact.fee for fact in fee_facts),
            provenance=provenance,
            request_id=f"canary-fees:{cursor_value}:{order_id}",
            cursor=cursor,
        )
        reconciliation = ExternalReconciliationSnapshot.assemble(
            account=account_observation,
            positions=position_observation,
            open_orders=order_observation,
            fills=fill_observation,
            fees=fee_observation,
            funding=None,
            funding_applicable=False,
            now=now,
            stale_after=timedelta(minutes=2),
            max_observation_skew=timedelta(minutes=2),
        )
        return ExternalCanaryFactBundle(
            fills=fills,
            fees=tuple(fact.fee for fact in fee_facts),
            account=account,
            positions=positions,
            open_orders=open_orders,
            reconciliation=reconciliation,
        )

    def market_fact(self, *, instrument_id: str, now: datetime) -> dict[str, object]:
        if self._runtime is None or self._instruments is None or self._market is None:
            raise RuntimeBoundaryError(
                "canary_market_reader_unavailable",
                "external canary binding has no typed market reader",
            )
        instrument = self._instruments.get(instrument_id)
        raw = self._runtime._invoke_native(
            "market_data",
            "ticker",
            {"instrument_id": instrument_id},
        )
        envelope = self._market.map_ticker(
            request_id=f"canary-market:{instrument_id}",
            broker_symbol=instrument.broker_symbol,
            raw=raw,
            now=now,
        )
        market_envelope = envelope.data
        ticker = market_envelope.data
        price = ticker.mid or ticker.bid or ticker.ask
        if price is None:
            raise RuntimeBoundaryError("canary_market_price_missing", "external ticker has no executable price")
        observed_at = ticker.venue_timestamp or envelope.provenance.received_at
        result: dict[str, object] = {
            "instrument_id": instrument_id,
            "contract_multiplier": "1",
            "price": str(price),
            "freshness": market_envelope.freshness.value,
            "observed_at": observed_at.isoformat(),
            "max_age_seconds": "120",
            "source": envelope.provenance.source,
            "transport_state": envelope.provenance.transport_state,
            "mapping_revision": envelope.provenance.mapping_revision,
        }
        result["fact_digest"] = _market_fact_digest(result)
        return result

    @staticmethod
    def _observation(*, context, fact_type, data, provenance, request_id, cursor):
        envelope = ExternalFactEnvelope.create(
            context=context,
            fact_type=fact_type,
            data=data,
            request_id=request_id,
            provenance=provenance,
        )
        return ExternalReconciliationObservation(
            fact=envelope,
            cursor=cursor,
            receipt_digest=envelope.fact_digest,
        )


def _market_fact_digest(value: dict[str, object]) -> str:
    fields = (
        "instrument_id",
        "contract_multiplier",
        "price",
        "freshness",
        "observed_at",
        "max_age_seconds",
        "source",
        "transport_state",
        "mapping_revision",
    )
    encoded = json.dumps(
        {field: value.get(field) for field in fields},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class ExternalCanaryBinding:
    """One exact external Testnet order/facts binding for CANARY-03."""

    name = "hyperliquid_external_testnet_canary"

    def __init__(
        self,
        *,
        host: ExternalBrokerHost,
        order: object,
        facts: ExternalCanaryFactsReader,
    ) -> None:
        if not isinstance(host, ExternalBrokerHost):
            raise TypeError("canary binding requires the public ExternalBrokerHost")
        context = host.context
        if context.identity.broker_id != "hyperliquid" or context.identity.environment is not BrokerEnvironment.TESTNET:
            raise RuntimeBoundaryError(
                "canary_profile_invalid",
                "canary binding requires Hyperliquid Testnet",
            )
        if host.external_profile_id != "hyperliquid-testnet-default":
            raise RuntimeBoundaryError(
                "canary_profile_invalid",
                "canary binding requires the exact external Testnet profile",
            )
        if context.runtime_identity.transport_state != "external_testnet":
            raise RuntimeBoundaryError(
                "canary_transport_invalid",
                "canary binding requires external_testnet transport",
            )
        if not isinstance(facts, ExternalCanaryFactsReader):
            raise TypeError("canary binding requires a typed facts reader")
        required_order_methods = ("submit", "query", "replace", "cancel", "fills", "open_orders")
        if any(not callable(getattr(order, method, None)) for method in required_order_methods):
            raise TypeError("canary binding requires the public external order facade")
        self._host = host
        self._order = order
        self._facts = facts
        self._context = context
        self._idempotency: dict[str, tuple[str, OrderIntent]] = {}
        self._intents: dict[str, OrderIntent] = {}

    @property
    def context(self) -> ExternalBrokerBuildContext:
        return self._context

    @property
    def local_only(self) -> bool:
        return False

    @property
    def transport_state(self) -> str:
        return "external_testnet"

    def preflight(self) -> dict[str, Any]:
        receipt = self._host.preflight(
            request_id=f"canary-preflight:{self._context.session.lifecycle_id}",
            required_operations=_CANARY_OPERATIONS,
        )
        protection = self._host.protection_capabilities
        protection_gap = "external_protection_unavailable"
        if protection is not None:
            unsupported = sorted(
                name for name, supported in protection.values.items() if supported is not True
            )
            if unsupported:
                protection_gap = "capability_gap:protection_order:" + ",".join(unsupported)
        result: dict[str, Any] = {
            "canary_ready": receipt.accepted is True,
            "host_ready": receipt.accepted is True,
            "environment": "testnet",
            "transport_profile": self._host.external_profile_id,
            "transport_state": receipt.runtime_identity.transport_state,
            "account_fingerprint": account_fingerprint(self._context.identity.account_address or ""),
            "runtime_id": self._context.session.lifecycle_id,
            "release_sha": self._context.release_sha,
            "capability_revision": self._context.capabilities.revision,
            "network_io": receipt.network_io,
            "real_money_eligible": receipt.real_money_eligible,
            "broker_operation_invoked": False,
            "preflight_io_performed": False,
            "protection_ready": False,
            "protection_gap": protection_gap,
            "upstream_receipt_digest": receipt.receipt_digest,
        }
        market_reader = getattr(self._facts, "market_fact", None)
        if callable(market_reader):
            # Market is intentionally fetched only when the canary coordinator
            # asks for it; preflight itself remains network-free.
            result["market_fact_reader_available"] = True
        return result

    def submit(self, intent: OrderIntent) -> ExternalCanaryReceipt:
        self._remember_intent(intent)
        receipt = self._order.submit(intent)
        return self._wrap_receipt(receipt, intent)

    def query(self, order_id: str) -> ExternalCanaryReceipt:
        receipt = self._order.query(order_id)
        intent = self._intent_for_order(receipt.order_id)
        return self._wrap_receipt(receipt, intent)

    def query_by_idempotency_key(self, idempotency_key: str) -> ExternalCanaryReceipt:
        record = self._idempotency.get(str(idempotency_key))
        if record is None:
            raise RuntimeBoundaryError(
                "idempotency_recovery_unavailable",
                "this binding has no durable idempotency ownership for the requested key",
            )
        order_id, intent = record
        return self._wrap_receipt(self._order.query(order_id), intent)

    def replace(self, order_id: str, intent: OrderIntent) -> ExternalCanaryReceipt:
        self._remember_intent(intent)
        receipt = self._order.replace(order_id, intent)
        return self._wrap_receipt(receipt, intent)

    def cancel(self, order_id: str) -> ExternalCanaryReceipt:
        receipt = self._order.cancel(order_id)
        intent = self._intent_for_order(receipt.order_id)
        return self._wrap_receipt(receipt, intent)

    def read_facts(
        self,
        *,
        order_id: str,
        instrument_id: str,
        now: datetime,
    ) -> ExternalCanaryFactBundle:
        bundle = self._facts.read(order_id=order_id, instrument_id=instrument_id, now=now)
        if not isinstance(bundle, ExternalCanaryFactBundle):
            raise RuntimeBoundaryError(
                "canary_facts_invalid",
                "typed canary facts reader returned an invalid bundle",
            )
        return bundle

    def market_fact(self, *, instrument_id: str, now: datetime) -> dict[str, object]:
        reader = getattr(self._facts, "market_fact", None)
        if not callable(reader):
            raise RuntimeBoundaryError(
                "canary_market_reader_unavailable",
                "external canary binding has no typed market reader",
            )
        return reader(instrument_id=instrument_id, now=now)

    def _remember_intent(self, intent: OrderIntent) -> None:
        if not isinstance(intent, OrderIntent):
            raise TypeError("canary order binding accepts canonical OrderIntent only")
        existing = self._idempotency.get(intent.idempotency_key)
        if existing is not None and existing[1] != intent:
            raise RuntimeBoundaryError(
                "idempotency_conflict",
                "idempotency key was reused for a different canonical intent",
            )
        self._idempotency[intent.idempotency_key] = (intent.order_id, intent)
        self._intents[intent.order_id] = intent

    def _intent_for_order(self, order_id: str) -> OrderIntent:
        intent = self._intents.get(str(order_id))
        if intent is None:
            raise RuntimeBoundaryError(
                "order_intent_identity_missing",
                "canonical order query has no bound request identity",
            )
        return intent

    def _wrap_receipt(self, receipt: OrderReceipt, intent: OrderIntent) -> ExternalCanaryReceipt:
        if not isinstance(receipt, OrderReceipt):
            raise RuntimeBoundaryError("canary_receipt_invalid", "external order facade returned a non-canonical receipt")
        context = self._context
        if (
            receipt.broker_id != context.identity.broker_id
            or receipt.environment is not BrokerEnvironment.TESTNET
            or receipt.lifecycle_id != context.session.lifecycle_id
            or receipt.release_sha != context.release_sha
            or receipt.provenance.transport_state != "external_testnet"
            or receipt.provenance.mapping_revision != context.capabilities.revision
        ):
            raise RuntimeBoundaryError("canary_receipt_identity_mismatch", "receipt identity does not match the exact binding")
        return ExternalCanaryReceipt(
            canonical_receipt=receipt,
            instrument_id=intent.instrument_id,
            side=intent.side.value,
            quantity=intent.quantity,
            order_type=intent.order_type.value,
            time_in_force=intent.time_in_force.value,
            limit_price=intent.limit_price or Decimal("0"),
            account_fingerprint=account_fingerprint(context.identity.account_address or ""),
            capability_revision=context.capabilities.revision,
        )


def digest_external_canary_value(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
