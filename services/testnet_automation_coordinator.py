"""Testnet Automation Coordinator composition-root boundary.

The coordinator binds immutable activation identity, candidate selection,
subtractive Portfolio sizing, and the canonical DCA/Grid lifecycles.  It never
constructs venue-native requests; an explicit Broker adapter and attended
Testnet confirmation remain required before any exposure-changing call.
"""

from __future__ import annotations

import json
import hashlib
import re
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from schemas.portfolio import ExecutionSlice
from services.account_identity import account_fingerprint, LEGACY_ACCOUNT_FINGERPRINT_SCHEME
from services.journal_store import load_json, write_json


COORDINATOR_SCHEMA = "testnet-automation-coordinator-v1"
COORDINATOR_ROOT = "testnet_automation"
COORDINATOR_STATE_FILE = "current.json"
COORDINATOR_EVENTS_FILE = "events.json"
TESTNET_BROKER_ID = "hyperliquid"
TESTNET_ENVIRONMENT = "testnet"
TESTNET_TRANSPORT_PROFILE = "hyperliquid-testnet-default"
TESTNET_PROTECTED_TRANSPORT_PROFILE = "hyperliquid-testnet-position-protection"
MAX_TESTNET_CONFIRMATION_AGE_SECONDS = 900
_TESTNET_TRANSPORT_PROFILES = frozenset(
    {TESTNET_TRANSPORT_PROFILE, TESTNET_PROTECTED_TRANSPORT_PROFILE}
)
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}", re.IGNORECASE)
_RELEASE_RE = re.compile(r"[0-9a-f]{40}", re.IGNORECASE)
_ACTIONS = frozenset(
    {
        "activate",
        "status",
        "preflight",
        "pause",
        "stop",
        "flatten",
        "interrupt",
        "resume",
        "select_candidate",
        "reconcile_stop",
    }
)
_APPROVED_MARKET_SOURCES = frozenset(
    {"hyperliquid.external_testnet", "nautilus-hyperliquid.testnet"}
)
_ACTIVATION_FIELDS = (
    "strategy_family",
    "strategy_session_id",
    "strategy_revision_id",
    "plan_digest",
    "account_fingerprint",
    "broker_id",
    "environment",
    "transport_profile",
    "instrument_id",
    "runtime_id",
    "release_sha",
    "capability_revision",
    "requested_notional",
    "effective_notional",
    "requested_max_loss",
    "effective_max_loss",
    "risk_gate_digest",
    "execution_slice",
)
_FORBIDDEN_SECRET_FIELDS = frozenset(
    {
        "private_key",
        "secret",
        "secret_file",
        "signer",
        "signature",
        "signed_payload",
        "api_key",
        "api_secret",
    }
)


class TestnetCoordinatorError(ValueError):
    """Stable, fail-closed Coordinator contract error."""

    __test__ = False

    def __init__(self, code: str, evidence: Mapping[str, Any] | None = None) -> None:
        self.code = str(code)
        self.evidence = dict(evidence or {})
        super().__init__(self.code)


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise TestnetCoordinatorError(f"{field}_required")
    return text


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError(f"unsupported_activation_value:{type(value).__name__}")


