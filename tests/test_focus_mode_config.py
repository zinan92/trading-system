from services.config_loader import load_pipeline_config, load_strategy_config
from services.strategy_registry import StrategyRegistry


def test_focus_mode_parks_strategy_fleet_and_demo_trading_binding() -> None:
    strategies = StrategyRegistry(load_strategy_config())
    pipeline = load_pipeline_config()

    assert len(strategies.strategies()) == 25
    assert strategies.enabled() == []
    assert pipeline["demo_trading"]["enabled"] is False
    assert pipeline["schedule"]["profile"] == "dualtrack_focus"
