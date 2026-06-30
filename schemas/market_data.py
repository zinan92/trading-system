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


@dataclass(frozen=True)
class MarketEvent:
    event_id: str
    symbol_scope: list[str]
    title: str
    source: str
    timestamp: str
    sentiment: str
    impact_score: int
    evidence_url: str = ""

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "symbol_scope": self.symbol_scope,
            "title": self.title,
            "source": self.source,
            "timestamp": self.timestamp,
            "sentiment": self.sentiment,
            "impact_score": self.impact_score,
            "evidence_url": self.evidence_url,
        }


@dataclass(frozen=True)
class CleanDatasetManifest:
    symbol: str
    timeframe: str
    source_files: list[str]
    raw_rows: int
    clean_rows: int
    missing_bars: int
    spike_flags: int
    duplicate_rows: int
    timezone: str
    generated_at: str

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "source_files": self.source_files,
            "raw_rows": self.raw_rows,
            "clean_rows": self.clean_rows,
            "missing_bars": self.missing_bars,
            "spike_flags": self.spike_flags,
            "duplicate_rows": self.duplicate_rows,
            "timezone": self.timezone,
            "generated_at": self.generated_at,
        }


@dataclass(frozen=True)
class PaperOrder:
    order_id: str
    ticket_id: str
    status: str
    requested_price: float
    fill_price: float | None
    quantity: float
    filled_at: str
    rejection_reason: str = ""
    gross_fill_price: float | None = None
    slippage_cost: float = 0.0
    spread_cost: float = 0.0
    commission: float = 0.0
    total_cost: float = 0.0
    cost_model: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "ticket_id": self.ticket_id,
            "status": self.status,
            "requested_price": self.requested_price,
            "fill_price": self.fill_price,
            "quantity": self.quantity,
            "filled_at": self.filled_at,
            "rejection_reason": self.rejection_reason,
            "gross_fill_price": self.gross_fill_price,
            "slippage_cost": self.slippage_cost,
            "spread_cost": self.spread_cost,
            "commission": self.commission,
            "total_cost": self.total_cost,
            "cost_model": self.cost_model,
        }


@dataclass(frozen=True)
class PaperPosition:
    symbol: str
    side: str
    quantity: float
    avg_price: float
    unrealized_pnl: float
    realized_pnl: float
    risk_used_pct: float

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "avg_price": self.avg_price,
            "unrealized_pnl": self.unrealized_pnl,
            "realized_pnl": self.realized_pnl,
            "risk_used_pct": self.risk_used_pct,
        }
