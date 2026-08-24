"""Pure OHLCV value objects required by the Grid simulator."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Bar:
    symbol: str
    timeframe: str
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    provider: str
    quality_flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "timestamp": self.timestamp,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "provider": self.provider,
            "quality_flags": self.quality_flags,
        }
