"""Explicit, frozen registry for trusted execution-engine factories."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator

from services.execution_engine_port import (
    EXECUTION_ENGINE_CAPABILITIES,
    EXECUTION_ENGINE_PORT_SCHEMA,
    EXECUTION_ENGINE_ROLES,
    ExecutionEngineAdapter,
    ExecutionEngineBuildRequest,
    ExecutionEngineFactory,
    ExecutionEnginePluginDescriptor,
)


class ExecutionEnginePluginRegistry:
    """Fail-closed registry with deterministic metadata provenance."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[ExecutionEnginePluginDescriptor, ExecutionEngineFactory]] = {}
        self._frozen = False
        self._fingerprint = ""

    def register(
        self,
        descriptor: ExecutionEnginePluginDescriptor,
        factory: ExecutionEngineFactory,
    ) -> ExecutionEnginePluginRegistry:
        if self._frozen:
            raise RuntimeError("execution engine plugin registry is frozen")
        self._validate_descriptor(descriptor)
        if not callable(factory):
            raise TypeError(f"execution engine plugin factory is not callable: {descriptor.name}")
        if descriptor.name in self._entries:
            raise ValueError(f"duplicate execution engine plugin: {descriptor.name}")
        self._entries[descriptor.name] = (descriptor, factory)
        return self

    def freeze(self) -> ExecutionEnginePluginRegistry:
        if not self._entries:
            raise ValueError("execution engine plugin registry cannot be empty")
        payload = {
            "schema_version": EXECUTION_ENGINE_PORT_SCHEMA,
            "plugins": [
                self._entries[name][0].public_dict()
                for name in sorted(self._entries)
            ],
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self._fingerprint = f"sha256:{hashlib.sha256(encoded).hexdigest()}"
        self._frozen = True
        return self

    @property
    def fingerprint(self) -> str:
        if not self._frozen:
            raise RuntimeError("execution engine plugin registry must be frozen before use")
        return self._fingerprint

    def descriptors(self) -> Iterator[ExecutionEnginePluginDescriptor]:
        self._require_frozen()
        for name in sorted(self._entries):
            yield self._entries[name][0]

    def descriptor(self, name: str, *, role: str | None = None) -> ExecutionEnginePluginDescriptor:
        self._require_frozen()
        normalized = str(name or "").strip().lower()
        entry = self._entries.get(normalized)
        if entry is None:
            raise KeyError(normalized)
        descriptor = entry[0]
        if role is not None and role not in descriptor.roles:
            raise ValueError(f"execution engine plugin {normalized} does not support {role} role")
        return descriptor

    def build(self, name: str, request: ExecutionEngineBuildRequest) -> ExecutionEngineAdapter:
        descriptor = self.descriptor(name, role=request.role)
        adapter = self._entries[descriptor.name][1](request)
        if not isinstance(adapter, ExecutionEngineAdapter):
            raise TypeError(f"execution engine plugin returned an invalid adapter: {descriptor.name}")
        implementation = f"{adapter.__class__.__module__}.{adapter.__class__.__qualname__}"
        if implementation != descriptor.implementation:
            raise TypeError(
                f"execution engine plugin implementation mismatch: "
                f"expected {descriptor.implementation}, got {implementation}"
            )
        missing = [
            capability
            for capability in descriptor.capabilities
            if not callable(getattr(adapter, capability, None))
        ]
        if missing:
            raise TypeError(
                f"execution engine plugin {descriptor.name} is missing capabilities: {','.join(missing)}"
            )
        return adapter

    def _require_frozen(self) -> None:
        if not self._frozen:
            raise RuntimeError("execution engine plugin registry must be frozen before use")

    @staticmethod
    def _validate_descriptor(descriptor: ExecutionEnginePluginDescriptor) -> None:
        name = str(descriptor.name or "")
        if (
            not name
            or name != name.strip().lower()
            or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in name)
        ):
            raise ValueError("execution engine plugin name is invalid")
        if not str(descriptor.implementation or "").strip():
            raise ValueError(f"execution engine plugin implementation is required: {name}")
        roles = set(descriptor.roles)
        if not roles or not roles.issubset(EXECUTION_ENGINE_ROLES):
            raise ValueError(f"execution engine plugin roles are invalid: {name}")
        if len(roles) != len(descriptor.roles):
            raise ValueError(f"execution engine plugin roles contain duplicates: {name}")
        if descriptor.paper_only is not True or descriptor.real_money_eligible is not False:
            raise ValueError(f"execution engine plugin must be paper-only: {name}")
        capabilities = tuple(descriptor.capabilities)
        if not capabilities or len(set(capabilities)) != len(capabilities):
            raise ValueError(f"execution engine plugin capabilities are invalid: {name}")
        missing = sorted(set(EXECUTION_ENGINE_CAPABILITIES) - set(capabilities))
        if missing:
            raise ValueError(
                f"execution engine plugin {name} omits required capabilities: {','.join(missing)}"
            )
        if "wrapper" in roles and "flush_shadow" not in capabilities:
            raise ValueError(f"execution engine wrapper plugin must declare flush_shadow: {name}")
