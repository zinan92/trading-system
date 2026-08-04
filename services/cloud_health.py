"""Layered, read-only health contract for the always-on Cloud Paper runtime."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from services.cycle_decision import CycleDecisionLedger
from services.datafeed_market_repository import DatafeedMarketRepository
from services.dualtrack_clock import cycle_window
from services.dualtrack_config import dualtrack_config
from services.journal_store import load_json, write_json

BJ_TZ = ZoneInfo("Asia/Shanghai")
TICK_MAX_AGE_SECONDS = 180.0
DATA_MAX_AGE_SECONDS = 180.0
BACKUP_MAX_AGE_SECONDS = 36 * 60 * 60

# This is an explicit, fail-closed severity contract.  A newly introduced
# machine code is critical until it is deliberately classified here.
HEALTH_SEVERITY_BY_CODE = {
    # Non-blocking observability / ramp states.
    "daily_self_review_missing_or_incomplete": "warning",
    "backup_missing_or_stale": "warning",
    "runtime_utilization_below_target": "warning",
    "runtime_utilization_insufficient": "insufficient",
    "supervisor_backing_off": "none",
    "supervisor_probing": "none",
    "supervisor_not_required": "none",
    # Healthy evidence.
    "datafeed_fresh": "none",
    "live_tick_fresh": "none",
    "execution_runtime_ready": "none",
    "cycle_decision_ready": "none",
    "cycle_decision_recorded": "none",
    "supervisor_observation_fresh": "none",
    "supervisor_attempt_fresh": "none",
    "supervisor_running": "none",
    "reconciliation_pass": "none",
    "daily_self_review_current": "none",
    "backup_current": "none",
    "scheduler_owner_pass": "none",
    "source_sha_known": "none",
    # Structural/runtime blockers.  Unknown codes also use this severity.
    "datafeed_probe_failed": "critical",
    "datafeed_latest_timestamp_missing": "critical",
    "datafeed_stale": "critical",
    "live_tick_heartbeat_missing": "critical",
    "live_tick_stale": "critical",
    "execution_runtime_missing": "critical",
    "execution_runtime_unresolved": "critical",
    "cycle_decision_missing": "critical",
    "cycle_decision_invalid": "critical",
    "cycle_decision_stalled_after_plan_activation": "critical",
    "reconciliation_not_applicable_without_cycle_snapshot": "critical",
    "reconciliation_not_pass": "critical",
    "scheduler_owner_mismatch": "critical",
    "source_sha_unknown": "critical",
    "supervisor_observation_missing": "critical",
    "supervisor_observation_stale": "critical",
    "supervisor_attempt_missing": "critical",
    "supervisor_attempt_stale": "critical",
    "supervisor_structural_blocker": "critical",
    "supervisor_episode_exhausted": "critical",
    "supervisor_episode_invalid": "critical",
    "supervisor_read_model_unavailable": "critical",
}


def health_severity(code: str) -> str:
    """Return the explicit severity for a code; unknown is fail-closed."""

    return HEALTH_SEVERITY_BY_CODE.get(str(code), "critical")


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _latest(path: Path) -> dict[str, Any]:
    try:
        rows = load_json(path)
    except (OSError, ValueError):
        return {}
    return dict(rows[-1]) if rows and isinstance(rows[-1], dict) else {}


def _check(
    stage: str,
    status: str,
    *,
    code: str,
    summary: str,
    next_action: str,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "stage": stage,
        "status": status,
        "code": code,
        "severity": health_severity(code),
        "summary": summary,
        "next_action": next_action,
        "evidence": evidence or {},
    }


class CloudPaperHealth:
    """Project current evidence without changing strategy or execution state."""

    def __init__(
        self,
        *,
        output_root: Path,
        backup_root: Path,
        owner_id: str | None = None,
        deployed_sha: str = "",
        now: Callable[[], datetime] | None = None,
        latest_market_provider: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.backup_root = Path(backup_root)
        self.owner_id = str(
            owner_id
            if owner_id is not None
            else os.getenv("GRIDMIND_SCHEDULER_OWNER_ID") or ""
        )
        self.deployed_sha = str(deployed_sha)
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.latest_market_provider = latest_market_provider or self._latest_market

    def run(self, *, persist: bool = True) -> dict[str, Any]:
        observed = self.now().astimezone(timezone.utc).replace(microsecond=0)
        checks = {
            "datafeed": self._datafeed(observed),
            "live_tick": self._live_tick(observed),
            "execution": self._execution(),
            **(
                {"supervisor": self._supervisor(observed)}
                if self._supervisor_mode()
                else {"cycle_decision": self._cycle_decision(observed)}
            ),
            "reconciliation": self._reconciliation(),
            "daily_self_review": self._daily_review(observed),
            "backup": self._backup(observed),
            "scheduler_ownership": self._ownership(),
            "source": self._source(),
        }
        statuses = {row["status"] for row in checks.values()}
        if "blocked" in statuses:
            status = "blocked"
        elif statuses & {"degraded", "unknown"}:
            status = "degraded"
        else:
            status = "healthy"
        incidents = [
            {
                "stage": row["stage"],
                "status": row["status"],
                "code": row["code"],
                "severity": row["severity"],
                "summary": row["summary"],
                "next_action": row["next_action"],
            }
            for row in checks.values()
            if row["status"] != "ready" or row["severity"] != "none"
        ]
        payload = {
            "schema_version": "cloud-paper-health-v1",
            "runtime_mode": "cloud",
            "paper_only": True,
            "checked_at": observed.isoformat(),
            "status": status,
            "severity": self._overall_severity(checks),
            "critical_incidents": [
                row for row in incidents if row["severity"] == "critical"
            ],
            "warning_incidents": [
                row for row in incidents if row["severity"] == "warning"
            ],
            "insufficient_conditions": [
                row for row in incidents if row["severity"] == "insufficient"
            ],
            "dashboard_reachable_is_not_system_health": True,
            "checks": checks,
            "incidents": incidents,
            "control_actions_executed": 0,
            "secrets_included": False,
        }
        if persist:
            write_json(
                self.output_root / "cloud" / "health" / "current.json",
                [payload],
            )
        return payload

    def _supervisor_mode(self) -> bool:
        convergence = dualtrack_config().get("convergence")
        return (
            isinstance(convergence, dict)
            and convergence.get("mode") == "paper_supervisor"
        )

    def _overall_severity(self, checks: dict[str, dict[str, Any]]) -> str:
        severities = {
            row["severity"]
            for row in checks.values()
            if row.get("status") != "ready" or row.get("severity") != "none"
        }
        if "critical" in severities:
            return "critical"
        if "warning" in severities:
            return "warning"
        if "insufficient" in severities:
            return "insufficient"
        return "none"

    def _supervisor(self, now: datetime) -> dict[str, Any]:
        """Health for the exclusive Supervisor convergence mode."""

        runtime = _latest(
            self.output_root / "dualtrack" / "strategy_control" / "runtime.json"
        )
        # Runtime may legitimately lag a cycle boundary while Supervisor is
        # responsible for recovering the current active plan. Health must
        # inspect the wall-clock cycle rather than hiding that plan behind a
        # stale runtime cycle.
        cycle_id = cycle_window(now).cycle_id
        plans_path = (
            self.output_root
            / "dualtrack"
            / "strategy_control"
            / "plans"
            / f"{cycle_id}.json"
        )
        active_plans = [
            row for row in load_json(plans_path)
            if isinstance(row, dict) and row.get("status") == "active"
        ]
        active_plan = active_plans[-1] if active_plans else {}
        running = (
            runtime.get("cycle_id") == cycle_id
            and runtime.get("actual_state") == "running"
        )
        if not active_plan:
            return _check(
                "supervisor",
                "ready",
                code="supervisor_not_required",
                summary="No active plan requires Supervisor convergence.",
                next_action="No action.",
                evidence={"cycle_id": cycle_id, "active_plan": False},
            )
        try:
            from services.paper_supervisor_read_model import (
                build_paper_supervisor_read_model,
            )

            model = build_paper_supervisor_read_model(
                self.output_root,
                cycle_id=cycle_id,
                as_of=now,
                persist_utilization_index=True,
            )
        except Exception as exc:  # noqa: BLE001 - health fails closed.
            return _check(
                "supervisor",
                "blocked",
                code="supervisor_read_model_unavailable",
                summary=f"Supervisor read-model is unavailable: {type(exc).__name__}.",
                next_action="Restore Supervisor observations before creating any control request.",
                evidence={"cycle_id": cycle_id},
            )
        current = model.get("current_cycle") if isinstance(model.get("current_cycle"), dict) else {}
        utilization = model.get("utilization") if isinstance(model.get("utilization"), dict) else {}
        summary_evidence = {
            "cycle_id": cycle_id,
            "current_cycle_summary": {
                "attempt_count": current.get("attempt_count"),
                "start_intent_count": current.get("start_intent_count"),
                "last_attempt": current.get("last_attempt"),
            },
            "runtime_utilization": utilization,
        }
        last_observed = _parse_ts(current.get("last_observed_at"))
        last_attempt = current.get("last_attempt") if isinstance(current.get("last_attempt"), dict) else {}
        attempted_at = _parse_ts(last_attempt.get("observed_at"))
        observation_age = (now - last_observed).total_seconds() if last_observed else None
        attempt_age = (now - attempted_at).total_seconds() if attempted_at else None
        episode = current.get("episode") if isinstance(current.get("episode"), dict) else {}
        blocker = episode.get("blocker") if isinstance(episode.get("blocker"), dict) else {}
        mode = str(episode.get("mode") or "")
        blocker_code = str(blocker.get("machine_code") or "")
        if mode == "blocked_structural":
            return _check(
                "supervisor",
                "blocked",
                code="supervisor_structural_blocker",
                summary="Supervisor has an active structural blocker and requires attention.",
                next_action="Inspect the immutable blocker and authoritative runtime; do not retry blindly.",
                evidence={
                    **summary_evidence,
                    "runtime_running": running,
                    "blocker_machine_code": blocker_code or None,
                    "blocker": blocker,
                },
            )
        if episode.get("alert_required") is True or str(blocker.get("machine_code") or "") in {
            "dangerous_start_attempt_cap_reached",
            "clean_refusal_observation_cap_reached",
            "episode_short_budget_exhausted",
        }:
            return _check(
                "supervisor",
                "blocked",
                code="supervisor_episode_exhausted",
                summary="Supervisor retry episode is exhausted and requires attention.",
                next_action="Review the immutable Supervisor attempt history; do not retry blindly.",
                evidence={**summary_evidence, "blocker": blocker, "episode": episode},
            )
        if last_observed is None or observation_age is None or observation_age > 300:
            return _check(
                "supervisor",
                "blocked",
                code="supervisor_observation_missing" if last_observed is None else "supervisor_observation_stale",
                summary="Supervisor has not produced a fresh observation within 300 seconds.",
                next_action="Inspect the Supervisor/live-tick scheduler and preserve control state.",
                evidence={**summary_evidence, "age_seconds": observation_age},
            )
        if attempted_at is None or attempt_age is None or attempt_age > 300:
            return _check(
                "supervisor",
                "blocked",
                code="supervisor_attempt_missing" if attempted_at is None else "supervisor_attempt_stale",
                summary="Supervisor has not attempted convergence within 300 seconds.",
                next_action="Inspect the Supervisor episode and current active plan; do not create a manual start.",
                evidence={**summary_evidence, "age_seconds": attempt_age},
            )
        if mode not in {"ready", "backing_off", "probing"}:
            return _check(
                "supervisor",
                "blocked",
                code="supervisor_episode_invalid",
                summary="Supervisor episode mode is missing or invalid.",
                next_action="Preserve the episode and restore a valid Supervisor read model.",
                evidence={**summary_evidence, "mode": mode or None},
            )
        if mode == "backing_off":
            return _check(
                "supervisor", "ready", code="supervisor_backing_off",
                summary="Supervisor is in an explicit transient backoff.",
                next_action="Wait for the recorded next probe; no manual retry.",
                evidence={**summary_evidence, "episode": episode},
            )
        if mode == "probing":
            return _check(
                "supervisor", "ready", code="supervisor_probing",
                summary="Supervisor is probing a transient condition.",
                next_action="Wait for the recorded probe; no manual retry.",
                evidence={**summary_evidence, "episode": episode},
            )
        window = (utilization.get("windows") or {}).get("24h") if isinstance(utilization.get("windows"), dict) else {}
        if isinstance(window, dict) and window.get("evidence_status") == "insufficient":
            return _check(
                "supervisor", "ready", code="runtime_utilization_insufficient",
                summary="The first complete 24-hour runtime utilization window has not formed.",
                next_action="Continue collecting Supervisor running evidence.",
                evidence={**summary_evidence, "utilization": window},
            )
        if isinstance(window, dict) and float(window.get("conservative_percentage") or 0) < 85:
            return _check(
                "supervisor", "degraded", code="runtime_utilization_below_target",
                summary="Conservative Supervisor runtime utilization is below 85%.",
                next_action="Review the running-evidence gaps; this is warning-only and does not fail dead-man.",
                evidence={**summary_evidence, "utilization": window},
            )
        return _check(
            "supervisor", "ready", code=("supervisor_running" if running else "supervisor_observation_fresh"),
            summary=(
                "Supervisor runtime is running and convergence evidence is fresh."
                if running
                else "Supervisor observation and attempt cadence are fresh."
            ),
            next_action="No action.",
            evidence={**summary_evidence, "observation_age_seconds": observation_age, "attempt_age_seconds": attempt_age},
        )

    def _datafeed(self, now: datetime) -> dict[str, Any]:
        try:
            row = self.latest_market_provider()
        except Exception as exc:  # noqa: BLE001 - health must classify failures.
            return _check(
                "datafeed",
                "blocked",
                code="datafeed_probe_failed",
                summary=f"Trusted GOLD datafeed probe failed: {type(exc).__name__}",
                next_action="Check loopback datafeed health and upstream venue access; do not start a strategy.",
            )
        observed = _parse_ts(row.get("timestamp"))
        provider = str(row.get("provider") or "")
        if observed is None:
            return _check(
                "datafeed",
                "blocked",
                code="datafeed_latest_timestamp_missing",
                summary="Trusted GOLD latest bar is missing or has no valid timestamp.",
                next_action="Restore the execution-venue datafeed and rerun Cloud health.",
                evidence={"provider": provider or None},
            )
        age = (now - observed).total_seconds()
        ready = -120 <= age <= DATA_MAX_AGE_SECONDS
        return _check(
            "datafeed",
            "ready" if ready else "blocked",
            code="datafeed_fresh" if ready else "datafeed_stale",
            summary="Trusted GOLD datafeed is fresh." if ready else "Trusted GOLD datafeed is stale.",
            next_action="No action." if ready else "Repair the datafeed/upstream route; new starts must remain frozen.",
            evidence={
                "provider": provider or None,
                "latest_timestamp": observed.isoformat(),
                "age_seconds": round(age, 2),
                "max_age_seconds": DATA_MAX_AGE_SECONDS,
            },
        )

    def _cycle_decision(self, now: datetime) -> dict[str, Any]:
        cycle_id = cycle_window(now).cycle_id
        runtime = _latest(
            self.output_root / "dualtrack" / "strategy_control" / "runtime.json"
        )
        health = CycleDecisionLedger(self.output_root).health(cycle_id)
        ready = health.get("status") == "ready"
        if not ready:
            plans_path = (
                self.output_root
                / "dualtrack"
                / "strategy_control"
                / "plans"
                / f"{cycle_id}.json"
            )
            active_plans = [
                row
                for row in load_json(plans_path)
                if isinstance(row, dict) and row.get("status") == "active"
            ]
            active_plan = active_plans[-1] if active_plans else {}
            locked_at = _parse_ts(active_plan.get("locked_at"))
            decision_config = dualtrack_config().get("cycle_decision")
            decision_config = (
                decision_config if isinstance(decision_config, dict) else {}
            )
            deadline = float(
                decision_config.get("terminal_deadline_seconds") or 300
            )
            age = (now - locked_at).total_seconds() if locked_at else None
            current_runtime_running = (
                runtime.get("cycle_id") == cycle_id
                and runtime.get("desired_state") == "running"
                and runtime.get("actual_state") == "running"
            )
            if (
                active_plan
                and age is not None
                and age > deadline
                and not current_runtime_running
            ):
                health = {
                    **health,
                    "code": "cycle_decision_stalled_after_plan_activation",
                    "strategy_plan_id": active_plan.get("strategy_plan_id"),
                    "plan_locked_at": locked_at.isoformat(),
                    "plan_age_seconds": round(age, 2),
                    "terminal_deadline_seconds": deadline,
                    "runtime_cycle_id": runtime.get("cycle_id"),
                    "runtime_actual_state": runtime.get("actual_state"),
                }
        return _check(
            "cycle_decision",
            "ready" if ready else "blocked",
            code=str(health.get("code") or "cycle_decision_invalid"),
            summary=(
                "Current Paper cycle has exactly one durable strategy decision."
                if ready
                else (
                    "Current Paper cycle has an active plan but no terminal "
                    "execution decision within its deadline."
                    if health.get("code")
                    == "cycle_decision_stalled_after_plan_activation"
                    else "Current Paper cycle is missing one valid strategy decision."
                )
            ),
            next_action=(
                "No action."
                if ready
                else "Inspect the live-tick cycle-decision phase; do not replay control actions manually."
            ),
            evidence=health,
        )

    def _live_tick(self, now: datetime) -> dict[str, Any]:
        candidates: list[dict[str, Any]] = []
        runner = self.output_root / "dualtrack" / "runner"
        for path in runner.glob("*.json") if runner.exists() else []:
            try:
                rows = load_json(path)
            except (OSError, ValueError):
                rows = []
            for row in rows:
                if isinstance(row, dict) and row.get("event") == "live_tick_heartbeat":
                    candidates.append(row)
        candidates.sort(key=lambda row: str(row.get("ts") or ""))
        latest = candidates[-1] if candidates else {}
        observed = _parse_ts(latest.get("ts"))
        if observed is None:
            return _check(
                "live_tick",
                "blocked",
                code="live_tick_heartbeat_missing",
                summary="No valid Cloud Paper live-tick heartbeat is available.",
                next_action="Inspect gridmind-live-tick.service before enabling or starting a strategy.",
            )
        age = (now - observed).total_seconds()
        ready = -120 <= age <= TICK_MAX_AGE_SECONDS
        return _check(
            "live_tick",
            "ready" if ready else "blocked",
            code="live_tick_fresh" if ready else "live_tick_stale",
            summary="Cloud Paper live-tick is fresh." if ready else "Cloud Paper live-tick is stale.",
            next_action="No action." if ready else "Inspect the live-tick timer and latest failure receipt; do not replay controls.",
            evidence={
                "latest_timestamp": observed.isoformat(),
                "age_seconds": round(age, 2),
                "max_age_seconds": TICK_MAX_AGE_SECONDS,
            },
        )

    def _execution(self) -> dict[str, Any]:
        runtime = _latest(
            self.output_root / "dualtrack" / "strategy_control" / "runtime.json"
        )
        if not runtime:
            return _check(
                "execution",
                "unknown",
                code="execution_runtime_missing",
                summary="Authoritative Paper execution runtime is unavailable.",
                next_action="Restore the runtime artifact before any strategy control action.",
            )
        known = runtime.get("accepted_order_count_known") is not False
        unresolved = runtime.get("previous_runtime_unresolved") is True
        ready = known and not unresolved
        return _check(
            "execution",
            "ready" if ready else "blocked",
            code="execution_runtime_ready" if ready else "execution_runtime_unresolved",
            summary="Paper execution runtime is internally resolved." if ready else "Paper execution runtime contains unresolved order state.",
            next_action="No action." if ready else "Reconcile the current and previous cycle; do not create new exposure.",
            evidence={
                "cycle_id": runtime.get("cycle_id"),
                "actual_state": runtime.get("actual_state"),
                "accepted_order_count": runtime.get("accepted_order_count") if known else None,
                "previous_runtime_unresolved": unresolved,
            },
        )

    def _reconciliation(self) -> dict[str, Any]:
        runtime = _latest(
            self.output_root / "dualtrack" / "strategy_control" / "runtime.json"
        )
        cycle_id = str(runtime.get("cycle_id") or "")
        snapshot = _latest(
            self.output_root
            / "dualtrack"
            / "nautilus_authoritative"
            / "snapshots"
            / f"{cycle_id}.json"
        ) if cycle_id else {}
        reconciliation = (
            snapshot.get("reconciliation")
            if isinstance(snapshot.get("reconciliation"), dict)
            else {}
        )
        status = str(reconciliation.get("status") or "")
        if not snapshot and str(runtime.get("actual_state") or "") == "stopped":
            return _check(
                "reconciliation",
                "unknown",
                code="reconciliation_not_applicable_without_cycle_snapshot",
                summary="No current execution snapshot exists while Paper is stopped.",
                next_action="Require a passing reconciliation before the next scheduler cutover or strategy start.",
                evidence={"cycle_id": cycle_id or None, "actual_state": "stopped"},
            )
        ready = status in {"ok", "pass"}
        return _check(
            "reconciliation",
            "ready" if ready else "blocked",
            code="reconciliation_pass" if ready else "reconciliation_not_pass",
            summary="Current execution reconciliation passes." if ready else "Current execution reconciliation is missing or not passing.",
            next_action="No action." if ready else "Resolve reconciliation drift before starting or migrating Paper execution.",
            evidence={"cycle_id": cycle_id or None, "status": status or "missing"},
        )

    def _daily_review(self, now: datetime) -> dict[str, Any]:
        review = _latest(
            self.output_root / "dualtrack" / "daily_self_reviews" / "current.json"
        )
        expected_date = (now.astimezone(BJ_TZ).date() - timedelta(days=1)).isoformat()
        actual_date = str(review.get("report_date") or "")
        observed_hash = str(review.get("review_hash") or "")
        candidate = dict(review)
        candidate.pop("review_hash", None)
        integrity = (
            "pass"
            if observed_hash and observed_hash == _hash_json(candidate)
            else "invalid"
        )
        ready = (
            actual_date == expected_date
            and review.get("status") == "complete"
            and integrity == "pass"
        )
        return _check(
            "daily_self_review",
            "ready" if ready else "degraded",
            code="daily_self_review_current" if ready else "daily_self_review_missing_or_incomplete",
            summary="Prior Beijing-day self-review is complete." if ready else "Prior Beijing-day self-review is missing or incomplete.",
            next_action="No action." if ready else "Run the terminal report, then regenerate the evidence-backed daily self-review.",
            evidence={
                "expected_report_date": expected_date,
                "report_date": actual_date or None,
                "review_status": review.get("status") or "missing",
                "review_hash": review.get("review_hash"),
                "integrity": integrity if review else "missing",
            },
        )

    def _backup(self, now: datetime) -> dict[str, Any]:
        receipt = _latest(self.backup_root / "current.json")
        created = _parse_ts(receipt.get("created_at"))
        age = (now - created).total_seconds() if created else None
        ready = (
            receipt.get("status") == "pass"
            and created is not None
            and age is not None
            and -120 <= age <= BACKUP_MAX_AGE_SECONDS
        )
        return _check(
            "backup",
            "ready" if ready else "degraded",
            code="backup_current" if ready else "backup_missing_or_stale",
            summary="Verified Paper backup is current." if ready else "Verified Paper backup is missing or stale.",
            next_action="No action." if ready else "Run the verified backup job and inspect its manifest receipt.",
            evidence={
                "backup_id": receipt.get("backup_id"),
                "created_at": created.isoformat() if created else None,
                "age_seconds": round(age, 2) if age is not None else None,
                "max_age_seconds": BACKUP_MAX_AGE_SECONDS,
                "manifest_hash": receipt.get("manifest_hash"),
            },
        )

    def _ownership(self) -> dict[str, Any]:
        current = _latest(
            self.output_root / "cloud" / "scheduler_ownership" / "current.json"
        )
        ready = (
            current.get("status") == "active"
            and bool(self.owner_id)
            and current.get("active_owner_id") == self.owner_id
        )
        return _check(
            "scheduler_ownership",
            "ready" if ready else "blocked",
            code="scheduler_owner_pass" if ready else "scheduler_owner_mismatch",
            summary="This Cloud host is the single active Paper scheduler owner." if ready else "This host is not the verified active Paper scheduler owner.",
            next_action="No action." if ready else "Keep all tick timers disabled until the explicit single-owner cutover completes.",
            evidence={
                "configured_owner_id": self.owner_id or None,
                "active_owner_id": current.get("active_owner_id"),
                "owner_status": current.get("status") or "missing",
                "epoch": current.get("epoch"),
            },
        )

    def _source(self) -> dict[str, Any]:
        sha = self.deployed_sha.strip().lower()
        if not sha:
            preflight = _latest(
                self.output_root / "cloud" / "preflight" / "current.json"
            )
            source = next(
                (
                    row
                    for row in preflight.get("checks") or []
                    if isinstance(row, dict) and row.get("id") == "source_attestation"
                ),
                {},
            )
            sha = str(source.get("source_sha") or "").strip().lower()
        ready = len(sha) == 40 and all(char in "0123456789abcdef" for char in sha)
        return _check(
            "source",
            "ready" if ready else "unknown",
            code="source_sha_known" if ready else "source_sha_unknown",
            summary="Deployed source SHA is explicit." if ready else "Deployed source SHA is unavailable.",
            next_action="No action." if ready else "Redeploy from an immutable clean SHA and rerun preflight.",
            evidence={"deployed_sha": sha or None},
        )

    @staticmethod
    def _latest_market() -> dict[str, Any]:
        return DatafeedMarketRepository().load_latest_bar("GOLD", "1m")


def _hash_json(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
