"""Typed contracts shared by the three backtest use cases.

Signal evidence, historical strategy ranking, and execution replay deliberately
use separate methods and requests. They share plugin provenance, not one loose
``run(dict)`` payload.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from schemas.analysis import Analysis
from schemas.backtest import BacktestEvidence
from schemas.market_data import Bar
from schemas.signal import Signal


SIGNAL_BACKTEST_REQUEST_SCHEMA = "signal-backtest-request-v1"
HISTORICAL_STRATEGY_BACKTEST_REQUEST_SCHEMA = "historical-strategy-backtest-request-v1"
BACKTEST_PLUGIN_SCHEMA = "backtest-plugin-v1"
BACKTEST_PLUGIN_AUDIT_SCHEMA = "backtest-plugin-audit-v1"

SIGNAL_EVIDENCE_KIND = "signal_evidence"
HISTORICAL_STRATEGY_KIND = "historical_strategy"
STRATEGY_SHADOW_KIND = "strategy_shadow"
BACKTEST_PLUGIN_KINDS = frozenset({
    SIGNAL_EVIDENCE_KIND,
    HISTORICAL_STRATEGY_KIND,
    STRATEGY_SHADOW_KIND,
})


@dataclass(frozen=True)
class SignalBacktestRequest:
    signal: Mapping[str, Any]
    analysis: Mapping[str, Any]
    bars: tuple[Mapping[str, Any], ...]
    backtest_config: Mapping[str, Any]
    run_context: Mapping[str, Any]
    schema_version: str = SIGNAL_BACKTEST_REQUEST_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != SIGNAL_BACKTEST_REQUEST_SCHEMA:
            raise ValueError(f"unsupported signal backtest request schema: {self.schema_version}")
        if not isinstance(self.signal, Mapping) or not isinstance(self.analysis, Mapping):
            raise ValueError("signal backtest signal and analysis must be mappings")
        for field_name in ("backtest_config", "run_context"):
            if not isinstance(getattr(self, field_name), Mapping):
                raise ValueError(f"signal backtest {field_name} must be a mapping")
        if any(not isinstance(bar, Mapping) for bar in self.bars):
            raise ValueError("signal backtest bars must be mappings")
        object.__setattr__(self, "signal", _freeze(self.signal))
        object.__setattr__(self, "analysis", _freeze(self.analysis))
        object.__setattr__(self, "bars", tuple(_freeze(bar) for bar in self.bars))
        object.__setattr__(self, "backtest_config", _freeze(self.backtest_config))
        object.__setattr__(self, "run_context", _freeze(self.run_context))

    @classmethod
    def from_domain(
        cls,
        signal: Signal,
        analysis: Analysis,
        bars: list[Bar] | tuple[Bar, ...],
        *,
        backtest_config: Mapping[str, Any] | None = None,
        run_context: Mapping[str, Any] | None = None,
    ) -> SignalBacktestRequest:
        return cls(
            signal=signal.to_dict(),
            analysis=analysis.to_dict(),
            bars=tuple(bar.to_dict() for bar in bars),
            backtest_config=dict(backtest_config or {}),
            run_context=dict(run_context or {}),
        )

    @property
    def input_hash(self) -> str:
        return _content_hash(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "signal": _thaw(self.signal),
            "analysis": _thaw(self.analysis),
            "bars": [_thaw(bar) for bar in self.bars],
            "backtest_config": _thaw(self.backtest_config),
            "run_context": _thaw(self.run_context),
        }

    def signal_object(self) -> Signal:
        return Signal(**_thaw(self.signal))

    def analysis_object(self) -> Analysis:
        return Analysis(**_thaw(self.analysis))

    def bar_objects(self) -> list[Bar]:
        return [Bar(**_thaw(bar)) for bar in self.bars]


@dataclass(frozen=True)
class HistoricalStrategyBacktestRequest:
    strategy: Mapping[str, Any]
    bars: tuple[Mapping[str, Any], ...]
    signals: tuple[Mapping[str, Any], ...]
    backtest_config: Mapping[str, Any]
    cost_rules: Mapping[str, Any]
    schema_version: str = HISTORICAL_STRATEGY_BACKTEST_REQUEST_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != HISTORICAL_STRATEGY_BACKTEST_REQUEST_SCHEMA:
            raise ValueError(
                f"unsupported historical strategy backtest request schema: {self.schema_version}"
            )
        for field_name in ("strategy", "backtest_config", "cost_rules"):
            if not isinstance(getattr(self, field_name), Mapping):
                raise ValueError(f"historical strategy backtest {field_name} must be a mapping")
        if any(not isinstance(row, Mapping) for row in self.bars):
            raise ValueError("historical strategy backtest bars must be mappings")
        if any(not isinstance(row, Mapping) for row in self.signals):
            raise ValueError("historical strategy backtest signals must be mappings")
        object.__setattr__(self, "strategy", _freeze(self.strategy))
        object.__setattr__(self, "bars", tuple(_freeze(row) for row in self.bars))
        object.__setattr__(self, "signals", tuple(_freeze(row) for row in self.signals))
        object.__setattr__(self, "backtest_config", _freeze(self.backtest_config))
        object.__setattr__(self, "cost_rules", _freeze(self.cost_rules))

    @property
    def input_hash(self) -> str:
        return _content_hash(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "strategy": _thaw(self.strategy),
            "bars": [_thaw(row) for row in self.bars],
            "signals": [_thaw(row) for row in self.signals],
            "backtest_config": _thaw(self.backtest_config),
            "cost_rules": _thaw(self.cost_rules),
        }

    def bar_objects(self) -> list[Bar]:
        return [Bar(**_thaw(row)) for row in self.bars]

    def signal_rows(self) -> list[dict[str, Any]]:
        return [_thaw(row) for row in self.signals]


@runtime_checkable
class SignalBacktestPort(Protocol):
    def evaluate(self, request: SignalBacktestRequest) -> BacktestEvidence:
        ...


@runtime_checkable
class HistoricalStrategyBacktestPort(Protocol):
    def run(self, request: HistoricalStrategyBacktestRequest) -> dict[str, Any]:
        ...


@runtime_checkable
class StrategyShadowReplayPort(Protocol):
    def replay(self, scenario: dict[str, Any]) -> dict[str, Any]:
        ...


class BacktestPluginFactory(Protocol):
    def __call__(self, context: Mapping[str, Any]) -> object:
        ...


@dataclass(frozen=True)
class BacktestPluginDescriptor:
    name: str
    kind: str
    implementation: str
    evidence_tier: str
    promotion_evidence_capable: bool
    degraded_by_design: bool
    capabilities: tuple[str, ...]
    schema_version: str = BACKTEST_PLUGIN_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "kind": self.kind,
            "implementation": self.implementation,
            "evidence_tier": self.evidence_tier,
            "promotion_evidence_capable": self.promotion_evidence_capable,
            "degraded_by_design": self.degraded_by_design,
            "capabilities": list(self.capabilities),
        }


@dataclass(frozen=True)
class BacktestPluginRuntime:
    port: object
    descriptor: BacktestPluginDescriptor
    registry_fingerprint: str

    def audit_dict(self) -> dict[str, Any]:
        return {
            "schema_version": BACKTEST_PLUGIN_AUDIT_SCHEMA,
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


def _content_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
