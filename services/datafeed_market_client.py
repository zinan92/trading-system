"""Read-only client for the independent datafeed market-data port."""

from __future__ import annotations

import json
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class DatafeedUnavailable(RuntimeError):
    pass


class DatafeedMarketClient:
    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:8100",
        timeout_seconds: float = 10.0,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.opener = opener

    def candles(
        self,
        *,
        asset_class: str,
        ticker: str,
        timeframe: str,
        limit: int,
        source: str,
        cache_policy: str = "allow",
        quality: str = "standard",
        require_execution_venue: bool = False,
        start: str | None = None,
        end: str | None = None,
    ) -> dict:
        params: dict[str, Any] = {
            "timeframe": timeframe,
            "limit": max(1, min(int(limit), 60000)),
            "source": source,
            "cache_policy": cache_policy,
            "quality": quality,
            "fallback_policy": "none",
            "require_execution_venue": str(require_execution_venue).lower(),
        }
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        path = f"/api/candles/{asset_class}/{ticker}"
        return self._get(path, params)

    def health(self) -> dict:
        return self._get("/api/health", {})

    def sessions(
        self,
        *,
        asset_class: str,
        ticker: str,
        source: str,
        trading_date: str | None = None,
    ) -> dict:
        params = {"source": source}
        if trading_date:
            params["trading_date"] = trading_date
        return self._get(f"/api/sessions/{asset_class}/{ticker}", params)

    def _get(self, path: str, params: dict[str, Any]) -> dict:
        query = f"?{urlencode(params, doseq=True)}" if params else ""
        request = Request(
            f"{self.base_url}{path}{query}",
            headers={"Accept": "application/json", "User-Agent": "TradingOrchestrator/1.0"},
        )
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                status = int(getattr(response, "status", 200) or 200)
                raw = response.read()
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise DatafeedUnavailable(f"datafeed HTTP {error.code}: {detail}") from error
        except (OSError, URLError, TimeoutError) as error:
            raise DatafeedUnavailable(f"datafeed unavailable: {error}") from error
        if status != 200:
            raise DatafeedUnavailable(f"datafeed returned HTTP {status}")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DatafeedUnavailable("datafeed returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise DatafeedUnavailable("datafeed response must be an object")
        return payload
