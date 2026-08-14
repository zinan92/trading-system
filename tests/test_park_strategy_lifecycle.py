from __future__ import annotations

from pathlib import Path

import pytest

from services.park_strategy_lifecycle import (
    ParkStrategyLifecycleError,
    ParkStrategyLifecycleLedger,
    admit_clean_slate,
    assert_immutable_revision,
    validate_owned_artifact,
)


def _plan(**overrides: object) -> dict:
    plan = {
        "strategy_session_id": "session-1",
        "strategy_revision_id": "revision-1",
        "plan_digest": "sha256:plan-1",
        "direction": "short",
        "strategy_type": "dca",
        "upper_price_boundary": 4444.0,
        "lower_price_boundary": 4200.0,
        "stop_price": 4444.0,
        "take_profit_price": 4200.0,
        "maximum_leverage": 10.0,
        "maximum_acceptable_loss": 100.0,
    }
    plan.update(overrides)
    return plan


def test_clean_slate_gate_is_non_mutating_and_typed() -> None:
    assert admit_clean_slate({"reconciliation_healthy": True}) == {
        "admitted": True,
        "code": "clean_slate",
        "blockers": [],
        "mutations": [],
    }
    blocked = admit_clean_slate({"reconciliation_healthy": False, "open_positions": 1})
    assert blocked["admitted"] is False
    assert set(blocked["blockers"]) == {"reconciliation_unhealthy", "open_positions"}
    assert blocked["mutations"] == []


def test_all_artifacts_need_exact_immutable_ownership() -> None:
    owned = {"order_id": "order-1", **_plan()}
    assert validate_owned_artifact(owned, strategy_session_id="session-1", strategy_revision_id="revision-1", artifact_type="order")["order_id"] == "order-1"
    with pytest.raises(ParkStrategyLifecycleError, match="ownership") as exc:
        validate_owned_artifact(owned, strategy_session_id="session-2", strategy_revision_id="revision-1", artifact_type="fill")
    assert exc.value.code == "ownership_mismatch"


def test_active_plan_rejects_reversal_or_parameter_edit() -> None:
    with pytest.raises(ParkStrategyLifecycleError, match="cannot change"):
        assert_immutable_revision(_plan(), _plan(direction="long"))
    with pytest.raises(ParkStrategyLifecycleError, match="immutable"):
        assert_immutable_revision(_plan(), _plan(strategy_revision_id="revision-2"))


def test_boundary_plan_is_ordered_identity_bound_and_idempotent(tmp_path: Path) -> None:
    ledger = ParkStrategyLifecycleLedger(tmp_path / "outputs")
    ledger.activate(_plan())
    first = ledger.boundary_action_plan(
        strategy_session_id="session-1",
        strategy_revision_id="revision-1",
        boundary="upper",
        observed_price=4444.0,
        trusted_market=True,
        fresh_tick=True,
    )
    second = ledger.boundary_action_plan(
        strategy_session_id="session-1",
        strategy_revision_id="revision-1",
        boundary="upper",
        observed_price=4444.0,
        trusted_market=True,
        fresh_tick=True,
    )
    assert first == second
    assert first["ordered_actions"] == [
        "freeze_new_entries",
        "cancel_remaining_strategy_entries",
        "close_all_strategy_owned_positions",
        "reconcile",
        "persist_closure",
        "notify_park",
        "enter_paused",
    ]
    assert ledger.active_plan()["state"] == "PAUSED"
    assert len([row for row in ledger.rows() if row["event"] == "terminal_action_plan"]) == 1


def test_boundary_requires_trusted_fresh_market_and_exact_active_identity(tmp_path: Path) -> None:
    ledger = ParkStrategyLifecycleLedger(tmp_path / "outputs")
    ledger.activate(_plan())
    with pytest.raises(ParkStrategyLifecycleError, match="trusted fresh"):
        ledger.boundary_action_plan(
            strategy_session_id="session-1", strategy_revision_id="revision-1",
            boundary="lower", observed_price=4200, trusted_market=False, fresh_tick=True,
        )
    with pytest.raises(ParkStrategyLifecycleError, match="active strategy"):
        ledger.boundary_action_plan(
            strategy_session_id="session-2", strategy_revision_id="revision-2",
            boundary="lower", observed_price=4200, trusted_market=True, fresh_tick=True,
        )


def test_structural_blocker_never_authorizes_flatten(tmp_path: Path) -> None:
    ledger = ParkStrategyLifecycleLedger(tmp_path / "outputs")
    blocker = ledger.structural_blocker(
        strategy_session_id="session-1",
        strategy_revision_id="revision-1",
        code="reconciliation_unknown",
    )
    assert blocker["entry_authority"] == "freeze_new_exposure"
    assert blocker["position_authority"] == "preserve_safe_protection_no_automatic_flatten"
    assert "flatten" not in blocker["next_action"]
