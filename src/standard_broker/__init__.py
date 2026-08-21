"""Canonical broker contracts and Paper-safe adapter foundations."""

from .account import AccountSnapshot, LiquidationFact, PositionFact, PositionSide
from .capabilities import PORT_NAMES, CapabilityDescriptor
from .errors import BrokerCapabilityError, BrokerError, PaperBoundaryError
from .fees import (
    FeeEvent,
    FeeKind,
    FeeScheduleSnapshot,
    FeeSource,
    FeeState,
    FillFact,
    FundingPayment,
)
from .models import (
    AccountScope,
    BrokerEnvironment,
    BrokerIdentity,
    Provenance,
    SignerKind,
)
from .paper import (
    InMemoryPaperTransport,
    PaperBrokerAdapter,
    PaperPreflight,
    PaperReceipt,
)
from .ports import (
    AccountPort,
    FeePort,
    InstrumentPort,
    MarketDataPort,
    OrderExecutionPort,
    ProtectionOrderPort,
)

__all__ = [
    "PORT_NAMES",
    "AccountPort",
    "AccountScope",
    "AccountSnapshot",
    "BrokerCapabilityError",
    "BrokerEnvironment",
    "BrokerError",
    "BrokerIdentity",
    "CapabilityDescriptor",
    "FeeEvent",
    "FeeKind",
    "FeePort",
    "FeeScheduleSnapshot",
    "FeeSource",
    "FeeState",
    "FillFact",
    "FundingPayment",
    "InMemoryPaperTransport",
    "InstrumentPort",
    "LiquidationFact",
    "MarketDataPort",
    "OrderExecutionPort",
    "PaperBoundaryError",
    "PaperBrokerAdapter",
    "PaperPreflight",
    "PaperReceipt",
    "PositionFact",
    "PositionSide",
    "ProtectionOrderPort",
    "Provenance",
    "SignerKind",
]
