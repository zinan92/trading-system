from __future__ import annotations

import os
import plistlib
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

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
        jobs = [self._inspect_job(job) for job in schedule.get("jobs", [])]
        required = set(labels_for_profile(profile))
        present = {job.get("label") for job in jobs}
        generated = {str(job.get("label") or "") for job in jobs}
        orphan_jobs = self._orphan_jobs(generated)
        missing_generated = sorted(required - present)
        installed_count = sum(1 for job in jobs if job.get("installed"))
        loaded_count = sum(1 for job in jobs if job.get("installed") and job.get("loaded"))
        matching_generated_count = sum(1 for job in jobs if job.get("installed") and job.get("matches_generated"))
        active_current_count = sum(1 for job in jobs if job.get("installed") and job.get("matches_generated") and job.get("loaded"))
        missing_installed_jobs = sorted(str(job.get("label") or "") for job in jobs if not job.get("installed"))
        mismatched_jobs = sorted(str(job.get("label") or "") for job in jobs if job.get("installed") and not job.get("matches_generated"))
        unloaded_jobs = sorted(str(job.get("label") or "") for job in jobs if job.get("installed") and job.get("matches_generated") and not job.get("loaded"))
        if not schedule:
            status = "missing"
            message = "schedule artifacts have not been generated"
        elif missing_generated:
            status = "fail"
            message = "generated schedule is missing required jobs"
        elif active_current_count == len(required) and not orphan_jobs:
            status = "active"
            message = "all launchd jobs match the generated schedule and are loaded"
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
            "missing_installed_jobs": missing_installed_jobs,
            "mismatched_jobs": mismatched_jobs,
            "unloaded_jobs": unloaded_jobs,
            "orphan_jobs": orphan_jobs,
            "orphan_count": len(orphan_jobs),
            "required_count": len(required),
            "jobs": jobs,
            "install_commands": schedule.get("install_commands", []),
        }
        write_json(self.output_root / "schedules" / "status_current.json", [payload])
        write_json(self.output_root / "schedules" / f"status_{run_date}.json", [payload])
        return payload

    def _inspect_job(self, job: dict) -> dict:
        label = str(job.get("label", ""))
        generated = Path(str(job.get("plist", "")))
        installed = self.launch_agents_dir / f"{label}.plist"
        installed_exists = installed.exists()
        matches_generated = self._plist_matches(generated, installed) if installed_exists and generated.exists() else False
        loaded, load_message = self._loaded(label)
        return {
            "label": label,
            "generated_plist": str(generated),
            "installed_plist": str(installed),
            "installed": installed_exists,
            "matches_generated": matches_generated,
            "loaded": loaded,
            "load_message": load_message,
            "start_interval": job.get("start_interval"),
            "start_calendar_interval": job.get("start_calendar_interval"),
            "keep_alive": job.get("keep_alive", False),
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
        if not label:
            return False, "missing label"
        try:
            result = self.command_runner(["launchctl", "print", f"gui/{os.getuid()}/{label}"])
        except (OSError, subprocess.SubprocessError) as exc:
            return False, str(exc)
        if result.returncode == 0:
            return True, "loaded"
        stderr = (result.stderr or "").strip() if isinstance(result.stderr, str) else ""
        return False, stderr or "not loaded"

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
