from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services.cloud_soak import EXPECTED_TICKS, MONITORED_UNITS, CloudPaperSoak
from services.journal_store import load_json, write_json
from services.scheduler_ownership import SchedulerOwnershipStore


NOW = datetime(2026, 7, 28, 4, 0, tzinfo=timezone.utc)


def _services(active: str = "active") -> dict:
    return {
        unit: {"active": active, "restarts": 0, "result": "success"}
        for unit in MONITORED_UNITS
    }


def _soak(tmp_path: Path, *, now=NOW, services=None) -> CloudPaperSoak:
    output = tmp_path / "outputs"
    store = SchedulerOwnershipStore(output, now=lambda: NOW)
    local = store.initialize_local()
    paused = store.pause(expected_owner_id="local-mac", expected_epoch=local["epoch"])
    store.activate(new_owner_id="cloud-primary", expected_epoch=paused["epoch"])
    return CloudPaperSoak(
        output_root=output,
        backup_root=tmp_path / "backups",
        deployed_sha="a" * 40,
        owner_id="cloud-primary",
        now=lambda: now,
        service_probe=lambda: services or _services(),
        health_provider=lambda _observed: {
            "checks": {"datafeed": {"status": "ready"}}
        },
    )


def test_start_requires_explicit_disabled_mac_scheduler_evidence(tmp_path: Path):
    soak = _soak(tmp_path)

    with pytest.raises(
        ValueError,
        match="cloud_soak_requires_mac_scheduler_disabled_evidence",
    ):
        soak.start(mac_schedulers_disabled=False)


def test_start_writes_running_paper_only_receipt(tmp_path: Path):
    soak = _soak(tmp_path)

    result = soak.start(mac_schedulers_disabled=True)

    assert result["status"] == "running"
    assert result["control_actions_executed"] == 0
    assert result["scheduler_owner"]["active_owner_id"] == "cloud-primary"
    assert result["requirements"]["duration_hours"] == 24
    assert load_json(soak.path)[-1]["soak_id"] == result["soak_id"]


def test_check_passes_complete_24_hour_evidence(tmp_path: Path):
    soak = _soak(tmp_path)
    started = soak.start(mac_schedulers_disabled=True)
    output = soak.output_root
    tick_rows = [
        {
            "event": "live_tick_heartbeat",
            "ts": (NOW + timedelta(minutes=index)).isoformat(),
        }
        for index in range(EXPECTED_TICKS)
    ]
    write_json(output / "dualtrack" / "runner" / "2026-07-28_DAY.json", tick_rows)
    write_json(
        output / "dualtrack" / "daily_reports" / "2026-07-28.json",
        [{"generated_at": (NOW + timedelta(hours=13)).isoformat()}],
    )
    write_json(
        output / "dualtrack" / "daily_self_reviews" / "current.json",
        [{"status": "complete", "generated_at": (NOW + timedelta(hours=14)).isoformat()}],
    )
    write_json(
        soak.backup_root / "current.json",
        [{"status": "pass", "created_at": (NOW + timedelta(hours=14)).isoformat()}],
    )
    write_json(
        output / "deadman_ping" / "current.json",
        [
            {
                "checked_at": (NOW + timedelta(hours=14)).isoformat(),
                "ping": {"delivered": True},
            }
        ],
    )
    soak.now = lambda: NOW + timedelta(hours=24, minutes=1)
    soak.deployed_sha = started["deployed_sha"]
    result = soak.check()

    assert result["status"] == "pass"
    assert all(result["completion_checks"].values())


def test_check_blocks_owner_change_without_failback(tmp_path: Path):
    soak = _soak(tmp_path)
    soak.start(mac_schedulers_disabled=True)
    store = SchedulerOwnershipStore(soak.output_root, now=lambda: NOW)
    current = store.current()
    store.pause(
        expected_owner_id="cloud-primary",
        expected_epoch=current["epoch"],
    )

    result = soak.check()

    assert result["status"] == "blocked"
    assert result["blockers"] == ["scheduler_ownership_changed"]
    assert result["control_actions_executed"] == 0
