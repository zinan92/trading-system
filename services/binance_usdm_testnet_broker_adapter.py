from __future__ import annotations

import hashlib
import json
import urllib.error
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from schemas.market_data import PaperOrder
from services.broker_adapter import BrokerOrderRequest, LiveBrokerAdapter
from services.journal_store import load_json, write_json
from services.live_reconciliation import LiveBrokerReconciliation
from services.order_lifecycle import IllegalOrderTransition, OrderLifecycleStore
from services.paper_executor import PaperExecutor


TESTNET_BASE_URL = "https://demo-fapi.binance.com"
TESTNET_SYMBOL = "XAUUSDT"


class BinanceUsdmTestnetBrokerAdapter(LiveBrokerAdapter):
    """Locked Binance USDM testnet adapter for XAUUSDT go-live drills.

    It deliberately bypasses the real-money activation gate only for Binance's
    testnet endpoint. Mainnet remains blocked by the normal LiveBrokerAdapter
    live_activation path.
    """

    name = "binance_usdm_testnet"

    def __init__(self, output_root: Path, broker_config: dict, testnet_config: dict | None = None, opener=None) -> None:
        self.testnet_config = testnet_config or {}
        merged = {
            **broker_config,
            "provider": "binance_usdm",
            "environment": "testnet",
            "base_url": TESTNET_BASE_URL,
            "dry_run": False,
            "request_dir": str(self.testnet_config.get("request_dir", broker_config.get("request_dir", "testnet_order_requests"))),
            "protective_order_endpoint": str(self.testnet_config.get("protective_order_endpoint", broker_config.get("protective_order_endpoint", "algoOrder"))),
            "reconcile_account_history": True,
            "instrument_map": {"GOLD": TESTNET_SYMBOL, "XAUUSD": TESTNET_SYMBOL, **broker_config.get("instrument_map", {})},
        }
        super().__init__(output_root, live_trading_enabled=True, broker_config=merged, opener=opener)

    def submit_order(self, request: BrokerOrderRequest) -> PaperOrder:
        readiness = self.preflight()
        guard = self._testnet_static_guard(readiness)
        if not guard["ready"]:
            self._record_testnet_block(request.run_date, request.ticket.get("ticket_id", ""), guard, readiness)
            raise RuntimeError(guard["block_reason"])
        if not readiness.get("ready"):
            self._record_testnet_block(request.run_date, request.ticket.get("ticket_id", ""), guard, readiness)
            raise RuntimeError(f"Binance USDM testnet broker preflight failed: {readiness.get('block_reason')}")
        reconciliation = self._testnet_reconciliation_check(request.run_date)
        readiness = {**readiness, "live_reconciliation": reconciliation.get("report", {})}
        if not reconciliation["ready"]:
            self._record_testnet_block(request.run_date, request.ticket.get("ticket_id", ""), reconciliation, readiness)
            raise RuntimeError(reconciliation["block_reason"])
        if bool(self.testnet_config.get("require_flat_before_entry", True)):
            flat = self._flat_position_check()
            if not flat["ready"]:
                self._record_testnet_block(request.run_date, request.ticket.get("ticket_id", ""), flat, readiness)
                raise RuntimeError(flat["block_reason"])
            readiness = {**readiness, "position_precheck": flat}
        capped = self._with_capped_quantity(request, readiness)
        return self._submit_binance_order(capped, {**readiness, "mode": "testnet", "testnet_trading": True})

    def close_testnet_position(
        self,
        run_date: str,
        *,
        order_id: str = "",
        confirm_close_testnet_position: bool = False,
        exit_reason: str = "binance_usdm_testnet_canary_close",
    ) -> dict:
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        readiness = self.preflight()
        guard = self._testnet_static_guard(readiness)
        report = {
            "run_date": run_date,
            "checked_at": now,
            "provider": "binance_usdm",
            "mode": "testnet",
            "base_url": self._binance_base_url(),
            "symbol": TESTNET_SYMBOL,
            "confirm_close_testnet_position": confirm_close_testnet_position,
            "network_order_created": False,
            "readiness": readiness,
            "guard": guard,
            "position": {},
            "close_request": {},
            "close_response": {},
            "protective_cancel": {},
            "reconciliation_after": {},
            "local_mirror": {},
            "status": "dry_run",
        }
        if not guard["ready"] or not readiness.get("ready"):
            report["status"] = "blocked"
            report["block_reason"] = guard["block_reason"] or f"Binance USDM testnet broker preflight failed: {readiness.get('block_reason')}"
            self._write_testnet_close_report(run_date, report)
            return report
        position = self._position_snapshot()
        report["position"] = position
        if not position.get("ok"):
            report["status"] = "blocked"
            report["block_reason"] = "cannot read Binance USDM testnet position before close"
            self._write_testnet_close_report(run_date, report)
            return report
        amount = float(position.get("positionAmt", 0) or 0)
        if amount == 0:
            report["status"] = "flat"
            report["reconciliation_after"] = self._reconcile_after_close(run_date)
            self._write_testnet_close_report(run_date, report)
            return report
        close_payload = {
            "symbol": TESTNET_SYMBOL,
            "side": "BUY" if amount < 0 else "SELL",
            "type": "MARKET",
            "quantity": self._format_decimal(abs(amount)),
            "reduceOnly": "true",
            "newOrderRespType": "RESULT",
            "newClientOrderId": self._testnet_close_id(run_date),
        }
        report["close_request"] = self._safe_order_payload(close_payload)
        if not confirm_close_testnet_position:
            report["status"] = "dry_run"
            report["block_reason"] = "dry-run only; pass confirm_close_testnet_position to submit reduce-only testnet close"
            self._write_testnet_close_report(run_date, report)
            return report
        try:
            close_response = self._post_binance_order(close_payload)
            report["close_response"] = close_response
            report["network_order_created"] = True
            report["status"] = "submitted"
            close_price = self._binance_fill_price(close_response) or float(position.get("entryPrice", 0) or 0)
            close_qty = self._binance_executed_quantity(close_response) or abs(amount)
            if order_id:
                report["local_mirror"] = PaperExecutor(self.output_root).record_external_close(
                    run_date,
                    order_id=order_id,
                    exit_price=close_price,
                    quantity=close_qty,
                    exit_reason=exit_reason,
                    close_order_id=str(close_response.get("orderId") or close_response.get("clientOrderId") or ""),
                )
                if report["local_mirror"].get("closed"):
                    try:
                        OrderLifecycleStore(self.output_root).transition(
                            run_date,
                            order_id,
                            "closed",
                            reason=exit_reason,
                            metadata={"close_response": self._safe_order_payload(close_response)},
                        )
                        report["lifecycle_transition"] = {"status": "closed"}
                    except IllegalOrderTransition as exc:
                        report["lifecycle_transition"] = {"status": "skipped", "error": str(exc)}
            report["protective_cancel"] = self._cancel_testnet_protective_orders()
            if report["protective_cancel"].get("status") == "failed":
                report["status"] = "protective_cancel_failed"
                report["block_reason"] = "testnet position closed but protective order cancel failed"
        except (OSError, TimeoutError, RuntimeError, urllib.error.HTTPError, json.JSONDecodeError, ValueError) as exc:
            report["status"] = "failed"
            report["block_reason"] = f"{type(exc).__name__}: {exc}"
        report["reconciliation_after"] = self._reconcile_after_close(run_date)
        self._write_testnet_close_report(run_date, report)
        return report

    def _testnet_static_guard(self, readiness: dict) -> dict:
        symbol = self._binance_symbol("GOLD")
        reasons = []
        if self.provider != "binance_usdm":
            reasons.append(f"provider is {self.provider}, expected binance_usdm")
        if self._binance_base_url() != TESTNET_BASE_URL:
            reasons.append(f"base_url is {self._binance_base_url()}, expected {TESTNET_BASE_URL}")
        if symbol != TESTNET_SYMBOL:
            reasons.append(f"symbol is {symbol}, expected {TESTNET_SYMBOL}")
        if self.dry_run:
            reasons.append("dry_run is true; testnet drill requires real testnet exchange orders")
        if readiness.get("exchange_status", {}).get("status") not in {"TRADING", None}:
            reasons.append(f"exchange status is {readiness.get('exchange_status', {}).get('status')}")
        return {
            "ready": not reasons,
            "block_reason": "; ".join(reasons),
            "base_url": self._binance_base_url(),
            "symbol": symbol,
            "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        }

    def _testnet_reconciliation_check(self, run_date: str) -> dict:
        report = LiveBrokerReconciliation(self.output_root, self.broker_config, opener=self.opener).run(run_date)
        if report.get("suspected_naked_position"):
            return {
                "ready": False,
                "block_reason": f"Binance USDM testnet suspected naked position: {report.get('escalation_action') or report.get('reason_code')}",
                "report": report,
            }
        if report.get("confirmation_status") == "cannot_confirm":
            return {
                "ready": False,
                "block_reason": f"Binance USDM testnet reconciliation cannot confirm venue state: {report.get('error')}",
                "report": report,
            }
        if report.get("drift_count", 0):
            reasons = sorted({str(item.get("reason", "reconciliation drift")) for item in report.get("drifts", [])})
            return {
                "ready": False,
                "block_reason": "Binance USDM testnet reconciliation drift: " + "; ".join(reasons),
                "report": report,
            }
        return {"ready": True, "block_reason": "", "report": report}

    def _with_capped_quantity(self, request: BrokerOrderRequest, readiness: dict) -> BrokerOrderRequest:
        ticket = request.ticket
        requested_price = float(request.latest_price or self._entry_midpoint(ticket["entry_zone"]))
        raw_quantity = float(request.actual_size or self._quantity(ticket, requested_price) or 0)
        max_quantity = float(self.testnet_config.get("max_order_quantity", 0.002))
        quantity = raw_quantity if raw_quantity > 0 else max_quantity
        quantity = min(quantity, max_quantity)
        filters = readiness.get("exchange_status", {}).get("filters", {})
        quantity = self._round_step(quantity, str(filters.get("step_size", "0.001")))
        if quantity <= 0:
            raise RuntimeError("Binance USDM testnet order quantity is zero after cap and step-size rounding")
        return replace(request, actual_size=quantity)

    def _flat_position_check(self) -> dict:
        position = self._position_snapshot()
        if not position.get("ok"):
            return {"ready": False, "block_reason": "cannot read Binance USDM testnet position before strategy order", "position": position}
        amount = float(position.get("positionAmt", 0) or 0)
        if amount != 0:
            return {
                "ready": False,
                "block_reason": "XAUUSDT testnet position is already open; testnet adapter will not stack exposure",
                "position": position,
            }
        return {"ready": True, "block_reason": "", "position": position}

    def _position_snapshot(self) -> dict:
        payload = self._binance_signed_get("/fapi/v2/positionRisk", {"symbol": TESTNET_SYMBOL})
        item = payload[0] if isinstance(payload, list) and payload else (payload if isinstance(payload, dict) else {})
        return {
            "ok": bool(item),
            "symbol": item.get("symbol", TESTNET_SYMBOL),
            "positionAmt": item.get("positionAmt", "0"),
            "entryPrice": item.get("entryPrice", "0"),
            "unRealizedProfit": item.get("unRealizedProfit", "0"),
        }

    def _cancel_testnet_protective_orders(self) -> dict:
        result = {"status": "cancelled", "symbol": TESTNET_SYMBOL, "open_orders": {}, "algo_orders": {}}
        try:
            result["open_orders"] = self._delete_binance_open_orders(TESTNET_SYMBOL)
        except (OSError, TimeoutError, RuntimeError, urllib.error.HTTPError, json.JSONDecodeError, ValueError) as exc:
            result.update({"status": "failed", "open_orders_error": f"{type(exc).__name__}: {exc}"})
        if self._binance_uses_algo_protective_orders():
            try:
                result["algo_orders"] = self._delete_binance_algo_open_orders(TESTNET_SYMBOL)
            except (OSError, TimeoutError, RuntimeError, urllib.error.HTTPError, json.JSONDecodeError, ValueError) as exc:
                result.update({"status": "failed", "algo_orders_error": f"{type(exc).__name__}: {exc}"})
        return result

    def _write_testnet_close_report(self, run_date: str, report: dict) -> None:
        write_json(self.output_root / "testnet_position_closes" / "current.json", [report])
        path = self.output_root / "testnet_position_closes" / f"{run_date}.json"
        rows = load_json(path)
        rows.append(report)
        write_json(path, rows)

    def _reconcile_after_close(self, run_date: str) -> dict:
        max_retries = int(self.testnet_config.get("reconciliation_retries", 2))
        attempts = []
        report: dict = {}
        for attempt in range(max_retries + 1):
            report = LiveBrokerReconciliation(self.output_root, self.broker_config, opener=self.opener).run(run_date)
            attempts.append(
                {
                    "attempt": attempt + 1,
                    "confirmation_status": report.get("confirmation_status"),
                    "system_state": report.get("system_state"),
                    "error": report.get("error", ""),
                }
            )
            if report.get("confirmation_status") != "cannot_confirm":
                break
        report["attempts"] = attempts
        return report

    def _record_testnet_block(self, run_date: str, ticket_id: str, guard: dict, readiness: dict) -> None:
        path = self.output_root / str(self.broker_config.get("request_dir", "testnet_order_requests")) / f"{run_date}.json"
        rows = load_json(path)
        rows.append(
            {
                "requested_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "provider": "binance_usdm",
                "mode": "testnet",
                "run_date": run_date,
                "ticket_id": ticket_id,
                "status": "blocked",
                "guard": guard,
                "readiness": readiness,
            }
        )
        write_json(path, rows)

    def _testnet_close_id(self, run_date: str) -> str:
        digest = hashlib.sha256(f"{run_date}:{TESTNET_SYMBOL}:testnet-close".encode("utf-8")).hexdigest()[:18]
        return f"codex_xau_testnet_close_{digest}"[:36]
