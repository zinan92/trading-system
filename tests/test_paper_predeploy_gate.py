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
    compatibility = LaunchdPythonCompatibility(
        tmp_path,
        interpreter="/usr/bin/python3",
        command_runner=lambda command, **kwargs: _probe(),
    )

    result = PaperPredeployGate(
        tmp_path,
        compatibility=compatibility,
        source_sha_resolver=lambda: "a" * 40,
    ).run()

    assert result["status"] == "pass", result
    assert result["compatibility_receipt"]["status"] == "pass"
    assert not any(result["operations"].values())
    receipt = json.loads((tmp_path / "release_gates" / "paper_predeploy_current.json").read_text())[-1]
    assert receipt["schema_version"] == "paper-predeploy-gate-v3"
    assert receipt["source_sha"] == "a" * 40
    assert receipt["source_tree_sha"] == "a" * 40
    assert receipt["tracked_tree_clean"] is True
    assert receipt["max_age_seconds"] == 900
    target = receipt["compatibility_receipt"]["targets"][0]
    assert target["api_surface_results"]["do_GET"] == "ok"


def test_paper_predeploy_gate_blocks_an_incompatible_import_without_runtime_action(tmp_path: Path):
    compatibility = LaunchdPythonCompatibility(
        tmp_path,
        interpreter="/usr/bin/python3",
        command_runner=lambda command, **kwargs: _probe(import_name="pipelines.dashboard_server"),
    )

    result = PaperPredeployGate(
        tmp_path,
        compatibility=compatibility,
        source_sha_resolver=lambda: "a" * 40,
    ).run()

    assert result["status"] == "blocked"
    assert result["reason"] == "launchd_python_compatibility_failed"
    target = result["compatibility_receipt"]["targets"][0]
    assert target["import_results"]["pipelines.dashboard_server"] == "SyntaxError"
    assert not any(result["operations"].values())


def test_paper_predeploy_gate_writes_blocked_receipt_when_source_sha_resolution_raises(tmp_path: Path):
    compatibility = LaunchdPythonCompatibility(
        tmp_path,
        interpreter="/usr/bin/python3",
        command_runner=lambda command, **kwargs: _probe(),
    )

    result = PaperPredeployGate(
        tmp_path,
        compatibility=compatibility,
        source_sha_resolver=lambda: (_ for _ in ()).throw(RuntimeError("git unavailable")),
    ).run()

    assert result["status"] == "blocked"
    assert result["reason"] == "paper_predeploy_exception"
    assert result["source_sha"] == ""
    receipt = json.loads((tmp_path / "release_gates" / "paper_predeploy_current.json").read_text())[-1]
    assert receipt["status"] == "blocked"


def test_paper_predeploy_gate_blocks_a_dirty_tracked_tree(tmp_path: Path):
    compatibility = LaunchdPythonCompatibility(
        tmp_path,
        interpreter="/usr/bin/python3",
        command_runner=lambda command, **kwargs: _probe(),
    )
    result = PaperPredeployGate(
        tmp_path,
        compatibility=compatibility,
        source_attestation_resolver=lambda: {
            "source_sha": "a" * 40,
            "source_tree_sha": "c" * 40,
            "tracked_tree_clean": False,
        },
    ).run()

    assert result["status"] == "blocked"
    assert result["reason"] == "tracked_source_tree_dirty"
    assert result["tracked_tree_clean"] is False
