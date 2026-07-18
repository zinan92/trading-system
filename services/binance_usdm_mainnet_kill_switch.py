from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from services.broker_adapter import LiveBrokerAdapter
from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import write_json
from services.live_money_guardrails import LiveHaltStore
from services.live_reconciliation import LiveBrokerReconciliation


MAINNET_BASE_URL = "https://fapi.binance.com"
MAINNET_SYMBOL = "XAUUSDT"


class BinanceUsdmMainnetKillSwitch:
    """Explicit operator kill switch for the attended Binance USDM mainnet canary.

    Dry-run by default. The destructive path requires both `confirm_mainnet_kill`
    and `mainnet_approved`, then persists HALT before cancel/close attempts.
    """

    def __init__(self, output_root: Path | None = None, opener=None) -> None:
        config = load_pipeline_config()
        self.config = config
        self.output_root = output_root or ROOT / config.get("output_root", "outputs")
        self.broker_config = self._broker_config(config)
        self.opener = opener or urllib.request.urlopen
        self.halt_store = LiveHaltStore(self.output_root)

    def run(
        self,
        run_date: str,
        *,
        confirm_mainnet_kill: bool = False,
        mainnet_approved: bool = False,
        reason: str = "operator_mainnet_kill_switch",
    ) -> dict:
        adapter = LiveBrokerAdapter(self.output_root, live_trading_enabled=True, broker_config=self.broker_config, opener=self.opener)
        report = {
            "run_date": run_date,
            "checked_at": _utcnow(),
            "provider": "binance_usdm",
            "mode": "mainnet",
            "base_url": adapter._binance_base_url(),
            "symbol": MAINNET_SYMBOL,
            "confirm_mainnet_kill": confirm_mainnet_kill,
            "mainnet_approved": mainnet_approved,
            "halt": self.halt_store.current(),
            "network_order_created": False,
            "status": "dry_run",
            "guard": {},
            "cancel_before": {},
            "positions_before": [],
            "close_orders": [],
            "cancel_after": {},
            "reconciliation_after": {},
            "confirmed_flat": False,
            "block_reason": "",
        }
        try:
            guard = self._mainnet_static_guard(adapter)
        except (OSError, TimeoutError, RuntimeError, urllib.error.HTTPError, json.JSONDecodeError, ValueError) as exc:
            guard = {"ready": False, "block_reason": f"{type(exc).__name__}: {exc}", "base_url": adapter._binance_base_url(), "symbol": MAINNET_SYMBOL}
        report["guard"] = guard
        if not confirm_mainnet_kill or not mainnet_approved:
            report["block_reason"] = "dry-run only; pass both confirm_mainnet_kill and mainnet_approved to cancel/close real Binance USDM mainnet exposure"
            self._write_report(run_date, report)
            return report

        report["halt"] = self.halt_store.activate(
            run_date,
            reason=reason,
            source="binance_usdm_mainnet_kill_switch",
            details={"mode": "mainnet", "symbol": MAINNET_SYMBOL, "confirm_mainnet_kill": confirm_mainnet_kill},
        )
        if not guard.get("ready"):
            report["status"] = "halt_active_guard_failed"
            report["block_reason"] = guard.get("block_reason") or "mainnet static guard failed"
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
                report["block_reason"] = "kill switch ran but reconciliation did not confirm flat and clear"
        except (OSError, TimeoutError, RuntimeError, urllib.error.HTTPError, json.JSONDecodeError, ValueError) as exc:
            report["status"] = "halt_active_failed"
            report["block_reason"] = f"{type(exc).__name__}: {exc}"
        self._write_report(run_date, report)
        return report

    def clear_halt(self, run_date: str, *, confirm_clear: bool = False, reason: str = "operator_cleared_mainnet_halt") -> dict:
        if not confirm_clear:
            return {
                "run_date": run_date,
                "status": "dry_run",
                "halt": self.halt_store.current(),
                "block_reason": "dry-run only; pass confirm_clear to clear persistent HALT",
            }
        record = self.halt_store.clear(run_date, reason=reason, source="binance_usdm_mainnet_kill_switch", operator="manual")
        result = {"run_date": run_date, "status": "halt_cleared", "halt": record}
        self._write_report(run_date, result)
        return result

    def _broker_config(self, config: dict) -> dict:
        broker = config.get("broker", {}) or {}
        return {
            **broker,
            "provider": "binance_usdm",
            "environment": "live",
            "base_url": MAINNET_BASE_URL,
            "dry_run": False,
            "request_dir": "live_order_requests",
            "protective_order_endpoint": "algoOrder",
            "reconcile_account_history": True,
            "instrument_map": {"GOLD": MAINNET_SYMBOL, "XAUUSD": MAINNET_SYMBOL, **broker.get("instrument_map", {})},
        }

    def _mainnet_static_guard(self, adapter: LiveBrokerAdapter) -> dict:
        readiness = adapter.preflight()
        symbol = adapter._binance_symbol("GOLD")
        reasons = []
        if adapter.provider != "binance_usdm":
            reasons.append(f"provider is {adapter.provider}, expected binance_usdm")
        if adapter._binance_base_url() != MAINNET_BASE_URL:
            reasons.append(f"base_url is {adapter._binance_base_url()}, expected {MAINNET_BASE_URL}")
        if symbol != MAINNET_SYMBOL:
            reasons.append(f"symbol is {symbol}, expected {MAINNET_SYMBOL}")
        if adapter.dry_run:
            reasons.append("dry_run is true; mainnet kill switch requires real reduce-only close capability")
        if readiness.get("exchange_status", {}).get("status") != "TRADING":
            reasons.append(f"exchange status is {readiness.get('exchange_status', {}).get('status')}")
        if readiness.get("missing_env"):
            reasons.append(f"missing Binance environment variables: {', '.join(readiness.get('missing_env', []))}")
        return {
            "ready": not reasons,
            "block_reason": "; ".join(reasons),
            "base_url": adapter._binance_base_url(),
            "symbol": symbol,
            "readiness": readiness,
            "checked_at": _utcnow(),
        }

    def _cancel_all(self, adapter: LiveBrokerAdapter) -> dict:
        result = {"status": "cancelled", "open_orders": {}, "algo_orders": {}}
        try:
            result["open_orders"] = adapter._delete_binance_open_orders(MAINNET_SYMBOL)
        except (OSError, TimeoutError, RuntimeError, urllib.error.HTTPError, json.JSONDecodeError, ValueError) as exc:
            result.update({"status": "failed", "open_orders_error": f"{type(exc).__name__}: {exc}"})
        try:
            result["algo_orders"] = adapter._delete_binance_algo_open_orders(MAINNET_SYMBOL)
        except (OSError, TimeoutError, RuntimeError, urllib.error.HTTPError, json.JSONDecodeError, ValueError) as exc:
            result.update({"status": "failed", "algo_orders_error": f"{type(exc).__name__}: {exc}"})
        return result

    def _open_positions(self, adapter: LiveBrokerAdapter) -> list[dict]:
        payload = adapter._binance_position_risk(MAINNET_SYMBOL)
        rows = payload if isinstance(payload, list) else [payload] if isinstance(payload, dict) else []
        positions = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            amount = float(row.get("positionAmt", 0) or 0)
            if amount:
                positions.append(row)
        return positions

    def _close_position(self, adapter: LiveBrokerAdapter, run_date: str, position: dict) -> dict:
        amount = float(position.get("positionAmt", 0) or 0)
        payload = {
            "symbol": MAINNET_SYMBOL,
            "side": "BUY" if amount < 0 else "SELL",
            "type": "MARKET",
            "quantity": adapter._format_decimal(abs(amount)),
            "reduceOnly": "true",
            "newOrderRespType": "RESULT",
            "newClientOrderId": self._kill_close_id(run_date, MAINNET_SYMBOL, amount),
        }
        result = {
            "symbol": MAINNET_SYMBOL,
            "position_amt": amount,
            "network_order_created": False,
            "status": "failed",
            "request": adapter._safe_order_payload(payload),
            "response": {},
            "error": {},
        }
        response = adapter._post_binance_order(payload)
        error = self._close_response_error(adapter, response, payload)
        result["response"] = response
        if error:
            result["error"] = error
            return result
        result.update(
            {
                "network_order_created": True,
                "status": "submitted",
                "fill_price": adapter._binance_fill_price(response) or float(position.get("entryPrice", 0) or 0),
                "quantity": adapter._binance_executed_quantity(response) or abs(amount),
            }
        )
        return result

    def _close_response_error(self, adapter: LiveBrokerAdapter, response: dict, payload: dict) -> dict:
        safe_payload = adapter._safe_order_payload(payload)
        if not isinstance(response, dict):
            return {"payload": safe_payload, "error_type": "InvalidCloseResponse", "message": "close response is not a JSON object", "response": response}
        if response.get("code") not in {None, "", 0, "0"}:
            return {"payload": safe_payload, "error_type": "BinanceCloseBodyError", "message": adapter._binance_error_message(response), "binance_error": response}
        terminal = adapter._binance_entry_terminal_state(response)
        if terminal:
            return {"payload": safe_payload, "error_type": "BinanceCloseTerminalState", "message": f"close order returned terminal state {terminal}", "response": adapter._safe_order_payload(response)}
        executed = adapter._binance_executed_quantity(response)
        status = str(response.get("status") or "").upper()
        if executed is None and status not in {"NEW", "PARTIALLY_FILLED", "FILLED"}:
            return {"payload": safe_payload, "error_type": "BinanceCloseUnverified", "message": f"close order status is {status or 'missing'} with no executed quantity", "response": adapter._safe_order_payload(response)}
        return {}

    def _confirm_flat(self, run_date: str, adapter: LiveBrokerAdapter) -> dict:
        report = LiveBrokerReconciliation(self.output_root, adapter.broker_config, opener=self.opener).run(run_date)
        return report

    def _write_report(self, run_date: str, report: dict) -> None:
        write_json(self.output_root / "mainnet_kill_switch" / "current.json", [report])
        path = self.output_root / "mainnet_kill_switch" / f"{run_date}.json"
        from services.journal_store import load_json

        rows = load_json(path)
        rows.append(report)
        write_json(path, rows)

    def _kill_close_id(self, run_date: str, symbol: str, amount: float) -> str:
        digest = hashlib.sha256(f"{run_date}:{symbol}:{amount}:mainnet-kill".encode("utf-8")).hexdigest()[:12]
        return f"codex_kill_{symbol.lower()}_{digest}"[:36]


def run_binance_usdm_mainnet_kill_switch(
    run_date: str,
    *,
    confirm_mainnet_kill: bool = False,
    mainnet_approved: bool = False,
    clear_halt: bool = False,
    confirm_clear: bool = False,
    output_root: Path | None = None,
) -> dict:
    service = BinanceUsdmMainnetKillSwitch(output_root)
    if clear_halt:
        return service.clear_halt(run_date, confirm_clear=confirm_clear)
    return service.run(run_date, confirm_mainnet_kill=confirm_mainnet_kill, mainnet_approved=mainnet_approved)


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
