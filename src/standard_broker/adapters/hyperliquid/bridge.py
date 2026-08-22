"""Paper-safe compatibility boundary for the Nautilus Hyperliquid adapter."""

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
import re

from ...capabilities import PORT_NAMES, CapabilityDescriptor
from ...errors import BrokerCapabilityError, BrokerError, RuntimeBoundaryError
from ...models import BrokerEnvironment, BrokerIdentity, Provenance, SignerKind
from ...paper import PaperPreflight
from ...runtime import (
    BrokerRuntimeSession,
    RuntimeActivationPolicy,
    RuntimeOperationGuard,
    RuntimePreflight,
    preflight_runtime_session,
)


class NautilusCompatibilityError(BrokerError):
    """Raised when the injected Nautilus adapter does not match the contract."""


class NautilusRuntimeError(BrokerError):
    """Raised when the Nautilus runtime is not safely ready for invocation."""

    def __init__(self, reason_code: str, detail: str) -> None:
        self.reason_code = reason_code
        super().__init__(f"{reason_code}: {detail}")


class NautilusRuntimeState(str, Enum):
    CREATED = "created"
    READY = "ready"
    CLOSED = "closed"
    FAULTED = "faulted"


@dataclass(frozen=True)
class NautilusAdapterMetadata:
    package: str
    version: str
    commit: str
    capabilities: CapabilityDescriptor


@dataclass(frozen=True)
class NautilusRuntimeConfig:
    """Pinned compatibility and activation policy for one runtime instance."""

    expected_version: str
    expected_commit: str
    policy: RuntimeActivationPolicy = field(default_factory=RuntimeActivationPolicy)
    expected_release_sha: str | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        for name in ("expected_version", "expected_commit"):
            value = getattr(self, name)
            if not value or value != value.strip():
                raise ValueError(f"{name} is required")
        if self.expected_release_sha is not None:
            if not re.fullmatch(r"[0-9a-f]{40}", self.expected_release_sha):
                raise ValueError("expected_release_sha must be a full lowercase 40-character SHA-1")


@dataclass(frozen=True)
class NautilusRuntimeHealth:
    """Non-secret runtime health and activation facts."""

    state: NautilusRuntimeState
    broker_id: str
    environment: BrokerEnvironment
    adapter_version: str
    adapter_commit: str
    external_network: bool
    invocation_performed: bool
    reason: str | None = None


