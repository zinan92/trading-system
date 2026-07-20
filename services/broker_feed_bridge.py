from __future__ import annotations

import os
from hashlib import sha256
from datetime import datetime, timezone
from pathlib import Path

from services.bar_importer import BarCsvImporter
from services.broker_feed_doctor import BrokerFeedDoctor
from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json
from services.market_store import MarketStore
from services.market_data_access import uses_independent_datafeed


class BrokerFeedBridge:
    def __init__(self, output_root: Path | None = None, market_db: Path | None = None, config: dict | None = None) -> None:
        pipeline_config = load_pipeline_config()
        self.config = config or pipeline_config.get("broker_feed", {})
        self.output_root = output_root or Path(os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(ROOT / pipeline_config.get("output_root", "outputs"))))
        self.market_db = market_db or Path(os.getenv("TRADING_ORCHESTRATOR_MARKET_DB", str(ROOT / pipeline_config.get("local_market_db", "data/market_data.db"))))
        self.input_dir = self._resolve_path(os.getenv("TRADING_ORCHESTRATOR_BROKER_FEED_INPUT_DIR") or self.config.get("input_dir", "data/broker_feeds/gold_5m"))
        self.provider = str(self.config.get("provider", "mt5_csv"))
        self.symbol = str(self.config.get("symbol", "GOLD"))
        self.timeframe = str(self.config.get("timeframe", "5m"))
        self.pattern = str(self.config.get("pattern", "*.csv"))

    def import_pending(self, run_date: str) -> dict:
        if uses_independent_datafeed(self.market_db):
            summary = {
                "run_date": run_date,
                "status": "skipped",
                "reason": "CSV market-data imports must be installed as datafeed adapters",
                "market_data_backend": "datafeed",
                "new_files": 0,
                "imported_rows": 0,
            }
            write_json(self.output_root / "broker_feed_imports" / "current.json", [summary])
            return summary
        self.input_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.output_root / "broker_feed_imports" / f"{run_date}.json"
        existing = load_json(log_path)
        imported_fingerprints = {
            (item.get("path"), item.get("sha256"))
            for item in existing
            if item.get("status") == "imported" and item.get("sha256")
        }
        legacy_imported_files = {
            item.get("path")
            for item in existing
            if item.get("status") == "imported" and not item.get("sha256")
        }
        importer = BarCsvImporter(MarketStore(self.market_db))
        doctor = BrokerFeedDoctor(self.output_root, self.config)
        results = existing[:]
        new_imports = []
        for path in sorted(self.input_dir.glob(self.pattern)):
            if not path.is_file() or self._is_helper_file(path):
                continue
            file_hash = self._sha256(path)
            if (str(path), file_hash) in imported_fingerprints:
                continue
            if str(path) in legacy_imported_files and not file_hash:
                continue
            inspection = {}
            try:
                inspection = doctor.inspect_file(path)
                if inspection.get("status") == "fail":
                    raise ValueError(f"broker feed validation failed: {'; '.join(inspection.get('errors', []))}")
                result = importer.import_csv(path, self.symbol, self.timeframe, self.provider)
                record = {
                    **result,
                    "status": "imported",
                    "sha256": file_hash,
                    "validation": inspection,
                    "imported_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                    "source": "broker_feed_bridge",
                    "local_db": str(self.market_db),
                }
            except Exception as exc:
                record = {
                    "path": str(path),
                    "status": "error",
                    "sha256": file_hash,
                    "error": str(exc),
                    "validation": inspection,
                    "imported_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                    "source": "broker_feed_bridge",
                    "local_db": str(self.market_db),
                }
            results.append(record)
            new_imports.append(record)
        write_json(log_path, results)
        summary = {
            "run_date": run_date,
            "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "input_dir": str(self.input_dir),
            "pattern": self.pattern,
            "provider": self.provider,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "new_files": len(new_imports),
            "imported_rows": sum(int(item.get("imported_rows", 0)) for item in new_imports if item.get("status") == "imported"),
            "errors": [item for item in new_imports if item.get("status") == "error"],
            "imported_files": [self._summary_record(item) for item in new_imports if item.get("status") == "imported"],
            "skipped_previously_imported": len(imported_fingerprints) + len(legacy_imported_files),
            "total_import_log_entries": len(results),
            "log_path": str(log_path),
            "local_db": str(self.market_db),
        }
        write_json(self.output_root / "broker_feed_imports" / "current.json", [summary])
        return summary

    def _is_helper_file(self, path: Path) -> bool:
        return path.name.startswith("NEEDS_") or path.name.endswith(".template") or path.name.upper().startswith("README")

    def _sha256(self, path: Path) -> str:
        digest = sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _summary_record(self, item: dict) -> dict:
        coverage = [
            row
            for row in item.get("coverage", [])
            if row.get("symbol") == self.symbol and row.get("timeframe") == self.timeframe and row.get("provider") == self.provider
        ]
        latest = coverage[-1] if coverage else {}
        return {
            "path": item.get("path", ""),
            "sha256": item.get("sha256", ""),
            "imported_rows": item.get("imported_rows", 0),
            "provider": item.get("provider", self.provider),
            "latest_timestamp": latest.get("last_timestamp", ""),
            "coverage_rows": latest.get("rows", 0),
        }

    def _resolve_path(self, path_value: str) -> Path:
        path = Path(path_value)
        if path.is_absolute():
            return path
        return ROOT / path
