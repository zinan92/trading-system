from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.config_loader import ROOT, load_json_yaml, load_pipeline_config
from services.connector_activation_plan import build_config_apply_package_id, config_apply_patch_digest
from services.connector_onboarding import reject_secret_payload
from services.dualtrack_clock import comex_futures_session_status
from services.journal_store import load_json, write_json


CONFIG_APPLY_SCHEMA_VERSION = "connector-config-apply-v1"
CONFIG_ROLLBACK_SCHEMA_VERSION = "connector-config-rollback-v1"
CONFIG_CHECK_SCHEMA_VERSION = "connector-config-check-v1"
CONFIG_HANDOFF_SCHEMA_VERSION = "connector-config-handoff-v1"
CONFIG_REHEARSAL_SCHEMA_VERSION = "connector-config-rehearsal-v1"
CONFIG_AUTHORIZATION_SCHEMA_VERSION = "connector-config-authorization-v1"
CONFIG_READINESS_AUDIT_SCHEMA_VERSION = "connector-final-readiness-audit-v1"
CONFIG_POST_SWITCH_SCHEMA_VERSION = "connector-post-switch-validation-v1"
CONFIG_PRICE_FEED_REFRESH_RUNBOOK_SCHEMA_VERSION = "connector-price-feed-refresh-runbook-v1"
ROLLBACK_ACKNOWLEDGEMENT = "I_UNDERSTAND_CONNECTOR_CONFIG_ROLLBACK_WILL_OVERWRITE_CURRENT_CONFIG"
PRICE_FEED_EVIDENCE_MAX_AGE_SECONDS = 15 * 60
CONFIG_SWITCH_EVIDENCE_MAX_AGE_SECONDS = 30 * 60


