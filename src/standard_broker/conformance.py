"""Provider-neutral Paper conformance and evidence checks."""

from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, time
from enum import Enum
from collections.abc import Iterable, Mapping
from decimal import Decimal
from pathlib import Path
import re
import hashlib

from .capabilities import PORT_NAMES
from .errors import BrokerError
from .models import BrokerEnvironment, SignerKind
from .security import find_secret_like_literals


_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_HANDOFF_SHA256 = "e7cded8b2112fbf258bcd2563ef90c15b7eee7aa8b2998d8e66949a6f09b5dcf"

_EXPECTED_EXTERNAL_PROTECTION_VALUES = {
    "submit": False,
    "cancel": False,
    "replace": False,
    "query": False,
    "retry": False,
    "position_coverage": False,
    "partial_fill_repair": False,
    "reduce_only_close": True,
    "reduce_only": False,
    "mark_price_trigger": False,
    "grouped_tp_sl": False,
    "sibling_cancellation": False,
    "bracket": False,
    "parent_child": False,
    "fixed_size": False,
    "position_following": False,
    "position_level_tpsl": False,
    "take_profit_market": False,
    "take_profit_limit": False,
    "stop_loss_market": False,
    "stop_loss_limit": False,
    "cancel_replace": False,
}


class EvidenceKind(str, Enum):
    STATIC_FIXTURE = "static_fixture"
    EXTERNAL_STATIC_FIXTURE = "external_static_fixture"


@dataclass(frozen=True)
class ConformanceReport:
    passed: bool
    evidence_kind: EvidenceKind
    checks: tuple[str, ...]
    failures: tuple[str, ...]
    network_io: bool
    real_money_eligible: bool


@dataclass(frozen=True)
class ExternalConformanceReport:
    """Static proof of the public external host contract without transport I/O."""

    passed: bool
    evidence_kind: EvidenceKind
    checks: tuple[str, ...]
    failures: tuple[str, ...]
    network_io: bool
    credential_required: bool
    write_credential_required: bool
    real_money_eligible: bool
    external_network_bound: bool
    handoff: str


def run_paper_conformance(adapter: object) -> ConformanceReport:
    """Check one adapter through its public Paper seam without invoking a request."""

    checks: list[str] = []
    failures: list[str] = []
    identity = getattr(adapter, "identity", None)
    capabilities = getattr(adapter, "capabilities", None)
    preflight = None
    try:
        preflight = adapter.preflight()
    except (AttributeError, BrokerError, TypeError, ValueError):
        failures.append("paper_preflight")

    network_io = bool(getattr(preflight, "network_io", True))
    real_money_eligible = bool(getattr(preflight, "real_money_eligible", True))
    credential_required = bool(getattr(preflight, "credential_required", True))

    if identity is not None and getattr(identity, "environment", None) is BrokerEnvironment.PAPER:
        checks.append("paper_environment")
    else:
        failures.append("paper_environment")
    if identity is not None and getattr(identity, "signer_kind", None) is SignerKind.NONE:
        checks.append("paper_signer")
    else:
        failures.append("paper_signer")

    port_names = tuple(getattr(adapter, "ports", {}).keys())
    if set(port_names) == set(PORT_NAMES):
        checks.append("canonical_ports")
    else:
        failures.append("canonical_ports")

    if (
        identity is not None
        and capabilities is not None
        and getattr(capabilities, "broker_id", None) == getattr(identity, "broker_id", None)
        and getattr(capabilities, "environment", None) is getattr(identity, "environment", None)
    ):
        checks.append("capability_identity")
    else:
        failures.append("capability_identity")

    if network_io:
        failures.append("paper_network_io")
    else:
        checks.append("paper_boundary")
    if credential_required:
        failures.append("paper_credentials")
    else:
        checks.append("no_credentials")
    if real_money_eligible:
        failures.append("paper_real_money")
    else:
        checks.append("no_real_money")

    return ConformanceReport(
        passed=not failures,
        evidence_kind=EvidenceKind.STATIC_FIXTURE,
        checks=tuple(checks),
        failures=tuple(failures),
        network_io=network_io,
        real_money_eligible=real_money_eligible,
    )


