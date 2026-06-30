from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json


class DataIntegrityCheck:
    def __init__(self, output_root: Path | None = None, market_db: Path | None = None) -> None:
        config = load_pipeline_config()
        self.output_root = output_root or ROOT / config.get("output_root", "outputs")
        self.market_db = market_db or ROOT / config.get("local_market_db", "data/market_data.db")

    def run(self, run_date: str) -> dict:
        checks = [
            self._sqlite_check(),
            self._gold_clean_files_check(run_date),
            self._archive_check(run_date),
            self._snapshot_restore_check(run_date),
        ]
        payload = {
            "run_date": run_date,
            "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": self._rollup(checks),
            "summary": {
                "passed": sum(1 for item in checks if item["status"] == "pass"),
                "warned": sum(1 for item in checks if item["status"] == "warn"),
                "failed": sum(1 for item in checks if item["status"] == "fail"),
            },
            "checks": checks,
            "source_artifacts": {
                "market_db": str(self.market_db),
                "raw_gold": str(self.output_root / "raw_snapshots" / run_date / "GOLD_5m.json"),
                "raw_quotes": str(self.output_root / "raw_snapshots" / run_date / "quote_snapshots.json"),
                "clean_gold": str(self.output_root / "clean_bars" / run_date / "GOLD_5m.json"),
                "manifest": str(self.output_root / "clean_bars" / run_date / "manifest.json"),
                "data_archive": str(self.output_root / "data_archive" / f"{run_date}.json"),
                "snapshot": str(self.output_root / "snapshots" / f"{run_date}.json"),
            },
        }
        write_json(self.output_root / "data_integrity" / f"{run_date}.json", [payload])
        write_json(self.output_root / "data_integrity" / "current.json", [payload])
        self._write_markdown(run_date, payload)
        return payload

    def _sqlite_check(self) -> dict:
        if not self.market_db.exists():
            return self._check("sqlite_market_db", "fail", "market_data.db is missing", {"path": str(self.market_db)})
        try:
            with sqlite3.connect(self.market_db) as conn:
                integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
                bars = conn.execute("SELECT COUNT(*) FROM bars").fetchone()[0]
                quotes = conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]
                gold_5m = conn.execute("SELECT COUNT(*) FROM bars WHERE symbol='GOLD' AND timeframe='5m'").fetchone()[0]
        except sqlite3.Error as exc:
            return self._check("sqlite_market_db", "fail", f"SQLite check failed: {exc}", {"path": str(self.market_db)})
        status = "pass" if integrity == "ok" and gold_5m > 0 else "fail"
        return self._check("sqlite_market_db", status, f"SQLite integrity={integrity}; GOLD 5m rows={gold_5m}", {"integrity": integrity, "bars": bars, "quotes": quotes, "gold_5m_rows": gold_5m})

    def _gold_clean_files_check(self, run_date: str) -> dict:
        raw = load_json(self.output_root / "raw_snapshots" / run_date / "GOLD_5m.json")
        quotes = load_json(self.output_root / "raw_snapshots" / run_date / "quote_snapshots.json")
        clean = load_json(self.output_root / "clean_bars" / run_date / "GOLD_5m.json")
        manifest = load_json(self.output_root / "clean_bars" / run_date / "manifest.json")
        gold_manifest = next((item for item in manifest if item.get("symbol") == "GOLD" and item.get("timeframe") == "5m"), {})
        if not raw or not clean or not gold_manifest:
            return self._check("gold_clean_files", "fail", "raw, clean, or manifest GOLD 5m artifact is missing", {"raw_rows": len(raw), "clean_rows": len(clean), "manifest_found": bool(gold_manifest)})
        expected = int(gold_manifest.get("clean_rows", len(clean)) or 0)
        status = "pass" if expected == len(clean) and quotes else "warn"
        summary = f"GOLD raw={len(raw)} clean={len(clean)} quote_snapshots={len(quotes)} manifest_clean={expected}"
        return self._check("gold_clean_files", status, summary, {"raw_rows": len(raw), "quote_snapshot_rows": len(quotes), "clean_rows": len(clean), "manifest": gold_manifest})

    def _archive_check(self, run_date: str) -> dict:
        archive = self._latest(self.output_root / "data_archive" / f"{run_date}.json") or self._latest(self.output_root / "data_archive" / "current.json")
        if not archive:
            return self._check("data_archive_manifest", "fail", "data archive manifest is missing", {})
        missing = archive.get("missing_files", [])
        if missing:
            return self._check("data_archive_manifest", "fail", "data archive manifest has missing files", {"missing_files": missing})
        return self._check("data_archive_manifest", "pass", f"archive files present {archive.get('present_file_count')}/{archive.get('file_count')}", {"present_file_count": archive.get("present_file_count"), "file_count": archive.get("file_count")})

    def _snapshot_restore_check(self, run_date: str) -> dict:
        snapshot = self._latest(self.output_root / "snapshots" / f"{run_date}.json") or self._latest(self.output_root / "snapshots" / "current.json")
        if not snapshot:
            return self._check("daily_snapshot", "fail", "daily snapshot receipt is missing", {})
        snapshot_path = Path(str(snapshot.get("snapshot_path", "")))
        included = snapshot.get("included_files", [])
        has_db = any(item.get("arcname") == "data/market_data.db" for item in included)
        if snapshot.get("status") != "pass" or not snapshot_path.exists() or not has_db:
            return self._check("daily_snapshot", "fail", "snapshot package is not restorable enough", {"snapshot": snapshot, "has_market_db": has_db})
        return self._check("daily_snapshot", "pass", "snapshot package exists and includes market_data.db", {"snapshot_path": str(snapshot_path), "sha256": snapshot.get("snapshot_sha256"), "included_file_count": snapshot.get("included_file_count")})

    def _latest(self, path: Path) -> dict:
        rows = load_json(path)
        return rows[-1] if rows else {}

    def _rollup(self, checks: list[dict]) -> str:
        states = {item["status"] for item in checks}
        if "fail" in states:
            return "fail"
        if "warn" in states:
            return "warn"
        return "pass"

    def _check(self, name: str, status: str, summary: str, evidence: dict) -> dict:
        return {"name": name, "status": status, "summary": summary, "evidence": evidence}

    def _write_markdown(self, run_date: str, payload: dict) -> None:
        lines = [
            f"# Data Integrity Check - {run_date}",
            "",
            f"- Status: {payload['status']}",
            f"- Passed: {payload['summary']['passed']}",
            f"- Warned: {payload['summary']['warned']}",
            f"- Failed: {payload['summary']['failed']}",
            "",
            "## Checks",
        ]
        for item in payload["checks"]:
            lines.append(f"- {item['status']}: {item['name']} - {item['summary']}")
        path = self.output_root / "data_integrity" / f"{run_date}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
