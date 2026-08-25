import json
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from types import MappingProxyType

import pytest

from schemas.portfolio import (
    AssetAllocationSlice,
    ExecutionSlice,
    PortfolioPolicy,
    PortfolioRiskHold,
    PortfolioSelection,
    PortfolioSession,
    PortfolioSnapshot,
    StrategyCandidateSet,
    StrategyPositionPlan,
)


def _plan(
    *,
    candidate_id: str = "candidate-btc",
    rank: int = 1,
    session_id: str = "strategy-session-1",
    quantity: str = "0.003",
):
    return StrategyPositionPlan(
        strategy_session_id=session_id,
        strategy_revision_id="dca-revision-1",
        candidate_id=candidate_id,
        candidate_rank=rank,
        asset="BTC",
        direction="long",
        requested_quantity=quantity,
        requested_notional="240.00",
        position_action="add",
        position_management={"add_on": "next_entry", "exit": "strategy-owned"},
        protection_intent={"stop": "strategy-owned", "take_profit": "strategy-owned"},
        provenance={"source": "strategy-test"},
    )


def _session() -> PortfolioSession:
    return PortfolioSession(
        portfolio_session_id="portfolio-session-1",
        strategy_session_id="strategy-session-1",
        account_id="account-testnet-1",
        session_revision_id="portfolio-revision-1",
        opened_at="2026-08-25T04:00:00+00:00",
        account_scope={"account_id": "account-testnet-1"},
        provenance={"source": "test"},
    )


def _policy() -> PortfolioPolicy:
    return PortfolioPolicy(
        policy_id="portfolio-policy-1",
        policy_revision="2026-08-25-a",
        provenance={"source": "test"},
    )


def _snapshot(session: PortfolioSession) -> PortfolioSnapshot:
    plan = _plan()
    allocation = AssetAllocationSlice(
        portfolio_session_id=session.portfolio_session_id,
        allocation_id="allocation-btc",
        candidate_id="candidate-btc",
        asset="BTC",
        direction="long",
        requested_quantity="0.003",
        effective_quantity="0.003",
        source_strategy_plan_digest=plan.digest,
        position_action=plan.position_action,
        position_management=plan.position_management,
        protection_intent=plan.protection_intent,
        status="accepted",
        execution_slice_id="execution-btc",
        reasons=(),
        provenance={"source": "test"},
    )
    execution = ExecutionSlice(
        execution_slice_id="execution-btc",
        portfolio_session_id=session.portfolio_session_id,
        allocation_id=allocation.allocation_id,
        asset="BTC",
        broker_binding={"broker_id": "fixture", "environment": "paper"},
        orders=(),
        position={"quantity": "0.003"},
        protection={"status": "not-submitted"},
        fills=(),
        reconciliation={"status": "coherent"},
        status="coherent",
    )
    return PortfolioSnapshot(
        snapshot_id="snapshot-1",
        portfolio_session_id=session.portfolio_session_id,
        account_id=session.account_id,
        observed_at="2026-08-25T04:01:00+00:00",
        equity="1000.00",
        available_cash="760.00",
        positions=(
            {
                "asset": "BTC",
                "quantity": "0.003",
                "portfolio_session_id": session.portfolio_session_id,
                "account_id": session.account_id,
            },
        ),
        open_orders=(),
        allocation_slices=(allocation,),
        execution_slices=(execution,),
        ownership_facts=(
            {
                "asset": "BTC",
                "portfolio_session_id": session.portfolio_session_id,
                "account_id": session.account_id,
                "owner_type": "strategy",
                "owner_id": "strategy-session-1",
            },
        ),
        coherent=True,
        fresh=True,
        provenance={"source": "fixture"},
    )


def test_strategy_plan_freezes_strategy_semantics_and_is_digestable():
    management = {"add_on": "next_entry"}
    plan = StrategyPositionPlan(
        strategy_session_id="strategy-session-1",
        strategy_revision_id="dca-revision-1",
        candidate_id="candidate-btc",
        candidate_rank=1,
        asset="BTC",
        direction="long",
        requested_quantity=Decimal("0.003"),
        position_action="add",
        position_management=management,
        protection_intent={"stop": "strategy-owned"},
    )
    management["add_on"] = "mutated-after-build"

    assert plan.requested_quantity == Decimal("0.003")
    assert plan.to_dict()["position_management"] == {"add_on": "next_entry"}
    assert isinstance(plan.position_management, MappingProxyType)
    assert plan.digest.startswith("sha256:")
    assert json.loads(plan.to_json())["requested_quantity"] == "0.003"
    with pytest.raises(TypeError):
        plan.position_management["new"] = "forbidden"
    with pytest.raises(FrozenInstanceError):
        plan.asset = "ETH"


