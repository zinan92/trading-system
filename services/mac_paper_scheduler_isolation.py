"""Persistently isolate the Mac Paper schedule after Cloud ownership cutover."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from services.journal_store import write_json
from services.paper_release_receipt import PaperReleaseReceiptGate
from services.schedule_profiles import FOCUS_SCHEDULE_LABELS
from services.scheduler_ownership import LOCAL_OWNER_ID, SchedulerOwnershipStore


RESTORE_ACKNOWLEDGEMENT = (
    "I_UNDERSTAND_THIS_RESTORES_LOCAL_PAPER_SCHEDULERS"
)


class MacPaperSchedulerIsolation:
    """Disable both the current launchd job and its next-login bootstrap."""

    def __init__(
        self,
        output_root: Path,
        *,
        launch_agents_dir: Path | None = None,
        uid: int | None = None,
        command_runner: Callable[..., subprocess.CompletedProcess] | None = None,
        platform: str | None = None,
        now: Callable[[], datetime] | None = None,
        release_gate_validator: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.launch_agents_dir = (
            Path(launch_agents_dir)
            if launch_agents_dir is not None
            else Path.home() / "Library" / "LaunchAgents"
        )
        self.uid = int(os.getuid() if uid is None else uid)
        self.command_runner = command_runner or subprocess.run
        self.platform = str(platform or sys.platform)
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.release_gate_validator = (
            release_gate_validator
            or PaperReleaseReceiptGate(self.output_root).verify
        )

    def isolate(self) -> dict[str, Any]:
        owner = SchedulerOwnershipStore(self.output_root).current()
        blocker = self._isolation_owner_blocker(owner)
        release_gate = self._release_gate()
        if not release_gate.get("ok"):
            blocker = str(
                release_gate.get("blocker")
                or "paper_predeploy_gate_failed"
            )
        if self.platform != "darwin":
            blocker = "mac_scheduler_isolation_requires_darwin"
        before = self._snapshot() if not blocker else []
        operations: list[dict[str, Any]] = []
        if not blocker:
            for row in before:
                label = row["label"]
                operations.append(self._command(label, "disable"))
                if row["loaded"]:
                    operations.append(self._command(label, "bootout"))
        after = self._snapshot() if not blocker else []
        failures = [item for item in operations if not item["ok"]]
        verified = bool(after) and all(
            item["loaded"] is False and item["disabled"] is True for item in after
        )
        status = "pass" if not blocker and not failures and verified else "blocked"
        if not blocker and failures:
            blocker = "mac_scheduler_isolation_command_failed"
        elif not blocker and not verified:
            blocker = "mac_scheduler_isolation_not_verified"
        return self._write_receipt(
            action="isolate",
            status=status,
            blocker=blocker,
            owner=owner,
            before=before,
            operations=operations,
            after=after,
            release_gate=release_gate,
        )

    def restore(self, *, acknowledgement: str = "") -> dict[str, Any]:
        owner = SchedulerOwnershipStore(self.output_root).current()
        blocker = self._restore_owner_blocker(owner)
        release_gate = self._release_gate()
        if not release_gate.get("ok"):
            blocker = str(
                release_gate.get("blocker")
                or "paper_predeploy_gate_failed"
            )
        if acknowledgement != RESTORE_ACKNOWLEDGEMENT:
            blocker = "missing_restore_acknowledgement"
        if self.platform != "darwin":
            blocker = "mac_scheduler_restore_requires_darwin"
        before = self._snapshot() if not blocker else []
        operations: list[dict[str, Any]] = []
        if not blocker:
            for row in before:
                label = row["label"]
                operations.append(self._command(label, "enable"))
                if not row["loaded"]:
                    operations.append(self._command(label, "bootstrap"))
        after = self._snapshot() if not blocker else []
        failures = [item for item in operations if not item["ok"]]
        verified = bool(after) and all(
            item["loaded"] is True and item["disabled"] is False for item in after
        )
        status = "pass" if not blocker and not failures and verified else "blocked"
        if not blocker and failures:
            blocker = "mac_scheduler_restore_command_failed"
        elif not blocker and not verified:
            blocker = "mac_scheduler_restore_not_verified"
        return self._write_receipt(
            action="restore",
            status=status,
            blocker=blocker,
            owner=owner,
            before=before,
            operations=operations,
            after=after,
            release_gate=release_gate,
        )

    def verify(self) -> dict[str, Any]:
        owner = SchedulerOwnershipStore(self.output_root).current()
        blocker = self._isolation_owner_blocker(owner)
        if self.platform != "darwin":
            blocker = "mac_scheduler_isolation_requires_darwin"
        after = self._snapshot() if not blocker else []
        verified = bool(after) and all(
            item["loaded"] is False and item["disabled"] is True for item in after
        )
        if not blocker and not verified:
            blocker = "mac_scheduler_isolation_not_verified"
        return self._write_receipt(
            action="verify",
            status="pass" if not blocker and verified else "blocked",
            blocker=blocker,
            owner=owner,
            before=[],
            operations=[],
            after=after,
            release_gate={"ok": True, "status": "not_required_for_verify"},
        )

    def _snapshot(self) -> list[dict[str, Any]]:
        disabled_result = self._run(
            ["launchctl", "print-disabled", f"gui/{self.uid}"]
        )
        disabled_text = str(disabled_result.stdout or "")
        rows = []
        for label in FOCUS_SCHEDULE_LABELS:
            loaded_result = self._run(
                ["launchctl", "print", f"gui/{self.uid}/{label}"]
            )
            match = re.search(
                rf'"{re.escape(label)}"\s*=>\s*(true|false|enabled|disabled)',
                disabled_text,
            )
            disabled_value = match.group(1) if match else ""
            rows.append(
                {
                    "label": label,
                    "loaded": loaded_result.returncode == 0,
                    "disabled": (
                        disabled_value in {"true", "disabled"}
                        if disabled_result.returncode == 0 and match
                        else None
                    ),
                    "plist_exists": (
                        self.launch_agents_dir / f"{label}.plist"
                    ).exists(),
                }
            )
        return rows

    def _command(self, label: str, action: str) -> dict[str, Any]:
        if action in {"disable", "enable"}:
            command = ["launchctl", action, f"gui/{self.uid}/{label}"]
        elif action == "bootout":
            command = ["launchctl", "bootout", f"gui/{self.uid}/{label}"]
        elif action == "bootstrap":
            command = [
                "launchctl",
                "bootstrap",
                f"gui/{self.uid}",
                str(self.launch_agents_dir / f"{label}.plist"),
            ]
        else:  # pragma: no cover - private caller is closed over four actions.
            raise ValueError("unsupported_launchctl_action")
        result = self._run(command)
        return {
            "label": label,
            "action": action,
            "command": command,
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stderr": str(result.stderr or "")[-500:],
        }

    def _run(self, command: list[str]) -> subprocess.CompletedProcess:
        try:
            return self.command_runner(
                command,
                capture_output=True,
                text=True,
                check=False,
            )
        except Exception as exc:  # noqa: BLE001 - uncertainty must block.
            return subprocess.CompletedProcess(
                command,
                1,
                stdout="",
                stderr=f"{type(exc).__name__}: {exc}",
            )

    def _release_gate(self) -> dict[str, Any]:
        try:
            result = self.release_gate_validator()
            return dict(result) if isinstance(result, dict) else {
                "ok": False,
                "blocker": "paper_predeploy_gate_invalid",
            }
        except Exception as exc:  # noqa: BLE001 - mutation must remain blocked.
            return {
                "ok": False,
                "blocker": "paper_predeploy_gate_failed",
                "detail": f"{type(exc).__name__}: {exc}",
            }

    @staticmethod
    def _isolation_owner_blocker(owner: dict[str, Any]) -> str:
        if not owner:
            return "scheduler_ownership_missing"
        if owner.get("status") != "paused":
            return "local_scheduler_ownership_not_paused"
        if owner.get("active_owner_id") is not None:
            return "local_scheduler_owner_still_active"
        if owner.get("dual_owner_allowed") is not False:
            return "dual_scheduler_ownership_not_forbidden"
        return ""

    @staticmethod
    def _restore_owner_blocker(owner: dict[str, Any]) -> str:
        if not owner:
            return "scheduler_ownership_missing"
        if owner.get("status") != "active":
            return "local_scheduler_ownership_not_active"
        if owner.get("active_owner_id") != LOCAL_OWNER_ID:
            return "local_scheduler_owner_mismatch"
        if owner.get("dual_owner_allowed") is not False:
            return "dual_scheduler_ownership_not_forbidden"
        return ""

    def _write_receipt(
        self,
        *,
        action: str,
        status: str,
        blocker: str,
        owner: dict[str, Any],
        before: list[dict[str, Any]],
        operations: list[dict[str, Any]],
        after: list[dict[str, Any]],
        release_gate: dict[str, Any],
    ) -> dict[str, Any]:
        checked_at = self.now().astimezone(timezone.utc).replace(
            microsecond=0
        )
        payload = {
            "schema_version": "mac-paper-scheduler-isolation-v1",
            "scope": "paper_only",
            "action": action,
            "status": status,
            "blocker": blocker,
            "checked_at": checked_at.isoformat(),
            "labels": list(FOCUS_SCHEDULE_LABELS),
            "scheduler_ownership": owner,
            "release_gate": release_gate,
            "before": before,
            "operations": operations,
            "after": after,
            "safety": {
                "touches_live": False,
                "touches_exchange_keys": False,
                "changes_strategy_plan": False,
                "allowlist_only": True,
            },
        }
        directory = (
            self.output_root / "cloud" / "mac_paper_scheduler_isolation"
        )
        write_json(directory / "current.json", [payload])
        stamp = checked_at.strftime("%Y%m%dT%H%M%SZ")
        write_json(directory / f"{action}_{stamp}.json", [payload])
        return payload
