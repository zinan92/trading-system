"""Immutable, broker-neutral contracts for multi-asset portfolio selection.

The contracts in this module are evidence envelopes.  They describe Strategy
intent, account facts, policy, and a resulting selection/hold; they do not
submit, cancel, or reconcile orders.  A later Portfolio Risk Gate may consume
these contracts, but this module deliberately contains no allocation algorithm.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, ClassVar


PORTFOLIO_SESSION_SCHEMA = "portfolio-session-v1"
STRATEGY_POSITION_PLAN_SCHEMA = "strategy-position-plan-v1"
STRATEGY_CANDIDATE_SET_SCHEMA = "strategy-candidate-set-v1"
PORTFOLIO_SNAPSHOT_SCHEMA = "portfolio-snapshot-v1"
PORTFOLIO_POLICY_SCHEMA = "portfolio-policy-v1"
ASSET_ALLOCATION_SLICE_SCHEMA = "asset-allocation-slice-v1"
EXECUTION_SLICE_SCHEMA = "execution-slice-v1"
PORTFOLIO_SELECTION_SCHEMA = "portfolio-selection-v1"
PORTFOLIO_RISK_HOLD_SCHEMA = "portfolio-risk-hold-v1"

_DIRECTIONS = frozenset({"long", "short", "flat"})
_POSITION_ACTIONS = frozenset({"open", "add", "reduce", "exit", "hold"})
_SELECTION_STATUSES = frozenset({"accepted", "scaled", "held", "open", "closing"})
Numberish = Decimal | str | int | float


class _PortfolioContract:
    schema_version: ClassVar[str]

    @property
    def digest(self) -> str:
        return f"sha256:{_digest(self.to_dict())}"

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )


@dataclass(frozen=True)
class PortfolioSession(_PortfolioContract):
    """One account-scoped session coordinating strategy allocations."""

    portfolio_session_id: str
    strategy_session_id: str
    account_id: str
    session_revision_id: str
    opened_at: str
    account_scope: Mapping[str, Any] = field(default_factory=dict)
    provenance: Mapping[str, Any] = field(default_factory=dict)
    schema_version: ClassVar[str] = PORTFOLIO_SESSION_SCHEMA

    def __post_init__(self) -> None:
        portfolio_session_id = _required_text(self.portfolio_session_id, "portfolio session id")
        strategy_session_id = _required_text(self.strategy_session_id, "strategy session id")
        account_id = _required_text(self.account_id, "account id")
        session_revision_id = _required_text(self.session_revision_id, "session revision id")
        opened_at = _aware_iso(self.opened_at, "portfolio session opened_at")
        account_scope = _object(self.account_scope, "portfolio session account_scope")
        _check_scope(account_scope, portfolio_session_id, account_id)
        object.__setattr__(self, "portfolio_session_id", portfolio_session_id)
        object.__setattr__(self, "strategy_session_id", strategy_session_id)
        object.__setattr__(self, "account_id", account_id)
        object.__setattr__(self, "session_revision_id", session_revision_id)
        object.__setattr__(self, "opened_at", opened_at)
        object.__setattr__(self, "account_scope", account_scope)
        object.__setattr__(self, "provenance", _object(self.provenance, "portfolio session provenance"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "portfolio_session_id": self.portfolio_session_id,
            "strategy_session_id": self.strategy_session_id,
            "account_id": self.account_id,
            "session_revision_id": self.session_revision_id,
            "opened_at": self.opened_at,
            "account_scope": _thaw(self.account_scope),
            "provenance": _thaw(self.provenance),
        }


@dataclass(frozen=True)
class StrategyPositionPlan(_PortfolioContract):
    """Strategy-owned desired position and management semantics for a candidate."""

    strategy_session_id: str
    strategy_revision_id: str
    candidate_id: str
    candidate_rank: int
    asset: str
    direction: str
    requested_quantity: Numberish
    position_action: str = "hold"
    requested_notional: Numberish | None = None
    position_management: Mapping[str, Any] = field(default_factory=dict)
    protection_intent: Mapping[str, Any] = field(default_factory=dict)
    provenance: Mapping[str, Any] = field(default_factory=dict)
    schema_version: ClassVar[str] = STRATEGY_POSITION_PLAN_SCHEMA

    def __post_init__(self) -> None:
        strategy_session_id = _required_text(self.strategy_session_id, "strategy session id")
        strategy_revision_id = _required_text(self.strategy_revision_id, "strategy revision id")
        candidate_id = _required_text(self.candidate_id, "candidate id")
        asset = _asset(self.asset, "asset")
        direction = _choice(self.direction, _DIRECTIONS, "direction")
        action = _choice(self.position_action, _POSITION_ACTIONS, "position action")
        rank = _positive_int(self.candidate_rank, "candidate rank")
        quantity = _decimal(self.requested_quantity, "requested quantity", minimum=Decimal("0"))
        notional = (
            None
            if self.requested_notional is None
            else _decimal(self.requested_notional, "requested notional", minimum=Decimal("0"))
        )
        if direction == "flat" and quantity != 0:
            raise ValueError("flat strategy position plan must have zero requested quantity")
        if direction == "flat" and action in {"open", "add"}:
            raise ValueError("flat strategy position plan cannot open or add")
        object.__setattr__(self, "strategy_session_id", strategy_session_id)
        object.__setattr__(self, "strategy_revision_id", strategy_revision_id)
        object.__setattr__(self, "candidate_id", candidate_id)
        object.__setattr__(self, "candidate_rank", rank)
        object.__setattr__(self, "asset", asset)
        object.__setattr__(self, "direction", direction)
        object.__setattr__(self, "requested_quantity", quantity)
        object.__setattr__(self, "position_action", action)
        object.__setattr__(self, "requested_notional", notional)
        object.__setattr__(self, "position_management", _object(self.position_management, "position management"))
        object.__setattr__(self, "protection_intent", _object(self.protection_intent, "protection intent"))
        object.__setattr__(self, "provenance", _object(self.provenance, "strategy plan provenance"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "strategy_session_id": self.strategy_session_id,
            "strategy_revision_id": self.strategy_revision_id,
            "candidate_id": self.candidate_id,
            "candidate_rank": self.candidate_rank,
            "asset": self.asset,
            "direction": self.direction,
            "requested_quantity": _decimal_text(self.requested_quantity),
            "position_action": self.position_action,
            "requested_notional": (
                None if self.requested_notional is None else _decimal_text(self.requested_notional)
            ),
            "position_management": _thaw(self.position_management),
            "protection_intent": _thaw(self.protection_intent),
            "provenance": _thaw(self.provenance),
        }


@dataclass(frozen=True)
class StrategyCandidateSet(_PortfolioContract):
    """Ranked, asset-agnostic strategy opportunities for one strategy revision."""

    candidate_set_id: str
    strategy_session_id: str
    strategy_revision_id: str
    created_at: str
    candidates: Sequence[StrategyPositionPlan] = ()
    provenance: Mapping[str, Any] = field(default_factory=dict)
    schema_version: ClassVar[str] = STRATEGY_CANDIDATE_SET_SCHEMA

    def __post_init__(self) -> None:
        candidate_set_id = _required_text(self.candidate_set_id, "candidate set id")
        strategy_session_id = _required_text(self.strategy_session_id, "candidate set strategy session id")
        strategy_revision_id = _required_text(self.strategy_revision_id, "candidate set strategy revision id")
        created_at = _aware_iso(self.created_at, "candidate set created_at")
        candidates = tuple(self.candidates)
        if not all(isinstance(item, StrategyPositionPlan) for item in candidates):
            raise TypeError("candidate set candidates must be StrategyPositionPlan contracts")
        candidate_ids = [item.candidate_id for item in candidates]
        ranks = [item.candidate_rank for item in candidates]
        if len(set(candidate_ids)) != len(candidate_ids):
            raise ValueError("candidate set candidate ids must be unique")
        if len(set(ranks)) != len(ranks):
            raise ValueError("candidate set ranks must be unique")
        for item in candidates:
            if item.strategy_session_id != strategy_session_id:
                raise ValueError("candidate set strategy session identity mismatch")
            if item.strategy_revision_id != strategy_revision_id:
                raise ValueError("candidate set strategy revision identity mismatch")
        object.__setattr__(self, "candidate_set_id", candidate_set_id)
        object.__setattr__(self, "strategy_session_id", strategy_session_id)
        object.__setattr__(self, "strategy_revision_id", strategy_revision_id)
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "candidates", tuple(sorted(candidates, key=lambda item: item.candidate_rank)))
        object.__setattr__(self, "provenance", _object(self.provenance, "candidate set provenance"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "candidate_set_id": self.candidate_set_id,
            "strategy_session_id": self.strategy_session_id,
            "strategy_revision_id": self.strategy_revision_id,
            "created_at": self.created_at,
            "candidates": [item.to_dict() for item in self.candidates],
            "provenance": _thaw(self.provenance),
        }


@dataclass(frozen=True)
class PortfolioPolicy(_PortfolioContract):
    """Versioned, subtractive portfolio constraints; no gate algorithm lives here."""

    policy_id: str
    policy_revision: str
    max_single_asset_aum_pct: Numberish = Decimal("30")
    max_active_assets: int = 10
    max_total_exposure_pct: Numberish | None = None
    max_margin_pct: Numberish | None = None
    max_loss_pct: Numberish | None = None
    min_cash_buffer_pct: Numberish | None = None
    max_leverage: Numberish | None = None
    min_order_quantity: Numberish | None = None
    min_order_notional: Numberish | None = None
    provenance: Mapping[str, Any] = field(default_factory=dict)
    schema_version: ClassVar[str] = PORTFOLIO_POLICY_SCHEMA

    def __post_init__(self) -> None:
        policy_id = _required_text(self.policy_id, "policy id")
        policy_revision = _required_text(self.policy_revision, "policy revision")
        single_cap = _percentage(self.max_single_asset_aum_pct, "max_single_asset_aum_pct", strictly_positive=True)
        active_assets = _positive_int(self.max_active_assets, "max_active_assets")
        optional_limits = {
            name: (
                None
                if value is None
                else _percentage(value, name, strictly_positive=False)
            )
            for name, value in (
                ("max_total_exposure_pct", self.max_total_exposure_pct),
                ("max_margin_pct", self.max_margin_pct),
                ("max_loss_pct", self.max_loss_pct),
                ("min_cash_buffer_pct", self.min_cash_buffer_pct),
            )
        }
        max_leverage = (
            None
            if self.max_leverage is None
            else _positive_decimal(self.max_leverage, "max_leverage")
        )
        min_order_quantity = (
            None
            if self.min_order_quantity is None
            else _positive_decimal(self.min_order_quantity, "min_order_quantity")
        )
        min_order_notional = (
            None
            if self.min_order_notional is None
            else _positive_decimal(self.min_order_notional, "min_order_notional")
        )
        object.__setattr__(self, "policy_id", policy_id)
        object.__setattr__(self, "policy_revision", policy_revision)
        object.__setattr__(self, "max_single_asset_aum_pct", single_cap)
        object.__setattr__(self, "max_active_assets", active_assets)
        for name, value in optional_limits.items():
            object.__setattr__(self, name, value)
        object.__setattr__(self, "max_leverage", max_leverage)
        object.__setattr__(self, "min_order_quantity", min_order_quantity)
        object.__setattr__(self, "min_order_notional", min_order_notional)
        object.__setattr__(self, "provenance", _object(self.provenance, "portfolio policy provenance"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "policy_revision": self.policy_revision,
            "max_single_asset_aum_pct": _decimal_text(self.max_single_asset_aum_pct),
            "max_active_assets": self.max_active_assets,
            "max_total_exposure_pct": _optional_decimal_text(self.max_total_exposure_pct),
            "max_margin_pct": _optional_decimal_text(self.max_margin_pct),
            "max_loss_pct": _optional_decimal_text(self.max_loss_pct),
            "min_cash_buffer_pct": _optional_decimal_text(self.min_cash_buffer_pct),
            "max_leverage": _optional_decimal_text(self.max_leverage),
            "min_order_quantity": _optional_decimal_text(self.min_order_quantity),
            "min_order_notional": _optional_decimal_text(self.min_order_notional),
            "provenance": _thaw(self.provenance),
        }


@dataclass(frozen=True)
class AssetAllocationSlice(_PortfolioContract):
    """One asset's portfolio allocation, retaining requested and effective size."""

    portfolio_session_id: str
    allocation_id: str
    candidate_id: str
    asset: str
    direction: str
    requested_quantity: Numberish
    effective_quantity: Numberish
    source_strategy_plan_digest: str
    position_action: str
    position_management: Mapping[str, Any] = field(default_factory=dict)
    protection_intent: Mapping[str, Any] = field(default_factory=dict)
    requested_notional: Numberish | None = None
    effective_notional: Numberish | None = None
    status: str = "accepted"
    execution_slice_id: str | None = None
    reasons: Sequence[str] = ()
    provenance: Mapping[str, Any] = field(default_factory=dict)
    schema_version: ClassVar[str] = ASSET_ALLOCATION_SLICE_SCHEMA

    def __post_init__(self) -> None:
        portfolio_session_id = _required_text(self.portfolio_session_id, "allocation portfolio session id")
        allocation_id = _required_text(self.allocation_id, "allocation id")
        candidate_id = _required_text(self.candidate_id, "allocation candidate id")
        asset = _asset(self.asset, "allocation asset")
        direction = _choice(self.direction, _DIRECTIONS, "allocation direction")
        requested = _decimal(self.requested_quantity, "requested allocation quantity", minimum=Decimal("0"))
        effective = _decimal(self.effective_quantity, "effective allocation quantity", minimum=Decimal("0"))
        source_strategy_plan_digest = _required_text(
            self.source_strategy_plan_digest,
            "source strategy plan digest",
        )
        position_action = _choice(self.position_action, _POSITION_ACTIONS, "allocation position action")
        requested_notional = (
            None
            if self.requested_notional is None
            else _decimal(self.requested_notional, "requested allocation notional", minimum=Decimal("0"))
        )
        effective_notional = (
            requested_notional
            if self.effective_notional is None and requested_notional is not None
            else (
                None
                if self.effective_notional is None
                else _decimal(self.effective_notional, "effective allocation notional", minimum=Decimal("0"))
            )
        )
        if requested_notional is None and effective_notional is not None:
            raise ValueError("effective allocation notional requires requested notional")
        if requested_notional is not None and effective_notional is not None and effective_notional > requested_notional:
            raise ValueError("effective notional cannot exceed requested notional")
        if effective > requested:
            raise ValueError("effective quantity cannot exceed requested quantity")
        if direction == "flat" and (requested != 0 or effective != 0):
            raise ValueError("flat allocation must have zero requested and effective quantity")
        status = _choice(self.status, _SELECTION_STATUSES, "allocation status")
        execution_slice_id = (
            None if self.execution_slice_id is None else _required_text(self.execution_slice_id, "execution slice id")
        )
        reasons = _text_tuple(self.reasons, "allocation reasons")
        downscaled = effective < requested or (
            requested_notional is not None
            and effective_notional is not None
            and effective_notional < requested_notional
        )
        if downscaled and (status != "scaled" or not reasons):
            raise ValueError("downscaled allocation requires scaled status and a reason")
        if not downscaled and status == "scaled":
            raise ValueError("scaled allocation must reduce effective quantity")
        object.__setattr__(self, "portfolio_session_id", portfolio_session_id)
        object.__setattr__(self, "allocation_id", allocation_id)
        object.__setattr__(self, "candidate_id", candidate_id)
        object.__setattr__(self, "asset", asset)
        object.__setattr__(self, "direction", direction)
        object.__setattr__(self, "requested_quantity", requested)
        object.__setattr__(self, "effective_quantity", effective)
        object.__setattr__(self, "source_strategy_plan_digest", source_strategy_plan_digest)
        object.__setattr__(self, "position_action", position_action)
        object.__setattr__(self, "position_management", _object(self.position_management, "allocation position management"))
        object.__setattr__(self, "protection_intent", _object(self.protection_intent, "allocation protection intent"))
        object.__setattr__(self, "requested_notional", requested_notional)
        object.__setattr__(self, "effective_notional", effective_notional)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "execution_slice_id", execution_slice_id)
        object.__setattr__(self, "reasons", reasons)
        object.__setattr__(self, "provenance", _object(self.provenance, "allocation provenance"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "portfolio_session_id": self.portfolio_session_id,
            "allocation_id": self.allocation_id,
            "candidate_id": self.candidate_id,
            "asset": self.asset,
            "direction": self.direction,
            "requested_quantity": _decimal_text(self.requested_quantity),
            "effective_quantity": _decimal_text(self.effective_quantity),
            "source_strategy_plan_digest": self.source_strategy_plan_digest,
            "position_action": self.position_action,
            "position_management": _thaw(self.position_management),
            "protection_intent": _thaw(self.protection_intent),
            "requested_notional": _optional_decimal_text(self.requested_notional),
            "effective_notional": _optional_decimal_text(self.effective_notional),
            "status": self.status,
            "execution_slice_id": self.execution_slice_id,
            "reasons": list(self.reasons),
            "provenance": _thaw(self.provenance),
        }


@dataclass(frozen=True)
class ExecutionSlice(_PortfolioContract):
    """Broker-bound execution facts for one allocation; still read-only evidence."""

    execution_slice_id: str
    portfolio_session_id: str
    allocation_id: str
    asset: str
    broker_binding: Mapping[str, Any]
    orders: Sequence[Mapping[str, Any]] = ()
    position: Mapping[str, Any] = field(default_factory=dict)
    protection: Mapping[str, Any] = field(default_factory=dict)
    fills: Sequence[Mapping[str, Any]] = ()
    reconciliation: Mapping[str, Any] = field(default_factory=dict)
    status: str = "pending"
    schema_version: ClassVar[str] = EXECUTION_SLICE_SCHEMA

    def __post_init__(self) -> None:
        execution_slice_id = _required_text(self.execution_slice_id, "execution slice id")
        portfolio_session_id = _required_text(self.portfolio_session_id, "execution portfolio session id")
        allocation_id = _required_text(self.allocation_id, "execution allocation id")
        asset = _asset(self.asset, "execution asset")
        object.__setattr__(self, "execution_slice_id", execution_slice_id)
        object.__setattr__(self, "portfolio_session_id", portfolio_session_id)
        object.__setattr__(self, "allocation_id", allocation_id)
        object.__setattr__(self, "asset", asset)
        object.__setattr__(self, "broker_binding", _object(self.broker_binding, "execution broker binding"))
        object.__setattr__(self, "orders", _mapping_rows(self.orders, "execution orders", canonical=True))
        object.__setattr__(self, "position", _object(self.position, "execution position"))
        object.__setattr__(self, "protection", _object(self.protection, "execution protection"))
        object.__setattr__(self, "fills", _mapping_rows(self.fills, "execution fills", canonical=True))
        object.__setattr__(self, "reconciliation", _object(self.reconciliation, "execution reconciliation"))
        object.__setattr__(self, "status", _required_text(self.status, "execution status").lower())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "execution_slice_id": self.execution_slice_id,
            "portfolio_session_id": self.portfolio_session_id,
            "allocation_id": self.allocation_id,
            "asset": self.asset,
            "broker_binding": _thaw(self.broker_binding),
            "orders": _thaw(self.orders),
            "position": _thaw(self.position),
            "protection": _thaw(self.protection),
            "fills": _thaw(self.fills),
            "reconciliation": _thaw(self.reconciliation),
            "status": self.status,
        }


