"""Portfolio ownership and failure-scope classification.

Ownership is an explicit Portfolio concern.  This module only classifies
read-only facts and emits adoption evidence; it never flattens, submits, or
mutates a Broker slice.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from schemas.portfolio import (
    AssetAllocationSlice,
    ExecutionSlice,
    PortfolioOwnershipAssessment,
    PortfolioOwnershipRecord,
    PortfolioPolicy,
    PortfolioRiskHold,
    PortfolioSnapshot,
)


PORTFOLIO_OWNERSHIP_REGISTRY_SCHEMA = "portfolio-ownership-registry-v1"


class PortfolioOwnershipRegistry:
    """Classify owned, unowned, adopted, flat, and unknown slice facts."""

    schema_version = PORTFOLIO_OWNERSHIP_REGISTRY_SCHEMA

    def evaluate(
        self,
        snapshot: PortfolioSnapshot,
        policy: PortfolioPolicy,
    ) -> PortfolioOwnershipAssessment:
        _require_type(snapshot, PortfolioSnapshot, "snapshot")
        _require_type(policy, PortfolioPolicy, "policy")
        execution_by_id = {item.execution_slice_id: item for item in snapshot.execution_slices}
        owned: set[str] = set()
        blocked: set[str] = set()
        frozen: set[str] = set()
        removable: set[str] = set()
        adoption_required: set[str] = set()
        evidence: list[PortfolioOwnershipRecord] = []
        identity_conflict = False

        fact_assets = sorted(
            {
                *(item.asset for item in snapshot.allocation_slices),
                *(str(row.get("asset", "")).strip().upper() for row in (*snapshot.positions, *snapshot.open_orders) if row.get("asset")),
            }
        )
        if not snapshot.coherent or not snapshot.fresh:
            reason = "snapshot_not_coherent" if not snapshot.coherent else "snapshot_stale"
            hold = _risk_hold(snapshot, policy, fact_assets, False, False, reason_override=reason)
            return _assessment(
                snapshot,
                policy,
                status="unknown_hold",
                frozen=fact_assets,
                hold=hold,
                blocks_new=True,
                evidence=(),
                provenance={"fresh": snapshot.fresh, "coherent": snapshot.coherent},
            )
        if fact_assets and (not snapshot.provenance.get("cursor") or not snapshot.provenance.get("source")):
            hold = _risk_hold(snapshot, policy, fact_assets, False, False, reason_override="missing_account_provenance")
            return _assessment(
                snapshot,
                policy,
                status="unknown_hold",
                frozen=fact_assets,
                hold=hold,
                blocks_new=True,
                evidence=(),
                provenance={"missing_account_provenance": True},
            )

        for allocation in snapshot.allocation_slices:
            execution = (
                None
                if allocation.execution_slice_id is None
                else execution_by_id.get(allocation.execution_slice_id)
            )
            if execution is not None and execution.ownership and allocation.ownership:
                if not _ownership_identity_matches(allocation.ownership, execution.ownership):
                    identity_conflict = True
                    frozen.add(allocation.asset)
                    continue
            record = _record_from_payload(
                allocation.ownership,
                snapshot=snapshot,
                allocation=allocation,
            )
            if allocation.effective_quantity == 0 and _flat_reconciled(allocation, execution):
                removable.add(allocation.asset)
                if record is not None:
                    evidence.append(record)
                continue
            if record is None:
                if _unknown_non_zero(allocation, execution):
                    frozen.add(allocation.asset)
                else:
                    blocked.add(allocation.asset)
                    adoption_required.add(allocation.asset)
                continue
            evidence.append(record)
            if record.status in {"owned", "adopted"}:
                owned.add(allocation.asset)
            elif record.status == "unknown" or _unknown_non_zero(allocation, execution):
                frozen.add(allocation.asset)
            else:
                blocked.add(allocation.asset)
                adoption_required.add(allocation.asset)

        allocated_assets = {item.asset for item in snapshot.allocation_slices}
        for row in (*snapshot.positions, *snapshot.open_orders):
            asset = str(row.get("asset", "")).strip().upper()
            if not asset or asset in allocated_assets or not _non_zero_row(row):
                continue
            status = str(row.get("ownership_status", row.get("status", "unowned"))).strip().lower()
            if status in {"unknown", "ambiguous"}:
                frozen.add(asset)
            else:
                blocked.add(asset)
                adoption_required.add(asset)

        cross_margin_unknown = bool(snapshot.provenance.get("cross_margin_unknown", False))
        unknown_assets = sorted(frozen)
        hold = None
        if (identity_conflict or cross_margin_unknown) and unknown_assets:
            hold = _risk_hold(snapshot, policy, unknown_assets, identity_conflict, cross_margin_unknown)
            status = "unknown_hold"
        elif unknown_assets:
            status = "mixed"
        elif blocked:
            status = "unowned_block"
        elif removable and not owned:
            status = "flat_removable"
        else:
            status = "owned"
        blocks_new = bool(blocked or frozen or adoption_required or hold is not None)
        provenance = {
            "registry_schema": self.schema_version,
            "snapshot_id": snapshot.snapshot_id,
            "policy_id": policy.policy_id,
            "policy_revision": policy.policy_revision,
            "coherent": snapshot.coherent,
            "fresh": snapshot.fresh,
            "cross_margin_unknown": cross_margin_unknown,
            "identity_conflict": identity_conflict,
        }
        return _assessment(
            snapshot,
            policy,
            status=status,
            owned=owned,
            blocked=blocked,
            frozen=frozen,
            removable=removable,
            adoption_required=adoption_required,
            evidence=evidence,
            hold=hold,
            blocks_new=blocks_new,
            provenance=provenance,
        )

    def adopt(
        self,
        allocation: AssetAllocationSlice,
        *,
        account_id: str,
        strategy_session_id: str,
        strategy_revision_id: str,
        adoption_id: str,
        adopted_at: str,
        reason: str,
    ) -> PortfolioOwnershipRecord:
        _require_type(allocation, AssetAllocationSlice, "allocation")
        if allocation.ownership.get("status") in {"owned", "adopted"}:
            raise ValueError("owned allocation does not require adoption")
        account_id = _required_text(account_id, "adoption account id")
        strategy_session_id = _required_text(strategy_session_id, "adoption strategy session id")
        strategy_revision_id = _required_text(strategy_revision_id, "adoption strategy revision id")
        adoption_id = _required_text(adoption_id, "adoption id")
        reason = _required_text(reason, "adoption reason")
        return PortfolioOwnershipRecord(
            ownership_id=_stable_id(
                "ownership",
                {
                    "allocation_digest": allocation.digest,
                    "account_id": account_id,
                    "strategy_session_id": strategy_session_id,
                    "strategy_revision_id": strategy_revision_id,
                    "adoption_id": adoption_id,
                },
            ),
            portfolio_session_id=allocation.portfolio_session_id,
            account_id=account_id,
            asset=allocation.asset,
            owner_type="strategy_adoption",
            owner_id=strategy_session_id,
            status="adopted",
            strategy_session_id=strategy_session_id,
            strategy_revision_id=strategy_revision_id,
            adoption_id=adoption_id,
            adopted_at=adopted_at,
            provenance={
                "registry_schema": self.schema_version,
                "source_allocation_digest": allocation.digest,
                "reason": reason,
            },
        )


def _record_from_payload(
    payload: Mapping[str, Any],
    *,
    snapshot: PortfolioSnapshot,
    allocation: AssetAllocationSlice,
) -> PortfolioOwnershipRecord | None:
    if not payload:
        return None
    try:
        return PortfolioOwnershipRecord(
            ownership_id=str(payload.get("ownership_id") or _stable_id("ownership", dict(payload))),
            portfolio_session_id=str(payload.get("portfolio_session_id", snapshot.portfolio_session_id)),
            account_id=str(payload.get("account_id", snapshot.account_id)),
            asset=str(payload.get("asset", allocation.asset)),
            owner_type=str(payload.get("owner_type", "")),
            owner_id=str(payload.get("owner_id", "")),
            status=str(payload.get("status", "unknown")),
            strategy_session_id=payload.get("strategy_session_id"),
            strategy_revision_id=payload.get("strategy_revision_id"),
            adoption_id=payload.get("adoption_id"),
            adopted_at=payload.get("adopted_at"),
            provenance={"source": "slice-ownership"},
        )
    except (TypeError, ValueError):
        return None


def _ownership_identity_matches(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return all(left.get(key) == right.get(key) for key in ("account_id", "portfolio_session_id", "asset", "owner_type", "owner_id", "status"))


def _flat_reconciled(allocation: AssetAllocationSlice, execution: ExecutionSlice | None) -> bool:
    if execution is None:
        return False
    status = str(execution.reconciliation.get("status", "")).strip().lower()
    if status not in {"flat", "reconciled", "closed"}:
        return False
    return not any(_non_zero_row(row) or _active_order(row) for row in execution.orders)


def _unknown_non_zero(allocation: AssetAllocationSlice, execution: ExecutionSlice | None) -> bool:
    if allocation.effective_quantity == 0:
        return False
    if execution is None:
        return True
    status = str(execution.reconciliation.get("status", "unknown")).strip().lower()
    return status in {"unknown", "ambiguous", "incomplete", "stale"}


def _non_zero_row(row: Mapping[str, Any]) -> bool:
    for key in ("quantity", "position_qty", "notional", "market_value", "exposure"):
        if key not in row or row[key] is None:
            continue
        try:
            value = Decimal(str(row[key]))
        except (InvalidOperation, ValueError):
            continue
        if value.is_finite() and value != 0:
            return True
    return _active_order(row)


def _active_order(row: Mapping[str, Any]) -> bool:
    return str(row.get("status", "")).strip().lower() in {"open", "working", "new", "pending", "partially_filled"}


def _risk_hold(
    snapshot: PortfolioSnapshot,
    policy: PortfolioPolicy,
    unknown_assets: list[str],
    identity_conflict: bool,
    cross_margin_unknown: bool,
    *,
    reason_override: str | None = None,
) -> PortfolioRiskHold:
    reason = reason_override or ("ownership_identity_conflict" if identity_conflict else "unknown_shared_account_risk")
    return PortfolioRiskHold(
        hold_id=_stable_id(
            "hold",
            {
                "snapshot_id": snapshot.snapshot_id,
                "policy_id": policy.policy_id,
                "policy_revision": policy.policy_revision,
                "reason": reason,
                "assets": unknown_assets,
            },
        ),
        portfolio_session_id=snapshot.portfolio_session_id,
        candidate_set_id=f"ownership-{snapshot.snapshot_id}",
        policy_id=policy.policy_id,
        policy_revision=policy.policy_revision,
        snapshot_id=snapshot.snapshot_id,
        created_at=snapshot.observed_at,
        reason_code=reason,
        message=(
            "ownership identity conflicts across slices"
            if reason == "ownership_identity_conflict"
            else (
                "shared-account or cross-margin exposure cannot be proven independent"
                if reason == "unknown_shared_account_risk"
                else reason.replace("_", " ")
            )
        ),
        affected_assets=tuple(unknown_assets),
        decision_provenance={
            "registry_schema": PORTFOLIO_OWNERSHIP_REGISTRY_SCHEMA,
            "cross_margin_unknown": cross_margin_unknown,
            "identity_conflict": identity_conflict,
        },
    )


def _assessment(
    snapshot: PortfolioSnapshot,
    policy: PortfolioPolicy,
    *,
    status: str,
    owned: set[str] | tuple[str, ...] = (),
    blocked: set[str] | tuple[str, ...] = (),
    frozen: set[str] | tuple[str, ...] = (),
    removable: set[str] | tuple[str, ...] = (),
    adoption_required: set[str] | tuple[str, ...] = (),
    evidence: tuple[PortfolioOwnershipRecord, ...] | list[PortfolioOwnershipRecord] = (),
    hold: PortfolioRiskHold | None = None,
    blocks_new: bool = True,
    provenance: Mapping[str, Any] | None = None,
) -> PortfolioOwnershipAssessment:
    rendered_provenance = dict(provenance or {})
    rendered_provenance.update(
        {
            "registry_schema": PORTFOLIO_OWNERSHIP_REGISTRY_SCHEMA,
            "snapshot_id": snapshot.snapshot_id,
            "policy_id": policy.policy_id,
            "policy_revision": policy.policy_revision,
        }
    )
    payload = {
        "provenance": rendered_provenance,
        "owned": sorted(owned),
        "blocked": sorted(blocked),
        "frozen": sorted(frozen),
        "removable": sorted(removable),
        "adoption_required": sorted(adoption_required),
        "evidence": [item.to_dict() for item in evidence],
        "hold": None if hold is None else hold.to_dict(),
    }
    return PortfolioOwnershipAssessment(
        assessment_id=_stable_id("assessment", payload),
        portfolio_session_id=snapshot.portfolio_session_id,
        snapshot_id=snapshot.snapshot_id,
        status=status,
        owned_assets=tuple(owned),
        blocked_assets=tuple(blocked),
        frozen_assets=tuple(frozen),
        removable_assets=tuple(removable),
        adoption_required_assets=tuple(adoption_required),
        evidence=tuple(evidence),
        portfolio_risk_hold=hold,
        blocks_new_allocations=blocks_new,
        provenance=rendered_provenance,
    )


def _require_type(value: Any, expected: type[Any], label: str) -> None:
    if not isinstance(value, expected):
        raise TypeError(f"{label} must be {expected.__name__}")


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")
    return value.strip()


def _stable_id(prefix: str, *parts: Mapping[str, Any]) -> str:
    payload = json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return f"{prefix}-{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


__all__ = ["PORTFOLIO_OWNERSHIP_REGISTRY_SCHEMA", "PortfolioOwnershipRegistry"]
