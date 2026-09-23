"""Telegram pushes from the desk, merged so Park gets a few messages a day instead of a stream.

Runs on the 20-minute tick. Messages go into trading-system's Park Telegram outbox, which the
park-paper-control pass already delivers every minute; the desk never holds the bot token.

1. 早上一条 (first tick after 10:00 Beijing time): 晨报 link, 本周三件事, AI suggestion per asset,
   calls that were scored since the last digest (你 / AI), reports that did not arrive, and a nudge
   when Park has not recorded today's call yet. The AI's call never counts as his.
2. 事件提醒: two hours before a scheduled item on the three-things list, one message (with the
   trading page link where 暂停补单 lives).
3. 突发: when a new flash replaces an item on the list; at most one message every two hours.
4. 价格: the BTC grid price within 0.5% of the range top (entries start filling) or within 1.5% of
   the hard stop; once per condition per day.
Testnet fills are already pushed by trading-system itself.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from . import review
from .digest import latest_kline_file
from .watch import BJT, when_label

PUBLIC = "https://trade.park-ai-intel.com"
CALL = {"long": "做多", "short": "做空", "flat": "观望"}
OUTCOME = {"hit": "说中", "miss": "没说中", "even": "基本没动", "unverifiable": "无法核对"}
DIGEST_HOUR = 10
EVENT_LEAD = timedelta(hours=2)
BREAKING_GAP = timedelta(hours=2)
NEAR_TOP_PCT = 0.5
NEAR_STOP_PCT = 1.5

Queue = Callable[[str, str, str], dict[str, Any]]  # (idempotency_key, message_type, text) -> outbox row


def park_outbox(config: Any) -> Queue | None:
    """Queue into trading-system's Park Telegram outbox; None when the identity file is missing."""
    identity = Path.home() / ".config/trading-system/park-paper-telegram.env"
    values: dict[str, str] = {}
    try:
        for line in identity.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"')
    except OSError:
        return None
    chat, user = values.get("TRADING_ORCHESTRATOR_TELEGRAM_CHAT_ID"), values.get("TRADING_ORCHESTRATOR_TELEGRAM_PARK_USER_ID")
    if not chat or not user:
        return None
    if str(config.trading_system_checkout) not in sys.path:
        sys.path.insert(0, str(config.trading_system_checkout))
    from services.park_telegram_control import ParkTelegramLedger  # noqa: PLC0415 - trading-system owns the outbox format

    ledger = ParkTelegramLedger(config.paper_output, park_user_id=user, chat_id=chat)
    return lambda key, kind, text: ledger.queue_outbound(idempotency_key=key, message_type=kind, text=text)


