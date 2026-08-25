import json
from dataclasses import replace

from schemas.portfolio import (
    AssetAllocationSlice,
    ExecutionSlice,
    PortfolioPolicy,
    PortfolioRiskHold,
    PortfolioSelection,
    PortfolioSnapshot,
    StrategyPositionPlan,
)
from services.trading_system_read_model import (
    project_portfolio_read_model,
    project_trading_system_read_model,
)


def _plan() -> StrategyPositionPlan:
    return StrategyPositionPlan(
        strategy_session_id="strategy-session-1",
        strategy_revision_id="dca-revision-1",
        candidate_id="candidate-btc",
        candidate_rank=1,
        asset="BTC",
        direction="long",
        requested_quantity="0.006",
        requested_notional="480",
        position_action="add",
        position_management={"mode": "dca", "next_entry": "strategy-owned"},
        protection_intent={"stop": "strategy-owned", "take_profit": "strategy-owned"},
    )


def _ownership() -> dict:
    return {
        "status": "owned",
        "account_id": "account-1",
        "portfolio_session_id": "portfolio-session-1",
        "asset": "BTC",
        "owner_type": "strategy",
        "owner_id": "strategy-session-1",
        "strategy_session_id": "strategy-session-1",
        "strategy_revision_id": "dca-revision-1",
    }


def _allocation(*, status: str = "scaled", execution_slice_id: str | None = "execution-btc") -> AssetAllocationSlice:
    plan = _plan()
    return AssetAllocationSlice(
        portfolio_session_id="portfolio-session-1",
        allocation_id="allocation-btc",
        candidate_id=plan.candidate_id,
        candidate_rank=plan.candidate_rank,
        asset="BTC",
        direction="long",
        requested_quantity=plan.requested_quantity,
        effective_quantity="0.003",
        source_strategy_plan_digest=plan.digest,
        position_action=plan.position_action,
        position_management=plan.position_management,
        protection_intent=plan.protection_intent,
        requested_notional=plan.requested_notional,
        effective_notional="240",
        status=status,
        execution_slice_id=execution_slice_id,
        reasons=("single_asset_concentration",),
        ownership=_ownership(),
        provenance={"next_action": "wait_for_fill"},
    )


def _execution() -> ExecutionSlice:
    return ExecutionSlice(
        execution_slice_id="execution-btc",
        portfolio_session_id="portfolio-session-1",
        allocation_id="allocation-btc",
        asset="BTC",
        broker_binding={"broker_id": "fixture", "environment": "paper"},
        orders=({"client_order_id": "order-btc", "status": "working", "api_secret": "must-not-leak"},),
        position={"quantity": "0.003"},
        protection={"status": "attached", "stop": "strategy-owned"},
        fills=({"fill_id": "fill-btc", "quantity": "0.003"},),
        fees={"currency": "USDC", "total": "0.25"},
        reconciliation={"status": "coherent"},
        status="coherent",
        ownership=_ownership(),
    )


def _snapshot(allocation: AssetAllocationSlice, execution: ExecutionSlice | None) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        snapshot_id="snapshot-1",
        portfolio_session_id="portfolio-session-1",
        account_id="account-1",
        observed_at="2026-08-25T04:00:00+00:00",
        equity="1000",
        available_cash="760",
        total_exposure="240",
        margin_used="240",
        leverage="0.24",
        positions=(),
        allocation_slices=(allocation,),
        execution_slices=() if execution is None else (execution,),
        provenance={"cursor": "cursor-1", "source": "fixture"},
    )


def _selection(allocation: AssetAllocationSlice) -> PortfolioSelection:
    return PortfolioSelection(
        selection_id="selection-1",
        portfolio_session_id="portfolio-session-1",
        candidate_set_id="candidate-set-1",
        policy_id="portfolio-policy-1",
        policy_revision="2026-08-25-a",
        snapshot_id="snapshot-1",
        created_at="2026-08-25T04:00:00+00:00",
        selected_allocations=(allocation,),
        decision_provenance={"outcome": "SCALE_DOWN", "reasons": ["single_asset_concentration"]},
    )


