"""Thin trading-system bridge over standard-broker's public canary binding."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal
import hashlib
from typing import Any

from services.standard_broker_testnet_canary import (
    TestnetCanaryError,
    TestnetCanaryOrderRequest,
)


class StandardBrokerExternalCanaryError(RuntimeError):
    """Stable blocker for an unavailable or mismatched public binding."""


class StandardBrokerExternalCanaryAdapter:
    """Map local canary requests to standard-broker canonical types only."""

    name = "standard_broker_external_testnet_canary"

    def __init__(self, binding: object) -> None:
        try:
            from standard_broker import ExternalCanaryBinding
        except ModuleNotFoundError as exc:
            raise StandardBrokerExternalCanaryError(
                "standard-broker public canary binding is unavailable"
            ) from exc
        required = ("preflight", "market_fact", "submit", "query", "query_by_idempotency_key", "replace", "cancel", "read_facts")
        if not isinstance(binding, ExternalCanaryBinding) and any(
            not callable(getattr(binding, name, None)) for name in required
        ):
            raise StandardBrokerExternalCanaryError(
                "public standard-broker canary binding is required"
            )
        self._binding = binding

    def preflight(self) -> dict[str, Any]:
        return dict(self._binding.preflight())

    def market_fact(self, *, instrument_id: str, now: datetime) -> dict[str, Any]:
        return dict(self._binding.market_fact(instrument_id=instrument_id, now=now))

    def submit(self, request: TestnetCanaryOrderRequest) -> object:
        return self._binding.submit(self._intent(request))

    def recover(
        self,
        request: TestnetCanaryOrderRequest,
        *,
        broker_order_id: str,
        state: str,
    ) -> None:
        """Restore one persisted canonical intent without submitting it."""

        recover = getattr(self._binding, "recover", None)
        if not callable(recover):
            raise StandardBrokerExternalCanaryError("public binding does not support intent recovery")
        recover(
            self._intent(request),
            broker_order_id=broker_order_id,
            state=state,
        )

    def query(self, order_id: str) -> object:
        return self._binding.query(order_id)

    def query_by_idempotency_key(self, idempotency_key: str) -> object:
        return self._binding.query_by_idempotency_key(idempotency_key)

    def replace(self, order_id: str, request: TestnetCanaryOrderRequest) -> object:
        return self._binding.replace(order_id, self._intent(request))

    def cancel(self, order_id: str) -> object:
        return self._binding.cancel(order_id)

    def read_facts(self, *, order_id: str, instrument_id: str, now: datetime) -> object:
        bundle = self._binding.read_facts(
            order_id=order_id,
            instrument_id=instrument_id,
            now=now,
        )
        return self._convert_bundle(bundle)

    def read_account_state(self, *, instrument_id: str, now: datetime) -> object:
        """Read the public cursor-bound account snapshot without an order fill query."""

        bundle = self._binding.read_facts(
            order_id="",
            instrument_id=instrument_id,
            now=now,
        )
        return self._convert_bundle(bundle)

    @staticmethod
    def _intent(request: TestnetCanaryOrderRequest) -> object:
        if not isinstance(request, TestnetCanaryOrderRequest):
            raise StandardBrokerExternalCanaryError("canary request must be canonical")
        try:
            from standard_broker import OrderIntent, OrderSide, OrderType, TimeInForce
        except ModuleNotFoundError as exc:
            raise StandardBrokerExternalCanaryError("standard-broker public types are unavailable") from exc
        try:
            return OrderIntent(
                order_id=request.order_id,
                instrument_id=request.instrument_id,
                side=OrderSide(request.side),
                order_type=OrderType(request.order_type),
                quantity=request.quantity,
                limit_price=request.limit_price,
                time_in_force=TimeInForce(request.time_in_force),
                idempotency_key=request.idempotency_key,
                reduce_only=request.reduce_only,
                close_position=request.close_position,
            )
        except (TypeError, ValueError) as exc:
            raise StandardBrokerExternalCanaryError(
                f"canonical request mapping failed: {type(exc).__name__}"
            ) from exc

    @classmethod
    def _convert_bundle(cls, bundle: object) -> object:
        try:
            from standard_broker import ExternalCanaryFactBundle
        except ModuleNotFoundError as exc:
            raise StandardBrokerExternalCanaryError("standard-broker typed facts are unavailable") from exc
        if not isinstance(bundle, ExternalCanaryFactBundle):
            raise StandardBrokerExternalCanaryError("standard-broker returned an invalid typed facts bundle")
        try:
            from services.standard_broker_testnet_canary_facts import (
                CanaryAccountFact,
                CanaryFactBundle,
                CanaryFeeFact,
                CanaryFillFact,
                CanaryPositionFact,
                CanaryReconciliationFact,
                canary_fact_digest,
            )
        except ImportError as exc:
            raise StandardBrokerExternalCanaryError("trading-system fact schema is unavailable") from exc
        snapshot = bundle.reconciliation
        try:
            from standard_broker import ExternalReconciliationSnapshot
        except ModuleNotFoundError as exc:
            raise StandardBrokerExternalCanaryError("standard-broker reconciliation contract is unavailable") from exc
        if not isinstance(snapshot, ExternalReconciliationSnapshot):
            raise StandardBrokerExternalCanaryError(
                "standard-broker returned a non-canonical reconciliation snapshot"
            )
        try:
            snapshot.verify_integrity()
        except Exception as exc:  # noqa: BLE001 - integrity is a hard evidence gate.
            raise StandardBrokerExternalCanaryError("standard-broker reconciliation integrity failed") from exc
        if snapshot.identity is None or snapshot.cursor is None:
            raise StandardBrokerExternalCanaryError(
                "standard-broker returned no cursor-bound reconciliation snapshot"
            )
        identity = snapshot.identity
        account_address = identity.account_address
        fingerprint = "sha256:" + hashlib.sha256(account_address.encode()).hexdigest()
        runtime_id = identity.lifecycle_id
        release_sha = identity.release_sha
        capability_revision = identity.capability_revision
        transport_state = identity.runtime_identity.transport_state
        cursor = str(snapshot.cursor.value)
        observed_at = (snapshot.observed_at or snapshot.cursor.observed_at).isoformat()

        def add_digest(value):
            return replace(value, fact_digest=canary_fact_digest(value))

        fills = tuple(
            add_digest(
                CanaryFillFact(
                    fill_id=fill.fill_id,
                    order_id=fill.order_id,
                    broker_order_id=fill.broker_order_id or "",
                    client_order_id=fill.client_order_id,
                    instrument_id=fill.instrument_id,
                    side=fill.side.value,
                    price=fill.price,
                    quantity=fill.quantity,
                    occurred_at=fill.occurred_at.isoformat(),
                    account_fingerprint=fingerprint,
                    runtime_id=runtime_id,
                    release_sha=release_sha,
                    capability_revision=capability_revision,
                    transport_state=transport_state,
                    cursor=cursor,
                )
            )
            for fill in bundle.fills
        )
        fees = tuple(
            add_digest(
                CanaryFeeFact(
                    fee_id=fee.fee_id,
                    fill_id=fee.fill_id or "",
                    amount_usd=fee.amount,
                    currency=fee.currency,
                    occurred_at=fee.occurred_at.isoformat(),
                    account_fingerprint=fingerprint,
                    runtime_id=runtime_id,
                    release_sha=release_sha,
                    capability_revision=capability_revision,
                    transport_state=transport_state,
                    cursor=cursor,
                    fee_source=fee.source.value,
                    fee_state=fee.state.value,
                )
            )
            for fee in bundle.fees
        )
        if bundle.account is None:
            raise StandardBrokerExternalCanaryError("standard-broker account fact is missing")
        account = add_digest(
            CanaryAccountFact(
                account_fingerprint=fingerprint,
                equity_usd=bundle.account.equity or Decimal("0"),
                cursor=cursor,
                observed_at=bundle.account.provenance.received_at.isoformat(),
                runtime_id=runtime_id,
                release_sha=release_sha,
                capability_revision=capability_revision,
                transport_state=transport_state,
            )
        )
        positions = tuple(
            add_digest(
                CanaryPositionFact(
                    instrument_id=position.instrument_id,
                    signed_quantity=position.signed_quantity,
                    cursor=cursor,
                    observed_at=position.provenance.received_at.isoformat(),
                    account_fingerprint=fingerprint,
                    runtime_id=runtime_id,
                    release_sha=release_sha,
                    capability_revision=capability_revision,
                    transport_state=transport_state,
                )
            )
            for position in bundle.positions
        )
        open_order_ids = tuple(order.order_id for order in bundle.open_orders)
        signed_position = sum((position.signed_quantity for position in positions), Decimal("0"))
        reconciliation = add_digest(
            CanaryReconciliationFact(
                coherent=snapshot.passed,
                freshness="fresh" if snapshot.passed else "unknown",
                cursor=cursor,
                open_order_ids=open_order_ids,
                signed_position_quantity=signed_position,
                observed_at=observed_at,
                account_fingerprint=fingerprint,
                runtime_id=runtime_id,
                release_sha=release_sha,
                capability_revision=capability_revision,
                transport_state=transport_state,
                canonical_schema="ExternalReconciliationSnapshot",
                evidence_digest=snapshot.evidence_digest,
            )
        )
        return CanaryFactBundle(
            fills=fills,
            fees=fees,
            account=account,
            positions=positions,
            open_orders=open_order_ids,
            reconciliation=reconciliation,
        )
