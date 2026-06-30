from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Signal:
    signal_id: str
    asset: str
    asset_class: str
    direction: str
    strength: int
    confidence: int
    horizon: str
    thesis: str
    evidence: list[str] = field(default_factory=list)
    methods: list[str] = field(default_factory=list)
    regime: str = "unknown"
    factor_scores: dict[str, float] = field(default_factory=dict)
    source_artifacts: list[str] = field(default_factory=list)
    backtest_verdict: str = "not_checked"
    invalid_if: str = ""
    generated_at: str = ""
    expires_at: str = ""
    status: str = "new"

    def approved_candidate(self, min_strength: int, min_confidence: int) -> bool:
        return (
            self.direction in {"long", "short"}
            and self.strength >= min_strength
            and self.confidence >= min_confidence
        )

    def to_dict(self) -> dict:
        return {
            "signal_id": self.signal_id,
            "asset": self.asset,
            "asset_class": self.asset_class,
            "direction": self.direction,
            "strength": self.strength,
            "confidence": self.confidence,
            "horizon": self.horizon,
            "thesis": self.thesis,
            "evidence": self.evidence,
            "methods": self.methods,
            "regime": self.regime,
            "factor_scores": self.factor_scores,
            "source_artifacts": self.source_artifacts,
            "backtest_verdict": self.backtest_verdict,
            "invalid_if": self.invalid_if,
            "generated_at": self.generated_at,
            "expires_at": self.expires_at,
            "status": self.status,
        }
