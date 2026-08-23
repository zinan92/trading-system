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
from .external_host import (
    ExternalBrokerBuildContext,
    ExternalBrokerHost,
    ExternalFactEnvelope,
    ExternalHostRequest,
)
from .external_reconciliation import (
    ExternalCursorKind,
    ExternalReconciliationCursor,
    ExternalReconciliationObservation,
    ExternalReconciliationSnapshot,
)
from .external_protection import ExternalProtectionBinding
from .fees import FeeEvent
from .host import CanonicalHostRequest, CanonicalPortQuery
from .models import BrokerEnvironment
from .orders import OrderFill, OrderIntent, OrderReceipt


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


class HyperliquidExternalSnapshotReader:
    """Read typed observations and return one verified cursor-bound snapshot."""

    def __init__(
        self,
        *,
        context: ExternalBrokerBuildContext,
        host: ExternalBrokerHost,
        order: object,
        facts_mapper: object,
        instruments: object,
    ) -> None:
        self._context = context
        self._host = host
        self._order = order
        self._mapper = facts_mapper
        self._instruments = instruments
        if not callable(getattr(order, "query_fills", None)) or not callable(getattr(order, "open_orders", None)):
            raise TypeError("snapshot reader requires the public order facts facade")
        for method in ("map_account", "map_positions", "map_fill"):
            if not callable(getattr(facts_mapper, method, None)):
                raise TypeError("snapshot reader requires the typed Hyperliquid fact mapper")

    def read_reconciliation(
        self,
        *,
        order_id: str,
        instrument_id: str,
        now: datetime,
    ) -> ExternalReconciliationSnapshot:
        if now.tzinfo is None:
            raise ValueError("snapshot now must include timezone")
        instrument = self._instruments.get(instrument_id)
        account_envelope = self._read_fact(
            port="account",
            operation="read",
            subject=self._context.identity.account_address or "",
            kind="account",
            request_id=f"canary-account:{order_id}",
            mapper=lambda raw: self._mapper.map_account(
                request_id=f"canary-account:{order_id}",
                raw=raw,
            ),
        )
        positions_envelope = self._read_fact(
            port="account",
            operation="positions",
            subject=instrument.broker_symbol,
            kind="instrument",
            request_id=f"canary-positions:{order_id}",
            mapper=lambda raw: self._mapper.map_positions(
                request_id=f"canary-positions:{order_id}",
                broker_symbol=instrument.broker_symbol,
                raw=raw,
            ),
        )
        fills = tuple(self._order.query_fills(order_id=order_id))
        open_orders = tuple(self._order.open_orders(instrument_id))
        provenance = account_envelope.provenance
        fill_envelope = self._envelope(
            fact_type="canary.fills",
            data=fills,
            provenance=provenance,
            request_id=f"canary-fills:{order_id}",
        ) if fills else None
        open_order_envelope = self._envelope(
            fact_type="canary.open_orders",
            data=open_orders,
            provenance=provenance,
            request_id=f"canary-open-orders:{order_id}",
        )
        fee_events: list[FeeEvent] = []
        for fill in fills:
            mapped = self._read_fact(
                port="fee",
                operation="fill",
                subject=fill.fill_id,
                kind="fill",
                request_id=f"canary-fee:{fill.fill_id}",
                mapper=lambda raw, fill_id=fill.fill_id: self._mapper.map_fill(
                    request_id=f"canary-fee:{fill_id}",
                    raw=raw,
                ),
            )
            fill_fact = mapped.data
            if fill_fact.fill_id != fill.fill_id or fill_fact.order_id not in {None, fill.order_id}:
                raise RuntimeBoundaryError(
                    "canary_fee_fill_identity_mismatch",
                    "actual fee fact does not match the canonical fill",
                )
            fee_events.append(fill_fact.fee)
        fee_envelope = self._envelope(
            fact_type="canary.fees",
            data=tuple(fee_events),
            provenance=provenance,
            request_id=f"canary-fees:{order_id}",
        ) if fee_events else None
        envelopes = [account_envelope, positions_envelope, open_order_envelope]
        if fill_envelope is not None:
            envelopes.append(fill_envelope)
        if fee_envelope is not None:
            envelopes.append(fee_envelope)
        observed_times = [envelope.provenance.received_at for envelope in envelopes]
        observed_times.extend(fill.occurred_at for fill in fills)
        observed_times.extend(receipt.updated_at for receipt in open_orders)
        observed_times.extend(fee.occurred_at for fee in fee_events)
        observed_at = max(observed_times)
        watermark = int(observed_at.timestamp() * 1000)
        if watermark <= 0:
            raise RuntimeBoundaryError(
                "canary_reconciliation_watermark_missing",
                "typed observations have no positive Broker watermark",
            )
        cursor = ExternalReconciliationCursor(
            kind=ExternalCursorKind.WATERMARK,
            value=watermark,
            observed_at=observed_at,
        )
        observations = [
            ExternalReconciliationObservation(
                fact=envelope,
                cursor=cursor,
                receipt_digest=envelope.fact_digest,
            )
            for envelope in envelopes
        ]
        account_observation, positions_observation, open_orders_observation = observations[:3]
        index = 3
        fills_observation = None
        fees_observation = None
        if fill_envelope is not None:
            fills_observation = observations[index]
            index += 1
        if fee_envelope is not None:
            fees_observation = observations[index]
        return ExternalReconciliationSnapshot.assemble(
            account=account_observation,
            positions=positions_observation,
            open_orders=open_orders_observation,
            fills=fills_observation,
            fees=fees_observation,
            funding=None,
            funding_applicable=False,
            now=now,
            stale_after=timedelta(minutes=2),
            max_observation_skew=timedelta(minutes=2),
        )

    def _read_fact(self, *, port, operation, subject, kind, request_id, mapper):
        return self._host.read_fact(
            request=ExternalHostRequest(
                request_id=request_id,
                request=CanonicalHostRequest(
                    port=port,
                    operation=operation,
                    payload=CanonicalPortQuery(subject=subject, kind=kind),
                ),
            ),
            mapper=mapper,
        )

    def _envelope(self, *, fact_type, data, provenance, request_id):
        return ExternalFactEnvelope.create(
            context=self._context,
            fact_type=fact_type,
            data=data,
            request_id=request_id,
            provenance=provenance,
        )


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


