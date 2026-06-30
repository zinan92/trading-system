"""Strategy library + registry.

Turns `configs/strategy.yaml` from a single hardcoded block into a library of
named strategies, each with its own params, symbol, and enabled flag. This is
the abstraction boundary that lets the runner (Phase 3) execute many strategies
in parallel — the signal logic itself is already fully config-driven, so each
strategy is just a config block plus a `SignalEngine` built from it.

Symbol-parameterized for future multi-instrument strategies; defaults to GOLD.
"""

from __future__ import annotations

from dataclasses import dataclass

from services.config_loader import load_pipeline_config, load_strategy_config
from services.signal_engine import SignalEngine

REQUIRED_CLASSIFICATION_FIELDS = {
    "family",
    "style",
    "directionality",
    "frequency_bucket",
    "holding_period",
    "expected_trades_per_day_min",
    "expected_trades_per_day_max",
    "return_profile",
    "risk_profile",
}


@dataclass(frozen=True)
class Strategy:
    strategy_id: str
    symbol: str
    params: dict
    enabled: bool = True
    starting_equity: float = 10_000.0
    timeframe: str = "5m"
    live: bool = False

    def signal_engine(self):
        """Build this strategy's engine by type, then wrap it in any configured
        confirm `filters` (e.g. a MACD zero-line filter on a chan 二买). The base
        engine is chosen by `engine`: `chan` → Chan-theory 二买/类二买 (needs
        py>=3.11 + pandas, imported lazily so the 3.9 MA jobs never load it),
        `macd` → MACD-cross engine, anything else → the default MA SignalEngine."""
        engine = self._base_engine()
        filters = (self.params.get("signal", {}) or {}).get("filters")
        if filters:
            from services.signal_filters import build_filtered_engine
            return build_filtered_engine(engine, filters)
        return engine

    def _base_engine(self):
        engine_type = str(self.params.get("engine", "ma")).lower()
        if engine_type == "chan":
            from services.chan_signal_engine import ChanSignalEngine
            return ChanSignalEngine(self.params)
        if engine_type == "macd":
            from services.macd_signal_engine import MacdSignalEngine
            return MacdSignalEngine(self.params)
        if engine_type in {
            "grid",
            "bollinger_reversion",
            "bollinger_reclaim_filter",
            "breakout",
            "fibonacci",
            "ema50_position",
            "adx_ema_pullback",
            "vwap_extension_reversion",
            "london_ny_compression_breakout",
            "breakout_retest_continuation",
            "false_breakout_reversal",
            "macd_trend_volatility_filter",
        }:
            from services.technical_rule_signal_engine import TechnicalRuleSignalEngine
            return TechnicalRuleSignalEngine(self.params)
        return SignalEngine(self.params, strategy_id=None)

    @property
    def classification(self) -> dict:
        value = self.params.get("classification", {})
        return value if isinstance(value, dict) else {}

    def classification_audit(self) -> dict:
        classification = self.classification
        missing = sorted(field for field in REQUIRED_CLASSIFICATION_FIELDS if classification.get(field) in (None, ""))
        min_trades = classification.get("expected_trades_per_day_min")
        max_trades = classification.get("expected_trades_per_day_max")
        numeric_errors: list[str] = []
        try:
            min_value = float(min_trades)
            max_value = float(max_trades)
            if min_value < 0:
                numeric_errors.append("expected_trades_per_day_min must be >= 0")
            if max_value < min_value:
                numeric_errors.append("expected_trades_per_day_max must be >= min")
        except (TypeError, ValueError):
            numeric_errors.append("expected_trades_per_day_min/max must be numeric")
        issues = missing + numeric_errors
        return {
            "strategy_id": self.strategy_id,
            "status": "pass" if not issues else "fail",
            "missing_fields": missing,
            "issues": issues,
            "classification": classification,
        }


class StrategyRegistry:
    def __init__(self, config: dict | None = None, default_starting_equity: float | None = None) -> None:
        self.config = config if config is not None else load_strategy_config()
        if default_starting_equity is None:
            default_starting_equity = float(load_pipeline_config().get("paper_account", {}).get("starting_equity", 10_000.0))
        self.default_starting_equity = default_starting_equity

    def strategies(self) -> list[Strategy]:
        out: list[Strategy] = []
        for strategy_id, block in (self.config or {}).items():
            if not isinstance(block, dict):
                continue
            out.append(
                Strategy(
                    strategy_id=strategy_id,
                    symbol=str(block.get("symbol", "GOLD")),
                    params=block,
                    enabled=bool(block.get("enabled", True)),
                    starting_equity=float(block.get("starting_equity", self.default_starting_equity)),
                    timeframe=str(block.get("timeframe", "5m")),
                    live=bool(block.get("live", False)),
                )
            )
        return out

    def enabled(self) -> list[Strategy]:
        return [strategy for strategy in self.strategies() if strategy.enabled]

    def enabled_for_runner(self) -> list[Strategy]:
        return [
            strategy
            for strategy in self.enabled()
            if strategy.classification_audit().get("status") == "pass"
        ]

    def classification_audit(self) -> dict:
        rows = [strategy.classification_audit() for strategy in self.strategies()]
        enabled_failures = [
            row
            for row, strategy in zip(rows, self.strategies())
            if strategy.enabled and row.get("status") != "pass"
        ]
        return {
            "status": "pass" if not enabled_failures else "fail",
            "strategy_count": len(rows),
            "enabled_failure_count": len(enabled_failures),
            "strategies": rows,
        }

    def get(self, strategy_id: str) -> Strategy | None:
        return next((s for s in self.strategies() if s.strategy_id == strategy_id), None)

    def default(self) -> Strategy | None:
        enabled = self.enabled()
        return enabled[0] if enabled else None
