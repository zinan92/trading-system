"""Attended, single-order Hyperliquid Testnet canary coordinator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from services.journal_store import load_json, write_json
from services.park_confirmation import ParkConfirmationLedger
from services.strategy_control_plane import production_mutation_lock


CANARY_SCHEMA = "standard-broker-testnet-canary-v1"
EXTERNAL_PROFILE = "hyperliquid-testnet-default"
EXTERNAL_ENVIRONMENT = "testnet"
MAX_ALLOWED_LOSS_USD = Decimal("50")
MAX_MARKET_FACT_AGE_SECONDS = Decimal("120")
DEFAULT_MARKET_SOURCES = frozenset({"hyperliquid.external_testnet", "nautilus-hyperliquid.testnet"})
_DIGEST_PREFIX = "sha256:"
_KNOWN_RECEIPT_STATES = {
    "submitting",
    "resting",
    "waiting_for_fill",
    "waiting_for_trigger",
    "partially_filled",
    "filled",
    "cancel_pending",
    "canceled",
    "modify_pending",
    "rejected",
    "unknown",
}
_MARKET_FACT_FIELDS = (
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
_PLAN_FIELDS = (
    "canary_id",
    "broker_id",
    "environment",
    "profile_id",
    "account_fingerprint",
    "runtime_id",
    "release_sha",
    "capability_revision",
    "instrument_id",
    "direction",
    "quantity",
    "contract_multiplier",
    "quantity_step",
    "order_type",
    "entry_price",
    "price_tick",
    "time_in_force",
    "max_slippage",
    "max_notional",
    "max_leverage",
    "account_equity",
    "max_open_orders",
    "max_open_positions",
    "close_price",
    "fee_reserve_usd",
    "entry_fee_estimate_usd",
    "exit_fee_estimate_usd",
    "explicit_loss_buffer_usd",
    "max_loss_usd",
    "close_mode",
    "expires_at",
)


class TestnetCanaryError(RuntimeError):
    """Durable canary blocker; callers must not continue or retry blindly."""

    __test__ = False


def _canonical(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_canonical(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if hasattr(value, "value"):
        return _canonical(value.value)
    raise TypeError(f"unsupported canary digest value: {type(value).__name__}")


def _digest(value: object) -> str:
    payload = json.dumps(
        _canonical(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _DIGEST_PREFIX + hashlib.sha256(payload).hexdigest()


def canary_plan_digest(value: Mapping[str, Any]) -> str:
    """Return the digest of the plan fields, excluding its supplied digest."""

    return _digest({field: value.get(field) for field in _PLAN_FIELDS})


def market_fact_digest(value: Mapping[str, Any]) -> str:
    """Digest the allowlisted canonical market fact, excluding its digest."""

    return _digest({field: value.get(field) for field in _MARKET_FACT_FIELDS})


def _text(value: object, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise TestnetCanaryError(f"{field} is required")
    return result


def _decimal(value: object, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise TestnetCanaryError(f"{field} must be numeric") from exc
    if not result.is_finite() or result <= 0:
        raise TestnetCanaryError(f"{field} must be positive and finite")
    return result


def _nonnegative_decimal(value: object, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise TestnetCanaryError(f"{field} must be numeric") from exc
    if not result.is_finite() or result < 0:
        raise TestnetCanaryError(f"{field} must be non-negative and finite")
    return result


def _timestamp(value: object, field: str) -> datetime:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise TestnetCanaryError(f"{field} must be an ISO timestamp") from exc
    if result.tzinfo is None:
        raise TestnetCanaryError(f"{field} must include a timezone")
    return result.astimezone(timezone.utc)


@dataclass(frozen=True)
class TestnetCanaryOrderRequest:
    order_id: str
    instrument_id: str
    side: str
    quantity: Decimal
    order_type: str
    limit_price: Decimal
    time_in_force: str
    idempotency_key: str
    reduce_only: bool = False
    close_position: bool = False


@runtime_checkable
class TestnetCanaryOrderPort(Protocol):
    """Canary-only public order seam; implementations own Broker mapping."""

    def preflight(self) -> Mapping[str, Any]:
        ...

    def submit(self, request: TestnetCanaryOrderRequest) -> object:
        ...

    def query(self, order_id: str) -> object:
        ...

    def query_by_idempotency_key(self, idempotency_key: str) -> object:
        ...

    def replace(self, order_id: str, request: TestnetCanaryOrderRequest) -> object:
        ...

    def cancel(self, order_id: str) -> object:
        ...

    def market_fact(self, *, instrument_id: str, now: datetime) -> Mapping[str, Any]:
        ...


@dataclass(frozen=True)
class TestnetCanaryPlan:
    canary_id: str
    plan_digest: str
    broker_id: str
    environment: str
    profile_id: str
    account_fingerprint: str
    runtime_id: str
    release_sha: str
    capability_revision: str
    instrument_id: str
    direction: str
    quantity: Decimal
    contract_multiplier: Decimal
    quantity_step: Decimal
    order_type: str
    entry_price: Decimal
    price_tick: Decimal
    time_in_force: str
    max_slippage: Decimal
    max_notional: Decimal
    max_leverage: Decimal
    account_equity: Decimal
    max_open_orders: int
    max_open_positions: int
    close_price: Decimal
    fee_reserve_usd: Decimal
    entry_fee_estimate_usd: Decimal
    exit_fee_estimate_usd: Decimal
    explicit_loss_buffer_usd: Decimal
    max_loss_usd: Decimal
    close_mode: str
    expires_at: str

    __test__ = False

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> "TestnetCanaryPlan":
        if not isinstance(value, Mapping):
            raise TestnetCanaryError("canary plan must be an object")
        missing = [field for field in (*_PLAN_FIELDS, "plan_digest") if field not in value]
        if missing:
            raise TestnetCanaryError("canary plan fields missing: " + ", ".join(missing))
        allowed_fields = set(_PLAN_FIELDS) | {"plan_digest"}
        extra = sorted(str(field) for field in value if str(field) not in allowed_fields)
        if extra:
            raise TestnetCanaryError("canary plan fields unknown: " + ", ".join(extra))
        try:
            plan = cls(
                canary_id=_text(value["canary_id"], "canary_id"),
                plan_digest=_text(value["plan_digest"], "plan_digest"),
                broker_id=_text(value["broker_id"], "broker_id").lower(),
                environment=_text(value["environment"], "environment").lower(),
                profile_id=_text(value["profile_id"], "profile_id"),
                account_fingerprint=_text(value["account_fingerprint"], "account_fingerprint"),
                runtime_id=_text(value["runtime_id"], "runtime_id"),
                release_sha=_text(value["release_sha"], "release_sha"),
                capability_revision=_text(value["capability_revision"], "capability_revision"),
                instrument_id=_text(value["instrument_id"], "instrument_id"),
                direction=_text(value["direction"], "direction").lower(),
                quantity=_decimal(value["quantity"], "quantity"),
                contract_multiplier=_decimal(value["contract_multiplier"], "contract_multiplier"),
                quantity_step=_decimal(value["quantity_step"], "quantity_step"),
                order_type=_text(value["order_type"], "order_type").lower(),
                entry_price=_decimal(value["entry_price"], "entry_price"),
                price_tick=_decimal(value["price_tick"], "price_tick"),
                time_in_force=_text(value["time_in_force"], "time_in_force").lower(),
                max_slippage=_decimal(value["max_slippage"], "max_slippage"),
                max_notional=_decimal(value["max_notional"], "max_notional"),
                max_leverage=_decimal(value["max_leverage"], "max_leverage"),
                account_equity=_decimal(value["account_equity"], "account_equity"),
                max_open_orders=int(value["max_open_orders"]),
                max_open_positions=int(value["max_open_positions"]),
                close_price=_decimal(value["close_price"], "close_price"),
                fee_reserve_usd=_nonnegative_decimal(value["fee_reserve_usd"], "fee_reserve_usd"),
                entry_fee_estimate_usd=_nonnegative_decimal(value["entry_fee_estimate_usd"], "entry_fee_estimate_usd"),
                exit_fee_estimate_usd=_nonnegative_decimal(value["exit_fee_estimate_usd"], "exit_fee_estimate_usd"),
                explicit_loss_buffer_usd=_nonnegative_decimal(value["explicit_loss_buffer_usd"], "explicit_loss_buffer_usd"),
                max_loss_usd=_decimal(value["max_loss_usd"], "max_loss_usd"),
                close_mode=_text(value["close_mode"], "close_mode"),
                expires_at=_text(value["expires_at"], "expires_at"),
            )
        except (TypeError, ValueError) as exc:
            raise TestnetCanaryError("canary plan numeric field is invalid") from exc
        plan.validate(now=now)
        if plan.plan_digest != canary_plan_digest(plan.to_mapping()):
            raise TestnetCanaryError("plan_digest does not match canonical plan fields")
        return plan

    def validate(self, *, now: datetime | None = None) -> None:
        if self.broker_id != "hyperliquid":
            raise TestnetCanaryError("canary broker must be hyperliquid")
        if self.environment != EXTERNAL_ENVIRONMENT:
            raise TestnetCanaryError("canary environment must be testnet")
        if self.profile_id != EXTERNAL_PROFILE:
            raise TestnetCanaryError("canary profile must be hyperliquid-testnet-default")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.account_fingerprint.lower()):
            raise TestnetCanaryError("account_fingerprint must be a sha256 fingerprint")
        if not re.fullmatch(r"[0-9a-f]{40}", self.release_sha.lower()):
            raise TestnetCanaryError("release_sha must be a commit SHA")
        if self.direction not in {"buy", "sell"}:
            raise TestnetCanaryError("canary direction must be buy or sell")
        if self.order_type not in {"limit", "ioc_limit"}:
            raise TestnetCanaryError("canary order_type must be limit or ioc_limit")
        if self.time_in_force not in {"gtc", "ioc", "alo"}:
            raise TestnetCanaryError("canary time_in_force is unsupported")
        if self.close_mode != "ordinary_reduce_only_close":
            raise TestnetCanaryError("canary close_mode must be ordinary_reduce_only_close")
        if self.direction == "buy" and self.close_price >= self.entry_price:
            raise TestnetCanaryError("buy close_price must be below entry_price")
        if self.direction == "sell" and self.close_price <= self.entry_price:
            raise TestnetCanaryError("sell close_price must be above entry_price")
        if self.quantity % self.quantity_step != 0:
            raise TestnetCanaryError("quantity does not satisfy explicit quantity_step")
        if self.entry_price % self.price_tick != 0 or self.close_price % self.price_tick != 0:
            raise TestnetCanaryError("price does not satisfy explicit price_tick")
        if self.max_open_orders < 1 or self.max_open_positions < 1:
            raise TestnetCanaryError("canary open-order and position limits must be positive")
        if self.max_loss_usd > MAX_ALLOWED_LOSS_USD:
            raise TestnetCanaryError("max_loss_usd exceeds the 50 USD canary ceiling")
        self._validate_price_risk(self.entry_price)
        expires = _timestamp(self.expires_at, "expires_at")
        current = now.astimezone(timezone.utc) if now is not None else datetime.now(timezone.utc)
        if expires <= current:
            raise TestnetCanaryError("canary plan is expired")
        if (
            not isinstance(self.plan_digest, str)
            or not self.plan_digest.startswith(_DIGEST_PREFIX)
            or len(self.plan_digest) != 71
        ):
            raise TestnetCanaryError("plan_digest must be a sha256 digest")
        if self.plan_digest != canary_plan_digest(self.to_mapping()):
            raise TestnetCanaryError("plan_digest does not match canonical plan fields")

    def _validate_price_risk(self, entry_price: Decimal) -> None:
        risk = self.risk_snapshot(entry_price=entry_price)
        if risk["notional"] > self.max_notional:
            raise TestnetCanaryError("entry notional exceeds max_notional")
        if risk["leverage"] > self.max_leverage:
            raise TestnetCanaryError("entry leverage exceeds max_leverage")
        if risk["worst_case_loss_usd"] > self.max_loss_usd:
            raise TestnetCanaryError("computed worst-case loss exceeds max_loss_usd")

    def risk_snapshot(self, *, entry_price: Decimal | None = None) -> dict[str, Decimal]:
        price = entry_price or self.entry_price
        notional = price * self.quantity * self.contract_multiplier
        gross_stop_loss = (
            max(Decimal(0), price - self.close_price)
            if self.direction == "buy"
            else max(Decimal(0), self.close_price - price)
        ) * self.quantity * self.contract_multiplier
        max_slippage_loss = self.max_slippage * self.quantity * self.contract_multiplier
        return {
            "notional": notional,
            "leverage": notional / self.account_equity,
            "gross_stop_loss_usd": gross_stop_loss,
            "max_slippage_loss_usd": max_slippage_loss,
            "entry_fee_estimate_usd": self.entry_fee_estimate_usd,
            "exit_fee_estimate_usd": self.exit_fee_estimate_usd,
            "fee_reserve_usd": self.fee_reserve_usd,
            "explicit_loss_buffer_usd": self.explicit_loss_buffer_usd,
            "worst_case_loss_usd": (
                gross_stop_loss
                + max_slippage_loss
                + self.entry_fee_estimate_usd
                + self.exit_fee_estimate_usd
                + self.fee_reserve_usd
                + self.explicit_loss_buffer_usd
            ),
        }

    def to_mapping(self) -> dict[str, Any]:
        return {
            "canary_id": self.canary_id,
            "plan_digest": self.plan_digest,
            "broker_id": self.broker_id,
            "environment": self.environment,
            "profile_id": self.profile_id,
            "account_fingerprint": self.account_fingerprint,
            "runtime_id": self.runtime_id,
            "release_sha": self.release_sha,
            "capability_revision": self.capability_revision,
            "instrument_id": self.instrument_id,
            "direction": self.direction,
            "quantity": self.quantity,
            "contract_multiplier": self.contract_multiplier,
            "quantity_step": self.quantity_step,
            "order_type": self.order_type,
            "entry_price": self.entry_price,
            "price_tick": self.price_tick,
            "time_in_force": self.time_in_force,
            "max_slippage": self.max_slippage,
            "max_notional": self.max_notional,
            "max_leverage": self.max_leverage,
            "account_equity": self.account_equity,
            "max_open_orders": self.max_open_orders,
            "max_open_positions": self.max_open_positions,
            "close_price": self.close_price,
            "fee_reserve_usd": self.fee_reserve_usd,
            "entry_fee_estimate_usd": self.entry_fee_estimate_usd,
            "exit_fee_estimate_usd": self.exit_fee_estimate_usd,
            "explicit_loss_buffer_usd": self.explicit_loss_buffer_usd,
            "max_loss_usd": self.max_loss_usd,
            "close_mode": self.close_mode,
            "expires_at": self.expires_at,
        }

    def order_request(self, *, entry_price: Decimal | None = None) -> TestnetCanaryOrderRequest:
        price = entry_price or self.entry_price
        return TestnetCanaryOrderRequest(
            order_id=f"{self.canary_id}:entry",
            instrument_id=self.instrument_id,
            side=self.direction,
            quantity=self.quantity,
            order_type=self.order_type,
            limit_price=price,
            time_in_force=self.time_in_force,
            idempotency_key=f"{self.canary_id}:entry:{price}",
        )


class TestnetCanary:
    """Own the attended canary state, not Broker lifecycle internals."""

    __test__ = False

    def __init__(
        self,
        output_root: Path,
        broker: TestnetCanaryOrderPort,
        *,
        park_user_id: str = "park",
        confirmation_ledger: ParkConfirmationLedger | None = None,
        clock: Callable[[], datetime] | None = None,
        max_clock_skew: timedelta = timedelta(minutes=5),
        approved_market_sources: set[str] | frozenset[str] | None = None,
    ) -> None:
        if not isinstance(broker, TestnetCanaryOrderPort):
            raise TypeError("canary broker must implement the public canary order port")
        self.output_root = Path(output_root)
        self.root = self.output_root / "standard_broker_testnet_canary"
        self.current_path = self.root / "current.json"
        self.broker = broker
        self.park_user_id = _text(park_user_id, "park_user_id")
        self.confirmation_ledger = confirmation_ledger or ParkConfirmationLedger(
            self.output_root,
            park_user_id=self.park_user_id,
        )
        if max_clock_skew < timedelta(0):
            raise ValueError("max_clock_skew cannot be negative")
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_clock_skew = max_clock_skew
        self.approved_market_sources = frozenset(
            approved_market_sources or DEFAULT_MARKET_SOURCES
        )
        if not self.approved_market_sources:
            raise ValueError("approved_market_sources cannot be empty")

    def snapshot(self, plan: TestnetCanaryPlan | None = None) -> dict[str, Any]:
        rows = load_json(self.current_path)
        state = dict(rows[-1]) if rows and isinstance(rows[-1], dict) else {}
        if plan is not None and state and state.get("plan_digest") != plan.plan_digest:
            raise TestnetCanaryError("canary plan digest does not match persisted state")
        return state

    def prepare(
        self,
        plan: TestnetCanaryPlan | Mapping[str, Any],
        *,
        confirmation: Mapping[str, Any],
        timestamp: str,
    ) -> dict[str, Any]:
        with production_mutation_lock(self.output_root):
            return self._prepare_locked(
                plan,
                confirmation=confirmation,
                timestamp=timestamp,
            )

    def _prepare_locked(
        self,
        plan: TestnetCanaryPlan | Mapping[str, Any],
        *,
        confirmation: Mapping[str, Any],
        timestamp: str,
    ) -> dict[str, Any]:
        try:
            now = self._checked_timestamp(timestamp, "timestamp")
        except TestnetCanaryError as exc:
            self._persist_invalid_blocker(plan, str(exc), timestamp=str(timestamp))
            raise
        try:
            normalized_plan = self._coerce_plan(plan, now=now)
            normalized_plan.validate(now=now)
        except TestnetCanaryError as exc:
            self._persist_invalid_blocker(plan, str(exc), timestamp=timestamp)
            raise
        plan = normalized_plan
        existing = self.snapshot()
        if existing:
            if existing.get("plan_digest") != plan.plan_digest:
                raise TestnetCanaryError("a different canary plan is already persisted")
            return existing
        preflight: Mapping[str, Any] | None = None
        try:
            preflight = self._preflight(plan, now=now)
        except TestnetCanaryError as exc:
            self._persist_blocker(
                plan,
                str(exc),
                timestamp=timestamp,
                preflight=preflight,
                confirmation=confirmation,
            )
            raise
        state = {
            "schema_version": CANARY_SCHEMA,
            "canary_id": plan.canary_id,
            "plan_digest": plan.plan_digest,
            "status": "AWAITING_ATTENDED_CONFIRMATION",
            "lifecycle_state": "AWAITING_ATTENDED_CONFIRMATION",
            "preflight_state": "PREFLIGHTED",
            "confirmation_state": "AWAITING_ATTENDED_CONFIRMATION",
            "plan": self._safe_plan(plan),
            "risk": {
                key: str(value)
                for key, value in plan.risk_snapshot().items()
            },
            "preflight": self._safe_mapping(preflight),
            "confirmation": self._safe_confirmation(confirmation),
            "orders": [],
            "lineage": [],
            "events": [],
            "next_action": "await_attended_confirmation",
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        self._event(state, "plan_accepted", timestamp=timestamp)
        self._event(state, "preflight_passed", timestamp=timestamp)
        self._save(state)
        try:
            self._require_confirmation(plan, confirmation, now)
        except TestnetCanaryError as exc:
            self._block(state, str(exc), timestamp=timestamp)
            self._save(state)
            raise
        try:
            market_fact = self._read_market_fact(plan, now=now)
            preflight = {**preflight, "market_fact": market_fact}
            state["preflight"] = self._safe_mapping(preflight)
        except TestnetCanaryError as exc:
            self._block(state, str(exc), timestamp=timestamp)
            self._save(state)
            raise
        state["status"] = "AWAITING_ATTENDED_START"
        state["lifecycle_state"] = "AWAITING_ATTENDED_START"
        state["confirmation_state"] = "CONFIRMED"
        state["next_action"] = "attended_submit_entry"
        state["confirmation"] = self._safe_confirmation(confirmation)
        self._event(state, "attended_confirmation_verified", timestamp=timestamp)
        self._event(state, "canary_prepared", timestamp=timestamp)
        self._save(state)
        try:
            self._consume_confirmation(state, timestamp=timestamp)
        except TestnetCanaryError as exc:
            self._block(state, str(exc), timestamp=timestamp)
            self._save(state)
            raise
        return state

    def submit_entry(
        self,
        plan: TestnetCanaryPlan,
        *,
        confirmation: Mapping[str, Any],
        timestamp: str,
    ) -> dict[str, Any]:
        with production_mutation_lock(self.output_root):
            return self._submit_entry_locked(
                plan,
                confirmation=confirmation,
                timestamp=timestamp,
            )

    def _submit_entry_locked(
        self,
        plan: TestnetCanaryPlan,
        *,
        confirmation: Mapping[str, Any],
        timestamp: str,
    ) -> dict[str, Any]:
        state = self._state(plan)
        try:
            now = self._checked_timestamp(timestamp, "timestamp")
        except TestnetCanaryError as exc:
            self._block(state, str(exc), timestamp=str(timestamp))
            self._save(state)
            raise
        try:
            self._require_confirmation(plan, confirmation, now, state=state)
        except TestnetCanaryError as exc:
            self._block(state, str(exc), timestamp=timestamp)
            self._save(state)
            raise
        if state["status"] in {"ENTRY_RECONCILED", "ENTRY_FILLED", "NO_FILL_RECONCILED", "BLOCKED"}:
            return state
        if state["status"] == "ENTRY_SUBMIT_INTENT_RESERVED":
            return self._recover_reserved_entry(state, plan, timestamp=timestamp)
        if state["status"] != "AWAITING_ATTENDED_START":
            raise TestnetCanaryError(f"canary cannot submit from {state['status']}")
        try:
            request = plan.order_request()
            state["status"] = "ENTRY_SUBMIT_INTENT_RESERVED"
            state["lifecycle_state"] = "ENTRY_SUBMITTED"
            state["entry_intent"] = self._safe_request(request)
            state["next_action"] = "recover_or_query_reserved_entry"
            self._event(state, "entry_submit_intent_reserved", timestamp=timestamp)
            self._save(state)
            receipt = self.broker.submit(request)
            self._append_receipt(state, receipt, timestamp=timestamp, operation="submit")
            submitted_state = self._receipt_state(receipt)
            if submitted_state in {"unknown", "rejected"}:
                raise TestnetCanaryError(f"entry submit state is {submitted_state}")
            order_id = self._safe_identifier(getattr(receipt, "order_id", ""))
            if not order_id:
                raise TestnetCanaryError("entry receipt order identity is missing")
            state["entry_order_id"] = order_id
            self._append_lineage(state, request, timestamp=timestamp, operation="submit")
            self._event(state, "entry_submitted", timestamp=timestamp, order_id=order_id)
            queried = self.broker.query(order_id)
            self._append_receipt(state, queried, timestamp=timestamp, operation="query")
            queried_state = self._receipt_state(queried)
            if queried_state in {"unknown", "rejected"}:
                raise TestnetCanaryError(f"entry query state is {queried_state}")
            if queried_state in {"filled", "partially_filled"}:
                state["status"] = "ENTRY_FILLED"
                state["lifecycle_state"] = "ENTRY_FILLED"
                state["next_action"] = "consume_fill_facts"
            else:
                state["status"] = "ENTRY_RECONCILED"
                state["lifecycle_state"] = "ENTRY_RECONCILED"
                state["next_action"] = "attended_cancel_or_replace_entry"
        except TestnetCanaryError as exc:
            self._block(state, str(exc), timestamp=timestamp)
            self._save(state)
            raise
        except Exception as exc:  # noqa: BLE001 - unknown external outcomes freeze.
            self._block(state, f"entry_unknown:{type(exc).__name__}", timestamp=timestamp)
            self._save(state)
            raise TestnetCanaryError(state["blocker"]) from exc
        state["updated_at"] = timestamp
        self._save(state)
        return state

    def _recover_reserved_entry(
        self,
        state: dict[str, Any],
        plan: TestnetCanaryPlan,
        *,
        timestamp: str,
    ) -> dict[str, Any]:
        request_data = state.get("entry_intent") or {}
        idempotency_key = str(request_data.get("idempotency_key") or "").strip()
        query_by_idempotency = getattr(self.broker, "query_by_idempotency_key", None)
        if not idempotency_key or not callable(query_by_idempotency):
            reason = "entry submit outcome unknown; durable idempotency recovery is unavailable"
            self._block(state, reason, timestamp=timestamp)
            self._save(state)
            raise TestnetCanaryError(reason)
        try:
            receipt = query_by_idempotency(idempotency_key)
            self._append_receipt(state, receipt, timestamp=timestamp, operation="recovery_query")
            receipt_state = self._receipt_state(receipt)
            if receipt_state in {"unknown", "rejected"}:
                raise TestnetCanaryError(f"reserved entry recovery state is {receipt_state}")
            order_id = self._safe_identifier(getattr(receipt, "order_id", ""))
            if not order_id:
                raise TestnetCanaryError("reserved entry recovery identity is missing")
            state["entry_order_id"] = order_id
            state["status"] = "ENTRY_FILLED" if receipt_state in {"filled", "partially_filled"} else "ENTRY_RECONCILED"
            state["lifecycle_state"] = state["status"]
            state["next_action"] = "consume_fill_facts" if state["status"] == "ENTRY_FILLED" else "attended_cancel_or_replace_entry"
            self._event(state, "entry_recovered_by_idempotency", timestamp=timestamp, order_id=order_id)
        except TestnetCanaryError as exc:
            self._block(state, str(exc), timestamp=timestamp)
            self._save(state)
            raise
        except Exception as exc:  # noqa: BLE001
            self._block(
                state,
                f"entry submit outcome unknown; idempotency recovery failed:{type(exc).__name__}",
                timestamp=timestamp,
            )
            self._save(state)
            raise TestnetCanaryError(state["blocker"]) from exc
        state["updated_at"] = timestamp
        self._save(state)
        return state

    def replace_entry(
        self,
        plan: TestnetCanaryPlan,
        *,
        entry_price: Decimal,
        confirmation: Mapping[str, Any],
        timestamp: str,
    ) -> dict[str, Any]:
        with production_mutation_lock(self.output_root):
            return self._replace_entry_locked(
                plan,
                entry_price=entry_price,
                confirmation=confirmation,
                timestamp=timestamp,
            )

    def _replace_entry_locked(
        self,
        plan: TestnetCanaryPlan,
        *,
        entry_price: Decimal,
        confirmation: Mapping[str, Any],
        timestamp: str,
    ) -> dict[str, Any]:
        state = self._state(plan)
        try:
            now = self._checked_timestamp(timestamp, "timestamp")
        except TestnetCanaryError as exc:
            self._block(state, str(exc), timestamp=str(timestamp))
            self._save(state)
            raise
        try:
            self._require_confirmation(plan, confirmation, now, state=state)
        except TestnetCanaryError as exc:
            self._block(state, str(exc), timestamp=timestamp)
            self._save(state)
            raise
        if state["status"] == "ENTRY_REPLACE_INTENT_RESERVED":
            reason = "replacement outcome unknown; manual reconciliation is required"
            self._block(state, reason, timestamp=timestamp)
            self._save(state)
            raise TestnetCanaryError(reason)
        if state["status"] != "ENTRY_RECONCILED":
            raise TestnetCanaryError("entry replace requires ENTRY_RECONCILED")
        try:
            replacement_price = _decimal(entry_price, "replacement entry_price")
            if abs(replacement_price - plan.entry_price) > plan.max_slippage:
                raise TestnetCanaryError("replacement price exceeds max_slippage")
            if replacement_price % plan.price_tick != 0:
                raise TestnetCanaryError("replacement price violates explicit price_tick")
            plan._validate_price_risk(replacement_price)
        except TestnetCanaryError as exc:
            self._block(state, str(exc), timestamp=timestamp)
            self._save(state)
            raise
        order_id = str(state.get("entry_order_id") or "")
        try:
            request = plan.order_request(entry_price=replacement_price)
            state["entry_intent"] = self._safe_request(request)
            state["status"] = "ENTRY_REPLACE_INTENT_RESERVED"
            state["next_action"] = "recover_or_query_replacement"
            self._event(state, "entry_replace_intent_reserved", timestamp=timestamp, order_id=order_id)
            self._save(state)
            receipt = self.broker.replace(order_id, request)
            self._append_receipt(state, receipt, timestamp=timestamp, operation="replace")
            self._append_lineage(state, request, timestamp=timestamp, operation="replace")
            queried = self.broker.query(order_id)
            self._append_receipt(state, queried, timestamp=timestamp, operation="query")
            queried_state = self._receipt_state(queried)
            if queried_state in {"unknown", "rejected"}:
                raise TestnetCanaryError(f"replacement query state is {queried_state}")
            if queried_state in {"filled", "partially_filled"}:
                state["status"] = "ENTRY_FILLED"
                state["lifecycle_state"] = "ENTRY_FILLED"
                state["next_action"] = "consume_fill_facts"
            else:
                state["status"] = "ENTRY_RECONCILED"
                state["lifecycle_state"] = "ENTRY_RECONCILED"
                state["next_action"] = "attended_cancel_or_replace_entry"
        except TestnetCanaryError as exc:
            self._block(state, str(exc), timestamp=timestamp)
            self._save(state)
            raise
        except Exception as exc:  # noqa: BLE001
            self._block(state, f"replace_unknown:{type(exc).__name__}", timestamp=timestamp)
            self._save(state)
            raise TestnetCanaryError(state["blocker"]) from exc
        self._event(state, "entry_replaced", timestamp=timestamp, order_id=order_id)
        state["updated_at"] = timestamp
        self._save(state)
        return state

    def cancel_entry(
        self,
        plan: TestnetCanaryPlan,
        *,
        confirmation: Mapping[str, Any],
        timestamp: str,
    ) -> dict[str, Any]:
        with production_mutation_lock(self.output_root):
            return self._cancel_entry_locked(
                plan,
                confirmation=confirmation,
                timestamp=timestamp,
            )

    def _cancel_entry_locked(
        self,
        plan: TestnetCanaryPlan,
        *,
        confirmation: Mapping[str, Any],
        timestamp: str,
    ) -> dict[str, Any]:
        state = self._state(plan)
        try:
            now = self._checked_timestamp(timestamp, "timestamp")
        except TestnetCanaryError as exc:
            self._block(state, str(exc), timestamp=str(timestamp))
            self._save(state)
            raise
        try:
            self._require_confirmation(plan, confirmation, now, state=state)
        except TestnetCanaryError as exc:
            self._block(state, str(exc), timestamp=timestamp)
            self._save(state)
            raise
        if state["status"] == "NO_FILL_RECONCILED":
            return state
        if state["status"] == "ENTRY_CANCEL_INTENT_RESERVED":
            reason = "cancel outcome unknown; manual reconciliation is required"
            self._block(state, reason, timestamp=timestamp)
            self._save(state)
            raise TestnetCanaryError(reason)
        if state["status"] != "ENTRY_RECONCILED":
            raise TestnetCanaryError("entry cancel requires ENTRY_RECONCILED")
        order_id = str(state.get("entry_order_id") or "")
        try:
            state["status"] = "ENTRY_CANCEL_INTENT_RESERVED"
            state["next_action"] = "recover_or_query_cancel"
            self._event(state, "entry_cancel_intent_reserved", timestamp=timestamp, order_id=order_id)
            self._save(state)
            receipt = self.broker.cancel(order_id)
            self._append_receipt(state, receipt, timestamp=timestamp, operation="cancel")
            queried = self.broker.query(order_id)
            self._append_receipt(state, queried, timestamp=timestamp, operation="query")
            queried_state = self._receipt_state(queried)
            if queried_state != "canceled":
                raise TestnetCanaryError("entry cancel did not reach terminal canceled state")
            filled_quantity = self._receipt_decimal(queried, "filled_quantity")
            if filled_quantity > 0:
                raise TestnetCanaryError("entry cancel observed a partial fill; CANARY-02 is required")
        except TestnetCanaryError as exc:
            self._block(state, str(exc), timestamp=timestamp)
            self._save(state)
            raise
        except Exception as exc:  # noqa: BLE001
            self._block(state, f"cancel_unknown:{type(exc).__name__}", timestamp=timestamp)
            self._save(state)
            raise TestnetCanaryError(state["blocker"]) from exc
        state["status"] = "NO_FILL_RECONCILED"
        state["lifecycle_state"] = "NO_FILL_RECONCILED"
        state["next_action"] = "record_no_fill_result"
        self._event(state, "entry_canceled_no_fill", timestamp=timestamp, order_id=order_id)
        state["updated_at"] = timestamp
        self._save(state)
        return state

    def _preflight(self, plan: TestnetCanaryPlan, *, now: datetime) -> Mapping[str, Any]:
        try:
            result = self.broker.preflight()
        except Exception as exc:  # noqa: BLE001
            raise TestnetCanaryError(f"canary_preflight_unknown:{type(exc).__name__}") from exc
        if not isinstance(result, Mapping):
            raise TestnetCanaryError("canary preflight must return a mapping")
        expected = {
            "environment": plan.environment,
            "transport_profile": plan.profile_id,
            "account_fingerprint": plan.account_fingerprint,
            "runtime_id": plan.runtime_id,
            "release_sha": plan.release_sha,
            "capability_revision": plan.capability_revision,
        }
        if result.get("canary_ready") is not True:
            raise TestnetCanaryError("canary preflight is not canary_ready")
        for key, value in expected.items():
            if result.get(key) != value:
                raise TestnetCanaryError(f"canary preflight {key} mismatch")
        if result.get("transport_state") != "external_testnet":
            raise TestnetCanaryError("canary transport is not external_testnet")
        if result.get("network_io") is not True or result.get("real_money_eligible") is not False:
            raise TestnetCanaryError("canary transport safety flags are invalid")
        if result.get("broker_operation_invoked") is not False:
            raise TestnetCanaryError("canary preflight invoked a Broker operation")
        if result.get("preflight_io_performed") is not False:
            raise TestnetCanaryError("canary preflight performed network I/O")
        if result.get("host_ready") is not True:
            raise TestnetCanaryError("canary host is not ready")
        if result.get("protection_ready") is not False or not str(result.get("protection_gap") or "").strip():
            raise TestnetCanaryError("canary protection gap is not explicit")
        if plan.order_type == "ioc_limit":
            raise TestnetCanaryError("external Testnet canary does not support ioc_limit")
        if result.get("market_fact") is not None:
            self._validate_market_fact(result.get("market_fact"), plan, now=now)
        return result

    def _read_market_fact(self, plan: TestnetCanaryPlan, *, now: datetime) -> Mapping[str, Any]:
        reader = getattr(self.broker, "market_fact", None)
        if not callable(reader):
            raise TestnetCanaryError("fresh market fact reader is unavailable")
        try:
            value = reader(instrument_id=plan.instrument_id, now=now)
        except TestnetCanaryError:
            raise
        except Exception as exc:  # noqa: BLE001 - market read is a hard admission gate.
            raise TestnetCanaryError(f"market_fact_unknown:{type(exc).__name__}") from exc
        if not isinstance(value, Mapping):
            raise TestnetCanaryError("market fact reader returned a non-mapping value")
        self._validate_market_fact(value, plan, now=now)
        return value

    @staticmethod
    def _coerce_plan(
        plan: TestnetCanaryPlan | Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> TestnetCanaryPlan:
        if isinstance(plan, TestnetCanaryPlan):
            return TestnetCanaryPlan.from_mapping(plan.to_mapping(), now=now)
        if isinstance(plan, Mapping):
            return TestnetCanaryPlan.from_mapping(plan, now=now)
        raise TestnetCanaryError("canary plan must be an object")

    def _checked_timestamp(self, value: object, field: str) -> datetime:
        parsed = _timestamp(value, field)
        wall = self.clock().astimezone(timezone.utc)
        if parsed > wall + self.max_clock_skew:
            raise TestnetCanaryError(f"{field} is too far in the future")
        return parsed

    def _persist_invalid_blocker(
        self,
        plan: object,
        reason: str,
        *,
        timestamp: str,
    ) -> None:
        if self.snapshot():
            return
        raw = (
            plan.to_mapping()
            if isinstance(plan, TestnetCanaryPlan)
            else plan if isinstance(plan, Mapping) else {}
        )
        canary_id = str(getattr(plan, "canary_id", "") or raw.get("canary_id") or "unknown")
        plan_digest = str(getattr(plan, "plan_digest", "") or raw.get("plan_digest") or "unknown")
        state = self._new_blocker_state(
            canary_id=canary_id,
            plan_digest=plan_digest,
            reason=reason,
            timestamp=timestamp,
            plan=self._safe_raw_plan(raw),
        )
        self._save(state)

    def _persist_blocker(
        self,
        plan: TestnetCanaryPlan,
        reason: str,
        *,
        timestamp: str,
        preflight: Mapping[str, Any] | None = None,
        confirmation: Mapping[str, Any] | None = None,
    ) -> None:
        if self.snapshot():
            return
        state = self._new_blocker_state(
            canary_id=plan.canary_id,
            plan_digest=plan.plan_digest,
            reason=reason,
            timestamp=timestamp,
            plan=self._safe_plan(plan),
            preflight=self._safe_mapping(preflight or {}),
            confirmation=self._safe_confirmation(confirmation or {}) if confirmation else {},
        )
        self._save(state)

    def _consume_confirmation(self, state: Mapping[str, Any], *, timestamp: str) -> None:
        proposal_id = str((state.get("confirmation") or {}).get("proposal_id") or "").strip()
        receipt_digest = str((state.get("confirmation") or {}).get("receipt_digest") or "").strip()
        if not proposal_id or not receipt_digest:
            raise TestnetCanaryError("durable confirmation receipt is required")
        path = self.output_root / "dualtrack" / "testnet_canary_confirmation_consumed.json"
        rows = load_json(path)
        if any(
            isinstance(row, dict)
            and row.get("proposal_id") == proposal_id
            for row in rows
        ):
            raise TestnetCanaryError("durable confirmation replay")
        rows.append(
            {
                "event": "testnet_canary_confirmation_consumed",
                "proposal_id": proposal_id,
                "receipt_digest": receipt_digest,
                "canary_id": state.get("canary_id"),
                "plan_digest": state.get("plan_digest"),
                "consumed_at": timestamp,
            }
        )
        write_json(path, rows)

    def _new_blocker_state(
        self,
        *,
        canary_id: str,
        plan_digest: str,
        reason: str,
        timestamp: str,
        plan: Mapping[str, Any],
        preflight: Mapping[str, Any] | None = None,
        confirmation: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        state: dict[str, Any] = {
            "schema_version": CANARY_SCHEMA,
            "canary_id": canary_id,
            "plan_digest": plan_digest,
            "status": "BLOCKED",
            "lifecycle_state": "BLOCKED",
            "plan": dict(plan),
            "risk": {},
            "preflight": dict(preflight or {}),
            "confirmation": dict(confirmation or {}),
            "orders": [],
            "lineage": [],
            "events": [],
            "next_action": "notify_park_and_wait",
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        self._block(state, reason, timestamp=timestamp)
        return state

    def _require_confirmation(
        self,
        plan: TestnetCanaryPlan,
        confirmation: Mapping[str, Any],
        now: datetime,
        *,
        state: Mapping[str, Any] | None = None,
    ) -> None:
        if not isinstance(confirmation, Mapping):
            raise TestnetCanaryError("attended confirmation is required")
        operator_id = str(
            confirmation.get("operator_id")
            or confirmation.get("park_user_id")
            or ""
        ).strip()
        confirmation_id = str(
            confirmation.get("confirmation_id")
            or confirmation.get("proposal_id")
            or ""
        ).strip()
        if (
            confirmation.get("execution_authorized") is not True
            or confirmation.get("execution_environment") != "testnet"
            or confirmation.get("canary_id") != plan.canary_id
            or confirmation.get("plan_digest") != plan.plan_digest
            or operator_id != self.park_user_id
            or not confirmation_id
            or ("event" in confirmation and confirmation.get("event") != "confirmed")
        ):
            raise TestnetCanaryError("attended confirmation identity is invalid")
        if _timestamp(confirmation.get("expires_at"), "confirmation.expires_at") <= now:
            raise TestnetCanaryError("attended confirmation is expired")
        proposal_id = str(
            confirmation.get("proposal_id")
            or confirmation.get("confirmation_id")
            or ""
        ).strip()
        receipt_digest = str(confirmation.get("receipt_digest") or "").strip()
        if not proposal_id or not receipt_digest:
            raise TestnetCanaryError("durable confirmation receipt is required")
        expected = (state or {}).get("confirmation") if state else None
        if expected is not None:
            if (
                str(expected.get("proposal_id") or expected.get("confirmation_id") or "") != proposal_id
                or str(expected.get("receipt_digest") or "") != receipt_digest
            ):
                raise TestnetCanaryError("attended confirmation receipt does not match prepared state")
            self._verify_durable_confirmation(
                plan,
                confirmation,
                now=now,
                proposal_id=proposal_id,
                receipt_digest=receipt_digest,
            )
            return
        self._verify_durable_confirmation(
            plan,
            confirmation,
            now=now,
            proposal_id=proposal_id,
            receipt_digest=receipt_digest,
        )

    def _verify_durable_confirmation(
        self,
        plan: TestnetCanaryPlan,
        confirmation: Mapping[str, Any],
        *,
        now: datetime,
        proposal_id: str,
        receipt_digest: str,
    ) -> None:
        rows = self.confirmation_ledger.rows()
        proposal = next(
            (
                row
                for row in rows
                if row.get("event") == "proposal"
                and str(row.get("proposal_id") or "") == proposal_id
            ),
            None,
        )
        decision = next(
            (
                row
                for row in reversed(rows)
                if row.get("event") == "confirmed"
                and str(row.get("proposal_id") or "") == proposal_id
            ),
            None,
        )
        if not proposal or not decision:
            raise TestnetCanaryError("durable confirmation receipt is missing")
        try:
            proposal_expires = float(proposal.get("expires_at") or 0)
        except (TypeError, ValueError) as exc:
            raise TestnetCanaryError("durable confirmation expiry is invalid") from exc
        if proposal_expires <= now.timestamp():
            raise TestnetCanaryError("durable confirmation is expired")
        if (
            proposal.get("execution_environment") != "testnet"
            or decision.get("execution_environment") != "testnet"
            or proposal.get("plan_digest") != plan.plan_digest
            or decision.get("plan_digest") != plan.plan_digest
            or decision.get("execution_authorized") is not True
            or decision.get("receipt_digest") != receipt_digest
            or decision.get("park_user_id") != self.park_user_id
        ):
            raise TestnetCanaryError("durable confirmation receipt does not match plan")

    def _state(self, plan: TestnetCanaryPlan) -> dict[str, Any]:
        state = self.snapshot(plan)
        if not state:
            raise TestnetCanaryError("canary has not been prepared")
        try:
            plan.validate()
        except Exception as exc:  # noqa: BLE001 - malformed typed plan freezes the canary.
            reason = str(exc) or f"plan_invalid:{type(exc).__name__}"
            self._block(state, reason, timestamp=state.get("updated_at") or datetime.now(timezone.utc).isoformat())
            self._save(state)
            raise TestnetCanaryError(reason) from exc
        return state

    @staticmethod
    def _safe_plan(plan: TestnetCanaryPlan) -> dict[str, Any]:
        # Journal rows must be JSON-safe while retaining exact decimal text.
        return {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in plan.to_mapping().items()
        }

    @staticmethod
    def _safe_raw_plan(value: Mapping[str, Any]) -> dict[str, Any]:
        allowed = set(_PLAN_FIELDS) | {"plan_digest"}
        return {
            str(key): str(item) if isinstance(item, Decimal) else item
            for key, item in value.items()
            if str(key) in allowed and isinstance(item, (str, int, float, bool, Decimal))
        }

    @staticmethod
    def _safe_confirmation(value: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: str(value[key])
            for key in (
                "operator_id",
                "park_user_id",
                "confirmation_id",
                "proposal_id",
                "receipt_digest",
                "event",
                "execution_environment",
                "canary_id",
                "plan_digest",
                "expires_at",
            )
            if key in value and value[key] is not None
        }

    @staticmethod
    def _safe_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {
            "canary_ready",
            "host_ready",
            "environment",
            "transport_profile",
            "transport_state",
            "account_fingerprint",
            "runtime_id",
            "release_sha",
            "capability_revision",
            "network_io",
            "real_money_eligible",
            "upstream_receipt_digest",
            "market_fact",
            "broker_operation_invoked",
            "preflight_io_performed",
            "protection_ready",
            "protection_gap",
        }
        result = {str(key): value[key] for key in sorted(allowed.intersection(value))}
        if isinstance(result.get("market_fact"), Mapping):
            result["market_fact"] = TestnetCanary._safe_market_fact(result["market_fact"])
        return result

    @staticmethod
    def _safe_market_fact(value: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {
            "instrument_id",
            "contract_multiplier",
            "price",
            "freshness",
            "observed_at",
            "source",
            "transport_state",
            "mapping_revision",
            "max_age_seconds",
            "fact_digest",
        }
        result: dict[str, Any] = {}
        for key in sorted(allowed.intersection(value)):
            item = value[key]
            if isinstance(item, Decimal):
                result[str(key)] = str(item)
            elif isinstance(item, (str, int, float, bool)) or item is None:
                result[str(key)] = item
        return result

    def _validate_market_fact(
        self,
        value: object,
        plan: TestnetCanaryPlan,
        *,
        now: datetime,
    ) -> None:
        if not isinstance(value, Mapping):
            raise TestnetCanaryError("market fact is missing or ambiguous")
        if value.get("instrument_id") != plan.instrument_id:
            raise TestnetCanaryError("market fact instrument identity mismatch")
        try:
            multiplier = Decimal(str(value.get("contract_multiplier")))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise TestnetCanaryError("market fact contract multiplier is invalid") from exc
        if multiplier != plan.contract_multiplier:
            raise TestnetCanaryError("market fact contract multiplier mismatch")
        if value.get("freshness") != "fresh":
            raise TestnetCanaryError("market fact is stale or unknown")
        if value.get("transport_state") != "external_testnet":
            raise TestnetCanaryError("market fact transport identity mismatch")
        if value.get("mapping_revision") != plan.capability_revision:
            raise TestnetCanaryError("market fact capability mismatch")
        source = str(value.get("source") or "").strip().lower()
        if source not in self.approved_market_sources:
            raise TestnetCanaryError("market fact source is not an approved public seam")
        observed = _timestamp(value.get("observed_at"), "market_fact.observed_at")
        if observed > now:
            raise TestnetCanaryError("market fact timestamp is in the future")
        max_age = _decimal(value.get("max_age_seconds"), "market_fact.max_age_seconds")
        if max_age > MAX_MARKET_FACT_AGE_SECONDS:
            raise TestnetCanaryError("market fact max age exceeds local safety bound")
        if now - observed > timedelta(seconds=float(max_age)):
            raise TestnetCanaryError("market fact is stale or unknown")
        price = _decimal(value.get("price"), "market_fact.price")
        if price % plan.price_tick != 0:
            raise TestnetCanaryError("market fact price violates explicit price_tick")
        if abs(price - plan.entry_price) > plan.max_slippage:
            raise TestnetCanaryError("entry price exceeds fresh market slippage bound")
        if value.get("fact_digest") != market_fact_digest(value):
            raise TestnetCanaryError("market fact digest is invalid")

    @staticmethod
    def _receipt_state(receipt: object) -> str:
        state = getattr(receipt, "state", "")
        normalized = str(getattr(state, "value", state) or "").lower()
        return normalized if normalized in _KNOWN_RECEIPT_STATES else "unknown"

    def _append_receipt(
        self,
        state: dict[str, Any],
        receipt: object,
        *,
        timestamp: str,
        operation: str,
        request: TestnetCanaryOrderRequest | None = None,
    ) -> None:
        order_id = self._safe_identifier(getattr(receipt, "order_id", ""))
        if not order_id:
            raise TestnetCanaryError(f"{operation} receipt order identity is missing")
        expected_order_id = str(state.get("entry_order_id") or "").strip()
        if (
            expected_order_id
            and order_id != expected_order_id
            and not (request is not None and request.close_position)
        ):
            raise TestnetCanaryError(
                f"{operation} receipt order identity contradicts entry order"
            )
        self._validate_receipt_identity(state, receipt, operation=operation)
        self._validate_receipt_payload(
            state,
            receipt,
            operation=operation,
            request=request,
        )
        provenance = getattr(receipt, "provenance", None)
        safe_provenance = {
            key: self._safe_identifier(getattr(provenance, key))
            for key in ("source", "execution_scope", "transport_state", "mapping_revision")
            if provenance is not None and getattr(provenance, key, None) is not None
        }
        reason = str(getattr(receipt, "reason", "") or "")
        if reason not in {
            "",
            "accepted",
            "pending",
            "resting",
            "open",
            "filled",
            "partially_filled",
            "canceled",
            "rejected",
            "expired",
        }:
            reason = ""
        state.setdefault("orders", []).append(
            {
                "operation": operation,
                "order_id": order_id,
                "client_order_id": self._safe_identifier(getattr(receipt, "client_order_id", "")),
                "broker_order_id": self._safe_identifier(getattr(receipt, "broker_order_id", "")),
                "state": self._receipt_state(receipt),
                "original_quantity": self._safe_decimal_text(getattr(receipt, "original_quantity", "")),
                "filled_quantity": self._safe_decimal_text(getattr(receipt, "filled_quantity", "")),
                "remaining_quantity": self._safe_decimal_text(getattr(receipt, "remaining_quantity", "")),
                "average_fill_price": self._safe_decimal_text(getattr(receipt, "average_fill_price", "")),
                "reason": reason,
                "environment": "testnet",
                "account_fingerprint": state["plan"]["account_fingerprint"],
                "release_sha": state["plan"]["release_sha"],
                "provenance": safe_provenance,
                "observed_at": timestamp,
            }
        )

    def _validate_receipt_identity(
        self,
        state: Mapping[str, Any],
        receipt: object,
        *,
        operation: str,
    ) -> None:
        plan = state.get("plan") or {}
        broker_id = str(getattr(receipt, "broker_id", "") or "").strip().lower()
        environment = str(
            getattr(getattr(receipt, "environment", ""), "value", getattr(receipt, "environment", ""))
            or ""
        ).strip().lower()
        if broker_id != str(plan.get("broker_id") or "").lower():
            raise TestnetCanaryError(f"{operation} receipt broker identity mismatch")
        if environment != str(plan.get("environment") or "").lower():
            raise TestnetCanaryError(f"{operation} receipt environment identity mismatch")
        account_value = getattr(receipt, "account_fingerprint", None)
        if account_value is None:
            account_value = getattr(receipt, "account_address", None)
        account_text = str(account_value or "").strip()
        if account_text.startswith("sha256:"):
            account_fingerprint = account_text
        elif account_text:
            account_fingerprint = _DIGEST_PREFIX + hashlib.sha256(account_text.encode("utf-8")).hexdigest()
        else:
            account_fingerprint = ""
        if account_fingerprint != str(plan.get("account_fingerprint") or ""):
            raise TestnetCanaryError(f"{operation} receipt account identity mismatch")
        lifecycle_id = str(getattr(receipt, "lifecycle_id", "") or "").strip()
        release_sha = str(getattr(receipt, "release_sha", "") or "").strip()
        if lifecycle_id != str(plan.get("runtime_id") or ""):
            raise TestnetCanaryError(f"{operation} receipt runtime identity mismatch")
        if release_sha != str(plan.get("release_sha") or ""):
            raise TestnetCanaryError(f"{operation} receipt release identity mismatch")
        provenance = getattr(receipt, "provenance", None)
        transport_state = str(getattr(provenance, "transport_state", "") or "").strip()
        mapping_revision = str(getattr(provenance, "mapping_revision", "") or "").strip()
        if transport_state != "external_testnet":
            raise TestnetCanaryError(f"{operation} receipt transport identity mismatch")
        if mapping_revision != str(plan.get("capability_revision") or ""):
            raise TestnetCanaryError(f"{operation} receipt capability identity mismatch")

    def _validate_receipt_payload(
        self,
        state: Mapping[str, Any],
        receipt: object,
        *,
        operation: str,
        request: TestnetCanaryOrderRequest | None = None,
    ) -> None:
        for field in ("client_order_id", "broker_order_id"):
            if not self._safe_identifier(getattr(receipt, field, "")):
                raise TestnetCanaryError(f"{operation} receipt {field} is missing")
        original = self._receipt_decimal(receipt, "original_quantity")
        filled = self._receipt_decimal(receipt, "filled_quantity")
        remaining = self._receipt_decimal(receipt, "remaining_quantity")
        if original <= 0 or filled + remaining > original:
            raise TestnetCanaryError(f"{operation} receipt quantity is contradictory")
        average = getattr(receipt, "average_fill_price", None)
        if average is not None and average != "":
            average_price = self._receipt_decimal_value(average, f"{operation} average_fill_price")
            if average_price <= 0:
                raise TestnetCanaryError(f"{operation} receipt average_fill_price is invalid")
        request_data = self._safe_request(request) if request is not None else state.get("entry_intent") or {}
        expected_instrument = str(request_data.get("instrument_id") or "")
        expected_side = str(request_data.get("side") or "")
        expected_order_type = str(request_data.get("order_type") or "")
        expected_tif = str(request_data.get("time_in_force") or "")
        if (
            str(getattr(receipt, "instrument_id", "") or "") != expected_instrument
            or str(getattr(getattr(receipt, "side", ""), "value", getattr(receipt, "side", "")) or "") != expected_side
            or str(getattr(receipt, "order_type", "") or "") != expected_order_type
            or str(getattr(receipt, "time_in_force", "") or "") != expected_tif
        ):
            raise TestnetCanaryError(f"{operation} receipt request identity mismatch")
        receipt_quantity = self._receipt_decimal_value(getattr(receipt, "quantity", None), f"{operation} quantity")
        if receipt_quantity != self._receipt_decimal_value(request_data.get("quantity"), f"{operation} request quantity"):
            raise TestnetCanaryError(f"{operation} receipt quantity identity mismatch")
        receipt_price = self._receipt_decimal_value(getattr(receipt, "limit_price", None), f"{operation} limit_price")
        if receipt_price != self._receipt_decimal_value(request_data.get("limit_price"), f"{operation} request limit_price"):
            raise TestnetCanaryError(f"{operation} receipt price identity mismatch")

    def _append_lineage(
        self,
        state: dict[str, Any],
        request: TestnetCanaryOrderRequest,
        *,
        timestamp: str,
        operation: str,
    ) -> None:
        state.setdefault("lineage", []).append(
            {
                "operation": operation,
                "original_order_id": state.get("entry_order_id") or request.order_id,
                "request_order_id": request.order_id,
                "idempotency_key": self._safe_identifier(request.idempotency_key),
                "instrument_id": self._safe_identifier(request.instrument_id),
                "side": self._safe_identifier(request.side),
                "quantity": self._safe_decimal_text(request.quantity),
                "limit_price": self._safe_decimal_text(request.limit_price),
                "observed_at": timestamp,
            }
        )

    @classmethod
    def _safe_request(cls, request: TestnetCanaryOrderRequest) -> dict[str, Any]:
        return {
            "order_id": cls._safe_identifier(request.order_id),
            "instrument_id": cls._safe_identifier(request.instrument_id),
            "side": cls._safe_identifier(request.side),
            "quantity": cls._safe_decimal_text(request.quantity),
            "order_type": cls._safe_identifier(request.order_type),
            "limit_price": cls._safe_decimal_text(request.limit_price),
            "time_in_force": cls._safe_identifier(request.time_in_force),
            "idempotency_key": cls._safe_identifier(request.idempotency_key),
            "reduce_only": bool(request.reduce_only),
            "close_position": bool(request.close_position),
        }

    @staticmethod
    def _safe_identifier(value: object) -> str:
        text = str(getattr(value, "value", value) or "").strip()
        if not text or len(text) > 200 or not re.fullmatch(r"[A-Za-z0-9_.:/-]+", text):
            return ""
        return text

    @staticmethod
    def _safe_decimal_text(value: object) -> str:
        if value is None or value == "":
            return ""
        try:
            parsed = Decimal(str(getattr(value, "value", value)))
        except (InvalidOperation, TypeError, ValueError):
            return ""
        return str(parsed) if parsed.is_finite() else ""

    @staticmethod
    def _receipt_decimal(receipt: object, field: str) -> Decimal:
        return TestnetCanary._receipt_decimal_value(getattr(receipt, field, None), f"receipt {field}")

    @staticmethod
    def _receipt_decimal_value(value: object, field: str) -> Decimal:
        try:
            parsed = Decimal(str(getattr(value, "value", value)))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise TestnetCanaryError(f"receipt {field} is invalid") from exc
        if not parsed.is_finite() or parsed < 0:
            raise TestnetCanaryError(f"receipt {field} is invalid")
        return parsed

    def _event(self, state: dict[str, Any], event: str, *, timestamp: str, **payload: Any) -> None:
        row = {
            "event": event,
            "timestamp": timestamp,
            "canary_id": state["canary_id"],
            "plan_digest": state["plan_digest"],
            **payload,
        }
        row["event_digest"] = _digest(row)
        state.setdefault("events", []).append(row)
        state["updated_at"] = timestamp

    def _block(self, state: dict[str, Any], reason: str, *, timestamp: str) -> None:
        state["status"] = "BLOCKED"
        state["lifecycle_state"] = "BLOCKED"
        state["blocker"] = str(reason)[:300]
        state["next_action"] = "notify_park_and_wait"
        self._event(state, "canary_blocked", timestamp=timestamp, reason=state["blocker"])

    def _save(self, state: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        write_json(self.current_path, [state])
