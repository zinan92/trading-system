from pathlib import Path

import pytest

from services.strategy_control_plane import StrategyControlMachineError, StrategyControlPlane
from tests.test_grid_testnet_lifecycle import _broker, _fill, _plan
from tests.test_strategy_control_plane_testnet import _durable_confirmation


def test_control_plane_starts_and_advances_confirmed_grid_testnet(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path)
    plane = StrategyControlPlane(tmp_path)
    plan = _plan()
    confirmation = _durable_confirmation(tmp_path, plan)
    started = plane.start_testnet_grid(
        plan,
        confirmation=confirmation,
        market={"execution_ready": True, "fresh": True, "is_synthetic": False, "fallback_policy": "none"},
        adapter=broker,
        now="2026-08-22T01:00:00+00:00",
    )
    assert started["runtime"]["actual_state"] == "running"
    entry = started["lifecycle"]["orders"][0]
    advanced = plane.advance_testnet_grid(
        plan["cycle_id"],
        adapter=broker,
        fill=_fill(entry, price=65000.0, tid=100),
        timestamp="2026-08-22T01:01:00+00:00",
    )
    assert advanced["runtime"]["grid_lifecycle_status"] == "active"
    assert advanced["lifecycle"]["hard_stop_protection"]["quantity"] > 0


def test_control_plane_grid_rejects_untrusted_market_without_mutation(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path)
    plane = StrategyControlPlane(tmp_path)
    plan = _plan()
    confirmation = _durable_confirmation(tmp_path, plan)
    with pytest.raises(StrategyControlMachineError, match="testnet_market_not_authoritative"):
        plane.start_testnet_grid(
            plan,
            confirmation=confirmation,
            market={"execution_ready": False, "fresh": False, "is_synthetic": True, "fallback_policy": "cache"},
            adapter=broker,
        )
    assert plane.active_plan(plan["cycle_id"]) is None
