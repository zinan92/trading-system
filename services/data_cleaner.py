from __future__ import annotations

from datetime import datetime, timezone

from schemas.asset import Asset
from schemas.market_data import Bar, CleanDatasetManifest


class DataCleaner:
    def clean(
        self,
        asset: Asset,
        timeframe: str,
        bars: list[Bar],
        source_file: str,
    ) -> tuple[list[Bar], CleanDatasetManifest]:
        sorted_bars = sorted(bars, key=lambda item: item.timestamp)
        deduped: list[Bar] = []
        seen: set[str] = set()
        duplicates = 0
        for bar in sorted_bars:
            if bar.timestamp in seen:
                duplicates += 1
                continue
            seen.add(bar.timestamp)
            deduped.append(bar)

        cleaned: list[Bar] = []
        missing_bars = 0
        spike_flags = 0
        previous_close: float | None = None
        previous_index: int | None = None
        previous_time: datetime | None = None
        expected_seconds = self._timeframe_seconds(timeframe)
        for bar in deduped:
            flags = list(bar.quality_flags)
            current_index = self._mock_index(bar.timestamp)
            if previous_index is not None and current_index is not None and current_index - previous_index > 1:
                flags.append("missing_bar_before")
                missing_bars += current_index - previous_index - 1
            current_time = self._parse_timestamp(bar.timestamp)
            if expected_seconds and previous_time and current_time:
                gap_seconds = (current_time - previous_time).total_seconds()
                if gap_seconds > expected_seconds * 1.5:
                    if "quote_derived_bar" in flags:
                        flags.append("quote_snapshot_gap_before")
                    elif self._is_market_closure_gap(asset, timeframe, previous_time, current_time, gap_seconds):
                        flags.append("market_closure_before")
                    else:
                        flags.append("missing_bar_before")
                        missing_bars += max(1, round(gap_seconds / expected_seconds) - 1)
            if previous_close and previous_close > 0:
                move_pct = abs((bar.close - previous_close) / previous_close) * 100
                if move_pct > 10:
                    flags.append("spike")
                    spike_flags += 1
            cleaned.append(
                Bar(
                    symbol=bar.symbol,
                    timeframe=bar.timeframe,
                    timestamp=bar.timestamp,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                    provider=bar.provider,
                    quality_flags=sorted(set(flags)),
                )
            )
            previous_close = bar.close
            previous_index = current_index if current_index is not None else previous_index
            previous_time = current_time if current_time is not None else previous_time

        manifest = CleanDatasetManifest(
            symbol=asset.symbol,
            timeframe=timeframe,
            source_files=[source_file],
            raw_rows=len(bars),
            clean_rows=len(cleaned),
            missing_bars=missing_bars,
            spike_flags=spike_flags,
            duplicate_rows=duplicates,
            timezone=asset.timezone,
            generated_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        )
        return cleaned, manifest

    def _mock_index(self, timestamp: str) -> int | None:
        if not timestamp.startswith("mock-"):
            return None
        try:
            return int(timestamp.rsplit("-", 1)[1])
        except (IndexError, ValueError):
            return None

    def _parse_timestamp(self, timestamp: str) -> datetime | None:
        if timestamp.startswith("mock-"):
            return None
        try:
            return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            return None

    def _timeframe_seconds(self, timeframe: str) -> int | None:
        units = {"m": 60, "h": 3600, "d": 86400}
        unit = timeframe[-1:]
        if unit not in units:
            return None
        try:
            return int(timeframe[:-1]) * units[unit]
        except ValueError:
            return None

    def _is_market_closure_gap(self, asset: Asset, timeframe: str, previous_time: datetime, current_time: datetime, gap_seconds: float) -> bool:
        if asset.symbol != "GOLD" or timeframe != "5m":
            return False
        daily_maintenance = 50 * 60 <= gap_seconds <= 90 * 60
        long_weekend_or_holiday_closure = gap_seconds >= 12 * 3600
        crosses_weekend = previous_time.weekday() >= 4 and current_time.weekday() in {0, 6}
        return daily_maintenance or long_weekend_or_holiday_closure or crosses_weekend
