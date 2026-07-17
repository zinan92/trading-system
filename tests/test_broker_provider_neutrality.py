import inspect

from pipelines.dashboard_server import build_dualtrack_order_post_response
from services.broker_read_model import (
    broker_reconciliation_block_reason,
    broker_reconciliation_status,
    project_broker_read_model,
)
from services.lab_execution_conformance import build_lab_execution_conformance_token
from services.multi_strategy_runner import MultiStrategyRunner
from services.strategy_control_plane import StrategyControlPlane
from services.trading_system_read_model import project_trading_system_read_model


def _assert_provider_neutral(function) -> None:
    source = inspect.getsource(function).lower()
    assert "binance" not in source
    assert "tiger" not in source
    assert "if provider" not in source
    assert "broker_adapter import" not in source


def test_command_execution_functions_remain_provider_neutral():
    for function in (
        StrategyControlPlane._submit_plan_orders,
        build_dualtrack_order_post_response,
        build_lab_execution_conformance_token,
        MultiStrategyRunner._broker_adapter_for,
        MultiStrategyRunner._execution_profile_for,
        MultiStrategyRunner._demo_reconciliation_block_reason,
        MultiStrategyRunner._reconciliation_status,
        project_broker_read_model,
        broker_reconciliation_block_reason,
        broker_reconciliation_status,
        project_trading_system_read_model,
    ):
        _assert_provider_neutral(function)
