from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TradeTicket:
    ticket_id: str
    signal_id: str
    asset: str
    asset_class: str
    action: str
    entry_zone: str
    stop_loss: float
    targets: list[float] = field(default_factory=list)
    position_size_pct: float = 0.0
    max_loss_pct: float = 0.0
    order_type: str = "limit"
    time_in_force: str = "day"
    paper_only: bool = True
    trigger: str = ""
    invalid_if: str = ""
    methods: list[str] = field(default_factory=list)
    backtest: dict = field(default_factory=dict)
    signal_regime: str = ""
    signal_strength: int = 0
    signal_confidence: int = 0
    factor_scores: dict[str, float] = field(default_factory=dict)
    source_artifacts: list[str] = field(default_factory=list)
    rationale: str = ""
    counter_rationale: str = ""
    manual_execution_required: bool = True
    verdict: str = "approved"
    trade_quality: dict = field(default_factory=dict)
    generated_at: str = ""
    latest_price: float | None = None
    entry_order_limit_price: float | None = None
    entry_order_ttl_bars: int = 0
    entry_order_timeframe: str = ""
    entry_order_created_bar_timestamp: str = ""

    def to_dict(self) -> dict:
        return {
            "ticket_id": self.ticket_id,
            "signal_id": self.signal_id,
            "asset": self.asset,
            "asset_class": self.asset_class,
            "action": self.action,
            "entry_zone": self.entry_zone,
            "stop_loss": self.stop_loss,
            "targets": self.targets,
            "position_size_pct": self.position_size_pct,
            "max_loss_pct": self.max_loss_pct,
            "order_type": self.order_type,
            "time_in_force": self.time_in_force,
            "paper_only": self.paper_only,
            "trigger": self.trigger,
            "invalid_if": self.invalid_if,
            "methods": self.methods,
            "backtest": self.backtest,
            "signal_regime": self.signal_regime,
            "signal_strength": self.signal_strength,
            "signal_confidence": self.signal_confidence,
            "factor_scores": self.factor_scores,
            "source_artifacts": self.source_artifacts,
            "rationale": self.rationale,
            "counter_rationale": self.counter_rationale,
            "manual_execution_required": self.manual_execution_required,
            "verdict": self.verdict,
            "trade_quality": self.trade_quality,
            "generated_at": self.generated_at,
            "latest_price": self.latest_price,
            "entry_order_limit_price": self.entry_order_limit_price,
            "entry_order_ttl_bars": self.entry_order_ttl_bars,
            "entry_order_timeframe": self.entry_order_timeframe,
            "entry_order_created_bar_timestamp": self.entry_order_created_bar_timestamp,
        }
