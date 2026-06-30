from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import write_json
from services.live_env import apply_live_env, live_env_value_present


class OandaAccountPreflight:
    def __init__(
        self,
        config: dict | None = None,
        output_root: Path | None = None,
        opener=None,
    ) -> None:
        pipeline_config = load_pipeline_config()
        self.config = config or pipeline_config.get("oanda_feed", {})
        self.output_root = output_root or Path(os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(ROOT / pipeline_config.get("output_root", "outputs"))))
        self.opener = opener or urllib.request.urlopen

    def run(self, run_date: str) -> dict:
        base = self.preflight_base(run_date)
        if not base["ready"]:
            payload = {
                **base,
                "status": "skipped",
                "account_ready": False,
                "instrument_ready": False,
                "message": "OANDA credentials missing; account and instrument preflight skipped.",
                "account": {},
                "instrument": {},
            }
            self._write(run_date, payload)
            return payload
        try:
            account = self.fetch_account_summary()
            instrument = self.fetch_instrument()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, RuntimeError, json.JSONDecodeError) as exc:
            payload = {
                **base,
                "status": "fail",
                "account_ready": False,
                "instrument_ready": False,
                "message": f"OANDA account preflight failed: {exc}",
                "account": {},
                "instrument": {},
            }
            self._write(run_date, payload)
            return payload
        account_ready = bool(account.get("account_id"))
        instrument_ready = bool(instrument.get("name") == self.instrument)
        payload = {
            **base,
            "status": "pass" if account_ready and instrument_ready else "fail",
            "account_ready": account_ready,
            "instrument_ready": instrument_ready,
            "message": "OANDA account and XAU_USD instrument are reachable." if account_ready and instrument_ready else "OANDA account reachable, but configured instrument was not found.",
            "account": account,
            "instrument": instrument,
        }
        self._write(run_date, payload)
        return payload

    def preflight_base(self, run_date: str) -> dict:
        env = apply_live_env()
        token_env = str(self.config.get("token_env", "OANDA_API_TOKEN"))
        account_env = str(self.config.get("account_id_env", "OANDA_ACCOUNT_ID"))
        missing = [name for name in [token_env, account_env] if not live_env_value_present(name)]
        return {
            "run_date": run_date,
            "provider": "oanda",
            "ready": not missing,
            "missing_env": missing,
            "token_env": token_env,
            "account_id_env": account_env,
            "env_file": env["path"],
            "env_file_exists": env["exists"],
            "base_url": self._base_url(),
            "instrument_name": self.instrument,
            "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "note": "Credential values are intentionally not written to artifacts.",
        }

    def fetch_account_summary(self) -> dict:
        payload = self._get_json(f"{self._base_url()}/v3/accounts/{urllib.parse.quote(self.account_id, safe='')}/summary")
        account = payload.get("account", {})
        return {
            "account_id": account.get("id", ""),
            "currency": account.get("currency", ""),
            "balance": self._number_or_none(account.get("balance")),
            "NAV": self._number_or_none(account.get("NAV")),
            "margin_available": self._number_or_none(account.get("marginAvailable")),
            "open_trade_count": self._int_or_none(account.get("openTradeCount")),
            "pending_order_count": self._int_or_none(account.get("pendingOrderCount")),
            "hedging_enabled": account.get("hedgingEnabled"),
        }

    def fetch_instrument(self) -> dict:
        params = urllib.parse.urlencode({"instruments": self.instrument})
        payload = self._get_json(f"{self._base_url()}/v3/accounts/{urllib.parse.quote(self.account_id, safe='')}/instruments?{params}")
        instruments = payload.get("instruments", [])
        match = next((item for item in instruments if item.get("name") == self.instrument), {})
        return {
            "name": match.get("name", ""),
            "display_name": match.get("displayName", ""),
            "type": match.get("type", ""),
            "trade_units_precision": self._int_or_none(match.get("tradeUnitsPrecision")),
            "minimum_trade_size": self._number_or_none(match.get("minimumTradeSize")),
            "margin_rate": self._number_or_none(match.get("marginRate")),
        }

    def _get_json(self, url: str) -> dict:
        token_env = str(self.config.get("token_env", "OANDA_API_TOKEN"))
        token = os.getenv(token_env)
        if not live_env_value_present(token_env):
            raise RuntimeError("OANDA_API_TOKEN is required")
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept-Datetime-Format": "RFC3339",
                "User-Agent": "TradingOrchestrator/1.0",
            },
        )
        with self.opener(request, timeout=int(self.config.get("timeout_seconds", 10))) as response:
            return json.loads(response.read().decode("utf-8"))

    @property
    def account_id(self) -> str:
        account_env = str(self.config.get("account_id_env", "OANDA_ACCOUNT_ID"))
        value = os.getenv(account_env)
        if not live_env_value_present(account_env):
            raise RuntimeError("OANDA_ACCOUNT_ID is required")
        return value

    @property
    def instrument(self) -> str:
        return str(self.config.get("instrument", "XAU_USD"))

    def _base_url(self) -> str:
        environment = str(self.config.get("environment", "practice")).lower()
        if self.config.get("base_url"):
            return str(self.config["base_url"]).rstrip("/")
        if environment == "live":
            return "https://api-fxtrade.oanda.com"
        return "https://api-fxpractice.oanda.com"

    def _write(self, run_date: str, payload: dict) -> None:
        write_json(self.output_root / "oanda_account" / "current.json", [payload])
        write_json(self.output_root / "oanda_account" / f"{run_date}.json", [payload])

    def _number_or_none(self, value) -> float | None:
        if value is None or value == "":
            return None
        return float(value)

    def _int_or_none(self, value) -> int | None:
        if value is None or value == "":
            return None
        return int(value)


def run_oanda_account_preflight(run_date: str, output_root: Path | None = None) -> dict:
    return OandaAccountPreflight(output_root=output_root).run(run_date)
