"""Public, provider-neutral external Broker host binding."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
import hashlib
import json
import re
from typing import Generic, Protocol, TypeVar, runtime_checkable

from .capabilities import CapabilityDescriptor
from .errors import BrokerCapabilityError, RuntimeBoundaryError
from .host import CanonicalHostRequest
from .models import AccountScope, BrokerEnvironment, BrokerIdentity, Provenance, SignerKind
from .protection import ProtectionCapabilityMatrix, ProtectionGroup
from .runtime import (
    BrokerRuntimeSession,
    ExternalEnvironmentApproval,
    RuntimePreflight,
)
from .security import find_secret_like_literals


_SHA1 = re.compile(r"[0-9a-f]{40}")
_ALLOWED_TRANSPORT_STATES = frozenset({"local_fixture", "external_testnet"})


def _canonicalize(value: object) -> object:
    """Convert safe canonical values into deterministic digest input."""

    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {
            str(key): _canonicalize(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_canonicalize(item) for item in value]
    if is_dataclass(value):
        return {
            field_name: _canonicalize(getattr(value, field_name))
            for field_name in value.__dataclass_fields__
        }
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise RuntimeBoundaryError(
        "canonical_value_forbidden",
        f"unsupported value type at the external host boundary: {type(value).__name__}",
    )


def _digest(value: object) -> str:
    encoded = json.dumps(
        _canonicalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _require_sha(value: str, field: str) -> None:
    if not isinstance(value, str) or not _SHA1.fullmatch(value):
        raise RuntimeBoundaryError(
            "external_identity_invalid",
            f"{field} must be a lowercase 40-character release identity",
        )


def _safe_text(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise RuntimeBoundaryError("external_identity_invalid", f"{field} is required")
    return text


@dataclass(frozen=True)
class ExternalRuntimeIdentity:
    """Public identity of the low-level runtime behind one external host."""

    adapter_id: str
    version: str
    commit: str
    mapping_revision: str
    transport_state: str

    def __post_init__(self) -> None:
        for field in ("adapter_id", "version", "mapping_revision"):
            _safe_text(getattr(self, field), field)
        _require_sha(self.commit, "runtime commit")
        if self.transport_state not in _ALLOWED_TRANSPORT_STATES:
            raise RuntimeBoundaryError(
                "external_identity_invalid",
                "transport_state must be local_fixture or external_testnet",
            )


@dataclass(frozen=True)
class ExternalTransportProfile:
    """One exact, non-wildcard external transport profile."""

    profile_id: str
    broker_id: str
    environment: BrokerEnvironment
    execution_scope: str
    adapter_id: str
    version: str
    commit: str
    mapping_revision: str
    transport_state: str
    signer_kind: SignerKind
    capabilities: CapabilityDescriptor
    protection_capabilities: ProtectionCapabilityMatrix | None = None

    def __post_init__(self) -> None:
        for field in (
            "profile_id",
            "broker_id",
            "execution_scope",
            "adapter_id",
            "version",
            "mapping_revision",
        ):
            _safe_text(getattr(self, field), field)
        _require_sha(self.commit, "profile commit")
        if not isinstance(self.environment, BrokerEnvironment):
            raise TypeError("profile environment must be a BrokerEnvironment")
        if not isinstance(self.signer_kind, SignerKind):
            raise TypeError("profile signer_kind must be a SignerKind")
        if self.transport_state not in _ALLOWED_TRANSPORT_STATES:
            raise RuntimeBoundaryError(
                "external_profile_invalid",
                "profile transport_state must be local_fixture or external_testnet",
            )
        if not isinstance(self.capabilities, CapabilityDescriptor):
            raise TypeError("profile capabilities must be a CapabilityDescriptor")
        if self.protection_capabilities is not None and not isinstance(
            self.protection_capabilities,
            ProtectionCapabilityMatrix,
        ):
            raise TypeError("profile protection_capabilities must be a ProtectionCapabilityMatrix")

    def validate(
        self,
        *,
        context: "ExternalBrokerBuildContext",
        runtime: "_ExternalRuntimePort",
        require_approval: bool,
    ) -> None:
        if (
            context.identity.broker_id != self.broker_id
            or context.identity.environment is not self.environment
            or context.identity.execution_scope != self.execution_scope
            or context.runtime_identity.adapter_id != self.adapter_id
            or context.runtime_identity.version != self.version
            or context.runtime_identity.commit != self.commit
            or context.runtime_identity.mapping_revision != self.mapping_revision
            or context.runtime_identity.transport_state != self.transport_state
            or context.identity.signer_kind is not self.signer_kind
            or context.capabilities != self.capabilities
            or runtime.session != context.session
            or runtime.transport_state != self.transport_state
        ):
            raise RuntimeBoundaryError(
                "external_profile_mismatch",
                "context or runtime does not match the exact external transport profile",
            )
        if require_approval:
            if context.approval is None:
                raise RuntimeBoundaryError(
                    "external_approval_required",
                    "exact external Testnet profile requires an approval artifact",
                )
            _validate_approval_identity(
                approval=context.approval,
                session=context.session,
                release_sha=context.release_sha,
            )


@dataclass(frozen=True)
class ExternalBrokerBuildContext:
    """Explicit, non-secret identity used to assemble an external host."""

    session: BrokerRuntimeSession
    runtime_identity: ExternalRuntimeIdentity
    release_sha: str
    approval: ExternalEnvironmentApproval | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.session, BrokerRuntimeSession):
            raise TypeError("external host context requires a BrokerRuntimeSession")
        if not isinstance(self.runtime_identity, ExternalRuntimeIdentity):
            raise TypeError("external host context requires an ExternalRuntimeIdentity")
        _require_sha(self.release_sha, "release SHA")
        if self.session.environment is BrokerEnvironment.MAINNET:
            raise RuntimeBoundaryError(
                "mainnet_not_in_external_host",
                "Mainnet/live requires a separate activation specification",
            )
        if self.session.environment is BrokerEnvironment.PAPER and self.runtime_identity.transport_state != "local_fixture":
            raise RuntimeBoundaryError(
                "paper_transport_invalid",
                "Paper host contexts require local_fixture transport",
            )
        if self.session.environment is BrokerEnvironment.PAPER and self.approval is not None:
            raise RuntimeBoundaryError(
                "paper_approval_forbidden",
                "Paper host contexts cannot carry an external approval artifact",
            )
        if self.approval is not None:
            _validate_approval_identity(
                approval=self.approval,
                session=self.session,
                release_sha=self.release_sha,
            )

    @property
    def identity(self) -> BrokerIdentity:
        return self.session.identity

    @property
    def capabilities(self) -> CapabilityDescriptor:
        return self.session.capabilities

    @property
    def credential_source_ref(self) -> str:
        """Return the opaque signer-provider reference, never its secret."""

        return self.session.signer.reference


@dataclass(frozen=True)
class ExternalHostRequest:
    """A canonical request with a caller-owned correlation identity."""

    request_id: str
    request: CanonicalHostRequest

    def __post_init__(self) -> None:
        _safe_text(self.request_id, "request_id")
        if not isinstance(self.request, CanonicalHostRequest):
            raise TypeError("external host request requires a CanonicalHostRequest")


@dataclass(frozen=True)
class ExternalPreflightReceipt:
    """Non-secret result of an external host preflight."""

    broker_id: str
    environment: BrokerEnvironment
    account_scope: AccountScope
    account_address: str
    execution_scope: str
    lifecycle_id: str
    release_sha: str
    runtime_identity: ExternalRuntimeIdentity
    capability_revision: str
    accepted: bool
    network_io: bool
    real_money_eligible: bool
    required_operations: tuple[tuple[str, tuple[str, ...]], ...]
    provenance: Provenance
    request_digest: str
    receipt_digest: str


@dataclass(frozen=True)
class ExternalCanonicalReceipt:
    """Provider-neutral receipt returned by the external host seam."""

    receipt_id: str
    request_id: str
    broker_id: str
    environment: BrokerEnvironment
    account_scope: AccountScope
    account_address: str
    signer_kind: SignerKind
    execution_scope: str
    lifecycle_id: str
    release_sha: str
    runtime_identity: ExternalRuntimeIdentity
    capability_revision: str
    port: str
    operation: str
    accepted: bool
    network_io: bool
    real_money_eligible: bool
    provenance: Provenance
    request_digest: str
    receipt_digest: str
    raw_payload_digest: str | None = None


T = TypeVar("T")


@dataclass(frozen=True)
class ExternalFactEnvelope(Generic[T]):
    """Canonical fact with external identity and non-secret digests."""

    fact_type: str
    data: T
    broker_id: str
    environment: BrokerEnvironment
    account_scope: AccountScope
    account_address: str
    signer_kind: SignerKind
    execution_scope: str
    lifecycle_id: str
    release_sha: str
    runtime_identity: ExternalRuntimeIdentity
    capability_revision: str
    provenance: Provenance
    request_digest: str
    fact_digest: str
    raw_payload_digest: str | None = None

    @classmethod
    def create(
        cls,
        *,
        context: ExternalBrokerBuildContext,
        fact_type: str,
        data: T,
        request_id: str,
        provenance: Provenance,
        raw_payload: object | None = None,
    ) -> "ExternalFactEnvelope[T]":
        raw_payload_digest = _digest(raw_payload) if raw_payload is not None else None
        request_digest = _digest(
            {
                "request_id": request_id,
                "fact_type": fact_type,
                "raw_payload_digest": raw_payload_digest,
                "lifecycle_id": context.session.lifecycle_id,
            }
        )
        fact_digest = _digest(
            {
                "fact_type": fact_type,
                "data": data,
                "request_digest": request_digest,
                "raw_payload_digest": raw_payload_digest,
                "broker_id": context.identity.broker_id,
                "environment": context.identity.environment,
                "account_address": context.identity.account_address,
                "lifecycle_id": context.session.lifecycle_id,
                "provenance": provenance,
            }
        )
        return cls(
            fact_type=fact_type,
            data=data,
            broker_id=context.identity.broker_id,
            environment=context.identity.environment,
            account_scope=context.identity.account_scope,
            account_address=context.identity.account_address or "",
            signer_kind=context.identity.signer_kind,
            execution_scope=context.identity.execution_scope,
            lifecycle_id=context.session.lifecycle_id,
            release_sha=context.release_sha,
            runtime_identity=context.runtime_identity,
            capability_revision=context.capabilities.revision,
            provenance=provenance,
            request_digest=request_digest,
            fact_digest=fact_digest,
            raw_payload_digest=raw_payload_digest,
        )


@runtime_checkable
class _ExternalRuntimePort(Protocol):
    """Small public runtime dependency required by ExternalBrokerHost."""

    @property
    def session(self) -> BrokerRuntimeSession:
        ...

    @property
    def transport_state(self) -> str:
        ...

    def preflight(
        self,
        *,
        required_operations: dict[str, set[str]] | None = None,
    ) -> RuntimePreflight:
        ...

    def invoke(self, port: str, operation: str, request: object) -> object:
        ...


class ExternalBrokerHost:
    """Public canonical host around one explicitly bound runtime."""

    name = "external_broker_host"

    def __init__(
        self,
        *,
        context: ExternalBrokerBuildContext,
        runtime: _ExternalRuntimePort,
        profile: ExternalTransportProfile | None = None,
    ) -> None:
        if not isinstance(context, ExternalBrokerBuildContext):
            raise TypeError("ExternalBrokerHost requires an ExternalBrokerBuildContext")
        if not isinstance(runtime, _ExternalRuntimePort):
            raise TypeError("ExternalBrokerHost runtime must expose the public runtime port")
        if context.session.environment is BrokerEnvironment.TESTNET and profile is None:
            raise RuntimeBoundaryError(
                "external_profile_required",
                "Testnet hosts must be built through an exact external transport profile",
            )
        if runtime.session != context.session:
            raise RuntimeBoundaryError(
                "runtime_identity_mismatch",
                "runtime session does not match the external host context",
            )
        if runtime.transport_state != context.runtime_identity.transport_state:
            raise RuntimeBoundaryError(
                "runtime_transport_profile_mismatch",
                "runtime transport profile does not match the external host context",
            )
        if profile is not None:
            profile.validate(context=context, runtime=runtime, require_approval=False)
        self._context = context
        self._runtime = runtime
        self._profile = profile

    @property
    def identity(self) -> BrokerIdentity:
        return self._context.identity

    @property
    def capabilities(self) -> CapabilityDescriptor:
        return self._context.capabilities

    @property
    def runtime_identity(self) -> ExternalRuntimeIdentity:
        return self._context.runtime_identity

    @property
    def context(self) -> ExternalBrokerBuildContext:
        return self._context

    @property
    def protection_capabilities(self) -> ProtectionCapabilityMatrix | None:
        return self._profile.protection_capabilities if self._profile is not None else None

    def require_protection(self, group: ProtectionGroup, *, operation: str) -> None:
        """Fail closed before transport when protection semantics are unavailable."""

        matrix = self.protection_capabilities
        if matrix is None:
            raise BrokerCapabilityError(
                "protection_order",
                operation,
                "external protection capability profile is unavailable",
            )
        matrix.require_group(group, operation=operation)

    def preflight(
        self,
        *,
        request_id: str,
        required_operations: Mapping[str, Iterable[str]] | None = None,
    ) -> ExternalPreflightReceipt:
        request_id = _safe_text(request_id, "request_id")
        normalized = self._normalize_operations(required_operations or {})
        if self._profile is not None:
            self._profile.validate(context=self._context, runtime=self._runtime, require_approval=True)
        for port, operations in normalized:
            for operation in operations:
                self.capabilities.require(port, operation)
        request_digest = _digest({"request_id": request_id, "operations": normalized})
        try:
            result = self._runtime.preflight(
                required_operations={port: set(operations) for port, operations in normalized}
            )
        except BrokerCapabilityError:
            raise
        except RuntimeBoundaryError:
            raise
        if not isinstance(result, RuntimePreflight):
            raise RuntimeBoundaryError(
                "runtime_preflight_invalid",
                "runtime returned a non-canonical preflight result",
            )
        if result.accepted is not True:
            raise RuntimeBoundaryError(
                "runtime_preflight_not_accepted",
                "runtime preflight did not accept the requested external operations",
            )
        if (
            result.broker_id != self.identity.broker_id
            or result.environment is not self.identity.environment
            or result.account_address != self.identity.account_address
            or result.lifecycle_id != self._context.session.lifecycle_id
            or result.release_sha != self._context.release_sha
        ):
            raise RuntimeBoundaryError(
                "runtime_preflight_identity_mismatch",
                "runtime preflight identity does not match the external context",
            )
        if result.capability_revision != self.capabilities.revision:
            raise RuntimeBoundaryError(
                "runtime_preflight_capability_mismatch",
                "runtime preflight capability revision does not match the context",
            )
        expected_external_network = self.identity.environment is not BrokerEnvironment.PAPER
        if result.external_network is not expected_external_network:
            raise RuntimeBoundaryError(
                "runtime_preflight_network_mismatch",
                "runtime preflight network identity does not match the context",
            )
        if result.real_money_eligible is not False:
            raise RuntimeBoundaryError(
                "runtime_preflight_real_money",
                "external host runtime is not real-money eligible in this milestone",
            )
        needs_credential = any(
            operation in {"submit", "cancel", "replace", "cancel_replace", "modify"}
            for _, operations in normalized
            for operation in operations
        )
        if result.credential_required is not needs_credential:
            raise RuntimeBoundaryError(
                "runtime_preflight_credential_mismatch",
                "runtime credential requirement does not match the requested operations",
            )
        provenance = self._provenance(transport_state=self.runtime_identity.transport_state)
        receipt_data = {
            "request_id": request_id,
            "request_digest": request_digest,
            "broker_id": self.identity.broker_id,
            "environment": self.identity.environment,
            "account_address": self.identity.account_address,
            "lifecycle_id": self._context.session.lifecycle_id,
            "release_sha": self._context.release_sha,
            "runtime_identity": self.runtime_identity,
            "required_operations": normalized,
        }
        return ExternalPreflightReceipt(
            broker_id=self.identity.broker_id,
            environment=self.identity.environment,
            account_scope=self.identity.account_scope,
            account_address=self.identity.account_address or "",
            execution_scope=self.identity.execution_scope,
            lifecycle_id=self._context.session.lifecycle_id,
            release_sha=self._context.release_sha,
            runtime_identity=self.runtime_identity,
            capability_revision=self.capabilities.revision,
            accepted=result.accepted,
            network_io=self.runtime_identity.transport_state == "external_testnet",
            real_money_eligible=False,
            required_operations=normalized,
            provenance=provenance,
            request_digest=request_digest,
            receipt_digest=_digest(receipt_data),
        )

    def authorize(self, request: ExternalHostRequest) -> ExternalPreflightReceipt:
        """Authorize one typed request without invoking the transport.

        External adapter facades use this seam when the canonical operation
        needs to return a richer domain receipt than ``ExternalCanonicalReceipt``.
        The authorization path remains centralized in this host, while the
        adapter owns the provider-specific lifecycle mapping.
        """

        if not isinstance(request, ExternalHostRequest):
            raise TypeError("external host requires an ExternalHostRequest")
        self._validate_public_payload(request.request)
        if isinstance(request.request.payload, ProtectionGroup):
            self.require_protection(
                request.request.payload,
                operation=request.request.operation,
            )
        return self.preflight(
            request_id=request.request_id,
            required_operations={request.request.port: {request.request.operation}},
        )

    def request(self, request: ExternalHostRequest) -> ExternalCanonicalReceipt:
        if not isinstance(request, ExternalHostRequest):
            raise TypeError("external host requires an ExternalHostRequest")
        request_digest = _digest(request.request)
        self.authorize(request)
        runtime_receipt = self._runtime.invoke(
            request.request.port,
            request.request.operation,
            request.request.payload,
        )
        self._validate_runtime_receipt(runtime_receipt, request.request)
        provenance = runtime_receipt.provenance
        network_io = provenance.transport_state == "external_testnet"
        receipt_data = {
            "request_id": request.request_id,
            "request_digest": request_digest,
            "broker_id": self.identity.broker_id,
            "environment": self.identity.environment,
            "account_address": self.identity.account_address,
            "lifecycle_id": self._context.session.lifecycle_id,
            "release_sha": self._context.release_sha,
            "port": request.request.port,
            "operation": request.request.operation,
            "accepted": bool(runtime_receipt.accepted),
            "provenance": provenance,
        }
        receipt_digest = _digest(receipt_data)
        receipt = ExternalCanonicalReceipt(
            receipt_id=f"external-receipt:{request.request_id}:{receipt_digest[7:19]}",
            request_id=request.request_id,
            broker_id=self.identity.broker_id,
            environment=self.identity.environment,
            account_scope=self.identity.account_scope,
            account_address=self.identity.account_address or "",
            signer_kind=self.identity.signer_kind,
            execution_scope=self.identity.execution_scope,
            lifecycle_id=self._context.session.lifecycle_id,
            release_sha=self._context.release_sha,
            runtime_identity=self.runtime_identity,
            capability_revision=self.capabilities.revision,
            port=request.request.port,
            operation=request.request.operation,
            accepted=bool(runtime_receipt.accepted),
            network_io=network_io,
            real_money_eligible=False,
            provenance=provenance,
            request_digest=request_digest,
            receipt_digest=receipt_digest,
            raw_payload_digest=getattr(runtime_receipt, "raw_payload_digest", None),
        )
        return receipt

    @staticmethod
    def _normalize_operations(
        operations: Mapping[str, Iterable[str]],
    ) -> tuple[tuple[str, tuple[str, ...]], ...]:
        normalized: list[tuple[str, tuple[str, ...]]] = []
        for port, values in operations.items():
            port_name = _safe_text(port, "capability port")
            operation_names = tuple(sorted({_safe_text(value, "capability operation") for value in values}))
            normalized.append((port_name, operation_names))
        return tuple(sorted(normalized))

    @staticmethod
    def _validate_public_payload(request: CanonicalHostRequest) -> None:
        serialized = json.dumps(
            _canonicalize(request),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if find_secret_like_literals(serialized):
            raise RuntimeBoundaryError(
                "raw_payload_forbidden",
                "secret-like material cannot cross the external host boundary",
            )

    def _validate_runtime_receipt(self, receipt: object, request: CanonicalHostRequest) -> None:
        required = (
            "broker_id",
            "environment",
            "port",
            "operation",
            "accepted",
            "adapter_version",
            "adapter_commit",
            "invocation_performed",
            "account_address",
            "lifecycle_id",
            "release_sha",
            "provenance",
        )
        if any(not hasattr(receipt, name) for name in required):
            raise RuntimeBoundaryError(
                "runtime_receipt_invalid",
                "runtime returned a receipt without the required canonical identity",
            )
        if (
            receipt.broker_id != self.identity.broker_id
            or receipt.environment is not self.identity.environment
            or receipt.port != request.port
            or receipt.operation != request.operation
            or receipt.account_address != self.identity.account_address
            or receipt.lifecycle_id != self._context.session.lifecycle_id
            or receipt.release_sha != self._context.release_sha
            or receipt.adapter_version != self.runtime_identity.version
            or receipt.adapter_commit != self.runtime_identity.commit
            or receipt.invocation_performed is not True
            or not isinstance(receipt.accepted, bool)
        ):
            raise RuntimeBoundaryError(
                "runtime_receipt_identity_mismatch",
                "runtime receipt does not match the external host context",
            )
        if not isinstance(receipt.provenance, Provenance):
            raise RuntimeBoundaryError(
                "runtime_receipt_provenance_missing",
                "runtime receipt must carry canonical provenance",
            )
        if not receipt.provenance.source.strip():
            raise RuntimeBoundaryError(
                "runtime_receipt_provenance_missing",
                "runtime receipt provenance requires a source",
            )
        if self.runtime_identity.adapter_id not in receipt.provenance.source:
            raise RuntimeBoundaryError(
                "runtime_receipt_source_mismatch",
                "runtime receipt source does not identify the bound adapter",
            )
        if (
            receipt.provenance.transport_state != self.runtime_identity.transport_state
            or receipt.provenance.execution_scope != self.identity.execution_scope
            or receipt.provenance.mapping_revision != self.capabilities.revision
            or receipt.provenance.mapping_revision != self.runtime_identity.mapping_revision
        ):
            raise RuntimeBoundaryError(
                "runtime_transport_state_invalid",
                "runtime receipt provenance does not match the external host profile",
            )

    def _provenance(self, *, transport_state: str) -> Provenance:
        return Provenance(
            source="standard-broker.external-host",
            execution_scope=self.identity.execution_scope,
            transport_state=transport_state,
            mapping_revision=self.capabilities.revision,
        )


def _validate_approval_identity(
    *,
    approval: ExternalEnvironmentApproval,
    session: BrokerRuntimeSession,
    release_sha: str,
) -> None:
    if approval.environment is not BrokerEnvironment.TESTNET:
        raise RuntimeBoundaryError(
            "external_approval_invalid",
            "the current external host only accepts Testnet approval artifacts",
        )
    if (
        approval.release_sha != release_sha
        or approval.account_address != session.account.address
        or approval.lifecycle_id != session.lifecycle_id
    ):
        raise RuntimeBoundaryError(
            "external_approval_identity_mismatch",
            "approval identity does not match the external host context",
        )
