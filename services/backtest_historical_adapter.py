"""Adapter around the existing event-driven historical strategy backtester."""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

from services.backtest_port import HistoricalStrategyBacktestRequest
from services.strategy_backtester import BacktestConfig, StrategyBacktester


class EventDrivenHistoricalBacktestAdapter:
    def __init__(self, _context: Mapping[str, Any] | None = None) -> None:
        pass

    def run(self, request: HistoricalStrategyBacktestRequest) -> dict[str, Any]:
        request_config = dict(request.backtest_config)
        strategy = SimpleNamespace(
            params={"backtest": dict(request_config.get("strategy_backtest") or {})},
            starting_equity=float(request_config.get("starting_equity", 10_000.0)),
        )
        config = BacktestConfig.from_strategy(strategy)
        return StrategyBacktester(config, cost_rules=dict(request.cost_rules)).run(
            request.bar_objects(),
            request.signal_rows(),
        )
