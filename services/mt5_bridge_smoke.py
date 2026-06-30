from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from services.broker_adapter import BrokerOrderRequest, LiveBrokerAdapter
from services.broker_receipts import BrokerReceiptImporter
from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json


class Mt5BridgeSmoke:
    def __init__(self, output_root: Path | None = None, sandbox_root: Path | None = None) -> None:
        config = load_pipeline_config()
        self.output_root = output_root or ROOT / config.get("output_root", "outputs")
        self.sandbox_root = sandbox_root or ROOT / "data" / "broker_bridge_smoke"
        self.outbox = self.sandbox_root / "outbox"
        self.inbox = self.sandbox_root / "inbox"

    def run(self, run_date: str) -> dict:
        broker_config = {
            "provider": "mt5_file_bridge",
            "dry_run": True,
            "request_dir": "live_order_requests_smoke",
            "allowed_symbols": ["GOLD"],
            "outbox_dir": str(self.outbox),
            "inbox_dir": str(self.inbox),
        }
        adapter = LiveBrokerAdapter(self.output_root, True, broker_config)
        ticket = self._ticket(run_date)
        order = adapter.submit_order(BrokerOrderRequest(run_date, ticket, latest_price=4571.3))
        receipt_file = self._write_mock_receipt(order.order_id)
        receipt_summary = BrokerReceiptImporter(self.output_root, {**broker_config, "receipt_pattern": "*.json"}).import_pending(run_date)
        imported_receipts = load_json(self.output_root / "broker_receipts" / "current.json")
        has_receipt = any(item.get("order_id") == order.order_id for item in imported_receipts)
        result = {
            "run_date": run_date,
            "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": "pass" if has_receipt else "fail",
            "order": order.to_dict(),
            "outbox_files": [str(path) for path in sorted(self.outbox.glob("*.json"))],
            "mock_receipt_file": str(receipt_file),
            "has_imported_receipt": has_receipt,
            "receipt_summary": receipt_summary,
            "sandbox_root": str(self.sandbox_root),
        }
        write_json(self.output_root / "mt5_bridge_smoke" / "current.json", [result])
        write_json(self.output_root / "mt5_bridge_smoke" / f"{run_date}.json", [result])
        return result

    def _write_mock_receipt(self, order_id: str) -> Path:
        self.inbox.mkdir(parents=True, exist_ok=True)
        path = self.inbox / f"{order_id}.receipt.json"
        payload = {
            "order_id": order_id,
            "broker_order_id": f"smoke-{order_id[-6:]}",
            "status": "filled",
            "fill_price": 4571.3,
            "filled_quantity": 1.0,
            "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "message": "smoke receipt generated locally; no broker was contacted",
        }
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return path

    def _ticket(self, run_date: str) -> dict:
        compact = run_date.replace("-", "")
        return {
            "ticket_id": f"ticket_gold_{compact}_mt5_smoke",
            "signal_id": f"sig_gold_{compact}_mt5_smoke",
            "asset": "GOLD",
            "asset_class": "commodity",
            "action": "prepare_buy",
            "entry_zone": "4560-4580",
            "stop_loss": 4480,
            "targets": [4750],
            "position_size_pct": 0.1,
            "max_loss_pct": 0.01,
            "order_type": "limit",
            "time_in_force": "day",
            "paper_only": True,
            "manual_execution_required": True,
        }
