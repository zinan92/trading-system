from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from services.cloud_cutover import CloudDeployManifest, CloudPaperCutover
from services.journal_store import load_json, write_json
from services.scheduler_ownership import SchedulerOwnershipStore


SHA = "a" * 40
DATAFEED_SHA = "b" * 40


class FakePorts:
    def __init__(self, *, fail_stage: str = "") -> None:
        self.calls: list[str] = []
        self.fail_stage = fail_stage
        self.cloud_epoch = 0

    def _pass(self, stage: str, extra=None):
        self.calls.append(stage)
        if self.fail_stage == stage:
            return {"status": "blocked"}
        return {"status": "pass", **(extra or {})}

    def disable_local_tick(self):
        return self._pass("disable_local_tick")

    def disable_cloud_tick(self):
        return self._pass("disable_cloud_tick")

    def transfer_paused_state(self, backup):
        return self._pass(
            "transfer_paused_state",
            {"manifest_hash": backup["manifest_hash"]},
        )

    def activate_cloud_owner(self, *, expected_epoch, owner_id):
        self.calls.append("activate_cloud_owner")
        if self.fail_stage == "activate_cloud_owner":
            return {"status": "blocked"}
        self.cloud_epoch = expected_epoch + 1
        return {
            "status": "active",
            "active_owner_id": owner_id,
            "epoch": self.cloud_epoch,
        }

    def enable_cloud_tick(self):
        return self._pass("enable_cloud_tick")

    def cloud_health(self):
        self.calls.append("cloud_health")
        return {
            "status": "blocked" if self.fail_stage == "cloud_health" else "healthy"
        }

    def pause_cloud_owner(self, *, expected_epoch, owner_id):
        self.calls.append("pause_cloud_owner")
        if self.fail_stage == "pause_cloud_owner":
            return {"status": "blocked"}
        self.cloud_epoch = expected_epoch + 1
        return {
            "status": "paused",
            "active_owner_id": None,
            "previous_owner_id": owner_id,
            "epoch": self.cloud_epoch,
            "dual_owner_allowed": False,
        }

    def transfer_cloud_paused_state(self):
        self.calls.append("transfer_cloud_paused_state")
        return {
            "status": "paused",
            "active_owner_id": None,
            "previous_owner_id": "cloud-primary",
            "epoch": self.cloud_epoch,
            "action": "pause",
            "dual_owner_allowed": False,
        }

    def enable_local_tick(self):
        return self._pass("enable_local_tick")


def _fixture(tmp_path: Path, *, fail_stage: str = ""):
    output = tmp_path / "outputs"
    backup_root = tmp_path / "backups"
    write_json(
        output / "dualtrack" / "strategy_control" / "runtime.json",
        [
            {
                "cycle_id": "2026-07-28_DAY",
                "actual_state": "stopped",
                "accepted_order_count_known": True,
                "accepted_order_count": 0,
            }
        ],
    )
    write_json(
        output
        / "dualtrack"
        / "nautilus_authoritative"
        / "snapshots"
        / "2026-07-28_DAY.json",
        [
            {
                "positions": [
                    {"status": "closed", "remaining_units": 0.0},
                ],
                "reconciliation": {"status": "ok"},
            }
        ],
    )
    SchedulerOwnershipStore(
        output,
        now=lambda: datetime(2026, 7, 28, tzinfo=timezone.utc),
    ).initialize_local(owner_id="local-mac")
    write_json(
        backup_root / "current.json",
        [
            {
                "status": "pass",
                "manifest_hash": "precutover-manifest",
                "created_at": "2026-07-28T00:00:00+00:00",
            }
        ],
    )
    ports = FakePorts(fail_stage=fail_stage)
    backup_calls = []

    def backup_factory():
        backup_calls.append("backup")
        return {"status": "pass", "manifest_hash": "paused-manifest"}

    controller = CloudPaperCutover(
        local_output_root=output,
        local_backup_root=backup_root,
        local_owner_id="local-mac",
        cloud_owner_id="cloud-primary",
        deployed_sha=SHA,
        backup_factory=backup_factory,
        ports=ports,
        now=lambda: datetime(2026, 7, 28, 1, 0, tzinfo=timezone.utc),
    )
    preflight = {
        "status": "pass",
        "paper_only": True,
        "control_actions_executed": 0,
        "checks": [{"id": "source_attestation", "source_sha": SHA}],
    }
    tick = {"enabled": False, "active": False}
    return controller, ports, backup_calls, preflight, tick


def test_source_bound_deploy_manifest_contains_no_secret_or_scheduler_action():
    result = CloudDeployManifest(
        trading_system_sha=SHA,
        datafeed_sha=DATAFEED_SHA,
        hostname="goldbot.example.com",
        region="ap-southeast-1",
    ).to_dict()

    assert result["scheduler_activated"] is False
    assert result["strategy_control_actions"] == 0
    assert result["secret_values_included"] is False
    assert all("checkout --detach" in " ".join(row) for row in result["commands"] if "checkout" in row)


