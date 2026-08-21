"""Frozen capability declarations for the canonical Broker boundary."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .errors import BrokerCapabilityError
from .models import BrokerEnvironment

PORT_NAMES = (
    "market_data",
    "instrument",
    "account",
    "order_execution",
    "protection_order",
    "fee",
)
_META_PORTS = frozenset({"preflight"})


@dataclass(frozen=True)
class CapabilityDescriptor:
    """Immutable operations a Broker composition explicitly supports."""

    broker_id: str
    environment: BrokerEnvironment
    operations: Mapping[str, frozenset[str]]
    revision: str

    def __post_init__(self) -> None:
        if not self.broker_id or self.broker_id != self.broker_id.strip():
            raise ValueError("capability broker_id must be a non-empty trimmed string")
        if not isinstance(self.environment, BrokerEnvironment):
            raise TypeError("capability environment must be a BrokerEnvironment")
        if not self.revision or self.revision != self.revision.strip():
            raise ValueError("capability revision must be a non-empty trimmed string")

        normalized: dict[str, frozenset[str]] = {}
        for port, operations in self.operations.items():
            if port not in PORT_NAMES and port not in _META_PORTS:
                raise ValueError(f"unknown capability port: {port}")
            if not isinstance(port, str) or not port.strip():
                raise ValueError("capability port must be a non-empty string")
            normalized[port] = frozenset(str(operation) for operation in operations)
        object.__setattr__(self, "operations", MappingProxyType(normalized))

    def supports(self, port: str, operation: str) -> bool:
        """Return whether one exact canonical operation is declared."""

        return operation in self.operations.get(port, frozenset())

    def require(self, port: str, operation: str) -> None:
        """Raise before transport when a capability is absent."""

        if not self.supports(port, operation):
            raise BrokerCapabilityError(port, operation)