class Notifier:
    def __init__(self, config: Any, store: Any, sources: Any, watch: Any, advisor: Any, *, queue: Queue | None,
                 now: Callable[[], datetime] | None = None) -> None:
        self.config, self.store, self.sources, self.watch, self.advisor = config, store, sources, watch, advisor
        self.queue = queue
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.state_path = config.db_path.parent / "notify-state.json"

    def _state(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, state: dict[str, Any]) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.state_path)

    def _send(self, state: dict[str, Any], key: str, kind: str, text: str) -> bool:
        if key in state.setdefault("sent", {}):
            return False
        if self.queue is None:
            return False
        self.queue(f"desk:{key}", kind, text)
        state["sent"][key] = self.now().astimezone(BJT).isoformat(timespec="seconds")
        return True

    def run(self) -> list[str]:
        state = self._state()
        now = self.now().astimezone(BJT)
        sent: list[str] = []
        for check in (self._digest, self._events, self._breaking, self._prices):
            key = check(state, now)
            if key:
                sent.append(key)
        cutoff = (now - timedelta(days=10)).isoformat()
        state["sent"] = {k: v for k, v in (state.get("sent") or {}).items() if v >= cutoff}
        self._save(state)
        return sent

    # ---- 1. morning digest -------------------------------------------------------------
    def _digest(self, state: dict[str, Any], now: datetime) -> str | None:
        key = f"digest:{now:%Y-%m-%d}"
        if now.hour < DIGEST_HOUR or key in (state.get("sent") or {}):
            return None
        review.resolve_due(self.store, self.sources, self.config.review_hours)
        lines = [f"☀️ {now:%m月%d日} 交易台早报"]
        brief = self._brief_today(now)
        lines.append(f"晨报已出：{PUBLIC}/desk#news" if brief else "⚠️ 今天的晨报还没出")
        items = self.watch.current().get("items") or []
        if items:
            lines.append("\n本周要看的三件事")
            lines += [f"{i}. {item.get('title')}（{when_label(item, now)}）" for i, item in enumerate(items, 1)]
        lines.append("\nAI 今天的建议")
        missing = []
        for asset in self.store.assets():
            ai = self.advisor.today(asset["key"])
            lines.append(f"· {asset['label']}：{CALL[ai['direction']]}，{ai['reason']}" if ai else f"· {asset['label']}：还没生成")
            local_today = now.date().isoformat()
            if not any(_local_date(j["created_at"]) == local_today for j in self.store.judgments(asset["key"], limit=3)):
                missing.append(asset["label"])
        since = state.get("digest_scored_since") or (now - timedelta(days=1)).isoformat()
        scored = [j for j in self.store.judgments(None, limit=200, author=None) if j.get("resolved_at") and _bjt(j["resolved_at"]) > since]
        if scored:
            lines.append("\n到期核对")
            for j in scored[:6]:
                who = "AI" if j.get("author") == "ai" else "你"
                move = f"（{j['move_pct']:+.2f}%）" if j.get("move_pct") is not None else ""
                lines.append(f"· {who} {j['asset']} {CALL.get(j['direction'], j['direction'])} → {OUTCOME.get(j.get('outcome'), j.get('outcome'))}{move}")
        problems = self._late_reports(now)
        if problems:
            lines.append("\n⚠️ " + "；".join(problems))
        if missing:
            lines.append(f"\n📝 今天还没记判断：{'、'.join(missing)}。点晨报里的看多/观望/看空就行：{PUBLIC}/desk#news")
        if self._send(state, key, "desk_digest", "\n".join(lines)):
            state["digest_scored_since"] = now.isoformat()
            return key
        return None

    def _brief_today(self, now: datetime) -> bool:
        path = self.config.morning_archive / f"{now:%Y-%m-%d}.html"
        return path.exists()

    def _late_reports(self, now: datetime) -> list[str]:
        problems = []
        kline = latest_kline_file(self.config.kline_archive, ".md")
        if not kline or kline.name[:10] != now.date().isoformat():
            problems.append("K 线日报今天没出")
        weekly = self.config.weekly_latest_html.with_suffix(".md")
        if weekly.exists() and (now.timestamp() - weekly.stat().st_mtime) > 8 * 86400:
            problems.append("宏观周报超过 8 天没更新")
        return problems

    # ---- 2. scheduled events ---------------------------------------------------------
    def _events(self, state: dict[str, Any], now: datetime) -> str | None:
        due = []
        for item in self.watch.current().get("items") or []:
            if item.get("kind") != "event":
                continue
            at = datetime.fromisoformat(item["at"])
            if timedelta(0) < at - now <= EVENT_LEAD and f"event:{item['ref']}" not in (state.get("sent") or {}):
                due.append(item)
        if not due:
            return None
        lines = ["⏰ 两小时内有大事件"]
        for item in due:
            lines.append(f"· {item.get('title')}（{when_label(item, now)}）")
            if item.get("why"):
                lines.append(f"  {item['why']}")
        lines.append(f"要暂停网格补单，到交易页按「暂停补单」：{PUBLIC}/trade")
        key = f"event:{due[0]['ref']}"
        if not self._send(state, key, "desk_event", "\n".join(lines)):
            return None
        for item in due[1:]:
            state["sent"][f"event:{item['ref']}"] = state["sent"][key]
        return key

    # ---- 3. breaking replacements ----------------------------------------------------
    def _breaking(self, state: dict[str, Any], now: datetime) -> str | None:
        current = self.watch.current()
        fresh = [item for item in current.get("items") or [] if item.get("kind") == "breaking" and item["ref"] in (current.get("replaced") or [])
                 and f"breaking:{item['ref']}" not in (state.get("sent") or {})]
        if not fresh:
            return None
        last = state.get("last_breaking")
        if last and now - datetime.fromisoformat(last) < BREAKING_GAP:
            return None
        lines = ["⚡ 三件事有更新（突发）"] + [f"· {item.get('title')}：{item.get('why') or item.get('source_title')}" for item in fresh]
        lines.append(f"{PUBLIC}/trade")
        key = f"breaking:{fresh[0]['ref']}"
        if not self._send(state, key, "desk_breaking", "\n".join(lines)):
            return None
        for item in fresh[1:]:
            state["sent"][f"breaking:{item['ref']}"] = state["sent"][key]
        state["last_breaking"] = now.isoformat()
        return key

    # ---- 4. price near the grid ------------------------------------------------------
    def _prices(self, state: dict[str, Any], now: datetime) -> str | None:
        for asset in self.store.assets():
            if asset["kind"] != "hl_testnet":
                continue
            grid = self.sources.hl_grid(asset)
            price, upper, stop = grid.get("price"), grid.get("upper"), grid.get("hard_stop")
            if not grid.get("ok") or grid.get("sealed") or price is None:
                continue
            price = float(price)
            checks = []
            if stop is not None and abs(price - float(stop)) / float(stop) * 100 <= NEAR_STOP_PCT:
                checks.append(("stop", f"🚨 {asset['label']} 离硬止损只剩 {abs(price - float(stop)) / float(stop) * 100:.2f}%：现价 {price:,.1f}，硬止损 {float(stop):,.1f}"))
            if upper is not None and price > float(upper) and (price - float(upper)) / float(upper) * 100 <= NEAR_TOP_PCT:
                checks.append(("top", f"📉 {asset['label']} 回落到网格上沿附近：现价 {price:,.1f}，上沿 {float(upper):,.1f}，买单可能开始成交"))
            for name, text in checks:
                key = f"price:{asset['key']}:{name}:{now:%Y-%m-%d}"
                if self._send(state, key, "desk_price", f"{text}\n{PUBLIC}/trade"):
                    return key
        return None


def _local_date(iso: str) -> str:
    return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone().date().isoformat()


def _bjt(iso: str) -> str:
    return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(BJT).isoformat()
