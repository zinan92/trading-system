"""Verified, non-secret Cloud Paper snapshot and restore."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from services.journal_store import load_json, write_json


class CloudPaperBackup:
    def __init__(
        self,
        *,
        output_root: Path,
        datafeed_db: Path,
        backup_root: Path,
        deployed_sha: str = "",
        encryption_mode: str = "provider_volume_at_rest",
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.output_root = Path(output_root).resolve()
        self.datafeed_db = Path(datafeed_db).resolve()
        self.backup_root = Path(backup_root).resolve()
        self.deployed_sha = str(deployed_sha)
        self.encryption_mode = str(encryption_mode)
        self.now = now or (lambda: datetime.now(timezone.utc))

    def create(self) -> dict[str, Any]:
        if self.backup_root == self.output_root or self.output_root in self.backup_root.parents:
            raise ValueError("backup_root_must_not_be_inside_output_root")
        if not self.datafeed_db.is_file():
            raise FileNotFoundError(f"datafeed SQLite missing: {self.datafeed_db}")
        observed = self.now().astimezone(timezone.utc).replace(microsecond=0)
        backup_id = f"backup-{observed.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
        self.backup_root.mkdir(parents=True, exist_ok=True)
        staging = self.backup_root / f".staging-{backup_id}"
        final = self.backup_root / backup_id
        if staging.exists() or final.exists():
            raise FileExistsError(backup_id)
        staging.mkdir()
        try:
            shutil.copytree(
                self.output_root,
                staging / "outputs",
                copy_function=shutil.copy2,
                ignore=_ignore_transient,
            )
            self._sqlite_backup(self.datafeed_db, staging / "datafeed" / "kline.db")
            files = self._file_manifest(staging)
            manifest = {
                "schema_version": "cloud-paper-backup-v1",
                "backup_id": backup_id,
                "created_at": observed.isoformat(),
                "deployed_sha": self.deployed_sha or None,
                "encryption_mode": self.encryption_mode,
                "secret_environment_included": False,
                "source": {
                    "output_root": str(self.output_root),
                    "datafeed_db": str(self.datafeed_db),
                },
                "files": files,
                "file_count": len(files),
                "total_bytes": sum(row["size"] for row in files),
            }
            manifest["manifest_hash"] = _hash_json(manifest)
            write_json(staging / "manifest.json", [manifest])
            staging.rename(final)
            receipt = {
                "status": "pass",
                "backup_id": backup_id,
                "created_at": observed.isoformat(),
                "path": str(final),
                "manifest_hash": manifest["manifest_hash"],
                "file_count": manifest["file_count"],
                "total_bytes": manifest["total_bytes"],
                "encryption_mode": self.encryption_mode,
                "secret_environment_included": False,
            }
            write_json(self.backup_root / "current.json", [receipt])
            return receipt
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise

    def restore(
        self,
        *,
        backup_id: str,
        destination_output_root: Path,
        destination_datafeed_db: Path,
    ) -> dict[str, Any]:
        backup = self._backup_path(backup_id)
        manifest = self._verified_manifest(backup)
        output_destination = Path(destination_output_root).resolve()
        db_destination = Path(destination_datafeed_db).resolve()
        if output_destination.exists() and any(output_destination.iterdir()):
            raise ValueError("restore_output_destination_not_empty")
        if db_destination.exists():
            raise ValueError("restore_datafeed_destination_exists")
        output_destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(backup / "outputs", output_destination, dirs_exist_ok=True)
        db_destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup / "datafeed" / "kline.db", db_destination)
        self._sqlite_integrity(db_destination)
        restored_files = self._restored_manifest(output_destination, db_destination)
        expected = {
            row["path"]: row["sha256"]
            for row in manifest["files"]
        }
        observed = {row["path"]: row["sha256"] for row in restored_files}
        if expected != observed:
            raise ValueError("restored_file_hash_mismatch")
        receipt = {
            "schema_version": "cloud-paper-restore-v1",
            "status": "pass",
            "backup_id": backup_id,
            "manifest_hash": manifest["manifest_hash"],
            "restored_at": self.now().astimezone(timezone.utc).replace(microsecond=0).isoformat(),
            "destination_output_root": str(output_destination),
            "destination_datafeed_db": str(db_destination),
            "file_count": len(restored_files),
            "reconciliation_required": True,
            "scheduler_activated": False,
        }
        write_json(output_destination / "cloud" / "restore" / "current.json", [receipt])
        return receipt

    def prune(self, *, keep: int) -> dict[str, Any]:
        keep = max(1, int(keep))
        valid: list[tuple[Path, dict[str, Any]]] = []
        for path in sorted(self.backup_root.glob("backup-*")):
            if not path.is_dir():
                continue
            try:
                valid.append((path, self._verified_manifest(path)))
            except (OSError, ValueError):
                continue
        remove = valid[:-keep]
        removed: list[str] = []
        for path, manifest in remove:
            if path.parent != self.backup_root or path.name != manifest.get("backup_id"):
                raise ValueError("backup_prune_target_invalid")
            shutil.rmtree(path)
            removed.append(path.name)
        return {
            "status": "pass",
            "keep": keep,
            "removed_backup_ids": removed,
            "active_state_changed": False,
        }

    def _verified_manifest(self, backup: Path) -> dict[str, Any]:
        rows = load_json(backup / "manifest.json")
        manifest = rows[-1] if rows and isinstance(rows[-1], dict) else {}
        observed_hash = str(manifest.get("manifest_hash") or "")
        candidate = dict(manifest)
        candidate.pop("manifest_hash", None)
        if not observed_hash or observed_hash != _hash_json(candidate):
            raise ValueError("backup_manifest_hash_invalid")
        expected = {row["path"]: row for row in manifest.get("files") or []}
        actual = {row["path"]: row for row in self._file_manifest(backup)}
        actual.pop("manifest.json", None)
        if set(expected) != set(actual):
            raise ValueError("backup_file_set_mismatch")
        for path, row in expected.items():
            if row["sha256"] != actual[path]["sha256"] or row["size"] != actual[path]["size"]:
                raise ValueError(f"backup_file_hash_mismatch:{path}")
        return manifest

    def _backup_path(self, backup_id: str) -> Path:
        name = str(backup_id)
        if not name.startswith("backup-") or "/" in name or ".." in name:
            raise ValueError("backup_id_invalid")
        path = (self.backup_root / name).resolve()
        if path.parent != self.backup_root:
            raise ValueError("backup_id_invalid")
        return path

    @staticmethod
    def _sqlite_backup(source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(source) as source_db, sqlite3.connect(destination) as target_db:
            source_db.backup(target_db)
            target_db.execute("PRAGMA journal_mode=DELETE")
        CloudPaperBackup._sqlite_integrity(destination)

    @staticmethod
    def _sqlite_integrity(path: Path) -> None:
        with sqlite3.connect(path) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise ValueError("sqlite_backup_integrity_failed")

    @staticmethod
    def _file_manifest(root: Path) -> list[dict[str, Any]]:
        rows = []
        for path in sorted(Path(root).rglob("*")):
            if not path.is_file() or path.name == "manifest.json":
                continue
            rows.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "size": path.stat().st_size,
                    "sha256": _hash_file(path),
                }
            )
        return rows

    @staticmethod
    def _restored_manifest(output_root: Path, datafeed_db: Path) -> list[dict[str, Any]]:
        rows = []
        for path in sorted(output_root.rglob("*")):
            if not path.is_file():
                continue
            relative = Path("outputs") / path.relative_to(output_root)
            rows.append(
                {
                    "path": relative.as_posix(),
                    "size": path.stat().st_size,
                    "sha256": _hash_file(path),
                }
            )
        rows.append(
            {
                "path": "datafeed/kline.db",
                "size": datafeed_db.stat().st_size,
                "sha256": _hash_file(datafeed_db),
            }
        )
        return sorted(rows, key=lambda row: row["path"])


def _ignore_transient(_directory: str, names: list[str]) -> set[str]:
    return {
        name
        for name in names
        if name.startswith(".") and (name.endswith(".tmp") or name.endswith(".lock"))
    }


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _hash_json(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
