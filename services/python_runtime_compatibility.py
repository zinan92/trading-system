"""Verify the interpreters actually configured for local launchd Paper jobs."""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import write_json


DEFAULT_LAUNCHD_PYTHON = "/usr/bin/python3"  # Explicit-probe compatibility only.
LAUNCHD_IMPORT_TARGETS = (
    "pipelines.park_control",
    "pipelines.dashboard_server",
    "schemas.portfolio",
    "pipelines.dualtrack_cycle_runner",
    "pipelines.trading_daily_24h_report",
    "services.schedule_manager",
    "services.strategy_control_plane",
)
LAUNCHD_API_SURFACE = (
    "do_GET",
    "do_POST",
    "_handle_trading_system_read_model",
)
NAUTILUS_IMPORT_TARGETS = ("nautilus_trader",)
PAPER_JOB_LABELS = (
    "com.wendy.trading-orchestrator.dashboard",
    "com.wendy.trading-orchestrator.dualtrack-live-tick",
)


class LaunchdPythonCompatibility:
    """A bounded, credential-free compatibility gate for local launchd."""

    def __init__(
        self,
        output_root: Optional[Path] = None,
        *,
        interpreter: Optional[str] = None,
        launch_agents_dir: Optional[Path] = None,
        generated_launch_agents_dir: Optional[Path] = None,
        command_runner: Optional[Callable[..., subprocess.CompletedProcess]] = None,
    ) -> None:
        config = load_pipeline_config()
        self.output_root = Path(output_root or ROOT / str(config.get("output_root", "outputs")))
        self.explicit_interpreter = str(
            interpreter or os.getenv("TRADING_ORCHESTRATOR_LAUNCHD_PYTHON") or ""
        ).strip()
        self.launch_agents_dir = Path(launch_agents_dir or Path.home() / "Library" / "LaunchAgents")
        self.generated_launch_agents_dir = Path(
            generated_launch_agents_dir or self.output_root / "schedules" / "launch_agents"
        )
        self.command_runner = command_runner or subprocess.run

    def run(self) -> dict[str, Any]:
        checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        payload: dict[str, Any] = {
            "schema_version": "launchd-python-compatibility-v2",
            "checked_at": checked_at,
            "status": "failed",
            "targets": [],
        }
        try:
            targets = self._targets()
            results = [self._probe_target(target) for target in targets]
            payload["targets"] = results
            failed = [row for row in results if row.get("status") != "pass"]
            if failed:
                payload.update({
                    "reason": "launchd_python_target_failed",
                    "failed_target_ids": [str(row.get("target_id") or "") for row in failed],
                    "next_action": (
                        "Fix every reported job or Nautilus interpreter before restarting "
                        "a launchd Paper service."
                    ),
                })
            else:
                payload["status"] = "pass"
        except Exception as exc:  # noqa: BLE001 - every discovery/probe failure needs a receipt.
            payload.update({
                "status": "failed",
                "reason": "launchd_python_compatibility_exception",
                "detail": f"{type(exc).__name__}: {exc}",
                "next_action": (
                    "Fix the plist discovery or compatibility-probe failure before restarting "
                    "a launchd Paper service."
                ),
            })
        write_json(
            self.output_root / "runtime_compatibility" / "launchd_python_current.json",
            [payload],
        )
        return payload

    def _targets(self) -> list[dict[str, Any]]:
        if self.explicit_interpreter:
            return [{
                "target_id": "explicit_python",
                "kind": "explicit_interpreter",
                "label": "",
                "plist_path": "",
                "configured_executable": self.explicit_interpreter,
                "resolved_interpreter": self.explicit_interpreter,
                "probe_profile": "paper_application",
            }]

        targets: list[dict[str, Any]] = []
        nautilus_sources: dict[str, list[str]] = {}
        for label in PAPER_JOB_LABELS:
            plist_path = self._plist_path(label)
            with plist_path.open("rb") as handle:
                job = plistlib.load(handle)
            arguments = job.get("ProgramArguments")
            if not isinstance(arguments, list) or not arguments or not str(arguments[0]).strip():
                raise ValueError(f"{label} plist has no ProgramArguments executable")
            environment = job.get("EnvironmentVariables")
            environment = environment if isinstance(environment, dict) else {}
            configured = str(arguments[0]).strip()
            resolved = _resolve_executable(configured, str(environment.get("PATH") or os.defpath))
            targets.append({
                "target_id": label,
                "kind": "launchd_job",
                "label": label,
                "plist_path": str(plist_path),
                "configured_executable": configured,
                "resolved_interpreter": resolved,
                "probe_profile": "paper_application",
            })
            nautilus = str(environment.get("TRADING_ORCHESTRATOR_NAUTILUS_PYTHON") or "").strip()
            if nautilus:
                nautilus_sources.setdefault(nautilus, []).append(label)

        for index, (interpreter, labels) in enumerate(sorted(nautilus_sources.items()), start=1):
            targets.append({
                "target_id": f"nautilus_dependency_{index}",
                "kind": "nautilus_dependency",
                "label": "",
                "source_job_labels": labels,
                "plist_path": "",
                "configured_executable": interpreter,
                "resolved_interpreter": _resolve_executable(interpreter, os.defpath),
                "probe_profile": "nautilus_dependency",
            })
        return targets

    def _plist_path(self, label: str) -> Path:
        installed = self.launch_agents_dir / f"{label}.plist"
        if installed.is_file():
            return installed
        generated = self.generated_launch_agents_dir / f"{label}.plist"
        if generated.is_file():
            return generated
        raise FileNotFoundError(f"launchd plist missing for {label}")

    def _probe_target(self, target: dict[str, Any]) -> dict[str, Any]:
        profile = str(target["probe_profile"])
        imports = NAUTILUS_IMPORT_TARGETS if profile == "nautilus_dependency" else LAUNCHD_IMPORT_TARGETS
        api_surface = () if profile == "nautilus_dependency" else LAUNCHD_API_SURFACE
        command = [
            str(target["resolved_interpreter"]),
            "-c",
            _probe_program(imports, api_surface, root=ROOT),
        ]
        row = {
            **target,
            "status": "failed",
            "imports": list(imports),
            "api_surface": list(api_surface),
        }
        try:
            result = self.command_runner(
                command,
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                check=False,
            )
        except Exception as exc:  # noqa: BLE001 - preserve a failed target receipt.
            row.update({
                "reason": "interpreter_probe_exception",
                "detail": f"{type(exc).__name__}: {exc}",
            })
            return row

        parsed = _probe_payload(result.stdout)
        observed = parsed.get("version") if isinstance(parsed, dict) else None
        import_results = parsed.get("imports") if isinstance(parsed, dict) else None
        compile_results = parsed.get("compileall") if isinstance(parsed, dict) else None
        api_results = parsed.get("api_surface") if isinstance(parsed, dict) else None
        compile_ok = isinstance(compile_results, dict) and all(
            compile_results.get(name) == "ok" for name in imports
        )
        imports_ok = (
            isinstance(import_results, dict)
            and all(import_results.get(name) == "ok" for name in imports)
        )
        api_ok = (
            not api_surface
            or (
                isinstance(api_results, dict)
                and all(api_results.get(name) == "ok" for name in api_surface)
            )
        )
        if result.returncode == 0 and isinstance(observed, list) and compile_ok and imports_ok and api_ok:
            row.update({
                "status": "pass",
                "observed_version": observed,
                "compileall_results": compile_results,
                "import_results": import_results,
                "api_surface_results": api_results if isinstance(api_results, dict) else {},
            })
        else:
            row.update({
                "reason": "interpreter_import_or_api_failed",
                "observed_version": observed,
                "compileall_results": compile_results if isinstance(compile_results, dict) else {},
                "import_results": import_results if isinstance(import_results, dict) else {},
                "api_surface_results": api_results if isinstance(api_results, dict) else {},
                "returncode": result.returncode,
                "stderr_tail": str(result.stderr or "")[-600:],
            })
        return row


