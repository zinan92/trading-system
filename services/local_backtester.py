from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from schemas.analysis import Analysis
from schemas.backtest import BacktestEvidence
from schemas.market_data import Bar
from schemas.signal import Signal


@dataclass(frozen=True)
class LocalBacktestConfig:
    stop_pct: float = 0.006
    target_pct: float = 0.012
    max_hold_bars: int = 24
    min_sample_size: int = 20
    supportive_min_win_rate: float = 0.52
    supportive_min_avg_r: float = 0.0
    supportive_min_profit_factor: float = 1.15
    supportive_max_drawdown_pct: float = 8.0
    mixed_min_profit_factor: float = 1.0
    mixed_min_avg_r: float = -0.05

    @classmethod
    def from_strategy_config(cls, config: dict | None = None) -> "LocalBacktestConfig":
        config = config or {}
        if "gold_5m_v1" in config:
            config = config["gold_5m_v1"]
        backtest = config.get("backtest", config)
        supportive = backtest.get("supportive", {})
        mixed = backtest.get("mixed", {})
        return cls(
            stop_pct=float(backtest.get("stop_pct", cls.stop_pct)),
            target_pct=float(backtest.get("target_pct", cls.target_pct)),
            max_hold_bars=int(backtest.get("max_hold_bars", cls.max_hold_bars)),
            min_sample_size=int(backtest.get("min_sample_size", cls.min_sample_size)),
            supportive_min_win_rate=float(supportive.get("min_win_rate", cls.supportive_min_win_rate)),
            supportive_min_avg_r=float(supportive.get("min_avg_r", cls.supportive_min_avg_r)),
            supportive_min_profit_factor=float(supportive.get("min_profit_factor", cls.supportive_min_profit_factor)),
            supportive_max_drawdown_pct=float(supportive.get("max_drawdown_pct", cls.supportive_max_drawdown_pct)),
            mixed_min_profit_factor=float(mixed.get("min_profit_factor", cls.mixed_min_profit_factor)),
            mixed_min_avg_r=float(mixed.get("min_avg_r", cls.mixed_min_avg_r)),
        )


