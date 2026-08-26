"""Read-only Testnet Automation Coordinator boundary.

The coordinator is the composition-root contract for the later Testnet
execution slices.  This first slice only binds an immutable activation
identity and records operator intent.  It deliberately has no Broker
dependency and cannot submit, cancel, protect, or flatten an order.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from services.journal_store import load_json, write_json


COORDINATOR_SCHEMA = "testnet-automation-coordinator-v1"
COORDINATOR_ROOT = "testnet_automation"
COORDINATOR_STATE_FILE = "current.json"
COORDINATOR_EVENTS_FILE = "events.json"
TESTNET_BROKER_ID = "hyperliquid"
TESTNET_ENVIRONMENT = "testnet"
TESTNET_TRANSPORT_PROFILE = "hyperliquid-testnet-default"
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}", re.IGNORECASE)
_RELEASE_RE = re.compile(r"[0-9a-f]{40}", re.IGNORECASE)
_ACTIONS = frozenset({"activate", "status", "preflight", "pause", "interrupt", "resume"})
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
        if transport_profile != TESTNET_TRANSPORT_PROFILE:
            raise TestnetCoordinatorError("testnet_profile_required")
        release_sha = _required_text(value.get("release_sha"), "release_sha").lower()
        if _RELEASE_RE.fullmatch(release_sha) is None:
            raise TestnetCoordinatorError("release_sha_invalid")
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
            capability_revision=_required_text(
                value.get("capability_revision"), "capability_revision"
            ),
        )

    def to_mapping(self) -> dict[str, str]:
        return {field: str(getattr(self, field)) for field in _ACTIVATION_FIELDS}

    @property
    def activation_id(self) -> str:
        return activation_digest(self.to_mapping())


class TestnetAutomationCoordinator:
    """One durable, execution-disabled Testnet activation boundary."""

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

    def preflight(self) -> dict[str, Any]:
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
        return {
            **current,
            "event": "preflight",
            "ready": current.get("blocker") is None,
            "execution_ready": False,
            "broker_operation_invoked": False,
            "network_operation_invoked": False,
            "next_action": "await_execution_capability",
        }

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
        if current is not None:
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

    def _record(self, state: Mapping[str, Any]) -> dict[str, Any]:
        row = dict(state)
        events = self._read_events()
        events.append(row)
        write_json(self.events_path, events)
        write_json(self.current_path, [row])
        return dict(row)

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
    "TESTNET_TRANSPORT_PROFILE",
    "TestnetActivation",
    "TestnetAutomationCoordinator",
    "TestnetCoordinatorError",
    "activation_digest",
]
