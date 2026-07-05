"""Point-in-time feature snapshots for Strategy Lab R2."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from statistics import pstdev
from typing import Iterable

from schemas.market_data import Bar
from services.indicators import ema, macd
from services.lab_labels import atr_series

META_FIELDS = {"timestamp", "label_primary", "label_zigzag", "atr", "target_atr", "stop_atr", "horizon_bars"}


def build_feature_rows(bars: list[Bar], primary_labels: list[dict], zigzag_labels: list[dict]) -> list[dict]:
    if not bars:
        return []
    primary_by_ts = {item["timestamp"]: item for item in primary_labels}
    zigzag_by_ts = {item["timestamp"]: item for item in zigzag_labels}
    atr = atr_series(bars, 14)
    closes = [float(bar.close) for bar in bars]
    macd_line, signal_line, hist = macd(closes)
    rsi = _rsi(closes, 14)
    completed_4h = _completed_htf_features(bars, 4 * 3600)
    completed_1d = _completed_htf_features(bars, 24 * 3600)
    prev_days = _previous_day_levels(bars)
    session_state: dict[str, dict[str, float]] = {}
    rows: list[dict] = []
    for index, bar in enumerate(bars):
        ts = _parse(bar.timestamp)
        day = ts.date().isoformat()
        session = _session(ts.hour)
        key = f"{day}|{session}"
        state = session_state.setdefault(key, {"high": float(bar.high), "low": float(bar.low)})
        state["high"] = max(state["high"], float(bar.high))
        state["low"] = min(state["low"], float(bar.low))
        label = primary_by_ts.get(bar.timestamp, {})
        zlabel = zigzag_by_ts.get(bar.timestamp, {})
        row = {
            "timestamp": bar.timestamp,
            "label_primary": label.get("label", "none"),
            "label_zigzag": zlabel.get("label", "none"),
            "atr": round(float(label.get("atr", atr[index])), 8),
            "target_atr": label.get("target_atr", 2.0),
            "stop_atr": label.get("stop_atr", 1.0),
            "horizon_bars": label.get("horizon_bars", 48),
            **_position_features(bars, index, atr[index]),
            **_htf_prefixed("4h", completed_4h[index]),
            **_htf_prefixed("1d", completed_1d[index]),
            **_key_level_features(bar, index, bars, atr[index], prev_days[index], state),
            **_time_regime_features(ts, bars, index),
            **_micro_features(closes, index, atr, macd_line, signal_line, hist, rsi),
        }
        rows.append(row)
    return rows


def feature_names(rows: list[dict]) -> list[str]:
    if not rows:
        return []
    return sorted(key for key, value in rows[0].items() if key not in META_FIELDS and isinstance(value, (int, float)))


def lookahead_shift_audit(rows: list[dict], *, label_field: str, legit_feature: str, leaky_feature: str) -> dict:
    labels = [_binary_label(row.get(label_field)) for row in rows]
    original_legit = _auc([float(row[legit_feature]) for row in rows], labels)
    shifted_legit = _auc([float(row[legit_feature]) for row in rows[1:]], labels[:-1])
    original_leaky = _auc([float(row[leaky_feature]) for row in rows], labels)
    shifted_leaky = _auc([float(row[leaky_feature]) for row in rows[1:]], labels[:-1])
    return {
        "legit_original_auc": round(original_legit, 6),
        "legit_shifted_auc": round(shifted_legit, 6),
        "leaky_original_auc": round(original_leaky, 6),
        "leaky_shifted_auc": round(shifted_leaky, 6),
        "leaky_auc_drop": round(original_leaky - shifted_leaky, 6),
    }


def _position_features(bars: list[Bar], index: int, atr: float) -> dict:
    close = float(bars[index].close)
    denom = atr if atr > 0 else 0.0
    out = {}
    for name, window in (("5d", 5 * 24 * 12), ("20d", 20 * 24 * 12)):
        rows = bars[max(0, index - window + 1): index + 1]
        high = max(float(bar.high) for bar in rows)
        low = min(float(bar.low) for bar in rows)
        width = high - low
        out[f"close_pct_range_{name}"] = _safe((close - low) / width) if width else 0.5
        out[f"atr_dist_high_{name}"] = _safe((high - close) / denom) if denom else 0.0
        out[f"atr_dist_low_{name}"] = _safe((close - low) / denom) if denom else 0.0
    return out


def _completed_htf_features(bars: list[Bar], seconds: int) -> list[dict]:
    buckets: dict[int, list[Bar]] = {}
    for bar in bars:
        epoch = int(_parse(bar.timestamp).timestamp())
        bucket = (epoch // seconds) * seconds
        buckets.setdefault(bucket, []).append(bar)
    completed = {}
    closes: list[float] = []
    for bucket in sorted(buckets):
        rows = buckets[bucket]
        open_price = float(rows[0].open)
        close = float(rows[-1].close)
        high = max(float(row.high) for row in rows)
        low = min(float(row.low) for row in rows)
        closes.append(close)
        ema_values = ema(closes, min(10, max(2, len(closes))))
        slope = ema_values[-1] - ema_values[-2] if len(ema_values) > 1 else 0.0
        completed[bucket] = {
            "direction": 1.0 if close > open_price else (-1.0 if close < open_price else 0.0),
            "body_ratio": _safe(abs(close - open_price) / (high - low)) if high != low else 0.0,
            "close_location": _safe((close - low) / (high - low)) if high != low else 0.5,
            "ema_slope": slope,
        }
    out = []
    for bar in bars:
        epoch = int(_parse(bar.timestamp).timestamp())
        current_bucket = (epoch // seconds) * seconds
        out.append(completed.get(current_bucket - seconds, {"direction": 0.0, "body_ratio": 0.0, "close_location": 0.5, "ema_slope": 0.0}))
    return out


def _previous_day_levels(bars: list[Bar]) -> list[dict]:
    daily: dict[str, dict[str, float]] = {}
    for bar in bars:
        day = _parse(bar.timestamp).date().isoformat()
        row = daily.setdefault(day, {"high": float(bar.high), "low": float(bar.low), "close": float(bar.close)})
        row["high"] = max(row["high"], float(bar.high))
        row["low"] = min(row["low"], float(bar.low))
        row["close"] = float(bar.close)
    days = sorted(daily)
    prev_by_day = {days[index]: daily[days[index - 1]] for index in range(1, len(days))}
    return [prev_by_day.get(_parse(bar.timestamp).date().isoformat(), {"high": float(bar.close), "low": float(bar.close), "close": float(bar.close)}) for bar in bars]


def _key_level_features(bar: Bar, index: int, bars: list[Bar], atr: float, prev_day: dict, session: dict) -> dict:
    close = float(bar.close)
    denom = atr if atr > 0 else 0.0
    round5 = round(close / 5) * 5
    round10 = round(close / 10) * 10
    recent = bars[max(0, index - 12 * 24): index + 1]
    touch5 = sum(1 for item in recent if abs(float(item.close) - round(float(item.close) / 5) * 5) <= atr * 0.25)
    touch10 = sum(1 for item in recent if abs(float(item.close) - round(float(item.close) / 10) * 10) <= atr * 0.25)
    return {
        "atr_dist_prev_day_high": _safe((prev_day["high"] - close) / denom) if denom else 0.0,
        "atr_dist_prev_day_low": _safe((close - prev_day["low"]) / denom) if denom else 0.0,
        "atr_dist_prev_day_close": _safe((close - prev_day["close"]) / denom) if denom else 0.0,
        "atr_dist_session_high": _safe((session["high"] - close) / denom) if denom else 0.0,
        "atr_dist_session_low": _safe((close - session["low"]) / denom) if denom else 0.0,
        "atr_dist_round5": _safe(abs(close - round5) / denom) if denom else 0.0,
        "atr_dist_round10": _safe(abs(close - round10) / denom) if denom else 0.0,
        "round5_touch_count_24h": touch5,
        "round10_touch_count_24h": touch10,
    }


def _time_regime_features(ts: datetime, bars: list[Bar], index: int) -> dict:
    session = _session(ts.hour)
    recent = bars[max(0, index - 48): index + 1]
    closes = [float(bar.close) for bar in recent]
    vol = pstdev([_return(closes, idx, 1) for idx in range(1, len(closes))]) if len(closes) > 3 else 0.0
    trend = abs(closes[-1] - closes[0]) / closes[0] if len(closes) > 1 and closes[0] else 0.0
    return {
        "hour_sin": math.sin(2 * math.pi * ts.hour / 24),
        "hour_cos": math.cos(2 * math.pi * ts.hour / 24),
        "session_asia": 1.0 if session == "asia" else 0.0,
        "session_london": 1.0 if session == "london" else 0.0,
        "session_ny": 1.0 if session == "ny" else 0.0,
        "causal_regime_vol": vol,
        "causal_regime_trend": trend,
    }


def _micro_features(closes: list[float], index: int, atr: list[float], macd_line: list[float], signal_line: list[float], hist: list[float], rsi: list[float]) -> dict:
    vol_short = _realized_vol(closes, index, 12)
    vol_long = _realized_vol(closes, index, 48)
    return {
        "ret_1_5m": _return(closes, index, 1),
        "ret_3_5m": _return(closes, index, 3),
        "ret_12_5m": _return(closes, index, 12),
        "realized_vol_ratio_1h_4h": _safe(vol_short / vol_long) if vol_long else 0.0,
        "atr_pct": _safe(atr[index] / closes[index] * 100),
        "macd_line": macd_line[index],
        "macd_signal": signal_line[index],
        "macd_hist": hist[index],
        "rsi14": rsi[index],
    }


def _htf_prefixed(prefix: str, row: dict) -> dict:
    return {f"{prefix}_{key}": value for key, value in row.items()}


def _rsi(closes: list[float], period: int) -> list[float]:
    out = []
    for index in range(len(closes)):
        if index == 0:
            out.append(50.0)
            continue
        window = closes[max(0, index - period): index + 1]
        gains = [max(0.0, window[i] - window[i - 1]) for i in range(1, len(window))]
        losses = [max(0.0, window[i - 1] - window[i]) for i in range(1, len(window))]
        avg_loss = sum(losses) / len(losses) if losses else 0.0
        avg_gain = sum(gains) / len(gains) if gains else 0.0
        out.append(100.0 if avg_loss == 0 and avg_gain else (50.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)))
    return out


def _realized_vol(closes: list[float], index: int, window: int) -> float:
    returns = [_return(closes, idx, 1) for idx in range(max(1, index - window + 1), index + 1)]
    return pstdev(returns) if len(returns) > 1 else 0.0


def _return(closes: list[float], index: int, lookback: int) -> float:
    if index - lookback < 0 or not closes[index - lookback]:
        return 0.0
    return (closes[index] - closes[index - lookback]) / closes[index - lookback]


def _auc(scores: Iterable[float], labels: Iterable[int]) -> float:
    pairs = sorted(zip(scores, labels), key=lambda item: item[0])
    positives = sum(label for _, label in pairs)
    negatives = len(pairs) - positives
    if positives == 0 or negatives == 0:
        return 0.5
    rank_sum = sum(rank for rank, (_, label) in enumerate(pairs, start=1) if label)
    return (rank_sum - positives * (positives + 1) / 2) / (positives * negatives)


def _binary_label(value: object) -> int:
    return 1 if str(value) in {"long_win", "short_win", "win", "1", "true"} else 0


def _session(hour: int) -> str:
    if 0 <= hour < 7:
        return "asia"
    if 7 <= hour < 13:
        return "london"
    if 13 <= hour < 21:
        return "ny"
    return "off"


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _safe(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return round(float(value), 8)
