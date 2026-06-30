from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from schemas.asset import Asset
from schemas.signal import Signal
from services.intel_client import IntelEvent
from services.kline_client import Candle


class SignalEngine:
    def __init__(self, config: dict | None = None, strategy_id: str | None = "gold_5m_v1") -> None:
        config = config or {}
        # When `config` is a strategy library keyed by id, extract that block;
        # when it is already a single strategy's params (strategy_id=None), use
        # it directly. The default keeps the legacy single-strategy behavior.
        self.config = config.get(strategy_id, config) if strategy_id else config
        self.signal_config = self.config.get("signal", {})

    def generate(
        self,
        asset: Asset,
        candles: list[Candle],
        events: list[IntelEvent],
        run_date: str,
        factor_context: dict[str, dict] | None = None,
    ) -> Signal:
        if len(candles) < 2:
            return self._no_signal(asset, run_date, "Not enough candle data")

        last = candles[-1]
        previous = candles[-2]
        factor_context = factor_context or {}
        price_change_pct = ((last.close - previous.close) / previous.close) * 100 if previous.close else 0
        ma_short_bars = int(self.signal_config.get("ma_short_bars", 5))
        ma_long_bars = int(self.signal_config.get("ma_long_bars", 20))
        closes = [bar.close for bar in candles[-ma_long_bars:]]
        ma_short = sum(closes[-ma_short_bars:]) / min(ma_short_bars, len(closes))
        ma_long = sum(closes) / len(closes)
        trend_score = 50 + (12 if last.close > ma_short else -8) + (12 if ma_short > ma_long else -8)
        trend_score += max(-15, min(15, price_change_pct * 5))

        macro_score = self._macro_score(factor_context)
        volatility_score = self._volatility_score(candles)
        event_score = max((event.impact_score for event in events), default=50)
        if any(event.impact_score <= 45 for event in events):
            event_score = min(event_score, 48)

        factor_scores = {
            "trend": round(max(0, min(100, trend_score)), 1),
            "macro": round(max(0, min(100, macro_score)), 1),
            "volatility": round(max(0, min(100, volatility_score)), 1),
            "event": round(max(0, min(100, event_score)), 1),
        }
        weights = self.signal_config.get("weights", {})
        confidence_config = self.signal_config.get("confidence", {})
        strength = int(
            (factor_scores["trend"] * float(weights.get("trend", 0.42)))
            + (factor_scores["macro"] * float(weights.get("macro", 0.28)))
            + (factor_scores["event"] * float(weights.get("event", 0.2)))
            + (factor_scores["volatility"] * float(weights.get("volatility", 0.1)))
        )
        confidence = int(
            min(
                100,
                max(
                    0,
                    strength * float(confidence_config.get("strength_weight", 0.68))
                    + event_score * float(confidence_config.get("event_weight", 0.22))
                    + (float(confidence_config.get("real_data_bonus", 10)) if last.provider != "mock_kline" else 0),
                ),
            )
        )

        if factor_scores["event"] < float(self.signal_config.get("event_block_below", 45)):
            direction = "watch"
            status = "no_signal"
            regime = "event_risk_reduction"
            thesis = f"{asset.symbol} setup is blocked by event risk despite market context."
        elif strength >= int(self.signal_config.get("long_strength_min", 60)) and confidence >= int(self.signal_config.get("long_confidence_min", 55)):
            direction = "long"
            status = "new"
            regime = "trend_following" if factor_scores["trend"] >= 65 else "pullback_long"
            thesis = f"{asset.symbol} has a constructive gold setup: trend, macro, and event scores support a controlled long bias."
        elif strength <= int(self.signal_config.get("short_strength_max", 38)) and confidence >= int(self.signal_config.get("long_confidence_min", 55)):
            direction = "short"
            status = "new"
            regime = "risk_off_reversal"
            thesis = f"{asset.symbol} shows downside pressure with confirming macro context."
        else:
            direction = "watch"
            status = "no_signal"
            regime = "no_trade"
            thesis = f"{asset.symbol} does not yet meet gold trading thresholds."

        generated_at = datetime.now(timezone.utc).replace(microsecond=0)
        expires_at = generated_at + timedelta(days=1)
        signal_id = self._signal_id(asset.symbol, run_date, direction, last.close)
        evidence = [
            f"Last close {last.close} vs previous close {previous.close}",
            f"One-period change {price_change_pct:.2f}%",
            f"Trend score {factor_scores['trend']}",
            f"Macro score {factor_scores['macro']}",
            f"Volatility score {factor_scores['volatility']}",
            f"Event score {factor_scores['event']}",
        ]
        evidence.extend(f"Intel: {event.title} ({event.impact_score})" for event in events[:2])
        source_artifacts = [f"clean_bars/{run_date}/{asset.symbol}_{last.timeframe}.json"]
        source_artifacts.extend(
            f"raw_snapshots/{run_date}/{symbol}_{context.get('timeframe') or last.timeframe}.json"
            for symbol, context in sorted(factor_context.items())
        )

        return Signal(
            signal_id=signal_id,
            asset=asset.symbol,
            asset_class=asset.asset_class,
            direction=direction,
            strength=strength,
            confidence=confidence,
            horizon="intraday_5m",
            thesis=thesis,
            evidence=evidence,
            regime=regime,
            factor_scores=factor_scores,
            source_artifacts=source_artifacts,
            invalid_if="Price closes back below the latest breakout/reference level.",
            generated_at=generated_at.isoformat(),
            expires_at=expires_at.isoformat(),
            status=status,
        )

    def _macro_score(self, factor_context: dict[str, dict]) -> float:
        score = 55.0
        dxy = self._last_change(factor_context.get("DXY", {}).get("bars", []))
        real_yield = self._last_change(factor_context.get("US10Y_REAL", {}).get("bars", []))
        gld_flow = self._last_change(factor_context.get("GLD_FLOW", {}).get("bars", []))
        if dxy < 0:
            score += 10
        elif dxy > 0:
            score -= 8
        if real_yield < 0:
            score += 12
        elif real_yield > 0:
            score -= 10
        if gld_flow > 0:
            score += 6
        elif gld_flow < 0:
            score -= 4
        return score

    def _volatility_score(self, candles: list[Candle]) -> float:
        if len(candles) < 5:
            return 50.0
        ranges = [((bar.high - bar.low) / bar.close) * 100 for bar in candles[-10:] if bar.close]
        avg_range = sum(ranges) / len(ranges)
        if avg_range > 4:
            return 42.0
        if avg_range > 2.5:
            return 55.0
        return 68.0

    def _last_change(self, bars: list[Candle]) -> float:
        if len(bars) < 2 or not bars[-2].close:
            return 0.0
        return ((bars[-1].close - bars[-2].close) / bars[-2].close) * 100

    def _no_signal(self, asset: Asset, run_date: str, reason: str) -> Signal:
        generated_at = datetime.now(timezone.utc).replace(microsecond=0)
        return Signal(
            signal_id=self._signal_id(asset.symbol, run_date, "watch", 0),
            asset=asset.symbol,
            asset_class=asset.asset_class,
            direction="watch",
            strength=0,
            confidence=0,
            horizon="intraday_5m",
            thesis=reason,
            evidence=[reason],
            regime="no_trade",
            factor_scores={"trend": 0, "macro": 0, "volatility": 0, "event": 0},
            source_artifacts=[],
            invalid_if="",
            generated_at=generated_at.isoformat(),
            expires_at=generated_at.isoformat(),
            status="no_signal",
        )

    def _signal_id(self, symbol: str, run_date: str, direction: str, close: float) -> str:
        raw = f"{symbol}:{run_date}:{direction}:{close}".encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()[:10]
        return f"sig_{symbol.lower()}_{run_date.replace('-', '')}_{digest}"
