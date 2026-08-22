"""Attended Live DCA canary state machine.

The default transport used by tests is a local fixture.  This module never
constructs a venue client and never discovers credentials.  A future Live
adapter may supply a transport explicitly, but every operation remains behind
the source-bound activation prerequisite and the same risk/protection gates.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from pathlib import Path
from typing import Any, Mapping, Protocol

from services.journal_store import load_json, write_json
from services.live_activation_gate import LiveActivationGate, _digest


CANARY_SCHEMA = "live-dca-canary-v1"
REQUIRED_TRANSPORT_CAPABILITIES = frozenset(
    {
        "submit_entry",
        "cancel_order",
        "replace_protection",
        "query_order",
        "account_snapshot",
        "reconcile",
        "flatten_reduce_only",
    }
)
REQUIRED_RISK_LIMITS = (
    "max_acceptable_loss",
    "max_notional",
    "max_leverage",
    "max_open_orders",
    "max_positions",
    "max_slippage",
)


class LiveDcaCanaryError(RuntimeError):
    def __init__(self, code: str, message: str, evidence: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = str(code)
        self.evidence = dict(evidence or {})


class LiveDcaTransport(Protocol):
    """Minimal canonical transport; implementations own venue wire details."""

    capabilities: Any
    network_io: bool

    def submit_entry(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def cancel_order(self, order_id: str) -> Mapping[str, Any]: ...
    def replace_protection(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def query_order(self, order_id: str) -> Mapping[str, Any]: ...
    def account_snapshot(self) -> Mapping[str, Any]: ...
    def reconcile(self, expected: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def flatten_reduce_only(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...


def _number(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise LiveDcaCanaryError("risk_limit_invalid", f"{name} must be numeric") from exc
    if not math.isfinite(result) or result <= 0:
        raise LiveDcaCanaryError("risk_limit_invalid", f"{name} must be positive and finite")
    return result


class LiveDcaCanary:
    """One attended, bounded DCA Live canary over an injected transport."""

    def __init__(
        self,
        output_root: Path,
        *,
        transport: LiveDcaTransport,
        park_user_id: str,
        park_chat_id: str | int,
        gate: LiveActivationGate | None = None,
        allow_network: bool = False,
        now: Any | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.root = self.output_root / "dualtrack" / "live_dca_canary"
        self.path = self.root / "current.json"
        self.transport = transport
        self.now = now or time.time
        self.gate = gate or LiveActivationGate(
            self.output_root,
            park_user_id=park_user_id,
            park_chat_id=park_chat_id,
        )
        if bool(getattr(transport, "network_io", False)) and not allow_network:
            raise LiveDcaCanaryError("network_transport_not_allowed", "the default attended harness accepts only a no-network transport")
        self.allow_network = bool(allow_network)

    def snapshot(self) -> dict[str, Any]:
        rows = load_json(self.path)
        return dict(rows[-1]) if rows and isinstance(rows[-1], Mapping) else {}

    def start(self, plan: Mapping[str, Any], *, timestamp: str) -> dict[str, Any]:
        admission = self.gate.activation_prerequisite_status()
        self._require(admission.get("ready") is True, "activation_prerequisite_blocked", admission)
        normalized = self._validate_plan(plan, admission)
        capabilities = self._capabilities()
        missing = sorted(REQUIRED_TRANSPORT_CAPABILITIES - capabilities)
        self._require(not missing, "capability_gap", {"missing": missing})
        existing = self.snapshot()
        if existing:
            if existing.get("activation_digest") != admission.get("activation_digest") or existing.get("plan_digest") != normalized["plan_digest"]:
                raise LiveDcaCanaryError("canary_immutable", "a different canary is already recorded")
            return existing
        state = {
            "schema_version": CANARY_SCHEMA,
            "status": "prepared",
            "activation_digest": admission["activation_digest"],
            "preflight_digest": admission["preflight_digest"],
            "plan_digest": normalized["plan_digest"],
            "broker_id": "hyperliquid",
            "environment": "mainnet",
            "account_id": admission["account_id"],
            "environment_fingerprint": admission["environment_fingerprint"],
            "release_sha": admission["release_sha"],
            "strategy_scope": "dca",
            "direction": normalized["direction"],
            "risk_limits": normalized["risk_limits"],
            "derived_leverage": normalized["derived_leverage"],
            "entries": normalized["entries"],
            "target_price": normalized["target_price"],
            "stop_price": normalized["stop_price"],
            "orders": [],
            "fills": [],
            "protection": None,
            "events": [],
            "idempotency": {},
            "network_io": bool(getattr(self.transport, "network_io", False)),
            "real_money_eligible": bool(self.allow_network),
            "live_writes_enabled": False,
            "created_at": str(timestamp),
            "updated_at": str(timestamp),
            "next_action": "attended_submit_entry",
        }
        self._event(state, "canary_prepared", timestamp=timestamp, network_io=state["network_io"])
        self._save(state)
        return dict(state)

    def submit_entry(self, index: int, *, timestamp: str) -> dict[str, Any]:
        state = self._state()
        self._require(state.get("status") in {"prepared", "running"}, "canary_not_accepting_entries", state)
        if self._open_quantity(state) > 0 and state.get("protection") is None:
            self._block(state, "protection_required_before_next_entry", timestamp=timestamp)
            raise LiveDcaCanaryError("protection_required_before_next_entry", "aggregate protection must cover the current position before another entry")
        try:
            entry = dict(state["entries"][int(index)])
        except (IndexError, TypeError, ValueError) as exc:
            raise LiveDcaCanaryError("entry_index_invalid", "entry index is outside the approved DCA ladder") from exc
        key = f"entry:{int(index)}"
        prior = state["idempotency"].get(key)
        if prior:
            return dict(state)
        request = {
            "activation_digest": state["activation_digest"],
            "plan_digest": state["plan_digest"],
            "broker_id": "hyperliquid",
            "environment": "mainnet",
            "account_id": state["account_id"],
            "release_sha": state["release_sha"],
            "event": "dca_entry",
            "index": int(index),
            "side": state["direction"],
            "price": entry["price"],
            "quantity": entry["quantity"],
            "notional": entry["notional"],
            "reduce_only": False,
            "idempotency_key": f"{state['activation_digest']}:entry:{int(index)}",
        }
        try:
            response = self._call("submit_entry", request, timestamp=timestamp)
            self._require(response.get("status") not in {"unknown", "rejected", "error"}, "entry_submission_unknown", response)
        except LiveDcaCanaryError as exc:
            self._block(state, exc.code, timestamp=timestamp)
            self._save(state)
            raise
        order_id = str(response.get("order_id") or response.get("client_order_id") or "")
        self._require(bool(order_id), "entry_order_identity_missing", response)
        state["orders"].append({"order_id": order_id, "event": "entry", "index": int(index), "price": entry["price"], "quantity": entry["quantity"], "notional": entry["notional"], "state": str(response.get("status") or "submitted"), "reduce_only": False})
        state["idempotency"][key] = order_id
        fill = response.get("fill") if isinstance(response.get("fill"), Mapping) else None
        if fill:
            state["fills"].append({"order_id": order_id, **dict(fill), "index": int(index), "environment": "mainnet", "account_id": state["account_id"], "release_sha": state["release_sha"]})
            actual_price = _number(fill.get("price"), "fill price")
            if abs(actual_price - entry["price"]) > state["risk_limits"]["max_slippage"]:
                self._block(state, "entry_slippage_exceeded", timestamp=timestamp)
                self._save(state)
                raise LiveDcaCanaryError("entry_slippage_exceeded", "entry fill exceeded the approved slippage ceiling")
        state["status"] = "running"
        self._event(state, "entry_submitted", timestamp=timestamp, order_id=order_id, index=int(index))
        self._save(state)
        return dict(state)

    def replace_protection(self, *, quantity: float, timestamp: str) -> dict[str, Any]:
        state = self._state()
        quantity = _number(quantity, "protection quantity")
        open_quantity = self._open_quantity(state)
        self._require(open_quantity > 0, "protection_without_position", {"open_quantity": open_quantity})
        self._require(quantity >= open_quantity, "protection_quantity_undercoverage", {"quantity": quantity, "open_quantity": open_quantity})
        request = {
            "activation_digest": state["activation_digest"],
            "plan_digest": state["plan_digest"],
            "broker_id": "hyperliquid",
            "environment": "mainnet",
            "account_id": state["account_id"],
            "release_sha": state["release_sha"],
            "quantity": quantity,
            "take_profit": state["target_price"],
            "stop_loss": state["stop_price"],
            "reduce_only": True,
            "idempotency_key": f"{state['activation_digest']}:protection:{quantity}",
        }
        try:
            response = self._call("replace_protection", request, timestamp=timestamp)
            self._require(response.get("status") not in {"unknown", "rejected", "error"}, "protection_update_unknown", response)
            self._require(response.get("reduce_only") is True, "protection_not_reduce_only", response)
        except LiveDcaCanaryError as exc:
            self._block(state, exc.code, timestamp=timestamp)
            self._save(state)
            raise
        state["protection"] = {"quantity": quantity, "take_profit": state["target_price"], "stop_loss": state["stop_price"], "reduce_only": True, "group_id": response.get("group_id")}
        state["status"] = "running"
        self._event(state, "protection_replaced", timestamp=timestamp, quantity=quantity)
        self._save(state)
        return dict(state)

    def cancel(self, order_id: str, *, timestamp: str) -> dict[str, Any]:
        state = self._state()
        order_id = str(order_id or "").strip()
        self._require(bool(order_id), "cancel_order_identity_missing", {})
        key = f"cancel:{order_id}"
        if state["idempotency"].get(key):
            return dict(state)
        try:
            response = self._call("cancel_order", {"order_id": order_id, "activation_digest": state["activation_digest"], "plan_digest": state["plan_digest"], "environment": "mainnet", "account_id": state["account_id"], "release_sha": state["release_sha"], "idempotency_key": f"{state['activation_digest']}:cancel:{order_id}"}, timestamp=timestamp)
            self._require(response.get("status") not in {"unknown", "error"}, "cancel_unknown", response)
        except LiveDcaCanaryError as exc:
            self._block(state, exc.code, timestamp=timestamp)
            self._save(state)
            raise
        state["idempotency"][key] = True
        for order in state["orders"]:
            if order.get("order_id") == order_id:
                order["state"] = "canceled" if response.get("status") in {"canceled", "cancelled", "accepted"} else str(response.get("status"))
        self._event(state, "entry_canceled", timestamp=timestamp, order_id=order_id)
        self._reconcile(state, timestamp=timestamp)
        self._save(state)
        return dict(state)

    def flatten(self, *, timestamp: str, reason: str = "attended_flatten") -> dict[str, Any]:
        state = self._state()
        key = "flatten"
        if state["idempotency"].get(key):
            return dict(state)
        try:
            response = self._call("flatten_reduce_only", {"activation_digest": state["activation_digest"], "plan_digest": state["plan_digest"], "environment": "mainnet", "account_id": state["account_id"], "release_sha": state["release_sha"], "reduce_only": True, "reason": str(reason), "idempotency_key": f"{state['activation_digest']}:flatten"}, timestamp=timestamp)
            self._require(response.get("status") not in {"unknown", "error", "rejected"}, "flatten_unknown", response)
            self._require(response.get("reduce_only") is True, "flatten_not_reduce_only", response)
        except LiveDcaCanaryError as exc:
            self._block(state, exc.code, timestamp=timestamp)
            self._save(state)
            raise
        state["idempotency"][key] = True
        self._event(state, "flatten_requested", timestamp=timestamp, reason=reason)
        self._reconcile(state, timestamp=timestamp, require_flat=True)
        if reason == "completed":
            self._record_canary_passed(state, timestamp=timestamp)
        state["status"] = "rolled_back" if reason != "completed" else "completed"
        state["live_writes_enabled"] = False
        state["next_action"] = "record_and_stop"
        self._save(state)
        return dict(state)

    def stop(self, *, timestamp: str, reason: str = "attended_stop") -> dict[str, Any]:
        state = self._state()
        if state.get("status") in {"stopped", "rolled_back", "completed"}:
            return dict(state)
        state["status"] = "stopping"
        self._save(state)
        for order in list(state.get("orders") or []):
            if order.get("state") not in {"canceled", "cancelled", "filled", "closed"}:
                self.cancel(str(order.get("order_id") or ""), timestamp=timestamp)
        result = self.flatten(timestamp=timestamp, reason=reason)
        if result.get("status") == "rolled_back":
            result["status"] = "stopped"
            self._save(result)
        return result

    def kill(self, *, timestamp: str, reason: str = "kill_switch") -> dict[str, Any]:
        return self.stop(timestamp=timestamp, reason=reason)

    def _validate_plan(self, plan: Mapping[str, Any], admission: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(plan, Mapping):
            raise LiveDcaCanaryError("plan_shape_invalid", "DCA plan must be an object")
        if str(plan.get("strategy_type") or plan.get("strategy_scope") or "").lower() != "dca" or str(plan.get("direction") or "").lower() not in {"long", "short"}:
            raise LiveDcaCanaryError("live_dca_scope_invalid", "Live canary accepts DCA long/short only")
        if str(plan.get("plan_digest") or "") != str(admission.get("plan_digest") or ""):
            raise LiveDcaCanaryError("plan_digest_mismatch", "canary plan digest does not match activation")
        entries_raw = plan.get("entries") or plan.get("ladder")
        if not isinstance(entries_raw, list) or not entries_raw:
            raise LiveDcaCanaryError("dca_entries_missing", "approved DCA ladder is required")
        limits = {key: _number((plan.get("risk_limits") or {}).get(key), key) for key in REQUIRED_RISK_LIMITS}
        entries: list[dict[str, float]] = []
        total_notional = 0.0
        theoretical_loss = 0.0
        direction = str(plan.get("direction")).lower()
        stop = _number(plan.get("stop_price"), "stop_price")
        target = _number(plan.get("target_price"), "target_price")
        for raw in entries_raw:
            price = _number(raw.get("price"), "entry price")
            quantity = _number(raw.get("quantity"), "entry quantity")
            notional = _number(raw.get("notional", price * quantity), "entry notional")
            if abs(notional - price * quantity) > max(1e-8, notional * 1e-6):
                raise LiveDcaCanaryError("entry_notional_mismatch", "entry notional must equal price times quantity")
            total_notional += notional
            theoretical_loss += ((price - stop) if direction == "long" else (stop - price)) * quantity
            entries.append({"price": price, "quantity": quantity, "notional": notional})
        if theoretical_loss < 0 or theoretical_loss > limits["max_acceptable_loss"]:
            raise LiveDcaCanaryError("risk_budget_exceeded", "approved DCA ladder exceeds maximum acceptable loss", {"theoretical_loss": theoretical_loss, "max_acceptable_loss": limits["max_acceptable_loss"]})
        account_equity = _number(plan.get("account_equity"), "account_equity")
        derived_leverage = total_notional / account_equity
        if derived_leverage > limits["max_leverage"]:
            raise LiveDcaCanaryError("leverage_ceiling_exceeded", "approved DCA ladder exceeds maximum leverage")
        if total_notional > limits["max_notional"] or len(entries) > limits["max_open_orders"] or len(entries) > limits["max_positions"]:
            raise LiveDcaCanaryError("global_risk_ceiling_exceeded", "approved DCA ladder exceeds a global ceiling")
        return {"plan_digest": str(plan["plan_digest"]), "direction": direction, "entries": entries, "target_price": target, "stop_price": stop, "risk_limits": limits, "derived_leverage": derived_leverage}

    def _capabilities(self) -> set[str]:
        value = getattr(self.transport, "capabilities", set())
        if isinstance(value, Mapping):
            result = {str(key) for key, supported in value.items() if supported is True}
        elif hasattr(value, "names"):
            result = {str(item) for item in value.names}
        else:
            result = {str(item) for item in value}
        return result

    def _open_quantity(self, state: Mapping[str, Any]) -> float:
        return sum(float(fill.get("quantity") or 0) for fill in state.get("fills") or [])

    def _reconcile(self, state: dict[str, Any], *, timestamp: str, require_flat: bool = False) -> None:
        expected = {"activation_digest": state["activation_digest"], "plan_digest": state["plan_digest"], "environment": "mainnet", "account_id": state["account_id"], "release_sha": state["release_sha"], "open_quantity": self._open_quantity(state), "require_flat": bool(require_flat)}
        report = self._call("reconcile", expected, timestamp=timestamp)
        if report.get("status") not in {"ok", "pass", "reconciled"} or (require_flat and float(report.get("open_quantity") or 0) != 0):
            state["status"] = "blocked_reconciliation"
            state["next_action"] = "notify_park_and_wait"
            self._event(state, "reconciliation_blocked", timestamp=timestamp, report=dict(report))
            raise LiveDcaCanaryError("reconciliation_mismatch", "canary reconciliation did not prove the expected state", report)
        state["reconciliation"] = dict(report)

    def _call(self, operation: str, request: Mapping[str, Any], *, timestamp: str) -> dict[str, Any]:
        method = getattr(self.transport, operation, None)
        if not callable(method):
            raise LiveDcaCanaryError("capability_gap", f"transport does not implement {operation}")
        try:
            response = method(request) if operation not in {"cancel_order", "query_order", "account_snapshot"} else method(str(request.get("order_id") or ""))
        except Exception as exc:  # unknown outcome freezes the canary.
            raise LiveDcaCanaryError(f"{operation}_unknown", f"{operation} returned an unknown error") from exc
        if not isinstance(response, Mapping):
            raise LiveDcaCanaryError(f"{operation}_shape_invalid", f"{operation} response is not an object")
        return dict(response)

    def _state(self) -> dict[str, Any]:
        state = self.snapshot()
        if not state:
            raise LiveDcaCanaryError("canary_not_started", "start the attended canary first")
        return state

    @staticmethod
    def _require(condition: bool, code: str, evidence: Mapping[str, Any]) -> None:
        if not condition:
            raise LiveDcaCanaryError(code, code.replace("_", " "), evidence)

    @staticmethod
    def _block(state: dict[str, Any], code: str, *, timestamp: str) -> None:
        state["status"] = "blocked"
        state["blocker"] = str(code)
        state["next_action"] = "notify_park_and_wait"
        LiveDcaCanary._event(state, "canary_blocked", timestamp=timestamp, code=str(code))

    @staticmethod
    def _event(state: dict[str, Any], event: str, *, timestamp: str, **payload: Any) -> None:
        row = {"event": event, "timestamp": str(timestamp), "activation_digest": state.get("activation_digest"), "plan_digest": state.get("plan_digest"), "environment": "mainnet", "account_id": state.get("account_id"), "release_sha": state.get("release_sha"), "network_io": bool(state.get("network_io")), **payload}
        row["event_digest"] = _digest({key: value for key, value in row.items() if key != "event_digest"})
        state.setdefault("events", []).append(row)
        state["updated_at"] = str(timestamp)

    def _save(self, state: Mapping[str, Any]) -> None:
        if isinstance(state, dict):
            state["state_digest"] = _digest({key: value for key, value in state.items() if key != "state_digest"})
        write_json(self.path, [dict(state)])

    def _record_canary_passed(self, state: Mapping[str, Any], *, timestamp: str) -> None:
        """Publish a canary receipt to the activation journal after flat proof."""

        try:
            source = self.gate._source_attestation_resolver()
        except Exception:
            source = {}
        row = {
            "schema_version": "live-activation-gate-v1",
            "event": "canary_passed",
            "activation_digest": state["activation_digest"],
            "preflight_digest": state["preflight_digest"],
            "plan_digest": state["plan_digest"],
            "release_sha": state["release_sha"],
            "account_id": state["account_id"],
            "environment_fingerprint": state["environment_fingerprint"],
            "environment": "mainnet",
            "broker_id": "hyperliquid",
            "strategy_scope": "dca",
            "execution_authorized": bool(self.allow_network),
            "live_writes_enabled": bool(self.allow_network),
            "risk_limits_digest": _digest(state["risk_limits"]),
            "source_attestation": dict(source) if isinstance(source, Mapping) else {},
            "canary_status": "pass",
            "finished_at": str(timestamp),
        }
        row["canary_receipt_digest"] = _digest(row)
        self.gate._append(row)
