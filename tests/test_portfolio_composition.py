import json
from decimal import Decimal

import pytest

from schemas.portfolio import (
    PortfolioPolicy,
    PortfolioRiskHold,
    PortfolioSelection,
    PortfolioSnapshot,
    StrategyCandidateSet,
)
from services.portfolio_composition import (
    compose_portfolio_read_only,
    prepare_btc_candidate,
)


BTC_FACTS = {
    "venue": "hyperliquid",
    "environment": "testnet",
    "base_asset": "BTC",
    "quote_asset": "USD",
    "instrument_id": "BTC-USD-PERP",
    "contract_type": "perpetual",
    "contract_multiplier": "1",
    "mark_price": "80000",
    "oracle_price": "79900",
    "quantity_increment": "0.0001",
    "min_quantity": "0.0001",
    "min_notional": "10",
    "api_secret": "must-not-enter-provenance",
}


def _candidate_set():
    candidate = prepare_btc_candidate(
        BTC_FACTS,
        strategy_notional="240",
        strategy_session_id="strategy-session-1",
        strategy_revision_id="dca-revision-1",
        candidate_id="candidate-btc",
        candidate_rank=1,
        position_management={"mode": "dca"},
        protection_intent={"stop": "strategy-owned"},
    )
    return StrategyCandidateSet(
        candidate_set_id="candidate-set-btc",
        strategy_session_id="strategy-session-1",
        strategy_revision_id="dca-revision-1",
        created_at="2026-08-25T04:00:00+00:00",
        candidates=(candidate,),
    )


def _snapshot(*, allocations=(), executions=(), cross_margin_unknown=False):
    return PortfolioSnapshot(
        snapshot_id="snapshot-btc",
        portfolio_session_id="portfolio-session-1",
        account_id="account-1",
        observed_at="2026-08-25T04:00:00+00:00",
        equity="1000",
        available_cash="800",
        total_exposure="0",
        margin_used="0",
        leverage="0",
        allocation_slices=allocations,
        execution_slices=executions,
        provenance={
            "cursor": "cursor-1",
            "source": "fixture",
            "cross_margin_unknown": cross_margin_unknown,
        },
    )


def _policy():
    return PortfolioPolicy(
        policy_id="portfolio-policy-1",
        policy_revision="2026-08-25-a",
        max_single_asset_aum_pct="30",
        max_active_assets=10,
    )


def test_btc_candidate_recalculates_quantity_from_strategy_notional_and_is_paper_only():
    candidate_set = _candidate_set()
    plan = candidate_set.candidates[0]
    assert plan.asset == "BTC"
    assert plan.requested_notional == Decimal("240")
    assert plan.requested_quantity == Decimal("0.003")
    assert plan.provenance["instrument_id"] == "BTC-USD-PERP"
    assert plan.provenance["prepared_read_only"] is True
    assert "api_secret" not in plan.provenance

    with pytest.raises(ValueError, match="base_asset=BTC"):
        prepare_btc_candidate(
            {**BTC_FACTS, "base_asset": "PAXG"},
            strategy_notional="240",
            strategy_session_id="strategy-session-1",
            strategy_revision_id="dca-revision-1",
            candidate_id="candidate-paxg",
            candidate_rank=1,
        )


def test_composition_gate_precedes_execution_and_replays_selection_read_only():
    result = compose_portfolio_read_only(_candidate_set(), _snapshot(), _policy())

    assert isinstance(result.gate_result, PortfolioSelection)
    assert len(result.effective_allocations) == 1
    assert result.effective_allocations[0].asset == "BTC"
    assert result.execution_requests == ()
    assert result.read_model["summary"]["selection_id"] == result.gate_result.selection_id
    assert result.read_model["summary"]["policy_revision"] == "2026-08-25-a"
    replay = json.loads(json.dumps(result.to_dict(), allow_nan=False))
    assert replay["gate_result"]["selection_id"] == result.gate_result.selection_id
    assert replay["execution_requests"] == []


def test_composition_replaces_gate_result_with_ownership_hold_and_emits_no_effective_plan():
    from schemas.portfolio import AssetAllocationSlice

    unknown = AssetAllocationSlice(
        portfolio_session_id="portfolio-session-1",
        allocation_id="allocation-existing",
        candidate_id="candidate-existing",
        candidate_rank=1,
        asset="ETH",
        direction="long",
        requested_quantity="1",
        effective_quantity="1",
        source_strategy_plan_digest="sha256:existing",
        position_action="add",
        requested_notional="200",
        effective_notional="200",
        ownership={},
    )
    result = compose_portfolio_read_only(
        _candidate_set(),
        _snapshot(allocations=(unknown,), cross_margin_unknown=True),
        _policy(),
    )

    assert isinstance(result.gate_result, PortfolioRiskHold)
    assert result.gate_result.reason_code == "unknown_shared_account_risk"
    assert result.effective_allocations == ()
    assert result.execution_requests == ()
    assert result.read_model["status"] == "held"


def test_composition_requires_real_current_selection_for_rebalance_validation():
    from tests.test_portfolio_rebalance import _allocation, _decision, _selection

    old = _allocation("ETH", "candidate-old", 1)
    new = _allocation("SOL", "candidate-new", 1)
    old_selection = _selection("selection-old", (old,))
    new_selection = _selection("selection-new", (new,))
    decision = _decision(
        old_selection,
        new_selection,
        old,
        new,
    )
    with pytest.raises(ValueError, match="current selection"):
        compose_portfolio_read_only(
            _candidate_set(),
            _snapshot(),
            _policy(),
            rebalance_decision=decision,
        )
    with pytest.raises(ValueError, match="stale"):
        compose_portfolio_read_only(
            _candidate_set(),
            _snapshot(),
            _policy(),
            rebalance_decision=decision,
            current_selection=new_selection,
        )
    with pytest.raises(ValueError, match="duplicate"):
        compose_portfolio_read_only(
            _candidate_set(),
            _snapshot(),
            _policy(),
            rebalance_decision=decision,
            current_selection=old_selection,
            seen_decision_ids=(decision.decision_id,),
        )
