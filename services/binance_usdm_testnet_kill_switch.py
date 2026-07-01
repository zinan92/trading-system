from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from services.binance_usdm_testnet_broker_adapter import BinanceUsdmTestnetBrokerAdapter, TESTNET_BASE_URL, TESTNET_SYMBOL
from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json
from services.live_money_guardrails import LiveHaltStore
from services.live_reconciliation import LiveBrokerReconciliation
from services.paper_executor import PaperExecutor


class BinanceUsdmTestnetKillSwitch:
    def __init__(self, output_root: Path | None = None, opener=None) -> None:
        config = load_pipeline_config()
        self.config = config
        self.output_root = output_root or ROOT / config.get("output_root", "outputs")
        self.broker_config = self._broker_config(config)
        self.testnet_config = config.get("testnet_trading", {}) or {}
        self.opener = opener or urllib.request.urlopen
        self.halt_store = LiveHaltStore(self.output_root)

    def run(self, run_date: str, *, confirm_testnet_kill: bool = False, reason: str = "operator_testnet_kill_switch") -> dict:
        checked_at = _utcnow()
        adapter = BinanceUsdmTestnetBrokerAdapter(self.output_root, self.broker_config, self.testnet_config, opener=self.opener)
        report = {
            "run_date": run_date,
            "checked_at": checked_at,
            "provider": "binance_usdm",
            "mode": "testnet",
            "base_url": adapter._binance_base_url(),
            "symbol": TESTNET_SYMBOL,
            "confirm_testnet_kill": confirm_testnet_kill,
            "halt": self.halt_store.current(),
            "network_order_created": False,
            "status": "dry_run",
            "cancel_before": {},
            "positions_before": [],
            "close_orders": [],
            "local_mirrors": [],
            "cancel_after": {},
            "reconciliation_after": {},
            "confirmed_flat": False,
            "block_reason": "",
        }
        if confirm_testnet_kill:
            report["halt"] = self.halt_store.activate(
                run_date,
                reason=reason,
                source="binance_usdm_testnet_kill_switch",
                details={"mode": "testnet", "symbol": TESTNET_SYMBOL, "confirm_testnet_kill": confirm_testnet_kill},
            )
        guard = adapter._testnet_static_guard({"exchange_status": {}})
        report["guard"] = guard
        if not guard.get("ready"):
            report["status"] = "blocked_halt_active"
            report["block_reason"] = guard.get("block_reason") or "testnet static guard failed"
            self._write_report(run_date, report)
            return report
        if not confirm_testnet_kill:
            report["block_reason"] = "dry-run only; pass confirm_testnet_kill to close real Binance USDM testnet positions"
            self._write_report(run_date, report)
            return report

        try:
            report["cancel_before"] = self._cancel_all(adapter)
            positions = self._open_positions(adapter)
            report["positions_before"] = positions
            for position in positions:
                close = self._close_position(adapter, run_date, position)
                report["close_orders"].append(close)
                if close.get("network_order_created"):
                    report["network_order_created"] = True
                    report["local_mirrors"].extend(self._mirror_local_closes(run_date, position, close))
            report["cancel_after"] = self._cancel_all(adapter)
            report["reconciliation_after"] = self._confirm_flat(run_date, adapter)
            report["confirmed_flat"] = (
                report["reconciliation_after"].get("confirmation_status") == "confirmed_flat"
                and not report["reconciliation_after"].get("exchange_open_orders", [])
            )
            if report["confirmed_flat"]:
                report["status"] = "halted_flat_confirmed"
            else:
                report["status"] = "halt_active_not_flat"
                report["block_reason"] = "kill switch submitted actions but reconciliation did not confirm flat and clear"
        except (OSError, TimeoutError, RuntimeError, urllib.error.HTTPError, json.JSONDecodeError, ValueError) as exc:
            report["status"] = "halt_active_failed"
            report["block_reason"] = f"{type(exc).__name__}: {exc}"
        self._write_report(run_date, report)
        return report

    def clear_halt(self, run_date: str, *, confirm_clear: bool = False, reason: str = "operator_cleared_testnet_halt") -> dict:
        if not confirm_clear:
            return {
                "run_date": run_date,
                "status": "dry_run",
                "halt": self.halt_store.current(),
                "block_reason": "dry-run only; pass confirm_clear to clear persistent HALT",
            }
        record = self.halt_store.clear(run_date, reason=reason, source="binance_usdm_testnet_kill_switch", operator="manual")
        result = {"run_date": run_date, "status": "halt_cleared", "halt": record}
        self._write_report(run_date, result)
        return result

    def _broker_config(self, config: dict) -> dict:
        profiles = config.get("broker_profiles", {}) or {}
        profile = profiles.get("binance_usdm_testnet") or {}
        broker = config.get("broker", {}) or {}
        return {**broker, **profile, "base_url": TESTNET_BASE_URL, "environment": "testnet", "dry_run": False}

    def _cancel_all(self, adapter: BinanceUsdmTestnetBrokerAdapter) -> dict:
        result = {"status": "cancelled", "open_orders": {}, "algo_orders": {}}
        try:
            result["open_orders"] = adapter._delete_binance_open_orders(TESTNET_SYMBOL)
        except (OSError, TimeoutError, RuntimeError, urllib.error.HTTPError, json.JSONDecodeError, ValueError) as exc:
            result.update({"status": "failed", "open_orders_error": f"{type(exc).__name__}: {exc}"})
        try:
            result["algo_orders"] = adapter._delete_binance_algo_open_orders(TESTNET_SYMBOL)
        except (OSError, TimeoutError, RuntimeError, urllib.error.HTTPError, json.JSONDecodeError, ValueError) as exc:
            result.update({"status": "failed", "algo_orders_error": f"{type(exc).__name__}: {exc}"})
        return result

    def _open_positions(self, adapter: BinanceUsdmTestnetBrokerAdapter) -> list[dict]:
        payload = adapter._binance_signed_get("/fapi/v2/positionRisk", {"symbol": TESTNET_SYMBOL})
        rows = payload if isinstance(payload, list) else [payload] if isinstance(payload, dict) else []
        positions = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            amount = float(row.get("positionAmt", 0) or 0)
            if amount:
                positions.append(row)
        return positions

    def _close_position(self, adapter: BinanceUsdmTestnetBrokerAdapter, run_date: str, position: dict) -> dict:
        amount = float(position.get("positionAmt", 0) or 0)
        payload = {
            "symbol": TESTNET_SYMBOL,
            "side": "BUY" if amount < 0 else "SELL",
            "type": "MARKET",
            "quantity": adapter._format_decimal(abs(amount)),
            "reduceOnly": "true",
            "newOrderRespType": "RESULT",
            "newClientOrderId": self._kill_close_id(run_date, TESTNET_SYMBOL, amount),
        }
        response = adapter._post_binance_order(payload)
        return {
            "symbol": TESTNET_SYMBOL,
            "position_amt": amount,
            "network_order_created": True,
            "request": adapter._safe_order_payload(payload),
            "response": response,
            "fill_price": adapter._binance_fill_price(response) or float(position.get("entryPrice", 0) or 0),
            "quantity": adapter._binance_executed_quantity(response) or abs(amount),
        }

    def _mirror_local_closes(self, run_date: str, position: dict, close: dict) -> list[dict]:
        trades = load_json(self.output_root / "paper_trades" / "current.json")
        matching = [
            item
            for item in trades
            if isinstance(item, dict)
            and item.get("status", "open") == "open"
            and str(item.get("symbol") or "GOLD") == "GOLD"
            and "exchange_managed" in (item.get("quality_flags") or [])
        ]
        mirrors = []
        for trade in matching:
            mirrors.append(
                PaperExecutor(self.output_root).record_external_close(
                    run_date,
                    order_id=str(trade.get("order_id") or ""),
                    exit_price=float(close.get("fill_price") or position.get("entryPrice") or 0),
                    quantity=float(trade.get("quantity", 0) or 0),
                    exit_reason="operator_testnet_kill_switch",
                    close_order_id=str(close.get("response", {}).get("orderId") or close.get("response", {}).get("clientOrderId") or ""),
                )
            )
        return mirrors

    def _confirm_flat(self, run_date: str, adapter: BinanceUsdmTestnetBrokerAdapter) -> dict:
        max_retries = int(self.testnet_config.get("reconciliation_retries", 2))
        attempts = []
        report: dict = {}
        for attempt in range(max_retries + 1):
            report = LiveBrokerReconciliation(self.output_root, adapter.broker_config, opener=self.opener).run(run_date)
            attempts.append(
                {
                    "attempt": attempt + 1,
                    "confirmation_status": report.get("confirmation_status"),
                    "system_state": report.get("system_state"),
                    "open_orders": len(report.get("exchange_open_orders", []) or []),
                    "error": report.get("error", ""),
                }
            )
            if report.get("confirmation_status") == "confirmed_flat" and not report.get("exchange_open_orders", []):
                break
        report["attempts"] = attempts
        return report

    def _write_report(self, run_date: str, report: dict) -> None:
        write_json(self.output_root / "testnet_kill_switch" / "current.json", [report])
        path = self.output_root / "testnet_kill_switch" / f"{run_date}.json"
        rows = load_json(path)
        rows.append(report)
        write_json(path, rows)

    def _kill_close_id(self, run_date: str, symbol: str, amount: float) -> str:
        digest = hashlib.sha256(f"{run_date}:{symbol}:{amount}:testnet-kill".encode("utf-8")).hexdigest()[:12]
        return f"codex_kill_{symbol.lower()}_{digest}"[:36]


def run_binance_usdm_testnet_kill_switch(
    run_date: str,
    *,
    confirm_testnet_kill: bool = False,
    clear_halt: bool = False,
    confirm_clear: bool = False,
    output_root: Path | None = None,
) -> dict:
    service = BinanceUsdmTestnetKillSwitch(output_root)
    if clear_halt:
        return service.clear_halt(run_date, confirm_clear=confirm_clear)
    return service.run(run_date, confirm_testnet_kill=confirm_testnet_kill)


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
