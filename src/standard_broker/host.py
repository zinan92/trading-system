"""Provider-neutral host composition and Recording Track contract."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Protocol, TypeAlias, runtime_checkable

from .account import AccountSnapshot, LiquidationFact, PositionFact
from .capabilities import PORT_NAMES, CapabilityDescriptor
from .errors import BrokerError
from .fees import FeeEvent, FillFact, FundingPayment
from .market_data import MarketDataEnvelope
from .models import BrokerEnvironment, BrokerIdentity, Provenance
from .orders import OrderFill, OrderIntent, OrderReceipt
from .paper import PaperPreflight, PaperReceipt
from .protection import ProtectionGroup, ProtectionReceipt


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


CanonicalPayload: TypeAlias = None | str | int | Decimal | OrderIntent | ProtectionGroup


@dataclass(frozen=True)
class CanonicalPortQuery:
    """Provider-neutral selector usable by any canonical read port."""

    subject: str | None = None
    kind: str | None = None

    def __post_init__(self) -> None:
        for name in ("subject", "kind"):
            value = getattr(self, name)
            if value is not None and (not value or value != value.strip()):
                raise BrokerError(f"canonical query {name} must be non-empty when provided")


@dataclass(frozen=True)
class CanonicalHostRequest:
    """Typed provider-neutral request accepted by the host composition seam."""

    port: str
    operation: str
    payload: CanonicalPayload | CanonicalPortQuery = None

    def __post_init__(self) -> None:
        for name in ("port", "operation"):
            value = getattr(self, name)
            if not value or value != value.strip():
                raise BrokerError(f"canonical request {name} is required")
        if self.port not in PORT_NAMES:
            raise BrokerError(f"unknown canonical port: {self.port}")
        if self.payload is not None and not isinstance(
            self.payload,
            (str, int, Decimal, OrderIntent, ProtectionGroup, CanonicalPortQuery),
        ):
            raise BrokerError("canonical request payload is not a supported provider-neutral type")


@runtime_checkable
class CanonicalAdapterReceipt(Protocol):
    """Provider-neutral receipt shape required at the host boundary."""

    broker_id: str
    environment: BrokerEnvironment
    port: str
    operation: str
    accepted: bool
    network_io: bool
    real_money_eligible: bool
    provenance: Provenance


@dataclass(frozen=True)
class CanonicalHostReceipt:
    broker_id: str
    environment: BrokerEnvironment
    port: str
    operation: str
    accepted: bool
    network_io: bool
    real_money_eligible: bool
    provenance: Provenance


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
    PositionFact,
    PaperPreflight,
    PaperReceipt,
    ProtectionReceipt,
    CanonicalHostReceipt,
)

_HOST_READ_OPERATIONS = frozenset(
    {"read", "query", "open_orders", "fills", "positions", "account", "fees", "funding", "preflight"}
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
        payload_provenance = getattr(event.payload, "provenance", None)
        if payload_provenance is not None and payload_provenance != event.provenance:
            raise BrokerError("Recording Event provenance does not match its canonical payload")
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
        if (
            not isinstance(facts, PaperPreflight)
            or facts.network_io
            or facts.real_money_eligible
            or facts.credential_required
            or facts.broker_id != selection.broker_id
            or facts.environment is not selection.environment
            or tuple(facts.ports) != PORT_NAMES
        ):
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


class CanonicalBrokerHost:
    """Resolve and invoke one explicit canonical Paper Broker binding."""

    def __init__(
        self,
        *,
        registry: BrokerRegistry,
        policy: HostSafetyPolicy,
        recording: RecordingTrack | None = None,
    ) -> None:
        self._registry = registry
        self._policy = policy
        self._recording = recording or RecordingTrack()
        self._resolved_bindings: dict[int, HostBrokerBinding] = {}

    @property
    def recording(self) -> RecordingTrack:
        return self._recording

    def resolve(self, selection: BrokerSelection) -> HostBrokerBinding:
        """Resolve exactly the requested Broker/environment/role selection."""

        binding = self._registry.resolve(selection, policy=self._policy)
        self._resolved_bindings[id(binding)] = binding
        return binding

    def invoke(
        self,
        binding: HostBrokerBinding,
        *,
        request: CanonicalHostRequest,
    ) -> CanonicalHostReceipt:
        """Invoke an adapter request and accept only a canonical Paper receipt."""

        if not isinstance(binding, HostBrokerBinding):
            raise BrokerError("host invocation requires an explicit HostBrokerBinding")
        if not isinstance(request, CanonicalHostRequest):
            raise BrokerError("host invocation requires a CanonicalHostRequest")
        if self._resolved_bindings.get(id(binding)) is not binding:
            raise BrokerError("host invocation binding was not resolved by this host")
        if binding.selection.environment is not BrokerEnvironment.PAPER:
            raise BrokerError("host contract is Paper-only")
        if binding.selection.role is not HostRole.AUTHORITATIVE:
            if request.operation not in _HOST_READ_OPERATIONS:
                raise BrokerError("non-authoritative host roles allow only known read operations")
        if (
            binding.identity.broker_id != binding.selection.broker_id
            or binding.identity.environment is not binding.selection.environment
            or binding.capabilities.broker_id != binding.selection.broker_id
            or binding.capabilities.environment is not binding.selection.environment
            or binding.paper_only is not True
            or binding.real_money_eligible is not False
            or binding.control_plane != self._policy.control_plane
        ):
            raise BrokerError("host binding identity or safety flags do not match the resolved selection")
        binding.capabilities.require(request.port, request.operation)
        adapter_request = getattr(binding.adapter, "request", None)
        if not callable(adapter_request):
            raise BrokerError("resolved Broker adapter has no canonical request()")
        receipt = adapter_request(request.port, request.operation, request.payload)
        if not isinstance(receipt, CanonicalAdapterReceipt):
            raise BrokerError("Broker adapter returned a non-canonical receipt")
        if (
            receipt.broker_id != binding.identity.broker_id
            or receipt.environment is not BrokerEnvironment.PAPER
            or receipt.port != request.port
            or receipt.operation != request.operation
            or receipt.network_io
            or receipt.real_money_eligible
            or receipt.provenance.execution_scope != binding.identity.execution_scope
            or receipt.provenance.mapping_revision != binding.capabilities.revision
            or receipt.provenance.transport_state != "local_fixture"
        ):
            raise BrokerError("Broker adapter returned a receipt with mismatched Paper identity or provenance")
        return CanonicalHostReceipt(
            broker_id=receipt.broker_id,
            environment=receipt.environment,
            port=receipt.port,
            operation=receipt.operation,
            accepted=receipt.accepted,
            network_io=receipt.network_io,
            real_money_eligible=receipt.real_money_eligible,
            provenance=receipt.provenance,
        )

    def record(self, event: RecordingEvent) -> None:
        """Record only a canonical event through the host-owned Recording Track."""

        self._recording.record(event)
