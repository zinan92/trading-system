"""Credential-free Hyperliquid Testnet market facts for attended previews."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any
from urllib.request import Request, urlopen


HYPERLIQUID_TESTNET_INFO_URL = "https://api.hyperliquid-testnet.xyz/info"
SUPPORTED_TESTNET_INSTRUMENTS = {"BTC-USD-PERP": "BTC"}
SUPPORTED_CANDLE_INTERVALS = {
    "1m": 60,
    "5m": 5 * 60,
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 60 * 60,
    "4h": 4 * 60 * 60,
    "1d": 24 * 60 * 60,
}


class HyperliquidTestnetMarketError(ValueError):
    """Stable, non-secret public-market read blocker."""


class HyperliquidTestnetMarketReader:
    """Read one source-bound BTC Testnet mid and top-of-book envelope."""

    def __init__(
        self,
        *,
        opener: Callable[..., Any] = urlopen,
        endpoint: str = HYPERLIQUID_TESTNET_INFO_URL,
        timeout_seconds: float = 5.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.opener = opener
        self.endpoint = str(endpoint)
        self.timeout_seconds = max(0.5, float(timeout_seconds))
        self.clock = clock

    def read(self, instrument_id: str = "BTC-USD-PERP") -> dict[str, Any]:
        instrument = str(instrument_id or "").strip()
        symbol = SUPPORTED_TESTNET_INSTRUMENTS.get(instrument)
        if symbol is None:
            raise HyperliquidTestnetMarketError("unsupported_testnet_instrument")
        mids = self._post({"type": "allMids"})
        book = self._post({"type": "l2Book", "coin": symbol})
        mid = self._positive_number(mids.get(symbol), "testnet_mid_missing")
        bids, asks = self._book_levels(book, symbol)
        bid = max(level["price"] for level in bids)
        ask = min(level["price"] for level in asks)
        if bid >= ask:
            raise HyperliquidTestnetMarketError("testnet_book_crossed")
        book_mid = (bid + ask) / 2
        if abs(book_mid - mid) > max(mid * 0.01, 1e-9):
            raise HyperliquidTestnetMarketError("testnet_mid_bbo_incoherent")
        observed_at = datetime.fromtimestamp(float(self.clock()), tz=timezone.utc).isoformat()
        return {
            "schema_version": "hyperliquid-testnet-market-v1",
            "provider": "hyperliquid",
            "source": "hyperliquid.external_testnet",
            "source_id": "hyperliquid.external_testnet",
            "environment": "testnet",
            "instrument_id": instrument,
            "symbol": symbol,
            "price": mid,
            "mid": mid,
            "bid": bid,
            "ask": ask,
            "spread": ask - bid,
            "bids": bids,
            "asks": asks,
            "trusted": True,
            "fresh": True,
            "raw_status": "ready",
            "observed_at": observed_at,
            "source_cursor": self._digest({"mids": mids, "book": book}),
        }

    def read_catalog(self) -> dict[str, Any]:
        """Read the public default-perp metadata without credentials.

        Metadata is an inventory fact, not an execution-ready market fact.  A
        caller must still read and validate market/account/protection facts for
        the selected Instrument before confirmation.
        """

        payload = self._post({"type": "meta"})
        universe = payload.get("universe")
        if not isinstance(universe, list):
            raise HyperliquidTestnetMarketError("testnet_catalog_universe_invalid")
        instruments: list[dict[str, Any]] = []
        for row in universe:
            if not isinstance(row, Mapping):
                raise HyperliquidTestnetMarketError("testnet_catalog_instrument_invalid")
            symbol = str(row.get("name") or "").strip()
            if not symbol:
                raise HyperliquidTestnetMarketError("testnet_catalog_symbol_missing")
            instruments.append(
                {
                    "instrument_id": f"{symbol}-USD-PERP",
                    "asset": symbol,
                    "asset_index": row.get("index"),
                    "size_decimals": row.get("szDecimals"),
                    "max_leverage": row.get("maxLeverage"),
                    "eligibility": "unknown",
                    "blockers": ["market_facts_pending"],
                    "catalog_source": "hyperliquid.external_testnet",
                }
            )
        return {
            "schema_version": "hyperliquid-testnet-instrument-catalog-v1",
            "provider": "hyperliquid",
            "source": "hyperliquid.external_testnet",
            "environment": "testnet",
            "instrument_scope": "default_perpetuals",
            "instruments": instruments,
            "catalog_revision": self._digest({"universe": universe}),
            "trusted": True,
            "fresh": True,
            "observed_at": datetime.fromtimestamp(
                float(self.clock()), tz=timezone.utc
            ).isoformat(),
            "source_cursor": self._digest(payload),
        }

    def read_bars(
        self,
        instrument_id: str = "BTC-USD-PERP",
        *,
        timeframe: str = "30m",
        limit: int = 240,
        end: str | None = None,
    ) -> dict[str, Any]:
        """Read credential-free Testnet candles in the Standard K-line shape."""

        instrument = str(instrument_id or "").strip()
        symbol = SUPPORTED_TESTNET_INSTRUMENTS.get(instrument)
        if symbol is None:
            raise HyperliquidTestnetMarketError("unsupported_testnet_instrument")
        interval = str(timeframe or "").strip()
        interval_seconds = SUPPORTED_CANDLE_INTERVALS.get(interval)
        if interval_seconds is None:
            raise HyperliquidTestnetMarketError("unsupported_testnet_candle_interval")
        requested_limit = int(limit)
        if requested_limit < 1 or requested_limit > 480:
            raise HyperliquidTestnetMarketError("testnet_candle_limit_invalid")
        end_ms = self._end_milliseconds(end)
        start_ms = end_ms - (interval_seconds * 1000 * (requested_limit + 1))
        rows = self._post_list(
            {
                "type": "candleSnapshot",
                "req": {
                    "coin": symbol,
                    "interval": interval,
                    "startTime": start_ms,
                    "endTime": end_ms,
                },
            }
        )
        parsed: dict[int, dict[str, Any]] = {}
        close_times: dict[int, int] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                raise HyperliquidTestnetMarketError("testnet_candle_payload_invalid")
            if str(row.get("s") or "") != symbol or str(row.get("i") or "") != interval:
                raise HyperliquidTestnetMarketError("testnet_candle_identity_mismatch")
            try:
                opened_ms = int(row.get("t"))
                closed_ms = int(row.get("T"))
            except (TypeError, ValueError) as exc:
                raise HyperliquidTestnetMarketError("testnet_candle_timestamp_invalid") from exc
            opened = self._positive_number(row.get("o"), "testnet_candle_ohlc_invalid")
            high = self._positive_number(row.get("h"), "testnet_candle_ohlc_invalid")
            low = self._positive_number(row.get("l"), "testnet_candle_ohlc_invalid")
            closed = self._positive_number(row.get("c"), "testnet_candle_ohlc_invalid")
            volume = self._non_negative_number(row.get("v"), "testnet_candle_volume_invalid")
            if opened_ms <= 0 or closed_ms < opened_ms or high < max(opened, closed) or low > min(opened, closed):
                raise HyperliquidTestnetMarketError("testnet_candle_payload_invalid")
            parsed[opened_ms] = {
                "symbol": symbol,
                "provider_symbol": symbol,
                "instrument_id": instrument,
                "timeframe": interval,
                "timestamp": self._iso_milliseconds(opened_ms),
                "open": opened,
                "high": high,
                "low": low,
                "close": closed,
                "volume": volume,
                "provider": "hyperliquid",
                "quality_flags": ["public_api", "testnet", "execution_venue"],
            }
            close_times[opened_ms] = closed_ms
        ordered_keys = sorted(parsed)[-requested_limit:]
        bars = [parsed[key] for key in ordered_keys]
        if not bars:
            raise HyperliquidTestnetMarketError("testnet_candles_missing")
        latest_key = ordered_keys[-1]
        age_seconds = max(0.0, float(self.clock()) - close_times[latest_key] / 1000.0)
        fresh = age_seconds <= max(120.0, interval_seconds * 2.0)
        historical_page = end is not None and str(end).strip() != ""
        next_end_ms = ordered_keys[0] - 1
        return {
            "schema_version": "dashboard-control-market-bars-v1",
            "status": "ready",
            "source_mode": "hyperliquid.external_testnet",
            "source": "hyperliquid.external_testnet",
            "venue_profile_id": "hyperliquid.testnet",
            "instrument_id": instrument,
            "environment": "testnet",
            "symbol": symbol,
            "provider_symbol": symbol,
            "timeframe": interval,
            "provider": "hyperliquid",
            "quality_flags": ["public_api", "testnet", "execution_venue"],
            "is_synthetic": False,
            "requested": {
                "instrument_id": instrument,
                "timeframe": interval,
                "limit": requested_limit,
            },
            "bar_count": len(bars),
            "latest_timestamp": bars[-1]["timestamp"],
            "latest_close": bars[-1]["close"],
            "fresh": fresh,
            "trusted": fresh,
            "historical_page": historical_page,
            "trusted_history": True,
            "age_minutes": round(age_seconds / 60.0, 3),
            "bars": bars,
            "access_issues": [] if fresh else ["testnet_candles_stale"],
            "pagination": {
                "has_more": len(rows) >= requested_limit,
                "next_end": self._iso_milliseconds(next_end_ms),
            },
            "source_cursor": self._digest({"candles": rows}),
            "safety": {
                "read_only": True,
                "uses_credentials": False,
                "submits_orders": False,
            },
        }

    def _request(self, payload: Mapping[str, Any]) -> Any:
        request = Request(
            self.endpoint,
            data=json.dumps(dict(payload), separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                value = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - public read failures are typed.
            raise HyperliquidTestnetMarketError("testnet_market_unavailable") from exc
        return value

    def _post(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        value = self._request(payload)
        if not isinstance(value, Mapping):
            raise HyperliquidTestnetMarketError("testnet_market_payload_invalid")
        return value

    def _post_list(self, payload: Mapping[str, Any]) -> list[Any]:
        value = self._request(payload)
        if not isinstance(value, list):
            raise HyperliquidTestnetMarketError("testnet_market_payload_invalid")
        return value

    def _end_milliseconds(self, value: str | None) -> int:
        if value is None or not str(value).strip():
            return int(float(self.clock()) * 1000)
        text = str(value).strip()
        try:
            numeric = float(text)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError as exc:
                raise HyperliquidTestnetMarketError("testnet_candle_end_invalid") from exc
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.astimezone(timezone.utc).timestamp() * 1000)
        return int(numeric if numeric > 10_000_000_000 else numeric * 1000)

    @staticmethod
    def _iso_milliseconds(value: int) -> str:
        return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc).isoformat()

    @classmethod
    def _book_levels(
        cls,
        book: Mapping[str, Any],
        symbol: str,
    ) -> tuple[list[dict[str, float]], list[dict[str, float]]]:
        if str(book.get("coin") or "") != symbol:
            raise HyperliquidTestnetMarketError("testnet_book_symbol_mismatch")
        levels = book.get("levels")
        if not isinstance(levels, list) or len(levels) != 2:
            raise HyperliquidTestnetMarketError("testnet_book_levels_invalid")

        def parse(side: Any, code: str) -> list[dict[str, float]]:
            if not isinstance(side, list) or not side:
                raise HyperliquidTestnetMarketError(code)
            parsed: list[dict[str, float]] = []
            for row in side:
                if not isinstance(row, Mapping):
                    raise HyperliquidTestnetMarketError(code)
                parsed.append(
                    {
                        "price": cls._positive_number(row.get("px"), code),
                        "size": cls._positive_number(row.get("sz"), code),
                    }
                )
            return parsed

        return parse(levels[0], "testnet_bids_invalid"), parse(levels[1], "testnet_asks_invalid")

    @staticmethod
    def _positive_number(value: Any, code: str) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise HyperliquidTestnetMarketError(code) from exc
        if number <= 0:
            raise HyperliquidTestnetMarketError(code)
        return number

    @staticmethod
    def _non_negative_number(value: Any, code: str) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise HyperliquidTestnetMarketError(code) from exc
        if number < 0:
            raise HyperliquidTestnetMarketError(code)
        return number

    @staticmethod
    def _digest(value: Mapping[str, Any]) -> str:
        encoded = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "HYPERLIQUID_TESTNET_INFO_URL",
    "HyperliquidTestnetMarketError",
    "HyperliquidTestnetMarketReader",
    "SUPPORTED_CANDLE_INTERVALS",
    "SUPPORTED_TESTNET_INSTRUMENTS",
]
