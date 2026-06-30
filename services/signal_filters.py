"""Reusable signal filters — a confirm layer that wraps ANY signal engine and
downgrades a directional signal to `watch` unless a confirming condition holds.

This is the "MACD filter" pattern (e.g. only take a chan 二买 long when MACD is
above its zero line), but it is engine-agnostic: `FilteredSignalEngine` delegates
to a base engine and then applies an ordered list of filters to its output. Each
filter sees the same candles, so it can compute its own indicator.
"""

from __future__ import annotations

from services.indicators import macd


class _DirectionOnly:
    """Minimal stand-in so filters can be reused in a backtest, where only the
    candidate direction is known (no full Signal object yet)."""

    __slots__ = ("direction",)

    def __init__(self, direction: str) -> None:
        self.direction = direction


class MacdFilter:
    """Confirm a directional signal against MACD state.

    mode "zero_line" (default): a long needs the MACD line > 0, a short needs it
    < 0 — i.e. only trade in the direction of MACD momentum. mode "histogram":
    confirm against the histogram sign instead.
    """

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        self.mode = str(config.get("mode", "zero_line"))
        self.fast = int(config.get("macd_fast", 12))
        self.slow = int(config.get("macd_slow", 26))
        self.signal_period = int(config.get("macd_signal", 9))

    def accepts(self, sig, candles) -> tuple[bool, str]:
        if sig.direction not in ("long", "short"):
            return True, ""
        closes = [float(bar.close) for bar in candles]
        if len(closes) <= self.slow:
            return True, ""  # not enough data to judge — don't veto
        macd_line, _signal_line, histogram = macd(closes, self.fast, self.slow, self.signal_period)
        value = histogram[-1] if self.mode == "histogram" else macd_line[-1]
        metric = "MACD histogram" if self.mode == "histogram" else "MACD line"
        if sig.direction == "long" and value <= 0:
            return False, f"{metric} {value:.4f} <= 0 (no bullish confirmation)"
        if sig.direction == "short" and value >= 0:
            return False, f"{metric} {value:.4f} >= 0 (no bearish confirmation)"
        return True, ""


_FILTERS = {"macd": MacdFilter}


def build_filter(spec):
    """spec is a filter name string, or a dict {"type": name, ...config}."""
    if isinstance(spec, str):
        spec = {"type": spec}
    filter_type = str(spec.get("type", "")).lower()
    factory = _FILTERS.get(filter_type)
    if factory is None:
        raise ValueError(f"unknown signal filter: {filter_type!r}")
    return factory(spec)


class FilteredSignalEngine:
    """Wraps a base engine; passes its signal through `filters`. The first filter
    that rejects downgrades the signal to a `watch` (no_signal), recording why."""

    def __init__(self, base_engine, filters: list) -> None:
        self.base = base_engine
        self.filters = list(filters)

    def historical_signals(self, candles: list) -> list[dict]:
        """Base engine's historical signals, each kept only if every filter accepts
        it given the candles up to that bar — so the backtester sees exactly what
        the live filtered engine would have traded."""
        base_signals = getattr(self.base, "historical_signals", None)
        if base_signals is None:
            return []
        kept = []
        for item in base_signals(candles):
            window = candles[: item["index"] + 1]
            stub = _DirectionOnly(item["direction"])
            if all(signal_filter.accepts(stub, window)[0] for signal_filter in self.filters):
                kept.append(item)
        return kept

    def generate(self, asset, candles, events=None, run_date: str = "", factor_context=None):
        signal = self.base.generate(asset, candles, events, run_date, factor_context=factor_context)
        if signal.direction not in ("long", "short"):
            return signal
        for signal_filter in self.filters:
            ok, reason = signal_filter.accepts(signal, candles)
            if not ok:
                return signal.__class__(
                    **{
                        **signal.to_dict(),
                        "direction": "watch",
                        "status": "no_signal",
                        "strength": 0,
                        "confidence": 0,
                        "thesis": f"{signal.thesis} [filter 否决: {reason}]",
                        "evidence": signal.evidence + [f"signal filter rejected: {reason}"],
                    }
                )
        return signal


def build_filtered_engine(base_engine, filter_specs: list) -> FilteredSignalEngine:
    return FilteredSignalEngine(base_engine, [build_filter(spec) for spec in filter_specs])
