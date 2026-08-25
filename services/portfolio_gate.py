"""Pure, subtractive evaluation for one Strategy Position Plan.

This module is intentionally a composition-root policy service, not a Broker
adapter.  It consumes immutable portfolio facts and returns immutable evidence;
the caller remains responsible for authorization, persistence, and execution.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from typing import Any

from schemas.portfolio import (
    AssetAllocationSlice,
    PortfolioPolicy,
    PortfolioRiskHold,
    PortfolioSelection,
    PortfolioSnapshot,
    StrategyCandidateSet,
    StrategyPositionPlan,
)


PORTFOLIO_GATE_SCHEMA = "portfolio-risk-gate-v1"

_REASON_MESSAGES = {
    "invalid_equity": "account equity must be positive before increasing exposure",
    "missing_requested_notional": "an exposure-increasing plan must declare requested notional",
    "non_positive_notional": "requested notional must be positive for a new exposure",
    "below_minimum_quantity": "requested quantity is below the configured minimum lot",
    "below_minimum_notional": "requested notional is below the configured minimum notional",
    "single_asset_concentration": "single-asset AUM concentration cap leaves no capacity",
    "global_exposure": "global exposure cap leaves no capacity",
    "margin": "margin cap leaves no capacity",
    "leverage": "leverage cap leaves no capacity",
    "cash_buffer": "cash-buffer cap leaves no capacity",
    "loss_limit": "loss limit blocks new exposure",
    "max_active_assets": "active-asset capacity is exhausted",
    "scaled_below_minimum_quantity": "downward scaling would fall below the minimum lot",
    "scaled_below_minimum_notional": "downward scaling would fall below the minimum notional",
}


class PortfolioRiskGate:
    """Evaluate one candidate or ranked candidate set subtractively."""

    schema_version = PORTFOLIO_GATE_SCHEMA

    def evaluate(
        self,
        candidate: StrategyPositionPlan | StrategyCandidateSet,
        snapshot: PortfolioSnapshot,
        policy: PortfolioPolicy,
    ) -> PortfolioSelection | PortfolioRiskHold:
        if isinstance(candidate, StrategyCandidateSet):
            return self.evaluate_candidates(candidate, snapshot, policy)
        _require_contract(candidate, StrategyPositionPlan, "candidate")
        _require_contract(snapshot, PortfolioSnapshot, "snapshot")
        _require_contract(policy, PortfolioPolicy, "policy")
        return self._evaluate_one(
            candidate,
            snapshot,
            policy,
            candidate_set_id=_candidate_set_id(candidate),
        )

    def evaluate_candidates(
        self,
        candidate_set: StrategyCandidateSet,
        snapshot: PortfolioSnapshot,
        policy: PortfolioPolicy,
    ) -> PortfolioSelection | PortfolioRiskHold:
        """Process a ranked set against a virtual snapshot, never replacing slices."""

        _require_contract(candidate_set, StrategyCandidateSet, "candidate set")
        _require_contract(snapshot, PortfolioSnapshot, "snapshot")
        _require_contract(policy, PortfolioPolicy, "policy")
        if not candidate_set.candidates:
            provenance = {
                "gate_schema": self.schema_version,
                "candidate_set_id": candidate_set.candidate_set_id,
                "snapshot_id": snapshot.snapshot_id,
                "policy_id": policy.policy_id,
                "policy_revision": policy.policy_revision,
                "outcome": "EMPTY_CANDIDATE_SET",
                "reasons": [],
            }
            return PortfolioSelection(
                selection_id=_stable_id("selection", provenance),
                portfolio_session_id=snapshot.portfolio_session_id,
                candidate_set_id=candidate_set.candidate_set_id,
                policy_id=policy.policy_id,
                policy_revision=policy.policy_revision,
                snapshot_id=snapshot.snapshot_id,
                created_at=snapshot.observed_at,
                selected_allocations=(),
                rejected_candidates=(),
                decision_provenance=provenance,
            )

        working_snapshot = snapshot
        selected: list[AssetAllocationSlice] = []
        rejected: list[Mapping[str, Any]] = []
        for plan in candidate_set.candidates:
            result = self._evaluate_one(
                plan,
                working_snapshot,
                policy,
                candidate_set_id=candidate_set.candidate_set_id,
            )
            if isinstance(result, PortfolioRiskHold):
                return result
            if result.selected_allocations:
                allocation = result.selected_allocations[0]
                selected.append(allocation)
                if _increases_exposure(plan):
                    working_snapshot = _project_selection(working_snapshot, allocation)
            else:
                rejected.extend(result.rejected_candidates)

        provenance = {
            "gate_schema": self.schema_version,
            "candidate_set_id": candidate_set.candidate_set_id,
            "candidate_set_digest": candidate_set.digest,
            "snapshot_id": snapshot.snapshot_id,
            "policy_id": policy.policy_id,
            "policy_revision": policy.policy_revision,
            "outcome": "SELECT_CANDIDATES",
            "processed_count": len(candidate_set.candidates),
            "selected_count": len(selected),
            "rejected_count": len(rejected),
        }
        return PortfolioSelection(
            selection_id=_stable_id(
                "selection",
                {
                    "provenance": provenance,
                    "selected": [item.to_dict() for item in selected],
                    "rejected": [_thaw(item) for item in rejected],
                },
            ),
            portfolio_session_id=snapshot.portfolio_session_id,
            candidate_set_id=candidate_set.candidate_set_id,
            policy_id=policy.policy_id,
            policy_revision=policy.policy_revision,
            snapshot_id=snapshot.snapshot_id,
            created_at=snapshot.observed_at,
            selected_allocations=tuple(selected),
            rejected_candidates=tuple(rejected),
            decision_provenance=provenance,
        )

    def _evaluate_one(
        self,
        candidate: StrategyPositionPlan,
        snapshot: PortfolioSnapshot,
        policy: PortfolioPolicy,
        *,
        candidate_set_id: str,
    ) -> PortfolioSelection | PortfolioRiskHold:

        if not snapshot.coherent:
            return self._hold(candidate, snapshot, policy, "snapshot_not_coherent", candidate_set_id=candidate_set_id)
        if not snapshot.fresh:
            return self._hold(candidate, snapshot, policy, "snapshot_stale", candidate_set_id=candidate_set_id)

        increasing = _increases_exposure(candidate)
        if not increasing:
            return self._selection(
                candidate,
                snapshot,
                policy,
                effective_quantity=candidate.requested_quantity,
                effective_notional=candidate.requested_notional,
                status="accepted",
                reasons=(),
                outcome="ACCEPT_UNCHANGED",
                candidate_set_id=candidate_set_id,
            )

        if snapshot.equity <= 0:
            return self._reject(candidate, snapshot, policy, "invalid_equity", candidate_set_id=candidate_set_id)
        requested_notional = candidate.requested_notional
        if requested_notional is None:
            return self._reject(candidate, snapshot, policy, "missing_requested_notional", candidate_set_id=candidate_set_id)
        if requested_notional <= 0:
            return self._reject(candidate, snapshot, policy, "non_positive_notional", candidate_set_id=candidate_set_id)
        if (
            policy.min_order_quantity is not None
            and candidate.requested_quantity < policy.min_order_quantity
        ):
            return self._reject(candidate, snapshot, policy, "below_minimum_quantity", candidate_set_id=candidate_set_id)
        if (
            policy.min_order_notional is not None
            and requested_notional < policy.min_order_notional
        ):
            return self._reject(candidate, snapshot, policy, "below_minimum_notional", candidate_set_id=candidate_set_id)

        early_blocker = self._early_blocker(candidate, snapshot, policy)
        if early_blocker is not None:
            return self._reject(candidate, snapshot, policy, early_blocker, candidate_set_id=candidate_set_id)

        caps = self._available_caps(candidate, snapshot, policy, requested_notional)
        effective_notional = requested_notional
        reasons: list[str] = []
        for reason, available in caps:
            if available < effective_notional:
                effective_notional = available
                reasons.append(reason)

        if not reasons:
            return self._selection(
                candidate,
                snapshot,
                policy,
                effective_quantity=candidate.requested_quantity,
                effective_notional=requested_notional,
                status="accepted",
                reasons=(),
                outcome="ACCEPT_UNCHANGED",
                candidate_set_id=candidate_set_id,
            )

        if effective_notional <= 0:
            return self._reject(candidate, snapshot, policy, reasons[0], candidate_set_id=candidate_set_id)

        effective_quantity = candidate.requested_quantity * effective_notional / requested_notional
        if policy.min_order_quantity is not None and effective_quantity < policy.min_order_quantity:
            return self._reject(candidate, snapshot, policy, "scaled_below_minimum_quantity", candidate_set_id=candidate_set_id)
        if policy.min_order_notional is not None and effective_notional < policy.min_order_notional:
            return self._reject(candidate, snapshot, policy, "scaled_below_minimum_notional", candidate_set_id=candidate_set_id)
        return self._selection(
            candidate,
            snapshot,
            policy,
            effective_quantity=effective_quantity,
            effective_notional=effective_notional,
            status="scaled",
            reasons=tuple(dict.fromkeys(reasons)),
            outcome="SCALE_DOWN",
            candidate_set_id=candidate_set_id,
        )

    def _early_blocker(
        self,
        candidate: StrategyPositionPlan,
        snapshot: PortfolioSnapshot,
        policy: PortfolioPolicy,
    ) -> str | None:
        if policy.max_loss_pct is not None and snapshot.loss_pct >= policy.max_loss_pct:
            return "loss_limit"
        if policy.max_leverage is not None and snapshot.leverage > policy.max_leverage:
            return "leverage"
        active_assets = _active_assets(snapshot)
        if candidate.asset not in active_assets and len(active_assets) >= policy.max_active_assets:
            return "max_active_assets"
        return None

    def _available_caps(
        self,
        candidate: StrategyPositionPlan,
        snapshot: PortfolioSnapshot,
        policy: PortfolioPolicy,
        requested_notional: Decimal,
    ) -> tuple[tuple[str, Decimal], ...]:
        equity = snapshot.equity
        existing_asset = _asset_exposure(snapshot, candidate.asset)
        caps: list[tuple[str, Decimal]] = [
            (
                "single_asset_concentration",
                equity * policy.max_single_asset_aum_pct / Decimal("100") - existing_asset,
            )
        ]
        if policy.max_total_exposure_pct is not None:
            caps.append(
                (
                    "global_exposure",
                    equity * policy.max_total_exposure_pct / Decimal("100") - snapshot.total_exposure,
                )
            )
        if policy.max_margin_pct is not None:
            caps.append(
                (
                    "margin",
                    equity * policy.max_margin_pct / Decimal("100") - snapshot.margin_used,
                )
            )
        if policy.max_leverage is not None:
            caps.append(
                (
                    "leverage",
                    equity * policy.max_leverage - snapshot.total_exposure,
                )
            )
        if policy.min_cash_buffer_pct is not None:
            required_cash = equity * policy.min_cash_buffer_pct / Decimal("100")
            caps.append(("cash_buffer", snapshot.available_cash - required_cash))
        return tuple(caps)

    def _selection(
        self,
        candidate: StrategyPositionPlan,
        snapshot: PortfolioSnapshot,
        policy: PortfolioPolicy,
        *,
        effective_quantity: Decimal,
        effective_notional: Decimal | None,
        status: str,
        reasons: tuple[str, ...],
        outcome: str,
        candidate_set_id: str,
    ) -> PortfolioSelection:
        allocation = AssetAllocationSlice(
            portfolio_session_id=snapshot.portfolio_session_id,
            allocation_id=_allocation_id(candidate, snapshot, policy),
            candidate_id=candidate.candidate_id,
            asset=candidate.asset,
            direction=candidate.direction,
            requested_quantity=candidate.requested_quantity,
            effective_quantity=effective_quantity,
            source_strategy_plan_digest=candidate.digest,
            position_action=candidate.position_action,
            position_management=candidate.position_management,
            protection_intent=candidate.protection_intent,
            requested_notional=candidate.requested_notional,
            effective_notional=effective_notional,
            candidate_rank=candidate.candidate_rank,
            status=status,
            reasons=reasons,
            provenance={
                "gate_schema": self.schema_version,
                "candidate_digest": candidate.digest,
                "snapshot_id": snapshot.snapshot_id,
                "policy_id": policy.policy_id,
                "policy_revision": policy.policy_revision,
            },
            ownership={
                "status": "owned",
                "account_id": snapshot.account_id,
                "portfolio_session_id": snapshot.portfolio_session_id,
                "asset": candidate.asset,
                "owner_type": "strategy",
                "owner_id": candidate.strategy_session_id,
                "strategy_session_id": candidate.strategy_session_id,
                "strategy_revision_id": candidate.strategy_revision_id,
            },
        )
        provenance = self._provenance(candidate, snapshot, policy, outcome, reasons)
        selection_id = _stable_id("selection", provenance, allocation.to_dict())
        return PortfolioSelection(
            selection_id=selection_id,
            portfolio_session_id=snapshot.portfolio_session_id,
            candidate_set_id=candidate_set_id,
            policy_id=policy.policy_id,
            policy_revision=policy.policy_revision,
            snapshot_id=snapshot.snapshot_id,
            created_at=snapshot.observed_at,
            selected_allocations=(allocation,),
            decision_provenance=provenance,
        )

    def _reject(
        self,
        candidate: StrategyPositionPlan,
        snapshot: PortfolioSnapshot,
        policy: PortfolioPolicy,
        reason: str,
        *,
        candidate_set_id: str,
    ) -> PortfolioSelection:
        provenance = self._provenance(candidate, snapshot, policy, "REJECT", (reason,))
        row = {
            "candidate_id": candidate.candidate_id,
            "reason": reason,
            "message": _REASON_MESSAGES[reason],
            "source_strategy_plan_digest": candidate.digest,
            "requested_quantity": _decimal_text(candidate.requested_quantity),
            "requested_notional": _optional_decimal_text(candidate.requested_notional),
            "direction": candidate.direction,
            "position_action": candidate.position_action,
            "position_management": _thaw(candidate.position_management),
            "protection_intent": _thaw(candidate.protection_intent),
        }
        return PortfolioSelection(
            selection_id=_stable_id("selection", provenance, row),
            portfolio_session_id=snapshot.portfolio_session_id,
            candidate_set_id=candidate_set_id,
            policy_id=policy.policy_id,
            policy_revision=policy.policy_revision,
            snapshot_id=snapshot.snapshot_id,
            created_at=snapshot.observed_at,
            selected_allocations=(),
            rejected_candidates=(row,),
            decision_provenance=provenance,
        )

    def _hold(
        self,
        candidate: StrategyPositionPlan,
        snapshot: PortfolioSnapshot,
        policy: PortfolioPolicy,
        reason: str,
        *,
        candidate_set_id: str,
    ) -> PortfolioRiskHold:
        provenance = self._provenance(candidate, snapshot, policy, "PORTFOLIO_RISK_HOLD", (reason,))
        return PortfolioRiskHold(
            hold_id=_stable_id("hold", provenance),
            portfolio_session_id=snapshot.portfolio_session_id,
            candidate_set_id=candidate_set_id,
            policy_id=policy.policy_id,
            policy_revision=policy.policy_revision,
            snapshot_id=snapshot.snapshot_id,
            created_at=snapshot.observed_at,
            reason_code=reason,
            message=_REASON_MESSAGES.get(reason, reason.replace("_", " ")),
            affected_assets=(candidate.asset,),
            decision_provenance=provenance,
        )

    def _provenance(
        self,
        candidate: StrategyPositionPlan,
        snapshot: PortfolioSnapshot,
        policy: PortfolioPolicy,
        outcome: str,
        reasons: tuple[str, ...],
    ) -> dict[str, Any]:
        return {
            "gate_schema": self.schema_version,
            "candidate_digest": candidate.digest,
            "snapshot_id": snapshot.snapshot_id,
            "policy_id": policy.policy_id,
            "policy_revision": policy.policy_revision,
            "outcome": outcome,
            "reasons": list(reasons),
        }


def _require_contract(value: Any, expected: type[Any], label: str) -> None:
    if not isinstance(value, expected):
        raise TypeError(f"{label} must be {expected.__name__}")


def _increases_exposure(candidate: StrategyPositionPlan) -> bool:
    return (
        candidate.position_action in {"open", "add"}
        and candidate.direction != "flat"
        and candidate.requested_quantity > 0
    )


def _active_assets(snapshot: PortfolioSnapshot) -> set[str]:
    return {
        allocation.asset
        for allocation in snapshot.allocation_slices
        if allocation.effective_quantity > 0 and allocation.direction != "flat"
    }


def _project_selection(snapshot: PortfolioSnapshot, allocation: AssetAllocationSlice) -> PortfolioSnapshot:
    """Project one accepted exposure into a private virtual snapshot for replay."""

    notional = allocation.effective_notional or Decimal("0")
    if notional <= 0:
        return snapshot
    total_exposure = snapshot.total_exposure + notional
    leverage = total_exposure / snapshot.equity if snapshot.equity > 0 else snapshot.leverage
    projected_allocation = replace(
        allocation,
        provenance={**_thaw(allocation.provenance), "virtual_projection": True},
    )
    return replace(
        snapshot,
        total_exposure=total_exposure,
        margin_used=snapshot.margin_used + notional,
        leverage=leverage,
        available_cash=snapshot.available_cash - notional,
        cash_buffer_pct=None,
        allocation_slices=(*snapshot.allocation_slices, projected_allocation),
    )


def _asset_exposure(snapshot: PortfolioSnapshot, asset: str) -> Decimal:
    position_values = [
        _row_notional(row)
        for row in snapshot.positions
        if str(row.get("asset", "")).upper() == asset
    ]
    position_values = [value for value in position_values if value is not None]
    position_total = sum((abs(value) for value in position_values), Decimal("0"))
    staged_total = sum(
        (
            allocation.effective_notional or Decimal("0")
            for allocation in snapshot.allocation_slices
            if allocation.asset == asset
            and allocation.provenance.get("virtual_projection") is True
        ),
        Decimal("0"),
    )
    if staged_total:
        return position_total + staged_total
    if position_values:
        return position_total
    return sum(
        (
            allocation.effective_notional or Decimal("0")
            for allocation in snapshot.allocation_slices
            if allocation.asset == asset
        ),
        Decimal("0"),
    )


def _row_notional(row: Mapping[str, Any]) -> Decimal | None:
    for key in ("notional", "market_value", "exposure", "abs_notional"):
        if key not in row or row[key] is None:
            continue
        try:
            value = Decimal(str(row[key]))
        except (InvalidOperation, ValueError):
            continue
        if value.is_finite():
            return value
    return None


def _allocation_id(candidate: StrategyPositionPlan, snapshot: PortfolioSnapshot, policy: PortfolioPolicy) -> str:
    return _stable_id(
        "allocation",
        {
            "candidate": candidate.digest,
            "snapshot": snapshot.snapshot_id,
            "policy": policy.policy_id,
            "policy_revision": policy.policy_revision,
        },
    )


def _candidate_set_id(candidate: StrategyPositionPlan) -> str:
    return f"single-{candidate.digest.removeprefix('sha256:')}"


def _stable_id(prefix: str, *parts: Mapping[str, Any]) -> str:
    payload = json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return f"{prefix}-{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _decimal_text(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _optional_decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else _decimal_text(value)


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


__all__ = ["PORTFOLIO_GATE_SCHEMA", "PortfolioRiskGate"]
