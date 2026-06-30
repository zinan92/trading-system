from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from schemas.market_data import Bar
from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import write_json
from services.market_store import MarketStore

# Transient network/TLS failures worth retrying. The long 1m history backfill
# makes hundreds of back-to-back calls and occasionally hits a dropped TLS
# connection ("SSL: UNEXPECTED_EOF_WHILE_READING", surfaced as URLError) or a
# brief rate-limit reset — none of which mean the data is unavailable.
_TRANSIENT_ERRORS = (urllib.error.URLError, TimeoutError, OSError)


class BinanceFuturesFeedClient:
    def __init__(
        self,
        store: MarketStore,
        config: dict | None = None,
        opener=None,
        sleeper=None,
    ) -> None:
        self.store = store
        self.config = config or load_pipeline_config().get("binance_usdm_feed", {})
        self.opener = opener or urllib.request.urlopen
        self.sleeper = sleeper or time.sleep
        self.max_retries = int(self.config.get("max_retries", 4))
        self.retry_pause_seconds = float(self.config.get("retry_pause_seconds", 1.0))

    def preflight(self) -> dict:
        symbol_status = self._symbol_status()
        ready = symbol_status.get("status") == "TRADING"
        return {
            "provider": "binance_usdm",
            "ready": ready,
            "status": "pass" if ready else "warn",
            "base_url": self._base_url(),
            "symbol": self.symbol,
            "interval": self.interval,
            "output_symbol": self.output_symbol,
            "timeframe": self.timeframe,
            "symbol_status": symbol_status,
            "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        }

    def fetch_and_store(self, limit: int | None = None) -> dict:
        preflight = self.preflight()
        if not preflight["ready"]:
            return {
                **preflight,
                "status": "skipped",
                "message": f"Binance USDM symbol {self.symbol} is not trading or exchangeInfo is unavailable",
                "imported_rows": 0,
                "coverage": self._coverage(),
            }
        try:
            bars = self.fetch(limit=limit)
        except (urllib.error.URLError, TimeoutError, OSError, RuntimeError, json.JSONDecodeError, KeyError, ValueError) as exc:
            return {
                **preflight,
                "status": "fail",
                "message": f"Binance USDM fetch failed: {exc}",
                "imported_rows": 0,
                "latest_timestamp": "",
                "latest_price": None,
                "coverage": self._coverage(),
            }
        self.store.upsert_bars(bars)
        if bars:
            self.store.upsert_quote(bars[-1])
        return {
            **preflight,
            "status": "pass" if bars else "warn",
            "message": "imported Binance USDM klines" if bars else "Binance returned no klines",
            "imported_rows": len(bars),
            "latest_timestamp": bars[-1].timestamp if bars else "",
            "latest_price": bars[-1].close if bars else None,
            "coverage": self._coverage(),
        }

    def fetch(self, limit: int | None = None) -> list[Bar]:
        params = {
            "symbol": self.symbol,
            "interval": self.interval,
            "limit": str(limit or int(self.config.get("limit", 500))),
        }
        return self._fetch_klines(params)

    def fetch_range(self, start_ms: int, end_ms: int, page_limit: int = 1500, pause_seconds: float = 0.0) -> list[Bar]:
        """Fetch every kline in [start_ms, end_ms] by paginating Binance's
        time-windowed klines endpoint (max ~1500 rows per call).

        Binance XAUUSDT is a 24/7 perpetual, so this fills calendar gaps the
        recent-only ``fetch`` cannot reach (e.g. weekend windows that the
        COMEX/yahoo feed leaves empty). Rows are deduped by timestamp and
        returned in chronological order.
        """
        if end_ms < start_ms:
            return []
        step = self._interval_ms()
        seen: dict[str, Bar] = {}
        cursor = start_ms
        while cursor <= end_ms:
            params = {
                "symbol": self.symbol,
                "interval": self.interval,
                "startTime": str(cursor),
                "endTime": str(end_ms),
                "limit": str(page_limit),
            }
            payload_bars = self._fetch_klines(params, raw=True)
            if not payload_bars:
                break
            for row in payload_bars:
                bar = self._parse_bar(row)
                seen[bar.timestamp] = bar
            last_open = int(payload_bars[-1][0])
            cursor = last_open + step
            if len(payload_bars) < page_limit:
                break
            if pause_seconds:  # be polite across many pages so we don't trip rate limits
                self.sleeper(pause_seconds)
        return [seen[key] for key in sorted(seen)]

    def backfill_and_store(self, start_ms: int, end_ms: int, replace_providers: list[str] | None = None) -> dict:
        """Backfill a time window and persist it, optionally deleting other
        providers' rows for the same symbol/timeframe first so the result is a
        single coherent instrument series."""
        deleted = 0
        for provider in replace_providers or []:
            deleted += self.store.delete_bars(self.output_symbol, self.timeframe, provider)
        bars = self.fetch_range(start_ms, end_ms)
        if bars:
            self.store.upsert_bars(bars)
        return {
            "provider": "binance_usdm",
            "symbol": self.symbol,
            "output_symbol": self.output_symbol,
            "timeframe": self.timeframe,
            "backfilled_rows": len(bars),
            "deleted_rows": deleted,
            "first_timestamp": bars[0].timestamp if bars else "",
            "last_timestamp": bars[-1].timestamp if bars else "",
            "coverage": self._coverage(),
        }

    def _fetch_klines(self, params: dict, raw: bool = False):
        url = f"{self._base_url()}/fapi/v1/klines?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers={"User-Agent": "TradingOrchestrator/1.0"})
        timeout = int(self.config.get("timeout_seconds", 10))
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                with self.opener(request, timeout=timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                return payload if raw else [self._parse_bar(row) for row in payload]
            except _TRANSIENT_ERRORS as exc:  # transient drop / rate-limit reset — back off and retry
                last_error = exc
                if attempt < self.max_retries:
                    self.sleeper(self.retry_pause_seconds * (attempt + 1))
        raise last_error  # exhausted retries — let the caller decide

    def _interval_ms(self) -> int:
        unit = self.interval[-1]
        try:
            value = int(self.interval[:-1])
        except ValueError:
            return 300_000
        return value * {"m": 60_000, "h": 3_600_000, "d": 86_400_000}.get(unit, 60_000)

    @property
    def symbol(self) -> str:
        return str(self.config.get("symbol", "XAUUSDT")).upper()

    @property
    def interval(self) -> str:
        return str(self.config.get("interval", "5m"))

    @property
    def output_symbol(self) -> str:
        return str(self.config.get("output_symbol", "GOLD"))

    @property
    def timeframe(self) -> str:
        return str(self.config.get("timeframe", "5m"))

    def _parse_bar(self, row: list) -> Bar:
        return Bar(
            symbol=self.output_symbol,
            timeframe=self.timeframe,
            timestamp=self._normalize_ms(int(row[0])),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
            provider="binance_usdm",
            quality_flags=[
                # Binance USDM XAUUSDT is a crypto-exchange perpetual that
                # tracks gold price; it is NOT an official broker XAU/USD
                # feed. Keep it in the public/proxy tier so kline_client still
                # fetches gold-api.com live snapshots and data_source_preflight
                # does not mark the system "live ready" on this alone.
                "public_proxy_feed",
                "exchange_futures",
                "crypto_perpetual",
                self.symbol.lower(),
            ],
        )

    def _symbol_status(self) -> dict:
        try:
            params = urllib.parse.urlencode({"symbol": self.symbol})
            request = urllib.request.Request(
                f"{self._base_url()}/fapi/v1/exchangeInfo?{params}",
                headers={"User-Agent": "TradingOrchestrator/1.0"},
            )
            with self.opener(request, timeout=int(self.config.get("timeout_seconds", 10))) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, TimeoutError, json.JSONDecodeError, KeyError, ValueError):
            return {"symbol": self.symbol, "status": "UNKNOWN"}
        symbols = payload.get("symbols") or []
        item = next((row for row in symbols if row.get("symbol") == self.symbol), symbols[0] if symbols else {})
        return {
            "symbol": item.get("symbol", self.symbol),
            "status": item.get("status", "UNKNOWN"),
            "contract_type": item.get("contractType", ""),
            "underlying_type": item.get("underlyingType", ""),
            "margin_asset": item.get("marginAsset", ""),
        }

    def _normalize_ms(self, value: int) -> str:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).replace(microsecond=0).isoformat()

    def _base_url(self) -> str:
        if self.config.get("base_url"):
            return str(self.config["base_url"]).rstrip("/")
        environment = str(self.config.get("environment", "live")).lower()
        return "https://testnet.binancefuture.com" if environment == "testnet" else "https://fapi.binance.com"

    def _coverage(self) -> list[dict]:
        return [
            item
            for item in self.store.coverage()
            if item["symbol"] == self.output_symbol and item["timeframe"] == self.timeframe
        ]


