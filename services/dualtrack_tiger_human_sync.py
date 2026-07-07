from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.config_loader import ROOT, load_pipeline_config
from services.dualtrack_clock import cycle_window, parse_utc
from services.dualtrack_config import dualtrack_config
from services.dualtrack_human import DualTrackHumanEngine
from services.journal_store import load_json, write_json


class DualTrackTigerHumanSync:
    """Import Tiger filled orders into the dualtrack human ledger.

    This service consumes the read-only `tiger_order_sync` artifact. It never
    opens Tiger SDK clients and never submits, modifies, cancels, or closes
    orders.
    """

    def __init__(self, output_root: Path | None = None, *, config: dict[str, Any] | None = None) -> None:
        pipeline_config = load_pipeline_config()
        self.output_root = Path(output_root) if output_root else ROOT / pipeline_config.get("output_root", "outputs")
        self.config = self._import_config(config or dualtrack_config())
        self.engine = DualTrackHumanEngine(self.output_root, config=self.config)

    def run(self, run_date: str, *, order_sync_path: Path | None = None) -> dict[str, Any]:
        source_path = order_sync_path or self.output_root / "tiger_order_sync" / "current.json"
        source_rows = load_json(source_path)
        source = source_rows[-1] if source_rows else {}
        if not source:
            return self._write_report(
                run_date,
                {
                    "status": "blocked",
                    "reason": "order_sync_missing",
                    "source_artifact": str(source_path),
                    "source_sync_status": "missing",
                    "source_filled_order_count": 0,
                    "imported_count": 0,
                    "duplicate_count": 0,
                    "skipped_count": 0,
                    "imported_fills": [],
                    "duplicates": [],
                    "skipped": [{"reason": "order_sync_missing"}],
                },
            )
        if str(source.get("sync_status") or "") != "synced":
            return self._write_report(
                run_date,
                {
                    "status": "blocked",
                    "reason": "order_sync_not_synced",
                    "source_artifact": str(source_path),
                    "source_sync_status": str(source.get("sync_status") or "missing"),
                    "source_filled_order_count": int(source.get("filled_order_count") or 0),
                    "imported_count": 0,
                    "duplicate_count": 0,
                    "skipped_count": 0,
                    "imported_fills": [],
                    "duplicates": [],
                    "skipped": [{"reason": "order_sync_not_synced", "error": str(source.get("error") or "")}],
                },
            )

        imported: list[dict[str, Any]] = []
        duplicates: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for row in [item for item in source.get("exchange_filled_orders") or [] if isinstance(item, dict)]:
            payload, skip = self._payload_from_order(row, checked_at=str(source.get("checked_at") or ""))
            if skip:
                skipped.append(skip)
                continue
            assert payload is not None
            if self._source_fill_exists(str(payload["cycle_id"]), str(payload["source_fill_id"])):
                duplicates.append({
                    "source_fill_id": payload["source_fill_id"],
                    "cycle_id": payload["cycle_id"],
                    "external_order_id": payload.get("external_order_id", ""),
                })
                continue
            fill = self.engine.submit_order(payload)
            imported.append({
                "fill_id": fill["fill_id"],
                "cycle_id": fill["fill_id"].rsplit("_human_", 1)[0],
                "source_fill_id": fill.get("source_fill_id", ""),
                "external_order_id": fill.get("external_order_id", ""),
                "symbol": fill.get("symbol", ""),
                "side": fill.get("side", ""),
                "contracts": fill.get("contracts"),
                "price": fill.get("price"),
                "cost": fill.get("cost"),
                "out_of_plan": fill.get("out_of_plan"),
            })

        status = "synced" if not skipped else ("partial" if imported or duplicates else "blocked")
        return self._write_report(
            run_date,
            {
                "status": status,
                "reason": "fills_imported" if imported else ("no_new_fills" if duplicates and not skipped else "no_importable_fills"),
                "source_artifact": str(source_path),
                "source_sync_status": str(source.get("sync_status") or ""),
                "source_filled_order_count": int(source.get("filled_order_count") or len(source.get("exchange_filled_orders") or [])),
                "imported_count": len(imported),
                "duplicate_count": len(duplicates),
                "skipped_count": len(skipped),
                "imported_fills": imported,
                "duplicates": duplicates,
                "skipped": skipped,
            },
        )

    def _payload_from_order(self, row: dict[str, Any], *, checked_at: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        symbol = str(row.get("symbol") or "")
        root_symbol = str(row.get("root_symbol") or self._root_symbol(symbol))
        if root_symbol != "MGC" and not symbol.startswith("MGC"):
            return None, self._skip(row, "unsupported_symbol")
        side = self._side(row.get("side"))
        if side is None:
            return None, self._skip(row, "unsupported_side")
        contracts = self._float(row.get("filled_quantity"))
        if contracts is None or contracts <= 0:
            return None, self._skip(row, "missing_filled_quantity")
        if not float(contracts).is_integer():
            return None, self._skip(row, "non_whole_contract_quantity")
        price = self._float(row.get("average_fill_price"))
        if price is None or price <= 0:
            return None, self._skip(row, "missing_average_fill_price")
        ts = self._timestamp(row, checked_at)
        source_fill_id = self._source_fill_id(row, ts=ts, contracts=contracts, price=price)
        return {
            "cycle_id": cycle_window(ts).cycle_id,
            "ts": ts,
            "side": side,
            "order_type": self._order_type(row.get("type")),
            "price": price,
            "contracts": int(contracts),
            "commission": self._positive_commission(row.get("commission")),
            "source": "tiger_openapi_order_sync",
            "source_fill_id": source_fill_id,
            "external_order_id": str(row.get("order_id") or ""),
            "external_parent_id": str(row.get("parent_id") or ""),
            "broker": "tiger_openapi",
            "broker_order_type": str(row.get("type") or ""),
            "broker_status": str(row.get("status") or ""),
            "symbol": symbol,
            "root_symbol": root_symbol,
            "currency": str(row.get("currency") or "USD"),
            "imported_at": self._now(),
        }, None

    def _source_fill_exists(self, cycle_id: str, source_fill_id: str) -> bool:
        rows = load_json(self.output_root / "dualtrack" / "fills" / f"{cycle_id}_human.json")
        return any(str(row.get("source_fill_id") or "") == source_fill_id for row in rows)

    def _write_report(self, run_date: str, payload: dict[str, Any]) -> dict[str, Any]:
        report = {
            "run_date": run_date,
            "provider": "tiger_openapi",
            "target": "dualtrack_human_ledger",
            "safety": {
                "reads_order_sync_artifact": True,
                "opens_tiger_sdk_client": False,
                "submits_orders": False,
                "cancels_orders": False,
                "closes_positions": False,
                "writes_human_ledger": True,
            },
            "checked_at": self._now(),
            **payload,
        }
        write_json(self.output_root / "dualtrack" / "tiger_human_sync" / "current.json", [report])
        write_json(self.output_root / "dualtrack" / "tiger_human_sync" / f"{run_date}.json", [report])
        return report

    def _import_config(self, config: dict[str, Any]) -> dict[str, Any]:
        merged = deepcopy(config)
        model = merged.get("execution_cost_model")
        if not isinstance(model, dict) or not model.get("venue"):
            merged["execution_cost_model"] = {
                "venue": "tiger_mgc",
                "quantity_mode": "integer_contracts",
                "contract_multiplier": 10,
                "contracts_per_rung": 1,
            }
        return merged

    def _timestamp(self, row: dict[str, Any], checked_at: str) -> str:
        value = row.get("filled_at") or row.get("created_at") or checked_at or self._now()
        return parse_utc(value).isoformat()

    def _source_fill_id(self, row: dict[str, Any], *, ts: str, contracts: float, price: float) -> str:
        order_id = str(row.get("order_id") or "")
        if order_id:
            return f"tiger_openapi:{order_id}:{ts}:{int(contracts)}:{round(float(price), 8)}"
        digest = hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
        return f"tiger_openapi:fill:{digest}"

    def _skip(self, row: dict[str, Any], reason: str) -> dict[str, Any]:
        return {
            "reason": reason,
            "order_id": str(row.get("order_id") or ""),
            "symbol": str(row.get("symbol") or ""),
            "filled_quantity": row.get("filled_quantity"),
            "average_fill_price": row.get("average_fill_price"),
            "side": row.get("side"),
        }

    def _side(self, value: Any) -> str | None:
        text = str(value or "").strip().lower()
        if text in {"buy", "bot", "bought", "buy_to_open", "buy_to_close"}:
            return "buy"
        if text in {"sell", "sld", "sold", "sell_to_open", "sell_to_close"}:
            return "sell"
        return None

    def _order_type(self, value: Any) -> str:
        text = str(value or "").strip().lower()
        if text in {"lmt", "limit"}:
            return "limit"
        if text in {"stp", "stop", "stop_limit", "stp_lmt"}:
            return "stop"
        return "market"

    def _root_symbol(self, symbol: str) -> str:
        prefix = ""
        for char in str(symbol):
            if char.isalpha() or (char.isdigit() and not prefix):
                prefix += char
            else:
                break
        return prefix or str(symbol)

    def _float(self, value: Any) -> float | None:
        try:
            return float(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    def _positive_commission(self, value: Any) -> float | None:
        commission = self._float(value)
        return commission if commission is not None and commission > 0 else None

    def _now(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
