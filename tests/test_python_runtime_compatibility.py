import json
import subprocess
from pathlib import Path

from services.python_runtime_compatibility import (
    EXPECTED_LAUNCHD_VERSION,
    LAUNCHD_IMPORT_TARGETS,
    LaunchdPythonCompatibility,
)


def _complete_probe(*, imports=None, version=None, returncode=0, stderr=""):
    payload = {
        "version": list(version or EXPECTED_LAUNCHD_VERSION),
        "imports": imports or {name: "ok" for name in LAUNCHD_IMPORT_TARGETS},
    }
    return subprocess.CompletedProcess(
        ["/usr/bin/python3", "-c", "probe"],
        returncode,
        json.dumps(payload),
        stderr,
    )


def test_launchd_python_compatibility_writes_a_visible_passing_receipt(tmp_path: Path):
    commands = []

    def runner(command, **kwargs):
        commands.append((command, kwargs))
        return _complete_probe()

    result = LaunchdPythonCompatibility(tmp_path, command_runner=runner).run()

    assert result["status"] == "pass"
    assert result["observed_version"] == [3, 9]
    assert result["import_results"] == {name: "ok" for name in LAUNCHD_IMPORT_TARGETS}
    assert commands[0][0][:2] == ["/usr/bin/python3", "-c"]
    receipt = json.loads((tmp_path / "runtime_compatibility" / "launchd_python_current.json").read_text())[-1]
    assert receipt["status"] == "pass"
    assert receipt["interpreter"] == "/usr/bin/python3"


def test_launchd_python_compatibility_fails_explicitly_when_interpreter_is_unavailable(tmp_path: Path):
    def runner(command, **kwargs):
        raise FileNotFoundError("missing interpreter")

    result = LaunchdPythonCompatibility(tmp_path, command_runner=runner).run()

    assert result["status"] == "failed"
    assert result["reason"] == "launchd_interpreter_unavailable"
    assert "install or configure" in result["next_action"]


def test_launchd_python_compatibility_reports_the_specific_failed_import(tmp_path: Path):
    imports = {name: "ok" for name in LAUNCHD_IMPORT_TARGETS}
    imports["pipelines.dashboard_server"] = "SyntaxError"

    result = LaunchdPythonCompatibility(
        tmp_path,
        command_runner=lambda command, **kwargs: _complete_probe(imports=imports, returncode=1, stderr="SyntaxError"),
    ).run()

    assert result["status"] == "failed"
    assert result["reason"] == "launchd_python_import_or_version_failed"
    assert result["import_results"]["pipelines.dashboard_server"] == "SyntaxError"
    assert result["stderr_tail"] == "SyntaxError"
