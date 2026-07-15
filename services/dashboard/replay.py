"""Trade replay bar selection, time bounds, and replay OHLC metadata."""

from __future__ import annotations

import re

from services.market_data_access import market_data_repository, uses_independent_datafeed


class ReplayMixin:
    def _replay_bars_for_trades(
        self,
        *,
        strategy_id: str,
        requested_timeframe: str,
        artifact_bars: list[dict],
        trades: list[dict],
    ) -> tuple[list[dict], dict]:
        fallback = self._replay_ohlc_meta(
            strategy_id=strategy_id,
            requested_timeframe=requested_timeframe,
            selected_timeframe=requested_timeframe,
            source="strategy_artifact",
            bars=artifact_bars,
            reason="market_db_unavailable",
            trade_count=len(trades or []),
        )
        if not trades or (not uses_independent_datafeed(self.market_db) and not self.market_db.exists()):
            return artifact_bars, fallback
        bounds = self._replay_time_bounds(trades, artifact_bars)
        if not bounds:
            return artifact_bars, {**fallback, "reason": "no_trade_time_bounds"}
        start_epoch, end_epoch = bounds
        if end_epoch <= start_epoch:
            return artifact_bars, {**fallback, "reason": "invalid_trade_time_bounds"}

        selected_timeframe = self._select_replay_timeframe(requested_timeframe, start_epoch, end_epoch)
        store = market_data_repository(self.market_db)
        rows = store.load_bars_between(
            "GOLD",
            selected_timeframe,
            self._format_epoch(start_epoch),
            self._format_epoch(end_epoch),
        )
        if not rows and selected_timeframe != requested_timeframe:
            rows = store.load_bars_between(
                "GOLD",
                requested_timeframe,
                self._format_epoch(start_epoch),
                self._format_epoch(end_epoch),
            )
            if rows:
                selected_timeframe = requested_timeframe
        if not rows:
            return artifact_bars, {**fallback, "reason": "market_db_window_empty"}
        replay_bars = [bar.to_dict() for bar in rows]
        return replay_bars, self._replay_ohlc_meta(
            strategy_id=strategy_id,
            requested_timeframe=requested_timeframe,
            selected_timeframe=selected_timeframe,
            source="market_data_db",
            bars=replay_bars,
            reason="covers_trade_replay_window",
            trade_count=len(trades or []),
            start_epoch=start_epoch,
            end_epoch=end_epoch,
        )

    def _replay_time_bounds(self, trades: list[dict], artifact_bars: list[dict]) -> tuple[float, float] | None:
        times: list[float] = []
        for trade in trades or []:
            for key in ("opened_at", "closed_at"):
                parsed = self._parse_ts(trade.get(key))
                if parsed is not None:
                    times.append(parsed)
        if not times:
            return None
        latest_bar_ts = None
        for bar in artifact_bars or []:
            parsed = self._parse_ts(bar.get("timestamp") if isinstance(bar, dict) else None)
            if parsed is not None:
                latest_bar_ts = parsed if latest_bar_ts is None else max(latest_bar_ts, parsed)
        open_trades = [
            trade
            for trade in trades or []
            if trade.get("opened_at") and not trade.get("closed_at") and str(trade.get("status", "")).lower() != "closed"
        ]
        if open_trades and latest_bar_ts is not None:
            times.append(latest_bar_ts)
        start = max(0.0, min(times) - self._REPLAY_OHLC_PAD_SECONDS)
        end = max(times) + self._REPLAY_OHLC_PAD_SECONDS
        if latest_bar_ts is not None:
            end = min(end, latest_bar_ts)
        return start, end

    def _select_replay_timeframe(self, requested_timeframe: str, start_epoch: float, end_epoch: float) -> str:
        requested = requested_timeframe or "5m"
        requested_seconds = self._timeframe_seconds(requested)
        estimated = (end_epoch - start_epoch) / requested_seconds if requested_seconds else self._REPLAY_OHLC_MAX_BARS + 1
        if estimated <= self._REPLAY_OHLC_MAX_BARS:
            return requested
        if requested != "5m":
            five_min_estimated = (end_epoch - start_epoch) / self._timeframe_seconds("5m")
            if five_min_estimated <= max(self._REPLAY_OHLC_MAX_BARS, 12_000):
                return "5m"
        return requested

    @staticmethod
    def _timeframe_seconds(timeframe: str) -> int:
        units = {"m": 60, "h": 3600, "d": 86400}
        text = str(timeframe or "5m").strip().lower()
        match = re.match(r"^(\d+)([mhd])$", text)
        if not match:
            return 300
        return max(1, int(match.group(1))) * units[match.group(2)]

    def _replay_ohlc_meta(
        self,
        *,
        strategy_id: str,
        requested_timeframe: str,
        selected_timeframe: str,
        source: str,
        bars: list[dict],
        reason: str,
        trade_count: int,
        start_epoch: float | None = None,
        end_epoch: float | None = None,
    ) -> dict:
        first = bars[0].get("timestamp") if bars and isinstance(bars[0], dict) else ""
        last = bars[-1].get("timestamp") if bars and isinstance(bars[-1], dict) else ""
        providers = sorted({
            str(item.get("provider"))
            for item in bars or []
            if isinstance(item, dict) and item.get("provider")
        })
        return {
            "strategy_id": strategy_id,
            "source": source,
            "reason": reason,
            "requested_timeframe": requested_timeframe,
            "timeframe": selected_timeframe,
            "bar_count": len(bars or []),
            "first_timestamp": first,
            "last_timestamp": last,
            "window_start": self._format_epoch(start_epoch) if start_epoch is not None else first,
            "window_end": self._format_epoch(end_epoch) if end_epoch is not None else last,
            "trade_count": trade_count,
            "providers": providers,
            "max_bar_budget": self._REPLAY_OHLC_MAX_BARS,
            "is_complete_replay_window": source == "market_data_db" and bool(bars),
        }
