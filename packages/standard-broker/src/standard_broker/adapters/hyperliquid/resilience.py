"""Paper-safe reconciliation, reconnect, deduplication, and retry disposition."""

import math
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from threading import RLock
from types import MappingProxyType
from typing import Protocol

from ...account import AccountSnapshot, PositionFact
from ...errors import BrokerError
from ...fees import FeeEvent, FeeKind, FeeSource, FeeState, FillFact
from ...models import BrokerEnvironment, Provenance
from ...orders import OrderReceipt, OrderState
from ...runtime import BrokerRuntimeSession


class RuntimeConnectionState(str, Enum):
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    DEGRADED = "degraded"
    CLOSED = "closed"


class ObservationSource(str, Enum):
    REST = "rest"
    WEBSOCKET = "websocket"
    REPLAY = "replay"


class TransportDisposition(str, Enum):
    RETRYABLE = "retryable"
    AMBIGUOUS = "ambiguous"
    NON_RETRYABLE = "non_retryable"


_RETRYABLE_REASONS = frozenset(
    {
        "ambiguous_submit",
        "ambiguous_cancel",
        "ambiguous_modify",
        "ambiguous_query",
        "transient",
    }
)


class RateLimitError(BrokerError):
    """A rate-limit condition with an explicit side-effect safety declaration."""

    def __init__(self, *, retry_after_seconds: float, side_effect_free: bool = False) -> None:
        if not math.isfinite(retry_after_seconds) or retry_after_seconds < 0:
            raise ValueError("retry_after_seconds must be finite and non-negative")
        if type(side_effect_free) is not bool:
            raise TypeError("side_effect_free must be a bool")
        self.retry_after_seconds = retry_after_seconds
        self.side_effect_free = side_effect_free
        super().__init__(f"rate_limit: retry after {retry_after_seconds}s")


class RuntimeRecoveryError(BrokerError):
    """Raised when a lifecycle cannot be safely recovered or ingested."""

    def __init__(self, reason_code: str, detail: str) -> None:
        self.reason_code = reason_code
        super().__init__(f"{reason_code}: {detail}")


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    max_delay_seconds: float = 30.0

    def __post_init__(self) -> None:
        if (
            type(self.max_attempts) is not int
            or self.max_attempts < 1
            or not math.isfinite(self.max_delay_seconds)
            or self.max_delay_seconds <= 0
        ):
            raise ValueError("retry policy bounds must be positive")


@dataclass(frozen=True)
class RetryPlan:
    disposition: TransportDisposition
    attempt: int
    retry_allowed: bool
    delay_seconds: float


@dataclass(frozen=True)
class RecoveryDecision:
    order_id: str
    receipt: OrderReceipt
    retry_allowed: bool
    reason: str
    facts: Mapping[str, object]
    event_version: int
    connection_epoch: int
    owner_token: object
    lease_token: object


@dataclass(frozen=True)
class ReconciliationSnapshot:
    """One cursor-bound Paper fact snapshot used before an ambiguous retry."""

    order_id: str
    receipt: OrderReceipt
    fills: tuple[FillFact, ...]
    positions: tuple[PositionFact, ...]
    account: AccountSnapshot
    watermark: int

    def __post_init__(self) -> None:
        if not isinstance(self.order_id, str) or not self.order_id.strip():
            raise ValueError("reconciliation snapshot order_id is required")
        if type(self.watermark) is not int or self.watermark < 0:
            raise ValueError("reconciliation snapshot watermark must be a non-negative integer")


class ReconciliationFacts(Protocol):
    """One cursor-bound canonical fact source required before an ambiguous retry."""

    local_only: bool

    @property
    def runtime_session(self) -> BrokerRuntimeSession: ...

    def query_snapshot(self, order_id: str) -> ReconciliationSnapshot: ...


class OrderLifecyclePort(Protocol):
    """Canonical order lifecycle owner consumed by resilience coordination.

    Receipt quantities, fill aggregation, and terminal state are authoritative
    here. RT-06 only coordinates observation identity, ordering, and recovery.
    """

    local_only: bool

    @property
    def runtime_session(self) -> BrokerRuntimeSession: ...

    def apply_order_update(self, raw: Mapping[str, object]) -> OrderReceipt: ...

    def apply_fill(self, raw: Mapping[str, object]) -> OrderReceipt: ...

    def validate_fill(self, raw: Mapping[str, object]) -> None: ...

    def validate_receipt(self, candidate: OrderReceipt) -> None: ...

    def query(self, order_id: str) -> OrderReceipt: ...

    def transaction(self): ...


def classify_transport_error(error: BaseException) -> TransportDisposition:
    """Classify transport errors without performing a retry."""

    if isinstance(error, RateLimitError):
        return (
            TransportDisposition.RETRYABLE
            if error.side_effect_free
            else TransportDisposition.AMBIGUOUS
        )
    if isinstance(error, (TimeoutError, OSError)):
        return TransportDisposition.AMBIGUOUS
    return TransportDisposition.NON_RETRYABLE


def plan_retry(
    error: BaseException,
    *,
    attempt: int,
    policy: RetryPolicy | None = None,
) -> RetryPlan:
    """Create a bounded retry plan without executing or sleeping."""

    effective_policy = policy or RetryPolicy()
    if type(attempt) is not int or attempt < 1 or attempt > effective_policy.max_attempts:
        raise ValueError("retry attempt must be within the configured bounds")
    disposition = classify_transport_error(error)
    allowed = disposition is TransportDisposition.RETRYABLE and attempt < effective_policy.max_attempts
    delay = (
        min(effective_policy.max_delay_seconds, math.ldexp(1.0, min(max(0, attempt - 1), 1023)))
        if allowed
        else 0.0
    )
    if isinstance(error, RateLimitError) and allowed:
        if error.retry_after_seconds > effective_policy.max_delay_seconds:
            return RetryPlan(disposition, attempt, False, 0.0)
        delay = max(delay, error.retry_after_seconds)
    return RetryPlan(disposition, attempt, allowed, delay)


