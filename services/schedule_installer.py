from __future__ import annotations

import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json
from services.schedule_status import ScheduleStatus
from services.schedule_profiles import PROJECT_LABEL_PREFIX
from services.paper_release_receipt import PaperReleaseReceiptGate


SCHEDULE_INSTALL_ACKNOWLEDGEMENT = "I_UNDERSTAND_SCHEDULE_INSTALL_WILL_REPLACE_OR_RESTART_LOCAL_LAUNCHD_JOBS"
SCHEDULE_ROLLBACK_ACKNOWLEDGEMENT = "I_UNDERSTAND_SCHEDULE_ROLLBACK_WILL_RESTORE_LOCAL_LAUNCHD_JOBS_FROM_BACKUP"
DASHBOARD_LABEL = "com.wendy.trading-orchestrator.dashboard"
DASHBOARD_HEALTH_URL = "http://127.0.0.1:8765/dashboard-v4.html"


class ScheduleInstaller:
    def __init__(
        self,
        output_root: Path | None = None,
        launch_agents_dir: Path | None = None,
        command_runner: Callable[[list[str]], subprocess.CompletedProcess] | None = None,
        *,
        sleep: Callable[[float], None] | None = None,
        teardown_poll_seconds: float = 0.5,
        teardown_timeout_seconds: float = 10.0,
        bootstrap_retry_delay_seconds: float = 2.0,
        dashboard_poll_seconds: float = 0.5,
        dashboard_timeout_seconds: float = 15.0,
        release_gate_validator: Callable[[], dict] | None = None,
    ) -> None:
        config = load_pipeline_config()
        self.output_root = output_root or ROOT / config.get("output_root", "outputs")
        self.launch_agents_dir = launch_agents_dir or Path.home() / "Library" / "LaunchAgents"
        self.command_runner = command_runner or self._run_command
        self.sleep = sleep or time.sleep
        self.teardown_poll_seconds = teardown_poll_seconds
        self.teardown_timeout_seconds = teardown_timeout_seconds
        self.bootstrap_retry_delay_seconds = bootstrap_retry_delay_seconds
        self.dashboard_poll_seconds = dashboard_poll_seconds
        self.dashboard_timeout_seconds = dashboard_timeout_seconds
        self.release_gate_validator = release_gate_validator or PaperReleaseReceiptGate(
            self.output_root
        ).verify

    def plan(self, run_date: str, restart_loaded: bool = True, *, persist: bool = True) -> dict:
        schedule_rows = load_json(self.output_root / "schedules" / "current.json")
        schedule = schedule_rows[-1] if schedule_rows else {}
        status = ScheduleStatus(self.output_root, self.launch_agents_dir, self.command_runner).run(run_date)
        inspected = {str(job.get("label") or ""): job for job in status.get("jobs", [])}
        jobs = [self._plan_job(job, inspected.get(str(job.get("label") or ""), {}), restart_loaded) for job in schedule.get("jobs", [])]
        generated_labels = {str(job.get("label") or "") for job in schedule.get("jobs", [])}
        orphans = self._plan_orphans(generated_labels)
        blocked = [job for job in jobs if job["action"] == "blocked_missing_generated_plist"]
        stale = [job for job in jobs if job.get("requires_reinstall")]
        changes_required = bool(stale or orphans)
        payload = {
            "run_date": run_date,
            "planned_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": "blocked" if blocked else ("ready" if changes_required else "noop"),
            "mode": "dry_run",
            "restart_loaded": restart_loaded,
            "launch_agents_dir": str(self.launch_agents_dir),
            "summary": {
                "job_count": len(jobs),
                "requires_reinstall_count": len(stale),
                "orphan_count": len(orphans),
                "blocked_count": len(blocked),
                "already_current_active_count": sum(1 for job in jobs if job["action"] == "already_current_active"),
            },
            "jobs": jobs,
            "orphans": orphans,
            "schedule_status": status,
            "safety": {
                "dry_run": True,
                "writes_launch_agents": False,
                "runs_launchctl_modification": False,
                "runs_launchctl_print": True,
                "opens_broker_clients": False,
                "submits_orders": False,
            },
        }
        if persist:
            write_json(self.output_root / "schedules" / "install_plan_current.json", [payload])
            write_json(self.output_root / "schedules" / f"install_plan_{run_date}.json", [payload])
        return payload

    def install(
        self,
        run_date: str,
        restart_loaded: bool = True,
        acknowledgement: str = "",
        package_id: str = "",
    ) -> dict:
        plan = self.plan(run_date, restart_loaded=restart_loaded)
        acknowledgement_ok = acknowledgement == SCHEDULE_INSTALL_ACKNOWLEDGEMENT
        if plan.get("status") == "blocked":
            return self._blocked_install_receipt(run_date, restart_loaded, plan, "install_plan_blocked", acknowledgement_ok)
        if plan.get("status") != "noop" and not acknowledgement_ok:
            return self._blocked_install_receipt(run_date, restart_loaded, plan, "missing_acknowledgement", acknowledgement_ok)
        package_gate = self._package_gate(run_date, package_id) if plan.get("status") != "noop" else {"ok": True, "status": "not_required_for_noop"}
        if plan.get("status") != "noop" and not package_gate.get("ok"):
            return self._blocked_install_receipt(
                run_date,
                restart_loaded,
                plan,
                str(package_gate.get("blocker") or "package_gate_failed"),
                acknowledgement_ok,
                package_gate=package_gate,
            )
        release_gate = (
            self.release_gate_validator()
            if plan.get("status") != "noop"
            else {"ok": True, "status": "not_required_for_noop"}
        )
        if plan.get("status") != "noop" and not release_gate.get("ok"):
            return self._blocked_install_receipt(
                run_date,
                restart_loaded,
                plan,
                str(release_gate.get("blocker") or "paper_predeploy_gate_failed"),
                acknowledgement_ok,
                package_gate=package_gate,
                release_gate=release_gate,
            )
        if plan.get("status") == "noop":
            payload = {
                "run_date": run_date,
                "installed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "status": "noop",
                "mode": "apply",
                "restart_loaded": restart_loaded,
                "launch_agents_dir": str(self.launch_agents_dir),
                "acknowledgement_ok": acknowledgement_ok,
                "required_acknowledgement": SCHEDULE_INSTALL_ACKNOWLEDGEMENT,
                "package_gate": package_gate,
                "release_gate": release_gate,
                "jobs": [],
                "orphans": [],
                "plan": plan,
                "schedule_status": plan.get("schedule_status", {}),
                "safety": self._install_safety(writes=False),
            }
            write_json(self.output_root / "schedules" / "install_current.json", [payload])
            write_json(self.output_root / "schedules" / f"install_{run_date}.json", [payload])
            return payload

        schedule_rows = load_json(self.output_root / "schedules" / "current.json")
        schedule = schedule_rows[-1] if schedule_rows else {}
        install_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_dir = self.output_root / "schedules" / "launch_agent_backups" / install_id
        self.launch_agents_dir.mkdir(parents=True, exist_ok=True)
        results = []
        for job in self._install_order(schedule.get("jobs", [])):
            result = self._install_job(job, restart_loaded, backup_dir)
            results.append(result)
            if result["status"] == "fail":
                break
        orphan_results = []
        if not any(item["status"] == "fail" for item in results):
            for orphan in plan.get("orphans", []):
                result = self._remove_orphan_job(orphan, backup_dir)
                orphan_results.append(result)
                if result["status"] == "fail":
                    break
        all_results = [*results, *orphan_results]
        failed = [item for item in all_results if item["status"] == "fail"]
        rollback = self._rollback_failed_install(run_date, all_results, restart_loaded) if failed else {}
        status = ScheduleStatus(self.output_root, self.launch_agents_dir, self.command_runner).run(run_date)
        backups = [item["backup"] for item in all_results if item.get("backup")]
        payload = {
            "run_date": run_date,
            "installed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": "fail" if failed else status.get("status", "unknown"),
            "mode": "apply",
            "restart_loaded": restart_loaded,
            "launch_agents_dir": str(self.launch_agents_dir),
            "acknowledgement_ok": True,
            "required_acknowledgement": SCHEDULE_INSTALL_ACKNOWLEDGEMENT,
            "package_gate": package_gate,
            "release_gate": release_gate,
            "backup_dir": str(backup_dir) if backups else "",
            "backup_count": len(backups),
            "jobs": results,
            "orphans": orphan_results,
            "rollback": rollback,
            "plan": plan,
            "schedule_status": status,
            "safety": self._install_safety(writes=True),
        }
        write_json(self.output_root / "schedules" / "install_current.json", [payload])
        write_json(self.output_root / "schedules" / f"install_{run_date}.json", [payload])
        return payload

    def rollback_plan(
        self,
        run_date: str,
        receipt_path: Path | None = None,
        restart_loaded: bool = True,
        *,
        persist: bool = True,
    ) -> dict:
        receipt_source = receipt_path or self.output_root / "schedules" / "install_current.json"
        receipt = self._load_receipt(receipt_source)
        status = ScheduleStatus(self.output_root, self.launch_agents_dir, self.command_runner).run(run_date)
        receipt_jobs = [*receipt.get("jobs", []), *receipt.get("orphans", [])]
        jobs = [self._rollback_plan_job(job, restart_loaded) for job in receipt_jobs]
        blocked = [job for job in jobs if job.get("blocked")]
        restorable = [job for job in jobs if job.get("restorable")]
        if not receipt:
            plan_status = "blocked"
            blocker = "missing_install_receipt"
        elif not jobs:
            plan_status = "blocked"
            blocker = "missing_backup_records"
        elif blocked:
            plan_status = "blocked"
            blocker = "rollback_plan_blocked"
        else:
            plan_status = "ready" if restorable else "blocked"
            blocker = "" if restorable else "missing_backup_records"
        payload = {
            "run_date": run_date,
            "planned_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": plan_status,
            "mode": "rollback_dry_run",
            "blocker": blocker,
            "restart_loaded": restart_loaded,
            "receipt_path": str(receipt_source),
            "launch_agents_dir": str(self.launch_agents_dir),
            "summary": {
                "job_count": len(jobs),
                "restorable_count": len(restorable),
                "blocked_count": len(blocked),
            },
            "jobs": jobs,
            "install_receipt": self._compact_install_receipt(receipt),
            "schedule_status": status,
            "safety": {
                "dry_run": True,
                "writes_launch_agents": False,
                "runs_launchctl_modification": False,
                "runs_launchctl_print": True,
                "opens_broker_clients": False,
                "submits_orders": False,
            },
        }
        if persist:
            write_json(self.output_root / "schedules" / "rollback_plan_current.json", [payload])
            write_json(self.output_root / "schedules" / f"rollback_plan_{run_date}.json", [payload])
        return payload

    def rollback(
        self,
        run_date: str,
        receipt_path: Path | None = None,
        restart_loaded: bool = True,
        acknowledgement: str = "",
    ) -> dict:
        plan = self.rollback_plan(run_date, receipt_path=receipt_path, restart_loaded=restart_loaded)
        acknowledgement_ok = acknowledgement == SCHEDULE_ROLLBACK_ACKNOWLEDGEMENT
        if plan.get("status") != "ready":
            return self._blocked_rollback_receipt(run_date, restart_loaded, plan, plan.get("blocker") or "rollback_plan_blocked", acknowledgement_ok)
        if not acknowledgement_ok:
            return self._blocked_rollback_receipt(run_date, restart_loaded, plan, "missing_acknowledgement", acknowledgement_ok)
        release_gate = self.release_gate_validator()
        if not release_gate.get("ok"):
            return self._blocked_rollback_receipt(
                run_date,
                restart_loaded,
                plan,
                str(release_gate.get("blocker") or "paper_predeploy_gate_failed"),
                acknowledgement_ok,
                release_gate=release_gate,
            )
        rollback_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        pre_rollback_backup_dir = self.output_root / "schedules" / "rollback_target_backups" / rollback_id
        results = [self._rollback_job(job, restart_loaded, pre_rollback_backup_dir) for job in plan.get("jobs", [])]
        status = ScheduleStatus(self.output_root, self.launch_agents_dir, self.command_runner).run(run_date)
        failed = [item for item in results if item["status"] == "fail"]
        backups = [item["pre_rollback_backup"] for item in results if item.get("pre_rollback_backup")]
        payload = {
            "run_date": run_date,
            "rolled_back_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": "fail" if failed else "rolled_back",
            "mode": "rollback_apply",
            "restart_loaded": restart_loaded,
            "launch_agents_dir": str(self.launch_agents_dir),
            "acknowledgement_ok": True,
            "required_acknowledgement": SCHEDULE_ROLLBACK_ACKNOWLEDGEMENT,
            "release_gate": release_gate,
            "pre_rollback_backup_dir": str(pre_rollback_backup_dir) if backups else "",
            "pre_rollback_backup_count": len(backups),
            "jobs": results,
            "plan": plan,
            "schedule_status": status,
            "safety": self._install_safety(writes=True),
        }
        write_json(self.output_root / "schedules" / "rollback_current.json", [payload])
        write_json(self.output_root / "schedules" / f"rollback_{run_date}.json", [payload])
        return payload

    def _blocked_install_receipt(
        self,
        run_date: str,
        restart_loaded: bool,
        plan: dict,
        blocker: str,
        acknowledgement_ok: bool,
        *,
        package_gate: dict | None = None,
        release_gate: dict | None = None,
    ) -> dict:
        payload = {
            "run_date": run_date,
            "installed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": "blocked",
            "mode": "apply",
            "blocker": blocker,
            "restart_loaded": restart_loaded,
            "launch_agents_dir": str(self.launch_agents_dir),
            "acknowledgement_ok": acknowledgement_ok,
            "required_acknowledgement": SCHEDULE_INSTALL_ACKNOWLEDGEMENT,
            "package_gate": package_gate or {},
            "release_gate": release_gate or {},
            "jobs": [],
            "orphans": [],
            "plan": plan,
            "schedule_status": plan.get("schedule_status", {}),
            "safety": self._install_safety(writes=False),
        }
        write_json(self.output_root / "schedules" / "install_current.json", [payload])
        write_json(self.output_root / "schedules" / f"install_{run_date}.json", [payload])
        return payload

    def _package_gate(self, run_date: str, package_id: str) -> dict:
        requested_id = str(package_id or "").strip()
        if not requested_id:
            return {"ok": False, "blocker": "missing_package_id", "provided_package_id": ""}
        package_path = self.output_root / "schedules" / "takeover_package_current.json"
        rows = load_json(package_path)
        latest = rows[-1] if rows else {}
        if not latest:
            return {
                "ok": False,
                "blocker": "missing_takeover_package",
                "provided_package_id": requested_id,
                "artifact": str(package_path),
            }
        expected_id = str(latest.get("package_id") or "")
        status = str(latest.get("status") or "")
        if str(latest.get("run_date") or "") != str(run_date):
            return {
                "ok": False,
                "blocker": "stale_or_mismatched_package",
                "provided_package_id": requested_id,
                "expected_package_id": expected_id,
                "package_run_date": str(latest.get("run_date") or ""),
                "run_date": run_date,
                "status": status,
                "artifact": str(package_path),
            }
        if expected_id != requested_id:
            return {
                "ok": False,
                "blocker": "stale_or_mismatched_package",
                "provided_package_id": requested_id,
                "expected_package_id": expected_id,
                "status": status,
                "artifact": str(package_path),
            }
        if status != "ready_for_attended_install":
            return {
                "ok": False,
                "blocker": "takeover_package_not_ready",
                "provided_package_id": requested_id,
                "expected_package_id": expected_id,
                "status": status,
                "artifact": str(package_path),
            }
        expires_at = str(latest.get("expires_at") or "")
        expired = self._is_expired(expires_at)
        if expired:
            return {
                "ok": False,
                "blocker": "expired_package",
                "provided_package_id": requested_id,
                "expected_package_id": expected_id,
                "status": status,
                "expires_at": expires_at,
                "artifact": str(package_path),
            }
        return {
            "ok": True,
            "status": status,
            "package_id": requested_id,
            "expires_at": expires_at,
            "artifact": str(package_path),
        }

    def _is_expired(self, expires_at: str) -> bool:
        if not expires_at:
            return False
        try:
            parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            return True
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc).replace(microsecond=0) > parsed.astimezone(timezone.utc)

    def _blocked_rollback_receipt(
        self,
        run_date: str,
        restart_loaded: bool,
        plan: dict,
        blocker: str,
        acknowledgement_ok: bool,
        *,
        release_gate: dict | None = None,
    ) -> dict:
        payload = {
            "run_date": run_date,
            "rolled_back_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": "blocked",
            "mode": "rollback_apply",
            "blocker": blocker,
            "restart_loaded": restart_loaded,
            "launch_agents_dir": str(self.launch_agents_dir),
            "acknowledgement_ok": acknowledgement_ok,
            "required_acknowledgement": SCHEDULE_ROLLBACK_ACKNOWLEDGEMENT,
            "release_gate": release_gate or {},
            "jobs": [],
            "plan": plan,
            "schedule_status": plan.get("schedule_status", {}),
            "safety": self._install_safety(writes=False),
        }
        write_json(self.output_root / "schedules" / "rollback_current.json", [payload])
        write_json(self.output_root / "schedules" / f"rollback_{run_date}.json", [payload])
        return payload

    def _install_safety(self, writes: bool) -> dict:
        return {
            "dry_run": False,
            "writes_launch_agents": writes,
            "runs_launchctl_modification": writes,
            "runs_launchctl_print": True,
            "opens_broker_clients": False,
            "submits_orders": False,
        }

    def _plan_job(self, job: dict, inspected: dict, restart_loaded: bool) -> dict:
        label = str(job.get("label", ""))
        source = Path(str(job.get("plist", "")))
        target = self.launch_agents_dir / f"{label}.plist"
        generated_exists = source.exists()
        installed = bool(inspected.get("installed", False))
        loaded = bool(inspected.get("loaded", False))
        matches_generated = bool(inspected.get("matches_generated", False))
        requires_reinstall = generated_exists and not (installed and matches_generated)
        if not generated_exists:
            action = "blocked_missing_generated_plist"
        elif installed and matches_generated and loaded:
            action = "already_current_active"
        elif installed and matches_generated:
            action = "already_current_not_loaded"
        elif installed and loaded and restart_loaded:
            action = "replace_stale_and_restart"
        elif installed and loaded:
            action = "stage_current_plist_without_restart"
        elif installed:
            action = "replace_stale_and_bootstrap"
        else:
            action = "install_missing_and_bootstrap"
        return {
            "label": label,
            "source": str(source),
            "target": str(target),
            "action": action,
            "installed": installed,
            "loaded": loaded,
            "matches_generated": matches_generated,
            "requires_reinstall": requires_reinstall,
            "restart_loaded": restart_loaded,
            "planned_commands": self._planned_commands(label, source, target, restart_loaded, loaded, generated_exists),
            "note": self._plan_note(action),
        }

    def _plan_orphans(self, generated_labels: set[str]) -> list[dict]:
        orphans: list[dict] = []
        for target in self._orphan_plists(generated_labels):
            label = target.stem
            orphans.append({
                "label": label,
                "target": str(target),
                "action": "remove_orphan_and_bootout",
                "requires_reinstall": True,
                "planned_commands": [
                    ["launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
                    ["rm", str(target)],
                ],
                "note": "Installed project LaunchAgent is outside the generated schedule profile and will be backed up, booted out, and removed.",
            })
        return orphans

    def _orphan_plists(self, generated_labels: set[str]) -> list[Path]:
        if not self.launch_agents_dir.exists():
            return []
        return [
            path
            for path in sorted(self.launch_agents_dir.glob(f"{PROJECT_LABEL_PREFIX}*.plist"))
            if path.stem not in generated_labels
        ]

    def _install_job(self, job: dict, restart_loaded: bool, backup_dir: Path) -> dict:
        label = str(job.get("label", ""))
        source = Path(str(job.get("plist", "")))
        target = self.launch_agents_dir / f"{label}.plist"
        commands: list[dict] = []
        if not label or not source.exists():
            return {
                "label": label,
                "source": str(source),
                "target": str(target),
                "status": "fail",
                "error": "generated plist is missing",
                "commands": commands,
            }
        backup = ""
        if target.exists():
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_target = backup_dir / target.name
            shutil.copy2(target, backup_target)
            backup = str(backup_target)
            commands.append({"command": ["backup", str(target), backup], "returncode": 0, "stdout": "", "stderr": ""})
        created = not target.exists()
        if restart_loaded:
            commands.append(self._command(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], allow_failure=True))
            wait = self._wait_until_unloaded(label)
            commands.extend(wait["commands"])
            if wait["status"] == "fail":
                return {
                    "label": label,
                    "source": str(source),
                    "target": str(target),
                    "status": "fail",
                    "error": wait["error"],
                    "backup": backup,
                    "created": created,
                    "commands": commands,
                }
        shutil.copy2(source, target)
        commands.append({"command": ["copy", str(source), str(target)], "returncode": 0, "stdout": "", "stderr": ""})
        if not restart_loaded:
            loaded = self._command(["launchctl", "print", f"gui/{os.getuid()}/{label}"], allow_failure=True)
            commands.append(loaded)
            if loaded["returncode"] == 0:
                return {
                    "label": label,
                    "source": str(source),
                    "target": str(target),
                    "status": "installed",
                    "already_loaded": True,
                    "backup": backup,
                    "created": created,
                    "commands": commands,
                }
        bootstrap_result = self._bootstrap_with_retries(target)
        commands.extend(bootstrap_result["commands"])
        bootstrap = bootstrap_result["last_command"]
        if bootstrap["returncode"] != 0 and "already bootstrapped" not in bootstrap["stderr"].lower():
            return {
                "label": label,
                "source": str(source),
                "target": str(target),
                "status": "fail",
                "error": bootstrap["stderr"] or bootstrap["stdout"] or "launchctl bootstrap failed",
                "backup": backup,
                "created": created,
                "bootstrap_attempts": bootstrap_result["attempts"],
                "bootstrap_retry_count": bootstrap_result["retry_count"],
                "commands": commands,
            }
        if restart_loaded:
            commands.append(self._command(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"], allow_failure=True))
        if label == DASHBOARD_LABEL:
            dashboard = self._wait_for_dashboard()
            commands.extend(dashboard["commands"])
            if dashboard["status"] == "fail":
                return {
                    "label": label,
                    "source": str(source),
                    "target": str(target),
                    "status": "fail",
                    "error": dashboard["error"],
                    "backup": backup,
                    "created": created,
                    "bootstrap_attempts": bootstrap_result["attempts"],
                    "bootstrap_retry_count": bootstrap_result["retry_count"],
                    "commands": commands,
                }
        return {
            "label": label,
            "source": str(source),
            "target": str(target),
            "status": "installed",
            "backup": backup,
            "created": created,
            "bootstrap_attempts": bootstrap_result["attempts"],
            "bootstrap_retry_count": bootstrap_result["retry_count"],
            "commands": commands,
        }

    def _remove_orphan_job(self, orphan: dict, backup_dir: Path) -> dict:
        label = str(orphan.get("label", ""))
        target = Path(str(orphan.get("target", "")))
        commands: list[dict] = []
        if not label.startswith(PROJECT_LABEL_PREFIX) or not target.name.startswith(PROJECT_LABEL_PREFIX):
            return {
                "label": label,
                "target": str(target),
                "status": "fail",
                "error": "refusing to remove non-project launchd plist",
                "commands": commands,
            }
        if not target.exists():
            return {
                "label": label,
                "target": str(target),
                "status": "removed",
                "backup": "",
                "commands": commands,
            }
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_target = backup_dir / target.name
        shutil.copy2(target, backup_target)
        commands.append({"command": ["backup", str(target), str(backup_target)], "returncode": 0, "stdout": "", "stderr": ""})
        commands.append(self._command(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], allow_failure=True))
        wait = self._wait_until_unloaded(label)
        commands.extend(wait["commands"])
        if wait["status"] == "fail":
            return {
                "label": label,
                "target": str(target),
                "status": "fail",
                "error": wait["error"],
                "backup": str(backup_target),
                "commands": commands,
            }
        target.unlink()
        commands.append({"command": ["rm", str(target)], "returncode": 0, "stdout": "", "stderr": ""})
        return {
            "label": label,
            "target": str(target),
            "status": "removed",
            "backup": str(backup_target),
            "commands": commands,
        }

    def _planned_commands(
        self,
        label: str,
        source: Path,
        target: Path,
        restart_loaded: bool,
        loaded: bool,
        generated_exists: bool,
    ) -> list[list[str]]:
        if not label or not generated_exists:
            return []
        commands: list[list[str]] = []
        if restart_loaded:
            commands.append(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"])
        commands.append(["copy", str(source), str(target)])
        if not restart_loaded and loaded:
            commands.append(["launchctl", "print", f"gui/{os.getuid()}/{label}"])
            return commands
        commands.append(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(target)])
        if restart_loaded:
            commands.append(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"])
        return commands

    def _plan_note(self, action: str) -> str:
        notes = {
            "already_current_active": "Installed plist matches generated schedule and launchd reports it loaded.",
            "already_current_not_loaded": "Installed plist matches generated schedule but launchd does not report it loaded.",
            "replace_stale_and_restart": "Installed job is loaded but stale; default install will bootout, replace, bootstrap, and kickstart it.",
            "stage_current_plist_without_restart": "No-restart install would copy the current plist, but the loaded launchd job may keep running the old definition until restarted.",
            "replace_stale_and_bootstrap": "Installed plist is stale and not loaded; install will replace and bootstrap it.",
            "install_missing_and_bootstrap": "Generated plist is missing from LaunchAgents; install will copy and bootstrap it.",
            "blocked_missing_generated_plist": "Generated plist is missing; regenerate schedule artifacts before installing.",
        }
        return notes.get(action, "")

    def _load_receipt(self, receipt_path: Path) -> dict:
        rows = load_json(receipt_path)
        return rows[-1] if rows else {}

    def _compact_install_receipt(self, receipt: dict) -> dict:
        return {
            "run_date": receipt.get("run_date", ""),
            "installed_at": receipt.get("installed_at", ""),
            "status": receipt.get("status", ""),
            "backup_dir": receipt.get("backup_dir", ""),
            "backup_count": receipt.get("backup_count", 0),
        }

    def _rollback_plan_job(self, job: dict, restart_loaded: bool) -> dict:
        label = str(job.get("label", ""))
        backup = Path(str(job.get("backup", "")))
        target = Path(str(job.get("target") or self.launch_agents_dir / f"{label}.plist"))
        if not label:
            action = "blocked_missing_label"
        elif job.get("created") is True and not str(job.get("backup", "")):
            action = "remove_created_job_and_restart" if restart_loaded else "remove_created_job_without_restart"
        elif not str(job.get("backup", "")):
            action = "blocked_missing_backup_record"
        elif not backup.exists():
            action = "blocked_missing_backup_file"
        else:
            action = "restore_backup_and_restart" if restart_loaded else "restore_backup_without_restart"
        restorable = action.startswith("restore_backup") or action.startswith("remove_created_job")
        return {
            "label": label,
            "backup": str(backup) if str(job.get("backup", "")) else "",
            "target": str(target),
            "action": action,
            "restorable": restorable,
            "blocked": not restorable,
            "restart_loaded": restart_loaded,
            "planned_commands": self._rollback_planned_commands(label, backup, target, restart_loaded, restorable, action),
            "note": self._rollback_plan_note(action),
        }

    def _rollback_planned_commands(self, label: str, backup: Path, target: Path, restart_loaded: bool, restorable: bool, action: str) -> list[list[str]]:
        if not label or not restorable:
            return []
        commands: list[list[str]] = []
        if restart_loaded:
            commands.append(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"])
        if action.startswith("remove_created_job"):
            commands.append(["rm", str(target)])
            if not restart_loaded:
                commands.append(["launchctl", "print", f"gui/{os.getuid()}/{label}"])
            return commands
        commands.append(["copy", str(backup), str(target)])
        if not restart_loaded:
            commands.append(["launchctl", "print", f"gui/{os.getuid()}/{label}"])
            return commands
        commands.append(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(target)])
        commands.append(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"])
        return commands

    def _rollback_plan_note(self, action: str) -> str:
        notes = {
            "restore_backup_and_restart": "Rollback would bootout the loaded job, restore the backup plist, bootstrap it, and kickstart it.",
            "restore_backup_without_restart": "Rollback would restore the backup plist on disk, but a loaded launchd job may keep running its current definition until restarted.",
            "remove_created_job_and_restart": "Rollback would bootout and remove a LaunchAgent plist that was newly created by install.",
            "remove_created_job_without_restart": "Rollback would remove a newly created LaunchAgent plist without restarting loaded services.",
            "blocked_missing_label": "Install receipt job is missing a label; rollback cannot identify the launchd service.",
            "blocked_missing_backup_record": "Install receipt has no backup path for this job; rollback cannot restore it.",
            "blocked_missing_backup_file": "Backup plist path from the install receipt no longer exists.",
        }
        return notes.get(action, "")

    def _rollback_job(self, job: dict, restart_loaded: bool, pre_rollback_backup_dir: Path) -> dict:
        label = str(job.get("label", ""))
        backup = Path(str(job.get("backup", "")))
        target = Path(str(job.get("target", "")))
        action = str(job.get("action", ""))
        commands: list[dict] = []
        if action.startswith("remove_created_job"):
            if not label or not target:
                return {
                    "label": label,
                    "backup": str(backup),
                    "target": str(target),
                    "status": "fail",
                    "error": "rollback target is missing",
                    "commands": commands,
                }
            if restart_loaded:
                commands.append(self._command(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], allow_failure=True))
                wait = self._wait_until_unloaded(label)
                commands.extend(wait["commands"])
                if wait["status"] == "fail":
                    return {
                        "label": label,
                        "backup": "",
                        "target": str(target),
                        "status": "fail",
                        "error": wait["error"],
                        "commands": commands,
                    }
            if target.exists():
                target.unlink()
                commands.append({"command": ["rm", str(target)], "returncode": 0, "stdout": "", "stderr": ""})
            return {
                "label": label,
                "backup": "",
                "target": str(target),
                "status": "removed",
                "commands": commands,
            }
        if not label or not backup.exists() or not target:
            return {
                "label": label,
                "backup": str(backup),
                "target": str(target),
                "status": "fail",
                "error": "rollback backup is missing",
                "commands": commands,
            }
        pre_backup = ""
        if target.exists():
            pre_rollback_backup_dir.mkdir(parents=True, exist_ok=True)
            pre_backup_target = pre_rollback_backup_dir / target.name
            shutil.copy2(target, pre_backup_target)
            pre_backup = str(pre_backup_target)
            commands.append({"command": ["backup", str(target), pre_backup], "returncode": 0, "stdout": "", "stderr": ""})
        if restart_loaded:
            commands.append(self._command(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], allow_failure=True))
            wait = self._wait_until_unloaded(label)
            commands.extend(wait["commands"])
            if wait["status"] == "fail":
                return {
                    "label": label,
                    "backup": str(backup),
                    "target": str(target),
                    "status": "fail",
                    "error": wait["error"],
                    "pre_rollback_backup": pre_backup,
                    "commands": commands,
                }
        shutil.copy2(backup, target)
        commands.append({"command": ["copy", str(backup), str(target)], "returncode": 0, "stdout": "", "stderr": ""})
        if not restart_loaded:
            loaded = self._command(["launchctl", "print", f"gui/{os.getuid()}/{label}"], allow_failure=True)
            commands.append(loaded)
            return {
                "label": label,
                "backup": str(backup),
                "target": str(target),
                "status": "restored",
                "already_loaded": loaded["returncode"] == 0,
                "pre_rollback_backup": pre_backup,
                "commands": commands,
            }
        bootstrap_result = self._bootstrap_with_retries(target)
        commands.extend(bootstrap_result["commands"])
        bootstrap = bootstrap_result["last_command"]
        if bootstrap["returncode"] != 0 and "already bootstrapped" not in bootstrap["stderr"].lower():
            return {
                "label": label,
                "backup": str(backup),
                "target": str(target),
                "status": "fail",
                "error": bootstrap["stderr"] or bootstrap["stdout"] or "launchctl bootstrap failed",
                "pre_rollback_backup": pre_backup,
                "bootstrap_attempts": bootstrap_result["attempts"],
                "bootstrap_retry_count": bootstrap_result["retry_count"],
                "commands": commands,
            }
        commands.append(self._command(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"], allow_failure=True))
        return {
            "label": label,
            "backup": str(backup),
            "target": str(target),
            "status": "restored",
            "pre_rollback_backup": pre_backup,
            "bootstrap_attempts": bootstrap_result["attempts"],
            "bootstrap_retry_count": bootstrap_result["retry_count"],
            "commands": commands,
        }

    def _install_order(self, jobs: list[dict]) -> list[dict]:
        return sorted(jobs, key=lambda job: str(job.get("label") or "") == DASHBOARD_LABEL)

    def _wait_until_unloaded(self, label: str) -> dict:
        commands: list[dict] = []
        service = f"gui/{os.getuid()}/{label}"
        deadline = time.monotonic() + self.teardown_timeout_seconds
        while True:
            printed = self._command(["launchctl", "print", service], allow_failure=True)
            commands.append(printed)
            if printed["returncode"] != 0:
                return {"status": "pass", "commands": commands}
            if time.monotonic() >= deadline:
                return {
                    "status": "fail",
                    "error": "launchctl service did not disappear after bootout",
                    "commands": commands,
                }
            self.sleep(self.teardown_poll_seconds)

    def _bootstrap_with_retries(self, target: Path) -> dict:
        commands: list[dict] = []
        attempts = 0
        while attempts < 3:
            attempts += 1
            result = self._command(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(target)], allow_failure=True)
            commands.append(result)
            if result["returncode"] != 5:
                break
            if attempts < 3:
                self.sleep(self.bootstrap_retry_delay_seconds)
        return {
            "commands": commands,
            "last_command": commands[-1],
            "attempts": attempts,
            "retry_count": max(0, attempts - 1),
        }

    def _wait_for_dashboard(self) -> dict:
        commands: list[dict] = []
        deadline = time.monotonic() + self.dashboard_timeout_seconds
        while True:
            result = self._command(["curl", "-fsS", "--max-time", "2", DASHBOARD_HEALTH_URL], allow_failure=True)
            commands.append(result)
            if result["returncode"] == 0:
                return {"status": "pass", "commands": commands}
            if time.monotonic() >= deadline:
                return {"status": "fail", "error": "dashboard health check failed after bootstrap", "commands": commands}
            self.sleep(self.dashboard_poll_seconds)

    def _rollback_failed_install(self, run_date: str, results: list[dict], restart_loaded: bool) -> dict:
        restorable = [
            {
                "label": str(item.get("label") or ""),
                "backup": str(item.get("backup") or ""),
                "target": str(item.get("target") or ""),
                "created": bool(item.get("created")),
                "action": ("remove_created_job_and_restart" if restart_loaded else "remove_created_job_without_restart") if item.get("created") and not item.get("backup") else "",
            }
            for item in results
            if item.get("backup") or item.get("created")
        ]
        if not restorable:
            return {"status": "skipped", "reason": "no_backups_to_restore", "jobs": []}
        rollback_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        pre_rollback_backup_dir = self.output_root / "schedules" / "rollback_target_backups" / f"failed_install_{rollback_id}"
        rollback_jobs = [self._rollback_job(job, restart_loaded, pre_rollback_backup_dir) for job in restorable]
        failed = [item for item in rollback_jobs if item["status"] == "fail"]
        return {
            "run_date": run_date,
            "status": "fail" if failed else "rolled_back",
            "reason": "install_failed",
            "pre_rollback_backup_dir": str(pre_rollback_backup_dir),
            "jobs": rollback_jobs,
        }

    def _command(self, command: list[str], allow_failure: bool = False) -> dict:
        try:
            result = self.command_runner(command)
        except (OSError, subprocess.SubprocessError) as exc:
            if allow_failure:
                return {"command": command, "returncode": 1, "stdout": "", "stderr": str(exc)}
            raise
        return {
            "command": command,
            "returncode": result.returncode,
            "stdout": (result.stdout or "").strip() if isinstance(result.stdout, str) else "",
            "stderr": (result.stderr or "").strip() if isinstance(result.stderr, str) else "",
        }

    def _run_command(self, command: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
