"""Engine-neutral broker execution contracts.

The application owns normalized intent. Venue adapters own translation,
network I/O, lifecycle interpretation, and reconciliation with external truth.
Capabilities are declared from normalized provider identity and never inferred
from inherited method presence.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from schemas.market_data import PaperOrder


BROKER_PORT_DESCRIPTOR_SCHEMA = "broker-port-descriptor-v1"


@dataclass(frozen=True)
class BrokerOrderRequest:
    run_date: str
    ticket: dict
    latest_price: float | None = None
    actual_size: float | None = None

    def __post_init__(self) -> None:
        if not str(self.run_date).strip():
            raise ValueError("broker order run_date is required")
        if not isinstance(self.ticket, dict) or not str(self.ticket.get("ticket_id") or "").strip():
            raise ValueError("broker order ticket with ticket_id is required")


@dataclass(frozen=True)
class BrokerCancelRequest:
    run_date: str
    asset: str
    client_order_id: str = ""
    broker_order_id: str = ""

    def __post_init__(self) -> None:
        if not str(self.run_date).strip():
            raise ValueError("broker cancel run_date is required")
        if not str(self.asset).strip():
            raise ValueError("broker cancel asset is required")
        if not str(self.client_order_id).strip() and not str(self.broker_order_id).strip():
            raise ValueError("broker cancel order identity is required")


@dataclass(frozen=True)
class BrokerProtectiveRecoveryRequest:
    run_date: str
    lifecycle_record: dict
    exchange_position: dict | None = None
    source: str = "order_recovery"

    def __post_init__(self) -> None:
        if not str(self.run_date).strip():
            raise ValueError("broker protective recovery run_date is required")
        if not isinstance(self.lifecycle_record, dict) or not str(self.lifecycle_record.get("order_id") or "").strip():
            raise ValueError("broker protective recovery lifecycle_record with order_id is required")
        if not str(self.source).strip():
            raise ValueError("broker protective recovery source is required")


class BrokerCapability(str, Enum):
    PREFLIGHT = "preflight"
    SUBMIT_ORDER = "submit_order"
    CANCEL_ORDER = "cancel_order"
    REPLACE_ORDER = "replace_order"
    QUERY_ORDER = "query_order"
    OPEN_ORDERS = "open_orders"
    ORDER_FILL = "order_fill"
    ORDER_RECONCILIATION = "order_reconciliation"
    PROTECTIVE_RECOVERY = "protective_recovery"
    RECONCILIATION = "reconciliation"


@dataclass(frozen=True)
class BrokerCapabilities:
    values: frozenset[BrokerCapability | str]

    def __post_init__(self) -> None:
        normalized: set[BrokerCapability] = set()
        for value in self.values:
            try:
                normalized.add(value if isinstance(value, BrokerCapability) else BrokerCapability(str(value)))
            except ValueError as exc:
                raise ValueError(f"unknown broker capability: {value}") from exc
        object.__setattr__(self, "values", frozenset(normalized))

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(value.value for value in self.values))

    def supports(self, capability: BrokerCapability | str) -> bool:
        try:
            normalized = capability if isinstance(capability, BrokerCapability) else BrokerCapability(str(capability))
        except ValueError:
            return False
        return normalized in self.values

    def merged(self, *capabilities: BrokerCapability) -> BrokerCapabilities:
        return BrokerCapabilities(frozenset({*self.values, *capabilities}))


_BASE_EXECUTION_CAPABILITIES = BrokerCapabilities(
    frozenset({BrokerCapability.PREFLIGHT, BrokerCapability.SUBMIT_ORDER})
)


def execution_capabilities_for(*, provider: str, adapter_name: str = "") -> BrokerCapabilities:
    """Return fail-closed execution capabilities for a normalized provider.

    ``adapter_name`` is retained in the signature for auditability and future
    adapter-specific capabilities, but inherited methods never grant access.
    The Binance lineage is currently the only implementation of cancel and
    missing-protection recovery.
    """

    del adapter_name
    normalized_provider = str(provider or "").strip().lower()
    if normalized_provider == "binance_usdm":
        return _BASE_EXECUTION_CAPABILITIES.merged(
            BrokerCapability.CANCEL_ORDER,
            BrokerCapability.PROTECTIVE_RECOVERY,
        )
    return _BASE_EXECUTION_CAPABILITIES


class UnsupportedBrokerCapability(RuntimeError):
    pass


def require_broker_capability(adapter: Any, capability: BrokerCapability | str) -> None:
    declared = getattr(adapter, "capabilities", None)
    if not isinstance(declared, BrokerCapabilities) or not declared.supports(capability):
        name = capability.value if isinstance(capability, BrokerCapability) else str(capability)
        provider = str(getattr(adapter, "provider", "") or getattr(adapter, "name", "unknown"))
        raise UnsupportedBrokerCapability(f"broker provider {provider} does not support {name}")


@dataclass(frozen=True)
class BrokerPortDescriptor:
    adapter_name: str
    provider: str
    environment: str
    capabilities: tuple[str, ...]
    credential_env_names: tuple[str, ...]
    schema_version: str = BROKER_PORT_DESCRIPTOR_SCHEMA

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "adapter_name": self.adapter_name,
            "provider": self.provider,
            "environment": self.environment,
            "capabilities": list(self.capabilities),
            "credential_env_names": list(self.credential_env_names),
        }


_CREDENTIAL_ENV_CONFIG_KEYS = (
    "account_id_env",
    "api_key_env",
    "api_secret_env",
    "props_path_env",
    "secret_key_env",
    "token_env",
)


def broker_port_descriptor(adapter: Any) -> BrokerPortDescriptor:
    broker_config = getattr(adapter, "broker_config", {})
    config = broker_config if isinstance(broker_config, dict) else {}
    capabilities = getattr(adapter, "capabilities", None)
    if not isinstance(capabilities, BrokerCapabilities):
        capabilities = execution_capabilities_for(
            provider=str(getattr(adapter, "provider", "") or ""),
            adapter_name=str(getattr(adapter, "name", "") or ""),
        )
    env_names = tuple(
        sorted(
            {
                str(config[key]).strip()
                for key in _CREDENTIAL_ENV_CONFIG_KEYS
                if str(config.get(key) or "").strip()
            }
        )
    )
    return BrokerPortDescriptor(
        adapter_name=str(getattr(adapter, "name", "") or adapter.__class__.__name__),
        provider=str(getattr(adapter, "provider", "") or getattr(adapter, "name", "unknown")),
        environment=str(config.get("environment") or ("paper" if getattr(adapter, "name", "") == "paper" else "")),
        capabilities=capabilities.names,
        credential_env_names=env_names,
    )


@runtime_checkable
class BrokerExecutionPort(Protocol):
    name: str
    provider: str

    @property
    def capabilities(self) -> BrokerCapabilities:
        ...

    @property
    def descriptor(self) -> BrokerPortDescriptor:
        ...

    def preflight(self) -> dict:
        ...

    def submit_order(self, request: BrokerOrderRequest) -> PaperOrder:
        ...


@runtime_checkable
class BrokerReconciliationPort(Protocol):
    def run(self, run_date: str) -> dict:
        ...
