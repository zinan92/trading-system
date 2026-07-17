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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.accounting_projection import project_broker_accounting
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
        payload = self._signed_get_list("/fapi/v2/positionRisk", {})
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
        payload = self._signed_get_list("/fapi/v2/balance", {})
        asset = str(self.broker_config.get("margin_asset", "USDT"))
        row = next((item for item in payload if item.get("asset") == asset), {})
        return {
            "asset": asset,
            "balance": float(row.get("balance", 0) or 0),
            "available": float(row.get("availableBalance", 0) or 0),
            "balance_present": bool(row),
            "truth_source": "fapi/v2/balance" if row else "",
        }

    def exchange_open_orders(self) -> list[dict]:
        payload = self._signed_get_list("/fapi/v1/openOrders", {})
        orders = self._normalize_open_orders(payload, source="open_orders")
        if self._uses_algo_protective_orders():
            orders.extend(self.exchange_open_algo_orders())
        return orders

    def exchange_open_algo_orders(self) -> list[dict]:
        payload = self._signed_get_list("/fapi/v1/openAlgoOrders", {})
        return self._normalize_open_orders(payload, source="open_algo_orders")

    def exchange_fills(self, symbol: str, run_date: str | None = None) -> list[dict]:
        start_ms, end_ms = self._utc_day_window_ms(run_date) if run_date else (None, None)
        params = {"symbol": symbol, "limit": int(self.broker_config.get("trade_history_limit", 1000))}
        if start_ms is not None and end_ms is not None:
            params.update({"startTime": start_ms, "endTime": end_ms})
        payload = self._signed_get_list("/fapi/v1/userTrades", params)
        fills = []
        expected_symbol = symbol.upper()
        for row in payload:
            if not isinstance(row, dict):
                raise ValueError(f"userTrades row was not an object: {type(row).__name__}")
            row_symbol = str(row.get("symbol", "")).upper()
            if row_symbol != expected_symbol:
                raise ValueError(f"userTrades symbol mismatch: expected {expected_symbol}, got {row_symbol or '<missing>'}")
            if start_ms is not None and end_ms is not None and not self._row_time_in_window(row.get("time"), start_ms, end_ms):
                continue
            fills.append(
                {
                    "symbol": str(row.get("symbol", "")),
                    "id": str(row.get("id", "")),
                    "order_id": str(row.get("orderId", "")),
                    "side": str(row.get("side", "")),
                    "price": float(row.get("price", 0) or 0),
                    "qty": float(row.get("qty", 0) or 0),
                    "quote_qty": float(row.get("quoteQty", 0) or 0),
                    "commission": float(row.get("commission", 0) or 0),
                    "commission_asset": str(row.get("commissionAsset", "")),
                    "realized_pnl": float(row.get("realizedPnl", 0) or 0),
                    "time": row.get("time"),
                }
            )
        return fills

    def exchange_income(self, symbol: str, run_date: str | None = None) -> list[dict]:
        start_ms, end_ms = self._utc_day_window_ms(run_date) if run_date else (None, None)
        params = {"symbol": symbol, "limit": int(self.broker_config.get("income_history_limit", 1000))}
        if start_ms is not None and end_ms is not None:
            params.update({"startTime": start_ms, "endTime": end_ms})
        payload = self._signed_get_list("/fapi/v1/income", params)
        income = []
        expected_symbol = symbol.upper()
        for row in payload:
            if not isinstance(row, dict):
                raise ValueError(f"income row was not an object: {type(row).__name__}")
            row_symbol = str(row.get("symbol", "")).upper()
            if not row_symbol:
                continue
            if row_symbol != expected_symbol:
                raise ValueError(f"income symbol mismatch: expected {expected_symbol}, got {row_symbol or '<missing>'}")
            if start_ms is not None and end_ms is not None and not self._row_time_in_window(row.get("time"), start_ms, end_ms):
                continue
            income.append(
                {
                    "symbol": str(row.get("symbol", "")),
                    "income_type": str(row.get("incomeType", "")),
                    "income": float(row.get("income", 0) or 0),
                    "asset": str(row.get("asset", "")),
                    "time": row.get("time"),
                    "tran_id": str(row.get("tranId", "")),
                    "trade_id": str(row.get("tradeId", "")),
                }
            )
        return income

    def _normalize_open_orders(self, payload: list, *, source: str) -> list[dict]:
        orders = []
        for row in payload:
            if not isinstance(row, dict):
                continue
            orders.append(
                {
                    "symbol": str(row.get("symbol", "")),
                    "order_id": str(row.get("orderId", row.get("algoId", ""))),
                    "client_order_id": str(row.get("clientOrderId", row.get("clientAlgoId", ""))),
                    "type": str(row.get("type", row.get("orderType", ""))),
                    "algo_type": str(row.get("algoType", "")),
                    "side": str(row.get("side", "")),
                    "orig_qty": float(row.get("origQty", row.get("quantity", 0)) or 0),
                    "reduce_only": str(row.get("reduceOnly", row.get("reduce_only", ""))).lower() == "true",
                    "close_position": str(row.get("closePosition", row.get("close_position", ""))).lower() == "true",
                    "status": str(row.get("status", row.get("algoStatus", ""))),
                    "source": source,
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
        exchange_fills: list[dict] = []
        exchange_income: list[dict] = []
        balance: dict = {}
        primary_symbol = self._primary_exchange_symbol()
        account_observation = self._account_observation_template(primary_symbol)
        try:
            exchange_positions = self.exchange_positions()
            balance = self.exchange_balance()
            account_observation["balance_present"] = bool(balance.get("balance_present"))
            exchange_open_orders = self.exchange_open_orders()
            if self._reconciles_account_history():
                exchange_fills = self.exchange_fills(primary_symbol, run_date)
                account_observation["fills_observed"] = True
                account_observation["fills_count"] = len(exchange_fills)
                exchange_income = self.exchange_income(primary_symbol, run_date)
                account_observation["income_observed"] = True
                account_observation["income_count"] = len(exchange_income)
                account_observation["account_observed"] = True
                account_observation["reason"] = "account history observed; empty lists mean confirmed no rows in the UTC day window"
            else:
                account_observation["reason"] = "account history reconciliation is disabled"
        except _FETCH_ERRORS as exc:
            error = f"{type(exc).__name__}: {exc}"
            account_observation["reason"] = f"account observation failed: {error}"

        local = self.local_positions()
        instrument_map = self.broker_config.get("instrument_map", {"GOLD": "XAUUSDT"})
        submitting_intents = self._submitting_lifecycle_intents(run_date, instrument_map)
        exchange_by_symbol = {p["symbol"]: p for p in exchange_positions}
        submitting_by_symbol: dict[str, list[dict]] = {}
        for intent in submitting_intents:
            submitting_by_symbol.setdefault(str(intent.get("exchange_symbol") or "").upper(), []).append(intent)
        matched: set[str] = set()
        drifts: list[dict] = []
        position_protection: list[dict] = []
        protection_by_symbol: dict[str, dict] = {}

        if not error:
            position_shape_drifts = self._position_shape_drifts(exchange_positions)
            drifts.extend(position_shape_drifts)
            position_protection = [self._position_protection_snapshot(position, exchange_open_orders) for position in exchange_positions]
            protection_by_symbol = {str(item.get("exchange_symbol") or "").upper(): item for item in position_protection}
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
                    protection = protection_by_symbol.get(exchange_symbol, {})
                    drift = {
                        "symbol": "",
                        "exchange_symbol": exchange_symbol,
                        "local_qty": 0.0,
                        "exchange_qty": exchange["position_amt"],
                        "reason": "exchange position has no local record",
                    }
                    if not protection.get("covered", False):
                        drift.update(
                            {
                                "reason": "suspected naked position: exchange position has no local record and lacks a resting reduceOnly protective stop",
                                "reason_code": "naked_position_suspected",
                                "naked_position_risk": True,
                                "missing_protective_order": True,
                                "position_protection": protection,
                            }
                        )
                        if intents:
                            drift["submitting_intents"] = intents
                    elif intents:
                        drift.update(
                            {
                                "reason": "suspected naked position: exchange position exists while local order is still submitting",
                                "reason_code": "naked_position_suspected",
                                "naked_position_risk": True,
                                "submitting_intents": intents,
                            }
                        )
                    drifts.append(drift)

            for protection in position_protection:
                exchange_symbol = str(protection.get("exchange_symbol") or "").upper()
                if protection.get("covered", False) or exchange_symbol not in matched:
                    continue
                drifts.append(
                    {
                        "symbol": self._local_symbol_for_exchange(exchange_symbol, instrument_map),
                        "exchange_symbol": exchange_symbol,
                        "local_qty": protection.get("position_amt"),
                        "exchange_qty": protection.get("position_amt"),
                        "reason": "suspected naked position: exchange position lacks a resting reduceOnly protective stop",
                        "reason_code": "naked_position_suspected",
                        "naked_position_risk": True,
                        "missing_protective_order": True,
                        "position_protection": protection,
                    }
                )

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
            "position_protection": position_protection,
            "exchange_fills": exchange_fills,
            "exchange_income": exchange_income,
            "exchange_accounting": self._exchange_accounting(exchange_fills, exchange_income, run_date=run_date),
            "exchange_balance": balance,
            "account_observation": account_observation,
            "local_positions": local,
            "known_protective_order_ids": sorted(self._known_protective_order_ids()),
            "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        }
        report["accounting_snapshot"] = project_broker_accounting(report).to_dict()
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

    def _position_protection_snapshot(self, position: dict, exchange_open_orders: list[dict]) -> dict:
        exchange_symbol = str(position.get("symbol", "")).upper()
        position_amt = float(position.get("position_amt", 0) or 0)
        required_side = "SELL" if position_amt > 0 else "BUY"
        matching = [
            order
            for order in exchange_open_orders
            if self._covers_position_with_resting_stop(order, exchange_symbol=exchange_symbol, required_side=required_side)
        ]
        covered_qty = 0.0
        covers_full_position = False
        for order in matching:
            if order.get("close_position"):
                covers_full_position = True
                covered_qty = max(covered_qty, abs(position_amt))
                continue
            covered_qty += float(order.get("orig_qty", 0) or 0)
        covered = covers_full_position or covered_qty + _QTY_TOLERANCE >= abs(position_amt)
        return {
            "exchange_symbol": exchange_symbol,
            "position_amt": position_amt,
            "required_side": required_side,
            "required_qty": abs(position_amt),
            "covered": covered,
            "covered_qty": round(covered_qty, 12),
            "matching_protective_orders": [
                {
                    "order_id": item.get("order_id", ""),
                    "client_order_id": item.get("client_order_id", ""),
                    "type": item.get("type", ""),
                    "side": item.get("side", ""),
                    "orig_qty": item.get("orig_qty"),
                    "reduce_only": item.get("reduce_only"),
                    "close_position": item.get("close_position"),
                    "status": item.get("status", ""),
                    "source": item.get("source", ""),
                }
                for item in matching
            ],
            "reason_code": "" if covered else "position_without_resting_protective_stop",
        }

    def _position_shape_drifts(self, exchange_positions: list[dict]) -> list[dict]:
        by_symbol: dict[str, list[dict]] = {}
        for position in exchange_positions:
            by_symbol.setdefault(str(position.get("symbol", "")).upper(), []).append(position)
        drifts = []
        for exchange_symbol, rows in by_symbol.items():
            if len(rows) <= 1:
                continue
            drifts.append(
                {
                    "symbol": "",
                    "exchange_symbol": exchange_symbol,
                    "local_qty": 0.0,
                    "exchange_qty": sum(float(item.get("position_amt", 0) or 0) for item in rows),
                    "reason": "exchange positionRisk returned multiple non-zero rows for one symbol; hedge/dual-side mode is not supported by this reconciler",
                    "reason_code": "unsupported_dual_side_position_shape",
                    "position_rows": rows,
                }
            )
        return drifts

    def _covers_position_with_resting_stop(self, order: dict, *, exchange_symbol: str, required_side: str) -> bool:
        if str(order.get("symbol", "")).upper() != exchange_symbol:
            return False
        if str(order.get("side", "")).upper() != required_side:
            return False
        if not (order.get("reduce_only") or order.get("close_position")):
            return False
        if not self._is_resting_order(order):
            return False
        order_type = str(order.get("type") or "").upper()
        return order_type in {"STOP", "STOP_MARKET"}

    def _is_resting_order(self, order: dict) -> bool:
        status = str(order.get("status") or "").upper()
        if not status:
            return True
        return status not in {"CANCELED", "CANCELLED", "EXPIRED", "REJECTED", "FILLED"}

    def _local_symbol_for_exchange(self, exchange_symbol: str, instrument_map: dict) -> str:
        for local_symbol, mapped in instrument_map.items():
            if str(mapped).upper() == exchange_symbol:
                return str(local_symbol)
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
                    response = row.get("broker_response", {}) if isinstance(row.get("broker_response"), dict) else {}
                    for item in response.get("protective_orders", []) or []:
                        if not isinstance(item, dict):
                            continue
                        for key in ("clientOrderId", "clientAlgoId", "client_order_id"):
                            if item.get(key):
                                ids.add(str(item[key]))
        return ids

    def _is_protective_order(self, order: dict) -> bool:
        order_type = str(order.get("type") or "").upper()
        if order_type in {"STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET"}:
            return True
        return bool(order.get("reduce_only") or order.get("close_position"))

    def _primary_exchange_symbol(self) -> str:
        mapping = self.broker_config.get("instrument_map", {"GOLD": "XAUUSDT"})
        return str(mapping.get("GOLD") or mapping.get("XAUUSD") or "XAUUSDT").upper()

    def _uses_algo_protective_orders(self) -> bool:
        value = str(self.broker_config.get("protective_order_endpoint", "order")).lower()
        return value in {"algo", "algo_order", "algoorder", "/fapi/v1/algoorder"}

    def _reconciles_account_history(self) -> bool:
        return bool(self.broker_config.get("reconcile_account_history", True))

    def _account_observation_template(self, symbol: str) -> dict:
        return {
            "account_observed": False,
            "balance_present": False,
            "history_requested": self._reconciles_account_history(),
            "fills_observed": False,
            "income_observed": False,
            "fills_count": 0,
            "income_count": 0,
            "symbol": symbol,
            "reason": "",
        }

    def _exchange_accounting(self, fills: list[dict], income: list[dict], *, run_date: str | None = None) -> dict:
        commission_by_asset: dict[str, float] = {}
        for fill in fills:
            asset = str(fill.get("commission_asset") or "")
            if asset:
                commission_by_asset[asset] = commission_by_asset.get(asset, 0.0) + float(fill.get("commission", 0) or 0)
        funding_by_asset: dict[str, float] = {}
        income_by_type: dict[str, float] = {}
        for item in income:
            income_type = str(item.get("income_type") or "")
            asset = str(item.get("asset") or "")
            amount = float(item.get("income", 0) or 0)
            income_by_type[income_type] = income_by_type.get(income_type, 0.0) + amount
            if income_type == "FUNDING_FEE" and asset:
                funding_by_asset[asset] = funding_by_asset.get(asset, 0.0) + amount
        margin_asset = str(self.broker_config.get("margin_asset", "USDT"))
        realized_pnl_from_fills = sum(float(fill.get("realized_pnl", 0) or 0) for fill in fills)
        commission_margin_asset = commission_by_asset.get(margin_asset, 0.0)
        funding_margin_asset = funding_by_asset.get(margin_asset, 0.0)
        result = {
            "trade_count": len(fills),
            "income_count": len(income),
            "realized_pnl_from_fills": round(realized_pnl_from_fills, 8),
            "commission_by_asset": {asset: round(value, 8) for asset, value in sorted(commission_by_asset.items())},
            "funding_by_asset": {asset: round(value, 8) for asset, value in sorted(funding_by_asset.items())},
            "income_by_type": {kind: round(value, 8) for kind, value in sorted(income_by_type.items())},
            "net_realized_pnl_estimate": round(realized_pnl_from_fills + funding_margin_asset - commission_margin_asset, 8),
            "net_realized_pnl_asset": margin_asset,
            "net_realized_pnl_formula": "sum(userTrades.realizedPnl) + sum(income.FUNDING_FEE) - sum(userTrades.commission in margin asset)",
        }
        if run_date:
            start_ms, end_ms = self._utc_day_window_ms(run_date)
            result["utc_trading_day"] = {"run_date": run_date, "start_time_ms": start_ms, "end_time_ms": end_ms}
        return result

    def _utc_day_window_ms(self, run_date: str) -> tuple[int, int]:
        start = datetime.strptime(run_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end = start + timedelta(days=1) - timedelta(milliseconds=1)
        return int(start.timestamp() * 1000), int(end.timestamp() * 1000)

    def _row_time_in_window(self, value, start_ms: int, end_ms: int) -> bool:
        try:
            timestamp_ms = int(value)
        except (TypeError, ValueError):
            return False
        return start_ms <= timestamp_ms <= end_ms

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

    def _signed_get_list(self, endpoint: str, params: dict) -> list:
        payload = self._signed_get(endpoint, params)
        if not isinstance(payload, list):
            code = payload.get("code") if isinstance(payload, dict) else ""
            msg = payload.get("msg") if isinstance(payload, dict) else ""
            detail = f" code={code} msg={msg}" if code or msg else ""
            raise ValueError(f"{endpoint} response was not a list; account state not observed{detail}")
        return payload

    def _base_url(self) -> str:
        if self.broker_config.get("base_url"):
            return str(self.broker_config["base_url"]).rstrip("/")
        environment = str(self.broker_config.get("environment", "live")).lower()
        return "https://testnet.binancefuture.com" if environment == "testnet" else "https://fapi.binance.com"
