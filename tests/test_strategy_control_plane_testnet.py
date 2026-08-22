from pathlib import Path

import pytest

from services.strategy_control_plane import StrategyControlMachineError, StrategyControlPlane


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
    confirmation = {
        "execution_authorized": True,
        "plan_digest": plan["plan_digest"],
        "source": "telegram",
        "strategy_session_id": plan["strategy_session_id"],
        "strategy_revision_id": plan["strategy_revision_id"],
    }

    with pytest.raises(StrategyControlMachineError, match="testnet_market_not_authoritative"):
        plane.start_testnet_dca(
            plan,
            confirmation=confirmation,
            market={"execution_ready": False, "fresh": False, "is_synthetic": True, "fallback_policy": "cache"},
            adapter=ReadinessOnlyTestnetAdapter(),
        )
