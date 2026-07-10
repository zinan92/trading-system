from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from services.config_loader import ROOT, load_pipeline_config


class DualTrackMarketFeed:
    """Read-only dashboard market bars snapshot.

    The dual-track dashboard should not open browser-side exchange sockets or
    initialize writer-capable feed clients. This reader uses SQLite read-only
    mode and reads exactly one requested or configured source. Missing or stale
    data is surfaced as such; it never switches provider or creates seed bars.
    """

    def __init__(self, market_db: Path | None = None, config: dict | None = None) -> None:
        self.config = config or load_pipeline_config()
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
    ) -> dict:
        resolved_limit = max(1, min(int(limit), 500))
        checked_at = self._parse_as_of(as_of)
        requested = {
            "symbol": symbol or "",
            "timeframe": timeframe or "",
            "limit": resolved_limit,
        }
        candidates = self._candidates(symbol=symbol, timeframe=timeframe)
        access_issues: list[str] = []
        if self.market_db.exists():
            for candidate in candidates:
                try:
                    bars = self._load_bars(candidate["symbol"], candidate["timeframe"], resolved_limit)
                except sqlite3.Error as exc:
                    access_issues.append(f"{type(exc).__name__}: {exc}")
                    bars = []
                if bars:
                    freshness = self._freshness(bars[-1], checked_at=checked_at)
                    status = "ready" if freshness["fresh"] else "stale"
                    return self._payload(status, candidate, bars, requested, access_issues, freshness=freshness)
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

    def _candidates(self, *, symbol: str | None, timeframe: str | None) -> list[dict]:
        if symbol:
            return [{
                "symbol": symbol,
                "timeframe": timeframe or "1m",
                "provider": "local_market_db",
                "source_mode": "requested_symbol",
            }]

        tiger = self.config.get("tiger_futures_feed", {}) or {}
        requested_timeframe = timeframe or ""
        return [
            {
                "symbol": str(tiger.get("output_symbol") or tiger.get("contract") or "MGCmain"),
                "timeframe": requested_timeframe or str(tiger.get("timeframe") or tiger.get("period") or "1m"),
                "provider": str(tiger.get("provider") or "tiger_openapi:COMEX"),
                "source_mode": "tiger_openapi",
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

    def _load_bars(self, symbol: str, timeframe: str, limit: int) -> list[dict]:
        uri = f"file:{quote(str(self.market_db.resolve()))}?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            rows = conn.execute(
                """
                SELECT symbol, timeframe, timestamp, open, high, low, close, volume, provider, quality_flags
                FROM bars
                WHERE symbol = ? AND timeframe = ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (symbol, timeframe, limit),
            ).fetchall()
        return [
            {
                "symbol": row[0],
                "timeframe": row[1],
                "timestamp": row[2],
                "open": float(row[3]),
                "high": float(row[4]),
                "low": float(row[5]),
                "close": float(row[6]),
                "volume": float(row[7]),
                "provider": row[8],
                "quality_flags": [item for item in str(row[9]).split(",") if item],
            }
            for row in reversed(rows)
        ]

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
            source_limit = min(max(limit * int(target_seconds / source_seconds) + int(target_seconds / source_seconds), limit), 30_000)
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
        return None

    def _fallback_symbol(self) -> str:
        tiger = self.config.get("tiger_futures_feed", {}) or {}
        return str(tiger.get("output_symbol") or tiger.get("contract") or "MGCmain")
