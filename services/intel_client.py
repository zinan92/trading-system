from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from schemas.asset import Asset
from schemas.market_data import MarketEvent


IntelEvent = MarketEvent


class IntelClient:
    def __init__(self, base_url: str, fallback_to_mock: bool = True) -> None:
        self.base_url = base_url.rstrip("/")
        self.fallback_to_mock = fallback_to_mock

    def fetch(self, asset: Asset) -> list[IntelEvent]:
        try:
            return self._fetch_remote(asset)
        except (OSError, urllib.error.URLError, TimeoutError, ValueError, KeyError):
            if not self.fallback_to_mock:
                raise
            return self._mock_events(asset)

    def _fetch_remote(self, asset: Asset) -> list[IntelEvent]:
        params = urllib.parse.urlencode({"symbol": asset.symbol, "asset_class": asset.asset_class})
        url = f"{self.base_url}/api/intel?{params}"
        with urllib.request.urlopen(url, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return [
            MarketEvent(
                event_id=str(item["event_id"]),
                symbol_scope=[asset.symbol],
                title=str(item["title"]),
                source=str(item.get("source", "intel")),
                timestamp=str(item.get("timestamp", "")),
                sentiment=str(item.get("sentiment", "neutral")),
                impact_score=int(item.get("impact_score", item.get("score", 50))),
                evidence_url=str(item.get("evidence_url", "")),
            )
            for item in payload.get("events", [])
        ]

    def _mock_events(self, asset: Asset) -> list[IntelEvent]:
        if asset.symbol == "GOLD":
            title = "Macro uncertainty keeps defensive demand elevated"
            score = 68
        elif asset.symbol == "DXY":
            title = "Dollar index is easing, reducing pressure on gold"
            score = 62
        elif asset.symbol == "US10Y_REAL":
            title = "Real yields are softening from recent highs"
            score = 64
        elif asset.symbol == "GLD_FLOW":
            title = "Gold ETF flow is mildly positive"
            score = 58
        else:
            title = "Fed/CPI event window requires reduced size"
            score = 45
        sentiment = "positive" if score >= 60 else "neutral"
        return [
            MarketEvent(
                event_id=f"mock_{asset.symbol.lower()}_intel",
                symbol_scope=[asset.symbol],
                title=title,
                source="mock_intel",
                timestamp=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                sentiment=sentiment,
                impact_score=score,
                evidence_url="mock://intel",
            )
        ]
