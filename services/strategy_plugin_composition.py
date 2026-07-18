"""Trusted built-in composition root for strategy-analysis plugins.

Factories import concrete engines only when selected. This preserves the Chan
runtime's optional dependency boundary while keeping application/domain code
free of engine-name branches.
"""

from __future__ import annotations

from typing import Any

from services.strategy_analysis_port import StrategyAnalysisPort
from services.strategy_plugin_registry import StrategyPluginRegistry


TECHNICAL_RULE_PLUGIN_NAMES = (
    "grid",
    "adr_exhaustion_reversion",
    "bollinger_reversion",
    "bollinger_reclaim_filter",
    "vwap_zscore_reversion",
    "breakout",
    "fibonacci",
    "ema50_position",
    "adx_ema_pullback",
    "vwap_extension_reversion",
    "london_ny_compression_breakout",
    "ny_opening_range_breakout",
    "breakout_retest_continuation",
    "false_breakout_reversal",
    "psych_level_rejection",
    "macd_trend_volatility_filter",
    "vwap_trend_pullback",
)


def build_strategy_plugin_registry() -> StrategyPluginRegistry:
    registry = StrategyPluginRegistry()
    registry.register(
        "ma",
        _build_ma,
        implementation="services.signal_engine.SignalEngine",
    )
    registry.register(
        "chan",
        _build_chan,
        implementation="services.chan_signal_engine.ChanSignalEngine",
        capabilities=("generate", "historical_signals"),
    )
    registry.register(
        "macd",
        _build_macd,
        implementation="services.macd_signal_engine.MacdSignalEngine",
        capabilities=("generate", "historical_signals"),
    )
    for name in TECHNICAL_RULE_PLUGIN_NAMES:
        registry.register(
            name,
            _build_technical_rule,
            implementation="services.technical_rule_signal_engine.TechnicalRuleSignalEngine",
            capabilities=("generate", "historical_signals"),
        )
    return registry.freeze()


def _build_ma(params: dict[str, Any]) -> StrategyAnalysisPort:
    from services.signal_engine import SignalEngine

    return SignalEngine(params, strategy_id=None)


def _build_chan(params: dict[str, Any]) -> StrategyAnalysisPort:
    from services.chan_signal_engine import ChanSignalEngine

    return ChanSignalEngine(params)


def _build_macd(params: dict[str, Any]) -> StrategyAnalysisPort:
    from services.macd_signal_engine import MacdSignalEngine

    return MacdSignalEngine(params)


def _build_technical_rule(params: dict[str, Any]) -> StrategyAnalysisPort:
    from services.technical_rule_signal_engine import TechnicalRuleSignalEngine

    return TechnicalRuleSignalEngine(params)
