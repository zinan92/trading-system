from __future__ import annotations

from dataclasses import dataclass, field


MARKET_DATA_ENVELOPE_SCHEMA = "market-data-envelope-v1"


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
class MarketDataEnvelope:
    """Versioned market-data batch with provenance and trust evidence.

    ``Bar`` remains the sole OHLCV value object used by the trading domain.
    This envelope owns the version because trust and source-selection semantics
    apply to the complete response, not to one candle in isolation.
    """

    schema_version: str
    upstream_schema_version: str
    instrument_id: str
    provider_symbol: str
    asset_class: str
    timeframe: str
    provider: str
    source_mode: str
    requested_source: str
    selected_source: str
    selection_reason: str
    attempted_sources: tuple[str, ...]
    cache_policy: str
    quality_policy: str
    fallback_policy: str
    require_execution_venue: bool
    quality_flags: tuple[str, ...]
    is_synthetic: bool
    served_from: str
    fresh: bool | None
    latest_timestamp: str | None
    age_seconds: float | None
    max_age_seconds: float | None
    execution_venue: bool
    reject_reason: str | None
    access_issues: tuple[str, ...]
    bars: tuple[Bar, ...]

    @property
    def execution_ready(self) -> bool:
        """Return whether this response satisfies the strict live-data policy.

        A real execution venue is necessary but not sufficient.  The caller
        must also have explicitly requested strict, no-fallback, cache-bypassed
        execution data and received that exact source without access warnings.
        """

        return bool(
            self.schema_version == MARKET_DATA_ENVELOPE_SCHEMA
            and self.instrument_id
            and self.provider
            and self.source_mode
            and self.selected_source
            and self.selected_source == self.requested_source
            and self.require_execution_venue
            and self.execution_venue
            and self.cache_policy == "bypass"
            and self.quality_policy == "strict"
            and self.fallback_policy == "none"
            and self.selection_reason == "requested_or_default"
            and self.served_from in {"upstream", "websocket"}
            and self.fresh is True
            and self.is_synthetic is False
            and not self.reject_reason
            and not self.access_issues
            and self.latest_timestamp
            and self.bars
        )

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "upstream_schema_version": self.upstream_schema_version,
            "instrument_id": self.instrument_id,
            "provider_symbol": self.provider_symbol,
            "asset_class": self.asset_class,
            "timeframe": self.timeframe,
            "count": len(self.bars),
            "provider": self.provider,
            "source_mode": self.source_mode,
            "requested_source": self.requested_source,
            "selected_source": self.selected_source,
            "selection_reason": self.selection_reason,
            "attempted_sources": list(self.attempted_sources),
            "cache_policy": self.cache_policy,
            "quality_policy": self.quality_policy,
            "fallback_policy": self.fallback_policy,
            "require_execution_venue": self.require_execution_venue,
            "quality_flags": list(self.quality_flags),
            "is_synthetic": self.is_synthetic,
            "served_from": self.served_from,
            "fresh": self.fresh,
            "latest_timestamp": self.latest_timestamp,
            "age_seconds": self.age_seconds,
            "max_age_seconds": self.max_age_seconds,
            "execution_venue": self.execution_venue,
            "reject_reason": self.reject_reason,
            "access_issues": list(self.access_issues),
            "candles": [bar.to_dict() for bar in self.bars],
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
