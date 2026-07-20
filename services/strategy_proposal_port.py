"""Authority-limited contract for machine strategy proposal plugins.

Proposal plugins receive immutable research and market context. They may return
an untrusted candidate decision, but they cannot validate, persist, execute, or
promote that decision.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable


STRATEGY_PROPOSAL_REQUEST_SCHEMA = "strategy-proposal-request-v1"
STRATEGY_PROPOSAL_PLUGIN_SCHEMA = "strategy-proposal-plugin-v1"
STRATEGY_PROPOSAL_PLUGIN_AUDIT_SCHEMA = "strategy-proposal-plugin-audit-v1"
MAX_NEWSLETTER_CHARS = 16_000


@dataclass(frozen=True)
class StrategyProposalRequest:
    """Versioned, deeply immutable context supplied by the trusted core."""

    cycle_id: str
    cycle_hours: int
    market: Mapping[str, Any]
    prev_cycle_range: float
    volatility_context: Mapping[str, Any]
    previous_review: Mapping[str, Any]
    replan_context: Mapping[str, Any]
    newsletter_text: str
    schema_version: str = STRATEGY_PROPOSAL_REQUEST_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != STRATEGY_PROPOSAL_REQUEST_SCHEMA:
            raise ValueError(f"unsupported strategy proposal request schema: {self.schema_version}")
        if not self.cycle_id.strip():
            raise ValueError("strategy proposal cycle_id is required")
        if self.cycle_hours <= 0:
            raise ValueError("strategy proposal cycle_hours must be positive")
        for field_name in ("market", "volatility_context", "previous_review", "replan_context"):
            if not isinstance(getattr(self, field_name), Mapping):
                raise ValueError(f"strategy proposal {field_name} must be a mapping")
        if not isinstance(self.newsletter_text, str):
            raise ValueError("strategy proposal newsletter_text must be a string")
        if len(self.newsletter_text) > MAX_NEWSLETTER_CHARS:
            raise ValueError(f"strategy proposal newsletter exceeds {MAX_NEWSLETTER_CHARS} characters")
        object.__setattr__(self, "market", _freeze(self.market))
        object.__setattr__(self, "volatility_context", _freeze(self.volatility_context))
        object.__setattr__(self, "previous_review", _freeze(self.previous_review))
        object.__setattr__(self, "replan_context", _freeze(self.replan_context))
        object.__setattr__(self, "prev_cycle_range", float(self.prev_cycle_range))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "cycle_id": self.cycle_id,
            "cycle_hours": self.cycle_hours,
            "market": _thaw(self.market),
            "prev_cycle_range": self.prev_cycle_range,
            "volatility_context": _thaw(self.volatility_context),
            "previous_review": _thaw(self.previous_review),
            "replan_context": _thaw(self.replan_context),
            "newsletter_text": self.newsletter_text,
        }


@runtime_checkable
class StrategyProposalPort(Protocol):
    """Return one untrusted strategy proposal for core validation."""

    def propose(self, request: StrategyProposalRequest) -> dict[str, Any]:
        ...


class StrategyProposalFactory(Protocol):
    def __call__(self, params: Mapping[str, Any]) -> StrategyProposalPort:
        ...


@dataclass(frozen=True)
class StrategyProposalPluginDescriptor:
    name: str
    implementation: str
    plan_source: str
    default_decision_mode: str
    capabilities: tuple[str, ...] = ("propose",)
    required_context: tuple[str, ...] = ()
    schema_version: str = STRATEGY_PROPOSAL_PLUGIN_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "implementation": self.implementation,
            "plan_source": self.plan_source,
            "default_decision_mode": self.default_decision_mode,
            "capabilities": list(self.capabilities),
            "required_context": list(self.required_context),
        }


@dataclass(frozen=True)
class StrategyProposalRuntime:
    """Selected proposal port plus safe provenance supplied by composition."""

    port: StrategyProposalPort
    descriptor: StrategyProposalPluginDescriptor
    registry_fingerprint: str

    def audit_dict(self) -> dict[str, Any]:
        return {
            "schema_version": STRATEGY_PROPOSAL_PLUGIN_AUDIT_SCHEMA,
            "plugin": self.descriptor.to_dict(),
            "registry_fingerprint": self.registry_fingerprint,
        }


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value
