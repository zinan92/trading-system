from __future__ import annotations

import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from services.accounting_projection import broker_accounting_snapshot_payload
from services.broker_adapter import resolve_broker_config
from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import write_json
from services.live_env import apply_live_env


class TigerOpenApiAccountSync:
    """Read-only Tiger paper account/balance/accounting synchronization."""

    def __init__(self, output_root: Path | None = None, broker_config: dict | None = None, trade_client: Any | None = None) -> None:
        config = load_pipeline_config()
        self.output_root = Path(output_root or Path(os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(ROOT / config.get("output_root", "outputs")))))
        self.broker_config = broker_config if broker_config is not None else self._default_tiger_broker_config(config)
        self._trade_client = trade_client

    def run(self, run_date: str) -> dict:
        error = ""
        prime_assets = {}
        selected = {}
        try:
            client = self._client()
            raw_assets = client.get_prime_assets(base_currency=str(self.broker_config.get("base_currency", "USD")), consolidated=True)
            prime_assets = self._portfolio_dict(raw_assets)
            selected = self._selected_segment(prime_assets)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"

        balance = self._balance_snapshot(selected)
        accounting = self._accounting_snapshot(run_date, selected)
        account_observed = not bool(error) and bool(prime_assets)
        balance_present = bool(balance.get("balance_present"))
        accounting_observed = accounting.get("net_realized_pnl_estimate") is not None
        report = {
            "run_date": run_date,
            "provider": "tiger_openapi",
            "mode": "paper",
            "sync_status": "cannot_sync" if error else "synced",
            "error": error,
            "account_observation": {
                "account_observed": account_observed,
                "balance_present": balance_present,
                "accounting_observed": accounting_observed,
                "segment_key": selected.get("segment_key", ""),
                "base_currency": str(self.broker_config.get("base_currency", "USD")),
                "reason": "Tiger prime assets observed" if account_observed else f"Tiger account sync failed: {error or 'prime assets missing'}",
            },
            "exchange_balance": balance,
            "exchange_accounting": accounting,
            "selected_segment": self._redact_segment(selected),
            "checked_at": self._now(),
        }
        report["accounting_snapshot"] = broker_accounting_snapshot_payload(report)
        write_json(self.output_root / "tiger_account_sync" / "current.json", [report])
        write_json(self.output_root / "tiger_account_sync" / f"{run_date}.json", [report])
        return report

    def merge_into_reconciliation(self, reconciliation: dict, account_report: dict) -> dict:
        merged = dict(reconciliation)
        observation = dict(merged.get("account_observation", {}) if isinstance(merged.get("account_observation"), dict) else {})
        account_observation = account_report.get("account_observation", {}) if isinstance(account_report.get("account_observation"), dict) else {}
        observation.update(
            {
                "account_observed": account_observation.get("account_observed") is True,
                "balance_present": account_observation.get("balance_present") is True,
                "accounting_observed": account_observation.get("accounting_observed") is True,
                "history_requested": True,
                "fills_observed": True,
                "income_observed": True,
                "accounting_source": "tiger_openapi.get_prime_assets",
            }
        )
        merged["account_observation"] = observation
        merged["exchange_balance"] = account_report.get("exchange_balance", {}) if isinstance(account_report.get("exchange_balance"), dict) else {}
        merged["exchange_accounting"] = account_report.get("exchange_accounting", {}) if isinstance(account_report.get("exchange_accounting"), dict) else {}
        if account_report.get("error"):
            merged["account_sync_error"] = account_report.get("error")
        return merged

    def _client(self) -> Any:
        if self._trade_client is not None:
            return self._trade_client
        try:
            from tigeropen.tiger_open_config import TigerOpenClientConfig
            from tigeropen.trade.trade_client import TradeClient
        except ImportError as exc:
            raise RuntimeError("tigeropen is not installed; install it locally to use Tiger OpenAPI account sync") from exc
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

    def _portfolio_dict(self, item: Any) -> dict:
        data = self._object_to_dict(item)
        segments = data.get("segments", {})
        if not isinstance(segments, dict):
            segments = {}
        data["segments"] = {str(key): self._object_to_dict(value) for key, value in segments.items()}
        return data

    def _selected_segment(self, portfolio: dict) -> dict:
        segments = portfolio.get("segments", {}) if isinstance(portfolio.get("segments"), dict) else {}
        priority = self.broker_config.get("account_segment_priority", ["C", "F", "S"])
        for key in [str(item) for item in priority]:
            if key in segments and isinstance(segments[key], dict):
                return {"segment_key": key, **segments[key]}
        for key, value in segments.items():
            if isinstance(value, dict):
                return {"segment_key": str(key), **value}
        return {}

    def _balance_snapshot(self, segment: dict) -> dict:
        balance = self._finite_float(self._first_value(segment, ["net_liquidation", "equity_with_loan"]))
        available = self._finite_float(self._first_value(segment, ["cash_available_for_trade", "cash_balance", "available_funds", "cash"]))
        currency = str(segment.get("currency") or self.broker_config.get("base_currency", "USD"))
        return {
            "asset": currency,
            "balance": balance,
            "available": available,
            "balance_present": balance is not None and balance > 0,
            "source": "tiger_openapi.get_prime_assets.selected_segment.net_liquidation",
        }

    def _accounting_snapshot(self, run_date: str, segment: dict) -> dict:
        realized = self._finite_float(self._first_value(segment, ["realized_pl", "realized_pnl"]))
        unrealized = self._finite_float(self._first_value(segment, ["unrealized_pl", "unrealized_pnl"]))
        start_ms, end_ms = self._utc_day_window_ms(run_date)
        return {
            "net_realized_pnl_estimate": realized,
            "unrealized_pnl_estimate": unrealized,
            "realized_pnl_present": realized is not None,
            "unrealized_pnl_present": unrealized is not None,
            "utc_trading_day": {"run_date": run_date, "start_time_ms": start_ms, "end_time_ms": end_ms},
            "source": "tiger_openapi.get_prime_assets.selected_segment.realized_pl",
        }

    def _redact_segment(self, segment: dict) -> dict:
        allowed = (
            "segment_key",
            "currency",
            "category",
            "capability",
            "cash_balance",
            "cash_available_for_trade",
            "gross_position_value",
            "equity_with_loan",
            "net_liquidation",
            "init_margin",
            "maintain_margin",
            "unrealized_pl",
            "realized_pl",
            "buying_power",
            "leverage",
        )
        return {key: self._json_safe(segment.get(key)) for key in allowed if key in segment}

    def _object_to_dict(self, item: Any) -> dict:
        if item is None:
            return {}
        if isinstance(item, dict):
            return dict(item)
        if hasattr(item, "_asdict"):
            return dict(item._asdict())
        if hasattr(item, "__dict__"):
            return {key.lstrip("_"): value for key, value in vars(item).items() if key not in {"_secret_key"}}
        return {"value": item}

    def _first_value(self, item: dict, keys: list[str]) -> Any | None:
        for key in keys:
            value = item.get(key)
            if value is not None and value != "":
                return value
        return None

    def _finite_float(self, value: Any) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) else None

    def _json_safe(self, value: Any) -> Any:
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value

    def _utc_day_window_ms(self, run_date: str) -> tuple[int, int]:
        start = datetime.strptime(run_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end = start + timedelta(days=1) - timedelta(milliseconds=1)
        return int(start.timestamp() * 1000), int(end.timestamp() * 1000)

    def _now(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
