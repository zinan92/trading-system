import json
import plistlib
import subprocess
from pathlib import Path

from services.python_runtime_compatibility import (
    LAUNCHD_API_SURFACE,
    LAUNCHD_IMPORT_TARGETS,
    NAUTILUS_IMPORT_TARGETS,
    LaunchdPythonCompatibility,
)


DASHBOARD = "com.wendy.trading-orchestrator.dashboard"
LIVE_TICK = "com.wendy.trading-orchestrator.dualtrack-live-tick"


def _write_plist(
    root: Path,
    label: str,
    executable: str,
    *,
    path_value: str = "",
    nautilus_python: str = "",
) -> None:
    environment = {}
    if path_value:
        environment["PATH"] = path_value
    if nautilus_python:
        environment["TRADING_ORCHESTRATOR_NAUTILUS_PYTHON"] = nautilus_python
    payload = {
        "Label": label,
        "ProgramArguments": [executable, "-m", "pipelines.dashboard_server"],
        "EnvironmentVariables": environment,
    }
    path = root / f"{label}.plist"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        plistlib.dump(payload, handle)


def _complete_probe(command, *, returncode=0, stderr=""):
    program = command[2]
    is_nautilus = "nautilus_trader" in program and "api_targets = []" in program
    imports = (
        {name: "ok" for name in NAUTILUS_IMPORT_TARGETS}
        if is_nautilus
        else {name: "ok" for name in LAUNCHD_IMPORT_TARGETS}
    )
    api_surface = {} if is_nautilus else {name: "ok" for name in LAUNCHD_API_SURFACE}
    payload = {
        "version": [3, 13] if is_nautilus else [3, 14],
        "compileall": {
            name: "ok"
            for name in (NAUTILUS_IMPORT_TARGETS if is_nautilus else LAUNCHD_IMPORT_TARGETS)
        },
        "imports": imports,
        "api_surface": api_surface,
    }
    return subprocess.CompletedProcess(command, returncode, json.dumps(payload), stderr)


def test_launchd_python_compatibility_resolves_each_job_path_and_nautilus_dependency(
    tmp_path: Path,
    monkeypatch,
):
    installed = tmp_path / "LaunchAgents"
    homebrew_bin = tmp_path / "homebrew-bin"
    homebrew_bin.mkdir()
    homebrew_python = homebrew_bin / "python3"
    homebrew_python.touch()
    homebrew_python.chmod(0o755)
    dashboard_python = tmp_path / "dashboard-python"
    dashboard_python.touch()
    nautilus_python = tmp_path / "nautilus-python"
    nautilus_python.touch()
    _write_plist(
        installed,
        DASHBOARD,
        str(dashboard_python),
        nautilus_python=str(nautilus_python),
    )
    _write_plist(
        installed,
        LIVE_TICK,
        "python3",
        path_value=str(homebrew_bin),
        nautilus_python=str(nautilus_python),
    )
    commands = []

    def runner(command, **kwargs):
        commands.append(command)
        return _complete_probe(command)

    result = LaunchdPythonCompatibility(
        tmp_path / "outputs",
        launch_agents_dir=installed,
        command_runner=runner,
    ).run()

    assert result["status"] == "pass", result
    assert [row["target_id"] for row in result["targets"]] == [
        DASHBOARD,
        LIVE_TICK,
        "nautilus_dependency_1",
    ]
    assert result["targets"][0]["resolved_interpreter"] == str(dashboard_python)
    assert result["targets"][1]["resolved_interpreter"] == str(homebrew_python)
    assert result["targets"][2]["resolved_interpreter"] == str(nautilus_python)
    assert result["targets"][2]["source_job_labels"] == [DASHBOARD, LIVE_TICK]
    assert [command[0] for command in commands] == [
        str(dashboard_python),
        str(homebrew_python),
        str(nautilus_python),
    ]
    receipt = json.loads(
        (tmp_path / "outputs" / "runtime_compatibility" / "launchd_python_current.json").read_text()
    )[-1]
    assert receipt["schema_version"] == "launchd-python-compatibility-v2"
    assert receipt["status"] == "pass"


def test_launchd_python_compatibility_falls_back_to_generated_plists(tmp_path: Path):
    generated = tmp_path / "generated"
    _write_plist(generated, DASHBOARD, "/runtime/dashboard-python")
    _write_plist(generated, LIVE_TICK, "/runtime/tick-python")

    result = LaunchdPythonCompatibility(
        tmp_path / "outputs",
        launch_agents_dir=tmp_path / "missing-installed",
        generated_launch_agents_dir=generated,
        command_runner=lambda command, **kwargs: _complete_probe(command),
    ).run()

    assert result["status"] == "pass", result
    assert all(row["plist_path"].startswith(str(generated)) for row in result["targets"])


def test_launchd_python_compatibility_records_non_oserror_probe_failure(tmp_path: Path):
    def runner(command, **kwargs):
        raise RuntimeError("probe runner exploded")

    result = LaunchdPythonCompatibility(
        tmp_path,
        interpreter="/runtime/python",
        command_runner=runner,
    ).run()

    assert result["status"] == "failed"
    assert result["reason"] == "launchd_python_target_failed"
    assert result["targets"][0]["reason"] == "interpreter_probe_exception"
    assert "RuntimeError" in result["targets"][0]["detail"]
    receipt = json.loads(
        (tmp_path / "runtime_compatibility" / "launchd_python_current.json").read_text()
    )[-1]
    assert receipt["status"] == "failed"


def test_launchd_python_compatibility_records_discovery_failure(tmp_path: Path):
    result = LaunchdPythonCompatibility(
        tmp_path / "outputs",
        launch_agents_dir=tmp_path / "missing-installed",
        generated_launch_agents_dir=tmp_path / "missing-generated",
    ).run()

    assert result["status"] == "failed"
    assert result["reason"] == "launchd_python_compatibility_exception"
    assert "FileNotFoundError" in result["detail"]
