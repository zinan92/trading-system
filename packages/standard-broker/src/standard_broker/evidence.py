"""Evidence identity and local runtime readiness contracts."""

from dataclasses import dataclass
from enum import Enum
import re

from .host import HostBrokerBinding, RecordingTrack
from .models import AccountScope, BrokerEnvironment, Provenance
from .security import find_secret_like_literals

EVIDENCE_IDENTITY_FIELDS = (
    "environment",
    "account_scope",
    "account_address",
    "execution_scope",
    "lifecycle_id",
    "release_sha",
    "order_ids",
    "fill_ids",
    "fee_ids",
    "position_ids",
    "reconciliation_ids",
)

TESTNET_LIFECYCLE_STEPS = (
    "submit",
    "query",
    "cancel_or_replace",
    "fill",
    "fee",
    "position",
    "reconciliation",
)


class EvidenceClass(str, Enum):
    PAPER = "paper"
    TESTNET = "testnet"
    LIVE = "live"


@dataclass(frozen=True)
class EvidenceIdentity:
    evidence_class: EvidenceClass
    broker_id: str
    environment: BrokerEnvironment
    account_scope: AccountScope
    account_address: str
    execution_scope: str
    lifecycle_id: str
    release_sha: str | None
    order_ids: tuple[str, ...] = ()
    fill_ids: tuple[str, ...] = ()
    fee_ids: tuple[str, ...] = ()
    position_ids: tuple[str, ...] = ()
    reconciliation_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.evidence_class, EvidenceClass):
            raise TypeError("evidence_class must be an EvidenceClass")
        expected_environment = {
            EvidenceClass.PAPER: BrokerEnvironment.PAPER,
            EvidenceClass.TESTNET: BrokerEnvironment.TESTNET,
            EvidenceClass.LIVE: BrokerEnvironment.MAINNET,
        }[self.evidence_class]
        if self.environment is not expected_environment:
            raise ValueError("evidence class and environment do not match")
        if not isinstance(self.account_scope, AccountScope):
            raise TypeError("account_scope must be an AccountScope")
        for name in ("broker_id", "account_address", "execution_scope", "lifecycle_id"):
            value = getattr(self, name)
            if not value or value != value.strip():
                raise ValueError(f"{name} is required")
        if self.evidence_class is EvidenceClass.PAPER:
            if self.release_sha is not None:
                raise ValueError("Paper evidence cannot bind an external release SHA")
        elif self.release_sha is None or not re.fullmatch(r"[0-9a-f]{40}", self.release_sha):
            raise ValueError("external evidence requires a full lowercase release SHA")
        for name in (
            "order_ids",
            "fill_ids",
            "fee_ids",
            "position_ids",
            "reconciliation_ids",
        ):
            values = getattr(self, name)
            if not isinstance(values, tuple) or not all(
                isinstance(value, str) and value.strip() for value in values
            ):
                raise ValueError(f"{name} must be a tuple of non-empty strings")
        if self.evidence_class is not EvidenceClass.PAPER and any(
            not getattr(self, name)
            for name in ("order_ids", "fill_ids", "fee_ids", "position_ids", "reconciliation_ids")
        ):
            raise ValueError("external evidence requires order, fill, fee, position, and reconciliation IDs")


@dataclass(frozen=True)
class ReconciliationEvidence:
    reconciliation_id: str
    broker_id: str
    environment: BrokerEnvironment
    account_scope: AccountScope
    account_address: str
    execution_scope: str
    lifecycle_id: str
    watermark: int
    provenance: Provenance

    def __post_init__(self) -> None:
        if not self.reconciliation_id or not self.reconciliation_id.strip():
            raise ValueError("reconciliation_id is required")
        if self.watermark < 0:
            raise ValueError("watermark must be non-negative")
        if not isinstance(self.account_scope, AccountScope):
            raise TypeError("account_scope must be an AccountScope")
        for name in ("broker_id", "account_address", "execution_scope", "lifecycle_id"):
            value = getattr(self, name)
            if not value or value != value.strip():
                raise ValueError(f"{name} is required")
        if not isinstance(self.provenance, Provenance):
            raise TypeError("provenance must be a Provenance")


