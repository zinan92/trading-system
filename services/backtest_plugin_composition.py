"""Trusted composition root for all backtest plugin kinds."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from services.backtest_historical_adapter import EventDrivenHistoricalBacktestAdapter
from services.backtest_plugin_registry import (
    BacktestPluginRegistry,
    InvalidBacktestPlugin,
    normalize_backtest_plugin_name,
)
from services.backtest_port import (
    HISTORICAL_STRATEGY_KIND,
    SIGNAL_EVIDENCE_KIND,
    STRATEGY_SHADOW_KIND,
    BacktestPluginRuntime,
)
from services.backtest_signal_adapters import (
    LocalSignalBacktestAdapter,
    RemoteSignalBacktestAdapter,
    SyntheticSignalContextAdapter,
)
from services.strategy_shadow_nautilus import NautilusStrategyShadowReplay


DEFAULT_SIGNAL_BACKTEST_PLUGIN = "local_signal"
DEFAULT_HISTORICAL_BACKTEST_PLUGIN = "event_driven_strategy"
DEFAULT_STRATEGY_SHADOW_PLUGIN = "nautilus_strategy_shadow"


def build_backtest_plugin_registry() -> BacktestPluginRegistry:
    registry = BacktestPluginRegistry()
    registry.register(
        DEFAULT_SIGNAL_BACKTEST_PLUGIN,
        LocalSignalBacktestAdapter,
        kind=SIGNAL_EVIDENCE_KIND,
        implementation="services.backtest_signal_adapters.LocalSignalBacktestAdapter",
        evidence_tier="local_historical_signal",
        promotion_evidence_capable=True,
    )
    registry.register(
        "remote_signal",
        RemoteSignalBacktestAdapter,
        kind=SIGNAL_EVIDENCE_KIND,
        implementation="services.backtest_signal_adapters.RemoteSignalBacktestAdapter",
        evidence_tier="remote_historical_signal",
        promotion_evidence_capable=True,
    )
    registry.register(
        "synthetic_signal_context",
        SyntheticSignalContextAdapter,
        kind=SIGNAL_EVIDENCE_KIND,
        implementation="services.backtest_signal_adapters.SyntheticSignalContextAdapter",
        evidence_tier="synthetic_context",
        promotion_evidence_capable=False,
        degraded_by_design=True,
    )
    registry.register(
        DEFAULT_HISTORICAL_BACKTEST_PLUGIN,
        EventDrivenHistoricalBacktestAdapter,
        kind=HISTORICAL_STRATEGY_KIND,
        implementation=(
            "services.backtest_historical_adapter."
            "EventDrivenHistoricalBacktestAdapter"
        ),
        evidence_tier="historical_strategy",
        promotion_evidence_capable=True,
    )
    registry.register(
        DEFAULT_STRATEGY_SHADOW_PLUGIN,
        _build_nautilus_strategy_shadow,
        kind=STRATEGY_SHADOW_KIND,
        implementation="services.strategy_shadow_nautilus.NautilusStrategyShadowReplay",
        evidence_tier="execution_replay",
        promotion_evidence_capable=True,
    )
    return registry.freeze()


def compose_signal_backtest(
    config: Mapping[str, Any],
    *,
    strategy_config: Mapping[str, Any] | None = None,
    registry: BacktestPluginRegistry | None = None,
) -> BacktestPluginRuntime:
    plugin_name = _configured_plugin(
        config,
        "signal",
        DEFAULT_SIGNAL_BACKTEST_PLUGIN,
    )
    context = {
        "strategy_config": dict(strategy_config or {}),
        "base_url": str(config.get("backtest_base_url") or ""),
        "timeout_seconds": float(config.get("backtest_timeout_seconds", 5.0)),
    }
    return _compose(plugin_name, SIGNAL_EVIDENCE_KIND, context, registry)


def compose_historical_strategy_backtest(
    config: Mapping[str, Any],
    *,
    registry: BacktestPluginRegistry | None = None,
) -> BacktestPluginRuntime:
    plugin_name = _configured_plugin(
        config,
        "historical_strategy",
        DEFAULT_HISTORICAL_BACKTEST_PLUGIN,
    )
    return _compose(plugin_name, HISTORICAL_STRATEGY_KIND, {}, registry)


def compose_strategy_shadow_backtest(
    config: Mapping[str, Any],
    *,
    output_root: Path,
    nautilus_python: str | Path,
    preflight_path: str | Path,
    replay_executor: Any = None,
    registry: BacktestPluginRegistry | None = None,
) -> BacktestPluginRuntime:
    plugin_name = _configured_plugin(
        config,
        "strategy_shadow",
        DEFAULT_STRATEGY_SHADOW_PLUGIN,
    )
    context = {
        "output_root": Path(output_root),
        "nautilus_python": Path(nautilus_python),
        "preflight_path": Path(preflight_path),
        "config": dict(config),
        "replay_executor": replay_executor,
    }
    return _compose(plugin_name, STRATEGY_SHADOW_KIND, context, registry)


def _compose(
    plugin_name: str,
    kind: str,
    context: Mapping[str, Any],
    registry: BacktestPluginRegistry | None,
) -> BacktestPluginRuntime:
    effective_registry = registry or build_backtest_plugin_registry()
    effective_registry.freeze()
    descriptor = effective_registry.descriptor(plugin_name)
    port = effective_registry.build(plugin_name, context, expected_kind=kind)
    return BacktestPluginRuntime(
        port=port,
        descriptor=descriptor,
        registry_fingerprint=effective_registry.fingerprint,
    )


def _configured_plugin(config: Mapping[str, Any], key: str, default: str) -> str:
    section_value = config.get("backtest_plugins")
    section = section_value if isinstance(section_value, Mapping) else {}
    if key not in section:
        return default
    plugin_name = normalize_backtest_plugin_name(section.get(key))
    if not plugin_name:
        raise InvalidBacktestPlugin(f"configured {key} backtest plugin is empty")
    return plugin_name


def _build_nautilus_strategy_shadow(context: Mapping[str, Any]) -> NautilusStrategyShadowReplay:
    for key in ("output_root", "nautilus_python", "preflight_path", "config"):
        if key not in context:
            raise InvalidBacktestPlugin(
                f"nautilus strategy shadow context is missing required field: {key}"
            )
    config = context["config"]
    if not isinstance(config, Mapping):
        raise InvalidBacktestPlugin("nautilus strategy shadow config must be a mapping")
    return NautilusStrategyShadowReplay(
        Path(context["output_root"]),
        nautilus_python=Path(context["nautilus_python"]),
        preflight_path=Path(context["preflight_path"]),
        config=dict(config),
        replay_executor=context.get("replay_executor"),
    )
