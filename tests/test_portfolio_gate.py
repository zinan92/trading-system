from decimal import Decimal

import pytest

from schemas.portfolio import (
    AssetAllocationSlice,
    PortfolioPolicy,
    PortfolioRiskHold,
    PortfolioSelection,
    PortfolioSnapshot,
    StrategyPositionPlan,
)
from services.portfolio_gate import PortfolioRiskGate


def _plan(
    *,
    candidate_id: str = "candidate-btc",
    asset: str = "BTC",
    quantity: str = "1",
    notional: str | None = "200",
    rank: int = 1,
    action: str = "add",
    direction: str = "long",
):
    return StrategyPositionPlan(
        strategy_session_id="strategy-session-1",
        strategy_revision_id="dca-revision-1",
        candidate_id=candidate_id,
        candidate_rank=rank,
        asset=asset,
        direction=direction,
        requested_quantity=quantity,
        requested_notional=notional,
        position_action=action,
        position_management={"mode": "dca", "step": "strategy-owned"},
        protection_intent={"stop": "strategy-owned", "take_profit": "strategy-owned"},
        provenance={"source": "gate-test"},
    )


def _snapshot(
    *,
    equity: str = "1000",
    available_cash: str = "800",
    total_exposure: str = "0",
    margin_used: str = "0",
    leverage: str = "0",
    loss_pct: str = "0",
    cash_buffer_pct: str | None = None,
    allocations: tuple[AssetAllocationSlice, ...] = (),
    coherent: bool = True,
    fresh: bool = True,
):
    return PortfolioSnapshot(
        snapshot_id="snapshot-1",
        portfolio_session_id="portfolio-session-1",
        account_id="account-1",
        observed_at="2026-08-25T04:00:00+00:00",
        equity=equity,
        available_cash=available_cash,
        total_exposure=total_exposure,
        margin_used=margin_used,
        leverage=leverage,
        loss_pct=loss_pct,
        cash_buffer_pct=cash_buffer_pct,
        allocation_slices=allocations,
        coherent=coherent,
        fresh=fresh,
    )


def _policy(**overrides):
    values = {
        "policy_id": "portfolio-policy-1",
        "policy_revision": "2026-08-25-a",
    }
    values.update(overrides)
    return PortfolioPolicy(**values)


def _allocation(asset: str, index: int) -> AssetAllocationSlice:
    plan = _plan(candidate_id=f"existing-{index}", asset=asset, notional="10")
    return AssetAllocationSlice(
        portfolio_session_id="portfolio-session-1",
        allocation_id=f"allocation-{index}",
        candidate_id=plan.candidate_id,
        asset=asset,
        direction="long",
        requested_quantity="1",
        effective_quantity="1",
        source_strategy_plan_digest=plan.digest,
        position_action="add",
        requested_notional="10",
        effective_notional="10",
        status="accepted",
    )


def test_accept_unchanged_keeps_requested_size_and_strategy_semantics():
    plan = _plan()
    result = PortfolioRiskGate().evaluate(plan, _snapshot(), _policy())

    assert isinstance(result, PortfolioSelection)
    allocation = result.selected_allocations[0]
    assert allocation.status == "accepted"
    assert allocation.requested_quantity == Decimal("1")
    assert allocation.effective_quantity == Decimal("1")
    assert allocation.requested_notional == Decimal("200")
    assert allocation.effective_notional == Decimal("200")
    assert allocation.direction == plan.direction
    assert allocation.position_action == plan.position_action
    assert allocation.position_management == plan.position_management
    assert allocation.protection_intent == plan.protection_intent
    assert result.policy_revision == "2026-08-25-a"
    assert result.decision_provenance["outcome"] == "ACCEPT_UNCHANGED"


def test_single_asset_cap_scales_only_down_and_preserves_direction():
    plan = _plan(quantity="2", notional="500")
    result = PortfolioRiskGate().evaluate(plan, _snapshot(), _policy())

    assert isinstance(result, PortfolioSelection)
    allocation = result.selected_allocations[0]
    assert allocation.status == "scaled"
    assert allocation.effective_notional == Decimal("300")
    assert allocation.effective_quantity == Decimal("1.2")
    assert allocation.direction == "long"
    assert allocation.position_management == plan.position_management
    assert allocation.protection_intent == plan.protection_intent
    assert "single_asset_concentration" in allocation.reasons


