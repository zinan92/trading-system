"""Provider-neutral Paper conformance and evidence checks."""

from dataclasses import dataclass
from enum import Enum

from .capabilities import PORT_NAMES
from .errors import BrokerError
from .models import BrokerEnvironment, SignerKind


class EvidenceKind(str, Enum):
    STATIC_FIXTURE = "static_fixture"


@dataclass(frozen=True)
class ConformanceReport:
    passed: bool
    evidence_kind: EvidenceKind
    checks: tuple[str, ...]
    failures: tuple[str, ...]
    network_io: bool
    real_money_eligible: bool


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
