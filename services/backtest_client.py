from __future__ import annotations

import json
import urllib.error
import urllib.request

from schemas.analysis import Analysis
from schemas.backtest import BacktestEvidence
from schemas.market_data import Bar
from schemas.signal import Signal
from services.local_backtester import LocalBacktestConfig, LocalBacktester


class BacktestClient:
    def __init__(self, base_url: str, fallback_to_mock: bool = True, local_enabled: bool = False, strategy_config: dict | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.fallback_to_mock = fallback_to_mock
        self.local_enabled = local_enabled
        self.local_config = LocalBacktestConfig.from_strategy_config(strategy_config)

    def evaluate(self, signal: Signal, analysis: Analysis, bars: list[Bar] | None = None) -> BacktestEvidence:
        if self.local_enabled and bars:
            return LocalBacktester(self.local_config).evaluate(signal, analysis, bars)
        try:
            return self._evaluate_remote(signal, analysis)
        except (OSError, urllib.error.URLError, TimeoutError, ValueError, KeyError):
            if not self.fallback_to_mock:
                raise
            return self._mock_evidence(signal, analysis)

    def _evaluate_remote(self, signal: Signal, analysis: Analysis) -> BacktestEvidence:
        payload = json.dumps({"signal": signal.to_dict(), "analysis": analysis.to_dict()}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/api/evaluate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
        return BacktestEvidence(
            backtest_id=str(data["backtest_id"]),
            signal_id=signal.signal_id,
            asset=signal.asset,
            sample_size=int(data.get("sample_size", 0)),
            win_rate=float(data.get("win_rate", 0)),
            avg_r=float(data.get("avg_r", 0)),
            max_drawdown_pct=float(data.get("max_drawdown_pct", 0)),
            profit_factor=float(data.get("profit_factor", 0)),
            verdict=str(data.get("verdict", "unknown")),
            evaluated_bars=int(data.get("evaluated_bars", 0)),
            setup_count=int(data.get("setup_count", data.get("sample_size", 0))),
            skipped_reason=str(data.get("skipped_reason", "")),
        )

    def _mock_evidence(self, signal: Signal, analysis: Analysis) -> BacktestEvidence:
        sample_by_asset = {
            "crypto": 86,
            "commodity": 64,
            "us_stock": 142,
            "a_share": 118,
        }
        base_win_rate = 0.48 + min(signal.strength, 90) / 500
        avg_r = 0.25 + min(signal.confidence, 90) / 300
        max_drawdown = 8.0 if signal.asset_class == "crypto" else 5.5
        profit_factor = 1.0 + max(0, base_win_rate - 0.5) * 3 + max(0, avg_r - 0.35)
        verdict = "supportive" if base_win_rate >= 0.58 and avg_r >= 0.45 and profit_factor >= 1.25 else "thin"
        return BacktestEvidence(
            backtest_id=f"backtest_{signal.signal_id.removeprefix('sig_')}",
            signal_id=signal.signal_id,
            asset=signal.asset,
            sample_size=sample_by_asset.get(signal.asset_class, 50),
            win_rate=round(base_win_rate, 3),
            avg_r=round(avg_r, 2),
            max_drawdown_pct=max_drawdown,
            profit_factor=round(profit_factor, 2),
            verdict=verdict,
            evaluated_bars=sample_by_asset.get(signal.asset_class, 50),
            setup_count=sample_by_asset.get(signal.asset_class, 50),
        )
