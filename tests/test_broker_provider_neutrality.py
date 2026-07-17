import inspect

from pipelines.dashboard_server import build_dualtrack_order_post_response
from services.lab_execution_conformance import build_lab_execution_conformance_token
from services.multi_strategy_runner import MultiStrategyRunner
from services.strategy_control_plane import StrategyControlPlane


def _assert_provider_neutral(function) -> None:
    source = inspect.getsource(function).lower()
    assert "binance" not in source
    assert "tiger" not in source
    assert "if provider" not in source
    assert "broker_adapter import" not in source


def test_command_execution_functions_remain_provider_neutral():
    # Presentation-only diagnostics are intentionally outside this boundary and
    # move to the stable A6 read model. This test protects mutation paths only.
    for function in (
        StrategyControlPlane._submit_plan_orders,
        build_dualtrack_order_post_response,
        build_lab_execution_conformance_token,
        MultiStrategyRunner._broker_adapter_for,
    ):
        _assert_provider_neutral(function)

