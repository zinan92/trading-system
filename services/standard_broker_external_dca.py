"""Canonical external DCA lifecycle for one attended Testnet deal.

This module is intentionally separate from the historical local-fixture
``DcaTestnetLifecycle``.  It consumes only the public typed order/fact and
opt-in ProtectionOrder seams; native Hyperliquid identifiers never enter its
strategy state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Protocol, runtime_checkable

from services.journal_store import load_json, write_json
from services.park_confirmation import ParkConfirmationLedger
from services.strategy_control_plane import production_mutation_lock
from services.standard_broker_testnet_canary import (
    TestnetCanaryOrderRequest,
    market_fact_digest,
)
from services.standard_broker_testnet_canary_facts import (
    CanaryFactBundle,
    canary_fact_digest,
)


EXTERNAL_DCA_SCHEMA = "standard-broker-external-dca-v1"
MAX_ALLOWED_LOSS_USD = Decimal("50")
MAX_FACT_AGE_SECONDS = 120
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_SHA1 = re.compile(r"^[0-9a-f]{40}$")

_PLAN_FIELDS = (
    "plan_id",
    "plan_version",
    "cycle_id",
    "strategy_session_id",
    "strategy_revision_id",
    "broker_id",
    "environment",
    "profile_id",
    "account_fingerprint",
    "runtime_id",
    "release_sha",
    "capability_revision",
    "instrument_id",
    "direction",
    "entry_levels",
    "entry_quantities",
    "contract_multiplier",
    "target_price",
    "stop_price",
    "close_price",
    "time_in_force",
    "quantity_step",
    "price_tick",
    "max_slippage",
    "max_notional",
    "max_leverage",
    "account_equity",
    "max_open_orders",
    "max_open_positions",
    "fee_budget_usd",
    "max_loss_usd",
    "expires_at",
)


class ExternalDcaError(RuntimeError):
    """Durable external DCA blocker; callers must not continue or retry blindly."""


def _canonical(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _canonical(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "value"):
        return _canonical(value.value)
    raise TypeError(f"unsupported DCA digest value: {type(value).__name__}")


def _digest(value: object) -> str:
    payload = json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _text(value: object, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ExternalDcaError(f"{field}_required")
    return result


def _decimal(value: object, field: str, *, nonnegative: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ExternalDcaError(f"{field}_invalid") from exc
    if not result.is_finite() or (result < 0 if nonnegative else result <= 0):
        raise ExternalDcaError(f"{field}_invalid")
    return result


def _timestamp(value: object, field: str) -> datetime:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExternalDcaError(f"{field}_invalid") from exc
    if result.tzinfo is None:
        raise ExternalDcaError(f"{field}_timezone_required")
    return result.astimezone(timezone.utc)


def external_dca_plan_digest(mapping: Mapping[str, Any]) -> str:
    return _digest({field: mapping.get(field) for field in _PLAN_FIELDS})


@dataclass(frozen=True)
class ExternalDcaPlan:
    plan_id: str
    plan_version: int
    cycle_id: str
    strategy_session_id: str
    strategy_revision_id: str
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
    entry_levels: tuple[Decimal, ...]
    entry_quantities: tuple[Decimal, ...]
    contract_multiplier: Decimal
    target_price: Decimal
    stop_price: Decimal
    close_price: Decimal
    time_in_force: str
    quantity_step: Decimal
    price_tick: Decimal
    max_slippage: Decimal
    max_notional: Decimal
    max_leverage: Decimal
    account_equity: Decimal
    max_open_orders: int
    max_open_positions: int
    fee_budget_usd: Decimal
    max_loss_usd: Decimal
    expires_at: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], *, now: datetime | None = None) -> "ExternalDcaPlan":
        if not isinstance(value, Mapping):
            raise ExternalDcaError("plan_must_be_object")
        missing = [field for field in (*_PLAN_FIELDS, "plan_digest") if field not in value]
        if missing:
            raise ExternalDcaError("plan_fields_missing:" + ",".join(missing))
        extra = sorted(str(key) for key in value if str(key) not in set(_PLAN_FIELDS) | {"plan_digest"})
        if extra:
            raise ExternalDcaError("plan_fields_unknown:" + ",".join(extra))
        try:
            levels = tuple(_decimal(item, "entry_level") for item in value["entry_levels"])
            quantities = tuple(_decimal(item, "entry_quantity") for item in value["entry_quantities"])
        except (TypeError, ValueError) as exc:
            raise ExternalDcaError("entry_ladder_invalid") from exc
        plan = cls(
            plan_id=_text(value["plan_id"], "plan_id"),
            plan_version=int(value["plan_version"]),
            cycle_id=_text(value["cycle_id"], "cycle_id"),
            strategy_session_id=_text(value["strategy_session_id"], "strategy_session_id"),
            strategy_revision_id=_text(value["strategy_revision_id"], "strategy_revision_id"),
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
            entry_levels=levels,
            entry_quantities=quantities,
            contract_multiplier=_decimal(value["contract_multiplier"], "contract_multiplier"),
            target_price=_decimal(value["target_price"], "target_price"),
            stop_price=_decimal(value["stop_price"], "stop_price"),
            close_price=_decimal(value["close_price"], "close_price"),
            time_in_force=_text(value["time_in_force"], "time_in_force").lower(),
            quantity_step=_decimal(value["quantity_step"], "quantity_step"),
            price_tick=_decimal(value["price_tick"], "price_tick"),
            max_slippage=_decimal(value["max_slippage"], "max_slippage"),
            max_notional=_decimal(value["max_notional"], "max_notional"),
            max_leverage=_decimal(value["max_leverage"], "max_leverage"),
            account_equity=_decimal(value["account_equity"], "account_equity"),
            max_open_orders=int(value["max_open_orders"]),
            max_open_positions=int(value["max_open_positions"]),
            fee_budget_usd=_decimal(value["fee_budget_usd"], "fee_budget_usd", nonnegative=True),
            max_loss_usd=_decimal(value["max_loss_usd"], "max_loss_usd"),
            expires_at=_text(value["expires_at"], "expires_at"),
        )
        plan.validate(now=now)
        if plan.plan_digest != external_dca_plan_digest(plan.to_mapping()):
            raise ExternalDcaError("plan_digest_mismatch")
        return plan

    def to_mapping(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "plan_version": self.plan_version,
            "cycle_id": self.cycle_id,
            "strategy_session_id": self.strategy_session_id,
            "strategy_revision_id": self.strategy_revision_id,
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
            "entry_levels": self.entry_levels,
            "entry_quantities": self.entry_quantities,
            "contract_multiplier": self.contract_multiplier,
            "target_price": self.target_price,
            "stop_price": self.stop_price,
            "close_price": self.close_price,
            "time_in_force": self.time_in_force,
            "quantity_step": self.quantity_step,
            "price_tick": self.price_tick,
            "max_slippage": self.max_slippage,
            "max_notional": self.max_notional,
            "max_leverage": self.max_leverage,
            "account_equity": self.account_equity,
            "max_open_orders": self.max_open_orders,
            "max_open_positions": self.max_open_positions,
            "fee_budget_usd": self.fee_budget_usd,
            "max_loss_usd": self.max_loss_usd,
            "expires_at": self.expires_at,
        }

    def validate(self, *, now: datetime | None = None) -> None:
        if self.plan_version < 1:
            raise ExternalDcaError("plan_version_invalid")
        if self.broker_id != "hyperliquid" or self.environment != "testnet":
            raise ExternalDcaError("environment_identity_mismatch")
        if self.profile_id != "hyperliquid-testnet-position-protection":
            raise ExternalDcaError("protection_profile_mismatch")
        if not _SHA256.fullmatch(self.account_fingerprint.lower()):
            raise ExternalDcaError("account_fingerprint_invalid")
        if not _SHA1.fullmatch(self.release_sha.lower()):
            raise ExternalDcaError("release_sha_invalid")
        if self.capability_revision != "hyperliquid-testnet-position-protection-runtime-v1":
            raise ExternalDcaError("capability_revision_mismatch")
        if self.direction not in {"long", "short"}:
            raise ExternalDcaError("direction_invalid")
        if self.time_in_force not in {"gtc", "ioc", "alo"}:
            raise ExternalDcaError("time_in_force_invalid")
        if not self.entry_levels or len(self.entry_levels) != len(self.entry_quantities):
            raise ExternalDcaError("entry_ladder_invalid")
        if self.contract_multiplier <= 0:
            raise ExternalDcaError("contract_multiplier_invalid")
        if self.direction == "long":
            if any(a <= b for a, b in zip(self.entry_levels, self.entry_levels[1:])):
                raise ExternalDcaError("long_entry_levels_must_descend")
            if not self.stop_price < min(self.entry_levels) or not self.target_price > max(self.entry_levels):
                raise ExternalDcaError("long_protection_geometry_invalid")
        else:
            if any(a >= b for a, b in zip(self.entry_levels, self.entry_levels[1:])):
                raise ExternalDcaError("short_entry_levels_must_ascend")
            if not self.stop_price > max(self.entry_levels) or not self.target_price < min(self.entry_levels):
                raise ExternalDcaError("short_protection_geometry_invalid")
        for quantity in self.entry_quantities:
            if quantity % self.quantity_step != 0:
                raise ExternalDcaError("entry_quantity_precision_invalid")
        for price in (*self.entry_levels, self.target_price, self.stop_price, self.close_price):
            if price % self.price_tick != 0:
                raise ExternalDcaError("price_precision_invalid")
        if self.max_open_orders < 1 or self.max_open_positions < 1:
            raise ExternalDcaError("open_limits_invalid")
        if self.max_loss_usd > MAX_ALLOWED_LOSS_USD:
            raise ExternalDcaError("max_loss_exceeds_50_usd")
        full_notional = sum(
            (price * quantity * self.contract_multiplier for price, quantity in zip(self.entry_levels, self.entry_quantities, strict=True)),
            Decimal("0"),
        )
        if full_notional > self.max_notional:
            raise ExternalDcaError("full_depth_notional_exceeds_limit")
        if full_notional / self.account_equity > self.max_leverage:
            raise ExternalDcaError("full_depth_leverage_exceeds_limit")
        if self.worst_case_loss_usd() > self.max_loss_usd:
            raise ExternalDcaError("worst_case_loss_exceeds_plan_limit")
        current = now.astimezone(timezone.utc) if now is not None else datetime.now(timezone.utc)
        if _timestamp(self.expires_at, "expires_at") <= current:
            raise ExternalDcaError("plan_expired")
        if self.plan_digest != external_dca_plan_digest(self.to_mapping()):
            raise ExternalDcaError("plan_digest_mismatch")

    def worst_case_loss_usd(self) -> Decimal:
        gross = Decimal("0")
        for price, quantity in zip(self.entry_levels, self.entry_quantities, strict=True):
            stop_distance = price - self.stop_price if self.direction == "long" else self.stop_price - price
            gross += max(Decimal("0"), stop_distance) * quantity * self.contract_multiplier
        slippage = self.max_slippage * sum(self.entry_quantities, Decimal("0")) * self.contract_multiplier
        return gross + slippage + self.fee_budget_usd


@runtime_checkable
class ExternalDcaOrderPort(Protocol):
    def preflight(self) -> Mapping[str, Any]: ...
    def market_fact(self, *, instrument_id: str, now: datetime) -> Mapping[str, Any]: ...
    def submit(self, request: TestnetCanaryOrderRequest) -> object: ...
    def query(self, order_id: str) -> object: ...
    def cancel(self, order_id: str) -> object: ...
    def open_orders(self, instrument_id: str) -> tuple[object, ...]: ...


@runtime_checkable
class ExternalDcaFactsPort(Protocol):
    def read_facts(self, *, order_id: str, instrument_id: str, now: datetime) -> CanaryFactBundle: ...


@runtime_checkable
class ExternalDcaProtectionPort(Protocol):
    def submit(self, group: object) -> object: ...
    def reconcile(self, group: object) -> object: ...
    def replace(self, group: object) -> object: ...
    def cancel(self, group: object) -> object: ...


class ExternalDcaLifecycle:
    """Attended, canonical DCA state machine with explicit protection gates."""

    def __init__(
        self,
        output_root: Path,
        orders: ExternalDcaOrderPort,
        facts: ExternalDcaFactsPort,
        protection: ExternalDcaProtectionPort,
        *,
        park_user_id: str = "park",
    ) -> None:
        if not isinstance(orders, ExternalDcaOrderPort):
            raise TypeError("external DCA orders must implement the public order port")
        if not isinstance(facts, ExternalDcaFactsPort):
            raise TypeError("external DCA facts must implement the typed facts port")
        if not isinstance(protection, ExternalDcaProtectionPort):
            raise TypeError("external DCA protection must implement the public protection port")
        self.output_root = Path(output_root)
        self.root = self.output_root / "standard_broker_external_dca"
        self.current_path = self.root / "current.json"
        self.orders = orders
        self.facts = facts
        self.protection = protection
        self.park_user_id = _text(park_user_id, "park_user_id")
        self.confirmations = ParkConfirmationLedger(self.output_root, park_user_id=self.park_user_id)

    def snapshot(self) -> dict[str, Any]:
        rows = load_json(self.current_path)
        return dict(rows[-1]) if rows and isinstance(rows[-1], dict) else {}

    def prepare(
        self,
        plan: ExternalDcaPlan | Mapping[str, Any],
        *,
        confirmation: Mapping[str, Any],
        timestamp: str,
    ) -> dict[str, Any]:
        with production_mutation_lock(self.output_root):
            return self._prepare(
                plan,
                confirmation=confirmation,
                timestamp=timestamp,
            )

    def _prepare(
        self,
        plan: ExternalDcaPlan | Mapping[str, Any],
        *,
        confirmation: Mapping[str, Any],
        timestamp: str,
    ) -> dict[str, Any]:
        now = _timestamp(timestamp, "timestamp")
        normalized = plan if isinstance(plan, ExternalDcaPlan) else ExternalDcaPlan.from_mapping(plan, now=now)
        normalized.validate(now=now)
        self._require_confirmation(normalized, confirmation, now)
        existing = self.snapshot()
        if existing:
            if existing.get("plan_digest") != normalized.plan_digest:
                raise ExternalDcaError("persisted_plan_digest_mismatch")
            return existing
        state = self._new_state(normalized, timestamp)
        self._save(state)
        try:
            self._validate_preflight(normalized)
            self._validate_market_fact(normalized, now=now)
            request = self._entry_request(normalized, 0)
            state["entry_intent"] = self._safe_request(request)
            state["status"] = "ENTRY_SUBMIT_INTENT_RESERVED"
            self._event(state, "entry_submit_intent_reserved", timestamp=timestamp)
            self._save(state)
            receipt = self.orders.submit(request)
            self._record_receipt(state, receipt, request=request, timestamp=timestamp, operation="submit")
            if self._receipt_state(receipt) in {"unknown", "rejected"}:
                raise ExternalDcaError("entry_submit_not_accepted")
            order_id = str(getattr(receipt, "order_id", "") or "").strip()
            if not order_id:
                raise ExternalDcaError("entry_receipt_identity_missing")
            state["entry_order_id"] = order_id
            state.setdefault("entry_order_ids", []).append(order_id)
            queried = self.orders.query(order_id)
            self._record_receipt(state, queried, request=request, timestamp=timestamp, operation="query")
            receipt_state = self._receipt_state(queried)
            if receipt_state in {"unknown", "rejected"}:
                raise ExternalDcaError(f"entry_query_{receipt_state}")
            state["status"] = "ENTRY_FILLED_PENDING_FACTS" if receipt_state in {"filled", "partially_filled"} else "WAITING_ENTRY"
            state["next_action"] = "read_entry_facts" if state["status"] != "WAITING_ENTRY" else "attended_query_or_cancel_entry"
            self._event(state, "entry_reconciled", timestamp=timestamp, lifecycle_state=state["status"])
            self._save(state)
            return state
        except ExternalDcaError as exc:
            return self._block(state, str(exc), timestamp=timestamp)
        except Exception as exc:  # noqa: BLE001
            return self._block(state, f"entry_unknown:{type(exc).__name__}", timestamp=timestamp)

    def on_entry_facts(
        self,
        plan: ExternalDcaPlan,
        *,
        bundle: CanaryFactBundle,
        confirmation: Mapping[str, Any],
        timestamp: str,
    ) -> dict[str, Any]:
        with production_mutation_lock(self.output_root):
            return self._on_entry_facts(
                plan,
                bundle=bundle,
                confirmation=confirmation,
                timestamp=timestamp,
            )

    def _on_entry_facts(
        self,
        plan: ExternalDcaPlan,
        *,
        bundle: CanaryFactBundle,
        confirmation: Mapping[str, Any],
        timestamp: str,
    ) -> dict[str, Any]:
        state = self._state(plan)
        try:
            self._require_confirmation(plan, confirmation, _timestamp(timestamp, "timestamp"))
        except ExternalDcaError as exc:
            return self._block(state, str(exc), timestamp=timestamp)
        if state.get("status") not in {"ENTRY_FILLED_PENDING_FACTS", "PROTECTION_ACTIVE"}:
            raise ExternalDcaError("entry_facts_not_expected")
        order_id = str(state.get("entry_order_id") or "")
        try:
            position_quantity, average_entry, actual_fees = self._validate_facts(
                plan,
                bundle,
                order_id=order_id,
                timestamp=timestamp,
            )
            if position_quantity <= 0:
                raise ExternalDcaError("entry_position_missing")
            state["position_quantity"] = str(position_quantity)
            state["average_entry_price"] = str(average_entry)
            state["actual_fee_usd"] = str(actual_fees)
            group = self._protection_group(plan, state, position_quantity, average_entry)
            previous = state.get("protection")
            receipt = (
                self.protection.submit(group)
                if previous is None
                else self.protection.replace(group)
            )
            confirmed = self.protection.reconcile(group)
            observation = getattr(confirmed, "observation", None)
            if observation is None or getattr(observation, "state", None).value != "active":
                raise ExternalDcaError("protection_not_active")
            covered = Decimal(str(getattr(observation, "covered_quantity", "0")))
            if covered < position_quantity:
                raise ExternalDcaError("protection_coverage_below_position")
            state["protection"] = self._safe_protection(receipt, confirmed, covered)
            state["status"] = "PROTECTION_ACTIVE"
            state["next_action"] = "submit_next_entry_only_after_attended_price_gate"
            self._event(state, "protection_confirmed", timestamp=timestamp, covered_quantity=str(covered))
            self._save(state)
            return state
        except ExternalDcaError as exc:
            return self._block(state, str(exc), timestamp=timestamp)
        except Exception as exc:  # noqa: BLE001
            return self._block(state, f"facts_or_protection_unknown:{type(exc).__name__}", timestamp=timestamp)

    def submit_next_entry(
        self,
        plan: ExternalDcaPlan,
        *,
        confirmation: Mapping[str, Any],
        timestamp: str,
    ) -> dict[str, Any]:
        with production_mutation_lock(self.output_root):
            return self._submit_next_entry(
                plan,
                confirmation=confirmation,
                timestamp=timestamp,
            )

    def _submit_next_entry(
        self,
        plan: ExternalDcaPlan,
        *,
        confirmation: Mapping[str, Any],
        timestamp: str,
    ) -> dict[str, Any]:
        state = self._state(plan)
        now = _timestamp(timestamp, "timestamp")
        try:
            self._require_confirmation(plan, confirmation, now)
        except ExternalDcaError as exc:
            return self._block(state, str(exc), timestamp=timestamp)
        index = int(state.get("next_entry_index") or 0) + 1
        if state.get("status") != "PROTECTION_ACTIVE":
            return self._block(state, "new_entry_requires_active_protection", timestamp=timestamp)
        if index >= len(plan.entry_levels):
            state["next_action"] = "await_terminal_target_or_stop"
            self._save(state)
            return state
        request = self._entry_request(plan, index)
        try:
            self._validate_market_fact(plan, now=_timestamp(timestamp, "timestamp"))
            state["status"] = "ENTRY_SUBMIT_INTENT_RESERVED"
            state["entry_intent"] = self._safe_request(request)
            self._event(state, "entry_submit_intent_reserved", timestamp=timestamp, index=index)
            self._save(state)
            receipt = self.orders.submit(request)
            self._record_receipt(state, receipt, request=request, timestamp=timestamp, operation="submit")
            if self._receipt_state(receipt) in {"unknown", "rejected"}:
                raise ExternalDcaError("entry_submit_not_accepted")
            order_id = str(getattr(receipt, "order_id", "") or "").strip()
            if not order_id:
                raise ExternalDcaError("entry_receipt_identity_missing")
            state.setdefault("entry_order_ids", []).append(order_id)
            state["entry_order_id"] = order_id
            state["next_entry_index"] = index
            queried = self.orders.query(order_id)
            self._record_receipt(state, queried, request=request, timestamp=timestamp, operation="query")
            receipt_state = self._receipt_state(queried)
            if receipt_state in {"unknown", "rejected"}:
                raise ExternalDcaError(f"entry_query_{receipt_state}")
            state["status"] = "ENTRY_FILLED_PENDING_FACTS" if receipt_state in {"filled", "partially_filled"} else "WAITING_ENTRY"
            state["next_action"] = "read_entry_facts" if state["status"] != "WAITING_ENTRY" else "attended_query_or_cancel_entry"
            self._save(state)
            return state
        except ExternalDcaError as exc:
            return self._block(state, str(exc), timestamp=timestamp)
        except Exception as exc:  # noqa: BLE001
            return self._block(state, f"entry_unknown:{type(exc).__name__}", timestamp=timestamp)

    def flatten(
        self,
        plan: ExternalDcaPlan,
        *,
        bundle: CanaryFactBundle,
        confirmation: Mapping[str, Any],
        timestamp: str,
    ) -> dict[str, Any]:
        with production_mutation_lock(self.output_root):
            return self._flatten(
                plan,
                bundle=bundle,
                confirmation=confirmation,
                timestamp=timestamp,
            )

    def _flatten(
        self,
        plan: ExternalDcaPlan,
        *,
        bundle: CanaryFactBundle,
        confirmation: Mapping[str, Any],
        timestamp: str,
    ) -> dict[str, Any]:
        state = self._state(plan)
        try:
            self._require_confirmation(plan, confirmation, _timestamp(timestamp, "timestamp"))
        except ExternalDcaError as exc:
            return self._block(state, str(exc), timestamp=timestamp)
        if state.get("status") not in {"PROTECTION_ACTIVE", "WAITING_ENTRY", "ENTRY_FILLED_PENDING_FACTS"}:
            raise ExternalDcaError("flatten_not_available")
        try:
            for order_id in tuple(state.get("entry_order_ids") or [state.get("entry_order_id")]):
                if order_id and self._entry_is_open(state, str(order_id)):
                    canceled = self.orders.cancel(str(order_id))
                    if self._receipt_state(canceled) not in {"canceled", "rejected"}:
                        raise ExternalDcaError("entry_cancel_not_confirmed")
            position_quantity = Decimal(str(state.get("position_quantity") or "0"))
            if position_quantity <= 0:
                raise ExternalDcaError("flatten_position_missing")
            group = self._protection_group(
                plan,
                state,
                position_quantity,
                Decimal(str(state.get("average_entry_price") or plan.entry_levels[0])),
            )
            canceled_protection = self.protection.cancel(group)
            canceled_observation = getattr(canceled_protection, "observation", None)
            if (
                canceled_observation is None
                or getattr(getattr(canceled_observation, "state", None), "value", "") != "canceled"
            ):
                raise ExternalDcaError("protection_cancel_not_confirmed")
            request = TestnetCanaryOrderRequest(
                order_id=f"{plan.plan_id}:flatten",
                instrument_id=plan.instrument_id,
                side="sell" if plan.direction == "long" else "buy",
                quantity=position_quantity,
                order_type="limit",
                limit_price=plan.close_price,
                time_in_force="ioc",
                idempotency_key=f"{plan.plan_id}:flatten:{position_quantity}:{plan.close_price}",
                reduce_only=True,
                close_position=True,
            )
            state["status"] = "FLATTEN_SUBMIT_INTENT_RESERVED"
            self._save(state)
            receipt = self.orders.submit(request)
            self._record_receipt(state, receipt, request=request, timestamp=timestamp, operation="flatten_submit")
            if self._receipt_state(receipt) in {"unknown", "rejected"}:
                raise ExternalDcaError("flatten_submit_not_accepted")
            close_id = str(getattr(receipt, "order_id", "") or "").strip()
            if not close_id:
                raise ExternalDcaError("flatten_receipt_identity_missing")
            queried = self.orders.query(close_id)
            self._record_receipt(state, queried, request=request, timestamp=timestamp, operation="flatten_query")
            if self._receipt_state(queried) != "filled":
                raise ExternalDcaError("flatten_not_filled")
            close_fill_quantity = sum(
                (fill.quantity for fill in bundle.fills if fill.order_id == close_id),
                Decimal("0"),
            )
            if close_fill_quantity < position_quantity:
                raise ExternalDcaError("flatten_fill_quantity_incomplete")
            final = self._validate_facts(plan, bundle, order_id=close_id, timestamp=timestamp, require_flat=True)
            if final[0] != 0:
                raise ExternalDcaError("final_position_not_flat")
            state["status"] = "FLAT_RECONCILED"
            state["next_action"] = "record_dca_result"
            self._event(state, "flat_reconciled", timestamp=timestamp)
            self._save(state)
            return state
        except ExternalDcaError as exc:
            return self._block(state, str(exc), timestamp=timestamp)
        except Exception as exc:  # noqa: BLE001
            return self._block(state, f"flatten_unknown:{type(exc).__name__}", timestamp=timestamp)

    def _validate_preflight(self, plan: ExternalDcaPlan) -> None:
        result = self.orders.preflight()
        if not isinstance(result, Mapping):
            raise ExternalDcaError("order_preflight_invalid")
        for key, expected in {
            "environment": "testnet",
            "transport_state": "external_testnet",
            "real_money_eligible": False,
        }.items():
            if result.get(key) != expected:
                raise ExternalDcaError(f"order_preflight_{key}_mismatch")
        for key, expected in {
            "account_fingerprint": plan.account_fingerprint,
            "runtime_id": plan.runtime_id,
            "release_sha": plan.release_sha,
            "capability_revision": "hyperliquid-testnet-runtime-v1",
        }.items():
            if result.get(key) != expected:
                raise ExternalDcaError(f"order_preflight_{key}_mismatch")
        if result.get("network_io") is not True or result.get("canary_ready") is not True:
            raise ExternalDcaError("order_preflight_not_ready")
        if result.get("transport_profile") != "hyperliquid-testnet-default":
            raise ExternalDcaError("order_preflight_profile_mismatch")
        matrix = getattr(self.protection, "protection_capabilities", None)
        if matrix is None or matrix.profile_id != "hyperliquid-testnet-position-protection-v1":
            raise ExternalDcaError("protection_profile_not_ready")
        session = getattr(self.protection, "runtime_session", None)
        if session is None or session.account.address is None:
            raise ExternalDcaError("protection_runtime_identity_missing")
        if session.lifecycle_id != plan.runtime_id or session.capability_revision != plan.capability_revision:
            raise ExternalDcaError("protection_runtime_identity_mismatch")

    def _validate_market_fact(self, plan: ExternalDcaPlan, *, now: datetime) -> None:
        reader = getattr(self.orders, "market_fact", None)
        if not callable(reader):
            raise ExternalDcaError("fresh_market_fact_reader_missing")
        try:
            value = reader(instrument_id=plan.instrument_id, now=now)
        except Exception as exc:  # noqa: BLE001
            raise ExternalDcaError("market_fact_unknown") from exc
        if not isinstance(value, Mapping):
            raise ExternalDcaError("market_fact_invalid")
        if (
            value.get("instrument_id") != plan.instrument_id
            or value.get("freshness") != "fresh"
            or value.get("transport_state") != "external_testnet"
            or value.get("source") not in {"nautilus-hyperliquid.testnet", "hyperliquid.external_testnet"}
        ):
            raise ExternalDcaError("market_fact_identity_invalid")
        observed = _timestamp(value.get("observed_at"), "market_fact.observed_at")
        if abs((now - observed).total_seconds()) > 120:
            raise ExternalDcaError("market_fact_stale")
        try:
            price = Decimal(str(value.get("price")))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ExternalDcaError("market_fact_price_invalid") from exc
        if not price.is_finite() or price <= 0:
            raise ExternalDcaError("market_fact_price_invalid")
        try:
            multiplier = Decimal(str(value.get("contract_multiplier")))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ExternalDcaError("market_fact_contract_multiplier_invalid") from exc
        if multiplier != plan.contract_multiplier:
            raise ExternalDcaError("market_fact_contract_multiplier_mismatch")
        if price % plan.price_tick != 0:
            raise ExternalDcaError("market_fact_price_tick_mismatch")
        if value.get("fact_digest") != market_fact_digest(value):
            raise ExternalDcaError("market_fact_digest_invalid")

    def _require_confirmation(self, plan: ExternalDcaPlan, confirmation: Mapping[str, Any], now: datetime) -> None:
        if not isinstance(confirmation, Mapping):
            raise ExternalDcaError("confirmation_required")
        operator = str(confirmation.get("operator_id") or confirmation.get("park_user_id") or "").strip()
        if (
            operator != self.park_user_id
            or confirmation.get("execution_authorized") is not True
            or confirmation.get("execution_environment") != "testnet"
            or confirmation.get("plan_digest") != plan.plan_digest
            or confirmation.get("canary_id") != plan.plan_id
            or not confirmation.get("receipt_digest")
            or not _SHA256.fullmatch(str(confirmation.get("receipt_digest") or "").lower())
        ):
            raise ExternalDcaError("confirmation_identity_invalid")
        if _timestamp(confirmation.get("expires_at"), "confirmation.expires_at") <= now:
            raise ExternalDcaError("confirmation_expired")
        proposal_id = str(confirmation.get("proposal_id") or confirmation.get("confirmation_id") or "").strip()
        rows = self.confirmations.rows()
        proposal = next((row for row in rows if row.get("event") == "proposal" and str(row.get("proposal_id")) == proposal_id), None)
        decision = next((row for row in reversed(rows) if row.get("event") == "confirmed" and str(row.get("proposal_id")) == proposal_id), None)
        try:
            proposal_expires = float(proposal.get("expires_at") or 0) if proposal else 0
        except (TypeError, ValueError) as exc:
            raise ExternalDcaError("durable_confirmation_expiry_invalid") from exc
        if (
            not proposal
            or not decision
            or proposal_expires <= now.timestamp()
            or proposal.get("execution_environment") != "testnet"
            or decision.get("execution_environment") != "testnet"
            or decision.get("execution_authorized") is not True
            or decision.get("park_user_id") != self.park_user_id
            or decision.get("plan_digest") != plan.plan_digest
            or decision.get("receipt_digest") != confirmation.get("receipt_digest")
        ):
            raise ExternalDcaError("durable_confirmation_missing")

    def _validate_facts(
        self,
        plan: ExternalDcaPlan,
        bundle: CanaryFactBundle,
        *,
        order_id: str,
        timestamp: str,
        require_flat: bool = False,
    ) -> tuple[Decimal, Decimal, Decimal]:
        if not isinstance(bundle, CanaryFactBundle):
            raise ExternalDcaError("facts_bundle_invalid")
        account = bundle.account
        reconciliation = bundle.reconciliation
        if account is None or reconciliation is None or not reconciliation.coherent or reconciliation.freshness != "fresh":
            raise ExternalDcaError("facts_reconciliation_not_coherent")
        if reconciliation.canonical_schema != "ExternalReconciliationSnapshot":
            raise ExternalDcaError("facts_reconciliation_contract_invalid")
        if not _SHA256.fullmatch(str(reconciliation.evidence_digest or "").lower()):
            raise ExternalDcaError("facts_reconciliation_digest_invalid")
        cursor = reconciliation.cursor
        if not cursor:
            raise ExternalDcaError("facts_cursor_missing")
        typed_facts = (account, reconciliation, *bundle.positions, *bundle.fills, *bundle.fees)
        current = _timestamp(timestamp, "timestamp")
        for fact in typed_facts:
            if getattr(fact, "cursor", cursor) != cursor:
                raise ExternalDcaError("facts_cursor_mismatch")
            if getattr(fact, "fact_digest", "") != canary_fact_digest(fact):
                raise ExternalDcaError("facts_digest_invalid")
            for field in ("observed_at", "occurred_at"):
                value = getattr(fact, field, None)
                if value is None:
                    continue
                observed = _timestamp(value, f"facts.{field}")
                age = (current - observed).total_seconds()
                if age < -300 or age > MAX_FACT_AGE_SECONDS:
                    raise ExternalDcaError("facts_stale")
        if tuple(bundle.open_orders) != tuple(reconciliation.open_order_ids):
            raise ExternalDcaError("facts_open_order_reconciliation_mismatch")
        identity_values = (account, reconciliation, *bundle.positions, *bundle.fills, *bundle.fees)
        for fact in identity_values:
            if getattr(fact, "account_fingerprint", plan.account_fingerprint) != plan.account_fingerprint:
                raise ExternalDcaError("facts_account_identity_mismatch")
            if getattr(fact, "runtime_id", plan.runtime_id) != plan.runtime_id:
                raise ExternalDcaError("facts_runtime_identity_mismatch")
            if getattr(fact, "release_sha", plan.release_sha) != plan.release_sha:
                raise ExternalDcaError("facts_release_identity_mismatch")
            if getattr(fact, "capability_revision", plan.capability_revision) != plan.capability_revision:
                raise ExternalDcaError("facts_capability_identity_mismatch")
            if getattr(fact, "transport_state", "external_testnet") != "external_testnet":
                raise ExternalDcaError("facts_transport_identity_mismatch")
        fills = tuple(fill for fill in bundle.fills if fill.order_id == order_id)
        if not fills:
            raise ExternalDcaError("canonical_fill_missing")
        if fills:
            try:
                index = int(order_id.rsplit(":", 1)[-1])
            except ValueError as exc:
                if not require_flat:
                    raise ExternalDcaError("entry_order_index_invalid") from exc
                index = -1
            if index < 0 or index >= len(plan.entry_levels):
                if not require_flat:
                    raise ExternalDcaError("entry_order_index_invalid")
            else:
                expected_side = "buy" if plan.direction == "long" else "sell"
                for fill in fills:
                    if fill.side != expected_side:
                        raise ExternalDcaError("entry_fill_side_mismatch")
                    if fill.quantity > plan.entry_quantities[index]:
                        raise ExternalDcaError("entry_fill_quantity_exceeds_plan")
                    if abs(fill.price - plan.entry_levels[index]) > plan.max_slippage:
                        raise ExternalDcaError("entry_fill_slippage_exceeded")
        fee_fill_ids = {fee.fill_id for fee in bundle.fees}
        if fee_fill_ids != {fill.fill_id for fill in fills}:
            raise ExternalDcaError("actual_fee_coverage_incomplete")
        quantity = sum((fill.quantity for fill in fills), Decimal("0"))
        average = (
            sum((fill.price * fill.quantity for fill in fills), Decimal("0")) / quantity
            if quantity > 0
            else Decimal("0")
        )
        actual_fees = sum((fee.amount_usd for fee in bundle.fees), Decimal("0"))
        if actual_fees > plan.fee_budget_usd:
            raise ExternalDcaError("actual_fees_exceed_budget")
        position_quantity = sum(
            (position.signed_quantity for position in bundle.positions if position.instrument_id == plan.instrument_id),
            Decimal("0"),
        )
        if (plan.direction == "long" and reconciliation.signed_position_quantity < 0) or (
            plan.direction == "short" and reconciliation.signed_position_quantity > 0
        ):
            raise ExternalDcaError("position_side_identity_mismatch")
        if require_flat and (position_quantity != 0 or bundle.open_orders):
            raise ExternalDcaError("final_reconciliation_not_flat")
        if require_flat:
            expected_close_side = "sell" if plan.direction == "long" else "buy"
            if any(fill.side != expected_close_side for fill in fills):
                raise ExternalDcaError("flatten_fill_side_mismatch")
        return abs(position_quantity), average, actual_fees

    @staticmethod
    def _protection_group(plan: ExternalDcaPlan, state: Mapping[str, Any], quantity: Decimal, entry_price: Decimal) -> object:
        try:
            from standard_broker import (
                OrderSide,
                ProtectionExecution,
                ProtectionGroup,
                ProtectionLeg,
                ProtectionQuantityPolicy,
                ProtectionType,
            )
        except (ImportError, ModuleNotFoundError) as exc:
            raise ExternalDcaError("standard_broker_protection_types_unavailable") from exc
        return ProtectionGroup(
            protection_id=f"{plan.plan_id}:protection",
            parent_order_id=str(state.get("entry_order_id") or ""),
            instrument_id=plan.instrument_id,
            entry_side=OrderSide.BUY if plan.direction == "long" else OrderSide.SELL,
            entry_price=entry_price,
            quantity=quantity,
            quantity_policy=ProtectionQuantityPolicy.POSITION_FOLLOWING,
            take_profit=ProtectionLeg(
                protection_type=ProtectionType.TAKE_PROFIT,
                execution=ProtectionExecution.MARKET,
                trigger_price=plan.target_price,
            ),
            stop_loss=ProtectionLeg(
                protection_type=ProtectionType.STOP_LOSS,
                execution=ProtectionExecution.MARKET,
                trigger_price=plan.stop_price,
            ),
        )

    @staticmethod
    def _entry_request(plan: ExternalDcaPlan, index: int) -> TestnetCanaryOrderRequest:
        return TestnetCanaryOrderRequest(
            order_id=f"{plan.plan_id}:entry:{index}",
            instrument_id=plan.instrument_id,
            side="buy" if plan.direction == "long" else "sell",
            quantity=plan.entry_quantities[index],
            order_type="limit",
            limit_price=plan.entry_levels[index],
            time_in_force=plan.time_in_force,
            idempotency_key=f"{plan.plan_id}:entry:{index}:{plan.entry_levels[index]}",
        )

    def _new_state(self, plan: ExternalDcaPlan, timestamp: str) -> dict[str, Any]:
        return {
            "schema_version": EXTERNAL_DCA_SCHEMA,
            "plan_id": plan.plan_id,
            "plan_digest": plan.plan_digest,
            "strategy_session_id": plan.strategy_session_id,
            "strategy_revision_id": plan.strategy_revision_id,
            "broker_id": plan.broker_id,
            "environment": plan.environment,
            "profile_id": plan.profile_id,
            "account_fingerprint": plan.account_fingerprint,
            "runtime_id": plan.runtime_id,
            "release_sha": plan.release_sha,
            "capability_revision": plan.capability_revision,
            "instrument_id": plan.instrument_id,
            "direction": plan.direction,
            "next_entry_index": 0,
            "entry_order_ids": [],
            "events": [],
            "receipts": [],
            "status": "NEW",
            "next_action": "submit_first_entry",
            "created_at": timestamp,
            "updated_at": timestamp,
        }

    def _state(self, plan: ExternalDcaPlan) -> dict[str, Any]:
        state = self.snapshot()
        if not state:
            raise ExternalDcaError("external_dca_not_prepared")
        if state.get("plan_digest") != plan.plan_digest:
            raise ExternalDcaError("persisted_plan_digest_mismatch")
        return state

    def _block(self, state: dict[str, Any], reason: str, *, timestamp: str) -> dict[str, Any]:
        state["status"] = "BLOCKED"
        state["blocker"] = reason.split(":", 1)[0]
        state["next_action"] = "notify_park_and_wait"
        state["updated_at"] = timestamp
        self._event(state, "blocked", timestamp=timestamp, reason=state["blocker"])
        self._save(state)
        return state

    @staticmethod
    def _receipt_state(receipt: object) -> str:
        state = getattr(receipt, "state", "unknown")
        return str(getattr(state, "value", state) or "unknown").lower()

    @staticmethod
    def _entry_is_open(state: Mapping[str, Any], order_id: str) -> bool:
        rows = [
            row
            for row in state.get("receipts") or ()
            if isinstance(row, Mapping) and str(row.get("order_id") or "") == order_id
        ]
        if not rows:
            return True
        latest = str(rows[-1].get("state") or "unknown").lower()
        return latest in {"submitting", "resting", "waiting_for_fill", "waiting_for_trigger", "partially_filled", "cancel_pending", "modify_pending", "unknown"}

    def _record_receipt(self, state: dict[str, Any], receipt: object, *, request: TestnetCanaryOrderRequest, timestamp: str, operation: str) -> None:
        self._validate_receipt_identity(state, receipt, operation=operation)
        receipt_state = self._receipt_state(receipt)
        if receipt_state == "unknown":
            raise ExternalDcaError(f"{operation}_receipt_unknown")
        state.setdefault("receipts", []).append(
            {
                "operation": operation,
                "order_id": str(getattr(receipt, "order_id", "")),
                "client_order_id": str(getattr(receipt, "client_order_id", "")),
                "broker_order_id": str(getattr(receipt, "broker_order_id", "") or ""),
                "state": receipt_state,
                "quantity": str(request.quantity),
                "price": str(request.limit_price),
                "side": request.side,
                "instrument_id": request.instrument_id,
                "environment": "testnet",
                "account_fingerprint": state["account_fingerprint"],
                "runtime_id": state["runtime_id"],
                "release_sha": state["release_sha"],
                "capability_revision": state["capability_revision"],
                "observed_at": timestamp,
            }
        )

    @staticmethod
    def _validate_receipt_identity(state: Mapping[str, Any], receipt: object, *, operation: str) -> None:
        broker_id = getattr(receipt, "broker_id", None)
        if broker_id is not None and str(getattr(broker_id, "value", broker_id)).lower() != "hyperliquid":
            raise ExternalDcaError(f"{operation}_receipt_broker_identity_mismatch")
        environment = getattr(receipt, "environment", None)
        if environment is not None and str(getattr(environment, "value", environment)).lower() != "testnet":
            raise ExternalDcaError(f"{operation}_receipt_environment_identity_mismatch")
        account = getattr(receipt, "account_fingerprint", None)
        if account is None:
            account = getattr(receipt, "account_address", None)
        if account:
            text = str(account)
            fingerprint = text if text.startswith("sha256:") else "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
            if fingerprint != state["account_fingerprint"]:
                raise ExternalDcaError(f"{operation}_receipt_account_identity_mismatch")
        runtime_id = getattr(receipt, "lifecycle_id", None)
        if runtime_id is not None and str(runtime_id) != state["runtime_id"]:
            raise ExternalDcaError(f"{operation}_receipt_runtime_identity_mismatch")
        release_sha = getattr(receipt, "release_sha", None)
        if release_sha is not None and str(release_sha) != state["release_sha"]:
            raise ExternalDcaError(f"{operation}_receipt_release_identity_mismatch")
        provenance = getattr(receipt, "provenance", None)
        if provenance is not None:
            if getattr(provenance, "transport_state", None) != "external_testnet":
                raise ExternalDcaError(f"{operation}_receipt_transport_identity_mismatch")
            mapping_revision = getattr(provenance, "mapping_revision", None)
            if mapping_revision not in {None, state["capability_revision"], "hyperliquid-testnet-runtime-v1"}:
                raise ExternalDcaError(f"{operation}_receipt_mapping_identity_mismatch")

    @staticmethod
    def _safe_request(request: TestnetCanaryOrderRequest) -> dict[str, Any]:
        return {
            "order_id": request.order_id,
            "instrument_id": request.instrument_id,
            "side": request.side,
            "quantity": str(request.quantity),
            "order_type": request.order_type,
            "limit_price": str(request.limit_price),
            "time_in_force": request.time_in_force,
            "idempotency_key": request.idempotency_key,
            "reduce_only": request.reduce_only,
            "close_position": request.close_position,
        }

    @staticmethod
    def _safe_protection(submitted: object, confirmed: object, covered: Decimal) -> dict[str, Any]:
        def evidence(receipt: object) -> dict[str, Any]:
            observation = getattr(receipt, "observation", None)
            return {
                "operation": str(getattr(receipt, "operation", "")),
                "accepted": bool(getattr(receipt, "accepted", False)),
                "state": str(getattr(getattr(observation, "state", "unknown"), "value", "unknown")),
                "covered_quantity": str(getattr(observation, "covered_quantity", covered)),
                "observation_digest": str(getattr(observation, "observation_digest", "")),
            }
        return {"covered_quantity": str(covered), "submitted": evidence(submitted), "confirmed": evidence(confirmed)}

    @staticmethod
    def _event(state: dict[str, Any], event: str, *, timestamp: str, **details: Any) -> None:
        state.setdefault("events", []).append({"event": event, "timestamp": timestamp, **details})

    def _save(self, state: dict[str, Any]) -> None:
        rows = load_json(self.current_path)
        rows.append(json.loads(json.dumps(state, default=str)))
        write_json(self.current_path, rows)