class ConnectorConfigApply:
    """Apply a connector activation plan to local config with backup/rollback.

    This is intentionally separate from ConnectorActivationPlan. The activation
    plan remains a preview; this class is the attended config-writer boundary.
    """

    def __init__(
        self,
        output_root: Path | None = None,
        *,
        pipeline_config_path: Path | None = None,
        dualtrack_config_path: Path | None = None,
    ) -> None:
        pipeline_config = load_pipeline_config()
        self.output_root = Path(output_root) if output_root else ROOT / str(pipeline_config.get("output_root", "outputs"))
        self.pipeline_config_path = Path(pipeline_config_path) if pipeline_config_path else ROOT / "configs" / "pipeline.yaml"
        self.dualtrack_config_path = Path(dualtrack_config_path) if dualtrack_config_path else ROOT / "configs" / "dualtrack.yaml"

    def apply(
        self,
        payload: dict[str, Any],
        *,
        persist: bool = True,
        require_current_rehearsal: bool = True,
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("body must be a JSON object")
        reject_secret_payload(payload)
        plan = payload.get("activation_plan")
        if not isinstance(plan, dict):
            raise ValueError("activation_plan is required")
        dry_run = payload.get("write") is not True
        acknowledgement = str(payload.get("acknowledgement") or "")
        accept_warnings = payload.get("accept_warnings") is True
        package_id = str(payload.get("package_id") or "")

        validation = self._validate_plan(
            plan,
            acknowledgement=acknowledgement,
            accept_warnings=accept_warnings,
            dry_run=dry_run,
            package_id=package_id,
            require_current_rehearsal=require_current_rehearsal,
        )
        apply_id = self._apply_id()
        pipeline_changes = [row for row in (plan.get("config_patch_preview") or []) if isinstance(row, dict)]
        profile = plan.get("dualtrack_profile_preview", {}) if isinstance(plan.get("dualtrack_profile_preview"), dict) else {}
        dualtrack_changes = [row for row in (profile.get("dualtrack_config_patch_preview") or []) if isinstance(row, dict)]
        config_targets = self._config_targets(pipeline_changes, dualtrack_changes)
        receipt: dict[str, Any] = {
            "schema_version": CONFIG_APPLY_SCHEMA_VERSION,
            "apply_id": apply_id,
            "checked_at": self._now(),
            "status": "blocked" if validation["blockers"] else ("dry_run_ready" if dry_run else "applied"),
            "mode": "dry_run" if dry_run else "write",
            "connector_id": str(plan.get("connector_id") or ""),
            "requested_roles": list(plan.get("requested_roles") or []),
            "package_id": str((plan.get("config_apply_package") or {}).get("package_id") or "") if isinstance(plan.get("config_apply_package"), dict) else "",
            "activation_plan_status": str(plan.get("status") or ""),
            "activation_gate_status": str((plan.get("activation_gate") or {}).get("status") or "") if isinstance(plan.get("activation_gate"), dict) else "",
            "blockers": validation["blockers"],
            "warnings": validation["warnings"],
            "accepted_warnings": accept_warnings,
            "config_targets": config_targets,
            "change_counts": {
                "pipeline": len(pipeline_changes),
                "dualtrack": len(dualtrack_changes),
                "total": len(pipeline_changes) + len(dualtrack_changes),
            },
            "backup": {
                "required": True,
                "created": False,
                "backup_dir": "",
                "files": [],
            },
            "rollback": {
                "available": False,
                "acknowledgement": ROLLBACK_ACKNOWLEDGEMENT,
                "receipt_path": "",
                "backup_dir": "",
            },
            "safety": self._safety(writes_runtime_config=not dry_run and not validation["blockers"]),
        }
        if validation["blockers"]:
            if persist:
                self._write_apply_receipt(receipt)
            return receipt

        if not dry_run:
            backup = self._backup_configs(apply_id, config_targets)
            self._write_configs(pipeline_changes, dualtrack_changes)
            receipt["backup"] = backup
            receipt["rollback"] = {
                "available": True,
                "acknowledgement": ROLLBACK_ACKNOWLEDGEMENT,
                "receipt_path": str(self.output_root / "connector_config_apply" / f"{apply_id}.json"),
                "backup_dir": backup["backup_dir"],
            }
        if persist:
            self._write_apply_receipt(receipt)
        return receipt

    def rollback(self, payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("body must be a JSON object")
        reject_secret_payload(payload)
        receipt = payload.get("apply_receipt")
        if not isinstance(receipt, dict):
            raise ValueError("apply_receipt is required")
        acknowledgement = str(payload.get("acknowledgement") or "")
        rollback_id = self._apply_id(prefix="rollback")
        blockers: list[dict[str, Any]] = []
        if acknowledgement != ROLLBACK_ACKNOWLEDGEMENT:
            blockers.append({"name": "rollback_acknowledgement", "summary": "Exact rollback acknowledgement is required."})
        backup = receipt.get("backup", {}) if isinstance(receipt.get("backup"), dict) else {}
        backup_dir = Path(str(backup.get("backup_dir") or ""))
        files = [row for row in (backup.get("files") or []) if isinstance(row, dict)]
        if not backup.get("created") or not files or not backup_dir.exists():
            blockers.append({"name": "rollback_backup_missing", "summary": "Apply receipt does not contain a usable backup."})

        result: dict[str, Any] = {
            "schema_version": CONFIG_ROLLBACK_SCHEMA_VERSION,
            "rollback_id": rollback_id,
            "apply_id": str(receipt.get("apply_id") or ""),
            "checked_at": self._now(),
            "status": "blocked" if blockers else "rolled_back",
            "blockers": blockers,
            "restored_files": [],
            "safety": self._safety(writes_runtime_config=not blockers),
        }
        if blockers:
            if persist:
                self._write_rollback_receipt(result)
            return result

        restored = []
        for row in files:
            target = Path(str(row.get("target") or ""))
            backup_path = Path(str(row.get("backup") or ""))
            if not backup_path.exists():
                raise ValueError(f"backup file missing: {backup_path}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup_path, target)
            restored.append({"target": str(target), "backup": str(backup_path), "sha256": self._sha256(target)})
        result["restored_files"] = restored
        if persist:
            self._write_rollback_receipt(result)
        return result

    def check_plan(
        self,
        payload: dict[str, Any],
        *,
        persist: bool = True,
        require_current_handoff: bool = True,
        require_current_rehearsal: bool = True,
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("body must be a JSON object")
        reject_secret_payload(payload)
        plan = payload.get("activation_plan")
        if not isinstance(plan, dict):
            raise ValueError("activation_plan is required")
        package = plan.get("config_apply_package", {}) if isinstance(plan.get("config_apply_package"), dict) else {}
        package_id = str(package.get("package_id") or "")
        recomputed_package_id = self._recompute_package_id(plan, package)
        latest_apply = self._latest_apply_receipt()
        apply_safety = latest_apply.get("safety", {}) if isinstance(latest_apply.get("safety"), dict) else {}
        warnings = [row for row in (plan.get("warnings") or []) if isinstance(row, dict)]
        blockers: list[dict[str, Any]] = []
        if plan.get("schema_version") != "connector-activation-plan-v1":
            blockers.append({"name": "activation_plan_schema", "summary": "Unsupported or missing activation plan schema."})
        if plan.get("status") == "blocked" or plan.get("blockers"):
            blockers.append({"name": "activation_plan_blocked", "summary": "Blocked activation plans cannot be authorized."})
        if not package_id:
            blockers.append({"name": "config_apply_package_id_missing", "summary": "Activation plan has no config apply package id."})
        elif package_id != recomputed_package_id:
            blockers.append({"name": "config_apply_package_id_stale", "summary": "Config apply package id does not match the activation plan contents."})
        if not latest_apply:
            blockers.append({"name": "config_apply_dry_run_missing", "summary": "Run config apply dry-run before authorizing config write."})
        elif latest_apply.get("status") != "dry_run_ready":
            blockers.append({"name": "config_apply_dry_run_not_ready", "summary": "Latest config apply receipt is not a ready dry-run."})
        elif str(latest_apply.get("package_id") or "") != package_id:
            blockers.append({"name": "config_apply_dry_run_package_mismatch", "summary": "Latest dry-run package id does not match the activation package."})
        elif apply_safety.get("writes_runtime_config") is not False:
            blockers.append({"name": "config_apply_dry_run_safety", "summary": "Latest dry-run receipt does not prove a no-write safety boundary."})

        write_blockers: list[dict[str, Any]] = []
        if require_current_handoff and not blockers:
            write_blockers = self._validate_handoff_for_write(plan, package_id)
        if require_current_rehearsal and not blockers and not write_blockers:
            write_blockers = self._validate_rehearsal_for_write(plan, package_id)

        action = "resolve_config_package_check"
        if not blockers and write_blockers:
            if any(str(row.get("name") or "").startswith("config_handoff") for row in write_blockers):
                action = "generate_current_operator_handoff"
            else:
                action = "run_sandbox_config_rehearsal"
        elif not blockers:
            action = "authorize_attended_config_write_with_warning_acceptance" if warnings else "authorize_attended_config_write"
        if blockers:
            status = "blocked"
        elif write_blockers and action == "generate_current_operator_handoff":
            status = "ready_for_handoff"
        elif write_blockers:
            status = "ready_for_rehearsal"
        else:
            status = "ready_for_attended_config_write"
        result = {
            "schema_version": CONFIG_CHECK_SCHEMA_VERSION,
            "checked_at": self._now(),
            "status": status,
            "package_id": package_id,
            "recomputed_package_id": recomputed_package_id,
            "usable_for_attended_config_write": not blockers and not write_blockers,
            "blockers": blockers,
            "warnings": [{"name": str(row.get("name") or "warning"), "summary": str(row.get("summary") or "")} for row in warnings],
            "latest_dry_run": self._apply_summary(latest_apply),
            "write_preflight": {
                "requires_current_handoff": require_current_handoff,
                "requires_current_rehearsal": require_current_rehearsal,
                "status": "skipped" if blockers else ("ready" if not write_blockers else "blocked"),
                "usable_for_attended_config_write": not blockers and not write_blockers,
                "blockers": write_blockers,
                "latest_handoff": self._handoff_summary(self._latest_handoff_receipt()),
                "latest_rehearsal": self._rehearsal_summary(self._latest_rehearsal_receipt()),
            },
            "attended_apply_command": str(package.get("attended_apply_command") or ""),
            "post_apply_validation_commands": [
                str(command)
                for command in (package.get("post_apply_validation_commands") or [])
                if command
            ],
            "rollback_boundary": package.get("rollback", {}) if isinstance(package.get("rollback"), dict) else {},
            "operator_next_action": {
                "action": action,
                "requires_accept_warnings": bool(warnings),
                "requires_acknowledgement": True,
                "requires_package_id": True,
            },
            "safety": {
                "read_only": True,
                "writes_runtime_config": False,
                "opens_network_clients": False,
                "submits_orders": False,
                "cancels_orders": False,
                "closes_positions": False,
                "credential_values_exposed": False,
                "can_enable_broker_orders": False,
            },
        }
        if persist:
            self._write_check_receipt(result)
        return result

    def handoff(self, payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("body must be a JSON object")
        reject_secret_payload(payload)
        plan = payload.get("activation_plan")
        if not isinstance(plan, dict):
            raise ValueError("activation_plan is required")
        check = self.check_plan(
            {"activation_plan": plan},
            persist=False,
            require_current_handoff=False,
            require_current_rehearsal=False,
        )
        package = plan.get("config_apply_package", {}) if isinstance(plan.get("config_apply_package"), dict) else {}
        current_runtime = self._runtime_summary()
        evidence_chain = self._handoff_evidence_chain(plan, check, current_runtime)
        handoff_id = self._apply_id(prefix="handoff")
        result = {
            "schema_version": CONFIG_HANDOFF_SCHEMA_VERSION,
            "handoff_id": handoff_id,
            "checked_at": self._now(),
            "status": "ready_for_operator_review" if check.get("status") != "blocked" else "blocked",
            "package_id": str(package.get("package_id") or ""),
            "connector_id": str(plan.get("connector_id") or ""),
            "requested_roles": list(plan.get("requested_roles") or []),
            "current_runtime": current_runtime,
            "check": {
                "status": str(check.get("status") or ""),
                "usable_for_attended_config_write": check.get("usable_for_attended_config_write") is True,
                "blocker_count": len([row for row in (check.get("blockers") or []) if isinstance(row, dict)]),
                "warning_count": len([row for row in (check.get("warnings") or []) if isinstance(row, dict)]),
                "operator_next_action": check.get("operator_next_action", {}) if isinstance(check.get("operator_next_action"), dict) else {},
            },
            "evidence_chain": evidence_chain,
            "attended_apply_command": str(check.get("attended_apply_command") or ""),
            "post_apply_validation_commands": list(check.get("post_apply_validation_commands") or []),
            "rollback_boundary": check.get("rollback_boundary", {}) if isinstance(check.get("rollback_boundary"), dict) else {},
            "not_authorized": [
                "writing runtime config from this handoff command",
                "switching active broker without attended config apply",
                "opening Tiger SDK clients",
                "submitting, cancelling, or closing broker orders",
                "arming Tiger paper TradeClient submission",
                "launchd schedule takeover",
                "strategy promotion or real-money execution",
            ],
            "artifacts": {
                "activation_plan": str(self.output_root / "connector_activation_plan" / "current.json"),
                "dry_run": str(self.output_root / "connector_config_apply" / "current.json"),
                "check": str(self.output_root / "connector_config_apply" / "check_current.json"),
                "handoff_json": str(self.output_root / "connector_config_apply" / "handoff_current.json"),
                "handoff_markdown": str(self.output_root / "connector_config_apply" / "handoff_current.md"),
            },
            "safety": {
                "read_only": True,
                "writes_runtime_config": False,
                "opens_network_clients": False,
                "submits_orders": False,
                "cancels_orders": False,
                "closes_positions": False,
                "credential_values_exposed": False,
                "can_enable_broker_orders": False,
            },
        }
        if persist:
            self._write_handoff(result)
        return result

    def rehearse(self, payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("body must be a JSON object")
        reject_secret_payload(payload)
        plan = payload.get("activation_plan")
        if not isinstance(plan, dict):
            raise ValueError("activation_plan is required")
        rehearsal_id = self._apply_id(prefix="rehearsal")
        rehearsal_root = self.output_root / "connector_config_apply" / "rehearsals" / rehearsal_id
        sandbox_output = rehearsal_root / "outputs"
        sandbox_config = rehearsal_root / "configs"
        sandbox_pipeline = sandbox_config / self.pipeline_config_path.name
        sandbox_dualtrack = sandbox_config / self.dualtrack_config_path.name
        real_before = self._runtime_config_evidence()
        sandbox_config.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.pipeline_config_path, sandbox_pipeline)
        shutil.copy2(self.dualtrack_config_path, sandbox_dualtrack)
        write_json(sandbox_output / "connector_activation_plan" / "current.json", [plan])
        self._seed_rehearsal_support(sandbox_output, rehearsal_root)

        sandbox = ConnectorConfigApply(
            sandbox_output,
            pipeline_config_path=sandbox_pipeline,
            dualtrack_config_path=sandbox_dualtrack,
        )
        dry_run = sandbox.apply({"activation_plan": plan})
        pre_handoff_check = sandbox.check_plan({"activation_plan": plan}, require_current_rehearsal=False)
        handoff = sandbox.handoff({"activation_plan": plan})
        write_check = sandbox.check_plan({"activation_plan": plan}, require_current_rehearsal=False)
        sandbox_authorization = sandbox._authorization({"activation_plan": plan}, require_current_rehearsal=False)
        sandbox_readiness_audit = sandbox.readiness_audit({"activation_plan": plan})
        package = plan.get("config_apply_package", {}) if isinstance(plan.get("config_apply_package"), dict) else {}
        applied = sandbox.apply(
            {
                "activation_plan": plan,
                "write": True,
                "accept_warnings": True,
                "acknowledgement": str(package.get("operator_acknowledgement") or ""),
                "package_id": str(package.get("package_id") or ""),
            },
            require_current_rehearsal=False,
        )
        post_switch_validation = sandbox.post_switch_validate({"package_id": str(package.get("package_id") or "")})
        rollback = sandbox.rollback({"apply_receipt": applied, "acknowledgement": ROLLBACK_ACKNOWLEDGEMENT})
        real_after = self._runtime_config_evidence()
        sandbox_after_rollback = {
            "pipeline_sha256": self._sha256(sandbox_pipeline),
            "dualtrack_sha256": self._sha256(sandbox_dualtrack),
        }
        result = {
            "schema_version": CONFIG_REHEARSAL_SCHEMA_VERSION,
            "rehearsal_id": rehearsal_id,
            "checked_at": self._now(),
            "status": self._rehearsal_status(dry_run, pre_handoff_check, handoff, write_check, sandbox_authorization, sandbox_readiness_audit, applied, post_switch_validation, rollback, real_before, real_after),
            "package_id": str(package.get("package_id") or ""),
            "sandbox": {
                "root": str(rehearsal_root),
                "output_root": str(sandbox_output),
                "pipeline_config": str(sandbox_pipeline),
                "dualtrack_config": str(sandbox_dualtrack),
            },
            "steps": {
                "dry_run": self._apply_summary(dry_run),
                "pre_handoff_check": self._check_summary(pre_handoff_check),
                "handoff": self._handoff_summary(handoff),
                "write_check": self._check_summary(write_check),
                "authorization": self._authorization_summary(sandbox_authorization),
                "readiness_audit": self._readiness_audit_summary(sandbox_readiness_audit),
                "sandbox_apply": self._apply_summary(applied),
                "post_switch_validation": self._post_switch_validation_summary(post_switch_validation),
                "sandbox_rollback": self._rollback_summary(rollback),
            },
            "runtime_config": {
                "before": real_before,
                "after": real_after,
                "unchanged": real_before == real_after,
            },
            "sandbox_after_rollback": sandbox_after_rollback,
            "safety": {
                "writes_runtime_config": False,
                "writes_sandbox_config": True,
                "opens_network_clients": False,
                "submits_orders": False,
                "cancels_orders": False,
                "closes_positions": False,
                "credential_values_exposed": False,
                "can_enable_broker_orders": False,
            },
        }
        if persist:
            self._write_rehearsal(result)
        return result

    def authorization(self, payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
        return self._authorization(payload, persist=persist, require_current_rehearsal=True)

    def _authorization(
        self,
        payload: dict[str, Any],
        *,
        persist: bool = True,
        require_current_rehearsal: bool = True,
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("body must be a JSON object")
        reject_secret_payload(payload)
        plan = payload.get("activation_plan")
        if not isinstance(plan, dict):
            raise ValueError("activation_plan is required")
        check = self.check_plan(
            {"activation_plan": plan},
            persist=persist,
            require_current_rehearsal=require_current_rehearsal,
        )
        handoff = self._latest_handoff_receipt()
        rehearsal = self._latest_rehearsal_receipt()
        current_runtime = self._runtime_summary()
        package = plan.get("config_apply_package", {}) if isinstance(plan.get("config_apply_package"), dict) else {}
        package_id = str(package.get("package_id") or "")
        blockers = self._authorization_blockers(
            check,
            handoff,
            rehearsal,
            current_runtime,
            package_id,
            require_current_rehearsal=require_current_rehearsal,
        )
        authorization_id = self._apply_id(prefix="authorization")
        result = {
            "schema_version": CONFIG_AUTHORIZATION_SCHEMA_VERSION,
            "authorization_id": authorization_id,
            "checked_at": self._now(),
            "status": "ready_for_operator_authorization" if not blockers else "blocked",
            "package_id": package_id,
            "connector_id": str(plan.get("connector_id") or ""),
            "requested_roles": list(plan.get("requested_roles") or []),
            "current_runtime": current_runtime,
            "check": self._check_summary(check),
            "handoff": self._handoff_summary(handoff),
            "rehearsal": self._rehearsal_summary(rehearsal),
            "operator_next_action": check.get("operator_next_action", {}) if isinstance(check.get("operator_next_action"), dict) else {},
            "attended_apply_command": str(check.get("attended_apply_command") or ""),
            "post_apply_validation_commands": [
                str(command)
                for command in (check.get("post_apply_validation_commands") or [])
                if command
            ],
            "rollback_boundary": check.get("rollback_boundary", {}) if isinstance(check.get("rollback_boundary"), dict) else {},
            "blockers": blockers,
            "not_authorized": [
                "writing runtime config from this authorization package",
                "opening Tiger SDK clients",
                "submitting, cancelling, or closing broker orders",
                "arming Tiger paper TradeClient submission",
                "launchd schedule takeover",
                "strategy promotion or real-money execution",
            ],
            "artifacts": {
                "activation_plan": str(self.output_root / "connector_activation_plan" / "current.json"),
                "dry_run": str(self.output_root / "connector_config_apply" / "current.json"),
                "check": str(self.output_root / "connector_config_apply" / "check_current.json"),
                "handoff_json": str(self.output_root / "connector_config_apply" / "handoff_current.json"),
                "handoff_markdown": str(self.output_root / "connector_config_apply" / "handoff_current.md"),
                "rehearsal_json": str(self.output_root / "connector_config_apply" / "rehearsal_current.json"),
                "authorization_json": str(self.output_root / "connector_config_apply" / "authorization_current.json"),
                "authorization_markdown": str(self.output_root / "connector_config_apply" / "authorization_current.md"),
            },
            "safety": {
                "read_only": True,
                "writes_runtime_config": False,
                "opens_network_clients": False,
                "submits_orders": False,
                "cancels_orders": False,
                "closes_positions": False,
                "credential_values_exposed": False,
                "can_enable_broker_orders": False,
            },
        }
        if persist:
            self._write_authorization(result)
        return result

    def readiness_audit(self, payload: dict[str, Any] | None = None, *, persist: bool = True) -> dict[str, Any]:
        payload = payload or {}
        if not isinstance(payload, dict):
            raise ValueError("body must be a JSON object")
        reject_secret_payload(payload)
        plan = payload.get("activation_plan")
        if not isinstance(plan, dict):
            plan = self._latest_activation_plan()
        current_runtime = self._runtime_summary()
        price_feed_acceptance = self._latest_output_receipt("tiger_price_feed_acceptance")
        price_feed_readiness = self._latest_output_receipt("tiger_price_feed_readiness")
        realtime_validation = self._latest_output_receipt("tiger_realtime_validation")
        futures_feed = self._latest_output_receipt("tiger_futures_feed")
        paper_order_readiness = self._latest_output_receipt("tiger_paper_order_readiness")
        paper_order_approval = self._latest_output_receipt("tiger_paper_order_approval")
        authorization = self._latest_authorization_receipt()
        market_coverage = self._tiger_market_coverage()
        activation_profile = plan.get("dualtrack_profile_preview", {}) if isinstance(plan.get("dualtrack_profile_preview"), dict) else {}
        activation_gates = activation_profile.get("gates", {}) if isinstance(activation_profile.get("gates"), dict) else {}
        blockers = self._readiness_audit_blockers(
            price_feed_acceptance=price_feed_acceptance,
            price_feed_readiness=price_feed_readiness,
            realtime_validation=realtime_validation,
            futures_feed=futures_feed,
            authorization=authorization,
            market_coverage=market_coverage,
            current_runtime=current_runtime,
        )
        config_switch_ready = not blockers
        audit_id = self._apply_id(prefix="readiness_audit")
        result = {
            "schema_version": CONFIG_READINESS_AUDIT_SCHEMA_VERSION,
            "audit_id": audit_id,
            "checked_at": self._now(),
            "status": "go_for_attended_config_switch" if config_switch_ready else "blocked",
            "current_stage": "pre_switch_authorization_ready" if config_switch_ready else "pre_switch_blocked",
            "progress": {"bar": "██████████", "percent": 99.99 if config_switch_ready else 95.0},
            "package_id": str(authorization.get("package_id") or ""),
            "can_switch_config_with_operator_authorization": config_switch_ready,
            "can_trade_machine_track": False,
            "can_submit_tiger_orders": False,
            "current_runtime": current_runtime,
            "price_feed": {
                "acceptance": self._artifact_status(price_feed_acceptance, extra_keys=["ready_for_price_feed", "checked_at"]),
                "readiness": self._artifact_status(price_feed_readiness, extra_keys=["ready_for_price_feed", "checked_at"]),
                "realtime_validation": self._artifact_status(realtime_validation, extra_keys=["checked_at"]),
                "futures_feed": self._artifact_status(futures_feed, extra_keys=["ready", "checked_at"]),
                "market_coverage": market_coverage,
            },
            "config_authorization": self._authorization_summary(authorization),
            "strategy_gate": {
                "requires_strategy_edge_approval": activation_gates.get("requires_strategy_edge_approval") is True,
                "can_enable_broker_orders_from_this_profile": activation_gates.get("can_enable_broker_orders_from_this_profile") is True,
                "status": "not_trade_ready",
                "summary": "Strategy edge approval remains separate from the Tiger/MGC config switch.",
            },
            "broker_order_gate": {
                "paper_order_readiness": self._artifact_status(paper_order_readiness, extra_keys=["ready_for_attended_paper_order", "checked_at"]),
                "paper_order_approval": self._artifact_status(
                    paper_order_approval,
                    extra_keys=[
                        "submit_requested",
                        "can_submit_without_explicit_operator_authorization",
                        "real_tiger_network_call_attempted",
                        "checked_at",
                    ],
                ),
                "status": "closed",
                "summary": "Tiger order submission remains a separate operator authorization path.",
            },
            "operator_next_action": {
                "action": "review_authorization_package_and_run_attended_apply_if_approved" if config_switch_ready else "resolve_readiness_audit_blockers",
                "authorization_markdown": str(self.output_root / "connector_config_apply" / "authorization_current.md"),
                "audit_markdown": str(self.output_root / "connector_config_apply" / "final_readiness_audit_current.md"),
            },
            "blockers": blockers,
            "not_authorized": [
                "writing runtime config from this audit",
                "opening Tiger SDK clients",
                "submitting, cancelling, or closing broker orders",
                "arming Tiger paper TradeClient submission",
                "launchd schedule takeover",
                "strategy promotion or real-money execution",
            ],
            "safety": {
                "read_only": True,
                "writes_runtime_config": False,
                "writes_market_db": False,
                "opens_network_clients": False,
                "submits_orders": False,
                "cancels_orders": False,
                "closes_positions": False,
                "credential_values_exposed": False,
                "can_enable_broker_orders": False,
            },
        }
        if persist:
            self._write_readiness_audit(result)
        return result

    def post_switch_validate(self, payload: dict[str, Any] | None = None, *, persist: bool = True) -> dict[str, Any]:
        payload = payload or {}
        if not isinstance(payload, dict):
            raise ValueError("body must be a JSON object")
        reject_secret_payload(payload)
        expected_package_id = str(payload.get("package_id") or "")
        latest_apply = self._latest_apply_receipt()
        latest_authorization = self._latest_authorization_receipt()
        latest_readiness_audit = self._latest_readiness_audit_receipt()
        current_runtime = self._runtime_summary()
        price_feed_acceptance = self._latest_output_receipt("tiger_price_feed_acceptance")
        realtime_validation = self._latest_output_receipt("tiger_realtime_validation")
        market_coverage = self._tiger_market_coverage()
        package_id = expected_package_id or str(latest_authorization.get("package_id") or latest_apply.get("package_id") or "")
        blockers = self._post_switch_blockers(
            package_id=package_id,
            latest_apply=latest_apply,
            latest_authorization=latest_authorization,
            latest_readiness_audit=latest_readiness_audit,
            current_runtime=current_runtime,
            price_feed_acceptance=price_feed_acceptance,
            realtime_validation=realtime_validation,
            market_coverage=market_coverage,
        )
        validation_id = self._apply_id(prefix="post_switch")
        result = {
            "schema_version": CONFIG_POST_SWITCH_SCHEMA_VERSION,
            "validation_id": validation_id,
            "checked_at": self._now(),
            "status": "validated_post_switch" if not blockers else "blocked",
            "package_id": package_id,
            "current_runtime": current_runtime,
            "latest_apply": self._apply_summary(latest_apply),
            "latest_authorization": self._authorization_summary(latest_authorization),
            "latest_readiness_audit": self._readiness_audit_summary(latest_readiness_audit),
            "price_feed": {
                "acceptance": self._artifact_status(price_feed_acceptance, extra_keys=["ready_for_price_feed", "checked_at"]),
                "realtime_validation": self._artifact_status(realtime_validation, extra_keys=["checked_at"]),
                "market_coverage": market_coverage,
            },
            "rollback": {
                "backup_created": bool((latest_apply.get("backup") or {}).get("created")) if isinstance(latest_apply.get("backup"), dict) else False,
                "backup_dir": str((latest_apply.get("backup") or {}).get("backup_dir") or "") if isinstance(latest_apply.get("backup"), dict) else "",
                "available": bool((latest_apply.get("rollback") or {}).get("available")) if isinstance(latest_apply.get("rollback"), dict) else False,
            },
            "blockers": blockers,
            "operator_next_action": {
                "action": "run_post_apply_validation_commands_and_keep_orders_disabled" if not blockers else "resolve_post_switch_validation_blockers",
                "rollback_acknowledgement": ROLLBACK_ACKNOWLEDGEMENT,
            },
            "not_authorized": [
                "opening Tiger SDK clients",
                "submitting, cancelling, or closing broker orders",
                "arming Tiger paper TradeClient submission",
                "launchd schedule takeover",
                "strategy promotion or real-money execution",
            ],
            "safety": {
                "read_only": True,
                "writes_runtime_config": False,
                "writes_market_db": False,
                "opens_network_clients": False,
                "submits_orders": False,
                "cancels_orders": False,
                "closes_positions": False,
                "credential_values_exposed": False,
                "can_enable_broker_orders": False,
            },
        }
        if persist:
            self._write_post_switch_validation(result)
        return result

    def status(self) -> dict[str, Any]:
        rollback_rows = load_json(self.output_root / "connector_config_apply" / "rollback" / "current.json")
        check_rows = load_json(self.output_root / "connector_config_apply" / "check_current.json")
        handoff_rows = load_json(self.output_root / "connector_config_apply" / "handoff_current.json")
        rehearsal_rows = load_json(self.output_root / "connector_config_apply" / "rehearsal_current.json")
        authorization_rows = load_json(self.output_root / "connector_config_apply" / "authorization_current.json")
        readiness_audit_rows = load_json(self.output_root / "connector_config_apply" / "final_readiness_audit_current.json")
        post_switch_rows = load_json(self.output_root / "connector_config_apply" / "post_switch_validation_current.json")
        price_feed_refresh_runbook_rows = load_json(self.output_root / "connector_config_apply" / "price_feed_refresh_runbook_current.json")
        latest_apply = self._latest_apply_receipt()
        latest_rollback = rollback_rows[-1] if rollback_rows else {}
        latest_check = check_rows[-1] if check_rows else {}
        latest_handoff = handoff_rows[-1] if handoff_rows else {}
        latest_rehearsal = rehearsal_rows[-1] if rehearsal_rows else {}
        latest_authorization = authorization_rows[-1] if authorization_rows else {}
        latest_readiness_audit = readiness_audit_rows[-1] if readiness_audit_rows else {}
        latest_post_switch = post_switch_rows[-1] if post_switch_rows else {}
        latest_price_feed_refresh_runbook = price_feed_refresh_runbook_rows[-1] if price_feed_refresh_runbook_rows else {}
        backup = latest_apply.get("backup", {}) if isinstance(latest_apply.get("backup"), dict) else {}
        current_runtime = self._runtime_summary()
        price_feed = {
            "acceptance": self._artifact_status(
                self._latest_output_receipt("tiger_price_feed_acceptance"),
                extra_keys=["ready_for_price_feed", "checked_at", "run_date", "contract"],
                max_age_seconds=PRICE_FEED_EVIDENCE_MAX_AGE_SECONDS,
            ),
            "readiness": self._artifact_status(
                self._latest_output_receipt("tiger_price_feed_readiness"),
                extra_keys=["ready_for_price_feed", "checked_at", "run_date", "contract"],
                max_age_seconds=PRICE_FEED_EVIDENCE_MAX_AGE_SECONDS,
            ),
            "realtime_validation": self._artifact_status(
                self._latest_output_receipt("tiger_realtime_validation"),
                extra_keys=["checked_at", "run_date", "contract"],
                max_age_seconds=PRICE_FEED_EVIDENCE_MAX_AGE_SECONDS,
            ),
        }
        summaries = {
            "latest_apply": self._apply_summary(latest_apply),
            "latest_check": self._check_summary(latest_check),
            "latest_handoff": self._handoff_summary(latest_handoff),
            "latest_rehearsal": self._rehearsal_summary(latest_rehearsal),
            "latest_authorization": self._authorization_summary(latest_authorization),
            "latest_readiness_audit": self._readiness_audit_summary(latest_readiness_audit),
            "latest_post_switch_validation": self._post_switch_validation_summary(latest_post_switch),
            "latest_price_feed_refresh_runbook": self._price_feed_refresh_runbook_summary(latest_price_feed_refresh_runbook),
            "latest_rollback": self._rollback_summary(latest_rollback),
        }
        return {
            "schema_version": "connector-config-status-v1",
            "checked_at": self._now(),
            "status": str(latest_apply.get("status") or "missing"),
            **summaries,
            "current_runtime": current_runtime,
            "price_feed": price_feed,
            "operator_stage": self._operator_stage_summary(summaries, current_runtime, price_feed),
            "backup": {
                "created": backup.get("created") is True,
                "backup_dir": str(backup.get("backup_dir") or ""),
                "file_count": len([row for row in (backup.get("files") or []) if isinstance(row, dict)]),
            },
            "safety": {
                "read_only": True,
                "opens_network_clients": False,
                "submits_orders": False,
                "writes_runtime_config": False,
                "credential_values_exposed": False,
            },
        }

    def price_feed_refresh_runbook(self, *, persist: bool = True, as_of: str | datetime | None = None) -> dict[str, Any]:
        status = self.status()
        operator_stage = status.get("operator_stage", {}) if isinstance(status.get("operator_stage"), dict) else {}
        commands = [row for row in (operator_stage.get("refresh_commands") or []) if isinstance(row, dict)]
        runbook_status = "ready_for_operator_refresh" if commands else "not_required"
        refresh_window_gate = self._price_feed_refresh_window_gate(commands, as_of=as_of)
        receipt = {
            "schema_version": CONFIG_PRICE_FEED_REFRESH_RUNBOOK_SCHEMA_VERSION,
            "runbook_id": self._apply_id(prefix="price_feed_refresh_runbook"),
            "checked_at": self._now(),
            "status": runbook_status,
            "operator_stage": {
                "stage": str(operator_stage.get("stage") or ""),
                "summary": str(operator_stage.get("summary") or ""),
                "next_action": str(operator_stage.get("next_action") or ""),
                "runtime_switched_to_tiger_mgc": operator_stage.get("runtime_switched_to_tiger_mgc") is True,
                "price_feed_status_ready": operator_stage.get("price_feed_status_ready") is True,
                "price_feed_evidence_fresh": operator_stage.get("price_feed_evidence_fresh") is True,
                "price_feed_ready": operator_stage.get("price_feed_ready") is True,
                "can_switch_config_with_operator_authorization": operator_stage.get("can_switch_config_with_operator_authorization") is True,
                "can_trade_machine_track": operator_stage.get("can_trade_machine_track") is True,
                "can_submit_tiger_orders": operator_stage.get("can_submit_tiger_orders") is True,
            },
            "current_runtime": status.get("current_runtime", {}) if isinstance(status.get("current_runtime"), dict) else {},
            "price_feed": status.get("price_feed", {}) if isinstance(status.get("price_feed"), dict) else {},
            "status_receipt": status,
            "command_sequence": commands,
            "refresh_window_gate": refresh_window_gate,
            "artifacts": {
                "runbook_json": str(self.output_root / "connector_config_apply" / "price_feed_refresh_runbook_current.json"),
                "runbook_markdown": str(self.output_root / "connector_config_apply" / "price_feed_refresh_runbook_current.md"),
                "status_json": str(self.output_root / "connector_config_apply" / "status_current.json"),
                "activation_plan": str(self.output_root / "connector_activation_plan" / "current.json"),
            },
            "not_authorized": [
                "writing runtime config from this runbook",
                "running Tiger price-feed acceptance automatically",
                "opening Tiger SDK clients from this runbook command",
                "submitting, cancelling, or closing broker orders",
                "arming Tiger paper TradeClient submission",
                "launchd schedule takeover",
                "strategy promotion or real-money execution",
            ],
            "safety": {
                "read_only": True,
                "writes_runtime_config": False,
                "writes_market_db": False,
                "opens_network_clients": False,
                "opens_quote_client": False,
                "opens_trade_client": False,
                "submits_orders": False,
                "cancels_orders": False,
                "closes_positions": False,
                "credential_values_exposed": False,
                "can_enable_broker_orders": False,
            },
        }
        if persist:
            self._write_price_feed_refresh_runbook(receipt)
        return receipt

    def _price_feed_refresh_window_gate(
        self,
        commands: list[dict[str, Any]],
        *,
        as_of: str | datetime | None = None,
    ) -> dict[str, Any]:
        session = comex_futures_session_status(as_of)
        has_acceptance_step = any(row.get("name") == "refresh_tiger_price_feed_acceptance" for row in commands)
        if not has_acceptance_step:
            status = "not_required"
            action = "no_price_feed_acceptance_needed"
            summary = "No Tiger price-feed acceptance refresh is required from the current operator stage."
        elif session.get("is_open") is True:
            status = "ready_to_run_acceptance_now"
            action = "run_price_feed_acceptance_sequence_now"
            summary = "COMEX is open by the local session calendar; the operator can run the explicit read-only acceptance sequence."
        else:
            status = "wait_for_comex_open"
            action = "wait_until_next_open"
            summary = "COMEX is closed by the local session calendar; preview is safe now, but run the QuoteClient acceptance step after the next open."
        return {
            "schema_version": "connector-price-feed-refresh-window-gate-v1",
            "status": status,
            "operator_action": action,
            "summary": summary,
            "checked_at": str(session.get("checked_at") or ""),
            "is_open": session.get("is_open") is True,
            "reason": str(session.get("reason") or ""),
            "next_open": session.get("next_open"),
            "can_preview_now": bool(commands),
            "can_run_quote_client_step": bool(has_acceptance_step and session.get("is_open") is True),
            "local_session_check": session,
            "safety": {
                "read_only": True,
                "uses_local_session_calendar": True,
                "opens_network_clients": False,
                "opens_quote_client": False,
                "opens_trade_client": False,
                "submits_orders": False,
                "writes_runtime_config": False,
            },
        }

    def _operator_stage_summary(
        self,
        summaries: dict[str, dict[str, Any]],
        current_runtime: dict[str, Any],
        price_feed: dict[str, Any],
    ) -> dict[str, Any]:
        post_switch = summaries.get("latest_post_switch_validation", {})
        readiness = summaries.get("latest_readiness_audit", {})
        authorization = summaries.get("latest_authorization", {})
        rehearsal = summaries.get("latest_rehearsal", {})
        check = summaries.get("latest_check", {})
        handoff = summaries.get("latest_handoff", {})
        latest_apply = summaries.get("latest_apply", {})

        price_feed_status_ready = (
            (price_feed.get("acceptance") or {}).get("status") == "accepted"
            and (price_feed.get("readiness") or {}).get("status") == "ready_for_price_feed"
            and (price_feed.get("realtime_validation") or {}).get("status") == "pass"
        )
        price_feed_evidence_fresh = all(
            (price_feed.get(name) or {}).get("fresh") is True
            for name in ("acceptance", "readiness", "realtime_validation")
        )
        price_feed_ready = price_feed_status_ready and price_feed_evidence_fresh
        readiness_audit_fresh = self._is_recent_checked_at(
            readiness.get("checked_at"),
            max_age_seconds=CONFIG_SWITCH_EVIDENCE_MAX_AGE_SECONDS,
        )

        can_switch_now = (
            readiness.get("can_switch_config_with_operator_authorization") is True
            and readiness_audit_fresh
            and price_feed_ready
        )

        if post_switch.get("status") == "validated_post_switch":
            stage = "post_switch_validated"
            summary = "Runtime is switched to Tiger/MGC and post-switch validation passed; Tiger order submission is still separate."
            next_action = "keep_orders_disabled_and_run_trade_readiness_gates"
        elif authorization.get("status") == "ready_for_operator_authorization" and price_feed_status_ready and not price_feed_evidence_fresh:
            stage = "price_feed_evidence_stale_refresh_acceptance"
            summary = "Previous Tiger price-feed checks passed, but the evidence is no longer fresh enough for an attended config switch."
            next_action = "rerun_tiger_price_feed_acceptance_then_readiness_audit"
        elif readiness.get("status") == "go_for_attended_config_switch" and authorization.get("status") == "ready_for_operator_authorization" and not readiness_audit_fresh:
            stage = "readiness_audit_stale_refresh_audit"
            summary = "Final readiness audit passed previously, but the audit receipt is stale; refresh it before any attended config switch."
            next_action = "rerun_readiness_audit"
        elif readiness.get("status") == "go_for_attended_config_switch" and authorization.get("status") == "ready_for_operator_authorization":
            stage = "ready_for_attended_config_switch"
            summary = "Price feed, rehearsal, authorization, and final audit are ready for an explicit attended config switch."
            next_action = "operator_review_authorization_then_run_attended_config_apply_if_approved"
        elif authorization.get("status") == "ready_for_operator_authorization":
            stage = "authorization_ready_pending_final_audit"
            summary = "Operator authorization package is ready, but the final Go/No-Go audit has not passed yet."
            next_action = "run_readiness_audit"
        elif rehearsal.get("status") == "passed":
            stage = "rehearsal_passed_pending_authorization"
            summary = "Sandbox rehearsal passed; generate or refresh the operator authorization package."
            next_action = "run_authorization"
        elif check.get("status") == "ready_for_attended_config_write":
            stage = "write_preflight_ready_pending_rehearsal_or_authorization"
            summary = "Config write preflight is ready; rehearsal and authorization evidence must be current before any write."
            next_action = "run_rehearsal_then_authorization"
        elif handoff.get("status") == "ready_for_operator_review":
            stage = "handoff_ready_pending_rehearsal"
            summary = "Operator handoff exists; sandbox rehearsal is the next required proof."
            next_action = "run_rehearsal"
        elif latest_apply.get("status") == "dry_run_ready":
            stage = "dry_run_ready_pending_handoff"
            summary = "Dry run is ready; generate the operator handoff and keep config unchanged."
            next_action = "run_handoff"
        else:
            stage = "not_ready"
            summary = "Tiger/MGC connector switch evidence is incomplete."
            next_action = "start_or_refresh_activation_plan"

        runtime_switched = (
            current_runtime.get("broker_provider") == "tiger_openapi"
            and current_runtime.get("dualtrack_symbol") == "MGCmain"
            and current_runtime.get("dualtrack_provider") == "tiger_openapi:COMEX"
            and current_runtime.get("execution_cost_venue") == "tiger_mgc"
            and current_runtime.get("execution_quantity_mode") == "integer_contracts"
        )
        return {
            "stage": stage,
            "summary": summary,
            "next_action": next_action,
            "refresh_commands": self._operator_stage_refresh_commands(next_action, price_feed),
            "runtime_switched_to_tiger_mgc": runtime_switched,
            "price_feed_status_ready": price_feed_status_ready,
            "price_feed_evidence_fresh": price_feed_evidence_fresh,
            "price_feed_ready": price_feed_ready,
            "readiness_audit_fresh": readiness_audit_fresh,
            "can_switch_config_with_operator_authorization": can_switch_now,
            "can_trade_machine_track": readiness.get("can_trade_machine_track") is True,
            "can_submit_tiger_orders": readiness.get("can_submit_tiger_orders") is True,
            "attended_switch_review": self._operator_stage_attended_switch_review(
                can_switch_now=can_switch_now,
                check=check,
                authorization=authorization,
                readiness=readiness,
            ),
            "not_authorized": [
                "machine strategy promotion",
                "Tiger paper TradeClient order submission",
                "real-money execution",
                "launchd schedule takeover",
            ],
        }

    def _operator_stage_attended_switch_review(
        self,
        *,
        can_switch_now: bool,
        check: dict[str, Any],
        authorization: dict[str, Any],
        readiness: dict[str, Any],
    ) -> dict[str, Any]:
        action = check.get("operator_next_action", {}) if isinstance(check.get("operator_next_action"), dict) else {}
        package_id = str(readiness.get("package_id") or authorization.get("package_id") or check.get("package_id") or "")
        return {
            "schema_version": "connector-attended-switch-review-v1",
            "status": "ready_for_operator_review" if can_switch_now else "not_ready",
            "package_id": package_id,
            "authorization_id": str(authorization.get("authorization_id") or ""),
            "audit_id": str(readiness.get("audit_id") or ""),
            "can_switch_config_with_operator_authorization": can_switch_now,
            "requires_operator_command": True,
            "requires_warning_acceptance": action.get("requires_accept_warnings") is True,
            "requires_acknowledgement": action.get("requires_acknowledgement") is True,
            "requires_package_id": action.get("requires_package_id") is True,
            "rollback_required": check.get("rollback_required") is True,
            "post_apply_validation_count": int(check.get("post_apply_validation_count") or 0),
            "authorization_markdown": str(self.output_root / "connector_config_apply" / "authorization_current.md"),
            "final_readiness_audit_markdown": str(self.output_root / "connector_config_apply" / "final_readiness_audit_current.md"),
            "runtime_config_writes_from_status_endpoint": False,
            "opens_network_clients_from_status_endpoint": False,
            "submits_orders_from_status_endpoint": False,
            "can_trade_machine_track_after_switch": readiness.get("can_trade_machine_track") is True,
            "can_submit_tiger_orders_after_switch": readiness.get("can_submit_tiger_orders") is True,
            "not_authorized_after_switch": [
                "machine strategy promotion",
                "Tiger paper TradeClient order submission",
                "real-money execution",
                "launchd schedule takeover",
            ],
        }

    def _operator_stage_refresh_commands(self, next_action: str, price_feed: dict[str, Any]) -> list[dict[str, Any]]:
        if next_action != "rerun_tiger_price_feed_acceptance_then_readiness_audit":
            return []
        run_date = self._operator_stage_run_date(price_feed)
        contract = self._operator_stage_contract(price_feed)
        return [
            {
                "name": "preview_tiger_price_feed_acceptance_refresh",
                "command": f"python3 -m pipelines.tiger_price_feed_acceptance --date {run_date} --contract {contract} --poll-seconds 75 --plan-only --json",
                "purpose": "Preview the Tiger price-feed refresh sequence and safety boundary without opening Tiger SDK clients.",
                "opens_quote_client": False,
                "opens_trade_client": False,
                "submits_orders": False,
                "writes_runtime_config": False,
                "writes_market_db": False,
                "writes_plan_artifact": True,
            },
            {
                "name": "refresh_tiger_price_feed_acceptance",
                "command": f"python3 -m pipelines.tiger_price_feed_acceptance --date {run_date} --contract {contract} --poll-seconds 75 --json",
                "purpose": "Refresh Tiger/COMEX market-hours price-feed evidence.",
                "opens_quote_client": True,
                "opens_trade_client": False,
                "submits_orders": False,
                "writes_runtime_config": False,
                "writes_market_db": False,
                "writes_plan_artifact": False,
            },
            {
                "name": "refresh_final_readiness_audit",
                "command": "python3 -m pipelines.connector_config_apply readiness-audit --plan outputs/connector_activation_plan/current.json --json",
                "purpose": "Recompute the attended-switch Go/No-Go result after fresh price-feed evidence.",
                "opens_quote_client": False,
                "opens_trade_client": False,
                "submits_orders": False,
                "writes_runtime_config": False,
                "writes_market_db": False,
                "writes_plan_artifact": False,
            },
            {
                "name": "show_connector_switch_status",
                "command": "python3 -m pipelines.connector_config_apply status --json",
                "purpose": "Confirm whether operator_stage has returned to ready_for_attended_config_switch.",
                "opens_quote_client": False,
                "opens_trade_client": False,
                "submits_orders": False,
                "writes_runtime_config": False,
                "writes_market_db": False,
                "writes_plan_artifact": False,
            },
        ]

    def _operator_stage_run_date(self, price_feed: dict[str, Any]) -> str:
        for name in ("acceptance", "readiness", "realtime_validation"):
            row = price_feed.get(name) if isinstance(price_feed.get(name), dict) else {}
            value = str(row.get("run_date") or "")
            if value:
                return value
        return datetime.now(timezone.utc).date().isoformat()

    def _operator_stage_contract(self, price_feed: dict[str, Any]) -> str:
        for name in ("acceptance", "readiness", "realtime_validation"):
            row = price_feed.get(name) if isinstance(price_feed.get(name), dict) else {}
            value = str(row.get("contract") or "")
            if value:
                return value
        return "MGCmain"

    def _validate_plan(
        self,
        plan: dict[str, Any],
        *,
        acknowledgement: str,
        accept_warnings: bool,
        dry_run: bool,
        package_id: str,
        require_current_rehearsal: bool = True,
    ) -> dict[str, list[dict[str, Any]]]:
        blockers: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        package = plan.get("config_apply_package", {}) if isinstance(plan.get("config_apply_package"), dict) else {}
        expected_ack = str(package.get("operator_acknowledgement") or "")
        expected_package_id = str(package.get("package_id") or "")
        recomputed_package_id = self._recompute_package_id(plan, package)
        plan_warnings = [row for row in (plan.get("warnings") or []) if isinstance(row, dict)]
        if plan.get("schema_version") != "connector-activation-plan-v1":
            blockers.append({"name": "activation_plan_schema", "summary": "Unsupported or missing activation plan schema."})
        if plan.get("status") == "blocked" or plan.get("blockers"):
            blockers.append({"name": "activation_plan_blocked", "summary": "Blocked activation plans cannot be applied."})
        if not dry_run:
            if not expected_ack:
                blockers.append({"name": "operator_acknowledgement_missing", "summary": "Activation plan has no operator acknowledgement string."})
            elif acknowledgement != expected_ack:
                blockers.append({"name": "operator_acknowledgement", "summary": "Exact config apply acknowledgement is required."})
            if not expected_package_id:
                blockers.append({"name": "config_apply_package_id_missing", "summary": "Activation plan has no config apply package id."})
            elif recomputed_package_id != expected_package_id:
                blockers.append({"name": "config_apply_package_id_stale", "summary": "Config apply package id does not match the activation plan contents."})
            elif not package_id:
                blockers.append({"name": "config_apply_package_id_required", "summary": "Exact config apply package id is required for config write."})
            elif package_id != expected_package_id:
                blockers.append({"name": "config_apply_package_id_mismatch", "summary": "Config apply package id does not match the reviewed activation plan."})
            if plan_warnings and not accept_warnings:
                blockers.append({"name": "warnings_not_accepted", "summary": "Activation warnings must be explicitly accepted before config write."})
            if not blockers:
                write_blockers = self._validate_handoff_for_write(plan, expected_package_id)
                if not write_blockers and require_current_rehearsal:
                    write_blockers = self._validate_rehearsal_for_write(plan, expected_package_id)
                blockers.extend(write_blockers)
        if plan_warnings:
            warnings.extend({"name": str(row.get("name") or "warning"), "summary": str(row.get("summary") or "")} for row in plan_warnings)
        if dry_run:
            warnings.append({"name": "dry_run", "summary": "Dry run did not write config or create backups."})
        return {"blockers": blockers, "warnings": warnings}

    def _latest_apply_receipt(self) -> dict[str, Any]:
        apply_rows = load_json(self.output_root / "connector_config_apply" / "current.json")
        return apply_rows[-1] if apply_rows else {}

    def _latest_handoff_receipt(self) -> dict[str, Any]:
        handoff_rows = load_json(self.output_root / "connector_config_apply" / "handoff_current.json")
        return handoff_rows[-1] if handoff_rows else {}

    def _latest_rehearsal_receipt(self) -> dict[str, Any]:
        rehearsal_rows = load_json(self.output_root / "connector_config_apply" / "rehearsal_current.json")
        return rehearsal_rows[-1] if rehearsal_rows else {}

    def _latest_authorization_receipt(self) -> dict[str, Any]:
        authorization_rows = load_json(self.output_root / "connector_config_apply" / "authorization_current.json")
        return authorization_rows[-1] if authorization_rows else {}

    def _latest_readiness_audit_receipt(self) -> dict[str, Any]:
        readiness_rows = load_json(self.output_root / "connector_config_apply" / "final_readiness_audit_current.json")
        return readiness_rows[-1] if readiness_rows else {}

    def _latest_activation_plan(self) -> dict[str, Any]:
        rows = load_json(self.output_root / "connector_activation_plan" / "current.json")
        return rows[-1] if rows else {}

    def _latest_output_receipt(self, name: str) -> dict[str, Any]:
        rows = load_json(self.output_root / name / "current.json")
        return rows[-1] if rows else {}

    def _seed_rehearsal_support(self, sandbox_output: Path, rehearsal_root: Path) -> None:
        for name in [
            "tiger_price_feed_acceptance",
            "tiger_price_feed_readiness",
            "tiger_realtime_validation",
            "tiger_futures_feed",
            "tiger_paper_order_readiness",
            "tiger_paper_order_approval",
        ]:
            source = self.output_root / name / "current.json"
            if source.exists():
                target = sandbox_output / name / "current.json"
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        pipeline = load_json_yaml(self.pipeline_config_path)
        source_db = self._resolve_runtime_path(str(pipeline.get("local_market_db") or "data/market_data.db"))
        if source_db.exists():
            target_db = rehearsal_root / "data" / "market_data.db"
            target_db.parent.mkdir(parents=True, exist_ok=True)
            self._copy_sqlite_db(source_db, target_db)

    def _copy_sqlite_db(self, source: Path, target: Path) -> None:
        source_uri = f"file:{source}?mode=ro"
        with sqlite3.connect(source_uri, uri=True) as source_conn:
            with sqlite3.connect(target) as target_conn:
                source_conn.backup(target_conn)

    def _artifact_status(
        self,
        receipt: dict[str, Any],
        *,
        extra_keys: list[str] | None = None,
        max_age_seconds: int | None = None,
    ) -> dict[str, Any]:
        if not receipt:
            return {"status": "missing"}
        payload = {
            "status": str(receipt.get("status") or "missing"),
            "blocker_count": len([row for row in (receipt.get("blockers") or []) if isinstance(row, dict)]),
        }
        for key in extra_keys or []:
            if key in receipt:
                payload[key] = receipt.get(key)
        if max_age_seconds is not None:
            age = self._checked_at_age_seconds(receipt.get("checked_at"))
            payload["max_age_seconds"] = int(max_age_seconds)
            payload["age_seconds"] = None if age is None else round(age, 2)
            payload["fresh"] = age is not None and age <= max_age_seconds
        return payload

    def _checked_at_age_seconds(self, value: Any) -> float | None:
        if value in (None, ""):
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds())

    def _is_recent_checked_at(self, value: Any, *, max_age_seconds: int) -> bool:
        age = self._checked_at_age_seconds(value)
        return age is not None and age <= max_age_seconds

    def _tiger_market_coverage(self) -> dict[str, Any]:
        pipeline = load_json_yaml(self.pipeline_config_path)
        db_path = self._resolve_runtime_path(str(pipeline.get("local_market_db") or "data/market_data.db"))
        result: dict[str, Any] = {
            "market_db": str(db_path),
            "symbol": "MGCmain",
            "timeframe": "1m",
            "provider": "tiger_openapi:COMEX",
            "rows": 0,
            "first_timestamp": "",
            "last_timestamp": "",
            "exists": db_path.exists(),
        }
        if not db_path.exists():
            return result
        try:
            uri = f"file:{db_path}?mode=ro"
            with sqlite3.connect(uri, uri=True) as conn:
                row = conn.execute(
                    """
                    SELECT COUNT(*), MIN(timestamp), MAX(timestamp)
                    FROM bars
                    WHERE symbol = ? AND timeframe = ? AND provider = ?
                    """,
                    ("MGCmain", "1m", "tiger_openapi:COMEX"),
                ).fetchone()
        except sqlite3.Error as exc:
            return {**result, "error": str(exc)}
        if row:
            result.update({
                "rows": int(row[0] or 0),
                "first_timestamp": str(row[1] or ""),
                "last_timestamp": str(row[2] or ""),
            })
        return result

    def _resolve_runtime_path(self, value: str) -> Path:
        path = Path(value)
        if path.is_absolute():
            return path
        if self.pipeline_config_path.parent.name == "configs":
            return self.pipeline_config_path.parent.parent / path
        return ROOT / path

    def _readiness_audit_blockers(
        self,
        *,
        price_feed_acceptance: dict[str, Any],
        price_feed_readiness: dict[str, Any],
        realtime_validation: dict[str, Any],
        futures_feed: dict[str, Any],
        authorization: dict[str, Any],
        market_coverage: dict[str, Any],
        current_runtime: dict[str, Any],
    ) -> list[dict[str, Any]]:
        blockers: list[dict[str, Any]] = []
        if price_feed_acceptance.get("status") != "accepted" or price_feed_acceptance.get("ready_for_price_feed") is not True:
            blockers.append({"name": "tiger_price_feed_acceptance_not_ready", "summary": "Tiger price-feed acceptance is not accepted."})
        if price_feed_readiness.get("status") != "ready_for_price_feed" or price_feed_readiness.get("ready_for_price_feed") is not True:
            blockers.append({"name": "tiger_price_feed_readiness_not_ready", "summary": "Tiger price-feed readiness is not ready."})
        if realtime_validation.get("status") != "pass":
            blockers.append({"name": "tiger_realtime_validation_not_passed", "summary": "Tiger realtime validation has not passed."})
        stale_artifacts = [
            name
            for name, receipt in [
                ("acceptance", price_feed_acceptance),
                ("readiness", price_feed_readiness),
                ("realtime_validation", realtime_validation),
            ]
            if not self._is_recent_checked_at(receipt.get("checked_at"), max_age_seconds=PRICE_FEED_EVIDENCE_MAX_AGE_SECONDS)
        ]
        if stale_artifacts:
            blockers.append(
                {
                    "name": "tiger_price_feed_evidence_stale",
                    "summary": "Tiger price-feed evidence is too old for an attended config switch; rerun acceptance.",
                    "evidence": {
                        "stale_artifacts": stale_artifacts,
                        "max_age_seconds": PRICE_FEED_EVIDENCE_MAX_AGE_SECONDS,
                    },
                }
            )
        if futures_feed.get("status") != "pass" or futures_feed.get("ready") is not True:
            blockers.append({"name": "tiger_futures_feed_not_ready", "summary": "Tiger futures feed import is not ready."})
        if int(market_coverage.get("rows") or 0) < 500:
            blockers.append({"name": "tiger_market_coverage_insufficient", "summary": "Local MGCmain 1m Tiger/COMEX coverage has fewer than 500 rows."})
        if authorization.get("status") != "ready_for_operator_authorization":
            blockers.append({"name": "config_authorization_not_ready", "summary": "Connector config authorization package is missing or not ready."})
        auth_safety = authorization.get("safety", {}) if isinstance(authorization.get("safety"), dict) else {}
        if auth_safety.get("writes_runtime_config") is not False or auth_safety.get("opens_network_clients") is not False or auth_safety.get("submits_orders") is not False:
            blockers.append({"name": "config_authorization_safety", "summary": "Authorization package does not prove read-only/no-network/no-order safety."})
        if current_runtime.get("broker_provider") == "tiger_openapi" or current_runtime.get("dualtrack_symbol") == "MGCmain":
            blockers.append({"name": "runtime_already_switched", "summary": "Runtime config already appears switched; use post-switch validation instead of pre-switch audit."})
        return blockers

    def _post_switch_blockers(
        self,
        *,
        package_id: str,
        latest_apply: dict[str, Any],
        latest_authorization: dict[str, Any],
        latest_readiness_audit: dict[str, Any],
        current_runtime: dict[str, Any],
        price_feed_acceptance: dict[str, Any],
        realtime_validation: dict[str, Any],
        market_coverage: dict[str, Any],
    ) -> list[dict[str, Any]]:
        blockers: list[dict[str, Any]] = []
        if latest_apply.get("status") != "applied":
            blockers.append({"name": "config_apply_not_applied", "summary": "Latest connector config apply receipt is not an applied write."})
        if latest_apply.get("status") == "applied" and package_id and str(latest_apply.get("package_id") or "") != package_id:
            blockers.append({"name": "config_apply_package_mismatch", "summary": "Latest applied config package does not match the expected package id."})
        apply_safety = latest_apply.get("safety", {}) if isinstance(latest_apply.get("safety"), dict) else {}
        if latest_apply and (
            apply_safety.get("opens_network_clients") is not False
            or apply_safety.get("submits_orders") is not False
        ):
            blockers.append({"name": "config_apply_safety", "summary": "Latest config apply does not prove no-network/no-order safety."})
        backup = latest_apply.get("backup", {}) if isinstance(latest_apply.get("backup"), dict) else {}
        rollback = latest_apply.get("rollback", {}) if isinstance(latest_apply.get("rollback"), dict) else {}
        if latest_apply.get("status") == "applied" and (backup.get("created") is not True or rollback.get("available") is not True):
            blockers.append({"name": "rollback_backup_missing", "summary": "Applied config receipt does not expose a rollback backup."})
        if latest_authorization.get("status") != "ready_for_operator_authorization":
            blockers.append({"name": "config_authorization_missing", "summary": "No ready authorization package is available for the applied switch."})
        if package_id and str(latest_authorization.get("package_id") or "") != package_id:
            blockers.append({"name": "config_authorization_package_mismatch", "summary": "Authorization package does not match the applied package id."})
        if latest_readiness_audit.get("status") != "go_for_attended_config_switch":
            blockers.append({"name": "readiness_audit_not_go", "summary": "Latest final readiness audit was not go_for_attended_config_switch before the applied switch."})
        if package_id and latest_readiness_audit and str(latest_readiness_audit.get("package_id") or "") != package_id:
            blockers.append({"name": "readiness_audit_package_mismatch", "summary": "Final readiness audit does not match the applied package id."})
        expected_runtime = {
            "broker_provider": "tiger_openapi",
            "broker_dry_run": True,
            "dualtrack_symbol": "MGCmain",
            "dualtrack_provider": "tiger_openapi:COMEX",
            "market_session_enabled": True,
            "human_fill_sync_enabled": True,
            "execution_cost_venue": "tiger_mgc",
            "execution_quantity_mode": "integer_contracts",
        }
        for key, expected in expected_runtime.items():
            if current_runtime.get(key) != expected:
                blockers.append({"name": f"runtime_{key}_mismatch", "summary": f"Runtime {key} is not {expected!r}."})
        if current_runtime.get("can_enable_broker_orders") is True:
            blockers.append({"name": "runtime_broker_orders_enabled", "summary": "Runtime config appears to enable broker orders; expected post-switch profile to keep orders closed."})
        if price_feed_acceptance.get("status") != "accepted" or price_feed_acceptance.get("ready_for_price_feed") is not True:
            blockers.append({"name": "tiger_price_feed_acceptance_not_ready", "summary": "Tiger price-feed acceptance is not accepted after config switch."})
        if realtime_validation.get("status") != "pass":
            blockers.append({"name": "tiger_realtime_validation_not_passed", "summary": "Tiger realtime validation has not passed after config switch."})
        if int(market_coverage.get("rows") or 0) < 500:
            blockers.append({"name": "tiger_market_coverage_insufficient", "summary": "Local MGCmain 1m Tiger/COMEX coverage has fewer than 500 rows."})
        return blockers

    def _authorization_blockers(
        self,
        check: dict[str, Any],
        handoff: dict[str, Any],
        rehearsal: dict[str, Any],
        current_runtime: dict[str, Any],
        package_id: str,
        *,
        require_current_rehearsal: bool = True,
    ) -> list[dict[str, Any]]:
        blockers: list[dict[str, Any]] = []
        if check.get("status") != "ready_for_attended_config_write":
            blockers.append({"name": "config_check_not_write_ready", "summary": "Current package check is not ready for attended config write."})
        if check.get("usable_for_attended_config_write") is not True:
            blockers.append({"name": "config_check_not_usable", "summary": "Current package check does not allow attended config write."})
        write_preflight = check.get("write_preflight", {}) if isinstance(check.get("write_preflight"), dict) else {}
        if write_preflight.get("status") != "ready":
            blockers.append({"name": "config_write_preflight_not_ready", "summary": "Current write preflight is not ready."})
        if handoff.get("status") != "ready_for_operator_review":
            blockers.append({"name": "config_handoff_not_ready", "summary": "Current operator handoff is missing or not ready."})
        if str(handoff.get("package_id") or "") != package_id:
            blockers.append({"name": "config_handoff_package_mismatch", "summary": "Current operator handoff does not match the package id."})
        if require_current_rehearsal:
            if rehearsal.get("status") != "passed":
                blockers.append({"name": "config_rehearsal_not_passed", "summary": "Current sandbox rehearsal is missing or did not pass."})
            if str(rehearsal.get("package_id") or "") != package_id:
                blockers.append({"name": "config_rehearsal_package_mismatch", "summary": "Current sandbox rehearsal does not match the package id."})
            runtime = rehearsal.get("runtime_config", {}) if isinstance(rehearsal.get("runtime_config"), dict) else {}
            if runtime.get("unchanged") is not True:
                blockers.append({"name": "config_rehearsal_runtime_not_proven", "summary": "Current rehearsal does not prove runtime config stayed unchanged."})
        safety_pairs = [
            ("handoff", handoff.get("safety", {}) if isinstance(handoff.get("safety"), dict) else {}),
        ]
        if require_current_rehearsal:
            safety_pairs.append(("rehearsal", rehearsal.get("safety", {}) if isinstance(rehearsal.get("safety"), dict) else {}))
        for name, safety in safety_pairs:
            if safety.get("writes_runtime_config") is not False or safety.get("opens_network_clients") is not False or safety.get("submits_orders") is not False:
                blockers.append({"name": f"config_{name}_safety", "summary": f"Current {name} does not prove no runtime write, no network, and no order safety."})
        if current_runtime.get("broker_provider") == "tiger_openapi" or current_runtime.get("dualtrack_symbol") == "MGCmain":
            blockers.append({"name": "runtime_already_switched", "summary": "Runtime config already appears switched; do not generate a pre-switch authorization package."})
        return blockers

    def _validate_handoff_for_write(self, plan: dict[str, Any], expected_package_id: str) -> list[dict[str, Any]]:
        handoff = self._latest_handoff_receipt()
        if not handoff:
            return [{"name": "config_handoff_missing", "summary": "Run connector config handoff before config write."}]
        blockers: list[dict[str, Any]] = []
        if handoff.get("status") != "ready_for_operator_review":
            blockers.append({"name": "config_handoff_not_ready", "summary": "Latest handoff is not ready for operator review."})
        if str(handoff.get("package_id") or "") != expected_package_id:
            blockers.append({"name": "config_handoff_package_mismatch", "summary": "Latest handoff package id does not match the reviewed activation package."})
        safety = handoff.get("safety", {}) if isinstance(handoff.get("safety"), dict) else {}
        if (
            safety.get("writes_runtime_config") is not False
            or safety.get("opens_network_clients") is not False
            or safety.get("submits_orders") is not False
        ):
            blockers.append({"name": "config_handoff_safety", "summary": "Latest handoff does not prove a no-write/no-network/no-order boundary."})

        evidence = handoff.get("evidence_chain", {}) if isinstance(handoff.get("evidence_chain"), dict) else {}
        activation = self._evidence_by_name(evidence.get("sources", []), "activation_plan")
        if str(activation.get("payload_sha256") or "") != self._payload_sha256(plan):
            blockers.append({"name": "config_handoff_plan_mismatch", "summary": "Latest handoff was generated from a different activation plan payload."})
        for expected in ("activation_plan", "config_apply_dry_run"):
            row = self._evidence_by_name(evidence.get("sources", []), expected)
            if self._evidence_artifact_stale(row):
                blockers.append({"name": f"config_handoff_{expected}_stale", "summary": f"Latest handoff evidence for {expected} is missing or stale."})
        for expected in ("pipeline_config", "dualtrack_config"):
            row = self._evidence_by_name(evidence.get("current_config", []), expected)
            if self._evidence_artifact_stale(row):
                blockers.append({"name": f"config_handoff_{expected}_stale", "summary": f"Current config file no longer matches latest handoff evidence for {expected}."})
        return blockers

    def _validate_rehearsal_for_write(self, plan: dict[str, Any], expected_package_id: str) -> list[dict[str, Any]]:
        rehearsal = self._latest_rehearsal_receipt()
        if not rehearsal:
            return [{"name": "config_rehearsal_missing", "summary": "Run connector config rehearsal before config write."}]
        blockers: list[dict[str, Any]] = []
        if rehearsal.get("status") != "passed":
            blockers.append({"name": "config_rehearsal_not_passed", "summary": "Latest sandbox config rehearsal did not pass."})
        if str(rehearsal.get("package_id") or "") != expected_package_id:
            blockers.append({"name": "config_rehearsal_package_mismatch", "summary": "Latest rehearsal package id does not match the reviewed activation package."})
        safety = rehearsal.get("safety", {}) if isinstance(rehearsal.get("safety"), dict) else {}
        if (
            safety.get("writes_runtime_config") is not False
            or safety.get("writes_sandbox_config") is not True
            or safety.get("opens_network_clients") is not False
            or safety.get("submits_orders") is not False
        ):
            blockers.append({"name": "config_rehearsal_safety", "summary": "Latest rehearsal does not prove sandbox-only/no-network/no-order safety."})
        runtime = rehearsal.get("runtime_config", {}) if isinstance(rehearsal.get("runtime_config"), dict) else {}
        if runtime.get("unchanged") is not True:
            blockers.append({"name": "config_rehearsal_runtime_changed", "summary": "Latest rehearsal did not prove runtime config stayed unchanged."})
        before = runtime.get("before", {}) if isinstance(runtime.get("before"), dict) else {}
        after = runtime.get("after", {}) if isinstance(runtime.get("after"), dict) else {}
        current = self._runtime_config_evidence()
        if before != after or after != current:
            blockers.append({"name": "config_rehearsal_runtime_config_stale", "summary": "Current runtime config no longer matches latest rehearsal evidence."})
        steps = rehearsal.get("steps", {}) if isinstance(rehearsal.get("steps"), dict) else {}
        expected_steps = {
            "dry_run": "dry_run_ready",
            "pre_handoff_check": "ready_for_handoff",
            "handoff": "ready_for_operator_review",
            "write_check": "ready_for_attended_config_write",
            "authorization": "ready_for_operator_authorization",
            "readiness_audit": "go_for_attended_config_switch",
            "sandbox_apply": "applied",
            "post_switch_validation": "validated_post_switch",
            "sandbox_rollback": "rolled_back",
        }
        for name, expected_status in expected_steps.items():
            row = steps.get(name, {}) if isinstance(steps.get(name), dict) else {}
            if row.get("status") != expected_status:
                blockers.append({"name": f"config_rehearsal_{name}_status", "summary": f"Latest rehearsal step {name} did not reach {expected_status}."})
        if str(rehearsal.get("package_id") or "") != str((plan.get("config_apply_package") or {}).get("package_id") or ""):
            blockers.append({"name": "config_rehearsal_plan_package_mismatch", "summary": "Latest rehearsal does not match the submitted activation plan package id."})
        return blockers

    def _evidence_by_name(self, rows: Any, name: str) -> dict[str, Any]:
        for row in rows or []:
            if isinstance(row, dict) and row.get("name") == name:
                return row
        return {}

    def _evidence_artifact_stale(self, row: dict[str, Any]) -> bool:
        if not row or row.get("exists") is not True:
            return True
        path = Path(str(row.get("path") or ""))
        expected_sha = str(row.get("sha256") or "")
        return not path.is_file() or not expected_sha or self._sha256(path) != expected_sha

    def _recompute_package_id(self, plan: dict[str, Any], package: dict[str, Any]) -> str:
        profile = plan.get("dualtrack_profile_preview", {}) if isinstance(plan.get("dualtrack_profile_preview"), dict) else {}
        changes = [row for row in (plan.get("config_patch_preview") or []) if isinstance(row, dict)]
        dualtrack_changes = [
            row
            for row in (profile.get("dualtrack_config_patch_preview") or [])
            if isinstance(row, dict)
        ]
        return build_config_apply_package_id(
            connector_id=str(plan.get("connector_id") or ""),
            requested_roles=[str(item) for item in (plan.get("requested_roles") or [])],
            status=str(package.get("status") or ""),
            patch_digest=config_apply_patch_digest(changes),
            dualtrack_patch_digest=config_apply_patch_digest(dualtrack_changes),
            warnings=[row for row in (plan.get("warnings") or []) if isinstance(row, dict)],
            blockers=[row for row in (plan.get("blockers") or []) if isinstance(row, dict)],
        )

    def _config_targets(self, pipeline_changes: list[dict[str, Any]], dualtrack_changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        targets = []
        if pipeline_changes:
            targets.append({"name": "pipeline", "path": str(self.pipeline_config_path), "change_count": len(pipeline_changes)})
        if dualtrack_changes:
            targets.append({"name": "dualtrack", "path": str(self.dualtrack_config_path), "change_count": len(dualtrack_changes)})
        return targets

    def _backup_configs(self, apply_id: str, targets: list[dict[str, Any]]) -> dict[str, Any]:
        backup_dir = self.output_root / "connector_config_apply" / "backups" / apply_id
        files = []
        for target in targets:
            source = Path(str(target["path"]))
            if not source.exists():
                raise ValueError(f"config file missing: {source}")
            backup_path = backup_dir / source.name
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, backup_path)
            files.append(
                {
                    "name": str(target["name"]),
                    "target": str(source),
                    "backup": str(backup_path),
                    "sha256": self._sha256(source),
                }
            )
        return {"required": True, "created": True, "backup_dir": str(backup_dir), "files": files}

    def _runtime_summary(self) -> dict[str, Any]:
        pipeline = load_json_yaml(self.pipeline_config_path)
        dualtrack = load_json_yaml(self.dualtrack_config_path)
        broker = pipeline.get("broker", {}) if isinstance(pipeline.get("broker"), dict) else {}
        broker_profiles = pipeline.get("broker_profiles", {}) if isinstance(pipeline.get("broker_profiles"), dict) else {}
        active_profile_name = str(broker.get("profile") or "")
        active_profile = broker_profiles.get(active_profile_name, {}) if active_profile_name else {}
        if not isinstance(active_profile, dict):
            active_profile = {}
        market_data = dualtrack.get("market_data", {}) if isinstance(dualtrack.get("market_data"), dict) else {}
        market_session = dualtrack.get("market_session", {}) if isinstance(dualtrack.get("market_session"), dict) else {}
        execution_cost = dualtrack.get("execution_cost_model", {}) if isinstance(dualtrack.get("execution_cost_model"), dict) else {}
        human_fill_sync = dualtrack.get("human_fill_sync", {}) if isinstance(dualtrack.get("human_fill_sync"), dict) else {}
        return {
            "broker_provider": str(broker.get("provider") or ""),
            "broker_profile": str(broker.get("profile") or ""),
            "broker_environment": str(broker.get("environment") or ""),
            "broker_dry_run": broker.get("dry_run") is True,
            "broker_profile_dry_run": active_profile.get("dry_run") is True,
            "can_enable_broker_orders": active_profile.get("network_order_submission") == "paper_tradeclient"
            and active_profile.get("confirm_tiger_paper_orders") is True
            and active_profile.get("dry_run") is False,
            "dualtrack_symbol": str(market_data.get("symbol") or ""),
            "dualtrack_provider": str(market_data.get("provider") or ""),
            "market_session_enabled": market_session.get("enabled") is True,
            "market_session_venue": str(market_session.get("venue") or ""),
            "execution_cost_venue": str(execution_cost.get("venue") or ""),
            "execution_quantity_mode": str(execution_cost.get("quantity_mode") or ""),
            "human_fill_sync_enabled": human_fill_sync.get("enabled") is True,
        }

    def _handoff_evidence_chain(
        self,
        plan: dict[str, Any],
        check: dict[str, Any],
        current_runtime: dict[str, Any],
    ) -> dict[str, Any]:
        activation_path = self.output_root / "connector_activation_plan" / "current.json"
        dry_run_path = self.output_root / "connector_config_apply" / "current.json"
        check_path = self.output_root / "connector_config_apply" / "check_current.json"
        persisted_check_rows = load_json(check_path)
        persisted_check = persisted_check_rows[-1] if persisted_check_rows else {}
        latest_dry_run = self._latest_apply_receipt()
        package_id = str((plan.get("config_apply_package") or {}).get("package_id") or "") if isinstance(plan.get("config_apply_package"), dict) else ""
        sources = [
            self._artifact_evidence(
                "activation_plan",
                activation_path,
                {
                    "status": str(plan.get("status") or ""),
                    "checked_at": str(plan.get("checked_at") or ""),
                    "package_id": package_id,
                    "connector_id": str(plan.get("connector_id") or ""),
                    "payload_sha256": self._payload_sha256(plan),
                },
            ),
            self._artifact_evidence(
                "config_apply_dry_run",
                dry_run_path,
                {
                    "status": str(latest_dry_run.get("status") or ""),
                    "checked_at": str(latest_dry_run.get("checked_at") or ""),
                    "apply_id": str(latest_dry_run.get("apply_id") or ""),
                    "package_id": str(latest_dry_run.get("package_id") or ""),
                },
            ),
            self._artifact_evidence(
                "persisted_config_check",
                check_path,
                {
                    "status": str(persisted_check.get("status") or "missing"),
                    "checked_at": str(persisted_check.get("checked_at") or ""),
                    "package_id": str(persisted_check.get("package_id") or ""),
                },
            ),
        ]
        computed_check = {
            "status": str(check.get("status") or ""),
            "checked_at": str(check.get("checked_at") or ""),
            "package_id": str(check.get("package_id") or ""),
            "payload_sha256": self._payload_sha256(check),
        }
        configs = [
            self._artifact_evidence(
                "pipeline_config",
                self.pipeline_config_path,
                {
                    "broker_provider": str(current_runtime.get("broker_provider") or ""),
                    "broker_dry_run": current_runtime.get("broker_dry_run") is True,
                },
            ),
            self._artifact_evidence(
                "dualtrack_config",
                self.dualtrack_config_path,
                {
                    "dualtrack_symbol": str(current_runtime.get("dualtrack_symbol") or ""),
                    "dualtrack_provider": str(current_runtime.get("dualtrack_provider") or ""),
                    "human_fill_sync_enabled": current_runtime.get("human_fill_sync_enabled") is True,
                },
            ),
        ]
        return {
            "schema_version": "connector-config-handoff-evidence-v1",
            "sources": sources,
            "computed_check": computed_check,
            "current_config": configs,
            "evidence_count": len(sources) + len(configs) + 1,
        }

    def _artifact_evidence(self, name: str, path: Path, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        exists = path.exists()
        payload = {
            "name": name,
            "path": str(path),
            "exists": exists,
            "sha256": self._sha256(path) if exists and path.is_file() else "",
        }
        if extra:
            payload.update(extra)
        return payload

    def _write_configs(self, pipeline_changes: list[dict[str, Any]], dualtrack_changes: list[dict[str, Any]]) -> None:
        if pipeline_changes:
            config = load_json_yaml(self.pipeline_config_path)
            for change in pipeline_changes:
                self._apply_change(config, change)
            self._write_config(self.pipeline_config_path, config)
        if dualtrack_changes:
            config = load_json_yaml(self.dualtrack_config_path)
            for change in dualtrack_changes:
                self._apply_change(config, change)
            self._write_config(self.dualtrack_config_path, config)

    def _apply_change(self, config: dict[str, Any], change: dict[str, Any]) -> None:
        path = str(change.get("path") or "")
        if not path:
            raise ValueError("config change missing path")
        op = str(change.get("op") or "set")
        keys = path.split(".")
        if op == "set":
            self._set_path(config, keys, change.get("planned"))
        elif op == "remove":
            self._remove_path(config, keys)
        else:
            raise ValueError(f"unsupported config op: {op}")

    def _set_path(self, config: dict[str, Any], keys: list[str], value: Any) -> None:
        current: Any = config
        for key in keys[:-1]:
            if key not in current or not isinstance(current[key], dict):
                current[key] = {}
            current = current[key]
        current[keys[-1]] = value

    def _remove_path(self, config: dict[str, Any], keys: list[str]) -> None:
        current: Any = config
        for key in keys[:-1]:
            if not isinstance(current, dict) or key not in current:
                return
            current = current[key]
        if isinstance(current, dict):
            current.pop(keys[-1], None)

    def _write_config(self, path: Path, config: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(config, indent=2, ensure_ascii=False) + "\n"
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise

    def _write_apply_receipt(self, receipt: dict[str, Any]) -> None:
        base = self.output_root / "connector_config_apply"
        write_json(base / "current.json", [receipt])
        write_json(base / f"{receipt['apply_id']}.json", [receipt])
        history = load_json(base / "history.json")
        history.append(receipt)
        write_json(base / "history.json", history)

    def _write_rollback_receipt(self, receipt: dict[str, Any]) -> None:
        base = self.output_root / "connector_config_apply" / "rollback"
        write_json(base / "current.json", [receipt])
        write_json(base / f"{receipt['rollback_id']}.json", [receipt])

    def _write_check_receipt(self, receipt: dict[str, Any]) -> None:
        base = self.output_root / "connector_config_apply"
        write_json(base / "check_current.json", [receipt])
        history = load_json(base / "check_history.json")
        history.append(receipt)
        write_json(base / "check_history.json", history)

    def _write_handoff(self, receipt: dict[str, Any]) -> None:
        base = self.output_root / "connector_config_apply"
        write_json(base / "handoff_current.json", [receipt])
        write_json(base / f"{receipt['handoff_id']}.json", [receipt])
        (base / "handoff_current.md").write_text(self._handoff_markdown(receipt), encoding="utf-8")
        history = load_json(base / "handoff_history.json")
        history.append(receipt)
        write_json(base / "handoff_history.json", history)

    def _write_rehearsal(self, receipt: dict[str, Any]) -> None:
        base = self.output_root / "connector_config_apply"
        write_json(base / "rehearsal_current.json", [receipt])
        write_json(base / f"{receipt['rehearsal_id']}.json", [receipt])
        history = load_json(base / "rehearsal_history.json")
        history.append(receipt)
        write_json(base / "rehearsal_history.json", history)

    def _write_authorization(self, receipt: dict[str, Any]) -> None:
        base = self.output_root / "connector_config_apply"
        write_json(base / "authorization_current.json", [receipt])
        write_json(base / f"{receipt['authorization_id']}.json", [receipt])
        (base / "authorization_current.md").write_text(self._authorization_markdown(receipt), encoding="utf-8")
        history = load_json(base / "authorization_history.json")
        history.append(receipt)
        write_json(base / "authorization_history.json", history)

    def _write_readiness_audit(self, receipt: dict[str, Any]) -> None:
        base = self.output_root / "connector_config_apply"
        write_json(base / "final_readiness_audit_current.json", [receipt])
        write_json(base / f"{receipt['audit_id']}.json", [receipt])
        (base / "final_readiness_audit_current.md").write_text(self._readiness_audit_markdown(receipt), encoding="utf-8")
        history = load_json(base / "final_readiness_audit_history.json")
        history.append(receipt)
        write_json(base / "final_readiness_audit_history.json", history)

    def _write_post_switch_validation(self, receipt: dict[str, Any]) -> None:
        base = self.output_root / "connector_config_apply"
        write_json(base / "post_switch_validation_current.json", [receipt])
        write_json(base / f"{receipt['validation_id']}.json", [receipt])
        (base / "post_switch_validation_current.md").write_text(self._post_switch_validation_markdown(receipt), encoding="utf-8")
        history = load_json(base / "post_switch_validation_history.json")
        history.append(receipt)
        write_json(base / "post_switch_validation_history.json", history)

    def _write_price_feed_refresh_runbook(self, receipt: dict[str, Any]) -> None:
        base = self.output_root / "connector_config_apply"
        status_receipt = receipt.get("status_receipt") if isinstance(receipt.get("status_receipt"), dict) else {}
        if status_receipt:
            write_json(base / "status_current.json", [status_receipt])
        write_json(base / "price_feed_refresh_runbook_current.json", [receipt])
        write_json(base / f"{receipt['runbook_id']}.json", [receipt])
        (base / "price_feed_refresh_runbook_current.md").write_text(self._price_feed_refresh_runbook_markdown(receipt), encoding="utf-8")
        history = load_json(base / "price_feed_refresh_runbook_history.json")
        history.append(receipt)
        write_json(base / "price_feed_refresh_runbook_history.json", history)

    def _handoff_markdown(self, receipt: dict[str, Any]) -> str:
        current = receipt.get("current_runtime", {}) if isinstance(receipt.get("current_runtime"), dict) else {}
        check = receipt.get("check", {}) if isinstance(receipt.get("check"), dict) else {}
        action = check.get("operator_next_action", {}) if isinstance(check.get("operator_next_action"), dict) else {}
        evidence = receipt.get("evidence_chain", {}) if isinstance(receipt.get("evidence_chain"), dict) else {}
        lines = [
            "# Tiger/MGC Connector Config Handoff",
            "",
            f"- Status: `{receipt.get('status', '')}`",
            f"- Package ID: `{receipt.get('package_id', '')}`",
            f"- Checked at: `{receipt.get('checked_at', '')}`",
            f"- Operator next action: `{action.get('action', '')}`",
            f"- Requires warning acceptance: `{action.get('requires_accept_warnings') is True}`",
            "",
            "## Current Runtime",
            "",
            f"- Active broker: `{current.get('broker_provider', '')}`",
            f"- Broker dry run: `{current.get('broker_dry_run') is True}`",
            f"- Dualtrack symbol: `{current.get('dualtrack_symbol', '')}`",
            f"- Dualtrack provider: `{current.get('dualtrack_provider', '')}`",
            f"- Human fill sync enabled: `{current.get('human_fill_sync_enabled') is True}`",
            "",
            "## Evidence Chain",
            "",
            f"- Evidence count: `{evidence.get('evidence_count', 0)}`",
            f"- Computed check SHA256: `{(evidence.get('computed_check') or {}).get('payload_sha256', '')}`",
        ]
        for row in evidence.get("sources", []) or []:
            if isinstance(row, dict):
                lines.append(
                    f"- {row.get('name', '')}: exists=`{row.get('exists') is True}` sha256=`{row.get('sha256', '')}` path=`{row.get('path', '')}`"
                )
        for row in evidence.get("current_config", []) or []:
            if isinstance(row, dict):
                lines.append(
                    f"- {row.get('name', '')}: exists=`{row.get('exists') is True}` sha256=`{row.get('sha256', '')}` path=`{row.get('path', '')}`"
                )
        lines.extend([
            "",
            "## Attended Config Apply",
            "",
            "```bash",
            str(receipt.get("attended_apply_command") or ""),
            "```",
            "",
            "## Post-Apply Validation",
            "",
        ])
        lines.extend(f"1. `{command}`" for command in receipt.get("post_apply_validation_commands", []) or [])
        lines.extend(
            [
                "",
                "## Rollback Boundary",
                "",
                f"- Required: `{(receipt.get('rollback_boundary') or {}).get('required') is True}`",
                "- Rollback becomes actionable only after an attended config write creates backups.",
                "",
                "## Not Authorized",
                "",
            ]
        )
        lines.extend(f"- {item}" for item in receipt.get("not_authorized", []) or [])
        lines.extend(["", "## Safety", ""])
        safety = receipt.get("safety", {}) if isinstance(receipt.get("safety"), dict) else {}
        for key in ["writes_runtime_config", "opens_network_clients", "submits_orders", "cancels_orders", "closes_positions", "can_enable_broker_orders"]:
            lines.append(f"- {key}: `{safety.get(key) is True}`")
        lines.append("")
        return "\n".join(lines)

    def _price_feed_refresh_runbook_markdown(self, receipt: dict[str, Any]) -> str:
        stage = receipt.get("operator_stage", {}) if isinstance(receipt.get("operator_stage"), dict) else {}
        runtime = receipt.get("current_runtime", {}) if isinstance(receipt.get("current_runtime"), dict) else {}
        safety = receipt.get("safety", {}) if isinstance(receipt.get("safety"), dict) else {}
        window_gate = receipt.get("refresh_window_gate", {}) if isinstance(receipt.get("refresh_window_gate"), dict) else {}
        window_safety = window_gate.get("safety", {}) if isinstance(window_gate.get("safety"), dict) else {}
        lines = [
            "# Tiger/MGC Price-Feed Refresh Runbook",
            "",
            f"- Status: `{receipt.get('status', '')}`",
            f"- Runbook ID: `{receipt.get('runbook_id', '')}`",
            f"- Checked at: `{receipt.get('checked_at', '')}`",
            f"- Operator stage: `{stage.get('stage', '')}`",
            f"- Next action: `{stage.get('next_action', '')}`",
            "",
            "## Objective",
            "",
            "- Refresh Tiger/MGC price-feed evidence before any attended config switch.",
            "- Keep price-feed refresh, config switching, machine strategy approval, and broker order submission as separate gates.",
            "",
            "## Current Runtime",
            "",
            f"- Active broker: `{runtime.get('broker_provider', '')}`",
            f"- Broker dry run: `{runtime.get('broker_dry_run') is True}`",
            f"- Dualtrack symbol: `{runtime.get('dualtrack_symbol', '')}`",
            f"- Runtime switched to Tiger/MGC: `{stage.get('runtime_switched_to_tiger_mgc') is True}`",
            f"- Can switch config now: `{stage.get('can_switch_config_with_operator_authorization') is True}`",
            f"- Can trade machine track: `{stage.get('can_trade_machine_track') is True}`",
            f"- Can submit Tiger orders: `{stage.get('can_submit_tiger_orders') is True}`",
            "",
            "## Refresh Window Gate",
            "",
            f"- Status: `{window_gate.get('status', '')}`",
            f"- Operator action: `{window_gate.get('operator_action', '')}`",
            f"- COMEX open: `{window_gate.get('is_open') is True}`",
            f"- Next open: `{window_gate.get('next_open') or ''}`",
            f"- Can run QuoteClient acceptance step now: `{window_gate.get('can_run_quote_client_step') is True}`",
            f"- Safety: opens_quote_client=`{window_safety.get('opens_quote_client') is True}`, opens_trade_client=`{window_safety.get('opens_trade_client') is True}`, submits_orders=`{window_safety.get('submits_orders') is True}`, writes_runtime_config=`{window_safety.get('writes_runtime_config') is True}`",
            "",
            "## Command Sequence",
            "",
        ]
        commands = [row for row in (receipt.get("command_sequence") or []) if isinstance(row, dict)]
        if not commands:
            lines.append("- No refresh command is required from the current operator stage.")
        for index, row in enumerate(commands, start=1):
            lines.extend(
                [
                    f"{index}. {row.get('name', '')}",
                    "",
                    f"   Purpose: {row.get('purpose', '')}",
                    "",
                    "   ```bash",
                    f"   {row.get('command', '')}",
                    "   ```",
                    "",
                    f"   Safety: opens_quote_client=`{row.get('opens_quote_client') is True}`, opens_trade_client=`{row.get('opens_trade_client') is True}`, submits_orders=`{row.get('submits_orders') is True}`, writes_runtime_config=`{row.get('writes_runtime_config') is True}`, writes_market_db=`{row.get('writes_market_db') is True}`",
                    "",
                ]
            )
        lines.extend(["## Not Authorized", ""])
        lines.extend(f"- {item}" for item in receipt.get("not_authorized", []) or [])
        lines.extend(["", "## Runbook Generation Safety", ""])
        for key in [
            "writes_runtime_config",
            "writes_market_db",
            "opens_network_clients",
            "opens_quote_client",
            "opens_trade_client",
            "submits_orders",
            "cancels_orders",
            "closes_positions",
            "can_enable_broker_orders",
        ]:
            lines.append(f"- {key}: `{safety.get(key) is True}`")
        lines.append("")
        return "\n".join(lines)

    def _authorization_markdown(self, receipt: dict[str, Any]) -> str:
        current = receipt.get("current_runtime", {}) if isinstance(receipt.get("current_runtime"), dict) else {}
        check = receipt.get("check", {}) if isinstance(receipt.get("check"), dict) else {}
        handoff = receipt.get("handoff", {}) if isinstance(receipt.get("handoff"), dict) else {}
        rehearsal = receipt.get("rehearsal", {}) if isinstance(receipt.get("rehearsal"), dict) else {}
        action = receipt.get("operator_next_action", {}) if isinstance(receipt.get("operator_next_action"), dict) else {}
        artifacts = receipt.get("artifacts", {}) if isinstance(receipt.get("artifacts"), dict) else {}
        lines = [
            "# Tiger/MGC Connector Config Authorization Package",
            "",
            f"- Status: `{receipt.get('status', '')}`",
            f"- Package ID: `{receipt.get('package_id', '')}`",
            f"- Checked at: `{receipt.get('checked_at', '')}`",
            f"- Operator next action: `{action.get('action', '')}`",
            f"- Requires warning acceptance: `{action.get('requires_accept_warnings') is True}`",
            "",
            "## Current Runtime",
            "",
            f"- Active broker: `{current.get('broker_provider', '')}`",
            f"- Broker dry run: `{current.get('broker_dry_run') is True}`",
            f"- Dualtrack symbol: `{current.get('dualtrack_symbol', '')}`",
            f"- Dualtrack provider: `{current.get('dualtrack_provider', '')}`",
            f"- Human fill sync enabled: `{current.get('human_fill_sync_enabled') is True}`",
            "",
            "## Package Evidence",
            "",
            f"- Check: `{check.get('status', '')}` / write preflight `{check.get('write_preflight_status', '')}`",
            f"- Handoff: `{handoff.get('status', '')}` / `{handoff.get('handoff_id', '')}`",
            f"- Rehearsal: `{rehearsal.get('status', '')}` / `{rehearsal.get('rehearsal_id', '')}`",
            f"- Runtime unchanged in rehearsal: `{rehearsal.get('runtime_config_unchanged') is True}`",
            "",
            "## Attended Config Apply",
            "",
            "```bash",
            str(receipt.get("attended_apply_command") or ""),
            "```",
            "",
            "## Post-Apply Validation",
            "",
        ]
        lines.extend(f"1. `{command}`" for command in receipt.get("post_apply_validation_commands", []) or [])
        lines.extend(
            [
                "",
                "## Rollback Boundary",
                "",
                f"- Required: `{(receipt.get('rollback_boundary') or {}).get('required') is True}`",
                "- Rollback becomes actionable only after an attended config write creates backups.",
                "",
                "## Artifacts",
                "",
            ]
        )
        lines.extend(f"- {name}: `{path}`" for name, path in artifacts.items())
        lines.extend(["", "## Blockers", ""])
        blockers = [row for row in (receipt.get("blockers") or []) if isinstance(row, dict)]
        lines.extend(
            [f"- {row.get('name', '')}: {row.get('summary', '')}" for row in blockers]
            or ["- none"]
        )
        lines.extend(["", "## Not Authorized", ""])
        lines.extend(f"- {item}" for item in receipt.get("not_authorized", []) or [])
        lines.extend(["", "## Safety", ""])
        safety = receipt.get("safety", {}) if isinstance(receipt.get("safety"), dict) else {}
        for key in ["writes_runtime_config", "opens_network_clients", "submits_orders", "cancels_orders", "closes_positions", "can_enable_broker_orders"]:
            lines.append(f"- {key}: `{safety.get(key) is True}`")
        lines.append("")
        return "\n".join(lines)

    def _readiness_audit_markdown(self, receipt: dict[str, Any]) -> str:
        current = receipt.get("current_runtime", {}) if isinstance(receipt.get("current_runtime"), dict) else {}
        price_feed = receipt.get("price_feed", {}) if isinstance(receipt.get("price_feed"), dict) else {}
        market = price_feed.get("market_coverage", {}) if isinstance(price_feed.get("market_coverage"), dict) else {}
        authorization = receipt.get("config_authorization", {}) if isinstance(receipt.get("config_authorization"), dict) else {}
        strategy = receipt.get("strategy_gate", {}) if isinstance(receipt.get("strategy_gate"), dict) else {}
        order_gate = receipt.get("broker_order_gate", {}) if isinstance(receipt.get("broker_order_gate"), dict) else {}
        next_action = receipt.get("operator_next_action", {}) if isinstance(receipt.get("operator_next_action"), dict) else {}
        lines = [
            "# Tiger/MGC Final Readiness Audit",
            "",
            f"- Status: `{receipt.get('status', '')}`",
            f"- Current stage: `{receipt.get('current_stage', '')}`",
            f"- Progress: `{(receipt.get('progress') or {}).get('percent', '')}%`",
            f"- Package ID: `{receipt.get('package_id', '')}`",
            f"- Checked at: `{receipt.get('checked_at', '')}`",
            "",
            "## Current Runtime",
            "",
            f"- Active broker: `{current.get('broker_provider', '')}`",
            f"- Broker dry run: `{current.get('broker_dry_run') is True}`",
            f"- Dualtrack symbol: `{current.get('dualtrack_symbol', '')}`",
            f"- Dualtrack provider: `{current.get('dualtrack_provider', '')}`",
            f"- Human fill sync enabled: `{current.get('human_fill_sync_enabled') is True}`",
            "",
            "## Go / No-Go Matrix",
            "",
            "| User value | Status | Evidence |",
            "|---|---|---|",
            f"| Use Tiger/MGC price source | `{(price_feed.get('acceptance') or {}).get('status', 'missing')}` | realtime `{(price_feed.get('realtime_validation') or {}).get('status', 'missing')}`, market rows `{market.get('rows', 0)}` through `{market.get('last_timestamp', '')}` |",
            f"| Manual config switch | `{authorization.get('status', 'missing')}` | package `{authorization.get('package_id', '')}`, blockers `{authorization.get('blocker_count', 0)}` |",
            f"| Current runtime unchanged | `{'yes' if current.get('broker_provider') != 'tiger_openapi' and current.get('dualtrack_symbol') != 'MGCmain' else 'no'}` | active `{current.get('broker_provider', '')}` / `{current.get('dualtrack_symbol', '')}` |",
            f"| Machine strategy tradability | `{strategy.get('status', 'not_trade_ready')}` | requires edge approval `{strategy.get('requires_strategy_edge_approval') is True}` |",
            f"| Tiger order submission | `{order_gate.get('status', 'closed')}` | can submit `{receipt.get('can_submit_tiger_orders') is True}` |",
            "",
            "## Operator Next Action",
            "",
            f"- Action: `{next_action.get('action', '')}`",
            f"- Authorization package: `{next_action.get('authorization_markdown', '')}`",
            "",
            "## Blockers",
            "",
        ]
        blockers = [row for row in (receipt.get("blockers") or []) if isinstance(row, dict)]
        lines.extend([f"- {row.get('name', '')}: {row.get('summary', '')}" for row in blockers] or ["- none"])
        lines.extend(["", "## Not Authorized", ""])
        lines.extend(f"- {item}" for item in receipt.get("not_authorized", []) or [])
        lines.extend(["", "## Safety", ""])
        safety = receipt.get("safety", {}) if isinstance(receipt.get("safety"), dict) else {}
        for key in ["writes_runtime_config", "writes_market_db", "opens_network_clients", "submits_orders", "cancels_orders", "closes_positions", "can_enable_broker_orders"]:
            lines.append(f"- {key}: `{safety.get(key) is True}`")
        lines.append("")
        return "\n".join(lines)

    def _post_switch_validation_markdown(self, receipt: dict[str, Any]) -> str:
        current = receipt.get("current_runtime", {}) if isinstance(receipt.get("current_runtime"), dict) else {}
        price_feed = receipt.get("price_feed", {}) if isinstance(receipt.get("price_feed"), dict) else {}
        market = price_feed.get("market_coverage", {}) if isinstance(price_feed.get("market_coverage"), dict) else {}
        apply = receipt.get("latest_apply", {}) if isinstance(receipt.get("latest_apply"), dict) else {}
        rollback = receipt.get("rollback", {}) if isinstance(receipt.get("rollback"), dict) else {}
        lines = [
            "# Tiger/MGC Post-Switch Validation",
            "",
            f"- Status: `{receipt.get('status', '')}`",
            f"- Package ID: `{receipt.get('package_id', '')}`",
            f"- Checked at: `{receipt.get('checked_at', '')}`",
            "",
            "## Runtime Config",
            "",
            f"- Active broker: `{current.get('broker_provider', '')}`",
            f"- Broker profile: `{current.get('broker_profile', '')}`",
            f"- Broker dry run: `{current.get('broker_dry_run') is True}`",
            f"- Broker orders enabled: `{current.get('can_enable_broker_orders') is True}`",
            f"- Dualtrack symbol: `{current.get('dualtrack_symbol', '')}`",
            f"- Dualtrack provider: `{current.get('dualtrack_provider', '')}`",
            f"- Market session: `{current.get('market_session_enabled') is True}` / `{current.get('market_session_venue', '')}`",
            f"- Cost model: `{current.get('execution_cost_venue', '')}` / `{current.get('execution_quantity_mode', '')}`",
            f"- Human fill sync enabled: `{current.get('human_fill_sync_enabled') is True}`",
            "",
            "## Evidence",
            "",
            f"- Latest apply: `{apply.get('status', '')}` / `{apply.get('apply_id', '')}`",
            f"- Rollback backup: `{rollback.get('backup_created') is True}` / `{rollback.get('backup_dir', '')}`",
            f"- Price-feed acceptance: `{(price_feed.get('acceptance') or {}).get('status', 'missing')}`",
            f"- Realtime validation: `{(price_feed.get('realtime_validation') or {}).get('status', 'missing')}`",
            f"- Market coverage: `{market.get('rows', 0)}` rows through `{market.get('last_timestamp', '')}`",
            "",
            "## Blockers",
            "",
        ]
        blockers = [row for row in (receipt.get("blockers") or []) if isinstance(row, dict)]
        lines.extend([f"- {row.get('name', '')}: {row.get('summary', '')}" for row in blockers] or ["- none"])
        lines.extend(["", "## Not Authorized", ""])
        lines.extend(f"- {item}" for item in receipt.get("not_authorized", []) or [])
        lines.extend(["", "## Safety", ""])
        safety = receipt.get("safety", {}) if isinstance(receipt.get("safety"), dict) else {}
        for key in ["writes_runtime_config", "writes_market_db", "opens_network_clients", "submits_orders", "cancels_orders", "closes_positions", "can_enable_broker_orders"]:
            lines.append(f"- {key}: `{safety.get(key) is True}`")
        lines.append("")
        return "\n".join(lines)

    def _apply_summary(self, receipt: dict[str, Any]) -> dict[str, Any]:
        if not receipt:
            return {"status": "missing", "mode": "", "apply_id": "", "connector_id": "", "backup_created": False}
        backup = receipt.get("backup", {}) if isinstance(receipt.get("backup"), dict) else {}
        changes = receipt.get("change_counts", {}) if isinstance(receipt.get("change_counts"), dict) else {}
        return {
            "status": str(receipt.get("status") or "missing"),
            "mode": str(receipt.get("mode") or ""),
            "apply_id": str(receipt.get("apply_id") or ""),
            "connector_id": str(receipt.get("connector_id") or ""),
            "package_id": str(receipt.get("package_id") or ""),
            "requested_roles": list(receipt.get("requested_roles") or []),
            "change_count": int(changes.get("total") or 0),
            "backup_created": backup.get("created") is True,
            "backup_dir": str(backup.get("backup_dir") or ""),
            "blocker_count": len([row for row in (receipt.get("blockers") or []) if isinstance(row, dict)]),
            "warning_count": len([row for row in (receipt.get("warnings") or []) if isinstance(row, dict)]),
        }

    def _check_summary(self, receipt: dict[str, Any]) -> dict[str, Any]:
        if not receipt:
            return {"status": "missing", "package_id": "", "usable_for_attended_config_write": False, "blocker_count": 0}
        rollback_boundary = receipt.get("rollback_boundary", {}) if isinstance(receipt.get("rollback_boundary"), dict) else {}
        return {
            "status": str(receipt.get("status") or "missing"),
            "package_id": str(receipt.get("package_id") or ""),
            "usable_for_attended_config_write": receipt.get("usable_for_attended_config_write") is True,
            "blocker_count": len([row for row in (receipt.get("blockers") or []) if isinstance(row, dict)]),
            "warning_count": len([row for row in (receipt.get("warnings") or []) if isinstance(row, dict)]),
            "write_preflight_status": str((receipt.get("write_preflight") or {}).get("status") or "missing")
            if isinstance(receipt.get("write_preflight"), dict)
            else "missing",
            "write_preflight_blocker_count": len(
                [
                    row
                    for row in ((receipt.get("write_preflight") or {}).get("blockers") or [])
                    if isinstance(row, dict)
                ]
            )
            if isinstance(receipt.get("write_preflight"), dict)
            else 0,
            "post_apply_validation_count": len(
                [command for command in (receipt.get("post_apply_validation_commands") or []) if command]
            ),
            "rollback_required": rollback_boundary.get("required") is True,
            "operator_next_action": receipt.get("operator_next_action", {}) if isinstance(receipt.get("operator_next_action"), dict) else {},
        }

    def _rollback_summary(self, receipt: dict[str, Any]) -> dict[str, Any]:
        if not receipt:
            return {"status": "missing", "rollback_id": "", "apply_id": "", "restored_file_count": 0}
        return {
            "status": str(receipt.get("status") or "missing"),
            "rollback_id": str(receipt.get("rollback_id") or ""),
            "apply_id": str(receipt.get("apply_id") or ""),
            "restored_file_count": len([row for row in (receipt.get("restored_files") or []) if isinstance(row, dict)]),
            "blocker_count": len([row for row in (receipt.get("blockers") or []) if isinstance(row, dict)]),
        }

    def _handoff_summary(self, receipt: dict[str, Any]) -> dict[str, Any]:
        if not receipt:
            return {
                "status": "missing",
                "handoff_id": "",
                "package_id": "",
                "evidence_count": 0,
                "post_apply_validation_count": 0,
            }
        current = receipt.get("current_runtime", {}) if isinstance(receipt.get("current_runtime"), dict) else {}
        evidence = receipt.get("evidence_chain", {}) if isinstance(receipt.get("evidence_chain"), dict) else {}
        safety = receipt.get("safety", {}) if isinstance(receipt.get("safety"), dict) else {}
        return {
            "status": str(receipt.get("status") or "missing"),
            "handoff_id": str(receipt.get("handoff_id") or ""),
            "package_id": str(receipt.get("package_id") or ""),
            "broker_provider": str(current.get("broker_provider") or ""),
            "dualtrack_symbol": str(current.get("dualtrack_symbol") or ""),
            "evidence_count": int(evidence.get("evidence_count") or 0),
            "post_apply_validation_count": len([command for command in (receipt.get("post_apply_validation_commands") or []) if command]),
            "writes_runtime_config": safety.get("writes_runtime_config") is True,
            "opens_network_clients": safety.get("opens_network_clients") is True,
            "submits_orders": safety.get("submits_orders") is True,
        }

    def _rehearsal_summary(self, receipt: dict[str, Any]) -> dict[str, Any]:
        if not receipt:
            return {
                "status": "missing",
                "rehearsal_id": "",
                "package_id": "",
                "runtime_config_unchanged": False,
            }
        runtime = receipt.get("runtime_config", {}) if isinstance(receipt.get("runtime_config"), dict) else {}
        sandbox = receipt.get("sandbox", {}) if isinstance(receipt.get("sandbox"), dict) else {}
        safety = receipt.get("safety", {}) if isinstance(receipt.get("safety"), dict) else {}
        return {
            "status": str(receipt.get("status") or "missing"),
            "rehearsal_id": str(receipt.get("rehearsal_id") or ""),
            "package_id": str(receipt.get("package_id") or ""),
            "runtime_config_unchanged": runtime.get("unchanged") is True,
            "sandbox_root": str(sandbox.get("root") or ""),
            "writes_runtime_config": safety.get("writes_runtime_config") is True,
            "writes_sandbox_config": safety.get("writes_sandbox_config") is True,
            "opens_network_clients": safety.get("opens_network_clients") is True,
            "submits_orders": safety.get("submits_orders") is True,
        }

    def _authorization_summary(self, receipt: dict[str, Any]) -> dict[str, Any]:
        if not receipt:
            return {
                "status": "missing",
                "authorization_id": "",
                "package_id": "",
                "operator_next_action": {},
                "blocker_count": 0,
                "post_apply_validation_count": 0,
                "writes_runtime_config": False,
                "opens_network_clients": False,
                "submits_orders": False,
            }
        safety = receipt.get("safety", {}) if isinstance(receipt.get("safety"), dict) else {}
        return {
            "status": str(receipt.get("status") or "missing"),
            "authorization_id": str(receipt.get("authorization_id") or ""),
            "package_id": str(receipt.get("package_id") or ""),
            "operator_next_action": receipt.get("operator_next_action", {}) if isinstance(receipt.get("operator_next_action"), dict) else {},
            "blocker_count": len([row for row in (receipt.get("blockers") or []) if isinstance(row, dict)]),
            "post_apply_validation_count": len([command for command in (receipt.get("post_apply_validation_commands") or []) if command]),
            "writes_runtime_config": safety.get("writes_runtime_config") is True,
            "opens_network_clients": safety.get("opens_network_clients") is True,
            "submits_orders": safety.get("submits_orders") is True,
        }

    def _readiness_audit_summary(self, receipt: dict[str, Any]) -> dict[str, Any]:
        if not receipt:
            return {
                "status": "missing",
                "audit_id": "",
                "checked_at": "",
                "current_stage": "",
                "package_id": "",
                "can_switch_config_with_operator_authorization": False,
                "can_trade_machine_track": False,
                "can_submit_tiger_orders": False,
                "blocker_count": 0,
                "writes_runtime_config": False,
                "opens_network_clients": False,
                "submits_orders": False,
            }
        safety = receipt.get("safety", {}) if isinstance(receipt.get("safety"), dict) else {}
        return {
            "status": str(receipt.get("status") or "missing"),
            "audit_id": str(receipt.get("audit_id") or ""),
            "checked_at": str(receipt.get("checked_at") or ""),
            "current_stage": str(receipt.get("current_stage") or ""),
            "package_id": str(receipt.get("package_id") or ""),
            "can_switch_config_with_operator_authorization": receipt.get("can_switch_config_with_operator_authorization") is True,
            "can_trade_machine_track": receipt.get("can_trade_machine_track") is True,
            "can_submit_tiger_orders": receipt.get("can_submit_tiger_orders") is True,
            "blocker_count": len([row for row in (receipt.get("blockers") or []) if isinstance(row, dict)]),
            "writes_runtime_config": safety.get("writes_runtime_config") is True,
            "opens_network_clients": safety.get("opens_network_clients") is True,
            "submits_orders": safety.get("submits_orders") is True,
        }

    def _post_switch_validation_summary(self, receipt: dict[str, Any]) -> dict[str, Any]:
        if not receipt:
            return {
                "status": "missing",
                "validation_id": "",
                "package_id": "",
                "blocker_count": 0,
                "broker_provider": "",
                "dualtrack_symbol": "",
                "rollback_available": False,
                "writes_runtime_config": False,
                "opens_network_clients": False,
                "submits_orders": False,
            }
        current = receipt.get("current_runtime", {}) if isinstance(receipt.get("current_runtime"), dict) else {}
        rollback = receipt.get("rollback", {}) if isinstance(receipt.get("rollback"), dict) else {}
        safety = receipt.get("safety", {}) if isinstance(receipt.get("safety"), dict) else {}
        return {
            "status": str(receipt.get("status") or "missing"),
            "validation_id": str(receipt.get("validation_id") or ""),
            "package_id": str(receipt.get("package_id") or ""),
            "blocker_count": len([row for row in (receipt.get("blockers") or []) if isinstance(row, dict)]),
            "broker_provider": str(current.get("broker_provider") or ""),
            "dualtrack_symbol": str(current.get("dualtrack_symbol") or ""),
            "rollback_available": rollback.get("available") is True,
            "writes_runtime_config": safety.get("writes_runtime_config") is True,
            "opens_network_clients": safety.get("opens_network_clients") is True,
            "submits_orders": safety.get("submits_orders") is True,
        }

    def _price_feed_refresh_runbook_summary(self, receipt: dict[str, Any]) -> dict[str, Any]:
        if not receipt:
            return {
                "status": "missing",
                "runbook_id": "",
                "checked_at": "",
                "operator_stage": "",
                "command_count": 0,
                "status_snapshot_exists": False,
                "writes_runtime_config": False,
                "opens_network_clients": False,
                "opens_quote_client": False,
                "opens_trade_client": False,
                "submits_orders": False,
                "refresh_window_status": "",
                "refresh_window_next_open": None,
                "refresh_window_can_run_quote_client_step": False,
            }
        stage = receipt.get("operator_stage", {}) if isinstance(receipt.get("operator_stage"), dict) else {}
        artifacts = receipt.get("artifacts", {}) if isinstance(receipt.get("artifacts"), dict) else {}
        safety = receipt.get("safety", {}) if isinstance(receipt.get("safety"), dict) else {}
        window_gate = receipt.get("refresh_window_gate", {}) if isinstance(receipt.get("refresh_window_gate"), dict) else {}
        status_path_text = str(artifacts.get("status_json") or "")
        status_snapshot_exists = Path(status_path_text).exists() if status_path_text else False
        return {
            "status": str(receipt.get("status") or "missing"),
            "runbook_id": str(receipt.get("runbook_id") or ""),
            "checked_at": str(receipt.get("checked_at") or ""),
            "operator_stage": str(stage.get("stage") or ""),
            "command_count": len([row for row in (receipt.get("command_sequence") or []) if isinstance(row, dict)]),
            "status_snapshot_exists": status_snapshot_exists,
            "writes_runtime_config": safety.get("writes_runtime_config") is True,
            "opens_network_clients": safety.get("opens_network_clients") is True,
            "opens_quote_client": safety.get("opens_quote_client") is True,
            "opens_trade_client": safety.get("opens_trade_client") is True,
            "submits_orders": safety.get("submits_orders") is True,
            "refresh_window_status": str(window_gate.get("status") or ""),
            "refresh_window_next_open": window_gate.get("next_open"),
            "refresh_window_can_run_quote_client_step": window_gate.get("can_run_quote_client_step") is True,
        }

    def _runtime_config_evidence(self) -> dict[str, str]:
        return {
            "pipeline_config": str(self.pipeline_config_path),
            "pipeline_sha256": self._sha256(self.pipeline_config_path),
            "dualtrack_config": str(self.dualtrack_config_path),
            "dualtrack_sha256": self._sha256(self.dualtrack_config_path),
        }

    def _rehearsal_status(
        self,
        dry_run: dict[str, Any],
        pre_handoff_check: dict[str, Any],
        handoff: dict[str, Any],
        write_check: dict[str, Any],
        authorization: dict[str, Any],
        readiness_audit: dict[str, Any],
        applied: dict[str, Any],
        post_switch_validation: dict[str, Any],
        rollback: dict[str, Any],
        real_before: dict[str, str],
        real_after: dict[str, str],
    ) -> str:
        if dry_run.get("status") != "dry_run_ready":
            return "blocked"
        if pre_handoff_check.get("status") != "ready_for_handoff":
            return "blocked"
        if handoff.get("status") != "ready_for_operator_review":
            return "blocked"
        if write_check.get("status") != "ready_for_attended_config_write":
            return "blocked"
        if authorization.get("status") != "ready_for_operator_authorization":
            return "blocked"
        if readiness_audit.get("status") != "go_for_attended_config_switch":
            return "blocked"
        if applied.get("status") != "applied":
            return "blocked"
        if post_switch_validation.get("status") != "validated_post_switch":
            return "blocked"
        if rollback.get("status") != "rolled_back":
            return "blocked"
        if real_before != real_after:
            return "failed_runtime_config_changed"
        return "passed"

    def _sha256(self, path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _payload_sha256(self, payload: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()

    def _apply_id(self, *, prefix: str = "apply") -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        return f"{prefix}_{stamp}"

    def _now(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    def _safety(self, *, writes_runtime_config: bool) -> dict[str, bool]:
        return {
            "writes_runtime_config": writes_runtime_config,
            "opens_network_clients": False,
            "submits_orders": False,
            "cancels_orders": False,
            "closes_positions": False,
            "stores_credentials": False,
            "credential_values_exposed": False,
            "can_enable_broker_orders": False,
        }
