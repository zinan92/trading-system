"""Evidence-only 24-hour Cloud Paper soak receipt."""

from __future__ import annotations

import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from services.cloud_health import CloudPaperHealth
from services.journal_store import load_json, write_json
from services.scheduler_ownership import SchedulerOwnershipStore


SOAK_HOURS = 24
EXPECTED_TICKS = 1_440
MINIMUM_TICK_COVERAGE = 0.95
MONITORED_UNITS = (
    "gridmind-live-tick.timer",
    "gridmind-daily-24h.timer",
    "gridmind-daily-self-review.timer",
    "gridmind-backup.timer",
    "gridmind-next-cycle-plan.timer",
    "gridmind-deadman-ping.timer",
    "gridmind-ai-provider-readiness.timer",
    "gridmind-dashboard.service",
    "gridmind-datafeed.service",
    "gridmind-access-gateway.service",
    "gridmind-cloudflared.service",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _parse(value: Any) -> datetime | None:
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
    rows = load_json(path)
    return dict(rows[-1]) if rows and isinstance(rows[-1], dict) else {}


class CloudPaperSoak:
    """Start and evaluate a source-bound soak without any control action."""

    def __init__(
        self,
        *,
        output_root: Path,
        backup_root: Path,
        deployed_sha: str,
        owner_id: str,
        now: Callable[[], datetime] | None = None,
        service_probe: Callable[[], dict[str, dict[str, Any]]] | None = None,
        health_provider: Callable[[datetime], dict[str, Any]] | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.backup_root = Path(backup_root)
        self.deployed_sha = str(deployed_sha or "").strip()
        self.owner_id = str(owner_id or "").strip()
        self.now = now or _utcnow
        self.service_probe = service_probe or self._systemd_snapshot
        self.health_provider = health_provider or self._cloud_health
        self.path = self.output_root / "cloud" / "soak" / "current.json"

    def start(self, *, mac_schedulers_disabled: bool) -> dict[str, Any]:
        if len(self.deployed_sha) != 40:
            raise ValueError("cloud_soak_source_sha_invalid")
        if mac_schedulers_disabled is not True:
            raise ValueError("cloud_soak_requires_mac_scheduler_disabled_evidence")
        ownership = SchedulerOwnershipStore(self.output_root).current()
        if (
            ownership.get("status") != "active"
            or ownership.get("active_owner_id") != self.owner_id
        ):
            raise ValueError("cloud_soak_scheduler_owner_not_active")
        observed = self.now().astimezone(timezone.utc).replace(microsecond=0)
        payload = {
            "schema_version": "cloud-paper-soak-v1",
            "soak_id": f"soak-{observed.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}",
            "status": "running",
            "started_at": observed.isoformat(),
            "ends_at": (observed + timedelta(hours=SOAK_HOURS)).isoformat(),
            "last_checked_at": observed.isoformat(),
            "deployed_sha": self.deployed_sha,
            "paper_only": True,
            "control_actions_executed": 0,
            "mac_schedulers_disabled_at_start": True,
            "scheduler_owner": {
                "active_owner_id": ownership.get("active_owner_id"),
                "epoch": ownership.get("epoch"),
                "dual_owner_allowed": False,
            },
            "requirements": {
                "duration_hours": SOAK_HOURS,
                "expected_tick_count": EXPECTED_TICKS,
                "minimum_tick_coverage_pct": MINIMUM_TICK_COVERAGE * 100,
                "required_artifacts": [
                    "terminal_daily_report",
                    "daily_self_review",
                    "verified_backup",
                    "delivered_deadman_ping",
                ],
                "required_services": list(MONITORED_UNITS),
            },
            "baseline": {
                "heartbeat_count": len(self._heartbeats()),
                "backup_id": _latest(self.backup_root / "current.json").get("backup_id"),
                "services": self.service_probe(),
            },
            "metrics": self._metrics(observed, observed),
            "next_action": "Keep Cloud as the sole scheduler owner and check this receipt after 24 hours.",
        }
        self._write(payload)
        return payload

    def check(self) -> dict[str, Any]:
        current = _latest(self.path)
        if not current:
            raise ValueError("cloud_soak_not_started")
        started = _parse(current.get("started_at"))
        ends = _parse(current.get("ends_at"))
        if started is None or ends is None:
            raise ValueError("cloud_soak_window_invalid")
        observed = self.now().astimezone(timezone.utc).replace(microsecond=0)
        metrics = self._metrics(started, observed)
        ownership = SchedulerOwnershipStore(self.output_root).current()
        owner_ready = (
            ownership.get("status") == "active"
            and ownership.get("active_owner_id")
            == (current.get("scheduler_owner") or {}).get("active_owner_id")
            and ownership.get("epoch") == (current.get("scheduler_owner") or {}).get("epoch")
        )
        hard_failures = []
        if not owner_ready:
            hard_failures.append("scheduler_ownership_changed")
        if metrics["tick_failure_count"]:
            hard_failures.append("live_tick_failure_recorded")
        if metrics["inactive_required_units"]:
            hard_failures.append("required_cloud_unit_inactive")
        complete_checks = {
            "tick_coverage": metrics["tick_coverage_pct"]
            >= MINIMUM_TICK_COVERAGE * 100,
            "terminal_daily_report": metrics["terminal_daily_report_after_start"],
            "daily_self_review": metrics["daily_self_review_after_start"],
            "verified_backup": metrics["verified_backup_after_start"],
            "delivered_deadman_ping": metrics["delivered_deadman_ping_after_start"],
            "datafeed_fresh": metrics["datafeed_fresh"],
            "scheduler_unique": owner_ready,
        }
        if hard_failures:
            status = "blocked"
        elif observed < ends:
            status = "running"
        else:
            status = "pass" if all(complete_checks.values()) else "incomplete"
        payload = {
            **current,
            "status": status,
            "last_checked_at": observed.isoformat(),
            "metrics": metrics,
            "completion_checks": complete_checks,
            "blockers": hard_failures,
            "control_actions_executed": 0,
            "next_action": (
                "Soak passed; retain Cloud as the sole Paper scheduler owner."
                if status == "pass"
                else "Repair the recorded blocker without enabling the Mac scheduler."
                if status == "blocked"
                else "Keep the soak running and recheck after its 24-hour end."
            ),
        }
        self._write(payload)
        return payload

    def _metrics(self, started: datetime, observed: datetime) -> dict[str, Any]:
        beats = [
            ts
            for ts in self._heartbeats()
            if started <= ts <= observed
        ]
        failures = [
            row
            for row in load_json(
                self.output_root
                / "dualtrack"
                / "strategy_control"
                / "live_tick_failure.json"
            )
            if isinstance(row, dict)
            and (ts := _parse(row.get("recorded_at") or row.get("ts"))) is not None
            and started <= ts <= observed
        ]
        services = self.service_probe()
        inactive = [
            unit
            for unit in MONITORED_UNITS
            if str((services.get(unit) or {}).get("active") or "") != "active"
        ]
        health = self.health_provider(observed)
        report_after = self._has_timestamped_row(
            self.output_root / "dualtrack" / "daily_reports",
            started,
            ("generated_at", "created_at", "checked_at"),
        )
        review = _latest(
            self.output_root / "dualtrack" / "daily_self_reviews" / "current.json"
        )
        review_after = (
            review.get("status") == "complete"
            and self._after(review.get("generated_at"), started)
        )
        backup = _latest(self.backup_root / "current.json")
        backup_after = (
            backup.get("status") == "pass"
            and self._after(backup.get("created_at"), started)
        )
        deadman = _latest(self.output_root / "deadman_ping" / "current.json")
        deadman_after = (
            bool((deadman.get("ping") or {}).get("delivered"))
            and self._after(deadman.get("checked_at"), started)
        )
        baseline_services = (
            (_latest(self.path).get("baseline") or {}).get("services")
            if self.path.exists()
            else {}
        ) or {}
        restart_delta = {
            unit: max(
                0,
                int((services.get(unit) or {}).get("restarts") or 0)
                - int((baseline_services.get(unit) or {}).get("restarts") or 0),
            )
            for unit in MONITORED_UNITS
        }
        return {
            "successful_tick_count": len(beats),
            "tick_coverage_pct": round(len(beats) / EXPECTED_TICKS * 100, 4),
            "latest_tick_at": beats[-1].isoformat() if beats else None,
            "tick_failure_count": len(failures),
            "terminal_daily_report_after_start": report_after,
            "daily_self_review_after_start": review_after,
            "verified_backup_after_start": backup_after,
            "delivered_deadman_ping_after_start": deadman_after,
            "datafeed_fresh": (
                (health.get("checks") or {}).get("datafeed", {}).get("status")
                == "ready"
            ),
            "inactive_required_units": inactive,
            "service_restart_delta": restart_delta,
            "service_snapshot": services,
        }

    def _heartbeats(self) -> list[datetime]:
        rows: list[datetime] = []
        runner = self.output_root / "dualtrack" / "runner"
        for path in runner.glob("*.json") if runner.exists() else []:
            for row in load_json(path):
                if not isinstance(row, dict) or row.get("event") != "live_tick_heartbeat":
                    continue
                if (ts := _parse(row.get("ts"))) is not None:
                    rows.append(ts)
        return sorted(set(rows))

    @staticmethod
    def _after(value: Any, started: datetime) -> bool:
        observed = _parse(value)
        return observed is not None and observed >= started

    def _has_timestamped_row(
        self,
        directory: Path,
        started: datetime,
        keys: tuple[str, ...],
    ) -> bool:
        for path in directory.glob("*.json") if directory.exists() else []:
            for row in load_json(path):
                if not isinstance(row, dict):
                    continue
                if any(self._after(row.get(key), started) for key in keys):
                    return True
        return False

    @staticmethod
    def _systemd_snapshot() -> dict[str, dict[str, Any]]:
        snapshot: dict[str, dict[str, Any]] = {}
        for unit in MONITORED_UNITS:
            result = subprocess.run(
                [
                    "systemctl",
                    "show",
                    unit,
                    "-p",
                    "ActiveState",
                    "-p",
                    "NRestarts",
                    "-p",
                    "Result",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            values = {}
            for line in result.stdout.splitlines():
                if "=" in line:
                    key, value = line.split("=", 1)
                    values[key] = value
            snapshot[unit] = {
                "active": values.get("ActiveState") or "unknown",
                "restarts": int(values.get("NRestarts") or 0),
                "result": values.get("Result") or "unknown",
            }
        return snapshot

    def _cloud_health(self, observed: datetime) -> dict[str, Any]:
        return CloudPaperHealth(
            output_root=self.output_root,
            backup_root=self.backup_root,
            deployed_sha=self.deployed_sha,
            now=lambda: observed,
        ).run(persist=False)

    def _write(self, payload: dict[str, Any]) -> None:
        write_json(self.path, [payload])
        write_json(
            self.path.parent / f"{payload['soak_id']}.json",
            [payload],
        )
