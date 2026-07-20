"""Strategy library + registry.

Turns `configs/strategy.yaml` from a single hardcoded block into a library of
named strategies, each with its own params, symbol, and enabled flag. This is
the abstraction boundary that lets the runner (Phase 3) execute many strategies
in parallel — the signal logic itself is already fully config-driven, so each
strategy is just a config block plus a `SignalEngine` built from it.

Symbol-parameterized for future multi-instrument strategies; defaults to GOLD.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from services.config_loader import load_pipeline_config, load_strategy_config
from services.strategy_analysis_port import STRATEGY_ANALYSIS_PLUGIN_AUDIT_SCHEMA
from services.strategy_plugin_registry import StrategyPluginRegistry, UnknownStrategyPlugin

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
    analysis_plugins: StrategyPluginRegistry | None = field(default=None, repr=False, compare=False)

    @property
    def analysis_plugin_name(self) -> str:
        value = self.params["engine"] if "engine" in self.params else "ma"
        return str(value or "").strip().lower()

    def signal_engine(self):
        """Build the registered base plugin, then apply configured filters."""
        engine = self._analysis_plugin_registry().build(self.analysis_plugin_name, self.params)
        filters = (self.params.get("signal", {}) or {}).get("filters")
        if filters:
            from services.signal_filters import build_filtered_engine
            return build_filtered_engine(engine, filters)
        return engine

    def analysis_plugin_audit(self) -> dict:
        registry = self._analysis_plugin_registry()
        try:
            descriptor = registry.descriptor(self.analysis_plugin_name)
        except UnknownStrategyPlugin as exc:
            return {
                "strategy_id": self.strategy_id,
                "engine": self.analysis_plugin_name,
                "status": "fail",
                "issues": [str(exc)],
                "plugin": None,
            }
        return {
            "strategy_id": self.strategy_id,
            "engine": self.analysis_plugin_name,
            "status": "pass",
            "issues": [],
            "plugin": descriptor.to_dict(),
        }

    def _analysis_plugin_registry(self) -> StrategyPluginRegistry:
        if self.analysis_plugins is not None:
            return self.analysis_plugins
        from services.strategy_plugin_composition import build_strategy_plugin_registry

        return build_strategy_plugin_registry()

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
    def __init__(
        self,
        config: dict | None = None,
        default_starting_equity: float | None = None,
        analysis_plugins: StrategyPluginRegistry | None = None,
    ) -> None:
        self.config = config if config is not None else load_strategy_config()
        if default_starting_equity is None:
            default_starting_equity = float(load_pipeline_config().get("paper_account", {}).get("starting_equity", 10_000.0))
        self.default_starting_equity = default_starting_equity
        if analysis_plugins is None:
            from services.strategy_plugin_composition import build_strategy_plugin_registry

            analysis_plugins = build_strategy_plugin_registry()
        self.analysis_plugins = analysis_plugins.freeze()

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
                    analysis_plugins=self.analysis_plugins,
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
            and strategy.analysis_plugin_audit().get("status") == "pass"
        ]

    def classification_audit(self) -> dict:
        strategies = self.strategies()
        rows = [strategy.classification_audit() for strategy in strategies]
        enabled_failures = [
            row
            for row, strategy in zip(rows, strategies)
            if strategy.enabled and row.get("status") != "pass"
        ]
        return {
            "status": "pass" if not enabled_failures else "fail",
            "strategy_count": len(rows),
            "enabled_failure_count": len(enabled_failures),
            "strategies": rows,
        }

    def analysis_plugin_audit(self) -> dict:
        strategies = self.strategies()
        rows = [strategy.analysis_plugin_audit() for strategy in strategies]
        enabled_failures = [
            row
            for row, strategy in zip(rows, strategies)
            if strategy.enabled and row.get("status") != "pass"
        ]
        return {
            "schema_version": STRATEGY_ANALYSIS_PLUGIN_AUDIT_SCHEMA,
            "status": "pass" if not enabled_failures else "fail",
            "registry_frozen": self.analysis_plugins.frozen,
            "registry_fingerprint": self.analysis_plugins.fingerprint,
            "registered_plugins": [descriptor.to_dict() for descriptor in self.analysis_plugins.descriptors()],
            "strategy_count": len(rows),
            "enabled_failure_count": len(enabled_failures),
            "strategies": rows,
        }

    def get(self, strategy_id: str) -> Strategy | None:
        return next((s for s in self.strategies() if s.strategy_id == strategy_id), None)

    def default(self) -> Strategy | None:
        enabled = self.enabled()
        return enabled[0] if enabled else None

    def require_default(self) -> Strategy:
        strategy = self.default()
        if strategy is None:
            raise RuntimeError("no enabled strategy is configured")
        return strategy

    def require_legacy_default(self, strategy_id: str = "gold_5m_v1") -> Strategy:
        strategy = self.get(strategy_id)
        if strategy is None:
            raise RuntimeError(f"legacy default strategy is not configured: {strategy_id}")
        return strategy
