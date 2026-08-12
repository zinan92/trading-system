from __future__ import annotations

from pathlib import Path

from services.cloud_systemd import (
    DROP_IN_FILES,
    UNIT_NAMES,
    CloudSystemdInstaller,
    CloudSystemdPaths,
    CloudSystemdRenderer,
)


def paths(tmp_path: Path) -> CloudSystemdPaths:
    return CloudSystemdPaths(
        repo_root=Path("/opt/gridmind/src/trading-system"),
        datafeed_root=Path("/opt/gridmind/src/datafeed"),
        app_python=Path("/opt/gridmind/venvs/app/bin/python"),
        datafeed_python=Path("/opt/gridmind/venvs/datafeed/bin/python"),
        systemd_dir=tmp_path / "systemd",
    )


def test_renderer_emits_loopback_source_gated_non_overlapping_units(tmp_path: Path):
    result = CloudSystemdRenderer(paths(tmp_path)).render(tmp_path / "rendered")

    assert result["units"] == sorted(UNIT_NAMES)
    assert result["scheduler_active"] is False
    rendered = {
        str(path.relative_to(tmp_path / "rendered")): path.read_text(encoding="utf-8")
        for path in (tmp_path / "rendered").rglob("*")
        if path.is_file()
    }
    all_text = "\n".join(rendered.values())
    assert "/Users/" not in all_text
    assert "--host 127.0.0.1 --port 8100" in rendered["gridmind-datafeed.service"]
    assert "--host 127.0.0.1 --port 8765" in rendered["gridmind-dashboard.service"]
    assert "--host 127.0.0.1 --port 8766" in rendered["gridmind-access-gateway.service"]
    assert "http://127.0.0.1:8766" not in rendered["gridmind-cloudflared.service"]
    assert "/etc/gridmind/cloudflared.yml" in rendered["gridmind-cloudflared.service"]
    assert (
        "pipelines.cloud_service_boot --service dashboard"
        in rendered["gridmind-dashboard.service"]
    )
    assert (
        "pipelines.cloud_service_boot --service datafeed"
        not in rendered["gridmind-datafeed.service"]
    )
    assert "Environment=PYTHONPATH=/opt/gridmind/src/trading-system" in all_text
    assert (
        "pipelines.cloud_service_boot --service dualtrack-live-tick"
        in rendered["gridmind-live-tick.service"]
    )
    assert (
        "ReadWritePaths=/opt/gridmind/.codex" in rendered["gridmind-live-tick.service"]
    )
    assert (
        "ReadWritePaths=/opt/gridmind/.codex"
        not in rendered["gridmind-dashboard.service"]
    )
    assert (
        "pipelines.cloud_daily_self_review"
        in rendered["gridmind-daily-self-review.service"]
    )
    assert "OnCalendar=*-*-* 01:03:00 UTC" in rendered["gridmind-daily-24h.timer"]
    assert (
        "OnCalendar=*-*-* 01:10:00 UTC" in rendered["gridmind-daily-self-review.timer"]
    )
    assert (
        "pipelines.cloud_backup create --keep 7" in rendered["gridmind-backup.service"]
    )
    assert "OnCalendar=*-*-* 01:30:00 UTC" in rendered["gridmind-backup.timer"]
    timer = rendered["gridmind-live-tick.timer"]
    assert "OnUnitInactiveSec=60" in timer
    assert "OnUnitActiveSec" not in timer
    assert "Persistent=true" in timer
    assert "Unit=gridmind-live-tick.service" in timer
    assert "Type=oneshot" in rendered["gridmind-live-tick.service"]
    deadman_timer = rendered["gridmind-deadman-ping.timer"]
    assert "OnUnitInactiveSec=300" in deadman_timer
    assert "OnUnitActiveSec" not in deadman_timer
    deadman_watchdog = rendered["gridmind-deadman-watchdog.service"]
    deadman_watchdog_timer = rendered["gridmind-deadman-watchdog.timer"]
    assert "pipelines.cloud_deadman_watchdog" in deadman_watchdog
    assert "MemoryMax=64M" in deadman_watchdog
    assert "OnUnitInactiveSec=60" in deadman_watchdog_timer
    assert "Unit=gridmind-deadman-watchdog.service" in deadman_watchdog_timer
    readiness_service = rendered["gridmind-ai-provider-readiness.service"]
    readiness_timer = rendered["gridmind-ai-provider-readiness.timer"]
    assert "User=gridmind" in readiness_service
    assert "Type=oneshot" in readiness_service
    assert "ReadWritePaths=/opt/gridmind/.codex" in readiness_service
    assert (
        "pipelines.cloud_service_boot --service ai-provider-readiness"
        in readiness_service
    )
    assert (
        "pipelines.cloud_ai_provider_readiness --renew-if-due --json"
        in readiness_service
    )
    assert "OnBootSec=120" in readiness_timer
    assert "OnUnitInactiveSec=300" in readiness_timer
    assert "OnUnitActiveSec" not in readiness_timer
    assert "Unit=gridmind-ai-provider-readiness.service" in readiness_timer
    next_cycle_service = rendered["gridmind-next-cycle-plan.service"]
    next_cycle_timer = rendered["gridmind-next-cycle-plan.timer"]
    assert "Type=oneshot" in next_cycle_service
    assert "ReadWritePaths=/opt/gridmind/.codex" in next_cycle_service
    assert (
        "pipelines.cloud_service_boot --service next-cycle-plan" in next_cycle_service
    )
    assert "pipelines.paper_next_cycle_plan --json" in next_cycle_service
    assert "TimeoutStartSec=120" in next_cycle_service
    assert "OnUnitInactiveSec=300" in next_cycle_timer
    assert "OnUnitActiveSec" not in next_cycle_timer
    assert "Unit=gridmind-next-cycle-plan.service" in next_cycle_timer
    assert result["drop_ins"] == sorted(DROP_IN_FILES)
    assert (
        rendered["ssh.service.d/90-gridmind-oom-protection.conf"]
        == "[Service]\nOOMScoreAdjust=-900\n"
    )
    assert (
        rendered["user-.slice.d/90-gridmind-memory-budget.conf"]
        == "[Slice]\nMemoryAccounting=true\nMemoryHigh=384M\nMemoryMax=512M\n"
    )
    assert "OOMScoreAdjust=-900" in rendered["gridmind-cloudflared.service"]
    expected_limits = {
        "gridmind-datafeed.service": "MemoryMax=768M",
        "gridmind-dashboard.service": "MemoryMax=256M",
        "gridmind-access-gateway.service": "MemoryMax=96M",
        "gridmind-live-tick.service": "MemoryMax=512M",
        "gridmind-daily-24h.service": "MemoryMax=128M",
        "gridmind-daily-self-review.service": "MemoryMax=128M",
        "gridmind-backup.service": "MemoryMax=384M",
        "gridmind-deadman-ping.service": "MemoryMax=192M",
        "gridmind-ai-provider-readiness.service": "MemoryMax=128M",
        "gridmind-next-cycle-plan.service": "MemoryMax=128M",
        "gridmind-deadman-watchdog.service": "MemoryMax=64M",
    }
    for unit, limit in expected_limits.items():
        assert limit in rendered[unit]
        assert "OnFailure=gridmind-unit-failure-alert@%n.service" in rendered[unit]
    assert "MemoryMax=128M" in rendered["gridmind-cloudflared.service"]
    assert "MemoryHigh=384M" in rendered["gridmind-live-tick.service"]
    assert "MemoryHigh=256M" in rendered["gridmind-backup.service"]
    failure_alert = rendered["gridmind-unit-failure-alert@.service"]
    assert "pipelines.cloud_unit_failure_alert --failed-unit %i" in failure_alert
    assert "MemoryMax=64M" in failure_alert
    assert "OnFailure=" not in failure_alert