def test_strategy_plan_rejects_non_finite_or_contradictory_size():
    with pytest.raises(ValueError, match="finite"):
        _plan(quantity="nan")
    with pytest.raises(ValueError, match="finite"):
        _plan(quantity="inf")
    with pytest.raises(ValueError, match="non-negative"):
        _plan(quantity="-0.001")
    with pytest.raises(ValueError, match="flat"):
        StrategyPositionPlan(
            strategy_session_id="strategy-session-1",
            strategy_revision_id="dca-revision-1",
            candidate_id="candidate-flat",
            candidate_rank=1,
            asset="BTC",
            direction="flat",
            requested_quantity="0.001",
            position_action="exit",
        )


def test_candidate_set_orders_ranks_and_rejects_identity_mismatch():
    candidate_set = StrategyCandidateSet(
        candidate_set_id="candidate-set-1",
        strategy_session_id="strategy-session-1",
        strategy_revision_id="dca-revision-1",
        created_at="2026-08-25T04:00:00+00:00",
        candidates=(_plan(candidate_id="candidate-eth", rank=2, quantity="0.10"), _plan()),
        provenance={"source": "strategy"},
    )

    assert [item.candidate_id for item in candidate_set.candidates] == [
        "candidate-btc",
        "candidate-eth",
    ]
    assert [item.candidate_rank for item in candidate_set.candidates] == [1, 2]
    assert candidate_set.digest == StrategyCandidateSet(
        candidate_set_id="candidate-set-1",
        strategy_session_id="strategy-session-1",
        strategy_revision_id="dca-revision-1",
        created_at="2026-08-25T04:00:00+00:00",
        candidates=tuple(reversed(candidate_set.candidates)),
        provenance={"source": "strategy"},
    ).digest
    with pytest.raises(ValueError, match="strategy session"):
        StrategyCandidateSet(
            candidate_set_id="candidate-set-2",
            strategy_session_id="strategy-session-other",
            strategy_revision_id="dca-revision-1",
            created_at="2026-08-25T04:00:00+00:00",
            candidates=(_plan(),),
        )


def test_portfolio_policy_exposes_confirmed_limits_and_rejects_bad_limits():
    policy = _policy()
    assert policy.max_single_asset_aum_pct == Decimal("30")
    assert policy.max_active_assets == 10
    assert json.loads(policy.to_json())["max_single_asset_aum_pct"] == "30"

    with pytest.raises(ValueError, match="max_single_asset_aum_pct"):
        PortfolioPolicy(
            policy_id="bad-policy",
            policy_revision="1",
            max_single_asset_aum_pct="101",
        )
    with pytest.raises(ValueError, match="max_active_assets"):
        PortfolioPolicy(policy_id="bad-policy", policy_revision="1", max_active_assets=0)
    with pytest.raises(ValueError, match="finite"):
        PortfolioPolicy(policy_id="bad-policy", policy_revision="1", max_margin_pct="nan")


def test_snapshot_rejects_conflicting_ownership_facts_before_evaluation():
    session = _session()
    good = _snapshot(session)
    assert good.digest.startswith("sha256:")
    assert json.loads(good.to_json())["snapshot_id"] == "snapshot-1"

    with pytest.raises(ValueError, match="ownership"):
        PortfolioSnapshot(
            snapshot_id="snapshot-conflict",
            portfolio_session_id=session.portfolio_session_id,
            account_id=session.account_id,
            observed_at="2026-08-25T04:01:00+00:00",
            equity="1000",
            available_cash="760",
            ownership_facts=(
                {
                    "asset": "BTC",
                    "portfolio_session_id": session.portfolio_session_id,
                    "account_id": session.account_id,
                    "owner_type": "strategy",
                    "owner_id": "owner-a",
                },
                {
                    "asset": "BTC",
                    "portfolio_session_id": session.portfolio_session_id,
                    "account_id": session.account_id,
                    "owner_type": "strategy",
                    "owner_id": "owner-b",
                },
            ),
        )

    with pytest.raises(ValueError, match="portfolio session"):
        PortfolioSnapshot(
            snapshot_id="snapshot-orphan",
            portfolio_session_id=session.portfolio_session_id,
            account_id=session.account_id,
            observed_at="2026-08-25T04:01:00+00:00",
            equity="1000",
            available_cash="760",
            allocation_slices=(
                AssetAllocationSlice(
                    portfolio_session_id="other-portfolio",
                    allocation_id="allocation-btc",
                    candidate_id="candidate-btc",
                    asset="BTC",
                    direction="long",
                    requested_quantity="0.003",
                    effective_quantity="0.003",
                    source_strategy_plan_digest=_plan().digest,
                    position_action="add",
                ),
            ),
        )


