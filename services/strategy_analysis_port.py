"""Engine-neutral contract for strategy analysis plugins.

The application supplies normalized strategy parameters and market-domain
objects. A plugin owns signal logic only; it has no authority over risk,
execution, broker routing, accounting, or persistence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


STRATEGY_ANALYSIS_PLUGIN_SCHEMA = "strategy-analysis-plugin-v1"
STRATEGY_ANALYSIS_PLUGIN_AUDIT_SCHEMA = "strategy-analysis-plugin-audit-v1"


@runtime_checkable
class StrategyAnalysisPort(Protocol):
    """Minimum live-analysis surface shared by every signal engine."""

    def generate(
        self,
        asset: Any,
        candles: list[Any],
        events: list[Any] | None = None,
        run_date: str = "",
        factor_context: dict[str, dict] | None = None,
    ) -> Any:
        ...


class StrategyPluginFactory(Protocol):
    def __call__(self, params: dict[str, Any]) -> StrategyAnalysisPort:
        ...


@dataclass(frozen=True)
class StrategyPluginDescriptor:
    name: str
    implementation: str
    capabilities: tuple[str, ...] = ("generate",)
    schema_version: str = STRATEGY_ANALYSIS_PLUGIN_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "implementation": self.implementation,
            "capabilities": list(self.capabilities),
        }