class LocalBacktester:
    def __init__(self, config: LocalBacktestConfig | None = None) -> None:
        self.config = config or LocalBacktestConfig()

    def evaluate(self, signal: Signal, analysis: Analysis, bars: list[Bar]) -> BacktestEvidence:
        usable = self._dedupe_and_sort(bars)
        if signal.direction not in {"long", "short"}:
            r_values = self._simulate_no_trade_context(usable)
            return self._evidence(
                signal,
                sample_size=len(r_values),
                r_values=r_values,
                verdict="no_trade",
                evaluated_bars=len(usable),
                setup_count=len(r_values),
                skipped_reason=f"signal_direction={signal.direction}",
            )
        r_values = self._simulate(signal, usable)
        if len(r_values) < self.config.min_sample_size:
            return self._evidence(signal, sample_size=len(r_values), r_values=r_values, verdict="thin", evaluated_bars=len(usable), setup_count=len(r_values))
        win_rate = self._win_rate(r_values)
        avg_r = sum(r_values) / len(r_values)
        profit_factor = self._profit_factor(r_values)
        max_drawdown = self._max_drawdown_pct(r_values)
        if (
            win_rate >= self.config.supportive_min_win_rate
            and avg_r > self.config.supportive_min_avg_r
            and profit_factor >= self.config.supportive_min_profit_factor
            and max_drawdown <= self.config.supportive_max_drawdown_pct
        ):
            verdict = "supportive"
        elif profit_factor >= self.config.mixed_min_profit_factor and avg_r >= self.config.mixed_min_avg_r:
            verdict = "mixed"
        else:
            verdict = "thin"
        return self._evidence(signal, sample_size=len(r_values), r_values=r_values, verdict=verdict, evaluated_bars=len(usable), setup_count=len(r_values))

    def _simulate(self, signal: Signal, bars: list[Bar]) -> list[float]:
        r_values: list[float] = []
        direction = 1 if signal.direction == "long" else -1
        for index in range(20, max(20, len(bars) - self.config.max_hold_bars)):
            if not self._setup_matches(signal, bars, index):
                continue
            outcome = self._simulate_outcome(bars, index, direction)
            if outcome is not None:
                r_values.append(outcome)
        return r_values

    def _simulate_no_trade_context(self, bars: list[Bar]) -> list[float]:
        r_values: list[float] = []
        for index in range(20, max(20, len(bars) - self.config.max_hold_bars)):
            direction = self._generic_setup_direction(bars, index)
            if direction == 0:
                continue
            outcome = self._simulate_outcome(bars, index, direction)
            if outcome is not None:
                r_values.append(outcome)
        return r_values

    def _simulate_outcome(self, bars: list[Bar], index: int, direction: int) -> float | None:
        entry_bar = bars[index + 1]
        entry = entry_bar.close
        if entry <= 0:
            return None
        stop = entry * (1 - self.config.stop_pct * direction)
        target = entry * (1 + self.config.target_pct * direction)
        risk = abs(entry - stop)
        if risk <= 0:
            return None
        outcome = 0.0
        for future in bars[index + 2 : index + 2 + self.config.max_hold_bars]:
            if self._is_gap(index, future, bars):
                break
            if direction == 1:
                if future.low <= stop:
                    outcome = -1.0
                    break
                if future.high >= target:
                    outcome = self.config.target_pct / self.config.stop_pct
                    break
            else:
                if future.high >= stop:
                    outcome = -1.0
                    break
                if future.low <= target:
                    outcome = self.config.target_pct / self.config.stop_pct
                    break
            outcome = ((future.close - entry) * direction) / risk
        return round(max(-1.0, min(self.config.target_pct / self.config.stop_pct, outcome)), 4)

    def _setup_matches(self, signal: Signal, bars: list[Bar], index: int) -> bool:
        closes = [bar.close for bar in bars[index - 19 : index + 1]]
        ma_short = sum(closes[-5:]) / 5
        ma_long = sum(closes) / len(closes)
        close = bars[index].close
        previous = bars[index - 1].close
        if signal.direction == "long":
            if signal.regime == "pullback_long":
                return close > ma_long and close <= ma_short * 1.002 and close >= previous * 0.997
            return close > ma_short > ma_long
        if signal.regime == "risk_off_reversal":
            return close < ma_short < ma_long
        return False

    def _generic_setup_direction(self, bars: list[Bar], index: int) -> int:
        closes = [bar.close for bar in bars[index - 19 : index + 1]]
        ma_short = sum(closes[-5:]) / 5
        ma_long = sum(closes) / len(closes)
        close = bars[index].close
        if close > ma_short > ma_long:
            return 1
        if close < ma_short < ma_long:
            return -1
        return 0

    def _dedupe_and_sort(self, bars: list[Bar]) -> list[Bar]:
        by_key = {(bar.symbol, bar.timeframe, bar.timestamp): bar for bar in bars if bar.close > 0}
        return sorted(by_key.values(), key=lambda item: item.timestamp)

    def _is_gap(self, index: int, future: Bar, bars: list[Bar]) -> bool:
        previous = bars[index]
        try:
            prev_ts = datetime.fromisoformat(previous.timestamp.replace("Z", "+00:00"))
            future_ts = datetime.fromisoformat(future.timestamp.replace("Z", "+00:00"))
        except ValueError:
            return False
        return (future_ts - prev_ts).total_seconds() > 60 * 60 * 72

    def _evidence(
        self,
        signal: Signal,
        sample_size: int,
        r_values: list[float],
        verdict: str,
        evaluated_bars: int = 0,
        setup_count: int = 0,
        skipped_reason: str = "",
    ) -> BacktestEvidence:
        raw = f"{signal.signal_id}:{sample_size}:{sum(r_values):.4f}:{verdict}".encode("utf-8")
        return BacktestEvidence(
            backtest_id=f"local5m_{hashlib.sha256(raw).hexdigest()[:10]}",
            signal_id=signal.signal_id,
            asset=signal.asset,
            sample_size=sample_size,
            win_rate=round(self._win_rate(r_values), 3),
            avg_r=round(sum(r_values) / len(r_values), 3) if r_values else 0.0,
            max_drawdown_pct=round(self._max_drawdown_pct(r_values), 3),
            verdict=verdict,
            profit_factor=round(self._profit_factor(r_values), 3),
            evaluated_bars=evaluated_bars,
            setup_count=setup_count,
            skipped_reason=skipped_reason,
        )

    def _win_rate(self, r_values: list[float]) -> float:
        return sum(1 for value in r_values if value > 0) / len(r_values) if r_values else 0.0

    def _profit_factor(self, r_values: list[float]) -> float:
        wins = sum(value for value in r_values if value > 0)
        losses = abs(sum(value for value in r_values if value < 0))
        if wins and not losses:
            return 99.0
        return wins / losses if losses else 0.0

    def _max_drawdown_pct(self, r_values: list[float]) -> float:
        equity = 0.0
        peak = 0.0
        max_dd = 0.0
        for value in r_values:
            equity += value * 0.5
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)
        return max_dd
