"""Render and install the bounded Cloud Paper systemd surface."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


UNIT_NAMES = (
    "gridmind-datafeed.service",
    "gridmind-dashboard.service",
    "gridmind-access-gateway.service",
    "gridmind-cloudflared.service",
    "gridmind-live-tick.service",
    "gridmind-live-tick.timer",
    "gridmind-daily-24h.service",
    "gridmind-daily-24h.timer",
    "gridmind-daily-self-review.service",
    "gridmind-daily-self-review.timer",
    "gridmind-backup.service",
    "gridmind-backup.timer",
    "gridmind-deadman-ping.service",
    "gridmind-deadman-ping.timer",
    "gridmind-ai-provider-readiness.service",
    "gridmind-ai-provider-readiness.timer",
)


@dataclass(frozen=True)
class CloudSystemdPaths:
    repo_root: Path
    datafeed_root: Path
    app_python: Path
    datafeed_python: Path
    systemd_dir: Path = Path("/etc/systemd/system")

    def validate(self) -> None:
        for name, value in (
            ("repo_root", self.repo_root),
            ("datafeed_root", self.datafeed_root),
            ("app_python", self.app_python),
            ("datafeed_python", self.datafeed_python),
            ("systemd_dir", self.systemd_dir),
        ):
            if not value.is_absolute():
                raise ValueError(f"{name} must be absolute")


class CloudSystemdRenderer:
    def __init__(self, paths: CloudSystemdPaths) -> None:
        paths.validate()
        self.paths = paths

    def render(self, destination: Path) -> dict[str, Any]:
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        units = self._units()
        for name, content in units.items():
            (destination / name).write_text(content.rstrip() + "\n", encoding="utf-8")
        return {
            "status": "rendered",
            "destination": str(destination),
            "units": sorted(units),
            "scheduler_active": False,
            "persistent_data_changed": False,
        }

    def _units(self) -> dict[str, str]:
        p = self.paths
        common = "\n".join(
            [
                "User=gridmind",
                "Group=gridmind",
                f"WorkingDirectory={p.repo_root}",
                "Environment=GRIDMIND_RUNTIME_MODE=cloud",
                f"Environment=PYTHONPATH={p.repo_root}",
                "EnvironmentFile=/etc/gridmind/runtime.env",
                "EnvironmentFile=-/etc/gridmind/paper.env",
                "NoNewPrivileges=true",
                "PrivateTmp=true",
                "ProtectHome=true",
                "ProtectSystem=strict",
                "ReadWritePaths=/var/lib/gridmind",
            ]
        )
        return {
            "gridmind-datafeed.service": f"""[Unit]
Description=GridMind independent datafeed
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
{common}
WorkingDirectory={p.datafeed_root}
ExecStart={p.datafeed_python} -m uvicorn kline.app:create_app --factory --host 127.0.0.1 --port 8100
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target""",
            "gridmind-dashboard.service": f"""[Unit]
Description=GridMind Paper Dashboard
After=network-online.target gridmind-datafeed.service
Requires=gridmind-datafeed.service

[Service]
Type=simple
{common}
ExecStartPre={p.app_python} -m pipelines.cloud_service_boot --service dashboard
ExecStart={p.app_python} -m pipelines.dashboard_server --host 127.0.0.1 --port 8765
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target""",
            "gridmind-access-gateway.service": f"""[Unit]
Description=GridMind authenticated allowlist gateway
After=gridmind-dashboard.service
Requires=gridmind-dashboard.service

[Service]
Type=simple
{common}
ExecStartPre={p.app_python} -m pipelines.cloud_service_boot --service access-gateway
ExecStart={p.app_python} -m services.cloud_access_gateway --host 127.0.0.1 --port 8766
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target""",
            "gridmind-cloudflared.service": """[Unit]
Description=GridMind authenticated Cloudflare Tunnel
After=gridmind-access-gateway.service network-online.target
Requires=gridmind-access-gateway.service
Wants=network-online.target

[Service]
Type=simple
User=gridmind
Group=gridmind
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ExecStart=/usr/local/bin/cloudflared --no-autoupdate --config /etc/gridmind/cloudflared.yml tunnel run
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target""",
            "gridmind-live-tick.service": f"""[Unit]
Description=GridMind one-shot Paper live tick
After=gridmind-datafeed.service
Requires=gridmind-datafeed.service

[Service]
Type=oneshot
{common}
ReadWritePaths=/opt/gridmind/.codex
ExecStartPre={p.app_python} -m pipelines.cloud_service_boot --service dualtrack-live-tick
ExecStart={p.app_python} -m pipelines.dualtrack_cycle_runner --event live-tick
TimeoutStartSec=55""",
            "gridmind-live-tick.timer": """[Unit]
Description=GridMind non-overlapping minute Paper tick

[Timer]
OnBootSec=60
OnUnitInactiveSec=60
AccuracySec=1
Persistent=true
Unit=gridmind-live-tick.service

[Install]
WantedBy=timers.target""",
            "gridmind-daily-24h.service": f"""[Unit]
Description=GridMind terminal Beijing 24-hour report
After=gridmind-live-tick.service

[Service]
Type=oneshot
{common}
ExecStartPre={p.app_python} -m pipelines.cloud_service_boot --service daily-24h
ExecStart={p.app_python} -m pipelines.trading_daily_24h_report --send --verify
TimeoutStartSec=300""",
            "gridmind-daily-24h.timer": """[Unit]
Description=GridMind terminal report timer

