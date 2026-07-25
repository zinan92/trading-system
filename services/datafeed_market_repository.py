"""Trading-domain read model backed only by the independent datafeed API."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from schemas.market_data import Bar, MarketDataEnvelope
from services.config_loader import load_pipeline_config
from services.datafeed_market_client import DatafeedMarketClient
from services.datafeed_market_mapper import map_candle_response


class DatafeedMarketRepository:
    def __init__(self, client: DatafeedMarketClient | None = None, config: dict | None = None) -> None:
        pipeline = config if config is not None else load_pipeline_config()
        datafeed = pipeline.get("datafeed", {}) or {}
        self.client = client or DatafeedMarketClient(
            base_url=str(datafeed.get("base_url") or "http://127.0.0.1:8100"),
            timeout_seconds=float(datafeed.get("timeout_seconds", 10)),
        )
        self.routes = datafeed.get("instrument_routes", {}) or {
            "GOLD": {
                "asset_class": "commodity",
                "ticker": "GOLD",
                "source": str(datafeed.get("source") or "binance_usdm_futures"),
            },
            "MGCmain": {
                "asset_class": "commodity",
                "ticker": "MGCmain",
                "source": "tiger_openapi_comex",
            },
        }

    def load_envelope(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        *,
        start: str | None = None,
        end: str | None = None,
    ) -> MarketDataEnvelope:
        """Return the authoritative versioned trust envelope for one read."""

        route = self._route(symbol)
        source = str(route["source"])
        require_execution_venue = bool(
            route.get("require_execution_venue", False)
        )
        historical = bool(start or end)
        cache_policy = str(
            route.get(
                "historical_cache_policy" if historical else "live_cache_policy"
            )
            or route.get("cache_policy")
            or "require"
        )
        quality_policy = str(
            route.get(
                "historical_quality_policy" if historical else "live_quality_policy"
            )
            or route.get("quality_policy")
            or route.get("quality")
            or "standard"
        )
        payload = self.client.candles(
            asset_class=str(route["asset_class"]),
            ticker=str(route.get("ticker") or symbol),
            timeframe=timeframe,
            limit=limit,
            source=source,
            cache_policy=cache_policy,
            quality=quality_policy,
            require_execution_venue=require_execution_venue,
            start=start,
            end=end,
        )
        return map_candle_response(
            payload,
            expected_asset_class=str(route["asset_class"]),
            expected_timeframe=timeframe,
            expected_source=source,
            require_execution_venue=require_execution_venue,
        )

    def load_bars(self, symbol: str, timeframe: str, limit: int) -> list[Bar]:
        return list(self.load_envelope(symbol, timeframe, limit).bars)

    def load_bars_between(
        self,
        symbol: str,
        timeframe: str,
        start_timestamp: str,
        end_timestamp: str,
    ) -> list[Bar]:
        return list(
            self.load_envelope(
                symbol,
                timeframe,
                60_000,
                start=start_timestamp,
                end=end_timestamp,
            ).bars
        )

    def load_latest_bar(
        self, symbol: str, timeframe: str, providers: list[str] | None = None
    ) -> dict:
        bars = self.load_bars(symbol, timeframe, 1)
        if not bars:
            return {}
        bar = bars[-1]
        if providers and bar.provider not in providers and self._source(symbol) not in providers:
            return {}
        return {**bar.to_dict(), "record_type": "bar"}

    def load_latest_quote(self, symbol: str) -> dict:
        latest = self.load_latest_bar(symbol, "1m")
        if not latest:
            return {}
        return {
            "symbol": latest["symbol"],
            "timestamp": latest["timestamp"],
            "close": latest["close"],
            "provider": latest["provider"],
            "quality_flags": latest["quality_flags"],
            "record_type": "quote",
        }

    def load_bar_at_or_before(self, symbol: str, timeframe: str, timestamp: str) -> dict:
        bars = self.load_envelope(
            symbol,
            timeframe,
            1,
            end=timestamp,
        ).bars
        return {**bars[-1].to_dict(), "record_type": "bar"} if bars else {}

    def load_aggregated_bars_between(
        self,
        symbol: str,
        source_timeframe: str,
        target_timeframe: str,
        start_timestamp: str,
        end_timestamp: str,
        target_seconds: int,
        limit: int,
    ) -> list[dict]:
        bars = self.load_bars_between(symbol, source_timeframe, start_timestamp, end_timestamp)
        buckets: dict[int, dict] = {}
        for bar in bars:
            parsed = datetime.fromisoformat(bar.timestamp.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            bucket_epoch = int(parsed.timestamp()) // target_seconds * target_seconds
            bucket = buckets.get(bucket_epoch)
            if bucket is None:
                bucket = {
                    "symbol": symbol,
                    "timeframe": target_timeframe,
                    "timestamp": datetime.fromtimestamp(
                        bucket_epoch, tz=timezone.utc
                    ).isoformat(),
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                    "provider": f"derived:{bar.provider}",
                    "quality_flags": sorted(
                        set(bar.quality_flags)
                        | {"derived_timeframe", f"source_{source_timeframe}"}
                    ),
                    "bucket_end": datetime.fromtimestamp(
                        bucket_epoch + target_seconds, tz=timezone.utc
                    ).isoformat(),
                }
                buckets[bucket_epoch] = bucket
            else:
                bucket["high"] = max(bucket["high"], bar.high)
                bucket["low"] = min(bucket["low"], bar.low)
                bucket["close"] = bar.close
                bucket["volume"] += bar.volume
        return [buckets[key] for key in sorted(buckets)][-limit:]

    def coverage(self) -> list[dict]:
        health = self.client.health()
        return [
            {
                "symbol": row.get("instrument_id") or row.get("ticker"),
                "timeframe": row.get("timeframe"),
                "provider": row.get("source_id"),
                "rows": int(row.get("count") or 0),
                "first_timestamp": row.get("first_timestamp"),
                "last_timestamp": row.get("latest_timestamp"),
            }
            for row in health.get("storage_coverage", [])
        ]

    def health(self) -> dict:
        return self.client.health()

    def _route(self, symbol: str) -> dict[str, Any]:
        route = self.routes.get(symbol)
        if not isinstance(route, dict):
            raise KeyError(f"No datafeed instrument route configured for {symbol}")
        return route

    def _source(self, symbol: str) -> str:
        return str(self._route(symbol)["source"])
