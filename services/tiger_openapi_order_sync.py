from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.broker_adapter import resolve_broker_config
from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import write_json
from services.live_env import apply_live_env


class TigerOpenApiOrderSync:
    """Read-only Tiger paper order/fill synchronization.

    This service is an attribution surface. It fetches Tiger futures open orders
    and filled orders, normalizes them into local artifacts, and never submits,
    cancels, modifies, or closes orders.
    """

    def __init__(self, output_root: Path | None = None, broker_config: dict | None = None, trade_client: Any | None = None) -> None:
        config = load_pipeline_config()
        self.output_root = Path(output_root or Path(os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(ROOT / config.get("output_root", "outputs")))))
        self.broker_config = broker_config if broker_config is not None else self._default_tiger_broker_config(config)
        self._trade_client = trade_client

    def run(self, run_date: str, *, start_time: Any | None = None, end_time: Any | None = None) -> dict:
        start_time = start_time or f"{run_date} 00:00:00"
        end_time = end_time or f"{run_date} 23:59:59"
        errors: list[str] = []
        open_orders: list[dict] = []
        filled_orders: list[dict] = []
        account_observed = False
        open_orders_observed = False
        filled_orders_observed = False
        try:
            client = self._client()
            account_observed = True
        except Exception as exc:
            client = None
            errors.append(f"{type(exc).__name__}: {exc}")

        if client is not None:
            try:
                open_orders = self.exchange_open_orders(client)
                open_orders_observed = True
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
            try:
                filled_orders = self.exchange_filled_orders(client, start_time=start_time, end_time=end_time)
                filled_orders_observed = True
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
        error = "; ".join(errors)

        report = {
            "run_date": run_date,
            "provider": "tiger_openapi",
            "mode": "paper",
            "sync_status": "cannot_sync" if error else "synced",
            "error": error,
            "open_order_count": len(open_orders),
            "filled_order_count": len(filled_orders),
            "exchange_open_orders": open_orders,
            "exchange_filled_orders": filled_orders,
            "query": {
                "sec_type": "FUT",
                "start_time": self._time_value(start_time),
                "end_time": self._time_value(end_time),
            },
            "account_observation": {
                "account_observed": account_observed,
                "open_orders_observed": open_orders_observed,
                "filled_orders_observed": filled_orders_observed,
                "reason": "Tiger paper open/filled orders observed" if not error else f"Tiger paper order sync failed: {error}",
            },
            "checked_at": self._now(),
        }
        write_json(self.output_root / "tiger_order_sync" / "current.json", [report])
        write_json(self.output_root / "tiger_order_sync" / f"{run_date}.json", [report])
        return report

    def exchange_open_orders(self, client: Any) -> list[dict]:
        return [self._normalize_order(row, source="tiger_open_orders") for row in self._safe_list_call(client, "get_open_orders", sec_type="FUT")]

    def exchange_filled_orders(self, client: Any, *, start_time: Any | None = None, end_time: Any | None = None) -> list[dict]:
        kwargs = {"sec_type": "FUT"}
        if start_time is not None:
            kwargs["start_time"] = start_time
        if end_time is not None:
            kwargs["end_time"] = end_time
        return [self._normalize_order(row, source="tiger_filled_orders") for row in self._safe_list_call(client, "get_filled_orders", **kwargs)]

    def _normalize_order(self, row: Any, *, source: str) -> dict:
        item = self._object_to_dict(row)
        contract = self._contract_dict(item.get("contract"))
        local_symbol = str(contract.get("local_symbol") or contract.get("identifier") or item.get("local_symbol") or item.get("symbol") or contract.get("symbol") or "")
        root_symbol = str(contract.get("symbol") or item.get("root_symbol") or item.get("symbol") or self._root_symbol(local_symbol))
        filled_quantity = self._first_float(item, ["filled_quantity", "filled_qty", "filled", "filled_shares", "filled_quantity_scale"])
        average_fill_price = self._first_float(item, ["avg_fill_price", "average_fill_price", "filled_avg_price", "fill_price", "filled_price", "price"])
        quantity = self._first_float(item, ["orig_qty", "quantity", "qty", "total_quantity", "order_quantity"])
        return {
            "symbol": local_symbol or root_symbol,
            "root_symbol": root_symbol,
            "order_id": str(item.get("id") or item.get("order_id") or ""),
            "parent_id": str(item.get("parent_id") or ""),
            "side": str(item.get("action") or item.get("side") or ""),
            "type": str(item.get("order_type") or item.get("type") or ""),
            "status": str(item.get("status") or ""),
            "quantity": quantity,
            "filled_quantity": filled_quantity,
            "average_fill_price": average_fill_price,
            "limit_price": self._first_float(item, ["limit_price", "lmt_price"]),
            "aux_price": self._first_float(item, ["aux_price", "stop_price", "trailing_percent"]),
            "commission": self._first_float(item, ["commission", "commission_and_fee", "charges"]),
            "currency": str(item.get("currency") or contract.get("currency") or ""),
            "created_at": self._time_value(self._first_value(item, ["created_at", "created_time", "order_time", "time"])),
            "filled_at": self._time_value(self._first_value(item, ["filled_at", "filled_time", "trade_time", "latest_time", "updated_at"])),
            "source": source,
            "raw": self._redact_large(item),
        }

    def _client(self) -> Any:
        if self._trade_client is not None:
            return self._trade_client
        try:
            from tigeropen.tiger_open_config import TigerOpenClientConfig
            from tigeropen.trade.trade_client import TradeClient
        except ImportError as exc:
            raise RuntimeError("tigeropen is not installed; install it locally to use Tiger OpenAPI order sync") from exc
        props_path = self._props_path()
        if not props_path:
            raise RuntimeError("Tiger OpenAPI config path is required")
        return TradeClient(TigerOpenClientConfig(props_path=props_path))

    def _default_tiger_broker_config(self, config: dict) -> dict:
        resolved = resolve_broker_config(config)
        if str(resolved.get("provider") or "") == "tiger_openapi":
            return resolved
        profile = (config.get("broker_profiles", {}) or {}).get("tiger_openapi_paper")
        if isinstance(profile, dict) and profile:
            return dict(profile)
        return resolved

    def _props_path(self) -> str:
        if self.broker_config.get("props_path"):
            return str(self.broker_config["props_path"])
        apply_live_env()
        return os.getenv(str(self.broker_config.get("props_path_env", "TIGER_OPENAPI_CONFIG_PATH")), "")

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

    def _first_value(self, item: dict, keys: list[str]) -> Any | None:
        for key in keys:
            if key in item and item.get(key) not in {None, ""}:
                return item.get(key)
        return None

    def _first_float(self, item: dict, keys: list[str]) -> float | None:
        value = self._first_value(item, keys)
        try:
            return float(value) if value not in {None, ""} else None
        except (TypeError, ValueError):
            return None

    def _root_symbol(self, symbol: str) -> str:
        prefix = ""
        for char in str(symbol):
            if char.isalpha() or (char.isdigit() and not prefix):
                prefix += char
            else:
                break
        return prefix or str(symbol)

    def _time_value(self, value: Any | None) -> str:
        if value is None:
            return ""
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()
        return str(value)

    def _redact_large(self, item: dict) -> dict:
        return {key: value for key, value in item.items() if key not in {"account", "secret_key", "private_key"}}

    def _now(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
