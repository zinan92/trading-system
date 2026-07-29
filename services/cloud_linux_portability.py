"""Executable architecture check for the Linux Cloud runtime closure."""

from __future__ import annotations

from pathlib import Path
from typing import Any


CLOUD_RUNTIME_PATHS = (
    "configs/dualtrack.yaml",
    "deploy/cloud",
    "pipelines/cloud_aliyun.py",
    "pipelines/cloud_backup.py",
    "pipelines/cloud_daily_self_review.py",
    "pipelines/cloud_health.py",
    "pipelines/cloud_paper_preflight.py",
    "pipelines/cloud_service_boot.py",
    "pipelines/cloud_soak.py",
    "pipelines/cloud_systemd.py",
    "pipelines/dashboard_server.py",
    "pipelines/dualtrack_cycle_runner.py",
    "services/cloud_access_gateway.py",
    "services/cloud_aliyun.py",
    "services/cloud_backup.py",
    "services/cloud_daily_self_review.py",
    "services/cloud_health.py",
    "services/cloud_paper_preflight.py",
    "services/cloud_service_boot.py",
    "services/cloud_soak.py",
    "services/cloud_systemd.py",
    "services/codex_newsletter_strategy_proposal.py",
    "services/dualtrack_config.py",
    "services/dualtrack_machine_plan.py",
    "services/strategy_recommendation.py",
)

FORBIDDEN_CLOUD_MARKERS = {
    "mac_homebrew": "/opt/homebrew",
    "personal_macos_home": "/Users/",
    "mac_system_python": "/usr/bin/python3",
    "mac_launch_agents": "Library/LaunchAgents",
    "mac_launchctl": "launchctl",
    "mac_osascript": "osascript",
}


def audit_cloud_runtime(repo_root: Path) -> dict[str, Any]:
    """Return every macOS/personal-path dependency in the declared Cloud closure."""

    root = Path(repo_root)
    files: list[Path] = []
    missing: list[str] = []
    for relative in CLOUD_RUNTIME_PATHS:
        candidate = root / relative
        if candidate.is_dir():
            files.extend(
                path
                for path in sorted(candidate.rglob("*"))
                if path.is_file()
                and path.suffix in {".py", ".sh", ".json", ".env", ".example"}
            )
        elif candidate.is_file():
            files.append(candidate)
        else:
            missing.append(relative)

    violations: list[dict[str, Any]] = []
    for path in sorted(set(files)):
        relative = path.relative_to(root).as_posix()
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(),
            start=1,
        ):
            for code, marker in FORBIDDEN_CLOUD_MARKERS.items():
                if marker in line:
                    violations.append(
                        {
                            "code": code,
                            "path": relative,
                            "line": line_number,
                            "marker": marker,
                        }
                    )
    return {
        "schema_version": "cloud-linux-portability-v1",
        "status": "blocked" if missing or violations else "pass",
        "file_count": len(set(files)),
        "missing_paths": missing,
        "violations": violations,
    }