def test_passive_install_does_not_enable_scheduler(tmp_path: Path):
    rendered = tmp_path / "rendered"
    rendered.mkdir()
    installer = CloudSystemdInstaller(systemd_dir=tmp_path / "systemd")

    result = installer.apply(rendered, "install-passive", dry_run=True)

    assert result["status"] == "dry_run"
    assert result["scheduler_active"] is False
    command_text = [" ".join(command) for command in result["commands"]]
    assert any("enable --now gridmind-datafeed.service" in row for row in command_text)
    assert not any(
        "enable --now gridmind-dashboard.service" in row for row in command_text
    )
    assert not any(
        "enable --now gridmind-live-tick.timer" in row for row in command_text
    )
    assert not any(
        "enable --now gridmind-ai-provider-readiness.timer" in row
        for row in command_text
    )
    assert any(
        "ssh.service.d/90-gridmind-oom-protection.conf" in row
        and "install -D -m 0644" in row
        for row in command_text
    )

    dashboard = installer.apply(rendered, "activate-dashboard", dry_run=True)
    assert dashboard["commands"] == [
        ["systemctl", "enable", "--now", "gridmind-dashboard.service"]
    ]
    remote = installer.apply(
        rendered,
        "activate-remote-access",
        dry_run=True,
    )
    assert remote["commands"] == [
        [
            "systemctl",
            "enable",
            "--now",
            "gridmind-access-gateway.service",
            "gridmind-cloudflared.service",
        ]
    ]
    readiness = installer.apply(
        rendered,
        "activate-provider-readiness",
        dry_run=True,
    )
    assert readiness["commands"] == [
        [
            "systemctl",
            "enable",
            "--now",
            "gridmind-ai-provider-readiness.timer",
        ]
    ]
    next_cycle = installer.apply(
        rendered,
        "activate-next-cycle-plan",
        dry_run=True,
    )
    assert next_cycle["commands"] == [
        [
            "systemctl",
            "enable",
            "--now",
            "gridmind-next-cycle-plan.timer",
        ]
    ]
    watcher = installer.apply(
        rendered,
        "activate-deadman-watchdog",
        dry_run=True,
    )
    assert watcher["commands"] == [
        [
            "systemctl",
            "enable",
            "--now",
            "gridmind-deadman-watchdog.timer",
        ]
    ]


def test_uninstall_never_targets_persistent_state(tmp_path: Path):
    installer = CloudSystemdInstaller(systemd_dir=tmp_path / "systemd")

    commands = installer.plan(tmp_path / "rendered", "uninstall")
    command_text = "\n".join(" ".join(command) for command in commands)

    assert "/var/lib/gridmind" not in command_text
    assert "/etc/gridmind/runtime.env" not in command_text
    assert "/etc/gridmind/paper.env" not in command_text
    assert all(command[0] in {"systemctl", "rm"} for command in commands)