def run_binance_usdm_feed_import(run_date: str, output_root: Path | None = None, market_db: Path | None = None, opener=None) -> dict:
    config = load_pipeline_config()
    output_root = output_root or Path(os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(ROOT / config.get("output_root", "outputs"))))
    market_db = market_db or Path(os.getenv("TRADING_ORCHESTRATOR_MARKET_DB", str(ROOT / config.get("local_market_db", "data/market_data.db"))))
    result = BinanceFuturesFeedClient(MarketStore(market_db), config.get("binance_usdm_feed", {}), opener=opener).fetch_and_store()
    result["run_date"] = run_date
    result["market_db"] = str(market_db)
    write_json(output_root / "binance_usdm_feed" / "current.json", [result])
    write_json(output_root / "binance_usdm_feed" / f"{run_date}.json", [result])
    return result


def run_binance_usdm_1m_feed_import(run_date: str, output_root: Path | None = None, market_db: Path | None = None, opener=None) -> dict:
    """Import the most recent Binance XAUUSDT 1m klines into the local market DB
    as a (GOLD, 1m) series. Separate from the 5m feed (own config block) so the
    1m chan strategy gets a live-refreshed series without touching 5m."""
    config = load_pipeline_config()
    output_root = output_root or Path(os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(ROOT / config.get("output_root", "outputs"))))
    market_db = market_db or Path(os.getenv("TRADING_ORCHESTRATOR_MARKET_DB", str(ROOT / config.get("local_market_db", "data/market_data.db"))))
    result = BinanceFuturesFeedClient(MarketStore(market_db), config.get("binance_usdm_1m_feed", {}), opener=opener).fetch_and_store()
    result["run_date"] = run_date
    result["market_db"] = str(market_db)
    write_json(output_root / "binance_usdm_1m_feed" / "current.json", [result])
    write_json(output_root / "binance_usdm_1m_feed" / f"{run_date}.json", [result])
    return result


