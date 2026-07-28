from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from services.cloud_cutover_state import CloudCutoverStatePackage
from services.journal_store import load_json, write_json
from services.scheduler_ownership import SchedulerOwnershipStore


def _package(tmp_path: Path) -> CloudCutoverStatePackage:
    output = tmp_path / "source-outputs"
    write_json(
        output / "dualtrack" / "strategy_control" / "runtime.json",
        [{"actual_state": "stopped", "accepted_order_count": 0}],
    )
    SchedulerOwnershipStore(
        output,
        now=lambda: datetime(2026, 7, 28, tzinfo=timezone.utc),
    ).initialize_local(owner_id="local-mac")
    return CloudCutoverStatePackage(
        output_root=output,
        backup_root=tmp_path / "backups",
        deployed_sha="a" * 40,
        now=lambda: datetime(2026, 7, 28, 1, 2, 3, tzinfo=timezone.utc),
    )


def test_minimal_cutover_package_restores_verified_state_without_scheduler(
    tmp_path: Path,
) -> None:
    package = _package(tmp_path)
    created = package.create()
    destination = tmp_path / "cloud-outputs"

    restored = package.restore(
        package_id=created["package_id"],
        destination_output_root=destination,
    )

    assert created["status"] == "pass"
    assert created["secret_environment_included"] is False
    assert restored["status"] == "pass"
    assert restored["scheduler_activated"] is False
    assert restored["strategy_control_actions"] == 0
    assert load_json(
        destination / "dualtrack" / "strategy_control" / "runtime.json"
    )[-1]["actual_state"] == "stopped"
    assert load_json(
        destination / "cloud" / "scheduler_ownership" / "current.json"
    )[-1]["active_owner_id"] == "local-mac"


def test_restore_refuses_existing_authoritative_state(tmp_path: Path) -> None:
    package = _package(tmp_path)
    created = package.create()
    destination = tmp_path / "cloud-outputs"
    write_json(destination / "dualtrack" / "existing.json", [{"x": 1}])

    with pytest.raises(ValueError, match="destination_exists"):
        package.restore(
            package_id=created["package_id"],
            destination_output_root=destination,
        )


def test_create_refuses_symlinked_state(tmp_path: Path) -> None:
    package = _package(tmp_path)
    target = tmp_path / "outside.json"
    target.write_text("[]\n", encoding="utf-8")
    link = package.output_root / "dualtrack" / "linked.json"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="symlink_forbidden"):
        package.create()
