"""The six provider-neutral canonical port markers."""

from decimal import Decimal
from typing import Protocol, runtime_checkable

from .protection import (
    ProtectionGroup,
    ProtectionLifecycleStatus,
    ProtectionReceipt,
    ProtectionRetryPlan,
)


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

    def submit(self, group: ProtectionGroup) -> ProtectionReceipt:
        ...

    def cancel(self, group: ProtectionGroup) -> ProtectionReceipt:
        ...

    def replace(self, group: ProtectionGroup) -> ProtectionReceipt:
        ...

    def reconcile(self, group: ProtectionGroup) -> ProtectionReceipt:
        ...

    def repair_after_partial_fill(self, group: ProtectionGroup, *, filled_quantity: Decimal) -> ProtectionReceipt:
        ...

    def reconcile_position_coverage(
        self,
        group: ProtectionGroup,
        *,
        owned_quantity: Decimal,
    ) -> ProtectionReceipt | ProtectionLifecycleStatus:
        ...

    def status(self, protection_id: str) -> ProtectionLifecycleStatus:
        ...

    def retry(
        self,
        group: ProtectionGroup,
        *,
        attempt: int,
        delay_elapsed: bool = False,
    ) -> ProtectionReceipt:
        ...

    def retry_plan(self, group: ProtectionGroup, *, attempt: int) -> ProtectionRetryPlan:
        ...


class FeePort(CanonicalPort, Protocol):
    """Canonical fee, rebate, and funding observations."""
