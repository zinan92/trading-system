import json
from dataclasses import replace

import pytest

from schemas.portfolio import (
    AssetAllocationSlice,
    PortfolioPolicy,
    PortfolioRebalanceDecision,
    PortfolioSelection,
    PortfolioSnapshot,
    StrategyPositionPlan,
)
from services.portfolio_rebalance import PortfolioRebalanceRegistry
from services.trading_system_read_model import project_portfolio_read_model


def _plan(asset: str, candidate_id: str, rank: int) -> StrategyPositionPlan:
    return StrategyPositionPlan(
        strategy_session_id="strategy-session-1",
        strategy_revision_id="dca-revision-1",
        candidate_id=candidate_id,
        candidate_rank=rank,
        asset=asset,
        direction="long",
        requested_quantity="1",
        requested_notional="200",
        position_action="add",
        position_management={"mode": "dca"},
        protection_intent={"stop": "strategy-owned"},
    )


def _allocation(asset: str, candidate_id: str, rank: int, *, quantity: str = "1", notional: str = "200", status: str = "accepted", reasons=()):
    plan = _plan(asset, candidate_id, rank)
    return AssetAllocationSlice(
        portfolio_session_id="portfolio-session-1",
        allocation_id=f"allocation-{asset.lower()}",
        candidate_id=candidate_id,
        candidate_rank=rank,
        asset=asset,
        direction="long",
        requested_quantity="1",
        effective_quantity=quantity,
        source_strategy_plan_digest=plan.digest,
        position_action=plan.position_action,
        position_management=plan.position_management,
        protection_intent=plan.protection_intent,
        requested_notional="200",
        effective_notional=notional,
        status=status,
        reasons=reasons,
        ownership={
            "status": "owned",
            "account_id": "account-1",
            "portfolio_session_id": "portfolio-session-1",
            "asset": asset,
            "owner_type": "strategy",
            "owner_id": "strategy-session-1",
            "strategy_session_id": "strategy-session-1",
            "strategy_revision_id": "dca-revision-1",
        },
    )


def _selection(selection_id: str, allocations: tuple[AssetAllocationSlice, ...]) -> PortfolioSelection:
    return PortfolioSelection(
        selection_id=selection_id,
        portfolio_session_id="portfolio-session-1",
        candidate_set_id=f"candidate-set-{selection_id}",
        policy_id="portfolio-policy-1",
        policy_revision="2026-08-25-a",
        snapshot_id="snapshot-1",
        created_at="2026-08-25T04:00:00+00:00",
        selected_allocations=allocations,
        decision_provenance={"outcome": "SELECT_CANDIDATES"},
    )


def _decision(old: PortfolioSelection, new: PortfolioSelection, reduction, addition) -> PortfolioRebalanceDecision:
    return PortfolioRebalanceDecision(
        decision_id="rebalance-1",
        portfolio_session_id="portfolio-session-1",
        old_selection=old,
        new_selection=new,
        policy_id="portfolio-policy-1",
        policy_revision="2026-08-25-a",
        requested_reductions=(reduction,),
        requested_additions=(addition,),
        reasons=("explicit_candidate_replacement",),
        decision_provenance={"source": "portfolio-coordinator", "operator": "park"},
        state="confirmed",
    )


def test_rebalance_records_before_after_and_subtractive_effective_sizes():
    old = _allocation("BTC", "candidate-btc", 1)
    new = _allocation("ETH", "candidate-eth", 1)
    old_selection = _selection("selection-old", (old,))
    new_selection = _selection("selection-new", (new,))
    reduction = replace(old, effective_quantity="0", effective_notional="0", status="scaled", reasons=("rebalance_exit",))
    addition = replace(new, effective_quantity="1", effective_notional="200")
    decision = _decision(old_selection, new_selection, reduction, addition)

    payload = decision.to_dict()
    assert payload["old_selection"]["selection_id"] == "selection-old"
    assert payload["new_selection"]["selection_id"] == "selection-new"
    assert payload["requested_reductions"][0]["effective_quantity"] == "0"
    assert payload["requested_additions"][0]["effective_quantity"] == "1"
    assert decision.policy_revision == "2026-08-25-a"
    assert json.loads(decision.to_json())["state"] == "confirmed"


def test_rebalance_validation_is_stale_and_duplicate_safe():
    old = _allocation("BTC", "candidate-btc", 1)
    new = _allocation("ETH", "candidate-eth", 1)
    decision = _decision(
        _selection("selection-old", (old,)),
        _selection("selection-new", (new,)),
        replace(old, effective_quantity="0", effective_notional="0", status="scaled", reasons=("exit",)),
        new,
    )
    registry = PortfolioRebalanceRegistry()
    policy = PortfolioPolicy(policy_id="portfolio-policy-1", policy_revision="2026-08-25-a")
    assert registry.validate(decision, current_selection=decision.old_selection, current_policy=policy) == decision
    assert registry.replay_key(decision) == decision.digest
    with pytest.raises(ValueError, match="stale"):
        registry.validate(decision, current_selection=decision.new_selection, current_policy=policy)
    with pytest.raises(ValueError, match="duplicate"):
        registry.validate(decision, current_selection=decision.old_selection, current_policy=policy, seen_decision_ids=("rebalance-1",))


def test_rebalance_rejects_semantic_rewrite_or_upsize_and_leaves_old_selection_unchanged():
    old = _allocation("BTC", "candidate-btc", 1)
    new = _allocation("ETH", "candidate-eth", 1)
    old_selection = _selection("selection-old", (old,))
    new_selection = _selection("selection-new", (new,))
    rewritten = replace(old, position_action="reduce", effective_quantity="0", effective_notional="0", status="scaled", reasons=("exit",))
    with pytest.raises(ValueError, match="Strategy semantics"):
        _decision(old_selection, new_selection, rewritten, new)

    with pytest.raises(ValueError, match="cannot increase quantity"):
        _decision(
            old_selection,
            new_selection,
            replace(
                old,
                requested_quantity="2",
                effective_quantity="2",
                requested_notional="400",
                effective_notional="400",
            ),
            new,
        )
    assert old_selection.selected_allocations[0].effective_quantity == old.effective_quantity


def test_rebalance_is_readable_without_creating_execution_requests():
    old = _allocation("BTC", "candidate-btc", 1)
    new = _allocation("ETH", "candidate-eth", 1)
    decision = _decision(
        _selection("selection-old", (old,)),
        _selection("selection-new", (new,)),
        replace(old, effective_quantity="0", effective_notional="0", status="scaled", reasons=("exit",)),
        new,
    )
    snapshot = PortfolioSnapshot(
        snapshot_id="snapshot-1",
        portfolio_session_id="portfolio-session-1",
        account_id="account-1",
        observed_at="2026-08-25T04:00:00+00:00",
        equity="1000",
        available_cash="800",
        provenance={"cursor": "cursor-1", "source": "fixture"},
    )
    view = project_portfolio_read_model(decision.new_selection, snapshot, decision)
    assert view["rebalance"]["decision_id"] == "rebalance-1"
    assert view["rebalance"]["old_selection_id"] == "selection-old"
    assert view["rebalance"]["new_selection_id"] == "selection-new"
    assert view["rebalance"]["read_only"] is True
    assert "execution_request" not in view["rebalance"]