@dataclass(frozen=True)
class PortfolioSnapshot(_PortfolioContract):
    """Coherent account facts used as the input snapshot for selection."""

    snapshot_id: str
    portfolio_session_id: str
    account_id: str
    observed_at: str
    equity: Numberish
    available_cash: Numberish
    total_exposure: Numberish = Decimal("0")
    margin_used: Numberish = Decimal("0")
    leverage: Numberish = Decimal("0")
    loss_pct: Numberish = Decimal("0")
    cash_buffer_pct: Numberish | None = None
    positions: Sequence[Mapping[str, Any]] = ()
    open_orders: Sequence[Mapping[str, Any]] = ()
    allocation_slices: Sequence[AssetAllocationSlice] = ()
    execution_slices: Sequence[ExecutionSlice] = ()
    ownership_facts: Sequence[Mapping[str, Any]] = ()
    coherent: bool = True
    fresh: bool = True
    provenance: Mapping[str, Any] = field(default_factory=dict)
    schema_version: ClassVar[str] = PORTFOLIO_SNAPSHOT_SCHEMA

    def __post_init__(self) -> None:
        snapshot_id = _required_text(self.snapshot_id, "snapshot id")
        portfolio_session_id = _required_text(self.portfolio_session_id, "snapshot portfolio session id")
        account_id = _required_text(self.account_id, "snapshot account id")
        observed_at = _aware_iso(self.observed_at, "portfolio snapshot observed_at")
        equity = _decimal(self.equity, "snapshot equity")
        available_cash = _decimal(self.available_cash, "snapshot available cash")
        total_exposure = _decimal(self.total_exposure, "snapshot total exposure", minimum=Decimal("0"))
        margin_used = _decimal(self.margin_used, "snapshot margin used", minimum=Decimal("0"))
        leverage = _decimal(self.leverage, "snapshot leverage", minimum=Decimal("0"))
        loss_pct = _decimal(self.loss_pct, "snapshot loss pct")
        cash_buffer_pct = (
            None
            if self.cash_buffer_pct is None
            else _percentage(self.cash_buffer_pct, "snapshot cash_buffer_pct", strictly_positive=False)
        )
        positions = _mapping_rows(self.positions, "snapshot positions", canonical=True)
        open_orders = _mapping_rows(self.open_orders, "snapshot open orders", canonical=True)
        allocations = tuple(self.allocation_slices)
        executions = tuple(self.execution_slices)
        if not all(isinstance(item, AssetAllocationSlice) for item in allocations):
            raise TypeError("snapshot allocation_slices must be AssetAllocationSlice contracts")
        if not all(isinstance(item, ExecutionSlice) for item in executions):
            raise TypeError("snapshot execution_slices must be ExecutionSlice contracts")
        allocation_id_values = [item.allocation_id for item in allocations]
        execution_id_values = [item.execution_slice_id for item in executions]
        if len(set(allocation_id_values)) != len(allocation_id_values):
            raise ValueError("snapshot allocation ids must be unique")
        if len(set(execution_id_values)) != len(execution_id_values):
            raise ValueError("snapshot execution slice ids must be unique")
        allocation_execution_ids = [item.execution_slice_id for item in allocations if item.execution_slice_id is not None]
        if len(set(allocation_execution_ids)) != len(allocation_execution_ids):
            raise ValueError("snapshot allocation execution references must be unique")
        allocation_ids = set(allocation_id_values)
        execution_by_id = {item.execution_slice_id: item for item in executions}
        for item in allocations:
            if item.portfolio_session_id != portfolio_session_id:
                raise ValueError("snapshot allocation portfolio session identity mismatch")
            if item.execution_slice_id is not None:
                execution = execution_by_id.get(item.execution_slice_id)
                if execution is None:
                    raise ValueError("snapshot allocation references an unknown execution slice")
                if execution.asset != item.asset:
                    raise ValueError("snapshot allocation and execution asset identity mismatch")
                if execution.allocation_id != item.allocation_id:
                    raise ValueError("snapshot allocation and execution link identity mismatch")
        for item in executions:
            if item.portfolio_session_id != portfolio_session_id:
                raise ValueError("snapshot execution portfolio session identity mismatch")
            if item.allocation_id not in allocation_ids:
                raise ValueError("snapshot execution references an unknown allocation")
        ownership = _ownership_rows(self.ownership_facts, portfolio_session_id, account_id)
        for row in (*positions, *open_orders):
            _check_row_scope(row, portfolio_session_id, account_id, required=True)
        if not isinstance(self.coherent, bool) or not isinstance(self.fresh, bool):
            raise TypeError("snapshot coherent and fresh flags must be bool")
        object.__setattr__(self, "snapshot_id", snapshot_id)
        object.__setattr__(self, "portfolio_session_id", portfolio_session_id)
        object.__setattr__(self, "account_id", account_id)
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "equity", equity)
        object.__setattr__(self, "available_cash", available_cash)
        object.__setattr__(self, "total_exposure", total_exposure)
        object.__setattr__(self, "margin_used", margin_used)
        object.__setattr__(self, "leverage", leverage)
        object.__setattr__(self, "loss_pct", loss_pct)
        object.__setattr__(self, "cash_buffer_pct", cash_buffer_pct)
        object.__setattr__(self, "positions", positions)
        object.__setattr__(self, "open_orders", open_orders)
        object.__setattr__(self, "allocation_slices", tuple(sorted(allocations, key=lambda item: item.allocation_id)))
        object.__setattr__(self, "execution_slices", tuple(sorted(executions, key=lambda item: item.execution_slice_id)))
        object.__setattr__(self, "ownership_facts", ownership)
        object.__setattr__(self, "provenance", _object(self.provenance, "snapshot provenance"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "portfolio_session_id": self.portfolio_session_id,
            "account_id": self.account_id,
            "observed_at": self.observed_at,
            "equity": _decimal_text(self.equity),
            "available_cash": _decimal_text(self.available_cash),
            "total_exposure": _decimal_text(self.total_exposure),
            "margin_used": _decimal_text(self.margin_used),
            "leverage": _decimal_text(self.leverage),
            "loss_pct": _decimal_text(self.loss_pct),
            "cash_buffer_pct": _optional_decimal_text(self.cash_buffer_pct),
            "positions": _thaw(self.positions),
            "open_orders": _thaw(self.open_orders),
            "allocation_slices": [item.to_dict() for item in self.allocation_slices],
            "execution_slices": [item.to_dict() for item in self.execution_slices],
            "ownership_facts": _thaw(self.ownership_facts),
            "coherent": self.coherent,
            "fresh": self.fresh,
            "provenance": _thaw(self.provenance),
        }


@dataclass(frozen=True)
class PortfolioSelection(_PortfolioContract):
    """Immutable accepted/scaled allocation result with explicit provenance."""

    selection_id: str
    portfolio_session_id: str
    candidate_set_id: str
    policy_id: str
    policy_revision: str
    snapshot_id: str
    created_at: str
    selected_allocations: Sequence[AssetAllocationSlice] = ()
    rejected_candidates: Sequence[Mapping[str, Any]] = ()
    decision_provenance: Mapping[str, Any] = field(default_factory=dict)
    schema_version: ClassVar[str] = PORTFOLIO_SELECTION_SCHEMA

    def __post_init__(self) -> None:
        selection_id = _required_text(self.selection_id, "selection id")
        portfolio_session_id = _required_text(self.portfolio_session_id, "selection portfolio session id")
        candidate_set_id = _required_text(self.candidate_set_id, "selection candidate set id")
        policy_id = _required_text(self.policy_id, "selection policy id")
        policy_revision = _required_text(self.policy_revision, "selection policy revision")
        snapshot_id = _required_text(self.snapshot_id, "selection snapshot id")
        created_at = _aware_iso(self.created_at, "portfolio selection created_at")
        allocations = tuple(self.selected_allocations)
        if not all(isinstance(item, AssetAllocationSlice) for item in allocations):
            raise TypeError("selection selected_allocations must be AssetAllocationSlice contracts")
        allocation_ids = [item.allocation_id for item in allocations]
        candidate_ids = [item.candidate_id for item in allocations]
        if len(set(allocation_ids)) != len(allocation_ids):
            raise ValueError("selection allocation ids must be unique")
        if len(set(candidate_ids)) != len(candidate_ids):
            raise ValueError("selection candidate ids must be unique")
        for item in allocations:
            if item.portfolio_session_id != portfolio_session_id:
                raise ValueError("selection allocation portfolio session identity mismatch")
        rejected = _rejection_rows(self.rejected_candidates)
        selected_candidate_ids = {item.candidate_id for item in allocations}
        rejected_candidate_ids = {str(row["candidate_id"]) for row in rejected}
        if selected_candidate_ids & rejected_candidate_ids:
            raise ValueError("selection cannot both select and reject the same candidate")
        object.__setattr__(self, "selection_id", selection_id)
        object.__setattr__(self, "portfolio_session_id", portfolio_session_id)
        object.__setattr__(self, "candidate_set_id", candidate_set_id)
        object.__setattr__(self, "policy_id", policy_id)
        object.__setattr__(self, "policy_revision", policy_revision)
        object.__setattr__(self, "snapshot_id", snapshot_id)
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "selected_allocations", tuple(sorted(allocations, key=lambda item: item.allocation_id)))
        object.__setattr__(self, "rejected_candidates", rejected)
        object.__setattr__(self, "decision_provenance", _object(self.decision_provenance, "selection decision provenance"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "selection_id": self.selection_id,
            "portfolio_session_id": self.portfolio_session_id,
            "candidate_set_id": self.candidate_set_id,
            "policy_id": self.policy_id,
            "policy_revision": self.policy_revision,
            "snapshot_id": self.snapshot_id,
            "created_at": self.created_at,
            "selected_allocations": [item.to_dict() for item in self.selected_allocations],
            "rejected_candidates": _thaw(self.rejected_candidates),
            "decision_provenance": _thaw(self.decision_provenance),
        }


@dataclass(frozen=True)
class PortfolioRiskHold(_PortfolioContract):
    """Immutable portfolio-level hold when shared exposure cannot be proven safe."""

    hold_id: str
    portfolio_session_id: str
    candidate_set_id: str
    policy_id: str
    policy_revision: str
    snapshot_id: str
    created_at: str
    reason_code: str
    message: str
    affected_assets: Sequence[str] = ()
    decision_provenance: Mapping[str, Any] = field(default_factory=dict)
    blocks_new_entries: bool = True
    schema_version: ClassVar[str] = PORTFOLIO_RISK_HOLD_SCHEMA

    def __post_init__(self) -> None:
        hold_id = _required_text(self.hold_id, "risk hold id")
        portfolio_session_id = _required_text(self.portfolio_session_id, "risk hold portfolio session id")
        candidate_set_id = _required_text(self.candidate_set_id, "risk hold candidate set id")
        policy_id = _required_text(self.policy_id, "risk hold policy id")
        policy_revision = _required_text(self.policy_revision, "risk hold policy revision")
        snapshot_id = _required_text(self.snapshot_id, "risk hold snapshot id")
        created_at = _aware_iso(self.created_at, "risk hold created_at")
        reason_code = _required_text(self.reason_code, "risk hold reason code").lower()
        message = _required_text(self.message, "risk hold message")
        assets = tuple(sorted({_asset(item, "risk hold affected asset") for item in self.affected_assets}))
        if not isinstance(self.blocks_new_entries, bool):
            raise TypeError("risk hold blocks_new_entries must be bool")
        if not self.blocks_new_entries:
            raise ValueError("risk hold must block new entries")
        object.__setattr__(self, "hold_id", hold_id)
        object.__setattr__(self, "portfolio_session_id", portfolio_session_id)
        object.__setattr__(self, "candidate_set_id", candidate_set_id)
        object.__setattr__(self, "policy_id", policy_id)
        object.__setattr__(self, "policy_revision", policy_revision)
        object.__setattr__(self, "snapshot_id", snapshot_id)
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "reason_code", reason_code)
        object.__setattr__(self, "message", message)
        object.__setattr__(self, "affected_assets", assets)
        object.__setattr__(self, "decision_provenance", _object(self.decision_provenance, "risk hold decision provenance"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "hold_id": self.hold_id,
            "portfolio_session_id": self.portfolio_session_id,
            "candidate_set_id": self.candidate_set_id,
            "policy_id": self.policy_id,
            "policy_revision": self.policy_revision,
            "snapshot_id": self.snapshot_id,
            "created_at": self.created_at,
            "reason_code": self.reason_code,
            "message": self.message,
            "affected_assets": list(self.affected_assets),
            "decision_provenance": _thaw(self.decision_provenance),
            "blocks_new_entries": self.blocks_new_entries,
        }


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")
    return value.strip()


def _asset(value: Any, label: str) -> str:
    return _required_text(value, label).upper()


def _choice(value: Any, choices: frozenset[str], label: str) -> str:
    normalized = _required_text(value, label).lower()
    if normalized not in choices:
        raise ValueError(f"unsupported {label}: {normalized}")
    return normalized


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _decimal(value: Any, label: str, *, minimum: Decimal | None = None) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be finite")
    try:
        rendered = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} must be finite") from exc
    if not rendered.is_finite():
        raise ValueError(f"{label} must be finite")
    if minimum is not None and rendered < minimum:
        raise ValueError(f"{label} must be non-negative")
    return rendered


