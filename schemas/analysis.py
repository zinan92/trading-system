from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Analysis:
    analysis_id: str
    signal_id: str
    asset: str
    methods: list[str] = field(default_factory=list)
    thesis: str = ""
    counter_thesis: str = ""
    checklist: list[str] = field(default_factory=list)
    confidence_adjustment: int = 0

    def to_dict(self) -> dict:
        return {
            "analysis_id": self.analysis_id,
            "signal_id": self.signal_id,
            "asset": self.asset,
            "methods": self.methods,
            "thesis": self.thesis,
            "counter_thesis": self.counter_thesis,
            "checklist": self.checklist,
            "confidence_adjustment": self.confidence_adjustment,
        }