@pytest.mark.parametrize(
    ("snapshot_kwargs", "policy_kwargs", "reason"),
    [
        ({"total_exposure": "700"}, {"max_total_exposure_pct": "80"}, "global_exposure"),
        ({"margin_used": "100"}, {"max_margin_pct": "15"}, "margin"),
        ({"total_exposure": "500"}, {"max_leverage": "0.6"}, "leverage"),
        ({"available_cash": "200"}, {"min_cash_buffer_pct": "10"}, "cash_buffer"),
    ],
)
def test_global_constraints_create_stable_downward_scale(snapshot_kwargs, policy_kwargs, reason):
    result = PortfolioRiskGate().evaluate(_plan(quantity="2", notional="200"), _snapshot(**snapshot_kwargs), _policy(**policy_kwargs))

    assert isinstance(result, PortfolioSelection)
    allocation = result.selected_allocations[0]
    assert allocation.status == "scaled"
    assert reason in allocation.reasons
    assert allocation.effective_notional < allocation.requested_notional


def test_loss_limit_and_active_allocation_limit_reject_new_exposure():
    loss_result = PortfolioRiskGate().evaluate(
        _plan(),
        _snapshot(loss_pct="5"),
        _policy(max_loss_pct="5"),
    )
    assert isinstance(loss_result, PortfolioSelection)
    assert loss_result.selected_allocations == ()
    assert loss_result.rejected_candidates[0]["reason"] == "loss_limit"

    allocations = tuple(_allocation(f"ASSET-{index}", index) for index in range(10))
    capacity_result = PortfolioRiskGate().evaluate(
        _plan(asset="NEW-ASSET"),
        _snapshot(allocations=allocations),
        _policy(max_active_assets=10),
    )
    assert isinstance(capacity_result, PortfolioSelection)
    assert capacity_result.rejected_candidates[0]["reason"] == "max_active_assets"


def test_minimum_quantity_or_notional_rejects_unexecutable_effective_plan():
    quantity_result = PortfolioRiskGate().evaluate(
        _plan(quantity="0.5", notional="200"),
        _snapshot(),
        _policy(min_order_quantity="1"),
    )
    assert isinstance(quantity_result, PortfolioSelection)
    assert quantity_result.rejected_candidates[0]["reason"] == "below_minimum_quantity"

    notional_result = PortfolioRiskGate().evaluate(
        _plan(quantity="1", notional="5"),
        _snapshot(),
        _policy(min_order_notional="10"),
    )
    assert isinstance(notional_result, PortfolioSelection)
    assert notional_result.rejected_candidates[0]["reason"] == "below_minimum_notional"


def test_scale_below_minimum_becomes_reject_and_never_upscales():
    result = PortfolioRiskGate().evaluate(
        _plan(quantity="1", notional="500"),
        _snapshot(),
        _policy(min_order_notional="250", max_single_asset_aum_pct="20"),
    )
    assert isinstance(result, PortfolioSelection)
    assert result.rejected_candidates[0]["reason"] == "scaled_below_minimum_notional"


def test_untrusted_snapshot_returns_portfolio_risk_hold_without_selection():
    result = PortfolioRiskGate().evaluate(
        _plan(),
        _snapshot(coherent=False),
        _policy(),
    )

    assert isinstance(result, PortfolioRiskHold)
    assert result.reason_code == "snapshot_not_coherent"
    assert result.blocks_new_entries is True
    assert result.affected_assets == ("BTC",)


def test_reduce_or_exit_is_not_blocked_by_exposure_caps():
    result = PortfolioRiskGate().evaluate(
        _plan(action="reduce", quantity="1", notional=None),
        _snapshot(total_exposure="1000", available_cash="0", loss_pct="100"),
        _policy(max_total_exposure_pct="1", min_cash_buffer_pct="99", max_loss_pct="1"),
    )

    assert isinstance(result, PortfolioSelection)
    assert result.selected_allocations[0].status == "accepted"
    assert result.selected_allocations[0].effective_quantity == Decimal("1")


def test_evaluation_is_deterministic_and_has_no_broker_side_effect_surface():
    plan = _plan()
    gate = PortfolioRiskGate()
    first = gate.evaluate(plan, _snapshot(), _policy())
    second = gate.evaluate(plan, _snapshot(), _policy())

    assert first.digest == second.digest
    assert not hasattr(gate, "submit")
    assert not hasattr(gate, "cancel")
