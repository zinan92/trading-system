from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from schemas.market_data import PaperOrder
from services.broker_port import (
    BrokerOrderRequest,
    BrokerPortDescriptor,
    broker_port_descriptor,
    execution_capabilities_for,
)
from services.config_loader import ROOT
from services.journal_store import load_json, write_json


class Mt5FileBridgeBrokerAdapter:
    """Filesystem delivery adapter for an external MT5 EA/manual bridge."""

    name = "mt5_file_bridge"
    provider = "mt5_file_bridge"

    def __init__(
        self,
        output_root: Path,
        live_trading_enabled: bool,
        broker_config: dict | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.live_trading_enabled = live_trading_enabled
        self.broker_config = broker_config or {}
        self.dry_run = bool(self.broker_config.get("dry_run", True))
        self._capabilities = execution_capabilities_for(
            provider=self.provider,
            adapter_name=self.name,
        )

    @property
    def capabilities(self):
        return self._capabilities

    @property
    def descriptor(self) -> BrokerPortDescriptor:
        return broker_port_descriptor(self)

    def preflight(self) -> dict:
        outbox_dir = self._outbox_dir()
        inbox_dir = self._inbox_dir()
        docs = {}
        errors = []
        writable = False
        inbox_writable = False
        try:
            outbox_dir.mkdir(parents=True, exist_ok=True)
            probe = outbox_dir / ".write_test"
            probe.write_text("ok\n", encoding="utf-8")
            probe.unlink(missing_ok=True)
            writable = True
        except OSError as exc:
            errors.append(f"outbox_not_writable:{exc}")
        try:
            inbox_dir.mkdir(parents=True, exist_ok=True)
            probe = inbox_dir / ".write_test"
            probe.write_text("ok\n", encoding="utf-8")
            probe.unlink(missing_ok=True)
            inbox_writable = True
        except OSError as exc:
            errors.append(f"inbox_not_writable:{exc}")
        if writable or inbox_writable:
            docs = self._ensure_bridge_docs(outbox_dir, inbox_dir)
        ready = bool(
            self.live_trading_enabled and writable and inbox_writable
        )
        block_reason = ""
        if not self.live_trading_enabled:
            block_reason = "live_trading_enabled is false"
        elif not writable:
            block_reason = "MT5 outbox directory is not writable"
        elif not inbox_writable:
            block_reason = "MT5 inbox directory is not writable"
        elif self.dry_run:
            block_reason = "MT5 file bridge dry_run enabled; request artifact only"
        else:
            block_reason = (
                "MT5 file bridge outbox ready; external MT5 EA must consume files"
            )
        return {
            "provider": self.provider,
            "mode": "live",
            "dry_run": self.dry_run,
            "live_trading_enabled": self.live_trading_enabled,
            "ready": ready,
            "block_reason": block_reason,
            "missing_env": [],
            "allowed_symbols": list(self.broker_config.get("allowed_symbols", [])),
            "outbox_dir": str(outbox_dir),
            "inbox_dir": str(inbox_dir),
            "outbox_writable": writable,
            "inbox_writable": inbox_writable,
            **docs,
            "errors": errors,
            "checked_at": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat(),
        }

    def submit_order(self, request: BrokerOrderRequest) -> PaperOrder:
        if not self.live_trading_enabled:
            raise RuntimeError(
                "live trading is disabled; set live_trading_enabled only after wiring a real broker adapter"
            )
        readiness = self.preflight()
        if not readiness["ready"] and not self.dry_run:
            raise RuntimeError(
                f"live broker preflight failed: {readiness['block_reason']}"
            )
        if not self.dry_run:
            activation = self._live_activation(request.run_date)
            if activation.get("real_money_ready") is not True:
                raise RuntimeError(
                    "live activation gate is not real_money_ready; real broker submission is blocked"
                )
        return self._submit_with_readiness(request, readiness)

    def _submit_with_readiness(
        self,
        request: BrokerOrderRequest,
        readiness: dict,
    ) -> PaperOrder:
        ticket = request.ticket
        requested_price = float(
            request.latest_price or self._entry_midpoint(ticket["entry_zone"])
        )
        quantity = float(
            request.actual_size or self._quantity(ticket, requested_price)
        )
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        order_id = self._order_id(
            ticket["ticket_id"],
            request.run_date,
            requested_price,
        )
        status = "bridge_dry_run" if self.dry_run else "submitted_to_bridge"
        rejection = (
            "MT5 bridge dry-run request recorded locally; no EA should execute it"
            if self.dry_run
            else (
                "submitted to MT5 file bridge outbox; execution depends on external EA"
            )
        )
        receipt = PaperOrder(
            order_id=order_id,
            ticket_id=ticket["ticket_id"],
            status=status,
            requested_price=round(requested_price, 4),
            fill_price=None,
            quantity=round(quantity, 6),
            filled_at="",
            rejection_reason=rejection,
        )
        bridge_payload = {
            "order_id": order_id,
            "created_at": now,
            "dry_run": self.dry_run,
            "symbol": ticket.get("asset"),
            "action": ticket.get("action"),
            "order_type": ticket.get("order_type", "limit"),
            "time_in_force": ticket.get("time_in_force", "day"),
            "requested_price": receipt.requested_price,
            "quantity": receipt.quantity,
            "stop_loss": ticket.get("stop_loss"),
            "targets": ticket.get("targets", []),
            "source_ticket_id": ticket.get("ticket_id"),
            "manual_execution_required": ticket.get(
                "manual_execution_required",
                True,
            ),
        }
        outbox_file = self._outbox_dir() / f"{order_id}.json"
        outbox_file.write_text(
            json.dumps(bridge_payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        payload = {
            "order_id": order_id,
            "requested_at": now,
            "provider": self.provider,
            "run_date": request.run_date,
            "ticket": ticket,
            "request": bridge_payload,
            "readiness": readiness,
            "outbox_file": str(outbox_file),
            "receipt": receipt.to_dict(),
        }
        request_dir = str(
            self.broker_config.get("request_dir", "live_order_requests")
        )
        path = self.output_root / request_dir / f"{request.run_date}.json"
        rows = [
            item for item in load_json(path) if item.get("order_id") != order_id
        ]
        rows.append(payload)
        write_json(path, rows)
        return receipt

    def _outbox_dir(self) -> Path:
        raw = Path(
            str(self.broker_config.get("outbox_dir", "data/broker_outbox/mt5"))
        )
        return raw if raw.is_absolute() else ROOT / raw

    def _inbox_dir(self) -> Path:
        raw = Path(
            str(self.broker_config.get("inbox_dir", "data/broker_inbox/mt5"))
        )
        return raw if raw.is_absolute() else ROOT / raw

    def _ensure_bridge_docs(
        self,
        outbox_dir: Path,
        inbox_dir: Path,
    ) -> dict:
        request_template = outbox_dir / "ORDER_REQUEST.json.template"
        receipt_template = inbox_dir / "ORDER_RECEIPT.json.template"
        outbox_readme = outbox_dir / "README_MT5_FILE_BRIDGE.md"
        inbox_readme = inbox_dir / "README_MT5_FILE_BRIDGE.md"
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        request_example = {
            "order_id": "live_dryrun_example",
            "created_at": now,
            "dry_run": True,
            "symbol": "GOLD",
            "action": "prepare_buy",
            "order_type": "limit",
            "time_in_force": "day",
            "requested_price": 4571.3,
            "quantity": 0.25,
            "stop_loss": 4480.0,
            "targets": [4750.0],
            "source_ticket_id": "ticket_gold_YYYYMMDD_example",
            "manual_execution_required": True,
        }
        receipt_example = {
            "order_id": "live_dryrun_example",
            "broker_order_id": "mt5-ticket-id",
            "status": "filled",
            "fill_price": 4571.3,
            "filled_quantity": 0.25,
            "timestamp": now,
            "message": "executed by external MT5 EA or manual bridge",
        }
        request_template.write_text(
            json.dumps(request_example, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        receipt_template.write_text(
            json.dumps(receipt_example, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        outbox_readme.write_text(
            "\n".join(
                [
                    "# MT5 File Bridge Outbox",
                    "",
                    "The trading bot writes one JSON order request per order into this directory.",
                    "An external MT5 Expert Advisor, script, or manual bridge may consume these files.",
                    "",
                    "Contract:",
                    "- Treat `dry_run: true` as a non-executable test artifact.",
                    "- Execute only when live mode is intentionally enabled and `dry_run: false`.",
                    "- Preserve `order_id`; it is the correlation key for broker receipts.",
                    "- Do not modify request files in place after consuming them.",
                    "",
                    "Order request fields are shown in `ORDER_REQUEST.json.template`.",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        inbox_readme.write_text(
            "\n".join(
                [
                    "# MT5 File Bridge Inbox",
                    "",
                    "Write broker execution receipts here as JSON files matching `*.json`.",
                    "The bot imports receipts with `python3 -m pipelines.broker_receipts --date YYYY-MM-DD`.",
                    "",
                    "Required receipt fields:",
                    "- `order_id` or `client_order_id`",
                    "- `status` such as `filled`, `rejected`, `partial`, or `pending`",
                    "",
                    "Recommended receipt fields:",
                    "- `broker_order_id` or `ticket`",
                    "- `fill_price`",
                    "- `filled_quantity` or `quantity`",
                    "- `timestamp` or `filled_at`",
                    "- `message` or `reason`",
                    "",
                    "Receipt examples are shown in `ORDER_RECEIPT.json.template`.",
                    "Templates use `.json.template` so they are ignored by the `*.json` importer.",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return {
            "outbox_readme": str(outbox_readme),
            "inbox_readme": str(inbox_readme),
            "request_template": str(request_template),
            "receipt_template": str(receipt_template),
        }

    def _entry_midpoint(self, entry_zone: str) -> float:
        low, high = [float(part) for part in entry_zone.split("-", 1)]
        return (low + high) / 2

    def _quantity(self, ticket: dict, price: float) -> float:
        account_equity = float(
            self.broker_config.get("dry_run_account_equity", 100_000)
        )
        notional = account_equity * (
            float(ticket.get("position_size_pct", 0)) / 100
        )
        return notional / price if price else 0.0

    def _order_id(
        self,
        ticket_id: str,
        run_date: str,
        requested_price: float,
    ) -> str:
        del requested_price
        raw = f"{ticket_id}:{run_date}:{self.provider}:entry".encode("utf-8")
        return f"live_dryrun_{hashlib.sha256(raw).hexdigest()[:10]}"

    def _live_activation(self, run_date: str) -> dict:
        rows = load_json(self.output_root / "live_activation" / f"{run_date}.json")
        if not rows:
            rows = load_json(self.output_root / "live_activation" / "current.json")
        return rows[-1] if rows else {}
