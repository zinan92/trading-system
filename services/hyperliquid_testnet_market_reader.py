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

    def _post(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
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
        if not isinstance(value, Mapping):
            raise HyperliquidTestnetMarketError("testnet_market_payload_invalid")
        return value

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
    def _digest(value: Mapping[str, Any]) -> str:
        encoded = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "HYPERLIQUID_TESTNET_INFO_URL",
    "HyperliquidTestnetMarketError",
    "HyperliquidTestnetMarketReader",
    "SUPPORTED_TESTNET_INSTRUMENTS",
]
