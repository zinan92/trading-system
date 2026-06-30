from __future__ import annotations

import json
import urllib.error
import urllib.request

from schemas.analysis import Analysis
from schemas.signal import Signal


class CopilotClient:
    def __init__(self, base_url: str, fallback_to_mock: bool = True) -> None:
        self.base_url = base_url.rstrip("/")
        self.fallback_to_mock = fallback_to_mock

    def analyze(self, signal: Signal) -> Analysis:
        try:
            return self._analyze_remote(signal)
        except (OSError, urllib.error.URLError, TimeoutError, ValueError, KeyError):
            if not self.fallback_to_mock:
                raise
            return self._mock_analysis(signal)

    def _analyze_remote(self, signal: Signal) -> Analysis:
        payload = json.dumps({"signal": signal.to_dict()}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/api/analyze",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
        return Analysis(
            analysis_id=str(data["analysis_id"]),
            signal_id=signal.signal_id,
            asset=signal.asset,
            methods=list(data.get("methods", [])),
            thesis=str(data.get("thesis", "")),
            counter_thesis=str(data.get("counter_thesis", "")),
            checklist=list(data.get("checklist", [])),
            confidence_adjustment=int(data.get("confidence_adjustment", 0)),
        )

    def _mock_analysis(self, signal: Signal) -> Analysis:
        if signal.regime == "trend_following":
            methods = ["trend-following", "macro-liquidity", "risk-management"]
        elif signal.regime == "pullback_long":
            methods = ["pullback-long", "scenario-analyzer", "risk-management"]
        elif signal.regime == "event_risk_reduction":
            methods = ["event-risk-reduction", "scenario-analyzer", "risk-management"]
        else:
            methods = ["no-trade", "risk-management"]
        return Analysis(
            analysis_id=f"analysis_{signal.signal_id.removeprefix('sig_')}",
            signal_id=signal.signal_id,
            asset=signal.asset,
            methods=methods,
            thesis=f"{signal.asset} is best interpreted through {', '.join(methods[:2])}; current regime is {signal.regime}.",
            counter_thesis="Signal should be ignored if price confirmation fails, macro pressure reverses, or event risk dominates.",
            checklist=[
                "Confirm signal is still fresh before execution.",
                "Confirm entry is inside the planned zone.",
                "Do not execute if stop distance exceeds configured risk.",
                "Keep paper-only mode enabled until the local journal has enough reviewed trades.",
            ],
            confidence_adjustment=3 if signal.strength >= 70 else 0,
        )
