"""Source-bound, fail-disabled Cloud Paper scheduler cutover."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from services.journal_store import load_json, write_json
from services.scheduler_ownership import SchedulerOwnershipStore


def _latest(path: Path) -> dict[str, Any]:
    try:
        rows = load_json(path)
    except (OSError, ValueError):
        return {}
    return dict(rows[-1]) if rows and isinstance(rows[-1], dict) else {}


def _hash_json(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _nonzero(value: Any) -> bool:
    try:
        return abs(float(value or 0.0)) > 0
    except (TypeError, ValueError):
        return True


class CloudCutoverPorts(Protocol):
    """Remote/local service mutations; implementations must not control strategy."""

    def disable_local_tick(self) -> dict[str, Any]: ...
    def disable_cloud_tick(self) -> dict[str, Any]: ...
    def transfer_paused_state(self, backup: dict[str, Any]) -> dict[str, Any]: ...
    def activate_cloud_owner(self, *, expected_epoch: int, owner_id: str) -> dict[str, Any]: ...
    def enable_cloud_tick(self) -> dict[str, Any]: ...
    def cloud_health(self) -> dict[str, Any]: ...
    def pause_cloud_owner(self, *, expected_epoch: int, owner_id: str) -> dict[str, Any]: ...
    def transfer_cloud_paused_state(self) -> dict[str, Any]: ...
    def enable_local_tick(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class CloudDeployManifest:
    trading_system_sha: str
    datafeed_sha: str
    hostname: str
    region: str

    def to_dict(self) -> dict[str, Any]:
        for label, sha in (
            ("trading_system_sha", self.trading_system_sha),
            ("datafeed_sha", self.datafeed_sha),
        ):
            if len(sha) != 40 or any(char not in "0123456789abcdef" for char in sha.lower()):
                raise ValueError(f"{label}_invalid")
        if not self.hostname or any(char.isspace() for char in self.hostname):
            raise ValueError("hostname_invalid")
        if not self.region or any(char.isspace() for char in self.region):
            raise ValueError("region_invalid")
        commands = [
            ["git", "clone", "https://github.com/zinan92/trading-system.git", "/opt/gridmind/src/trading-system"],
            ["git", "-C", "/opt/gridmind/src/trading-system", "checkout", "--detach", self.trading_system_sha],
            ["git", "clone", "https://github.com/zinan92/datafeed.git", "/opt/gridmind/src/datafeed"],
            ["git", "-C", "/opt/gridmind/src/datafeed", "checkout", "--detach", self.datafeed_sha],
            [
                "/opt/gridmind/venvs/app/bin/python",
                "-m",
                "pipelines.cloud_paper_preflight",
                "--json",
            ],
        ]
        return {
            "schema_version": "cloud-paper-deploy-manifest-v1",
            "paper_only": True,
            "trading_system_sha": self.trading_system_sha.lower(),
            "datafeed_sha": self.datafeed_sha.lower(),
            "hostname": self.hostname,
            "region": self.region,
            "secret_source": "/etc/gridmind/paper.env",
            "secret_values_included": False,
            "commands": commands,
            "scheduler_activated": False,
            "strategy_control_actions": 0,
        }


class CloudPaperCutover:
    def __init__(
        self,
        *,
        local_output_root: Path,
        local_backup_root: Path,
        local_owner_id: str,
        cloud_owner_id: str,
        deployed_sha: str,
        backup_factory: Callable[[], dict[str, Any]],
        ports: CloudCutoverPorts,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.local_output_root = Path(local_output_root)
        self.local_backup_root = Path(local_backup_root)
        self.local_owner_id = str(local_owner_id)
        self.cloud_owner_id = str(cloud_owner_id)
        self.deployed_sha = str(deployed_sha).lower()
        self.backup_factory = backup_factory
        self.ports = ports
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.receipt_root = self.local_output_root / "cloud" / "cutover"

    def precheck(
        self,
        *,
        cloud_preflight: dict[str, Any],
        cloud_tick: dict[str, Any],
    ) -> dict[str, Any]:
        runtime = _latest(
            self.local_output_root
            / "dualtrack"
            / "strategy_control"
            / "runtime.json"
        )
        cycle_id = str(runtime.get("cycle_id") or "")
        execution = _latest(
            self.local_output_root
            / "dualtrack"
            / "nautilus_authoritative"
            / "snapshots"
            / f"{cycle_id}.json"
        ) if cycle_id else {}
        reconciliation = (
            execution.get("reconciliation")
            if isinstance(execution.get("reconciliation"), dict)
            else {}
        )
        open_positions = [
            row
            for row in execution.get("positions") or []
            if isinstance(row, dict)
            and (
                str(row.get("status") or "").lower() == "open"
                or _nonzero(row.get("remaining_units"))
            )
        ]
        ownership = SchedulerOwnershipStore(self.local_output_root).current()
        backup = _latest(self.local_backup_root / "current.json")
        source_check = next(
            (
                row
                for row in cloud_preflight.get("checks") or []
                if isinstance(row, dict) and row.get("id") == "source_attestation"
            ),
            {},
        )
        checks = {
            "paper_stopped": runtime.get("actual_state") == "stopped",
            "accepted_orders_zero": (
                runtime.get("accepted_order_count_known") is True
                and int(runtime.get("accepted_order_count") or 0) == 0
            ),
            "positions_zero": len(open_positions) == 0,
            "reconciliation_pass": str(reconciliation.get("status") or "") in {"ok", "pass"},
            "verified_backup": (
                backup.get("status") == "pass"
                and bool(backup.get("manifest_hash"))
                and bool(backup.get("created_at"))
            ),
            "local_owner_active": (
                ownership.get("status") == "active"
                and ownership.get("active_owner_id") == self.local_owner_id
            ),
            "cloud_preflight_pass": (
                cloud_preflight.get("status") == "pass"
                and cloud_preflight.get("paper_only") is True
                and int(cloud_preflight.get("control_actions_executed") or 0) == 0
            ),
            "source_sha_match": (
                len(self.deployed_sha) == 40
                and str(source_check.get("source_sha") or "").lower() == self.deployed_sha
            ),
            "cloud_tick_disabled": (
                cloud_tick.get("enabled") is False
                and cloud_tick.get("active") is False
            ),
        }
        blockers = [name for name, passed in checks.items() if not passed]
        payload = {
            "schema_version": "cloud-paper-cutover-precheck-v1",
            "status": "pass" if not blockers else "blocked",
            "paper_only": True,
            "checked_at": self._timestamp(),
            "checks": checks,
            "blockers": blockers,
            "cycle_id": cycle_id or None,
            "ownership_epoch": ownership.get("epoch"),
            "source_sha": self.deployed_sha or None,
            "strategy_control_actions": 0,
            "secrets_included": False,
        }
        write_json(self.receipt_root / "precheck_current.json", [payload])
        return payload

    def cutover(
        self,
        *,
        cloud_preflight: dict[str, Any],
        cloud_tick: dict[str, Any],
        dry_run: bool = True,
    ) -> dict[str, Any]:
        completed_receipt = _latest(self.receipt_root / "cutover_current.json")
        if (
            completed_receipt.get("status") == "pass"
            and completed_receipt.get("active_owner_id") == self.cloud_owner_id
            and completed_receipt.get("source_sha") == self.deployed_sha
        ):
            return {**completed_receipt, "idempotent": True}
        precheck = self.precheck(
            cloud_preflight=cloud_preflight,
            cloud_tick=cloud_tick,
        )
        plan = [
            "disable_local_tick",
            "pause_local_owner",
            "create_paused_state_backup",
            "transfer_and_verify_paused_state",
            "activate_cloud_owner",
            "enable_cloud_tick",
            "verify_cloud_health",
        ]
        if precheck["status"] != "pass":
            return self._write(
                "cutover",
                {
                    "status": "blocked",
                    "phase": "precheck",
                    "blockers": precheck["blockers"],
                    "planned_steps": plan,
                    "dry_run": dry_run,
                },
            )
        if dry_run:
            return self._write(
                "cutover",
                {
                    "status": "dry_run",
                    "phase": "planned",
                    "blockers": [],
                    "planned_steps": plan,
                    "dry_run": True,
                    "scheduler_activated": False,
                },
            )
        epoch = int(precheck["ownership_epoch"])
        completed: list[str] = []
        paused = False
        local_disabled = False
        try:
            self._require_pass(self.ports.disable_local_tick(), "disable_local_tick")
            local_disabled = True
            completed.append("disable_local_tick")
            pause = SchedulerOwnershipStore(self.local_output_root).pause(
                expected_owner_id=self.local_owner_id,
                expected_epoch=epoch,
            )
            epoch = int(pause["epoch"])
            paused = True
            completed.append("pause_local_owner")
            backup = self.backup_factory()
            self._require_pass(backup, "create_paused_state_backup")
            completed.append("create_paused_state_backup")
            transferred = self.ports.transfer_paused_state(backup)
            self._require_pass(transferred, "transfer_and_verify_paused_state")
            if transferred.get("manifest_hash") != backup.get("manifest_hash"):
                raise RuntimeError("transferred_state_manifest_hash_mismatch")
            completed.append("transfer_and_verify_paused_state")
            cloud_owner = self.ports.activate_cloud_owner(
                expected_epoch=epoch,
                owner_id=self.cloud_owner_id,
            )
            self._require_owner(
                cloud_owner,
                owner_id=self.cloud_owner_id,
                expected_epoch=epoch + 1,
            )
            completed.append("activate_cloud_owner")
            self._require_pass(self.ports.enable_cloud_tick(), "enable_cloud_tick")
            completed.append("enable_cloud_tick")
            health = self.ports.cloud_health()
            if health.get("status") != "healthy":
                raise RuntimeError("cloud_health_not_healthy_after_activation")
            completed.append("verify_cloud_health")
            return self._write(
                "cutover",
                {
                    "status": "pass",
                    "phase": "complete",
                    "completed_steps": completed,
                    "active_owner_id": self.cloud_owner_id,
                    "ownership_epoch": epoch + 1,
                    "source_sha": self.deployed_sha,
                    "backup_manifest_hash": backup.get("manifest_hash"),
                    "dry_run": False,
                },
            )
        except Exception as exc:  # noqa: BLE001 - every failure must fail disabled.
            cloud_disabled = self._safe_disable_cloud()
            return self._write(
                "cutover",
                {
                    "status": "blocked",
                    "phase": completed[-1] if completed else "disable_local_tick",
                    "completed_steps": completed,
                    "error_code": str(exc),
                    "local_owner_paused": paused,
                    "local_tick_disabled": local_disabled,
                    "cloud_tick_disabled": cloud_disabled,
                    "both_schedulers_disabled": local_disabled and paused and cloud_disabled,
                    "next_action": (
                        "Keep both tick schedulers disabled; inspect this receipt "
                        "and use the explicit rollback path."
                    ),
                    "dry_run": False,
                },
            )

    def rollback(
        self,
        *,
        cloud_owner: dict[str, Any],
        dry_run: bool = True,
    ) -> dict[str, Any]:
        completed_receipt = _latest(self.receipt_root / "rollback_current.json")
        if (
            completed_receipt.get("status") == "pass"
            and completed_receipt.get("active_owner_id") == self.local_owner_id
        ):
            return {**completed_receipt, "idempotent": True}
        plan = [
            "disable_cloud_tick",
            "pause_cloud_owner",
            "transfer_cloud_paused_state",
            "import_paused_owner_state",
            "activate_local_owner",
            "enable_local_tick",
        ]
        if (
            cloud_owner.get("status") != "active"
            or cloud_owner.get("active_owner_id") != self.cloud_owner_id
        ):
            return self._write(
                "rollback",
                {
                    "status": "blocked",
                    "phase": "precheck",
                    "blockers": ["cloud_owner_not_active"],
                    "planned_steps": plan,
                    "dry_run": dry_run,
                },
            )
        if dry_run:
            return self._write(
                "rollback",
                {
                    "status": "dry_run",
                    "phase": "planned",
                    "planned_steps": plan,
                    "dry_run": True,
                },
            )
        completed: list[str] = []
        try:
            self._require_pass(self.ports.disable_cloud_tick(), "disable_cloud_tick")
            completed.append("disable_cloud_tick")
            paused = self.ports.pause_cloud_owner(
                expected_epoch=int(cloud_owner["epoch"]),
                owner_id=self.cloud_owner_id,
            )
            self._require_paused(paused, int(cloud_owner["epoch"]) + 1)
            completed.append("pause_cloud_owner")
            transferred = self.ports.transfer_cloud_paused_state()
            self._require_paused(transferred, int(paused["epoch"]))
            completed.append("transfer_cloud_paused_state")
            self._import_paused_owner(transferred)
            completed.append("import_paused_owner_state")
            local = SchedulerOwnershipStore(self.local_output_root).activate(
                new_owner_id=self.local_owner_id,
                expected_epoch=int(transferred["epoch"]),
            )
            completed.append("activate_local_owner")
            self._require_pass(self.ports.enable_local_tick(), "enable_local_tick")
            completed.append("enable_local_tick")
            return self._write(
                "rollback",
                {
                    "status": "pass",
                    "phase": "complete",
                    "completed_steps": completed,
                    "active_owner_id": local["active_owner_id"],
                    "ownership_epoch": local["epoch"],
                    "dry_run": False,
                },
            )
        except Exception as exc:  # noqa: BLE001 - remain disabled on uncertainty.
            return self._write(
                "rollback",
                {
                    "status": "blocked",
                    "phase": completed[-1] if completed else "disable_cloud_tick",
                    "completed_steps": completed,
                    "error_code": str(exc),
                    "cloud_tick_disabled": True,
                    "next_action": "Keep both schedulers disabled and reconcile owner epochs manually.",
                    "dry_run": False,
                },
            )

    def _import_paused_owner(self, payload: dict[str, Any]) -> None:
        store = SchedulerOwnershipStore(self.local_output_root)
        current = store.current()
        if payload.get("status") != "paused":
            raise RuntimeError("imported_owner_not_paused")
        if payload.get("dual_owner_allowed") is not False:
            raise RuntimeError("imported_owner_allows_dual_owner")
        if int(payload.get("epoch") or 0) <= int(current.get("epoch") or 0):
            raise RuntimeError("imported_owner_epoch_not_monotonic")
        rows = load_json(store.path)
        rows.append(dict(payload))
        write_json(store.path, rows)

    def _safe_disable_cloud(self) -> bool:
        try:
            return self.ports.disable_cloud_tick().get("status") == "pass"
        except Exception:  # noqa: BLE001 - evidence remains false.
            return False

    @staticmethod
    def _require_pass(payload: dict[str, Any], stage: str) -> None:
        if payload.get("status") != "pass":
            raise RuntimeError(f"{stage}_failed")

    @staticmethod
    def _require_owner(
        payload: dict[str, Any],
        *,
        owner_id: str,
        expected_epoch: int,
    ) -> None:
        if (
            payload.get("status") != "active"
            or payload.get("active_owner_id") != owner_id
            or int(payload.get("epoch") or 0) != expected_epoch
        ):
            raise RuntimeError("cloud_owner_activation_not_verified")

    @staticmethod
    def _require_paused(payload: dict[str, Any], expected_epoch: int) -> None:
        if (
            payload.get("status") != "paused"
            or payload.get("active_owner_id") is not None
            or payload.get("dual_owner_allowed") is not False
            or int(payload.get("epoch") or 0) != expected_epoch
        ):
            raise RuntimeError("paused_owner_state_invalid")

    def _write(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        receipt = {
            "schema_version": f"cloud-paper-{action}-v1",
            "paper_only": True,
            "recorded_at": self._timestamp(),
            "strategy_control_actions": 0,
            "live_money_actions": 0,
            "secrets_included": False,
            **payload,
        }
        receipt["receipt_hash"] = _hash_json(receipt)
        write_json(self.receipt_root / f"{action}_current.json", [receipt])
        return receipt

    def _timestamp(self) -> str:
        return self.now().astimezone(timezone.utc).replace(microsecond=0).isoformat()
