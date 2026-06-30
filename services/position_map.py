from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.indicators import ema
from services.journal_store import load_json, write_json


class GoldPositionMap:
    """Daily price-location map for gold.

    This is deliberately a planning/risk context layer, not a replacement for
    the signal engine. It answers: is price near a meaningful higher/lower
    timeframe level, and should a small-timeframe trigger be considered today?
    """

    TIMEFRAMES = ("1m", "5m", "1h", "4h")

    def __init__(self, output_root: Path | None = None, proximity_pct: float = 0.25) -> None:
        config = load_pipeline_config()
        self.output_root = output_root or ROOT / config.get("output_root", "outputs")
        self.proximity_pct = proximity_pct

    def build(self, run_date: str, strategy_id: str | None = None) -> dict:
        row_sets: dict[str, list[dict]] = {}
        sources: dict[str, str] = {}
        for timeframe in self.TIMEFRAMES:
            rows, source_path = self._rows(run_date, timeframe, strategy_id)
            if rows:
                row_sets[timeframe] = rows
                sources[timeframe] = source_path
        self._derive_missing_higher_timeframes(row_sets, sources)
        frames = {}
        for timeframe in self.TIMEFRAMES:
            frame = self._frame(timeframe, row_sets.get(timeframe, []), sources.get(timeframe, ""))
            if frame:
                frames[timeframe] = frame
        return self._write_payload(run_date, strategy_id, frames)

    def build_from_timeframes(self, run_date: str, strategy_id: str | None, timeframe_rows: dict[str, list]) -> dict:
        row_sets: dict[str, list[dict]] = {}
        sources: dict[str, str] = {}
        for timeframe in self.TIMEFRAMES:
            rows = self._normalize_rows(timeframe_rows.get(timeframe, []))
            if rows:
                row_sets[timeframe] = rows
                sources[timeframe] = f"in_memory:{timeframe}"
        self._derive_missing_higher_timeframes(row_sets, sources)
        frames = {}
        for timeframe in self.TIMEFRAMES:
            frame = self._frame(timeframe, row_sets.get(timeframe, []), sources.get(timeframe, ""))
            if frame:
                frames[timeframe] = frame
        return self._write_payload(run_date, strategy_id, frames)

    def _write_payload(self, run_date: str, strategy_id: str | None, frames: dict) -> dict:
        primary = self._primary_frame(frames)
        price = float(primary.get("latest_close", 0) or 0) if primary else 0.0
        nearest = self._nearest_levels(price, frames)
        trade_nearest = self._nearest_trade_levels(price, frames)
        near_key_level = bool(trade_nearest and trade_nearest[0]["distance_pct"] <= self.proximity_pct)
        location = primary.get("location", "unknown") if primary else "unknown"
        payload = {
            "run_date": run_date,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "symbol": "GOLD",
            "strategy_id": strategy_id or "",
            "status": "ready" if frames else "insufficient_data",
            "primary_timeframe": primary.get("timeframe", "") if primary else "",
            "latest_price": price or None,
            "location": location,
            "frames": frames,
            "nearest_levels": nearest[:8],
            "nearest_trade_levels": trade_nearest[:6],
            "trade_zone": {
                "near_key_level": near_key_level,
                "proximity_threshold_pct": self.proximity_pct,
                "allow_small_timeframe_trigger": bool(near_key_level and location in {"low", "middle", "high"}),
                "bias": self._bias(location, trade_nearest or nearest),
                "blocks": [] if near_key_level else ["price is not near higher-timeframe EMA50/Fibonacci/range level"],
            },
        }
        write_json(self.output_root / "position_maps" / f"{run_date}.json", [payload])
        write_json(self.output_root / "position_maps" / "current.json", [payload])
        return payload

    def gate_signal(self, signal: dict, position_map: dict) -> dict:
        raw = str(signal.get("direction", "watch"))
        if raw not in {"long", "short"}:
            return {
                "raw_direction": raw,
                "effective_direction": "watch",
                "allow_candidate": False,
                "reason": signal.get("thesis") or "no actionable signal",
                "position_map_status": position_map.get("status", "unknown"),
            }
        if position_map.get("status") != "ready":
            return self._blocked(raw, "position map is not ready", position_map)
        trade_zone = position_map.get("trade_zone") or {}
        if not trade_zone.get("allow_small_timeframe_trigger"):
            return self._blocked(raw, "; ".join(trade_zone.get("blocks") or ["not in a key price location"]), position_map)
        bias = str(trade_zone.get("bias", "neutral"))
        if raw == "long" and bias == "short_bias":
            return self._blocked(raw, "long trigger appears in a high/resistance-biased zone", position_map)
        if raw == "short" and bias == "long_bias":
            return self._blocked(raw, "short trigger appears in a low/support-biased zone", position_map)
        return {
            "raw_direction": raw,
            "effective_direction": raw,
            "allow_candidate": True,
            "reason": f"{raw} trigger is near a key price location",
            "position_map_status": position_map.get("status", "unknown"),
            "nearest_level": (position_map.get("nearest_levels") or [{}])[0],
            "bias": bias,
        }

    def _blocked(self, raw: str, reason: str, position_map: dict) -> dict:
        return {
            "raw_direction": raw,
            "effective_direction": "watch",
            "allow_candidate": False,
            "reason": reason,
            "position_map_status": position_map.get("status", "unknown"),
            "nearest_level": (position_map.get("nearest_levels") or [{}])[0],
            "bias": (position_map.get("trade_zone") or {}).get("bias", "neutral"),
        }

    def _rows(self, run_date: str, timeframe: str, strategy_id: str | None) -> tuple[list[dict], str]:
        candidates = []
        if strategy_id:
            candidates.append(self.output_root / "strategies" / strategy_id / "clean_bars" / run_date / f"GOLD_{timeframe}.json")
        candidates.append(self.output_root / "clean_bars" / run_date / f"GOLD_{timeframe}.json")
        for path in candidates:
            rows = load_json(path)
            if rows:
                return rows, str(path)
        return [], ""

    def _frame(self, timeframe: str, rows: list[dict], source_path: str) -> dict:
        if len(rows) < 10:
            return {}
        recent = rows[-min(len(rows), 500) :]
        closes = [float(item["close"]) for item in recent]
        highs = [float(item["high"]) for item in recent]
        lows = [float(item["low"]) for item in recent]
        latest = closes[-1]
        swing_high = max(highs)
        swing_low = min(lows)
        span = swing_high - swing_low
        position_pct = ((latest - swing_low) / span * 100.0) if span > 0 else 50.0
        ema50 = ema(closes, 50)[-1]
        fibs = self._fib_levels(swing_low, swing_high)
        return {
            "timeframe": timeframe,
            "source_path": source_path,
            "bar_count": len(rows),
            "latest_timestamp": rows[-1].get("timestamp", ""),
            "latest_close": round(latest, 4),
            "ema50": round(ema50, 4),
            "ema50_distance_pct": self._pct(latest, ema50),
            "swing_high": round(swing_high, 4),
            "swing_low": round(swing_low, 4),
            "range_high": round(swing_high, 4),
            "range_low": round(swing_low, 4),
            "range_position_pct": round(position_pct, 2),
            "location": self._location(position_pct),
            "fib_levels": fibs,
        }

    def _normalize_rows(self, rows: list) -> list[dict]:
        normalized = []
        for item in rows:
            if hasattr(item, "to_dict"):
                normalized.append(item.to_dict())
            else:
                normalized.append(dict(item))
        return normalized

    def _derive_missing_higher_timeframes(self, row_sets: dict[str, list[dict]], sources: dict[str, str]) -> None:
        for target in ("1h", "4h"):
            if target in row_sets:
                continue
            for source in ("1m", "5m", "1h"):
                if source == target or source not in row_sets:
                    continue
                derived = self._aggregate_rows(row_sets[source], target)
                if derived:
                    row_sets[target] = derived
                    sources[target] = f"derived:{target}:from:{source}"
                    break

    def _aggregate_rows(self, rows: list[dict], target_timeframe: str) -> list[dict]:
        bucketed: dict[datetime, dict] = {}
        for item in sorted(rows, key=lambda row: str(row.get("timestamp", ""))):
            timestamp = self._parse_timestamp(str(item.get("timestamp", "")))
            if timestamp is None:
                continue
            bucket = self._bucket_start(timestamp, target_timeframe)
            if bucket is None:
                continue
            close = float(item["close"])
            high = float(item["high"])
            low = float(item["low"])
            volume = float(item.get("volume", 0) or 0)
            current = bucketed.get(bucket)
            if current is None:
                bucketed[bucket] = {
                    "symbol": item.get("symbol", "GOLD"),
                    "timeframe": target_timeframe,
                    "timestamp": bucket.isoformat(),
                    "open": float(item["open"]),
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": volume,
                    "provider": f"derived_from_{item.get('timeframe', '') or 'lower_tf'}",
                    "quality_flags": ["derived_timeframe", f"source_timeframe:{item.get('timeframe', '') or 'unknown'}"],
                }
                continue
            current["high"] = max(float(current["high"]), high)
            current["low"] = min(float(current["low"]), low)
            current["close"] = close
            current["volume"] = float(current.get("volume", 0) or 0) + volume
        return [bucketed[key] for key in sorted(bucketed)]

    def _parse_timestamp(self, raw: str) -> datetime | None:
        if not raw:
            return None
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _bucket_start(self, timestamp: datetime, target_timeframe: str) -> datetime | None:
        if target_timeframe == "1h":
            return timestamp.replace(minute=0, second=0, microsecond=0)
        if target_timeframe == "4h":
            hour = (timestamp.hour // 4) * 4
            return timestamp.replace(hour=hour, minute=0, second=0, microsecond=0)
        return None

    def _fib_levels(self, low: float, high: float) -> dict:
        if high <= low:
            return {}
        span = high - low
        return {
            f"fib_{ratio:g}": round(low + span * ratio, 4)
            for ratio in (0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0)
        }

    def _nearest_levels(self, price: float, frames: dict) -> list[dict]:
        if price <= 0:
            return []
        levels = []
        for timeframe, frame in frames.items():
            raw_levels = {
                "ema50": frame.get("ema50"),
                "swing_high": frame.get("swing_high"),
                "swing_low": frame.get("swing_low"),
                **(frame.get("fib_levels") or {}),
            }
            for name, value in raw_levels.items():
                if value is None:
                    continue
                level = float(value)
                levels.append(
                    {
                        "timeframe": timeframe,
                        "name": name,
                        "price": round(level, 4),
                        "side": "support" if level < price else ("resistance" if level > price else "at_price"),
                        "distance_pct": self._pct(price, level),
                    }
                )
        return sorted(levels, key=lambda item: (item["distance_pct"], item["timeframe"], item["name"]))

    def _nearest_trade_levels(self, price: float, frames: dict) -> list[dict]:
        if price <= 0:
            return []
        preferred_timeframes = [timeframe for timeframe in ("4h", "1h") if timeframe in frames]
        timeframes = preferred_timeframes or [timeframe for timeframe in ("5m", "1m") if timeframe in frames]
        levels = []
        for timeframe in timeframes:
            frame = frames[timeframe]
            raw_levels = {
                "ema50": frame.get("ema50"),
                "range_high": frame.get("range_high", frame.get("swing_high")),
                "range_low": frame.get("range_low", frame.get("swing_low")),
            }
            fibs = frame.get("fib_levels") or {}
            for name in ("fib_0.382", "fib_0.5", "fib_0.618"):
                raw_levels[name] = fibs.get(name)
            for name, value in raw_levels.items():
                if value is None:
                    continue
                level = float(value)
                levels.append(
                    {
                        "timeframe": timeframe,
                        "name": name,
                        "price": round(level, 4),
                        "side": "support" if level < price else ("resistance" if level > price else "at_price"),
                        "distance_pct": self._pct(price, level),
                    }
                )
        return sorted(levels, key=lambda item: (item["distance_pct"], item["timeframe"], item["name"]))

    def _primary_frame(self, frames: dict) -> dict:
        for timeframe in ("4h", "1h", "5m", "1m"):
            if timeframe in frames:
                return frames[timeframe]
        return next(iter(frames.values()), {})

    def _location(self, position_pct: float) -> str:
        if position_pct <= 38.2:
            return "low"
        if position_pct >= 61.8:
            return "high"
        return "middle"

    def _bias(self, location: str, nearest: list[dict]) -> str:
        if location == "low":
            return "long_bias"
        if location == "high":
            return "short_bias"
        if nearest:
            return "long_bias" if nearest[0].get("side") == "support" else "short_bias" if nearest[0].get("side") == "resistance" else "neutral"
        return "neutral"

    def _pct(self, price: float, level: float) -> float:
        return round(abs(float(price) - float(level)) / float(price) * 100.0, 4) if price else 0.0
