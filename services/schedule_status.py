from __future__ import annotations

import os
import plistlib
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json


class ScheduleStatus:
    def __init__(
        self,
        output_root: Path | None = None,
        launch_agents_dir: Path | None = None,
        command_runner: Callable[[list[str]], subprocess.CompletedProcess] | None = None,
    ) -> None:
        config = load_pipeline_config()
        self.output_root = output_root or ROOT / config.get("output_root", "outputs")
        self.launch_agents_dir = launch_agents_dir or Path.home() / "Library" / "LaunchAgents"
        self.command_runner = command_runner or self._run_command

    def run(self, run_date: str) -> dict:
        schedule_rows = load_json(self.output_root / "schedules" / "current.json")
        schedule = schedule_rows[-1] if schedule_rows else {}
        jobs = [self._inspect_job(job) for job in schedule.get("jobs", [])]
        required = {
            "com.wendy.trading-orchestrator.runner",
            "com.wendy.trading-orchestrator.trading-plan",
            "com.wendy.trading-orchestrator.evening-review",
            "com.wendy.trading-orchestrator.daily-review",
            "com.wendy.trading-orchestrator.dashboard",
            "com.wendy.trading-orchestrator.strategies",
        }
        present = {job.get("label") for job in jobs}
        missing_generated = sorted(required - present)
        installed_count = sum(1 for job in jobs if job.get("installed") and job.get("matches_generated"))
        loaded_count = sum(1 for job in jobs if job.get("installed") and job.get("matches_generated") and job.get("loaded"))
        if not schedule:
            status = "missing"
            message = "schedule artifacts have not been generated"
        elif missing_generated:
            status = "fail"
            message = "generated schedule is missing required jobs"
        elif installed_count == len(required) and loaded_count == len(required):
            status = "active"
            message = "all launchd jobs are installed and loaded"
        elif installed_count == len(required):
            status = "installed"
            message = "all launchd jobs are installed, but at least one is not loaded"
        else:
            status = "generated_only"
            message = "launchd plists are generated but not installed in ~/Library/LaunchAgents"
        payload = {
            "run_date": run_date,
            "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": status,
            "message": message,
            "launch_agents_dir": str(self.launch_agents_dir),
            "generated_at": schedule.get("generated_at", ""),
            "generated_launch_agents_dir": schedule.get("launch_agents_dir", ""),
            "missing_generated_jobs": missing_generated,
            "installed_count": installed_count,
            "loaded_count": loaded_count,
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

    def _run_command(self, command: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(command, capture_output=True, text=True, timeout=3, check=False)
