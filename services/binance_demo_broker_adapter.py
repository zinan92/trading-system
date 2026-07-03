from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from schemas.market_data import PaperOrder
from services.broker_adapter import BrokerOrderRequest, LiveBrokerAdapter
from services.config_loader import load_pipeline_config
from services.journal_store import load_json, write_json
from services.live_reconciliation import LiveBrokerReconciliation
from services.live_env import apply_live_env, live_env_value_present
from services.paper_executor import PaperExecutor


DEMO_BASE_URL = "https://demo-fapi.binance.com"
DEMO_SYMBOL = "XAUUSDT"
_DEMO_FETCH_ERRORS = (OSError, urllib.error.URLError, TimeoutError, RuntimeError, json.JSONDecodeError, ValueError, KeyError)


class BinanceDemoBrokerAdapter(LiveBrokerAdapter):
    """Real Binance Futures Demo execution for one selected strategy.

    This intentionally does not loosen the real-money `LiveBrokerAdapter`
    activation gate. It only runs against Binance's demo endpoint, only for
    XAUUSDT, and it caps order size before calling the shared Binance order
    submission path.
    """

    name = "binance_demo"

    def __init__(self, output_root: Path, broker_config: dict, demo_config: dict | None = None, opener=None) -> None:
        self.demo_config = demo_config or {}
        merged = {
            **broker_config,
            "provider": "binance_usdm",
            "environment": "demo",
            "base_url": DEMO_BASE_URL,
            "dry_run": False,
            "request_dir": str(self.demo_config.get("request_dir", broker_config.get("request_dir", "demo_order_requests"))),
            "protective_failure_action": str(self.demo_config.get("protective_failure_action", "reduce_only_close")),
            "reconcile_account_history": bool(self.demo_config.get("reconcile_account_history", False)),
            "instrument_map": {"GOLD": DEMO_SYMBOL, "XAUUSD": DEMO_SYMBOL, **broker_config.get("instrument_map", {})},
        }
        super().__init__(output_root, live_trading_enabled=True, broker_config=merged, opener=opener)

    def submit_order(self, request: BrokerOrderRequest) -> PaperOrder:
        guard = self._demo_static_guard({})
        if not guard["ready"]:
            readiness = {"ready": False, "block_reason": guard["block_reason"]}
            self._record_demo_block(request, guard, readiness)
            raise RuntimeError(guard["block_reason"])
        readiness = self.preflight()
        guard = self._demo_static_guard(readiness)
        if not guard["ready"]:
            self._record_demo_block(request, guard, readiness)
            raise RuntimeError(guard["block_reason"])
        if not readiness.get("ready"):
            self._record_demo_block(request, guard, readiness)
            raise RuntimeError(f"Binance demo broker preflight failed: {readiness.get('block_reason')}")
        reconciliation = self._demo_reconciliation_check(request.run_date)
        readiness = {**readiness, "live_reconciliation": reconciliation.get("report", {})}
        if not reconciliation["ready"]:
            self._record_demo_block(request, reconciliation, readiness)
            raise RuntimeError(reconciliation["block_reason"])
        if bool(self.demo_config.get("require_flat_before_entry", True)):
            flat = self._flat_position_check()
            if not flat["ready"]:
                self._record_demo_block(request, flat, readiness)
                raise RuntimeError(flat["block_reason"])
            readiness = {**readiness, "position_precheck": flat}
        capped = self._with_capped_quantity(request, readiness)
        return self._submit_binance_order(capped, {**readiness, "mode": "demo", "demo_trading": True})

    def close_demo_position(self, run_date: str, *, confirm_close_demo_position: bool = False) -> dict:
        readiness: dict = {}
        guard = self._demo_static_guard(readiness)
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        report = {
            "run_date": run_date,
            "checked_at": now,
            "provider": "binance_usdm",
            "mode": "demo",
            "base_url": self._binance_base_url(),
            "symbol": DEMO_SYMBOL,
            "confirm_close_demo_position": confirm_close_demo_position,
            "network_order_created": False,
            "readiness": readiness,
            "guard": guard,
            "position": {},
            "close_request": {},
            "close_response": {},
            "protective_cancel": {},
            "reconciliation_after": {},
            "status": "dry_run",
        }
        if not guard["ready"]:
            report["status"] = "blocked"
            report["block_reason"] = guard["block_reason"]
            self._write_demo_close_report(run_date, report)
            return report
        readiness = self.preflight()
        guard = self._demo_static_guard(readiness)
        report["readiness"] = readiness
        report["guard"] = guard
        if not guard["ready"]:
            report["status"] = "blocked"
            report["block_reason"] = guard["block_reason"]
            self._write_demo_close_report(run_date, report)
            return report
        if not readiness.get("ready"):
            report["status"] = "blocked"
            report["block_reason"] = f"Binance demo broker preflight failed: {readiness.get('block_reason')}"
            self._write_demo_close_report(run_date, report)
            return report
        position = self._demo_position_snapshot()
        report["position"] = position
        if not position.get("ok"):
            report["status"] = "blocked"
            report["block_reason"] = "cannot read Binance demo position before close"
            self._write_demo_close_report(run_date, report)
            return report
        amount = float(position.get("positionAmt", 0) or 0)
        if amount == 0:
            report["status"] = "flat"
            report["block_reason"] = ""
            report["reconciliation_after"] = LiveBrokerReconciliation(self.output_root, self.broker_config, opener=self.opener).run(run_date)
            self._write_demo_close_report(run_date, report)
            return report
        close_payload = {
            "symbol": DEMO_SYMBOL,
            "side": "BUY" if amount < 0 else "SELL",
            "type": "MARKET",
            "quantity": self._format_decimal(abs(amount)),
            "reduceOnly": "true",
            "newClientOrderId": self._demo_close_id(run_date),
        }
        report["close_request"] = self._safe_order_payload(close_payload)
        if not confirm_close_demo_position:
            report["status"] = "dry_run"
            report["block_reason"] = "dry-run only; pass --confirm-close-demo-position to submit reduce-only demo close"
            self._write_demo_close_report(run_date, report)
            return report
        try:
            report["close_response"] = self._post_binance_order(close_payload)
            report["network_order_created"] = True
            report["status"] = "submitted"
            report["protective_cancel"] = self._cancel_demo_protective_orders()
            if report["protective_cancel"].get("status") == "failed":
                report["status"] = "protective_cancel_failed"
                report["block_reason"] = "demo position closed but protective order cancel failed"
            report["close_fill"] = self._resolve_demo_close_fill(run_date, close_payload, report["close_response"])
            report["local_mirror"] = self._sync_demo_close_local_mirror(run_date, close_payload, report["close_fill"])
        except (OSError, TimeoutError, RuntimeError, urllib.error.HTTPError, json.JSONDecodeError, ValueError) as exc:
            report["status"] = "failed"
            report["block_reason"] = f"{type(exc).__name__}: {exc}"
        report["reconciliation_after"] = LiveBrokerReconciliation(self.output_root, self.broker_config, opener=self.opener).run(run_date)
        self._write_demo_close_report(run_date, report)
        return report

    def _demo_static_guard(self, readiness: dict) -> dict:
        symbol = self._binance_symbol("GOLD")
        reasons = []
        if self.provider != "binance_usdm":
            reasons.append(f"provider is {self.provider}, expected binance_usdm")
        if self._binance_base_url() != DEMO_BASE_URL:
            reasons.append(f"base_url is {self._binance_base_url()}, expected {DEMO_BASE_URL}")
        if symbol != DEMO_SYMBOL:
            reasons.append(f"symbol is {symbol}, expected {DEMO_SYMBOL}")
        if self.dry_run:
            reasons.append("dry_run is true; demo strategy requires real demo exchange orders")
        if readiness.get("exchange_status", {}).get("status") not in {"TRADING", None}:
            reasons.append(f"exchange status is {readiness.get('exchange_status', {}).get('status')}")
        return {
            "ready": not reasons,
            "block_reason": "; ".join(reasons),
            "base_url": self._binance_base_url(),
            "symbol": symbol,
            "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        }

    def _with_capped_quantity(self, request: BrokerOrderRequest, readiness: dict) -> BrokerOrderRequest:
        ticket = request.ticket
        requested_price = float(request.latest_price or self._entry_midpoint(ticket["entry_zone"]))
        raw_quantity = float(request.actual_size or self._quantity(ticket, requested_price) or 0)
        max_quantity = float(self.demo_config.get("max_order_quantity", 0.002))
        quantity = raw_quantity if raw_quantity > 0 else max_quantity
        quantity = min(quantity, max_quantity)
        filters = readiness.get("exchange_status", {}).get("filters", {})
        quantity = self._round_step(quantity, str(filters.get("step_size", "0.001")))
        if quantity <= 0:
            raise RuntimeError("Binance demo order quantity is zero after cap and step-size rounding")
        return replace(request, actual_size=quantity)

    def _flat_position_check(self) -> dict:
        symbol = self._binance_symbol("GOLD")
        response = self._binance_signed_get("/fapi/v2/positionRisk", {"symbol": symbol})
        if not response.get("ok"):
            return {
                "ready": False,
                "block_reason": "cannot read Binance demo position before strategy order",
                "response": self._safe_error(response),
            }
        item = self._position_item(response.get("body"))
        amount = float(item.get("positionAmt", 0) or 0)
        if amount != 0:
            return {
                "ready": False,
                "block_reason": "XAUUSDT demo position is already open; strategy demo adapter will not stack exposure",
                "position": self._safe_position_item(item),
            }
        return {
            "ready": True,
            "block_reason": "",
            "position": self._safe_position_item(item),
        }

    def _demo_reconciliation_check(self, run_date: str) -> dict:
        report = LiveBrokerReconciliation(self.output_root, self.broker_config, opener=self.opener).run(run_date)
        if report.get("suspected_naked_position"):
            return {
                "ready": False,
                "block_reason": f"Binance demo suspected naked position: {report.get('escalation_action') or report.get('reason_code')}",
                "report": report,
            }
        if report.get("confirmation_status") == "cannot_confirm":
            return {
                "ready": False,
                "block_reason": f"Binance demo reconciliation cannot confirm venue state: {report.get('error')}",
                "report": report,
            }
        if report.get("error"):
            return {
                "ready": False,
                "block_reason": f"Binance demo reconciliation failed: {report['error']}",
                "report": report,
            }
        if report.get("drift_count", 0):
            reasons = sorted({str(item.get("reason", "reconciliation drift")) for item in report.get("drifts", [])})
            return {
                "ready": False,
                "block_reason": "Binance demo reconciliation drift: " + "; ".join(reasons),
                "report": report,
            }
        return {"ready": True, "block_reason": "", "report": report}

    def _demo_position_snapshot(self) -> dict:
        response = self._binance_signed_get("/fapi/v2/positionRisk", {"symbol": DEMO_SYMBOL})
        if not response.get("ok"):
            return {"ok": False, "response": self._safe_error(response)}
        item = self._position_item(response.get("body"))
        return {"ok": True, **self._safe_position_item(item)}

    def _binance_signed_get(self, endpoint: str, params: dict) -> dict:
        apply_live_env()
        api_key_env = str(self.broker_config.get("api_key_env", "BINANCE_API_KEY"))
        secret_env = str(self.broker_config.get("api_secret_env", "BINANCE_API_SECRET"))
        api_key = os.getenv(api_key_env)
        api_secret = os.getenv(secret_env)
        if not live_env_value_present(api_key_env) or not live_env_value_present(secret_env):
            return {"ok": False, "status": None, "error": {"message": f"missing Binance environment variables: {api_key_env}, {secret_env}"}}
        signed = {**params, "timestamp": int(time.time() * 1000), "recvWindow": int(self.broker_config.get("recv_window_ms", 5000))}
        query = urllib.parse.urlencode(signed)
        signature = hmac.new(str(api_secret).encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
        request = urllib.request.Request(
            f"{self._binance_base_url()}{endpoint}?{query}&signature={signature}",
            method="GET",
            headers={"X-MBX-APIKEY": str(api_key), "User-Agent": "TradingOrchestrator/1.0"},
        )
        try:
            with self.opener(request, timeout=int(self.broker_config.get("timeout_seconds", 10))) as response:
                text = response.read().decode("utf-8")
                return {"ok": True, "status": getattr(response, "status", 200), "body": json.loads(text) if text else {}}
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", errors="replace")
            try:
                error = json.loads(text)
            except json.JSONDecodeError:
                error = {"raw": text[:500]}
            return {"ok": False, "status": exc.code, "error": error}
        except (OSError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            return {"ok": False, "status": None, "error": {"error_type": type(exc).__name__, "message": str(exc)}}

    def _position_item(self, body) -> dict:
        if isinstance(body, list):
            return body[0] if body else {}
        return body if isinstance(body, dict) else {}

    def _safe_position_item(self, item: dict) -> dict:
        return {
            "symbol": item.get("symbol", DEMO_SYMBOL),
            "positionAmt": item.get("positionAmt", "0"),
            "entryPrice": item.get("entryPrice", "0"),
            "unRealizedProfit": item.get("unRealizedProfit", "0"),
        }

    def _write_demo_close_report(self, run_date: str, report: dict) -> None:
        write_json(self.output_root / "demo_position_closes" / "current.json", [report])
        path = self.output_root / "demo_position_closes" / f"{run_date}.json"
        rows = load_json(path)
        rows.append(report)
        write_json(path, rows)

    def _resolve_demo_close_fill(self, run_date: str, close_payload: dict, close_response: dict) -> dict:
        direct = self._close_fill_from_order_payload(close_response, source="close_response")
        if direct.get("filled"):
            return direct
        status_payload = self._binance_signed_get(
            "/fapi/v1/order",
            {"symbol": DEMO_SYMBOL, "origClientOrderId": str(close_payload.get("newClientOrderId") or "")},
        )
        if status_payload.get("ok") and isinstance(status_payload.get("body"), dict):
            status_fill = self._close_fill_from_order_payload(status_payload["body"], source="order_status")
            if status_fill.get("filled"):
                return status_fill
        history_fill = self._close_fill_from_trade_history(run_date, close_payload, close_response)
        if history_fill.get("filled"):
            return history_fill
        return {
            "filled": False,
            "status": "not_confirmed",
            "order_id": str(close_response.get("orderId") or ""),
            "client_order_id": str(close_payload.get("newClientOrderId") or ""),
            "quantity": 0.0,
            "price": 0.0,
            "sources_checked": ["close_response", "order_status", "userTrades"],
        }

    def _close_fill_from_order_payload(self, payload: dict, *, source: str) -> dict:
        if not isinstance(payload, dict):
            return {"filled": False, "source": source}
        quantity = self._binance_executed_quantity(payload) or 0.0
        price = self._binance_fill_price(payload) or 0.0
        return {
            "filled": quantity > 0 and price > 0,
            "status": str(payload.get("status") or ""),
            "source": source,
            "order_id": str(payload.get("orderId") or ""),
            "client_order_id": str(payload.get("clientOrderId") or ""),
            "side": str(payload.get("side") or ""),
            "quantity": quantity,
            "price": price,
        }

    def _close_fill_from_trade_history(self, run_date: str, close_payload: dict, close_response: dict) -> dict:
        order_id = str(close_response.get("orderId") or "")
        try:
            fills = LiveBrokerReconciliation(self.output_root, self.broker_config, opener=self.opener).exchange_fills(DEMO_SYMBOL, run_date)
        except _DEMO_FETCH_ERRORS as exc:
            return {"filled": False, "source": "userTrades", "error": f"{type(exc).__name__}: {exc}"}
        matching = [
            item
            for item in fills
            if str(item.get("order_id") or "") == order_id and str(item.get("side") or "").upper() == str(close_payload.get("side") or "").upper()
        ]
        if not matching:
            return {"filled": False, "source": "userTrades", "order_id": order_id}
        quantity = sum(float(item.get("qty", 0) or 0) for item in matching)
        quote_qty = sum(float(item.get("quote_qty", 0) or 0) for item in matching)
        price = quote_qty / quantity if quantity > 0 else 0.0
        return {
            "filled": quantity > 0 and price > 0,
            "status": "FILLED",
            "source": "userTrades",
            "order_id": order_id,
            "client_order_id": str(close_payload.get("newClientOrderId") or ""),
            "side": str(close_payload.get("side") or ""),
            "quantity": quantity,
            "price": price,
            "commission": round(sum(float(item.get("commission", 0) or 0) for item in matching), 8),
            "realized_pnl": round(sum(float(item.get("realized_pnl", 0) or 0) for item in matching), 8),
        }

    def _sync_demo_close_local_mirror(self, run_date: str, close_payload: dict, close_fill: dict) -> dict:
        if not close_fill.get("filled"):
            return {"status": "skipped", "reason": "close fill was not confirmed", "close_fill": close_fill}
        roots = self._demo_local_mirror_roots()
        results = []
        for root in roots:
            results.append(self._sync_demo_close_root(root, run_date, close_payload, close_fill))
        closed = [item for item in results if item.get("closed")]
        return {"status": "closed" if closed else "no_open_trade", "roots": results}

    def _demo_local_mirror_roots(self) -> list[Path]:
        roots = [self.output_root]
        try:
            demo = load_pipeline_config().get("demo_trading", {}) or {}
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            demo = {}
        active_strategy_id = str(demo.get("active_strategy_id") or "")
        if active_strategy_id:
            roots.append(self.output_root / "strategies" / active_strategy_id)
        unique: list[Path] = []
        seen: set[str] = set()
        for root in roots:
            resolved = str(Path(root))
            if resolved in seen:
                continue
            seen.add(resolved)
            unique.append(Path(root))
        return unique

    def _sync_demo_close_root(self, root: Path, run_date: str, close_payload: dict, close_fill: dict) -> dict:
        trades_path = root / "paper_trades" / "current.json"
        trades = load_json(trades_path)
        if not trades:
            return {"root": str(root), "closed": False, "status": "no_open_trade"}
        closing_side = str(close_payload.get("side") or "").upper()
        closing_short = closing_side == "BUY"
        expected_side = "short" if closing_short else "long"
        candidates = [
            item
            for item in trades
            if item.get("status", "open") == "open"
            and str(item.get("symbol") or "GOLD") == "GOLD"
            and str(item.get("side") or "") == expected_side
            and "exchange_managed" in (item.get("quality_flags") or [])
        ]
        if not candidates:
            return {"root": str(root), "closed": False, "status": "no_matching_open_trade", "expected_side": expected_side}
        quantity = float(close_fill.get("quantity", 0) or 0)
        quantity_matches = [
            item for item in candidates if abs(float(item.get("quantity", 0) or 0) - quantity) <= 1e-8
        ]
        candidates = quantity_matches or candidates
        if len(candidates) != 1:
            return {
                "root": str(root),
                "closed": False,
                "status": "ambiguous_open_trade",
                "candidate_order_ids": [str(item.get("order_id") or "") for item in candidates],
            }
        trade = candidates[0]
        local = PaperExecutor(root).record_external_close(
            run_date,
            order_id=str(trade.get("order_id") or ""),
            exit_price=float(close_fill.get("price") or 0),
            quantity=quantity,
            exit_reason="demo_reduce_only_close",
            close_order_id=str(close_fill.get("order_id") or close_fill.get("client_order_id") or ""),
        )
        reconciliation = {}
        if local.get("closed"):
            try:
                reconciliation = LiveBrokerReconciliation(root, self.broker_config, opener=self.opener).run(run_date)
            except _DEMO_FETCH_ERRORS as exc:
                reconciliation = {"error": f"{type(exc).__name__}: {exc}"}
        return {
            "root": str(root),
            "closed": bool(local.get("closed")),
            "status": "closed" if local.get("closed") else "failed",
            "local": local,
            "reconciliation": {
                key: reconciliation.get(key)
                for key in ("reconciled", "confirmation_status", "system_state", "error", "drift_count")
                if key in reconciliation
            },
        }

    def _cancel_demo_protective_orders(self) -> dict:
        try:
            response = self._delete_binance_open_orders(DEMO_SYMBOL)
            return {"status": "cancelled", "symbol": DEMO_SYMBOL, "response": response}
        except (OSError, TimeoutError, RuntimeError, urllib.error.HTTPError, json.JSONDecodeError, ValueError) as exc:
            return {"status": "failed", "symbol": DEMO_SYMBOL, "error_type": type(exc).__name__, "message": str(exc)}

    def _demo_close_id(self, run_date: str) -> str:
        digest = hashlib.sha256(f"{run_date}:{DEMO_SYMBOL}:demo-close".encode("utf-8")).hexdigest()[:20]
        return f"demo_close_{digest}"[:36]

    def _safe_error(self, response: dict) -> dict:
        error = response.get("error") or {}
        return {
            "ok": False,
            "status": response.get("status"),
            "error": {key: error.get(key) for key in ["code", "msg", "error_type", "message", "raw"] if error.get(key) is not None},
        }

    def _record_demo_block(self, request: BrokerOrderRequest, guard: dict, readiness: dict) -> None:
        path = self.output_root / str(self.broker_config.get("request_dir", "demo_order_requests")) / f"{request.run_date}.json"
        rows = load_json(path)
        rows.append(
            {
                "requested_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "provider": "binance_usdm",
                "mode": "demo",
                "run_date": request.run_date,
                "ticket_id": request.ticket.get("ticket_id"),
                "status": "blocked",
                "guard": guard,
                "readiness": readiness,
            }
        )
        write_json(path, rows)
