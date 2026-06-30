from pathlib import Path

from schemas.analysis import Analysis
from services.config_loader import load_strategy_config
from services.local_backtester import LocalBacktestConfig, LocalBacktester
from tests.test_local_backtester import _signal, _trend_bars


def test_load_strategy_config_has_gold_5m_parameters():
    config = load_strategy_config()

    assert config["gold_5m_v1"]["signal"]["ma_short_bars"] == 5
    assert config["gold_5m_v1"]["backtest"]["max_hold_bars"] == 24
    assert config["gold_5m_v1"]["position_gate"]["enabled"] is False
    assert config["gold_1m_chan"]["position_gate"]["enabled"] is True
    assert config["gold_1m_chan_ungated"]["position_gate"]["enabled"] is False
    assert config["gold_1m_chan_ungated"]["signal"]["bsp_types"] == config["gold_1m_chan"]["signal"]["bsp_types"]


def test_local_backtest_config_can_be_built_from_strategy_config():
    config = {
        "gold_5m_v1": {
            "backtest": {
                "stop_pct": 0.01,
                "target_pct": 0.02,
                "max_hold_bars": 8,
                "min_sample_size": 3,
                "supportive": {"min_win_rate": 0.4, "min_avg_r": -0.1, "min_profit_factor": 0.5, "max_drawdown_pct": 99},
                "mixed": {"min_profit_factor": 0.2, "min_avg_r": -0.5},
            }
        }
    }

    backtest_config = LocalBacktestConfig.from_strategy_config(config)
    evidence = LocalBacktester(backtest_config).evaluate(_signal(), Analysis("a1", "sig_gold_test", "GOLD"), _trend_bars())

    assert backtest_config.stop_pct == 0.01
    assert backtest_config.max_hold_bars == 8
    assert evidence.backtest_id.startswith("local5m_")
