"""Typed fill-to-flat facts and evidence for the attended Testnet canary.

This module deliberately accepts a typed facts bundle rather than raw provider
payloads.  The broker adapter remains the only component allowed to map venue
objects; this coordinator only checks identity, ownership, and reconciliation.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from typing import Any, Protocol, runtime_checkable

from services.standard_broker_testnet_canary import (
    TestnetCanary,
    TestnetCanaryError,
    TestnetCanaryOrderRequest,
    TestnetCanaryOrderPort,
    TestnetCanaryPlan,
)
from services.strategy_control_plane import production_mutation_lock


FACT_SCHEMA = "standard-broker-testnet-canary-facts-v1"
MAX_FACT_AGE = timedelta(minutes=2)


@dataclass(frozen=True)
class CanaryFillFact:
    fill_id: str
    order_id: str
    instrument_id: str
    side: str
    price: Decimal
    quantity: Decimal
    occurred_at: str
    account_fingerprint: str
    runtime_id: str
    release_sha: str
    capability_revision: str
    transport_state: str = "external_testnet"
    broker_order_id: str = ""
    client_order_id: str = ""
    cursor: str = ""
    fact_digest: str = ""
    raw_payload_digest: str | None = None


@dataclass(frozen=True)
class CanaryFeeFact:
    fee_id: str
    fill_id: str
    amount_usd: Decimal
    currency: str
    occurred_at: str
    account_fingerprint: str
    runtime_id: str
    release_sha: str
    capability_revision: str
    transport_state: str = "external_testnet"
    cursor: str = ""
    fee_source: str = ""
    fee_state: str = ""
    fact_digest: str = ""
    raw_payload_digest: str | None = None


@dataclass(frozen=True)
class CanaryAccountFact:
    account_fingerprint: str
    equity_usd: Decimal
    cursor: str
    observed_at: str
    runtime_id: str
    release_sha: str
    capability_revision: str
    transport_state: str = "external_testnet"
    fact_digest: str = ""
    raw_payload_digest: str | None = None


@dataclass(frozen=True)
class CanaryPositionFact:
    instrument_id: str
    signed_quantity: Decimal
    cursor: str
    observed_at: str
    account_fingerprint: str
    runtime_id: str
    release_sha: str
    capability_revision: str
    transport_state: str = "external_testnet"
    fact_digest: str = ""
    raw_payload_digest: str | None = None


@dataclass(frozen=True)
class CanaryReconciliationFact:
    coherent: bool
    freshness: str
    cursor: str
    open_order_ids: tuple[str, ...]
    signed_position_quantity: Decimal
    observed_at: str
    account_fingerprint: str
    runtime_id: str
    release_sha: str
    capability_revision: str
    transport_state: str = "external_testnet"
    canonical_schema: str = "ExternalReconciliationSnapshot"
    evidence_digest: str = ""
    fact_digest: str = ""
    raw_payload_digest: str | None = None

    @property
    def passed(self) -> bool:
        return self.coherent

    def require_coherent(self) -> "CanaryReconciliationFact":
        if not self.coherent:
            raise TestnetCanaryError("external reconciliation is not coherent")
        return self


@dataclass(frozen=True)
class CanaryFactBundle:
    fills: tuple[CanaryFillFact, ...]
    fees: tuple[CanaryFeeFact, ...]
    account: CanaryAccountFact
    positions: tuple[CanaryPositionFact, ...]
    open_orders: tuple[str, ...]
    reconciliation: CanaryReconciliationFact


def canary_fact_digest(value: object) -> str:
    """Compute the redacted canonical fact digest used by the wrapper seam."""

    if not is_dataclass(value):
        raise TypeError("fact digest requires a dataclass fact")
    payload: dict[str, Any] = {}
    for field in fields(value):
        if field.name in {"fact_digest", "raw_payload_digest"}:
            continue
        item = getattr(value, field.name)
        if isinstance(item, Decimal):
            payload[field.name] = str(item)
        elif isinstance(item, tuple):
            payload[field.name] = list(item)
        else:
            payload[field.name] = item
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@runtime_checkable
class TestnetCanaryFactPort(Protocol):
    """Public typed fact/reconciliation seam supplied by standard-broker.

    The production implementation must wrap the public
    ``ExternalReconciliationSnapshot`` and call ``require_coherent`` before
    returning this bundle; this module never reads provider-native payloads.
    """

    def read_facts(self, *, order_id: str, instrument_id: str) -> CanaryFactBundle:
        ...


class TestnetCanaryFillFlat:
    """Complete CANARY-02 through attended ordinary reduce-only close."""

    __test__ = False

    def __init__(self, canary: TestnetCanary, facts: TestnetCanaryFactPort) -> None:
        if not isinstance(facts, TestnetCanaryFactPort):
            raise TypeError("canary facts must implement the public typed facts port")
        self.canary = canary
        self.facts = facts

    def snapshot(self, plan: TestnetCanaryPlan | None = None) -> dict[str, Any]:
        return self.canary.snapshot(plan)

    def complete(
        self,
        plan: TestnetCanaryPlan,
        *,
        confirmation: dict[str, Any],
        timestamp: str,
    ) -> dict[str, Any]:
        with production_mutation_lock(self.canary.output_root):
            return self._complete_locked(
                plan,
                confirmation=confirmation,
                timestamp=timestamp,
            )

    def _complete_locked(
        self,
        plan: TestnetCanaryPlan,
        *,
        confirmation: dict[str, Any],
        timestamp: str,
    ) -> dict[str, Any]:
        state = self.canary.snapshot(plan)
        if not state:
            raise TestnetCanaryError("canary has not been prepared")
        if state.get("status") in {"FLAT_RECONCILED", "BLOCKED"}:
            return state
        if state.get("status") in {"ENTRY_FACTS_READING", "CLOSE_SUBMIT_INTENT_RESERVED"}:
            return self._fail(
                state,
                "fill-to-flat outcome is unknown; manual reconciliation is required",
                timestamp=timestamp,
            )
        try:
            now = self.canary._checked_timestamp(timestamp, "timestamp")
            self.canary._require_confirmation(plan, confirmation, now, state=state)
        except TestnetCanaryError as exc:
            self._block(state, str(exc), timestamp=timestamp)
            raise
        if state.get("status") != "ENTRY_FILLED":
            raise TestnetCanaryError(
                f"fill-to-flat requires ENTRY_FILLED, got {state.get('status')}"
            )
        entry_order_id = str(state.get("entry_order_id") or "").strip()
        if not entry_order_id:
            return self._fail(state, "entry order identity is missing", timestamp=timestamp)

        try:
            state["status"] = "ENTRY_FACTS_READING"
            state["lifecycle_state"] = "ENTRY_FACTS_READING"
            state["next_action"] = "read_entry_facts"
            self.canary._event(state, "entry_facts_reading", timestamp=timestamp)
            self.canary._save(state)
            entry_bundle = self.facts.read_facts(
                order_id=entry_order_id,
                instrument_id=plan.instrument_id,
            )
            entry_evidence, position_quantity = self._validate_bundle(
                entry_bundle,
                plan,
                order_id=entry_order_id,
                phase="entry",
                now=now,
                expected_client_order_id=self._receipt_lineage(state, "query", "client_order_id"),
                expected_broker_order_id=self._receipt_lineage(state, "query", "broker_order_id"),
            )
            state["entry_facts"] = entry_evidence
            state["entry_facts_digest"] = self._evidence_digest(entry_evidence)
            state["entry_position_quantity"] = str(position_quantity)
            state["status"] = "ENTRY_FACTS_RECONCILED"
            state["lifecycle_state"] = "ENTRY_FACTS_RECONCILED"
            state["next_action"] = "attended_submit_ordinary_reduce_only_close"
            self.canary._event(state, "entry_facts_reconciled", timestamp=timestamp)
            self.canary._save(state)

            close_request = self._close_request(plan, position_quantity)
            state["status"] = "CLOSE_SUBMIT_INTENT_RESERVED"
            state["lifecycle_state"] = "CLOSE_SUBMITTED"
            state["close_intent"] = self.canary._safe_request(close_request)
            state["next_action"] = "recover_or_query_close"
            self.canary._event(
                state,
                "ordinary_reduce_only_close_intent_reserved",
                timestamp=timestamp,
            )
            self.canary._save(state)

            close_receipt = self.canary.broker.submit(close_request)
            self.canary._append_receipt(
                state,
                close_receipt,
                timestamp=timestamp,
                operation="close_submit",
                request=close_request,
            )
            self.canary._append_lineage(
                state,
                close_request,
                timestamp=timestamp,
                operation="ordinary_reduce_only_close",
            )
            close_order_id = self.canary._safe_identifier(
                getattr(close_receipt, "order_id", "")
            )
            if not close_order_id:
                raise TestnetCanaryError("close receipt order identity is missing")
            state["close_order_id"] = close_order_id
            close_state = self.canary._receipt_state(close_receipt)
            if close_state in {"unknown", "rejected"}:
                raise TestnetCanaryError(f"close submit state is {close_state}")
            queried_close = self.canary.broker.query(close_order_id)
            self.canary._append_receipt(
                state,
                queried_close,
                timestamp=timestamp,
                operation="close_query",
                request=close_request,
            )
            queried_close_state = self.canary._receipt_state(queried_close)
            if queried_close_state != "filled":
                raise TestnetCanaryError(
                    f"close query did not reach terminal filled state: {queried_close_state}"
                )
            state["close_state"] = queried_close_state
            self.canary._event(
                state,
                "ordinary_reduce_only_close_observed",
                timestamp=timestamp,
                order_id=close_order_id,
            )

            final_bundle = self.facts.read_facts(
                order_id=close_order_id,
                instrument_id=plan.instrument_id,
            )
            final_evidence, final_position_quantity = self._validate_bundle(
                final_bundle,
                plan,
                order_id=close_order_id,
                phase="final",
                now=now,
                expected_close_quantity=abs(position_quantity),
                expected_client_order_id=self._receipt_lineage(state, "close_query", "client_order_id"),
                expected_broker_order_id=self._receipt_lineage(state, "close_query", "broker_order_id"),
            )
            state["final_facts"] = final_evidence
            state["final_facts_digest"] = self._evidence_digest(final_evidence)
            if final_position_quantity != 0 or final_bundle.open_orders:
                raise TestnetCanaryError(
                    "final reconciliation is not flat: open orders or position remain"
                )
            if not final_bundle.reconciliation.coherent:
                raise TestnetCanaryError("final reconciliation is not coherent")
            state["status"] = "FLAT_RECONCILED"
            state["lifecycle_state"] = "FLAT_RECONCILED"
            state["next_action"] = "record_canary_result"
            self.canary._event(state, "flat_reconciled", timestamp=timestamp)
        except TestnetCanaryError as exc:
            self._block(state, str(exc), timestamp=timestamp)
            raise
        except Exception as exc:  # noqa: BLE001 - unknown external facts freeze.
            self._block(
                state,
                f"fill_flat_unknown:{type(exc).__name__}",
                timestamp=timestamp,
            )
            raise TestnetCanaryError(state["blocker"]) from exc
        state["updated_at"] = timestamp
        self.canary._save(state)
        return state

    @staticmethod
    def _close_request(
        plan: TestnetCanaryPlan,
        position_quantity: Decimal,
    ) -> TestnetCanaryOrderRequest:
        if position_quantity == 0:
            raise TestnetCanaryError("cannot close a zero position")
        return TestnetCanaryOrderRequest(
            order_id=f"{plan.canary_id}:close",
            instrument_id=plan.instrument_id,
            side="sell" if plan.direction == "buy" else "buy",
            quantity=abs(position_quantity),
            order_type="limit",
            limit_price=plan.close_price,
            time_in_force="ioc",
            idempotency_key=f"{plan.canary_id}:close:{abs(position_quantity)}:{plan.close_price}",
            reduce_only=True,
            close_position=True,
        )

    def _validate_bundle(
        self,
        bundle: CanaryFactBundle,
        plan: TestnetCanaryPlan,
        *,
        order_id: str,
        phase: str,
        now: datetime,
        expected_close_quantity: Decimal | None = None,
        expected_client_order_id: str = "",
        expected_broker_order_id: str = "",
    ) -> tuple[dict[str, Any], Decimal]:
        if not isinstance(bundle, CanaryFactBundle):
            raise TestnetCanaryError(f"{phase} facts are not a typed bundle")
        self._validate_common_identity(bundle.account, plan, phase=phase)
        self._validate_common_identity(bundle.reconciliation, plan, phase=phase)
        try:
            bundle.reconciliation.require_coherent()
        except Exception as exc:  # noqa: BLE001
            raise TestnetCanaryError(f"{phase} reconciliation is not coherent") from exc
        if bundle.reconciliation.canonical_schema != "ExternalReconciliationSnapshot":
            raise TestnetCanaryError(f"{phase} reconciliation contract is not canonical")
        if not bundle.reconciliation.passed:
            raise TestnetCanaryError(f"{phase} reconciliation is not coherent")
        if (
            not bundle.reconciliation.evidence_digest.startswith("sha256:")
            or len(bundle.reconciliation.evidence_digest) != 71
        ):
            raise TestnetCanaryError(f"{phase} reconciliation evidence digest is invalid")
        if bundle.reconciliation.freshness != "fresh":
            raise TestnetCanaryError(f"{phase} reconciliation is stale")
        if not bundle.reconciliation.cursor:
            raise TestnetCanaryError(f"{phase} reconciliation cursor is missing")
        self._validate_identifier(bundle.reconciliation.cursor, f"{phase}.reconciliation.cursor")
        self._validate_timestamp(bundle.reconciliation.observed_at, f"{phase}.reconciliation.observed_at", now=now)
        if bundle.account.cursor != bundle.reconciliation.cursor:
            raise TestnetCanaryError(f"{phase} account cursor mismatch")
        if not bundle.account.equity_usd.is_finite() or bundle.account.equity_usd < 0:
            raise TestnetCanaryError(f"{phase} account equity is invalid")
        self._validate_identifier(bundle.account.cursor, f"{phase}.account.cursor")
        self._validate_timestamp(bundle.account.observed_at, f"{phase}.account.observed_at", now=now)

        fills_by_id: dict[str, CanaryFillFact] = {}
        for fill in bundle.fills:
            self._validate_common_identity(fill, plan, phase=phase)
            self._validate_identifier(fill.fill_id, f"{phase}.fill_id")
            self._validate_identifier(fill.order_id, f"{phase}.fill.order_id")
            self._validate_identifier(fill.broker_order_id, f"{phase}.fill.broker_order_id")
            self._validate_identifier(fill.client_order_id, f"{phase}.fill.client_order_id")
            self._validate_identifier(fill.cursor, f"{phase}.fill.cursor")
            self._validate_fact_digest(fill, phase=phase)
            if fill.cursor != bundle.reconciliation.cursor:
                raise TestnetCanaryError(f"{phase} fill cursor mismatch")
            if expected_client_order_id and fill.client_order_id != expected_client_order_id:
                raise TestnetCanaryError(f"{phase} fill client lineage mismatch")
            if expected_broker_order_id and fill.broker_order_id != expected_broker_order_id:
                raise TestnetCanaryError(f"{phase} fill broker lineage mismatch")
            if fill.instrument_id != plan.instrument_id:
                raise TestnetCanaryError(f"{phase} fill instrument identity mismatch")
            if fill.order_id not in {str(plan.canary_id) + ":entry", order_id} and fill.order_id != order_id:
                raise TestnetCanaryError(f"{phase} fill order identity mismatch")
            if (
                fill.side not in {"buy", "sell"}
                or not fill.quantity.is_finite()
                or not fill.price.is_finite()
                or fill.quantity <= 0
                or fill.price <= 0
            ):
                raise TestnetCanaryError(f"{phase} fill values are invalid")
            if phase == "entry" and fill.order_id == order_id and fill.side != plan.direction:
                raise TestnetCanaryError(f"{phase} fill side identity mismatch")
            if phase == "final" and fill.order_id == order_id and fill.side == plan.direction:
                raise TestnetCanaryError(f"{phase} close fill side identity mismatch")
            self._validate_timestamp(fill.occurred_at, f"{phase}.fill.occurred_at", now=now)
            existing = fills_by_id.get(fill.fill_id)
            if existing is not None and existing != fill:
                raise TestnetCanaryError(f"{phase} fill id conflict")
            fills_by_id[fill.fill_id] = fill
        if not fills_by_id:
            raise TestnetCanaryError(f"{phase} fill fact is missing")

        fees_by_id: dict[str, CanaryFeeFact] = {}
        fee_fill_ids: set[str] = set()
        for fee in bundle.fees:
            self._validate_common_identity(fee, plan, phase=phase)
            self._validate_identifier(fee.fee_id, f"{phase}.fee_id")
            self._validate_identifier(fee.cursor, f"{phase}.fee.cursor")
            self._validate_fact_digest(fee, phase=phase)
            if fee.cursor != bundle.reconciliation.cursor:
                raise TestnetCanaryError(f"{phase} fee cursor mismatch")
            if (
                fee.currency != "USD"
                or not fee.amount_usd.is_finite()
                or fee.amount_usd < 0
                or not fee.fill_id
            ):
                raise TestnetCanaryError(f"{phase} fee fact is invalid")
            self._validate_identifier(fee.fill_id, f"{phase}.fee.fill_id")
            self._validate_timestamp(fee.occurred_at, f"{phase}.fee.occurred_at", now=now)
            if fee.fee_source != "actual_fill" or fee.fee_state != "actual":
                raise TestnetCanaryError(f"{phase} fee is not an actual fill fee")
            if fee.fill_id not in fills_by_id:
                raise TestnetCanaryError(f"{phase} fee is not bound to a fill")
            existing = fees_by_id.get(fee.fee_id)
            if existing is not None and existing != fee:
                raise TestnetCanaryError(f"{phase} fee id conflict")
            fees_by_id[fee.fee_id] = fee
            fee_fill_ids.add(fee.fill_id)
        if fee_fill_ids != set(fills_by_id):
            raise TestnetCanaryError(f"{phase} actual fee coverage is incomplete")
        actual_fee_total = sum((fee.amount_usd for fee in fees_by_id.values()), Decimal("0"))
        fee_budget = (
            plan.fee_reserve_usd
            + plan.entry_fee_estimate_usd
            + plan.exit_fee_estimate_usd
            + plan.explicit_loss_buffer_usd
        )
        if actual_fee_total > fee_budget:
            raise TestnetCanaryError(f"{phase} actual fees exceed the canary fee budget")

        for position in bundle.positions:
            self._validate_common_identity(position, plan, phase=phase)
            self._validate_fact_digest(position, phase=phase)
            if position.instrument_id != plan.instrument_id or not position.signed_quantity.is_finite():
                raise TestnetCanaryError(f"{phase} position instrument identity mismatch")
            if position.cursor != bundle.reconciliation.cursor:
                raise TestnetCanaryError(f"{phase} position cursor mismatch")
            self._validate_timestamp(position.observed_at, f"{phase}.position.observed_at", now=now)
        signed_position = sum(
            (position.signed_quantity for position in bundle.positions),
            Decimal("0"),
        )
        if (
            not bundle.reconciliation.signed_position_quantity.is_finite()
            or signed_position != bundle.reconciliation.signed_position_quantity
        ):
            raise TestnetCanaryError(f"{phase} position reconciliation mismatch")
        self._validate_fact_digest(bundle.account, phase=phase)
        self._validate_fact_digest(bundle.reconciliation, phase=phase)
        for order in bundle.open_orders:
            self._validate_identifier(order, f"{phase}.open_order_id")
        if tuple(bundle.open_orders) != tuple(bundle.reconciliation.open_order_ids):
            raise TestnetCanaryError(f"{phase} open-order reconciliation mismatch")

        fill_quantity = sum((fill.quantity for fill in fills_by_id.values()), Decimal("0"))
        if phase == "entry":
            if fill_quantity <= 0 or fill_quantity > plan.quantity:
                raise TestnetCanaryError("entry fill quantity exceeds the canary plan")
            expected_signed = fill_quantity if plan.direction == "buy" else -fill_quantity
            if signed_position != expected_signed:
                raise TestnetCanaryError("entry position does not match owned fill quantity")
        if phase == "final":
            close_fills = [fill for fill in fills_by_id.values() if fill.order_id == order_id]
            if not close_fills:
                raise TestnetCanaryError("final reconciliation has no causal close fill")
            close_quantity = sum((fill.quantity for fill in close_fills), Decimal("0"))
            if expected_close_quantity is None or close_quantity != expected_close_quantity:
                raise TestnetCanaryError("final close fill quantity does not match owned position")

        normalized_bundle = CanaryFactBundle(
            fills=tuple(fills_by_id.values()),
            fees=tuple(fees_by_id.values()),
            account=bundle.account,
            positions=bundle.positions,
            open_orders=tuple(bundle.open_orders),
            reconciliation=bundle.reconciliation,
        )
        return self._safe_bundle(normalized_bundle), signed_position

    @staticmethod
    def _validate_common_identity(value: object, plan: TestnetCanaryPlan, *, phase: str) -> None:
        for field, expected in (
            ("account_fingerprint", plan.account_fingerprint),
            ("runtime_id", plan.runtime_id),
            ("release_sha", plan.release_sha),
            ("capability_revision", plan.capability_revision),
            ("transport_state", "external_testnet"),
        ):
            if getattr(value, field, None) != expected:
                raise TestnetCanaryError(f"{phase} fact {field} identity mismatch")

    def _validate_identifier(self, value: object, field: str) -> None:
        text = str(value or "").strip()
        if self.canary._safe_identifier(text) != text:
            raise TestnetCanaryError(f"{field} identity is invalid")

    @staticmethod
    def _receipt_lineage(state: dict[str, Any], operation: str, field: str) -> str:
        rows = state.get("orders") or []
        for row in reversed(rows):
            if row.get("operation") == operation:
                return str(row.get(field) or "")
        return ""

    @staticmethod
    def _validate_fact_digest(value: object, *, phase: str) -> None:
        digest = str(getattr(value, "fact_digest", "") or "")
        if digest != canary_fact_digest(value):
            raise TestnetCanaryError(f"{phase} fact digest is invalid")
        raw_digest = getattr(value, "raw_payload_digest", None)
        if raw_digest is not None and (
            not isinstance(raw_digest, str)
            or not raw_digest.startswith("sha256:")
            or len(raw_digest) != 71
        ):
            raise TestnetCanaryError(f"{phase} raw payload digest is invalid")

    @staticmethod
    def _validate_timestamp(value: str, field: str, *, now: datetime) -> None:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise TestnetCanaryError(f"{field} timestamp is invalid") from exc
        if parsed.tzinfo is None:
            raise TestnetCanaryError(f"{field} timestamp must include a timezone")
        observed = parsed.astimezone(timezone.utc)
        current = now.astimezone(timezone.utc)
        if observed > current:
            raise TestnetCanaryError(f"{field} timestamp is in the future")
        if current - observed > MAX_FACT_AGE:
            raise TestnetCanaryError(f"{field} timestamp is stale")

    @staticmethod
    def _safe_bundle(bundle: CanaryFactBundle) -> dict[str, Any]:
        return {
            "schema_version": FACT_SCHEMA,
            "fills": [
                {
                    "fill_id": fill.fill_id,
                    "order_id": fill.order_id,
                    "instrument_id": fill.instrument_id,
                    "side": fill.side,
                    "price": str(fill.price),
                    "quantity": str(fill.quantity),
                    "occurred_at": fill.occurred_at,
                    "broker_order_id": fill.broker_order_id,
                    "client_order_id": fill.client_order_id,
                    "cursor": fill.cursor,
                    "fact_digest": fill.fact_digest,
                    "raw_payload_digest": fill.raw_payload_digest,
                }
                for fill in bundle.fills
            ],
            "fees": [
                {
                    "fee_id": fee.fee_id,
                    "fill_id": fee.fill_id,
                    "amount_usd": str(fee.amount_usd),
                    "currency": fee.currency,
                    "occurred_at": fee.occurred_at,
                    "cursor": fee.cursor,
                    "fee_source": fee.fee_source,
                    "fee_state": fee.fee_state,
                    "fact_digest": fee.fact_digest,
                    "raw_payload_digest": fee.raw_payload_digest,
                }
                for fee in bundle.fees
            ],
            "account": {
                "account_fingerprint": bundle.account.account_fingerprint,
                "equity_usd": str(bundle.account.equity_usd),
                "cursor": bundle.account.cursor,
                "observed_at": bundle.account.observed_at,
                "fact_digest": bundle.account.fact_digest,
                "raw_payload_digest": bundle.account.raw_payload_digest,
            },
            "positions": [
                {
                    "instrument_id": position.instrument_id,
                    "signed_quantity": str(position.signed_quantity),
                    "cursor": position.cursor,
                    "observed_at": position.observed_at,
                    "fact_digest": position.fact_digest,
                    "raw_payload_digest": position.raw_payload_digest,
                }
                for position in bundle.positions
            ],
            "open_orders": list(bundle.open_orders),
            "reconciliation": {
                "coherent": bundle.reconciliation.coherent,
                "freshness": bundle.reconciliation.freshness,
                "cursor": bundle.reconciliation.cursor,
                "open_order_ids": list(bundle.reconciliation.open_order_ids),
                "signed_position_quantity": str(bundle.reconciliation.signed_position_quantity),
                "observed_at": bundle.reconciliation.observed_at,
                "canonical_schema": bundle.reconciliation.canonical_schema,
                "evidence_digest": bundle.reconciliation.evidence_digest,
                "fact_digest": bundle.reconciliation.fact_digest,
                "raw_payload_digest": bundle.reconciliation.raw_payload_digest,
            },
        }

    @staticmethod
    def _evidence_digest(value: dict[str, Any]) -> str:
        payload = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _block(self, state: dict[str, Any], reason: str, *, timestamp: str) -> None:
        self.canary._block(state, reason, timestamp=timestamp)
        self.canary._save(state)

    def _fail(self, state: dict[str, Any], reason: str, *, timestamp: str) -> dict[str, Any]:
        self._block(state, reason, timestamp=timestamp)
        raise TestnetCanaryError(reason)
