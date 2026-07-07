from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json
from services.schedule_installer import (
    SCHEDULE_INSTALL_ACKNOWLEDGEMENT,
    SCHEDULE_ROLLBACK_ACKNOWLEDGEMENT,
    ScheduleInstaller,
)
from services.schedule_post_install_verifier import SchedulePostInstallVerifier

TAKEOVER_PACKAGE_VALID_FOR_MINUTES = 15


class ScheduleTakeoverPackage:
    def __init__(
        self,
        output_root: Path | None = None,
        launch_agents_dir: Path | None = None,
        command_runner: Callable[[list[str]], subprocess.CompletedProcess] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        config = load_pipeline_config()
        self.output_root = output_root or ROOT / config.get("output_root", "outputs")
        self.launch_agents_dir = launch_agents_dir
        self.command_runner = command_runner
        self.now = now or (lambda: datetime.now(timezone.utc).replace(microsecond=0))

    def run(self, run_date: str) -> dict:
        installer = ScheduleInstaller(self.output_root, self.launch_agents_dir, self.command_runner)
        install_plan = installer.plan(run_date)
        rollback_plan = installer.rollback_plan(run_date)
        post_verify = SchedulePostInstallVerifier(self.output_root, self.launch_agents_dir, self.command_runner).run(run_date)
        status = self._status(install_plan, post_verify)
        packaged_at = self.now().astimezone(timezone.utc).replace(microsecond=0)
        review_payload = {
            "schema_version": "schedule-takeover-package-v1",
            "run_date": run_date,
            "status": status,
            "summary": {
                "install_plan_status": install_plan.get("status", ""),
                "requires_reinstall_count": (install_plan.get("summary") or {}).get("requires_reinstall_count", 0),
                "install_plan_blocked_count": (install_plan.get("summary") or {}).get("blocked_count", 0),
                "post_install_verify_status": post_verify.get("status", ""),
                "rollback_plan_status": rollback_plan.get("status", ""),
                "rollback_plan_blocker": rollback_plan.get("blocker", ""),
            },
            "checks": self._checks(install_plan, post_verify),
            "acknowledgements": {
                "install": SCHEDULE_INSTALL_ACKNOWLEDGEMENT,
                "rollback": SCHEDULE_ROLLBACK_ACKNOWLEDGEMENT,
            },
            "current_evidence": {
                "install_plan": self._compact_install_plan(install_plan),
                "post_install_verify": self._compact_post_verify(post_verify),
                "rollback_plan": self._compact_rollback_plan(rollback_plan),
            },
        }
        package_id = self._package_id(review_payload)
        payload = {
            **review_payload,
            "run_date": run_date,
            "package_id": package_id,
            "packaged_at": packaged_at.isoformat(),
            "valid_for_minutes": TAKEOVER_PACKAGE_VALID_FOR_MINUTES,
            "expires_at": (packaged_at + timedelta(minutes=TAKEOVER_PACKAGE_VALID_FOR_MINUTES)).isoformat(),
            "commands": self._commands(run_date, package_id),
            "safety": {
                "writes_launch_agents": False,
                "runs_launchctl_modification": False,
                "runs_launchctl_print": True,
                "opens_broker_clients": False,
                "submits_orders": False,
            },
        }
        write_json(self.output_root / "schedules" / "takeover_package_current.json", [payload])
        write_json(self.output_root / "schedules" / f"takeover_package_{run_date}.json", [payload])
        return payload

    def check_current(self, run_date: str) -> dict:
        package_path = self.output_root / "schedules" / "takeover_package_current.json"
        rows = load_json(package_path)
        package = rows[-1] if rows else {}
        package_id = str(package.get("package_id") or "")
        package_status = str(package.get("status") or "missing")
        if package_status == "already_current":
            package_gate = {"ok": True, "status": "already_current", "package_id": package_id, "artifact": str(package_path)}
        else:
            package_gate = ScheduleInstaller(self.output_root, self.launch_agents_dir, self.command_runner)._package_gate(run_date, package_id)
        usable = bool(package_gate.get("ok"))
        blocker = "" if usable else str(package_gate.get("blocker") or "package_gate_failed")
        status = "ready_for_attended_install" if usable and package_status == "ready_for_attended_install" else ("already_current" if usable else "blocked")
        payload = {
            "run_date": run_date,
            "checked_at": self.now().astimezone(timezone.utc).replace(microsecond=0).isoformat(),
            "status": status,
            "usable_for_attended_install": status == "ready_for_attended_install",
            "blocker": blocker,
            "package": {
                "artifact": str(package_path),
                "available": bool(package),
                "status": package_status,
                "package_id": package_id,
                "packaged_at": str(package.get("packaged_at") or ""),
                "expires_at": str(package.get("expires_at") or ""),
                "valid_for_minutes": package.get("valid_for_minutes", ""),
            },
            "package_gate": package_gate,
            "operator_next_action": self._operator_next_action(status, blocker, package),
            "commands": {
                "regenerate_package": f"python3 -m pipelines.schedule_takeover_package --date {run_date} --json",
                "attended_install": (package.get("commands") or {}).get("attended_install", "") if usable else "",
                "post_install_verify": (package.get("commands") or {}).get("post_install_verify", "") if usable else "",
            },
            "safety": {
                "writes_receipt_artifacts": True,
                "writes_launch_agents": False,
                "runs_launchctl_modification": False,
                "runs_launchctl_print": False,
                "opens_broker_clients": False,
                "submits_orders": False,
            },
        }
        write_json(self.output_root / "schedules" / "takeover_package_check_current.json", [payload])
        write_json(self.output_root / "schedules" / f"takeover_package_check_{run_date}.json", [payload])
        return payload

    def _operator_next_action(self, status: str, blocker: str, package: dict) -> dict:
        if status == "ready_for_attended_install":
            return {
                "action": "authorize_attended_install",
                "summary": "Current takeover package is fresh. Run attended install only after explicit operator approval.",
            }
        if status == "already_current":
            return {
                "action": "no_install_required",
                "summary": "Current generated schedule already appears active; run post-install verification if needed.",
            }
        if blocker == "missing_takeover_package":
            return {
                "action": "generate_takeover_package",
                "summary": "No takeover package exists. Generate a fresh no-write package first.",
            }
        if blocker in {"expired_package", "stale_or_mismatched_package", "takeover_package_not_ready", "missing_package_id"}:
            return {
                "action": "regenerate_takeover_package",
                "summary": "Current takeover package cannot authorize install. Regenerate and review a fresh package.",
            }
        return {
            "action": "review_package_gate_blocker",
            "summary": f"Review takeover package blocker: {blocker or str(package.get('status') or 'unknown')}",
        }

    def _package_id(self, review_payload: dict) -> str:
        raw = json.dumps(review_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def _status(self, install_plan: dict, post_verify: dict) -> str:
        if post_verify.get("status") == "pass":
            return "already_current"
        if install_plan.get("status") == "ready" and int((install_plan.get("summary") or {}).get("blocked_count") or 0) == 0:
            return "ready_for_attended_install"
        return "blocked"

    def _checks(self, install_plan: dict, post_verify: dict) -> list[dict]:
        checks = []
        install_blocked = int((install_plan.get("summary") or {}).get("blocked_count") or 0)
        install_ready = install_plan.get("status") in {"ready", "noop"} and install_blocked == 0
        checks.append({
            "name": "install_plan_ready",
            "status": "pass" if install_ready else "fail",
            "summary": "install dry-run plan is reviewable and has no missing generated plists" if install_ready else "install dry-run plan is blocked",
            "evidence": self._compact_install_plan(install_plan),
        })
        verified = post_verify.get("status") == "pass"
        checks.append({
            "name": "current_takeover_verified",
            "status": "pass" if verified else "warn",
            "summary": "current generated schedule is already active" if verified else "current generated schedule is not active yet; run attended install before expecting this to pass",
            "evidence": self._compact_post_verify(post_verify),
        })
        checks.append({
            "name": "commands_available",
            "status": "pass",
            "summary": "attended install, post-install verification, and rollback commands are included",
            "evidence": {"command_count": len(self._commands("YYYY-MM-DD", "PACKAGE_ID"))},
        })
        return checks

    def _commands(self, run_date: str, package_id: str) -> dict:
        return {
            "review_install_plan": f"python3 -m pipelines.schedule_install --dry-run --date {run_date} --json",
            "attended_install": f"python3 -m pipelines.schedule_install --date {run_date} --package-id {package_id} --acknowledgement {SCHEDULE_INSTALL_ACKNOWLEDGEMENT} --json",
            "post_install_verify": f"python3 -m pipelines.schedule_post_install_verify --date {run_date} --json",
            "review_rollback_plan": f"python3 -m pipelines.schedule_install --rollback --dry-run --date {run_date} --json",
            "attended_rollback": f"python3 -m pipelines.schedule_install --rollback --date {run_date} --acknowledgement {SCHEDULE_ROLLBACK_ACKNOWLEDGEMENT} --json",
        }

    def _compact_install_plan(self, plan: dict) -> dict:
        summary = plan.get("summary") or {}
        return {
            "status": plan.get("status", ""),
            "requires_reinstall_count": summary.get("requires_reinstall_count", 0),
            "blocked_count": summary.get("blocked_count", 0),
            "already_current_active_count": summary.get("already_current_active_count", 0),
        }

    def _compact_post_verify(self, verify: dict) -> dict:
        return {
            "status": verify.get("status", ""),
            "failed_checks": [check.get("name", "") for check in verify.get("checks", []) if check.get("status") != "pass"],
            "passed_checks": [check.get("name", "") for check in verify.get("checks", []) if check.get("status") == "pass"],
        }

    def _compact_rollback_plan(self, plan: dict) -> dict:
        summary = plan.get("summary") or {}
        return {
            "status": plan.get("status", ""),
            "blocker": plan.get("blocker", ""),
            "restorable_count": summary.get("restorable_count", 0),
            "blocked_count": summary.get("blocked_count", 0),
        }
