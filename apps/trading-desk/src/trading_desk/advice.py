"""AI 每日方向建议: one call per asset per day, proposed by the model and decided by Park.

At 08:40 Beijing time (and when Park presses 刷新) the model reads, per asset, the K-line daily
reading, the last 24 hours of related news and 本周要看的三件事, and proposes long / short /
wait with a one-line reason. The suggestion is stored as a judgment with author='ai' and
scored after 72 hours exactly like Park's own calls, which gives 你 vs AI. The grid attached
to it comes from the same deterministic plan builder Park's calls use; the model never sizes
or places anything. When the model fails nothing is stored: no rules-made "AI" advice.
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from typing import Any, Callable

from . import plans
from .store import Store
from .watch import codex_complete

DIRECTIONS = {"long": "做多", "short": "做空", "flat": "观望"}
_RUN_LOCK = threading.Lock()


class Advisor:
    def __init__(self, store: Store, sources: Any, watch: Any, *, complete: Callable[[str], str] = codex_complete,
                 asset_state: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> None:
        self.store = store
        self.sources = sources
        self.watch = watch
        self.complete = complete
        self.asset_state = asset_state

    def today(self, asset: str) -> dict[str, Any] | None:
        local_today = datetime.now(timezone.utc).astimezone().date().isoformat()
        return next((j for j in self.store.judgments(asset, limit=5, author="ai") if _local_date(j["created_at"]) == local_today), None)

    def generate(self, *, force: bool = False) -> dict[str, Any]:
        """Advice for every asset that has none today (or all when forced). One model call for all assets."""
        if not _RUN_LOCK.acquire(blocking=False):
            return {"status": "running"}
        try:
            targets = [a for a in self.store.assets() if force or self.today(a["key"]) is None]
            if not targets:
                return {"status": "fresh"}
            states = {a["key"]: (self.asset_state(a) if self.asset_state else {"price": None, "grid": {}}) for a in targets}
            prompt = self.prompt(targets, states)
            parsed = self.parse(self.complete(prompt), {a["key"] for a in targets})
            if not parsed:
                return {"status": "model_unavailable"}
            saved = []
            for asset in targets:
                call = parsed.get(asset["key"])
                if not call:
                    continue
                state = states[asset["key"]]
                plan = plans.build_plan(asset["kind"], call["direction"], state.get("price"), state.get("grid")) if asset["kind"] in plans.TEMPLATE else None
                existing = self.today(asset["key"])
                fields = dict(direction=call["direction"], confidence=call["confidence"], reason=call["reason"], cited=[],
                              price_at=state.get("price"), plan=plan, action="recorded")
                saved.append(self.store.revise_judgment(existing["id"], **fields) if existing else self.store.add_judgment(asset=asset["key"], author="ai", **fields))
            return {"status": "ok", "saved": [{"asset": j["asset"], "direction": j["direction"]} for j in saved]}
        finally:
            _RUN_LOCK.release()

    def prompt(self, assets: list[dict[str, Any]], states: dict[str, dict[str, Any]]) -> str:
        blocks = []
        for asset in assets:
            reading = self.sources.kline_view(asset)
            news = self.sources.news(asset, limit=40)
            titles = [n["title"] for n in news.get("items") or [] if n.get("bucket") in {"high_impact", "watch"}][:12]
            blocks.append({
                "key": asset["key"], "name": asset["label"], "price": states[asset["key"]].get("price"),
                "kline_daily": {k: reading.get(k) for k in ("position", "structure", "synthesis", "odds", "periods")} if reading.get("ok") else None,
                "news_24h": titles,
            })
        three = [{"title": i.get("title"), "at": i.get("at"), "why": i.get("why")} for i in (self.watch.current().get("items") or [])]
        return PROMPT.format(now=datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M"),
                             three=json.dumps(three, ensure_ascii=False), assets=json.dumps(blocks, ensure_ascii=False))

    @staticmethod
    def parse(text: str, keys: set[str]) -> dict[str, dict[str, Any]]:
        match = re.search(r"\{.*\}", text or "", flags=re.S)
        if not match:
            return {}
        try:
            rows = json.loads(match.group(0)).get("assets") or []
        except (ValueError, AttributeError):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            key, direction = str((row or {}).get("key") or ""), str(row.get("direction") or "")
            reason = str(row.get("reason") or "").strip()
            if key not in keys or direction not in DIRECTIONS or not reason:
                continue
            try:
                confidence = min(5, max(1, int(row.get("confidence") or 3)))
            except (TypeError, ValueError):
                confidence = 3
            out[key] = {"direction": direction, "confidence": confidence, "reason": reason[:80]}
        return out


def _local_date(iso: str) -> str:
    return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone().date().isoformat()


PROMPT = """你是一名宏观交易员，现在是北京时间 {now}。为下面每个品种给出今天的方向判断，接下来 72 小时会按收盘价核对对错。

先看全市场：本周要看的三件事
{three}

各品种的输入（price 是现价；kline_daily 是当天 K 线日报的结构化解读；news_24h 是过去 24 小时相关的重要新闻标题）
{assets}

规则：
- direction 只能是 long（做多）、short（做空）或 flat（观望）。证据互相矛盾、或大事件落地前方向不明时选 flat。
- 72 小时内涨跌不到 0.5% 算没说中，所以不要为了表态而表态。
- confidence：1 到 5 的整数。
- reason：一句中文，不超过 50 个字，写出最关键的依据，不写仓位、价位或下单建议。
只输出 JSON：{{"assets":[{{"key":"BTC","direction":"long","confidence":3,"reason":"..."}}]}}"""
