from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

from services.live_env import apply_live_env, live_env_value_present


def _epoch_milliseconds() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True, slots=True)
class BinanceUsdmEndpoints:
    order: str = "/fapi/v1/order"
    all_open_orders: str = "/fapi/v1/allOpenOrders"
    open_orders: str = "/fapi/v1/openOrders"
    algo_order: str = "/fapi/v1/algoOrder"
    algo_open_orders: str = "/fapi/v1/algoOpenOrders"
    open_algo_orders: str = "/fapi/v1/openAlgoOrders"
    exchange_info: str = "/fapi/v1/exchangeInfo"


BINANCE_USDM_ENDPOINTS = BinanceUsdmEndpoints()


@dataclass(frozen=True, slots=True)
class BinanceUsdmTransport:
    """Binance USD-M wire protocol with no execution or money authority."""

    broker_config: Mapping[str, Any]
    opener: Callable[..., Any] = urllib.request.urlopen
    clock_ms: Callable[[], int] = _epoch_milliseconds
    endpoints: BinanceUsdmEndpoints = field(default=BINANCE_USDM_ENDPOINTS)

    USER_AGENT: ClassVar[str] = "TradingOrchestrator/1.0"

    def base_url(self) -> str:
        if self.broker_config.get("base_url"):
            return str(self.broker_config["base_url"]).rstrip("/")
        environment = str(self.broker_config.get("environment", "live")).lower()
        return "https://testnet.binancefuture.com" if environment == "testnet" else "https://fapi.binance.com"

    def symbol(self, asset: str) -> str:
        mapping = self.broker_config.get("instrument_map", {"GOLD": "XAUUSDT", "XAUUSD": "XAUUSDT"})
        return str(mapping.get(asset, asset)).upper()

    def signed_request(self, method: str, endpoint: str, params: dict) -> dict:
        api_key, api_secret = self._credentials()
        query, signature = self._signed_query(params, api_secret)
        body = f"{query}&signature={signature}".encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url()}{endpoint}",
            data=body,
            method=method,
            headers={
                "X-MBX-APIKEY": api_key,
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": self.USER_AGENT,
            },
        )
        with self.opener(request, timeout=self._timeout_seconds()) as response:
            return json.loads(response.read().decode("utf-8"))

    def signed_get(self, endpoint: str, params: dict) -> dict:
        request = self._signed_get_request(endpoint, params)
        with self.opener(request, timeout=self._timeout_seconds()) as response:
            return json.loads(response.read().decode("utf-8"))

    def signed_get_envelope(self, endpoint: str, params: dict) -> dict:
        """Return the legacy demo read envelope without duplicating signing."""

        try:
            request = self._signed_get_request(endpoint, params)
            with self.opener(request, timeout=self._timeout_seconds()) as response:
                text = response.read().decode("utf-8")
                return {
                    "ok": True,
                    "status": getattr(response, "status", 200),
                    "body": json.loads(text) if text else {},
                }
        except RuntimeError as exc:
            if str(exc).startswith("missing Binance environment variables:"):
                return {"ok": False, "status": None, "error": {"message": str(exc)}}
            raise
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", errors="replace")
            try:
                error = json.loads(text)
            except json.JSONDecodeError:
                error = {"raw": text[:500]}
            return {"ok": False, "status": exc.code, "error": error}
        except (OSError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            return {
                "ok": False,
                "status": None,
                "error": {"error_type": type(exc).__name__, "message": str(exc)},
            }

    def symbol_status(self, symbol: str) -> dict:
        try:
            params = urllib.parse.urlencode({"symbol": symbol})
            request = urllib.request.Request(
                f"{self.base_url()}{self.endpoints.exchange_info}?{params}",
                headers={"User-Agent": self.USER_AGENT},
            )
            with self.opener(request, timeout=self._timeout_seconds()) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, TimeoutError, json.JSONDecodeError, KeyError, ValueError):
            return {
                "symbol": symbol,
                "status": "UNKNOWN",
                "filters": {},
                "truth_level": "exchange_info_unavailable",
            }
        symbols = payload.get("symbols") or []
        item = next((row for row in symbols if row.get("symbol") == symbol), symbols[0] if symbols else {})
        filters = {row.get("filterType"): row for row in item.get("filters", []) if row.get("filterType")}
        lot = filters.get("LOT_SIZE", {})
        market_lot = filters.get("MARKET_LOT_SIZE", {})
        price_filter = filters.get("PRICE_FILTER", {})
        min_notional = filters.get("MIN_NOTIONAL", {})
        return {
            "symbol": item.get("symbol", symbol),
            "status": item.get("status", "UNKNOWN"),
            "contract_type": item.get("contractType", ""),
            "underlying_type": item.get("underlyingType", ""),
            "margin_asset": item.get("marginAsset", ""),
            "filters": {
                "tick_size": price_filter.get("tickSize", "0.01"),
                "step_size": market_lot.get("stepSize") or lot.get("stepSize", "0.001"),
                "min_qty": market_lot.get("minQty") or lot.get("minQty", "0.001"),
                "min_notional": min_notional.get("notional", "5"),
            },
            "truth_level": "official_binance_exchange_info",
        }

    def protective_order_endpoint(self, *, use_algo_orders: bool) -> str:
        return self.endpoints.algo_order if use_algo_orders else self.endpoints.order

    def uses_algo_protective_orders(self) -> bool:
        value = str(self.broker_config.get("protective_order_endpoint", "order")).lower()
        return value in {"algo", "algo_order", "algoorder", self.endpoints.algo_order.lower()}

    def _signed_get_request(self, endpoint: str, params: dict) -> urllib.request.Request:
        api_key, api_secret = self._credentials()
        query, signature = self._signed_query(params, api_secret)
        return urllib.request.Request(
            f"{self.base_url()}{endpoint}?{query}&signature={signature}",
            method="GET",
            headers={"X-MBX-APIKEY": api_key, "User-Agent": self.USER_AGENT},
        )

    def _signed_query(self, params: dict, api_secret: str) -> tuple[str, str]:
        signed = {
            **params,
            "timestamp": self.clock_ms(),
            "recvWindow": int(self.broker_config.get("recv_window_ms", 5000)),
        }
        query = urllib.parse.urlencode(signed)
        signature = hmac.new(api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
        return query, signature

    def _credentials(self) -> tuple[str, str]:
        apply_live_env()
        api_key_env = str(self.broker_config.get("api_key_env", "BINANCE_API_KEY"))
        secret_env = str(self.broker_config.get("api_secret_env", "BINANCE_API_SECRET"))
        api_key = os.getenv(api_key_env)
        api_secret = os.getenv(secret_env)
        if not live_env_value_present(api_key_env) or not live_env_value_present(secret_env):
            raise RuntimeError(f"missing Binance environment variables: {api_key_env}, {secret_env}")
        return str(api_key), str(api_secret)

    def _timeout_seconds(self) -> int:
        return int(self.broker_config.get("timeout_seconds", 10))
