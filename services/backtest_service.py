"""Trusted validation and provenance for untrusted backtest plugin results."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from schemas.backtest import BACKTEST_EVIDENCE_SCHEMA, BacktestEvidence
from services.backtest_port import (
    HISTORICAL_STRATEGY_KIND,
    SIGNAL_EVIDENCE_KIND,
    BacktestPluginRuntime,
    HistoricalStrategyBacktestPort,
    HistoricalStrategyBacktestRequest,
    SignalBacktestPort,
    SignalBacktestRequest,
)


ALLOWED_SIGNAL_BACKTEST_VERDICTS = frozenset({"supportive", "mixed", "thin", "no_trade"})
HISTORICAL_METRIC_FIELDS = (
    "trades",
    "wins",
    "losses",
    "win_rate",
    "profit_factor",
    "net_pnl",
    "return_pct",
    "max_drawdown_pct",
    "avg_r",
    "sharpe_per_trade",
    "avg_bars_held",
    "final_equity",
)
REQUIRED_HISTORICAL_METRICS = frozenset(HISTORICAL_METRIC_FIELDS)


class SignalBacktestService:
    def __init__(self, runtime: BacktestPluginRuntime) -> None:
        if runtime.descriptor.kind != SIGNAL_EVIDENCE_KIND:
            raise ValueError("signal backtest service requires a signal_evidence plugin")
        if not isinstance(runtime.port, SignalBacktestPort):
            raise ValueError("signal backtest runtime does not expose evaluate()")
        self.runtime = runtime

    def evaluate(self, request: SignalBacktestRequest) -> BacktestEvidence:
        evidence = self.runtime.port.evaluate(request)
        if not isinstance(evidence, BacktestEvidence):
            raise ValueError("signal backtest plugin must return BacktestEvidence")
        _validate_signal_evidence(evidence, request)
        descriptor = self.runtime.descriptor
        degraded = bool(descriptor.degraded_by_design or evidence.degraded)
        return BacktestEvidence(
            backtest_id=evidence.backtest_id,
            signal_id=evidence.signal_id,
            asset=evidence.asset,
            sample_size=evidence.sample_size,
            win_rate=evidence.win_rate,
            avg_r=evidence.avg_r,
            max_drawdown_pct=evidence.max_drawdown_pct,
            verdict=evidence.verdict,
            profit_factor=evidence.profit_factor,
            evaluated_bars=evidence.evaluated_bars,
            setup_count=evidence.setup_count,
            skipped_reason=evidence.skipped_reason,
            schema_version=BACKTEST_EVIDENCE_SCHEMA,
            backtest_plugin=descriptor.name,
            evidence_tier=descriptor.evidence_tier,
            input_hash=request.input_hash,
            registry_fingerprint=self.runtime.registry_fingerprint,
            # Evidence-source eligibility only. Verdict, sample, regime, and
            # promotion policy remain independent downstream gates.
            promotion_eligible=bool(
                descriptor.promotion_evidence_capable
                and not degraded
                and evidence.sample_size > 0
            ),
            degraded=degraded,
        )


class HistoricalStrategyBacktestService:
    def __init__(self, runtime: BacktestPluginRuntime) -> None:
        if runtime.descriptor.kind != HISTORICAL_STRATEGY_KIND:
            raise ValueError("historical backtest service requires a historical_strategy plugin")
        if not isinstance(runtime.port, HistoricalStrategyBacktestPort):
            raise ValueError("historical backtest runtime does not expose run()")
        self.runtime = runtime

    def run(self, request: HistoricalStrategyBacktestRequest) -> dict[str, Any]:
        result = self.runtime.port.run(request)
        if not isinstance(result, Mapping):
            raise ValueError("historical strategy backtest plugin must return a mapping")
        normalized = dict(result)
        missing = sorted(REQUIRED_HISTORICAL_METRICS - normalized.keys())
        if missing:
            raise ValueError(
                "historical strategy backtest result is missing metrics: " + ",".join(missing)
            )
        for key in ("trades", "wins", "losses"):
            value = normalized[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"historical strategy backtest {key} must be a non-negative integer")
        if normalized["wins"] + normalized["losses"] > normalized["trades"]:
            raise ValueError("historical strategy backtest wins plus losses exceed trades")
        _require_ratio(normalized["win_rate"], "historical strategy backtest win_rate")
        for key in REQUIRED_HISTORICAL_METRICS - {"trades", "wins", "losses", "profit_factor"}:
            _require_finite_number(normalized[key], f"historical strategy backtest {key}")
        for key in ("max_drawdown_pct", "avg_bars_held", "final_equity"):
            if float(normalized[key]) < 0:
                raise ValueError(f"historical strategy backtest {key} must be non-negative")
        profit_factor = normalized["profit_factor"]
        if not isinstance(profit_factor, (int, float)) or isinstance(profit_factor, bool):
            raise ValueError("historical strategy backtest profit_factor must be numeric")
        if math.isnan(float(profit_factor)) or float(profit_factor) < 0:
            raise ValueError("historical strategy backtest profit_factor must be non-negative")
        return {key: normalized[key] for key in HISTORICAL_METRIC_FIELDS}


def _validate_signal_evidence(
    evidence: BacktestEvidence,
    request: SignalBacktestRequest,
) -> None:
    signal = request.signal_object()
    if not evidence.backtest_id.strip():
        raise ValueError("signal backtest backtest_id is required")
    if evidence.signal_id != signal.signal_id:
        raise ValueError("signal backtest signal_id does not match request")
    if evidence.asset != signal.asset:
        raise ValueError("signal backtest asset does not match request")
    if evidence.verdict not in ALLOWED_SIGNAL_BACKTEST_VERDICTS:
        raise ValueError(f"unsupported signal backtest verdict: {evidence.verdict or '<empty>'}")
    for key in ("sample_size", "evaluated_bars", "setup_count"):
        value = getattr(evidence, key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"signal backtest {key} must be a non-negative integer")
    _require_ratio(evidence.win_rate, "signal backtest win_rate")
    _require_finite_number(evidence.avg_r, "signal backtest avg_r")
    for key in ("max_drawdown_pct", "profit_factor"):
        value = _require_finite_number(getattr(evidence, key), f"signal backtest {key}")
        if value < 0:
            raise ValueError(f"signal backtest {key} must be non-negative")


def _require_ratio(value: Any, label: str) -> float:
    number = _require_finite_number(value, label)
    if not 0 <= number <= 1:
        raise ValueError(f"{label} must be between 0 and 1")
    return number


def _require_finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return number
