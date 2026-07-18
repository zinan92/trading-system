from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.broker_composition import resolve_broker_profile_config
from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json
from services.live_env import apply_live_env
from services.order_lifecycle import OrderLifecycleStore


class TigerOpenApiPaperReconciliation:
    """Read-only Tiger paper account reconciliation.

    This service fetches positions and open orders only. It never submits,
    cancels, modifies, or closes a Tiger order.
    """

    def __init__(self, output_root: Path | None = None, broker_config: dict | None = None, trade_client: Any | None = None) -> None:
        config = load_pipeline_config()
        self.output_root = Path(output_root or Path(os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(ROOT / config.get("output_root", "outputs")))))
        self.broker_config = broker_config if broker_config is not None else self._default_tiger_broker_config(config)
        self._trade_client = trade_client

    def run(self, run_date: str) -> dict:
        error = ""
        exchange_positions: list[dict] = []
        exchange_open_orders: list[dict] = []
        try:
            client = self._client()
            exchange_positions = self.exchange_positions(client)
            exchange_open_orders = self.exchange_open_orders(client)
        except (OSError, TimeoutError, RuntimeError, ValueError, TypeError, KeyError) as exc:
            error = f"{type(exc).__name__}: {exc}"

        local = self.local_positions()
        drifts: list[dict] = []
        position_protection: list[dict] = []
        if not error:
            position_protection = [self._position_protection_snapshot(position, exchange_open_orders) for position in exchange_positions]
            drifts.extend(self._position_drifts(local, exchange_positions, position_protection))
            drifts.extend(self._orphan_order_drifts(local, exchange_open_orders))

        suspected_naked = any(bool(item.get("naked_position_risk")) for item in drifts)
        confirmation_status = self._confirmation_status(error, drifts, exchange_positions, exchange_open_orders)
        report = {
            "run_date": run_date,
            "provider": "tiger_openapi",
            "mode": "paper",
            "reconciled": (not error) and (not drifts),
            "confirmation_status": confirmation_status,
            "system_state": self._system_state(confirmation_status, suspected_naked),
            "reason_code": self._reason_code(confirmation_status, suspected_naked),
            "blocks_new_orders": confirmation_status != "confirmed_flat",
            "flat_confirmed": confirmation_status == "confirmed_flat",
            "can_open_new_orders": confirmation_status == "confirmed_flat",
            "suspected_naked_position": suspected_naked,
            "naked_position_risks": [item for item in drifts if item.get("naked_position_risk")],
            "escalation_action": self._escalation_action(confirmation_status, suspected_naked),
            "error": error,
            "drift_count": len(drifts),
            "drifts": drifts,
            "exchange_positions": exchange_positions,
            "exchange_open_orders": exchange_open_orders,
            "position_protection": position_protection,
            "local_positions": local,
            "account_observation": {
                "account_observed": not bool(error),
                "positions_observed": not bool(error),
                "open_orders_observed": not bool(error),
                "reason": "Tiger paper positions/open orders observed" if not error else f"Tiger paper observation failed: {error}",
            },
            "checked_at": self._now(),
        }
        if report["reconciled"] and not exchange_positions and not exchange_open_orders:
            OrderLifecycleStore(self.output_root).mark_closed_orders_reconciled(
                run_date,
                reason="tiger_paper_reconciliation_flat",
                metadata={"provider": "tiger_openapi", "truth_source": "tiger_tradeclient_positions_open_orders"},
            )
        write_json(self.output_root / "tiger_reconciliation" / "current.json", [report])
        write_json(self.output_root / "tiger_reconciliation" / f"{run_date}.json", [report])
        return report

    def exchange_positions(self, client: Any) -> list[dict]:
        positions = []
        for row in self._safe_list_call(client, "get_positions", sec_type="FUT"):
            item = self._object_to_dict(row)
            quantity = self._quantity(item)
            if abs(quantity) <= 1e-12:
                continue
            contract = self._contract_dict(item.get("contract"))
            local_symbol = str(contract.get("local_symbol") or contract.get("identifier") or item.get("symbol") or contract.get("symbol") or "")
            root_symbol = str(contract.get("symbol") or item.get("symbol") or self._root_symbol(local_symbol))
            positions.append(
                {
                    "symbol": local_symbol or root_symbol,
                    "root_symbol": root_symbol,
                    "position_amt": quantity,
                    "average_cost": self._float_or_none(item.get("average_cost") or item.get("avg_cost")),
                    "market_price": self._float_or_none(item.get("market_price")),
                    "unrealized_pnl": self._float_or_none(item.get("unrealized_pnl")),
                    "raw": self._redact_large(item),
                }
            )
        return positions

    def exchange_open_orders(self, client: Any) -> list[dict]:
        orders = []
        for row in self._safe_list_call(client, "get_open_orders", sec_type="FUT"):
            item = self._object_to_dict(row)
            contract = self._contract_dict(item.get("contract"))
            local_symbol = str(contract.get("local_symbol") or contract.get("identifier") or item.get("symbol") or contract.get("symbol") or "")
            orders.append(
                {
                    "symbol": local_symbol or str(contract.get("symbol") or ""),
                    "root_symbol": str(contract.get("symbol") or item.get("symbol") or ""),
                    "order_id": str(item.get("id") or item.get("order_id") or ""),
                    "parent_id": str(item.get("parent_id") or ""),
                    "type": str(item.get("order_type") or item.get("type") or ""),
                    "leg_type": str(item.get("leg_type") or ""),
                    "side": str(item.get("action") or item.get("side") or ""),
                    "orig_qty": self._quantity(item),
                    "limit_price": self._float_or_none(item.get("limit_price")),
                    "aux_price": self._float_or_none(item.get("aux_price")),
                    "status": str(item.get("status") or ""),
                    "source": "tiger_open_orders",
                    "raw": self._redact_large(item),
                }
            )
        return orders

    def local_positions(self) -> dict:
        path = self.output_root / "paper_positions" / "current.json"
        if not path.exists():
            return {}
        rows = load_json(path)
        if not isinstance(rows, dict):
            return {}
        allowed = self._managed_local_symbols()
        if not allowed:
            return rows
        return {symbol: position for symbol, position in rows.items() if str(symbol) in allowed}

    def _client(self) -> Any:
        if self._trade_client is not None:
            return self._trade_client
        try:
            from tigeropen.tiger_open_config import TigerOpenClientConfig
            from tigeropen.trade.trade_client import TradeClient
        except ImportError as exc:
            raise RuntimeError("tigeropen is not installed; install it locally to use Tiger OpenAPI reconciliation") from exc
        props_path = self._props_path()
        if not props_path:
            raise RuntimeError("Tiger OpenAPI config path is required")
        return TradeClient(TigerOpenClientConfig(props_path=props_path))

    def _default_tiger_broker_config(self, config: dict) -> dict:
        resolved = resolve_broker_profile_config(config)
        if str(resolved.get("provider") or "") == "tiger_openapi":
            return resolved
        profile = (config.get("broker_profiles", {}) or {}).get("tiger_openapi_paper")
        if isinstance(profile, dict) and profile:
            return dict(profile)
        return resolved

    def _managed_local_symbols(self) -> set[str]:
        symbols = {str(symbol) for symbol in (self.broker_config.get("contract_specs", {}) or {})}
        symbols.update(str(symbol) for symbol in self.broker_config.get("allowed_symbols", []) if not str(symbol).lower().endswith("main"))
        symbols.update(str(value) for value in (self.broker_config.get("contract_map", {}) or {}).values() if not str(value).lower().endswith("main"))
        return {symbol for symbol in symbols if symbol}

    def _props_path(self) -> str:
        if self.broker_config.get("props_path"):
            return str(self.broker_config["props_path"])
        apply_live_env()
        return os.getenv(str(self.broker_config.get("props_path_env", "TIGER_OPENAPI_CONFIG_PATH")), "")

    def _position_drifts(self, local: dict, exchange_positions: list[dict], protection: list[dict]) -> list[dict]:
        exchange_by_symbol = {str(item.get("symbol") or "").upper(): item for item in exchange_positions}
        protection_by_symbol = {str(item.get("exchange_symbol") or "").upper(): item for item in protection}
        local_symbols = {str(symbol).upper() for symbol in local}
        drifts = []
        for symbol, position in local.items():
            local_qty = self._local_signed_quantity(position)
            exchange = exchange_by_symbol.get(str(symbol).upper())
            exchange_qty = float((exchange or {}).get("position_amt") or 0.0)
            if abs(local_qty - exchange_qty) > 1e-8:
                drifts.append(
                    {
                        "symbol": symbol,
                        "exchange_symbol": symbol,
                        "local_qty": local_qty,
                        "exchange_qty": exchange_qty,
                        "reason": "quantity mismatch" if exchange else "local position has no Tiger match",
                    }
                )
        for exchange in exchange_positions:
            symbol = str(exchange.get("symbol") or "").upper()
            if symbol in local_symbols:
                continue
            item = {
                "symbol": "",
                "exchange_symbol": exchange.get("symbol", ""),
                "local_qty": 0.0,
                "exchange_qty": exchange.get("position_amt"),
                "reason": "Tiger position has no local record",
            }
            coverage = protection_by_symbol.get(symbol, {})
            if not coverage.get("covered", False):
                item.update(
                    {
                        "reason": "suspected naked position: Tiger position has no local record and lacks attached/resting protective order",
                        "reason_code": "naked_position_suspected",
                        "naked_position_risk": True,
                        "missing_protective_order": True,
                        "position_protection": coverage,
                    }
                )
            drifts.append(item)
        return drifts

    def _orphan_order_drifts(self, local: dict, open_orders: list[dict]) -> list[dict]:
        if not open_orders:
            return []
        local_symbols = {str(symbol).upper() for symbol in local}
        drifts = []
        for order in open_orders:
            if str(order.get("symbol") or "").upper() in local_symbols:
                continue
            if self._is_protective_order(order):
                drifts.append(
                    {
                        "symbol": "",
                        "exchange_symbol": order.get("symbol", ""),
                        "local_qty": 0.0,
                        "exchange_qty": 0.0,
                        "reason": "orphan Tiger protective order has no local position",
                        "order_id": order.get("order_id", ""),
                        "order_type": order.get("type", ""),
                    }
                )
        return drifts

    def _position_protection_snapshot(self, position: dict, open_orders: list[dict]) -> dict:
        symbol = str(position.get("symbol") or "").upper()
        amount = float(position.get("position_amt") or 0.0)
        required_side = "SELL" if amount > 0 else "BUY"
        matching = [
            order
            for order in open_orders
            if str(order.get("symbol") or "").upper() == symbol
            and str(order.get("side") or "").upper() == required_side
            and self._is_protective_order(order)
        ]
        covered_qty = sum(abs(float(order.get("orig_qty") or 0.0)) for order in matching)
        covered = bool(matching) and (covered_qty <= 1e-12 or covered_qty + 1e-8 >= abs(amount))
        return {
            "exchange_symbol": position.get("symbol", ""),
            "position_amt": amount,
            "required_side": required_side,
            "required_qty": abs(amount),
            "covered": covered,
            "covered_qty": round(covered_qty, 12),
            "matching_protective_orders": [
                {
                    "order_id": item.get("order_id", ""),
                    "parent_id": item.get("parent_id", ""),
                    "type": item.get("type", ""),
                    "leg_type": item.get("leg_type", ""),
                    "side": item.get("side", ""),
                    "orig_qty": item.get("orig_qty"),
                    "status": item.get("status", ""),
                }
                for item in matching
            ],
            "reason_code": "" if covered else "position_without_attached_or_resting_protective_order",
        }

    def _confirmation_status(self, error: str, drifts: list[dict], positions: list[dict], open_orders: list[dict]) -> str:
        if error:
            return "cannot_confirm"
        if drifts:
            return "confirmed_drift"
        if positions or open_orders:
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
            return "position_or_order_confirmed_open"
        return "confirmed_flat"

    def _system_state(self, confirmation_status: str, suspected_naked: bool) -> str:
        if confirmation_status == "cannot_confirm":
            return "BLOCKED_RECONCILIATION_UNKNOWN"
        if suspected_naked:
            return "BLOCKED_NAKED_POSITION_SUSPECTED"
        if confirmation_status == "confirmed_drift":
            return "BLOCKED_RECONCILIATION_DRIFT"
        if confirmation_status == "confirmed_open":
            return "PAUSED_POSITION_OR_ORDER_OPEN"
        return "READY"

    def _escalation_action(self, confirmation_status: str, suspected_naked: bool) -> str:
        if suspected_naked:
            return "halt_new_orders_and_verify_or_flatten_tiger_paper_position"
        if confirmation_status == "cannot_confirm":
            return "halt_new_orders_until_tiger_state_can_be_confirmed"
        if confirmation_status == "confirmed_drift":
            return "halt_new_orders_until_tiger_reconciliation_drift_resolved"
        return ""

    def _is_protective_order(self, order: dict) -> bool:
        text = " ".join(str(order.get(key) or "").upper() for key in ["type", "leg_type"])
        return any(marker in text for marker in ["LOSS", "STOP", "STP"])

    def _local_signed_quantity(self, position: dict) -> float:
        if position.get("net_quantity") is not None:
            return float(position.get("net_quantity") or 0.0)
        qty = float(position.get("quantity") or 0.0)
        return qty if str(position.get("side", "long")).lower() == "long" else -qty

    def _safe_list_call(self, client: Any, method_name: str, **kwargs) -> list[Any]:
        method = getattr(client, method_name, None)
        if method is None:
            raise RuntimeError(f"Tiger TradeClient missing {method_name}")
        payload = method(**kwargs)
        if payload is None:
            return []
        if isinstance(payload, list):
            return payload
        if isinstance(payload, tuple):
            return list(payload)
        return [payload]

    def _object_to_dict(self, item: Any) -> dict:
        if isinstance(item, dict):
            return dict(item)
        if hasattr(item, "_asdict"):
            return dict(item._asdict())
        if hasattr(item, "__dict__"):
            return {key: value for key, value in vars(item).items() if not key.startswith("_")}
        return {"value": item}

    def _contract_dict(self, item: Any) -> dict:
        if item is None:
            return {}
        return self._object_to_dict(item)

    def _quantity(self, item: dict) -> float:
        for key in ["position_amt", "position_qty", "quantity", "qty", "orig_qty"]:
            if key not in item or item.get(key) in {None, ""}:
                continue
            try:
                return float(item.get(key))
            except (TypeError, ValueError, AttributeError):
                continue
        return 0.0

    def _root_symbol(self, symbol: str) -> str:
        prefix = ""
        for char in str(symbol):
            if char.isalpha() or (char.isdigit() and not prefix):
                prefix += char
            else:
                break
        return prefix or str(symbol)

    def _float_or_none(self, value: Any) -> float | None:
        try:
            return float(value) if value not in {None, ""} else None
        except (TypeError, ValueError):
            return None

    def _redact_large(self, item: dict) -> dict:
        return {key: value for key, value in item.items() if key not in {"account", "secret_key", "private_key"}}

    def _now(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
