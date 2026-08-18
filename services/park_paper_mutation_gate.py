"""Runtime-scoped, identity-bound mutation capability for Park Paper."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any, Callable, Mapping

_CAPABILITY_SECRET = object()
_GATE_FACTORY_SECRET = object()


@dataclass(frozen=True)
class ParkPaperMutationCapability:
    """Opaque capability minted only after Park runtime admission."""

    _secret: object
    cycle_id: str
    strategy_session_id: str
    strategy_revision_id: str
    plan_digest: str
    park_confirmation_digest: str
    cutover_status: str
    nonce: str
    # A legacy cutover capability is deliberately scoped to exact cancellation
    # only. It cannot be reused to submit entries or manage Park positions.
    legacy_cutover_id: str = ""
    operation: str = ""

    @classmethod
    def _create(cls, receipt: Mapping[str, Any]) -> "ParkPaperMutationCapability":
        required = (
            "issuer",
            "cycle_id",
            "strategy_session_id",
            "strategy_revision_id",
            "plan_digest",
            "park_confirmation_digest",
            "cutover_status",
        )
        if any(not str(receipt.get(field) or "").strip() for field in required):
            raise RuntimeError("Park mutation capability is incomplete")
        issuer = str(receipt.get("issuer") or "")
        if issuer not in {"ParkPaperRuntime.run_once", "ParkLegacyCutoverRuntime.run_once"}:
            raise RuntimeError("Park mutation capability issuer is invalid")
        if str(receipt.get("cutover_status")) != "pass":
            raise RuntimeError("Park mutation capability requires a passing cutover")
        legacy_cutover_id = str(receipt.get("legacy_cutover_id") or "").strip()
        operation = str(receipt.get("operation") or "").strip()
        if legacy_cutover_id and (issuer != "ParkLegacyCutoverRuntime.run_once" or operation != "cancel_legacy_orders"):
            raise RuntimeError("legacy cutover mutation capability scope is invalid")
        return cls(
            _secret=_CAPABILITY_SECRET,
            cycle_id=str(receipt["cycle_id"]),
            strategy_session_id=str(receipt["strategy_session_id"]),
            strategy_revision_id=str(receipt["strategy_revision_id"]),
            plan_digest=str(receipt["plan_digest"]),
            park_confirmation_digest=str(receipt["park_confirmation_digest"]),
            cutover_status="pass",
            nonce=secrets.token_urlsafe(18),
            legacy_cutover_id=legacy_cutover_id,
            operation=operation,
        )


def _mint_park_paper_capability(receipt: Mapping[str, Any]) -> ParkPaperMutationCapability:
    """Private runtime seam; callers never authorize with a plain mapping."""

    return ParkPaperMutationCapability._create(receipt)


class ParkPaperMutationGate:
    """Keep a direct adapter inert until an opaque capability is activated."""

    def __init__(self, _factory_secret: object) -> None:
        if _factory_secret is not _GATE_FACTORY_SECRET:
            raise RuntimeError("Park mutation gate must be created by the Park Paper factory")
        self._capability: ParkPaperMutationCapability | None = None

    @property
    def authorized(self) -> bool:
        return self._capability is not None

    def _activate(self, capability: ParkPaperMutationCapability) -> None:
        if not isinstance(capability, ParkPaperMutationCapability):
            raise RuntimeError("Park mutation capability must be opaque")
        if capability._secret is not _CAPABILITY_SECRET:
            raise RuntimeError("Park mutation capability provenance is invalid")
        self._capability = capability

    def revoke(self) -> None:
        self._capability = None

    def require(
        self,
        *,
        cycle_id: str | None = None,
        command: Mapping[str, Any] | None = None,
        strategy_plan_id: str | None = None,
        operation: str | None = None,
        legacy_cutover_id: str | None = None,
    ) -> None:
        capability = self._capability
        if capability is None:
            raise RuntimeError("Park adapter mutation requires runtime admission")
        if capability.legacy_cutover_id:
            if operation != capability.operation or str(legacy_cutover_id or "") != capability.legacy_cutover_id:
                raise RuntimeError("legacy cutover mutation operation is not authorized")
            if command is not None and str(command.get("legacy_cutover_id") or "") != capability.legacy_cutover_id:
                raise RuntimeError("legacy cutover command identity is not authorized")
            return
        if cycle_id is not None and str(cycle_id) != capability.cycle_id:
            raise RuntimeError("Park mutation cycle does not match admitted strategy session")
        if operation is not None or legacy_cutover_id not in (None, ""):
            raise RuntimeError("normal Park mutation cannot use legacy cutover scope")
        if strategy_plan_id not in (None, "", capability.plan_digest):
            raise RuntimeError("Park mutation plan does not match admitted revision")
        if command is None:
            return
        if str(command.get("strategy_session_id") or "") != capability.strategy_session_id:
            raise RuntimeError("Park command session does not match admitted revision")
        if str(command.get("strategy_revision_id") or "") != capability.strategy_revision_id:
            raise RuntimeError("Park command revision does not match admitted revision")
        if str(command.get("plan_digest") or command.get("strategy_plan_id") or "") != capability.plan_digest:
            raise RuntimeError("Park command plan does not match admitted revision")


def _new_park_paper_mutation_gate() -> ParkPaperMutationGate:
    """Private factory seam used only by the direct Park adapter composition."""

    return ParkPaperMutationGate(_GATE_FACTORY_SECRET)


@dataclass(frozen=True)
class ParkPaperAdapterBinding:
    """Adapter plus a runtime-only capability seam, kept off the adapter."""

    adapter: Any
    authorize: Callable[[ParkPaperMutationCapability], None]
    revoke: Callable[[], None]
