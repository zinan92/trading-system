import json
import subprocess
from pathlib import Path

from services.paper_predeploy_gate import PaperPredeployGate
from services.python_runtime_compatibility import LAUNCHD_API_SURFACE, LAUNCHD_IMPORT_TARGETS, LaunchdPythonCompatibility


def _probe(*, import_name: str = "", returncode: int = 0):
    imports = {name: "ok" for name in LAUNCHD_IMPORT_TARGETS}
    if import_name:
        imports[import_name] = "SyntaxError"
        returncode = 1
    return subprocess.CompletedProcess(
        ["/usr/bin/python3", "-c", "probe"],
        returncode,
        json.dumps({
            "version": [3, 9],
            "imports": imports,
            "api_surface": {name: "ok" for name in LAUNCHD_API_SURFACE},
        }),
        "incompatible import" if import_name else "",
    )


def test_paper_predeploy_gate_passes_and_records_the_compatibility_receipt(tmp_path: Path):
    compatibility = LaunchdPythonCompatibility(tmp_path, command_runner=lambda command, **kwargs: _probe())

    result = PaperPredeployGate(tmp_path, compatibility=compatibility).run()

    assert result["status"] == "pass"
    assert result["compatibility_receipt"]["status"] == "pass"
    assert not any(result["operations"].values())
    receipt = json.loads((tmp_path / "release_gates" / "paper_predeploy_current.json").read_text())[-1]
    assert receipt["schema_version"] == "paper-predeploy-gate-v1"
    assert receipt["compatibility_receipt"]["api_surface_results"]["do_GET"] == "ok"


def test_paper_predeploy_gate_blocks_an_incompatible_import_without_runtime_action(tmp_path: Path):
    compatibility = LaunchdPythonCompatibility(
        tmp_path,
        command_runner=lambda command, **kwargs: _probe(import_name="pipelines.dashboard_server"),
    )

    result = PaperPredeployGate(tmp_path, compatibility=compatibility).run()

    assert result["status"] == "blocked"
    assert result["reason"] == "launchd_python_compatibility_failed"
    assert result["compatibility_receipt"]["import_results"]["pipelines.dashboard_server"] == "SyntaxError"
    assert not any(result["operations"].values())