def test_portfolio_read_model_projects_summary_and_complete_asset_slice():
    allocation = _allocation()
    projected = project_portfolio_read_model(_selection(allocation), _snapshot(allocation, _execution()))

    assert projected["present"] is True
    assert projected["status"] == "scaled"
    assert projected["summary"] == {
        "aum": 1000.0,
        "exposure": 240.0,
        "margin": 240.0,
        "asset_count": 1,
        "policy_revision": "2026-08-25-a",
        "selection_id": "selection-1",
        "snapshot_id": "snapshot-1",
        "policy_id": "portfolio-policy-1",
        "status": "scaled",
        "read_only": True,
    }
    asset = projected["allocations"][0]
    assert asset["requested_quantity"] == "0.006"
    assert asset["effective_quantity"] == "0.003"
    assert asset["ownership"]["status"] == "owned"
    assert asset["orders"][0]["client_order_id"] == "order-btc"
    assert "api_secret" not in asset["orders"][0]
    assert asset["position"]["quantity"] == "0.003"
    assert asset["protection"]["status"] == "attached"
    assert asset["fills"][0]["fill_id"] == "fill-btc"
    assert asset["fees"]["total"] == "0.25"
    assert asset["reconciliation"]["status"] == "coherent"
    assert asset["next_action"] == "wait_for_fill"
    json.dumps(projected, allow_nan=False)


def test_read_model_keeps_rejected_held_and_missing_execution_states_distinct():
    allocation = _allocation(execution_slice_id=None)
    snapshot = _snapshot(allocation, None)
    rejected = PortfolioSelection(
        selection_id="selection-rejected",
        portfolio_session_id="portfolio-session-1",
        candidate_set_id="candidate-set-1",
        policy_id="portfolio-policy-1",
        policy_revision="2026-08-25-a",
        snapshot_id="snapshot-1",
        created_at="2026-08-25T04:00:00+00:00",
        rejected_candidates=(
            {
                "candidate_id": "candidate-btc",
                "asset": "BTC",
                "reason": "max_active_assets",
                "requested_quantity": "0.006",
                "source_strategy_plan_digest": "sha256:source",
            },
        ),
        decision_provenance={"outcome": "REJECT"},
    )
    rejected_view = project_portfolio_read_model(rejected, snapshot)
    assert rejected_view["status"] == "rejected"
    assert rejected_view["rejected_candidates"][0]["effective_quantity"] is None
    assert rejected_view["rejected_candidates"][0]["reason"] == "max_active_assets"

    held = PortfolioRiskHold(
        hold_id="hold-1",
        portfolio_session_id="portfolio-session-1",
        candidate_set_id="candidate-set-1",
        policy_id="portfolio-policy-1",
        policy_revision="2026-08-25-a",
        snapshot_id="snapshot-1",
        created_at="2026-08-25T04:00:00+00:00",
        reason_code="unknown_shared_account_risk",
        message="unknown shared account risk",
        affected_assets=("BTC",),
        decision_provenance={"source": "ownership"},
    )
    held_view = project_portfolio_read_model(held, snapshot)
    assert held_view["status"] == "held"
    assert held_view["risk_hold"]["reason_code"] == "unknown_shared_account_risk"
    assert held_view["allocations"][0]["status"] == "held"
    assert held_view["allocations"][0]["orders"] is None
    assert held_view["allocations"][0]["next_action"] == "wait_for_fill"

    flat = replace(
        allocation,
        effective_quantity="0",
        effective_notional="0",
        execution_slice_id="execution-btc",
        reasons=("flat",),
    )
    flat_view = project_portfolio_read_model(
        _selection(flat),
        _snapshot(flat, replace(_execution(), reconciliation={"status": "flat"})),
    )
    assert flat_view["allocations"][0]["status"] == "flat"


def test_existing_single_asset_read_model_has_no_portfolio_key_without_selection():
    model = project_trading_system_read_model(
        {
            "market": {"status": "missing", "fresh": False},
            "production_execution": {},
        }
    ).to_dict()
    assert "portfolio" not in model
