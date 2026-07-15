from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from csv import DictReader
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path

from schemas.asset import Asset
from services.config_loader import load_pipeline_config
from services.market_data_access import market_data_repository, uses_independent_datafeed
from schemas.market_data import Bar
from services.yahoo_chart_client import YahooChartClient


Candle = Bar


class KlineClient:
    def __init__(
        self,
        base_url: str,
        fallback_to_mock: bool = True,
        local_db_path: Path | None = None,
        allow_synthetic_seed: bool = False,
        allow_public_snapshot_bar_for_paper: bool = False,
        gold_backfill: dict | None = None,
        official_gold_5m_providers: list[str] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.fallback_to_mock = fallback_to_mock
        self.store = market_data_repository(local_db_path) if local_db_path else None
        self.independent_datafeed = uses_independent_datafeed(local_db_path)
        self.allow_synthetic_seed = allow_synthetic_seed
        self.allow_public_snapshot_bar_for_paper = allow_public_snapshot_bar_for_paper
        self.gold_backfill = gold_backfill or {}
        if official_gold_5m_providers is None:
            source_config = load_pipeline_config().get("market_data_sources", {}).get("gold_5m", {})
            official_gold_5m_providers = source_config.get("official_broker_providers", ["broker_csv", "mt5_csv", "ibkr", "oanda"])
        self.official_gold_5m_providers = set(official_gold_5m_providers)

    def fetch(self, asset: Asset, timeframe: str = "1d", limit: int = 30) -> list[Candle]:
        try:
            if self.independent_datafeed and self.store:
                return self.store.load_bars(asset.symbol, timeframe, limit)
            if asset.symbol == "GOLD" and timeframe == "5m" and self.store:
                return self._fetch_local_gold_5m(asset, limit)
            # Non-5m GOLD (e.g. the 1m series the chan strategy consumes) is a
            # plain read of real bars from the local store — backfilled by the
            # binance 1m feed. The gold-api snapshot / yahoo-backfill machinery is
            # 5m-only, so it must NOT run here (it would inject degenerate bars or
            # fall through to mock candles via _fetch_remote).
            if asset.symbol == "GOLD" and timeframe != "5m" and self.store:
                return self.store.load_bars(asset.symbol, timeframe, limit)
            if self.base_url == "local_gold_api" and self.store and asset.symbol != "GOLD":
                return self._fetch_local_factor(asset, timeframe, limit)
            return self._fetch_remote(asset, timeframe, limit)
        except (OSError, urllib.error.URLError, TimeoutError, ValueError, KeyError):
            if not self.fallback_to_mock:
                raise
            return self._mock_candles(asset, timeframe=timeframe)

    def fetch_timeframes(self, asset: Asset, timeframes: list[str], limit: int = 60) -> dict[str, list[Candle]]:
        return {timeframe: self.fetch(asset, timeframe=timeframe, limit=limit) for timeframe in timeframes}

    def _fetch_remote(self, asset: Asset, timeframe: str, limit: int) -> list[Candle]:
        if self.base_url == "local_gold_api":
            return self._factor_proxy_bars(asset, timeframe, limit)
        params = urllib.parse.urlencode({"timeframe": timeframe, "limit": limit})
        url = f"{self.base_url}/api/candles/{asset.asset_class}/{asset.symbol}?{params}"
        with urllib.request.urlopen(url, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return [
            Bar(
                symbol=asset.symbol,
                timeframe=timeframe,
                timestamp=str(item["timestamp"]),
                open=float(item["open"]),
                high=float(item["high"]),
                low=float(item["low"]),
                close=float(item["close"]),
                volume=float(item["volume"]),
                provider=str(item.get("provider", "kline_remote")),
                quality_flags=list(item.get("quality_flags", [])),
            )
            for item in payload["candles"]
        ]

    def _mock_candles(self, asset: Asset, timeframe: str = "1d") -> list[Candle]:
        base_by_asset = {
            "GOLD": 2350.0,
            "DXY": 104.0,
            "US10Y_REAL": 2.0,
            "GLD_FLOW": 0.0,
            "FED_CPI_EVENTS": 50.0,
        }
        base = base_by_asset.get(asset.symbol, 100.0)
        drift_by_symbol = {
            "GOLD": 1.006,
            "DXY": 0.999,
            "US10Y_REAL": 0.997,
            "GLD_FLOW": 1.004,
            "FED_CPI_EVENTS": 1.0,
        }
        drift = drift_by_symbol.get(asset.symbol, 1.001)
        candles: list[Candle] = []
        price = base
        for day in range(1, 31):
            open_price = price
            close = round(open_price * drift, 2)
            high = round(max(open_price, close) * 1.01, 2)
            low = round(min(open_price, close) * 0.99, 2)
            volume = 1_000_000 + day * 12_345
            candles.append(
                Bar(
                    symbol=asset.symbol,
                    timeframe=timeframe,
                    timestamp=f"mock-{timeframe}-{day:02d}",
                    open=round(open_price, 2),
                    high=high,
                    low=low,
                    close=close,
                    volume=volume,
                    provider="mock_kline",
                    quality_flags=["mock"],
                )
            )
            price = close
        return candles

    def _factor_proxy_bars(self, asset: Asset, timeframe: str, limit: int) -> list[Candle]:
        bars = self._mock_candles(asset, timeframe=timeframe)
        return [
            Bar(
                symbol=bar.symbol,
                timeframe=bar.timeframe,
                timestamp=bar.timestamp,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                provider="local_factor_proxy",
                quality_flags=["factor_proxy"],
            )
            for bar in bars[-limit:]
        ]

    def _fetch_local_gold_5m(self, asset: Asset, limit: int) -> list[Candle]:
        assert self.store is not None
        self._maybe_backfill_gold_5m(asset)
        existing = self._bar_candidates(self.store.load_bars(asset.symbol, "5m", limit))
        if self._latest_is_official_gold_5m(existing):
            return existing
        try:
            current = self._fetch_gold_api_snapshot(asset)
        except (OSError, urllib.error.URLError, TimeoutError, ValueError, KeyError):
            # Live snapshot unreachable (offline, blocked, or API down).
            # If we already have enough local history, return it so the
            # collector / signal pipeline can still run on cached data.
            # data_source_preflight + the collector's fetch_status field
            # will mark the result as stale.
            if len(existing) >= 20:
                return existing
            raise
        # Gold-api / env spot prices are point quotes (V=0, H=L=O=C), not real
        # 5m OHLC bars. Persist the live price in the quotes table only — writing
        # it into the bars table produced degenerate "candles" that perpetuated
        # as the latest row and injected chart/audit zigzag. Real OHLC comes from
        # the binance_usdm proxy feed / yahoo backfill. The legacy
        # allow_public_snapshot_bar_for_paper flag no longer writes snapshot bars.
        self.store.upsert_quote(current)
        if len(existing) >= 20:
            return existing
        # Cold start only: not enough real bars to generate a signal yet.
        # Optionally bootstrap a synthetic seed anchored to the live price so the
        # paper pipeline can start; otherwise surface the shortage explicitly.
        if self.allow_synthetic_seed:
            seed = self._synthetic_seed(asset, current, count=max(0, limit - len(existing)))
            if seed:
                self.store.upsert_bars(seed)
                existing = self._bar_candidates(self.store.load_bars(asset.symbol, "5m", limit))
        if len(existing) < 20:
            raise ValueError("not enough local GOLD 5m bars to generate a trading signal")
        return existing

    def _bar_candidates(self, bars: list[Candle]) -> list[Candle]:
        stable = [
            bar
            for bar in bars
            if not (
                bar.provider in {"gold-api.com", "env_gold_price"}
                and ("live_snapshot" in bar.quality_flags or "test_price" in bar.quality_flags)
                and "quote_derived_bar" not in bar.quality_flags
            )
        ]
        return stable or bars

    def _latest_is_official_gold_5m(self, bars: list[Candle]) -> bool:
        if len(bars) < 20:
            return False
        latest = bars[-1]
        return latest.provider in self.official_gold_5m_providers

    def _maybe_backfill_gold_5m(self, asset: Asset) -> None:
        if not self.gold_backfill.get("enabled"):
            return
        if os.getenv("TRADING_ORCHESTRATOR_GOLD_PRICE"):
            return
        coverage = self.store.coverage() if self.store else []
        non_seed_rows = sum(
            item["rows"]
            for item in coverage
            if item["symbol"] == asset.symbol and item["timeframe"] == "5m" and item["provider"] != "local_synthetic_seed"
        )
        if non_seed_rows >= int(self.gold_backfill.get("min_non_seed_rows", 240)):
            return
        yahoo_symbol = self.gold_backfill.get("yahoo_symbol", "GC=F")
        range_value = self.gold_backfill.get("range", "5d")
        try:
            bars = YahooChartClient().fetch_5m_bars(
                yahoo_symbol=yahoo_symbol,
                output_symbol=asset.symbol,
                range_value=range_value,
                interval="5m",
            )
        except (OSError, urllib.error.URLError, TimeoutError, ValueError, KeyError, json.JSONDecodeError):
            return
        if bars:
            self.store.upsert_bars(bars)

    def _fetch_local_factor(self, asset: Asset, timeframe: str, limit: int) -> list[Candle]:
        assert self.store is not None
        if timeframe != "1d":
            raise ValueError(f"factor asset {asset.symbol} only supports 1d timeframe in local mode")
        try:
            bars = self._fetch_fred_factor(asset, limit)
        except (OSError, urllib.error.URLError, TimeoutError, ValueError, KeyError):
            cached = self.store.load_bars(asset.symbol, "1d", limit)
            if cached:
                return cached
            raise
        self.store.upsert_bars(bars)
        return self.store.load_bars(asset.symbol, "1d", limit)

    def _fetch_fred_factor(self, asset: Asset, limit: int) -> list[Candle]:
        series_map = {
            "DXY": ("DTWEXBGS", "fred:DTWEXBGS", ["dxy_proxy_broad_dollar_index", "daily_factor"]),
            "US10Y_REAL": ("DFII10", "fred:DFII10", ["real_yield", "daily_factor"]),
            "GLD_FLOW": ("GVZCLS", "fred:GVZCLS", ["gld_flow_proxy_gold_volatility", "daily_factor"]),
            "FED_CPI_EVENTS": ("DFF", "fred:DFF", ["fed_policy_proxy", "daily_factor"]),
        }
        if asset.symbol not in series_map:
            raise ValueError(f"no local factor provider configured for {asset.symbol}")
        series_id, provider, flags = series_map[asset.symbol]
        if os.getenv("TRADING_ORCHESTRATOR_OFFLINE_FACTOR_FIXTURES"):
            return self._offline_factor_fixture(asset, limit, provider, flags)
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
        with urllib.request.urlopen(url, timeout=10) as response:
            text = response.read().decode("utf-8")
        rows = []
        for row in DictReader(StringIO(text)):
            raw_value = str(row.get(series_id, "")).strip()
            if not raw_value or raw_value == ".":
                continue
            value = float(raw_value)
            rows.append(
                Bar(
                    symbol=asset.symbol,
                    timeframe="1d",
                    timestamp=f"{row['observation_date']}T00:00:00+00:00",
                    open=value,
                    high=value,
                    low=value,
                    close=value,
                    volume=0,
                    provider=provider,
                    quality_flags=flags,
                )
            )
        if not rows:
            raise ValueError(f"FRED returned no usable rows for {asset.symbol}/{series_id}")
        return rows[-limit:]

    def _offline_factor_fixture(self, asset: Asset, limit: int, provider: str, flags: list[str]) -> list[Candle]:
        base_by_symbol = {
            "DXY": 119.0,
            "US10Y_REAL": 2.2,
            "GLD_FLOW": 25.0,
            "FED_CPI_EVENTS": 3.6,
        }
        base = base_by_symbol.get(asset.symbol, 100.0)
        bars = []
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for index in range(limit):
            value = round(base + index * 0.01, 4)
            bars.append(
                Bar(
                    symbol=asset.symbol,
                    timeframe="1d",
                    timestamp=(start + timedelta(days=index)).date().isoformat() + "T00:00:00+00:00",
                    open=value,
                    high=value,
                    low=value,
                    close=value,
                    volume=0,
                    provider=provider,
                    quality_flags=flags + ["offline_fixture"],
                )
            )
        return bars

    def _fetch_gold_api_snapshot(self, asset: Asset) -> Bar:
        env_price = os.getenv("TRADING_ORCHESTRATOR_GOLD_PRICE")
        if env_price:
            price = float(env_price)
            timestamp = os.getenv("TRADING_ORCHESTRATOR_GOLD_TIMESTAMP") or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            return Bar(
                symbol=asset.symbol,
                timeframe="5m",
                timestamp=timestamp,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=0,
                provider="env_gold_price",
                quality_flags=["test_price"],
            )
        request = urllib.request.Request(
            "https://api.gold-api.com/price/XAU",
            headers={"User-Agent": "TradingOrchestrator/1.0"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        price = float(payload["price"])
        timestamp = str(payload.get("updatedAt") or datetime.now(timezone.utc).isoformat())
        return Bar(
            symbol=asset.symbol,
            timeframe="5m",
            timestamp=timestamp,
            open=price,
            high=price,
            low=price,
            close=price,
            volume=0,
            provider="gold-api.com",
            quality_flags=["live_snapshot"],
        )

    def _synthetic_seed(self, asset: Asset, current: Bar, count: int) -> list[Bar]:
        bars: list[Bar] = []
        anchor = datetime.fromisoformat(current.timestamp.replace("Z", "+00:00"))
        price = current.close
        for index in range(count, 0, -1):
            ts = anchor - timedelta(minutes=5 * index)
            wave = ((index % 9) - 4) * 0.00035
            close = round(price * (1 - 0.0009 * index + wave), 4)
            open_price = round(close * (1 - wave / 2), 4)
            high = round(max(open_price, close) * 1.0006, 4)
            low = round(min(open_price, close) * 0.9994, 4)
            bars.append(
                Bar(
                    symbol=asset.symbol,
                    timeframe="5m",
                    timestamp=ts.replace(microsecond=0).isoformat(),
                    open=open_price,
                    high=high,
                    low=low,
                    close=close,
                    volume=0,
                    provider="local_synthetic_seed",
                    quality_flags=["synthetic_seed", "real_price_anchor"],
                )
            )
        return bars