def run_external_conformance(
    *,
    host: object,
    read_facts: Iterable[object],
    order_receipts: Iterable[object],
    reconciliation: object,
) -> ExternalConformanceReport:
    """Prove the external public seam with fixture inputs and preflight only.

    This function intentionally does not call ``host.request`` or any runtime
    private method.  A later host integration may use the report as static
    evidence, but it is not an external execution proof or soak result.
    """

    from .external_host import ExternalBrokerHost, ExternalFactEnvelope
    from .external_reconciliation import ExternalReconciliationSnapshot
    from .orders import OrderReceipt
    from .adapters.hyperliquid.profile import (
        HYPERLIQUID_TESTNET_PROFILE,
        resolve_external_profile,
    )

    checks: list[str] = []
    failures: list[str] = []
    handoff = "docs/handoffs/external-testnet-to-trading-system.md"
    identity = getattr(host, "identity", None)
    context = getattr(host, "context", None)
    capabilities = getattr(host, "capabilities", None)
    runtime_identity = getattr(host, "runtime_identity", None)
    if not isinstance(host, ExternalBrokerHost):
        failures.append("public_external_host")
    elif all(callable(getattr(host, name, None)) for name in ("preflight", "authorize", "request")):
        checks.append("public_external_host")
    else:
        failures.append("public_external_host")

    try:
        exact_profile = resolve_external_profile(HYPERLIQUID_TESTNET_PROFILE.profile_id)
    except (BrokerError, TypeError, ValueError):
        exact_profile = None
    if (
        exact_profile is not None
        and getattr(host, "external_profile_id", None) == exact_profile.profile_id
        and identity is not None
        and identity.environment is exact_profile.environment
        and identity.broker_id == exact_profile.broker_id
        and identity.execution_scope == exact_profile.execution_scope
        and identity.signer_kind is exact_profile.signer_kind
        and runtime_identity is not None
        and runtime_identity.adapter_id == exact_profile.adapter_id
        and runtime_identity.version == exact_profile.version
        and runtime_identity.commit == exact_profile.commit
        and runtime_identity.mapping_revision == exact_profile.mapping_revision
        and runtime_identity.transport_state == exact_profile.transport_state
        and context is not None
        and context.capabilities == exact_profile.capabilities
        and context.release_sha
        and getattr(host, "protection_capabilities", None) == exact_profile.protection_capabilities
    ):
        checks.append("exact_testnet_identity")
    else:
        failures.append("exact_testnet_identity")

    required_operations = {
        "market_data": {"ticker"},
        "instrument": {"read"},
        "account": {"read", "positions"},
        "order_execution": {"submit", "cancel", "replace", "query", "open_orders", "fills"},
        "fee": {"read", "schedule", "fill"},
    }
    write_requested = any(
        operation in {"submit", "cancel", "replace", "cancel_replace", "modify"}
        for operations in required_operations.values()
        for operation in operations
    )
    preflight = None
    try:
        preflight = host.preflight(
            request_id="external-conformance-preflight",
            required_operations=required_operations,
        )
    except (AttributeError, BrokerError, TypeError, ValueError):
        failures.append("external_preflight")
    if preflight is not None:
        if preflight.network_io is True and preflight.real_money_eligible is False:
            checks.append("external_network_profile")
        else:
            failures.append("external_network_profile")
        if write_requested and identity is not None and identity.signer_kind is not SignerKind.NONE:
            checks.append("write_credential_gate_declared")
        else:
            failures.append("write_credential_gate_declared")
        if capabilities is not None and preflight.capability_revision == capabilities.revision:
            checks.append("capability_preflight")
        else:
            failures.append("capability_preflight")

    facts = tuple(read_facts)
    if facts and all(
        _fact_matches_host(fact, identity=identity, context=context, runtime_identity=runtime_identity)
        for fact in facts
    ):
        checks.append("canonical_read_facts")
    else:
        failures.append("canonical_read_facts")

    receipts = tuple(order_receipts)
    if receipts and identity is not None and context is not None and all(
        isinstance(receipt, OrderReceipt)
        and _receipt_matches_host(receipt, identity=identity, context=context, runtime_identity=runtime_identity)
        for receipt in receipts
    ):
        checks.append("canonical_order_receipts")
    else:
        failures.append("canonical_order_receipts")

    if (
        isinstance(reconciliation, ExternalReconciliationSnapshot)
        and reconciliation.passed
        and _reconciliation_matches_host(reconciliation, identity=identity, context=context, runtime_identity=runtime_identity)
    ):
        try:
            reconciliation.verify_integrity()
            expected_orders = reconciliation.open_orders.fact.data if reconciliation.open_orders is not None else ()
            if tuple(receipts) != tuple(expected_orders):
                failures.append("order_evidence_binding")
            else:
                checks.append("order_evidence_binding")
            reconciliation.require_coherent()
            checks.append("reconciliation_evidence")
            if all(
                getattr(reconciliation, name)
                for name in ("request_digests", "receipt_digests", "raw_payload_digests", "fact_digests", "evidence_digest")
            ):
                checks.append("evidence_digests")
            else:
                failures.append("evidence_digests")
        except BrokerError:
            failures.append("reconciliation_evidence")
    else:
        failures.append("reconciliation_evidence")

    protection = getattr(host, "protection_capabilities", None)
    if (
        protection is not None
        and dict(protection.values) == _EXPECTED_EXTERNAL_PROTECTION_VALUES
    ):
        checks.append("protection_capability_gaps")
    else:
        failures.append("protection_capability_gaps")

    if all(_safe_canonical(value) for value in (facts, receipts, reconciliation)):
        checks.append("secret_and_raw_redaction")
    else:
        failures.append("secret_and_raw_redaction")
    if _handoff_is_valid(handoff):
        checks.append("trading_system_handoff")
    else:
        failures.append("trading_system_handoff")

    return ExternalConformanceReport(
        passed=not failures,
        evidence_kind=EvidenceKind.EXTERNAL_STATIC_FIXTURE,
        checks=tuple(checks),
        failures=tuple(failures),
        network_io=False,
        credential_required=False,
        write_credential_required=write_requested,
        real_money_eligible=False,
        external_network_bound=bool(getattr(preflight, "network_io", False)),
        handoff=handoff,
    )