@dataclass(frozen=True)
class ExternalTestnetLifecycleEvidence:
    """Non-secret evidence contract for one approved external lifecycle."""

    identity: EvidenceIdentity
    completed_steps: tuple[str, ...]
    final_reconciliation_id: str
    reconciliation: ReconciliationEvidence
    provenance: Provenance

    def __post_init__(self) -> None:
        if self.identity.evidence_class is not EvidenceClass.TESTNET:
            raise ValueError("Testnet lifecycle evidence requires the Testnet evidence class")
        if not isinstance(self.completed_steps, tuple) or any(
            step not in TESTNET_LIFECYCLE_STEPS for step in self.completed_steps
        ):
            raise ValueError("completed_steps contains an unknown lifecycle step")
        if set(self.completed_steps) != set(TESTNET_LIFECYCLE_STEPS):
            raise ValueError("Testnet lifecycle evidence requires every proof step")
        if self.final_reconciliation_id not in self.identity.reconciliation_ids:
            raise ValueError("final reconciliation ID must be part of the evidence identity")
        if self.reconciliation.reconciliation_id != self.final_reconciliation_id:
            raise ValueError("reconciliation fact does not match the final reconciliation ID")
        if self.reconciliation.environment is not BrokerEnvironment.TESTNET:
            raise ValueError("Testnet lifecycle evidence requires Testnet reconciliation")
        if self.reconciliation.account_address != self.identity.account_address:
            raise ValueError("reconciliation account does not match evidence identity")
        if self.reconciliation.execution_scope != self.identity.execution_scope:
            raise ValueError("reconciliation scope does not match evidence identity")
        if not isinstance(self.provenance, Provenance):
            raise TypeError("provenance must be a Provenance")
        if self.provenance.execution_scope != self.identity.execution_scope:
            raise ValueError("evidence provenance scope does not match identity")
        if self.provenance.transport_state != "external_testnet":
            raise ValueError("Testnet lifecycle evidence must use external_testnet provenance")


@dataclass(frozen=True)
class RuntimeReadinessReport:
    passed: bool
    identity: EvidenceIdentity
    checks: tuple[str, ...]
    failures: tuple[str, ...]
    network_io: bool
    credential_required: bool
    real_money_eligible: bool
    external_activation_required: bool
    external_gate_verified: bool
    required_evidence_fields: tuple[str, ...]
    reconciliation_ids: tuple[str, ...]


