from __future__ import annotations

import os
import plistlib
import re
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json
from services.schedule_profiles import PROJECT_LABEL_PREFIX, labels_for_profile, profile_from_schedule


class ScheduleStatus:
    def __init__(
        self,
        output_root: Path | None = None,
        launch_agents_dir: Path | None = None,
        command_runner: Callable[[list[str]], subprocess.CompletedProcess] | None = None,
    ) -> None:
        config = load_pipeline_config()
        self.config = config
        self.output_root = output_root or ROOT / config.get("output_root", "outputs")
        self.launch_agents_dir = launch_agents_dir or Path.home() / "Library" / "LaunchAgents"
        self.command_runner = command_runner or self._run_command

    def run(self, run_date: str) -> dict:
        schedule_rows = load_json(self.output_root / "schedules" / "current.json")
        schedule = schedule_rows[-1] if schedule_rows else {}
        profile = profile_from_schedule(schedule, self.config)
        jobs = [self._inspect_job(job, run_date=run_date) for job in schedule.get("jobs", [])]
        required = set(labels_for_profile(profile))
        present = {job.get("label") for job in jobs}
        generated = {str(job.get("label") or "") for job in jobs}
        orphan_jobs = self._orphan_jobs(generated)
        missing_generated = sorted(required - present)
        installed_count = sum(1 for job in jobs if job.get("installed"))
        loaded_count = sum(1 for job in jobs if job.get("installed") and job.get("loaded"))
        matching_generated_count = sum(1 for job in jobs if job.get("installed") and job.get("matches_generated"))
        active_current_count = sum(1 for job in jobs if job.get("installed") and job.get("matches_generated") and job.get("loaded"))
        healthy_current_count = sum(
            1
            for job in jobs
            if job.get("installed") and job.get("matches_generated") and job.get("loaded") and job.get("runtime_healthy")
        )
        missing_installed_jobs = sorted(str(job.get("label") or "") for job in jobs if not job.get("installed"))
        mismatched_jobs = sorted(str(job.get("label") or "") for job in jobs if job.get("installed") and not job.get("matches_generated"))
        unloaded_jobs = sorted(str(job.get("label") or "") for job in jobs if job.get("installed") and job.get("matches_generated") and not job.get("loaded"))
        runtime_failed_jobs = sorted(
            str(job.get("label") or "")
            for job in jobs
            if job.get("installed") and job.get("loaded") and not job.get("runtime_healthy")
        )
        if not schedule:
            status = "missing"
            message = "schedule artifacts have not been generated"
        elif missing_generated:
            status = "fail"
            message = "generated schedule is missing required jobs"
        elif runtime_failed_jobs:
            status = "runtime_failed"
            message = "launchd jobs are loaded but at least one last execution failed; inspect its durable runner diagnostic"
        elif healthy_current_count == len(required) and not orphan_jobs:
            status = "active"
            message = "all launchd jobs match the generated schedule, are loaded, and have no failed last execution"
        elif matching_generated_count == len(required) and not orphan_jobs:
            status = "installed"
            message = "all launchd jobs match the generated schedule, but at least one is not loaded"
        elif orphan_jobs:
            status = "stale_installed"
            message = "installed launchd plists include jobs outside the generated schedule; remove orphan jobs"
        elif mismatched_jobs:
            status = "stale_installed"
            message = "installed launchd plists differ from the generated schedule; reinstall generated plists"
        elif installed_count:
            status = "partial_installed"
            message = "some generated launchd plists are installed, but the current generated schedule is not fully installed"
        else:
            status = "generated_only"
            message = "launchd plists are generated but not installed in ~/Library/LaunchAgents"
        payload = {
            "run_date": run_date,
            "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": status,
            "message": message,
            "profile": profile,
            "launch_agents_dir": str(self.launch_agents_dir),
            "generated_at": schedule.get("generated_at", ""),
            "generated_launch_agents_dir": schedule.get("launch_agents_dir", ""),
            "missing_generated_jobs": missing_generated,
            "installed_count": installed_count,
            "loaded_count": loaded_count,
            "matching_generated_count": matching_generated_count,
            "active_current_count": active_current_count,
            "healthy_current_count": healthy_current_count,
            "missing_installed_jobs": missing_installed_jobs,
            "mismatched_jobs": mismatched_jobs,
            "unloaded_jobs": unloaded_jobs,
            "runtime_failed_jobs": runtime_failed_jobs,
            "orphan_jobs": orphan_jobs,
            "orphan_count": len(orphan_jobs),
            "required_count": len(required),
            "jobs": jobs,
            "install_commands": schedule.get("install_commands", []),
        }
        write_json(self.output_root / "schedules" / "status_current.json", [payload])
        write_json(self.output_root / "schedules" / f"status_{run_date}.json", [payload])
        return payload

    def _inspect_job(self, job: dict, *, run_date: str) -> dict:
        label = str(job.get("label", ""))
        generated = Path(str(job.get("plist", "")))
        installed = self.launch_agents_dir / f"{label}.plist"
        installed_exists = installed.exists()
        matches_generated = self._plist_matches(generated, installed) if installed_exists and generated.exists() else False
        launchd = self._launchd_status(label)
        return {
            "label": label,
            "generated_plist": str(generated),
            "installed_plist": str(installed),
            "installed": installed_exists,
            "matches_generated": matches_generated,
            "loaded": launchd["loaded"],
            "load_message": launchd["message"],
            "launchd_state": launchd["state"],
            "last_exit_code": launchd["last_exit_code"],
            "runtime_healthy": launchd["runtime_healthy"],
            "start_interval": job.get("start_interval"),
            "start_calendar_interval": job.get("start_calendar_interval"),
            "keep_alive": job.get("keep_alive", False),
            "stderr_summary": self._stderr_summary(label, launchd["last_exit_code"]),
            "report_artifact": self._daily_report_artifact(label, run_date=run_date),
        }

    def _daily_report_artifact(self, label: str, *, run_date: str) -> Optional[dict]:
        if label != "com.wendy.trading-orchestrator.daily-24h-report":
            return None
        try:
            report_date = date.fromisoformat(run_date)
        except ValueError:
            return {"status": "unknown", "reason": "run_date_invalid"}
        path = self.output_root / "dualtrack" / "daily_reports" / f"{report_date.isoformat()}.json"
        rows = load_json(path)
        latest = rows[-1] if rows and isinstance(rows[-1], dict) else {}
        if latest.get("schema_version") == "trading-daily-24h-v1" and latest.get("status") == "complete":
            return {"status": "present", "path": str(path), "report_hash": latest.get("report_hash")}
        return {
            "status": "missing",
            "path": str(path),
            "next_action": "run the daily report pipeline after all overlapping terminal cycle packages are available",
        }

    def _stderr_summary(self, label: str, last_exit_code: Optional[int]) -> Optional[dict]:
        """Expose a bounded, actionable failure reason without replaying logs."""

        if last_exit_code in (None, 0):
            return None
        path = self.output_root / "schedules" / "logs" / f"{label}.err.log"
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[-1200:].strip()
        except OSError:
            text = ""
        lowered = text.lower()
        if "can't open file" in lowered or "no such file or directory" in lowered:
            failure_class = "scheduled_command_missing"
            next_action = "reinstall the generated scheduler plist; the configured command is unavailable"
        elif "natural day is not terminal" in lowered:
            failure_class = "daily_report_not_terminal"
            next_action = "wait for the final Beijing-day cycle to close, then let the next schedule run"
        elif "cycle package" in lowered:
            failure_class = "daily_report_cycle_evidence_missing"
            next_action = "repair or close the missing terminal cycle package before retrying the report"
        elif "delivery failed" in lowered or "delivered=false" in lowered:
            failure_class = "daily_report_delivery_failed"
            next_action = "inspect the report delivery receipt and retry only after the channel is healthy"
        else:
            failure_class = "scheduled_command_failed"
            next_action = "inspect the bounded scheduler stderr summary and rerun the repository pipeline manually"
        return {
            "path": str(path),
            "exit_code": last_exit_code,
            "failure_class": failure_class,
            "next_action": next_action,
            "stderr_tail": text[-600:] if text else "stderr artifact unavailable",
        }

    def _plist_matches(self, generated: Path, installed: Path) -> bool:
        try:
            with generated.open("rb") as handle:
                generated_payload = plistlib.load(handle)
            with installed.open("rb") as handle:
                installed_payload = plistlib.load(handle)
        except (OSError, plistlib.InvalidFileException):
            return False
        return generated_payload == installed_payload

    def _loaded(self, label: str) -> tuple[bool, str]:
        status = self._launchd_status(label)
        return bool(status["loaded"]), str(status["message"])

    def _launchd_status(self, label: str) -> dict:
        if not label:
            return {
                "loaded": False,
                "message": "missing label",
                "state": "",
                "last_exit_code": None,
                "runtime_healthy": False,
            }
        try:
            result = self.command_runner(["launchctl", "print", f"gui/{os.getuid()}/{label}"])
        except (OSError, subprocess.SubprocessError) as exc:
            return {
                "loaded": False,
                "message": str(exc),
                "state": "",
                "last_exit_code": None,
                "runtime_healthy": False,
            }
        if result.returncode == 0:
            stdout = result.stdout if isinstance(result.stdout, str) else ""
            state_match = re.search(r"^\s*state\s*=\s*([^\n]+)", stdout, flags=re.MULTILINE)
            exit_match = re.search(r"^\s*last exit code\s*=\s*(-?\d+)", stdout, flags=re.MULTILINE)
            state = state_match.group(1).strip() if state_match else ""
            last_exit_code = int(exit_match.group(1)) if exit_match else None
            runtime_healthy = state == "running" or last_exit_code in {None, 0}
            message = "loaded" if runtime_healthy else f"loaded but last exit code is {last_exit_code}"
            return {
                "loaded": True,
                "message": message,
                "state": state,
                "last_exit_code": last_exit_code,
                "runtime_healthy": runtime_healthy,
            }
        stderr = (result.stderr or "").strip() if isinstance(result.stderr, str) else ""
        return {
            "loaded": False,
            "message": stderr or "not loaded",
            "state": "",
            "last_exit_code": None,
            "runtime_healthy": False,
        }

    def _orphan_jobs(self, generated: set[str]) -> list[dict]:
        jobs: list[dict] = []
        if not self.launch_agents_dir.exists():
            return jobs
        for path in sorted(self.launch_agents_dir.glob(f"{PROJECT_LABEL_PREFIX}*.plist")):
            label = path.stem
            if label in generated:
                continue
            loaded, load_message = self._loaded(label)
            jobs.append({
                "label": label,
                "installed_plist": str(path),
                "loaded": loaded,
                "load_message": load_message,
            })
        return jobs

    def _run_command(self, command: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(command, capture_output=True, text=True, timeout=3, check=False)