def test_snapshot_requires_complete_identity_and_rejects_scope_or_timestamp_conflicts():
    session = _session()
    with pytest.raises(ValueError, match="portfolio session ownership identity"):
        PortfolioSnapshot(
            snapshot_id="snapshot-missing-scope",
            portfolio_session_id=session.portfolio_session_id,
            account_id=session.account_id,
            observed_at="2026-08-25T04:01:00+00:00",
            equity="1000",
            available_cash="760",
            positions=({"asset": "BTC", "account_id": session.account_id},),
        )
    with pytest.raises(ValueError, match="owner type"):
        PortfolioSnapshot(
            snapshot_id="snapshot-missing-owner",
            portfolio_session_id=session.portfolio_session_id,
            account_id=session.account_id,
            observed_at="2026-08-25T04:01:00+00:00",
            equity="1000",
            available_cash="760",
            ownership_facts=(
                {
                    "asset": "BTC",
                    "portfolio_session_id": session.portfolio_session_id,
                    "account_id": session.account_id,
                    "owner_id": "strategy-session-1",
                },
            ),
        )
    with pytest.raises(ValueError, match="timezone"):
        PortfolioSnapshot(
            snapshot_id="snapshot-naive-time",
            portfolio_session_id=session.portfolio_session_id,
            account_id=session.account_id,
            observed_at="2026-08-25T04:01:00",
            equity="1000",
            available_cash="760",
        )
    with pytest.raises(ValueError, match="account scope"):
        PortfolioSession(
            portfolio_session_id=session.portfolio_session_id,
            strategy_session_id=session.strategy_session_id,
            account_id=session.account_id,
            session_revision_id=session.session_revision_id,
            opened_at="2026-08-25T04:00:00+00:00",
            account_scope={"account_id": "other-account"},
        )


def test_snapshot_canonicalizes_unordered_facts_and_rejects_cross_asset_or_duplicate_links():
    session = _session()
    plan = _plan()
    allocation = AssetAllocationSlice(
        portfolio_session_id=session.portfolio_session_id,
        allocation_id="allocation-btc",
        candidate_id="candidate-btc",
        asset="BTC",
        direction="long",
        requested_quantity="0.003",
        effective_quantity="0.003",
        source_strategy_plan_digest=plan.digest,
        position_action="add",
        execution_slice_id="execution-btc",
    )
    execution = ExecutionSlice(
        execution_slice_id="execution-btc",
        portfolio_session_id=session.portfolio_session_id,
        allocation_id=allocation.allocation_id,
        asset="BTC",
        broker_binding={"broker_id": "fixture", "environment": "paper"},
        orders=(
            {"client_order_id": "order-b", "status": "pending"},
            {"client_order_id": "order-a", "status": "pending"},
        ),
        fills=(),
    )
    snapshot_kwargs = {
        "portfolio_session_id": session.portfolio_session_id,
        "account_id": session.account_id,
        "observed_at": "2026-08-25T04:01:00+00:00",
        "equity": "1000",
        "available_cash": "760",
        "allocation_slices": (allocation,),
        "execution_slices": (execution,),
    }
    first = PortfolioSnapshot(
        snapshot_id="snapshot-order-a",
        positions=(
            {"asset": "ETH", "quantity": "0.1", "portfolio_session_id": session.portfolio_session_id, "account_id": session.account_id},
            {"asset": "BTC", "quantity": "0.003", "portfolio_session_id": session.portfolio_session_id, "account_id": session.account_id},
        ),
        **snapshot_kwargs,
    )
    second = PortfolioSnapshot(
        snapshot_id="snapshot-order-a",
        positions=tuple(reversed(first.positions)),
        **{key: value for key, value in snapshot_kwargs.items() if key != "positions"},
    )
    assert first.digest == second.digest
    assert execution.digest == replace(execution, orders=tuple(reversed(execution.orders))).digest

    mismatched_execution = replace(execution, asset="ETH")
    with pytest.raises(ValueError, match="asset identity"):
        PortfolioSnapshot(
            snapshot_id="snapshot-asset-conflict",
            positions=(),
            allocation_slices=(allocation,),
            execution_slices=(mismatched_execution,),
            **{key: value for key, value in snapshot_kwargs.items() if key not in {"allocation_slices", "execution_slices"}},
        )
    with pytest.raises(ValueError, match="allocation ids"):
        PortfolioSnapshot(
            snapshot_id="snapshot-duplicate-allocation",
            positions=(),
            allocation_slices=(allocation, allocation),
            execution_slices=(),
            **{key: value for key, value in snapshot_kwargs.items() if key not in {"allocation_slices", "execution_slices"}},
        )


