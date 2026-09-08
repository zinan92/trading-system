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

from services.broker_port import BrokerOrderRequest


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


class ExternalTestnetExecutionPort:
    """Fail-closed facade for the exact protected external Testnet profile.

    The object passed here is the already-constructed public trading-system
    adapter.  This module deliberately does not import venue types or read a
    signer file; the adapter owns those concerns.
    """

    port_name = "order_execution"
    profile_id = "hyperliquid-testnet-position-protection"

    def __init__(self, broker: object) -> None:
        self.broker = broker
        if getattr(broker, "transport_state", "") != "external_testnet":
            raise TestnetExecutionError("external_testnet_transport_required")
        config = getattr(broker, "broker_config", {})
        if not isinstance(config, Mapping) or config.get("transport_profile") != self.profile_id:
            raise TestnetExecutionError("external_protection_profile_required")
        if config.get("environment") != "testnet":
            raise TestnetExecutionError("external_testnet_environment_required")
        if config.get("real_money_eligible") is not False or config.get("live_trading_enabled") is True:
            raise TestnetExecutionError("external_testnet_real_money_forbidden")
        self._receipts: dict[str, dict[str, Any]] = {}

    def preflight(self) -> dict[str, Any]:
        result = self.broker.preflight(strategy_family="grid")
        if not isinstance(result, Mapping) or result.get("ready") is not True:
            raise TestnetExecutionError("external_testnet_preflight_blocked")
        return self._safe_mapping(result)

    def submit(self, request: CanonicalOrderRequest) -> dict[str, Any]:
        return self._order(request, "submit")

    def cancel(self, request: CanonicalOrderRequest) -> dict[str, Any]:
        return self._order(request, "cancel")

    def replace(self, request: CanonicalOrderRequest) -> dict[str, Any]:
        return self._order(request, "replace")

    def query(self, request: CanonicalOrderRequest) -> dict[str, Any]:
        try:
            raw = self.broker.request(self.port_name, "query", {"order_id": request.order_id})
        except Exception as exc:
            return self._unknown(request, "query", exc)
        return self._receipt(request, "query", raw)

    def reconcile_unknown(self, request: CanonicalOrderRequest) -> dict[str, Any]:
        return self.query(request)

    def read_facts(self, *, instrument_id: str, order_id: str = "") -> dict[str, Any]:
        try:
            raw = self.broker.read_facts(instrument_id=instrument_id, order_id=order_id)
        except Exception as exc:
            return {
                "state": "unknown",
                "unknown": True,
                "error_type": type(exc).__name__,
                "instrument_id": instrument_id,
                "order_id": order_id,
            }
        return self._safe_object(raw)

    def submit_protection(self, group: object) -> dict[str, Any]:
        return self._protection(group, "submit")

    def query_protection(self, group: object) -> dict[str, Any]:
        return self._protection(group, "query")

    def cancel_protection(self, group: object) -> dict[str, Any]:
        return self._protection(group, "cancel")

    def _order(self, request: CanonicalOrderRequest, operation: str) -> dict[str, Any]:
        key = request.idempotency_key
        if operation != "query" and key in self._receipts:
            return dict(self._receipts[key])
        ticket = {
            **request.to_dict(),
            "ticket_id": request.order_id,
            "idempotency_key": key,
            "order_type": "limit" if request.limit_price is not None else "market",
            "price": str(request.limit_price) if request.limit_price is not None else None,
        }
        try:
            raw = self.broker.submit_order(BrokerOrderRequest(
                run_date=request.command_id,
                ticket=ticket,
                latest_price=float(request.limit_price) if request.limit_price is not None else None,
                actual_size=float(request.quantity),
            )) if operation == "submit" else self.broker.request(self.port_name, operation, ticket)
        except Exception as exc:
            receipt = self._unknown(request, operation, exc)
            self._receipts[key] = receipt
            return receipt
        receipt = self._receipt(request, operation, raw)
        self._receipts[key] = receipt
        return receipt

    def _protection(self, group: object, operation: str) -> dict[str, Any]:
        try:
            raw = self.broker.request("protection_order", operation, group)
        except Exception as exc:
            return {
                "operation": operation,
                "state": "unknown",
                "accepted": False,
                "unknown": True,
                "error_type": type(exc).__name__,
                "protection_id": str(getattr(group, "protection_id", "")),
            }
        return self._safe_object(raw)

    @staticmethod
    def _safe_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
        return {str(key): ExternalTestnetExecutionPort._safe_value(item) for key, item in value.items()}

    @classmethod
    def _safe_object(cls, value: object) -> dict[str, Any]:
        if isinstance(value, Mapping):
            return cls._safe_mapping(value)
        fields = getattr(value, "__dataclass_fields__", {})
        if fields:
            return {name: cls._safe_value(getattr(value, name)) for name in fields}
        return {"state": str(getattr(value, "state", "unknown")), "accepted": bool(getattr(value, "accepted", False))}

    @classmethod
    def _safe_value(cls, value: object) -> Any:
        if isinstance(value, Mapping):
            return cls._safe_mapping(value)
        if isinstance(value, (list, tuple)):
            return [cls._safe_value(item) for item in value]
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if hasattr(value, "value"):
            return cls._safe_value(value.value)
        if getattr(value, "__dataclass_fields__", None):
            return cls._safe_object(value)
        return str(value)

    @classmethod
    def _receipt(cls, request: CanonicalOrderRequest, operation: str, raw: object) -> dict[str, Any]:
        result = cls._safe_object(raw)
        accepted = result.get("accepted") is True or str(result.get("state", "")).lower() in {"resting", "accepted", "active", "submitted"}
        return {
            "receipt_id": str(result.get("receipt_id") or f"external:{request.idempotency_key}"),
            "operation": operation,
            "port": cls.port_name,
            "state": str(result.get("state") or ("accepted" if accepted else "rejected")),
            "accepted": accepted,
            "unknown": False,
            "network_io": True,
            "real_money_eligible": False,
            "request": request.to_dict(),
            "broker": result,
        }

    @staticmethod
    def _unknown(request: CanonicalOrderRequest, operation: str, exc: Exception) -> dict[str, Any]:
        return {
            "receipt_id": f"external-unknown:{request.order_id}",
            "operation": operation,
            "port": ExternalTestnetExecutionPort.port_name,
            "state": "unknown",
            "accepted": False,
            "unknown": True,
            "error_type": type(exc).__name__,
            "network_io": True,
            "real_money_eligible": False,
            "request": request.to_dict(),
        }


TestnetExecutionPort = ExternalTestnetExecutionPort

__all__ = [
    "CanonicalOrderRequest",
    "ExternalTestnetExecutionPort",
    "PaperExecutionPort",
    "TestnetExecutionError",
    "TestnetExecutionPort",
    "map_grid_orders",
]
