"""Layered, read-only health contract for the always-on Cloud Paper runtime."""

from __future__ import annotations

import os
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from services.datafeed_market_repository import DatafeedMarketRepository
from services.cycle_decision import CycleDecisionLedger
from services.journal_store import load_json, write_json


BJ_TZ = ZoneInfo("Asia/Shanghai")
TICK_MAX_AGE_SECONDS = 180.0
DATA_MAX_AGE_SECONDS = 180.0
BACKUP_MAX_AGE_SECONDS = 36 * 60 * 60


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
            "cycle_decision": self._cycle_decision(),
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
                "summary": row["summary"],
                "next_action": row["next_action"],
            }
            for row in checks.values()
            if row["status"] != "ready"
        ]
        payload = {
            "schema_version": "cloud-paper-health-v1",
            "runtime_mode": "cloud",
            "paper_only": True,
            "checked_at": observed.isoformat(),
            "status": status,
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

    def _cycle_decision(self) -> dict[str, Any]:
        runtime = _latest(
            self.output_root / "dualtrack" / "strategy_control" / "runtime.json"
        )
        cycle_id = str(runtime.get("cycle_id") or "")
        if not cycle_id:
            return _check(
                "cycle_decision",
                "blocked",
                code="cycle_decision_missing",
                summary="Current Paper cycle has no durable strategy decision.",
                next_action="Restore the current runtime identity and let one complete live tick record its decision.",
            )
        health = CycleDecisionLedger(self.output_root).health(cycle_id)
        ready = health.get("status") == "ready"
        return _check(
            "cycle_decision",
            "ready" if ready else "blocked",
            code=str(health.get("code") or "cycle_decision_invalid"),
            summary=(
                "Current Paper cycle has exactly one durable strategy decision."
                if ready
                else "Current Paper cycle is missing one valid strategy decision."
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
