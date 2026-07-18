"""Explicit, frozen registry for strategy proposal plugin factories."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

from services.strategy_proposal_port import (
    StrategyProposalFactory,
    StrategyProposalPluginDescriptor,
    StrategyProposalPort,
)


class StrategyProposalPluginError(RuntimeError):
    pass


class UnknownStrategyProposalPlugin(StrategyProposalPluginError):
    pass


class DuplicateStrategyProposalPlugin(StrategyProposalPluginError):
    pass


class InvalidStrategyProposalPlugin(StrategyProposalPluginError):
    pass


def normalize_strategy_proposal_plugin_name(value: Any) -> str:
    return str(value or "").strip().lower()


class StrategyProposalPluginRegistry:
    """Own proposal factories without importing a concrete model provider."""

    def __init__(self) -> None:
        self._factories: dict[str, StrategyProposalFactory] = {}
        self._descriptors: dict[str, StrategyProposalPluginDescriptor] = {}
        self._frozen = False

    def register(
        self,
        name: str,
        factory: StrategyProposalFactory,
        *,
        implementation: str = "",
        plan_source: str = "",
        default_decision_mode: str = "",
        capabilities: Iterable[str] = ("propose",),
    ) -> StrategyProposalPluginDescriptor:
        if self._frozen:
            raise InvalidStrategyProposalPlugin("strategy proposal plugin registry is frozen")
        normalized = normalize_strategy_proposal_plugin_name(name)
        if not normalized:
            raise InvalidStrategyProposalPlugin("strategy proposal plugin name is required")
        if normalized in self._factories:
            raise DuplicateStrategyProposalPlugin(f"strategy proposal plugin already registered: {normalized}")
        if not callable(factory):
            raise InvalidStrategyProposalPlugin(
                f"strategy proposal plugin factory is not callable: {normalized}"
            )
        normalized_capabilities = tuple(sorted({str(item).strip() for item in capabilities if str(item).strip()}))
        if "propose" not in normalized_capabilities:
            raise InvalidStrategyProposalPlugin(
                f"strategy proposal plugin must declare propose capability: {normalized}"
            )
        descriptor = StrategyProposalPluginDescriptor(
            name=normalized,
            implementation=implementation.strip() or _callable_name(factory),
            plan_source=plan_source.strip() or f"machine_strategy_proposal:{normalized}",
            default_decision_mode=default_decision_mode.strip() or normalized,
            capabilities=normalized_capabilities,
        )
        self._factories[normalized] = factory
        self._descriptors[normalized] = descriptor
        return descriptor

    def descriptor(self, name: str) -> StrategyProposalPluginDescriptor:
        normalized = normalize_strategy_proposal_plugin_name(name)
        descriptor = self._descriptors.get(normalized)
        if descriptor is None:
            raise UnknownStrategyProposalPlugin(
                f"unknown strategy proposal plugin: {normalized or '<empty>'}; "
                f"registered={','.join(self.names()) or '<none>'}"
            )
        return descriptor

    def build(self, name: str, params: Mapping[str, Any]) -> StrategyProposalPort:
        descriptor = self.descriptor(name)
        if not isinstance(params, Mapping):
            raise InvalidStrategyProposalPlugin(
                f"strategy proposal plugin params must be a mapping: {descriptor.name}"
            )
        proposal = self._factories[descriptor.name](dict(params))
        if not isinstance(proposal, StrategyProposalPort):
            raise InvalidStrategyProposalPlugin(
                f"strategy proposal plugin {descriptor.name} returned "
                f"{type(proposal).__name__} without propose()"
            )
        missing = [
            capability
            for capability in descriptor.capabilities
            if not callable(getattr(proposal, capability, None))
        ]
        if missing:
            raise InvalidStrategyProposalPlugin(
                f"strategy proposal plugin {descriptor.name} is missing declared capabilities: "
                f"{','.join(missing)}"
            )
        return proposal

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._descriptors))

    def descriptors(self) -> tuple[StrategyProposalPluginDescriptor, ...]:
        return tuple(self._descriptors[name] for name in self.names())

    def freeze(self) -> StrategyProposalPluginRegistry:
        self._frozen = True
        return self

    @property
    def frozen(self) -> bool:
        return self._frozen

    @property
    def fingerprint(self) -> str:
        payload = [descriptor.to_dict() for descriptor in self.descriptors()]
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def _callable_name(factory: Any) -> str:
    module = str(getattr(factory, "__module__", "") or "")
    qualname = str(getattr(factory, "__qualname__", getattr(factory, "__name__", "")) or "")
    return ".".join(part for part in (module, qualname) if part) or type(factory).__name__
