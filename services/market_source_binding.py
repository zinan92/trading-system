"""Provider-neutral execution market-source identity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class MarketSourceIdentity:
    """One explicit, execution-grade source bound to Broker and Instrument."""

    source_id: str
    broker_id: str
    environment: str
    instrument_id: str
    execution_venue: bool

    @classmethod
    def from_mapping(cls, value: object) -> "MarketSourceIdentity":
        if not isinstance(value, Mapping):
            raise ValueError("market_source_invalid")
        if value.get("execution_venue") is not True:
            raise ValueError("market_source_not_execution_venue")
        return cls(
            source_id=_text(value.get("source_id"), "market_source_id"),
            broker_id=_text(value.get("broker_id"), "market_source_broker_id").lower(),
            environment=_text(value.get("environment"), "market_source_environment").lower(),
            instrument_id=_text(value.get("instrument_id"), "market_source_instrument_id"),
            execution_venue=True,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "broker_id": self.broker_id,
            "environment": self.environment,
            "instrument_id": self.instrument_id,
            "execution_venue": self.execution_venue,
        }


def _text(value: object, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"{field}_missing")
    return result
