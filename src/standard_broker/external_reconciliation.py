"""Provider-neutral external reconciliation and evidence envelopes."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
import re
from typing import Generic, TypeVar

from .account import AccountSnapshot, PositionFact
from .errors import RuntimeBoundaryError
from .external_host import (
    ExternalCanonicalReceipt,
    ExternalFactEnvelope,
    ExternalRuntimeIdentity,
    digest_canonical,
)
from .fees import FeeEvent, FeeScheduleSnapshot, FundingPayment
from .models import AccountScope, BrokerEnvironment, SignerKind
from .orders import OrderFill, OrderReceipt, OrderState


_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
T = TypeVar("T")


class ExternalCursorKind(str, Enum):
    WATERMARK = "watermark"
    CURSOR = "cursor"


@dataclass(frozen=True)
class ExternalReconciliationCursor:
    """One Broker-owned position in the observation stream."""

    kind: ExternalCursorKind
    value: int | str
    observed_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ExternalCursorKind):
            raise TypeError("cursor kind must be an ExternalCursorKind")
        if self.kind is ExternalCursorKind.WATERMARK:
            if type(self.value) is not int or self.value < 0:
                raise ValueError("watermark cursor must be a non-negative integer")
        elif not isinstance(self.value, str) or not self.value.strip():
            raise ValueError("opaque cursor must be a non-empty string")
        if self.observed_at.tzinfo is None:
            raise ValueError("cursor observation time must include timezone information")


@dataclass(frozen=True)
class ExternalReconciliationIdentity:
    """Identity shared by every fact admitted to one snapshot."""

    broker_id: str
    environment: BrokerEnvironment
    account_scope: AccountScope
    account_address: str
    signer_kind: SignerKind
    execution_scope: str
    lifecycle_id: str
    release_sha: str
    runtime_identity: ExternalRuntimeIdentity
    capability_revision: str

    def __post_init__(self) -> None:
        for name in (
            "broker_id",
            "account_address",
            "execution_scope",
            "lifecycle_id",
            "release_sha",
            "capability_revision",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required")
        if not re.fullmatch(r"[0-9a-f]{40}", self.release_sha):
            raise ValueError("external reconciliation identity requires a full release SHA")
        if not isinstance(self.environment, BrokerEnvironment):
            raise TypeError("environment must be a BrokerEnvironment")
        if not isinstance(self.account_scope, AccountScope):
            raise TypeError("account_scope must be an AccountScope")
        if not isinstance(self.signer_kind, SignerKind):
            raise TypeError("signer_kind must be a SignerKind")
        if not isinstance(self.runtime_identity, ExternalRuntimeIdentity):
            raise TypeError("runtime_identity must be an ExternalRuntimeIdentity")


@dataclass(frozen=True)
class ExternalReconciliationObservation(Generic[T]):
    """One canonical fact plus its cursor and transport receipt evidence."""

    fact: ExternalFactEnvelope[T]
    cursor: ExternalReconciliationCursor
    receipt_digest: str
    receipt: ExternalCanonicalReceipt | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.fact, ExternalFactEnvelope):
            raise TypeError("reconciliation observation requires an ExternalFactEnvelope")
        if not isinstance(self.cursor, ExternalReconciliationCursor):
            raise TypeError("reconciliation observation requires an external cursor")
        if not _DIGEST.fullmatch(self.receipt_digest):
            raise ValueError("receipt_digest must be a sha256 digest")
        if not _DIGEST.fullmatch(self.fact.request_digest):
            raise ValueError("fact request_digest must be a sha256 digest")
        if self.fact.fact_digest != _expected_fact_digest(self.fact):
            raise ValueError("fact_digest is not bound to the canonical fact payload")
        if self.receipt is None:
            if self.receipt_digest != self.fact.fact_digest:
                raise ValueError("receipt_digest must bind to the fact when no transport receipt is supplied")
        elif (
            self.receipt.receipt_digest != self.receipt_digest
            or self.receipt.broker_id != self.fact.broker_id
            or self.receipt.environment is not self.fact.environment
            or self.receipt.account_address != self.fact.account_address
            or self.receipt.lifecycle_id != self.fact.lifecycle_id
            or self.receipt.release_sha != self.fact.release_sha
            or self.receipt.runtime_identity != self.fact.runtime_identity
            or self.receipt.capability_revision != self.fact.capability_revision
        ):
            raise ValueError("transport receipt does not bind to the canonical fact identity")
        if self.fact.raw_payload_digest is not None and not _DIGEST.fullmatch(
            self.fact.raw_payload_digest
        ):
            raise ValueError("fact raw_payload_digest must be a sha256 digest")

    @property
    def request_digest(self) -> str:
        return self.fact.request_digest

    @property
    def raw_payload_digest(self) -> str | None:
        return self.fact.raw_payload_digest


class ExternalReconciliationOutcome(str, Enum):
    COHERENT = "coherent"
    INCOMPLETE = "incomplete"
    STALE = "stale"
    DRIFT = "drift"
    UNKNOWN = "unknown"
    IDENTITY_CONFLICT = "identity_conflict"


_Observation = ExternalReconciliationObservation[object]


@dataclass(frozen=True)
class ExternalReconciliationSnapshot:
    """A non-secret, cursor-bound external Broker reconciliation result.

    This is a contract and evidence value, not a scheduler or a persistence
    owner.  A non-coherent snapshot is retained as an explicit non-pass result;
    callers that need an execution decision must call ``require_coherent``.
    """

    identity: ExternalReconciliationIdentity | None
    cursor: ExternalReconciliationCursor | None
    observed_at: datetime | None
    account: ExternalReconciliationObservation[AccountSnapshot] | None
    positions: ExternalReconciliationObservation[tuple[PositionFact, ...]] | None
    open_orders: ExternalReconciliationObservation[tuple[OrderReceipt, ...]] | None
    fills: ExternalReconciliationObservation[tuple[OrderFill, ...]] | None
    fees: ExternalReconciliationObservation[tuple[FeeEvent | FeeScheduleSnapshot, ...]] | None
    funding: ExternalReconciliationObservation[tuple[FundingPayment, ...]] | None
    funding_applicable: bool
    outcome: ExternalReconciliationOutcome
    failure_reasons: tuple[str, ...]
    request_digests: tuple[str, ...]
    receipt_digests: tuple[str, ...]
    raw_payload_digests: tuple[str, ...]
    fact_digests: tuple[str, ...]
    account_ids: tuple[str, ...]
    order_ids: tuple[str, ...]
    fill_ids: tuple[str, ...]
    fee_ids: tuple[str, ...]
    funding_ids: tuple[str, ...]
    position_ids: tuple[str, ...]
    evidence_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, ExternalReconciliationOutcome):
            raise TypeError("outcome must be an ExternalReconciliationOutcome")
        if type(self.funding_applicable) is not bool:
            raise TypeError("funding_applicable must be a bool")
        if self.observed_at is not None and self.observed_at.tzinfo is None:
            raise ValueError("snapshot observation time must include timezone information")
        if self.outcome is ExternalReconciliationOutcome.COHERENT:
            if self.failure_reasons:
                raise ValueError("a coherent snapshot cannot contain failure reasons")
            if self.identity is None or self.cursor is None or self.observed_at is None:
                raise ValueError("a coherent snapshot requires identity, cursor, and observation time")
        elif not self.failure_reasons:
            raise ValueError("a non-coherent snapshot requires explicit failure reasons")
        if not isinstance(self.failure_reasons, tuple) or not all(
            isinstance(reason, str) and reason.strip() for reason in self.failure_reasons
        ):
            raise ValueError("failure_reasons must be a tuple of non-empty strings")
        for name in (
            "request_digests",
            "receipt_digests",
            "raw_payload_digests",
            "fact_digests",
        ):
            values = getattr(self, name)
            if not isinstance(values, tuple) or not all(_DIGEST.fullmatch(value) for value in values):
                raise ValueError(f"{name} must contain sha256 digests")
        for name in (
            "account_ids",
            "order_ids",
            "fill_ids",
            "fee_ids",
            "funding_ids",
            "position_ids",
        ):
            values = getattr(self, name)
            if not isinstance(values, tuple) or not all(
                isinstance(value, str) and value.strip() for value in values
            ):
                raise ValueError(f"{name} must contain non-empty IDs")
        if not _DIGEST.fullmatch(self.evidence_digest):
            raise ValueError("evidence_digest must be a sha256 digest")
        observations = _snapshot_observations(self)
        for name, observation in observations:
            _validate_observation_data(name, observation)
        if observations:
            first = observations[0][1]
            expected_identity = _identity(first.fact)
            if self.identity != expected_identity:
                raise ValueError("snapshot identity is not bound to its first canonical fact")
            if self.cursor != first.cursor or self.observed_at != first.cursor.observed_at:
                raise ValueError("snapshot cursor or observation time is not bound to its facts")
            computed_failures: list[str] = []
            if any(_identity(item.fact) != expected_identity for _, item in observations[1:]):
                computed_failures.append("identity_conflict")
            if any(item.cursor != first.cursor for _, item in observations[1:]):
                computed_failures.append("cursor_conflict")
            _append_type_failures(
                computed_failures,
                self.account,
                self.positions,
                self.open_orders,
                self.fills,
                self.fees,
                self.funding,
            )
            _append_payload_identity_failures(
                computed_failures,
                self.account,
                self.positions,
                self.open_orders,
                self.fills,
                self.fees,
                self.funding,
            )
            if self.open_orders is not None and any(
                receipt.state is OrderState.UNKNOWN for receipt in self.open_orders.fact.data
            ):
                computed_failures.append("unknown_order_state")
            if not set(_unique(computed_failures)).issubset(self.failure_reasons):
                raise ValueError("snapshot failure reasons are not bound to its facts")
        expected = _snapshot_projection(self)
        for name, actual, projected in expected:
            if actual != projected:
                raise ValueError(f"snapshot {name} projection is not bound to its facts")
        if self.outcome is not _outcome(self.failure_reasons):
            raise ValueError("snapshot outcome is not bound to its failure reasons")
        if self.evidence_digest != _evidence_digest(
            identity=self.identity,
            cursor=self.cursor,
            outcome=self.outcome,
            failure_reasons=self.failure_reasons,
            request_digests=self.request_digests,
            receipt_digests=self.receipt_digests,
            raw_payload_digests=self.raw_payload_digests,
            fact_digests=self.fact_digests,
            account_ids=self.account_ids,
            order_ids=self.order_ids,
            fill_ids=self.fill_ids,
            fee_ids=self.fee_ids,
            funding_ids=self.funding_ids,
            position_ids=self.position_ids,
        ):
            raise ValueError("evidence_digest is not bound to the snapshot projection")

    @property
    def passed(self) -> bool:
        return self.outcome is ExternalReconciliationOutcome.COHERENT

    def verify_integrity(self) -> None:
        """Re-run the immutable contract checks for an evidence consumer."""

        self.__post_init__()

    def require_coherent(self) -> "ExternalReconciliationSnapshot":
        """Fail closed when a caller attempts to use a non-pass snapshot."""

        if not self.passed:
            raise RuntimeBoundaryError(
                "external_reconciliation_not_coherent",
                ",".join(self.failure_reasons),
            )
        return self

    @classmethod
    def assemble(
        cls,
        *,
        account: ExternalReconciliationObservation[AccountSnapshot] | None,
        positions: ExternalReconciliationObservation[tuple[PositionFact, ...]] | None,
        open_orders: ExternalReconciliationObservation[tuple[OrderReceipt, ...]] | None,
        fills: ExternalReconciliationObservation[tuple[OrderFill, ...]] | None,
        fees: ExternalReconciliationObservation[tuple[FeeEvent | FeeScheduleSnapshot, ...]] | None,
        funding: ExternalReconciliationObservation[tuple[FundingPayment, ...]] | None,
        funding_applicable: bool,
        now: datetime | None = None,
        stale_after: timedelta | None = None,
        max_observation_skew: timedelta | None = None,
    ) -> "ExternalReconciliationSnapshot":
        observations: tuple[_Observation, ...] = tuple(
            item
            for item in (account, positions, open_orders, fills, fees, funding)
            if item is not None
        )
        for name, observation in _named_observations(account, positions, open_orders, fills, fees, funding):
            _validate_observation_data(name, observation)
        failures: list[str] = []
        required = {
            "account": account,
            "positions": positions,
            "open_orders": open_orders,
            "fills": fills,
            "fees": fees,
        }
        for name, value in required.items():
            if value is None:
                failures.append(f"missing_{name}")
        if funding_applicable and funding is None:
            failures.append("missing_funding")

        cursor = observations[0].cursor if observations else None
        if any(item.cursor != cursor for item in observations[1:]):
            failures.append("cursor_conflict")

        identities = tuple(_identity(item.fact) for item in observations)
        identity = identities[0] if identities else None
        if any(item != identity for item in identities[1:]):
            failures.append("identity_conflict")

        if cursor is not None:
            observed_at = cursor.observed_at
            if now is not None:
                if now.tzinfo is None:
                    raise ValueError("now must include timezone information")
                age = now - observed_at
                if age.total_seconds() < 0:
                    failures.append("observation_time_drift")
                elif stale_after is not None and age > stale_after:
                    failures.append("stale_observation")
            if max_observation_skew is not None:
                if max_observation_skew.total_seconds() < 0:
                    raise ValueError("max_observation_skew must be non-negative")
                if any(
                    abs(item.fact.provenance.received_at - observed_at) > max_observation_skew
                    for item in observations
                ):
                    failures.append("observation_time_drift")
        else:
            observed_at = None

        _append_type_failures(failures, account, positions, open_orders, fills, fees, funding)
        _append_payload_identity_failures(
            failures,
            account,
            positions,
            open_orders,
            fills,
            fees,
            funding,
        )
        if open_orders is not None and any(
            receipt.state is OrderState.UNKNOWN for receipt in open_orders.fact.data
        ):
            failures.append("unknown_order_state")

        failure_reasons = _unique(failures)
        outcome = _outcome(failure_reasons)
        request_digests = _unique(item.request_digest for item in observations)
        receipt_digests = _unique(item.receipt_digest for item in observations)
        raw_payload_digests = _unique(
            item.raw_payload_digest
            for item in observations
            if item.raw_payload_digest is not None
        )
        fact_digests = _unique(item.fact.fact_digest for item in observations)
        order_ids = _order_ids(open_orders, fills)
        fill_ids = _fill_ids(fills)
        fee_ids = _fee_ids(fees)
        funding_ids = _funding_ids(funding)
        account_ids = _account_ids(account)
        position_ids = _position_ids(account, positions)
        evidence_digest = _evidence_digest(
            identity=identity,
            cursor=cursor,
            outcome=outcome,
            failure_reasons=failure_reasons,
            request_digests=request_digests,
            receipt_digests=receipt_digests,
            raw_payload_digests=raw_payload_digests,
            fact_digests=fact_digests,
            account_ids=account_ids,
            order_ids=order_ids,
            fill_ids=fill_ids,
            fee_ids=fee_ids,
            funding_ids=funding_ids,
            position_ids=position_ids,
        )
        return cls(
            identity=identity,
            cursor=cursor,
            observed_at=observed_at,
            account=account,
            positions=positions,
            open_orders=open_orders,
            fills=fills,
            fees=fees,
            funding=funding,
            funding_applicable=funding_applicable,
            outcome=outcome,
            failure_reasons=failure_reasons,
            request_digests=request_digests,
            receipt_digests=receipt_digests,
            raw_payload_digests=raw_payload_digests,
            fact_digests=fact_digests,
            account_ids=account_ids,
            order_ids=order_ids,
            fill_ids=fill_ids,
            fee_ids=fee_ids,
            funding_ids=funding_ids,
            position_ids=position_ids,
            evidence_digest=evidence_digest,
        )


def _identity(fact: ExternalFactEnvelope[object]) -> ExternalReconciliationIdentity:
    return ExternalReconciliationIdentity(
        broker_id=fact.broker_id,
        environment=fact.environment,
        account_scope=fact.account_scope,
        account_address=fact.account_address,
        signer_kind=fact.signer_kind,
        execution_scope=fact.execution_scope,
        lifecycle_id=fact.lifecycle_id,
        release_sha=fact.release_sha,
        runtime_identity=fact.runtime_identity,
        capability_revision=fact.capability_revision,
    )


def _expected_fact_digest(fact: ExternalFactEnvelope[object]) -> str:
    return digest_canonical(
        {
            "fact_type": fact.fact_type,
            "data": fact.data,
            "request_digest": fact.request_digest,
            "raw_payload_digest": fact.raw_payload_digest,
            "broker_id": fact.broker_id,
            "environment": fact.environment,
            "account_scope": fact.account_scope,
            "account_address": fact.account_address,
            "signer_kind": fact.signer_kind,
            "execution_scope": fact.execution_scope,
            "lifecycle_id": fact.lifecycle_id,
            "release_sha": fact.release_sha,
            "runtime_identity": fact.runtime_identity,
            "capability_revision": fact.capability_revision,
            "provenance": fact.provenance,
        }
    )


def _named_observations(
    account: _Observation | None,
    positions: _Observation | None,
    open_orders: _Observation | None,
    fills: _Observation | None,
    fees: _Observation | None,
    funding: _Observation | None,
) -> tuple[tuple[str, _Observation], ...]:
    return tuple(
        (name, observation)
        for name, observation in (
            ("account", account),
            ("positions", positions),
            ("open_orders", open_orders),
            ("fills", fills),
            ("fees", fees),
            ("funding", funding),
        )
        if observation is not None
    )


def _snapshot_observations(
    snapshot: ExternalReconciliationSnapshot,
) -> tuple[tuple[str, _Observation], ...]:
    return _named_observations(
        snapshot.account,
        snapshot.positions,
        snapshot.open_orders,
        snapshot.fills,
        snapshot.fees,
        snapshot.funding,
    )


def _validate_observation_data(name: str, observation: _Observation) -> None:
    data = observation.fact.data
    if name == "account":
        if not isinstance(data, AccountSnapshot):
            raise RuntimeBoundaryError(
                "external_reconciliation_fact_invalid",
                "account observation is not a canonical AccountSnapshot",
            )
        return
    expected_item = {
        "positions": PositionFact,
        "open_orders": OrderReceipt,
        "fills": OrderFill,
        "fees": (FeeEvent, FeeScheduleSnapshot),
        "funding": FundingPayment,
    }[name]
    if not isinstance(data, tuple) or not all(isinstance(item, expected_item) for item in data):
        raise RuntimeBoundaryError(
            "external_reconciliation_fact_invalid",
            f"{name} observation contains a non-canonical fact",
        )


def _snapshot_projection(snapshot: ExternalReconciliationSnapshot):
    observations = _snapshot_observations(snapshot)
    first = observations[0][1] if observations else None
    return (
        ("identity", snapshot.identity, _identity(first.fact) if first is not None else None),
        ("cursor", snapshot.cursor, first.cursor if first is not None else None),
        ("observed_at", snapshot.observed_at, first.cursor.observed_at if first is not None else None),
        ("request_digests", snapshot.request_digests, _unique(item.request_digest for _, item in observations)),
        ("receipt_digests", snapshot.receipt_digests, _unique(item.receipt_digest for _, item in observations)),
        (
            "raw_payload_digests",
            snapshot.raw_payload_digests,
            _unique(item.raw_payload_digest for _, item in observations if item.raw_payload_digest is not None),
        ),
        ("fact_digests", snapshot.fact_digests, _unique(item.fact.fact_digest for _, item in observations)),
        ("account_ids", snapshot.account_ids, _account_ids(snapshot.account)),
        ("order_ids", snapshot.order_ids, _order_ids(snapshot.open_orders, snapshot.fills)),
        ("fill_ids", snapshot.fill_ids, _fill_ids(snapshot.fills)),
        ("fee_ids", snapshot.fee_ids, _fee_ids(snapshot.fees)),
        ("funding_ids", snapshot.funding_ids, _funding_ids(snapshot.funding)),
        ("position_ids", snapshot.position_ids, _position_ids(snapshot.account, snapshot.positions)),
    )


def _evidence_digest(
    *,
    identity,
    cursor,
    outcome,
    failure_reasons,
    request_digests,
    receipt_digests,
    raw_payload_digests,
    fact_digests,
    account_ids,
    order_ids,
    fill_ids,
    fee_ids,
    funding_ids,
    position_ids,
) -> str:
    return digest_canonical(
        {
            "identity": identity,
            "cursor": cursor,
            "outcome": outcome,
            "failure_reasons": failure_reasons,
            "request_digests": request_digests,
            "receipt_digests": receipt_digests,
            "raw_payload_digests": raw_payload_digests,
            "fact_digests": fact_digests,
            "account_ids": account_ids,
            "order_ids": order_ids,
            "fill_ids": fill_ids,
            "fee_ids": fee_ids,
            "funding_ids": funding_ids,
            "position_ids": position_ids,
        }
    )


def _append_type_failures(
    failures: list[str],
    account: _Observation | None,
    positions: _Observation | None,
    open_orders: _Observation | None,
    fills: _Observation | None,
    fees: _Observation | None,
    funding: _Observation | None,
) -> None:
    expected = (
        ("account", account, AccountSnapshot, None),
        ("positions", positions, tuple, PositionFact),
        ("open_orders", open_orders, tuple, OrderReceipt),
        ("fills", fills, tuple, OrderFill),
        ("fees", fees, tuple, (FeeEvent, FeeScheduleSnapshot)),
        ("funding", funding, tuple, FundingPayment),
    )
    for name, observation, expected_type, item_type in expected:
        if observation is None:
            continue
        data = observation.fact.data
        if not isinstance(data, expected_type) or (
            item_type is not None
            and (not isinstance(data, tuple) or not all(isinstance(item, item_type) for item in data))
        ):
            failures.append(f"incomplete_{name}")
        elif name == "account" and not data.observation_id:
            failures.append("incomplete_account_identity")


def _outcome(failures: tuple[str, ...]) -> ExternalReconciliationOutcome:
    if any(reason == "identity_conflict" or reason.startswith("payload_identity_") for reason in failures):
        return ExternalReconciliationOutcome.IDENTITY_CONFLICT
    if any(reason == "cursor_conflict" for reason in failures):
        return ExternalReconciliationOutcome.DRIFT
    if any(reason.startswith("missing_") or reason.startswith("incomplete_") for reason in failures):
        return ExternalReconciliationOutcome.INCOMPLETE
    if any(reason == "unknown_order_state" for reason in failures):
        return ExternalReconciliationOutcome.UNKNOWN
    if any(reason == "stale_observation" for reason in failures):
        return ExternalReconciliationOutcome.STALE
    if failures:
        return ExternalReconciliationOutcome.DRIFT
    return ExternalReconciliationOutcome.COHERENT


def _append_payload_identity_failures(
    failures: list[str],
    account: _Observation | None,
    positions: _Observation | None,
    open_orders: _Observation | None,
    fills: _Observation | None,
    fees: _Observation | None,
    funding: _Observation | None,
) -> None:
    for observation in (account, positions, open_orders, fills, fees, funding):
        if observation is None:
            continue
        expected = _identity(observation.fact)
        data = observation.fact.data
        values = (
            (data, *data.positions)
            if isinstance(data, AccountSnapshot)
            else data
            if isinstance(data, tuple)
            else (data,)
        )
        for value in values:
            if isinstance(value, AccountSnapshot):
                valid = (
                    value.broker_id == expected.broker_id
                    and value.environment is expected.environment
                    and value.account_address == expected.account_address
                    and value.provenance == observation.fact.provenance
                )
            elif isinstance(value, (PositionFact, FeeEvent)):
                valid = (
                    value.broker_id == expected.broker_id
                    and value.environment is expected.environment
                    and _same_provenance_identity(value.provenance, observation.fact.provenance)
                )
            elif isinstance(value, FeeScheduleSnapshot):
                valid = (
                    value.broker_id == expected.broker_id
                    and value.environment is expected.environment
                    and _same_provenance_identity(value.provenance, observation.fact.provenance)
                )
            elif isinstance(value, OrderReceipt):
                valid = (
                    value.broker_id == expected.broker_id
                    and value.environment is expected.environment
                    and value.account_address == expected.account_address
                    and value.lifecycle_id == expected.lifecycle_id
                    and value.release_sha == expected.release_sha
                    and value.provenance == observation.fact.provenance
                )
            elif isinstance(value, OrderFill):
                valid = (
                    value.environment is expected.environment
                    and value.account_address == expected.account_address
                    and value.lifecycle_id == expected.lifecycle_id
                    and value.release_sha == expected.release_sha
                )
            elif isinstance(value, FundingPayment):
                valid = (
                    value.broker_id == expected.broker_id
                    and value.environment is expected.environment
                    and _same_provenance_identity(value.provenance, observation.fact.provenance)
                    and _same_provenance_identity(value.fee.provenance, observation.fact.provenance)
                )
            else:
                continue
            if not valid:
                failures.append("payload_identity_conflict")
                break


def _same_provenance_identity(actual: object, expected: object) -> bool:
    """Compare stable provenance identity without collapsing observation times."""

    fields = ("source", "execution_scope", "transport_state", "mapping_revision")
    return all(getattr(actual, field, None) == getattr(expected, field, None) for field in fields)


def _unique(values):
    return tuple(dict.fromkeys(value for value in values if value is not None))


def _order_ids(
    open_orders: _Observation | None,
    fills: _Observation | None,
) -> tuple[str, ...]:
    values: list[str] = []
    if open_orders is not None and isinstance(open_orders.fact.data, tuple):
        values.extend(order.order_id for order in open_orders.fact.data if isinstance(order, OrderReceipt))
    if fills is not None and isinstance(fills.fact.data, tuple):
        values.extend(fill.order_id for fill in fills.fact.data if isinstance(fill, OrderFill))
    return tuple(sorted(set(values)))


def _account_ids(account: _Observation | None) -> tuple[str, ...]:
    if account is None or not isinstance(account.fact.data, AccountSnapshot):
        return ()
    return (account.fact.data.observation_id,) if account.fact.data.observation_id else ()


def _fill_ids(fills: _Observation | None) -> tuple[str, ...]:
    if fills is None or not isinstance(fills.fact.data, tuple):
        return ()
    return tuple(sorted({fill.fill_id for fill in fills.fact.data if isinstance(fill, OrderFill)}))


def _fee_ids(fees: _Observation | None) -> tuple[str, ...]:
    if fees is None or not isinstance(fees.fact.data, tuple):
        return ()
    values: list[str] = []
    for fee in fees.fact.data:
        if isinstance(fee, FeeEvent):
            values.append(fee.fee_id)
        elif isinstance(fee, FeeScheduleSnapshot):
            values.append(fee.schedule_identity)
    return tuple(sorted(set(values)))


def _funding_ids(funding: _Observation | None) -> tuple[str, ...]:
    if funding is None or not isinstance(funding.fact.data, tuple):
        return ()
    return tuple(sorted({item.funding_id for item in funding.fact.data if isinstance(item, FundingPayment)}))


def _position_ids(
    account: _Observation | None,
    positions: _Observation | None,
) -> tuple[str, ...]:
    values: list[str] = []
    for observation in (account, positions):
        if observation is None:
            continue
        data = observation.fact.data
        if isinstance(data, AccountSnapshot):
            values.extend(item.observation_id for item in data.positions if item.observation_id)
        elif isinstance(data, tuple):
            values.extend(item.observation_id for item in data if isinstance(item, PositionFact) and item.observation_id)
    return tuple(sorted(set(values)))
