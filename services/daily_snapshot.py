from __future__ import annotations

import hashlib
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from services.journal_store import write_json


class DailySnapshot:
    def __init__(self, output_root: Path, market_db: Path | None) -> None:
        self.output_root = output_root
        self.market_db = market_db

    def build(self, run_date: str, files: list[dict]) -> dict:
        snapshot_dir = self.output_root / "snapshots"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        snapshot_path = snapshot_dir / f"{run_date}.tar.gz"
        included: list[dict] = []
        with tarfile.open(snapshot_path, "w:gz") as archive:
            if self.market_db is not None and self.market_db.exists():
                archive.add(self.market_db, arcname="data/market_data.db")
                included.append(self._included_record(self.market_db, "data/market_data.db"))
            for item in files:
                if not item.get("exists"):
                    continue
                path = Path(str(item.get("path", "")))
                if not path.exists() or not path.is_file():
                    continue
                arcname = self._arcname(path)
                archive.add(path, arcname=arcname)
                included.append(self._included_record(path, arcname))
        payload = {
            "run_date": run_date,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": "pass" if included and snapshot_path.exists() else "warn",
            "snapshot_path": str(snapshot_path),
            "snapshot_size_bytes": snapshot_path.stat().st_size if snapshot_path.exists() else 0,
            "snapshot_sha256": self._sha256(snapshot_path) if snapshot_path.exists() else "",
            "included_file_count": len(included),
            "included_files": included,
            "restore_hint": f"tar -xzf {snapshot_path} -C /path/to/restore-root",
        }
        write_json(self.output_root / "snapshots" / "current.json", [payload])
        write_json(self.output_root / "snapshots" / f"{run_date}.json", [payload])
        self._write_markdown(run_date, payload)
        return payload

    def _arcname(self, path: Path) -> str:
        try:
            relative = path.relative_to(self.output_root.parent)
            return str(relative)
        except ValueError:
            return path.name

    def _included_record(self, path: Path, arcname: str) -> dict:
        return {
            "path": str(path),
            "arcname": arcname,
            "size_bytes": path.stat().st_size,
            "sha256": self._sha256(path),
        }

    def _sha256(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _write_markdown(self, run_date: str, payload: dict) -> None:
        lines = [
            f"# Daily Snapshot - {run_date}",
            "",
            f"- Status: {payload['status']}",
            f"- Snapshot: `{payload['snapshot_path']}`",
            f"- Size bytes: {payload['snapshot_size_bytes']}",
            f"- SHA256: {payload['snapshot_sha256']}",
            f"- Included files: {payload['included_file_count']}",
            f"- Restore: `{payload['restore_hint']}`",
            "",
            "## Included Files",
        ]
        for item in payload["included_files"]:
            lines.append(f"- `{item['arcname']}` size={item['size_bytes']} sha256={item['sha256'][:12]}")
        path = self.output_root / "snapshots" / f"{run_date}.md"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