def _percentage(value: Any, label: str, *, strictly_positive: bool) -> Decimal:
    minimum = Decimal("0")
    rendered = _decimal(value, label, minimum=minimum)
    if strictly_positive and rendered <= 0:
        raise ValueError(f"{label} must be positive")
    if rendered > Decimal("100"):
        raise ValueError(f"{label} must be between 0 and 100")
    return rendered


def _positive_decimal(value: Any, label: str) -> Decimal:
    rendered = _decimal(value, label, minimum=Decimal("0"))
    if rendered <= 0:
        raise ValueError(f"{label} must be positive")
    return rendered


def _decimal_text(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _optional_decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else _decimal_text(value)


def _aware_iso(value: Any, label: str) -> str:
    rendered = _required_text(value, label)
    try:
        parsed = datetime.fromisoformat(rendered.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _object(value: Any, label: str) -> Mapping[str, Any]:
    plain = _plain_json(value)
    if not isinstance(plain, dict):
        raise TypeError(f"{label} must be an object")
    return _freeze(plain)


def _mapping_rows(
    value: Any,
    label: str,
    *,
    canonical: bool = False,
) -> tuple[Mapping[str, Any], ...]:
    if isinstance(value, (str, bytes, Mapping)):
        raise TypeError(f"{label} must be a sequence of objects")
    try:
        rows = tuple(value)
    except TypeError as exc:
        raise TypeError(f"{label} must be a sequence of objects") from exc
    result: list[Mapping[str, Any]] = []
    for row in rows:
        result.append(_object(row, label))
    if canonical:
        return tuple(sorted(result, key=lambda row: json.dumps(_thaw(row), sort_keys=True, separators=(",", ":"))))
    return tuple(result)


def _text_tuple(value: Any, label: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise TypeError(f"{label} must be a sequence of strings")
    try:
        values = tuple(value)
    except TypeError as exc:
        raise TypeError(f"{label} must be a sequence of strings") from exc
    return tuple(_required_text(item, label) for item in values)


def _rejection_rows(value: Any) -> tuple[Mapping[str, Any], ...]:
    rows = _mapping_rows(value, "selection rejected_candidates")
    normalized: list[Mapping[str, Any]] = []
    candidate_ids: set[str] = set()
    for row in rows:
        candidate_id = _required_text(row.get("candidate_id"), "rejected candidate id")
        reason = _required_text(row.get("reason"), "rejected candidate reason")
        if candidate_id in candidate_ids:
            raise ValueError("selection rejected candidate ids must be unique")
        candidate_ids.add(candidate_id)
        item = dict(_thaw(row))
        item["candidate_id"] = candidate_id
        item["reason"] = reason
        normalized.append(_freeze(item))
    return tuple(sorted(normalized, key=lambda row: str(row["candidate_id"])))


def _ownership_rows(value: Any, portfolio_session_id: str, account_id: str) -> tuple[Mapping[str, Any], ...]:
    rows = _mapping_rows(value, "snapshot ownership_facts")
    normalized: list[Mapping[str, Any]] = []
    owners_by_asset: dict[str, tuple[Any, ...]] = {}
    for row in rows:
        item = dict(_thaw(row))
        asset = _asset(item.get("asset"), "ownership asset")
        item["asset"] = asset
        _check_row_scope(item, portfolio_session_id, account_id, required=True)
        item["owner_type"] = _required_text(item.get("owner_type"), "ownership owner type")
        item["owner_id"] = _required_text(item.get("owner_id"), "ownership owner id")
        owner_key = (item["owner_type"], item["owner_id"])
        previous = owners_by_asset.setdefault(asset, owner_key)
        if previous != owner_key:
            raise ValueError("conflicting ownership facts for asset")
        normalized.append(_freeze(item))
    return tuple(sorted(normalized, key=lambda row: (str(row["asset"]), json.dumps(_thaw(row), sort_keys=True))))


def _check_scope(scope: Mapping[str, Any], portfolio_session_id: str, account_id: str) -> None:
    if "portfolio_session_id" in scope and scope["portfolio_session_id"] != portfolio_session_id:
        raise ValueError("portfolio session scope identity mismatch")
    if "account_id" in scope and scope["account_id"] != account_id:
        raise ValueError("account scope identity mismatch")


def _check_row_scope(
    row: Mapping[str, Any],
    portfolio_session_id: str,
    account_id: str,
    *,
    required: bool = False,
) -> None:
    if required:
        for key, label in (
            ("portfolio_session_id", "portfolio session ownership identity"),
            ("account_id", "account ownership identity"),
        ):
            if key not in row or not isinstance(row[key], str) or not row[key].strip():
                raise ValueError(f"{label} is required")
    if "portfolio_session_id" in row and row["portfolio_session_id"] != portfolio_session_id:
        raise ValueError("portfolio session ownership scope mismatch")
    if "account_id" in row and row["account_id"] != account_id:
        raise ValueError("account ownership scope mismatch")


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("portfolio contract contains a non-finite decimal")
        return _decimal_text(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("portfolio contract contains a non-finite float")
        return value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"portfolio contract contains non-JSON value: {type(value).__name__}")


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "ASSET_ALLOCATION_SLICE_SCHEMA",
    "EXECUTION_SLICE_SCHEMA",
    "PORTFOLIO_POLICY_SCHEMA",
    "PORTFOLIO_RISK_HOLD_SCHEMA",
    "PORTFOLIO_SELECTION_SCHEMA",
    "PORTFOLIO_SESSION_SCHEMA",
    "PORTFOLIO_SNAPSHOT_SCHEMA",
    "STRATEGY_CANDIDATE_SET_SCHEMA",
    "STRATEGY_POSITION_PLAN_SCHEMA",
    "AssetAllocationSlice",
    "ExecutionSlice",
    "PortfolioPolicy",
    "PortfolioRiskHold",
    "PortfolioSelection",
    "PortfolioSession",
    "PortfolioSnapshot",
    "StrategyCandidateSet",
    "StrategyPositionPlan",
]
