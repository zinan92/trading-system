from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json
from services.schedule_status import ScheduleStatus


class ScheduleInstaller:
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

    def install(self, run_date: str, restart_loaded: bool = True) -> dict:
        schedule_rows = load_json(self.output_root / "schedules" / "current.json")
        schedule = schedule_rows[-1] if schedule_rows else {}
        self.launch_agents_dir.mkdir(parents=True, exist_ok=True)
        results = []
        for job in schedule.get("jobs", []):
            results.append(self._install_job(job, restart_loaded))
        status = ScheduleStatus(self.output_root, self.launch_agents_dir, self.command_runner).run(run_date)
        failed = [item for item in results if item["status"] == "fail"]
        payload = {
            "run_date": run_date,
            "installed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": "fail" if failed else status.get("status", "unknown"),
            "restart_loaded": restart_loaded,
            "launch_agents_dir": str(self.launch_agents_dir),
            "jobs": results,
            "schedule_status": status,
        }
        write_json(self.output_root / "schedules" / "install_current.json", [payload])
        write_json(self.output_root / "schedules" / f"install_{run_date}.json", [payload])
        return payload

    def _install_job(self, job: dict, restart_loaded: bool) -> dict:
        label = str(job.get("label", ""))
        source = Path(str(job.get("plist", "")))
        target = self.launch_agents_dir / f"{label}.plist"
        commands: list[dict] = []
        if not label or not source.exists():
            return {
                "label": label,
                "source": str(source),
                "target": str(target),
                "status": "fail",
                "error": "generated plist is missing",
                "commands": commands,
            }
        if restart_loaded:
            commands.append(self._command(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], allow_failure=True))
        shutil.copy2(source, target)
        commands.append({"command": ["copy", str(source), str(target)], "returncode": 0, "stdout": "", "stderr": ""})
        if not restart_loaded:
            loaded = self._command(["launchctl", "print", f"gui/{os.getuid()}/{label}"], allow_failure=True)
            commands.append(loaded)
            if loaded["returncode"] == 0:
                return {
                    "label": label,
                    "source": str(source),
                    "target": str(target),
                    "status": "installed",
                    "already_loaded": True,
                    "commands": commands,
                }
        bootstrap = self._command(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(target)], allow_failure=True)
        commands.append(bootstrap)
        if bootstrap["returncode"] != 0 and "already bootstrapped" not in bootstrap["stderr"].lower():
            return {
                "label": label,
                "source": str(source),
                "target": str(target),
                "status": "fail",
                "error": bootstrap["stderr"] or bootstrap["stdout"] or "launchctl bootstrap failed",
                "commands": commands,
            }
        if restart_loaded:
            commands.append(self._command(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"], allow_failure=True))
        return {
            "label": label,
            "source": str(source),
            "target": str(target),
            "status": "installed",
            "commands": commands,
        }

    def _command(self, command: list[str], allow_failure: bool = False) -> dict:
        try:
            result = self.command_runner(command)
        except (OSError, subprocess.SubprocessError) as exc:
            if allow_failure:
                return {"command": command, "returncode": 1, "stdout": "", "stderr": str(exc)}
            raise
        return {
            "command": command,
            "returncode": result.returncode,
            "stdout": (result.stdout or "").strip() if isinstance(result.stdout, str) else "",
            "stderr": (result.stderr or "").strip() if isinstance(result.stderr, str) else "",
        }

    def _run_command(self, command: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
