"""Read-through adapters. Each returns {"ok": bool, ...}; a failed source degrades, never blanks the page."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .config import Config

BUCKET_RANK = {"high_impact": 0, "watch": 1, "unrated": 2, "noise": 3}
SOURCE_LABEL = {"blockbeats_newsflash": "律动", "cls_telegraph": "财联社", "eastmoney_global_news": "东方财富", "reddit": "Reddit"}

Fetch = Callable[[str, dict[str, Any] | None], Any]


def http_json(url: str, body: dict[str, Any] | None = None, timeout: float = 12.0) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"content-type": "application/json"}, method="POST" if data else "GET")
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
    def __init__(self, config: Config, fetch: Fetch | None = None) -> None:
        self.config = config
        self.fetch = fetch or http_json
        self._news_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._news_lock = threading.Lock()

    # ---- news -------------------------------------------------------------
    NEWS_TTL_SECONDS = 300

    def news(self, asset: dict[str, Any], limit: int = 30) -> dict[str, Any]:
        """Cached for five minutes per asset and keyword set; when Intel fails, the last good list is served and marked stale."""
        cache_key = f"{asset['key']}:{'|'.join(asset['news_queries'])}"
        with self._news_lock:
            cached = self._news_cache.get(cache_key)
            if cached and time.monotonic() - cached[0] < self.NEWS_TTL_SECONDS:
                return cached[1]
            fresh = self._news_uncached(asset, limit)
            if fresh.get("ok"):
                self._news_cache[cache_key] = (time.monotonic(), fresh)
                return fresh
            if cached:
                return {**cached[1], "stale": True, "reason": fresh.get("reason")}
            return fresh

    def _news_uncached(self, asset: dict[str, Any], limit: int) -> dict[str, Any]:
        items: dict[str, dict[str, Any]] = {}
        errors = 0
        def one(query: str) -> Any:
            url = f"{self.config.intel_url}/api/articles/search?" + urllib.parse.urlencode({"q": query, "limit": 20})
            try:
                return self.fetch(url, None)
            except Exception:  # noqa: BLE001 - one failed query must not blank the list
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
        ordered.sort(key=lambda i: (BUCKET_RANK[i["bucket"]], i["topic"] == "宏观"))
        return {"ok": True, "items": ordered[:limit]}

    # ---- bars (venue-native: the chart that carries the grid uses the grid's own prices)
    HL_INTERVAL_MS = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}

    def bars(self, asset: dict[str, Any], timeframe: str, limit: int = 60) -> dict[str, Any]:
        if timeframe not in self.HL_INTERVAL_MS:
            return {"ok": False, "reason": "不支持的周期", "bars": []}
        if asset["kind"] == "hl_testnet":
            source = f"Hyperliquid Testnet {asset['coin']}（网格下单的同一个价格）"
            end = int(time.time() * 1000)
            start = end - self.HL_INTERVAL_MS[timeframe] * (limit + 1)
            try:
                rows = self.fetch(self.config.hyperliquid_info_url, {"type": "candleSnapshot", "req": {"coin": asset["coin"], "interval": timeframe, "startTime": start, "endTime": end}})
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "reason": f"K 线读取失败：{type(exc).__name__}", "bars": [], "source": source}
            bars = [[datetime.fromtimestamp(int(r["t"]) / 1000, timezone.utc).isoformat(), float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"])]
                    for r in rows or [] if isinstance(r, dict) and all(k in r for k in ("t", "o", "h", "l", "c"))][-limit:]
            if not bars:
                return {"ok": False, "reason": "K 线暂时没有数据", "bars": [], "source": source}
            return {"ok": True, "bars": bars, "source": source, "fresh": True, "latest_timestamp": bars[-1][0]}
        url = f"{self.config.dashboard_url}/api/dualtrack/market/bars?" + urllib.parse.urlencode({"symbol": "XAUUSDT", "timeframe": timeframe, "limit": limit})
        source = "Binance XAUUSDT 永续（纸面网格用的同一个价格）"
        try:
            payload = self.fetch(url, None)
        except Exception as exc:  # noqa: BLE001
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

    def catalog(self) -> dict[str, Any]:
        try:
            payload = self.fetch(f"{self.config.dashboard_url}/api/dashboard-control/catalog", None)
        except Exception as exc:  # noqa: BLE001
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
        files = sorted(folder.glob("*-kline-daily-newsletter.article.json")) if folder.exists() else []
        if not files:
            return {"ok": False, "reason": "还没有 K 线日报"}
        try:
            article = json.loads(files[-1].read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"ok": False, "reason": "K 线日报正在生成，稍后刷新"}
        blocks = [b for b in article.get("blocks") or [] if b.get("asset_key") == key]
        summary = next((b for b in blocks if b.get("type") == "asset_summary"), None)
        if not summary:
            return {"ok": False, "reason": "今天的 K 线日报没有这个品种"}
        periods = [{"label": b.get("label"), "text": b.get("text")} for b in blocks if b.get("type") == "period_text" and b.get("text")]
        return {"ok": True, "date": files[-1].name[:10], "cutoff_at": article.get("cutoff_at"),
                "position": summary.get("position"), "structure": summary.get("structure"), "synthesis": summary.get("synthesis"),
                "odds": summary.get("odds"), "periods": periods}

    # ---- BTC: Hyperliquid Testnet grid + account ---------------------------
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
    def xau_paper(self) -> dict[str, Any]:
        try:
            model = self.fetch(f"{self.config.dashboard_url}/api/park-paper/read-model", None)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"黄金纸面盘读不到：{type(exc).__name__}"}
        strategy = model.get("strategy") or {}
        execution = model.get("execution") or {}
        if strategy.get("active") is not True:
            terminal = model.get("terminal") or {}
            receipts = terminal.get("exit_receipts") or []
            ended = receipts[-1].get("ts") if receipts else None
            reason = {"lower_boundary_invalidated": "跌破区间下沿", "upper_boundary_invalidated": "涨破区间上沿", "hard_stop": "触发硬止损"}.get(terminal.get("reason"), terminal.get("reason") or "")
            price = (model.get("market") or {}).get("price")
            ended_text = f"：{reason}，{len(receipts)} 笔在 {terminal.get('observed_price')} 平仓" if terminal else ""
            grid = {"ok": False, "none": True, "ended": True, "reason": f"黄金网格已结束{ended_text}。现在没有运行中的黄金网格。",
                    "ended_at": ended, "status": "TERMINAL", "price": price}
            account_view = {"ok": True, "venue": "Binance 纸面盘 · 黄金网格", "money": "纸面模拟", "equity": None, "starting_cash": None,
                            "realized": None, "unrealized": None, "positions": [], "trades": None, "reconciliation": "ok",
                            "note": f"网格已结束（{reason}）· 无持仓"}
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
            "units_per_rung": positions[0].get("remaining_units") if positions else 0.2,
        }
        account_view = {
            "ok": True,
            "venue": "Binance 纸面盘 · 黄金网格",
            "money": "纸面模拟",
            "equity": account.get("equity"),
            "starting_cash": account.get("starting_cash"),
            "realized": pnl.get("realized"),
            "unrealized": pnl.get("unrealized"),
            "positions": [{"asset": "XAU", "side": p.get("side"), "size": p.get("remaining_units"),
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


def _same_day_window(collected_at: Any, hours: int = 30) -> bool:
    try:
        at = datetime.fromisoformat(str(collected_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - at).total_seconds() <= hours * 3600
