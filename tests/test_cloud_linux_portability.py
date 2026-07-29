from pathlib import Path

from services.cloud_linux_portability import (
    CLOUD_RUNTIME_PATHS,
    audit_cloud_runtime,
)
from services.config_loader import ROOT


def test_repository_cloud_runtime_is_linux_portable() -> None:
    result = audit_cloud_runtime(ROOT)

    assert result["status"] == "pass"
    assert result["missing_paths"] == []
    assert result["violations"] == []
    assert result["file_count"] >= len(CLOUD_RUNTIME_PATHS)


def test_cloud_runtime_audit_reports_exact_forbidden_path(tmp_path: Path) -> None:
    for relative in CLOUD_RUNTIME_PATHS:
        path = tmp_path / relative
        if Path(relative).suffix:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# portable\n", encoding="utf-8")
        else:
            path.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "configs" / "dualtrack.yaml"
    target.write_text('{"command": "/opt/homebrew/bin/codex"}\n', encoding="utf-8")

    result = audit_cloud_runtime(tmp_path)

    assert result["status"] == "blocked"
    assert result["missing_paths"] == []
    assert result["violations"] == [
        {
            "code": "mac_homebrew",
            "path": "configs/dualtrack.yaml",
            "line": 1,
            "marker": "/opt/homebrew",
        }
    ]
