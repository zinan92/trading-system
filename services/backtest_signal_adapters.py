"""Signal-evidence backtest adapters.

Each adapter performs one explicit job.  Fallback selection belongs to a
compatibility facade or a composition root, never inside an adapter.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Mapping
from typing import Any

from schemas.backtest import BacktestEvidence
from services.backtest_local_config import LocalBacktestConfig
from services.backtest_port import SignalBacktestRequest
from services.local_backtester import LocalBacktester


class LocalSignalBacktestAdapter:
    def __init__(self, context: Mapping[str, Any] | None = None) -> None:
        settings = dict(context or {})
        self._default_strategy_config = dict(settings.get("strategy_config") or {})

    def evaluate(self, request: SignalBacktestRequest) -> BacktestEvidence:
        strategy_config = (
            dict(request.backtest_config)
            if request.backtest_config
            else self._default_strategy_config
        )
        backtester = LocalBacktester(
            LocalBacktestConfig.from_strategy_config(strategy_config)
        )
        return backtester.evaluate(
            request.signal_object(),
            request.analysis_object(),
            request.bar_objects(),
        )


class RemoteSignalBacktestAdapter:
    def __init__(self, context: Mapping[str, Any]) -> None:
        self.base_url = str(context.get("base_url") or "").rstrip("/")
        if not self.base_url:
            raise ValueError("remote signal backtest base_url is required")
        self.timeout_seconds = float(context.get("timeout_seconds", 5.0))
        if self.timeout_seconds <= 0:
            raise ValueError("remote signal backtest timeout_seconds must be positive")

    def evaluate(self, request: SignalBacktestRequest) -> BacktestEvidence:
        signal = request.signal_object()
        request_payload = request.to_dict()
        payload = json.dumps(
            {
                "signal": request_payload["signal"],
                "analysis": request_payload["analysis"],
            }
        ).encode("utf-8")
        http_request = urllib.request.Request(
            f"{self.base_url}/api/evaluate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(http_request, timeout=self.timeout_seconds) as response:
            data = json.loads(response.read().decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("remote signal backtest response must be a JSON object")
        return BacktestEvidence(
            backtest_id=str(data.get("backtest_id") or ""),
            signal_id=str(data.get("signal_id") or signal.signal_id),
            asset=str(data.get("asset") or signal.asset),
            sample_size=int(data.get("sample_size", 0)),
            win_rate=float(data.get("win_rate", 0)),
            avg_r=float(data.get("avg_r", 0)),
            max_drawdown_pct=float(data.get("max_drawdown_pct", 0)),
            verdict=str(data.get("verdict") or ""),
            profit_factor=float(data.get("profit_factor", 0)),
            evaluated_bars=int(data.get("evaluated_bars", 0)),
            setup_count=int(data.get("setup_count", data.get("sample_size", 0))),
            skipped_reason=str(data.get("skipped_reason") or ""),
        )


class SyntheticSignalContextAdapter:
    """Legacy compatibility evidence; never historical or promotion eligible."""

    def __init__(self, _context: Mapping[str, Any] | None = None) -> None:
        pass

    def evaluate(self, request: SignalBacktestRequest) -> BacktestEvidence:
        signal = request.signal_object()
        sample_by_asset = {
            "crypto": 86,
            "commodity": 64,
            "us_stock": 142,
            "a_share": 118,
        }
        sample_size = sample_by_asset.get(signal.asset_class, 50)
        base_win_rate = 0.48 + min(signal.strength, 90) / 500
        avg_r = 0.25 + min(signal.confidence, 90) / 300
        max_drawdown = 8.0 if signal.asset_class == "crypto" else 5.5
        profit_factor = 1.0 + max(0, base_win_rate - 0.5) * 3 + max(0, avg_r - 0.35)
        verdict = (
            "supportive"
            if base_win_rate >= 0.58 and avg_r >= 0.45 and profit_factor >= 1.25
            else "thin"
        )
        return BacktestEvidence(
            backtest_id=f"backtest_{signal.signal_id.removeprefix('sig_')}",
            signal_id=signal.signal_id,
            asset=signal.asset,
            sample_size=sample_size,
            win_rate=round(base_win_rate, 3),
            avg_r=round(avg_r, 2),
            max_drawdown_pct=max_drawdown,
            profit_factor=round(profit_factor, 2),
            verdict=verdict,
            evaluated_bars=sample_size,
            setup_count=sample_size,
            skipped_reason="synthetic_context_not_historical_evidence",
            degraded=True,
        )
