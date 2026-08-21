"""Canonical broker contracts and Paper-safe adapter foundations."""

from .capabilities import PORT_NAMES, CapabilityDescriptor
from .errors import BrokerCapabilityError, BrokerError, PaperBoundaryError
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
    "BrokerCapabilityError",
    "BrokerEnvironment",
    "BrokerError",
    "BrokerIdentity",
    "CapabilityDescriptor",
    "FeePort",
    "InMemoryPaperTransport",
    "InstrumentPort",
    "MarketDataPort",
    "OrderExecutionPort",
    "PaperBoundaryError",
    "PaperBrokerAdapter",
    "PaperPreflight",
    "PaperReceipt",
    "ProtectionOrderPort",
    "Provenance",
    "SignerKind",
]
