import plistlib
import subprocess
from pathlib import Path

from services.paper_service_rebootstrap import PaperServiceRebootstrap


LABEL = "com.wendy.trading-orchestrator.dualtrack-live-tick"


def _plist(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{LABEL}.plist"
    with path.open("wb") as handle:
        plistlib.dump({"Label": LABEL, "ProgramArguments": ["python3"]}, handle)
    return path


def test_rebootstrap_rejects_non_allowlisted_label_without_commands(tmp_path: Path):
    commands = []
    result = PaperServiceRebootstrap(
        tmp_path / "outputs",
        launch_agents_dir=tmp_path / "LaunchAgents",
        command_runner=lambda command, **kwargs: commands.append(command),
        release_gate_validator=lambda: {"ok": True},
        uid=501,
    ).run("com.example.not-paper")

    assert result["blocker"] == "paper_service_label_not_allowlisted"
    assert commands == []


def test_rebootstrap_rejects_failed_release_gate_without_commands(tmp_path: Path):
    commands = []
    result = PaperServiceRebootstrap(
        tmp_path / "outputs",
        launch_agents_dir=tmp_path / "LaunchAgents",
        command_runner=lambda command, **kwargs: commands.append(command),
        release_gate_validator=lambda: {
            "ok": False,
            "blocker": "paper_predeploy_source_sha_mismatch",
        },
        uid=501,
    ).run(LABEL)

    assert result["blocker"] == "paper_predeploy_source_sha_mismatch"
    assert commands == []


def test_rebootstrap_mutates_only_the_requested_allowlisted_service(tmp_path: Path):
    launch_agents = tmp_path / "LaunchAgents"
    plist_path = _plist(launch_agents)
    commands = []

    def runner(command, **kwargs):
        commands.append(command)
        if command[:2] == ["launchctl", "print"]:
            return subprocess.CompletedProcess(
                command,
                0,
                "state = not running\nruns = 85\nlast exit code = 78: EX_CONFIG\n",
                "",
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    result = PaperServiceRebootstrap(
        tmp_path / "outputs",
        launch_agents_dir=launch_agents,
        command_runner=runner,
        release_gate_validator=lambda: {"ok": True, "source_sha": "a" * 40},
        uid=501,
    ).run(LABEL)

    service = f"gui/501/{LABEL}"
    assert result["status"] == "pass"
    assert commands == [
        ["launchctl", "print", service],
        ["launchctl", "bootout", service],
        ["launchctl", "bootstrap", "gui/501", str(plist_path)],
        ["launchctl", "print", service],
    ]
    assert result["before"]["runs"] == 85
    assert result["before"]["last_exit_code"] == "78: EX_CONFIG"
