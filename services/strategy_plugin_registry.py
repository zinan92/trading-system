"""Explicit registry for strategy-analysis plugin factories."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

from services.strategy_analysis_port import (
    StrategyAnalysisPort,
    StrategyPluginDescriptor,
    StrategyPluginFactory,
)


class StrategyPluginError(RuntimeError):
    pass


class UnknownStrategyPlugin(StrategyPluginError):
    pass


class DuplicateStrategyPlugin(StrategyPluginError):
    pass


class InvalidStrategyPlugin(StrategyPluginError):
    pass


def normalize_strategy_plugin_name(value: Any) -> str:
    return str(value or "").strip().lower()


class StrategyPluginRegistry:
    """Own named factories without importing any concrete signal engine."""

    def __init__(self) -> None:
        self._factories: dict[str, StrategyPluginFactory] = {}
        self._descriptors: dict[str, StrategyPluginDescriptor] = {}
        self._frozen = False

    def register(
        self,
        name: str,
        factory: StrategyPluginFactory,
        *,
        implementation: str = "",
        capabilities: Iterable[str] = ("generate",),
    ) -> StrategyPluginDescriptor:
        if self._frozen:
            raise InvalidStrategyPlugin("strategy plugin registry is frozen")
        normalized = normalize_strategy_plugin_name(name)
        if not normalized:
            raise InvalidStrategyPlugin("strategy plugin name is required")
        if normalized in self._factories:
            raise DuplicateStrategyPlugin(f"strategy plugin already registered: {normalized}")
        if not callable(factory):
            raise InvalidStrategyPlugin(f"strategy plugin factory is not callable: {normalized}")
        normalized_capabilities = tuple(sorted({str(item).strip() for item in capabilities if str(item).strip()}))
        if "generate" not in normalized_capabilities:
            raise InvalidStrategyPlugin(f"strategy plugin must declare generate capability: {normalized}")
        descriptor = StrategyPluginDescriptor(
            name=normalized,
            implementation=implementation.strip() or _callable_name(factory),
            capabilities=normalized_capabilities,
        )
        self._factories[normalized] = factory
        self._descriptors[normalized] = descriptor
        return descriptor

    def contains(self, name: str) -> bool:
        return normalize_strategy_plugin_name(name) in self._factories

    def descriptor(self, name: str) -> StrategyPluginDescriptor:
        normalized = normalize_strategy_plugin_name(name)
        descriptor = self._descriptors.get(normalized)
        if descriptor is None:
            raise UnknownStrategyPlugin(
                f"unknown strategy analysis plugin: {normalized or '<empty>'}; "
                f"registered={','.join(self.names()) or '<none>'}"
            )
        return descriptor

    def build(self, name: str, params: Mapping[str, Any]) -> StrategyAnalysisPort:
        descriptor = self.descriptor(name)
        if not isinstance(params, Mapping):
            raise InvalidStrategyPlugin(f"strategy plugin params must be a mapping: {descriptor.name}")
        engine = self._factories[descriptor.name](dict(params))
        if not isinstance(engine, StrategyAnalysisPort):
            raise InvalidStrategyPlugin(
                f"strategy plugin {descriptor.name} returned {type(engine).__name__} without generate()"
            )
        return engine

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._descriptors))

    def descriptors(self) -> tuple[StrategyPluginDescriptor, ...]:
        return tuple(self._descriptors[name] for name in self.names())

    def freeze(self) -> StrategyPluginRegistry:
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