def _safe_canonical(value: object, _seen: set[int] | None = None) -> bool:
    """Reject mappings, bytes, and secret-like fields from public evidence."""

    seen = _seen if _seen is not None else set()
    if isinstance(value, (bytes, bytearray, memoryview, Mapping)):
        return False
    if isinstance(value, (list, tuple, set, frozenset)):
        marker = id(value)
        if marker in seen:
            return False
        seen.add(marker)
        try:
            return all(_safe_canonical(item, seen) for item in value)
        finally:
            seen.remove(marker)
    if is_dataclass(value):
        marker = id(value)
        if marker in seen:
            return False
        seen.add(marker)
        for field in fields(value):
            field_name = field.name.lower()
            if field_name not in {"raw_payload_digest", "raw_payload_digests"} and (
                "raw_payload" in field_name
                or any(
                    token in field_name
                    for token in (
                        "private_key",
                        "signed_payload",
                        "signature",
                        "api_key",
                        "access_token",
                        "credential",
                    )
                )
            ):
                return False
            if not _safe_canonical(getattr(value, field.name), seen):
                seen.remove(marker)
                return False
        seen.remove(marker)
        return True
    if isinstance(value, Enum):
        return _safe_canonical(value.value)
    if isinstance(value, (Decimal, datetime, date, time)):
        return True
    if isinstance(value, str):
        lowered = value.lower()
        if find_secret_like_literals(value) or any(
            token in lowered
            for token in ("private_key", "signed_payload", "signature", "api_key", "access_token", "secret")
        ):
            return False
        return True
    return value is None or isinstance(value, (bool, int, float))


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def _fact_matches_host(fact: object, *, identity: object, context: object, runtime_identity: object) -> bool:
    from .external_host import ExternalFactEnvelope, digest_canonical
    from .models import Provenance

    if not isinstance(fact, ExternalFactEnvelope) or identity is None or context is None:
        return False
    if not (
        fact.broker_id == identity.broker_id
        and fact.environment is identity.environment
        and fact.account_scope is identity.account_scope
        and fact.account_address == identity.account_address
        and fact.signer_kind is identity.signer_kind
        and fact.execution_scope == identity.execution_scope
        and fact.lifecycle_id == context.session.lifecycle_id
        and fact.release_sha == context.release_sha
        and fact.runtime_identity == runtime_identity
        and fact.capability_revision == context.capabilities.revision
        and isinstance(fact.provenance, Provenance)
        and fact.provenance.execution_scope == identity.execution_scope
        and fact.provenance.transport_state == runtime_identity.transport_state
        and fact.provenance.mapping_revision == context.capabilities.revision
        and fact.provenance.source == f"{runtime_identity.adapter_id}.testnet"
        and _is_digest(fact.request_digest)
        and _is_digest(fact.fact_digest)
        and _is_digest(fact.raw_payload_digest)
        and _safe_canonical(fact.data)
    ):
        return False
    expected = digest_canonical(
        {
            "fact_type": fact.fact_type,
            "data": fact.data,
            "request_digest": fact.request_digest,
            "raw_payload_digest": fact.raw_payload_digest,
            "broker_id": fact.broker_id,
            "environment": fact.environment,
            "account_scope": fact.account_scope,
            "account_address": fact.account_address,
            "signer_kind": fact.signer_kind,
            "execution_scope": fact.execution_scope,
            "lifecycle_id": fact.lifecycle_id,
            "release_sha": fact.release_sha,
            "runtime_identity": fact.runtime_identity,
            "capability_revision": fact.capability_revision,
            "provenance": fact.provenance,
        }
    )
    return fact.fact_digest == expected


