"""Read-only client for the datafeed execution-market-v1 endpoint."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class ExecutionMarketUnavailable(RuntimeError):
    """The execution-grade market source could not provide a usable payload."""


class DatafeedExecutionMarketClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout_seconds: float = 5.0,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        self.base_url = (base_url or os.getenv("TRADING_ORCHESTRATOR_DATAFEED_URL") or "http://127.0.0.1:8100").rstrip("/")
        self.timeout_seconds = max(0.5, float(timeout_seconds))
        self.opener = opener

    def read(self, *, venue: str, instrument_id: str) -> dict[str, Any]:
        path = f"/api/execution-market/{quote(str(venue), safe='')}/{quote(str(instrument_id), safe='')}"
        request = Request(
            f"{self.base_url}{path}",
            headers={"Accept": "application/json", "User-Agent": "TradingOrchestrator/1.0"},
        )
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                status = int(getattr(response, "status", 200) or 200)
                raw = response.read()
        except HTTPError as error:
            raise ExecutionMarketUnavailable(f"execution market HTTP {error.code}") from error
        except (OSError, URLError, TimeoutError) as error:
            raise ExecutionMarketUnavailable(f"execution market unavailable: {error}") from error
        if status != 200:
            raise ExecutionMarketUnavailable(f"execution market returned HTTP {status}")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ExecutionMarketUnavailable("execution market returned invalid JSON") from error
        if not isinstance(payload, dict) or payload.get("schema_version") != "execution-market-v1":
            raise ExecutionMarketUnavailable("execution market schema mismatch")
        return dict(payload)


def market_source_mode() -> str:
    mode = os.getenv("TRADING_ORCHESTRATOR_MARKET_SOURCE", "direct").strip().lower()
    if mode not in {"direct", "dual", "datafeed"}:
        raise ValueError("TRADING_ORCHESTRATOR_MARKET_SOURCE must be direct, dual, or datafeed")
    return mode


def execution_market_payload(
    payload: Mapping[str, Any],
    *,
    source: str,
    provider: str,
    environment: str,
    instrument_id: str,
    symbol: str,
) -> dict[str, Any]:
    provenance = payload.get("provenance") if isinstance(payload.get("provenance"), Mapping) else {}
    return {
        "price": payload.get("price"),
        "trusted": payload.get("trusted") is True,
        "fresh": payload.get("fresh") is True,
        "source": source,
        "provider": provider,
        "environment": environment,
        "instrument_id": instrument_id,
        "symbol": symbol,
        "observed_at": str(payload.get("observed_at") or ""),
        "age_seconds": payload.get("age_seconds"),
        "reason": str(payload.get("reason") or ""),
        "raw_status": "ready" if payload.get("fresh") is True else "blocked",
        "execution_market": True,
        "provenance": dict(provenance),
    }


def write_compare_receipt(
    output_root: Path | str,
    *,
    mode: str,
    direct: Mapping[str, Any],
    datafeed: Mapping[str, Any],
) -> dict[str, Any]:
    direct_price = direct.get("price")
    datafeed_price = datafeed.get("price")
    try:
        price_delta = float(datafeed_price) - float(direct_price)
    except (TypeError, ValueError):
        price_delta = None
    def age(value: Mapping[str, Any]) -> Any:
        if value.get("age_seconds") is not None:
            return value.get("age_seconds")
        if value.get("age_minutes") is not None:
            return value.get("age_minutes") * 60
        observed_at = str(value.get("observed_at") or "").strip()
        if observed_at:
            try:
                observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
                return max(0.0, (datetime.now(timezone.utc) - observed).total_seconds())
            except ValueError:
                pass
        return None
    direct_age, datafeed_age = age(direct), age(datafeed)
    age_delta = None if direct_age is None or datafeed_age is None else float(datafeed_age) - float(direct_age)
    receipt = {
        "schema_version": "park-market-source-compare-v1",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "direct": {key: direct.get(key) for key in ("price", "observed_at", "fresh", "age_seconds")},
        "datafeed": {key: datafeed.get(key) for key in ("price", "observed_at", "fresh", "age_seconds")},
        "price_delta": price_delta,
        "age_delta_seconds": age_delta,
        "fresh_equal": direct.get("fresh") == datafeed.get("fresh"),
    }
    path = Path(output_root) / "park_strategy" / "market_source_compare.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(receipt, ensure_ascii=False, sort_keys=True) + "\n")
    return receipt


__all__ = [
    "DatafeedExecutionMarketClient",
    "ExecutionMarketUnavailable",
    "execution_market_payload",
    "market_source_mode",
    "write_compare_receipt",
]
