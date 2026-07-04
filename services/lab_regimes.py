"""Coverage and regime labeling for Strategy Lab."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from statistics import pstdev

from schemas.market_data import Bar
from services.journal_store import write_json


def build_coverage_report(bars: list[Bar]) -> dict:
    ordered = sorted(bars, key=lambda item: item.timestamp)
    if not ordered:
        return {"status": "invalid", "reason": "no_bars", "bars": 0, "gaps": []}
    gaps: list[dict] = []
    by_day: dict[str, int] = defaultdict(int)
    by_hour: dict[str, int] = defaultdict(int)
    previous = _parse_ts(ordered[0].timestamp)
    for bar in ordered[1:]:
        current = _parse_ts(bar.timestamp)
        delta = int((current - previous).total_seconds())
        if delta > 90:
            missing = max(0, delta // 60 - 1)
            day = previous.date().isoformat()
            hour = previous.strftime("%Y-%m-%dT%H:00:00+00:00")
            by_day[day] += missing
            by_hour[hour] += missing
            gaps.append({"start": previous.isoformat(), "end": current.isoformat(), "missing_minutes": missing})
        previous = current
    return {
        "status": "valid" if not gaps else "warn",
        "bars": len(ordered),
        "first_timestamp": ordered[0].timestamp,
        "last_timestamp": ordered[-1].timestamp,
        "gap_count": len(gaps),
        "missing_minutes": sum(item["missing_minutes"] for item in gaps),
        "gaps": gaps[:500],
        "gaps_truncated": len(gaps) > 500,
        "missing_by_day": dict(sorted(by_day.items())),
        "missing_by_hour": dict(sorted(by_hour.items())),
    }


def label_regimes(bars: list[Bar]) -> dict:
    ordered = sorted(bars, key=lambda item: item.timestamp)
    day_blocks = _blocks(ordered, "day")
    four_hour_blocks = _blocks(ordered, "4h")
    labeled_days = _label_blocks(day_blocks)
    labeled_4h = _label_blocks(four_hour_blocks)
    return {
        "status": "valid" if labeled_days and labeled_4h else "invalid",
        "days": labeled_days,
        "four_hour_blocks": labeled_4h,
        "summary": {
            "day_share": _share(labeled_days),
            "four_hour_share": _share(labeled_4h),
        },
    }


def write_regime_artifact(output_root, name: str, bars: list[Bar]) -> dict:
    payload = {
        "coverage": build_coverage_report(bars),
        "regimes": label_regimes(bars),
    }
    write_json(output_root / "lab" / "regimes" / f"{name}.json", [payload])
    return payload


def _blocks(bars: list[Bar], grain: str) -> list[dict]:
    grouped: dict[str, list[Bar]] = defaultdict(list)
    for bar in bars:
        ts = _parse_ts(bar.timestamp)
        if grain == "day":
            key = ts.date().isoformat()
        else:
            bucket = ts.replace(hour=(ts.hour // 4) * 4, minute=0, second=0, microsecond=0)
            key = bucket.isoformat()
        grouped[key].append(bar)
    return [{"key": key, "bars": rows} for key, rows in sorted(grouped.items()) if len(rows) >= 2]


def _label_blocks(blocks: list[dict]) -> list[dict]:
    raw = []
    for block in blocks:
        bars = block["bars"]
        returns = [(float(bars[i].close) - float(bars[i - 1].close)) / float(bars[i - 1].close) for i in range(1, len(bars)) if bars[i - 1].close]
        vol = pstdev(returns) if len(returns) >= 2 else 0.0
        atr = sum(float(bar.high) - float(bar.low) for bar in bars) / len(bars)
        trend = abs(float(bars[-1].close) - float(bars[0].open)) / atr if atr else 0.0
        raw.append({"key": block["key"], "bar_count": len(bars), "realized_vol": vol, "trend_atr": trend})
    vol_edges = _terciles([item["realized_vol"] for item in raw])
    trend_edges = _terciles([item["trend_atr"] for item in raw])
    out = []
    for item in raw:
        vol_bucket = _bucket(item["realized_vol"], vol_edges, ("low", "mid", "high"))
        trend_bucket = _bucket(item["trend_atr"], trend_edges, ("quiet", "directional", "strong_directional"))
        out.append({
            **item,
            "volatility_bucket": vol_bucket,
            "trend_bucket": trend_bucket,
            "tradeable_directional": trend_bucket in {"directional", "strong_directional"} and vol_bucket != "high",
        })
    return out


def _share(rows: list[dict]) -> dict:
    total = len(rows)
    tradeable = sum(1 for item in rows if item.get("tradeable_directional"))
    by_regime: dict[str, int] = defaultdict(int)
    for item in rows:
        by_regime[f"{item['volatility_bucket']}|{item['trend_bucket']}"] += 1
    return {
        "total": total,
        "tradeable_directional": tradeable,
        "tradeable_directional_pct": round(tradeable / total * 100, 6) if total else 0.0,
        "by_regime": dict(sorted(by_regime.items())),
    }


def _terciles(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    ordered = sorted(values)
    return ordered[len(ordered) // 3], ordered[(len(ordered) * 2) // 3]


def _bucket(value: float, edges: tuple[float, float], labels: tuple[str, str, str]) -> str:
    if value <= edges[0]:
        return labels[0]
    if value <= edges[1]:
        return labels[1]
    return labels[2]


def _parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0)
