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
    "gridmind-deadman-watchdog.service",
    "gridmind-deadman-watchdog.timer",
    "gridmind-ai-provider-readiness.service",
    "gridmind-ai-provider-readiness.timer",
    "gridmind-next-cycle-plan.service",
    "gridmind-next-cycle-plan.timer",
    "gridmind-unit-failure-alert@.service",
)

DROP_IN_FILES = (
    "ssh.service.d/90-gridmind-oom-protection.conf",
    "user-.slice.d/90-gridmind-memory-budget.conf",
)

_UNIT_FAILURE_HOOK = "OnFailure=gridmind-unit-failure-alert@%n.service"


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
            path = destination / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content.rstrip() + "\n", encoding="utf-8")
        return {
            "status": "rendered",
            "destination": str(destination),
            "units": sorted(name for name in units if name in UNIT_NAMES),
            "drop_ins": sorted(name for name in units if name in DROP_IN_FILES),
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
{_UNIT_FAILURE_HOOK}

[Service]
Type=simple
{common}
WorkingDirectory={p.datafeed_root}
ExecStart={p.datafeed_python} -m uvicorn kline.app:create_app --factory --host 127.0.0.1 --port 8100
MemoryMax=768M
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target""",
            "gridmind-dashboard.service": f"""[Unit]
Description=GridMind Paper Dashboard
After=network-online.target gridmind-datafeed.service
Requires=gridmind-datafeed.service
{_UNIT_FAILURE_HOOK}

[Service]
Type=simple
{common}
ExecStartPre={p.app_python} -m pipelines.cloud_service_boot --service dashboard
ExecStart={p.app_python} -m pipelines.dashboard_server --host 127.0.0.1 --port 8765
MemoryMax=256M
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target""",
            "gridmind-access-gateway.service": f"""[Unit]
Description=GridMind authenticated allowlist gateway
After=gridmind-dashboard.service
Requires=gridmind-dashboard.service
{_UNIT_FAILURE_HOOK}

[Service]
Type=simple
{common}
ExecStartPre={p.app_python} -m pipelines.cloud_service_boot --service access-gateway
ExecStart={p.app_python} -m services.cloud_access_gateway --host 127.0.0.1 --port 8766
MemoryMax=96M
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target""",
            "gridmind-cloudflared.service": f"""[Unit]
Description=GridMind authenticated Cloudflare Tunnel
After=gridmind-access-gateway.service network-online.target
Requires=gridmind-access-gateway.service
Wants=network-online.target
{_UNIT_FAILURE_HOOK}

[Service]
Type=simple
User=gridmind
Group=gridmind
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
OOMScoreAdjust=-900
ExecStart=/usr/local/bin/cloudflared --no-autoupdate --config /etc/gridmind/cloudflared.yml tunnel run
MemoryMax=128M
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target""",
            "gridmind-live-tick.service": f"""[Unit]
Description=GridMind one-shot Paper live tick
After=gridmind-datafeed.service
Requires=gridmind-datafeed.service
{_UNIT_FAILURE_HOOK}

[Service]
Type=oneshot
{common}
ReadWritePaths=/opt/gridmind/.codex
ExecStartPre={p.app_python} -m pipelines.cloud_service_boot --service dualtrack-live-tick
ExecStart={p.app_python} -m pipelines.dualtrack_cycle_runner --event live-tick
MemoryHigh=384M
MemoryMax=512M
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
{_UNIT_FAILURE_HOOK}

[Service]
Type=oneshot
{common}
ExecStartPre={p.app_python} -m pipelines.cloud_service_boot --service daily-24h
ExecStart={p.app_python} -m pipelines.trading_daily_24h_report --send --verify
MemoryMax=128M
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
{_UNIT_FAILURE_HOOK}

[Service]
Type=oneshot
{common}
ExecStartPre={p.app_python} -m pipelines.cloud_service_boot --service daily-self-review
ExecStart={p.app_python} -m pipelines.cloud_daily_self_review
MemoryMax=128M
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
{_UNIT_FAILURE_HOOK}

[Service]
Type=oneshot
{common}
ExecStartPre={p.app_python} -m pipelines.cloud_service_boot --service backup
ExecStart={p.app_python} -m pipelines.cloud_backup create --keep 7
MemoryHigh=256M
MemoryMax=384M
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
{_UNIT_FAILURE_HOOK}

[Service]
Type=oneshot
{common}
ExecStartPre={p.app_python} -m pipelines.cloud_service_boot --service deadman-ping
ExecStart={p.app_python} -m pipelines.deadman_ping
MemoryMax=192M
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
            "gridmind-deadman-watchdog.service": f"""[Unit]
Description=GridMind independent dead-man liveness watchdog
After=network-online.target
Wants=network-online.target
{_UNIT_FAILURE_HOOK}

[Service]
Type=oneshot
{common}
ExecStartPre={p.app_python} -m pipelines.cloud_service_boot --service deadman-watchdog
ExecStart={p.app_python} -m pipelines.cloud_deadman_watchdog
MemoryMax=64M
TimeoutStartSec=20""",
            "gridmind-deadman-watchdog.timer": """[Unit]
Description=GridMind independent dead-man liveness watchdog timer

[Timer]
OnBootSec=90
OnUnitInactiveSec=60
AccuracySec=5
Persistent=true
Unit=gridmind-deadman-watchdog.service

[Install]
WantedBy=timers.target""",
            "gridmind-ai-provider-readiness.service": f"""[Unit]
Description=GridMind bounded Cloud AI provider readiness renewal
After=network-online.target
Wants=network-online.target
{_UNIT_FAILURE_HOOK}

[Service]
Type=oneshot
{common}
ReadWritePaths=/opt/gridmind/.codex
ExecStartPre={p.app_python} -m pipelines.cloud_service_boot --service ai-provider-readiness
ExecStart={p.app_python} -m pipelines.cloud_ai_provider_readiness --renew-if-due --json
MemoryMax=128M
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
            "gridmind-next-cycle-plan.service": f"""[Unit]
Description=GridMind verified next-cycle Paper plan pre-generation
After=gridmind-live-tick.service gridmind-datafeed.service network-online.target
Requires=gridmind-datafeed.service
Wants=network-online.target
{_UNIT_FAILURE_HOOK}

[Service]
Type=oneshot
{common}
ReadWritePaths=/opt/gridmind/.codex
ExecStartPre={p.app_python} -m pipelines.cloud_service_boot --service next-cycle-plan
ExecStart={p.app_python} -m pipelines.paper_next_cycle_plan --json
MemoryMax=128M
TimeoutStartSec=120""",
            "gridmind-next-cycle-plan.timer": """[Unit]
Description=GridMind non-overlapping next-cycle Paper plan timer

[Timer]
OnBootSec=180
OnUnitInactiveSec=300
AccuracySec=30
Persistent=true
Unit=gridmind-next-cycle-plan.service

[Install]
WantedBy=timers.target""",
            "gridmind-unit-failure-alert@.service": f"""[Unit]
Description=GridMind external failure alert for %i
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
{common}
ExecStart={p.app_python} -m pipelines.cloud_unit_failure_alert --failed-unit %i
MemoryMax=64M
TimeoutStartSec=30""",
            "ssh.service.d/90-gridmind-oom-protection.conf": """[Service]
OOMScoreAdjust=-900""",
            "user-.slice.d/90-gridmind-memory-budget.conf": """[Slice]
MemoryAccounting=true
MemoryHigh=384M
MemoryMax=512M""",
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
                    [
                        "install",
                        "-D",
                        "-m",
                        "0644",
                        str(rendered_dir / name),
                        str(self.systemd_dir / name),
                    ]
                    for name in (*UNIT_NAMES, *DROP_IN_FILES)
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
        if action == "activate-next-cycle-plan":
            return [
                [
                    "systemctl",
                    "enable",
                    "--now",
                    "gridmind-next-cycle-plan.timer",
                ],
            ]
        if action == "activate-deadman-watchdog":
            return [
                [
                    "systemctl",
                    "enable",
                    "--now",
                    "gridmind-deadman-watchdog.timer",
                ],
            ]
        if action == "uninstall":
            return [
                ["systemctl", "disable", "--now", *UNIT_NAMES],
                *[
                    ["rm", "-f", str(self.systemd_dir / name)]
                    for name in (*UNIT_NAMES, *DROP_IN_FILES)
                ],
                ["systemctl", "daemon-reload"],
            ]
        raise ValueError(
            "action must be install-passive, activate-dashboard, "
            "activate-remote-access, activate-provider-readiness, "
            "activate-next-cycle-plan, activate-deadman-watchdog, or uninstall"
        )

    def apply(
        self, rendered_dir: Path, action: str, *, dry_run: bool = True
    ) -> dict[str, Any]:
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
            result = self.command_runner(
                command, capture_output=True, text=True, check=False
            )
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
