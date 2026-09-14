"""Local Paper implementation of the canonical Broker seam."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .capabilities import PORT_NAMES, CapabilityDescriptor
from .errors import PaperBoundaryError
from .models import BrokerEnvironment, BrokerIdentity, Provenance, SignerKind


@dataclass(frozen=True)
class _PortMarker:
    name: str


@dataclass(frozen=True)
class PaperPreflight:
    """Normalized local-Paper readiness facts."""

    broker_id: str
    environment: BrokerEnvironment
    network_io: bool
    real_money_eligible: bool
    credential_required: bool
    ports: tuple[str, ...]


@dataclass(frozen=True)
class PaperReceipt:
    """Normalized result for a local Paper contract call."""

    broker_id: str
    environment: BrokerEnvironment
    port: str
    operation: str
    accepted: bool
    network_io: bool
    real_money_eligible: bool
    provenance: Provenance


@dataclass(frozen=True)
class PaperTransportCall:
    port: str
    operation: str
    payload: object


class InMemoryPaperTransport:
    """A local-only transport that records calls without network access."""

    local_only = True

    def __init__(self) -> None:
        self.calls: list[PaperTransportCall] = []

    def request(self, port: str, operation: str, payload: object) -> object:
        self.calls.append(PaperTransportCall(port, operation, payload))
        return {"accepted": True}


class PaperBrokerAdapter:
    """The baseline local Paper Broker implementation."""

    def __init__(
        self,
        *,
        identity: BrokerIdentity,
        capabilities: CapabilityDescriptor,
        transport: object | None = None,
    ) -> None:
        self._validate_boundary(identity, capabilities)
        selected_transport = transport or InMemoryPaperTransport()
        if getattr(selected_transport, "local_only", False) is not True:
            raise PaperBoundaryError("Paper adapter requires a local-only transport")
        if not callable(getattr(selected_transport, "request", None)):
            raise TypeError("Paper transport must provide request(port, operation, payload)")

        self._identity = identity
        self._capabilities = capabilities
        self._transport = selected_transport
        self._ports = MappingProxyType({name: _PortMarker(name) for name in PORT_NAMES})

    @staticmethod
    def _validate_boundary(
        identity: BrokerIdentity,
        capabilities: CapabilityDescriptor,
    ) -> None:
        if identity.environment is not BrokerEnvironment.PAPER:
            raise PaperBoundaryError("Paper adapter only accepts the paper environment")
        if identity.signer_kind is not SignerKind.NONE:
            raise PaperBoundaryError("Paper adapter cannot accept a signer")
        if capabilities.broker_id != identity.broker_id:
            raise PaperBoundaryError("identity and capability broker_id do not match")
        if capabilities.environment is not identity.environment:
            raise PaperBoundaryError("identity and capability environment do not match")

    @property
    def identity(self) -> BrokerIdentity:
        return self._identity

    @property
    def capabilities(self) -> CapabilityDescriptor:
        return self._capabilities

    @property
    def ports(self) -> Mapping[str, _PortMarker]:
        return self._ports

    @property
    def transport(self) -> object:
        return self._transport

    def preflight(self) -> PaperPreflight:
        return PaperPreflight(
            broker_id=self._identity.broker_id,
            environment=self._identity.environment,
            network_io=False,
            real_money_eligible=False,
            credential_required=False,
            ports=PORT_NAMES,
        )

    def request(self, port: str, operation: str, payload: object = None) -> PaperReceipt:
        self._capabilities.require(port, operation)
        self._transport.request(port, operation, payload)
        return PaperReceipt(
            broker_id=self._identity.broker_id,
            environment=self._identity.environment,
            port=port,
            operation=operation,
            accepted=True,
            network_io=False,
            real_money_eligible=False,
            provenance=Provenance(
                source="paper_fixture",
                execution_scope=self._identity.execution_scope,
                transport_state="local_fixture",
                mapping_revision=self._capabilities.revision,
            ),
        )
