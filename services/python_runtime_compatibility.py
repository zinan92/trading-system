"""Verify that the interpreter used by launchd can import its Paper services."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import write_json


DEFAULT_LAUNCHD_PYTHON = "/usr/bin/python3"
EXPECTED_LAUNCHD_VERSION = (3, 9)
LAUNCHD_IMPORT_TARGETS = (
    "pipelines.dashboard_server",
    "pipelines.dualtrack_cycle_runner",
    "pipelines.trading_daily_24h_report",
    "services.schedule_manager",
    "services.strategy_control_plane",
)


class LaunchdPythonCompatibility:
    """A bounded, credential-free compatibility gate for local launchd."""

    def __init__(
        self,
        output_root: Optional[Path] = None,
        *,
        interpreter: Optional[str] = None,
        command_runner: Optional[Callable[..., subprocess.CompletedProcess]] = None,
    ) -> None:
        config = load_pipeline_config()
        self.output_root = Path(output_root or ROOT / str(config.get("output_root", "outputs")))
        self.interpreter = str(
            interpreter or os.getenv("TRADING_ORCHESTRATOR_LAUNCHD_PYTHON") or DEFAULT_LAUNCHD_PYTHON
        )
        self.command_runner = command_runner or subprocess.run

    def run(self) -> dict[str, Any]:
        checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        command = [self.interpreter, "-c", _probe_program()]
        payload: dict[str, Any] = {
            "schema_version": "launchd-python-compatibility-v1",
            "checked_at": checked_at,
            "interpreter": self.interpreter,
            "expected_version": ".".join(str(part) for part in EXPECTED_LAUNCHD_VERSION),
            "imports": list(LAUNCHD_IMPORT_TARGETS),
        }
        try:
            result = self.command_runner(command, cwd=str(ROOT), capture_output=True, text=True, check=False)
        except OSError as exc:
            payload.update({
                "status": "failed",
                "reason": "launchd_interpreter_unavailable",
                "detail": str(exc),
                "next_action": "install or configure the local launchd Python 3.9 interpreter before deployment",
            })
        else:
            parsed = _probe_payload(result.stdout)
            observed = parsed.get("version") if isinstance(parsed, dict) else None
            imports = parsed.get("imports") if isinstance(parsed, dict) else None
            version_ok = observed == list(EXPECTED_LAUNCHD_VERSION)
            imports_ok = isinstance(imports, dict) and all(imports.get(name) == "ok" for name in LAUNCHD_IMPORT_TARGETS)
            if result.returncode == 0 and version_ok and imports_ok:
                payload.update({"status": "pass", "observed_version": observed, "import_results": imports})
            else:
                payload.update({
                    "status": "failed",
                    "reason": "launchd_python_import_or_version_failed",
                    "observed_version": observed,
                    "import_results": imports if isinstance(imports, dict) else {},
                    "returncode": result.returncode,
                    "stderr_tail": str(result.stderr or "")[-600:],
                    "next_action": "fix the Python 3.9 import or version failure before restarting a launchd Paper service",
                })
        path = self.output_root / "runtime_compatibility" / "launchd_python_current.json"
        write_json(path, [payload])
        return payload


def _probe_program() -> str:
    targets = json.dumps(list(LAUNCHD_IMPORT_TARGETS))
    return (
        "import importlib\n"
        "import json\n"
        "import sys\n"
        f"targets = {targets}\n"
        "results = {}\n"
        "for name in targets:\n"
        "    try:\n"
        "        importlib.import_module(name)\n"
        "        results[name] = 'ok'\n"
        "    except Exception as exc:\n"
        "        results[name] = type(exc).__name__\n"
        "print(json.dumps({'version': list(sys.version_info[:2]), 'imports': results}, sort_keys=True))\n"
        "sys.exit(0 if all(value == 'ok' for value in results.values()) else 1)\n"
    )


def _probe_payload(stdout: Any) -> dict[str, Any]:
    try:
        value = json.loads(str(stdout or "").strip())
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}
