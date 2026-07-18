"""Trusted composition root for risk policy and audit-store adapters."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from services.risk_decision_store import FileRiskDecisionStore
from services.risk_live_money_adapter import LiveMoneyRiskDecisionAdapter
from services.risk_policy_paper import (
    PaperGridRiskDecisionPort,
    grid_risk_evaluator,
)
from services.risk_policy_registry import (
    RiskPolicyPluginDescriptor,
    RiskPolicyPluginRegistry,
    normalize_risk_policy_plugin_name,
)
from services.risk_port import RiskDecisionPort, RiskDecisionStorePort


DEFAULT_GRID_RISK_POLICY_PLUGIN = "paper_grid_risk"


@dataclass(frozen=True)
class RiskPolicyRuntime:
    port: RiskDecisionPort
    descriptor: RiskPolicyPluginDescriptor
    registry_fingerprint: str


def build_risk_policy_registry() -> RiskPolicyPluginRegistry:
    return (
        RiskPolicyPluginRegistry()
        .register(
            DEFAULT_GRID_RISK_POLICY_PLUGIN,
            PaperGridRiskDecisionPort,
            implementation=(
                "services.risk_policy_paper.PaperGridRiskDecisionPort"
            ),
            evaluator=grid_risk_evaluator(),
        )
        .freeze()
    )


RISK_POLICY_PLUGINS = build_risk_policy_registry()


def compose_grid_risk_policy(
    config: Mapping[str, Any],
    *,
    registry: RiskPolicyPluginRegistry = RISK_POLICY_PLUGINS,
) -> RiskPolicyRuntime:
    section_value = config.get("risk_policy")
    if section_value is not None and not isinstance(section_value, Mapping):
        raise ValueError("configured risk_policy must be a mapping")
    section = section_value if isinstance(section_value, Mapping) else {}
    if "paper_grid" not in section:
        plugin_name = DEFAULT_GRID_RISK_POLICY_PLUGIN
    else:
        plugin_name = normalize_risk_policy_plugin_name(section.get("paper_grid"))
        if not plugin_name:
            raise ValueError("configured paper-grid risk policy plugin is empty")
    descriptor = registry.descriptor(plugin_name)
    return RiskPolicyRuntime(
        port=registry.build(plugin_name),
        descriptor=descriptor,
        registry_fingerprint=registry.fingerprint,
    )


def build_risk_decision_store(output_root: Path) -> RiskDecisionStorePort:
    return FileRiskDecisionStore(Path(output_root))


def build_live_money_risk_adapter(
    output_root: Path,
    *,
    broker_config: Mapping[str, Any] | None = None,
    legacy_guardrails=None,
    store: RiskDecisionStorePort | None = None,
) -> LiveMoneyRiskDecisionAdapter:
    return LiveMoneyRiskDecisionAdapter(
        Path(output_root),
        broker_config=broker_config,
        legacy_guardrails=legacy_guardrails,
        store=store or build_risk_decision_store(Path(output_root)),
    )
