from __future__ import annotations

import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json
from services.live_env import apply_live_env, is_placeholder_value


CATALOG_SCHEMA_VERSION = "connector-catalog-v1"


class ConnectorCatalog:
    """Read-only catalog for venue/source capabilities and credential readiness.

    The catalog is a product surface for future connector onboarding. It reports
    what each connector can do and whether required credentials appear to be
    present, but it never returns credential values and never opens broker/feed
    clients.
    """

    def __init__(self, config: dict | None = None, output_root: Path | None = None, *, load_live_env: bool | None = None) -> None:
        should_load_live_env = (config is None) if load_live_env is None else bool(load_live_env)
        if should_load_live_env:
            apply_live_env()
        self.config = config if config is not None else load_pipeline_config()
        self.output_root = Path(output_root) if output_root else ROOT / str(self.config.get("output_root", "outputs"))

    def snapshot(self) -> dict[str, Any]:
        connectors = [
            self._binance_usdm(),
            self._tiger_openapi(),
            self._yahoo_chart(),
            self._gold_api(),
        ]
        return {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "checked_at": self._now(),
            "connectors": connectors,
            "summary": {
                "connector_count": len(connectors),
                "ready_price_feed_count": sum(1 for item in connectors if self._capability_ready(item, "price_feed")),
                "ready_broker_count": sum(1 for item in connectors if self._capability_ready(item, "broker_order")),
                "configured_broker_profile_count": len(self.config.get("broker_profiles", {}) or {}),
            },
            "safety": {
                "read_only": True,
                "credential_values_exposed": False,
                "opens_network_clients": False,
                "order_control_endpoint_registered": False,
            },
        }

    def _binance_usdm(self) -> dict[str, Any]:
        feed = self.config.get("binance_usdm_1m_feed", {}) or {}
        profiles = self.config.get("broker_profiles", {}) or {}
        broker_profiles = [name for name, profile in profiles.items() if str(profile.get("provider") or "") == "binance_usdm"]
        credentials = [
            self._env_secret(str((profiles.get("binance_usdm", {}) or {}).get("api_key_env", "BINANCE_API_KEY")), ["broker_order"]),
            self._env_secret(str((profiles.get("binance_usdm", {}) or {}).get("api_secret_env", "BINANCE_API_SECRET")), ["broker_order"]),
        ]
        broker_ready = all(item["present"] for item in credentials)
        return {
            "id": "binance_usdm",
            "label": "Binance USD-M",
            "kind": "exchange",
            "environments": sorted({str((profiles.get(name) or {}).get("environment") or "") for name in broker_profiles if (profiles.get(name) or {}).get("environment")}),
            "roles": ["price_feed", "broker_order"],
            "ports": {
                "price_feed": "datafeed:binance_usdm_futures",
                "broker_order": "services.binance_demo_broker_adapter.BinanceDemoBrokerAdapter",
            },
            "capabilities": [
                {
                    "name": "price_feed",
                    "status": "ready",
                    "summary": "Public/demo XAUUSDT futures feed is configured.",
                    "symbols": [str(feed.get("output_symbol") or "GOLD"), str(feed.get("symbol") or "XAUUSDT")],
                    "timeframes": [str(feed.get("interval") or "1m")],
                    "credential_required": False,
                },
                {
                    "name": "broker_order",
                    "status": "ready" if broker_ready else "needs_credentials",
                    "summary": "Broker order path is profile-gated and uses environment credentials.",
                    "profiles": broker_profiles,
                    "credential_required": True,
                },
            ],
            "credential_requirements": credentials,
            "active": self._active_provider() == "binance_usdm",
        }

    def _tiger_openapi(self) -> dict[str, Any]:
        feed = self.config.get("tiger_futures_feed", {}) or {}
        profile = (self.config.get("broker_profiles", {}) or {}).get("tiger_openapi_paper", {}) or {}
        props_env = str(profile.get("props_path_env") or feed.get("props_path_env") or "TIGER_OPENAPI_CONFIG_PATH")
        credential = self._file_path_secret(props_env, ["price_feed", "broker_order"], config_path=str(profile.get("props_path") or feed.get("props_path") or ""))
        credential_ready = credential["present"] and credential["file_exists"] and credential["owner_only"]
        price_readiness = self._tiger_price_feed_readiness()
        price_acceptance = self._tiger_price_feed_acceptance()
        price_feed_status = self._tiger_price_feed_status(credential_ready, price_readiness)
        allowed_symbols = [str(item) for item in (profile.get("allowed_symbols") or [])]
        return {
            "id": "tiger_openapi",
            "label": "Tiger OpenAPI",
            "kind": "broker",
            "environments": [str(profile.get("environment") or "paper")],
            "roles": ["price_feed", "broker_order"],
            "ports": {
                "price_feed": "datafeed:tiger_openapi_comex",
                "broker_order": "services.tiger_openapi_broker_adapter.TigerOpenApiPaperBrokerAdapter",
            },
            "capabilities": [
                {
                    "name": "price_feed",
                    "status": price_feed_status,
                    "summary": self._tiger_price_feed_summary(price_feed_status, price_readiness, price_acceptance),
                    "symbols": [str(feed.get("contract") or "MGCmain"), str(feed.get("output_symbol") or "MGCmain")],
                    "timeframes": [str(feed.get("period") or "1m")],
                    "credential_required": True,
                    "readiness": {
                        "status": str(price_readiness.get("status") or "missing"),
                        "ready_for_price_feed": price_readiness.get("ready_for_price_feed") is True,
                        "blocker_count": len([row for row in (price_readiness.get("blockers") or []) if isinstance(row, dict)]),
                        "checked_at": str(price_readiness.get("checked_at") or ""),
                    },
                    "acceptance": self._tiger_price_feed_acceptance_summary(price_acceptance),
                },
                {
                    "name": "broker_order",
                    "status": "ready" if credential_ready else "needs_credentials",
                    "summary": "Paper broker adapter exists, but submission remains guarded by profile flags and operator authorization.",
                    "profiles": ["tiger_openapi_paper"],
                    "allowed_symbols": allowed_symbols,
                    "credential_required": True,
                    "requires_operator_authorization": True,
                },
            ],
            "credential_requirements": [credential],
            "active": self._active_provider() == "tiger_openapi",
        }

    def _tiger_price_feed_readiness(self) -> dict[str, Any]:
        rows = load_json(self.output_root / "tiger_price_feed_readiness" / "current.json")
        if rows and isinstance(rows[-1], dict):
            return rows[-1]
        return {}

    def _tiger_price_feed_acceptance(self) -> dict[str, Any]:
        rows = load_json(self.output_root / "tiger_price_feed_acceptance" / "current.json")
        if rows and isinstance(rows[-1], dict):
            return rows[-1]
        return {}

    def _tiger_price_feed_acceptance_summary(self, acceptance: dict[str, Any]) -> dict[str, Any]:
        steps = acceptance.get("steps", {}) if isinstance(acceptance.get("steps"), dict) else {}
        realtime = steps.get("realtime_validation", {}) if isinstance(steps.get("realtime_validation"), dict) else {}
        gate = realtime.get("market_hours_gate", {}) if isinstance(realtime.get("market_hours_gate"), dict) else {}
        action = acceptance.get("operator_next_action", {}) if isinstance(acceptance.get("operator_next_action"), dict) else {}
        next_window = action.get("next_trading_window", {}) if isinstance(action.get("next_trading_window"), dict) else {}
        if not next_window:
            next_window = gate.get("next_trading_window", {}) if isinstance(gate.get("next_trading_window"), dict) else {}
        blockers = [row for row in (acceptance.get("blockers") or []) if isinstance(row, dict)]
        return {
            "status": str(acceptance.get("status") or "missing"),
            "ready_for_price_feed": acceptance.get("ready_for_price_feed") is True,
            "exit_code": acceptance.get("exit_code"),
            "blocker_count": len(blockers),
            "operator_action": str(gate.get("operator_action") or ""),
            "operator_status": str(action.get("status") or ""),
            "operator_summary": str(action.get("summary") or ""),
            "next_command": str(action.get("next_command") or ""),
            "next_trading_window": {
                "start": str(next_window.get("start") or ""),
                "end": str(next_window.get("end") or ""),
                "trading_date": str(next_window.get("trading_date") or ""),
            },
            "can_enable_broker_orders_from_this_gate": acceptance.get("can_enable_broker_orders_from_this_gate") is True,
            "checked_at": str(acceptance.get("checked_at") or ""),
        }

    def _tiger_price_feed_status(self, credential_ready: bool, readiness: dict[str, Any]) -> str:
        if not credential_ready:
            return "needs_credentials"
        if readiness.get("ready_for_price_feed") is True:
            return "ready"
        return "blocked"

    def _tiger_price_feed_summary(self, status: str, readiness: dict[str, Any], acceptance: dict[str, Any]) -> str:
        if status == "ready":
            return "COMEX futures price feed passed historical import and market-hours validation."
        if status == "needs_credentials":
            return "Tiger properties file must be present, existing, and owner-only before price-feed validation."
        if acceptance.get("status") == "pending_market_open":
            return "Tiger credentials are ready, but price-feed acceptance is waiting for the next market-hours validation window."
        if acceptance.get("status") == "blocked":
            return "Tiger credentials are ready, but price-feed acceptance is blocked by local validation artifacts."
        if readiness:
            return "Tiger credentials are ready, but price-feed readiness is still blocked by local validation artifacts."
        return "Tiger credentials are ready, but price-feed readiness artifact is missing."

    def _yahoo_chart(self) -> dict[str, Any]:
        config = self.config.get("gold_5m_backfill", {}) or {}
        return {
            "id": "yahoo_chart",
            "label": "Yahoo Chart",
            "kind": "public_data",
            "environments": ["public"],
            "roles": ["price_feed"],
            "ports": {"price_feed": "services.yahoo_chart_client"},
            "capabilities": [
                {
                    "name": "price_feed",
                    "status": "ready",
                    "summary": "Public fallback chart source for GC futures proxy data.",
                    "symbols": [str(config.get("yahoo_symbol") or "GC=F")],
                    "timeframes": [str(config.get("range") or "5d")],
                    "credential_required": False,
                }
            ],
            "credential_requirements": [],
            "active": False,
        }

    def _gold_api(self) -> dict[str, Any]:
        return {
            "id": "gold_api",
            "label": "gold-api.com",
            "kind": "public_data",
            "environments": ["public"],
            "roles": ["price_feed"],
            "ports": {"price_feed": "services.kline_client"},
            "capabilities": [
                {
                    "name": "price_feed",
                    "status": "ready",
                    "summary": "Public quote source; quotes are not written as synthetic bars.",
                    "symbols": ["XAUUSD", "GOLD"],
                    "timeframes": ["quote"],
                    "credential_required": False,
                }
            ],
            "credential_requirements": [],
            "active": False,
        }

    def _env_secret(self, env_name: str, required_for: list[str]) -> dict[str, Any]:
        value = os.getenv(env_name)
        return {
            "name": env_name,
            "kind": "env_secret",
            "required_for": required_for,
            "present": bool(value) and not is_placeholder_value(value),
            "value_exposed": False,
        }

    def _file_path_secret(self, env_name: str, required_for: list[str], *, config_path: str = "") -> dict[str, Any]:
        resolved = config_path or os.getenv(env_name, "")
        path = Path(resolved).expanduser() if resolved else None
        exists = bool(path and path.exists())
        mode = ""
        owner_only = False
        if path and exists:
            mode_int = stat.S_IMODE(path.stat().st_mode)
            mode = oct(mode_int)
            owner_only = not bool(mode_int & 0o077)
        return {
            "name": env_name,
            "kind": "file_path",
            "required_for": required_for,
            "present": bool(resolved),
            "file_exists": exists,
            "owner_only": owner_only,
            "mode": mode,
            "value_exposed": False,
        }

    def _active_provider(self) -> str:
        broker = self.config.get("broker", {}) or {}
        profile_name = str(self.config.get("broker_profile") or broker.get("profile") or broker.get("broker_profile") or "").strip()
        if profile_name:
            profile = (self.config.get("broker_profiles", {}) or {}).get(profile_name, {}) or {}
            return str(profile.get("provider") or broker.get("provider") or "")
        return str(broker.get("provider") or "")

    def _capability_ready(self, connector: dict[str, Any], capability_name: str) -> bool:
        return any(
            item.get("name") == capability_name and item.get("status") == "ready"
            for item in connector.get("capabilities", [])
            if isinstance(item, dict)
        )

    def _now(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