def _digest(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        _canonical(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def activation_digest(value: Mapping[str, Any]) -> str:
    """Return the stable digest for the allow-listed activation identity."""

    return _digest({field: value.get(field) for field in _ACTIVATION_FIELDS})


def _now_iso(clock: Callable[[], str | datetime] | None) -> str:
    value = clock() if clock is not None else datetime.now(timezone.utc)
    if isinstance(value, datetime):
        timestamp = value
    else:
        text = str(value).strip()
        try:
            timestamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise TestnetCoordinatorError("clock_timestamp_invalid") from exc
    if timestamp.tzinfo is None:
        raise TestnetCoordinatorError("clock_timestamp_timezone_missing")
    return timestamp.astimezone(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class TestnetActivation:
    __test__ = False

    strategy_family: str
    strategy_session_id: str
    strategy_revision_id: str
    plan_digest: str
    account_fingerprint: str
    broker_id: str
    environment: str
    transport_profile: str
    instrument_id: str
    runtime_id: str
    release_sha: str
    capability_revision: str
    requested_notional: str | None = None
    effective_notional: str | None = None
    requested_max_loss: str | None = None
    effective_max_loss: str | None = None
    risk_gate_digest: str | None = None
    execution_slice: Mapping[str, Any] | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "TestnetActivation":
        if not isinstance(value, Mapping):
            raise TestnetCoordinatorError("activation_shape_invalid")
        forbidden = sorted(_FORBIDDEN_SECRET_FIELDS.intersection(value))
        if forbidden:
            raise TestnetCoordinatorError(
                "secret_field_forbidden", {"fields": forbidden}
            )
        unknown = sorted(str(key) for key in value if str(key) not in _ACTIVATION_FIELDS)
        if unknown:
            raise TestnetCoordinatorError("activation_field_unknown", {"fields": unknown})
        strategy_family = _required_text(value.get("strategy_family"), "strategy_family").lower()
        if strategy_family not in {"dca", "grid"}:
            raise TestnetCoordinatorError("strategy_family_invalid")
        plan_digest = _required_text(value.get("plan_digest"), "plan_digest").lower()
        if _DIGEST_RE.fullmatch(plan_digest) is None:
            raise TestnetCoordinatorError("plan_digest_invalid")
        account_fingerprint = _required_text(
            value.get("account_fingerprint"), "account_fingerprint"
        ).lower()
        if _DIGEST_RE.fullmatch(account_fingerprint) is None:
            raise TestnetCoordinatorError("account_fingerprint_invalid")
        broker_id = _required_text(value.get("broker_id"), "broker_id").lower()
        if broker_id != TESTNET_BROKER_ID:
            raise TestnetCoordinatorError("hyperliquid_broker_required")
        environment = _required_text(value.get("environment"), "environment").lower()
        if environment != TESTNET_ENVIRONMENT:
            raise TestnetCoordinatorError("testnet_only")
        transport_profile = _required_text(
            value.get("transport_profile"), "transport_profile"
        )
        if transport_profile not in _TESTNET_TRANSPORT_PROFILES:
            raise TestnetCoordinatorError("testnet_profile_required")
        release_sha = _required_text(value.get("release_sha"), "release_sha").lower()
        if _RELEASE_RE.fullmatch(release_sha) is None:
            raise TestnetCoordinatorError("release_sha_invalid")
        capability_revision = _required_text(
            value.get("capability_revision"), "capability_revision"
        )
        if (
            transport_profile == TESTNET_PROTECTED_TRANSPORT_PROFILE
            and capability_revision != "hyperliquid-testnet-position-protection-runtime-v1"
        ):
            raise TestnetCoordinatorError("protected_capability_revision_required")
        optional_numbers: dict[str, str | None] = {}
        for field in (
            "requested_notional",
            "effective_notional",
            "requested_max_loss",
            "effective_max_loss",
        ):
            raw = value.get(field)
            if raw in (None, ""):
                optional_numbers[field] = None
                continue
            try:
                number = float(raw)
            except (TypeError, ValueError) as exc:
                raise TestnetCoordinatorError(f"{field}_invalid") from exc
            if number < 0 or not number == number or number in {float("inf"), float("-inf")}:
                raise TestnetCoordinatorError(f"{field}_invalid")
            optional_numbers[field] = str(raw)
        risk_gate_digest = value.get("risk_gate_digest")
        if risk_gate_digest in (None, ""):
            normalized_risk_gate_digest = None
        else:
            normalized_risk_gate_digest = str(risk_gate_digest).lower()
            if _DIGEST_RE.fullmatch(normalized_risk_gate_digest) is None:
                raise TestnetCoordinatorError("risk_gate_digest_invalid")
        execution_slice = value.get("execution_slice")
        if execution_slice is not None and not isinstance(execution_slice, Mapping):
            raise TestnetCoordinatorError("execution_slice_invalid")
        if isinstance(execution_slice, Mapping) and not str(execution_slice.get("execution_slice_id") or "").strip():
            raise TestnetCoordinatorError("execution_slice_identity_missing")
        return cls(
            strategy_family=strategy_family,
            strategy_session_id=_required_text(
                value.get("strategy_session_id"), "strategy_session_id"
            ),
            strategy_revision_id=_required_text(
                value.get("strategy_revision_id"), "strategy_revision_id"
            ),
            plan_digest=plan_digest,
            account_fingerprint=account_fingerprint,
            broker_id=broker_id,
            environment=environment,
            transport_profile=transport_profile,
            instrument_id=_required_text(value.get("instrument_id"), "instrument_id"),
            runtime_id=_required_text(value.get("runtime_id"), "runtime_id"),
            release_sha=release_sha,
            capability_revision=capability_revision,
            **optional_numbers,
            risk_gate_digest=normalized_risk_gate_digest,
            execution_slice=deepcopy(dict(execution_slice)) if isinstance(execution_slice, Mapping) else None,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {field: getattr(self, field) for field in _ACTIVATION_FIELDS}

    @property
    def activation_id(self) -> str:
        return activation_digest(self.to_mapping())


class TestnetAutomationCoordinator:
    """One durable, identity-bound Testnet automation boundary."""

    __test__ = False

    def __init__(
        self,
        output_root: Path,
        *,
        clock: Callable[[], str | datetime] | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.root = self.output_root / COORDINATOR_ROOT
        self.current_path = self.root / COORDINATOR_STATE_FILE
        self.events_path = self.root / COORDINATOR_EVENTS_FILE
        self.clock = clock

    def activate(
        self,
        activation: TestnetActivation | Mapping[str, Any],
        *,
        command_id: str | None = None,
        now: str | datetime | None = None,
    ) -> dict[str, Any]:
        return self.command(
            "activate",
            activation,
            command_id=command_id,
            now=now,
        )

    def command(
        self,
        action: str,
        payload: Mapping[str, Any] | TestnetActivation | None = None,
        *,
        command_id: str | None = None,
        now: str | datetime | None = None,
    ) -> dict[str, Any]:
        normalized_action = str(action or "").strip().lower()
        if normalized_action not in _ACTIONS:
            raise TestnetCoordinatorError("action_invalid", {"action": normalized_action})
        if normalized_action == "status":
            return self.status()
        if normalized_action == "preflight":
            return self.preflight()
        if normalized_action == "activate":
            return self._activate(payload, command_id=command_id, now=now)
        if normalized_action == "select_candidate":
            return self._select_candidate(
                payload,
                command_id=command_id,
                now=now,
            )
        if normalized_action == "reconcile_stop":
            return self._reconcile_stop(payload, command_id=command_id, now=now)
        return self._operator_intent(
            normalized_action,
            payload if isinstance(payload, Mapping) else {},
            command_id=command_id,
            now=now,
        )

    def status(self) -> dict[str, Any]:
        try:
            current = self._read_current()
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return self._blocked_state("coordinator_state_corrupt")
        if current is None:
            return self._idle_state()
        return dict(current)

    def preflight(
        self,
        *,
        broker: object | None = None,
        strategy_family: str | None = None,
        market: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = self.status()
        if current.get("status") == "idle":
            return {
                **current,
                "event": "preflight",
                "ready": False,
                "execution_ready": False,
                "broker_operation_invoked": False,
                "network_operation_invoked": False,
                "execution_blocker": "activation_required",
                "next_action": "await_activation",
            }
        base = {
            **current,
            "event": "preflight",
            "ready": current.get("blocker") is None,
            "execution_ready": False,
            "broker_operation_invoked": False,
            "network_operation_invoked": False,
            "next_action": "await_execution_capability",
        }
        if broker is None:
            return base
        family = str(strategy_family or current.get("strategy_family") or "").strip().lower()
        if family not in {"dca", "grid"}:
            return {
                **base,
                "ready": False,
                "execution_ready": False,
                "execution_blocker": "strategy_family_required",
                "next_action": "notify_park_and_wait",
            }
        try:
            broker_preflight = self._validate_lifecycle_preflight(
                broker,
                strategy_family=family,
                current=current,
            )
            if market is not None:
                self._validate_authoritative_market(
                    market,
                    current=current,
                    observed_at=self._timestamp(None),
                )
        except Exception as exc:  # noqa: BLE001 - preflight is a reporting gate.
            blocker = str(getattr(exc, "code", "") or "testnet_preflight_blocked")
            return {
                **base,
                "ready": False,
                "execution_ready": False,
                "execution_blocker": blocker,
                "broker_preflight": {
                    "status": "BLOCKED",
                    "reason": blocker,
                },
                "next_action": "notify_park_and_wait",
            }
        return {
            **base,
            "ready": True,
            "execution_ready": True,
            "execution_blocker": None,
            "broker_preflight": broker_preflight,
            "instrument_id": current.get("selected_instrument_id")
            or current.get("instrument_id"),
            "market_identity": (
                {
                    "source": market.get("source"),
                    "cursor": market.get("cursor"),
                    "instrument_id": market.get("instrument_id"),
                    "mapping_revision": market.get("mapping_revision"),
                    "universe_revision": market.get("universe_revision"),
                    "connection_epoch": market.get("connection_epoch"),
                }
                if market is not None
                else None
            ),
            "next_action": "await_candidate_selection",
        }

    def enable_paper_execution(self, broker: object, *, now: str | datetime | None = None) -> dict[str, Any]:
        """Bind the public standard-broker local Paper profile to this activation.

        The activation remains Testnet-labelled for the future external seam;
        this capability is explicitly local Paper and can never invoke network
        transport or credentials.
        """
        current = self._read_current_or_raise()
        if current is None or current.get("status") == "idle":
            raise TestnetCoordinatorError("activation_required")
        try:
            paper = broker.preflight()
            environment = getattr(paper.get("environment"), "value", paper.get("environment")) if isinstance(paper, Mapping) else getattr(paper.environment, "value", paper.environment)
            network_io = paper.get("network_io") if isinstance(paper, Mapping) else paper.network_io
            real_money = paper.get("real_money_eligible") if isinstance(paper, Mapping) else paper.real_money_eligible
            ports = paper.get("ports") if isinstance(paper, Mapping) else paper.ports
            if str(environment).lower() != "paper" or network_io is not False or real_money is not False or "order_execution" not in tuple(ports or ()):
                raise TestnetCoordinatorError("paper_profile_invalid")
            if getattr(getattr(broker, "transport", None), "local_only", False) is not True:
                raise TestnetCoordinatorError("paper_transport_not_local")
        except TestnetCoordinatorError:
            raise
        except Exception as exc:
            raise TestnetCoordinatorError("paper_profile_invalid", {"error": type(exc).__name__}) from exc
        state = {
            **current,
            "event": "paper_execution_enabled",
            "action": "enable_paper_execution",
            "status": "paper_execution_ready",
            "occurred_at": self._timestamp(now),
            "execution_enabled": True,
            "execution_ready": True,
            "execution_blocker": None,
            "execution_profile": "standard-broker-paper",
            "paper_network_io": False,
            "broker_operation_invoked": False,
            "network_operation_invoked": False,
            "execution_mutation": False,
            "next_action": "await_execution_command",
        }
        return self._record(state)

    def enable_testnet_execution(self, broker: object, *, now: str | datetime | None = None) -> dict[str, Any]:
        """Bind the explicit attended Hyperliquid Testnet protection profile."""
        current = self._read_current_or_raise()
        if current is None or current.get("status") == "idle":
            raise TestnetCoordinatorError("activation_required")
        if current.get("transport_profile") != TESTNET_PROTECTED_TRANSPORT_PROFILE:
            raise TestnetCoordinatorError("protected_testnet_profile_required")
        from services.testnet_execution import ExternalTestnetExecutionPort

        try:
            port = ExternalTestnetExecutionPort(broker)
            preflight = port.preflight()
        except Exception as exc:
            code = str(getattr(exc, "code", "") or "testnet_execution_blocked")
            return self._record({
                **current,
                "event": "testnet_execution_blocked",
                "action": "enable_testnet_execution",
                "status": "testnet_execution_blocked",
                "occurred_at": self._timestamp(now),
                "execution_enabled": False,
                "execution_ready": False,
                "execution_blocker": code,
                "next_action": "notify_park_and_wait",
                "broker_operation_invoked": False,
                "network_operation_invoked": False,
                "execution_mutation": False,
                "secret_material_present": False,
            })
        state = {
            **current,
            "event": "testnet_execution_enabled",
            "action": "enable_testnet_execution",
            "status": "testnet_execution_ready",
            "occurred_at": self._timestamp(now),
            "execution_enabled": True,
            "execution_ready": True,
            "execution_blocker": None,
            "execution_profile": TESTNET_PROTECTED_TRANSPORT_PROFILE,
            "broker_preflight": preflight,
            "paper_network_io": False,
            "network_operation_invoked": False,
            "execution_mutation": False,
            "secret_material_present": False,
            "next_action": "await_attended_execution_command",
        }
        return self._record(state)

    def submit_grid_orders(
        self,
        plan: Mapping[str, Any],
        *,
        broker: object,
        command_id: str,
        now: str | datetime | None = None,
    ) -> dict[str, Any]:
        """Submit one canonical request per Grid rung through the selected port."""
        current = self._read_current_or_raise()
        if current is None or current.get("status") not in {
            "paper_execution_ready", "paper_execution_active", "testnet_execution_ready", "testnet_execution_active",
        }:
            raise TestnetCoordinatorError("execution_not_enabled")
        normalized_command_id = self._command_id(
            command_id,
            "submit_grid_orders",
            str(current.get("activation_id") or ""),
        )
        replay = self._replay(normalized_command_id)
        if replay is not None:
            return replay
        if str(plan.get("plan_digest") or "") != str(current.get("plan_digest") or ""):
            raise TestnetCoordinatorError("plan_digest_mismatch")
        execution_slice = current.get("execution_slice")
        slice_id = execution_slice.get("execution_slice_id") if isinstance(execution_slice, Mapping) else None
        if not slice_id:
            raise TestnetCoordinatorError("execution_slice_required")
        from services.testnet_execution import ExternalTestnetExecutionPort, PaperExecutionPort, map_grid_orders

        try:
            requests = map_grid_orders(
                plan,
                activation_id=str(current["activation_id"]),
                slice_id=str(slice_id),
                command_id=normalized_command_id,
            )
            port = (
                ExternalTestnetExecutionPort(broker)
                if current.get("status", "").startswith("testnet_")
                else PaperExecutionPort(broker)
            )
            receipts = [port.submit(request) for request in requests]
        except Exception as exc:
            if isinstance(exc, TestnetCoordinatorError):
                raise
            raise TestnetCoordinatorError("execution_blocked", {"error": type(exc).__name__}) from exc
        unknown = [receipt for receipt in receipts if receipt.get("unknown")]
        external = current.get("status", "").startswith("testnet_")
        state = {
            **current,
            "event": "testnet_orders_submitted" if external else "paper_orders_submitted",
            "action": "submit_grid_orders",
            "command_id": normalized_command_id,
            "status": ("testnet_execution_active" if external else "paper_execution_active") if not unknown else ("testnet_execution_blocked" if external else "paper_execution_blocked"),
            "occurred_at": self._timestamp(now),
            "execution_receipts": receipts,
            "canonical_order_count": len(requests),
            "execution_enabled": not unknown,
            "execution_ready": not unknown,
            "execution_blocker": "execution_unknown" if unknown else None,
            "next_action": "await_fill_or_cancel" if not unknown else "notify_park_and_wait",
            "broker_operation_invoked": bool(receipts),
            "network_operation_invoked": external and bool(receipts),
            "execution_mutation": bool(receipts) and not unknown,
        }
        return self._record(state)

    def _activate(
        self,
        payload: Mapping[str, Any] | TestnetActivation | None,
        *,
        command_id: str | None,
        now: str | datetime | None,
    ) -> dict[str, Any]:
        activation = (
            payload
            if isinstance(payload, TestnetActivation)
            else TestnetActivation.from_mapping(payload or {})
        )
        normalized_command_id = self._command_id(command_id, "activate", activation.activation_id)
        replay = self._replay(normalized_command_id)
        if replay is not None:
            return replay
        current = self._read_current_or_raise()
        if current is not None and current.get("status") != "idle":
            current_activation = str(current.get("activation_id") or "")
            if current_activation != activation.activation_id:
                raise TestnetCoordinatorError(
                    "active_session_conflict",
                    {"active_activation_id": current_activation},
                )
            # Same immutable activation is safe to read back even if the
            # caller supplied a fresh command id.
            return dict(current)
        timestamp = self._timestamp(now)
        execution_slice = deepcopy(activation.execution_slice)
        if execution_slice is None:
            # Dashboard confirmation already selected one explicit
            # instrument. Materialize the pending broker-bound slice at the
            # composition root so enable_* cannot expose a null slice. The
            # full candidate selector may replace this envelope later when
            # fresh market/account facts are supplied via select_candidate.
            suffix = activation.activation_id.replace("sha256:", "")[:16]
            asset = activation.instrument_id.split("-", 1)[0].split(".", 1)[0]
            quantity = "1"
            execution_slice = {
                "schema_version": "execution-slice-v1",
                "execution_slice_id": f"execution-activation:{suffix}",
                "portfolio_session_id": activation.strategy_session_id,
                "allocation_id": f"allocation-activation:{suffix}",
                "asset": asset,
                "broker_binding": {
                    "activation_id": activation.activation_id,
                    "broker_id": activation.broker_id,
                    "environment": activation.environment,
                    "transport_profile": activation.transport_profile,
                    "instrument_id": activation.instrument_id,
                    "account_fingerprint": activation.account_fingerprint,
                    "runtime_id": activation.runtime_id,
                    "release_sha": activation.release_sha,
                    "capability_revision": activation.capability_revision,
                },
                "allocation": {
                    "allocation_id": f"allocation-activation:{suffix}",
                    "portfolio_session_id": activation.strategy_session_id,
                    "candidate_id": f"activation-candidate:{suffix}",
                    "asset": asset,
                    "instrument_id": activation.instrument_id,
                    "direction": "flat",
                    "requested_quantity": quantity,
                    "effective_quantity": quantity,
                    "requested_notional": activation.requested_notional,
                    "effective_notional": activation.effective_notional,
                    "position_action": "open",
                    "status": "accepted",
                    "execution_slice_id": f"execution-activation:{suffix}",
                    "source_strategy_plan_digest": activation.plan_digest,
                    "candidate_rank": 1,
                    "reasons": [],
                },
                "selection_id": f"activation-selection:{suffix}",
                "status": "pending",
            }
        if execution_slice is not None:
            allocation = execution_slice.get("allocation")
            if not isinstance(allocation, Mapping):
                raise TestnetCoordinatorError("execution_slice_allocation_required")
            if str(allocation.get("asset") or "").strip() != str(execution_slice.get("asset") or "").strip():
                raise TestnetCoordinatorError("execution_slice_asset_mismatch")
            binding = dict(execution_slice.get("broker_binding") or {})
            binding["activation_id"] = activation.activation_id
            execution_slice["broker_binding"] = binding
        state = {
            "schema_version": COORDINATOR_SCHEMA,
            "event": "activated",
            "action": "activate",
            "status": "activated",
            "command_id": normalized_command_id,
            "occurred_at": timestamp,
            "activation_id": activation.activation_id,
            **activation.to_mapping(),
            "ready": True,
            "execution_enabled": False,
            "execution_ready": False,
            "execution_blocker": "capability_gap:execution",
            "next_action": "await_execution_capability",
            "broker_operation_invoked": False,
            "network_operation_invoked": False,
            "execution_mutation": False,
            "secret_material_present": False,
            "blocker": None,
            "execution_slice": execution_slice,
            "selected_execution_slice_id": execution_slice.get("execution_slice_id") if execution_slice else None,
            "selected_asset": execution_slice.get("asset") if execution_slice else None,
            "selected_instrument_id": (execution_slice.get("broker_binding") or {}).get("instrument_id") if execution_slice else None,
        }
        return self._record(state)

    def _operator_intent(
        self,
        action: str,
        payload: Mapping[str, Any],
        *,
        command_id: str | None,
        now: str | datetime | None,
    ) -> dict[str, Any]:
        current = self._read_current_or_raise()
        if current is None or current.get("status") == "idle":
            raise TestnetCoordinatorError("activation_required")
        activation_id = str(current.get("activation_id") or "")
        normalized_command_id = self._command_id(command_id, action, activation_id)
        replay = self._replay(normalized_command_id)
        if replay is not None:
            return replay
        reason = str(payload.get("reason") or "").strip() if isinstance(payload, Mapping) else ""
        timestamp = self._timestamp(now)
        if action == "pause":
            status = "paused"
            event = "paused"
            execution_blocker = current.get("execution_blocker")
            next_action = "await_resume"
        elif action == "stop":
            status = "stop_requested"
            event = "stop_requested"
            execution_blocker = "cancel_and_flatten_reconciliation_required"
            next_action = "await_cancel_and_flat_reconcile"
        elif action == "flatten":
            status = "flatten_requested"
            event = "flatten_requested"
            execution_blocker = "flat_reconciliation_required"
            next_action = "await_flat_reconcile"
        elif action == "interrupt":
            status = "interrupted"
            event = "interrupted"
            execution_blocker = current.get("execution_blocker")
            next_action = "await_resume"
        else:
            status = "resume_pending"
            event = "resume_requested"
            execution_blocker = "revalidation_required"
            next_action = "await_revalidation"
        state = {
            **current,
            "event": event,
            "action": action,
            "status": status,
            "command_id": normalized_command_id,
            "occurred_at": timestamp,
            "reason": reason or None,
            "execution_enabled": False,
            "execution_ready": False,
            "execution_blocker": execution_blocker,
            "next_action": next_action,
            "broker_operation_invoked": False,
            "network_operation_invoked": False,
            "execution_mutation": False,
            "secret_material_present": False,
        }
        return self._record(state)

    def _reconcile_stop(
        self,
        payload: Mapping[str, Any],
        *,
        command_id: str | None,
        now: str | datetime | None,
    ) -> dict[str, Any]:
        """Close a zero-order local Paper stop without invoking a Broker."""

        current = self._read_current_or_raise()
        if current is None or current.get("status") == "idle":
            raise TestnetCoordinatorError("activation_required")
        activation_id = str(current.get("activation_id") or "")
        normalized_command_id = self._command_id(command_id, "reconcile_stop", activation_id)
        replay = self._replay(normalized_command_id)
        if replay is not None:
            return replay
        if current.get("status") != "stop_requested":
            raise TestnetCoordinatorError("stop_reconciliation_required")
        if current.get("execution_profile") != "standard-broker-paper":
            raise TestnetCoordinatorError("paper_stop_reconciliation_required")
        if current.get("execution_mutation") is True or current.get("network_operation_invoked") is True:
            raise TestnetCoordinatorError("submitted_orders_require_reconciliation")
        if int(current.get("canonical_order_count") or 0) != 0 or current.get("execution_receipts"):
            raise TestnetCoordinatorError("submitted_orders_require_reconciliation")
        timestamp = self._timestamp(now)
        receipt = {
            "schema_version": "testnet-stop-reconciliation-receipt-v1",
            "event": "reconcile_stop",
            "status": "reconciled",
            "activation_id": activation_id,
            "account_fingerprint": current.get("account_fingerprint"),
            "fingerprint_scheme": LEGACY_ACCOUNT_FINGERPRINT_SCHEME,
            "execution_profile": current.get("execution_profile"),
            "submitted_order_count": 0,
            "broker_operation_invoked": False,
            "network_operation_invoked": False,
            "execution_mutation": False,
            "occurred_at": timestamp,
            "reason": str(payload.get("reason") or "") or None,
        }
        state = {
            **self._idle_state(),
            "event": "reconcile_stop",
            "action": "reconcile_stop",
            "command_id": normalized_command_id,
            "occurred_at": timestamp,
            "previous_activation_id": activation_id,
            "status": "idle",
            "receipt": receipt,
            "fingerprint_scheme": receipt["fingerprint_scheme"],
        }
        return self._record(state)

    def _select_candidate(
        self,
        payload: Mapping[str, Any] | TestnetActivation | None,
        *,
        command_id: str | None,
        now: str | datetime | None,
    ) -> dict[str, Any]:
        current = self._read_current_or_raise()
        if current is None or current.get("status") == "idle":
            raise TestnetCoordinatorError("activation_required")
        if current.get("status") not in {
            "activated",
            "candidate_blocked",
            "portfolio_held",
        }:
            # Candidate replacement is a pre-side-effect operation only.  An
            # active/canary/lifecycle state already owns its selected slice.
            raise TestnetCoordinatorError("candidate_selection_locked")
        if not isinstance(payload, Mapping):
            raise TestnetCoordinatorError("candidate_selection_payload_invalid")
        activation_id = str(current.get("activation_id") or "")
        normalized_command_id = self._command_id(
            command_id,
            "select_candidate",
            activation_id,
        )
        replay = self._replay(normalized_command_id)
        if replay is not None:
            return replay
        candidates = payload.get("candidates")
        snapshot = payload.get("snapshot")
        policy = payload.get("policy")
        if not isinstance(candidates, (list, tuple)):
            raise TestnetCoordinatorError("candidate_selection_candidates_required")
        if snapshot is None or policy is None:
            raise TestnetCoordinatorError("candidate_selection_facts_required")
        from services.testnet_candidate_selection import TestnetCandidateSelector

        observed_at = now or self._timestamp(None)
        try:
            result = TestnetCandidateSelector().select(
                candidates,
                strategy_family=str(current.get("strategy_family") or ""),
                strategy_session_id=str(current.get("strategy_session_id") or ""),
                strategy_revision_id=str(current.get("strategy_revision_id") or ""),
                snapshot=snapshot,
                policy=policy,
                now=observed_at,
            )
        except Exception as exc:
            if isinstance(exc, TestnetCoordinatorError):
                raise
            raise TestnetCoordinatorError(
                "candidate_selection_blocked",
                {"error": f"{type(exc).__name__}:{exc}"},
            ) from exc
        if result.status == "selected":
            status = "candidate_selected"
            event = "candidate_selected"
            blocker = None
            next_action = "await_execution_capability"
        elif result.status == "held":
            status = "portfolio_held"
            event = "portfolio_held"
            blocker = result.blocker or "portfolio_risk_hold"
            next_action = "notify_park_and_wait"
        else:
            status = "candidate_blocked"
            event = "candidate_blocked"
            blocker = result.blocker or "no_eligible_candidate"
            next_action = "await_candidate_revalidation"
        execution_slice: dict[str, Any] | None = None
        if result.status == "selected":
            allocations = tuple(
                getattr(result.portfolio_result, "selected_allocations", ())
            )
            if len(allocations) != 1:
                raise TestnetCoordinatorError(
                    "execution_slice_cardinality_invalid",
                    {"selected_allocation_count": len(allocations)},
                )
            allocation = allocations[0]
            if not allocation.execution_slice_id:
                raise TestnetCoordinatorError("execution_slice_identity_missing")
            execution_slice_contract = ExecutionSlice(
                execution_slice_id=allocation.execution_slice_id,
                portfolio_session_id=allocation.portfolio_session_id,
                allocation_id=allocation.allocation_id,
                asset=allocation.asset,
                broker_binding={
                    "activation_id": activation_id,
                    "broker_id": current.get("broker_id"),
                    "environment": current.get("environment"),
                    "transport_profile": current.get("transport_profile"),
                    "instrument_id": result.selected_instrument_id,
                    "account_fingerprint": current.get("account_fingerprint"),
                    "runtime_id": current.get("runtime_id"),
                    "release_sha": current.get("release_sha"),
                    "capability_revision": current.get("capability_revision"),
                    "candidate_set_id": result.candidate_set.candidate_set_id,
                    "selection_id": result.portfolio_result.selection_id,
                },
                status="pending",
            )
            execution_slice = {
                **execution_slice_contract.to_dict(),
                "allocation": allocation.to_dict(),
                "selection_id": result.portfolio_result.selection_id,
            }
        state = {
            **current,
            "event": event,
            "action": "select_candidate",
            "status": status,
            "command_id": normalized_command_id,
            "occurred_at": self._timestamp(now),
            "candidate_selection": result.to_dict(),
            "candidate_set_id": result.candidate_set.candidate_set_id,
            "selected_asset": result.selected_asset,
            "selected_instrument_id": result.selected_instrument_id,
            "global_hold": result.global_hold,
            "execution_slice": execution_slice,
            "selected_execution_slice_id": (
                execution_slice.get("execution_slice_id")
                if execution_slice is not None
                else None
            ),
            "ready": result.status == "selected",
            "execution_enabled": False,
            "execution_ready": False,
            "execution_blocker": (
                "capability_gap:execution"
                if result.status == "selected"
                else blocker
            ),
            "next_action": next_action,
            "broker_operation_invoked": False,
            "network_operation_invoked": False,
            "execution_mutation": False,
            "secret_material_present": False,
            "blocker": blocker,
        }
        return self._record(state)

    def run_transport_canary(
        self,
        plan: object,
        *,
        confirmation: Mapping[str, Any],
        broker: object,
        facts: object,
        protection: object,
        confirmation_ledger: object | None = None,
        timestamp: str | datetime | None = None,
    ) -> dict[str, Any]:
        """Run one explicit BTC transport canary for the selected slice."""

        current = self._read_current_or_raise()
        if current is None or current.get("status") != "candidate_selected":
            raise TestnetCoordinatorError("candidate_selection_required")
        selected_instrument = str(current.get("selected_instrument_id") or "")
        from services.testnet_transport_canary import TestnetTransportCanary

        if not selected_instrument:
            raise TestnetCoordinatorError("selected_instrument_required")
        try:
            plan_instrument = str(getattr(plan, "instrument_id", "") or plan.get("instrument_id", ""))
        except AttributeError as exc:
            raise TestnetCoordinatorError("canary_plan_invalid") from exc
        if plan_instrument != selected_instrument:
            raise TestnetCoordinatorError(
                "selected_instrument_mismatch",
                {"selected_instrument_id": selected_instrument, "plan_instrument_id": plan_instrument},
            )
        self._validate_canary_identity(plan, current)
        try:
            runner = TestnetTransportCanary(
                self.output_root,
                broker,
                facts,
                protection,
                confirmation_ledger=confirmation_ledger,
            )
            result = runner.run(
                plan,
                confirmation=confirmation,
                timestamp=timestamp or self._timestamp(None),
            )
        except Exception as exc:  # noqa: BLE001 - normalize at the Coordinator boundary.
            blocker = str(getattr(exc, "code", "") or str(exc) or type(exc).__name__)
            state = {
                **current,
                "event": "transport_canary_blocked",
                "action": "run_transport_canary",
                "status": "canary_blocked",
                "occurred_at": self._timestamp(timestamp),
                "canary_blocker": blocker,
                "execution_enabled": False,
                "execution_ready": False,
                "execution_blocker": blocker,
                "next_action": "notify_park_and_wait",
                "execution_mutation": False,
                "network_operation_invoked": False,
                "blocker": blocker,
            }
            self._record(state)
            if isinstance(exc, TestnetCoordinatorError):
                raise
            raise TestnetCoordinatorError("transport_canary_blocked", {"blocker": blocker}) from exc
        state = {
            **current,
            "event": "transport_canary_completed",
            "action": "run_transport_canary",
            "status": "canary_completed",
            "occurred_at": self._timestamp(timestamp),
            "canary": result,
            "canary_status": result.get("status"),
            "execution_enabled": False,
            "execution_ready": False,
            "execution_blocker": "capability_gap:execution",
            "next_action": "record_canary_evidence",
            "execution_mutation": True,
            "network_operation_invoked": True,
            "blocker": None,
        }
        return self._record(state)

    def progressive_expand(
        self,
        candidates: Sequence[Mapping[str, Any]],
        *,
        preflight: Callable[[Mapping[str, Any]], Mapping[str, Any]],
        canary: Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]],
        equity: Any,
        command_id: str | None = None,
        timestamp: str | datetime | None = None,
    ) -> dict[str, Any]:
        """Admit the next ranked pair without creating concurrent slices."""

        current = self._read_current_or_raise()
        if current is None or current.get("status") == "idle":
            raise TestnetCoordinatorError("activation_required")
        if current.get("status") != "soak_ready":
            raise TestnetCoordinatorError(
                "testnet_expansion_soak_gate_required",
                {"current_status": current.get("status")},
            )
        scheduler_state = self._scheduler_state()
        if (
            scheduler_state.get("status") != "active"
            or str(scheduler_state.get("activation_id") or "")
            != str(current.get("activation_id") or "")
            or scheduler_state.get("owner_epoch") in (None, "")
        ):
            raise TestnetCoordinatorError(
                "testnet_expansion_scheduler_gate_required",
                {"scheduler_status": scheduler_state.get("status")},
            )
        if current.get("canary_status") not in {"FLAT_RECONCILED", "flat_reconciled"}:
            raise TestnetCoordinatorError("testnet_expansion_canary_gate_required")
        activation_id = str(current.get("activation_id") or "")
        normalized_command_id = self._command_id(command_id, "progressive_expand", activation_id)
        replay = self._replay(normalized_command_id)
        if replay is not None:
            return replay
        from services.testnet_progressive_expansion import TestnetProgressiveExpansion

        result = TestnetProgressiveExpansion().admit_ranked(
            candidates,
            preflight=preflight,
            canary=canary,
            equity=equity,
        )
        expansion_status = str(result.get("status") or "blocked")
        if expansion_status == "admitted":
            status = "candidate_admitted"
            next_action = "await_strategy_activation"
        elif expansion_status == "portfolio_hold":
            status = "portfolio_held"
            next_action = "notify_park_and_wait"
        else:
            status = "candidate_blocked"
            next_action = "await_candidate_revalidation"
        state = {
            **current,
            "event": "progressive_expansion_admitted" if expansion_status == "admitted" else "progressive_expansion_blocked",
            "action": "progressive_expand",
            "status": status,
            "command_id": normalized_command_id,
            "occurred_at": self._timestamp(timestamp),
            "expansion": result,
            "selected_asset": result.get("selected_asset"),
            "selected_instrument_id": result.get("selected_instrument_id"),
            "global_hold": result.get("global_hold") is True,
            "ready": expansion_status == "admitted",
            "execution_enabled": False,
            "execution_ready": False,
            "execution_blocker": result.get("blocker") or "capability_gap:execution",
            "next_action": next_action,
            "execution_mutation": False,
            "network_operation_invoked": False,
            "secret_material_present": False,
            "blocker": result.get("blocker"),
        }
        return self._record(state)

    def record_soak_window(
        self,
        observation: Mapping[str, Any],
        *,
        strategy_family: str,
        instrument_id: str = "BTC-USD-PERP",
        timestamp: str | datetime | None = None,
    ) -> dict[str, Any]:
        """Record one immutable Testnet automation evidence window."""

        current = self._read_current_or_raise()
        if current is None or current.get("status") == "idle":
            raise TestnetCoordinatorError("activation_required")
        self._validate_soak_identity(current, strategy_family=strategy_family, instrument_id=instrument_id)
        from services.testnet_automation_readiness import TestnetAutomationReadiness

        readiness = TestnetAutomationReadiness(
            self.output_root,
            strategy_family=strategy_family,
            instrument_id=instrument_id,
        )
        row = readiness.record_window(dict(observation))
        status = "soak_blocked" if row.get("status") != "pass" else "soak_in_progress"
        state = {
            **current,
            "event": "soak_window_recorded",
            "action": "record_soak_window",
            "status": status,
            "occurred_at": self._timestamp(timestamp),
            "soak": readiness.public_status(now=self._timestamp(timestamp)),
            "soak_window": row,
            "execution_enabled": False if status == "soak_blocked" else current.get("execution_enabled") is True,
            "execution_ready": False if status == "soak_blocked" else current.get("execution_ready") is True,
            "next_action": "notify_park_and_wait" if status == "soak_blocked" else "continue_soak",
            "blocker": next(iter(row.get("blockers") or []), None),
        }
        return self._record(state)

    def finalize_soak(
        self,
        *,
        strategy_family: str,
        instrument_id: str = "BTC-USD-PERP",
        now: str | None = None,
    ) -> dict[str, Any]:
        """Finalize the two-window Testnet readiness evidence for one mode."""

        current = self._read_current_or_raise()
        if current is None or current.get("status") == "idle":
            raise TestnetCoordinatorError("activation_required")
        self._validate_soak_identity(current, strategy_family=strategy_family, instrument_id=instrument_id)
        from services.testnet_automation_readiness import TestnetAutomationReadiness

        readiness = TestnetAutomationReadiness(
            self.output_root,
            strategy_family=strategy_family,
            instrument_id=instrument_id,
        )
        receipt = readiness.finalize(now=now)
        ready = receipt.get("status") == "ready"
        state = {
            **current,
            "event": "soak_finalized",
            "action": "finalize_soak",
            "status": "soak_ready" if ready else "soak_blocked",
            "occurred_at": self._timestamp(now),
            "soak": readiness.public_status(now=now),
            "soak_receipt": receipt,
            "execution_enabled": False,
            "execution_ready": False,
            "next_action": "continue_progressive_expansion" if ready else "notify_park_and_wait",
            "blocker": None if ready else next(iter(receipt.get("blockers") or []), None),
        }
        return self._record(state)

    @staticmethod
    def _validate_soak_identity(
        current: Mapping[str, Any],
        *,
        strategy_family: str,
        instrument_id: str,
    ) -> None:
        expected_family = str(current.get("strategy_family") or "").strip().lower()
        requested_family = str(strategy_family or "").strip().lower()
        if requested_family != expected_family:
            raise TestnetCoordinatorError(
                "soak_strategy_family_mismatch",
                {"expected": expected_family, "received": requested_family},
            )
        expected_instrument = str(
            current.get("selected_instrument_id") or current.get("instrument_id") or ""
        ).strip()
        if expected_instrument and str(instrument_id or "").strip() != expected_instrument:
            raise TestnetCoordinatorError(
                "soak_instrument_mismatch",
                {"expected": expected_instrument, "received": instrument_id},
            )

    def start_dca_session(
        self,
        plan: Mapping[str, Any],
        *,
        confirmation: Mapping[str, Any],
        market: Mapping[str, Any],
        broker: object,
        timestamp: str | datetime | None = None,
    ) -> dict[str, Any]:
        """Start the canonical DCA lifecycle for the selected slice."""

        current = self._require_strategy_slice(plan, family="dca", expected_status="candidate_selected")
        execution_plan = self._execution_plan(plan, current)
        observed_at = self._timestamp(timestamp)
        self._validate_testnet_confirmation(execution_plan, current, confirmation)
        self._validate_authoritative_market(market, current=current, observed_at=observed_at)
        self._validate_lifecycle_preflight(broker, strategy_family="dca", current=current)
        from services.dca_testnet_lifecycle import DcaTestnetLifecycle

        lifecycle = DcaTestnetLifecycle(self.output_root, broker)
        try:
            state = lifecycle.start(execution_plan, timestamp=observed_at)
        except Exception as exc:  # noqa: BLE001 - persist lifecycle blocker at the Coordinator seam.
            return self._publish_dca_state(
                current,
                execution_plan,
                {
                    "status": "blocked_reconciliation",
                    "blocker": f"dca_start_failed:{type(exc).__name__}:{exc}",
                    "next_action": "notify_park_and_wait",
                },
                observed_at=observed_at,
            )
        return self._publish_dca_state(current, execution_plan, state, observed_at=observed_at)

    def advance_dca_session(
        self,
        plan: Mapping[str, Any],
        *,
        broker: object,
        fill: Mapping[str, Any] | None = None,
        price: float | None = None,
        market: Mapping[str, Any] | None = None,
        timestamp: str | datetime | None = None,
    ) -> dict[str, Any]:
        """Apply one canonical DCA fill or market event through the Coordinator."""

        current = self._require_strategy_slice(plan, family="dca")
        execution_plan = self._execution_plan(plan, current)
        if current.get("status") not in {
            "dca_running",
            "dca_interrupted",
            "dca_blocked",
        }:
            raise TestnetCoordinatorError("dca_session_not_active")
        observed_at = self._timestamp(timestamp)
        self._validate_authoritative_market(market, current=current, observed_at=observed_at)
        self._validate_lifecycle_preflight(broker, strategy_family="dca", current=current)
        from services.dca_testnet_lifecycle import DcaTestnetLifecycle

        lifecycle = DcaTestnetLifecycle(self.output_root, broker)
        try:
            if fill is not None:
                state = lifecycle.on_fill(execution_plan, dict(fill), timestamp=observed_at)
            elif price is not None:
                state = lifecycle.on_market_event(
                    execution_plan,
                    price=float(price),
                    timestamp=observed_at,
                )
            else:
                raise TestnetCoordinatorError("dca_event_required")
        except TestnetCoordinatorError:
            raise
        except Exception as exc:  # noqa: BLE001 - retain the lifecycle blocker.
            try:
                state = lifecycle.snapshot(execution_plan)
            except Exception:
                state = {
                    "status": "blocked_reconciliation",
                    "blocker": f"dca_event_failed:{type(exc).__name__}:{exc}",
                    "next_action": "notify_park_and_wait",
                }
        return self._publish_dca_state(current, execution_plan, state, observed_at=observed_at)

    def interrupt_dca_session(
        self,
        plan: Mapping[str, Any],
        *,
        broker: object,
        reason: str = "manual_interrupt",
        timestamp: str | datetime | None = None,
    ) -> dict[str, Any]:
        """Cancel pending DCA entries while preserving the current position."""

        current = self._require_strategy_slice(plan, family="dca")
        execution_plan = self._execution_plan(plan, current)
        observed_at = self._timestamp(timestamp)
        from services.dca_testnet_lifecycle import DcaTestnetLifecycle

        state = DcaTestnetLifecycle(self.output_root, broker).interrupt(
            execution_plan,
            timestamp=observed_at,
            reason=reason,
        )
        return self._publish_dca_state(current, execution_plan, state, observed_at=observed_at)

    def resume_dca_session(
        self,
        plan: Mapping[str, Any],
        *,
        broker: object,
        confirmation: Mapping[str, Any],
        market: Mapping[str, Any],
        timestamp: str | datetime | None = None,
    ) -> dict[str, Any]:
        """Resume an interrupted DCA session after fresh host validation."""

        current = self._require_strategy_slice(plan, family="dca")
        execution_plan = self._execution_plan(plan, current)
        if current.get("status") != "dca_interrupted":
            raise TestnetCoordinatorError("dca_session_not_interrupted")
        observed_at = self._timestamp(timestamp)
        self._validate_testnet_confirmation(execution_plan, current, confirmation)
        self._validate_authoritative_market(market, current=current, observed_at=observed_at)
        self._validate_lifecycle_preflight(broker, strategy_family="dca", current=current)
        from services.dca_testnet_lifecycle import DcaTestnetLifecycle

        state = DcaTestnetLifecycle(self.output_root, broker).resume(
            execution_plan,
            timestamp=observed_at,
        )
        return self._publish_dca_state(current, execution_plan, state, observed_at=observed_at)

    def start_grid_session(
        self,
        plan: Mapping[str, Any],
        *,
        confirmation: Mapping[str, Any],
        market: Mapping[str, Any],
        broker: object,
        timestamp: str | datetime | None = None,
    ) -> dict[str, Any]:
        """Start the canonical Grid lifecycle for the selected slice."""

        current = self._require_strategy_slice(plan, family="grid", expected_status="candidate_selected")
        execution_plan = self._execution_plan(plan, current)
        observed_at = self._timestamp(timestamp)
        self._validate_testnet_confirmation(execution_plan, current, confirmation)
        self._validate_authoritative_market(market, current=current, observed_at=observed_at)
        self._validate_lifecycle_preflight(broker, strategy_family="grid", current=current)
        from services.grid_testnet_lifecycle import GridTestnetLifecycle

        lifecycle = GridTestnetLifecycle(self.output_root, broker)
        try:
            state = lifecycle.start(execution_plan, timestamp=observed_at)
        except Exception as exc:  # noqa: BLE001 - persist lifecycle blocker at the Coordinator seam.
            state = {
                "status": "blocked_reconciliation",
                "blocker": f"grid_start_failed:{type(exc).__name__}:{exc}",
                "next_action": "notify_park_and_wait",
            }
        return self._publish_grid_state(current, execution_plan, state, observed_at=observed_at)

    def advance_grid_session(
        self,
        plan: Mapping[str, Any],
        *,
        broker: object,
        fill: Mapping[str, Any] | None = None,
        price: float | None = None,
        market: Mapping[str, Any] | None = None,
        timestamp: str | datetime | None = None,
    ) -> dict[str, Any]:
        """Apply one canonical Grid fill or market event through the Coordinator."""

        current = self._require_strategy_slice(plan, family="grid")
        execution_plan = self._execution_plan(plan, current)
        if current.get("status") not in {
            "grid_running",
            "grid_interrupted",
            "grid_blocked",
        }:
            raise TestnetCoordinatorError("grid_session_not_active")
        observed_at = self._timestamp(timestamp)
        self._validate_authoritative_market(market, current=current, observed_at=observed_at)
        self._validate_lifecycle_preflight(broker, strategy_family="grid", current=current)
        from services.grid_testnet_lifecycle import GridTestnetLifecycle

        lifecycle = GridTestnetLifecycle(self.output_root, broker)
        try:
            if fill is not None:
                state = lifecycle.on_fill(execution_plan, dict(fill), timestamp=observed_at)
            elif price is not None:
                state = lifecycle.on_market_event(
                    execution_plan,
                    price=float(price),
                    timestamp=observed_at,
                )
            else:
                raise TestnetCoordinatorError("grid_event_required")
        except TestnetCoordinatorError:
            raise
        except Exception as exc:  # noqa: BLE001 - retain lifecycle blocker.
            try:
                state = lifecycle.snapshot(execution_plan)
            except Exception:
                state = {
                    "status": "blocked_reconciliation",
                    "blocker": f"grid_event_failed:{type(exc).__name__}:{exc}",
                    "next_action": "notify_park_and_wait",
                }
        return self._publish_grid_state(current, execution_plan, state, observed_at=observed_at)

    def interrupt_grid_session(
        self,
        plan: Mapping[str, Any],
        *,
        broker: object,
        reason: str = "manual_interrupt",
        timestamp: str | datetime | None = None,
    ) -> dict[str, Any]:
        """Cancel pending Grid entries while preserving known positions/protection."""

        current = self._require_strategy_slice(plan, family="grid")
        execution_plan = self._execution_plan(plan, current)
        observed_at = self._timestamp(timestamp)
        from services.grid_testnet_lifecycle import GridTestnetLifecycle

        state = GridTestnetLifecycle(self.output_root, broker).interrupt(
            execution_plan,
            timestamp=observed_at,
            reason=reason,
        )
        return self._publish_grid_state(current, execution_plan, state, observed_at=observed_at)

    def resume_grid_session(
        self,
        plan: Mapping[str, Any],
        *,
        broker: object,
        confirmation: Mapping[str, Any],
        market: Mapping[str, Any],
        timestamp: str | datetime | None = None,
    ) -> dict[str, Any]:
        """Resume an interrupted Grid after fresh host validation."""

        current = self._require_strategy_slice(plan, family="grid")
        execution_plan = self._execution_plan(plan, current)
        if current.get("status") != "grid_interrupted":
            raise TestnetCoordinatorError("grid_session_not_interrupted")
        observed_at = self._timestamp(timestamp)
        self._validate_testnet_confirmation(execution_plan, current, confirmation)
        self._validate_authoritative_market(market, current=current, observed_at=observed_at)
        self._validate_lifecycle_preflight(broker, strategy_family="grid", current=current)
        from services.grid_testnet_lifecycle import GridTestnetLifecycle

        state = GridTestnetLifecycle(self.output_root, broker).resume(
            execution_plan,
            timestamp=observed_at,
        )
        return self._publish_grid_state(current, execution_plan, state, observed_at=observed_at)

    def _publish_grid_state(
        self,
        current: Mapping[str, Any],
        plan: Mapping[str, Any],
        lifecycle: Mapping[str, Any],
        *,
        observed_at: str,
    ) -> dict[str, Any]:
        lifecycle_state = str(lifecycle.get("status") or "blocked")
        if lifecycle_state == "interrupted":
            status = "grid_interrupted"
            enabled = False
        elif lifecycle_state in {"terminal", "stopped"} or lifecycle.get("sealed") is True:
            status = "grid_terminal"
            enabled = False
        elif lifecycle_state.startswith("blocked"):
            status = "grid_blocked"
            enabled = False
        else:
            status = "grid_running"
            enabled = True
        next_action = str(lifecycle.get("next_action") or "await_fill_or_grid_event")
        if lifecycle.get("park_notification_required") or lifecycle_state in {"terminal", "stopped"}:
            next_action = "notify_park_and_wait"
        state = {
            **dict(current),
            "event": "grid_lifecycle_observed",
            "action": "grid_lifecycle",
            "status": status,
            "occurred_at": observed_at,
            "grid_lifecycle_status": lifecycle_state,
            "grid_lifecycle": json.loads(json.dumps(dict(lifecycle), sort_keys=True)),
            "plan_digest": plan.get("plan_digest"),
            "execution_enabled": enabled,
            "execution_ready": enabled,
            "execution_blocker": lifecycle.get("blocker"),
            "next_action": next_action,
            "execution_mutation": lifecycle_state not in {"blocked_protection", "blocked_reconciliation", "blocked_risk"},
            "network_operation_invoked": False,
            "blocker": lifecycle.get("blocker"),
        }
        state["lifecycle"] = state["grid_lifecycle"]
        return self._record(state)

    def _require_strategy_slice(
        self,
        plan: Mapping[str, Any],
        *,
        family: str,
        expected_status: str | None = None,
    ) -> dict[str, Any]:
        current = self._read_current_or_raise()
        if current is None or current.get("status") == "idle":
            raise TestnetCoordinatorError("activation_required")
        if expected_status is not None and current.get("status") != expected_status:
            raise TestnetCoordinatorError("candidate_selection_required")
        if str(current.get("strategy_family") or "").lower() != family:
            raise TestnetCoordinatorError("strategy_family_mismatch")
        if not isinstance(plan, Mapping):
            raise TestnetCoordinatorError("strategy_plan_invalid")
        plan_family = str(plan.get("strategy_type") or plan.get("strategy_family") or "").lower()
        if plan_family and plan_family != family:
            raise TestnetCoordinatorError("strategy_family_mismatch")
        instrument_id = str(
            plan.get("instrument_id")
            or (plan.get("execution_context") or {}).get("instrument_id")
            or ""
        )
        if instrument_id != str(current.get("selected_instrument_id") or ""):
            raise TestnetCoordinatorError(
                "selected_instrument_mismatch",
                {"selected_instrument_id": current.get("selected_instrument_id"), "plan_instrument_id": instrument_id},
            )
        for field in ("strategy_session_id", "strategy_revision_id", "plan_digest"):
            if str(plan.get(field) or "") != str(current.get(field) or ""):
                raise TestnetCoordinatorError("strategy_plan_identity_mismatch", {"field": field})
        execution_slice = current.get("execution_slice")
        if not isinstance(execution_slice, Mapping):
            raise TestnetCoordinatorError("execution_slice_required")
        allocation = execution_slice.get("allocation")
        if not isinstance(allocation, Mapping):
            raise TestnetCoordinatorError("execution_slice_allocation_required")
        if str(allocation.get("asset") or "") != str(current.get("selected_asset") or ""):
            raise TestnetCoordinatorError("execution_slice_asset_mismatch")
        if str(allocation.get("instrument_id") or "") not in {
            "",
            str(current.get("selected_instrument_id") or ""),
        }:
            raise TestnetCoordinatorError("execution_slice_instrument_mismatch")
        supplied_context = plan.get("execution_context")
        if isinstance(supplied_context, Mapping):
            supplied_slice_id = str(supplied_context.get("execution_slice_id") or "")
            if supplied_slice_id and supplied_slice_id != str(execution_slice.get("execution_slice_id") or ""):
                raise TestnetCoordinatorError("execution_slice_identity_mismatch")
        supplied_candidate_id = str(plan.get("candidate_id") or "")
        if supplied_candidate_id and supplied_candidate_id != str(allocation.get("candidate_id") or ""):
            raise TestnetCoordinatorError("execution_slice_candidate_mismatch")
        return current

    def _execution_plan(
        self,
        plan: Mapping[str, Any],
        current: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Bind the caller's strategy plan to the stored subtractive slice.

        The strategy remains the source of direction and lifecycle semantics;
        only quantities are clamped downward to the effective allocation that
        was durably selected before any Broker side effect.
        """

        execution_slice = current.get("execution_slice")
        allocation = execution_slice.get("allocation") if isinstance(execution_slice, Mapping) else None
        if not isinstance(execution_slice, Mapping) or not isinstance(allocation, Mapping):
            raise TestnetCoordinatorError("execution_slice_required")
        result = deepcopy(dict(plan))
        context = dict(result.get("execution_context") or {})
        context.update(
            {
                "execution_slice_id": execution_slice.get("execution_slice_id"),
                "portfolio_allocation_id": allocation.get("allocation_id"),
                "portfolio_selection_id": execution_slice.get("selection_id"),
                "candidate_id": allocation.get("candidate_id"),
                "requested_quantity": allocation.get("requested_quantity"),
                "effective_quantity": allocation.get("effective_quantity"),
                "requested_notional": allocation.get("requested_notional"),
                "effective_notional": allocation.get("effective_notional"),
                "activation_id": current.get("activation_id"),
            }
        )
        result["execution_context"] = context
        result["execution_slice_id"] = execution_slice.get("execution_slice_id")
        result["portfolio_allocation_id"] = allocation.get("allocation_id")
        effective_quantity = self._decimal_value(allocation.get("effective_quantity"))
        effective_notional = self._decimal_optional(allocation.get("effective_notional"))
        family = str(current.get("strategy_family") or "").lower()
        if family == "dca":
            dca = dict(result.get("dca") or {})
            risk = dict(result.get("risk_budget") or {})
            requested_per_addition = self._decimal_optional(dca.get("notional_per_addition"))
            if effective_notional is not None and requested_per_addition is not None:
                entry_levels = dca.get("entry_levels")
                entry_count = len(entry_levels) if isinstance(entry_levels, list) and entry_levels else 1
                entry_prices = [
                    self._decimal_optional(value)
                    for value in (entry_levels if isinstance(entry_levels, list) else ())
                ]
                # DCA's canonical lifecycle rounds quantity to five decimal
                # places.  Reserve one rounding unit at the highest entry
                # price so the cumulative notional cannot exceed the Gate cap.
                rounding_buffer = (
                    max((value for value in entry_prices if value is not None), default=Decimal("0"))
                    * Decimal("0.00001")
                )
                cumulative_cap_per_entry = max(
                    Decimal("0"),
                    effective_notional / Decimal(str(entry_count)) - rounding_buffer,
                )
                if requested_per_addition > cumulative_cap_per_entry:
                    dca["notional_per_addition"] = float(cumulative_cap_per_entry)
                existing_max_notional = self._decimal_optional(risk.get("max_notional"))
                if existing_max_notional is None or existing_max_notional > effective_notional:
                    risk["max_notional"] = float(effective_notional)
                context["portfolio_cumulative_notional_cap"] = str(effective_notional)
            elif effective_quantity is not None and requested_per_addition is not None:
                # If a candidate only declares quantity, use a conservative
                # quantity cap via the lifecycle's price conversion.
                context["effective_quantity_cap"] = str(effective_quantity)
            elif requested_per_addition is not None:
                raise TestnetCoordinatorError("execution_slice_notional_required")
            result["dca"] = dca
            result["risk_budget"] = risk
        elif family == "grid":
            grid = dict(result.get("grid") or {})
            raw_rungs = grid.get("rungs")
            if isinstance(raw_rungs, list) and effective_quantity is not None:
                quantities = [
                    self._decimal_optional(row.get("quantity"))
                    for row in raw_rungs
                    if isinstance(row, Mapping)
                ]
                total = sum((value for value in quantities if value is not None), Decimal("0"))
                total_notional = sum(
                    (
                        value * self._decimal_optional(row.get("price"))
                        for row, value in zip(raw_rungs, quantities)
                        if isinstance(row, Mapping)
                        and value is not None
                        and self._decimal_optional(row.get("price")) is not None
                    ),
                    Decimal("0"),
                )
                factors = [Decimal("1")]
                if total > effective_quantity > 0:
                    factors.append(effective_quantity / total)
                if effective_notional is not None and total_notional > effective_notional > 0:
                    factors.append(effective_notional / total_notional)
                factor = min(factors)
                if factor < 1:
                    scaled: list[dict[str, Any]] = []
                    for row in raw_rungs:
                        item = dict(row)
                        quantity = self._decimal_optional(item.get("quantity"))
                        if quantity is not None:
                            item["quantity"] = float(quantity * factor)
                        scaled.append(item)
                    grid["rungs"] = scaled
            result["grid"] = grid
        return result

    @staticmethod
    def _decimal_optional(value: Any) -> Decimal | None:
        if value in (None, ""):
            return None
        try:
            number = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return None
        return number if number.is_finite() else None

    @classmethod
    def _decimal_value(cls, value: Any) -> Decimal | None:
        return cls._decimal_optional(value)

    def verify_confirmation(
        self,
        plan: Mapping[str, Any],
        confirmation: Mapping[str, Any],
    ) -> None:
        """Verify one fresh, durable Park Testnet confirmation.

        This gate is intentionally owned by the Coordinator rather than only
        by a CLI. Direct lifecycle callers therefore cannot reach a capable
        Broker with a forged, stale, or already-rejected projection.
        """

        if not isinstance(plan, Mapping) or not isinstance(confirmation, Mapping):
            raise TestnetCoordinatorError("testnet_confirmation_invalid")
        if (
            confirmation.get("event") != "confirmed"
            or confirmation.get("execution_authorized") is not True
            or str(confirmation.get("execution_environment") or "").lower() != "testnet"
            or str(confirmation.get("plan_digest") or "")
            != str(plan.get("plan_digest") or "")
            or not str(confirmation.get("confirmation_id") or "").strip()
            or confirmation.get("confirmed_at") in (None, "")
            or not str(confirmation.get("proposal_id") or "").strip()
            or not str(confirmation.get("receipt_digest") or "").strip()
        ):
            raise TestnetCoordinatorError("testnet_confirmation_identity_invalid")

        def epoch(value: Any, field: str) -> float:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                rendered = float(value)
            else:
                text = str(value or "").strip()
                try:
                    rendered = float(text)
                except (TypeError, ValueError):
                    try:
                        rendered = datetime.fromisoformat(
                            text.replace("Z", "+00:00")
                        ).timestamp()
                    except ValueError as exc:
                        raise TestnetCoordinatorError(f"{field}_invalid") from exc
            if not rendered == rendered or rendered in {float("inf"), float("-inf")}:
                raise TestnetCoordinatorError(f"{field}_invalid")
            return rendered

        from services.park_confirmation import ParkConfirmationLedger

        rows = ParkConfirmationLedger(self.output_root, park_user_id="park").rows()
        proposal_id = str(confirmation.get("proposal_id") or "").strip()
        proposal = next(
            (
                row
                for row in rows
                if row.get("event") == "proposal"
                and str(row.get("proposal_id") or "") == proposal_id
            ),
            None,
        )
        decision = next(
            (
                row
                for row in reversed(rows)
                if row.get("event") in {"confirmed", "rejected"}
                and str(row.get("proposal_id") or "") == proposal_id
            ),
            None,
        )
        if proposal is None or decision is None:
            raise TestnetCoordinatorError("testnet_confirmation_durable_missing")
        if decision.get("event") != "confirmed":
            raise TestnetCoordinatorError("testnet_confirmation_not_confirmed")
        try:
            if epoch(proposal.get("expires_at"), "confirmation_expiry") <= time.time():
                raise TestnetCoordinatorError("testnet_confirmation_expired")
            confirmed_at = epoch(decision.get("confirmed_at"), "confirmed_at")
            projected_at = epoch(confirmation.get("confirmed_at"), "confirmed_at")
        except TestnetCoordinatorError:
            raise
        age = time.time() - confirmed_at
        if age < 0 or age > MAX_TESTNET_CONFIRMATION_AGE_SECONDS:
            raise TestnetCoordinatorError("testnet_confirmation_not_fresh")
        if (
            proposal.get("execution_environment") != "testnet"
            or decision.get("execution_environment") != "testnet"
            or proposal.get("plan_digest") != plan.get("plan_digest")
            or decision.get("plan_digest") != plan.get("plan_digest")
            or decision.get("execution_authorized") is not True
            or str(decision.get("receipt_digest") or "")
            != str(confirmation.get("receipt_digest") or "")
            or decision.get("park_user_id") != "park"
            or str(
                confirmation.get("operator_id")
                or confirmation.get("park_user_id")
                or ""
            )
            != "park"
            or abs(confirmed_at - projected_at) > 0.001
        ):
            raise TestnetCoordinatorError("testnet_confirmation_durable_mismatch")

    def _validate_testnet_confirmation(
        self,
        plan: Mapping[str, Any],
        current: Mapping[str, Any],
        confirmation: Mapping[str, Any],
    ) -> None:
        from services.strategy_control_plane import StrategyControlMachineError

        def blocked(reason: str) -> None:
            raise StrategyControlMachineError(
                "testnet_confirmation_blocked",
                {"reason": reason, "plan_digest": plan.get("plan_digest")},
            )

        if current.get("transport_profile") == TESTNET_PROTECTED_TRANSPORT_PROFILE:
            try:
                self.verify_confirmation(plan, confirmation)
            except TestnetCoordinatorError as exc:
                blocked(exc.code)
        else:
            if not isinstance(confirmation, Mapping):
                blocked("confirmation_required")
            if confirmation.get("execution_authorized") is not True:
                blocked("execution_authorized_required")
            if str(confirmation.get("execution_environment") or "").lower() != "testnet":
                blocked("testnet_confirmation_required")
            if str(confirmation.get("plan_digest") or "") != str(plan.get("plan_digest") or ""):
                blocked("plan_digest_mismatch")
        if str(confirmation.get("activation_id") or "") != str(current.get("activation_id") or ""):
            blocked("activation_identity_mismatch")

    @staticmethod
    def _validate_canary_identity(plan: object, current: Mapping[str, Any]) -> None:
        """Require the attended canary plan to be the activated identity."""

        def value(field: str) -> str:
            if isinstance(plan, Mapping):
                return str(plan.get(field) or "")
            return str(getattr(plan, field, "") or "")

        expected = {
            "plan_digest": current.get("plan_digest"),
            "account_fingerprint": current.get("account_fingerprint"),
            "broker_id": current.get("broker_id"),
            "environment": current.get("environment"),
            "profile_id": current.get("transport_profile"),
            "instrument_id": current.get("selected_instrument_id"),
            "runtime_id": current.get("runtime_id"),
            "release_sha": current.get("release_sha"),
            "capability_revision": current.get("capability_revision"),
        }
        for field, expected_value in expected.items():
            if str(expected_value or "") != value(field):
                raise TestnetCoordinatorError(
                    "canary_activation_identity_mismatch",
                    {"field": field},
                )

    def _validate_authoritative_market(
        self,
        market: Mapping[str, Any],
        *,
        current: Mapping[str, Any],
        observed_at: str,
    ) -> None:
        from services.strategy_control_plane import StrategyControlMachineError

        if not isinstance(market, Mapping):
            raise StrategyControlMachineError(
                "testnet_market_not_authoritative",
                {"reason": "market_facts_required"},
            )
        required_identity = (
            "source",
            "cursor",
            "broker_id",
            "environment",
            "instrument_id",
            "asset_index",
            "mapping_revision",
            "universe_revision",
            "connection_epoch",
            "observed_at",
        )
        missing = [field for field in required_identity if field not in market or market.get(field) in (None, "")]
        if missing:
            raise StrategyControlMachineError(
                "testnet_market_not_authoritative",
                {"reason": "market_identity_missing", "fields": missing},
            )
        if (
            market.get("execution_ready") is not True
            or market.get("fresh") is not True
            or market.get("is_synthetic") is True
            or market.get("fallback_policy") not in {"none", None}
            or str(market.get("broker_id") or "").lower() != "hyperliquid"
            or str(market.get("environment") or "").lower() != "testnet"
            or str(market.get("instrument_id") or "")
            != str(
                current.get("selected_instrument_id")
                or current.get("instrument_id")
                or ""
            )
            or str(market.get("source") or "").lower() not in _APPROVED_MARKET_SOURCES
        ):
            raise StrategyControlMachineError(
                "testnet_market_not_authoritative",
                {"reason": "market_identity_mismatch"},
            )
        try:
            market_time = datetime.fromisoformat(str(market["observed_at"]).replace("Z", "+00:00"))
            execution_time = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
        except ValueError as exc:
            raise StrategyControlMachineError(
                "testnet_market_not_authoritative",
                {"reason": "market_timestamp_invalid"},
            ) from exc
        if market_time.tzinfo is None or execution_time.tzinfo is None:
            raise StrategyControlMachineError(
                "testnet_market_not_authoritative",
                {"reason": "market_timestamp_timezone_missing"},
            )
        age = (execution_time.astimezone(timezone.utc) - market_time.astimezone(timezone.utc)).total_seconds()
        if age < 0 or age > 120:
            raise StrategyControlMachineError(
                "testnet_market_not_authoritative",
                {"reason": "market_stale", "age_seconds": age},
            )
        try:
            numeric = {
                field: Decimal(str(market[field]))
                for field in (
                    "bid",
                    "ask",
                    "mid",
                    "mark",
                    "oracle",
                    "impact",
                    "depth_notional",
                    "max_slippage",
                    "max_oracle_deviation_bps",
                )
            }
        except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
            raise StrategyControlMachineError(
                "testnet_market_not_authoritative",
                {"reason": "market_quality_facts_invalid"},
            ) from exc
        if (
            any(not number.is_finite() or number < 0 for number in numeric.values())
            or numeric["bid"] >= numeric["ask"]
            or not numeric["bid"] <= numeric["mid"] <= numeric["ask"]
            or numeric["depth_notional"] <= 0
            or numeric["oracle"] <= 0
            or numeric["mark"] <= 0
            or numeric["max_slippage"] <= 0
            or numeric["max_oracle_deviation_bps"] < 0
            or abs(numeric["impact"] - numeric["mid"]) > numeric["max_slippage"]
            or abs(numeric["mark"] - numeric["oracle"])
            / numeric["oracle"]
            * Decimal("10000")
            > numeric["max_oracle_deviation_bps"]
        ):
            raise StrategyControlMachineError(
                "testnet_market_not_authoritative",
                {"reason": "market_quality_gate_failed"},
            )

    @staticmethod
    def _validate_lifecycle_preflight(
        broker: object,
        *,
        strategy_family: str,
        current: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        from services.strategy_control_plane import StrategyControlMachineError

        try:
            try:
                preflight = broker.preflight(strategy_family=strategy_family)
            except TypeError:
                preflight = broker.preflight()
        except Exception as exc:  # noqa: BLE001 - normalize unknown preflight.
            raise StrategyControlMachineError(
                "testnet_preflight_blocked",
                {"reason": f"preflight_unknown:{type(exc).__name__}"},
            ) from exc
        if not isinstance(preflight, Mapping):
            raise StrategyControlMachineError("testnet_preflight_blocked", dict(preflight) if isinstance(preflight, Mapping) else {})
        if current is not None:
            broker_config = getattr(broker, "broker_config", {})
            config = broker_config if isinstance(broker_config, Mapping) else {}
            actual = {
                "broker_id": preflight.get("broker_id") or config.get("broker_id"),
                "environment": preflight.get("environment") or config.get("environment"),
                "transport_profile": preflight.get("transport_profile") or config.get("transport_profile"),
                "runtime_id": preflight.get("runtime_id") or config.get("runtime_id"),
                "release_sha": preflight.get("release_sha") or config.get("release_sha"),
                "capability_revision": preflight.get("capability_revision"),
                "account_fingerprint": preflight.get("account_fingerprint") or config.get("account_fingerprint"),
            }
            if not actual["account_fingerprint"] and config.get("account_id"):
                actual["account_fingerprint"] = account_fingerprint(config["account_id"])
            for field in (
                "broker_id",
                "environment",
                "transport_profile",
                "runtime_id",
                "release_sha",
                "capability_revision",
                "account_fingerprint",
            ):
                if str(actual.get(field) or "") != str(current.get(field) or ""):
                    raise StrategyControlMachineError(
                        "testnet_preflight_identity_mismatch",
                        {"field": field},
                    )
        gaps = {
            str(value)
            for value in preflight.get("capability_gaps") or ()
            if str(value).strip()
        }
        # The canonical DCA aggregate TP uses a limit leg.  The local
        # standard-broker fixture advertises a market-TP gap because it also
        # serves Grid; that gap is irrelevant to DCA, while every other
        # protection/account/environment failure remains a hard blocker.
        dca_only_market_tp_gap = (
            strategy_family == "dca"
            and gaps
            and gaps == {"protection_order.take_profit_market"}
            and getattr(broker, "protection_adapter", None) is not None
        )
        ready = preflight.get("ready") is True or dca_only_market_tp_gap
        protection_ready = preflight.get("protection_ready") is True or dca_only_market_tp_gap
        if (
            not ready
            or str(preflight.get("environment") or "").lower() != "testnet"
            or preflight.get("real_money_eligible") is not False
            or not protection_ready
            or preflight.get("account_read_ready") is not True
        ):
            raise StrategyControlMachineError("testnet_preflight_blocked", dict(preflight))
        if dca_only_market_tp_gap:
            preflight = {
                **dict(preflight),
                "ready": True,
                "protection_ready": True,
                "strategy_capability_exceptions": ["protection_order.take_profit_market"],
            }
        return dict(preflight)

    def _publish_dca_state(
        self,
        current: Mapping[str, Any],
        plan: Mapping[str, Any],
        lifecycle: Mapping[str, Any],
        *,
        observed_at: str,
    ) -> dict[str, Any]:
        lifecycle_state = str(lifecycle.get("status") or "blocked")
        if lifecycle_state == "interrupted":
            status = "dca_interrupted"
            enabled = False
        elif lifecycle_state in {"terminal", "stopped"} or lifecycle.get("sealed") is True:
            status = "dca_terminal"
            enabled = False
        elif lifecycle_state.startswith("blocked") or lifecycle_state in {"budget_exhausted", "target_triggered"}:
            status = "dca_blocked" if lifecycle_state.startswith("blocked") else "dca_running"
            enabled = not lifecycle_state.startswith("blocked")
        else:
            status = "dca_running"
            enabled = True
        next_action = str(lifecycle.get("next_action") or "await_fill_or_next_entry")
        if lifecycle.get("park_notification_required") or lifecycle_state in {"terminal", "stopped"}:
            next_action = "notify_park_and_wait"
        state = {
            **dict(current),
            "event": "dca_lifecycle_observed",
            "action": "dca_lifecycle",
            "status": status,
            "occurred_at": observed_at,
            "dca_lifecycle_status": lifecycle_state,
            "dca_lifecycle": json.loads(json.dumps(dict(lifecycle), sort_keys=True)),
            "plan_digest": plan.get("plan_digest"),
            "execution_enabled": enabled,
            "execution_ready": enabled,
            "execution_blocker": lifecycle.get("blocker"),
            "next_action": next_action,
            "execution_mutation": lifecycle_state not in {"blocked_protection", "blocked_reconciliation", "blocked_risk"},
            "network_operation_invoked": False,
            "blocker": lifecycle.get("blocker"),
        }
        state["lifecycle"] = state["dca_lifecycle"]
        return self._record(state)

    def _record(self, state: Mapping[str, Any]) -> dict[str, Any]:
        row = dict(state)
        events = self._read_events()
        events.append(row)
        write_json(self.events_path, events)
        write_json(self.current_path, [row])
        self._record_plan_closed(row)
        return dict(row)

    def _record_plan_closed(self, state: Mapping[str, Any]) -> None:
        reason = self._plan_closed_reason(state)
        if reason is None:
            return
        activation_id = str(
            state.get("activation_id") or state.get("previous_activation_id") or ""
        )
        if not activation_id:
            return
        confirmation_path = self.output_root / "dashboard_control_plane" / "confirmations.json"
        rows = load_json(confirmation_path)
        confirmed = next(
            (
                row
                for row in reversed(rows)
                if isinstance(row, Mapping)
                and row.get("status") == "confirmed"
                and str(row.get("activation_id") or "") == activation_id
            ),
            None,
        )
        if not isinstance(confirmed, Mapping):
            return
        if any(
            isinstance(row, Mapping)
            and row.get("event") == "plan_closed"
            and str(row.get("activation_id") or "") == activation_id
            and row.get("reason") == reason
            for row in rows
        ):
            return
        final_state = dict(state)
        closed = {
            "schema_version": "dashboard-confirmation-v1",
            "status": "plan_closed",
            "event": "plan_closed",
            "reason": reason,
            "activation_id": activation_id,
            "plan_digest": confirmed.get("preview_digest"),
            "closed_at": state.get("occurred_at") or self._timestamp(None),
            "coordinator_final_state_digest": _digest(final_state),
            "execution_mutation": False,
            "network_operation_invoked": False,
            "secret_material_present": False,
        }
        write_json(confirmation_path, [*rows, closed])

    @staticmethod
    def _plan_closed_reason(state: Mapping[str, Any]) -> str | None:
        event = str(state.get("event") or "").strip().lower()
        status = str(state.get("status") or "").strip().lower()
        if event == "stop_requested" and status == "stop_requested":
            return "stop"
        if event == "reconcile_stop" and status == "idle":
            return "reconcile_stop"
        if event == "interrupted":
            return "interrupt"
        if status not in {"dca_terminal", "grid_terminal"}:
            return None
        lifecycle = state.get("lifecycle")
        lifecycle = lifecycle if isinstance(lifecycle, Mapping) else {}
        terminal_reason = str(
            lifecycle.get("terminal_reason") or state.get("terminal_reason") or ""
        ).lower()
        if "hard_stop" in terminal_reason:
            return "hard_stop"
        if terminal_reason in {"stop", "strategy_stop_before_entry"}:
            return "stop"
        if "stop_loss" in terminal_reason or terminal_reason.endswith("_sl"):
            return "terminal_sl"
        return "terminal_tp"

    def _replay(self, command_id: str) -> dict[str, Any] | None:
        for row in reversed(self._read_events()):
            if str(row.get("command_id") or "") == command_id:
                return dict(row)
        return None

    def _read_current_or_raise(self) -> dict[str, Any] | None:
        try:
            return self._read_current()
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TestnetCoordinatorError("coordinator_state_corrupt") from exc

    def _read_current(self) -> dict[str, Any] | None:
        rows = load_json(self.current_path)
        if not isinstance(rows, list):
            raise TypeError("coordinator_current_must_be_list")
        if not rows:
            return None
        if not isinstance(rows[-1], dict):
            raise TypeError("coordinator_current_row_must_be_object")
        return dict(rows[-1])

    def _read_events(self) -> list[dict[str, Any]]:
        rows = load_json(self.events_path)
        if not isinstance(rows, list):
            raise TypeError("coordinator_events_must_be_list")
        return [dict(row) for row in rows if isinstance(row, Mapping)]

    def _scheduler_state(self) -> dict[str, Any]:
        """Read the Cloud scheduler receipt used by expansion admission."""

        path = self.output_root / COORDINATOR_ROOT / "scheduler" / COORDINATOR_STATE_FILE
        try:
            rows = load_json(path)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return {}
        return dict(rows[-1]) if rows and isinstance(rows[-1], Mapping) else {}

    @staticmethod
    def _command_id(command_id: str | None, action: str, activation_id: str) -> str:
        value = str(command_id or "").strip()
        if value:
            if len(value) > 200:
                raise TestnetCoordinatorError("command_id_invalid")
            return value
        return f"{action}:{activation_id}"

    def _timestamp(self, now: str | datetime | None) -> str:
        if now is None:
            return _now_iso(self.clock)
        return _now_iso(lambda: now)

    @staticmethod
    def _idle_state() -> dict[str, Any]:
        return {
            "schema_version": COORDINATOR_SCHEMA,
            "event": "status",
            "action": "status",
            "status": "idle",
            "activation_id": None,
            "strategy_family": None,
            "strategy_session_id": None,
            "strategy_revision_id": None,
            "plan_digest": None,
            "account_fingerprint": None,
            "broker_id": None,
            "environment": None,
            "transport_profile": None,
            "instrument_id": None,
            "runtime_id": None,
            "release_sha": None,
            "capability_revision": None,
            "ready": False,
            "execution_enabled": False,
            "execution_ready": False,
            "execution_blocker": "activation_required",
            "next_action": "await_activation",
            "broker_operation_invoked": False,
            "network_operation_invoked": False,
            "execution_mutation": False,
            "secret_material_present": False,
            "blocker": None,
        }

    @classmethod
    def _blocked_state(cls, blocker: str) -> dict[str, Any]:
        return {
            **cls._idle_state(),
            "event": "blocked",
            "action": "status",
            "status": "blocked",
            "ready": False,
            "execution_blocker": blocker,
            "next_action": "notify_park_and_wait",
            "blocker": blocker,
        }


__all__ = [
    "COORDINATOR_SCHEMA",
    "TESTNET_BROKER_ID",
    "TESTNET_ENVIRONMENT",
    "TESTNET_PROTECTED_TRANSPORT_PROFILE",
    "TESTNET_TRANSPORT_PROFILE",
    "TestnetActivation",
    "TestnetAutomationCoordinator",
    "TestnetCoordinatorError",
    "activation_digest",
]
