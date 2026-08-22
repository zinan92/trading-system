"""Environment and Paper composition boundaries for standard-broker.

This module is intentionally not an execution engine and does not import a
Hyperliquid wire client.  It resolves the reviewed local Paper binding and
explicitly identifies Testnet/Live selections without constructing transport.
Write-capable lifecycle code belongs to later, separately approved tickets.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from collections.abc import Callable, Mapping
import re
from typing import Any

from services.broker_port import (
    BrokerCapabilities,
    BrokerCapability,
    BrokerOrderRequest,
    BrokerPortDescriptor,
    UnsupportedBrokerCapability,
    broker_port_descriptor,
)


STANDARD_BROKER_COMMIT = "9f4d42637d48f6bf8926d85eb50ce62bd86b499e"
STANDARD_BROKER_REVISION = f"paper-host-{STANDARD_BROKER_COMMIT}"
STANDARD_BROKER_PAPER_CAPABILITIES = BrokerCapabilities(
    frozenset({BrokerCapability.PREFLIGHT})
)
STANDARD_BROKER_ENVIRONMENT_CAPABILITIES = BrokerCapabilities(
    frozenset({BrokerCapability.PREFLIGHT})
)
_RELEASE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")


@dataclass(frozen=True)
class StandardBrokerEnvironmentIdentity:
    """Public, non-secret identity for one standard-broker environment."""

    broker_id: str
    environment: str
    execution_scope: str
    account_id: str
    credential_source: str
    runtime_id: str
    ledger_namespace: str
    release_sha: str

    def __post_init__(self) -> None:
        broker_id = str(self.broker_id or "").strip().lower()
        environment = str(self.environment or "").strip().lower()
        if environment == "live":
            environment = "mainnet"
        if broker_id != "hyperliquid":
            raise ValueError("standard-broker environment broker_id must be hyperliquid")
        if environment not in {"paper", "testnet", "mainnet"}:
            raise ValueError(f"unsupported standard-broker environment: {environment!r}")
        object.__setattr__(self, "broker_id", broker_id)
        object.__setattr__(self, "environment", environment)
        for name in (
            "execution_scope",
            "account_id",
            "credential_source",
            "runtime_id",
            "ledger_namespace",
        ):
            value = str(getattr(self, name) or "").strip()
            if not value:
                raise ValueError(f"{name} is required for standard-broker environment identity")
            if not _IDENTIFIER_RE.fullmatch(value):
                raise ValueError(f"{name} contains unsafe identity characters")
            object.__setattr__(self, name, value)
        if self.execution_scope != "hypercore:default":
            raise ValueError("unsupported standard-broker execution_scope")
        release_sha = str(self.release_sha or "").strip().lower()
        if not _RELEASE_SHA_RE.fullmatch(release_sha):
            raise ValueError("release_sha must be a full lowercase 40-character SHA-1")
        object.__setattr__(self, "release_sha", release_sha)

    def to_public_dict(self) -> dict[str, str]:
        return {
            "broker_id": self.broker_id,
            "environment": self.environment,
            "execution_scope": self.execution_scope,
            "account_id": self.account_id,
            "credential_source": self.credential_source,
            "runtime_id": self.runtime_id,
            "ledger_namespace": self.ledger_namespace,
            "release_sha": self.release_sha,
        }


class StandardBrokerHostError(RuntimeError):
    """Stable host-level diagnostic for canonical broker composition failures."""


class StandardBrokerEnvironmentGateAdapter:
    """Read-only identity gate for explicitly selected non-Paper environments."""

    name = "standard_broker_environment_gate"
    provider = "standard_broker"

    def __init__(self, identity: StandardBrokerEnvironmentIdentity) -> None:
        self.identity = identity
        self.broker_config = {
            "provider": self.provider,
            "broker_id": identity.broker_id,
            "environment": identity.environment,
            "execution_scope": identity.execution_scope,
            "account_id": identity.account_id,
            "credential_source": identity.credential_source,
            "runtime_id": identity.runtime_id,
            "ledger_namespace": identity.ledger_namespace,
            "release_sha": identity.release_sha,
            "dry_run": True,
            "live_trading_enabled": False,
        }

    @property
    def capabilities(self) -> BrokerCapabilities:
        return STANDARD_BROKER_ENVIRONMENT_CAPABILITIES

    @property
    def descriptor(self) -> BrokerPortDescriptor:
        return broker_port_descriptor(self)

    def preflight(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "broker_id": self.identity.broker_id,
            "mode": self.identity.environment,
            "environment": self.identity.environment,
            "dry_run": True,
            "live_trading_enabled": False,
            "ready": False,
            "blocker": "capability_gate_pending",
            "network_io": False,
            "real_money_eligible": False,
            "credential_required": False,
            "control_plane": "telegram",
            "capability_revision": f"environment-gate-{self.identity.release_sha}",
            "identity": self.identity.to_public_dict(),
        }

    def submit_order(self, request: BrokerOrderRequest) -> Any:
        del request
        raise UnsupportedBrokerCapability(
            f"standard-broker {self.identity.environment} capability gate is read-only; "
            "order lifecycle ticket is not enabled"
        )

    def record_capability_gap(self, recording: Any, **kwargs: Any) -> dict[str, Any]:
        return record_standard_broker_capability_gap(
            recording,
            identity=self.identity,
            **kwargs,
        )


def build_standard_broker_environment_gate(
    *,
    broker_id: str,
    environment: str,
    account_id: str,
    credential_source: str,
    runtime_id: str,
    ledger_namespace: str,
    release_sha: str,
    execution_scope: str = "hypercore:default",
) -> StandardBrokerEnvironmentGateAdapter:
    """Build an explicit non-transport environment identity gate."""

    if str(broker_id or "").strip().lower() != "hyperliquid":
        raise StandardBrokerHostError(
            f"unsupported standard_broker selection: broker_id={broker_id!r}"
        )
    identity = StandardBrokerEnvironmentIdentity(
        broker_id=broker_id,
        environment=environment,
        execution_scope=execution_scope,
        account_id=account_id,
        credential_source=credential_source,
        runtime_id=runtime_id,
        ledger_namespace=ledger_namespace,
        release_sha=release_sha,
    )
    if identity.environment == "paper":
        raise StandardBrokerHostError(
            "Paper must resolve through the canonical local Paper binding"
        )
    return StandardBrokerEnvironmentGateAdapter(identity)


class StandardBrokerPaperExecutionAdapter:
    """Read-only execution-port facade over a canonical local Paper binding."""

    name = "standard_broker_paper"
    provider = "standard_broker"

    def __init__(
        self,
        *,
        broker_id: str,
        transport_factory: Callable[[], object] | None = None,
        fixture_operations: Mapping[str, Any] | None = None,
    ) -> None:
        self.broker_config = {
            "provider": self.provider,
            "broker_id": str(broker_id or "").strip().lower(),
            "environment": "paper",
            "dry_run": True,
        }
        self._binding = build_paper_broker_binding(
            broker_id=self.broker_config["broker_id"],
            transport_factory=transport_factory,
            fixture_operations=fixture_operations,
        )
        self.identity = StandardBrokerEnvironmentIdentity(
            broker_id=self.broker_config["broker_id"],
            environment="paper",
            execution_scope="hypercore:default",
            account_id="paper-local",
            credential_source="none",
            runtime_id="standard-broker-paper",
            ledger_namespace="ledger.standard-broker.paper",
            release_sha=STANDARD_BROKER_COMMIT,
        )

    @property
    def capabilities(self) -> BrokerCapabilities:
        return STANDARD_BROKER_PAPER_CAPABILITIES

    @property
    def descriptor(self) -> BrokerPortDescriptor:
        return broker_port_descriptor(self)

    @property
    def canonical_binding(self) -> Any:
        return self._binding

    def preflight(self) -> dict[str, Any]:
        facts = self._binding.adapter.preflight()
        return {
            "provider": self.provider,
            "broker_id": self._binding.identity.broker_id,
            "mode": "paper",
            "environment": "paper",
            "dry_run": True,
            "live_trading_enabled": False,
            "ready": True,
            "network_io": facts.network_io,
            "real_money_eligible": facts.real_money_eligible,
            "credential_required": facts.credential_required,
            "control_plane": self._binding.control_plane,
            "capability_revision": self._binding.capabilities.revision,
        }

    def submit_order(self, request: BrokerOrderRequest) -> Any:
        del request
        raise UnsupportedBrokerCapability(
            "standard-broker Paper host is read-only; submit_order capability is blocked"
        )

    def request(self, port: str, operation: str, payload: object | None = None) -> Any:
        return request_paper_broker(self._binding, port, operation, payload)

    def record_receipt(self, recording: Any, **kwargs: Any) -> dict[str, Any]:
        return record_paper_broker_receipt(recording, binding=self._binding, **kwargs)

    def record_capability_gap(self, recording: Any, **kwargs: Any) -> dict[str, Any]:
        return record_paper_broker_capability_gap(recording, **kwargs)


def build_paper_broker_binding(
    *,
    broker_id: str = "hyperliquid",
    transport_factory: Callable[[], object] | None = None,
    fixture_operations: Mapping[str, Any] | None = None,
) -> Any:
    """Resolve the exact Hyperliquid/Paper/authoritative canonical binding.

    The import is lazy so the existing host can remain importable when the
    separately packaged standard-broker dependency is absent.  Calling this
    seam without that dependency is a typed blocker, never a fallback.
    """

    if str(broker_id or "").strip().lower() != "hyperliquid":
        raise StandardBrokerHostError(
            f"unsupported broker selection: broker_id={broker_id!r}"
        )
    try:
        from standard_broker import (
            BrokerEnvironment,
            BrokerIdentity,
            BrokerRegistry,
            BrokerSelection,
            CapabilityDescriptor,
            HostRole,
            HostSafetyPolicy,
            PaperBrokerAdapter,
            PaperPreflight,
            SignerKind,
            PORT_NAMES,
        )
    except ModuleNotFoundError as exc:
        raise StandardBrokerHostError(
            "standard-broker dependency is unavailable; host integration is blocked"
        ) from exc
    operations = dict(fixture_operations or {"preflight": frozenset({"read"})})
    if fixture_operations is None:
        operations.update({port: frozenset({"read"}) for port in PORT_NAMES})
    if any(
        str(operation) != "read"
        for declared in operations.values()
        for operation in declared
    ):
        raise StandardBrokerHostError(
            "Paper fixture capabilities are read-only; write operations are blocked"
        )
    capabilities = CapabilityDescriptor(
        broker_id="hyperliquid",
        environment=BrokerEnvironment.PAPER,
        operations=operations,
        revision=STANDARD_BROKER_REVISION,
    )
    identity = BrokerIdentity(
        broker_id="hyperliquid",
        environment=BrokerEnvironment.PAPER,
        signer_kind=SignerKind.NONE,
        execution_scope="hypercore:default",
    )
    registry = BrokerRegistry()
    selection = BrokerSelection(
        broker_id="hyperliquid",
        environment=BrokerEnvironment.PAPER,
        role=HostRole.AUTHORITATIVE,
    )
    registry.register(
        selection,
        factory=lambda: PaperBrokerAdapter(
            identity=identity,
            capabilities=capabilities,
            transport=transport_factory() if transport_factory is not None else None,
        ),
        capabilities=capabilities,
    )
    registry.freeze()
    try:
        binding = registry.resolve(selection, policy=HostSafetyPolicy())
    except Exception as exc:  # noqa: BLE001 - preserve typed host blocker.
        raise StandardBrokerHostError(
            f"broker binding blocked: {type(exc).__name__}: {exc}"
        ) from exc
    preflight = binding.adapter.preflight()
    if not isinstance(preflight, PaperPreflight):
        raise StandardBrokerHostError("canonical Paper preflight type is invalid")
    if (
        preflight.environment is not BrokerEnvironment.PAPER
        or preflight.network_io is not False
        or preflight.real_money_eligible is not False
        or preflight.credential_required is not False
    ):
        raise StandardBrokerHostError("canonical Paper preflight violated local-only safety")
    if (
        binding.paper_only is not True
        or binding.real_money_eligible is not False
        or binding.control_plane != "telegram"
    ):
        raise StandardBrokerHostError(
            "canonical broker binding violated Paper/Telegram host policy"
        )
    return binding


def record_standard_broker_capability_gap(
    recording: Any,
    *,
    identity: StandardBrokerEnvironmentIdentity,
    record_window_id: str,
    strategy_session_id: str,
    strategy_revision_id: str,
    occurred_at: str,
    port: str,
    operation: str,
    reason: str,
) -> dict[str, Any]:
    """Persist a non-secret environment selection blocker."""

    return recording.record_event(
        record_window_id=record_window_id,
        strategy_session_id=strategy_session_id,
        strategy_revision_id=strategy_revision_id,
        category="execution_path",
        event_type="standard_broker_capability_gap",
        source=f"standard-broker.{identity.environment}",
        occurred_at=occurred_at,
        payload={
            **identity.to_public_dict(),
            "network_io": False,
            "real_money_eligible": False,
            "credential_required": False,
            "status": "blocked",
            "port": str(port),
            "operation": str(operation),
            "reason": str(reason)[:300],
            "capability_revision": f"environment-gate-{identity.release_sha}",
        },
    )


def request_paper_broker(
    binding: Any,
    port: str,
    operation: str,
    payload: object | None = None,
) -> Any:
    """Issue one canonical local-Paper request through an already resolved binding."""

    try:
        receipt = binding.adapter.request(port, operation, payload)
    except Exception as exc:  # noqa: BLE001 - convert capability gaps to host diagnostics.
        raise StandardBrokerHostError(
            f"broker request blocked: port={port} operation={operation} reason={type(exc).__name__}: {exc}"
        ) from exc
    if (
        getattr(receipt, "network_io", True) is not False
        or getattr(receipt, "real_money_eligible", True) is not False
    ):
        raise StandardBrokerHostError("Paper broker receipt is not local-only")
    return receipt


def record_paper_broker_receipt(
    recording: Any,
    *,
    binding: Any,
    record_window_id: str,
    strategy_session_id: str,
    strategy_revision_id: str,
    occurred_at: str,
    receipt: Any,
) -> dict[str, Any]:
    """Project one canonical local Paper receipt into Park Recording Track."""

    try:
        from standard_broker import PaperReceipt
    except ModuleNotFoundError as exc:
        raise StandardBrokerHostError(
            "standard-broker dependency is unavailable; receipt projection is blocked"
        ) from exc
    if not isinstance(receipt, PaperReceipt):
        raise StandardBrokerHostError("Paper broker receipt must be canonical PaperReceipt")
    if not getattr(binding, "capabilities", None).supports(receipt.port, receipt.operation):
        raise StandardBrokerHostError(
            "capability_gap receipt cannot be recorded as a successful Paper receipt"
        )
    provenance = receipt.provenance
    if (
        receipt.broker_id != "hyperliquid"
        or receipt.environment.value != "paper"
        or provenance.source != "paper_fixture"
        or provenance.transport_state != "local_fixture"
        or provenance.mapping_revision != STANDARD_BROKER_REVISION
    ):
        raise StandardBrokerHostError(
            "Paper broker receipt provenance is not the reviewed local Hyperliquid binding"
        )
    payload = asdict(receipt)

    def json_safe(value: Any) -> Any:
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, dict):
            return {str(key): json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [json_safe(item) for item in value]
        if hasattr(value, "value"):
            return value.value
        return value

    payload = json_safe(payload)
    if not isinstance(payload, dict):
        raise StandardBrokerHostError("Paper broker receipt payload is not canonical")
    required = {"broker_id", "environment", "network_io", "real_money_eligible", "provenance"}
    if not required.issubset(payload):
        raise StandardBrokerHostError("Paper broker receipt payload is incomplete")
    if (
        payload.get("environment") != "paper"
        or payload.get("network_io") is not False
        or payload.get("real_money_eligible") is not False
        or payload.get("credential_required", False) is not False
    ):
        raise StandardBrokerHostError("only local Paper receipts can enter Park Recording Track")
    return recording.record_event(
        record_window_id=record_window_id,
        strategy_session_id=strategy_session_id,
        strategy_revision_id=strategy_revision_id,
        category="execution_path",
        event_type="standard_broker_receipt",
        source="standard-broker.paper",
        occurred_at=occurred_at,
        payload=payload,
    )


def record_paper_broker_capability_gap(
    recording: Any,
    *,
    record_window_id: str,
    strategy_session_id: str,
    strategy_revision_id: str,
    occurred_at: str,
    port: str,
    operation: str,
    reason: str,
) -> dict[str, Any]:
    """Persist a capability blocker without attempting broker transport."""

    return recording.record_event(
        record_window_id=record_window_id,
        strategy_session_id=strategy_session_id,
        strategy_revision_id=strategy_revision_id,
        category="execution_path",
        event_type="standard_broker_capability_gap",
        source="standard-broker.paper",
        occurred_at=occurred_at,
        payload={
            "broker_id": "hyperliquid",
            "environment": "paper",
            "network_io": False,
            "real_money_eligible": False,
            "credential_required": False,
            "status": "blocked",
            "port": str(port),
            "operation": str(operation),
            "reason": str(reason)[:300],
            "capability_revision": STANDARD_BROKER_REVISION,
        },
    )
