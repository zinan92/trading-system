"""MACD signal engine — a SECOND rule-based engine type alongside MA and chan,
selected via a strategy's `engine: macd`. Pure stdlib (no pandas), so it runs
under any interpreter.

Enters on a fresh MACD cross: histogram crossing up through zero (MACD line
crossing above its signal line) → 金叉 → long; crossing down → 死叉 → short.
Mirrors ChanSignalEngine's contract (`generate(asset, candles, events, run_date,
factor_context)` → Signal) so the registry/runner treat it identically.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from schemas.signal import Signal
from services.indicators import macd


class MacdSignalEngine:
    def __init__(self, params: dict | None = None) -> None:
        self.params = params or {}
        signal_cfg = self.params.get("signal", {}) or {}
        self.fast = int(signal_cfg.get("macd_fast", 12))
        self.slow = int(signal_cfg.get("macd_slow", 26))
        self.signal_period = int(signal_cfg.get("macd_signal", 9))
        self.fresh_bars = int(signal_cfg.get("fresh_bars", 3))
        self.min_bars = int(signal_cfg.get("min_bars", max(60, self.slow + self.signal_period + 5)))

    def detect_cross(self, candles: list) -> tuple[str | None, int | None]:
        """Return (direction, bar_index) of the most RECENT MACD cross, or
        (None, None). long = histogram crossed up through 0, short = down."""
        closes = [float(bar.close) for bar in candles]
        if len(closes) < self.min_bars:
            return None, None
        _macd_line, _signal_line, hist = macd(closes, self.fast, self.slow, self.signal_period)
        direction: str | None = None
        index: int | None = None
        for i in range(1, len(hist)):
            if hist[i - 1] <= 0 < hist[i]:
                direction, index = "long", i
            elif hist[i - 1] >= 0 > hist[i]:
                direction, index = "short", i
        return direction, index

    def historical_signals(self, candles: list) -> list[dict]:
        """Every MACD cross over the series as {index, direction} — one pass, for
        the backtester."""
        closes = [float(bar.close) for bar in candles]
        if len(closes) < self.min_bars:
            return []
        _macd_line, _signal_line, hist = macd(closes, self.fast, self.slow, self.signal_period)
        out = []
        for i in range(1, len(hist)):
            if hist[i - 1] <= 0 < hist[i]:
                out.append({"index": i, "direction": "long"})
            elif hist[i - 1] >= 0 > hist[i]:
                out.append({"index": i, "direction": "short"})
        return out

    def generate(self, asset, candles, events=None, run_date: str = "", factor_context=None) -> Signal:
        if len(candles) < self.min_bars:
            return self._no_signal(asset, run_date, f"need >= {self.min_bars} bars for MACD", candles[-1] if candles else None)
        direction, index = self.detect_cross(candles)
        last = candles[-1]
        if direction is None:
            return self._no_signal(asset, run_date, "no MACD cross detected", last)
        # Fresh only if the cross is within the last `fresh_bars` bars.
        if index is None or index < len(candles) - self.fresh_bars:
            return self._no_signal(asset, run_date, "latest MACD cross not fresh", last)
        kind = "金叉" if direction == "long" else "死叉"
        regime = "macd_golden_cross" if direction == "long" else "macd_death_cross"
        return Signal(
            signal_id=self._sig_id(asset.symbol, run_date, direction, last.close),
            asset=asset.symbol, asset_class=asset.asset_class,
            direction=direction, strength=70, confidence=60, horizon="macd",
            thesis=f"{asset.symbol} MACD {kind}({self.fast}/{self.slow}/{self.signal_period}) 形成,{'多' if direction == 'long' else '空'}头入场。",
            evidence=[f"macd cross={kind}", f"cross bar index={index}/{len(candles)}", f"params={self.fast}/{self.slow}/{self.signal_period}"],
            regime=regime, factor_scores={"macd": 100, "trend": 0, "macro": 0, "volatility": 0},
            source_artifacts=[f"clean_bars/{run_date}/{asset.symbol}_{last.timeframe}.json"],
            invalid_if="MACD 反向交叉(动能反转)。",
            generated_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            expires_at=(datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0).isoformat(),
            status="new",
        )

    def _sig_id(self, symbol: str, run_date: str, direction: str, close: float) -> str:
        digest = hashlib.sha256(f"macd:{symbol}:{run_date}:{direction}:{close}".encode()).hexdigest()[:10]
        return f"sig_macd_{symbol.lower()}_{run_date.replace('-', '')}_{digest}"

    def _no_signal(self, asset, run_date: str, reason: str, last=None) -> Signal:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        return Signal(
            signal_id=self._sig_id(asset.symbol, run_date, "watch", last.close if last else 0),
            asset=asset.symbol, asset_class=asset.asset_class, direction="watch", strength=0, confidence=0,
            horizon="macd", thesis=reason, evidence=[reason], regime="no_trade",
            factor_scores={"macd": 0, "trend": 0, "macro": 0, "volatility": 0}, source_artifacts=[],
            invalid_if="", generated_at=now.isoformat(), expires_at=now.isoformat(), status="no_signal",
        )
