from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.daily_snapshot import DailySnapshot
from services.journal_store import write_json
from services.market_store import MarketStore


class DataArchiveManifest:
    def __init__(self, output_root: Path | None = None, market_db: Path | None = None) -> None:
        config = load_pipeline_config()
        self.output_root = output_root or Path(os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(ROOT / config.get("output_root", "outputs"))))
        self.market_db = market_db or Path(os.getenv("TRADING_ORCHESTRATOR_MARKET_DB", str(ROOT / config.get("local_market_db", "data/market_data.db"))))

    def run(self, run_date: str) -> dict:
        files = [self._file_record(path) for path in self._expected_paths(run_date)]
        missing = [item for item in files if not item["exists"]]
        coverage = MarketStore(self.market_db).coverage() if self.market_db.exists() else []
        gold_5m = [item for item in coverage if item["symbol"] == "GOLD" and item["timeframe"] == "5m"]
        snapshot = DailySnapshot(self.output_root, self.market_db).build(run_date, files)
        payload = {
            "run_date": run_date,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": "pass" if not missing and self.market_db.exists() and gold_5m else "warn",
            "market_db": self._file_record(self.market_db),
            "market_coverage": coverage,
            "gold_5m_rows": sum(int(item["rows"]) for item in gold_5m),
            "gold_5m_latest_timestamp": max((str(item.get("last_timestamp", "")) for item in gold_5m), default=""),
            "files": files,
            "missing_files": [item["path"] for item in missing],
            "file_count": len(files),
            "present_file_count": len(files) - len(missing),
            "snapshot": snapshot,
        }
        write_json(self.output_root / "data_archive" / "current.json", [payload])
        write_json(self.output_root / "data_archive" / f"{run_date}.json", [payload])
        self._write_markdown(run_date, payload)
        return payload

    def _expected_paths(self, run_date: str) -> list[Path]:
        return [
            self.output_root / "raw_snapshots" / run_date / "GOLD_5m.json",
            self.output_root / "raw_snapshots" / run_date / "quote_snapshots.json",
            self.output_root / "clean_bars" / run_date / "GOLD_5m.json",
            self.output_root / "clean_bars" / run_date / "manifest.json",
            self.output_root / "signals" / f"{run_date}.json",
            self.output_root / "backtests" / f"{run_date}.json",
            self.output_root / "trade_tickets" / f"{run_date}.json",
            self.output_root / "paper_orders" / f"{run_date}.json",
            self.output_root / "paper_positions" / "current.json",
            self.output_root / "paper_reconciliation" / f"{run_date}.json",
            self.output_root / "paper_trade_attribution" / f"{run_date}.json",
            self.output_root / "paper_exit_monitor" / f"{run_date}.json",
            self.output_root / "paper_exit_decisions" / f"{run_date}.json",
            self.output_root / "paper_exit_decisions" / f"{run_date}.decisions.json",
            self.output_root / "performance" / f"{run_date}.json",
            self.output_root / "equity_curve" / f"{run_date}.json",
            self.output_root / "journals" / f"{run_date}.md",
            self.output_root / "review_notes" / f"{run_date}.md",
            self.output_root / "strategy_snapshots" / f"{run_date}.json",
            self.output_root / "learning_ledger" / f"{run_date}.json",
            self.output_root / "strategy_change_proposals" / f"{run_date}.json",
            self.output_root / "strategy_learning_actions" / f"{run_date}.json",
            self.output_root / "strategy_experiments" / f"{run_date}.json",
            self.output_root / "strategy_improvement_plan" / f"{run_date}.json",
            self.output_root / "strategy_promotion_gate" / f"{run_date}.json",
            self.output_root / "strategy_guardrails" / f"{run_date}.json",
            self.output_root / "risk_monitor" / f"{run_date}.json",
            self.output_root / "paper_risk_action_plan" / f"{run_date}.json",
            self.output_root / "paper_auto_approval_gate" / f"{run_date}.json",
            self.output_root / "bot_supervisor" / f"{run_date}.json",
            self.output_root / "bot_checkpoints" / f"{run_date}.json",
            self.output_root / "data_source_lineage" / f"{run_date}.json",
            self.output_root / "data_trust" / f"{run_date}.json",
            self.output_root / "official_feed_receipts" / f"{run_date}.json",
            self.output_root / "operation_runbooks" / f"{run_date}.json",
            self.output_root / "live_switch_plan" / f"{run_date}.json",
            self.output_root / "live_submission_safety" / f"{run_date}.json",
            self.output_root / "live_dry_run_drill" / f"{run_date}.json",
            self.output_root / "live_broker_preflight" / f"{run_date}.json",
        ]

    def _file_record(self, path: Path) -> dict:
        exists = path.exists()
        record = {
            "path": str(path),
            "exists": exists,
            "size_bytes": path.stat().st_size if exists else 0,
            "sha256": self._sha256(path) if exists and path.is_file() else "",
            "modified_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).replace(microsecond=0).isoformat() if exists else "",
        }
        return record

    def _sha256(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _write_markdown(self, run_date: str, payload: dict) -> None:
        lines = [
            f"# Data Archive Manifest - {run_date}",
            "",
            f"- Status: {payload['status']}",
            f"- Market DB: {payload['market_db']['path']}",
            f"- GOLD 5m rows: {payload['gold_5m_rows']}",
            f"- GOLD latest timestamp: {payload['gold_5m_latest_timestamp']}",
            f"- Files: {payload['present_file_count']} / {payload['file_count']}",
            f"- Snapshot: {payload['snapshot']['snapshot_path']}",
            f"- Snapshot sha256: {payload['snapshot']['snapshot_sha256'][:12]}",
            "",
            "## Files",
        ]
        for item in payload["files"]:
            state = "ok" if item["exists"] else "missing"
            lines.append(f"- {state}: `{item['path']}` size={item['size_bytes']} sha256={item['sha256'][:12]}")
        path = self.output_root / "data_archive" / f"{run_date}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_data_archive_manifest(run_date: str, output_root: Path | None = None, market_db: Path | None = None) -> dict:
    return DataArchiveManifest(output_root, market_db).run(run_date)