def _iso_to_ms(value: str) -> int:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def run_binance_usdm_backfill(
    start: str,
    end: str,
    replace_providers: list[str] | None = None,
    output_root: Path | None = None,
    market_db: Path | None = None,
    opener=None,
) -> dict:
    """Backfill Binance XAUUSDT klines for an ISO datetime window into the
    local market DB, replacing the listed providers' rows so GOLD 5m becomes a
    single coherent XAUUSDT series (gaps — e.g. weekends — included)."""
    config = load_pipeline_config()
    output_root = output_root or Path(os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(ROOT / config.get("output_root", "outputs"))))
    market_db = market_db or Path(os.getenv("TRADING_ORCHESTRATOR_MARKET_DB", str(ROOT / config.get("local_market_db", "data/market_data.db"))))
    client = BinanceFuturesFeedClient(MarketStore(market_db), config.get("binance_usdm_feed", {}), opener=opener)
    result = client.backfill_and_store(_iso_to_ms(start), _iso_to_ms(end), replace_providers=replace_providers)
    result["start"] = start
    result["end"] = end
    result["market_db"] = str(market_db)
    write_json(output_root / "binance_usdm_backfill" / "current.json", [result])
    return result


def run_binance_usdm_1m_backfill(
    start: str,
    end: str,
    output_root: Path | None = None,
    market_db: Path | None = None,
    opener=None,
    chunk_days: int = 7,
    pause_seconds: float = 0.1,
    sleeper=None,
) -> dict:
    """Backfill Binance XAUUSDT 1m klines for an ISO datetime window into the
    local market DB as a (GOLD, 1m) series. 1m is a NEW series, so it never
    replaces any provider (unlike the 5m backfill, which swaps the yahoo proxy).
    Used to seed the longest available history before the chan strategy runs.

    The full history is hundreds of thousands of bars, so it is fetched in
    ``chunk_days``-sized windows and persisted **incrementally** (each chunk is
    upserted before the next is fetched). That makes the backfill resumable —
    a re-run is idempotent (upsert keyed on timestamp) and only re-fetches what
    a transient failure left missing — and gentle on the API (a pause between
    pages + chunks, plus per-page retry). A chunk that still fails after retries
    is recorded in ``failed_windows`` and skipped so one bad window cannot abort
    the whole multi-month backfill."""
    config = load_pipeline_config()
    output_root = output_root or Path(os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(ROOT / config.get("output_root", "outputs"))))
    market_db = market_db or Path(os.getenv("TRADING_ORCHESTRATOR_MARKET_DB", str(ROOT / config.get("local_market_db", "data/market_data.db"))))
    client = BinanceFuturesFeedClient(MarketStore(market_db), config.get("binance_usdm_1m_feed", {}), opener=opener, sleeper=sleeper)

    start_ms, end_ms = _iso_to_ms(start), _iso_to_ms(end)
    chunk_ms = max(1, chunk_days) * 86_400_000
    total = 0
    failed_windows: list[dict] = []
    cursor = start_ms
    while cursor <= end_ms:
        window_end = min(cursor + chunk_ms - 1, end_ms)
        try:
            bars = client.fetch_range(cursor, window_end, pause_seconds=pause_seconds)
            if bars:
                client.store.upsert_bars(bars)
                client.store.upsert_quote(bars[-1])
                total += len(bars)
        except _TRANSIENT_ERRORS as exc:  # one window's repeated failure must not abort the whole backfill
            failed_windows.append({"start_ms": cursor, "end_ms": window_end, "error": f"{type(exc).__name__}: {exc}"})
        cursor = window_end + 1
        if pause_seconds and cursor <= end_ms:
            client.sleeper(pause_seconds)

    coverage = [item for item in client.store.coverage() if item["symbol"] == client.output_symbol and item["timeframe"] == client.timeframe]
    result = {
        "provider": "binance_usdm",
        "symbol": client.symbol,
        "output_symbol": client.output_symbol,
        "timeframe": client.timeframe,
        "backfilled_rows": total,
        "failed_windows": failed_windows,
        "chunk_days": chunk_days,
        "start": start,
        "end": end,
        "market_db": str(market_db),
        "coverage": coverage,
    }
    write_json(output_root / "binance_usdm_1m_backfill" / "current.json", [result])
    write_json(output_root / "binance_usdm_1m_backfill" / f"{start[:10]}.json", [result])
    return result
