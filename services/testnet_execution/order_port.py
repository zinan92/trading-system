"""Coordinator-owned mapping to the public standard-broker OrderPort seam.

This module deliberately contains no venue types.  The Paper adapter is passed
in by the composition root and is only observed through its public
``request(port, operation, payload)`` contract.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping, Sequence


class TestnetExecutionError(RuntimeError):
    """A durable, fail-closed execution seam error."""

    __test__ = False

    def __init__(self, code: str, evidence: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.evidence = dict(evidence or {})
        super().__init__(code)


def _text(value: Any, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise TestnetExecutionError(f"{field}_required")
    return result


def _stable_id(*parts: object) -> str:
    raw = "|".join(str(part) for part in parts).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class CanonicalOrderRequest:
    """Provider-neutral order request with immutable activation provenance."""

    activation_id: str
    plan_digest: str
    slice_id: str
    order_id: str
    instrument_id: str
    side: str
    quantity: Decimal
    limit_price: Decimal | None
    idempotency_key: str
    command_id: str
    operation: str = "submit"

    def __post_init__(self) -> None:
        for field in ("activation_id", "plan_digest", "slice_id", "order_id", "instrument_id", "command_id"):
            _text(getattr(self, field), field)
        if self.side not in {"buy", "sell"}:
            raise TestnetExecutionError("order_side_invalid")
        if self.quantity <= 0 or not self.quantity.is_finite():
            raise TestnetExecutionError("order_quantity_invalid")
        if self.limit_price is not None and (self.limit_price <= 0 or not self.limit_price.is_finite()):
            raise TestnetExecutionError("order_price_invalid")
        if self.operation not in {"submit", "cancel", "replace", "query"}:
            raise TestnetExecutionError("order_operation_invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "activation_id": self.activation_id,
            "plan_digest": self.plan_digest,
            "slice_id": self.slice_id,
            "order_id": self.order_id,
            "instrument_id": self.instrument_id,
            "side": self.side,
            "quantity": str(self.quantity),
            "limit_price": str(self.limit_price) if self.limit_price is not None else None,
            "idempotency_key": self.idempotency_key,
            "command_id": self.command_id,
            "operation": self.operation,
        }


def map_grid_orders(
    plan: Mapping[str, Any],
    *,
    activation_id: str,
    slice_id: str,
    command_id: str,
) -> tuple[CanonicalOrderRequest, ...]:
    """Map every Grid rung to one canonical request before any side effect."""

    plan_digest = _text(plan.get("plan_digest"), "plan_digest")
    instrument_id = _text(
        plan.get("instrument_id") or (plan.get("execution_context") or {}).get("instrument_id"),
        "instrument_id",
    )
    grid = plan.get("grid")
    rungs = grid.get("rungs") if isinstance(grid, Mapping) else plan.get("rungs")
    if not isinstance(rungs, Sequence) or isinstance(rungs, (str, bytes)):
        raise TestnetExecutionError("grid_rungs_required")
    requests: list[CanonicalOrderRequest] = []
    for index, rung in enumerate(rungs):
        if not isinstance(rung, Mapping):
            raise TestnetExecutionError("grid_rung_invalid", {"index": index})
        rung_id = str(rung.get("rung") or index + 1)
        order_id = f"{slice_id}:rung:{rung_id}:entry"
        requests.append(
            CanonicalOrderRequest(
                activation_id=_text(activation_id, "activation_id"),
                plan_digest=plan_digest,
                slice_id=_text(slice_id, "slice_id"),
                order_id=order_id,
                instrument_id=instrument_id,
                side=_text(rung.get("side"), "order_side").lower(),
                quantity=Decimal(str(rung.get("quantity"))),
                limit_price=Decimal(str(rung.get("price"))) if rung.get("price") is not None else None,
                idempotency_key=_stable_id(activation_id, plan_digest, slice_id, order_id),
                command_id=_text(command_id, "command_id"),
            )
        )
    return tuple(requests)


class PaperExecutionPort:
    """A local Paper OrderPort facade with durable idempotency and unknown stop."""

    port_name = "order_execution"

    def __init__(self, broker: object) -> None:
        self.broker = broker
        self._receipts: dict[str, dict[str, Any]] = {}
        self._unknown_queries: set[str] = set()

    def _call(self, request: CanonicalOrderRequest, operation: str) -> dict[str, Any]:
        key = request.idempotency_key
        if key in self._receipts and operation != "query":
            return dict(self._receipts[key])
        try:
            raw = self.broker.request(self.port_name, operation, request.to_dict())
        except Exception as exc:  # Unknown side effect: query once, never retry blindly.
            receipt = self._unknown(request, operation, f"{type(exc).__name__}:{exc}")
            self._receipts[key] = receipt
            return receipt
        accepted = bool(getattr(raw, "accepted", raw.get("accepted", False) if isinstance(raw, Mapping) else False))
        receipt = {
            "receipt_id": f"paper-receipt:{key[7:19] if key.startswith('sha256:') else key[:12]}",
            "operation": operation,
            "port": self.port_name,
            "state": "accepted" if accepted else "rejected",
            "accepted": accepted,
            "unknown": False,
            "network_io": False,
            "real_money_eligible": False,
            "request": request.to_dict(),
        }
        self._receipts[key] = receipt
        return dict(receipt)

    def _unknown(self, request: CanonicalOrderRequest, operation: str, reason: str) -> dict[str, Any]:
        return {
            "receipt_id": f"paper-unknown:{request.order_id}",
            "operation": operation,
            "port": self.port_name,
            "state": "unknown",
            "accepted": False,
            "unknown": True,
            "unknown_reason": reason,
            "reconcile_allowed": request.idempotency_key not in self._unknown_queries,
            "network_io": False,
            "real_money_eligible": False,
            "request": request.to_dict(),
        }

    def submit(self, request: CanonicalOrderRequest) -> dict[str, Any]:
        return self._call(request, "submit")

    def cancel(self, request: CanonicalOrderRequest) -> dict[str, Any]:
        return self._call(request, "cancel")

    def replace(self, request: CanonicalOrderRequest) -> dict[str, Any]:
        return self._call(request, "replace")

    def reconcile_unknown(self, request: CanonicalOrderRequest) -> dict[str, Any]:
        key = request.idempotency_key
        if key in self._unknown_queries:
            return {"state": "unknown", "unknown": True, "reconcile_allowed": False, "request": request.to_dict()}
        self._unknown_queries.add(key)
        return self._call(request, "query")


__all__ = ["CanonicalOrderRequest", "PaperExecutionPort", "TestnetExecutionError", "map_grid_orders"]