@dataclass(frozen=True)
class NautilusRuntimeReceipt:
    """Provider-neutral runtime invocation receipt without native payloads."""

    broker_id: str
    environment: BrokerEnvironment
    port: str
    operation: str
    accepted: bool
    adapter_version: str
    adapter_commit: str
    invocation_performed: bool
    account_address: str
    lifecycle_id: str
    release_sha: str | None
    provenance: Provenance


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
        self._backend.invoke(port, operation, request)
        return NautilusBridgeReceipt(
            broker_id="hyperliquid",
            environment=self._config.environment,
            account_address=self._config.account_address,
            signer_kind=self._config.signer_kind,
            port=port,
            operation=operation,
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


class NautilusHyperliquidRuntime:
    """Lifecycle-managed runtime boundary around an injected Nautilus backend."""

    def __init__(
        self,
        *,
        session: BrokerRuntimeSession,
        backend: object,
        config: NautilusRuntimeConfig,
    ) -> None:
        self._validate_backend(session=session, backend=backend, config=config)
        self._session = session
        self._backend = backend
        self._config = config
        self._guard = RuntimeOperationGuard(
            session=session,
            backend=backend,
            policy=config.policy,
        )
        self._state = NautilusRuntimeState.CREATED
        self._invocation_performed = False
        self._metadata: NautilusAdapterMetadata = backend.metadata

    @staticmethod
    def _validate_backend(
        *,
        session: BrokerRuntimeSession,
        backend: object,
        config: NautilusRuntimeConfig,
    ) -> None:
        metadata = getattr(backend, "metadata", None)
        if not isinstance(metadata, NautilusAdapterMetadata):
            raise NautilusRuntimeError("metadata_missing", "backend metadata must identify the Nautilus adapter")
        if metadata.package != "nautilus-hyperliquid":
            raise NautilusRuntimeError("adapter_mismatch", "backend is not the Hyperliquid Nautilus adapter")
        if metadata.version != config.expected_version:
            raise NautilusRuntimeError(
                "version_mismatch",
                f"expected {config.expected_version}, got {metadata.version}",
            )
        if metadata.commit != config.expected_commit:
            raise NautilusRuntimeError(
                "commit_mismatch",
                f"expected {config.expected_commit}, got {metadata.commit}",
            )
        if metadata.capabilities != session.capabilities:
            raise NautilusRuntimeError("capability_mismatch", "backend capability profile does not match session")
        if session.environment is BrokerEnvironment.PAPER and getattr(backend, "local_only", False) is not True:
            raise NautilusRuntimeError("paper_backend_not_local", "Paper runtime requires a local-only backend")
        if session.environment is BrokerEnvironment.TESTNET and getattr(backend, "local_only", False) is not True:
            raise NautilusRuntimeError("testnet_backend_not_local", "Testnet fixture runtime requires a local-only backend")
        if not callable(getattr(backend, "invoke", None)):
            raise NautilusRuntimeError("backend_invoke_missing", "backend must expose invoke(port, operation, request)")

    @property
    def state(self) -> NautilusRuntimeState:
        return self._state

    @property
    def session(self) -> BrokerRuntimeSession:
        """Return the immutable session identity used by adapter mappers."""

        return self._session

    @property
    def health(self) -> NautilusRuntimeHealth:
        return NautilusRuntimeHealth(
            state=self._state,
            broker_id=self._session.broker_id,
            environment=self._session.environment,
            adapter_version=self._metadata.version,
            adapter_commit=self._metadata.commit,
            external_network=self._session.environment is not BrokerEnvironment.PAPER,
            invocation_performed=self._invocation_performed,
            reason=None,
        )

    def preflight(self, *, required_operations: dict[str, set[str]] | None = None) -> RuntimePreflight:
        """Validate activation and exact operations without invoking the backend."""

        if self._state is NautilusRuntimeState.CLOSED:
            raise NautilusRuntimeError("runtime_closed", "closed runtime cannot be preflighted")
        try:
            result = preflight_runtime_session(
                self._session,
                required_operations=required_operations or {},
                policy=self._config.policy,
            )
            if self._session.environment is BrokerEnvironment.TESTNET:
                approval = self._config.policy.testnet_approval
                if self._config.expected_release_sha is None:
                    raise RuntimeBoundaryError(
                        "testnet_release_binding_required",
                        "Testnet runtime requires an expected release SHA",
                    )
                if approval is None or approval.release_sha != self._config.expected_release_sha:
                    raise RuntimeBoundaryError(
                        "testnet_release_mismatch",
                        "Testnet approval does not match the expected release SHA",
                    )
                if (
                    approval.account_address != self._session.account.address
                    or approval.lifecycle_id != self._session.lifecycle_id
                ):
                    raise RuntimeBoundaryError(
                        "testnet_identity_mismatch",
                        "Testnet approval does not match the runtime account/lifecycle",
                    )
                result = replace(result, release_sha=self._config.expected_release_sha)
            return result
        except (BrokerCapabilityError, RuntimeBoundaryError) as exc:
            self._state = NautilusRuntimeState.FAULTED
            reason_code = getattr(exc, "reason_code", "runtime_preflight_failed")
            raise NautilusRuntimeError(reason_code, str(exc)) from exc

    def start(self) -> NautilusRuntimeHealth:
        """Move the runtime to READY after environment approval preflight."""

        if self._state is NautilusRuntimeState.CLOSED:
            raise NautilusRuntimeError("runtime_closed", "closed runtime cannot be started")
        required_operations = (
            {
                "order_execution": {
                    "submit",
                    "cancel",
                    "replace",
                    "query",
                    "open_orders",
                }
            }
            if self._session.environment is BrokerEnvironment.TESTNET
            else {}
        )
        self.preflight(required_operations=required_operations)
        self._state = NautilusRuntimeState.READY
        return self.health

    def _invoke_native(self, port: str, operation: str, request: object) -> object:
        """Adapter-internal native invocation; never use as a canonical receipt."""

        if self._state is not NautilusRuntimeState.READY:
            raise NautilusRuntimeError("runtime_not_ready", "runtime must be started before invocation")
        try:
            result = self._guard.invoke(port, operation, request)
        except (BrokerCapabilityError, RuntimeBoundaryError) as exc:
            reason_code = getattr(exc, "reason_code", "runtime_preflight_failed")
            raise NautilusRuntimeError(reason_code, str(exc)) from exc
        self._invocation_performed = True
        return result

    def invoke(self, port: str, operation: str, request: object) -> NautilusRuntimeReceipt:
        """Invoke and return a canonical receipt without provider-native payloads."""

        self._invoke_native(port, operation, request)
        return NautilusRuntimeReceipt(
            broker_id=self._session.broker_id,
            environment=self._session.environment,
            port=port,
            operation=operation,
            accepted=True,
            adapter_version=self._metadata.version,
            adapter_commit=self._metadata.commit,
            invocation_performed=self._invocation_performed,
            account_address=self._session.account.address,
            lifecycle_id=self._session.lifecycle_id,
            release_sha=self._config.expected_release_sha,
            provenance=Provenance(
                source="nautilus-hyperliquid.runtime",
                execution_scope=self._session.execution_scope,
                transport_state="local_fixture",
                mapping_revision=self._session.capabilities.revision,
            ),
        )

    def close(self) -> NautilusRuntimeHealth:
        """Close the runtime and prevent further backend invocation."""

        self._state = NautilusRuntimeState.CLOSED
        return self.health
