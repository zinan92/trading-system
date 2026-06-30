from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from schemas.market_data import Bar


class YahooChartClient:
    def fetch_5m_bars(
        self,
        yahoo_symbol: str = "GC=F",
        output_symbol: str = "GOLD",
        range_value: str = "5d",
        interval: str = "5m",
    ) -> list[Bar]:
        encoded = urllib.parse.quote(yahoo_symbol, safe="")
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?range={range_value}&interval={interval}"
        request = urllib.request.Request(url, headers={"User-Agent": "TradingOrchestrator/1.0"})
        with urllib.request.urlopen(request, timeout=12) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return self.parse_chart_payload(payload, yahoo_symbol=yahoo_symbol, output_symbol=output_symbol, timeframe=interval)

    def parse_chart_payload(self, payload: dict, yahoo_symbol: str, output_symbol: str, timeframe: str = "5m") -> list[Bar]:
        result = (payload.get("chart", {}).get("result") or [{}])[0]
        timestamps = result.get("timestamp") or []
        quote = (result.get("indicators", {}).get("quote") or [{}])[0]
        bars: list[Bar] = []
        for index, raw_timestamp in enumerate(timestamps):
            close = self._value_at(quote, "close", index)
            if close is None:
                continue
            open_price = self._value_at(quote, "open", index, close)
            high = self._value_at(quote, "high", index, close)
            low = self._value_at(quote, "low", index, close)
            volume = self._value_at(quote, "volume", index, 0) or 0
            timestamp = datetime.fromtimestamp(int(raw_timestamp), timezone.utc).replace(microsecond=0).isoformat()
            bars.append(
                Bar(
                    symbol=output_symbol,
                    timeframe=timeframe,
                    timestamp=timestamp,
                    open=float(open_price),
                    high=float(high),
                    low=float(low),
                    close=float(close),
                    volume=float(volume),
                    provider=f"yahoo_chart:{yahoo_symbol}",
                    quality_flags=["historical_5m", "futures_proxy"],
                )
            )
        return bars

    def _value_at(self, quote: dict, key: str, index: int, fallback: float | None = None) -> float | None:
        values = quote.get(key) or []
        if index >= len(values) or values[index] is None:
            return fallback
        return float(values[index])
