from __future__ import annotations

from pathlib import Path

import pytest

from services.scheduler_ownership import (
    LOCAL_OWNER_ID,
    SchedulerOwnershipGuard,
    SchedulerOwnershipStore,
)


def test_scheduler_owner_requires_pause_between_hosts(tmp_path: Path):
    store = SchedulerOwnershipStore(tmp_path)

    local = store.initialize_local()
    paused = store.pause(
        expected_owner_id=LOCAL_OWNER_ID,
        expected_epoch=local["epoch"],
    )
    cloud = store.activate(
        new_owner_id="cloud-primary",
        expected_epoch=paused["epoch"],
    )

    assert local["epoch"] == 1
    assert paused["status"] == "paused"
    assert cloud["epoch"] == 3
    assert cloud["active_owner_id"] == "cloud-primary"
    assert cloud["dual_owner_allowed"] is False
    with pytest.raises(ValueError, match="must_be_paused"):
        store.activate(new_owner_id="other", expected_epoch=cloud["epoch"])


def test_scheduler_owner_explicit_rollback_is_also_paused(tmp_path: Path):
    store = SchedulerOwnershipStore(tmp_path)
    local = store.initialize_local()
    paused = store.pause(expected_owner_id=LOCAL_OWNER_ID, expected_epoch=local["epoch"])
    cloud = store.activate(new_owner_id="cloud-primary", expected_epoch=paused["epoch"])
    rollback_pause = store.pause(
        expected_owner_id="cloud-primary",
        expected_epoch=cloud["epoch"],
    )
    restored = store.activate(
        new_owner_id=LOCAL_OWNER_ID,
        expected_epoch=rollback_pause["epoch"],
    )

    assert restored["active_owner_id"] == LOCAL_OWNER_ID
    assert restored["epoch"] == 5


def test_guard_blocks_local_after_cloud_owner_is_active(tmp_path: Path):
    store = SchedulerOwnershipStore(tmp_path)
    local = store.initialize_local()
    paused = store.pause(expected_owner_id=LOCAL_OWNER_ID, expected_epoch=local["epoch"])
    store.activate(new_owner_id="cloud-primary", expected_epoch=paused["epoch"])

    local_result = SchedulerOwnershipGuard(
        tmp_path,
        runtime_mode="local",
    ).verify()
    cloud_result = SchedulerOwnershipGuard(
        tmp_path,
        runtime_mode="cloud",
        owner_id="cloud-primary",
    ).verify()

    assert local_result["ok"] is False
    assert local_result["blocker"] == "scheduler_owner_id_mismatch"
    assert cloud_result["ok"] is True


def test_cloud_guard_blocks_missing_or_paused_ownership(tmp_path: Path):
    missing = SchedulerOwnershipGuard(
        tmp_path,
        runtime_mode="cloud",
        owner_id="cloud-primary",
    ).verify()
    assert missing["blocker"] == "scheduler_ownership_missing"

    store = SchedulerOwnershipStore(tmp_path)
    local = store.initialize_local()
    store.pause(expected_owner_id=LOCAL_OWNER_ID, expected_epoch=local["epoch"])
    paused = SchedulerOwnershipGuard(
        tmp_path,
        runtime_mode="cloud",
        owner_id="cloud-primary",
    ).verify()
    assert paused["blocker"] == "scheduler_ownership_paused"


def test_local_guard_materializes_formal_local_owner_with_epoch(tmp_path: Path):
    result = SchedulerOwnershipGuard(tmp_path, runtime_mode="local").verify()

    assert result["ok"] is True
    assert result["status"] == "pass"
    assert result["owner_status"] == "local"
    assert result["owner_id"] == LOCAL_OWNER_ID
    assert result["epoch"] == 1
    assert SchedulerOwnershipStore(tmp_path).current()["active_owner_id"] == LOCAL_OWNER_ID
