"""Paper-safe compatibility boundary for the Nautilus Hyperliquid adapter."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ...capabilities import PORT_NAMES, CapabilityDescriptor
from ...errors import BrokerCapabilityError, BrokerError
from ...models import BrokerEnvironment, BrokerIdentity, Provenance, SignerKind
from ...paper import PaperPreflight


class NautilusCompatibilityError(BrokerError):
    """Raised when the injected Nautilus adapter does not match the contract."""


@dataclass(frozen=True)
class NautilusAdapterMetadata:
    package: str
    version: str
    commit: str
    capabilities: CapabilityDescriptor


@dataclass(frozen=True)
class NautilusBridgeConfig:
    expected_version: str
    expected_commit: str
    account_address: str | None
    signer_kind: SignerKind = SignerKind.NONE
    environment: BrokerEnvironment = BrokerEnvironment.PAPER

    def __post_init__(self) -> None:
        if not self.expected_version or not self.expected_version.strip():
            raise ValueError("expected_version is required")
        if not self.expected_commit or not self.expected_commit.strip():
            raise ValueError("expected_commit is required")
        if self.environment is not BrokerEnvironment.PAPER:
            raise NautilusCompatibilityError("Paper bridge only accepts the paper environment")
        if self.signer_kind is not SignerKind.NONE:
            raise NautilusCompatibilityError("Paper bridge cannot accept a signer")


@dataclass(frozen=True)
class NautilusBridgeReceipt:
    broker_id: str
    environment: BrokerEnvironment
    account_address: str | None
    signer_kind: SignerKind
    port: str
    operation: str
    result: Any
    accepted: bool
    network_io: bool
    real_money_eligible: bool
    adapter_version: str
    adapter_commit: str
    provenance: Provenance = field(
        default_factory=lambda: Provenance(
            source="nautilus-hyperliquid.bridge",
            execution_scope="hypercore:default",
            transport_state="local_fixture",
            mapping_revision="bridge-contract-v1",
            received_at=datetime.now(UTC),
        )
    )


class NautilusHyperliquidBridge:
    """Validate and invoke an already-constructed, local-only adapter backend."""

    name = "nautilus_hyperliquid_bridge"

    def __init__(
        self,
        *,
        backend: object,
        config: NautilusBridgeConfig,
        declared_capabilities: CapabilityDescriptor,
    ) -> None:
        self._validate_backend(backend, config, declared_capabilities)
        self._backend = backend
        self._config = config
        self._capabilities = declared_capabilities
        self._metadata: NautilusAdapterMetadata = backend.metadata

    @staticmethod
    def _validate_backend(
        backend: object,
        config: NautilusBridgeConfig,
        declared_capabilities: CapabilityDescriptor,
    ) -> None:
        if getattr(backend, "local_only", False) is not True:
            raise NautilusCompatibilityError("Paper bridge requires a local-only backend")
        metadata = getattr(backend, "metadata", None)
        if not isinstance(metadata, NautilusAdapterMetadata):
            raise NautilusCompatibilityError("backend metadata must identify the Nautilus adapter")
        if metadata.package != "nautilus-hyperliquid":
            raise NautilusCompatibilityError("backend is not the Hyperliquid Nautilus adapter")
        if metadata.version != config.expected_version:
            raise NautilusCompatibilityError(
                f"Nautilus version mismatch: expected {config.expected_version}, got {metadata.version}"
            )
        if metadata.commit != config.expected_commit:
            raise NautilusCompatibilityError(
                f"Nautilus commit mismatch: expected {config.expected_commit}, got {metadata.commit}"
            )
        if metadata.capabilities != declared_capabilities:
            raise NautilusCompatibilityError("declared capabilities do not match Nautilus capabilities")
        if not callable(getattr(backend, "invoke", None)):
            raise NautilusCompatibilityError("backend must expose invoke(port, operation, request)")

    @property
    def capabilities(self) -> CapabilityDescriptor:
        return self._capabilities

    @property
    def identity(self) -> BrokerIdentity:
        return BrokerIdentity(
            broker_id="hyperliquid",
            environment=self._config.environment,
            account_address=self._config.account_address,
            signer_kind=self._config.signer_kind,
        )

    @property
    def adapter_metadata(self) -> NautilusAdapterMetadata:
        return self._metadata

    def request(self, port: str, operation: str, request: object) -> NautilusBridgeReceipt:
        try:
            self._capabilities.require(port, operation)
        except BrokerCapabilityError as exc:
            raise NautilusCompatibilityError(str(exc)) from exc
        result = self._backend.invoke(port, operation, request)
        return NautilusBridgeReceipt(
            broker_id="hyperliquid",
            environment=self._config.environment,
            account_address=self._config.account_address,
            signer_kind=self._config.signer_kind,
            port=port,
            operation=operation,
            result=result,
            accepted=True,
            network_io=False,
            real_money_eligible=False,
            adapter_version=self._metadata.version,
            adapter_commit=self._metadata.commit,
        )

    def preflight(self) -> PaperPreflight:
        return PaperPreflight(
            broker_id="hyperliquid",
            environment=BrokerEnvironment.PAPER,
            network_io=False,
            real_money_eligible=False,
            credential_required=False,
            ports=PORT_NAMES,
        )