class HyperliquidRuntimeReconciliationAdapter:
    """Coordinate canonical order lifecycle recovery around reconnect and event streams."""

    _OBSERVATION_STATE_FIELDS = (
        "_seen_order_event_ids",
        "_seen_fill_event_ids",
        "_seen_order_keys",
        "_seen_fill_keys",
        "_seen_fill_aliases",
        "_weak_fill_keys",
        "_weak_hash_shapes",
        "_event_sources",
        "_latest_order_sequence",
        "_latest_order_rank",
        "_latest_order_status",
        "_order_aliases",
        "_order_id_roots",
        "_client_id_roots",
        "_active_order_ids",
        "_active_client_ids",
        "_order_event_keys",
        "_fill_event_keys",
        "_order_event_fingerprints",
        "_fill_event_fingerprints",
        "_fill_key_fingerprints",
        "_fill_alias_fingerprints",
        "_fill_key_hashes",
        "_event_version",
        "_latest_account",
        "_latest_positions",
        "_latest_watermark",
        "_latest_snapshot_fingerprint",
        "_latest_fact_snapshot_fingerprint",
        "_latest_snapshot_kind",
    )

    def __init__(
        self,
        *,
        orders: OrderLifecyclePort,
        account_address: str | None = None,
        session: BrokerRuntimeSession | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        try:
            local_only = orders.local_only
            order_session = orders.runtime_session
        except Exception as error:
            raise RuntimeRecoveryError(
                "runtime_contract_missing",
                "order lifecycle must expose local_only and runtime_session",
            ) from error
        if local_only is not True:
            raise RuntimeRecoveryError(
                "non_local_order_lifecycle",
                "RT-06 reconciliation requires a local-only injected lifecycle",
            )
        bound_session = session or order_session
        if not isinstance(bound_session, BrokerRuntimeSession):
            raise RuntimeRecoveryError(
                "runtime_session_required",
                "reconciliation must bind the immutable Paper Broker Runtime Session",
            )
        if order_session is not bound_session:
            raise RuntimeRecoveryError("runtime_session_mismatch", "order lifecycle and reconciliation sessions differ")
        if (
            bound_session.broker_id != "hyperliquid"
            or bound_session.environment is not BrokerEnvironment.PAPER
            or bound_session.signer.kind.value != "none"
        ):
            raise RuntimeRecoveryError("paper_session_required", "RT-06 requires a Hyperliquid Paper no-signer session")
        if account_address is not None and account_address != bound_session.account.address:
            raise RuntimeRecoveryError("runtime_account_mismatch", "declared account differs from the bound session")
        for method_name in ("apply_order_update", "apply_fill", "validate_receipt", "query"):
            if not callable(getattr(orders, method_name, None)):
                raise RuntimeRecoveryError("runtime_contract_missing", f"order lifecycle missing {method_name}()")
        if not callable(getattr(orders, "validate_fill", None)):
            raise RuntimeRecoveryError("runtime_contract_missing", "order lifecycle missing validate_fill()")
        self._orders = orders
        self._session = bound_session
        self._account_address = bound_session.account.address
        self._lock = RLock()
        self._state = RuntimeConnectionState.CONNECTED
        self._seen_order_event_ids: set[str] = set()
        self._seen_fill_event_ids: set[str] = set()
        self._seen_order_keys: set[str] = set()
        self._seen_fill_keys: set[str] = set()
        self._seen_fill_aliases: set[str] = set()
        self._weak_fill_keys: set[str] = set()
        self._weak_hash_shapes: dict[str, set[str]] = {}
        self._event_sources: dict[str, set[ObservationSource]] = {}
        self._latest_order_sequence: dict[str, int] = {}
        self._latest_order_rank: dict[str, int] = {}
        self._latest_order_status: dict[str, str] = {}
        self._order_aliases: dict[str, str] = {}
        self._order_id_roots: dict[str, str] = {}
        self._client_id_roots: dict[str, str] = {}
        self._active_order_ids: dict[str, str] = {}
        self._active_client_ids: dict[str, str] = {}
        self._order_event_keys: dict[str, str] = {}
        self._fill_event_keys: dict[str, str] = {}
        self._order_event_fingerprints: dict[str, str] = {}
        self._fill_event_fingerprints: dict[str, str] = {}
        self._fill_key_fingerprints: dict[str, str] = {}
        self._fill_alias_fingerprints: dict[str, str] = {}
        self._fill_key_hashes: dict[str, str] = {}
        self._subscriptions: dict[str, Callable[[], object]] = {}
        self._retry_policy = retry_policy or RetryPolicy()
        self._reconciling_snapshot = False
        self._event_version = 0
        self._connection_epoch = 0
        self._owner_token = object()
        self._issued_leases: dict[object, tuple[str, OrderReceipt, bool, str, Mapping[str, object], int, int]] = {}
        self._active_leases: dict[str, object] = {}
        self._latest_account: AccountSnapshot | None = None
        self._latest_positions: tuple[PositionFact, ...] = ()
        self._latest_watermark: int | None = None
        self._latest_snapshot_fingerprint: str | None = None
        self._latest_fact_snapshot_fingerprint: str | None = None
        self._latest_snapshot_kind: str | None = None
        self._snapshot_recovery_dirty = False

    @property
    def state(self) -> RuntimeConnectionState:
        with self._lock:
            return self._state

    @property
    def latest_account(self) -> AccountSnapshot | None:
        with self._lock:
            return self._latest_account

    @property
    def latest_positions(self) -> tuple[PositionFact, ...]:
        with self._lock:
            return self._latest_positions

    @property
    def latest_watermark(self) -> int | None:
        with self._lock:
            return self._latest_watermark

    def disconnect(self, reason: str) -> RuntimeConnectionState:
        with self._lock:
            if self._state is RuntimeConnectionState.CLOSED:
                raise RuntimeRecoveryError("runtime_closed", "closed runtime cannot disconnect")
            self._connection_epoch += 1
            self._state = RuntimeConnectionState.RECONNECTING
            return self._state

    def register_subscription(self, name: str, callback: Callable[[], object]) -> None:
        with self._lock:
            if not name or not callable(callback):
                raise ValueError("subscription name and callback are required")
            self._require_local_callback(callback)
            self._subscriptions[name] = callback

    def reconnect(
        self,
        *,
        resubscribe: Callable[[], object] | None = None,
        snapshot_provider: Callable[[], Mapping[str, object]] | None = None,
    ) -> RuntimeConnectionState:
        with self._lock:
            return self._reconnect(resubscribe=resubscribe, snapshot_provider=snapshot_provider)

    def _reconnect(
        self,
        *,
        resubscribe: Callable[[], object] | None = None,
        snapshot_provider: Callable[[], Mapping[str, object]] | None = None,
    ) -> RuntimeConnectionState:
        if self._state is RuntimeConnectionState.CLOSED:
            raise RuntimeRecoveryError("runtime_closed", "closed runtime cannot reconnect")
        if self._snapshot_recovery_dirty:
            raise RuntimeRecoveryError(
                "reconciliation_rebuild_required",
                "a failed snapshot requires a fresh reconciliation controller before recovery",
            )
        self._connection_epoch += 1
        self._state = RuntimeConnectionState.RECONNECTING
        if snapshot_provider is None:
            raise RuntimeRecoveryError(
                "reconciliation_snapshot_required",
                "reconnect requires an authoritative local snapshot",
            )
        callbacks = list(self._subscriptions.values())
        if resubscribe is not None:
            callbacks.append(resubscribe)
        if callbacks:
            try:
                for callback in callbacks:
                    self._require_local_callback(callback)
                    if callback() is not True:
                        raise RuntimeRecoveryError(
                            "resubscription_unacknowledged",
                            "reconnect subscription did not return an explicit acknowledgement",
                        )
            except Exception as error:
                self._state = RuntimeConnectionState.DEGRADED
                if isinstance(error, RuntimeRecoveryError):
                    raise
                raise RuntimeRecoveryError("resubscription_failed", type(error).__name__) from error
        try:
            self._require_local_callback(snapshot_provider)
            snapshot = snapshot_provider()
            order_events, fill_events, positions, account, watermark = self._validate_snapshot(snapshot)
            self._require_snapshot_watermark(watermark)
            self._require_snapshot_session(account, positions)
            snapshot_fingerprint = self._snapshot_event_fingerprint(order_events, fill_events)
            self._require_snapshot_consistency(watermark, account, positions, snapshot_fingerprint)
            self._apply_authoritative_snapshot(
                order_events=order_events,
                fill_events=fill_events,
                positions=positions,
                account=account,
                watermark=watermark,
                snapshot_fingerprint=snapshot_fingerprint,
            )
        except Exception as error:
            self._state = RuntimeConnectionState.DEGRADED
            if isinstance(error, RuntimeRecoveryError):
                raise
            raise RuntimeRecoveryError("reconciliation_snapshot_failed", type(error).__name__) from error
        self._state = RuntimeConnectionState.CONNECTED
        return self._state

    def close(self) -> RuntimeConnectionState:
        with self._lock:
            self._state = RuntimeConnectionState.CLOSED
            return self._state

    def ingest_order_event(
        self,
        event_id: str,
        raw: Mapping[str, object],
        *,
        source: ObservationSource = ObservationSource.REPLAY,
    ) -> object | None:
        with self._lock:
            return self._ingest_order_event(event_id, raw, source=source)

    def _ingest_order_event(
        self,
        event_id: str,
        raw: Mapping[str, object],
        *,
        source: ObservationSource = ObservationSource.REPLAY,
    ) -> object | None:
        self._require_connected()
        if not isinstance(raw, Mapping):
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError("event_payload_invalid", "order event must be a mapping")
        self._validate_event_id(event_id)
        self._validate_source(source)
        self._validate_order_identity(raw)
        event_key = self._order_key(raw)
        raw_entity_key = self._order_entity_key(raw)
        entity_key = self._resolve_order_entity_key(raw)
        sequence = self._order_sequence(raw)
        status_rank = self._order_status_rank(raw.get("status"))
        if event_key is None or entity_key is None or sequence is None or sequence <= 0:
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError("order_sequence_required", "order event requires oid/cloid and timestamp")
        previous_key = self._order_event_keys.get(event_id) if event_id else None
        fingerprint = event_key
        previous_fingerprint = self._order_event_fingerprints.get(event_id) if event_id else None
        if previous_fingerprint is not None and previous_fingerprint != fingerprint:
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError("order_event_identity_conflict", "event_id was reused for a different order observation")
        if previous_key is not None and previous_key != event_key:
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError("order_event_identity_conflict", "event_id was reused for a different order observation")
        if event_id and previous_key is None:
            self._order_event_keys[event_id] = event_key
            self._order_event_fingerprints[event_id] = fingerprint
        latest = self._latest_order_sequence.get(entity_key)
        self._event_sources.setdefault(event_key, set()).add(source)
        normalized_status = self._normalize_order_status(raw.get("status"))
        if normalized_status not in {
            "accepted",
            "open",
            "resting",
            "partially_filled",
            "partial",
            "waitingforfill",
            "waitingfortrigger",
            "canceled",
            "rejected",
            "filled",
        }:
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError("order_status_unknown", "order event status is not supported by the canonical lifecycle")
        raw_broker_id = self._raw_broker_id(raw)
        raw_client_id = raw.get("cloid") or raw.get("client_order_id")
        active_broker_id = self._active_order_ids.get(entity_key)
        active_client_id = self._active_client_ids.get(entity_key)
        same_active_order = active_broker_id is None or raw_broker_id is None or raw_broker_id == active_broker_id
        known_broker_root = self._order_id_roots.get(raw_broker_id) if raw_broker_id is not None else None
        stale_broker = (
            active_broker_id is not None
            and raw_broker_id is not None
            and raw_broker_id != active_broker_id
            and (
                known_broker_root is not None
                or active_client_id is None
                or raw_client_id is None
                or str(raw_client_id) != active_client_id
            )
        )
        stale_client = active_client_id is not None and raw_client_id is not None and str(raw_client_id) != active_client_id
        if stale_broker or stale_client:
            if stale_client:
                self._state = RuntimeConnectionState.DEGRADED
                raise RuntimeRecoveryError("client_order_identity_conflict", "order cloid does not match the active canonical client identity")
            if not event_id:
                raise RuntimeRecoveryError("event_identity_required", "stale order event requires an identity")
            self._seen_order_event_ids.add(event_id)
            self._seen_order_keys.add(event_key)
            return None
        if (
            latest is not None
            and same_active_order
            and status_rank >= 40
            and self._latest_order_rank.get(entity_key, 0) >= 40
            and self._latest_order_status.get(entity_key) not in {None, normalized_status}
        ):
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError("order_terminal_conflict", "equal-timestamp terminal order observations conflict")
        if latest is not None and (
            sequence < latest
            or (
                same_active_order
                and status_rank < self._latest_order_rank.get(entity_key, status_rank)
            )
        ):
            if (
                same_active_order
                and self._latest_order_rank.get(entity_key, 0) >= 40
                and status_rank < self._latest_order_rank.get(entity_key, status_rank)
            ):
                self._state = RuntimeConnectionState.DEGRADED
                raise RuntimeRecoveryError("order_terminal_regression", "order lifecycle regressed from a terminal observation")
            return None
        if not event_id or event_id in self._seen_order_event_ids or event_key in self._seen_order_keys:
            if not event_id:
                raise RuntimeRecoveryError("event_identity_required", "order event requires an identity")
            self._order_event_keys[event_id] = event_key
            self._order_event_fingerprints[event_id] = fingerprint
            return None
        if normalized_status in {"filled", "partially_filled", "partial"}:
            self._validate_inline_fill_payload(raw)
            try:
                self._orders.validate_fill(raw)
            except RuntimeRecoveryError:
                self._state = RuntimeConnectionState.DEGRADED
                raise
            except Exception as error:
                self._state = RuntimeConnectionState.DEGRADED
                raise RuntimeRecoveryError("fill_context_invalid", type(error).__name__) from error
            embedded_duplicate = self._record_embedded_fill_identity(event_id, raw, source=source)
        else:
            embedded_duplicate = False
        if embedded_duplicate:
            self._seen_order_event_ids.add(event_id)
            self._order_event_keys[event_id] = event_key
            self._order_event_fingerprints[event_id] = fingerprint
            self._seen_order_keys.add(event_key)
            return None
        try:
            result = self._orders.apply_order_update(raw)
            self._require_paper_receipt(result)
            self._require_order_receipt_compatible(normalized_status, result)
            if normalized_status in {"filled", "partially_filled", "partial"} and not embedded_duplicate:
                self._require_fill_receipt_compatible(result, raw=raw)
        except RuntimeRecoveryError:
            self._state = RuntimeConnectionState.DEGRADED
            raise
        except Exception as error:
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError("order_event_apply_failed", type(error).__name__) from error
        result_broker_id = result.broker_order_id
        expected_order_id = entity_key.removeprefix("order:") if entity_key.startswith("order:") else None
        if expected_order_id is not None and result.order_id != expected_order_id:
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError("order_identity_mismatch", "order port returned a different canonical order")
        if raw_client_id is not None and str(raw_client_id) not in result.client_order_lineage:
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError("client_order_identity_mismatch", "order port returned a different client-order lineage")
        if raw_broker_id is not None and result_broker_id is not None and raw_broker_id != result_broker_id:
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError("order_identity_mismatch", "order port returned a different active Broker order")
        if normalized_status == "accepted" and raw_broker_id is None:
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError("replacement_identity_required", "accepted replacement requires a new Broker order identity")
        self._remember_order_aliases(raw_entity_key, result)
        self._seen_order_event_ids.add(event_id)
        self._order_event_keys[event_id] = event_key
        self._order_event_fingerprints[event_id] = fingerprint
        self._seen_order_keys.add(event_key)
        result_status = str(getattr(getattr(result, "state", None), "value", "")).lower()
        result_rank = self._order_status_rank(result_status)
        if normalized_status == "filled" and result_status != "filled":
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError("terminal_state_mismatch", "filled status did not produce a canonical filled receipt")
        if normalized_status in {"partially_filled", "partial"} and result_status not in {
            "partially_filled",
            "filled",
        }:
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError("terminal_state_mismatch", "partial status did not produce a canonical partial receipt")
        if normalized_status in {"filled", "partially_filled", "partial"} and result_rank == 0:
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError(
                "unresolved_terminal_order",
                "terminal order status lacks canonical fill evidence",
            )
        watermark_status = result_status
        watermark_rank = result_rank
        if status_rank >= 40 and result_rank < status_rank:
            watermark_status = f"unresolved:{normalized_status}"
            watermark_rank = status_rank
        self._latest_order_sequence[entity_key] = max(sequence, latest or sequence)
        self._latest_order_rank[entity_key] = max(watermark_rank, self._latest_order_rank.get(entity_key, watermark_rank))
        self._latest_order_status[entity_key] = watermark_status
        root = self._order_aliases.get(raw_entity_key or "")
        if root is not None:
            previous_active = self._active_order_ids.get(root)
            if result_broker_id is not None and previous_active is not None and previous_active != result_broker_id:
                self._latest_order_sequence[root] = sequence
                self._latest_order_rank[root] = watermark_rank
            if result_broker_id is not None:
                self._active_order_ids[root] = result_broker_id
            if result.client_order_id:
                self._active_client_ids[root] = result.client_order_id
            self._latest_order_sequence[root] = max(sequence, self._latest_order_sequence.get(root, sequence))
            self._latest_order_rank[root] = max(watermark_rank, self._latest_order_rank.get(root, watermark_rank))
            self._latest_order_status[root] = watermark_status
        self._event_version += 1
        return result

    def ingest_fill_event(
        self,
        event_id: str,
        raw: Mapping[str, object],
        *,
        source: ObservationSource = ObservationSource.REPLAY,
    ) -> object | None:
        with self._lock:
            return self._ingest_fill_event(event_id, raw, source=source)

    def _ingest_fill_event(
        self,
        event_id: str,
        raw: Mapping[str, object],
        *,
        source: ObservationSource = ObservationSource.REPLAY,
    ) -> object | None:
        self._require_connected()
        if not isinstance(raw, Mapping):
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError("event_payload_invalid", "fill event must be a mapping")
        self._validate_event_id(event_id)
        self._validate_source(source)
        self._validate_order_identity(raw)
        primary_key, alias_keys = self._fill_identity(raw)
        if primary_key is None or (raw.get("tid") is None and raw.get("hash") is None):
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError("fill_identity_required", "fill event requires tid, hash, or a complete fallback identity")
        has_native_id = raw.get("tid") is not None
        self._event_sources.setdefault(primary_key, set()).add(source)
        for alias_key in alias_keys:
            self._event_sources.setdefault(alias_key, set()).add(source)
        self._validate_fill_payload(raw)
        try:
            self._orders.validate_fill(raw)
        except RuntimeRecoveryError:
            self._state = RuntimeConnectionState.DEGRADED
            raise
        except Exception as error:
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError("fill_context_invalid", type(error).__name__) from error
        previous_key = self._fill_event_keys.get(event_id) if event_id else None
        if previous_key is not None and previous_key != primary_key:
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError("fill_event_identity_conflict", "event_id was reused for a different fill observation")
        fingerprint = self._fill_fingerprint(raw)
        hash_value = str(raw["hash"]).strip() if raw.get("hash") is not None else None
        previous_hash = self._fill_key_hashes.get(primary_key)
        if previous_hash is not None and hash_value is not None and previous_hash != hash_value:
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError("fill_identity_conflict", "native fill identity was reused with a different hash")
        if hash_value is not None:
            self._fill_key_hashes[primary_key] = hash_value
        shape_alias = next((alias for alias in alias_keys if alias.startswith("fill-shape:")), None)
        if not has_native_id and hash_value is not None and shape_alias is not None:
            known_shapes = self._weak_hash_shapes.get(hash_value, set())
            if known_shapes and shape_alias not in known_shapes:
                self._state = RuntimeConnectionState.DEGRADED
                raise RuntimeRecoveryError("fill_identity_ambiguous", "hash-only observations disagree on visible fill facts")
        previous_primary_fingerprint = self._fill_key_fingerprints.get(primary_key)
        if previous_primary_fingerprint is not None and previous_primary_fingerprint != fingerprint:
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError("fill_identity_conflict", "native fill identity was reused with different facts")
        if not has_native_id:
            shape_fingerprint = self._fill_shape_fingerprint(raw)
            for alias_key in alias_keys:
                previous_alias_fingerprint = self._fill_alias_fingerprints.get(alias_key)
                if previous_alias_fingerprint is not None and previous_alias_fingerprint != shape_fingerprint:
                    self._state = RuntimeConnectionState.DEGRADED
                    raise RuntimeRecoveryError("fill_identity_conflict", "weak fill identity was reused with different facts")
        previous_fingerprint = self._fill_event_fingerprints.get(event_id) if event_id else None
        if previous_fingerprint is not None and previous_fingerprint != fingerprint:
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError("fill_event_identity_conflict", "event_id was reused for a different fill observation")
        if event_id and previous_key is None:
            self._fill_event_keys[event_id] = primary_key
            self._fill_event_fingerprints[event_id] = fingerprint
        weak_alias = next((alias for alias in alias_keys if alias in self._weak_fill_keys), None)
        if has_native_id and weak_alias is not None:
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError(
                "fill_identity_ambiguous",
                "hash-only fill cannot be safely promoted to a native tid",
            )
        shape_collision = not has_native_id and any(
            alias.startswith("fill-shape:") and alias in self._seen_fill_aliases
            for alias in alias_keys
        )
        if shape_collision and primary_key not in self._seen_fill_keys and primary_key not in self._seen_fill_aliases:
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError(
                "fill_identity_ambiguous",
                "native fill identity conflicts with an existing weak shape observation",
            )
        duplicate = primary_key in self._seen_fill_keys or (
            not has_native_id
            and (
                primary_key in self._seen_fill_aliases
                or any(alias in self._seen_fill_aliases for alias in alias_keys)
            )
        )
        if not event_id or event_id in self._seen_fill_event_ids or duplicate:
            if not event_id:
                raise RuntimeRecoveryError("event_identity_required", "fill event requires an identity")
            self._fill_event_keys[event_id] = primary_key
            self._fill_event_fingerprints[event_id] = fingerprint
            return None
        self._require_fill_order_watermark(raw)
        try:
            result = self._orders.apply_fill(raw)
            self._require_paper_receipt(result)
            self._require_fill_receipt_compatible(result, raw=raw)
        except RuntimeRecoveryError:
            self._state = RuntimeConnectionState.DEGRADED
            raise
        except Exception as error:
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError("fill_apply_failed", type(error).__name__) from error
        self._seen_fill_event_ids.add(event_id)
        self._fill_event_keys[event_id] = primary_key
        self._fill_event_fingerprints[event_id] = fingerprint
        self._fill_key_fingerprints[primary_key] = fingerprint
        shape_fingerprint = self._fill_shape_fingerprint(raw)
        for alias_key in alias_keys:
            self._fill_alias_fingerprints[alias_key] = shape_fingerprint
        self._seen_fill_keys.add(primary_key)
        self._seen_fill_aliases.update(alias_keys)
        if not has_native_id:
            self._weak_fill_keys.update({primary_key, *alias_keys})
        if hash_value is not None and shape_alias is not None:
            self._weak_hash_shapes.setdefault(hash_value, set()).add(shape_alias)
        self._advance_order_watermark_from_fill(raw, result)
        self._event_version += 1
        return result

    def reconcile_snapshot(
        self,
        *,
        order_events: Sequence[tuple[str, Mapping[str, object]]],
        fill_events: Sequence[tuple[str, Mapping[str, object]]],
        positions: Sequence[PositionFact],
        account: AccountSnapshot,
        watermark: int,
    ) -> tuple[object, ...]:
        """Apply one authoritative Paper snapshot at the canonical reconciliation seam."""

        with self._lock:
            self._require_connected()
            try:
                validated_order_events, validated_fill_events, validated_positions, validated_account, validated_watermark = self._validate_snapshot(
                    {
                        "order_events": order_events,
                        "fill_events": fill_events,
                        "positions": positions,
                        "account": account,
                        "watermark": watermark,
                    }
                )
                self._require_snapshot_watermark(validated_watermark)
                self._require_snapshot_session(validated_account, validated_positions)
                snapshot_fingerprint = self._snapshot_event_fingerprint(
                    validated_order_events,
                    validated_fill_events,
                )
                self._require_snapshot_consistency(
                    validated_watermark,
                    validated_account,
                    validated_positions,
                    snapshot_fingerprint,
                )
                return self._apply_authoritative_snapshot(
                    order_events=validated_order_events,
                    fill_events=validated_fill_events,
                    positions=validated_positions,
                    account=validated_account,
                    watermark=validated_watermark,
                    snapshot_fingerprint=snapshot_fingerprint,
                )
            except Exception as error:
                self._state = RuntimeConnectionState.DEGRADED
                if isinstance(error, RuntimeRecoveryError):
                    raise
                raise RuntimeRecoveryError("reconciliation_snapshot_failed", type(error).__name__) from error

    def _capture_observation_state(self) -> dict[str, object]:
        return {
            name: deepcopy(getattr(self, name))
            for name in self._OBSERVATION_STATE_FIELDS
        }

    def _restore_observation_state(self, checkpoint: Mapping[str, object]) -> None:
        for name in self._OBSERVATION_STATE_FIELDS:
            setattr(self, name, checkpoint[name])

    def _apply_authoritative_snapshot(
        self,
        *,
        order_events: Sequence[tuple[str, Mapping[str, object]]],
        fill_events: Sequence[tuple[str, Mapping[str, object]]],
        positions: Sequence[PositionFact],
        account: AccountSnapshot,
        watermark: int,
        snapshot_fingerprint: str,
    ) -> tuple[object, ...]:
        transaction = getattr(self._orders, "transaction", None)
        if not callable(transaction):
            raise RuntimeRecoveryError(
                "runtime_contract_missing",
                "order lifecycle must expose transaction() for atomic snapshot apply",
            )
        checkpoint = self._capture_observation_state()
        self._reconciling_snapshot = True
        try:
            with transaction():
                results = self._apply_snapshot(order_events=order_events, fill_events=fill_events)
                self._latest_positions = tuple(positions)
                self._latest_account = account
                self._latest_watermark = watermark
                self._latest_snapshot_fingerprint = snapshot_fingerprint
                self._latest_snapshot_kind = "events"
                return results
        except Exception as error:
            self._restore_observation_state(checkpoint)
            self._state = RuntimeConnectionState.DEGRADED
            self._snapshot_recovery_dirty = True
            if isinstance(error, RuntimeRecoveryError):
                raise
            raise RuntimeRecoveryError("reconciliation_snapshot_failed", type(error).__name__) from error
        finally:
            self._reconciling_snapshot = False

    def _apply_snapshot(
        self,
        *,
        order_events: Sequence[tuple[str, Mapping[str, object]]],
        fill_events: Sequence[tuple[str, Mapping[str, object]]],
    ) -> tuple[object, ...]:
        results: list[object] = []
        combined: list[tuple[int, int, int, str, str, Mapping[str, object]]] = []
        for event_id, raw in order_events:
            combined.append((self._order_sequence(raw) or 0, 0, self._order_status_rank(raw.get("status")), event_id, "order", raw))
        for event_id, raw in fill_events:
            combined.append((self._fill_sequence(raw), 1, 0, event_id, "fill", raw))
        combined.sort(key=lambda event: (event[0], event[1], event[2], event[3]))
        for _, _, _, event_id, kind, raw in combined:
            result = (
                self.ingest_order_event(event_id, raw, source=ObservationSource.REPLAY)
                if kind == "order"
                else self.ingest_fill_event(event_id, raw, source=ObservationSource.REPLAY)
            )
            if result is not None:
                results.append(result)
        return tuple(results)

    def reconcile_before_retry(
        self,
        order_id: str,
        *,
        facts: ReconciliationFacts,
        attempt: int = 1,
    ) -> RecoveryDecision:
        with self._lock:
            return self._reconcile_before_retry(order_id, facts=facts, attempt=attempt)

    def _reconcile_before_retry(
        self,
        order_id: str,
        *,
        facts: ReconciliationFacts,
        attempt: int = 1,
    ) -> RecoveryDecision:
        """Query an ambiguous order and return a decision; never blind-retry here."""

        self._require_connected()
        try:
            facts_local_only = facts.local_only
            facts_session = facts.runtime_session
        except Exception as error:
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError(
                "runtime_contract_missing",
                "reconciliation facts must expose local_only and runtime_session",
            ) from error
        if facts_local_only is not True:
            raise RuntimeRecoveryError("non_local_fact_source", "reconciliation facts require a local-only source")
        if not isinstance(facts_session, BrokerRuntimeSession) or facts_session is not self._session:
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError("runtime_session_mismatch", "reconciliation facts and order lifecycle sessions differ")
        if not callable(getattr(facts, "query_snapshot", None)):
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError("runtime_contract_missing", "reconciliation facts missing query_snapshot()")
        self._validate_attempt(attempt, self._retry_policy)
        start_version = self._event_version
        start_epoch = self._connection_epoch
        try:
            reconciled_snapshot = facts.query_snapshot(order_id)
            if (
                not isinstance(reconciled_snapshot, ReconciliationSnapshot)
                or reconciled_snapshot.order_id != order_id
                or not isinstance(reconciled_snapshot.receipt, OrderReceipt)
                or reconciled_snapshot.receipt.order_id != order_id
            ):
                raise RuntimeRecoveryError(
                    "reconciliation_order_identity_mismatch",
                    "reconciliation snapshot does not match the requested canonical order",
                )
            receipt = reconciled_snapshot.receipt
            self._require_paper_receipt(receipt)
            self._require_order_receipt_compatible(receipt.state.value, receipt)
            try:
                self._orders.validate_receipt(receipt)
            except RuntimeRecoveryError:
                raise
            except Exception as error:
                raise RuntimeRecoveryError(
                    "reconciliation_order_identity_mismatch",
                    "reconciliation snapshot receipt is not current in the lifecycle owner",
                ) from error
            if (
                self._latest_watermark is not None
                and reconciled_snapshot.watermark < self._latest_watermark
            ):
                raise RuntimeRecoveryError(
                    "stale_reconciliation_snapshot",
                    "reconciliation snapshot watermark regressed",
                )
            snapshot_fingerprint = self._fact_snapshot_fingerprint(reconciled_snapshot)
            reconciled_facts = {
                "fills": reconciled_snapshot.fills,
                "positions": reconciled_snapshot.positions,
                "account": reconciled_snapshot.account,
                "watermark": reconciled_snapshot.watermark,
            }
            if (
                not isinstance(reconciled_facts["fills"], Sequence)
                or isinstance(reconciled_facts["fills"], (str, bytes, bytearray))
                or not isinstance(reconciled_facts["positions"], Sequence)
                or isinstance(reconciled_facts["positions"], (str, bytes, bytearray))
                or not isinstance(reconciled_facts["account"], AccountSnapshot)
            ):
                raise RuntimeRecoveryError("fact_payload_invalid", "Broker reconciliation snapshot has an invalid canonical shape")
            if isinstance(reconciled_facts["account"], AccountSnapshot):
                self._require_snapshot_session(
                    reconciled_facts["account"],
                    reconciled_facts["positions"],
                )
            self._require_fact_snapshot_consistency(reconciled_snapshot, snapshot_fingerprint)
            if self._event_version != start_version or self._connection_epoch != start_epoch:
                self._state = RuntimeConnectionState.DEGRADED
                raise RuntimeRecoveryError("reconciliation_changed_during_query", "lifecycle changed while facts were queried")
        except Exception as error:
            self._state = RuntimeConnectionState.DEGRADED
            if isinstance(error, RuntimeRecoveryError):
                raise
            raise RuntimeRecoveryError("reconciliation_ambiguous", type(error).__name__) from error
        facts_complete = all(
            self._fact_complete(reconciled_facts[name])
            for name in ("fills", "positions", "account")
        )
        has_fills = bool(reconciled_facts["fills"])
        positions = reconciled_facts["positions"]
        account = reconciled_facts["account"]
        has_positions = bool(positions) or (
            isinstance(account, AccountSnapshot) and bool(account.positions)
        )
        has_exposure = not isinstance(account, AccountSnapshot) or account.exposure is None or account.exposure != 0
        receipt_has_execution = (
            receipt.filled_quantity != 0
            or receipt.remaining_quantity != receipt.original_quantity
            or receipt.average_fill_price is not None
            or receipt.state in {OrderState.PARTIALLY_FILLED, OrderState.FILLED}
        )
        facts_match_receipt = self._facts_match_receipt(receipt, reconciled_facts)
        if facts_complete and not facts_match_receipt:
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError(
                "reconciliation_fact_identity_mismatch",
                "reconciled facts do not match the canonical order Broker identity",
            )
        if facts_complete:
            self._commit_fact_snapshot(
                snapshot=reconciled_snapshot,
                fingerprint=snapshot_fingerprint,
            )
            self._event_version += 1
        retry_budget_available = attempt < self._retry_policy.max_attempts
        rejection_retryable = (
            receipt.state in {OrderState.CANCELED, OrderState.REJECTED}
            and receipt.reason in _RETRYABLE_REASONS
        )
        retry_allowed = (
            receipt.state in {OrderState.CANCELED, OrderState.REJECTED}
            and facts_complete
            and not has_fills
            and not has_positions
            and not has_exposure
            and not receipt_has_execution
            and retry_budget_available
            and rejection_retryable
        )
        if not facts_complete:
            reason = "retry_blocked_incomplete_facts"
        elif has_fills:
            reason = "retry_blocked_existing_fills"
        elif receipt_has_execution:
            reason = "retry_blocked_receipt_has_execution"
        elif has_positions or has_exposure:
            reason = "retry_blocked_residual_exposure"
        elif not retry_budget_available:
            reason = "retry_blocked_retry_budget_exhausted"
        elif not rejection_retryable:
            reason = "retry_blocked_non_retryable_rejection"
        elif not retry_allowed:
            reason = "retry_blocked_until_authoritative_state"
        else:
            reason = "terminal_reconciled"
        decision_facts = MappingProxyType(
            {
                "fills": tuple(reconciled_facts["fills"]),
                "positions": tuple(reconciled_facts["positions"]),
                "account": reconciled_facts["account"],
                "watermark": reconciled_facts["watermark"],
            }
        )
        previous_lease = self._active_leases.get(order_id)
        if previous_lease is not None:
            self._issued_leases.pop(previous_lease, None)
        lease_token = object()
        self._active_leases[order_id] = lease_token
        decision_event_version = self._event_version
        decision_connection_epoch = self._connection_epoch
        self._issued_leases[lease_token] = (
            order_id,
            receipt,
            retry_allowed,
            reason,
            decision_facts,
            decision_event_version,
            decision_connection_epoch,
        )
        return RecoveryDecision(
            order_id,
            receipt,
            retry_allowed,
            reason,
            decision_facts,
            decision_event_version,
            decision_connection_epoch,
            self._owner_token,
            lease_token,
        )

    def recovery_decision_is_current(self, decision: RecoveryDecision) -> bool:
        """Validate a retry lease immediately before the host performs any retry action."""

        with self._lock:
            issued = self._issued_leases.pop(decision.lease_token, None)
            if issued is None:
                return False
            if self._active_leases.get(decision.order_id) is not decision.lease_token:
                return False
            self._active_leases.pop(decision.order_id, None)
            issued_order_id, issued_receipt, issued_retry_allowed, issued_reason, issued_facts, issued_version, issued_epoch = issued
            return (
                decision.retry_allowed
                and decision.order_id == issued_order_id
                and decision.receipt == issued_receipt
                and decision.retry_allowed == issued_retry_allowed
                and decision.reason == issued_reason
                and decision.facts == issued_facts
                and decision.event_version == issued_version == self._event_version
                and decision.connection_epoch == issued_epoch == self._connection_epoch
                and decision.owner_token is self._owner_token
                and self._state is RuntimeConnectionState.CONNECTED
            )

    def retry_plan(self, error: BaseException, *, attempt: int) -> RetryPlan:
        """Expose the bounded policy through the controller without executing it."""

        self._validate_attempt(attempt, self._retry_policy)
        return plan_retry(error, attempt=attempt, policy=self._retry_policy)

    @staticmethod
    def _order_key(raw: Mapping[str, object]) -> str | None:
        oid = raw.get("oid")
        cloid = raw.get("cloid") or raw.get("client_order_id")
        if oid is None and cloid is None:
            return None
        sequence = HyperliquidRuntimeReconciliationAdapter._first_present(raw, ("statusTimestamp", "timestamp", "time"))
        if sequence is None:
            return None
        fingerprint = ":".join(
            str(raw.get(field, ""))
            for field in (
                "coin",
                "side",
                "sz",
                "limitPx",
                "tif",
                "timeInForce",
                "orderType",
                "order_type",
                "filled",
                "totalSz",
                "remaining",
                "avgPx",
                "tid",
                "hash",
                "px",
                "time",
                "fee",
                "feeToken",
                "crossed",
                "closedPnl",
                "builderFee",
                "reduceOnly",
                "dir",
                "reason",
            )
        )
        fingerprint = f"{HyperliquidRuntimeReconciliationAdapter._normalize_order_status(raw.get('status'))}:{fingerprint}"
        return f"order-event:{oid}:{cloid}:{sequence}:{fingerprint}"

    @staticmethod
    def _order_entity_key(raw: Mapping[str, object]) -> str | None:
        oid = raw.get("oid")
        cloid = raw.get("cloid") or raw.get("client_order_id")
        if oid is None and cloid is None:
            return None
        return f"order_entity:{oid}:{cloid}"

    def _resolve_order_entity_key(self, raw: Mapping[str, object]) -> str | None:
        raw_key = self._order_entity_key(raw)
        if raw_key is None:
            return None
        broker_id = self._raw_broker_id(raw)
        client_id = raw.get("cloid") or raw.get("client_order_id")
        broker_root = self._order_id_roots.get(broker_id) if broker_id is not None else None
        client_root = self._client_id_roots.get(str(client_id)) if client_id is not None else None
        if broker_root is not None and client_root is not None and broker_root != client_root:
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError("order_identity_conflict", "oid and cloid resolve to different canonical orders")
        return broker_root or client_root or self._order_aliases.get(raw_key, raw_key)

    @staticmethod
    def _order_sequence(raw: Mapping[str, object]) -> int | None:
        value = HyperliquidRuntimeReconciliationAdapter._first_present(raw, ("statusTimestamp", "timestamp", "time"))
        return HyperliquidRuntimeReconciliationAdapter._strict_timestamp(value)

    @staticmethod
    def _first_present(raw: Mapping[str, object], fields: Sequence[str]) -> object | None:
        for field in fields:
            if field in raw and raw[field] is not None:
                return raw[field]
        return None

    @staticmethod
    def _raw_broker_id(raw: Mapping[str, object]) -> str | None:
        value = raw.get("oid")
        return str(value) if value is not None else None

    def _validate_event_id(self, event_id: object) -> None:
        if not isinstance(event_id, str) or not event_id.strip():
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError("event_identity_required", "event_id must be a non-empty string")

    def _validate_source(self, source: object) -> None:
        if not isinstance(source, ObservationSource):
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError("observation_source_invalid", "observation source is not canonical")

    def _validate_order_identity(self, raw: Mapping[str, object]) -> None:
        cloid = raw.get("cloid")
        client_order_id = raw.get("client_order_id")
        if cloid is not None and client_order_id is not None and cloid != client_order_id:
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError("order_identity_conflict", "cloid and client_order_id disagree")
        for field in ("oid", "cloid", "client_order_id"):
            value = raw.get(field)
            if value is None:
                continue
            if field == "oid":
                valid = (type(value) is int and value > 0) or (
                    isinstance(value, str) and value.isdecimal() and len(value) <= 18 and 0 < int(value) <= 10**18
                )
                if type(value) is int:
                    valid = 0 < value <= 10**18
            else:
                valid = isinstance(value, str) and bool(value.strip())
            if not valid:
                self._state = RuntimeConnectionState.DEGRADED
                raise RuntimeRecoveryError("order_identity_invalid", f"{field} has an invalid scalar identity")

    @staticmethod
    def _order_status_rank(value: object) -> int:
        return {
            "": 0,
            "accepted": 10,
            "open": 20,
            "resting": 20,
            "partially_filled": 30,
            "partial": 30,
            "waitingforfill": 30,
            "waitingfortrigger": 30,
            "canceled": 40,
            "cancelled": 40,
            "rejected": 40,
            "filled": 50,
        }.get(HyperliquidRuntimeReconciliationAdapter._normalize_order_status(value), 0)

    @staticmethod
    def _normalize_order_status(value: object) -> str:
        status = str(value or "").lower()
        return {
            "cancelled": "canceled",
            "partiallyfilled": "partially_filled",
            "waitingforfill": "waitingforfill",
            "waitingfortrigger": "waitingfortrigger",
        }.get(status, status)

    @staticmethod
    def _fill_identity(raw: Mapping[str, object]) -> tuple[str | None, frozenset[str]]:
        tid = raw.get("tid")
        hash_value = raw.get("hash")
        if tid is not None and not str(tid).strip():
            return None, frozenset()
        if hash_value is not None and not str(hash_value).strip():
            return None, frozenset()
        aliases: set[str] = set()
        shape_key = HyperliquidRuntimeReconciliationAdapter._fill_shape_key(raw)
        if shape_key is not None:
            aliases.add(shape_key)
        if hash_value is not None:
            aliases.add(HyperliquidRuntimeReconciliationAdapter._fill_hash_shape_key(raw))
        if tid is not None:
            canonical_tid = str(int(tid)) if type(tid) is int or (isinstance(tid, str) and tid.isdecimal()) else None
            if canonical_tid is None:
                return None, frozenset()
            return f"fill:tid:{canonical_tid}", frozenset(aliases)
        if hash_value is not None:
            return HyperliquidRuntimeReconciliationAdapter._fill_hash_shape_key(raw), frozenset(
                {shape_key} if shape_key is not None else set()
            )
        if shape_key is not None:
            return shape_key, frozenset()
        return None, frozenset()

    @staticmethod
    def _fill_hash_shape_key(raw: Mapping[str, object]) -> str:
        shape = ":".join(str(raw.get(field, "")) for field in ("oid", "coin", "side", "px", "sz", "time"))
        return f"fill:hash:{raw['hash']}:{shape}"

    @staticmethod
    def _fill_shape_key(raw: Mapping[str, object]) -> str | None:
        fields = ("oid", "coin", "side", "px", "sz", "time")
        if not all(raw.get(field) is not None for field in fields):
            return None
        shape = ":".join(str(raw[field]) for field in fields)
        return f"fill-shape:{shape}"

    @staticmethod
    def _fill_fingerprint(raw: Mapping[str, object]) -> str:
        client_id = raw.get("cloid") or raw.get("client_order_id") or ""
        return "|".join(
            [
                str(raw.get("tid", "")),
                str(client_id),
                *(
                    str(raw.get(field, ""))
                    for field in (
                        "oid",
                        "coin",
                        "side",
                        "px",
                        "sz",
                        "time",
                        "fee",
                        "feeToken",
                        "crossed",
                        "closedPnl",
                        "builderFee",
                        "dir",
                    )
                ),
            ]
        )

    @staticmethod
    def _fill_shape_fingerprint(raw: Mapping[str, object]) -> str:
        client_id = raw.get("cloid") or raw.get("client_order_id") or ""
        return "|".join(
            [
                str(client_id),
                *(
                    str(raw.get(field, ""))
                    for field in (
                        "oid",
                        "coin",
                        "side",
                        "px",
                        "sz",
                        "time",
                        "fee",
                        "feeToken",
                        "crossed",
                        "closedPnl",
                        "builderFee",
                        "dir",
                    )
                ),
            ]
        )

    def _require_fill_order_watermark(self, raw: Mapping[str, object]) -> None:
        entity_key = self._resolve_order_entity_key(raw)
        sequence = self._fill_sequence(raw)
        if entity_key is None or sequence == 0:
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError("fill_sequence_required", "fill event requires an order identity and timestamp")
        resolved = self._order_aliases.get(entity_key, entity_key)
        raw_broker_id = self._raw_broker_id(raw)
        raw_client_id = raw.get("cloid") or raw.get("client_order_id")
        active_broker_id = self._active_order_ids.get(resolved)
        active_client_id = self._active_client_ids.get(resolved)
        known_broker_root = self._order_id_roots.get(raw_broker_id) if raw_broker_id is not None else None
        if (
            active_broker_id is not None
            and raw_broker_id is not None
            and raw_broker_id != active_broker_id
            and not (
                raw_client_id is not None
                and active_client_id is not None
                and str(raw_client_id) == active_client_id
                and known_broker_root is None
            )
        ) or (
            active_client_id is not None
            and raw_client_id is not None
            and str(raw_client_id) != active_client_id
        ):
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError("fill_order_lineage_conflict", "fill does not belong to the active canonical order lineage")
        latest = self._latest_order_sequence.get(resolved)
        latest_rank = self._latest_order_rank.get(resolved, 0)
        if latest is not None and (sequence < latest or latest_rank >= 40):
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError("fill_lifecycle_conflict", "fill conflicts with an older or terminal order observation")

    def _require_paper_receipt(self, result: object) -> None:
        if (
            not isinstance(result, OrderReceipt)
            or result.broker_id != "hyperliquid"
            or result.environment is not BrokerEnvironment.PAPER
            or not isinstance(result.order_id, str)
            or not result.order_id.strip()
            or not isinstance(result.client_order_id, str)
            or not result.client_order_id.strip()
            or not isinstance(result.broker_order_lineage, tuple)
            or not isinstance(result.client_order_lineage, tuple)
            or not result.broker_order_lineage
            or not result.client_order_lineage
            or not all(isinstance(value, str) and value.strip() for value in result.broker_order_lineage)
            or not all(isinstance(value, str) and value.strip() for value in result.client_order_lineage)
            or not isinstance(result.broker_order_id, str)
            or result.broker_order_id != result.broker_order_lineage[-1]
            or result.client_order_id != result.client_order_lineage[-1]
            or not self._provenance_complete(result.provenance)
            or result.provenance.execution_scope != self._session.execution_scope
        ):
            raise RuntimeRecoveryError(
                "paper_receipt_required",
                "RT-06 accepts only canonical Hyperliquid Paper receipts",
            )

    def _require_order_receipt_compatible(self, status: str, result: OrderReceipt) -> None:
        state = result.state.value
        if status in {"canceled", "cancelled"} and state != "canceled":
            raise RuntimeRecoveryError("terminal_state_mismatch", "canceled status did not produce a canceled receipt")
        if status == "rejected" and state != "rejected":
            raise RuntimeRecoveryError("terminal_state_mismatch", "rejected status did not produce a rejected receipt")
        if status == "filled" and state != "filled":
            raise RuntimeRecoveryError("terminal_state_mismatch", "filled status did not produce a filled receipt")
        if status in {"partially_filled", "partial"} and state not in {"partially_filled", "filled"}:
            raise RuntimeRecoveryError("terminal_state_mismatch", "partial status did not produce a partial receipt")
        if status in {"accepted", "open", "resting"} and state in {
            "partially_filled",
            "filled",
            "canceled",
            "rejected",
        }:
            raise RuntimeRecoveryError("terminal_state_mismatch", "non-terminal status produced a terminal receipt")
        if status in {"accepted", "open", "resting"} and state not in {"resting"}:
            raise RuntimeRecoveryError("terminal_state_mismatch", "order status did not produce the expected resting state")
        if status == "waitingforfill" and state != "waiting_for_fill":
            raise RuntimeRecoveryError("terminal_state_mismatch", "waiting-for-fill status did not produce the expected state")
        if status == "waitingfortrigger" and state != "waiting_for_trigger":
            raise RuntimeRecoveryError("terminal_state_mismatch", "waiting-for-trigger status did not produce the expected state")
        if (
            result.original_quantity <= 0
            or not all(
                isinstance(value, Decimal) and value.is_finite()
                for value in (result.original_quantity, result.filled_quantity, result.remaining_quantity)
            )
            or result.filled_quantity < 0
            or result.remaining_quantity < 0
            or result.filled_quantity + result.remaining_quantity != result.original_quantity
            or (state == "filled" and (result.filled_quantity != result.original_quantity or result.remaining_quantity != 0))
            or (state == "partially_filled" and not 0 < result.filled_quantity < result.original_quantity)
            or (state in {"resting", "waiting_for_fill", "waiting_for_trigger"} and result.filled_quantity != 0)
            or (state in {"resting", "waiting_for_fill", "waiting_for_trigger", "submitting", "modify_pending", "cancel_pending"} and result.average_fill_price is not None)
            or (state == "rejected" and result.filled_quantity != 0)
            or (state == "canceled" and result.filled_quantity != 0 and result.average_fill_price is None)
            or (state in {"partially_filled", "filled"} and result.average_fill_price is None)
            or (
                result.average_fill_price is not None
                and (
                    not isinstance(result.average_fill_price, Decimal)
                    or not result.average_fill_price.is_finite()
                    or result.average_fill_price <= 0
                )
            )
        ):
            raise RuntimeRecoveryError("order_quantity_invariant_failed", "canonical receipt quantities are inconsistent")

    def _require_fill_receipt_compatible(self, result: OrderReceipt, *, raw: Mapping[str, object]) -> None:
        entity_key = self._resolve_order_entity_key(raw)
        expected_order_id = entity_key.removeprefix("order:") if entity_key and entity_key.startswith("order:") else None
        raw_broker_id = self._raw_broker_id(raw)
        raw_client_id = raw.get("cloid") or raw.get("client_order_id")
        if expected_order_id is not None and result.order_id != expected_order_id:
            raise RuntimeRecoveryError("fill_order_identity_mismatch", "fill receipt belongs to a different canonical order")
        if raw_broker_id is not None and raw_broker_id not in result.broker_order_lineage:
            raise RuntimeRecoveryError("fill_order_identity_mismatch", "fill oid is absent from the canonical order lineage")
        if raw_client_id is not None and str(raw_client_id) not in result.client_order_lineage:
            raise RuntimeRecoveryError("fill_client_identity_mismatch", "fill cloid is absent from the canonical client lineage")
        if result.state.value not in {"partially_filled", "filled"}:
            raise RuntimeRecoveryError("fill_state_mismatch", "fill did not produce a partial or filled receipt")
        self._require_order_receipt_compatible(result.state.value, result)

    def _record_embedded_fill_identity(
        self,
        event_id: str,
        raw: Mapping[str, object],
        *,
        source: ObservationSource,
    ) -> bool:
        self._validate_inline_fill_payload(raw)
        primary_key, alias_keys = self._fill_identity(raw)
        if primary_key is None:
            raise RuntimeRecoveryError("fill_identity_required", "embedded fill requires tid or hash")
        self._event_sources.setdefault(primary_key, set()).add(source)
        for alias_key in alias_keys:
            self._event_sources.setdefault(alias_key, set()).add(source)
        fingerprint = self._fill_fingerprint(raw)
        previous_key = self._fill_event_keys.get(event_id)
        previous_fingerprint = self._fill_event_fingerprints.get(event_id)
        if (previous_key is not None and previous_key != primary_key) or (
            previous_fingerprint is not None and previous_fingerprint != fingerprint
        ):
            raise RuntimeRecoveryError("fill_event_identity_conflict", "embedded fill identity was reused with different facts")
        previous_primary = self._fill_key_fingerprints.get(primary_key)
        if previous_primary is not None and previous_primary != fingerprint:
            raise RuntimeRecoveryError("fill_identity_conflict", "embedded fill identity was reused with different facts")
        for alias_key in alias_keys:
            previous_alias = self._fill_alias_fingerprints.get(alias_key)
            if previous_alias is not None and previous_alias != self._fill_shape_fingerprint(raw):
                raise RuntimeRecoveryError("fill_identity_conflict", "embedded fill alias was reused with different facts")
        hash_value = str(raw["hash"]).strip() if raw.get("hash") is not None else None
        previous_hash = self._fill_key_hashes.get(primary_key)
        if previous_hash is not None and hash_value is not None and previous_hash != hash_value:
            raise RuntimeRecoveryError("fill_identity_conflict", "embedded fill identity was reused with a different hash")
        if hash_value is not None:
            self._fill_key_hashes[primary_key] = hash_value
        if event_id and previous_key is None:
            self._fill_event_keys[event_id] = primary_key
            self._fill_event_fingerprints[event_id] = fingerprint
        weak_alias = next((alias for alias in alias_keys if alias in self._weak_fill_keys), None)
        if weak_alias is not None:
            raise RuntimeRecoveryError("fill_identity_ambiguous", "embedded fill conflicts with a weak fill observation")
        shape_collision = any(
            alias.startswith("fill-shape:") and alias in self._seen_fill_aliases
            for alias in alias_keys
        )
        if shape_collision and primary_key not in self._seen_fill_keys and primary_key not in self._seen_fill_aliases:
            raise RuntimeRecoveryError("fill_identity_ambiguous", "embedded fill conflicts with a weak shape observation")
        if primary_key in self._seen_fill_keys or primary_key in self._seen_fill_aliases:
            return True
        self._seen_fill_event_ids.add(event_id)
        self._fill_key_fingerprints[primary_key] = fingerprint
        shape_fingerprint = self._fill_shape_fingerprint(raw)
        for alias_key in alias_keys:
            self._fill_alias_fingerprints[alias_key] = shape_fingerprint
        self._seen_fill_keys.add(primary_key)
        self._seen_fill_aliases.update(alias_keys)
        hash_value = str(raw["hash"]).strip() if raw.get("hash") is not None else None
        shape_alias = next((alias for alias in alias_keys if alias.startswith("fill-shape:")), None)
        if hash_value is not None and shape_alias is not None:
            self._weak_hash_shapes.setdefault(hash_value, set()).add(shape_alias)
        return False

    def _require_snapshot_session(
        self,
        account: AccountSnapshot,
        positions: Sequence[PositionFact],
    ) -> None:
        if (
            account.broker_id != "hyperliquid"
            or account.environment is not BrokerEnvironment.PAPER
            or account.account_address != self._account_address
            or account.provenance.transport_state != "local_fixture"
            or account.provenance.execution_scope != self._session.execution_scope
            or not self._positions_match(account.positions, positions)
            or any(
                position.provenance.transport_state != "local_fixture"
                or position.provenance.execution_scope != self._session.execution_scope
                for position in positions
            )
        ):
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError(
                "reconciliation_session_mismatch",
                "snapshot facts do not belong to the bound Paper account session",
            )

    def _advance_order_watermark_from_fill(self, raw: Mapping[str, object], result: object) -> None:
        entity_key = self._resolve_order_entity_key(raw)
        sequence = self._fill_sequence(raw)
        if entity_key is None or sequence == 0:
            return
        resolved = self._order_aliases.get(entity_key, entity_key)
        result_status = str(getattr(getattr(result, "state", None), "value", "")).lower()
        rank = self._order_status_rank(result_status)
        latest = self._latest_order_sequence.get(resolved)
        if latest is None or sequence >= latest:
            self._latest_order_sequence[resolved] = sequence
            self._latest_order_rank[resolved] = max(rank, self._latest_order_rank.get(resolved, rank))
            self._latest_order_status[resolved] = result_status
        self._remember_order_aliases(entity_key, result)
        root = self._order_aliases.get(entity_key, entity_key)
        result_broker_id = getattr(result, "broker_order_id", None)
        if result_broker_id is not None:
            self._active_order_ids[root] = str(result_broker_id)
        result_client_id = getattr(result, "client_order_id", None)
        if result_client_id:
            self._active_client_ids[root] = result_client_id
        if root != resolved:
            self._latest_order_sequence[root] = max(sequence, self._latest_order_sequence.get(root, sequence))
            self._latest_order_rank[root] = max(rank, self._latest_order_rank.get(root, rank))
            self._latest_order_status[root] = result_status

    def _facts_match_receipt(self, receipt: OrderReceipt, facts: Mapping[str, object]) -> bool:
        account = facts["account"]
        if not isinstance(account, AccountSnapshot):
            return False
        if (
            account.broker_id != receipt.broker_id
            or account.environment is not receipt.environment
            or account.account_address != self._account_address
            or account.provenance.transport_state != "local_fixture"
            or account.provenance.execution_scope != self._session.execution_scope
        ):
            return False
        for value in (*facts["fills"], *facts["positions"]):
            if (
                value.broker_id != receipt.broker_id
                or value.environment is not receipt.environment
                or value.provenance.transport_state != "local_fixture"
                or value.provenance.execution_scope != self._session.execution_scope
            ):
                return False
            if isinstance(value, FillFact) and (
                not self._fee_complete(value.fee)
                or value.fee.provenance.execution_scope != self._session.execution_scope
                or value.fee.order_id not in {receipt.order_id, *receipt.broker_order_lineage}
                or (
                    value.builder_fee is not None
                    and (
                        not self._fee_complete(value.builder_fee)
                        or value.builder_fee.provenance.execution_scope != self._session.execution_scope
                        or value.builder_fee.order_id not in {receipt.order_id, *receipt.broker_order_lineage}
                    )
                )
            ):
                return False
        return (
            receipt.broker_id == "hyperliquid"
            and receipt.environment is BrokerEnvironment.PAPER
            and self._positions_match(
                account.positions,
                facts["positions"],
            )
        )

    def _require_connected(self) -> None:
        if self._state is not RuntimeConnectionState.CONNECTED and not (
            self._state is RuntimeConnectionState.RECONNECTING and self._reconciling_snapshot
        ):
            raise RuntimeRecoveryError("runtime_not_connected", f"runtime is {self._state.value}")

    @staticmethod
    def _fill_sequence(raw: Mapping[str, object]) -> int:
        for field in ("time", "timestamp", "statusTimestamp"):
            value = raw.get(field)
            if value is not None:
                return HyperliquidRuntimeReconciliationAdapter._strict_timestamp(value) or 0
        return 0

    @staticmethod
    def _strict_timestamp(value: object) -> int | None:
        if type(value) is int:
            return value if 0 < value <= 10**15 else None
        if isinstance(value, str) and value.isdecimal():
            parsed = int(value)
            return parsed if 0 < parsed <= 10**15 else None
        return None

    def _validate_inline_fill_payload(self, raw: Mapping[str, object]) -> None:
        self._validate_raw_fill_fields(raw, require_tid=True)

    def _validate_fill_payload(self, raw: Mapping[str, object]) -> None:
        if self._order_entity_key(raw) is None:
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError("fill_payload_incomplete", "fill lacks a canonical order identity")
        self._validate_raw_fill_fields(raw, require_tid=False)

    def _validate_raw_fill_fields(self, raw: Mapping[str, object], *, require_tid: bool) -> None:
        required = ("coin", "side", "px", "sz", "time")
        if require_tid:
            required += ("tid",)
        if any(raw.get(field) is None for field in required):
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError("fill_payload_incomplete", "fill lacks required identity, instrument, price, size, or side facts")
        tid = raw.get("tid")
        if tid is not None and not (
            (type(tid) is int and 0 < tid <= 10**18)
            or (isinstance(tid, str) and tid.isdecimal() and len(tid) <= 18 and int(tid) > 0)
        ):
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError("fill_identity_invalid", "fill tid must be a positive scalar identity")
        hash_value = raw.get("hash")
        if hash_value is not None and (not isinstance(hash_value, str) or not hash_value.strip()):
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError("fill_identity_invalid", "fill hash must be a non-empty string")
        if not isinstance(raw["coin"], str) or not raw["coin"].strip():
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError("fill_instrument_invalid", "fill coin must be a non-empty string")
        if not isinstance(raw["side"], str) or raw["side"] not in {"B", "A"}:
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError("fill_side_invalid", "fill side is not a Hyperliquid buy/sell side")
        try:
            timestamp = self._strict_timestamp(raw["time"])
            price = Decimal(str(raw["px"]))
            quantity = Decimal(str(raw["sz"]))
        except (InvalidOperation, TypeError, ValueError) as error:
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError("fill_payload_invalid", "fill timestamp, price, or size is malformed") from error
        if timestamp is None or not price.is_finite() or price <= 0 or not quantity.is_finite() or quantity <= 0:
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError("fill_payload_invalid", "fill timestamp, price, and size must be positive finite values")

    @staticmethod
    def _validate_attempt(attempt: int, policy: RetryPolicy | None = None) -> None:
        selected_policy = policy or RetryPolicy()
        if type(attempt) is not int or attempt < 1 or attempt > selected_policy.max_attempts:
            raise ValueError("retry attempt must be within the configured bounds")

    def _require_snapshot_watermark(self, watermark: int) -> None:
        previous = self._latest_watermark
        if isinstance(previous, int) and isinstance(watermark, int) and watermark < previous:
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError("stale_reconciliation_snapshot", "snapshot watermark regressed")

    def _require_snapshot_kind(self, watermark: int, kind: str) -> None:
        if (
            watermark == self._latest_watermark
            and self._latest_snapshot_kind is not None
            and self._latest_snapshot_kind != kind
        ):
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError(
                "snapshot_cursor_conflict",
                "same snapshot cursor was observed through different fact sources",
            )

    @staticmethod
    def _fact_snapshot_fingerprint(snapshot: ReconciliationSnapshot) -> str:
        return repr(
            (
                snapshot.order_id,
                snapshot.receipt,
                tuple(snapshot.fills),
                tuple(snapshot.positions),
                snapshot.account,
                snapshot.watermark,
            )
        )

    def _require_fact_snapshot_consistency(
        self,
        snapshot: ReconciliationSnapshot,
        fingerprint: str,
    ) -> None:
        self._require_snapshot_kind(snapshot.watermark, "facts")
        if (
            snapshot.watermark == self._latest_watermark
            and self._latest_fact_snapshot_fingerprint is not None
            and fingerprint != self._latest_fact_snapshot_fingerprint
        ):
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError(
                "snapshot_cursor_conflict",
                "same fact snapshot cursor carries different canonical facts",
            )

    def _commit_fact_snapshot(
        self,
        *,
        snapshot: ReconciliationSnapshot,
        fingerprint: str,
    ) -> None:
        self._latest_account = snapshot.account
        self._latest_positions = tuple(snapshot.positions)
        self._latest_watermark = snapshot.watermark
        self._latest_fact_snapshot_fingerprint = fingerprint
        self._latest_snapshot_kind = "facts"

    def _require_snapshot_consistency(
        self,
        watermark: int,
        account: AccountSnapshot,
        positions: Sequence[PositionFact],
        snapshot_fingerprint: str,
    ) -> None:
        self._require_snapshot_kind(watermark, "events")
        if (
            watermark == self._latest_watermark
            and self._latest_account is not None
            and (
                account != self._latest_account
                or tuple(positions) != self._latest_positions
                or snapshot_fingerprint != self._latest_snapshot_fingerprint
            )
        ):
            self._state = RuntimeConnectionState.DEGRADED
            raise RuntimeRecoveryError("snapshot_cursor_conflict", "same snapshot cursor carries different canonical facts")

    @staticmethod
    def _snapshot_event_fingerprint(
        order_events: Sequence[tuple[str, Mapping[str, object]]],
        fill_events: Sequence[tuple[str, Mapping[str, object]]],
    ) -> str:
        rows = []
        for event_id, raw in (*order_events, *fill_events):
            rows.append((event_id, tuple(sorted((str(key), repr(value)) for key, value in raw.items()))))
        return repr(tuple(rows))

    @staticmethod
    def _validate_snapshot(
        snapshot: Mapping[str, object],
    ) -> tuple[
        Sequence[tuple[str, Mapping[str, object]]],
        Sequence[tuple[str, Mapping[str, object]]],
        Sequence[PositionFact],
        AccountSnapshot,
        int,
    ]:
        required = {"order_events", "fill_events", "positions", "account", "watermark"}
        if not isinstance(snapshot, Mapping) or not required.issubset(snapshot):
            raise RuntimeRecoveryError(
                "reconciliation_snapshot_incomplete",
                "snapshot must include order_events, fill_events, positions, account, and watermark",
            )
        watermark = snapshot["watermark"]
        if type(watermark) is not int or watermark < 0:
            raise RuntimeRecoveryError("reconciliation_watermark_missing", "snapshot watermark must be a non-negative integer cursor")
        order_events = snapshot["order_events"]
        fill_events = snapshot["fill_events"]
        if not isinstance(order_events, Sequence) or isinstance(order_events, (str, bytes, bytearray)):
            raise RuntimeRecoveryError("reconciliation_order_snapshot_invalid", "order_events must be a sequence")
        if not isinstance(fill_events, Sequence) or isinstance(fill_events, (str, bytes, bytearray)):
            raise RuntimeRecoveryError("reconciliation_fill_snapshot_invalid", "fill_events must be a sequence")
        if not HyperliquidRuntimeReconciliationAdapter._fact_complete(snapshot["account"]):
            raise RuntimeRecoveryError("reconciliation_account_snapshot_incomplete", "account fact is incomplete")
        if not HyperliquidRuntimeReconciliationAdapter._fact_complete(snapshot["positions"]):
            raise RuntimeRecoveryError("reconciliation_position_snapshot_incomplete", "position facts are incomplete")
        return order_events, fill_events, snapshot["positions"], snapshot["account"], watermark

    def _remember_order_aliases(self, raw_entity_key: str | None, result: object) -> None:
        order_id = getattr(result, "order_id", None)
        if raw_entity_key is None or order_id is None:
            return
        root = f"order:{order_id}"
        aliases = {raw_entity_key}
        broker_lineage = getattr(result, "broker_order_lineage", ())
        client_lineage = getattr(result, "client_order_lineage", ())
        for broker_order_id in broker_lineage:
            aliases.add(f"order_entity:{broker_order_id}:None")
            for client_order_id in client_lineage:
                aliases.add(f"order_entity:{broker_order_id}:{client_order_id}")
        for client_order_id in client_lineage:
            aliases.add(f"order_entity:None:{client_order_id}")
        for alias in aliases:
            existing_root = self._order_aliases.get(alias)
            if existing_root is not None and existing_root != root:
                self._state = RuntimeConnectionState.DEGRADED
                raise RuntimeRecoveryError("order_lineage_conflict", "Broker/client lineage belongs to another canonical order")
            self._order_aliases[alias] = root
        for broker_order_id in broker_lineage:
            existing_root = self._order_id_roots.get(str(broker_order_id))
            if existing_root is not None and existing_root != root:
                self._state = RuntimeConnectionState.DEGRADED
                raise RuntimeRecoveryError("order_lineage_conflict", "Broker order identity belongs to another canonical order")
            self._order_id_roots[str(broker_order_id)] = root
        for client_order_id in client_lineage:
            existing_root = self._client_id_roots.get(str(client_order_id))
            if existing_root is not None and existing_root != root:
                self._state = RuntimeConnectionState.DEGRADED
                raise RuntimeRecoveryError("order_lineage_conflict", "client order identity belongs to another canonical order")
            self._client_id_roots[str(client_order_id)] = root

    @staticmethod
    def _require_local_callback(callback: Callable[[], object]) -> None:
        if not callable(callback):
            raise RuntimeRecoveryError("callback_required", "reconciliation callback must be callable")
        HyperliquidRuntimeReconciliationAdapter._require_local_component(
            callback,
            "non_local_callback",
            "reconciliation callbacks must declare local_only=True",
        )

    @staticmethod
    def _require_local_component(component: object, reason_code: str, detail: str) -> None:
        if getattr(component, "local_only", False) is not True:
            raise RuntimeRecoveryError(
                reason_code,
                detail,
            )

    @staticmethod
    def _provenance_complete(value: object) -> bool:
        return bool(
            isinstance(value, Provenance)
            and value.transport_state == "local_fixture"
            and value.source
            and value.execution_scope
            and value.mapping_revision
            and isinstance(value.received_at, datetime)
        )

    @staticmethod
    def _fact_complete(value: object) -> bool:
        if isinstance(value, AccountSnapshot):
            return bool(
                value.broker_id == "hyperliquid"
                and value.account_address
                and value.environment is BrokerEnvironment.PAPER
                and value.exposure is not None
                and all(
                    HyperliquidRuntimeReconciliationAdapter._decimal_optional_complete(number)
                    for number in (
                        value.equity,
                        value.balance,
                        value.withdrawable,
                        value.margin_used,
                        value.exposure,
                        value.realized_pnl,
                        value.unrealized_pnl,
                    )
                )
                and HyperliquidRuntimeReconciliationAdapter._provenance_complete(value.provenance)
                and value.observation_id
                and value.provenance.received_at
                and isinstance(value.positions, tuple)
                and all(isinstance(position, PositionFact) for position in value.positions)
            )
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            if not value:
                return True
            if all(isinstance(item, FillFact) for item in value):
                return all(
                    item.fill_id
                    and item.broker_id == "hyperliquid"
                    and item.environment is BrokerEnvironment.PAPER
                    and HyperliquidRuntimeReconciliationAdapter._provenance_complete(item.provenance)
                    and item.provenance.received_at
                    and item.price is not None
                    and item.quantity is not None
                    and all(
                        HyperliquidRuntimeReconciliationAdapter._decimal_optional_complete(number)
                        for number in (item.price, item.quantity, item.closed_pnl)
                    )
                    and HyperliquidRuntimeReconciliationAdapter._fee_complete(item.fee)
                    and item.fee.fill_id == item.fill_id
                    and item.fee.instrument_id == item.instrument_id
                    and isinstance(item.fee.order_id, str)
                    and bool(item.fee.order_id.strip())
                    and item.fee.source is FeeSource.ACTUAL_FILL
                    and item.fee.state is FeeState.ACTUAL
                    and (
                        item.builder_fee is None
                        or HyperliquidRuntimeReconciliationAdapter._fee_complete(item.builder_fee)
                        and item.builder_fee.fill_id == item.fill_id
                        and item.builder_fee.instrument_id == item.instrument_id
                        and isinstance(item.builder_fee.order_id, str)
                        and bool(item.builder_fee.order_id.strip())
                        and item.builder_fee.kind is FeeKind.BUILDER
                        and item.builder_fee.source is FeeSource.ACTUAL_FILL
                        and item.builder_fee.state is FeeState.ACTUAL
                    )
                    for item in value
                )
            if all(isinstance(item, PositionFact) for item in value):
                return all(
                    item.instrument_id
                    and item.broker_id == "hyperliquid"
                    and item.environment is BrokerEnvironment.PAPER
                    and HyperliquidRuntimeReconciliationAdapter._provenance_complete(item.provenance)
                    and item.observation_id
                    and item.provenance.received_at
                    and item.signed_quantity is not None
                    and all(
                        HyperliquidRuntimeReconciliationAdapter._decimal_optional_complete(number)
                        for number in (
                            item.signed_quantity,
                            item.entry_price,
                            item.leverage,
                            item.liquidation_price,
                            item.margin_used,
                            item.position_value,
                            item.unrealized_pnl,
                        )
                    )
                    for item in value
                )
            return False
        return False

    @staticmethod
    def _fee_complete(value: object) -> bool:
        return bool(
            isinstance(value, FeeEvent)
            and isinstance(value.fee_id, str)
            and bool(value.fee_id.strip())
            and value.broker_id == "hyperliquid"
            and isinstance(value.kind, FeeKind)
            and isinstance(value.amount, Decimal)
            and value.amount.is_finite()
            and isinstance(value.currency, str)
            and bool(value.currency.strip())
            and isinstance(value.occurred_at, datetime)
            and isinstance(value.source, FeeSource)
            and isinstance(value.state, FeeState)
            and value.environment is BrokerEnvironment.PAPER
            and HyperliquidRuntimeReconciliationAdapter._provenance_complete(value.provenance)
        )

    @staticmethod
    def _decimal_optional_complete(value: object) -> bool:
        return value is None or (isinstance(value, Decimal) and value.is_finite())

    @staticmethod
    def _positions_match(
        account_positions: Sequence[PositionFact],
        queried_positions: Sequence[PositionFact],
    ) -> bool:
        return Counter(account_positions) == Counter(queried_positions)

    def event_sources(self, canonical_key: str) -> frozenset[ObservationSource]:
        """Return source evidence recorded for one canonical observation identity."""

        with self._lock:
            return frozenset(self._event_sources.get(canonical_key, set()))
