from __future__ import annotations

import pytest

from services.strategy_analysis_port import StrategyAnalysisPort
from services.strategy_plugin_composition import (
    TECHNICAL_RULE_PLUGIN_NAMES,
    build_strategy_plugin_registry,
)
from services.strategy_plugin_registry import (
    DuplicateStrategyPlugin,
    InvalidStrategyPlugin,
    StrategyPluginRegistry,
    UnknownStrategyPlugin,
)
from services.strategy_registry import StrategyRegistry


class CustomAnalysisEngine:
    def __init__(self, params: dict) -> None:
        self.params = params

    def generate(self, asset, candles, events=None, run_date="", factor_context=None):
        return {
            "asset": getattr(asset, "symbol", ""),
            "bars": len(candles),
            "run_date": run_date,
            "tag": self.params.get("signal", {}).get("tag"),
        }


def _custom_registry() -> StrategyPluginRegistry:
    registry = StrategyPluginRegistry()
    registry.register(
        "custom_grid",
        CustomAnalysisEngine,
        implementation="tests.CustomAnalysisEngine",
    )
    return registry


def test_custom_plugin_runs_through_strategy_registry_without_core_edits() -> None:
    registry = StrategyRegistry(
        {
            "custom": {
                "engine": "custom_grid",
                "signal": {"tag": "injected"},
            }
        },
        analysis_plugins=_custom_registry(),
    )

    strategy = registry.get("custom")
    engine = strategy.signal_engine()

    assert isinstance(engine, StrategyAnalysisPort)
    assert engine.generate(type("Asset", (), {"symbol": "GOLD"})(), [], run_date="2026-07-18") == {
        "asset": "GOLD",
        "bars": 0,
        "run_date": "2026-07-18",
        "tag": "injected",
    }
    assert registry.analysis_plugin_audit()["status"] == "pass"


def test_missing_engine_preserves_explicit_ma_default() -> None:
    registry = StrategyRegistry({"default": {"signal": {}}})
    strategy = registry.get("default")

    assert strategy.analysis_plugin_name == "ma"
    assert type(strategy.signal_engine()).__name__ == "SignalEngine"


def test_empty_strategy_library_fails_instead_of_constructing_legacy_engine() -> None:
    registry = StrategyRegistry({})

    with pytest.raises(RuntimeError, match="no enabled strategy"):
        registry.require_default()
    with pytest.raises(RuntimeError, match="legacy default strategy is not configured"):
        registry.require_legacy_default()


def test_legacy_global_default_uses_registered_plugin_even_when_fleet_is_disabled() -> None:
    registry = StrategyRegistry({"gold_5m_v1": {"enabled": False, "signal": {}}})

    assert registry.default() is None
    assert type(registry.require_legacy_default().signal_engine()).__name__ == "SignalEngine"


def test_explicit_empty_engine_does_not_silently_become_ma() -> None:
    strategy = StrategyRegistry({"empty": {"engine": "", "signal": {}}}).get("empty")

    assert strategy.analysis_plugin_name == ""
    with pytest.raises(UnknownStrategyPlugin, match="<empty>"):
        strategy.signal_engine()


def test_explicit_unknown_engine_fails_closed_and_is_not_runner_eligible() -> None:
    registry = StrategyRegistry(
        {
            "unknown": {
                "engine": "not_installed",
                "classification": {
                    "family": "test",
                    "style": "test",
                    "directionality": "long_short",
                    "frequency_bucket": "medium",
                    "holding_period": "intraday",
                    "expected_trades_per_day_min": 1,
                    "expected_trades_per_day_max": 2,
                    "return_profile": "test",
                    "risk_profile": "medium",
                },
                "signal": {},
            }
        }
    )

    with pytest.raises(UnknownStrategyPlugin, match="not_installed"):
        registry.get("unknown").signal_engine()

    audit = registry.analysis_plugin_audit()
    assert audit["status"] == "fail"
    assert audit["enabled_failure_count"] == 1
    assert audit["strategies"][0]["engine"] == "not_installed"
    assert registry.enabled_for_runner() == []


def test_registry_rejects_empty_duplicate_and_structurally_invalid_plugins() -> None:
    registry = _custom_registry()

    with pytest.raises(InvalidStrategyPlugin, match="name is required"):
        registry.register(" ", CustomAnalysisEngine)
    with pytest.raises(DuplicateStrategyPlugin, match="custom_grid"):
        registry.register("CUSTOM_GRID", CustomAnalysisEngine)

    registry.register("broken", lambda _params: object())
    with pytest.raises(InvalidStrategyPlugin, match=r"without generate\(\)"):
        registry.build("broken", {})
    registry.register(
        "lying",
        CustomAnalysisEngine,
        capabilities=("generate", "historical_signals"),
    )
    with pytest.raises(InvalidStrategyPlugin, match="missing declared capabilities: historical_signals"):
        registry.build("lying", {})

    registry.freeze()
    with pytest.raises(InvalidStrategyPlugin, match="registry is frozen"):
        registry.register("late_plugin", CustomAnalysisEngine)


def test_builtin_composition_registers_every_configured_engine_name() -> None:
    plugins = build_strategy_plugin_registry()
    registry = StrategyRegistry(analysis_plugins=plugins)
    audit = registry.analysis_plugin_audit()

    expected = {"ma", "chan", "macd", *TECHNICAL_RULE_PLUGIN_NAMES}
    configured = {strategy.analysis_plugin_name for strategy in registry.strategies()}
    assert expected == set(plugins.names())
    assert configured <= expected
    assert audit["status"] == "pass"
    assert audit["registry_frozen"] is True
    assert len(audit["registry_fingerprint"]) == 64
    assert audit["strategy_count"] == 25
    assert all(item["status"] == "pass" for item in audit["strategies"])
    assert all(isinstance(strategy.signal_engine(), StrategyAnalysisPort) for strategy in registry.strategies())


def test_descriptors_are_stable_sorted_and_do_not_expose_factories() -> None:
    plugins = build_strategy_plugin_registry()
    rows = [descriptor.to_dict() for descriptor in plugins.descriptors()]

    assert [row["name"] for row in rows] == sorted(row["name"] for row in rows)
    assert all(row["schema_version"] == "strategy-analysis-plugin-v1" for row in rows)
    assert all("generate" in row["capabilities"] for row in rows)
    assert "factory" not in str(rows).lower()
