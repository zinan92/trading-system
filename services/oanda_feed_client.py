from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

from schemas.market_data import Bar
from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import write_json
from services.live_env import apply_live_env, live_env_value_present
from services.market_store import MarketStore
from services.market_data_access import uses_independent_datafeed
from services.market_data_refresh import refresh_market_data


class OandaFeedClient:
    def __init__(
        self,
        store: MarketStore,
        config: dict | None = None,
        opener=None,
    ) -> None:
        self.store = store
        self.config = config or load_pipeline_config().get("oanda_feed", {})
        self.opener = opener or urllib.request.urlopen

    def preflight(self) -> dict:
        env = apply_live_env()
        token_env = str(self.config.get("token_env", "OANDA_API_TOKEN"))
        account_env = str(self.config.get("account_id_env", "OANDA_ACCOUNT_ID"))
        missing = [name for name in [token_env, account_env] if not live_env_value_present(name)]
        return {
            "provider": "oanda",
            "ready": not missing,
            "missing_env": missing,
            "token_env": token_env,
            "account_id_env": account_env,
            "env_file": env["path"],
            "env_file_exists": env["exists"],
            "base_url": self._base_url(),
            "instrument": self.instrument,
            "granularity": self.granularity,
            "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        }

    def fetch_and_store(self, count: int | None = None) -> dict:
        preflight = self.preflight()
        if not preflight["ready"]:
            return {
                **preflight,
                "status": "skipped",
                "message": "OANDA credentials missing; set OANDA_API_TOKEN and OANDA_ACCOUNT_ID to import official 5m bars",
                "imported_rows": 0,
                "coverage": self._coverage(),
            }
        try:
            bars = self.fetch(count=count)
        except (urllib.error.URLError, TimeoutError, OSError, RuntimeError, json.JSONDecodeError) as exc:
            return {
                **preflight,
                "status": "fail",
                "message": f"OANDA fetch failed: {exc}",
                "imported_rows": 0,
                "latest_timestamp": "",
                "latest_price": None,
                "coverage": self._coverage(),
            }
        self.store.upsert_bars(bars)
        return {
            **preflight,
            "status": "pass" if bars else "warn",
            "message": "imported OANDA complete candles" if bars else "OANDA returned no complete candles",
            "imported_rows": len(bars),
            "latest_timestamp": bars[-1].timestamp if bars else "",
            "latest_price": bars[-1].close if bars else None,
            "coverage": self._coverage(),
        }

    def fetch(self, count: int | None = None) -> list[Bar]:
        apply_live_env()
        token = os.getenv(str(self.config.get("token_env", "OANDA_API_TOKEN")))
        if not live_env_value_present(str(self.config.get("token_env", "OANDA_API_TOKEN"))):
            raise RuntimeError("OANDA_API_TOKEN is required")
        params = {
            "price": str(self.config.get("price", "M")),
            "granularity": self.granularity,
            "count": str(count or int(self.config.get("count", 500))),
        }
        if self.config.get("from"):
            params["from"] = str(self.config["from"])
        if self.config.get("to"):
            params["to"] = str(self.config["to"])
        if self.config.get("includeFirst") is not None:
            params["includeFirst"] = str(self.config["includeFirst"]).lower()
        url = f"{self._base_url()}/v3/instruments/{urllib.parse.quote(self.instrument, safe='')}/candles?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept-Datetime-Format": "RFC3339",
                "User-Agent": "TradingOrchestrator/1.0",
            },
        )
        with self.opener(request, timeout=int(self.config.get("timeout_seconds", 10))) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return self._parse_bars(payload)

    @property
    def instrument(self) -> str:
        return str(self.config.get("instrument", "XAU_USD"))

    @property
    def granularity(self) -> str:
        return str(self.config.get("granularity", "M5"))

    def _parse_bars(self, payload: dict) -> list[Bar]:
        bars = []
        for item in payload.get("candles", []):
            if item.get("complete") is False:
                continue
            mid = item.get("mid") or {}
            if not all(key in mid for key in ["o", "h", "l", "c"]):
                continue
            bars.append(
                Bar(
                    symbol=str(self.config.get("symbol", "GOLD")),
                    timeframe=str(self.config.get("timeframe", "5m")),
                    timestamp=self._normalize_timestamp(str(item["time"])),
                    open=float(mid["o"]),
                    high=float(mid["h"]),
                    low=float(mid["l"]),
                    close=float(mid["c"]),
                    volume=float(item.get("volume") or 0),
                    provider="oanda",
                    quality_flags=["oanda_rest", "official_broker_feed"],
                )
            )
        return bars

    def _normalize_timestamp(self, value: str) -> str:
        # OANDA returns nanosecond-precision timestamps (e.g.
        # "2026-05-26T03:00:00.000000000Z") that datetime.fromisoformat refuses
        # because it only accepts up to 6 fractional digits. Truncate any
        # sub-microsecond tail before parsing.
        normalized = value.replace("Z", "+00:00")
        normalized = re.sub(r"(\.\d{6})\d+", r"\1", normalized)
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()

    def _base_url(self) -> str:
        environment = str(self.config.get("environment", "practice")).lower()
        if self.config.get("base_url"):
            return str(self.config["base_url"]).rstrip("/")
        if environment == "live":
            return "https://api-fxtrade.oanda.com"
        return "https://api-fxpractice.oanda.com"

    def _coverage(self) -> list[dict]:
        return [
            item
            for item in self.store.coverage()
            if item["symbol"] == str(self.config.get("symbol", "GOLD")) and item["timeframe"] == str(self.config.get("timeframe", "5m"))
        ]


def run_oanda_feed_import(run_date: str, output_root: Path | None = None, market_db: Path | None = None) -> dict:
    config = load_pipeline_config()
    output_root = output_root or Path(os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(ROOT / config.get("output_root", "outputs"))))
    market_db = market_db or Path(os.getenv("TRADING_ORCHESTRATOR_MARKET_DB", str(ROOT / config.get("local_market_db", "data/market_data.db"))))
    if uses_independent_datafeed(market_db):
        try:
            return refresh_market_data(
                run_date=run_date,
                symbol="GOLD",
                timeframe="5m",
                output_kind="oanda_feed",
                source="oanda_v20",
                output_root=output_root,
            )
        except Exception as error:
            result = {
                "run_date": run_date,
                "status": "skipped",
                "message": f"OANDA datafeed adapter is not available: {error}",
                "imported_rows": 0,
                "market_data_backend": "datafeed",
            }
            write_json(output_root / "oanda_feed" / "current.json", [result])
            write_json(output_root / "oanda_feed" / f"{run_date}.json", [result])
            return result
    result = OandaFeedClient(MarketStore(market_db), config.get("oanda_feed", {})).fetch_and_store()
    result["run_date"] = run_date
    result["market_db"] = str(market_db)
    write_json(output_root / "oanda_feed" / "current.json", [result])
    write_json(output_root / "oanda_feed" / f"{run_date}.json", [result])
    return result
