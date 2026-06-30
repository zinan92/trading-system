"""Pure-stdlib technical indicators (no pandas), so they run under any
interpreter the runner uses. Deterministic and side-effect free."""

from __future__ import annotations


def ema(values: list[float], period: int) -> list[float]:
    """Exponential moving average, seeded with the first value (standard for a
    streaming EMA). Returns a list the same length as `values`."""
    if period <= 0:
        raise ValueError("ema period must be positive")
    if not values:
        return []
    k = 2.0 / (period + 1)
    out: list[float] = []
    prev = float(values[0])
    for index, value in enumerate(values):
        prev = float(value) if index == 0 else float(value) * k + prev * (1 - k)
        out.append(prev)
    return out


def macd(
    closes: list[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[list[float], list[float], list[float]]:
    """MACD line (ema_fast - ema_slow), signal line (ema of MACD), and histogram
    (MACD - signal). Each returned list aligns 1:1 with `closes`."""
    if fast >= slow:
        raise ValueError("macd fast period must be < slow period")
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = ema(macd_line, signal)
    histogram = [m - s for m, s in zip(macd_line, signal_line)]
    return macd_line, signal_line, histogram
