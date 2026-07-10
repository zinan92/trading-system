from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from services.config_loader import load_pipeline_config
from services.live_env import apply_live_env, live_env_value_present


class BinanceAccountCostObserver:
    """Read account fee rates and public funding without placing orders."""

    def __init__(
        self,
        *,
        config: dict[str, Any] | None = None,
        opener: Callable[..., Any] = urllib.request.urlopen,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        pipeline = config or load_pipeline_config()
        self.config = dict(pipeline.get("broker") or {})
        self.opener = opener
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    def observe(self, symbol: str = "XAUUSDT") -> dict[str, Any]:
        apply_live_env()
        api_key_env = str(self.config.get("api_key_env") or "BINANCE_API_KEY")
        secret_env = str(self.config.get("api_secret_env") or "BINANCE_API_SECRET")
        if not live_env_value_present(api_key_env) or not live_env_value_present(secret_env):
            raise RuntimeError("Binance account fee observation requires configured API credentials")
        api_key = str(os.getenv(api_key_env) or "")
        api_secret = str(os.getenv(secret_env) or "")
        commission = self._signed_get(
            "/fapi/v1/commissionRate",
            {"symbol": symbol},
            api_key=api_key,
            api_secret=api_secret,
        )
        funding_rows = self._get("/fapi/v1/fundingRate", {"symbol": symbol, "limit": 1})
        if not isinstance(commission, dict):
            raise RuntimeError("Binance commissionRate returned an invalid payload")
        maker = _rate(commission.get("makerCommissionRate"), "maker fee", nonnegative=True)
        taker = _rate(commission.get("takerCommissionRate"), "taker fee", nonnegative=True)
        funding = funding_rows[-1] if isinstance(funding_rows, list) and funding_rows else {}
        if not isinstance(funding, dict) or funding.get("fundingTime") in (None, ""):
            raise RuntimeError("Binance fundingRate omitted the latest funding settlement")
        funding_rate = _rate(funding.get("fundingRate"), "funding", nonnegative=False)
        observed_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        environment = str(self.config.get("environment") or "unknown")
        return {
            "schema_version": "dualtrack-binance-account-costs-v1",
            "status": "ok",
            "provider": "binance_usdm",
            "environment": environment,
            "base_url": self._base_url(),
            "symbol": symbol,
            "maker_fee_rate": maker,
            "taker_fee_rate": taker,
            "fee_source": "authenticated_account_commissionRate",
            "funding_rate": funding_rate,
            "funding_time": funding.get("fundingTime"),
            "funding_source": "public_fundingRate" if funding else "unavailable",
            "observed_at": observed_at,
            "read_only": True,
            "real_money_eligible": False,
        }

    def _signed_get(
        self,
        endpoint: str,
        params: dict[str, Any],
        *,
        api_key: str,
        api_secret: str,
    ) -> Any:
        signed = {
            **params,
            "timestamp": self.clock_ms(),
            "recvWindow": int(self.config.get("recv_window_ms") or 5000),
        }
        query = urllib.parse.urlencode(signed)
        signature = hmac.new(api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
        request = urllib.request.Request(
            f"{self._base_url()}{endpoint}?{query}&signature={signature}",
            headers={"X-MBX-APIKEY": api_key, "User-Agent": "TradingOrchestrator/1.0"},
            method="GET",
        )
        return self._read(request)

    def _get(self, endpoint: str, params: dict[str, Any]) -> Any:
        query = urllib.parse.urlencode(params)
        request = urllib.request.Request(
            f"{self._base_url()}{endpoint}?{query}",
            headers={"User-Agent": "TradingOrchestrator/1.0"},
            method="GET",
        )
        return self._read(request)

    def _read(self, request: urllib.request.Request) -> Any:
        with self.opener(request, timeout=int(self.config.get("timeout_seconds") or 10)) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if isinstance(payload, dict) and payload.get("code") not in (None, 0, 200):
            raise RuntimeError(f"Binance read-only cost endpoint failed: code={payload.get('code')} msg={payload.get('msg')}")
        return payload

    def _base_url(self) -> str:
        return str(self.config.get("base_url") or "https://fapi.binance.com").rstrip("/")


def _rate(value: Any, label: str, *, nonnegative: bool) -> str:
    rendered = str(value or "").strip()
    if not rendered:
        raise RuntimeError(f"Binance endpoint omitted {label} rate")
    try:
        parsed = Decimal(rendered)
    except InvalidOperation as exc:
        raise RuntimeError(f"Binance endpoint returned invalid {label} rate") from exc
    if not parsed.is_finite() or (nonnegative and parsed < 0):
        raise RuntimeError(f"Binance endpoint returned invalid {label} rate")
    return rendered
