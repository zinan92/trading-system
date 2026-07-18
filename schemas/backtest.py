from __future__ import annotations

from dataclasses import dataclass


BACKTEST_EVIDENCE_SCHEMA = "backtest-evidence-v2"


@dataclass(frozen=True)
class BacktestEvidence:
    backtest_id: str
    signal_id: str
    asset: str
    sample_size: int
    win_rate: float
    avg_r: float
    max_drawdown_pct: float
    verdict: str
    profit_factor: float = 0.0
    evaluated_bars: int = 0
    setup_count: int = 0
    skipped_reason: str = ""
    schema_version: str = BACKTEST_EVIDENCE_SCHEMA
    backtest_plugin: str = "legacy_unclassified"
    evidence_tier: str = "legacy_context"
    input_hash: str = ""
    registry_fingerprint: str = ""
    promotion_eligible: bool = False
    degraded: bool = False

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "backtest_id": self.backtest_id,
            "signal_id": self.signal_id,
            "asset": self.asset,
            "sample_size": self.sample_size,
            "win_rate": self.win_rate,
            "avg_r": self.avg_r,
            "max_drawdown_pct": self.max_drawdown_pct,
            "profit_factor": self.profit_factor,
            "verdict": self.verdict,
            "evaluated_bars": self.evaluated_bars,
            "setup_count": self.setup_count,
            "skipped_reason": self.skipped_reason,
            "backtest_plugin": self.backtest_plugin,
            "evidence_tier": self.evidence_tier,
            "input_hash": self.input_hash,
            "registry_fingerprint": self.registry_fingerprint,
            "promotion_eligible": self.promotion_eligible,
            "degraded": self.degraded,
        }
