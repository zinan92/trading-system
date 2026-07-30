"""Fail-closed convergence loop for the Paper-only strategy runtime.

The Supervisor is intentionally a policy layer *above* the existing control
plane.  It never fabricates market data, writes orders itself, or relaxes a
gate.  Its only authority is deciding whether a fresh, independently-gated
attempt is due and retaining the evidence needed to recover safely after a
process crash.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping
from uuid import uuid4

from services.paper_supervisor_classifier import STRUCTURAL, TRANSIENT, classify_blocker
from services.paper_supervisor_store import (
    PaperSupervisorLeaseHeld,
    PaperSupervisorStore,
    PaperSupervisorStoreError,
)


SHORT_RETRY_DELAYS_SECONDS = (60, 120, 300, 600, 1200)
LONG_PROBE_DELAY_SECONDS = 30 * 60
MAX_START_ATTEMPTS_PER_CYCLE = 12


class SupervisorControlError(RuntimeError):
    """Typed outcome from a pre-existing control-plane action.

    ``code`` must be a stable machine code already emitted by the control
    plane.  Human prose deliberately has no role in the classifier.
    """

    def __init__(self, code: str, *, evidence: Mapping[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = str(code)
        self.evidence = dict(evidence or {})


@dataclass(frozen=True)
class SupervisorOperations:
    """The narrow control seam used by :class:`PaperSupervisor`.

    The concrete integration supplies fresh public-control requests.  Each
    callable either returns its documented mapping or raises
    :class:`SupervisorControlError`; raw exceptions are intentionally turned
    into structural ``unknown_blocker`` outcomes.
    """

    observe: Callable[[str], Mapping[str, Any]]
    fresh_preview: Callable[[str, Mapping[str, Any]], Mapping[str, Any]]
    prepare_start: Callable[[str, Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]]
    start: Callable[[str, str, Mapping[str, Any]], Mapping[str, Any]]


class PaperSupervisor:
    """Converge one Paper cycle toward a proven running strategy."""

    def __init__(
        self,
        store: PaperSupervisorStore,
        operations: SupervisorOperations,
        *,
        max_start_attempts: int = MAX_START_ATTEMPTS_PER_CYCLE,
    ) -> None:
        self.store = store
        self.operations = operations
        self.max_start_attempts = max_start_attempts

    def tick(self, cycle_id: str, *, now: str | datetime | None = None) -> dict[str, Any]:
        """Run one non-blocking convergence pass for ``cycle_id``.

        The caller invokes this only after a complete natural execution tick.
        A held lease is not an error: another completed tick is already doing
        the only permitted mutation work.
        """

        timestamp = _as_utc(now)
        try:
            with self.store.lease(cycle_id=cycle_id, now=timestamp) as lease:
                return self._tick_locked(cycle_id, timestamp, lease)
        except PaperSupervisorLeaseHeld:
            return {
                "cycle_id": cycle_id,
                "status": "lease_held",
                "next_action": "observe_on_next_natural_tick",
            }
        except PaperSupervisorStoreError as exc:
            # A broken local evidence store must never result in a blind
            # start.  There is no safe automatic repair for it.
            return {
                "cycle_id": cycle_id,
                "status": "blocked_structural",
                "machine_code": str(exc),
                "next_action": "alert_human_and_reconcile_store",
            }

    def _tick_locked(
        self,
        cycle_id: str,
        now: datetime,
        lease: Mapping[str, Any],
    ) -> dict[str, Any]:
        state = self.store.read_state(cycle_id)
        unfinished = self.store.unfinished_start_intent(cycle_id)
        if unfinished is not None:
            return self._block(
                cycle_id,
                state,
                now,
                machine_code="control_outcome_unknown",
                human_reason="A persisted start intent has no durable result; runtime and control audit reconciliation is required.",
                evidence={"start_intent": unfinished},
            )

        try:
            observed = self._observe(cycle_id)
        except SupervisorControlError as exc:
            return self._apply_classification(
                cycle_id,
                state,
                now,
                classify_blocker(control_code=exc.code, evidence=exc.evidence),
                phase="observation",
            )
        health = self._health_blocker(observed)
        healthy_running = health is None and _is_proven_running(observed)
        self.store.append_observation(
            cycle_id,
            observed_at=now,
            healthy_running=healthy_running,
            fields={
                "runtime_state": _runtime_state(observed),
                "plan_identity": _plan_identity(observed),
                "order_count": _order_count(observed),
                "reconciliation": _mapping(observed.get("reconciliation")),
                "tick": _mapping(observed.get("tick")),
                "lease_owner_id": lease.get("owner_id"),
            },
        )
        state = self.store.write_state(cycle_id, {**state, "last_observed_at": _iso(now)})

        if health is not None:
            return self._apply_classification(cycle_id, state, now, health, phase="observation")
        if healthy_running:
            status = "adopted_existing" if state.get("status") != "executed" else "executed"
            saved = self.store.write_state(
                cycle_id,
                {
                    **state,
                    "status": status,
                    "structural_blocker": None,
                    "episode": _clear_episode(state.get("episode")),
                },
            )
            self.store.append_event(cycle_id, "converged", now=now, fields={"result": status})
            return {"cycle_id": cycle_id, "status": status, "state": saved}
        if state.get("status") == "blocked_structural":
            return {
                "cycle_id": cycle_id,
                "status": "blocked_structural",
                "machine_code": _mapping(state.get("structural_blocker")).get("machine_code"),
                "next_action": "await_human_resolution",
            }
        if not _due(state, now):
            return {
                "cycle_id": cycle_id,
                "status": "waiting_retry_window",
                "next_attempt_at": _mapping(state.get("episode")).get("next_attempt_at"),
            }
        if int(state.get("start_call_count") or 0) >= self.max_start_attempts:
            return self._block(
                cycle_id,
                state,
                now,
                machine_code="cycle_start_attempt_cap_reached",
                human_reason="The cycle has reached its durable start-attempt safety cap.",
                evidence={"start_call_count": state.get("start_call_count"), "cap": self.max_start_attempts},
            )
        return self._attempt(cycle_id, state, now, observed)

    def _attempt(
        self,
        cycle_id: str,
        state: Mapping[str, Any],
        now: datetime,
        observed: Mapping[str, Any],
    ) -> dict[str, Any]:
        attempt_id = f"supervisor-attempt-{uuid4().hex}"
        episode_id = str(_mapping(state.get("episode")).get("episode_id") or f"episode-{uuid4().hex}")
        self.store.append_event(
            cycle_id,
            "attempt_started",
            now=now,
            attempt_id=attempt_id,
            episode_id=episode_id,
            fields={"mode": _attempt_mode(state), "result": "in_progress"},
        )
        failure_state: Mapping[str, Any] = state
        start_intent_written = False
        prepared_start_id: str | None = None
        preview_id: str | None = None
        try:
            preview = dict(self.operations.fresh_preview(cycle_id, observed))
            preview_id = _required_id(preview, "preview_id")
            prepared = dict(self.operations.prepare_start(cycle_id, preview, observed))
            prepared_start_id = _required_id(prepared, "prepared_start_id")
            if prepared_start_id in self.store.spent_prepared_start_ids(cycle_id):
                return self._block(
                    cycle_id,
                    state,
                    now,
                    machine_code="prepared_start_identity_changed",
                    human_reason="A prepared-start identifier was already consumed and cannot be reused.",
                    evidence={"prepared_start_id": prepared_start_id},
                    attempt_id=attempt_id,
                    episode_id=episode_id,
                )
            # A successful prepare proves that this episode's prior temporary
            # blocker cleared.  Reset it before attempting start; a start
            # refusal starts a fresh episode count.
            cleared = self.store.write_state(
                cycle_id,
                {
                    **dict(state),
                    "episode": _clear_episode(state.get("episode")),
                    "last_attempt_id": attempt_id,
                },
            )
            failure_state = cleared
            self.store.append_event(
                cycle_id,
                "prepared",
                now=now,
                attempt_id=attempt_id,
                episode_id=episode_id,
                fields={"preview_id": preview_id, "prepared_start_id": prepared_start_id, "result": "prepared"},
            )
            # This MUST be durable before the public start call.  Recovery
            # treats an absent result as unknown, never as permission to retry.
            self.store.append_event(
                cycle_id,
                "start_intent",
                now=now,
                attempt_id=attempt_id,
                episode_id=episode_id,
                fields={
                    "preview_id": preview_id,
                    "prepared_start_id": prepared_start_id,
                    "plan_identity": _plan_identity(observed),
                    "expected_order_count": _order_count(prepared),
                },
            )
            start_intent_written = True
            # The cap counts invocations, not accepted outcomes.  Persist the
            # count before crossing the non-idempotent control boundary.
            failure_state = self.store.write_state(
                cycle_id,
                {
                    **cleared,
                    "start_call_count": int(cleared.get("start_call_count") or 0) + 1,
                },
            )
            result = dict(self.operations.start(cycle_id, prepared_start_id, prepared))
            self.store.append_event(
                cycle_id,
                "start_result",
                now=now,
                attempt_id=attempt_id,
                episode_id=episode_id,
                fields={"preview_id": preview_id, "prepared_start_id": prepared_start_id, "result": "accepted"},
            )
            saved = self.store.write_state(
                cycle_id,
                {
                    **cleared,
                    "attempt_count": int(cleared.get("attempt_count") or 0) + 1,
                    "start_call_count": int(failure_state.get("start_call_count") or 0),
                    "last_attempt_id": attempt_id,
                    "status": "converging",
                },
            )
            return {"cycle_id": cycle_id, "status": "start_accepted", "attempt_id": attempt_id, "result": result, "state": saved}
        except SupervisorControlError as exc:
            if start_intent_written:
                # A typed control response proves this start invocation did
                # return.  Close its durable intent before deciding whether a
                # separate fresh attempt may ever be scheduled.
                self.store.append_event(
                    cycle_id,
                    "start_result",
                    now=now,
                    attempt_id=attempt_id,
                    episode_id=episode_id,
                    fields={
                        "preview_id": preview_id,
                        "prepared_start_id": prepared_start_id,
                        "result": "rejected",
                        "control_code": exc.code,
                    },
                )
            classification = classify_blocker(control_code=exc.code, evidence=exc.evidence)
            return self._apply_classification(
                cycle_id,
                failure_state,
                now,
                classification,
                phase="control",
                attempt_id=attempt_id,
                episode_id=episode_id,
            )
        except Exception:
            # Never classify exception prose.  A response loss or programming
            # error cannot justify another mutation attempt.
            classification = classify_blocker(evidence={"control_outcome": "unknown"})
            return self._apply_classification(
                cycle_id,
                failure_state,
                now,
                classification,
                phase="control",
                attempt_id=attempt_id,
                episode_id=episode_id,
            )

    def _observe(self, cycle_id: str) -> Mapping[str, Any]:
        try:
            observed = self.operations.observe(cycle_id)
        except SupervisorControlError:
            raise
        except Exception as exc:
            raise SupervisorControlError("", evidence={"control_outcome": "unknown"}) from exc
        if not isinstance(observed, Mapping):
            raise SupervisorControlError("", evidence={"control_outcome": "unknown"})
        return observed

    def _health_blocker(self, observed: Mapping[str, Any]) -> dict[str, Any] | None:
        typed = _mapping(observed.get("blocker_evidence"))
        if typed:
            return classify_blocker(evidence=typed)
        if _mapping(observed.get("reconciliation")).get("status") == "drift":
            return classify_blocker(evidence={"reconciliation": "drift"})
        return None

    def _apply_classification(
        self,
        cycle_id: str,
        state: Mapping[str, Any],
        now: datetime,
        classification: Mapping[str, Any],
        *,
        phase: str,
        attempt_id: str | None = None,
        episode_id: str | None = None,
    ) -> dict[str, Any]:
        if classification.get("classification") == TRANSIENT:
            return self._transient(cycle_id, state, now, classification, phase, attempt_id, episode_id)
        return self._block(
            cycle_id,
            state,
            now,
            machine_code=str(classification.get("machine_code") or "unknown_blocker"),
            human_reason="A structural safety gate requires human reconciliation before another start attempt.",
            evidence={"classification": dict(classification), "phase": phase},
            attempt_id=attempt_id,
            episode_id=episode_id,
        )

    def _transient(
        self,
        cycle_id: str,
        state: Mapping[str, Any],
        now: datetime,
        classification: Mapping[str, Any],
        phase: str,
        attempt_id: str | None,
        episode_id: str | None,
    ) -> dict[str, Any]:
        old_episode = _mapping(state.get("episode"))
        failures = int(old_episode.get("consecutive_transient_failures") or 0) + 1
        exhausted = failures >= len(SHORT_RETRY_DELAYS_SECONDS)
        delay = LONG_PROBE_DELAY_SECONDS if exhausted else SHORT_RETRY_DELAYS_SECONDS[failures - 1]
        next_attempt_at = _iso(now + timedelta(seconds=delay))
        episode = {
            "episode_id": episode_id or old_episode.get("episode_id") or f"episode-{uuid4().hex}",
            "consecutive_transient_failures": failures,
            "exhausted_at": _iso(now) if exhausted else None,
            "next_attempt_at": next_attempt_at,
        }
        saved = self.store.write_state(
            cycle_id,
            {
                **dict(state),
                "status": "long_interval_probe" if exhausted else "retry_scheduled",
                "attempt_count": int(state.get("attempt_count") or 0) + 1,
                "last_attempt_id": attempt_id,
                "episode": episode,
                "alert_requested_at": _iso(now) if exhausted else state.get("alert_requested_at"),
            },
        )
        self.store.append_event(
            cycle_id,
            "transient_failure",
            now=now,
            attempt_id=attempt_id,
            episode_id=str(episode["episode_id"]),
            fields={
                "mode": "long_interval_probe" if exhausted else "short_retry",
                "result": "retry_scheduled",
                "machine_code": classification.get("machine_code"),
                "classifier_version": classification.get("classifier_version"),
                "next_action": "alert_and_probe" if exhausted else "retry_fresh_preview_prepare_start",
                "next_attempt_at": next_attempt_at,
                "phase": phase,
            },
        )
        return {
            "cycle_id": cycle_id,
            "status": saved["status"],
            "machine_code": classification.get("machine_code"),
            "next_attempt_at": next_attempt_at,
            "alert_required": exhausted,
            "state": saved,
        }

    def _block(
        self,
        cycle_id: str,
        state: Mapping[str, Any],
        now: datetime,
        *,
        machine_code: str,
        human_reason: str,
        evidence: Mapping[str, Any],
        attempt_id: str | None = None,
        episode_id: str | None = None,
    ) -> dict[str, Any]:
        blocker = {
            "machine_code": machine_code,
            "human_reason": human_reason,
            "next_action": "human_reconcile_authoritative_runtime_and_control_audit",
            "evidence": dict(evidence),
            "blocked_at": _iso(now),
        }
        saved = self.store.write_state(
            cycle_id,
            {
                **dict(state),
                "status": "blocked_structural",
                "structural_blocker": blocker,
                "last_attempt_id": attempt_id or state.get("last_attempt_id"),
                "alert_requested_at": _iso(now),
            },
        )
        self.store.append_event(
            cycle_id,
            "structural_blocked",
            now=now,
            attempt_id=attempt_id,
            episode_id=episode_id,
            fields={
                "result": "blocked",
                "machine_code": machine_code,
                "human_reason": human_reason,
                "next_action": blocker["next_action"],
            },
        )
        return {
            "cycle_id": cycle_id,
            "status": "blocked_structural",
            "machine_code": machine_code,
            "alert_required": True,
            "state": saved,
        }


def _as_utc(value: str | datetime | None) -> datetime:
    if isinstance(value, datetime):
        current = value
    elif value:
        current = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    else:
        current = datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).replace(microsecond=0)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _required_id(value: Mapping[str, Any], key: str) -> str:
    item = str(value.get(key) or "")
    if not item:
        raise SupervisorControlError("", evidence={"control_outcome": "unknown"})
    return item


def _clear_episode(value: Any) -> dict[str, Any]:
    return {
        "episode_id": None,
        "consecutive_transient_failures": 0,
        "exhausted_at": None,
        "next_attempt_at": None,
    }


def _due(state: Mapping[str, Any], now: datetime) -> bool:
    next_attempt_at = _mapping(state.get("episode")).get("next_attempt_at")
    if not next_attempt_at:
        return True
    try:
        return now >= _as_utc(str(next_attempt_at))
    except ValueError:
        return False


def _attempt_mode(state: Mapping[str, Any]) -> str:
    episode = _mapping(state.get("episode"))
    return "long_interval_probe" if episode.get("exhausted_at") else "short_retry"


def _runtime_state(observed: Mapping[str, Any]) -> str:
    runtime = _mapping(observed.get("runtime"))
    return str(runtime.get("actual_state") or runtime.get("desired_state") or "unknown")


def _plan_identity(observed: Mapping[str, Any]) -> dict[str, Any]:
    plan = _mapping(observed.get("plan"))
    return {key: plan.get(key) for key in ("strategy_plan_id", "version", "cycle_id")}


def _order_count(value: Mapping[str, Any]) -> int | None:
    orders = value.get("orders")
    if isinstance(orders, list):
        return len(orders)
    expected = value.get("expected_order_count")
    return int(expected) if isinstance(expected, int) else None


def _is_proven_running(observed: Mapping[str, Any]) -> bool:
    runtime = _mapping(observed.get("runtime"))
    plan = _mapping(observed.get("plan"))
    reconciliation = _mapping(observed.get("reconciliation"))
    return (
        str(runtime.get("actual_state") or runtime.get("desired_state") or "") == "running"
        and bool(plan.get("strategy_plan_id"))
        and _order_count(observed) is not None
        and reconciliation.get("status") in {"pass", "ok"}
    )
