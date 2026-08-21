"""Provider-neutral host composition and Recording Track contract."""

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .account import AccountSnapshot, LiquidationFact
from .capabilities import CapabilityDescriptor
from .errors import BrokerError
from .fees import FeeEvent, FillFact, FundingPayment
from .market_data import MarketDataEnvelope
from .models import BrokerEnvironment, BrokerIdentity, Provenance
from .orders import OrderFill, OrderReceipt
from .paper import PaperPreflight, PaperReceipt


class HostRole(str, Enum):
    AUTHORITATIVE = "authoritative"
    SHADOW = "shadow"
    DIAGNOSTIC = "diagnostic"


@dataclass(frozen=True)
class BrokerSelection:
    broker_id: str
    environment: BrokerEnvironment
    role: HostRole


@dataclass(frozen=True)
class HostSafetyPolicy:
    paper_only: bool = True
    live_enabled: bool = False
    control_plane: str = "telegram"

    def __post_init__(self) -> None:
        if not self.paper_only:
            raise BrokerError("host policy is Paper-only")
        if self.live_enabled:
            raise BrokerError("live authority is outside the host contract")
        if self.control_plane != "telegram":
            raise BrokerError("Telegram is the only supported control plane")


@dataclass(frozen=True)
class HostBrokerBinding:
    selection: BrokerSelection
    identity: BrokerIdentity
    capabilities: CapabilityDescriptor
    adapter: object
    paper_only: bool
    real_money_eligible: bool
    control_plane: str


@dataclass(frozen=True)
class RecordingEvent:
    event_id: str
    kind: str
    payload: Any
    provenance: Provenance


_CANONICAL_RECORDING_TYPES = (
    AccountSnapshot,
    FeeEvent,
    FillFact,
    FundingPayment,
    LiquidationFact,
    MarketDataEnvelope,
    OrderFill,
    OrderReceipt,
    PaperPreflight,
    PaperReceipt,
)


class RecordingTrack:
    """In-memory canonical Recording Track sink for the host contract."""

    def __init__(self) -> None:
        self._events: list[RecordingEvent] = []

    @property
    def events(self) -> tuple[RecordingEvent, ...]:
        return tuple(self._events)

    def record(self, event: RecordingEvent) -> None:
        if not event.event_id or not event.kind:
            raise BrokerError("Recording Event requires event_id and kind")
        if not isinstance(event.provenance, Provenance):
            raise BrokerError("Recording Event requires canonical provenance")
        if not isinstance(event.payload, _CANONICAL_RECORDING_TYPES):
            raise BrokerError("Recording Track accepts canonical facts, not raw provider payloads")
        self._events.append(event)


@dataclass(frozen=True)
class _RegistryEntry:
    selection: BrokerSelection
    factory: Callable[[], object]
    capabilities: CapabilityDescriptor


class BrokerRegistry:
    """Frozen exact-match Broker composition registry."""

    def __init__(self) -> None:
        self._entries: dict[BrokerSelection, _RegistryEntry] = {}
        self._frozen = False

    def register(
        self,
        selection: BrokerSelection,
        *,
        factory: Callable[[], object],
        capabilities: CapabilityDescriptor,
    ) -> None:
        if self._frozen:
            raise BrokerError("Broker registry is frozen")
        if selection in self._entries:
            raise BrokerError(f"duplicate Broker selection: {selection}")
        if capabilities.broker_id != selection.broker_id:
            raise BrokerError("registry capability broker_id does not match selection")
        if capabilities.environment is not selection.environment:
            raise BrokerError("registry capability environment does not match selection")
        self._entries[selection] = _RegistryEntry(selection, factory, capabilities)

    def freeze(self) -> None:
        self._frozen = True

    @property
    def frozen(self) -> bool:
        return self._frozen

    def resolve(self, selection: BrokerSelection, *, policy: HostSafetyPolicy) -> HostBrokerBinding:
        if not self._frozen:
            raise BrokerError("Broker registry must be frozen before resolution")
        if selection.environment is not BrokerEnvironment.PAPER:
            raise BrokerError("only Paper Broker selections are enabled by this host contract")
        entry = self._entries.get(selection)
        if entry is None:
            raise BrokerError(f"unsupported Broker selection: {selection}")
        adapter = entry.factory()
        identity = getattr(adapter, "identity", None)
        capabilities = getattr(adapter, "capabilities", None)
        if not isinstance(identity, BrokerIdentity) or not isinstance(capabilities, CapabilityDescriptor):
            raise BrokerError("Broker factory did not return a canonical adapter")
        if identity.broker_id != selection.broker_id or identity.environment is not selection.environment:
            raise BrokerError("Broker factory identity does not match selection")
        if capabilities != entry.capabilities:
            raise BrokerError("Broker factory capabilities do not match registry declaration")
        preflight = getattr(adapter, "preflight", None)
        if not callable(preflight):
            raise BrokerError("Broker adapter has no preflight contract")
        facts = preflight()
        if not isinstance(facts, PaperPreflight) or facts.network_io or facts.real_money_eligible:
            raise BrokerError("Broker factory is not Paper-safe")
        return HostBrokerBinding(
            selection=selection,
            identity=identity,
            capabilities=capabilities,
            adapter=adapter,
            paper_only=True,
            real_money_eligible=False,
            control_plane=policy.control_plane,
        )
