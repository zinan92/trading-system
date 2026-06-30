from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json


class BrokerReceiptImporter:
    def __init__(self, output_root: Path | None = None, broker_config: dict | None = None) -> None:
        config = load_pipeline_config()
        self.output_root = output_root or Path(os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(ROOT / config.get("output_root", "outputs"))))
        self.broker_config = broker_config or config.get("broker", {})
        self.inbox_dir = self._resolve_path(str(self.broker_config.get("inbox_dir", "data/broker_inbox/mt5")))
        self.pattern = str(self.broker_config.get("receipt_pattern", "*.json"))

    def import_pending(self, run_date: str) -> dict:
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        receipt_path = self.output_root / "broker_receipts" / f"{run_date}.json"
        existing = load_json(receipt_path)
        seen_files = {item.get("source_file") for item in existing}
        imported = []
        errors = []
        for path in sorted(self.inbox_dir.glob(self.pattern)):
            if not path.is_file() or str(path) in seen_files:
                continue
            try:
                receipt = json.loads(path.read_text(encoding="utf-8"))
                normalized = self._normalize_receipt(receipt, path, run_date)
                existing = [item for item in existing if item.get("order_id") != normalized.get("order_id")]
                existing.append(normalized)
                imported.append(normalized)
            except Exception as exc:
                errors.append({"source_file": str(path), "error": str(exc)})
        write_json(receipt_path, existing)
        write_json(self.output_root / "broker_receipts" / "current.json", existing[-50:])
        summary = {
            "run_date": run_date,
            "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "inbox_dir": str(self.inbox_dir),
            "pattern": self.pattern,
            "new_receipts": len(imported),
            "total_receipts": len(existing),
            "errors": errors,
        }
        write_json(self.output_root / "broker_receipts" / "summary_current.json", [summary])
        return summary

    def _normalize_receipt(self, receipt: dict, path: Path, run_date: str) -> dict:
        order_id = str(receipt.get("order_id") or receipt.get("client_order_id") or "")
        if not order_id:
            raise ValueError("broker receipt must include order_id or client_order_id")
        status = str(receipt.get("status") or receipt.get("execution_status") or "unknown")
        return {
            "run_date": run_date,
            "order_id": order_id,
            "broker_order_id": receipt.get("broker_order_id") or receipt.get("ticket") or "",
            "status": status,
            "fill_price": receipt.get("fill_price"),
            "filled_quantity": receipt.get("filled_quantity") or receipt.get("quantity"),
            "filled_at": receipt.get("filled_at") or receipt.get("timestamp") or "",
            "message": receipt.get("message") or receipt.get("reason") or "",
            "raw": receipt,
            "source_file": str(path),
            "imported_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        }

    def _resolve_path(self, raw: str) -> Path:
        path = Path(raw)
        return path if path.is_absolute() else ROOT / path
