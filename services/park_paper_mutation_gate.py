"""Runtime-scoped mutation capability for the Park Paper adapter."""

from __future__ import annotations

from typing import Any, Mapping


class ParkPaperMutationGate:
    """Keep the direct adapter inert until Park runtime admission passes.

    Adapter construction is intentionally not an execution authorization.  A
    Park runtime must grant this short-lived capability after it has checked
    the exact Park confirmation and all deterministic safety gates.  The
    adapter consumes the same gate for every mutating call, so an imported
    factory cannot silently become an order path.
    """

    def __init__(self) -> None:
        self._receipt: dict[str, Any] | None = None

    @property
    def authorized(self) -> bool:
        return self._receipt is not None

    def grant(self, receipt: Mapping[str, Any]) -> None:
        required = (
            "issuer",
            "strategy_session_id",
            "strategy_revision_id",
            "plan_digest",
            "park_confirmation_digest",
            "cutover_status",
        )
        if any(not str(receipt.get(field) or "").strip() for field in required):
            raise RuntimeError("Park mutation capability is incomplete")
        if str(receipt.get("issuer")) != "ParkPaperRuntime.run_once":
            raise RuntimeError("Park mutation capability issuer is invalid")
        if str(receipt.get("cutover_status")) != "pass":
            raise RuntimeError("Park mutation capability requires a passing cutover")
        self._receipt = dict(receipt)

    def revoke(self) -> None:
        self._receipt = None

    def require(self) -> None:
        if self._receipt is None:
            raise RuntimeError("Park adapter mutation requires runtime admission")

