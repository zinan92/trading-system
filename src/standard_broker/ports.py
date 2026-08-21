"""The six provider-neutral canonical port markers."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class CanonicalPort(Protocol):
    """Common observable identity of a canonical port."""

    @property
    def name(self) -> str:
        ...


class MarketDataPort(CanonicalPort, Protocol):
    """Canonical market observations."""


class InstrumentPort(CanonicalPort, Protocol):
    """Canonical instrument and trading-rule observations."""


class AccountPort(CanonicalPort, Protocol):
    """Canonical account, position, and margin observations."""


class OrderExecutionPort(CanonicalPort, Protocol):
    """Canonical order and execution lifecycle operations."""


class ProtectionOrderPort(CanonicalPort, Protocol):
    """Canonical protective-order operations and semantics."""


class FeePort(CanonicalPort, Protocol):
    """Canonical fee, rebate, and funding observations."""