[Timer]
OnCalendar=*-*-* 01:03:00 UTC
Persistent=true
Unit=gridmind-daily-24h.service

[Install]
WantedBy=timers.target""",
            "gridmind-daily-self-review.service": f"""[Unit]
Description=GridMind evidence-backed daily self-review
After=gridmind-daily-24h.service

[Service]
Type=oneshot
{common}
ExecStartPre={p.app_python} -m pipelines.cloud_service_boot --service daily-self-review
ExecStart={p.app_python} -m pipelines.cloud_daily_self_review
TimeoutStartSec=300""",
            "gridmind-daily-self-review.timer": """[Unit]
Description=GridMind daily self-review timer

[Timer]
OnCalendar=*-*-* 01:10:00 UTC
Persistent=true
Unit=gridmind-daily-self-review.service

[Install]
WantedBy=timers.target""",
            "gridmind-backup.service": f"""[Unit]
Description=GridMind verified Paper backup
After=gridmind-daily-self-review.service

[Service]
Type=oneshot
{common}
ExecStartPre={p.app_python} -m pipelines.cloud_service_boot --service backup
ExecStart={p.app_python} -m pipelines.cloud_backup create --keep 7
TimeoutStartSec=900""",
            "gridmind-backup.timer": """[Unit]
Description=GridMind daily verified backup timer

[Timer]
OnCalendar=*-*-* 01:30:00 UTC
Persistent=true
Unit=gridmind-backup.service

[Install]
WantedBy=timers.target""",
            "gridmind-deadman-ping.service": f"""[Unit]
Description=GridMind external dead-man ping
After=gridmind-live-tick.service

[Service]
Type=oneshot
{common}
ExecStartPre={p.app_python} -m pipelines.cloud_service_boot --service deadman-ping
ExecStart={p.app_python} -m pipelines.deadman_ping
TimeoutStartSec=30""",
            "gridmind-deadman-ping.timer": """[Unit]
Description=GridMind dead-man timer

[Timer]
OnBootSec=300
OnUnitInactiveSec=300
Persistent=true
Unit=gridmind-deadman-ping.service

[Install]
WantedBy=timers.target""",
            "gridmind-ai-provider-readiness.service": f"""[Unit]
Description=GridMind bounded Cloud AI provider readiness renewal
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
{common}
ReadWritePaths=/opt/gridmind/.codex
ExecStartPre={p.app_python} -m pipelines.cloud_service_boot --service ai-provider-readiness
ExecStart={p.app_python} -m pipelines.cloud_ai_provider_readiness --renew-if-due --json
TimeoutStartSec=120""",
            "gridmind-ai-provider-readiness.timer": """[Unit]
Description=GridMind non-overlapping Cloud AI provider readiness renewal timer

[Timer]
OnBootSec=120
OnUnitInactiveSec=300
AccuracySec=30
Persistent=true
Unit=gridmind-ai-provider-readiness.service

[Install]
WantedBy=timers.target""",
        }


class CloudSystemdInstaller:
    def __init__(
        self,
        *,
        systemd_dir: Path = Path("/etc/systemd/system"),
        command_runner: Callable[..., subprocess.CompletedProcess] | None = None,
    ) -> None:
        self.systemd_dir = Path(systemd_dir)
        self.command_runner = command_runner or subprocess.run

    def plan(self, rendered_dir: Path, action: str) -> list[list[str]]:
        rendered_dir = Path(rendered_dir)
        if action == "install-passive":
            return [
                *[
                    ["install", "-m", "0644", str(rendered_dir / name), str(self.systemd_dir / name)]
                    for name in UNIT_NAMES
                ],
                ["systemctl", "daemon-reload"],
                ["systemctl", "enable", "--now", "gridmind-datafeed.service"],
            ]
        if action == "activate-dashboard":
            return [
                ["systemctl", "enable", "--now", "gridmind-dashboard.service"],
            ]
        if action == "activate-remote-access":
            return [
                [
                    "systemctl",
                    "enable",
                    "--now",
                    "gridmind-access-gateway.service",
                    "gridmind-cloudflared.service",
                ],
            ]
        if action == "activate-provider-readiness":
            return [
                [
                    "systemctl",
                    "enable",
                    "--now",
                    "gridmind-ai-provider-readiness.timer",
                ],
            ]
        if action == "uninstall":
            return [
                ["systemctl", "disable", "--now", *UNIT_NAMES],
                *[["rm", "-f", str(self.systemd_dir / name)] for name in UNIT_NAMES],
                ["systemctl", "daemon-reload"],
            ]
        raise ValueError(
            "action must be install-passive, activate-dashboard, "
            "activate-remote-access, activate-provider-readiness, or uninstall"
        )

    def apply(self, rendered_dir: Path, action: str, *, dry_run: bool = True) -> dict[str, Any]:
        commands = self.plan(rendered_dir, action)
        if dry_run:
            return {
                "status": "dry_run",
                "action": action,
                "commands": commands,
                "scheduler_active": False,
                "persistent_data_changed": False,
            }
        if os.name != "posix" or not Path("/run/systemd/system").exists():
            raise RuntimeError("systemd installer requires a Linux systemd host")
        for command in commands:
            result = self.command_runner(command, capture_output=True, text=True, check=False)
            if result.returncode != 0:
                raise RuntimeError(
                    f"systemd command failed: {command[0]}: {str(result.stderr or '')[-300:]}"
                )
        return {
            "status": "applied",
            "action": action,
            "commands": commands,
            "scheduler_active": False,
            "persistent_data_changed": False,
        }
