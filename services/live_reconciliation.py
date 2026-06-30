"""Binance USDM exchange-truth reconciliation — the safety floor for real-money
trading: fetch the ACTUAL positions + balance from the exchange and compare them
to the local account. Any drift (a position the exchange has but we don't, or a
quantity mismatch) means our books and the broker disagree — which on real money
must page/halt, not be discovered by a human later.

Pure read-only (signed GET); it never places or cancels an order.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import load_pipeline_config
from services.journal_store import load_json, write_json
from services.live_env import apply_live_env, live_env_value_present
from services.order_lifecycle import OrderLifecycleStore

_QTY_TOLERANCE = 1e-8
_FETCH_ERRORS = (OSError, urllib.error.URLError, TimeoutError, RuntimeError, json.JSONDecodeError, ValueError, KeyError)


class LiveBrokerReconciliation:
    def __init__(self, output_root: Path, broker_config: dict | None = None, opener=None) -> None:
        self.output_root = Path(output_root)
        self.broker_config = broker_config if broker_config is not None else load_pipeline_config().get("broker", {})
        self.opener = opener or urllib.request.urlopen

    def exchange_positions(self) -> list[dict]:
        payload = self._signed_get("/fapi/v2/positionRisk", {})
        positions = []
        for row in payload:
            amount = float(row.get("positionAmt", 0) or 0)
            if amount == 0:
                continue
            positions.append({
                "symbol": str(row.get("symbol", "")),
                "position_amt": amount,
                "entry_price": float(row.get("entryPrice", 0) or 0),
                "unrealized_pnl": float(row.get("unRealizedProfit", 0) or 0),
            })
        return positions

    def exchange_balance(self) -> dict:
        payload = self._signed_get("/fapi/v2/balance", {})
        asset = str(self.broker_config.get("margin_asset", "USDT"))
        row = next((item for item in payload if item.get("asset") == asset), {})
        return {"asset": asset, "balance": float(row.get("balance", 0) or 0), "available": float(row.get("availableBalance", 0) or 0)}

    def exchange_open_orders(self) -> list[dict]:
        payload = self._signed_get("/fapi/v1/openOrders", {})
        orders = []
        for row in payload:
            if not isinstance(row, dict):
                continue
            orders.append(
                {
                    "symbol": str(row.get("symbol", "")),
                    "order_id": str(row.get("orderId", "")),
                    "client_order_id": str(row.get("clientOrderId", "")),
                    "type": str(row.get("type", "")),
                    "side": str(row.get("side", "")),
                    "orig_qty": float(row.get("origQty", row.get("quantity", 0)) or 0),
                    "reduce_only": str(row.get("reduceOnly", row.get("reduce_only", ""))).lower() == "true",
                    "close_position": str(row.get("closePosition", row.get("close_position", ""))).lower() == "true",
                    "status": str(row.get("status", "")),
                }
            )
        return orders

    def local_positions(self) -> dict:
        path = self.output_root / "paper_positions" / "current.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def run(self, run_date: str) -> dict:
        error = ""
        exchange_positions: list[dict] = []
        exchange_open_orders: list[dict] = []
        balance: dict = {}
        try:
            exchange_positions = self.exchange_positions()
            balance = self.exchange_balance()
            exchange_open_orders = self.exchange_open_orders()
        except _FETCH_ERRORS as exc:
            error = f"{type(exc).__name__}: {exc}"

        local = self.local_positions()
        instrument_map = self.broker_config.get("instrument_map", {"GOLD": "XAUUSDT"})
        submitting_intents = self._submitting_lifecycle_intents(run_date, instrument_map)
        exchange_by_symbol = {p["symbol"]: p for p in exchange_positions}
        submitting_by_symbol: dict[str, list[dict]] = {}
        for intent in submitting_intents:
            submitting_by_symbol.setdefault(str(intent.get("exchange_symbol") or "").upper(), []).append(intent)
        matched: set[str] = set()
        drifts: list[dict] = []

        if not error:
            for symbol, position in local.items():
                exchange_symbol = str(instrument_map.get(symbol, symbol)).upper()
                if position.get("net_quantity") is not None:
                    local_signed = float(position.get("net_quantity", 0) or 0)
                else:
                    local_signed = float(position.get("quantity", 0) or 0) * (1 if str(position.get("side", "long")) == "long" else -1)
                exchange = exchange_by_symbol.get(exchange_symbol)
                exchange_qty = exchange["position_amt"] if exchange else 0.0
                if exchange:
                    matched.add(exchange_symbol)
                if abs(local_signed - exchange_qty) > _QTY_TOLERANCE:
                    drifts.append({
                        "symbol": symbol,
                        "exchange_symbol": exchange_symbol,
                        "local_qty": local_signed,
                        "exchange_qty": exchange_qty,
                        "reason": "quantity mismatch" if exchange else "local position has no exchange match",
                    })

            for exchange_symbol, exchange in exchange_by_symbol.items():
                if exchange_symbol not in matched:
                    intents = submitting_by_symbol.get(exchange_symbol, [])
                    drift = {
                        "symbol": "",
                        "exchange_symbol": exchange_symbol,
                        "local_qty": 0.0,
                        "exchange_qty": exchange["position_amt"],
                        "reason": "exchange position has no local record",
                    }
                    if intents:
                        drift.update(
                            {
                                "reason": "suspected naked position: exchange position exists while local order is still submitting",
                                "reason_code": "naked_position_suspected",
                                "naked_position_risk": True,
                                "submitting_intents": intents,
                            }
                        )
                    drifts.append(drift)

            drifts.extend(self._protective_order_drifts(exchange_open_orders, local, instrument_map))

        suspected_naked = any(bool(item.get("naked_position_risk")) for item in drifts)
        if error and submitting_intents:
            suspected_naked = True
        confirmation_status = self._confirmation_status(error, drifts, exchange_positions)
        reason_code = self._reason_code(confirmation_status, suspected_naked)
        system_state = self._system_state(confirmation_status, suspected_naked)

        report = {
            "run_date": run_date,
            "provider": "binance_usdm",
            "reconciled": (not error) and (not drifts),
            "confirmation_status": confirmation_status,
            "system_state": system_state,
            "reason_code": reason_code,
            "blocks_new_orders": confirmation_status != "confirmed_flat",
            "flat_confirmed": confirmation_status == "confirmed_flat",
            "can_open_new_orders": confirmation_status == "confirmed_flat",
            "suspected_naked_position": suspected_naked,
            "naked_position_risks": submitting_intents if error and submitting_intents else [item for item in drifts if item.get("naked_position_risk")],
            "escalation_action": self._escalation_action(confirmation_status, suspected_naked),
            "error": error,
            "drift_count": len(drifts),
            "drifts": drifts,
            "exchange_positions": exchange_positions,
            "exchange_open_orders": exchange_open_orders,
            "exchange_balance": balance,
            "local_positions": local,
            "known_protective_order_ids": sorted(self._known_protective_order_ids()),
            "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        }
        if report["reconciled"] and not exchange_positions:
            OrderLifecycleStore(self.output_root).mark_closed_orders_reconciled(
                run_date,
                reason="external_exchange_reconciliation_flat",
                metadata={"provider": "binance_usdm", "truth_source": "exchange_positionRisk_and_openOrders"},
            )
        write_json(self.output_root / "live_reconciliation" / "current.json", [report])
        write_json(self.output_root / "live_reconciliation" / f"{run_date}.json", [report])
        return report

    def _confirmation_status(self, error: str, drifts: list[dict], exchange_positions: list[dict]) -> str:
        if error:
            return "cannot_confirm"
        if drifts:
            return "confirmed_drift"
        if exchange_positions:
            return "confirmed_open"
        return "confirmed_flat"

    def _reason_code(self, confirmation_status: str, suspected_naked: bool) -> str:
        if suspected_naked:
            return "naked_position_suspected"
        if confirmation_status == "cannot_confirm":
            return "venue_state_unknown"
        if confirmation_status == "confirmed_drift":
            return "reconciliation_drift"
        if confirmation_status == "confirmed_open":
            return "position_confirmed_open"
        return "confirmed_flat"

    def _system_state(self, confirmation_status: str, suspected_naked: bool) -> str:
        if confirmation_status == "cannot_confirm":
            return "BLOCKED_RECONCILIATION_UNKNOWN"
        if suspected_naked:
            return "BLOCKED_NAKED_POSITION_SUSPECTED"
        if confirmation_status == "confirmed_drift":
            return "BLOCKED_RECONCILIATION_DRIFT"
        if confirmation_status == "confirmed_open":
            return "PAUSED_POSITION_LIMIT"
        return "READY"

    def _escalation_action(self, confirmation_status: str, suspected_naked: bool) -> str:
        if suspected_naked:
            return "halt_new_orders_and_verify_or_flatten_suspected_naked_position"
        if confirmation_status == "cannot_confirm":
            return "halt_new_orders_until_exchange_state_can_be_confirmed"
        if confirmation_status == "confirmed_drift":
            return "halt_new_orders_until_reconciliation_drift_resolved"
        return ""

    def _protective_order_drifts(self, exchange_open_orders: list[dict], local: dict, instrument_map: dict) -> list[dict]:
        known_ids = self._known_protective_order_ids()
        local_exchange_symbols = {str(instrument_map.get(symbol, symbol)).upper() for symbol in local}
        drifts = []
        for order in exchange_open_orders:
            if not self._is_protective_order(order):
                continue
            exchange_symbol = str(order.get("symbol", "")).upper()
            client_id = str(order.get("client_order_id") or "")
            if exchange_symbol not in local_exchange_symbols:
                drifts.append(
                    {
                        "symbol": "",
                        "exchange_symbol": exchange_symbol,
                        "local_qty": 0.0,
                        "exchange_qty": 0.0,
                        "reason": "orphan protective order has no local position",
                        "client_order_id": client_id,
                        "order_type": order.get("type", ""),
                    }
                )
                continue
            if not client_id or client_id not in known_ids:
                drifts.append(
                    {
                        "symbol": "",
                        "exchange_symbol": exchange_symbol,
                        "local_qty": 0.0,
                        "exchange_qty": 0.0,
                        "reason": "orphan protective order has no local owner",
                        "client_order_id": client_id,
                        "order_type": order.get("type", ""),
                    }
                )
        return drifts

    def _submitting_lifecycle_intents(self, run_date: str, instrument_map: dict) -> list[dict]:
        rows = load_json(self.output_root / "order_lifecycle" / f"{run_date}.json")
        intents: list[dict] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or row.get("state") != "submitting":
                continue
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            ticket = metadata.get("ticket") if isinstance(metadata.get("ticket"), dict) else {}
            asset = str(ticket.get("asset") or row.get("symbol") or metadata.get("asset") or "GOLD")
            exchange_symbol = str(metadata.get("symbol") or instrument_map.get(asset, asset)).upper()
            intents.append(
                {
                    "order_id": str(row.get("order_id") or ""),
                    "ticket_id": str(row.get("ticket_id") or ""),
                    "state": str(row.get("state") or ""),
                    "blocked": bool(row.get("blocked")),
                    "cycles_in_state": int(row.get("cycles_in_state", 0) or 0),
                    "requested_quantity": float(row.get("requested_quantity", 0) or 0),
                    "exchange_symbol": exchange_symbol,
                    "blocker": row.get("blocker", {}) if isinstance(row.get("blocker"), dict) else {},
                }
            )
        return intents

    def _known_protective_order_ids(self) -> set[str]:
        ids: set[str] = set()
        request_dirs = {
            str(self.broker_config.get("request_dir", "live_order_requests")),
            "live_order_requests",
            "demo_order_requests",
        }
        for dirname in request_dirs:
            directory = self.output_root / dirname
            if not directory.exists():
                continue
            for path in directory.glob("*.json"):
                rows = load_json(path)
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    request = row.get("request", {}) if isinstance(row.get("request"), dict) else {}
                    for payload in request.get("protective_orders", []) or []:
                        if isinstance(payload, dict) and payload.get("newClientOrderId"):
                            ids.add(str(payload["newClientOrderId"]))
                    response = row.get("broker_response", {}) if isinstance(row.get("broker_response"), dict) else {}
                    for item in response.get("protective_orders", []) or []:
                        if isinstance(item, dict) and item.get("clientOrderId"):
                            ids.add(str(item["clientOrderId"]))
        return ids

    def _is_protective_order(self, order: dict) -> bool:
        order_type = str(order.get("type") or "").upper()
        if order_type in {"STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET"}:
            return True
        return bool(order.get("reduce_only") or order.get("close_position"))

    def _signed_get(self, endpoint: str, params: dict) -> list:
        apply_live_env()
        api_key_env = str(self.broker_config.get("api_key_env", "BINANCE_API_KEY"))
        secret_env = str(self.broker_config.get("api_secret_env", "BINANCE_API_SECRET"))
        if not live_env_value_present(api_key_env) or not live_env_value_present(secret_env):
            raise RuntimeError(f"missing Binance environment variables: {api_key_env}, {secret_env}")
        api_key = os.getenv(api_key_env)
        api_secret = os.getenv(secret_env)
        signed = {**params, "timestamp": int(time.time() * 1000), "recvWindow": int(self.broker_config.get("recv_window_ms", 5000))}
        query = urllib.parse.urlencode(signed)
        signature = hmac.new(str(api_secret).encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
        url = f"{self._base_url()}{endpoint}?{query}&signature={signature}"
        request = urllib.request.Request(
            url,
            headers={"X-MBX-APIKEY": str(api_key), "User-Agent": "TradingOrchestrator/1.0"},
            method="GET",
        )
        with self.opener(request, timeout=int(self.broker_config.get("timeout_seconds", 10))) as response:
            return json.loads(response.read().decode("utf-8"))

    def _base_url(self) -> str:
        if self.broker_config.get("base_url"):
            return str(self.broker_config["base_url"]).rstrip("/")
        environment = str(self.broker_config.get("environment", "live")).lower()
        return "https://testnet.binancefuture.com" if environment == "testnet" else "https://fapi.binance.com"
