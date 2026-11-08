"""Read-through adapters. Each returns {"ok": bool, ...}; a failed source degrades, never blanks the page."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .config import Config
from .digest import latest_kline_file
from .venues import DEFAULT as DEFAULT_VENUES, Registry

from services.structured_log import get_logger, request_id

LOG = get_logger("desk.sources")

BUCKET_RANK = {"high_impact": 0, "watch": 1, "unrated": 2, "noise": 3}
SOURCE_LABEL = {"blockbeats_newsflash": "律动", "cls_telegraph": "财联社", "eastmoney_global_news": "东方财富", "reddit": "Reddit"}

Fetch = Callable[[str, dict[str, Any] | None], Any]


def http_json(url: str, body: dict[str, Any] | None = None, timeout: float = 12.0) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"content-type": "application/json", **({"X-Request-Id": request_id()} if request_id() else {})}
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        # Some upstreams answer a blocked-but-readable state with 503 and a JSON body; keep that body.
        raw = exc.read().decode(errors="replace")
        try:
            payload = json.loads(raw)
        except ValueError:
            raise exc from None
        if isinstance(payload, dict):
            payload.setdefault("_http_status", exc.code)
            return payload
        raise


class Sources:
    def __init__(self, config: Config, fetch: Fetch | None = None, venues: Registry | None = None) -> None:
        self.config = config
        self.venues = venues or DEFAULT_VENUES
        self.fetch = fetch or http_json
        self.slow_fetch = fetch or (lambda url, body: http_json(url, body, timeout=120))
        # GridMind's catalog takes 8-13 s; the default 12 s cut it off now and then, leaving gold's venue unknown (#1431).
        self.catalog_fetch = fetch or (lambda url, body: http_json(url, body, timeout=30))
        self._news_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._news_lock = threading.Lock()
        self._paper_catalog: tuple[float, dict[str, Any]] | None = None
        self._paper_catalog_lock = threading.Lock()
        self._paper_catalog_refreshing = False

    # ---- news -------------------------------------------------------------
    NEWS_TTL_SECONDS = 45

    def news(self, asset: dict[str, Any], limit: int = 60) -> dict[str, Any]:
        """Rolling list, newest first, from Intel's realtime feed; the browser polls it every minute.

        The realtime feed is shared by all assets and cached ~45s. Assets the feed does not map
        (no exposure key) fall back to keyword search. When Intel fails the last good list is served
        and marked stale.
        """
        cache_key = f"{asset['key']}:{'|'.join(asset['news_queries'])}"
        with self._news_lock:
            cached = self._news_cache.get(cache_key)
            if cached and time.monotonic() - cached[0] < self.NEWS_TTL_SECONDS:
                return cached[1]
            fresh = self._news_rolling(asset, limit)
            if fresh.get("ok"):
                self._news_cache[cache_key] = (time.monotonic(), fresh)
                return fresh
            if cached:
                return {**cached[1], "stale": True, "reason": fresh.get("reason")}
            return fresh

    def _realtime_feed(self) -> list[dict[str, Any]] | None:
        cached = self._news_cache.get("__realtime__")
        if cached and time.monotonic() - cached[0] < self.NEWS_TTL_SECONDS:
            return cached[1]
        try:
            data = self.fetch(f"{self.config.intel_url}/api/ui/realtime?" + urllib.parse.urlencode({"window": "24h", "limit": 200}), None)
        except Exception:  # noqa: BLE001
            LOG.warning("caught_exception", exc_info=True)
            return None
        rows = [row for row in (data or {}).get("items") or [] if isinstance(row, dict)]
        self._news_cache["__realtime__"] = (time.monotonic(), rows)
        return rows

    def _news_item(self, asset: dict[str, Any], row: dict[str, Any]) -> dict[str, Any] | None:
        if row.get("source") == "reddit":
            return None
        # Wire services prefix the same flash with "财联社9月15日电，周一（9月14日），"; strip it so duplicates collapse.
        title = re.sub(r"^(财联社)?\d{1,2}月\d{1,2}日电[，,]\s*(周.（\d{1,2}月\d{1,2}日），)?", "", str(row.get("title") or "")).strip()
        if not title:
            return None
        triage = row.get("triage") if isinstance(row.get("triage"), dict) else {}
        bucket = str(triage.get("bucket") or row.get("bucket") or "unrated")
        if bucket not in BUCKET_RANK:
            bucket = "unrated"
        primary = any(str(word).lower() in title.lower() for word in asset["primary"])
        stamp = str(row.get("collected_at") or row.get("published_at") or "")
        return {"topic": asset["label"] if primary else "宏观", "id": row.get("id"), "title": title,
                "source": SOURCE_LABEL.get(row.get("source"), row.get("source")), "url": row.get("url"),
                "bucket": bucket, "collected_at": stamp}

    def _news_rolling(self, asset: dict[str, Any], limit: int) -> dict[str, Any]:
        feed = self._realtime_feed()
        key = asset.get("kline_key")
        if feed is not None and key:
            items: dict[str, dict[str, Any]] = {}
            for row in feed:
                title = str(row.get("title") or "")
                related = key in (row.get("exposure_assets") or []) or any(str(w).lower() in title.lower() for w in asset["primary"])
                item = self._news_item(asset, row) if related else None
                if item and item["title"][:22] not in items:
                    items[item["title"][:22]] = item
            ordered = sorted(items.values(), key=lambda i: _utc_key(i["collected_at"]), reverse=True)
            return {"ok": True, "rolling": True, "items": ordered[:limit]}
        fallback = self._news_uncached(asset, limit)
        if fallback.get("ok"):
            fallback["items"].sort(key=lambda i: _utc_key(i.get("collected_at")), reverse=True)
        return fallback

    def _news_uncached(self, asset: dict[str, Any], limit: int) -> dict[str, Any]:
        items: dict[str, dict[str, Any]] = {}
        errors = 0
        def one(query: str) -> Any:
            url = f"{self.config.intel_url}/api/articles/search?" + urllib.parse.urlencode({"q": query, "limit": 20})
            try:
                return self.fetch(url, None)
            except Exception:  # noqa: BLE001 - one failed query must not blank the list
                LOG.warning("caught_exception", exc_info=True)
                return None

        # Intel serialises requests; parallel queries are slower than sequential ones and time out.
        results = [one(query) for query in asset["news_queries"]]
        for rows in results:
            if rows is None:
                errors += 1
                continue
            rows = rows if isinstance(rows, list) else (rows or {}).get("articles") or []
            for row in rows:
                if not isinstance(row, dict) or row.get("source") == "reddit":
                    continue
                title = str(row.get("title") or "").replace("财联社9月", "9月").strip()
                key = title[:22]
                if not title or key in items:
                    continue
                triage = row.get("triage") if isinstance(row.get("triage"), dict) else {}
                bucket = str(triage.get("bucket") or "unrated")
                if bucket not in BUCKET_RANK:
                    bucket = "unrated"
                primary = any(str(word).lower() in title.lower() for word in asset["primary"])
                items[key] = {
                    "topic": asset["label"] if primary else "宏观",
                    "id": row.get("id"),
                    "title": title,
                    "source": SOURCE_LABEL.get(row.get("source"), row.get("source")),
                    "url": row.get("url"),
                    "bucket": bucket,
                    "collected_at": row.get("collected_at"),
                }
        if errors == len(asset["news_queries"]):
            return {"ok": False, "reason": "Intel 新闻服务连不上（本机 8001），新闻暂时看不到。", "items": []}
        ordered = sorted(items.values(), key=lambda i: str(i.get("collected_at") or ""), reverse=True)
        ordered = [i for i in ordered if _same_day_window(i.get("collected_at"))] or ordered
        return {"ok": True, "items": ordered[:limit]}

    # ---- bars (venue-native: the chart that carries the grid uses the grid's own prices)
    HL_INTERVAL_MS = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}

    def testnet_bars_between(self, coin: str, start: str, end: str) -> dict[str, Any]:
        """Hourly Hyperliquid Testnet bars for one past window, for 交易回看."""
        source = f"Hyperliquid Testnet {coin}"
        try:
            start_ms = int(datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp() * 1000) - self.HL_INTERVAL_MS["1h"] * 6
            end_ms = int(datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp() * 1000) + self.HL_INTERVAL_MS["1h"] * 6
            rows = self.fetch(self.config.hyperliquid_info_url, {"type": "candleSnapshot", "req": {"coin": coin, "interval": "1h", "startTime": start_ms, "endTime": end_ms}})
        except Exception as exc:  # noqa: BLE001 - the page says the bars are missing.
            LOG.warning("caught_exception", exc_info=True)
            return {"ok": False, "reason": f"K 线读取失败：{type(exc).__name__}", "bars": [], "source": source}
        bars = [[datetime.fromtimestamp(int(r["t"]) / 1000, timezone.utc).isoformat(), float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"])]
                for r in rows or [] if isinstance(r, dict) and all(k in r for k in ("t", "o", "h", "l", "c"))]
        return {"ok": bool(bars), "bars": bars, "source": source}

    def bars(self, asset: dict[str, Any], timeframe: str, limit: int = 60) -> dict[str, Any]:
        if timeframe not in self.HL_INTERVAL_MS:
            return {"ok": False, "reason": "不支持的周期", "bars": []}
        return self.venues.of(asset).bars(self, asset, timeframe, limit)

    def hl_bars(self, asset: dict[str, Any], timeframe: str, limit: int) -> dict[str, Any]:
        """Hyperliquid Testnet candles for the asset's coin: the same price its grid trades on."""
        source = f"Hyperliquid Testnet {asset['coin']}（网格下单的同一个价格）"
        end = int(time.time() * 1000)
        start = end - self.HL_INTERVAL_MS[timeframe] * (limit + 1)
        try:
            rows = self.fetch(self.config.hyperliquid_info_url, {"type": "candleSnapshot", "req": {"coin": asset["coin"], "interval": timeframe, "startTime": start, "endTime": end}})
        except Exception as exc:  # noqa: BLE001
            LOG.warning("caught_exception", exc_info=True)
            return {"ok": False, "reason": f"K 线读取失败：{type(exc).__name__}", "bars": [], "source": source}
        bars = [[datetime.fromtimestamp(int(r["t"]) / 1000, timezone.utc).isoformat(), float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"])]
                for r in rows or [] if isinstance(r, dict) and all(k in r for k in ("t", "o", "h", "l", "c"))][-limit:]
        if not bars:
            return {"ok": False, "reason": "K 线暂时没有数据", "bars": [], "source": source}
        return {"ok": True, "bars": bars, "source": source, "fresh": True, "latest_timestamp": bars[-1][0]}

    def paper_gold_bars(self, timeframe: str, limit: int) -> dict[str, Any]:
        """The Paper gold grid's own market bars, from the Dashboard."""
        url = f"{self.config.dashboard_url}/api/dualtrack/market/bars?" + urllib.parse.urlencode({"symbol": "GOLD", "timeframe": timeframe, "limit": limit})
        paper = self.paper_gold()
        source = f"{paper['market_label']}（纸面网格用的同一个价格）" if paper else "纸面黄金（来源读不到：GridMind 目录不可用）"
        try:
            payload = self.fetch(url, None)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("caught_exception", exc_info=True)
            return {"ok": False, "reason": f"K 线读取失败：{type(exc).__name__}", "bars": [], "source": source}
        bars = [
            [b["timestamp"], float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"])]
            for b in (payload or {}).get("bars") or []
            if all(b.get(k) is not None for k in ("open", "high", "low", "close"))
        ]
        if not bars or payload.get("status") != "ready":
            return {"ok": False, "reason": "K 线暂时没有数据（行情服务未就绪）", "bars": [], "source": source}
        return {"ok": True, "bars": bars, "source": source, "fresh": payload.get("fresh") is not False,
                "latest_timestamp": payload.get("latest_timestamp")}

    # The Paper venue and instrument ids come from configs/dualtrack.yaml through GridMind's catalog, and a deploy
    # restarts the desk, so they are kept 10 minutes and refreshed in the background after 5 (#1431). GridMind's
    # catalog takes 8-13 s (it reads Hyperliquid's public market each time); gold, silver and crude used to read
    # it once each, so /api/assets took ~29 s and the page waited on it.
    PAPER_CATALOG_TTL_SECONDS = 600.0
    PAPER_CATALOG_REFRESH_SECONDS = 300.0

    def _fetch_paper_catalog(self) -> dict[str, Any] | None:
        try:
            payload = self.catalog_fetch(f"{self.config.dashboard_url}/api/dashboard-control/catalog", None)
        except Exception:  # noqa: BLE001 - unreachable catalog is reported by the caller.
            LOG.warning("caught_exception", exc_info=True)
            return None
        return payload if isinstance(payload, dict) else None

    def _refresh_paper_catalog(self) -> None:
        try:
            payload = self._fetch_paper_catalog()
            if payload is not None:  # a failed read never replaces the last good ids; they expire on their own
                with self._paper_catalog_lock:
                    self._paper_catalog = (time.monotonic(), payload)
        finally:
            self._paper_catalog_refreshing = False

    def paper_catalog(self) -> dict[str, Any] | None:
        """GridMind's catalog for the Paper ids: one read shared by gold, silver and crude, one request at a time.

        Fresh (under 5 min): served. Ageing (5-10 min): served, and one background refresh starts. Expired or
        never read: the caller waits for one read; concurrent callers wait for the same read. None when the
        catalog cannot be read and nothing unexpired is held; callers say so instead of guessing.
        """
        cached = self._paper_catalog
        age = time.monotonic() - cached[0] if cached else None
        if age is not None and age < self.PAPER_CATALOG_TTL_SECONDS:
            if age >= self.PAPER_CATALOG_REFRESH_SECONDS and not self._paper_catalog_refreshing:
                self._paper_catalog_refreshing = True
                threading.Thread(target=self._refresh_paper_catalog, name="paper-catalog", daemon=True).start()
            return cached[1]
        with self._paper_catalog_lock:
            cached = self._paper_catalog  # another caller may have read it while this one waited
            if cached and time.monotonic() - cached[0] < self.PAPER_CATALOG_TTL_SECONDS:
                return cached[1]
            payload = self._fetch_paper_catalog()
            if payload is not None:
                self._paper_catalog = (time.monotonic(), payload)
            return payload

    def paper_gold(self) -> dict[str, Any] | None:
        """The Paper gold venue GridMind actually uses, read from its catalog.

        The desk never names the venue itself, so a venue switch in
        configs/dualtrack.yaml shows here without a desk change. Returns None
        when the catalog is unreachable; callers say so instead of guessing.
        """

        payload = self.paper_catalog()
        if payload is None:
            return None
        profile = next((p for p in (payload or {}).get("venue_profiles") or [] if p.get("environment") == "paper"), None)
        instrument = next((i for i in (profile or {}).get("instruments") or [] if i.get("asset") == "XAU"), None)
        if not profile or not instrument:
            return None
        venue = profile.get("paper_gold_venue") or {}
        result = {
            "venue_profile_id": profile.get("id"),
            "instrument_id": instrument.get("instrument_id"),
            "venue_label": profile.get("label") or profile.get("id"),
            "market_label": venue.get("label") or profile.get("label") or profile.get("id"),
        }
        return result

    def paper_instrument(self, instrument: str) -> dict[str, Any] | None:
        """A Paper instrument's ids in GridMind's catalog (silver and crude, #1407); None when not listed or unreachable."""
        if instrument == "gold":
            return self.paper_gold()
        payload = self.paper_catalog()
        if payload is None:
            return None
        result = None
        for profile in (payload or {}).get("venue_profiles") or []:
            if profile.get("environment") != "paper":
                continue
            row = next((i for i in profile.get("instruments") or [] if i.get("asset") == PAPER_ASSETS[instrument]), None)
            if row:
                result = {"venue_profile_id": profile.get("id"), "instrument_id": row.get("instrument_id"),
                          "eligibility": row.get("eligibility")}
                break
        return result

    def catalog(self) -> dict[str, Any]:
        try:
            payload = self.fetch(f"{self.config.dashboard_url}/api/dashboard-control/catalog", None)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("caught_exception", exc_info=True)
            return {"ok": False, "reason": f"交易所品种目录读不到：{type(exc).__name__}", "instruments": []}
        profile = next((p for p in payload.get("venue_profiles") or [] if p.get("id") == "hyperliquid.testnet"), None)
        if not profile:
            return {"ok": False, "reason": "目录里没有 Hyperliquid 测试盘", "instruments": []}
        return {"ok": True, "venue": "Hyperliquid Testnet", "instruments": [
            {"coin": i.get("asset"), "instrument_id": i.get("instrument_id"), "eligibility": i.get("eligibility")}
            for i in profile.get("instruments") or [] if i.get("asset") and i.get("instrument_id")]}

    def kline_view(self, asset: dict[str, Any]) -> dict[str, Any]:
        """Today's K-line daily reading for this asset, from the machine-readable article."""
        key = asset.get("kline_key")
        if not key:
            return {"ok": False, "reason": "K 线日报不覆盖这个品种"}
        folder = self.config.kline_archive
        latest = latest_kline_file(folder, ".article.json")
        if latest is None:
            return {"ok": False, "reason": "还没有 K 线日报"}
        try:
            article = json.loads(latest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"ok": False, "reason": "K 线日报正在生成，稍后刷新"}
        blocks = [b for b in article.get("blocks") or [] if b.get("asset_key") == key]
        summary = next((b for b in blocks if b.get("type") == "asset_summary"), None)
        if not summary:
            return {"ok": False, "reason": "今天的 K 线日报没有这个品种"}
        periods = [{"label": b.get("label"), "text": b.get("text")} for b in blocks if b.get("type") == "period_text" and b.get("text")]
        return {"ok": True, "date": latest.name[:10], "cutoff_at": article.get("cutoff_at"),
                "position": summary.get("position"), "structure": summary.get("structure"), "synthesis": summary.get("synthesis"),
                "odds": summary.get("odds"), "periods": periods}

    # ---- judgment-only assets (silver, WTI): the K-line review service's candles ---------
    OVERVIEW_TF = {"1h": "thirty_minute", "4h": "four_hour", "1d": "daily"}
    OVERVIEW_TTL_SECONDS = 600
    OVERVIEW_STALE = timedelta(hours=2)

    def refresh_futures(self, keys: list[str], now: datetime | None = None) -> dict[str, Any]:
        """Ask 8932 to re-download intraday candles when the newest 30-minute candle of any judgment-only
        asset is older than two hours. Runs on the 20-minute job, never in a page request: a refresh takes
        ~20 seconds. Without it silver and oil calls would be recorded and scored against the same frozen
        candle from the morning brief."""
        now = now or datetime.now(timezone.utc)
        url = f"{self.config.kline_review_url}/api/overview"
        try:
            rows = {row.get("asset_key"): row for row in (self.fetch(url, None) or {}).get("assets") or [] if isinstance(row, dict)}
        except Exception as exc:  # noqa: BLE001
            LOG.warning("caught_exception", exc_info=True)
            return {"ok": False, "reason": type(exc).__name__}
        newest = []
        for key in keys:
            frame = next((tf for tf in (rows.get(key) or {}).get("timeframes") or [] if tf.get("timeframe") == "thirty_minute"), None)
            candles = (frame or {}).get("candles") or []
            newest.append(datetime.fromisoformat(str(candles[-1]["timestamp"]).replace("Z", "+00:00")) if candles else None)
        if all(stamp and now - stamp <= self.OVERVIEW_STALE for stamp in newest):
            return {"ok": True, "refreshed": False}
        try:
            self.slow_fetch(url + "?refresh=true", None)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("caught_exception", exc_info=True)
            return {"ok": False, "reason": type(exc).__name__}
        self._news_cache.pop("__overview__", None)
        return {"ok": True, "refreshed": True}

    def overview_bars(self, asset: dict[str, Any], timeframe: str, limit: int) -> dict[str, Any]:
        """Futures candles from Human K-line Review (8932), the same data the K-line daily reads.
        1h requests get 30-minute candles: finer, and enough for the 72-hour review. The service refreshes
        its intraday cache when the morning brief asks, so the latest candle can be hours old."""
        source = "期货 K 线（K 线日报同一数据源，可能延迟）"
        cached = self._news_cache.get("__overview__")
        if not cached or time.monotonic() - cached[0] > self.OVERVIEW_TTL_SECONDS:
            try:
                payload = self.fetch(f"{self.config.kline_review_url}/api/overview", None)
                cached = (time.monotonic(), {row.get("asset_key"): row for row in (payload or {}).get("assets") or [] if isinstance(row, dict)})
                self._news_cache["__overview__"] = cached
            except Exception as exc:  # noqa: BLE001
                LOG.warning("caught_exception", exc_info=True)
                if not cached:
                    return {"ok": False, "reason": f"K 线读取失败：{type(exc).__name__}", "bars": [], "source": source}
        row = cached[1].get(asset.get("kline_key") or "")
        frame = next((tf for tf in (row or {}).get("timeframes") or [] if tf.get("timeframe") == self.OVERVIEW_TF.get(timeframe)), None)
        bars = []
        for candle in (frame or {}).get("candles") or []:
            stamp = str(candle.get("timestamp") or "")
            if len(stamp) == 10:
                stamp += "T00:00:00+00:00"
            if all(candle.get(k) is not None for k in ("open", "high", "low", "close")):
                bars.append([stamp, float(candle["open"]), float(candle["high"]), float(candle["low"]), float(candle["close"])])
        bars = bars[-limit:]
        if not bars:
            return {"ok": False, "reason": "K 线暂时没有数据", "bars": [], "source": source}
        return {"ok": True, "bars": bars, "source": source, "fresh": False, "latest_timestamp": bars[-1][0]}

    # ---- OKX public swaps through the datafeed (silver, crude; #1387) -------
    OKX_SOURCE = "okx_swap_public"

    def okx_bars(self, asset: dict[str, Any], timeframe: str, limit: int, now: datetime | None = None) -> dict[str, Any]:
        """OKX's own candles for the asset's swap, from datafeed 8100. Fail closed: a reply from any other source or
        symbol, or a newest candle older than two periods, is ``ok=false``. Nothing falls back to futures candles."""
        instrument = str(asset["instrument_id"])
        source = f"OKX {instrument} 永续公开行情（datafeed）"
        url = f"{self.config.datafeed_url}/api/candles/commodity/{urllib.parse.quote(instrument)}?" + urllib.parse.urlencode(
            {"timeframe": timeframe, "limit": limit, "source": self.OKX_SOURCE, "cache_policy": "bypass"})  # 8100 caches 1h bars up to 3 hours (#1390)
        try:
            payload = self.fetch(url, None) or {}
        except Exception as exc:  # noqa: BLE001
            LOG.warning("caught_exception", exc_info=True)
            return {"ok": False, "reason": f"K 线读取失败：{type(exc).__name__}", "bars": [], "source": source}
        if payload.get("selected_source") != self.OKX_SOURCE or payload.get("provider_symbol") != instrument or payload.get("is_synthetic"):
            return {"ok": False, "reason": "K 线来源不是 OKX 这个合约，不用", "bars": [], "source": source}
        bars = [[c["timestamp"], float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"])]
                for c in payload.get("candles") or [] if all(c.get(k) is not None for k in ("timestamp", "open", "high", "low", "close"))][-limit:]
        if not bars:
            return {"ok": False, "reason": "K 线暂时没有数据", "bars": [], "source": source}
        latest = datetime.fromisoformat(str(bars[-1][0]).replace("Z", "+00:00"))
        if (now or datetime.now(timezone.utc)) - latest > timedelta(milliseconds=2 * self.HL_INTERVAL_MS[timeframe]):
            return {"ok": False, "reason": f"K 线过期（最新一根 {bars[-1][0][:16]}）", "bars": [], "source": source}
        return {"ok": True, "bars": bars, "source": source, "fresh": True, "latest_timestamp": bars[-1][0]}

    # ---- BTC: Hyperliquid Testnet grid + account ---------------------------
    def hl_mid(self, asset: dict[str, Any]) -> float | None:
        """The venue's live mid for this coin (public, no account), or None. Never a recorded price (#1372)."""
        try:
            mids = self.fetch(self.config.hyperliquid_info_url, {"type": "allMids"})
            value = float((mids or {})[asset["coin"]])
        except Exception:  # noqa: BLE001 - no mid means no venue price; the caller says which source it used
            LOG.warning("caught_exception", exc_info=True)
            return None
        return value if value > 0 else None

    def hl_grid(self, asset: dict[str, Any]) -> dict[str, Any]:
        folder = self.config.paper_output / "dualtrack" / "grid_testnet_lifecycle"
        # The live grid is the state with the newest updated_at; file mtime can be touched by hand edits.
        candidates = []
        unreadable = False
        for path in folder.glob("*.json") if folder.exists() else ():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                unreadable = True
                continue
            loaded = loaded[-1] if isinstance(loaded, list) and loaded else loaded
            if isinstance(loaded, dict) and str(loaded.get("instrument_id") or asset["instrument_id"]) == asset["instrument_id"]:
                candidates.append(loaded)
        if not candidates:
            return {"ok": False, "none": not unreadable, "reason": "网格记录正在写入，稍后自动刷新" if unreadable else f"{asset['label']} 还没有网格"}
        state = max(candidates, key=lambda s: str(s.get("updated_at") or ""))
        rungs = [r for r in state.get("rungs") or [] if isinstance(r, dict)]
        orders = [o for o in state.get("orders") or [] if isinstance(o, dict)]
        open_orders = [o for o in orders if o.get("state") in {"accepted", "cancel_pending", "submit_pending", "submit_unknown"}]
        return {
            "ok": True,
            "status": state.get("status"),
            "status_label": GRID_STATUS.get(str(state.get("status")), str(state.get("status"))),
            "direction": state.get("direction"),
            "lower": state.get("lower_boundary"),
            "upper": state.get("upper_boundary"),
            "hard_stop": state.get("hard_stop"),
            "rungs": [float(r["price"]) for r in rungs if r.get("price") is not None],
            "open_orders": len(open_orders),
            "fills": len(state.get("fills") or []),
            "price": state.get("last_market_price"),
            "updated_at": state.get("updated_at"),
            "blocker": state.get("blocker"),
            "sealed": bool(state.get("sealed")),
            "notional_per_rung": round(float(rungs[0]["price"]) * float(rungs[0]["quantity"]), 2) if rungs and rungs[0].get("quantity") else None,
        }

    def hl_account(self) -> dict[str, Any]:
        address = self.config.hyperliquid_account
        try:
            state = self.fetch(self.config.hyperliquid_info_url, {"type": "clearinghouseState", "user": address})
            fills = self.fetch(self.config.hyperliquid_info_url, {"type": "userFills", "user": address})
        except Exception as exc:  # noqa: BLE001
            LOG.warning("caught_exception", exc_info=True)
            return {"ok": False, "reason": f"Hyperliquid 测试盘账户读不到：{type(exc).__name__}"}
        positions = []
        unrealized = 0.0
        for item in state.get("assetPositions") or []:
            pos = item.get("position") or {}
            size = float(pos.get("szi") or 0)
            if abs(size) < 1e-12:
                continue
            pnl = float(pos.get("unrealizedPnl") or 0)
            unrealized += pnl
            positions.append({"asset": pos.get("coin"), "side": "long" if size > 0 else "short", "size": abs(size),
                              "entry": float(pos.get("entryPx") or 0), "unrealized": pnl})
        realized = sum(float(f.get("closedPnl") or 0) - float(f.get("fee") or 0) for f in fills or [])
        return {
            "ok": True, "venue": "Hyperliquid 测试盘账户", "money": "测试盘（假钱）",
            "equity": float((state.get("marginSummary") or {}).get("accountValue") or 0),
            "realized": round(realized, 4), "unrealized": round(unrealized, 4), "positions": positions,
            "trades": len(fills or []), "reconciliation": "ok",
        }

    # ---- XAU: Nautilus paper grid ------------------------------------------
    def _paper_ledger(self) -> dict[str, Any]:
        """The paper account's own ledger; the read-model only covers the active session."""
        folder = self.config.paper_output / "dualtrack" / "nautilus_authoritative" / "snapshots"
        files = sorted(folder.glob("*.json"), key=lambda path: path.stat().st_mtime) if folder.exists() else []
        if not files:
            return {}
        try:
            rows = json.loads(files[-1].read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        snap = rows[-1] if isinstance(rows, list) and rows else rows
        account, pnl = snap.get("account") or {}, snap.get("pnl") or {}
        positions = [{"asset": "XAU", "side": p.get("side"), "size": p.get("remaining_units"), "entry": p.get("entry_price"),
                      "tp": p.get("tp"), "sl": p.get("sl")}
                     for p in snap.get("positions") or [] if str(p.get("status") or "").lower() == "open"]
        return {"equity": account.get("equity"), "starting_cash": account.get("starting_cash"),
                "realized": pnl.get("realized"), "unrealized": pnl.get("unrealized"), "positions": positions}

    def xau_paper(self) -> dict[str, Any]:
        return self.paper_grid("gold")

    def paper_grid(self, instrument: str = "gold") -> dict[str, Any]:
        """One instrument's view of the Paper grid (ADR 0009: one instrument at a time).

        The running grid shows only under the instrument its session recorded; every other instrument sees
        "not running" and no price, never another instrument's grid or mark.
        """
        label = PAPER_LABELS[instrument]
        try:
            model = self.fetch(f"{self.config.dashboard_url}/api/park-paper/read-model", None)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("caught_exception", exc_info=True)
            return {"ok": False, "reason": f"{label}纸面盘读不到：{type(exc).__name__}"}
        strategy = model.get("strategy") or {}
        execution = model.get("execution") or {}
        running = str(strategy.get("paper_instrument") or "gold")  # a read model from before #1407 ran gold only
        if strategy.get("active") is True and running != instrument:
            other = PAPER_LABELS.get(running, running)
            ledger = self._paper_ledger()
            grid = {"ok": False, "none": True, "reason": f"纸面盘正在跑{other}网格，一次只跑一个；{label}现在没有网格。", "price": None}
            account_view = {"ok": True, "venue": f"纸面盘 · {other}网格在跑", "money": "纸面模拟",
                            "equity": ledger.get("equity"), "starting_cash": ledger.get("starting_cash"),
                            "realized": ledger.get("realized"), "unrealized": ledger.get("unrealized"),
                            "positions": ledger.get("positions") or [], "trades": None, "reconciliation": "unknown",
                            "note": f"纸面盘正在跑{other}网格"}
            return {"ok": True, "grid": grid, "account": account_view, "price": None}
        if strategy.get("active") is not True:
            terminal = model.get("terminal") or {}
            if terminal and str(terminal.get("paper_instrument") or "gold") != instrument:
                terminal = {}  # the last ended grid was another instrument's
            closed = terminal.get("closed_count")
            ended = terminal.get("ended_at")
            reason = {"lower_boundary_invalidated": "跌破区间下沿", "upper_boundary_invalidated": "涨破区间上沿", "hard_stop": "触发硬止损"}.get(terminal.get("reason"), terminal.get("reason") or "")
            # Idle, the mark is whatever ran last; only gold (the pre-#1407 view) keeps showing it.
            price = (model.get("market") or {}).get("price") if instrument == "gold" else None
            ended_text = (f"：{reason}，{closed} 笔已平仓" if closed is not None else f"：{reason}") if terminal else ""
            reconciliation = (terminal.get("reconciliation") or {}).get("status") if terminal.get("ended_flat") is True else None
            grid = {"ok": False, "none": True, "ended": True, "reason": f"{label}网格已结束{ended_text}。现在没有运行中的{label}网格。",
                    "ended_at": ended, "status": "TERMINAL", "price": price}
            if instrument != "gold" and not terminal:
                grid = {"ok": False, "none": True, "reason": f"现在没有运行中的{label}网格。", "price": None}
            ledger = self._paper_ledger()
            open_positions = ledger.get("positions") or []
            account_view = {"ok": True, "venue": PAPER_ACCOUNT_LABELS[instrument], "money": "纸面模拟",
                            "equity": ledger.get("equity"), "starting_cash": ledger.get("starting_cash"),
                            "realized": ledger.get("realized"), "unrealized": ledger.get("unrealized"),
                            "positions": open_positions, "trades": None, "reconciliation": reconciliation or "unknown",
                            "note": (f"网格已结束（{reason}）· " + (f"仍有 {len(open_positions)} 笔持仓无人管理" if open_positions else "无持仓"))}
            return {"ok": True, "grid": grid, "account": account_view, "price": price}
        account = execution.get("account") or {}
        pnl = execution.get("pnl") or {}
        positions = [p for p in execution.get("positions") or [] if p.get("status") == "open"]
        price = (model.get("market") or {}).get("price")
        rungs = [float(p) for p in strategy.get("grid_rung_prices") or []]
        grid = {
            "ok": bool(strategy),
            "status": strategy.get("state"),
            "status_label": PAPER_STATE.get(str(strategy.get("state")), str(strategy.get("state"))),
            "direction": strategy.get("direction"),
            "lower": strategy.get("lower_price_boundary"),
            "upper": strategy.get("upper_price_boundary"),
            "hard_stop": strategy.get("hard_stop"),
            "rungs": rungs,
            "open_orders": sum(1 for o in execution.get("orders") or [] if o.get("state") in {"accepted", "open", "resting"}),
            "fills": (execution.get("counts") or {}).get("fills"),
            "price": price,
            "updated_at": model.get("generated_at"),
            "max_loss": strategy.get("theoretical_max_loss"),
            "units_per_rung": positions[0].get("remaining_units") if positions else (0.2 if instrument == "gold" else None),
        }
        account_view = {
            "ok": True,
            "venue": PAPER_ACCOUNT_LABELS[instrument],
            "money": "纸面模拟",
            "equity": account.get("equity"),
            "starting_cash": account.get("starting_cash"),
            "realized": pnl.get("realized"),
            "unrealized": pnl.get("unrealized"),
            "positions": [{"asset": PAPER_ASSETS[instrument], "side": p.get("side"), "size": p.get("remaining_units"),
                           "entry": p.get("entry_price"), "tp": p.get("tp"), "sl": p.get("sl")} for p in positions],
            "trades": (execution.get("counts") or {}).get("closed_positions"),
            "reconciliation": (execution.get("reconciliation") or {}).get("status") or "unknown",
        }
        return {"ok": True, "grid": grid, "account": account_view, "price": price}


GRID_STATUS = {
    "active": "运行中", "paused_above_range": "价格高于区间 · 暂停挂新单", "paused_below_range": "价格低于区间 · 暂停",
    "hard_stop_triggered": "触发止损 · 平仓中", "blocked_reconciliation": "卡住 · 需要处理", "terminal": "已结束",
    "stopped_by_operator": "已手动停止",
}
PAPER_STATE = {"ACTIVE_LOCKED": "运行中", "PAUSED": "暂停", "TERMINAL": "已结束"}
# ADR 0009: the Paper instruments, by the key a Strategy Session records.
PAPER_LABELS = {"gold": "黄金", "silver": "白银", "crude": "原油"}
PAPER_ASSETS = {"gold": "XAU", "silver": "XAG", "crude": "CL"}
PAPER_ACCOUNT_LABELS = {"gold": "Binance 纸面盘 · 黄金网格", "silver": "OKX 纸面盘 · 白银网格", "crude": "OKX 纸面盘 · 原油网格"}


def _same_day_window(collected_at: Any, hours: int = 30) -> bool:
    try:
        at = datetime.fromisoformat(str(collected_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - at).total_seconds() <= hours * 3600


def _utc_key(stamp: Any) -> str:
    """Intel mixes naive and Z-suffixed UTC stamps; compare them as the same clock."""
    text = str(stamp or "")
    return text.replace("Z", "").replace("+00:00", "")[:26]
