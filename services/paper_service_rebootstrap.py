"""Narrow, source-gated recovery for allowlisted Paper launchd services."""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import write_json
from services.paper_release_receipt import PaperReleaseReceiptGate


PAPER_REBOOTSTRAP_LABELS = (
    "com.wendy.trading-orchestrator.dashboard",
    "com.wendy.trading-orchestrator.dualtrack-live-tick",
)


class PaperServiceRebootstrap:
    """Bootout/bootstrap one allowlisted Paper label after release verification."""

    def __init__(
        self,
        output_root: Optional[Path] = None,
        *,
        launch_agents_dir: Optional[Path] = None,
        command_runner: Optional[Callable[..., subprocess.CompletedProcess]] = None,
        release_gate_validator: Optional[Callable[[], dict[str, Any]]] = None,
        uid: Optional[int] = None,
    ) -> None:
        config = load_pipeline_config()
        self.output_root = Path(output_root or ROOT / str(config.get("output_root", "outputs")))
        self.launch_agents_dir = Path(launch_agents_dir or Path.home() / "Library" / "LaunchAgents")
        self.command_runner = command_runner or subprocess.run
        self.release_gate_validator = release_gate_validator or PaperReleaseReceiptGate(
            self.output_root
        ).verify
        self.uid = int(os.getuid() if uid is None else uid)

    def run(self, label: str) -> dict[str, Any]:
        recorded_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        base: dict[str, Any] = {
            "schema_version": "paper-service-rebootstrap-v1",
            "recorded_at": recorded_at,
            "label": str(label),
            "status": "blocked",
            "commands": [],
        }
        if label not in PAPER_REBOOTSTRAP_LABELS:
            return self._write({**base, "blocker": "paper_service_label_not_allowlisted"})

        release_gate = self.release_gate_validator()
        if not release_gate.get("ok"):
            return self._write({
                **base,
                "blocker": str(release_gate.get("blocker") or "paper_predeploy_gate_failed"),
                "release_gate": release_gate,
            })

        plist_path = self.launch_agents_dir / f"{label}.plist"
        try:
            with plist_path.open("rb") as handle:
                payload = plistlib.load(handle)
            if str(payload.get("Label") or "") != label:
                raise ValueError("installed plist label mismatch")
        except Exception as exc:  # noqa: BLE001 - invalid local service definition blocks.
            return self._write({
                **base,
                "blocker": "paper_service_plist_invalid",
                "detail": f"{type(exc).__name__}: {exc}",
                "plist_path": str(plist_path),
                "release_gate": release_gate,
            })

        service = f"gui/{self.uid}/{label}"
        before = self._launchctl(["launchctl", "print", service])
        bootout = self._launchctl(["launchctl", "bootout", service])
        commands = [
            self._command_receipt("print_before", before),
            self._command_receipt("bootout", bootout),
        ]
        if bootout.returncode not in {0, 3, 113}:
            return self._write({
                **base,
                "blocker": "paper_service_bootout_failed",
                "release_gate": release_gate,
                "plist_path": str(plist_path),
                "before": _launchd_state(before),
                "commands": commands,
            })

        bootstrap = self._launchctl(
            ["launchctl", "bootstrap", f"gui/{self.uid}", str(plist_path)]
        )
        after = self._launchctl(["launchctl", "print", service])
        commands.extend([
            self._command_receipt("bootstrap", bootstrap),
            self._command_receipt("print_after", after),
        ])
        if bootstrap.returncode != 0:
            return self._write({
                **base,
                "blocker": "paper_service_bootstrap_failed",
                "release_gate": release_gate,
                "plist_path": str(plist_path),
                "before": _launchd_state(before),
                "after": _launchd_state(after),
                "commands": commands,
            })
        return self._write({
            **base,
            "status": "pass",
            "blocker": "",
            "release_gate": release_gate,
            "plist_path": str(plist_path),
            "before": _launchd_state(before),
            "after": _launchd_state(after),
            "commands": commands,
        })

    def _launchctl(self, command: list[str]) -> subprocess.CompletedProcess:
        return self.command_runner(
            command,
            capture_output=True,
            text=True,
            check=False,
        )

    @staticmethod
    def _command_receipt(name: str, result: subprocess.CompletedProcess) -> dict[str, Any]:
        return {
            "name": name,
            "command": [str(item) for item in result.args],
            "returncode": int(result.returncode),
            "stderr_tail": str(result.stderr or "")[-300:],
        }

    def _write(self, payload: dict[str, Any]) -> dict[str, Any]:
        write_json(
            self.output_root / "release_gates" / "paper_service_rebootstrap_current.json",
            [payload],
        )
        return payload


def _launchd_state(result: subprocess.CompletedProcess) -> dict[str, Any]:
    text = str(result.stdout or "")
    state = re.search(r"^\s*state = (.+)$", text, flags=re.MULTILINE)
    runs = re.search(r"^\s*runs = (\d+)$", text, flags=re.MULTILINE)
    pid = re.search(r"^\s*pid = (\d+)$", text, flags=re.MULTILINE)
    exit_code = re.search(r"^\s*last exit code = (.+)$", text, flags=re.MULTILINE)
    return {
        "print_returncode": int(result.returncode),
        "state": state.group(1).strip() if state else "",
        "runs": int(runs.group(1)) if runs else None,
        "pid": int(pid.group(1)) if pid else None,
        "last_exit_code": exit_code.group(1).strip() if exit_code else "",
    }
