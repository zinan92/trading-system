"""Deterministic oracle labels for Strategy Lab R2."""

from __future__ import annotations

from dataclasses import dataclass

from schemas.market_data import Bar
from services.lab_evaluator import timeframe_seconds


@dataclass(frozen=True)
class TripleBarrierConfig:
    horizon_minutes: int = 240
    target_atr: float = 2.0
    stop_atr: float = 1.0
    atr_period: int = 14


def triple_barrier_labels(bars: list[Bar], config: TripleBarrierConfig | None = None) -> list[dict]:
    cfg = config or TripleBarrierConfig()
    if not bars:
        return []
    horizon_bars = max(1, int(round(cfg.horizon_minutes * 60 / timeframe_seconds(bars[0].timeframe))))
    atr_values = atr_series(bars, cfg.atr_period)
    labels: list[dict] = []
    for index, bar in enumerate(bars):
        atr = atr_values[index]
        label = "none"
        if atr > 0 and index + 1 < len(bars):
            label = _triple_barrier_label(bars, index, atr, cfg, horizon_bars)
        labels.append({
            "timestamp": bar.timestamp,
            "label": label,
            "horizon_bars": horizon_bars,
            "horizon_minutes": cfg.horizon_minutes,
            "atr": round(atr, 8),
            "target_atr": cfg.target_atr,
            "stop_atr": cfg.stop_atr,
        })
    return labels


def zigzag_swing_labels(bars: list[Bar], threshold_pct: float = 0.4) -> list[dict]:
    if not bars:
        return []
    pivots = _zigzag_pivots([float(bar.close) for bar in bars], threshold_pct)
    labels = [{"timestamp": bar.timestamp, "label": "none", "threshold_pct": threshold_pct} for bar in bars]
    for left, right in zip(pivots, pivots[1:]):
        if right <= left:
            continue
        direction = "long_win" if float(bars[right].close) > float(bars[left].close) else "short_win"
        for index in range(left, right):
            labels[index]["label"] = direction
    return labels


def label_base_rates(labels: list[dict]) -> dict:
    total = len(labels)
    counts: dict[str, int] = {}
    for item in labels:
        key = str(item.get("label", "none"))
        counts[key] = counts.get(key, 0) + 1
    return {
        "total": total,
        "counts": counts,
        "rates": {key: round(value / total, 8) if total else 0.0 for key, value in sorted(counts.items())},
    }


def atr_series(bars: list[Bar], period: int = 14) -> list[float]:
    if not bars:
        return []
    out: list[float] = []
    ranges: list[float] = []
    previous_close = float(bars[0].close)
    for bar in bars:
        high = float(bar.high)
        low = float(bar.low)
        true_range = max(high - low, abs(high - previous_close), abs(low - previous_close))
        ranges.append(true_range)
        window = ranges[-max(1, period):]
        out.append(sum(window) / len(window))
        previous_close = float(bar.close)
    return out


def _triple_barrier_label(bars: list[Bar], index: int, atr: float, cfg: TripleBarrierConfig, horizon_bars: int) -> str:
    close = float(bars[index].close)
    long_target = close + atr * cfg.target_atr
    long_stop = close - atr * cfg.stop_atr
    short_target = close - atr * cfg.target_atr
    short_stop = close + atr * cfg.stop_atr
    long_win_at: int | None = None
    short_win_at: int | None = None
    for offset, future in enumerate(bars[index + 1 : min(len(bars), index + horizon_bars + 1)], start=1):
        high = float(future.high)
        low = float(future.low)
        if long_win_at is None:
            if low <= long_stop:
                long_win_at = -1
            elif high >= long_target:
                long_win_at = offset
        if short_win_at is None:
            if high >= short_stop:
                short_win_at = -1
            elif low <= short_target:
                short_win_at = offset
        wins = [value for value in (long_win_at, short_win_at) if value and value > 0]
        if wins:
            break
    if long_win_at and long_win_at > 0 and (not short_win_at or short_win_at < 0 or long_win_at < short_win_at):
        return "long_win"
    if short_win_at and short_win_at > 0 and (not long_win_at or long_win_at < 0 or short_win_at < long_win_at):
        return "short_win"
    return "none"


def _zigzag_pivots(closes: list[float], threshold_pct: float) -> list[int]:
    pivots = [0]
    last_pivot = 0
    direction = 0
    for index, close in enumerate(closes[1:], start=1):
        pivot_price = closes[last_pivot]
        move_pct = (close - pivot_price) / pivot_price * 100 if pivot_price else 0.0
        if direction >= 0 and move_pct >= threshold_pct:
            direction = 1
            last_pivot = index
            pivots.append(index)
        elif direction <= 0 and move_pct <= -threshold_pct:
            direction = -1
            last_pivot = index
            pivots.append(index)
        elif direction == 1 and close > closes[last_pivot]:
            last_pivot = index
            pivots[-1] = index
        elif direction == -1 and close < closes[last_pivot]:
            last_pivot = index
            pivots[-1] = index
    if pivots[-1] != len(closes) - 1:
        pivots.append(len(closes) - 1)
    return sorted(set(pivots))