def _receipt_matches_host(receipt: object, *, identity: object, context: object, runtime_identity: object) -> bool:
    from .models import Provenance

    return (
        identity is not None
        and context is not None
        and isinstance(receipt.provenance, Provenance)
        and receipt.broker_id == identity.broker_id
        and receipt.environment is identity.environment
        and receipt.account_address == identity.account_address
        and receipt.lifecycle_id == context.session.lifecycle_id
        and receipt.release_sha == context.release_sha
        and receipt.provenance.execution_scope == identity.execution_scope
        and receipt.provenance.transport_state == runtime_identity.transport_state
        and receipt.provenance.mapping_revision == context.capabilities.revision
        and receipt.provenance.source == f"{runtime_identity.adapter_id}.testnet"
        and _safe_canonical(receipt)
    )


def _reconciliation_matches_host(snapshot: object, *, identity: object, context: object, runtime_identity: object) -> bool:
    if identity is None or context is None or snapshot.identity is None:
        return False
    snapshot_identity = snapshot.identity
    return (
        snapshot_identity.broker_id == identity.broker_id
        and snapshot_identity.environment is identity.environment
        and snapshot_identity.account_scope is identity.account_scope
        and snapshot_identity.account_address == identity.account_address
        and snapshot_identity.signer_kind is identity.signer_kind
        and snapshot_identity.execution_scope == identity.execution_scope
        and snapshot_identity.lifecycle_id == context.session.lifecycle_id
        and snapshot_identity.release_sha == context.release_sha
        and snapshot_identity.runtime_identity == runtime_identity
        and snapshot_identity.capability_revision == context.capabilities.revision
        and all(
            _is_digest(value)
            for value in (
                *snapshot.request_digests,
                *snapshot.receipt_digests,
                *snapshot.raw_payload_digests,
                *snapshot.fact_digests,
                snapshot.evidence_digest,
            )
        )
        and _safe_canonical(snapshot)
    )


def _handoff_is_valid(relative_path: str) -> bool:
    path = Path(__file__).resolve().parents[2] / relative_path
    try:
        raw = path.read_bytes()
        content = raw.decode("utf-8")
    except OSError:
        return False
    required = (
        "standard-broker",
        "trading-system",
        "private runtime methods",
        "native Hyperliquid payloads",
        "Mainnet/live",
    )
    return hashlib.sha256(raw).hexdigest() == _HANDOFF_SHA256 and all(item in content for item in required)
