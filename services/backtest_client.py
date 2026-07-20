from __future__ import annotations

import urllib.error

from schemas.analysis import Analysis
from schemas.backtest import BacktestEvidence
from schemas.market_data import Bar
from schemas.signal import Signal
from services.backtest_plugin_composition import compose_signal_backtest
from services.backtest_port import SignalBacktestRequest
from services.backtest_service import SignalBacktestService


class BacktestClient:
    def __init__(self, base_url: str, fallback_to_mock: bool = True, local_enabled: bool = False, strategy_config: dict | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.fallback_to_mock = fallback_to_mock
        self.local_enabled = local_enabled
        self.strategy_config = dict(strategy_config or {})

    def evaluate(self, signal: Signal, analysis: Analysis, bars: list[Bar] | None = None) -> BacktestEvidence:
        request = SignalBacktestRequest.from_domain(
            signal,
            analysis,
            bars or [],
            backtest_config=self.strategy_config,
            run_context={"compatibility_facade": "BacktestClient"},
        )
        if self.local_enabled and bars:
            return self._evaluate_with("local_signal", request)
        try:
            return self._evaluate_with("remote_signal", request)
        except (OSError, urllib.error.URLError, TimeoutError, ValueError, KeyError):
            if not self.fallback_to_mock:
                raise
            return self._evaluate_with("synthetic_signal_context", request)

    def _evaluate_with(
        self,
        plugin: str,
        request: SignalBacktestRequest,
    ) -> BacktestEvidence:
        runtime = compose_signal_backtest(
            {
                "backtest_base_url": self.base_url,
                "backtest_plugins": {"signal": plugin},
            },
            strategy_config=self.strategy_config,
        )
        return SignalBacktestService(runtime).evaluate(request)
