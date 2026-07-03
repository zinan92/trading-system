from __future__ import annotations

import hashlib
import math
from datetime import datetime, timedelta, timezone

from schemas.signal import Signal
from services.indicators import ema, macd


class TechnicalRuleSignalEngine:
    """Config-driven technical engines for paper/shadow strategy breadth.

    These are deliberately simple, deterministic engines. They add genuinely
    different signal families to the multi-strategy runner without bypassing the
    existing ticket, TP/SL quality, guardrail, and execution gates.
    """

    def __init__(self, params: dict | None = None) -> None:
        self.params = params or {}
        self.engine_type = str(self.params.get("engine", "technical_rule")).lower()
        self.signal_cfg = self.params.get("signal", {}) or {}
        self.min_bars = int(self.signal_cfg.get("min_bars", self._default_min_bars()))

    def generate(self, asset, candles, events=None, run_date: str = "", factor_context=None) -> Signal:
        if len(candles) < self.min_bars:
            return self._no_signal(asset, run_date, f"need >= {self.min_bars} bars for {self.engine_type}", candles[-1] if candles else None)
        setup = self._latest_setup(candles)
        last = candles[-1]
        if not setup:
            return self._no_signal(asset, run_date, f"no {self.engine_type} setup", last)
        direction = setup["direction"]
        strength = int(setup.get("strength", 72))
        confidence = int(setup.get("confidence", 62))
        return Signal(
            signal_id=self._sig_id(asset.symbol, run_date, direction, last.close),
            asset=asset.symbol,
            asset_class=asset.asset_class,
            direction=direction,
            strength=strength,
            confidence=confidence,
            horizon=f"{self.engine_type}_{last.timeframe}",
            thesis=setup["thesis"],
            evidence=setup["evidence"],
            regime=setup["regime"],
            factor_scores=setup.get("factor_scores", {self.engine_type: 100}),
            source_artifacts=[f"clean_bars/{run_date}/{asset.symbol}_{last.timeframe}.json"],
            invalid_if=setup.get("invalid_if", ""),
            generated_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            expires_at=(datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0).isoformat(),
            status="new",
        )

    def historical_signals(self, candles: list) -> list[dict]:
        if len(candles) < self.min_bars:
            return []
        out = []
        for index in range(self.min_bars - 1, len(candles)):
            setup = self._latest_setup(candles[: index + 1])
            if setup:
                out.append({"index": index, "direction": setup["direction"]})
        return out

    def _latest_setup(self, candles: list) -> dict | None:
        if self.engine_type == "grid":
            return self._grid(candles)
        if self.engine_type == "adr_exhaustion_reversion":
            return self._adr_exhaustion_reversion(candles)
        if self.engine_type == "bollinger_reversion":
            return self._bollinger_reversion(candles)
        if self.engine_type == "bollinger_reclaim_filter":
            return self._bollinger_reclaim_filter(candles)
        if self.engine_type == "breakout":
            return self._breakout(candles)
        if self.engine_type == "london_ny_compression_breakout":
            return self._london_ny_compression_breakout(candles)
        if self.engine_type == "ny_opening_range_breakout":
            return self._ny_opening_range_breakout(candles)
        if self.engine_type == "breakout_retest_continuation":
            return self._breakout_retest_continuation(candles)
        if self.engine_type == "false_breakout_reversal":
            return self._false_breakout_reversal(candles)
        if self.engine_type == "psych_level_rejection":
            return self._psych_level_rejection(candles)
        if self.engine_type == "macd_trend_volatility_filter":
            return self._macd_trend_volatility_filter(candles)
        if self.engine_type == "fibonacci":
            return self._fibonacci(candles)
        if self.engine_type == "ema50_position":
            return self._ema50_position(candles)
        if self.engine_type == "adx_ema_pullback":
            return self._adx_ema_pullback(candles)
        if self.engine_type == "vwap_extension_reversion":
            return self._vwap_extension_reversion(candles)
        return None

    def _macd_trend_volatility_filter(self, candles: list) -> dict | None:
        if not self._in_utc_session(str(candles[-1].timestamp)):
            return None
        fast = int(self.signal_cfg.get("macd_fast", 12))
        slow = int(self.signal_cfg.get("macd_slow", 26))
        signal_period = int(self.signal_cfg.get("macd_signal", 9))
        fresh_bars = int(self.signal_cfg.get("fresh_bars", 3))
        ema_period = int(self.signal_cfg.get("ema_period", 50))
        slope_lookback = int(self.signal_cfg.get("ema_slope_lookback_bars", 10))
        atr_lookback = int(self.signal_cfg.get("atr_lookback_bars", 14))
        min_atr_pct = float(self.signal_cfg.get("min_atr_pct", 0.015))
        max_atr_pct = float(self.signal_cfg.get("max_atr_pct", 0.16))
        min_required = max(slow + signal_period + 5, ema_period + slope_lookback, atr_lookback + 1)
        if len(candles) < min_required:
            return None
        closes = [float(bar.close) for bar in candles]
        _macd_line, _signal_line, hist = macd(closes, fast, slow, signal_period)
        direction: str | None = None
        cross_index: int | None = None
        for index in range(1, len(hist)):
            if hist[index - 1] <= 0 < hist[index]:
                direction, cross_index = "long", index
            elif hist[index - 1] >= 0 > hist[index]:
                direction, cross_index = "short", index
        if direction is None or cross_index is None or cross_index < len(candles) - fresh_bars:
            return None
        atr_pct = self._atr_pct(candles, atr_lookback)
        if atr_pct < min_atr_pct or atr_pct > max_atr_pct:
            return None
        ema_values = ema(closes, ema_period)
        ema_last = ema_values[-1]
        ema_prior = ema_values[-slope_lookback - 1]
        close = closes[-1]
        if direction == "long" and close > ema_last and ema_last > ema_prior:
            return self._setup(
                "long",
                "macd_trend_volatility_filter",
                "GOLD MACD turned up inside a tradeable volatility band and above rising EMA trend.",
                [
                    f"macd_cross=golden",
                    f"cross_bar={cross_index}/{len(candles)}",
                    f"close={close:.2f}",
                    f"ema{ema_period}={ema_last:.2f}",
                    f"atr={atr_pct:.3f}%",
                    f"session_utc={self.signal_cfg.get('session_start_utc', 7)}-{self.signal_cfg.get('session_end_utc', 20)}",
                ],
                "MACD 反向交叉、价格跌回 EMA 下方，或 ATR 跳出允许区间。",
                strength=74,
                confidence=63,
            )
        if direction == "short" and close < ema_last and ema_last < ema_prior:
            return self._setup(
                "short",
                "macd_trend_volatility_filter",
                "GOLD MACD turned down inside a tradeable volatility band and below falling EMA trend.",
                [
                    f"macd_cross=death",
                    f"cross_bar={cross_index}/{len(candles)}",
                    f"close={close:.2f}",
                    f"ema{ema_period}={ema_last:.2f}",
                    f"atr={atr_pct:.3f}%",
                    f"session_utc={self.signal_cfg.get('session_start_utc', 7)}-{self.signal_cfg.get('session_end_utc', 20)}",
                ],
                "MACD 反向交叉、价格站回 EMA 上方，或 ATR 跳出允许区间。",
                strength=74,
                confidence=63,
            )
        return None

    def _grid(self, candles: list) -> dict | None:
        lookback = int(self.signal_cfg.get("lookback_bars", 48))
        step_pct = float(self.signal_cfg.get("grid_step_pct", 0.12))
        rows = candles[-lookback:]
        closes = [float(bar.close) for bar in rows]
        anchor = sum(closes) / len(closes)
        last = float(candles[-1].close)
        deviation = (last - anchor) / anchor * 100 if anchor else 0.0
        if deviation <= -step_pct:
            return self._setup(
                "long",
                "grid_mean_reversion",
                f"GOLD grid long: price is {deviation:.2f}% below rolling anchor.",
                [f"anchor={anchor:.2f}", f"deviation={deviation:.2f}%", f"grid_step={step_pct:.2f}%"],
                "价格继续跌破下一格且未回到网格均值。",
                strength=min(88, 68 + int(abs(deviation) * 20)),
                confidence=62,
            )
        if deviation >= step_pct:
            return self._setup(
                "short",
                "grid_mean_reversion",
                f"GOLD grid short: price is {deviation:.2f}% above rolling anchor.",
                [f"anchor={anchor:.2f}", f"deviation={deviation:.2f}%", f"grid_step={step_pct:.2f}%"],
                "价格继续突破下一格且未回到网格均值。",
                strength=min(88, 68 + int(abs(deviation) * 20)),
                confidence=62,
            )
        return None

    def _adr_exhaustion_reversion(self, candles: list) -> dict | None:
        if not self._in_utc_session(str(candles[-1].timestamp)):
            return None
        session_lookback = int(self.signal_cfg.get("session_lookback_bars", 360))
        vwap_lookback = int(self.signal_cfg.get("vwap_lookback_bars", 80))
        atr_lookback = int(self.signal_cfg.get("atr_lookback_bars", 14))
        adx_lookback = int(self.signal_cfg.get("adx_lookback_bars", 14))
        min_range_atr_multiple = float(self.signal_cfg.get("min_range_atr_multiple", 6.0))
        max_adx = float(self.signal_cfg.get("max_adx", 38))
        extreme_zone_pct = float(self.signal_cfg.get("extreme_zone_pct", 18.0))
        min_vwap_gap_pct = float(self.signal_cfg.get("min_vwap_gap_pct", 0.12))
        min_close_reclaim_pct = float(self.signal_cfg.get("min_close_reclaim_pct", 0.015))
        min_required = max(vwap_lookback, atr_lookback + 1, adx_lookback * 2 + 1, 3)
        if len(candles) < min_required:
            return None
        rows = candles[-min(session_lookback, len(candles)) :]
        session_high = max(float(bar.high) for bar in rows)
        session_low = min(float(bar.low) for bar in rows)
        session_range = session_high - session_low
        close = float(candles[-1].close)
        prev = float(candles[-2].close)
        if session_range <= 0 or close <= 0:
            return None
        atr_pct = self._atr_pct(candles, atr_lookback)
        atr_abs = atr_pct / 100 * close
        if atr_abs <= 0 or session_range < atr_abs * min_range_atr_multiple:
            return None
        adx_value, plus_di, minus_di = self._adx(candles, adx_lookback)
        if adx_value > max_adx:
            return None
        vwap = self._rolling_vwap(candles[-vwap_lookback:])
        if vwap <= 0:
            return None
        range_position_pct = (close - session_low) / session_range * 100
        vwap_gap_pct = abs(close - vwap) / vwap * 100
        close_reclaim_pct = abs(close - prev) / prev * 100 if prev else 0.0
        if vwap_gap_pct < min_vwap_gap_pct or close_reclaim_pct < min_close_reclaim_pct:
            return None
        range_atr_multiple = session_range / atr_abs
        if range_position_pct <= extreme_zone_pct and close > prev and close < vwap:
            return self._setup(
                "long",
                "adr_exhaustion_reversion",
                "GOLD is near the lower end of an expanded intraday range and has started reclaiming toward VWAP.",
                [
                    f"close={close:.2f}",
                    f"prev_close={prev:.2f}",
                    f"session_low={session_low:.2f}",
                    f"session_high={session_high:.2f}",
                    f"range_position={range_position_pct:.2f}%",
                    f"range_atr={range_atr_multiple:.2f}x",
                    f"vwap={vwap:.2f}",
                    f"vwap_gap={vwap_gap_pct:.3f}%",
                    f"adx={adx_value:.2f}",
                    f"plus_di={plus_di:.2f}",
                    f"minus_di={minus_di:.2f}",
                    f"session_utc={self.signal_cfg.get('session_start_utc', 7)}-{self.signal_cfg.get('session_end_utc', 20)}",
                ],
                "价格刷新日内低点并远离 VWAP，或 ADX 升破趋势阈值显示极端波动继续单边扩张。",
                strength=72,
                confidence=63,
            )
        if range_position_pct >= 100 - extreme_zone_pct and close < prev and close > vwap:
            return self._setup(
                "short",
                "adr_exhaustion_reversion",
                "GOLD is near the upper end of an expanded intraday range and has started rejecting back toward VWAP.",
                [
                    f"close={close:.2f}",
                    f"prev_close={prev:.2f}",
                    f"session_low={session_low:.2f}",
                    f"session_high={session_high:.2f}",
                    f"range_position={range_position_pct:.2f}%",
                    f"range_atr={range_atr_multiple:.2f}x",
                    f"vwap={vwap:.2f}",
                    f"vwap_gap={vwap_gap_pct:.3f}%",
                    f"adx={adx_value:.2f}",
                    f"plus_di={plus_di:.2f}",
                    f"minus_di={minus_di:.2f}",
                    f"session_utc={self.signal_cfg.get('session_start_utc', 7)}-{self.signal_cfg.get('session_end_utc', 20)}",
                ],
                "价格刷新日内高点并远离 VWAP，或 ADX 升破趋势阈值显示极端波动继续单边扩张。",
                strength=72,
                confidence=63,
            )
        return None

    def _bollinger_reversion(self, candles: list) -> dict | None:
        lookback = int(self.signal_cfg.get("lookback_bars", 20))
        std_mult = float(self.signal_cfg.get("std_mult", 1.6))
        closes = [float(bar.close) for bar in candles[-lookback:]]
        mean = sum(closes) / len(closes)
        std = math.sqrt(sum((close - mean) ** 2 for close in closes) / len(closes))
        upper = mean + std_mult * std
        lower = mean - std_mult * std
        last = closes[-1]
        if last <= lower:
            return self._setup("long", "bollinger_reversion", "GOLD touched lower Bollinger band; mean-reversion long candidate.", [f"close={last:.2f}", f"lower={lower:.2f}", f"mean={mean:.2f}"], "价格继续收在下轨外。", strength=74, confidence=63)
        if last >= upper:
            return self._setup("short", "bollinger_reversion", "GOLD touched upper Bollinger band; mean-reversion short candidate.", [f"close={last:.2f}", f"upper={upper:.2f}", f"mean={mean:.2f}"], "价格继续收在上轨外。", strength=74, confidence=63)
        return None

    def _bollinger_reclaim_filter(self, candles: list) -> dict | None:
        if not self._in_utc_session(str(candles[-1].timestamp)):
            return None
        lookback = int(self.signal_cfg.get("lookback_bars", 20))
        std_mult = float(self.signal_cfg.get("std_mult", 1.8))
        atr_lookback = int(self.signal_cfg.get("atr_lookback_bars", 14))
        min_atr_pct = float(self.signal_cfg.get("min_atr_pct", 0.015))
        max_atr_pct = float(self.signal_cfg.get("max_atr_pct", 0.18))
        min_bandwidth_pct = float(self.signal_cfg.get("min_bandwidth_pct", 0.08))
        if len(candles) < max(lookback, atr_lookback) + 2:
            return None
        atr_pct = self._atr_pct(candles, atr_lookback)
        if atr_pct < min_atr_pct or atr_pct > max_atr_pct:
            return None
        current_closes = [float(bar.close) for bar in candles[-lookback:]]
        prior_closes = [float(bar.close) for bar in candles[-lookback - 1 : -1]]
        current_mean = sum(current_closes) / len(current_closes)
        prior_mean = sum(prior_closes) / len(prior_closes)
        current_std = math.sqrt(sum((close - current_mean) ** 2 for close in current_closes) / len(current_closes))
        prior_std = math.sqrt(sum((close - prior_mean) ** 2 for close in prior_closes) / len(prior_closes))
        current_upper = current_mean + std_mult * current_std
        current_lower = current_mean - std_mult * current_std
        prior_upper = prior_mean + std_mult * prior_std
        prior_lower = prior_mean - std_mult * prior_std
        if current_mean <= 0:
            return None
        bandwidth_pct = (current_upper - current_lower) / current_mean * 100
        if bandwidth_pct < min_bandwidth_pct:
            return None
        last = float(candles[-1].close)
        prev = float(candles[-2].close)
        if prev < prior_lower and current_lower < last < current_mean:
            return self._setup(
                "long",
                "bollinger_reclaim_filter",
                "GOLD reclaimed the lower Bollinger band after a downside stretch inside a tradeable volatility band.",
                [
                    f"prev_close={prev:.2f}",
                    f"prior_lower={prior_lower:.2f}",
                    f"close={last:.2f}",
                    f"lower={current_lower:.2f}",
                    f"mean={current_mean:.2f}",
                    f"bandwidth={bandwidth_pct:.3f}%",
                    f"atr={atr_pct:.3f}%",
                    f"session_utc={self.signal_cfg.get('session_start_utc', 7)}-{self.signal_cfg.get('session_end_utc', 20)}",
                ],
                "价格重新跌破下轨，或回到均线前动能耗尽。",
                strength=73,
                confidence=64,
            )
        if prev > prior_upper and current_mean < last < current_upper:
            return self._setup(
                "short",
                "bollinger_reclaim_filter",
                "GOLD reclaimed the upper Bollinger band from above after an upside stretch inside a tradeable volatility band.",
                [
                    f"prev_close={prev:.2f}",
                    f"prior_upper={prior_upper:.2f}",
                    f"close={last:.2f}",
                    f"upper={current_upper:.2f}",
                    f"mean={current_mean:.2f}",
                    f"bandwidth={bandwidth_pct:.3f}%",
                    f"atr={atr_pct:.3f}%",
                    f"session_utc={self.signal_cfg.get('session_start_utc', 7)}-{self.signal_cfg.get('session_end_utc', 20)}",
                ],
                "价格重新突破上轨，或回到均线前动能耗尽。",
                strength=73,
                confidence=64,
            )
        return None

    def _breakout(self, candles: list) -> dict | None:
        lookback = int(self.signal_cfg.get("lookback_bars", 36))
        buffer_pct = float(self.signal_cfg.get("breakout_buffer_pct", 0.02))
        previous = candles[-lookback - 1 : -1]
        last = candles[-1]
        high = max(float(bar.high) for bar in previous)
        low = min(float(bar.low) for bar in previous)
        close = float(last.close)
        if close >= high * (1 + buffer_pct / 100):
            return self._setup("long", "range_breakout", "GOLD broke above recent range high.", [f"close={close:.2f}", f"range_high={high:.2f}", f"buffer={buffer_pct:.2f}%"], "价格跌回突破区间内。", strength=73, confidence=61)
        if close <= low * (1 - buffer_pct / 100):
            return self._setup("short", "range_breakout", "GOLD broke below recent range low.", [f"close={close:.2f}", f"range_low={low:.2f}", f"buffer={buffer_pct:.2f}%"], "价格收回跌破区间内。", strength=73, confidence=61)
        return None

    def _london_ny_compression_breakout(self, candles: list) -> dict | None:
        if not self._in_utc_session(str(candles[-1].timestamp)):
            return None
        compression_lookback = int(self.signal_cfg.get("compression_lookback_bars", 24))
        breakout_lookback = int(self.signal_cfg.get("breakout_lookback_bars", 18))
        atr_lookback = int(self.signal_cfg.get("atr_lookback_bars", 14))
        max_range_pct = float(self.signal_cfg.get("max_compression_range_pct", 0.18))
        min_atr_pct = float(self.signal_cfg.get("min_atr_pct", 0.015))
        max_atr_pct = float(self.signal_cfg.get("max_atr_pct", 0.18))
        buffer_pct = float(self.signal_cfg.get("breakout_buffer_pct", 0.015))
        if len(candles) < max(compression_lookback, breakout_lookback, atr_lookback) + 1:
            return None
        prior_compression = candles[-compression_lookback - 1 : -1]
        prior_breakout = candles[-breakout_lookback - 1 : -1]
        last = candles[-1]
        close = float(last.close)
        compression_high = max(float(bar.high) for bar in prior_compression)
        compression_low = min(float(bar.low) for bar in prior_compression)
        midpoint = (compression_high + compression_low) / 2
        if midpoint <= 0:
            return None
        range_pct = (compression_high - compression_low) / midpoint * 100
        if range_pct > max_range_pct:
            return None
        atr_pct = self._atr_pct(candles, atr_lookback)
        if atr_pct < min_atr_pct or atr_pct > max_atr_pct:
            return None
        breakout_high = max(float(bar.high) for bar in prior_breakout)
        breakout_low = min(float(bar.low) for bar in prior_breakout)
        if close >= breakout_high * (1 + buffer_pct / 100):
            return self._setup(
                "long",
                "london_ny_compression_breakout",
                "GOLD broke higher from a compressed London-New York overlap range.",
                [
                    f"close={close:.2f}",
                    f"breakout_high={breakout_high:.2f}",
                    f"compression_range={range_pct:.3f}%",
                    f"atr={atr_pct:.3f}%",
                    f"session_utc={self.signal_cfg.get('session_start_utc', 13)}-{self.signal_cfg.get('session_end_utc', 17)}",
                ],
                "价格收回压缩区间或突破后 3 根 5m K 线无延续。",
                strength=76,
                confidence=64,
            )
        if close <= breakout_low * (1 - buffer_pct / 100):
            return self._setup(
                "short",
                "london_ny_compression_breakout",
                "GOLD broke lower from a compressed London-New York overlap range.",
                [
                    f"close={close:.2f}",
                    f"breakout_low={breakout_low:.2f}",
                    f"compression_range={range_pct:.3f}%",
                    f"atr={atr_pct:.3f}%",
                    f"session_utc={self.signal_cfg.get('session_start_utc', 13)}-{self.signal_cfg.get('session_end_utc', 17)}",
                ],
                "价格收回压缩区间或突破后 3 根 5m K 线无延续。",
                strength=76,
                confidence=64,
            )
        return None

    def _ny_opening_range_breakout(self, candles: list) -> dict | None:
        if not self._in_utc_session(str(candles[-1].timestamp)):
            return None
        opening_start = int(self.signal_cfg.get("opening_range_start_utc", 13))
        opening_end = int(self.signal_cfg.get("opening_range_end_utc", 14))
        atr_lookback = int(self.signal_cfg.get("atr_lookback_bars", 14))
        adx_lookback = int(self.signal_cfg.get("adx_lookback_bars", 14))
        min_atr_pct = float(self.signal_cfg.get("min_atr_pct", 0.015))
        max_atr_pct = float(self.signal_cfg.get("max_atr_pct", 0.30))
        min_range_pct = float(self.signal_cfg.get("min_opening_range_pct", 0.06))
        max_range_pct = float(self.signal_cfg.get("max_opening_range_pct", 0.40))
        breakout_buffer_pct = float(self.signal_cfg.get("breakout_buffer_pct", 0.015))
        min_adx = float(self.signal_cfg.get("min_adx", 14))
        min_volume_ratio = float(self.signal_cfg.get("min_volume_ratio", 0.75))
        min_required = max(atr_lookback + 1, adx_lookback * 2 + 1, 20)
        if len(candles) < min_required:
            return None
        try:
            last_time = datetime.fromisoformat(str(candles[-1].timestamp).replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            return None
        if last_time.hour < opening_end:
            return None
        opening_rows = []
        same_day_rows = []
        for bar in candles:
            try:
                bar_time = datetime.fromisoformat(str(bar.timestamp).replace("Z", "+00:00")).astimezone(timezone.utc)
            except ValueError:
                continue
            if bar_time.date() != last_time.date():
                continue
            same_day_rows.append(bar)
            if opening_start <= bar_time.hour < opening_end:
                opening_rows.append(bar)
        if len(opening_rows) < 6 or len(same_day_rows) < len(opening_rows) + 1:
            return None
        range_high = max(float(bar.high) for bar in opening_rows)
        range_low = min(float(bar.low) for bar in opening_rows)
        midpoint = (range_high + range_low) / 2
        if midpoint <= 0:
            return None
        range_pct = (range_high - range_low) / midpoint * 100
        if range_pct < min_range_pct or range_pct > max_range_pct:
            return None
        atr_pct = self._atr_pct(candles, atr_lookback)
        if atr_pct < min_atr_pct or atr_pct > max_atr_pct:
            return None
        adx_value, plus_di, minus_di = self._adx(candles, adx_lookback)
        if adx_value < min_adx:
            return None
        recent_volume = sum(max(float(bar.volume), 0.0) for bar in candles[-3:]) / 3
        average_volume = sum(max(float(bar.volume), 0.0) for bar in same_day_rows) / len(same_day_rows)
        volume_ratio = recent_volume / average_volume if average_volume else 0.0
        if volume_ratio < min_volume_ratio:
            return None
        previous_close = float(candles[-2].close)
        close = float(candles[-1].close)
        if previous_close <= range_high and close >= range_high * (1 + breakout_buffer_pct / 100) and plus_di > minus_di:
            return self._setup(
                "long",
                "ny_opening_range_breakout",
                "GOLD broke above the New York opening range with volatility, ADX, and volume confirmation.",
                [
                    f"close={close:.2f}",
                    f"opening_high={range_high:.2f}",
                    f"opening_low={range_low:.2f}",
                    f"opening_range={range_pct:.3f}%",
                    f"atr={atr_pct:.3f}%",
                    f"adx={adx_value:.2f}",
                    f"plus_di={plus_di:.2f}",
                    f"minus_di={minus_di:.2f}",
                    f"volume_ratio={volume_ratio:.2f}",
                    f"opening_range_utc={opening_start}-{opening_end}",
                    f"session_utc={self.signal_cfg.get('session_start_utc', 14)}-{self.signal_cfg.get('session_end_utc', 17)}",
                ],
                "价格收回纽约开盘区间内、DI 方向反转，或突破后两根 5m K 线无延续。",
                strength=75,
                confidence=64,
            )
        if previous_close >= range_low and close <= range_low * (1 - breakout_buffer_pct / 100) and minus_di > plus_di:
            return self._setup(
                "short",
                "ny_opening_range_breakout",
                "GOLD broke below the New York opening range with volatility, ADX, and volume confirmation.",
                [
                    f"close={close:.2f}",
                    f"opening_high={range_high:.2f}",
                    f"opening_low={range_low:.2f}",
                    f"opening_range={range_pct:.3f}%",
                    f"atr={atr_pct:.3f}%",
                    f"adx={adx_value:.2f}",
                    f"plus_di={plus_di:.2f}",
                    f"minus_di={minus_di:.2f}",
                    f"volume_ratio={volume_ratio:.2f}",
                    f"opening_range_utc={opening_start}-{opening_end}",
                    f"session_utc={self.signal_cfg.get('session_start_utc', 14)}-{self.signal_cfg.get('session_end_utc', 17)}",
                ],
                "价格收回纽约开盘区间内、DI 方向反转，或突破后两根 5m K 线无延续。",
                strength=75,
                confidence=64,
            )
        return None

    def _false_breakout_reversal(self, candles: list) -> dict | None:
        if not self._in_utc_session(str(candles[-1].timestamp)):
            return None
        lookback = int(self.signal_cfg.get("lookback_bars", 36))
        atr_lookback = int(self.signal_cfg.get("atr_lookback_bars", 14))
        adx_lookback = int(self.signal_cfg.get("adx_lookback_bars", 14))
        breakout_buffer_pct = float(self.signal_cfg.get("breakout_buffer_pct", 0.015))
        reclaim_buffer_pct = float(self.signal_cfg.get("reclaim_buffer_pct", 0.005))
        min_range_pct = float(self.signal_cfg.get("min_range_pct", 0.08))
        max_range_pct = float(self.signal_cfg.get("max_range_pct", 0.32))
        min_atr_pct = float(self.signal_cfg.get("min_atr_pct", 0.015))
        max_atr_pct = float(self.signal_cfg.get("max_atr_pct", 0.24))
        max_adx = float(self.signal_cfg.get("max_adx", 34))
        min_required = max(lookback + 2, atr_lookback + 1, adx_lookback * 2 + 1)
        if len(candles) < min_required:
            return None
        atr_pct = self._atr_pct(candles, atr_lookback)
        if atr_pct < min_atr_pct or atr_pct > max_atr_pct:
            return None
        adx_value, plus_di, minus_di = self._adx(candles, adx_lookback)
        if adx_value > max_adx:
            return None
        prior_range = candles[-lookback - 2 : -2]
        if len(prior_range) < lookback:
            return None
        range_high = max(float(bar.high) for bar in prior_range)
        range_low = min(float(bar.low) for bar in prior_range)
        midpoint = (range_high + range_low) / 2
        if midpoint <= 0:
            return None
        range_pct = (range_high - range_low) / midpoint * 100
        if range_pct < min_range_pct or range_pct > max_range_pct:
            return None
        probe = candles[-2]
        last = candles[-1]
        probe_close = float(probe.close)
        close = float(last.close)
        previous_close = float(candles[-3].close)
        upside_probe = probe_close >= range_high * (1 + breakout_buffer_pct / 100)
        downside_probe = probe_close <= range_low * (1 - breakout_buffer_pct / 100)
        if upside_probe and close < range_high * (1 - reclaim_buffer_pct / 100) and close < probe_close and previous_close <= range_high:
            return self._setup(
                "short",
                "false_breakout_reversal",
                "GOLD failed an upside range breakout and reclaimed back inside the prior range while trend strength is capped.",
                [
                    f"close={close:.2f}",
                    f"probe_close={probe_close:.2f}",
                    f"range_high={range_high:.2f}",
                    f"range_low={range_low:.2f}",
                    f"range={range_pct:.3f}%",
                    f"atr={atr_pct:.3f}%",
                    f"adx={adx_value:.2f}",
                    f"plus_di={plus_di:.2f}",
                    f"minus_di={minus_di:.2f}",
                    f"session_utc={self.signal_cfg.get('session_start_utc', 7)}-{self.signal_cfg.get('session_end_utc', 20)}",
                ],
                "价格重新收上突破高点，ADX 升破趋势阈值，或回到区间后没有延续。",
                strength=74,
                confidence=64,
            )
        if downside_probe and close > range_low * (1 + reclaim_buffer_pct / 100) and close > probe_close and previous_close >= range_low:
            return self._setup(
                "long",
                "false_breakout_reversal",
                "GOLD failed a downside range breakout and reclaimed back inside the prior range while trend strength is capped.",
                [
                    f"close={close:.2f}",
                    f"probe_close={probe_close:.2f}",
                    f"range_high={range_high:.2f}",
                    f"range_low={range_low:.2f}",
                    f"range={range_pct:.3f}%",
                    f"atr={atr_pct:.3f}%",
                    f"adx={adx_value:.2f}",
                    f"plus_di={plus_di:.2f}",
                    f"minus_di={minus_di:.2f}",
                    f"session_utc={self.signal_cfg.get('session_start_utc', 7)}-{self.signal_cfg.get('session_end_utc', 20)}",
                ],
                "价格重新收破突破低点，ADX 升破趋势阈值，或回到区间后没有延续。",
                strength=74,
                confidence=64,
            )
        return None

    def _breakout_retest_continuation(self, candles: list) -> dict | None:
        if not self._in_utc_session(str(candles[-1].timestamp)):
            return None
        lookback = int(self.signal_cfg.get("lookback_bars", 48))
        atr_lookback = int(self.signal_cfg.get("atr_lookback_bars", 14))
        adx_lookback = int(self.signal_cfg.get("adx_lookback_bars", 14))
        breakout_buffer_pct = float(self.signal_cfg.get("breakout_buffer_pct", 0.02))
        retest_tolerance_pct = float(self.signal_cfg.get("retest_tolerance_pct", 0.04))
        min_range_pct = float(self.signal_cfg.get("min_range_pct", 0.10))
        max_range_pct = float(self.signal_cfg.get("max_range_pct", 0.45))
        min_atr_pct = float(self.signal_cfg.get("min_atr_pct", 0.015))
        max_atr_pct = float(self.signal_cfg.get("max_atr_pct", 0.28))
        min_adx = float(self.signal_cfg.get("min_adx", 18))
        min_volume_ratio = float(self.signal_cfg.get("min_volume_ratio", 0.75))
        min_required = max(lookback + 2, atr_lookback + 1, adx_lookback * 2 + 1)
        if len(candles) < min_required:
            return None
        atr_pct = self._atr_pct(candles, atr_lookback)
        if atr_pct < min_atr_pct or atr_pct > max_atr_pct:
            return None
        adx_value, plus_di, minus_di = self._adx(candles, adx_lookback)
        if adx_value < min_adx:
            return None
        prior_range = candles[-lookback - 2 : -2]
        if len(prior_range) < lookback:
            return None
        range_high = max(float(bar.high) for bar in prior_range)
        range_low = min(float(bar.low) for bar in prior_range)
        midpoint = (range_high + range_low) / 2
        if midpoint <= 0:
            return None
        range_pct = (range_high - range_low) / midpoint * 100
        if range_pct < min_range_pct or range_pct > max_range_pct:
            return None
        recent_volume = sum(max(float(bar.volume), 0.0) for bar in candles[-3:]) / 3
        average_volume = sum(max(float(bar.volume), 0.0) for bar in candles[-lookback:]) / lookback
        volume_ratio = recent_volume / average_volume if average_volume else 0.0
        if volume_ratio < min_volume_ratio:
            return None
        probe = candles[-2]
        last = candles[-1]
        probe_close = float(probe.close)
        close = float(last.close)
        open_price = float(last.open)
        low = float(last.low)
        high = float(last.high)
        upside_breakout = probe_close >= range_high * (1 + breakout_buffer_pct / 100)
        downside_breakout = probe_close <= range_low * (1 - breakout_buffer_pct / 100)
        if (
            upside_breakout
            and low <= range_high * (1 + retest_tolerance_pct / 100)
            and close > range_high * (1 + breakout_buffer_pct / 100)
            and close > open_price
            and plus_di > minus_di
        ):
            return self._setup(
                "long",
                "breakout_retest_continuation",
                "GOLD broke above a 5m range, retested the breakout shelf, and resumed higher with directional strength.",
                [
                    f"close={close:.2f}",
                    f"probe_close={probe_close:.2f}",
                    f"range_high={range_high:.2f}",
                    f"range_low={range_low:.2f}",
                    f"range={range_pct:.3f}%",
                    f"atr={atr_pct:.3f}%",
                    f"adx={adx_value:.2f}",
                    f"plus_di={plus_di:.2f}",
                    f"minus_di={minus_di:.2f}",
                    f"volume_ratio={volume_ratio:.2f}",
                    f"session_utc={self.signal_cfg.get('session_start_utc', 7)}-{self.signal_cfg.get('session_end_utc', 20)}",
                ],
                "价格收回区间内、回踩失败，或 DI/ADX 不再支持突破延续。",
                strength=75,
                confidence=64,
            )
        if (
            downside_breakout
            and high >= range_low * (1 - retest_tolerance_pct / 100)
            and close < range_low * (1 - breakout_buffer_pct / 100)
            and close < open_price
            and minus_di > plus_di
        ):
            return self._setup(
                "short",
                "breakout_retest_continuation",
                "GOLD broke below a 5m range, retested the breakdown shelf, and resumed lower with directional strength.",
                [
                    f"close={close:.2f}",
                    f"probe_close={probe_close:.2f}",
                    f"range_high={range_high:.2f}",
                    f"range_low={range_low:.2f}",
                    f"range={range_pct:.3f}%",
                    f"atr={atr_pct:.3f}%",
                    f"adx={adx_value:.2f}",
                    f"plus_di={plus_di:.2f}",
                    f"minus_di={minus_di:.2f}",
                    f"volume_ratio={volume_ratio:.2f}",
                    f"session_utc={self.signal_cfg.get('session_start_utc', 7)}-{self.signal_cfg.get('session_end_utc', 20)}",
                ],
                "价格收回区间内、回踩失败，或 DI/ADX 不再支持跌破延续。",
                strength=75,
                confidence=64,
            )
        return None

    def _psych_level_rejection(self, candles: list) -> dict | None:
        if not self._in_utc_session(str(candles[-1].timestamp)):
            return None
        atr_lookback = int(self.signal_cfg.get("atr_lookback_bars", 14))
        adx_lookback = int(self.signal_cfg.get("adx_lookback_bars", 14))
        level_step = float(self.signal_cfg.get("level_step", 25.0))
        min_atr_pct = float(self.signal_cfg.get("min_atr_pct", 0.015))
        max_atr_pct = float(self.signal_cfg.get("max_atr_pct", 0.28))
        max_adx = float(self.signal_cfg.get("max_adx", 32))
        sweep_tolerance_pct = float(self.signal_cfg.get("sweep_tolerance_pct", 0.015))
        reclaim_buffer_pct = float(self.signal_cfg.get("reclaim_buffer_pct", 0.006))
        min_wick_pct = float(self.signal_cfg.get("min_rejection_wick_pct", 0.025))
        max_close_distance_pct = float(self.signal_cfg.get("max_close_distance_pct", 0.10))
        min_required = max(atr_lookback + 1, adx_lookback * 2 + 1)
        if len(candles) < min_required or level_step <= 0:
            return None
        atr_pct = self._atr_pct(candles, atr_lookback)
        if atr_pct < min_atr_pct or atr_pct > max_atr_pct:
            return None
        adx_value, plus_di, minus_di = self._adx(candles, adx_lookback)
        if adx_value > max_adx:
            return None
        last = candles[-1]
        close = float(last.close)
        open_price = float(last.open)
        high = float(last.high)
        low = float(last.low)
        if close <= 0:
            return None
        level = round(close / level_step) * level_step
        if level <= 0:
            return None
        close_distance_pct = abs(close - level) / level * 100
        if close_distance_pct > max_close_distance_pct:
            return None
        lower_wick_pct = (min(open_price, close) - low) / level * 100
        upper_wick_pct = (high - max(open_price, close)) / level * 100
        swept_support = low <= level * (1 - sweep_tolerance_pct / 100)
        reclaimed_support = close >= level * (1 + reclaim_buffer_pct / 100)
        swept_resistance = high >= level * (1 + sweep_tolerance_pct / 100)
        rejected_resistance = close <= level * (1 - reclaim_buffer_pct / 100)
        if swept_support and reclaimed_support and close > open_price and lower_wick_pct >= min_wick_pct:
            return self._setup(
                "long",
                "psych_level_rejection",
                "GOLD swept below a nearby psychological level and closed back above it while trend strength stayed capped.",
                [
                    f"close={close:.2f}",
                    f"level={level:.2f}",
                    f"low={low:.2f}",
                    f"lower_wick={lower_wick_pct:.3f}%",
                    f"atr={atr_pct:.3f}%",
                    f"adx={adx_value:.2f}",
                    f"plus_di={plus_di:.2f}",
                    f"minus_di={minus_di:.2f}",
                    f"session_utc={self.signal_cfg.get('session_start_utc', 7)}-{self.signal_cfg.get('session_end_utc', 20)}",
                ],
                "价格重新跌破心理整数位，或 ADX 升破趋势阈值显示扫单失败转为趋势延续。",
                strength=73,
                confidence=63,
            )
        if swept_resistance and rejected_resistance and close < open_price and upper_wick_pct >= min_wick_pct:
            return self._setup(
                "short",
                "psych_level_rejection",
                "GOLD swept above a nearby psychological level and closed back below it while trend strength stayed capped.",
                [
                    f"close={close:.2f}",
                    f"level={level:.2f}",
                    f"high={high:.2f}",
                    f"upper_wick={upper_wick_pct:.3f}%",
                    f"atr={atr_pct:.3f}%",
                    f"adx={adx_value:.2f}",
                    f"plus_di={plus_di:.2f}",
                    f"minus_di={minus_di:.2f}",
                    f"session_utc={self.signal_cfg.get('session_start_utc', 7)}-{self.signal_cfg.get('session_end_utc', 20)}",
                ],
                "价格重新站上心理整数位，或 ADX 升破趋势阈值显示扫单失败转为趋势延续。",
                strength=73,
                confidence=63,
            )
        return None

    def _fibonacci(self, candles: list) -> dict | None:
        lookback = int(self.signal_cfg.get("lookback_bars", 96))
        tolerance_pct = float(self.signal_cfg.get("tolerance_pct", 0.08))
        rows = candles[-lookback:]
        high = max(float(bar.high) for bar in rows)
        low = min(float(bar.low) for bar in rows)
        if high <= low:
            return None
        last = float(candles[-1].close)
        prev = float(candles[-2].close)
        midpoint = (high + low) / 2
        levels = {
            "fib_0.382": high - (high - low) * 0.382,
            "fib_0.5": high - (high - low) * 0.5,
            "fib_0.618": high - (high - low) * 0.618,
        }
        nearest_name, nearest = min(levels.items(), key=lambda item: abs(last - item[1]) / item[1])
        distance_pct = abs(last - nearest) / nearest * 100
        if distance_pct > tolerance_pct:
            return None
        if last >= midpoint and last >= prev:
            return self._setup("long", "fibonacci_pullback", f"GOLD bounced near {nearest_name} support.", [f"level={nearest:.2f}", f"distance={distance_pct:.2f}%", f"range={low:.2f}-{high:.2f}"], "价格跌破该 Fibonacci 回撤位。", strength=72, confidence=62)
        if last < midpoint and last <= prev:
            return self._setup("short", "fibonacci_rejection", f"GOLD rejected near {nearest_name} resistance.", [f"level={nearest:.2f}", f"distance={distance_pct:.2f}%", f"range={low:.2f}-{high:.2f}"], "价格重新站上该 Fibonacci 阻力位。", strength=72, confidence=62)
        return None

    def _ema50_position(self, candles: list) -> dict | None:
        period = int(self.signal_cfg.get("ema_period", 50))
        tolerance_pct = float(self.signal_cfg.get("tolerance_pct", 0.1))
        closes = [float(bar.close) for bar in candles]
        ema_values = ema(closes, period)
        last = closes[-1]
        prev = closes[-2]
        ema_last = ema_values[-1]
        distance_pct = (last - ema_last) / ema_last * 100 if ema_last else 0.0
        if abs(distance_pct) > tolerance_pct:
            return None
        if last > prev and last >= ema_last:
            return self._setup("long", "ema50_bounce", "GOLD is bouncing from EMA50 position.", [f"close={last:.2f}", f"ema{period}={ema_last:.2f}", f"distance={distance_pct:.2f}%"], "价格重新跌破 EMA50。", strength=72, confidence=62)
        if last < prev and last <= ema_last:
            return self._setup("short", "ema50_rejection", "GOLD is rejecting from EMA50 position.", [f"close={last:.2f}", f"ema{period}={ema_last:.2f}", f"distance={distance_pct:.2f}%"], "价格重新站上 EMA50。", strength=72, confidence=62)
        return None

    def _adx_ema_pullback(self, candles: list) -> dict | None:
        if not self._in_utc_session(str(candles[-1].timestamp)):
            return None
        ema_fast_period = int(self.signal_cfg.get("ema_fast_period", 20))
        ema_slow_period = int(self.signal_cfg.get("ema_slow_period", 50))
        slope_lookback = int(self.signal_cfg.get("ema_slope_lookback_bars", 8))
        adx_lookback = int(self.signal_cfg.get("adx_lookback_bars", 14))
        min_adx = float(self.signal_cfg.get("min_adx", 22))
        max_pullback_pct = float(self.signal_cfg.get("max_pullback_pct", 0.12))
        atr_lookback = int(self.signal_cfg.get("atr_lookback_bars", 14))
        min_atr_pct = float(self.signal_cfg.get("min_atr_pct", 0.015))
        max_atr_pct = float(self.signal_cfg.get("max_atr_pct", 0.22))
        min_required = max(ema_slow_period + slope_lookback + 1, adx_lookback * 2 + 1, atr_lookback + 1)
        if len(candles) < min_required:
            return None
        atr_pct = self._atr_pct(candles, atr_lookback)
        if atr_pct < min_atr_pct or atr_pct > max_atr_pct:
            return None
        adx_value, plus_di, minus_di = self._adx(candles, adx_lookback)
        if adx_value < min_adx:
            return None
        closes = [float(bar.close) for bar in candles]
        fast_values = ema(closes, ema_fast_period)
        slow_values = ema(closes, ema_slow_period)
        fast = fast_values[-1]
        slow = slow_values[-1]
        slow_prior = slow_values[-slope_lookback - 1]
        last = closes[-1]
        prev = closes[-2]
        if fast <= 0 or slow <= 0:
            return None
        distance_to_fast_pct = abs(last - fast) / fast * 100
        prev_distance_to_fast_pct = abs(prev - fast_values[-2]) / fast_values[-2] * 100 if fast_values[-2] else 0.0
        if distance_to_fast_pct > max_pullback_pct and prev_distance_to_fast_pct > max_pullback_pct:
            return None
        if fast > slow > slow_prior and plus_di > minus_di and prev <= fast_values[-2] * (1 + max_pullback_pct / 100) and last > prev and last >= fast:
            return self._setup(
                "long",
                "adx_ema_pullback",
                "GOLD pulled back to a rising EMA stack while ADX confirms directional trend strength.",
                [
                    f"close={last:.2f}",
                    f"ema{ema_fast_period}={fast:.2f}",
                    f"ema{ema_slow_period}={slow:.2f}",
                    f"adx={adx_value:.2f}",
                    f"plus_di={plus_di:.2f}",
                    f"minus_di={minus_di:.2f}",
                    f"atr={atr_pct:.3f}%",
                    f"session_utc={self.signal_cfg.get('session_start_utc', 7)}-{self.signal_cfg.get('session_end_utc', 20)}",
                ],
                "价格重新跌破快慢 EMA，ADX 跌破阈值，或 DI 方向反转。",
                strength=75,
                confidence=64,
            )
        if fast < slow < slow_prior and minus_di > plus_di and prev >= fast_values[-2] * (1 - max_pullback_pct / 100) and last < prev and last <= fast:
            return self._setup(
                "short",
                "adx_ema_pullback",
                "GOLD pulled back to a falling EMA stack while ADX confirms directional trend strength.",
                [
                    f"close={last:.2f}",
                    f"ema{ema_fast_period}={fast:.2f}",
                    f"ema{ema_slow_period}={slow:.2f}",
                    f"adx={adx_value:.2f}",
                    f"plus_di={plus_di:.2f}",
                    f"minus_di={minus_di:.2f}",
                    f"atr={atr_pct:.3f}%",
                    f"session_utc={self.signal_cfg.get('session_start_utc', 7)}-{self.signal_cfg.get('session_end_utc', 20)}",
                ],
                "价格重新站上快慢 EMA，ADX 跌破阈值，或 DI 方向反转。",
                strength=75,
                confidence=64,
            )
        return None

    def _vwap_extension_reversion(self, candles: list) -> dict | None:
        if not self._in_utc_session(str(candles[-1].timestamp)):
            return None
        lookback = int(self.signal_cfg.get("vwap_lookback_bars", 48))
        atr_lookback = int(self.signal_cfg.get("atr_lookback_bars", 14))
        adx_lookback = int(self.signal_cfg.get("adx_lookback_bars", 14))
        min_extension_pct = float(self.signal_cfg.get("min_extension_pct", 0.16))
        min_remaining_to_vwap_pct = float(self.signal_cfg.get("min_remaining_to_vwap_pct", 0.04))
        min_atr_pct = float(self.signal_cfg.get("min_atr_pct", 0.015))
        max_atr_pct = float(self.signal_cfg.get("max_atr_pct", 0.24))
        max_adx = float(self.signal_cfg.get("max_adx", 30))
        min_volume_ratio = float(self.signal_cfg.get("min_volume_ratio", 0.8))
        min_required = max(lookback, atr_lookback + 1, adx_lookback * 2 + 1)
        if len(candles) < min_required:
            return None
        atr_pct = self._atr_pct(candles, atr_lookback)
        if atr_pct < min_atr_pct or atr_pct > max_atr_pct:
            return None
        adx_value, plus_di, minus_di = self._adx(candles, adx_lookback)
        if adx_value > max_adx:
            return None
        rows = candles[-lookback:]
        volume_sum = sum(max(float(bar.volume), 0.0) for bar in rows)
        if volume_sum <= 0:
            return None
        vwap = sum(((float(bar.high) + float(bar.low) + float(bar.close)) / 3.0) * max(float(bar.volume), 0.0) for bar in rows) / volume_sum
        if vwap <= 0:
            return None
        close = float(candles[-1].close)
        prev = float(candles[-2].close)
        prev_vwap = self._rolling_vwap(candles[-lookback - 1 : -1])
        if prev_vwap <= 0:
            return None
        deviation_pct = (close - vwap) / vwap * 100
        prev_deviation_pct = (prev - prev_vwap) / prev_vwap * 100
        recent_volume = sum(max(float(bar.volume), 0.0) for bar in rows[-3:]) / min(3, len(rows))
        average_volume = volume_sum / len(rows)
        volume_ratio = recent_volume / average_volume if average_volume else 0.0
        if volume_ratio < min_volume_ratio:
            return None
        if prev_deviation_pct <= -min_extension_pct and close > prev and -deviation_pct >= min_remaining_to_vwap_pct and abs(deviation_pct) < abs(prev_deviation_pct):
            return self._setup(
                "long",
                "vwap_extension_reversion",
                "GOLD is reverting upward after a downside extension away from rolling VWAP while trend strength is capped.",
                [
                    f"close={close:.2f}",
                    f"vwap={vwap:.2f}",
                    f"deviation={deviation_pct:.3f}%",
                    f"prev_deviation={prev_deviation_pct:.3f}%",
                    f"atr={atr_pct:.3f}%",
                    f"adx={adx_value:.2f}",
                    f"plus_di={plus_di:.2f}",
                    f"minus_di={minus_di:.2f}",
                    f"volume_ratio={volume_ratio:.2f}",
                    f"session_utc={self.signal_cfg.get('session_start_utc', 7)}-{self.signal_cfg.get('session_end_utc', 20)}",
                ],
                "价格继续远离 VWAP、ADX 升破趋势阈值，或成交量不足以确认回归。",
                strength=73,
                confidence=63,
            )
        if prev_deviation_pct >= min_extension_pct and close < prev and deviation_pct >= min_remaining_to_vwap_pct and abs(deviation_pct) < abs(prev_deviation_pct):
            return self._setup(
                "short",
                "vwap_extension_reversion",
                "GOLD is reverting downward after an upside extension away from rolling VWAP while trend strength is capped.",
                [
                    f"close={close:.2f}",
                    f"vwap={vwap:.2f}",
                    f"deviation={deviation_pct:.3f}%",
                    f"prev_deviation={prev_deviation_pct:.3f}%",
                    f"atr={atr_pct:.3f}%",
                    f"adx={adx_value:.2f}",
                    f"plus_di={plus_di:.2f}",
                    f"minus_di={minus_di:.2f}",
                    f"volume_ratio={volume_ratio:.2f}",
                    f"session_utc={self.signal_cfg.get('session_start_utc', 7)}-{self.signal_cfg.get('session_end_utc', 20)}",
                ],
                "价格继续远离 VWAP、ADX 升破趋势阈值，或成交量不足以确认回归。",
                strength=73,
                confidence=63,
            )
        return None

    def _setup(self, direction: str, regime: str, thesis: str, evidence: list[str], invalid_if: str, *, strength: int, confidence: int) -> dict:
        return {
            "direction": direction,
            "regime": regime,
            "thesis": thesis,
            "evidence": evidence,
            "invalid_if": invalid_if,
            "strength": strength,
            "confidence": confidence,
            "factor_scores": {self.engine_type: 100, "trend": 0, "macro": 0, "volatility": 0},
        }

    def _default_min_bars(self) -> int:
        defaults = {
            "grid": 50,
            "adr_exhaustion_reversion": 120,
            "bollinger_reversion": 30,
            "bollinger_reclaim_filter": 50,
            "breakout": 50,
            "london_ny_compression_breakout": 50,
            "ny_opening_range_breakout": 80,
            "breakout_retest_continuation": 80,
            "false_breakout_reversal": 60,
            "psych_level_rejection": 60,
            "macd_trend_volatility_filter": 70,
            "fibonacci": 120,
            "ema50_position": 70,
            "adx_ema_pullback": 90,
            "vwap_extension_reversion": 80,
        }
        return defaults.get(self.engine_type, 50)

    def _in_utc_session(self, timestamp: str) -> bool:
        start = int(self.signal_cfg.get("session_start_utc", 13))
        end = int(self.signal_cfg.get("session_end_utc", 17))
        try:
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            return False
        hour = parsed.astimezone(timezone.utc).hour
        if start <= end:
            return start <= hour < end
        return hour >= start or hour < end

    def _atr_pct(self, candles: list, lookback: int) -> float:
        rows = candles[-lookback:]
        prev_close = float(candles[-lookback - 1].close)
        true_ranges = []
        for bar in rows:
            high = float(bar.high)
            low = float(bar.low)
            true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
            prev_close = float(bar.close)
        close = float(candles[-1].close)
        return (sum(true_ranges) / len(true_ranges)) / close * 100 if close else 0.0

    def _rolling_vwap(self, candles: list) -> float:
        volume_sum = sum(max(float(bar.volume), 0.0) for bar in candles)
        if volume_sum <= 0:
            return 0.0
        return sum(((float(bar.high) + float(bar.low) + float(bar.close)) / 3.0) * max(float(bar.volume), 0.0) for bar in candles) / volume_sum

    def _adx(self, candles: list, lookback: int) -> tuple[float, float, float]:
        if len(candles) < lookback * 2 + 1:
            return 0.0, 0.0, 0.0
        true_ranges = []
        plus_dm = []
        minus_dm = []
        for index in range(1, len(candles)):
            current = candles[index]
            previous = candles[index - 1]
            high = float(current.high)
            low = float(current.low)
            prev_high = float(previous.high)
            prev_low = float(previous.low)
            prev_close = float(previous.close)
            up_move = high - prev_high
            down_move = prev_low - low
            true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
            plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
            minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)
        tr_sum = sum(true_ranges[:lookback])
        plus_sum = sum(plus_dm[:lookback])
        minus_sum = sum(minus_dm[:lookback])
        dx_values = []
        for index in range(lookback, len(true_ranges)):
            tr_sum = tr_sum - (tr_sum / lookback) + true_ranges[index]
            plus_sum = plus_sum - (plus_sum / lookback) + plus_dm[index]
            minus_sum = minus_sum - (minus_sum / lookback) + minus_dm[index]
            plus_di = 100 * plus_sum / tr_sum if tr_sum else 0.0
            minus_di = 100 * minus_sum / tr_sum if tr_sum else 0.0
            denominator = plus_di + minus_di
            dx_values.append(100 * abs(plus_di - minus_di) / denominator if denominator else 0.0)
        if not dx_values or tr_sum <= 0:
            return 0.0, 0.0, 0.0
        adx_value = sum(dx_values[-lookback:]) / min(lookback, len(dx_values))
        plus_di = 100 * plus_sum / tr_sum
        minus_di = 100 * minus_sum / tr_sum
        return adx_value, plus_di, minus_di

    def _no_signal(self, asset, run_date: str, reason: str, last=None) -> Signal:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        return Signal(
            signal_id=self._sig_id(asset.symbol, run_date, "watch", last.close if last else 0),
            asset=asset.symbol,
            asset_class=asset.asset_class,
            direction="watch",
            strength=0,
            confidence=0,
            horizon=self.engine_type,
            thesis=reason,
            evidence=[reason],
            regime="no_trade",
            factor_scores={self.engine_type: 0},
            source_artifacts=[],
            invalid_if="",
            generated_at=now.isoformat(),
            expires_at=now.isoformat(),
            status="no_signal",
        )

    def _sig_id(self, symbol: str, run_date: str, direction: str, close: float) -> str:
        digest = hashlib.sha256(f"{self.engine_type}:{symbol}:{run_date}:{direction}:{close}".encode()).hexdigest()[:10]
        return f"sig_{self.engine_type}_{symbol.lower()}_{run_date.replace('-', '')}_{digest}"
