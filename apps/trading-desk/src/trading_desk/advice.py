"""AI 每日方向建议: one call per asset per day, proposed by the model and decided by Park.

At 08:40 Beijing time (and when Park presses 刷新) the model reads, per asset, the K-line daily
reading, the last 24 hours of related news and 本周要看的三件事, and proposes long / short /
wait with a one-line reason. Every call also stores what it was given (`evidence`: price and where it came from, the K-line reading,
the numbered news, 三件事), the input ids the model says it used (`cited`) and the numbers in its reason
that none of those inputs back (`evidence.unverified`); see AI 透明度 #1318. The suggestion is stored as a judgment with author='ai' and
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

from . import plans, provenance
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
            inputs = self.inputs(targets, states)
            parsed = self.parse(self.complete(self.prompt(inputs)), {a["key"] for a in targets},
                                {key: set(block["ids"]) for key, block in inputs["assets"].items()})
            if not parsed:
                return {"status": "model_unavailable"}
            saved = []
            for asset in targets:
                call = parsed.get(asset["key"])
                if not call:
                    continue
                state = states[asset["key"]]
                evidence = {**inputs["assets"][asset["key"]]["evidence"], "three": inputs["three"], "model": inputs["model"]}
                evidence["unverified"] = provenance.unverified(call["reason"], _content(evidence))
                venue = self.sources.venues.of(asset)
                plan = plans.build_plan(venue, call["direction"], state.get("price"), state.get("grid")) if venue.can("grid_plan") else None
                existing = self.today(asset["key"])
                fields = dict(direction=call["direction"], confidence=call["confidence"], reason=call["reason"], cited=call["cited"],
                              price_at=state.get("price"), plan=plan, action="recorded", evidence=evidence)
                saved.append(self.store.revise_judgment(existing["id"], **fields) if existing else self.store.add_judgment(asset=asset["key"], author="ai", **fields))
            return {"status": "ok", "saved": [{"asset": j["asset"], "direction": j["direction"]} for j in saved]}
        finally:
            _RUN_LOCK.release()

    def inputs(self, assets: list[dict[str, Any]], states: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """What the model is shown, numbered so it can say what it used. The same dict is stored as evidence."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        three = [{"id": f"w{i}", "title": item.get("title"), "at": item.get("at"), "why": item.get("why")}
                 for i, item in enumerate(self.watch.current().get("items") or [], 1)]
        out: dict[str, Any] = {}
        for asset in assets:
            state = states[asset["key"]]
            reading = self.sources.kline_view(asset)
            feed = self.sources.news(asset, limit=40)
            picked = [n for n in feed.get("items") or [] if n.get("bucket") in {"high_impact", "watch"}][:12]
            news = [{"id": f"n{i}", "intel_id": n.get("id"), "title": n.get("title"), "source": n.get("source"), "url": n.get("url"),
                     "bucket": n.get("bucket"), "collected_at": n.get("collected_at")} for i, n in enumerate(picked, 1)]
            kline = ({"id": "kline", "date": reading.get("date"), "cutoff_at": reading.get("cutoff_at"),
                      **{k: reading.get(k) for k in ("position", "structure", "synthesis", "odds", "periods")}}
                     if reading.get("ok") else {"id": "kline", "missing": reading.get("reason") or "读不到 K 线日报"})
            price = {"id": "price", "value": state.get("price"), "source": state.get("price_source") or ("none" if state.get("price") is None else "unknown"), "at": now}
            evidence = {"price": price, "kline": kline, "news": news, "news_stale": bool(feed.get("stale")),
                        **({"news_missing": feed.get("reason") or "新闻读不到"} if not feed.get("ok", True) and not news else {})}
            ids = ["price"] + (["kline"] if reading.get("ok") else []) + [n["id"] for n in news] + [w["id"] for w in three]
            out[asset["key"]] = {"name": asset["label"], "evidence": evidence, "ids": ids}
        return {"assets": out, "three": three, "model": {"channel": "codex", "asked_at": now}}

    def prompt(self, inputs: dict[str, Any]) -> str:
        blocks = []
        for key, block in inputs["assets"].items():
            ev = block["evidence"]
            kline = {k: v for k, v in ev["kline"].items() if k not in {"date", "cutoff_at"}} if "missing" not in ev["kline"] else None
            blocks.append({"key": key, "name": block["name"], "price": {"id": "price", "value": ev["price"]["value"]},
                           "kline_daily": kline, "news_24h": [{"id": n["id"], "title": n["title"]} for n in ev["news"]]})
        three = [{k: w[k] for k in ("id", "title", "at", "why")} for w in inputs["three"]]
        return PROMPT.format(now=datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M"),
                             three=json.dumps(three, ensure_ascii=False), assets=json.dumps(blocks, ensure_ascii=False))

    @staticmethod
    def parse(text: str, keys: set[str], allowed: dict[str, set[str]] | None = None) -> dict[str, dict[str, Any]]:
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
            valid = (allowed or {}).get(key, set())
            raw = row.get("cited") if isinstance(row.get("cited"), list) else []
            cited = list(dict.fromkeys(str(ref) for ref in raw if str(ref) in valid))  # only ids it was actually shown
            out[key] = {"direction": direction, "confidence": confidence, "reason": reason[:80], "cited": cited}
        return out


def _content(evidence: dict[str, Any]) -> list[str]:
    """The text the model read, without timestamps, ids or URLs: what a number in its reason may come from."""
    kline = evidence.get("kline") or {}
    parts = [str((evidence.get("price") or {}).get("value") or "")]
    parts += [json.dumps(kline.get(k), ensure_ascii=False) for k in ("position", "structure", "synthesis", "odds", "periods") if kline.get(k)]
    parts += [str(n.get("title") or "") for n in evidence.get("news") or []]
    parts += [f"{w.get('title') or ''} {w.get('why') or ''}" for w in evidence.get("three") or []]
    return parts


def _local_date(iso: str) -> str:
    return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone().date().isoformat()


PROMPT = """你是一名宏观交易员，现在是北京时间 {now}。为下面每个品种给出今天的方向判断，接下来 72 小时会按收盘价核对对错。

先看全市场：本周要看的三件事
{three}

各品种的输入（price 是现价，value 为 null 表示读不到；kline_daily 是当天 K 线日报的结构化解读；news_24h 是过去 24 小时相关的重要新闻标题）。每条输入都有 id。
{assets}

规则：
- direction 只能是 long（做多）、short（做空）或 flat（观望）。证据互相矛盾、或大事件落地前方向不明时选 flat。
- 72 小时内涨跌不到 0.5% 算没说中，所以不要为了表态而表态。
- confidence：1 到 5 的整数。
- reason：一句中文，不超过 50 个字，写出最关键的依据，不写仓位、价位或下单建议。
- cited：你这条判断真正依据的输入 id 列表（如 "price"、"kline"、"n3"、"w1"），只写上面出现过的 id。
只输出 JSON：{{"assets":[{{"key":"BTC","direction":"long","confidence":3,"reason":"...","cited":["kline","n2"]}}]}}"""