def run_paper_runtime_readiness(
    *,
    runtime: object,
    binding: HostBrokerBinding,
    recording: RecordingTrack,
    reconciliation_events: tuple[ReconciliationEvidence, ...] = (),
) -> RuntimeReadinessReport:
    """Prove local Paper readiness without invoking a Broker network."""

    checks: list[str] = []
    failures: list[str] = []
    session = getattr(runtime, "session", None)
    health = getattr(runtime, "health", None)
    order_ids: set[str] = set()
    fill_ids: set[str] = set()
    fee_ids: set[str] = set()
    position_ids: set[str] = set()
    for event in recording.events:
        payload = event.payload
        order_id = getattr(payload, "order_id", None)
        fill_id = getattr(payload, "fill_id", None)
        fee_id = getattr(payload, "fee_id", None)
        observation_id = getattr(payload, "observation_id", None)
        if isinstance(order_id, str) and order_id:
            order_ids.add(order_id)
        if isinstance(fill_id, str) and fill_id:
            fill_ids.add(fill_id)
        if isinstance(fee_id, str) and fee_id:
            fee_ids.add(fee_id)
        nested_fee = getattr(payload, "fee", None)
        nested_fee_id = getattr(nested_fee, "fee_id", None)
        if isinstance(nested_fee_id, str) and nested_fee_id:
            fee_ids.add(nested_fee_id)
        if hasattr(payload, "signed_quantity") and isinstance(observation_id, str) and observation_id:
            position_ids.add(observation_id)
    reconciliation_ids = tuple(event.reconciliation_id for event in reconciliation_events)
    identity = EvidenceIdentity(
        evidence_class=EvidenceClass.PAPER,
        broker_id=session.broker_id,
        environment=session.environment,
        account_scope=session.account.scope,
        account_address=session.account.address,
        execution_scope=session.execution_scope,
        lifecycle_id=session.lifecycle_id,
        release_sha=None,
        order_ids=tuple(sorted(order_ids)),
        fill_ids=tuple(sorted(fill_ids)),
        fee_ids=tuple(sorted(fee_ids)),
        position_ids=tuple(sorted(position_ids)),
        reconciliation_ids=reconciliation_ids,
    )

    if (
        session.broker_id == binding.identity.broker_id
        and session.environment is binding.identity.environment
        and session.account.scope is binding.identity.account_scope
        and session.account.address == binding.identity.account_address
        and session.execution_scope == binding.identity.execution_scope
        and session.capabilities.revision == binding.capabilities.revision
    ):
        checks.append("runtime_binding_identity")
    else:
        failures.append("runtime_binding_identity")
    if session.signer.kind.value == "none":
        checks.append("paper_no_signer")
    else:
        failures.append("paper_signer")
    if binding.paper_only and not binding.real_money_eligible and binding.control_plane == "telegram":
        checks.append("host_safety_boundary")
    else:
        failures.append("host_safety_boundary")

    preflight = None
    try:
        preflight = binding.adapter.preflight()
    except (AttributeError, TypeError, ValueError):
        failures.append("paper_preflight")
    network_io = bool(getattr(health, "external_network", True) or getattr(preflight, "network_io", True))
    credential_required = bool(getattr(preflight, "credential_required", True))
    real_money_eligible = bool(
        getattr(health, "real_money_eligible", binding.real_money_eligible)
        or getattr(preflight, "real_money_eligible", True)
    )
    if network_io:
        failures.append("paper_network_io")
    else:
        checks.append("paper_network_safe")
    if credential_required:
        failures.append("paper_credentials")
    else:
        checks.append("paper_credentials_safe")
    if real_money_eligible:
        failures.append("paper_real_money")
    else:
        checks.append("paper_real_money_safe")

    if recording.events and all(
        isinstance(event.provenance, Provenance)
        and event.provenance.execution_scope == session.execution_scope
        and event.provenance.mapping_revision == binding.capabilities.revision
        and getattr(event.payload, "environment", BrokerEnvironment.PAPER) is BrokerEnvironment.PAPER
        and not getattr(event.payload, "network_io", False)
        and not getattr(event.payload, "real_money_eligible", False)
        for event in recording.events
    ):
        checks.append("recording_provenance")
    else:
        failures.append("recording_provenance" if recording.events else "recording_empty")
    if all((order_ids, fill_ids, fee_ids, position_ids, reconciliation_ids)):
        checks.append("evidence_field_coverage")
    else:
        failures.append("evidence_field_coverage")
    if reconciliation_events and all(
        event.broker_id == session.broker_id
        and event.environment is session.environment
        and event.account_scope is session.account.scope
        and event.account_address == session.account.address
        and event.execution_scope == session.execution_scope
        and event.lifecycle_id == session.lifecycle_id
        and event.provenance.execution_scope == session.execution_scope
        and event.provenance.mapping_revision == binding.capabilities.revision
        for event in reconciliation_events
    ):
        checks.append("reconciliation_evidence")
    else:
        failures.append("reconciliation_evidence")
    artifact = repr((identity, binding.identity, binding.capabilities, recording.events, reconciliation_events))
    if find_secret_like_literals(artifact):
        failures.append("recording_secret_scan")
    else:
        checks.append("recording_secret_safe")

    checks.append("testnet_human_gate_required")
    return RuntimeReadinessReport(
        passed=not failures,
        identity=identity,
        checks=tuple(checks),
        failures=tuple(failures),
        network_io=network_io,
        credential_required=credential_required,
        real_money_eligible=real_money_eligible,
        external_activation_required=True,
        external_gate_verified=False,
        required_evidence_fields=EVIDENCE_IDENTITY_FIELDS,
        reconciliation_ids=reconciliation_ids,
    )
