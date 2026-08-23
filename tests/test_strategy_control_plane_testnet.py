from pathlib import Path

import pytest

from services.strategy_control_plane import StrategyControlMachineError, StrategyControlPlane
from services.park_confirmation import ParkConfirmationLedger


def _plan() -> dict:
    return {
        "schema_version": "strategy-plan-v1",
        "strategy_type": "dca",
        "strategy_plan_id": "dca-testnet-control-plan",
        "strategy_session_id": "session-testnet",
        "strategy_revision_id": "revision-testnet",
        "plan_digest": "sha256:" + "a" * 64,
        "version": 1,
        "cycle_id": "2026-08-22_DAY",
        "direction": "long",
        "dca": {
            "entry_levels": [65000.0],
            "notional_per_addition": 6500.0,
            "target_price": 66000.0,
            "stop_price": 64000.0,
        },
        "risk_budget": {
            "equity": 10000.0,
            "maximum_loss_at_full_depth": 1000.0,
            "leverage_limit": 10.0,
            "max_notional": 10000.0,
            "max_open_orders": 2,
            "max_open_positions": 1,
            "max_slippage": 50.0,
        },
    }


class ReadinessOnlyTestnetAdapter:
    name = "standard_broker_testnet"

    def preflight(self) -> dict:
        return {
            "ready": True,
            "environment": "testnet",
            "network_io": False,
            "real_money_eligible": False,
        }


def _durable_confirmation(tmp_path: Path, plan: dict) -> dict:
    ledger = ParkConfirmationLedger(tmp_path, park_user_id="park-user")
    proposal = ledger.create_proposal(
        proposal_id=f"proposal:{plan['strategy_plan_id']}",
        strategy_session_id=plan["strategy_session_id"],
        strategy_revision_id=plan["strategy_revision_id"],
        plan_digest=plan["plan_digest"],
        risk_digest="sha256:" + "b" * 64,
        expires_at=4102444800,
        execution_environment="testnet",
    )
    decision = ledger.decide(
        proposal_id=proposal["proposal_id"],
        park_user_id="park-user",
        command_text=f"confirm {plan['plan_digest']}",
        current_binding={
            "strategy_session_id": plan["strategy_session_id"],
            "strategy_revision_id": plan["strategy_revision_id"],
        },
        now=1787350000,
    )
    return dict(decision)


def test_testnet_control_requires_exact_park_confirmation(tmp_path: Path) -> None:
    plane = StrategyControlPlane(tmp_path)
    plan = _plan()

    with pytest.raises(StrategyControlMachineError, match="testnet_confirmation_blocked"):
        plane.start_testnet_dca(
            plan,
            confirmation={
                "execution_authorized": False,
                "plan_digest": plan["plan_digest"],
                "source": "telegram",
                "strategy_session_id": plan["strategy_session_id"],
                "strategy_revision_id": plan["strategy_revision_id"],
            },
            market={"execution_ready": True, "fresh": True, "is_synthetic": False, "fallback_policy": "none"},
            adapter=ReadinessOnlyTestnetAdapter(),
        )


def test_testnet_control_requires_authoritative_market_before_lifecycle(tmp_path: Path) -> None:
    plane = StrategyControlPlane(tmp_path)
    plan = _plan()
    confirmation = _durable_confirmation(tmp_path, plan)

    with pytest.raises(StrategyControlMachineError, match="testnet_market_not_authoritative"):
        plane.start_testnet_dca(
            plan,
            confirmation=confirmation,
            market={"execution_ready": False, "fresh": False, "is_synthetic": True, "fallback_policy": "cache"},
            adapter=ReadinessOnlyTestnetAdapter(),
        )


def test_testnet_control_starts_lifecycle_only_after_authority_gates(tmp_path: Path) -> None:
    from tests.test_dca_testnet_lifecycle import _broker, _plan

    broker, _ = _broker(tmp_path, protection=True)
    plane = StrategyControlPlane(tmp_path)
    plan = _plan()
    plan.update(
        {
            "strategy_session_id": "session-testnet",
            "strategy_revision_id": "revision-testnet",
        }
    )
    confirmation = _durable_confirmation(tmp_path, plan)

    result = plane.start_testnet_dca(
        plan,
        confirmation=confirmation,
        market={
            "execution_ready": True,
            "fresh": True,
            "is_synthetic": False,
            "fallback_policy": "none",
        },
        adapter=broker,
        now="2026-08-22T01:00:00+00:00",
    )

    assert result["runtime"]["execution_environment"] == "testnet"
    assert result["runtime"]["actual_state"] == "running"
    assert len(result["lifecycle"]["orders"]) == 1


def test_testnet_control_never_publishes_running_when_protection_capability_is_missing(tmp_path: Path) -> None:
    from tests.test_dca_testnet_lifecycle import _broker, _plan

    broker, _ = _broker(tmp_path, protection=False)
    plane = StrategyControlPlane(tmp_path)
    plan = _plan()
    confirmation = _durable_confirmation(tmp_path, plan)

    with pytest.raises(StrategyControlMachineError, match="testnet_preflight_blocked"):
        plane.start_testnet_dca(
            plan,
            confirmation=confirmation,
            market={"execution_ready": True, "fresh": True, "is_synthetic": False, "fallback_policy": "none"},
            adapter=broker,
        )

    assert plane.active_plan(plan["cycle_id"]) is None


def test_external_host_bridge_cannot_start_dca_before_protection_story(tmp_path: Path) -> None:
    from services.broker_composition import build_broker_execution_port
    from tests.test_standard_broker_external_testnet import _context, _host

    host, runtime = _host()
    adapter = build_broker_execution_port(_context(tmp_path, host))
    plane = StrategyControlPlane(tmp_path)
    plan = _plan()
    confirmation = _durable_confirmation(tmp_path, plan)

    with pytest.raises(StrategyControlMachineError, match="testnet_adapter_required"):
        plane.start_testnet_dca(
            plan,
            confirmation=confirmation,
            market={"execution_ready": True, "fresh": True, "is_synthetic": False, "fallback_policy": "none"},
            adapter=adapter,
        )

    assert plane.active_plan(plan["cycle_id"]) is None
    assert runtime.invoke_calls == []


def test_external_host_bridge_cannot_start_grid_before_protection_story(tmp_path: Path) -> None:
    from services.broker_composition import build_broker_execution_port
    from tests.test_grid_testnet_lifecycle import _plan as grid_plan
    from tests.test_standard_broker_external_testnet import _context, _host

    host, runtime = _host()
    adapter = build_broker_execution_port(_context(tmp_path, host))
    plane = StrategyControlPlane(tmp_path)
    plan = grid_plan()
    confirmation = _durable_confirmation(tmp_path, plan)

    with pytest.raises(StrategyControlMachineError, match="testnet_adapter_required"):
        plane.start_testnet_grid(
            plan,
            confirmation=confirmation,
            market={"execution_ready": True, "fresh": True, "is_synthetic": False, "fallback_policy": "none"},
            adapter=adapter,
        )

    assert plane.active_plan(plan["cycle_id"]) is None
    assert runtime.invoke_calls == []
