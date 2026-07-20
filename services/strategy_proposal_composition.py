"""Trusted composition root for strategy proposal plugins."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from services.codex_newsletter_strategy_proposal import CodexNewsletterStrategyProposal
from services.config_loader import ROOT
from services.strategy_proposal_port import (
    StrategyProposalPort,
    StrategyProposalRuntime,
)
from services.strategy_proposal_registry import (
    InvalidStrategyProposalPlugin,
    StrategyProposalPluginRegistry,
    normalize_strategy_proposal_plugin_name,
)


DEFAULT_STRATEGY_PROPOSAL_PLUGIN = "codex_newsletter"


def build_strategy_proposal_plugin_registry(
    *,
    decision_provider: Callable[[str], dict[str, Any]] | None = None,
    repo_root: Path = ROOT,
) -> StrategyProposalPluginRegistry:
    registry = StrategyProposalPluginRegistry()

    def build_codex_newsletter(params: Mapping[str, Any]) -> StrategyProposalPort:
        return CodexNewsletterStrategyProposal(
            params,
            decision_provider=decision_provider,
            repo_root=repo_root,
        )

    registry.register(
        DEFAULT_STRATEGY_PROPOSAL_PLUGIN,
        build_codex_newsletter,
        implementation=(
            "services.codex_newsletter_strategy_proposal."
            "CodexNewsletterStrategyProposal"
        ),
        plan_source="machine_ai_newsletter",
        default_decision_mode="ai_newsletter",
        required_context=("newsletter",),
    )
    return registry.freeze()


def compose_strategy_proposal(
    config: Mapping[str, Any],
    *,
    registry: StrategyProposalPluginRegistry | None = None,
    decision_provider: Callable[[str], dict[str, Any]] | None = None,
    repo_root: Path = ROOT,
) -> StrategyProposalRuntime:
    planner_config_value = config.get("machine_planner")
    planner_config = planner_config_value if isinstance(planner_config_value, Mapping) else {}
    if "plugin" in planner_config:
        plugin_name = normalize_strategy_proposal_plugin_name(planner_config.get("plugin"))
        if not plugin_name:
            raise InvalidStrategyProposalPlugin("configured strategy proposal plugin is empty")
    else:
        plugin_name = DEFAULT_STRATEGY_PROPOSAL_PLUGIN

    if registry is not None and decision_provider is not None:
        raise InvalidStrategyProposalPlugin(
            "decision_provider compatibility injection cannot be combined with a custom proposal registry"
        )
    effective_registry = registry or build_strategy_proposal_plugin_registry(
        decision_provider=decision_provider,
        repo_root=repo_root,
    )
    effective_registry.freeze()
    descriptor = effective_registry.descriptor(plugin_name)
    port = effective_registry.build(plugin_name, planner_config)
    return StrategyProposalRuntime(
        port=port,
        descriptor=descriptor,
        registry_fingerprint=effective_registry.fingerprint,
    )