def _resolve_executable(configured: str, path_value: str) -> str:
    candidate = Path(configured).expanduser()
    if candidate.is_absolute() or "/" in configured:
        return str(candidate)
    return str(shutil.which(configured, path=path_value) or configured)


def _probe_program(
    import_targets: tuple[str, ...],
    api_targets: tuple[str, ...],
    *,
    root: Path,
) -> str:
    targets = json.dumps(list(import_targets))
    api_surface = json.dumps(list(api_targets))
    source_root = json.dumps(str(root))
    return (
        "import compileall\n"
        "import importlib\n"
        "import json\n"
        "from pathlib import Path\n"
        "import sys\n"
        f"targets = {targets}\n"
        f"source_root = Path({source_root})\n"
        "compile_results = {}\n"
        "for name in targets:\n"
        "    source = source_root / Path(*name.split('.')).with_suffix('.py')\n"
        "    try:\n"
        "        compile_results[name] = 'ok' if compileall.compile_file(str(source), quiet=1) else 'failed'\n"
        "    except Exception as exc:\n"
        "        compile_results[name] = type(exc).__name__\n"
        "if tuple(sys.version_info[:2]) < (3, 10) and 'schemas.portfolio' in compile_results:\n"
        "    compile_results['schemas.portfolio'] = 'failed: schemas/portfolio.py requires Python 3.10+ type-union syntax'\n"
        "results = {}\n"
        "for name in targets:\n"
        "    try:\n"
        "        importlib.import_module(name)\n"
        "        results[name] = 'ok'\n"
        "    except Exception as exc:\n"
        "        results[name] = type(exc).__name__\n"
        f"api_targets = {api_surface}\n"
        "api_results = {}\n"
        "if api_targets:\n"
        "    try:\n"
        "        from pipelines.dashboard_server import DashboardHandler\n"
        "        for name in api_targets:\n"
        "            api_results[name] = 'ok' if callable(getattr(DashboardHandler, name, None)) else 'missing'\n"
        "    except Exception as exc:\n"
        "        api_results = {name: type(exc).__name__ for name in api_targets}\n"
        "print(json.dumps({'version': list(sys.version_info[:2]), 'compileall': compile_results, 'imports': results, 'api_surface': api_results}, sort_keys=True))\n"
        "sys.exit(0 if all(value == 'ok' for value in compile_results.values()) and all(value == 'ok' for value in results.values()) and all(value == 'ok' for value in api_results.values()) else 1)\n"
    )


def _probe_payload(stdout: Any) -> dict[str, Any]:
    try:
        value = json.loads(str(stdout or "").strip())
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}
