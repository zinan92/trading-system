"""Verified minimal state package for the Paper scheduler ownership cutover."""

from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from services.journal_store import load_json, write_json


STATE_PATHS = (
    Path("dualtrack"),
    Path("cloud/scheduler_ownership"),
)


class CloudCutoverStatePackage:
    def __init__(
        self,
        *,
        output_root: Path,
        backup_root: Path,
        deployed_sha: str,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.output_root = Path(output_root).resolve()
        self.backup_root = Path(backup_root).resolve()
        self.deployed_sha = str(deployed_sha).strip().lower()
        self.now = now or (lambda: datetime.now(timezone.utc))

    def create(self) -> dict[str, Any]:
        ownership = _latest(
            self.output_root / "cloud" / "scheduler_ownership" / "current.json"
        )
        if ownership.get("status") not in {"active", "paused"}:
            raise ValueError("cutover_state_ownership_missing")
        files = self._source_files()
        observed = self.now().astimezone(timezone.utc).replace(microsecond=0)
        package_id = (
            f"cutover-state-{observed.strftime('%Y%m%dT%H%M%SZ')}-"
            f"{uuid.uuid4().hex[:8]}"
        )
        self.backup_root.mkdir(parents=True, exist_ok=True)
        package_root = self.backup_root / package_id
        package_root.mkdir()
        archive = package_root / "state.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            for relative, source in files:
                bundle.add(source, arcname=relative.as_posix(), recursive=False)
        file_rows = [
            {
                "path": relative.as_posix(),
                "size": source.stat().st_size,
                "sha256": _hash_file(source),
            }
            for relative, source in files
        ]
        manifest = {
            "schema_version": "cloud-cutover-state-package-v1",
            "status": "pass",
            "paper_only": True,
            "package_id": package_id,
            "created_at": observed.isoformat(),
            "deployed_sha": self.deployed_sha or None,
            "state_paths": [path.as_posix() for path in STATE_PATHS],
            "ownership_status": ownership.get("status"),
            "ownership_epoch": ownership.get("epoch"),
            "active_owner_id": ownership.get("active_owner_id"),
            "file_count": len(file_rows),
            "total_bytes": sum(row["size"] for row in file_rows),
            "files": file_rows,
            "archive_sha256": _hash_file(archive),
            "secret_environment_included": False,
            "strategy_control_actions": 0,
        }
        manifest["manifest_hash"] = _hash_json(manifest)
        write_json(package_root / "manifest.json", [manifest])
        receipt = {
            "status": "pass",
            "package_id": package_id,
            "backup_id": package_id,
            "created_at": manifest["created_at"],
            "path": str(package_root),
            "archive_path": str(archive),
            "archive_sha256": manifest["archive_sha256"],
            "manifest_hash": manifest["manifest_hash"],
            "ownership_status": manifest["ownership_status"],
            "ownership_epoch": manifest["ownership_epoch"],
            "file_count": manifest["file_count"],
            "total_bytes": manifest["total_bytes"],
            "secret_environment_included": False,
        }
        write_json(self.backup_root / "current.json", [receipt])
        return receipt

    def restore(self, *, package_id: str, destination_output_root: Path) -> dict[str, Any]:
        package_root = self._package_root(package_id)
        manifest = _latest(package_root / "manifest.json")
        expected_hash = str(manifest.get("manifest_hash") or "")
        candidate = dict(manifest)
        candidate.pop("manifest_hash", None)
        if not expected_hash or expected_hash != _hash_json(candidate):
            raise ValueError("cutover_state_manifest_hash_invalid")
        archive = package_root / "state.tar.gz"
        if str(manifest.get("archive_sha256") or "") != _hash_file(archive):
            raise ValueError("cutover_state_archive_hash_invalid")
        destination = Path(destination_output_root).resolve()
        for relative in STATE_PATHS:
            if (destination / relative).exists():
                raise ValueError(f"cutover_state_destination_exists:{relative}")
        staging = destination.parent / f".cutover-restore-{package_id}"
        if staging.exists():
            raise ValueError("cutover_state_restore_staging_exists")
        staging.mkdir(parents=True)
        try:
            with tarfile.open(archive, "r:gz") as bundle:
                members = bundle.getmembers()
                if any(member.issym() or member.islnk() for member in members):
                    raise ValueError("cutover_state_archive_link_forbidden")
                bundle.extractall(staging, filter="data")
            observed = self._restored_files(staging)
            expected = {
                row["path"]: (int(row["size"]), str(row["sha256"]))
                for row in manifest.get("files") or []
            }
            if observed != expected:
                raise ValueError("cutover_state_restored_hash_mismatch")
            for relative in STATE_PATHS:
                source = staging / relative
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(target))
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        receipt = {
            "schema_version": "cloud-cutover-state-restore-v1",
            "status": "pass",
            "paper_only": True,
            "package_id": package_id,
            "manifest_hash": expected_hash,
            "ownership_status": manifest.get("ownership_status"),
            "ownership_epoch": manifest.get("ownership_epoch"),
            "restored_at": self.now()
            .astimezone(timezone.utc)
            .replace(microsecond=0)
            .isoformat(),
            "destination_output_root": str(destination),
            "file_count": manifest.get("file_count"),
            "scheduler_activated": False,
            "strategy_control_actions": 0,
        }
        write_json(destination / "cloud" / "restore" / "cutover_current.json", [receipt])
        return receipt

    def _source_files(self) -> list[tuple[Path, Path]]:
        files: list[tuple[Path, Path]] = []
        for state_path in STATE_PATHS:
            root = self.output_root / state_path
            if not root.is_dir():
                raise FileNotFoundError(f"cutover_state_path_missing:{state_path}")
            for source in sorted(root.rglob("*")):
                if source.is_symlink():
                    raise ValueError(f"cutover_state_symlink_forbidden:{source}")
                if source.is_file() and not source.name.endswith((".lock", ".tmp")):
                    files.append((source.relative_to(self.output_root), source))
        if not files:
            raise ValueError("cutover_state_empty")
        return files

    def _package_root(self, package_id: str) -> Path:
        name = str(package_id)
        if not name.startswith("cutover-state-") or "/" in name or ".." in name:
            raise ValueError("cutover_state_package_id_invalid")
        path = (self.backup_root / name).resolve()
        if path.parent != self.backup_root or not path.is_dir():
            raise ValueError("cutover_state_package_missing")
        return path

    @staticmethod
    def _restored_files(root: Path) -> dict[str, tuple[int, str]]:
        rows: dict[str, tuple[int, str]] = {}
        for source in sorted(root.rglob("*")):
            if source.is_file():
                rows[source.relative_to(root).as_posix()] = (
                    source.stat().st_size,
                    _hash_file(source),
                )
        return rows


def _latest(path: Path) -> dict[str, Any]:
    rows = load_json(path)
    return dict(rows[-1]) if rows and isinstance(rows[-1], dict) else {}


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_json(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
