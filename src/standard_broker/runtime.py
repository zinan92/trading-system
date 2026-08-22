"""Explicit Broker Runtime Session and fail-closed environment preflight."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
import re
from typing import Protocol, runtime_checkable

from .capabilities import CapabilityDescriptor
from .errors import RuntimeBoundaryError
from .models import AccountScope, BrokerEnvironment, BrokerIdentity, SignerKind

_WRITE_OPERATIONS = frozenset({"submit", "cancel", "replace", "cancel_replace", "modify"})
_RAW_PRIVATE_KEY = re.compile(r"^(?:0x)?[0-9a-fA-F]{64}$")
_FORBIDDEN_REQUEST_KEYS = frozenset({"private_key", "secret", "signature", "signed_payload"})


@dataclass(frozen=True)
class AccountReference:
    """Public Broker account identity used by one Runtime Session."""

    scope: AccountScope
    address: str

    def __post_init__(self) -> None:
        if not isinstance(self.scope, AccountScope):
            raise TypeError("account scope must be an AccountScope")
        if not self.address or self.address != self.address.strip():
            raise RuntimeBoundaryError("account_reference_required", "account address is required")


@dataclass(frozen=True)
class SignerReference:
    """Opaque reference to a credential provider, never credential material."""

    kind: SignerKind
    provider: str
    reference: str

    @classmethod
    def paper(cls) -> "SignerReference":
        """Return the explicit no-signer binding used by local Paper."""

        return cls(SignerKind.NONE, "none", "paper://none")

    def __post_init__(self) -> None:
        if self.kind is SignerKind.NONE:
            if self.provider != "none" or self.reference != "paper://none":
                raise RuntimeBoundaryError(
                    "signer_reference_invalid",
                    "kind none is reserved for the explicit Paper no-signer reference",
                )
            return
        for name in ("provider", "reference"):
            value = getattr(self, name)
            if not value or value != value.strip():
                raise RuntimeBoundaryError("signer_reference_required", f"{name} is required")
        if _RAW_PRIVATE_KEY.fullmatch(self.reference) or "BEGIN PRIVATE KEY" in self.reference:
            raise RuntimeBoundaryError("raw_credential_forbidden", "signer reference looks like key material")
        if "://" not in self.reference:
            raise RuntimeBoundaryError(
                "raw_credential_forbidden",
                "signer reference must be an opaque provider URI",
            )


@runtime_checkable
class SignerProvider(Protocol):
    """Out-of-band signer boundary; signed material never enters canonical facts."""

    def sign(self, signer: SignerReference, payload: bytes) -> bytes:
        """Sign provider-owned payload material inside the runtime boundary."""
        ...


@dataclass(frozen=True)
class BrokerRuntimeSession:
    """Explicit identity and lifecycle binding for one Broker runtime."""

    broker_id: str
    environment: BrokerEnvironment
    account: AccountReference
    signer: SignerReference
    signer_provider: SignerProvider | None
    capabilities: CapabilityDescriptor
    execution_scope: str
    lifecycle_id: str

    def __post_init__(self) -> None:
        if not self.broker_id or self.broker_id != self.broker_id.strip():
            raise RuntimeBoundaryError("broker_reference_required", "broker_id is required")
        if self.broker_id != self.broker_id.lower():
            raise RuntimeBoundaryError("broker_reference_invalid", "broker_id must be lowercase")
        if not isinstance(self.environment, BrokerEnvironment):
            raise TypeError("environment must be a BrokerEnvironment")
        if not isinstance(self.account, AccountReference):
            raise TypeError("account must be an AccountReference")
        if not isinstance(self.signer, SignerReference):
            raise TypeError("signer must be a SignerReference")
        if self.signer_provider is not None and not isinstance(self.signer_provider, SignerProvider):
            raise TypeError("signer_provider must implement SignerProvider")
        if not isinstance(self.capabilities, CapabilityDescriptor):
            raise TypeError("capabilities must be a CapabilityDescriptor")
        for name in ("execution_scope", "lifecycle_id"):
            value = getattr(self, name)
            if not value or value != value.strip():
                raise RuntimeBoundaryError("runtime_identity_required", f"{name} is required")
        if self.environment is BrokerEnvironment.PAPER and self.signer.kind is not SignerKind.NONE:
            raise RuntimeBoundaryError("paper_signer_forbidden", "Paper sessions cannot bind a signer")
        if self.environment is BrokerEnvironment.PAPER and self.signer_provider is not None:
            raise RuntimeBoundaryError("paper_signer_provider_forbidden", "Paper sessions cannot bind a signer provider")
        if self.environment is not BrokerEnvironment.PAPER and self.signer.kind is SignerKind.NONE:
            raise RuntimeBoundaryError(
                "signer_reference_required",
                "external sessions must bind an opaque signer reference",
            )
        if self.environment is not BrokerEnvironment.PAPER and self.signer_provider is None:
            raise RuntimeBoundaryError(
                "signer_provider_required",
                "external sessions must bind an out-of-band signer provider",
            )
        if self.capabilities.broker_id != self.broker_id:
            raise RuntimeBoundaryError("capability_broker_mismatch", "capability broker does not match session")
        if self.capabilities.environment is not self.environment:
            raise RuntimeBoundaryError(
                "capability_environment_mismatch",
                "capability environment does not match session",
            )

    @property
    def capability_revision(self) -> str:
        """Return the immutable capability profile revision bound to the session."""

        return self.capabilities.revision

    @property
    def identity(self) -> BrokerIdentity:
        """Return the canonical public Broker identity for this session."""

        return BrokerIdentity(
            broker_id=self.broker_id,
            environment=self.environment,
            account_scope=self.account.scope,
            account_address=self.account.address,
            signer_kind=self.signer.kind if self.signer is not None else SignerKind.NONE,
            execution_scope=self.execution_scope,
        )


@dataclass(frozen=True)
class RuntimeActivationPolicy:
    """Explicit external-environment approval policy; all external access is denied by default."""

    testnet_approval: "ExternalEnvironmentApproval | None" = None


@dataclass(frozen=True)
class ExternalEnvironmentApproval:
    """Human-approved, release-bound artifact required before testnet readiness."""

    environment: BrokerEnvironment
    approval_id: str
    release_sha: str
    approved_by: str
    approved_at: datetime
    account_address: str | None = None
    lifecycle_id: str | None = None

    def __post_init__(self) -> None:
        if self.environment is not BrokerEnvironment.TESTNET:
            raise RuntimeBoundaryError(
                "external_approval_invalid",
                "runtime v1 only accepts an explicit testnet approval artifact",
            )
        for name in ("approval_id", "release_sha", "approved_by"):
            value = getattr(self, name)
            if not value or value != value.strip():
                raise RuntimeBoundaryError("external_approval_required", f"{name} is required")
        if self.approved_at.tzinfo is None:
            raise RuntimeBoundaryError("external_approval_invalid", "approved_at must include timezone information")
        for name in ("account_address", "lifecycle_id"):
            value = getattr(self, name)
            if value is not None and (not value or value != value.strip()):
                raise RuntimeBoundaryError("external_approval_invalid", f"{name} must be non-empty when provided")


@dataclass(frozen=True)
class RuntimePreflight:
    """Non-secret result of a successful runtime authorization check."""

    broker_id: str
    environment: BrokerEnvironment
    account_address: str
    capability_revision: str
    lifecycle_id: str
    accepted: bool
    external_network: bool
    credential_required: bool
    real_money_eligible: bool
    release_sha: str | None = None


def preflight_runtime_session(
    session: BrokerRuntimeSession,
    *,
    required_operations: Mapping[str, Iterable[str]],
    policy: RuntimeActivationPolicy | None = None,
) -> RuntimePreflight:
    """Authorize a session and exact operations before any backend invocation."""

    selected_policy = policy or RuntimeActivationPolicy()
    capabilities = session.capabilities

    if session.environment is BrokerEnvironment.TESTNET:
        approval = selected_policy.testnet_approval
        if approval is None or approval.environment is not BrokerEnvironment.TESTNET:
            raise RuntimeBoundaryError(
                "external_environment_denied",
                "testnet requires a separate human-approved environment artifact",
            )
    if session.environment is BrokerEnvironment.MAINNET:
        raise RuntimeBoundaryError(
            "mainnet_not_in_runtime_v1",
            "mainnet requires a separate specification and activation boundary",
        )

    normalized_operations: list[tuple[str, str]] = []
    for port, operations in required_operations.items():
        for operation in operations:
            capabilities.require(port, operation)
            normalized_operations.append((port, operation))

    needs_signer = session.environment is not BrokerEnvironment.PAPER and any(
        operation in _WRITE_OPERATIONS for _, operation in normalized_operations
    )
    if needs_signer and session.signer.kind is SignerKind.NONE:
        raise RuntimeBoundaryError("signer_required", "external write operations require an opaque signer reference")

    return RuntimePreflight(
        broker_id=session.broker_id,
        environment=session.environment,
        account_address=session.account.address,
        capability_revision=capabilities.revision,
        lifecycle_id=session.lifecycle_id,
        accepted=True,
        external_network=session.environment is not BrokerEnvironment.PAPER,
        credential_required=needs_signer,
        real_money_eligible=False,
    )


class RuntimeOperationGuard:
    """Invoke an injected backend only after runtime and capability preflight."""

    def __init__(
        self,
        *,
        session: BrokerRuntimeSession,
        backend: object,
        policy: RuntimeActivationPolicy | None = None,
    ) -> None:
        if not callable(getattr(backend, "invoke", None)):
            raise TypeError("runtime backend must provide invoke(port, operation, request)")
        self._session = session
        self._backend = backend
        self._policy = policy or RuntimeActivationPolicy()

    def invoke(self, port: str, operation: str, request: object) -> object:
        if isinstance(request, (bytes, bytearray, memoryview)):
            raise RuntimeBoundaryError("raw_payload_forbidden", "signed or credential-bearing bytes cannot cross the canonical seam")
        if isinstance(request, Mapping) and any(
            str(key).lower() in _FORBIDDEN_REQUEST_KEYS for key in request
        ):
            raise RuntimeBoundaryError("raw_payload_forbidden", "credential-bearing request fields cannot cross the canonical seam")
        preflight_runtime_session(
            self._session,
            required_operations={port: {operation}},
            policy=self._policy,
        )
        return self._backend.invoke(port, operation, request)
