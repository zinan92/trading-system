"""Canonical broker contracts and Paper-safe adapter foundations."""

from .account import AccountSnapshot, LiquidationFact, PositionFact, PositionSide
from .capabilities import PORT_NAMES, CapabilityDescriptor
from .conformance import ConformanceReport, EvidenceKind, run_paper_conformance
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
from .orders import (
    OrderFill,
    OrderIntent,
    OrderReceipt,
    OrderSide,
    OrderState,
    OrderType,
    TimeInForce,
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
from .protection import (
    ProtectionExecution,
    ProtectionGroup,
    ProtectionLeg,
    ProtectionQuantityPolicy,
    ProtectionType,
    TriggerReference,
)
from .security import find_secret_like_literals

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
    "ConformanceReport",
    "EvidenceKind",
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
    "OrderFill",
    "OrderIntent",
    "OrderReceipt",
    "OrderSide",
    "OrderState",
    "OrderType",
    "PaperBoundaryError",
    "PaperBrokerAdapter",
    "PaperPreflight",
    "PaperReceipt",
    "PositionFact",
    "PositionSide",
    "ProtectionExecution",
    "ProtectionGroup",
    "ProtectionLeg",
    "ProtectionOrderPort",
    "ProtectionQuantityPolicy",
    "ProtectionType",
    "Provenance",
    "SignerKind",
    "TimeInForce",
    "TriggerReference",
    "find_secret_like_literals",
    "run_paper_conformance",
]
