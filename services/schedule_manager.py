from __future__ import annotations

import os
import plistlib
from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import write_json


class ScheduleManager:
    def __init__(self, output_root: Path | None = None, repo_root: Path | None = None) -> None:
        config = load_pipeline_config()
        self.repo_root = repo_root or ROOT
        self.output_root = output_root or ROOT / config.get("output_root", "outputs")
        self.python = os.getenv("TRADING_ORCHESTRATOR_PYTHON", "python3")
        # The chan strategy needs Python >= 3.11 + pandas, so the strategies job
        # runs on a dedicated interpreter. The other jobs (runner/daily-review/
        # dashboard) stay on the base python untouched. Defaults to the base
        # python so non-chan setups and tests are unaffected.
        self.strategies_python = os.getenv("TRADING_ORCHESTRATOR_STRATEGIES_PYTHON") or self.python

    def build(
        self,
        review_hour: int = 23,
        review_minute: int = 55,
        dashboard_port: int = 8765,
        plan_hour: int = 8,
        plan_minute: int = 30,
        evening_review_hour: int = 23,
        evening_review_minute: int = 30,
    ) -> dict:
        root = self.output_root / "schedules"
        launch_dir = root / "launch_agents"
        log_dir = root / "logs"
        launch_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)
        jobs = [
            self._runner_job(log_dir),
            self._trading_plan_job(log_dir, plan_hour, plan_minute),
            self._evening_review_job(log_dir, evening_review_hour, evening_review_minute),
            self._daily_review_job(log_dir, review_hour, review_minute),
            self._dashboard_job(log_dir, dashboard_port),
            self._strategies_job(log_dir),
        ]
        for job in jobs:
            path = launch_dir / f"{job['Label']}.plist"
            with path.open("wb") as handle:
                plistlib.dump(job, handle, sort_keys=True)
        install_commands = [
            f"mkdir -p ~/Library/LaunchAgents",
            f"cp {launch_dir}/com.wendy.trading-orchestrator.*.plist ~/Library/LaunchAgents/",
            *[
                f"launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/{job['Label']}.plist"
                for job in jobs
            ],
        ]
        payload = {
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": "generated",
            "repo_root": str(self.repo_root),
            "launch_agents_dir": str(launch_dir),
            "log_dir": str(log_dir),
            "jobs": [
                {
                    "label": job["Label"],
                    "plist": str(launch_dir / f"{job['Label']}.plist"),
                    "program_arguments": job["ProgramArguments"],
                    "start_interval": job.get("StartInterval"),
                    "start_calendar_interval": job.get("StartCalendarInterval"),
                    "run_at_load": job.get("RunAtLoad", False),
                    "keep_alive": job.get("KeepAlive", False),
                }
                for job in jobs
            ],
            "install_commands": install_commands,
            "note": "Generated only; not installed. Review the plist files before copying them into ~/Library/LaunchAgents.",
        }
        self._write_readme(root, payload)
        write_json(root / "current.json", [payload])
        write_json(root / f"{datetime.now(timezone.utc).date().isoformat()}.json", [payload])
        return payload

    def _runner_job(self, log_dir: Path) -> dict:
        label = "com.wendy.trading-orchestrator.runner"
        return self._base_job(
            label,
            [self.python, "-m", "pipelines.runner", "--paper-auto-approve", "--iterations", "1", "--interval-seconds", "300"],
            log_dir,
            extra={"StartInterval": 300, "RunAtLoad": True},
        )

    def _trading_plan_job(self, log_dir: Path, hour: int, minute: int) -> dict:
        label = "com.wendy.trading-orchestrator.trading-plan"
        return self._base_job(
            label,
            [self.python, "-m", "pipelines.trading_plan"],
            log_dir,
            extra={"StartCalendarInterval": {"Hour": hour, "Minute": minute}},
        )

    def _evening_review_job(self, log_dir: Path, hour: int, minute: int) -> dict:
        label = "com.wendy.trading-orchestrator.evening-review"
        return self._base_job(
            label,
            [self.python, "-m", "pipelines.evening_review"],
            log_dir,
            extra={"StartCalendarInterval": {"Hour": hour, "Minute": minute}},
        )

    def _daily_review_job(self, log_dir: Path, hour: int, minute: int) -> dict:
        label = "com.wendy.trading-orchestrator.daily-review"
        return self._base_job(
            label,
            [self.python, "-m", "pipelines.daily_review"],
            log_dir,
            extra={"StartCalendarInterval": {"Hour": hour, "Minute": minute}},
        )

    def _strategies_job(self, log_dir: Path) -> dict:
        # Refreshes every enabled strategy's isolated paper namespace + the
        # leaderboard on the same 5-min cadence as the runner, so the comparison
        # view stays live. Reads the shared market DB the runner keeps fresh.
        label = "com.wendy.trading-orchestrator.strategies"
        return self._base_job(
            label,
            [self.strategies_python, "-m", "pipelines.strategies", "--paper-auto-approve"],
            log_dir,
            extra={"StartInterval": 300, "RunAtLoad": True},
        )

    def _dashboard_job(self, log_dir: Path, port: int) -> dict:
        label = "com.wendy.trading-orchestrator.dashboard"
        return self._base_job(
            label,
            [self.python, "-m", "pipelines.dashboard_server", "--host", "127.0.0.1", "--port", str(port)],
            log_dir,
            extra={"RunAtLoad": True, "KeepAlive": True},
        )

    def _base_job(self, label: str, args: list[str], log_dir: Path, extra: dict) -> dict:
        return {
            "Label": label,
            "ProgramArguments": args,
            "WorkingDirectory": str(self.repo_root),
            "StandardOutPath": str(log_dir / f"{label}.out.log"),
            "StandardErrorPath": str(log_dir / f"{label}.err.log"),
            "EnvironmentVariables": {
                "PYTHONUNBUFFERED": "1",
                "TZ": "UTC",
                "TRADING_ORCHESTRATOR_OUTPUT_ROOT": str(self.output_root),
                "TRADING_ORCHESTRATOR_MARKET_DB": str(self.repo_root / "data" / "market_data.db"),
                "TRADING_ORCHESTRATOR_LIVE_ENV": str(self.repo_root / "configs" / "live.env"),
            },
            **extra,
        }

    def _write_readme(self, root: Path, payload: dict) -> None:
        lines = [
            "# Trading Orchestrator Local Schedule",
            "",
            "Generated only; not installed automatically.",
            "",
            "## Jobs",
            "",
        ]
        for job in payload["jobs"]:
            lines.extend(
                [
                    f"### {job['label']}",
                    "",
                    f"- Plist: `{job['plist']}`",
                    f"- Args: `{' '.join(job['program_arguments'])}`",
                    f"- StartInterval: `{job.get('start_interval')}`",
                    f"- StartCalendarInterval: `{job.get('start_calendar_interval')}`",
                    f"- RunAtLoad: `{job.get('run_at_load')}`",
                    f"- KeepAlive: `{job.get('keep_alive')}`",
                    "",
                ]
            )
        lines.extend(["## Install Commands", ""])
        lines.extend([f"```bash", *payload["install_commands"], "```", ""])
        (root / "README.md").write_text("\n".join(lines), encoding="utf-8")
