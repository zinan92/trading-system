from dataclasses import replace

import pytest

from schemas.portfolio import (
    AssetAllocationSlice,
    ExecutionSlice,
    PortfolioOwnershipAssessment,
    PortfolioOwnershipRecord,
    PortfolioPolicy,
    PortfolioRiskHold,
    PortfolioSnapshot,
    StrategyPositionPlan,
)
from services.portfolio_ownership import PortfolioOwnershipRegistry


def _plan(asset: str = "BTC", candidate_id: str = "candidate-btc") -> StrategyPositionPlan:
    return StrategyPositionPlan(
        strategy_session_id="strategy-session-1",
        strategy_revision_id="dca-revision-1",
        candidate_id=candidate_id,
        candidate_rank=1,
        asset=asset,
        direction="long",
        requested_quantity="1",
        requested_notional="200",
        position_action="add",
        position_management={"mode": "dca"},
        protection_intent={"stop": "strategy-owned"},
    )


def _ownership(
    *,
    asset: str = "BTC",
    status: str = "owned",
    owner_type: str = "strategy",
    owner_id: str = "strategy-session-1",
    strategy_session_id: str | None = "strategy-session-1",
    strategy_revision_id: str | None = "dca-revision-1",
):
    payload = {
        "status": status,
        "account_id": "account-1",
        "portfolio_session_id": "portfolio-session-1",
        "asset": asset,
        "owner_type": owner_type,
        "owner_id": owner_id,
    }
    if strategy_session_id is not None:
        payload["strategy_session_id"] = strategy_session_id
    if strategy_revision_id is not None:
        payload["strategy_revision_id"] = strategy_revision_id
    return payload


def _allocation(
    *,
    asset: str = "BTC",
    status: str = "accepted",
    quantity: str = "1",
    ownership=None,
    execution_slice_id: str | None = "execution-btc",
):
    plan = _plan(asset=asset)
    return AssetAllocationSlice(
        portfolio_session_id="portfolio-session-1",
        allocation_id=f"allocation-{asset.lower()}",
        candidate_id=plan.candidate_id,
        asset=asset,
        direction="long",
        requested_quantity="1",
        effective_quantity=quantity,
        source_strategy_plan_digest=plan.digest,
        position_action="add",
        requested_notional="200",
        effective_notional="200" if quantity != "0" else "0",
        status=status,
        execution_slice_id=execution_slice_id,
        reasons=("flat",) if quantity == "0" else (),
        ownership={} if ownership is None else ownership,
    )


def _execution(
    *,
    asset: str = "BTC",
    allocation_id: str = "allocation-btc",
    reconciliation_status: str = "coherent",
    ownership=None,
    orders=(),
):
    return ExecutionSlice(
        execution_slice_id="execution-btc",
        portfolio_session_id="portfolio-session-1",
        allocation_id=allocation_id,
        asset=asset,
        broker_binding={"broker_id": "fixture", "environment": "paper"},
        orders=orders,
        reconciliation={"status": reconciliation_status},
        ownership={} if ownership is None else ownership,
    )


def _snapshot(*, allocations=(), executions=(), positions=(), provenance=None, coherent=True, fresh=True):
    return PortfolioSnapshot(
        snapshot_id="snapshot-1",
        portfolio_session_id="portfolio-session-1",
        account_id="account-1",
        observed_at="2026-08-25T04:00:00+00:00",
        equity="1000",
        available_cash="800",
        positions=positions,
        allocation_slices=allocations,
        execution_slices=executions,
        coherent=coherent,
        fresh=fresh,
        provenance={"cursor": "cursor-1", "source": "fixture", **(provenance or {})},
    )


def _policy() -> PortfolioPolicy:
    return PortfolioPolicy(policy_id="portfolio-policy-1", policy_revision="2026-08-25-a")


def test_owned_slice_keeps_account_asset_strategy_and_revision_identity():
    ownership = _ownership()
    allocation = _allocation(ownership=ownership)
    execution = _execution(ownership=ownership)
    assessment = PortfolioOwnershipRegistry().evaluate(
        _snapshot(allocations=(allocation,), executions=(execution,)),
        _policy(),
    )

    assert isinstance(assessment, PortfolioOwnershipAssessment)
    assert assessment.status == "owned"
    assert assessment.owned_assets == ("BTC",)
    assert assessment.blocks_new_allocations is False
    assert assessment.evidence[0].strategy_revision_id == "dca-revision-1"


