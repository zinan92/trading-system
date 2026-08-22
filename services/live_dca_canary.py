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
    identity: Mapping[str, Any]

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


def _nonnegative(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise LiveDcaCanaryError("account_snapshot_invalid", f"{name} must be numeric") from exc
    if not math.isfinite(result) or result < 0:
        raise LiveDcaCanaryError("account_snapshot_invalid", f"{name} must be nonnegative and finite")
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
        network_io = getattr(transport, "network_io", None)
        if not isinstance(network_io, bool):
            raise LiveDcaCanaryError("network_io_flag_missing", "transport must explicitly declare network_io as a boolean")
        self._transport_network_io = network_io
        self._transport_identity: dict[str, Any] | None = None
        if network_io and not allow_network:
            raise LiveDcaCanaryError("network_transport_not_allowed", "the default attended harness accepts only a no-network transport")
        if allow_network and not network_io:
            raise LiveDcaCanaryError("network_transport_mismatch", "network_transport_mismatch: allow_network requires a transport that declares network_io=true")
        self.allow_network = bool(allow_network)

    def snapshot(self) -> dict[str, Any]:
        rows = load_json(self.path)
        return dict(rows[-1]) if rows and isinstance(rows[-1], Mapping) else {}

    def resume(self, *, require_activation: bool = True) -> dict[str, Any]:
        """Rebind a fresh process to the persisted canary identity."""

        state = self._state()
        persisted_identity = state.get("transport_identity")
        current_identity = getattr(self.transport, "identity", None)
        current_network_io = getattr(self.transport, "network_io", None)
        if not isinstance(persisted_identity, Mapping) or not isinstance(current_identity, Mapping) or dict(current_identity) != dict(persisted_identity) or current_network_io is not self._transport_network_io:
            raise LiveDcaCanaryError("transport_identity_changed", "fresh operator process does not match persisted canary transport identity")
        self._transport_identity = dict(persisted_identity)
        if require_activation:
            self._require_activation_current(state)
        return state

    def start(self, plan: Mapping[str, Any], *, timestamp: str) -> dict[str, Any]:
        admission = self.gate.activation_prerequisite_status()
        self._require(admission.get("ready") is True, "activation_prerequisite_blocked", admission)
        self._validate_transport_identity(admission)
        normalized = self._validate_plan(plan, admission)
        if dict(normalized["risk_limits"]) != dict((admission.get("preflight") or {}).get("risk_limits") or {}):
            raise LiveDcaCanaryError("current_risk_limits_mismatch", "canary risk limits do not match the activation preflight")
        capabilities = self._capabilities()
        missing = sorted(REQUIRED_TRANSPORT_CAPABILITIES - capabilities)
        self._require(not missing, "capability_gap", {"missing": missing})
        existing = self.snapshot()
        if existing:
            self._validate_state_integrity(existing)
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
            "transport_identity": dict(getattr(self.transport, "identity", {})),
            "account_snapshot": None,
            "live_writes_enabled": False,
            "created_at": str(timestamp),
            "updated_at": str(timestamp),
            "next_action": "attended_submit_entry",
            "receipts": [],
        }
        state["account_snapshot"] = self._account_snapshot(normalized["risk_limits"], state=state, timestamp=timestamp)
        state["account_snapshot"] = self._account_snapshot(normalized["risk_limits"], state=state, timestamp=timestamp)
        self._event(state, "canary_prepared", timestamp=timestamp, network_io=state["network_io"])
        self._save(state)
        return dict(state)

    def submit_entry(self, index: int, *, timestamp: str) -> dict[str, Any]:
        state = self._state()
        self._require(state.get("status") in {"prepared", "running"}, "canary_not_accepting_entries", state)
        self._require_activation_current(state)
        self._account_snapshot(state["risk_limits"], state=state, timestamp=timestamp)
        expected_index = sum(1 for order in state.get("orders") or [] if order.get("event") == "entry")
        if int(index) != expected_index:
            self._block(state, "entry_sequence_invalid", timestamp=timestamp)
            self._save(state)
            raise LiveDcaCanaryError("entry_sequence_invalid", "DCA entries must advance in approved ladder order")
        if state.get("pending_entry_order_id"):
            self._block(state, "prior_entry_not_terminal", timestamp=timestamp)
            self._save(state)
            raise LiveDcaCanaryError("prior_entry_not_terminal", "the prior DCA entry must be filled or canceled before the next entry")
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
            response = self._call("submit_entry", request, state=state, timestamp=timestamp)
            self._require(response.get("status") not in {"unknown", "rejected", "error"}, "entry_submission_unknown", response)
        except LiveDcaCanaryError as exc:
            self._block(state, exc.code, timestamp=timestamp)
            self._save(state)
            raise
        order_id = str(response.get("order_id") or response.get("client_order_id") or "")
        self._require(bool(order_id), "entry_order_identity_missing", response)
        state["orders"].append({"order_id": order_id, "event": "entry", "index": int(index), "price": entry["price"], "quantity": entry["quantity"], "notional": entry["notional"], "state": str(response.get("status") or "submitted"), "reduce_only": False})
        state["idempotency"][key] = order_id
        if str(response.get("status") or "").lower() not in {"filled", "partially_filled", "partial"}:
            state["pending_entry_order_id"] = order_id
        fill = response.get("fill") if isinstance(response.get("fill"), Mapping) else None
        response_status = str(response.get("status") or "").lower()
        if response_status in {"filled", "partially_filled", "partial"} and not isinstance(fill, Mapping):
            self._block(state, "fill_receipt_missing", timestamp=timestamp)
            self._save(state)
            raise LiveDcaCanaryError("fill_receipt_missing", "a terminal order response without fill quantity and price is unknown")
        if response_status in {"partially_filled", "partial"}:
            state["pending_entry_order_id"] = order_id
        if fill:
            quantity = _number(fill.get("quantity", fill.get("sz")), "fill quantity")
            actual_price = _number(fill.get("price", fill.get("px")), "fill price")
            if quantity > entry["quantity"]:
                self._recover_after_protection_gap(state, "fill_quantity_exceeded", timestamp=timestamp)
                raise LiveDcaCanaryError("fill_quantity_exceeded", "fill quantity exceeds the approved DCA entry")
            safe_fill = {key: fill.get(key) for key in ("fill_id", "tid", "hash", "fee", "funding") if key in fill}
            state["fills"].append({"order_id": order_id, **safe_fill, "quantity": quantity, "price": actual_price, "index": int(index), "environment": "mainnet", "account_id": state["account_id"], "release_sha": state["release_sha"]})
            if abs(actual_price - entry["price"]) > state["risk_limits"]["max_slippage"]:
                self._recover_after_protection_gap(state, "entry_slippage_exceeded", timestamp=timestamp)
                raise LiveDcaCanaryError("entry_slippage_exceeded", "entry fill exceeded the approved slippage ceiling")
            if self._projected_loss(state) > state["risk_limits"]["max_acceptable_loss"]:
                self._recover_after_protection_gap(state, "filled_risk_budget_exceeded", timestamp=timestamp)
                raise LiveDcaCanaryError("filled_risk_budget_exceeded", "actual fill prices consume more than the approved loss budget")
            try:
                self._account_snapshot(state["risk_limits"], state=state, timestamp=timestamp)
            except LiveDcaCanaryError as exc:
                self._recover_after_protection_gap(state, exc.code, timestamp=timestamp)
                raise
        state["status"] = "running"
        self._event(state, "entry_submitted", timestamp=timestamp, order_id=order_id, index=int(index))
        self._save(state)
        return dict(state)

    def replace_protection(self, *, quantity: float, timestamp: str) -> dict[str, Any]:
        state = self._state()
        self._require_activation_current(state)
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
            response = self._call("replace_protection", request, state=state, timestamp=timestamp)
            self._require(response.get("status") not in {"unknown", "rejected", "error"}, "protection_update_unknown", response)
            self._require(response.get("reduce_only") is True, "protection_not_reduce_only", response)
            covered_quantity = _number(response.get("covered_quantity"), "covered protection quantity")
            self._require(covered_quantity >= quantity, "protection_coverage_underreported", response)
            take_profit = _number(response.get("take_profit"), "protection take profit")
            stop_loss = _number(response.get("stop_loss"), "protection stop loss")
            self._require(take_profit == float(state["target_price"]), "protection_take_profit_mismatch", response)
            self._require(stop_loss == float(state["stop_price"]), "protection_stop_loss_mismatch", response)
            protection_id = str(response.get("group_id") or response.get("protection_order_id") or "").strip()
            self._require(bool(protection_id), "protection_identity_missing", response)
        except LiveDcaCanaryError as exc:
            self._recover_after_protection_gap(state, exc.code, timestamp=timestamp)
            raise
        state["protection"] = {"quantity": covered_quantity, "take_profit": take_profit, "stop_loss": stop_loss, "reduce_only": True, "group_id": protection_id}
        state["status"] = "running"
        self._event(state, "protection_replaced", timestamp=timestamp, quantity=quantity)
        self._save(state)
        return dict(state)

    def cancel(self, order_id: str, *, timestamp: str, allow_protection_open: bool = False) -> dict[str, Any]:
        state = self._state()
        order_id = str(order_id or "").strip()
        self._require(bool(order_id), "cancel_order_identity_missing", {})
        key = f"cancel:{order_id}"
        if state["idempotency"].get(key):
            return dict(state)
        order = next((item for item in state.get("orders") or [] if str(item.get("order_id") or "") == order_id), None)
        self._require(isinstance(order, Mapping), "cancel_order_outside_canary", {"order_id": order_id})
        self._require(str(order.get("state") or "") not in {"filled", "closed", "canceled", "cancelled"}, "cancel_order_not_working", {"order_id": order_id, "state": order.get("state")})
        try:
            response = self._call("cancel_order", {"order_id": order_id, "activation_digest": state["activation_digest"], "plan_digest": state["plan_digest"], "environment": "mainnet", "account_id": state["account_id"], "release_sha": state["release_sha"], "idempotency_key": f"{state['activation_digest']}:cancel:{order_id}"}, state=state, timestamp=timestamp)
            self._require(response.get("status") not in {"unknown", "error"}, "cancel_unknown", response)
            if response.get("status") == "accepted":
                terminal = self._call("query_order", {"order_id": order_id, "activation_digest": state["activation_digest"], "plan_digest": state["plan_digest"], "environment": "mainnet", "account_id": state["account_id"], "release_sha": state["release_sha"]}, state=state, timestamp=timestamp)
                self._require(terminal.get("status") in {"canceled", "cancelled"}, "cancel_not_terminal", terminal)
        except LiveDcaCanaryError as exc:
            self._block(state, exc.code, timestamp=timestamp)
            self._save(state)
            raise
        state["idempotency"][key] = True
        for order in state["orders"]:
            if order.get("order_id") == order_id:
                order["state"] = "canceled" if response.get("status") in {"canceled", "cancelled", "accepted"} else str(response.get("status"))
        if state.get("pending_entry_order_id") == order_id:
            state.pop("pending_entry_order_id", None)
        self._event(state, "entry_canceled", timestamp=timestamp, order_id=order_id)
        self._reconcile(state, timestamp=timestamp, require_no_open_orders=not allow_protection_open)
        self._save(state)
        return dict(state)

    def flatten(self, *, timestamp: str, reason: str = "attended_flatten") -> dict[str, Any]:
        state = self._state()
        if state["idempotency"].get("flatten_confirmed"):
            return dict(state)
        state["idempotency"]["flatten_requested"] = True
        self._save(state)
        try:
            response = self._call("flatten_reduce_only", {"activation_digest": state["activation_digest"], "plan_digest": state["plan_digest"], "environment": "mainnet", "account_id": state["account_id"], "release_sha": state["release_sha"], "reduce_only": True, "cancel_protection": True, "reason": str(reason), "idempotency_key": f"{state['activation_digest']}:flatten"}, state=state, timestamp=timestamp)
            self._require(response.get("status") not in {"unknown", "error", "rejected"}, "flatten_unknown", response)
            self._require(response.get("reduce_only") is True, "flatten_not_reduce_only", response)
            self._require(response.get("protection_canceled") is True, "flatten_protection_cancel_unknown", response)
        except LiveDcaCanaryError as exc:
            self._block(state, exc.code, timestamp=timestamp)
            self._save(state)
            raise
        self._event(state, "flatten_requested", timestamp=timestamp, reason=reason)
        self._reconcile(state, timestamp=timestamp, require_flat=True, require_no_open_orders=True)
        state["idempotency"]["flatten_confirmed"] = True
        if reason == "completed":
            self._require(self._open_quantity(state) > 0 and isinstance(state.get("protection"), Mapping), "canary_completion_proof_missing", {"open_quantity": self._open_quantity(state), "protection": bool(state.get("protection"))})
        state["status"] = "rolled_back" if reason != "completed" else "completed"
        state["live_writes_enabled"] = False
        state["next_action"] = "record_and_stop"
        self._save(state)
        if reason == "completed":
            self._record_canary_passed(state, timestamp=timestamp)
        return dict(state)

    def stop(self, *, timestamp: str, reason: str = "attended_stop") -> dict[str, Any]:
        state = self._state()
        if state.get("status") in {"stopped", "rolled_back", "completed"}:
            return dict(state)
        state["status"] = "stopping"
        self._save(state)
        for order in list(state.get("orders") or []):
            if order.get("state") not in {"canceled", "cancelled", "filled", "closed"}:
                self.cancel(str(order.get("order_id") or ""), timestamp=timestamp, allow_protection_open=True)
        result = self.flatten(timestamp=timestamp, reason=reason)
        if result.get("status") == "rolled_back" and reason != "attended_rollback":
            result["status"] = "stopped"
            self._save(result)
        elif result.get("status") == "rolled_back" and reason == "attended_rollback":
            self._save(result)
        return result

    def kill(self, *, timestamp: str, reason: str = "kill_switch") -> dict[str, Any]:
        return self.stop(timestamp=timestamp, reason=reason)

    def rollback(self, *, timestamp: str, reason: str = "attended_rollback") -> dict[str, Any]:
        return self.stop(timestamp=timestamp, reason=reason)

    def _validate_plan(self, plan: Mapping[str, Any], admission: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(plan, Mapping):
            raise LiveDcaCanaryError("plan_shape_invalid", "DCA plan must be an object")
        if str(plan.get("strategy_type") or plan.get("strategy_scope") or "").lower() != "dca" or str(plan.get("direction") or "").lower() not in {"long", "short"}:
            raise LiveDcaCanaryError("live_dca_scope_invalid", "Live canary accepts DCA long/short only")
        if str(plan.get("plan_digest") or "") != str(admission.get("plan_digest") or ""):
            raise LiveDcaCanaryError("plan_digest_mismatch", "canary plan digest does not match activation")
        approved = admission.get("approved_plan") if isinstance(admission.get("approved_plan"), Mapping) else {}
        canonical = approved.get("canonical_plan") if isinstance(approved.get("canonical_plan"), Mapping) else {}
        if not canonical or not isinstance(canonical.get("entries"), list) or not isinstance(canonical.get("risk_limits"), Mapping):
            raise LiveDcaCanaryError("approved_dca_plan_incomplete", "the approved DCA receipt does not contain the canonical ladder and risk limits")
        for key in ("direction", "target_price", "stop_price", "account_equity"):
            if str(plan.get(key)) != str(canonical.get(key)):
                raise LiveDcaCanaryError("approved_dca_plan_mismatch", f"DCA {key} differs from the approved canonical plan")
        if list(plan.get("entries") or plan.get("ladder") or []) != list(canonical.get("entries") or []):
            raise LiveDcaCanaryError("approved_dca_entries_mismatch", "DCA entries differ from the approved canonical ladder")
        if dict(plan.get("risk_limits") or {}) != dict(canonical.get("risk_limits") or {}):
            raise LiveDcaCanaryError("approved_dca_risk_mismatch", "DCA risk limits differ from the approved canonical limits")
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

    def _validate_transport_identity(self, admission: Mapping[str, Any]) -> None:
        identity = getattr(self.transport, "identity", None)
        if not isinstance(identity, Mapping):
            raise LiveDcaCanaryError("transport_identity_missing", "transport must declare broker/account/environment/release identity")
        expected = {
            "broker_id": "hyperliquid",
            "environment": "mainnet",
            "account_id": admission.get("account_id"),
            "environment_fingerprint": admission.get("environment_fingerprint"),
            "release_sha": admission.get("release_sha"),
        }
        if any(identity.get(key) != value for key, value in expected.items()):
            raise LiveDcaCanaryError("transport_identity_mismatch", "transport identity does not match the approved activation", {"expected": expected, "actual": {key: identity.get(key) for key in expected}})
        endpoint = str(identity.get("endpoint") or "")
        if not endpoint or (self.allow_network and not endpoint.startswith("https://")) or (not self.allow_network and not endpoint.startswith("fixture://")):
            raise LiveDcaCanaryError("transport_endpoint_invalid", "transport endpoint does not match the attended mode")
        self._transport_identity = dict(identity)

    def _open_quantity(self, state: Mapping[str, Any]) -> float:
        return sum(float(fill.get("quantity") or 0) for fill in state.get("fills") or [])

    def _projected_loss(self, state: Mapping[str, Any]) -> float:
        direction = str(state.get("direction") or "long")
        stop = float(state["stop_price"])
        fills_by_index: dict[int, float] = {}
        realized_projection = 0.0
        for fill in state.get("fills") or []:
            index = int(fill.get("index") or 0)
            quantity = float(fill.get("quantity") or 0)
            price = float(fill.get("price") or 0)
            fills_by_index[index] = fills_by_index.get(index, 0.0) + quantity
            realized_projection += ((price - stop) if direction == "long" else (stop - price)) * quantity
        residual_projection = 0.0
        for index, entry in enumerate(state.get("entries") or []):
            residual = max(float(entry["quantity"]) - fills_by_index.get(index, 0.0), 0.0)
            residual_projection += ((float(entry["price"]) - stop) if direction == "long" else (stop - float(entry["price"]))) * residual
        return max(0.0, realized_projection + residual_projection)

    def _account_snapshot(self, limits: Mapping[str, float], *, admission: Mapping[str, Any] | None = None, state: Mapping[str, Any] | None = None, timestamp: str) -> dict[str, Any]:
        identity = admission or state or {}
        request = {
            "activation_digest": identity.get("activation_digest"),
            "plan_digest": identity.get("plan_digest"),
            "environment": "mainnet",
            "account_id": identity.get("account_id"),
            "release_sha": identity.get("release_sha"),
        }
        response = self._call("account_snapshot", request, state=state, timestamp=timestamp)
        self._require(response.get("status") in {"ok", "pass", "ready"}, "account_snapshot_unknown", response)
        fields = {key: _nonnegative(response.get(key), key) for key in ("open_orders", "open_positions", "notional", "leverage", "loss")}
        if fields["open_orders"] > limits["max_open_orders"] or fields["open_positions"] > limits["max_positions"] or fields["notional"] > limits["max_notional"] or fields["leverage"] > limits["max_leverage"] or fields["loss"] > limits["max_acceptable_loss"]:
            self._require(False, "current_risk_ceiling_exceeded", {"account": fields, "limits": dict(limits)})
        return {"status": str(response.get("status")), **fields}

    def _require_activation_current(self, state: Mapping[str, Any]) -> None:
        admission = self.gate.activation_prerequisite_status()
        self._require(admission.get("ready") is True, "activation_prerequisite_blocked", admission)
        self._require(admission.get("activation_digest") == state.get("activation_digest") and admission.get("plan_digest") == state.get("plan_digest"), "activation_changed", admission)
        self._require(dict((admission.get("preflight") or {}).get("risk_limits") or {}) == dict(state.get("risk_limits") or {}), "current_risk_limits_mismatch", admission)

    def _reconcile(self, state: dict[str, Any], *, timestamp: str, require_flat: bool = False, require_no_open_orders: bool = False) -> None:
        expected = {"activation_digest": state["activation_digest"], "plan_digest": state["plan_digest"], "environment": "mainnet", "account_id": state["account_id"], "release_sha": state["release_sha"], "open_quantity": self._open_quantity(state), "require_flat": bool(require_flat), "require_no_open_orders": bool(require_no_open_orders)}
        try:
            report = self._call("reconcile", expected, state=state, timestamp=timestamp)
        except LiveDcaCanaryError as exc:
            state["status"] = "blocked_reconciliation"
            state["blocker"] = exc.code
            state["next_action"] = "notify_park_and_wait"
            self._event(state, "reconciliation_unknown", timestamp=timestamp, code=exc.code)
            self._save(state)
            raise
        if report.get("status") not in {"ok", "pass", "reconciled"} or (require_flat and float(report.get("open_quantity") or 0) != 0) or (require_no_open_orders and ("open_orders" not in report or float(report.get("open_orders") or 0) != 0)):
            state["status"] = "blocked_reconciliation"
            state["next_action"] = "notify_park_and_wait"
            self._event(state, "reconciliation_blocked", timestamp=timestamp, report=self._safe_reconciliation(report))
            self._save(state)
            raise LiveDcaCanaryError("reconciliation_mismatch", "canary reconciliation did not prove the expected state", report)
        state["reconciliation"] = self._safe_reconciliation(report)

    @staticmethod
    def _safe_reconciliation(report: Mapping[str, Any]) -> dict[str, Any]:
        allowed = ("status", "open_quantity", "open_orders", "position_quantity", "notional", "leverage", "loss", "fees", "funding", "receipt_digest")
        safe = {key: report.get(key) for key in allowed if key in report}
        safe["response_digest"] = _digest({key: value for key, value in report.items() if key in allowed})
        return safe

    def _recover_after_protection_gap(self, state: dict[str, Any], code: str, *, timestamp: str) -> None:
        self._block(state, code, timestamp=timestamp)
        self._save(state)
        try:
            self.flatten(timestamp=timestamp, reason=f"recovery:{code}")
        except LiveDcaCanaryError:
            state["status"] = "blocked"
            state["next_action"] = "notify_park_and_wait"
            self._save(state)

    def _call(self, operation: str, request: Mapping[str, Any], *, state: dict[str, Any] | None = None, timestamp: str) -> dict[str, Any]:
        method = getattr(self.transport, operation, None)
        if not callable(method):
            raise LiveDcaCanaryError("capability_gap", f"transport does not implement {operation}")
        current_network_io = getattr(self.transport, "network_io", None)
        current_identity = getattr(self.transport, "identity", None)
        if current_network_io is not self._transport_network_io or not isinstance(current_identity, Mapping) or self._transport_identity is None or dict(current_identity) != self._transport_identity:
            raise LiveDcaCanaryError("transport_identity_changed", "transport identity or network mode changed after activation")
        identity = getattr(self.transport, "identity", None)
        expected_identity = {
            "broker_id": request.get("broker_id", "hyperliquid"),
            "environment": request.get("environment", "mainnet"),
            "account_id": request.get("account_id"),
            "release_sha": request.get("release_sha"),
        }
        if not isinstance(identity, Mapping) or any(identity.get(key) != value for key, value in expected_identity.items() if value is not None):
            raise LiveDcaCanaryError("transport_identity_changed", "transport identity changed after activation")
        try:
            if operation == "account_snapshot":
                response = method()
            elif operation in {"cancel_order", "query_order"}:
                response = method(str(request.get("order_id") or ""))
            else:
                response = method(request)
        except Exception as exc:  # unknown outcome freezes the canary.
            if state is not None:
                self._receipt(state, operation, request, {"status": "unknown", "error_type": type(exc).__name__}, timestamp=timestamp)
                self._save(state)
            raise LiveDcaCanaryError(f"{operation}_unknown", f"{operation} returned an unknown error") from exc
        if not isinstance(response, Mapping):
            if state is not None:
                self._receipt(state, operation, request, {"status": "invalid_response"}, timestamp=timestamp)
                self._save(state)
            raise LiveDcaCanaryError(f"{operation}_shape_invalid", f"{operation} response is not an object")
        result = dict(response)
        if state is not None:
            self._receipt(state, operation, request, result, timestamp=timestamp)
            self._save(state)
        return result

    def _state(self) -> dict[str, Any]:
        state = self.snapshot()
        if not state:
            raise LiveDcaCanaryError("canary_not_started", "start the attended canary first")
        self._validate_state_integrity(state)
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
            state["receipt_chain_digest"] = _digest(state.get("receipts") or [])
            state["state_digest"] = _digest({key: value for key, value in state.items() if key != "state_digest"})
        write_json(self.path, [dict(state)])

    def _receipt(self, state: dict[str, Any], operation: str, request: Mapping[str, Any], response: Mapping[str, Any], *, timestamp: str) -> None:
        safe_request_keys = ("activation_digest", "plan_digest", "broker_id", "environment", "account_id", "release_sha", "event", "index", "side", "price", "quantity", "notional", "reduce_only", "cancel_protection", "reason", "idempotency_key", "require_flat", "require_no_open_orders")
        safe_response_keys = ("status", "order_id", "client_order_id", "group_id", "protection_order_id", "reduce_only", "covered_quantity", "take_profit", "stop_loss", "protection_canceled", "open_quantity", "open_orders", "error_type")
        safe_request = {key: request.get(key) for key in safe_request_keys if key in request}
        safe_response = {key: response.get(key) for key in safe_response_keys if key in response}
        receipt = {
            "operation": str(operation),
            "timestamp": str(timestamp),
            "idempotency_key": str(request.get("idempotency_key") or ""),
            "request_digest": _digest(safe_request),
            "response_digest": _digest(safe_response),
            "status": str(response.get("status") or "unknown"),
            "order_id": str(response.get("order_id") or request.get("order_id") or ""),
            "group_id": str(response.get("group_id") or response.get("protection_order_id") or ""),
            "reduce_only": response.get("reduce_only", request.get("reduce_only")),
            "environment": "mainnet",
            "broker_id": "hyperliquid",
            "account_id": state.get("account_id"),
            "release_sha": state.get("release_sha"),
            "network_io": bool(state.get("network_io")),
        }
        receipt["receipt_digest"] = _digest(receipt)
        state.setdefault("receipts", []).append(receipt)

    @staticmethod
    def _validate_state_integrity(state: Mapping[str, Any]) -> None:
        supplied = str(state.get("state_digest") or "")
        if not supplied or supplied != _digest({key: value for key, value in state.items() if key != "state_digest"}):
            raise LiveDcaCanaryError("canary_state_integrity_invalid", "canary state digest does not match")
        for event in state.get("events") or []:
            if not isinstance(event, Mapping):
                raise LiveDcaCanaryError("canary_event_shape_invalid", "canary event is not an object")
            digest = str(event.get("event_digest") or "")
            if not digest or digest != _digest({key: value for key, value in event.items() if key != "event_digest"}):
                raise LiveDcaCanaryError("canary_event_integrity_invalid", "canary event digest does not match")

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
            "state_digest": state.get("state_digest"),
            "receipt_chain_digest": state.get("receipt_chain_digest"),
            "reconciliation_digest": _digest(state.get("reconciliation") or {}),
            "finished_at": str(timestamp),
        }
        row["canary_receipt_digest"] = _digest(row)
        self.gate._append(row)
