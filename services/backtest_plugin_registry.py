"""Kind-aware frozen registry for backtest plugin factories."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

from services.backtest_port import (
    BACKTEST_PLUGIN_KINDS,
    HISTORICAL_STRATEGY_KIND,
    SIGNAL_EVIDENCE_KIND,
    STRATEGY_SHADOW_KIND,
    BacktestPluginDescriptor,
    BacktestPluginFactory,
    HistoricalStrategyBacktestPort,
    SignalBacktestPort,
    StrategyShadowReplayPort,
)


class BacktestPluginError(RuntimeError):
    pass


class UnknownBacktestPlugin(BacktestPluginError):
    pass


class DuplicateBacktestPlugin(BacktestPluginError):
    pass


class InvalidBacktestPlugin(BacktestPluginError):
    pass


def normalize_backtest_plugin_name(value: Any) -> str:
    return str(value or "").strip().lower()


class BacktestPluginRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, BacktestPluginFactory] = {}
        self._descriptors: dict[str, BacktestPluginDescriptor] = {}
        self._frozen = False

    def register(
        self,
        name: str,
        factory: BacktestPluginFactory,
        *,
        kind: str,
        implementation: str = "",
        evidence_tier: str,
        promotion_evidence_capable: bool = False,
        degraded_by_design: bool = False,
        capabilities: Iterable[str] | None = None,
    ) -> BacktestPluginDescriptor:
        if self._frozen:
            raise InvalidBacktestPlugin("backtest plugin registry is frozen")
        normalized = normalize_backtest_plugin_name(name)
        if not normalized:
            raise InvalidBacktestPlugin("backtest plugin name is required")
        if normalized in self._factories:
            raise DuplicateBacktestPlugin(f"backtest plugin already registered: {normalized}")
        if kind not in BACKTEST_PLUGIN_KINDS:
            raise InvalidBacktestPlugin(f"invalid backtest plugin kind: {kind}")
        if not callable(factory):
            raise InvalidBacktestPlugin(f"backtest plugin factory is not callable: {normalized}")
        required_capability = _required_capability(kind)
        normalized_capabilities = tuple(sorted({
            str(item).strip()
            for item in (capabilities or (required_capability,))
            if str(item).strip()
        }))
        if required_capability not in normalized_capabilities:
            raise InvalidBacktestPlugin(
                f"backtest plugin {normalized} must declare {required_capability} capability"
            )
        if not str(evidence_tier or "").strip():
            raise InvalidBacktestPlugin(f"backtest plugin evidence_tier is required: {normalized}")
        descriptor = BacktestPluginDescriptor(
            name=normalized,
            kind=kind,
            implementation=implementation.strip() or _callable_name(factory),
            evidence_tier=str(evidence_tier).strip(),
            promotion_evidence_capable=bool(promotion_evidence_capable),
            degraded_by_design=bool(degraded_by_design),
            capabilities=normalized_capabilities,
        )
        self._factories[normalized] = factory
        self._descriptors[normalized] = descriptor
        return descriptor

    def descriptor(self, name: str) -> BacktestPluginDescriptor:
        normalized = normalize_backtest_plugin_name(name)
        descriptor = self._descriptors.get(normalized)
        if descriptor is None:
            raise UnknownBacktestPlugin(
                f"unknown backtest plugin: {normalized or '<empty>'}; "
                f"registered={','.join(self.names()) or '<none>'}"
            )
        return descriptor

    def build(self, name: str, context: Mapping[str, Any], *, expected_kind: str) -> object:
        if expected_kind not in BACKTEST_PLUGIN_KINDS:
            raise InvalidBacktestPlugin(f"invalid expected backtest plugin kind: {expected_kind}")
        descriptor = self.descriptor(name)
        if descriptor.kind != expected_kind:
            raise InvalidBacktestPlugin(
                f"backtest plugin {descriptor.name} has kind {descriptor.kind}, expected {expected_kind}"
            )
        if not isinstance(context, Mapping):
            raise InvalidBacktestPlugin(f"backtest plugin context must be a mapping: {descriptor.name}")
        port = self._factories[descriptor.name](dict(context))
        expected_port = _port_type(expected_kind)
        if not isinstance(port, expected_port):
            raise InvalidBacktestPlugin(
                f"backtest plugin {descriptor.name} returned {type(port).__name__} "
                f"without {_required_capability(expected_kind)}()"
            )
        missing = [
            capability
            for capability in descriptor.capabilities
            if not callable(getattr(port, capability, None))
        ]
        if missing:
            raise InvalidBacktestPlugin(
                f"backtest plugin {descriptor.name} is missing declared capabilities: "
                f"{','.join(missing)}"
            )
        return port

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._descriptors))

    def descriptors(self) -> tuple[BacktestPluginDescriptor, ...]:
        return tuple(self._descriptors[name] for name in self.names())

    def freeze(self) -> BacktestPluginRegistry:
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


def _required_capability(kind: str) -> str:
    return {
        SIGNAL_EVIDENCE_KIND: "evaluate",
        HISTORICAL_STRATEGY_KIND: "run",
        STRATEGY_SHADOW_KIND: "replay",
    }[kind]


def _port_type(kind: str) -> type:
    return {
        SIGNAL_EVIDENCE_KIND: SignalBacktestPort,
        HISTORICAL_STRATEGY_KIND: HistoricalStrategyBacktestPort,
        STRATEGY_SHADOW_KIND: StrategyShadowReplayPort,
    }[kind]


def _callable_name(factory: Any) -> str:
    module = str(getattr(factory, "__module__", "") or "")
    qualname = str(getattr(factory, "__qualname__", getattr(factory, "__name__", "")) or "")
    return ".".join(part for part in (module, qualname) if part) or type(factory).__name__
