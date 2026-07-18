"""Provider-free read-only contract for broker accounting projections."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from schemas.accounting import AccountingSnapshot


BROKER_ACCOUNTING_PLUGIN_SCHEMA = "broker-accounting-plugin-v1"
BROKER_ACCOUNTING_CAPABILITIES = ("project",)


@runtime_checkable
class BrokerAccountingProjectionPort(Protocol):
    name: str
    source_names: tuple[str, ...]

    def project(self, source: Mapping[str, Any]) -> AccountingSnapshot:
        ...


class BrokerAccountingAdapterFactory(Protocol):
    def __call__(self) -> BrokerAccountingProjectionPort:
        ...


@dataclass(frozen=True)
class BrokerAccountingPluginDescriptor:
    name: str
    implementation: str
    source_names: tuple[str, ...]
    source_schema_version: str
    default_currency: str
    capabilities: tuple[str, ...] = BROKER_ACCOUNTING_CAPABILITIES
    read_only: bool = True

    def public_dict(self) -> dict[str, Any]:
        return {
            "schema_version": BROKER_ACCOUNTING_PLUGIN_SCHEMA,
            "name": self.name,
            "implementation": self.implementation,
            "source_names": list(self.source_names),
            "source_schema_version": self.source_schema_version,
            "default_currency": self.default_currency,
            "capabilities": list(self.capabilities),
            "read_only": self.read_only,
        }
