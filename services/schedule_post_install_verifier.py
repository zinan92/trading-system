from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json
from services.runner_status import RunnerStatusStore
from services.schedule_installer import ScheduleInstaller
from services.schedule_status import ScheduleStatus
from services.schedule_profiles import is_focus_profile


class SchedulePostInstallVerifier:
    def __init__(
        self,
        output_root: Path | None = None,
        launch_agents_dir: Path | None = None,
        command_runner: Callable[[list[str]], subprocess.CompletedProcess] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        config = load_pipeline_config()
        self.output_root = output_root or ROOT / config.get("output_root", "outputs")
        self.launch_agents_dir = launch_agents_dir
        self.command_runner = command_runner
        self.now = now or (lambda: datetime.now(timezone.utc))

    def run(self, run_date: str) -> dict:
        schedule_status = ScheduleStatus(self.output_root, self.launch_agents_dir, self.command_runner).run(run_date)
        install_receipt = self._latest(self.output_root / "schedules" / "install_current.json")
        rollback_plan = ScheduleInstaller(self.output_root, self.launch_agents_dir, self.command_runner).rollback_plan(run_date)
        runner = RunnerStatusStore(self.output_root).current()
        checks = [
            self._schedule_check(schedule_status),
            self._install_check(install_receipt),
            self._rollback_check(install_receipt, rollback_plan),
            self._runner_check(runner, str(schedule_status.get("profile") or "")),
        ]
        status = self._rollup(checks)
        payload = {
            "run_date": run_date,
            "checked_at": self.now().replace(microsecond=0).isoformat(),
            "status": status,
            "checks": checks,
            "schedule_status": schedule_status,
            "install_receipt": self._compact_install(install_receipt),
            "rollback_plan": self._compact_rollback_plan(rollback_plan),
            "runner": runner,
            "safety": {
                "writes_launch_agents": False,
                "runs_launchctl_modification": False,
                "runs_launchctl_print": True,
                "opens_broker_clients": False,
                "submits_orders": False,
            },
        }
        write_json(self.output_root / "schedules" / "post_install_verify_current.json", [payload])
        write_json(self.output_root / "schedules" / f"post_install_verify_{run_date}.json", [payload])
        return payload

    def _latest(self, path: Path) -> dict:
        rows = load_json(path)
        return rows[-1] if rows else {}

    def _schedule_check(self, schedule_status: dict) -> dict:
        required = int(schedule_status.get("required_count") or 0)
        active = int(schedule_status.get("active_current_count") or 0)
        if schedule_status.get("status") == "active" and required and active == required:
            return self._check("schedule_current_active", "pass", "all generated launchd jobs are installed, current, and loaded", {
                "active_current_count": active,
                "required_count": required,
            })
        return self._check("schedule_current_active", "fail", "launchd jobs are not all current-version active", {
            "status": schedule_status.get("status"),
            "active_current_count": active,
            "required_count": required,
            "mismatched_jobs": schedule_status.get("mismatched_jobs", []),
            "unloaded_jobs": schedule_status.get("unloaded_jobs", []),
            "missing_installed_jobs": schedule_status.get("missing_installed_jobs", []),
        })

    def _install_check(self, receipt: dict) -> dict:
        if not receipt:
            return self._check("install_receipt", "fail", "missing install receipt", {
                "path": str(self.output_root / "schedules" / "install_current.json"),
            })
        if receipt.get("status") in {"active", "installed", "noop"}:
            return self._check("install_receipt", "pass", "latest install receipt is successful or no-op", self._compact_install(receipt))
        return self._check("install_receipt", "fail", "latest install receipt is not successful", self._compact_install(receipt))

    def _rollback_check(self, install_receipt: dict, rollback_plan: dict) -> dict:
        backup_count = int(install_receipt.get("backup_count") or 0) if install_receipt else 0
        restorable_count = int((rollback_plan.get("summary") or {}).get("restorable_count") or 0)
        if backup_count > 0 and rollback_plan.get("status") == "ready" and restorable_count == backup_count:
            return self._check("rollback_ready", "pass", "rollback plan can restore every backed-up LaunchAgent plist", {
                "backup_count": backup_count,
                "restorable_count": restorable_count,
            })
        if install_receipt.get("status") == "noop" and backup_count == 0:
            return self._check("rollback_ready", "pass", "install was no-op, so no rollback backup is required", {
                "backup_count": 0,
                "rollback_plan_status": rollback_plan.get("status"),
            })
        return self._check("rollback_ready", "fail", "rollback plan cannot restore the latest install", {
            "backup_count": backup_count,
            "rollback_plan_status": rollback_plan.get("status"),
            "rollback_plan_blocker": rollback_plan.get("blocker", ""),
            "restorable_count": restorable_count,
        })

    def _runner_check(self, runner: dict, profile: str = "") -> dict:
        if is_focus_profile(profile):
            return self._check("runner_heartbeat", "pass", "runner heartbeat is parked by focus schedule profile", {
                "profile": profile,
                "restore_path": "schedule.profile: full",
            })
        if not runner:
            return self._check("runner_heartbeat", "warn", "runner heartbeat is missing", {
                "path": str(self.output_root / "runner_status" / "current.json"),
            })
        state = str(runner.get("state", ""))
        timestamp = self._runner_timestamp(runner)
        if state not in {"ok", "running"}:
            return self._check("runner_heartbeat", "warn", "runner heartbeat exists but state is not ok/running", {
                "state": state,
                "timestamp": timestamp,
            })
        if not timestamp:
            return self._check("runner_heartbeat", "warn", "runner heartbeat has no timestamp freshness evidence", {
                "state": state,
            })
        age_seconds = max(0.0, (self.now() - timestamp).total_seconds())
        interval = int(runner.get("interval_seconds") or 300)
        max_age_seconds = max(900, interval * 3)
        if age_seconds <= max_age_seconds:
            return self._check("runner_heartbeat", "pass", "runner heartbeat is fresh", {
                "state": state,
                "age_seconds": round(age_seconds, 3),
                "max_age_seconds": max_age_seconds,
            })
        return self._check("runner_heartbeat", "warn", "runner heartbeat is stale", {
            "state": state,
            "age_seconds": round(age_seconds, 3),
            "max_age_seconds": max_age_seconds,
        })

    def _runner_timestamp(self, runner: dict) -> datetime | None:
        for key in ("updated_at", "finished_at", "started_at"):
            value = runner.get(key)
            if not value:
                continue
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        return None

    def _compact_install(self, receipt: dict) -> dict:
        return {
            "status": receipt.get("status", ""),
            "blocker": receipt.get("blocker", ""),
            "installed_at": receipt.get("installed_at", ""),
            "backup_count": receipt.get("backup_count", 0),
            "backup_dir": receipt.get("backup_dir", ""),
            "acknowledgement_ok": receipt.get("acknowledgement_ok", False),
        }

    def _compact_rollback_plan(self, plan: dict) -> dict:
        summary = plan.get("summary") or {}
        return {
            "status": plan.get("status", ""),
            "blocker": plan.get("blocker", ""),
            "restorable_count": summary.get("restorable_count", 0),
            "blocked_count": summary.get("blocked_count", 0),
        }

    def _check(self, name: str, status: str, summary: str, evidence: dict) -> dict:
        return {"name": name, "status": status, "summary": summary, "evidence": evidence}

    def _rollup(self, checks: list[dict]) -> str:
        if any(check.get("status") == "fail" for check in checks):
            return "blocked"
        if any(check.get("status") == "warn" for check in checks):
            return "warn"
        return "pass"