def test_precheck_requires_stopped_flat_reconciled_backup_source_and_disabled_cloud_tick(
    tmp_path: Path,
):
    controller, _ports, _backup, preflight, tick = _fixture(tmp_path)

    ready = controller.precheck(cloud_preflight=preflight, cloud_tick=tick)
    blocked = controller.precheck(
        cloud_preflight={**preflight, "status": "blocked"},
        cloud_tick={"enabled": True, "active": True},
    )

    assert ready["status"] == "pass"
    assert blocked["status"] == "blocked"
    assert {"cloud_preflight_pass", "cloud_tick_disabled"} <= set(blocked["blockers"])
    assert blocked["strategy_control_actions"] == 0


def test_dry_run_performs_no_service_owner_backup_or_control_action(tmp_path: Path):
    controller, ports, backup, preflight, tick = _fixture(tmp_path)

    result = controller.cutover(
        cloud_preflight=preflight,
        cloud_tick=tick,
        dry_run=True,
    )

    assert result["status"] == "dry_run"
    assert result["scheduler_activated"] is False
    assert result["strategy_control_actions"] == 0
    assert ports.calls == []
    assert backup == []
    assert SchedulerOwnershipStore(controller.local_output_root).current()["epoch"] == 1


def test_cutover_passes_only_after_paused_state_transfer_and_healthy_cloud(
    tmp_path: Path,
):
    controller, ports, backup, preflight, tick = _fixture(tmp_path)

    result = controller.cutover(
        cloud_preflight=preflight,
        cloud_tick=tick,
        dry_run=False,
    )

    assert result["status"] == "pass"
    assert result["active_owner_id"] == "cloud-primary"
    assert result["ownership_epoch"] == 3
    assert backup == ["backup"]
    assert ports.calls == [
        "disable_local_tick",
        "transfer_paused_state",
        "activate_cloud_owner",
        "enable_cloud_tick",
        "cloud_health",
    ]
    local = SchedulerOwnershipStore(controller.local_output_root).current()
    assert local["status"] == "paused"
    assert local["epoch"] == 2


def test_fault_after_pause_leaves_both_schedulers_disabled(tmp_path: Path):
    controller, ports, _backup, preflight, tick = _fixture(
        tmp_path,
        fail_stage="transfer_paused_state",
    )

    result = controller.cutover(
        cloud_preflight=preflight,
        cloud_tick=tick,
        dry_run=False,
    )

    assert result["status"] == "blocked"
    assert result["both_schedulers_disabled"] is True
    assert result["local_owner_paused"] is True
    assert "enable_cloud_tick" not in ports.calls
    assert ports.calls[-1] == "disable_cloud_tick"


def test_repeated_completed_cutover_is_idempotent_without_new_mutation(
    tmp_path: Path,
):
    controller, ports, backup, preflight, tick = _fixture(tmp_path)
    first = controller.cutover(
        cloud_preflight=preflight,
        cloud_tick=tick,
        dry_run=False,
    )
    first_calls = list(ports.calls)

    second = controller.cutover(
        cloud_preflight={"status": "blocked"},
        cloud_tick={"enabled": True, "active": True},
        dry_run=False,
    )

    assert first["status"] == "pass"
    assert second["idempotent"] is True
    assert ports.calls == first_calls
    assert backup == ["backup"]


def test_source_mismatch_blocks_before_any_mutation(tmp_path: Path):
    controller, ports, backup, preflight, tick = _fixture(tmp_path)
    preflight["checks"][0]["source_sha"] = "c" * 40

    result = controller.cutover(
        cloud_preflight=preflight,
        cloud_tick=tick,
        dry_run=False,
    )

    assert result["status"] == "blocked"
    assert "source_sha_match" in result["blockers"]
    assert ports.calls == []
    assert backup == []


def test_rollback_preserves_monotonic_epoch_and_never_dual_owns(tmp_path: Path):
    controller, ports, _backup, _preflight, _tick = _fixture(tmp_path)
    store = SchedulerOwnershipStore(controller.local_output_root)
    store.pause(expected_owner_id="local-mac", expected_epoch=1)
    ports.cloud_epoch = 3

    result = controller.rollback(
        cloud_owner={
            "status": "active",
            "active_owner_id": "cloud-primary",
            "epoch": 3,
        },
        dry_run=False,
    )

    assert result["status"] == "pass"
    assert result["active_owner_id"] == "local-mac"
    assert result["ownership_epoch"] == 5
    assert ports.calls == [
        "disable_cloud_tick",
        "pause_cloud_owner",
        "transfer_cloud_paused_state",
        "enable_local_tick",
    ]
    history = load_json(store.path)
    assert [row["status"] for row in history[-3:]] == ["paused", "paused", "active"]
    assert all(
        row.get("active_owner_id") is None or isinstance(row.get("active_owner_id"), str)
        for row in history
    )
    assert all(row.get("dual_owner_allowed") is False for row in history)
