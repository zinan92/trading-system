from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services.cloud_backup import CloudPaperBackup
from services.journal_store import write_json


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 27, 3, 30, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        current = self.value
        self.value += timedelta(minutes=1)
        return current


def make_source(tmp_path: Path) -> tuple[Path, Path, Path]:
    output = tmp_path / "state" / "outputs"
    database = tmp_path / "state" / "datafeed" / "kline.db"
    backup_root = tmp_path / "state" / "backups"
    write_json(
        output / "dualtrack" / "strategy_control" / "runtime.json",
        [{"actual_state": "stopped", "accepted_order_count": 0}],
    )
    write_json(
        output / "cloud" / "scheduler_ownership" / "current.json",
        [{"status": "paused", "active_owner_id": None, "epoch": 2}],
    )
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute("create table candles(ts text primary key, close real)")
        connection.execute("insert into candles values('2026-07-27T00:00:00Z', 4123.0)")
    return output, database, backup_root


def builder(tmp_path: Path, *, clock=None) -> CloudPaperBackup:
    output, database, backup_root = make_source(tmp_path)
    return CloudPaperBackup(
        output_root=output,
        datafeed_db=database,
        backup_root=backup_root,
        deployed_sha="a" * 40,
        now=clock or Clock(),
    )


def test_backup_restore_preserves_every_manifest_hash(tmp_path: Path):
    backup = builder(tmp_path)
    created = backup.create()
    restored_output = tmp_path / "restore" / "outputs"
    restored_db = tmp_path / "restore" / "datafeed" / "kline.db"

    restored = backup.restore(
        backup_id=created["backup_id"],
        destination_output_root=restored_output,
        destination_datafeed_db=restored_db,
    )

    assert created["status"] == "pass"
    assert created["secret_environment_included"] is False
    assert restored["status"] == "pass"
    assert restored["scheduler_activated"] is False
    assert restored["reconciliation_required"] is True
    with sqlite3.connect(restored_db) as connection:
        assert connection.execute("select close from candles").fetchone()[0] == 4123.0
    assert (
        json.loads(
            (
                restored_output
                / "cloud"
                / "scheduler_ownership"
                / "current.json"
            ).read_text()
        )[-1]["status"]
        == "paused"
    )


def test_backup_normalizes_wal_database_to_self_contained_snapshot(tmp_path: Path):
    output, database, backup_root = make_source(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("insert into candles values('2026-07-27T00:01:00Z', 4124.0)")
    backup = CloudPaperBackup(
        output_root=output,
        datafeed_db=database,
        backup_root=backup_root,
        deployed_sha="a" * 40,
        now=Clock(),
    )

    created = backup.create()
    manifest = json.loads(
        (Path(created["path"]) / "manifest.json").read_text(encoding="utf-8")
    )[-1]

    assert "datafeed/kline.db" in {row["path"] for row in manifest["files"]}
    assert not any(
        row["path"].endswith(("-wal", "-shm")) for row in manifest["files"]
    )
    restored = backup.restore(
        backup_id=created["backup_id"],
        destination_output_root=tmp_path / "wal-restore" / "outputs",
        destination_datafeed_db=tmp_path / "wal-restore" / "datafeed" / "kline.db",
    )
    assert restored["status"] == "pass"


def test_corrupt_backup_is_rejected_before_restore(tmp_path: Path):
    backup = builder(tmp_path)
    created = backup.create()
    runtime = (
        Path(created["path"])
        / "outputs"
        / "dualtrack"
        / "strategy_control"
        / "runtime.json"
    )
    runtime.write_text("corrupt", encoding="utf-8")

    with pytest.raises(ValueError, match="backup_file_hash_mismatch"):
        backup.restore(
            backup_id=created["backup_id"],
            destination_output_root=tmp_path / "restore" / "outputs",
            destination_datafeed_db=tmp_path / "restore" / "datafeed" / "kline.db",
        )


def test_restore_refuses_nonempty_destination(tmp_path: Path):
    backup = builder(tmp_path)
    created = backup.create()
    destination = tmp_path / "restore" / "outputs"
    destination.mkdir(parents=True)
    (destination / "existing").write_text("do not overwrite", encoding="utf-8")

    with pytest.raises(ValueError, match="not_empty"):
        backup.restore(
            backup_id=created["backup_id"],
            destination_output_root=destination,
            destination_datafeed_db=tmp_path / "restore" / "datafeed" / "kline.db",
        )


def test_retention_removes_only_verified_backup_directories(tmp_path: Path):
    clock = Clock()
    backup = builder(tmp_path, clock=clock)
    first = backup.create()
    second = backup.create()
    invalid = Path(second["path"]).parent / "backup-invalid"
    invalid.mkdir()

    result = backup.prune(keep=1)

    assert first["backup_id"] in result["removed_backup_ids"]
    assert Path(second["path"]).is_dir()
    assert invalid.is_dir()
    assert result["active_state_changed"] is False
