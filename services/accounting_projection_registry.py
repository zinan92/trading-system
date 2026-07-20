"""Explicit frozen registry for read-only broker accounting adapters."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from typing import Any

from schemas.accounting import AccountingSnapshot
from services.accounting_projection_port import (
    BROKER_ACCOUNTING_CAPABILITIES,
    BROKER_ACCOUNTING_PLUGIN_SCHEMA,
    BrokerAccountingAdapterFactory,
    BrokerAccountingPluginDescriptor,
    BrokerAccountingProjectionPort,
)


class BrokerAccountingPluginRegistry:
    def __init__(self) -> None:
        self._entries: dict[
            str,
            tuple[BrokerAccountingPluginDescriptor, BrokerAccountingAdapterFactory],
        ] = {}
        self._sources: dict[str, str] = {}
        self._frozen = False
        self._fingerprint = ""

    def register(
        self,
        descriptor: BrokerAccountingPluginDescriptor,
        factory: BrokerAccountingAdapterFactory,
    ) -> BrokerAccountingPluginRegistry:
        if self._frozen:
            raise RuntimeError("broker accounting plugin registry is frozen")
        self._validate_descriptor(descriptor)
        if not callable(factory):
            raise TypeError(f"broker accounting plugin factory is not callable: {descriptor.name}")
        if descriptor.name in self._entries:
            raise ValueError(f"duplicate broker accounting plugin: {descriptor.name}")
        collisions = sorted(set(descriptor.source_names) & set(self._sources))
        if collisions:
            raise ValueError(f"duplicate broker accounting source: {','.join(collisions)}")
        self._entries[descriptor.name] = (descriptor, factory)
        for source_name in descriptor.source_names:
            self._sources[source_name] = descriptor.name
        return self

    def freeze(self) -> BrokerAccountingPluginRegistry:
        if not self._entries:
            raise ValueError("broker accounting plugin registry cannot be empty")
        payload = {
            "schema_version": BROKER_ACCOUNTING_PLUGIN_SCHEMA,
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
        self._require_frozen()
        return self._fingerprint

    def descriptors(self) -> Iterator[BrokerAccountingPluginDescriptor]:
        self._require_frozen()
        for name in sorted(self._entries):
            yield self._entries[name][0]

    def descriptor_for_source(self, source_name: str) -> BrokerAccountingPluginDescriptor:
        self._require_frozen()
        normalized = str(source_name or "").strip().lower()
        plugin_name = self._sources.get(normalized)
        if plugin_name is None:
            raise KeyError(normalized)
        return self._entries[plugin_name][0]

    def project(self, source_name: str, source: Mapping[str, Any]) -> AccountingSnapshot:
        descriptor = self.descriptor_for_source(source_name)
        adapter = self._entries[descriptor.name][1]()
        if not isinstance(adapter, BrokerAccountingProjectionPort):
            raise TypeError(f"broker accounting plugin returned an invalid adapter: {descriptor.name}")
        implementation = f"{adapter.__class__.__module__}.{adapter.__class__.__qualname__}"
        if implementation != descriptor.implementation:
            raise TypeError(
                f"broker accounting plugin implementation mismatch: "
                f"expected {descriptor.implementation}, got {implementation}"
            )
        if adapter.name != descriptor.name or tuple(adapter.source_names) != descriptor.source_names:
            raise TypeError(f"broker accounting plugin identity mismatch: {descriptor.name}")
        missing = [
            capability
            for capability in descriptor.capabilities
            if not callable(getattr(adapter, capability, None))
        ]
        if missing:
            raise TypeError(
                f"broker accounting plugin {descriptor.name} is missing capabilities: "
                f"{','.join(missing)}"
            )
        snapshot = adapter.project(source)
        if not isinstance(snapshot, AccountingSnapshot):
            raise TypeError(f"broker accounting plugin returned an invalid snapshot: {descriptor.name}")
        normalized = str(source_name or "").strip().lower()
        if snapshot.source_type != "broker_reconciliation":
            raise ValueError(f"broker accounting plugin returned wrong source_type: {descriptor.name}")
        if snapshot.source_name != normalized:
            raise ValueError(f"broker accounting plugin returned wrong source_name: {descriptor.name}")
        if snapshot.source_schema_version != descriptor.source_schema_version:
            raise ValueError(f"broker accounting plugin returned wrong source schema: {descriptor.name}")
        return snapshot

    def _require_frozen(self) -> None:
        if not self._frozen:
            raise RuntimeError("broker accounting plugin registry must be frozen before use")

    @staticmethod
    def _validate_descriptor(descriptor: BrokerAccountingPluginDescriptor) -> None:
        name = str(descriptor.name or "")
        if (
            not name
            or name != name.strip().lower()
            or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in name)
        ):
            raise ValueError("broker accounting plugin name is invalid")
        if not str(descriptor.implementation or "").strip():
            raise ValueError(f"broker accounting plugin implementation is required: {name}")
        sources = tuple(descriptor.source_names)
        if not sources or len(set(sources)) != len(sources):
            raise ValueError(f"broker accounting plugin source names are invalid: {name}")
        for source_name in sources:
            if (
                not source_name
                or source_name != source_name.strip().lower()
                or any(
                    character not in "abcdefghijklmnopqrstuvwxyz0123456789_-"
                    for character in source_name
                )
            ):
                raise ValueError(f"broker accounting source name is invalid: {source_name}")
        if not str(descriptor.source_schema_version or "").strip():
            raise ValueError(f"broker accounting source schema is required: {name}")
        if not str(descriptor.default_currency or "").strip():
            raise ValueError(f"broker accounting default currency is required: {name}")
        if descriptor.read_only is not True:
            raise ValueError(f"broker accounting plugin must be read-only: {name}")
        capabilities = tuple(descriptor.capabilities)
        if capabilities != BROKER_ACCOUNTING_CAPABILITIES:
            raise ValueError(f"broker accounting plugin capabilities are invalid: {name}")