def test_selection_preserves_requested_and_effective_size_without_upsizing():
    session = _session()
    selection = PortfolioSelection(
        selection_id="selection-1",
        portfolio_session_id=session.portfolio_session_id,
        candidate_set_id="candidate-set-1",
        policy_id="portfolio-policy-1",
        policy_revision="2026-08-25-a",
        snapshot_id="snapshot-1",
        created_at="2026-08-25T04:02:00+00:00",
        selected_allocations=(
            AssetAllocationSlice(
                portfolio_session_id=session.portfolio_session_id,
                allocation_id="allocation-btc",
                candidate_id="candidate-btc",
                asset="BTC",
                direction="long",
                requested_quantity="0.006",
                effective_quantity="0.003",
                source_strategy_plan_digest=_plan().digest,
                position_action="add",
                position_management={"add_on": "next_entry"},
                protection_intent={"stop": "strategy-owned"},
                status="scaled",
                reasons=("single_asset_cap",),
            ),
        ),
        rejected_candidates=({"candidate_id": "candidate-eth", "reason": "capacity"},),
        decision_provenance={"evaluator": "portfolio-gate-v1", "source_snapshot": "snapshot-1"},
    )

    assert selection.selected_allocations[0].requested_quantity == Decimal("0.006")
    assert selection.selected_allocations[0].effective_quantity == Decimal("0.003")
    assert selection.to_dict()["selected_allocations"][0]["requested_quantity"] == "0.006"
    assert selection.to_dict()["selected_allocations"][0]["effective_quantity"] == "0.003"
    assert selection.to_dict()["selected_allocations"][0]["source_strategy_plan_digest"].startswith("sha256:")
    assert selection.to_dict()["selected_allocations"][0]["position_action"] == "add"
    assert selection.to_dict()["selected_allocations"][0]["protection_intent"] == {"stop": "strategy-owned"}
    assert selection.digest != replace(selection, policy_revision="2026-08-26-b").digest
    assert selection.digest == PortfolioSelection(
        selection_id="selection-1",
        portfolio_session_id=session.portfolio_session_id,
        candidate_set_id="candidate-set-1",
        policy_id="portfolio-policy-1",
        policy_revision="2026-08-25-a",
        snapshot_id="snapshot-1",
        created_at="2026-08-25T04:02:00+00:00",
        selected_allocations=tuple(reversed(selection.selected_allocations)),
        rejected_candidates=({"reason": "capacity", "candidate_id": "candidate-eth"},),
        decision_provenance={"source_snapshot": "snapshot-1", "evaluator": "portfolio-gate-v1"},
    ).digest

    with pytest.raises(ValueError, match="effective quantity"):
        AssetAllocationSlice(
            portfolio_session_id=session.portfolio_session_id,
            allocation_id="allocation-too-large",
            candidate_id="candidate-btc",
            asset="BTC",
            direction="long",
            requested_quantity="0.003",
            effective_quantity="0.004",
            source_strategy_plan_digest=_plan().digest,
            position_action="add",
        )
    with pytest.raises(ValueError, match="both select and reject"):
        PortfolioSelection(
            selection_id="selection-overlap",
            portfolio_session_id=session.portfolio_session_id,
            candidate_set_id="candidate-set-1",
            policy_id="portfolio-policy-1",
            policy_revision="2026-08-25-a",
            snapshot_id="snapshot-1",
            created_at="2026-08-25T04:02:00+00:00",
            selected_allocations=selection.selected_allocations,
            rejected_candidates=({"candidate_id": "candidate-btc", "reason": "also-rejected"},),
        )


def test_execution_slice_and_hold_are_immutable_serializable_and_provenance_rich():
    session = _session()
    execution = ExecutionSlice(
        execution_slice_id="execution-btc",
        portfolio_session_id=session.portfolio_session_id,
        allocation_id="allocation-btc",
        asset="BTC",
        broker_binding={"environment": "paper", "broker_id": "fixture"},
        orders=({"client_order_id": "order-1", "status": "pending"},),
        position={"quantity": "0"},
        protection={"intent": "strategy-owned"},
        fills=(),
        reconciliation={"status": "unknown"},
        status="pending",
    )
    hold = PortfolioRiskHold(
        hold_id="hold-1",
        portfolio_session_id=session.portfolio_session_id,
        candidate_set_id="candidate-set-1",
        policy_id="portfolio-policy-1",
        policy_revision="2026-08-25-a",
        snapshot_id="snapshot-unknown",
        created_at="2026-08-25T04:03:00+00:00",
        reason_code="unknown_non_zero_exposure",
        message="account exposure cannot be proven independent",
        affected_assets=("BTC",),
        decision_provenance={"source": "reconciliation", "execution_slice": execution.execution_slice_id},
    )

    assert isinstance(execution.orders, tuple)
    assert json.loads(execution.to_json())["broker_binding"]["environment"] == "paper"
    assert hold.blocks_new_entries is True
    assert hold.digest.startswith("sha256:")
    with pytest.raises(FrozenInstanceError):
        hold.message = "changed"
    with pytest.raises(ValueError, match="must block"):
        replace(hold, blocks_new_entries=False)
