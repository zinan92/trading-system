"""Explicit frozen registry for normalized risk policy evaluators."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from services.risk_port import RiskDecisionPort


RISK_POLICY_PLUGIN_SCHEMA = "risk-policy-plugin-v1"
RISK_POLICY_CAPABILITIES = (
    "evaluate",
    "evaluator_metadata",
    "resolve_policy",
)
RiskPolicyFactory = Callable[[], RiskDecisionPort]


class RiskPolicyPluginError(RuntimeError):
    pass


class UnknownRiskPolicyPlugin(RiskPolicyPluginError):
    pass


class DuplicateRiskPolicyPlugin(RiskPolicyPluginError):
    pass


class InvalidRiskPolicyPlugin(RiskPolicyPluginError):
    pass


@dataclass(frozen=True)
class RiskPolicyPluginDescriptor:
    name: str
    implementation: str
    evaluator: Mapping[str, Any]
    capabilities: tuple[str, ...] = RISK_POLICY_CAPABILITIES
    schema_version: str = RISK_POLICY_PLUGIN_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "implementation": self.implementation,
            "evaluator": _thaw(self.evaluator),
            "capabilities": list(self.capabilities),
        }


def normalize_risk_policy_plugin_name(value: Any) -> str:
    return str(value or "").strip().lower()


class RiskPolicyPluginRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, RiskPolicyFactory] = {}
        self._descriptors: dict[str, RiskPolicyPluginDescriptor] = {}
        self._frozen = False
        self._fingerprint = ""

    def register(
        self,
        name: str,
        factory: RiskPolicyFactory,
        *,
        implementation: str = "",
        evaluator: Mapping[str, Any],
        capabilities: Iterable[str] = RISK_POLICY_CAPABILITIES,
    ) -> RiskPolicyPluginRegistry:
        if self._frozen:
            raise InvalidRiskPolicyPlugin("risk policy plugin registry is frozen")
        normalized = normalize_risk_policy_plugin_name(name)
        if not normalized:
            raise InvalidRiskPolicyPlugin("risk policy plugin name is required")
        if normalized in self._factories:
            raise DuplicateRiskPolicyPlugin(
                f"risk policy plugin already registered: {normalized}"
            )
        if not callable(factory):
            raise InvalidRiskPolicyPlugin(
                f"risk policy plugin factory is not callable: {normalized}"
            )
        resolved_evaluator = _validated_evaluator(evaluator, normalized)
        resolved_capabilities = tuple(
            sorted(
                {
                    str(item).strip()
                    for item in capabilities
                    if str(item).strip()
                }
            )
        )
        if resolved_capabilities != tuple(sorted(RISK_POLICY_CAPABILITIES)):
            raise InvalidRiskPolicyPlugin(
                f"risk policy plugin capabilities are invalid: {normalized}"
            )
        descriptor = RiskPolicyPluginDescriptor(
            name=normalized,
            implementation=implementation.strip() or _callable_name(factory),
            evaluator=_freeze(resolved_evaluator),
            capabilities=resolved_capabilities,
        )
        self._factories[normalized] = factory
        self._descriptors[normalized] = descriptor
        return self

    def freeze(self) -> RiskPolicyPluginRegistry:
        if not self._descriptors:
            raise InvalidRiskPolicyPlugin("risk policy plugin registry cannot be empty")
        payload = [
            self._descriptors[name].to_dict()
            for name in sorted(self._descriptors)
        ]
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self._fingerprint = f"sha256:{hashlib.sha256(encoded).hexdigest()}"
        self._frozen = True
        return self

    def descriptor(self, name: str) -> RiskPolicyPluginDescriptor:
        self._require_frozen()
        normalized = normalize_risk_policy_plugin_name(name)
        descriptor = self._descriptors.get(normalized)
        if descriptor is None:
            raise UnknownRiskPolicyPlugin(
                f"unknown risk policy plugin: {normalized or '<empty>'}; "
                f"registered={','.join(self.names()) or '<none>'}"
            )
        return descriptor

    def build(self, name: str) -> RiskDecisionPort:
        descriptor = self.descriptor(name)
        port = self._factories[descriptor.name]()
        if not isinstance(port, RiskDecisionPort):
            raise InvalidRiskPolicyPlugin(
                f"risk policy plugin {descriptor.name} returned "
                f"{type(port).__name__} without the complete port contract"
            )
        if str(port.name or "").strip().lower() != descriptor.name:
            raise InvalidRiskPolicyPlugin(
                f"risk policy plugin identity mismatch: {descriptor.name}"
            )
        implementation = (
            f"{port.__class__.__module__}.{port.__class__.__qualname__}"
        )
        if implementation != descriptor.implementation:
            raise InvalidRiskPolicyPlugin(
                "risk policy plugin implementation mismatch: "
                f"expected {descriptor.implementation}, got {implementation}"
            )
        missing = [
            capability
            for capability in descriptor.capabilities
            if not callable(getattr(port, capability, None))
        ]
        if missing:
            raise InvalidRiskPolicyPlugin(
                f"risk policy plugin {descriptor.name} is missing capabilities: "
                f"{','.join(missing)}"
            )
        actual_evaluator = _validated_evaluator(
            port.evaluator_metadata(),
            descriptor.name,
        )
        if actual_evaluator != descriptor.to_dict()["evaluator"]:
            raise InvalidRiskPolicyPlugin(
                f"risk policy plugin evaluator identity mismatch: {descriptor.name}"
            )
        return port

    def names(self) -> tuple[str, ...]:
        self._require_frozen()
        return tuple(sorted(self._descriptors))

    def descriptors(self) -> tuple[RiskPolicyPluginDescriptor, ...]:
        self._require_frozen()
        return tuple(self._descriptors[name] for name in sorted(self._descriptors))

    @property
    def frozen(self) -> bool:
        return self._frozen

    @property
    def fingerprint(self) -> str:
        self._require_frozen()
        return self._fingerprint

    def _require_frozen(self) -> None:
        if not self._frozen:
            raise InvalidRiskPolicyPlugin(
                "risk policy plugin registry must be frozen before use"
            )


def _validated_evaluator(
    evaluator: Mapping[str, Any],
    plugin_name: str,
) -> dict[str, Any]:
    if not isinstance(evaluator, Mapping):
        raise InvalidRiskPolicyPlugin(
            f"risk policy plugin evaluator must be a mapping: {plugin_name}"
        )
    payload = _thaw(evaluator)
    for field in ("name", "version", "code_sha256"):
        if not str(payload.get(field) or "").strip():
            raise InvalidRiskPolicyPlugin(
                f"risk policy plugin evaluator {field} is required: {plugin_name}"
            )
    if str(payload["name"]).strip().lower() != plugin_name:
        raise InvalidRiskPolicyPlugin(
            f"risk policy plugin evaluator name mismatch: {plugin_name}"
        )
    source_hashes = payload.get("source_hashes")
    if not isinstance(source_hashes, dict) or not source_hashes:
        raise InvalidRiskPolicyPlugin(
            f"risk policy plugin evaluator source hashes are required: {plugin_name}"
        )
    return payload


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def _callable_name(factory: Any) -> str:
    module = str(getattr(factory, "__module__", "") or "")
    qualname = str(
        getattr(factory, "__qualname__", getattr(factory, "__name__", ""))
        or ""
    )
    return ".".join(part for part in (module, qualname) if part) or type(
        factory
    ).__name__
