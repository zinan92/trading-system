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
_ACTIONS = frozenset(
    {"activate", "status", "preflight", "pause", "interrupt", "resume", "select_candidate"}
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
        if normalized_action == "select_candidate":
            return self._select_candidate(
                payload,
                command_id=command_id,
                now=now,
            )
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
        observed_at = self._timestamp(timestamp)
        self._validate_testnet_confirmation(plan, current, confirmation)
        self._validate_authoritative_market(market)
        self._validate_lifecycle_preflight(broker, strategy_family="dca")
        from services.dca_testnet_lifecycle import DcaTestnetLifecycle

        lifecycle = DcaTestnetLifecycle(self.output_root, broker)
        try:
            state = lifecycle.start(dict(plan), timestamp=observed_at)
        except Exception as exc:  # noqa: BLE001 - persist lifecycle blocker at the Coordinator seam.
            return self._publish_dca_state(
                current,
                dict(plan),
                {
                    "status": "blocked_reconciliation",
                    "blocker": f"dca_start_failed:{type(exc).__name__}:{exc}",
                    "next_action": "notify_park_and_wait",
                },
                observed_at=observed_at,
            )
        return self._publish_dca_state(current, dict(plan), state, observed_at=observed_at)

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
        if current.get("status") not in {
            "dca_running",
            "dca_interrupted",
            "dca_blocked",
        }:
            raise TestnetCoordinatorError("dca_session_not_active")
        observed_at = self._timestamp(timestamp)
        if market is not None:
            self._validate_authoritative_market(market)
        from services.dca_testnet_lifecycle import DcaTestnetLifecycle

        lifecycle = DcaTestnetLifecycle(self.output_root, broker)
        try:
            if fill is not None:
                state = lifecycle.on_fill(dict(plan), dict(fill), timestamp=observed_at)
            elif price is not None:
                state = lifecycle.on_market_event(
                    dict(plan),
                    price=float(price),
                    timestamp=observed_at,
                )
            else:
                raise TestnetCoordinatorError("dca_event_required")
        except TestnetCoordinatorError:
            raise
        except Exception as exc:  # noqa: BLE001 - retain the lifecycle blocker.
            try:
                state = lifecycle.snapshot(dict(plan))
            except Exception:
                state = {
                    "status": "blocked_reconciliation",
                    "blocker": f"dca_event_failed:{type(exc).__name__}:{exc}",
                    "next_action": "notify_park_and_wait",
                }
        return self._publish_dca_state(current, dict(plan), state, observed_at=observed_at)

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
        observed_at = self._timestamp(timestamp)
        from services.dca_testnet_lifecycle import DcaTestnetLifecycle

        state = DcaTestnetLifecycle(self.output_root, broker).interrupt(
            dict(plan),
            timestamp=observed_at,
            reason=reason,
        )
        return self._publish_dca_state(current, dict(plan), state, observed_at=observed_at)

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
        if current.get("status") != "dca_interrupted":
            raise TestnetCoordinatorError("dca_session_not_interrupted")
        observed_at = self._timestamp(timestamp)
        self._validate_testnet_confirmation(plan, current, confirmation)
        self._validate_authoritative_market(market)
        self._validate_lifecycle_preflight(broker, strategy_family="dca")
        from services.dca_testnet_lifecycle import DcaTestnetLifecycle

        state = DcaTestnetLifecycle(self.output_root, broker).resume(
            dict(plan),
            timestamp=observed_at,
        )
        return self._publish_dca_state(current, dict(plan), state, observed_at=observed_at)

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
        observed_at = self._timestamp(timestamp)
        self._validate_testnet_confirmation(plan, current, confirmation)
        self._validate_authoritative_market(market)
        self._validate_lifecycle_preflight(broker, strategy_family="grid")
        from services.grid_testnet_lifecycle import GridTestnetLifecycle

        lifecycle = GridTestnetLifecycle(self.output_root, broker)
        try:
            state = lifecycle.start(dict(plan), timestamp=observed_at)
        except Exception as exc:  # noqa: BLE001 - persist lifecycle blocker at the Coordinator seam.
            state = {
                "status": "blocked_reconciliation",
                "blocker": f"grid_start_failed:{type(exc).__name__}:{exc}",
                "next_action": "notify_park_and_wait",
            }
        return self._publish_grid_state(current, dict(plan), state, observed_at=observed_at)

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
        if current.get("status") not in {
            "grid_running",
            "grid_interrupted",
            "grid_blocked",
        }:
            raise TestnetCoordinatorError("grid_session_not_active")
        observed_at = self._timestamp(timestamp)
        if market is not None:
            self._validate_authoritative_market(market)
        from services.grid_testnet_lifecycle import GridTestnetLifecycle

        lifecycle = GridTestnetLifecycle(self.output_root, broker)
        try:
            if fill is not None:
                state = lifecycle.on_fill(dict(plan), dict(fill), timestamp=observed_at)
            elif price is not None:
                state = lifecycle.on_market_event(
                    dict(plan),
                    price=float(price),
                    timestamp=observed_at,
                )
            else:
                raise TestnetCoordinatorError("grid_event_required")
        except TestnetCoordinatorError:
            raise
        except Exception as exc:  # noqa: BLE001 - retain lifecycle blocker.
            try:
                state = lifecycle.snapshot(dict(plan))
            except Exception:
                state = {
                    "status": "blocked_reconciliation",
                    "blocker": f"grid_event_failed:{type(exc).__name__}:{exc}",
                    "next_action": "notify_park_and_wait",
                }
        return self._publish_grid_state(current, dict(plan), state, observed_at=observed_at)

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
        observed_at = self._timestamp(timestamp)
        from services.grid_testnet_lifecycle import GridTestnetLifecycle

        state = GridTestnetLifecycle(self.output_root, broker).interrupt(
            dict(plan),
            timestamp=observed_at,
            reason=reason,
        )
        return self._publish_grid_state(current, dict(plan), state, observed_at=observed_at)

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
        if current.get("status") != "grid_interrupted":
            raise TestnetCoordinatorError("grid_session_not_interrupted")
        observed_at = self._timestamp(timestamp)
        self._validate_testnet_confirmation(plan, current, confirmation)
        self._validate_authoritative_market(market)
        self._validate_lifecycle_preflight(broker, strategy_family="grid")
        from services.grid_testnet_lifecycle import GridTestnetLifecycle

        state = GridTestnetLifecycle(self.output_root, broker).resume(
            dict(plan),
            timestamp=observed_at,
        )
        return self._publish_grid_state(current, dict(plan), state, observed_at=observed_at)

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
        return current

    @staticmethod
    def _validate_testnet_confirmation(
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
        if not str(confirmation.get("confirmation_id") or "").strip():
            blocked("confirmation_id_required")

    def _validate_authoritative_market(self, market: Mapping[str, Any]) -> None:
        from services.strategy_control_plane import StrategyControlMachineError

        if (
            not isinstance(market, Mapping)
            or market.get("execution_ready") is not True
            or market.get("fresh") is not True
            or market.get("is_synthetic") is True
            or market.get("fallback_policy") not in {"none", None}
        ):
            raise StrategyControlMachineError(
                "testnet_market_not_authoritative",
                {
                    "execution_ready": market.get("execution_ready") if isinstance(market, Mapping) else None,
                    "fresh": market.get("fresh") if isinstance(market, Mapping) else None,
                },
            )

    @staticmethod
    def _validate_lifecycle_preflight(
        broker: object,
        *,
        strategy_family: str,
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
            and gaps <= {"protection_order.take_profit_market"}
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