@runtime_checkable
class ExternalCanarySnapshotReader(Protocol):
    """Public provider of one already cursor-bound reconciliation snapshot."""

    def read_reconciliation(
        self,
        *,
        order_id: str,
        instrument_id: str,
        now: datetime,
    ) -> ExternalReconciliationSnapshot:
        ...


class ExternalCanaryRuntimeFactsReader:
    """Project one public cursor-bound snapshot into canary facts.

    It intentionally does not assemble a cursor by copying one account
    timestamp over independent reads.  A real adapter must provide the
    Broker-owned ``ExternalReconciliationSnapshot``.
    """

    def __init__(
        self,
        *,
        context: ExternalBrokerBuildContext,
        host: ExternalBrokerHost,
        order: object,
        snapshot_reader: ExternalCanarySnapshotReader,
        instruments: object | None = None,
        market: object | None = None,
    ) -> None:
        self._context = context
        self._host = host
        self._order = order
        self._snapshot_reader = snapshot_reader
        self._instruments = instruments
        self._market = market
        if not callable(getattr(order, "query_fills", None)):
            raise TypeError("runtime fact reader requires the public order fills facade")
        if not isinstance(snapshot_reader, ExternalCanarySnapshotReader):
            raise TypeError("runtime fact reader requires a public reconciliation snapshot reader")

    def read(self, *, order_id: str, instrument_id: str, now: datetime) -> ExternalCanaryFactBundle:
        snapshot = self._snapshot_reader.read_reconciliation(
            order_id=order_id,
            instrument_id=instrument_id,
            now=now,
        )
        if not isinstance(snapshot, ExternalReconciliationSnapshot):
            raise RuntimeBoundaryError("canary_reconciliation_invalid", "snapshot reader returned a non-canonical snapshot")
        snapshot.verify_integrity()
        account = snapshot.account.fact.data if snapshot.account is not None else None
        positions = snapshot.positions.fact.data if snapshot.positions is not None else ()
        open_orders = snapshot.open_orders.fact.data if snapshot.open_orders is not None else ()
        fills = snapshot.fills.fact.data if snapshot.fills is not None else ()
        fees_data = snapshot.fees.fact.data if snapshot.fees is not None else ()
        fees = tuple(item for item in fees_data if isinstance(item, FeeEvent))
        return ExternalCanaryFactBundle(
            fills=tuple(fills),
            fees=fees,
            account=account,
            positions=tuple(positions),
            open_orders=tuple(open_orders),
            reconciliation=snapshot,
        )

    def market_fact(self, *, instrument_id: str, now: datetime) -> dict[str, object]:
        if self._instruments is None or self._market is None:
            raise RuntimeBoundaryError("canary_market_reader_unavailable", "typed public market reader is unavailable")
        instrument = self._instruments.get(instrument_id)
        request = ExternalHostRequest(
            request_id=f"canary-market:{instrument_id}",
            request=CanonicalHostRequest(
                port="market_data",
                operation="ticker",
                payload=CanonicalPortQuery(subject=instrument_id, kind="instrument"),
            ),
        )
        def map_ticker(raw):
            # The host receives the observation before invoking the mapper.
            # Use a completion-time floor so a caller timestamp captured before
            # a network read cannot make a just-received fact look future/unknown.
            effective_now = max(now, datetime.now(UTC))
            return self._market.map_ticker(
                request_id=request.request_id,
                broker_symbol=instrument.broker_symbol,
                raw=raw,
                now=effective_now,
            )

        envelope = self._host.read_fact(
            request=request,
            mapper=map_ticker,
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
        expected_profile_id: str = "hyperliquid-testnet-default",
        protection: ExternalProtectionBinding | None = None,
    ) -> None:
        if not isinstance(host, ExternalBrokerHost):
            raise TypeError("canary binding requires the public ExternalBrokerHost")
        context = host.context
        if context.identity.broker_id != "hyperliquid" or context.identity.environment is not BrokerEnvironment.TESTNET:
            raise RuntimeBoundaryError(
                "canary_profile_invalid",
                "canary binding requires Hyperliquid Testnet",
            )
        if host.external_profile_id != expected_profile_id:
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
        if protection is not None:
            if not isinstance(protection, ExternalProtectionBinding):
                raise TypeError("protected canary binding requires the public ExternalProtectionBinding")
            if protection.runtime_session != context.session:
                raise RuntimeBoundaryError(
                    "canary_protection_identity_mismatch",
                    "protected canary binding must share the exact runtime session",
                )
        required_order_methods = ("submit", "query", "replace", "cancel", "fills", "open_orders")
        if any(not callable(getattr(order, method, None)) for method in required_order_methods):
            raise TypeError("canary binding requires the public external order facade")
        self._host = host
        self._order = order
        self._facts = facts
        self._protection = protection
        self._context = context
        self._idempotency: dict[str, tuple[str, OrderIntent]] = {}
        self._intents: dict[str, OrderIntent] = {}

    @property
    def context(self) -> ExternalBrokerBuildContext:
        return self._context

    @property
    def runtime_session(self):
        """Return the exact public runtime session bound to this canary."""

        return self._context.session

    @property
    def protection_capabilities(self):
        """Return the profile-owned public protection capability matrix."""

        return self._host.protection_capabilities

    @property
    def local_only(self) -> bool:
        return False

    @property
    def transport_state(self) -> str:
        return "external_testnet"

    @property
    def protection(self) -> ExternalProtectionBinding | None:
        return self._protection

    @property
    def profile_id(self) -> str | None:
        return self._host.external_profile_id

    def preflight(self) -> dict[str, Any]:
        receipt = self._host.preflight(
            request_id=f"canary-preflight:{self._context.session.lifecycle_id}",
            required_operations=_CANARY_OPERATIONS,
        )
        protection = self._host.protection_capabilities
        protection_gap = "external_protection_unavailable"
        protection_ready = False
        if protection is not None and self._protection is not None:
            unsupported = sorted(
                name
                for name in {
                    "submit",
                    "cancel",
                    "replace",
                    "query",
                    "reduce_only",
                    "mark_price_trigger",
                    "grouped_tp_sl",
                    "sibling_cancellation",
                    "position_following",
                    "position_level_tpsl",
                    "take_profit_market",
                    "stop_loss_market",
                    "position_coverage",
                    "partial_fill_repair",
                    "cancel_replace",
                }
                if protection.supports(name) is not True
            )
            protection_ready = not unsupported
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
            "protection_ready": protection_ready,
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

    def recover(self, intent: OrderIntent) -> None:
        """Restore one persisted canonical intent without invoking transport."""

        self._remember_intent(intent)

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