def test_manual_unowned_exposure_blocks_and_requires_explicit_adoption():
    ownership = _ownership(status="unowned", owner_type="manual", owner_id="manual-wallet", strategy_session_id=None, strategy_revision_id=None)
    allocation = _allocation(ownership=ownership)
    assessment = PortfolioOwnershipRegistry().evaluate(
        _snapshot(allocations=(allocation,), executions=(_execution(ownership=ownership),)),
        _policy(),
    )

    assert assessment.status == "unowned_block"
    assert assessment.blocked_assets == ("BTC",)
    assert assessment.adoption_required_assets == ("BTC",)
    assert assessment.blocks_new_allocations is True

    record = PortfolioOwnershipRegistry().adopt(
        allocation,
        account_id="account-1",
        strategy_session_id="strategy-session-1",
        strategy_revision_id="dca-revision-1",
        adoption_id="adoption-1",
        adopted_at="2026-08-25T04:05:00+00:00",
        reason="Park explicitly adopted the inherited slice",
    )
    assert isinstance(record, PortfolioOwnershipRecord)
    assert record.status == "adopted"
    assert record.adoption_id == "adoption-1"
    assert record.provenance["source_allocation_digest"].startswith("sha256:")

    adopted = replace(allocation, ownership=record.to_dict())
    adopted_assessment = PortfolioOwnershipRegistry().evaluate(
        _snapshot(allocations=(adopted,), executions=(_execution(ownership=record.to_dict()),)),
        _policy(),
    )
    assert adopted_assessment.status == "owned"


def test_flat_causal_reconciled_slice_is_removable_without_freezing_unrelated_assets():
    flat = _allocation(
        ownership=_ownership(status="unowned", owner_type="manual", owner_id="manual-wallet", strategy_session_id=None, strategy_revision_id=None),
        status="scaled",
        quantity="0",
    )
    live = _allocation(asset="ETH", ownership=_ownership(asset="ETH"), execution_slice_id=None)
    assessment = PortfolioOwnershipRegistry().evaluate(
        _snapshot(
            allocations=(flat, live),
            executions=(_execution(reconciliation_status="flat", ownership=flat.ownership),),
        ),
        _policy(),
    )

    assert assessment.removable_assets == ("BTC",)
    assert assessment.owned_assets == ("ETH",)
    assert assessment.frozen_assets == ()
    assert assessment.blocks_new_allocations is False


def test_unknown_non_zero_slice_freezes_only_that_slice_and_cross_margin_escalates_hold():
    unknown = _allocation(ownership={}, execution_slice_id=None)
    assessment = PortfolioOwnershipRegistry().evaluate(
        _snapshot(allocations=(unknown,)),
        _policy(),
    )
    assert assessment.status == "mixed"
    assert assessment.frozen_assets == ("BTC",)
    assert assessment.portfolio_risk_hold is None

    held = PortfolioOwnershipRegistry().evaluate(
        _snapshot(allocations=(unknown,), provenance={"cross_margin_unknown": True}),
        _policy(),
    )
    assert held.status == "unknown_hold"
    assert isinstance(held.portfolio_risk_hold, PortfolioRiskHold)
    assert held.portfolio_risk_hold.reason_code == "unknown_shared_account_risk"


def test_account_wide_manual_position_and_stale_or_identity_conflict_fail_closed():
    manual_position = {
        "asset": "SOL",
        "quantity": "1",
        "portfolio_session_id": "portfolio-session-1",
        "account_id": "account-1",
        "status": "manual",
    }
    assessment = PortfolioOwnershipRegistry().evaluate(
        _snapshot(positions=(manual_position,)),
        _policy(),
    )
    assert assessment.status == "unowned_block"
    assert assessment.blocked_assets == ("SOL",)

    stale = PortfolioOwnershipRegistry().evaluate(
        _snapshot(allocations=(_allocation(ownership=_ownership(), execution_slice_id=None),), fresh=False),
        _policy(),
    )
    assert stale.status == "unknown_hold"
    assert stale.portfolio_risk_hold.reason_code == "snapshot_stale"

    missing_cursor = PortfolioOwnershipRegistry().evaluate(
        _snapshot(allocations=(_allocation(ownership=_ownership(), execution_slice_id=None),), provenance={"cursor": None}),
        _policy(),
    )
    assert missing_cursor.portfolio_risk_hold.reason_code == "missing_account_provenance"

    allocation = _allocation(ownership=_ownership(owner_id="strategy-a"))
    execution = _execution(ownership=_ownership(owner_id="strategy-b"))
    conflict = PortfolioOwnershipRegistry().evaluate(
        _snapshot(allocations=(allocation,), executions=(execution,)),
        _policy(),
    )
    assert conflict.status == "unknown_hold"
    assert conflict.portfolio_risk_hold.reason_code == "ownership_identity_conflict"


def test_registry_rejects_owned_record_without_strategy_revision():
    with pytest.raises(ValueError, match="strategy session and revision"):
        PortfolioOwnershipRecord(
            ownership_id="ownership-1",
            portfolio_session_id="portfolio-session-1",
            account_id="account-1",
            asset="BTC",
            owner_type="strategy",
            owner_id="strategy-session-1",
            status="owned",
        )
