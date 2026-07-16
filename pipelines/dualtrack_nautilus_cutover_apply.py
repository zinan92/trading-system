"""Transactional attended switch between Legacy and Nautilus paper engines.

The controller is deliberately paper-only and does not start a robot or submit
an order. It requires the independent M4 precheck, quiesces the two services
which construct the execution adapter, backs up every changed file, applies the
engine/service-environment change, and validates the restarted API. Any failed
post-switch validation restores the exact backup before returning.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import URLError
from urllib.request import Request, urlopen

from services.config_loader import ROOT, load_pipeline_config
from services.dualtrack_config import dualtrack_config
from services.journal_store import load_json, write_json


CUTOVER_ACKNOWLEDGEMENT = "I_UNDERSTAND_NAUTILUS_PAPER_CUTOVER_WILL_RESTART_LOCAL_SERVICES"
ROLLBACK_ACKNOWLEDGEMENT = "I_UNDERSTAND_NAUTILUS_PAPER_ROLLBACK_WILL_RESTORE_LEGACY_AND_RESTART_LOCAL_SERVICES"
SERVICE_LABELS = (
    "com.wendy.trading-orchestrator.dualtrack-live-tick",
    "com.wendy.trading-orchestrator.dashboard",
)


class DualTrackNautilusCutoverController:
    def __init__(
        self,
        output_root: Path,
        *,
        config_path: Path | None = None,
        launch_agents_dir: Path | None = None,
        environ: dict[str, str] | None = None,
        precheck_builder: Callable[..., dict[str, Any]] | None = None,
        rollback_precheck_builder: Callable[..., dict[str, Any]] | None = None,
        service_quiescer: Callable[[tuple[str, ...]], list[dict[str, Any]]] | None = None,
        service_starter: Callable[[tuple[Path, ...]], list[dict[str, Any]]] | None = None,
        engine_validator: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.config_path = Path(config_path or ROOT / "configs" / "dualtrack.yaml")
        self.launch_agents_dir = Path(launch_agents_dir or Path.home() / "Library" / "LaunchAgents")
        self.environ = dict(os.environ if environ is None else environ)
        self.precheck_builder = precheck_builder or self._default_precheck
        self.rollback_precheck_builder = rollback_precheck_builder or self._default_rollback_precheck
        self.service_quiescer = service_quiescer or self._default_quiesce
        self.service_starter = service_starter or self._default_start
        self.engine_validator = engine_validator or self._default_engine_validator

    def apply(self, *, acknowledgement: str, cycle_id: str | None = None) -> dict[str, Any]:
        if acknowledgement != CUTOVER_ACKNOWLEDGEMENT:
            return self._blocked("missing_cutover_acknowledgement", acknowledgement_ok=False)
        config = dualtrack_config(self.config_path)
        precheck = self.precheck_builder(
            self.output_root,
            config=config,
            environ=self.environ,
            cycle_id=cycle_id,
        )
        if precheck.get("status") != "ready_for_operator_cutover":
            return self._blocked("precheck_not_ready", acknowledgement_ok=True, precheck=precheck)

        runtime_path = str(self.environ.get("TRADING_ORCHESTRATOR_NAUTILUS_PYTHON") or "").strip()
        targets = self._targets()
        missing = [str(path) for path in targets if not path.exists()]
        if missing:
            return self._blocked(
                "cutover_target_missing",
                acknowledgement_ok=True,
                precheck=precheck,
                detail={"missing": missing},
            )

        cutover_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup_dir = self.output_root / "dualtrack" / "cutover" / "backups" / cutover_id
        backups = self._backup_targets(targets, backup_dir)
        service_paths = self._service_paths()
        quiesce_receipts: list[dict[str, Any]] = []
        start_receipts: list[dict[str, Any]] = []
        post_validation: dict[str, Any] = {"status": "not_run"}
        rollback_validation: dict[str, Any] = {"status": "not_run"}

        try:
            quiesce_receipts = self.service_quiescer(SERVICE_LABELS)
            self._require_service_success(quiesce_receipts, expected="stopped")
            self._write_cutover_files(runtime_path)
            start_receipts = self.service_starter(service_paths)
            self._require_service_success(start_receipts, expected="started")
            post_validation = self.engine_validator("nautilus_paper")
            if post_validation.get("status") != "ok" or post_validation.get("engine") != "nautilus_paper":
                raise RuntimeError("post-cutover Nautilus validation failed")
        except Exception as exc:
            rollback_quiesce = self._safe_quiesce()
            try:
                self._require_service_success(rollback_quiesce, expected="stopped")
                self._restore_backups(backups)
                rollback_start = self._safe_start(service_paths)
                self._require_service_success(rollback_start, expected="started")
                rollback_validation = self.engine_validator("legacy_paper")
                if rollback_validation.get("status") != "ok" or rollback_validation.get("engine") != "legacy_paper":
                    raise RuntimeError("automatic rollback Legacy validation failed")
                rollback_status = "restored"
                rollback_error = ""
                status = "rolled_back_after_failed_validation"
            except Exception as rollback_exception:
                rollback_start = locals().get("rollback_start", [])
                rollback_status = "failed"
                rollback_error = str(rollback_exception)
                rollback_validation = locals().get(
                    "rollback_validation",
                    {"status": "not_run", "error": rollback_error},
                )
                status = "automatic_rollback_failed"
            payload = {
                "schema_version": "dualtrack-nautilus-cutover-apply-v1",
                "cutover_id": cutover_id,
                "cycle_id": str(precheck.get("cycle_id") or cycle_id or ""),
                "status": status,
                "error": str(exc),
                "precheck": precheck,
                "backups": backups,
                "quiesce_receipts": quiesce_receipts,
                "start_receipts": start_receipts,
                "post_validation": post_validation,
                "automatic_rollback": {
                    "status": rollback_status,
                    "error": rollback_error,
                    "quiesce_receipts": rollback_quiesce,
                    "start_receipts": rollback_start,
                },
                "rollback_validation": rollback_validation,
                **self._safety(config_write_performed=True),
            }
            self._write_receipt("apply", cutover_id, payload)
            return payload

        payload = {
            "schema_version": "dualtrack-nautilus-cutover-apply-v1",
            "cutover_id": cutover_id,
            "cycle_id": str(precheck.get("cycle_id") or cycle_id or ""),
            "status": "applied",
            "precheck": precheck,
            "backups": backups,
            "quiesce_receipts": quiesce_receipts,
            "start_receipts": start_receipts,
            "post_validation": post_validation,
            "rollback": {
                "required": True,
                "acknowledgement": ROLLBACK_ACKNOWLEDGEMENT,
                "preserves_namespaces": ["legacy", "nautilus_paper", "nautilus_authoritative"],
            },
            **self._safety(config_write_performed=True),
        }
        self._write_receipt("apply", cutover_id, payload)
        return payload

    def rollback(
        self,
        apply_receipt: dict[str, Any],
        *,
        acknowledgement: str,
    ) -> dict[str, Any]:
        if acknowledgement != ROLLBACK_ACKNOWLEDGEMENT:
            return self._blocked("missing_rollback_acknowledgement", acknowledgement_ok=False)
        if apply_receipt.get("status") != "applied":
            return self._blocked("apply_receipt_not_rollback_eligible", acknowledgement_ok=True)
        precheck = self.rollback_precheck_builder(
            self.output_root,
            config=dualtrack_config(self.config_path),
            environ=self.environ,
            cycle_id=str(apply_receipt.get("cycle_id") or "") or None,
        )
        if precheck.get("status") != "ready_for_rollback":
            return self._blocked("rollback_precheck_not_ready", acknowledgement_ok=True, precheck=precheck)

        backups = list(apply_receipt.get("backups") or [])
        if not self._backups_match_targets(backups):
            return self._blocked("rollback_backup_contract_invalid", acknowledgement_ok=True, precheck=precheck)
        service_paths = self._service_paths()
        rollback_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        try:
            quiesce_receipts = self.service_quiescer(SERVICE_LABELS)
            self._require_service_success(quiesce_receipts, expected="stopped")
            self._restore_backups(backups)
            start_receipts = self.service_starter(service_paths)
            self._require_service_success(start_receipts, expected="started")
            post_validation = self.engine_validator("legacy_paper")
            if post_validation.get("status") != "ok" or post_validation.get("engine") != "legacy_paper":
                raise RuntimeError("post-rollback Legacy validation failed")
            status = "rolled_back"
            error = ""
        except Exception as exc:
            status = "rollback_validation_failed"
            error = str(exc)
            quiesce_receipts = locals().get("quiesce_receipts", [])
            start_receipts = locals().get("start_receipts", [])
            post_validation = locals().get("post_validation", {"status": "not_run"})

        payload = {
            "schema_version": "dualtrack-nautilus-cutover-rollback-v1",
            "rollback_id": rollback_id,
            "cutover_id": str(apply_receipt.get("cutover_id") or ""),
            "cycle_id": str(apply_receipt.get("cycle_id") or ""),
            "status": status,
            "error": error,
            "precheck": precheck,
            "quiesce_receipts": quiesce_receipts,
            "start_receipts": start_receipts,
            "post_validation": post_validation,
            **self._safety(config_write_performed=True),
        }
        self._write_receipt("rollback", rollback_id, payload)
        return payload

    def _default_precheck(self, *args, **kwargs) -> dict[str, Any]:
        from pipelines.dualtrack_nautilus_attended_cutover import build_attended_cutover_precheck

        return build_attended_cutover_precheck(*args, **kwargs)

    def _default_rollback_precheck(
        self,
        output_root: Path,
        *,
        config: dict[str, Any],
        environ: dict[str, str],
        cycle_id: str | None,
    ) -> dict[str, Any]:
        from services.dualtrack_execution_adapter import build_configured_execution_engine_adapter
        from services.strategy_control_plane import StrategyControlPlane

        selected_cycle = str(cycle_id or "")
        runtime = StrategyControlPlane(output_root).runtime_state(selected_cycle)
        blockers: list[dict[str, Any]] = []
        if (config.get("execution_engine") or {}).get("authoritative") != "nautilus_paper":
            blockers.append({"code": "nautilus_not_authoritative", "detail": {}})
        if runtime.get("desired_state") != "stopped" or runtime.get("actual_state") != "stopped":
            blockers.append({"code": "runtime_not_stopped", "detail": runtime})
        try:
            adapter = build_configured_execution_engine_adapter(output_root, config=config, environ=environ)
            snapshot = adapter.snapshot(selected_cycle)
            reconciliation = adapter.reconcile(selected_cycle)
            accepted = [row for row in snapshot.get("orders") or [] if row.get("state") == "accepted"]
            open_positions = [row for row in snapshot.get("positions") or [] if row.get("status") == "open"]
            if accepted:
                blockers.append({"code": "nautilus_orders_not_flat", "detail": {"count": len(accepted)}})
            if open_positions:
                blockers.append({"code": "nautilus_positions_not_flat", "detail": {"count": len(open_positions)}})
            if reconciliation.get("status") != "ok":
                blockers.append({"code": "nautilus_reconciliation_not_ok", "detail": reconciliation})
        except Exception as exc:
            reconciliation = {"status": "blocked", "error": str(exc)}
            blockers.append({"code": "nautilus_runtime_validation_failed", "detail": reconciliation})
        return {
            "status": "ready_for_rollback" if not blockers else "blocked",
            "blockers": blockers,
            "runtime": runtime,
            "reconciliation": reconciliation,
        }

    def _write_cutover_files(self, runtime_path: str) -> None:
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        settings = dict(config.get("execution_engine") or {})
        settings["authoritative"] = "nautilus_paper"
        settings["real_money_eligible"] = False
        config["execution_engine"] = settings
        self._atomic_write(self.config_path, (json.dumps(config, indent=2, ensure_ascii=False) + "\n").encode())
        for path in self._service_paths():
            with path.open("rb") as handle:
                payload = plistlib.load(handle)
            env = dict(payload.get("EnvironmentVariables") or {})
            env["TRADING_ORCHESTRATOR_NAUTILUS_PAPER_SWITCH_APPROVED"] = "1"
            env["TRADING_ORCHESTRATOR_NAUTILUS_PYTHON"] = runtime_path
            payload["EnvironmentVariables"] = env
            self._atomic_write(path, plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False))

    def _targets(self) -> tuple[Path, ...]:
        return (self.config_path, *self._service_paths())

    def _service_paths(self) -> tuple[Path, ...]:
        return tuple(self.launch_agents_dir / f"{label}.plist" for label in SERVICE_LABELS)

    def _backup_targets(self, targets: tuple[Path, ...], backup_dir: Path) -> list[dict[str, Any]]:
        backup_dir.mkdir(parents=True, exist_ok=False)
        rows: list[dict[str, Any]] = []
        for index, path in enumerate(targets):
            backup = backup_dir / f"{index:02d}-{path.name}"
            shutil.copy2(path, backup)
            rows.append({
                "original_path": str(path),
                "backup_path": str(backup),
                "sha256": self._sha256(path.read_bytes()),
            })
        return rows

    def _restore_backups(self, backups: list[dict[str, Any]]) -> None:
        for row in backups:
            backup = Path(str(row.get("backup_path") or ""))
            original = Path(str(row.get("original_path") or ""))
            if not backup.exists() or self._sha256(backup.read_bytes()) != row.get("sha256"):
                raise RuntimeError(f"rollback backup is missing or changed: {backup}")
            self._atomic_write(original, backup.read_bytes())

    def _backups_match_targets(self, backups: list[dict[str, Any]]) -> bool:
        expected = {str(path) for path in self._targets()}
        actual = {str(row.get("original_path") or "") for row in backups}
        return expected == actual and all(Path(str(row.get("backup_path") or "")).exists() for row in backups)

    def _default_quiesce(self, labels: tuple[str, ...]) -> list[dict[str, Any]]:
        rows = []
        for label in labels:
            command = ["launchctl", "bootout", f"gui/{os.getuid()}/{label}"]
            completed = subprocess.run(command, capture_output=True, text=True, check=False)
            probe = subprocess.run(
                ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                capture_output=True,
                text=True,
                check=False,
            )
            if probe.returncode == 0:
                raise RuntimeError(f"failed to stop {label}: service remains loaded")
            rows.append({
                "label": label,
                "status": "stopped",
                "returncode": completed.returncode,
                "confirmed_unloaded": True,
            })
        return rows

    def _default_start(self, paths: tuple[Path, ...]) -> list[dict[str, Any]]:
        rows = []
        domain = f"gui/{os.getuid()}"
        for path in paths:
            label = path.stem
            bootstrap = subprocess.run(
                ["launchctl", "bootstrap", domain, str(path)],
                capture_output=True,
                text=True,
                check=False,
            )
            if bootstrap.returncode != 0:
                raise RuntimeError(f"failed to bootstrap {label}: {bootstrap.stderr.strip()}")
            kickstart = subprocess.run(
                ["launchctl", "kickstart", "-k", f"{domain}/{label}"],
                capture_output=True,
                text=True,
                check=False,
            )
            if kickstart.returncode != 0:
                raise RuntimeError(f"failed to kickstart {label}: {kickstart.stderr.strip()}")
            rows.append({"label": label, "status": "started", "returncode": kickstart.returncode})
        return rows

    def _default_engine_validator(self, expected_engine: str) -> dict[str, Any]:
        last_error = ""
        for _ in range(30):
            try:
                request = Request(
                    "http://127.0.0.1:8765/api/strategy-console/current",
                    headers={"Accept": "application/json"},
                )
                with urlopen(request, timeout=2) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                execution = payload.get("production_execution") or {}
                runtime = payload.get("runtime") or {}
                reconciliation = execution.get("reconciliation") or {}
                engine = str(execution.get("engine") or "")
                accepted = sum(row.get("state") == "accepted" for row in execution.get("orders") or [])
                open_positions = sum(row.get("status") == "open" for row in execution.get("positions") or [])
                ok = (
                    engine == expected_engine
                    and runtime.get("desired_state") == "stopped"
                    and runtime.get("actual_state") == "stopped"
                    and accepted == 0
                    and open_positions == 0
                    and reconciliation.get("status") == "ok"
                )
                return {
                    "status": "ok" if ok else "blocked",
                    "engine": engine,
                    "runtime": runtime,
                    "accepted_order_count": accepted,
                    "open_position_count": open_positions,
                    "reconciliation": reconciliation,
                }
            except (OSError, URLError, ValueError, json.JSONDecodeError) as exc:
                last_error = str(exc)
                time.sleep(0.5)
        return {"status": "blocked", "engine": "", "error": last_error or "dashboard validation timed out"}

    def _safe_quiesce(self) -> list[dict[str, Any]]:
        try:
            return self.service_quiescer(SERVICE_LABELS)
        except Exception as exc:
            return [{"status": "failed", "error": str(exc)}]

    def _safe_start(self, paths: tuple[Path, ...]) -> list[dict[str, Any]]:
        try:
            return self.service_starter(paths)
        except Exception as exc:
            return [{"status": "failed", "error": str(exc)}]

    @staticmethod
    def _require_service_success(rows: list[dict[str, Any]], *, expected: str) -> None:
        if not rows or any(row.get("status") != expected for row in rows):
            raise RuntimeError(f"service transition did not reach {expected}")

    @staticmethod
    def _sha256(payload: bytes) -> str:
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _atomic_write(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise

    def _blocked(
        self,
        blocker: str,
        *,
        acknowledgement_ok: bool,
        precheck: dict[str, Any] | None = None,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "schema_version": "dualtrack-nautilus-cutover-apply-v1",
            "status": "blocked",
            "blocker": blocker,
            "detail": detail or {},
            "acknowledgement_ok": acknowledgement_ok,
            "precheck": precheck or {},
            **self._safety(config_write_performed=False),
        }
        write_json(self.output_root / "dualtrack" / "cutover" / "apply_current.json", [payload])
        return payload

    @staticmethod
    def _safety(*, config_write_performed: bool) -> dict[str, Any]:
        return {
            "scope": "paper_only",
            "config_write_performed": config_write_performed,
            "orders_submitted": False,
            "real_money_eligible": False,
        }

    def _write_receipt(self, kind: str, receipt_id: str, payload: dict[str, Any]) -> None:
        root = self.output_root / "dualtrack" / "cutover"
        write_json(root / f"{kind}_current.json", [payload])
        write_json(root / kind / f"{receipt_id}.json", [payload])


def _load_receipt(path: Path) -> dict[str, Any]:
    rows = load_json(path)
    return rows[-1] if rows else {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply or roll back the attended Nautilus paper-engine cutover.")
    parser.add_argument("action", choices=("apply", "rollback"))
    parser.add_argument("--output-root", default="")
    parser.add_argument("--config", default="")
    parser.add_argument("--launch-agents-dir", default="")
    parser.add_argument("--cycle-id", default="")
    parser.add_argument("--acknowledgement", required=True)
    parser.add_argument("--apply-receipt", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    output = Path(args.output_root) if args.output_root else ROOT / str(load_pipeline_config().get("output_root", "outputs"))
    controller = DualTrackNautilusCutoverController(
        output,
        config_path=Path(args.config) if args.config else None,
        launch_agents_dir=Path(args.launch_agents_dir) if args.launch_agents_dir else None,
    )
    if args.action == "apply":
        result = controller.apply(acknowledgement=args.acknowledgement, cycle_id=args.cycle_id or None)
    else:
        if not args.apply_receipt:
            parser.error("rollback requires --apply-receipt")
        result = controller.rollback(
            _load_receipt(Path(args.apply_receipt)),
            acknowledgement=args.acknowledgement,
        )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"dualtrack_nautilus_cutover_apply: action={args.action} status={result['status']}")
    return 0 if result["status"] in {"applied", "rolled_back"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
