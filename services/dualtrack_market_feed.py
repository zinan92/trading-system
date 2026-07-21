from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.datafeed_market_client import DatafeedMarketClient, DatafeedUnavailable
from services.market_data_access import market_data_repository


class DualTrackMarketFeed:
    """Read-only dashboard market bars snapshot.

    The dual-track dashboard should not open browser-side exchange sockets or
    initialize writer-capable feed clients. This reader uses SQLite read-only
    mode and reads exactly one requested or configured source. Missing or stale
    data is surfaced as such; it never switches provider or creates seed bars.
    """

    def __init__(
        self,
        market_db: Path | None = None,
        config: dict | None = None,
        datafeed_client: DatafeedMarketClient | None = None,
    ) -> None:
        self.config = config if config is not None else load_pipeline_config()
        datafeed_config = self.config.get("datafeed", {}) or {}
        self.datafeed_enabled = bool(datafeed_config.get("enabled", False))
        self.datafeed_source = str(datafeed_config.get("source") or "binance_usdm_futures")
        self.datafeed_asset_class = str(datafeed_config.get("asset_class") or "commodity")
        self.datafeed_client = datafeed_client or DatafeedMarketClient(
            base_url=str(datafeed_config.get("base_url") or "http://127.0.0.1:8100"),
            timeout_seconds=float(datafeed_config.get("timeout_seconds", 10)),
        )
        default_db = ROOT / str(self.config.get("local_market_db", "data/market_data.db"))
        self.market_db = self._resolve_market_db(
            market_db
            or os.getenv("TRADING_ORCHESTRATOR_MARKET_DB")
            or default_db
        )

    def snapshot(
        self,
        *,
        symbol: str | None = None,
        timeframe: str | None = None,
        limit: int = 96,
        as_of: str | None = None,
        end: str | None = None,
    ) -> dict:
        if self.datafeed_enabled:
            return self._datafeed_snapshot(
                symbol=symbol,
                timeframe=timeframe,
                limit=limit,
                as_of=as_of,
                end=end,
            )
        resolved_limit = max(1, min(int(limit), 500))
        checked_at = self._parse_as_of(as_of)
        requested = {
            "symbol": symbol or "",
            "timeframe": timeframe or "",
            "limit": resolved_limit,
        }
        if end:
            requested["end"] = end
        candidates = self._candidates(symbol=symbol, timeframe=timeframe)
        access_issues: list[str] = []
        stale_exact: dict | None = None
        if self.market_db.exists():
            for candidate in candidates:
                try:
                    load_limit = resolved_limit + 1 if end and resolved_limit < 500 else resolved_limit
                    bars = self._load_bars(candidate["symbol"], candidate["timeframe"], load_limit, end=end)
                except OSError as exc:
                    access_issues.append(f"{type(exc).__name__}: {exc}")
                    bars = []
                if bars:
                    has_more = bool(end and len(bars) > resolved_limit)
                    if end:
                        bars = bars[-resolved_limit:]
                    freshness = self._freshness(bars[-1], checked_at=checked_at)
                    if freshness["fresh"]:
                        return self._history_metadata(
                            self._payload("ready", candidate, bars, requested, access_issues, freshness=freshness),
                            requested_limit=resolved_limit,
                            historical=bool(end),
                            has_more=has_more,
                        )
                    access_issues.append(
                        f"stale_exact_source:{candidate['symbol']}:{candidate['timeframe']}:"
                        f"{bars[-1].get('timestamp', '')}"
                    )
                    stale_exact = self._payload("stale", candidate, bars, requested, access_issues, freshness=freshness)
            for derived in self._derived_candidates(candidates, symbol=symbol, timeframe=timeframe, limit=resolved_limit):
                freshness = self._freshness(derived["bars"][-1], checked_at=checked_at)
                if not freshness["fresh"] and not symbol:
                    access_issues.append(
                        f"stale_source:{derived['source']['symbol']}:{derived['source']['timeframe']}:"
                        f"{derived['bars'][-1].get('timestamp', '')}"
                    )
                    continue
                return self._payload(
                    "derived" if freshness["fresh"] else "stale",
                    derived["source"],
                    derived["bars"],
                    requested,
                    [*access_issues, derived["issue"]],
                    freshness=freshness,
                )
            if stale_exact is not None:
                return stale_exact
        else:
            access_issues.append("market_db_missing")

        primary = candidates[0] if candidates else {}
        access_issues.append(
            f"market_source_unavailable:{primary.get('symbol') or symbol or ''}:{primary.get('timeframe') or timeframe or ''}"
        )
        unavailable = {
            "symbol": primary.get("symbol") or symbol or "",
            "timeframe": timeframe or "1m",
            "provider": primary.get("provider") or "",
            "source_mode": "unavailable",
        }
        return self._payload(
            "blocked",
            unavailable,
            [],
            requested,
            access_issues,
            freshness={"fresh": False, "age_minutes": None, "max_age_minutes": self._max_age_minutes(timeframe)},
        )

    def _datafeed_snapshot(
        self,
        *,
        symbol: str | None,
        timeframe: str | None,
        limit: int,
        as_of: str | None,
        end: str | None,
    ) -> dict:
        resolved_symbol = symbol or "GOLD"
        resolved_timeframe = timeframe or "1m"
        resolved_limit = max(1, min(int(limit), 60000))
        requested = {
            "symbol": symbol or "",
            "timeframe": timeframe or "",
            "limit": resolved_limit,
        }
        historical = bool(end)
        if end:
            requested["end"] = end
        upstream_limit = resolved_limit + 1 if historical and resolved_limit < 60000 else resolved_limit
        response: dict | None = None
        request_errors: list[str] = []
        attempts = 1 if historical else 2
        for _attempt in range(attempts):
            try:
                response = self.datafeed_client.candles(
                    asset_class=self.datafeed_asset_class,
                    ticker=resolved_symbol,
                    timeframe=resolved_timeframe,
                    limit=upstream_limit,
                    source=self.datafeed_source,
                    cache_policy="require" if historical else "bypass",
                    quality="standard" if historical else "strict",
                    require_execution_venue=True,
                    end=end,
                )
                break
            except DatafeedUnavailable as error:
                request_errors.append(str(error))
        if response is None:
            return self._history_metadata({
                "schema_version": "dualtrack-market-bars-v1",
                "status": "blocked",
                "source_mode": "unavailable",
                "symbol": resolved_symbol,
                "timeframe": resolved_timeframe,
                "provider": "",
                "quality_flags": ["market_unavailable"],
                "is_synthetic": False,
                "requested": requested,
                "bar_count": 0,
                "latest_timestamp": "",
                "latest_close": None,
                "fresh": False,
                "age_minutes": None,
                "max_age_minutes": self._max_age_minutes(resolved_timeframe),
                "bars": [],
                "access_issues": request_errors,
                "datafeed": self.datafeed_client.base_url,
                "safety": self._datafeed_safety(),
            }, requested_limit=resolved_limit, historical=historical)

        response_candles = response.get("candles", [])
        has_more = bool(historical and len(response_candles) > resolved_limit)
        if historical:
            response_candles = response_candles[-resolved_limit:]
        bars = [
            {
                "symbol": response.get("instrument_id") or resolved_symbol,
                "provider_symbol": response.get("provider_symbol") or response.get("ticker"),
                "timeframe": resolved_timeframe,
                "timestamp": candle.get("timestamp"),
                "open": candle.get("open"),
                "high": candle.get("high"),
                "low": candle.get("low"),
                "close": candle.get("close"),
                "volume": candle.get("volume", 0),
                "provider": response.get("provider", ""),
                "quality_flags": candle.get("quality_flags") or response.get("quality_flags") or [],
            }
            for candle in response_candles
        ]
        freshness = self._freshness(
            bars[-1] if bars else {"timeframe": resolved_timeframe},
            checked_at=self._parse_as_of(as_of),
        )
        trusted_history = bool(
            historical
            and bars
            and not bool(response.get("is_synthetic", False))
            and "mock" not in str(response.get("provider") or "").lower()
            and "synthetic" not in str(response.get("provider") or "").lower()
        )
        status = "ready" if bars and (freshness["fresh"] or trusted_history) else "stale" if bars else "blocked"
        return self._history_metadata({
            "schema_version": "dualtrack-market-bars-v1",
            "status": status,
            "source_mode": response.get("selected_source") or response.get("source_mode") or "",
            "symbol": response.get("instrument_id") or resolved_symbol,
            "provider_symbol": response.get("provider_symbol") or response.get("ticker") or "",
            "timeframe": resolved_timeframe,
            "provider": response.get("provider", ""),
            "quality_flags": response.get("quality_flags") or [],
            "is_synthetic": bool(response.get("is_synthetic", False)),
            "requested": requested,
            "bar_count": len(bars),
            "latest_timestamp": bars[-1].get("timestamp", "") if bars else "",
            "latest_close": bars[-1].get("close") if bars else None,
            "fresh": freshness["fresh"],
            "age_minutes": freshness["age_minutes"],
            "max_age_minutes": freshness["max_age_minutes"],
            "bars": bars,
            "access_issues": response.get("access_issues") or [],
            "datafeed": self.datafeed_client.base_url,
            "selection_reason": response.get("selection_reason"),
            "attempted_sources": response.get("attempted_sources") or [],
            "safety": self._datafeed_safety(),
        }, requested_limit=resolved_limit, historical=historical, trusted_history=trusted_history, has_more=has_more)

    @staticmethod
    def _datafeed_safety() -> dict:
        return {
            "read_only": True,
            "writes_market_db": False,
            "opens_broker_clients": False,
            "opens_order_clients": False,
            "uses_browser_exchange_socket": False,
            "reads_private_market_db": False,
        }

    def _candidates(self, *, symbol: str | None, timeframe: str | None) -> list[dict]:
        if symbol:
            return [{
                "symbol": symbol,
                "timeframe": timeframe or "1m",
                "provider": "local_market_db",
                "source_mode": "requested_symbol",
            }]

        binance = self.config.get("binance_usdm_1m_feed", {}) or {}
        requested_timeframe = timeframe or ""
        return [
            {
                "symbol": str(binance.get("output_symbol") or "GOLD"),
                "timeframe": requested_timeframe or str(binance.get("timeframe") or binance.get("interval") or "1m"),
                "provider": str(binance.get("provider") or "binance_usdm"),
                "source_mode": "binance_usdm",
            },
        ]

    def _derived_candidates(self, candidates: list[dict], *, symbol: str | None, timeframe: str | None, limit: int) -> list[dict]:
        if symbol:
            derived = self._derived_bars(symbol=symbol, timeframe=timeframe, limit=limit)
            return [derived] if derived else []
        if not timeframe:
            return []
        rows: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for candidate in candidates:
            key = (str(candidate["symbol"]), str(timeframe))
            if key in seen:
                continue
            seen.add(key)
            derived = self._derived_bars(symbol=candidate["symbol"], timeframe=timeframe, limit=limit)
            if derived:
                rows.append(derived)
        return rows

    def _load_bars(self, symbol: str, timeframe: str, limit: int, *, end: str | None = None) -> list[dict]:
        repository = market_data_repository(self.market_db)
        if end:
            rows = repository.load_bars_between(symbol, timeframe, "1970-01-01T00:00:00+00:00", end)[-limit:]
        else:
            rows = repository.load_bars(symbol, timeframe, limit)
        return [
            bar.to_dict()
            for bar in rows
        ]

    @staticmethod
    def _history_metadata(
        payload: dict,
        *,
        requested_limit: int,
        historical: bool,
        trusted_history: bool | None = None,
        has_more: bool | None = None,
    ) -> dict:
        bars = payload.get("bars") or []
        trusted = bool(
            historical
            and bars
            and not payload.get("is_synthetic")
            and (trusted_history if trusted_history is not None else True)
        )
        return {
            **payload,
            "historical_page": historical,
            "trusted_history": trusted,
            "pagination": {
                "has_more": bool(
                    historical and (
                        has_more if has_more is not None else len(bars) >= requested_limit
                    )
                ),
                "next_before": bars[0].get("timestamp", "") if bars else "",
                "oldest_timestamp": bars[0].get("timestamp", "") if bars else "",
                "newest_timestamp": bars[-1].get("timestamp", "") if bars else "",
            },
        }

    def _derived_bars(self, *, symbol: str | None, timeframe: str | None, limit: int) -> dict | None:
        if not symbol or not timeframe:
            return None
        target_seconds = self._timeframe_seconds(timeframe)
        if target_seconds is None or target_seconds <= 60:
            return None
        for source_timeframe in ("1m", "5m"):
            source_seconds = self._timeframe_seconds(source_timeframe)
            if source_seconds is None or target_seconds % source_seconds:
                continue
            # D1 ATR14 requires more than 30k one-minute rows once the current
            # incomplete day and weekend filtering are accounted for.
            source_limit = min(max(limit * int(target_seconds / source_seconds) + int(target_seconds / source_seconds), limit), 60_000)
            source_bars = self._load_bars(symbol, source_timeframe, source_limit)
            if not source_bars:
                continue
            bars = self._aggregate_bars(source_bars, timeframe=timeframe, target_seconds=target_seconds)[-limit:]
            if not bars:
                continue
            return {
                "source": {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "provider": bars[-1].get("provider") or f"derived:{source_timeframe}",
                    "source_mode": f"derived_from_{source_timeframe}",
                },
                "bars": bars,
                "issue": f"derived_source:{symbol}:{source_timeframe}->{timeframe}",
            }
        return None

    def _aggregate_bars(self, bars: list[dict], *, timeframe: str, target_seconds: int) -> list[dict]:
        buckets: dict[int, dict] = {}
        order: list[int] = []
        for bar in bars:
            parsed = datetime.fromisoformat(str(bar["timestamp"]).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            parsed = parsed.astimezone(timezone.utc)
            bucket_epoch = int(parsed.timestamp()) // target_seconds * target_seconds
            bucket = buckets.get(bucket_epoch)
            if bucket is None:
                order.append(bucket_epoch)
                bucket_start = datetime.fromtimestamp(bucket_epoch, tz=timezone.utc).replace(microsecond=0)
                bucket = {
                    "symbol": bar["symbol"],
                    "timeframe": timeframe,
                    "timestamp": bucket_start.isoformat(),
                    "open": float(bar["open"]),
                    "high": float(bar["high"]),
                    "low": float(bar["low"]),
                    "close": float(bar["close"]),
                    "volume": float(bar.get("volume") or 0.0),
                    "provider": f"derived:{bar.get('provider') or 'local_market_db'}",
                    "quality_flags": [f"derived_timeframe:{timeframe}", f"source_timeframe:{bar.get('timeframe') or ''}"],
                }
                buckets[bucket_epoch] = bucket
                continue
            bucket["high"] = max(float(bucket["high"]), float(bar["high"]))
            bucket["low"] = min(float(bucket["low"]), float(bar["low"]))
            bucket["close"] = float(bar["close"])
            bucket["volume"] = float(bucket["volume"]) + float(bar.get("volume") or 0.0)
            provider = str(bar.get("provider") or "")
            if provider and provider not in str(bucket["provider"]):
                bucket["provider"] = f"{bucket['provider']}+{provider}"
        return [buckets[key] for key in order]

    def _payload(
        self,
        status: str,
        source: dict,
        bars: list[dict],
        requested: dict,
        access_issues: list[str],
        freshness: dict | None = None,
    ) -> dict:
        latest = bars[-1] if bars else {}
        quality_flags = list(latest.get("quality_flags") or [])
        if status == "blocked" and not bars:
            quality_flags = ["market_unavailable"]
        provider = latest.get("provider") or source["provider"]
        is_synthetic = self._is_synthetic_source({**source, "provider": provider}, quality_flags=quality_flags)
        return {
            "schema_version": "dualtrack-market-bars-v1",
            "status": status,
            "source_mode": source["source_mode"],
            "symbol": latest.get("symbol") or source["symbol"],
            "timeframe": latest.get("timeframe") or source["timeframe"],
            "provider": provider,
            "quality_flags": quality_flags,
            "is_synthetic": is_synthetic,
            "requested": requested,
            "bar_count": len(bars),
            "latest_timestamp": latest.get("timestamp", ""),
            "latest_close": latest.get("close"),
            "fresh": bool((freshness or {}).get("fresh", False)),
            "age_minutes": (freshness or {}).get("age_minutes"),
            "max_age_minutes": (freshness or {}).get("max_age_minutes"),
            "bars": bars,
            "access_issues": access_issues,
            "market_db": str(self.market_db),
            "safety": {
                "read_only": True,
                "writes_market_db": False,
                "opens_broker_clients": False,
                "opens_order_clients": False,
                "uses_browser_exchange_socket": False,
            },
        }

    def _resolve_market_db(self, value: Path | str) -> Path:
        path = Path(value).expanduser()
        if path.is_absolute():
            return path
        return ROOT / path

    def _is_synthetic_source(self, source: dict, *, quality_flags: list[str]) -> bool:
        provider = str(source.get("provider") or "").lower()
        mode = str(source.get("source_mode") or "").lower()
        flags = " ".join(str(item).lower() for item in quality_flags)
        return "synthetic" in provider or "synthetic" in mode or "synthetic_seed" in flags

    def _parse_as_of(self, as_of: str | None) -> datetime:
        if as_of:
            parsed = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).replace(second=0, microsecond=0)
        return datetime.now(timezone.utc).replace(second=0, microsecond=0)

    def _freshness(self, latest: dict, *, checked_at: datetime) -> dict:
        timestamp = latest.get("timestamp")
        if not timestamp:
            return {"fresh": False, "age_minutes": None, "max_age_minutes": self._max_age_minutes(latest.get("timeframe"))}
        parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        parsed = parsed.astimezone(timezone.utc)
        age = max(0.0, (checked_at - parsed).total_seconds() / 60.0)
        max_age = self._max_age_minutes(latest.get("timeframe"))
        return {"fresh": age <= max_age, "age_minutes": round(age, 2), "max_age_minutes": max_age}

    def _max_age_minutes(self, timeframe: str | None) -> float:
        value = str(timeframe or "").strip().lower()
        if value.endswith("m"):
            try:
                return max(3.0, float(value[:-1]) * 3.0)
            except ValueError:
                return 3.0
        if value.endswith("h"):
            try:
                return max(120.0, float(value[:-1]) * 180.0)
            except ValueError:
                return 120.0
        if value.endswith("d"):
            try:
                return max(4_320.0, float(value[:-1]) * 4_320.0)
            except ValueError:
                return 4_320.0
        return 3.0

    def _timeframe_seconds(self, timeframe: str | None) -> int | None:
        value = str(timeframe or "").strip().lower()
        if value.endswith("m"):
            try:
                return int(float(value[:-1]) * 60)
            except ValueError:
                return None
        if value.endswith("h"):
            try:
                return int(float(value[:-1]) * 3600)
            except ValueError:
                return None
        if value.endswith("d"):
            try:
                return int(float(value[:-1]) * 86_400)
            except ValueError:
                return None
        return None

    def _fallback_symbol(self) -> str:
        tiger = self.config.get("tiger_futures_feed", {}) or {}
        return str(tiger.get("output_symbol") or tiger.get("contract") or "MGCmain")
