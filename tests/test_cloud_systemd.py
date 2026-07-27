from __future__ import annotations

from pathlib import Path

from services.cloud_systemd import (
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
        path.name: path.read_text(encoding="utf-8")
        for path in (tmp_path / "rendered").iterdir()
    }
    all_text = "\n".join(rendered.values())
    assert "/Users/" not in all_text
    assert "--host 127.0.0.1 --port 8100" in rendered["gridmind-datafeed.service"]
    assert "--host 127.0.0.1 --port 8765" in rendered["gridmind-dashboard.service"]
    assert "pipelines.cloud_service_boot --service dashboard" in rendered[
        "gridmind-dashboard.service"
    ]
    assert "pipelines.cloud_service_boot --service datafeed" not in rendered[
        "gridmind-datafeed.service"
    ]
    assert "Environment=PYTHONPATH=/opt/gridmind/src/trading-system" in all_text
    assert "pipelines.cloud_service_boot --service dualtrack-live-tick" in rendered[
        "gridmind-live-tick.service"
    ]
    assert "pipelines.cloud_daily_self_review" in rendered[
        "gridmind-daily-self-review.service"
    ]
    assert "OnCalendar=*-*-* 17:10:00 UTC" in rendered[
        "gridmind-daily-self-review.timer"
    ]
    timer = rendered["gridmind-live-tick.timer"]
    assert "OnUnitActiveSec=60" in timer
    assert "Persistent=true" in timer
    assert "Unit=gridmind-live-tick.service" in timer
    assert "Type=oneshot" in rendered["gridmind-live-tick.service"]


def test_passive_install_does_not_enable_scheduler(tmp_path: Path):
    rendered = tmp_path / "rendered"
    rendered.mkdir()
    installer = CloudSystemdInstaller(systemd_dir=tmp_path / "systemd")

    result = installer.apply(rendered, "install-passive", dry_run=True)

    assert result["status"] == "dry_run"
    assert result["scheduler_active"] is False
    command_text = [" ".join(command) for command in result["commands"]]
    assert any("enable --now gridmind-datafeed.service" in row for row in command_text)
    assert not any("enable --now gridmind-dashboard.service" in row for row in command_text)
    assert not any("enable --now gridmind-live-tick.timer" in row for row in command_text)

    dashboard = installer.apply(rendered, "activate-dashboard", dry_run=True)
    assert dashboard["commands"] == [
        ["systemctl", "enable", "--now", "gridmind-dashboard.service"]
    ]


def test_uninstall_never_targets_persistent_state(tmp_path: Path):
    installer = CloudSystemdInstaller(systemd_dir=tmp_path / "systemd")

    commands = installer.plan(tmp_path / "rendered", "uninstall")
    command_text = "\n".join(" ".join(command) for command in commands)

    assert "/var/lib/gridmind" not in command_text
    assert "/etc/gridmind/runtime.env" not in command_text
    assert "/etc/gridmind/paper.env" not in command_text
    assert all(
        command[0] in {"systemctl", "rm"}
        for command in commands
    )
