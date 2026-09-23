"""本周要看的三件事: the three things with the biggest impact on the whole market right now.

Inputs are this week's economic calendar (Forex Factory's public weekly feed) and the
high-impact flashes from Intel's realtime news. A model ranks them by impact on the market as
a whole, not on any one asset, and writes one line on why each matters. Times, titles of the
source and the candidate set always come from the inputs; the model only chooses and explains.
When the model is unavailable the list is built by rules and says so.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.request import Request, urlopen

BJT = timezone(timedelta(hours=8))
CALENDAR_URLS = ("https://nfs.faireconomy.media/ff_calendar_thisweek.json", "https://nfs.faireconomy.media/ff_calendar_nextweek.json")
CALENDAR_TTL_SECONDS = 2 * 3600  # the feed asks clients not to poll it more than a few times an hour
INTEL_REALTIME = "/api/ui/realtime?window=24h&limit=200"
MARKETS = ("美股", "美债", "美元", "欧洲", "日本", "中国", "港股", "黄金", "原油", "商品", "加密", "汇率")
CODEX_BIN = Path("/opt/homebrew/bin/codex")
BREAKING_REPICK_SECONDS = 3600  # a new flash re-ranks the list at most once an hour
PAST_GRACE = timedelta(hours=2)  # an event stays on the list for two hours after it is released

# Rules fallback: how much a scheduled release moves the whole market.
EVENT_WEIGHT = (
    (r"FOMC|Federal Funds Rate", 100, "美联储利率决议"),
    (r"Non-Farm|Nonfarm", 90, "美国非农就业"),
    (r"^CPI|Core CPI", 85, "CPI 通胀"),
    (r"Core PCE|PCE Price", 80, "PCE 通胀"),
    (r"Main Refinancing Rate|ECB", 70, "欧洲央行利率决议"),
    (r"BOJ|Policy Rate", 70, "日本央行利率决议"),
    (r"GDP", 55, "GDP"),
    (r"Retail Sales", 50, "零售销售"),
    (r"ISM|PMI", 45, "PMI"),
    (r"Unemployment|Claimant", 40, "就业"),
)
COUNTRY = {"USD": "美国", "EUR": "欧元区", "GBP": "英国", "JPY": "日本", "CNY": "中国", "CAD": "加拿大", "AUD": "澳大利亚", "NZD": "新西兰", "CHF": "瑞士"}
COUNTRY_WEIGHT = {"USD": 1.0, "CNY": 0.7, "EUR": 0.7, "JPY": 0.6, "GBP": 0.45}


@dataclass
class Candidate:
    ref: str
    kind: str  # "event" | "breaking"
    title: str
    at: str  # ISO time in Beijing time
    detail: str
    weight: float


def http_json(url: str, timeout: float = 20) -> Any:
    request = Request(url, headers={"User-Agent": "park-trading-desk/1.0"})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def codex_complete(prompt: str, timeout: int = 180) -> str:
    """Non-interactive Codex call with a closed stdin and an empty cwd (otherwise it waits forever)."""
    if not CODEX_BIN.exists():
        return ""
    with tempfile.TemporaryDirectory(prefix="desk-watch-") as scratch:
        out = Path(scratch) / "answer.txt"
        cmd = [str(CODEX_BIN), "exec", "--skip-git-repo-check", "-s", "read-only", "-c", 'model_reasoning_effort="low"', "-o", str(out), prompt]
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=scratch, check=False,
                           stdin=subprocess.DEVNULL, env={**os.environ, "CODEX_NOTIFY_DISABLE": "1"})
        except subprocess.TimeoutExpired:
            return ""
        return out.read_text(encoding="utf-8").strip() if out.exists() else ""


class Watch:
    def __init__(self, folder: Path, intel_url: str, *, fetch: Callable[[str], Any] = http_json,
                 complete: Callable[[str], str] = codex_complete, now: Callable[[], datetime] | None = None) -> None:
        self.folder = folder
        self.intel_url = intel_url.rstrip("/")
        self.fetch = fetch
        self.complete = complete
        self.now = now or (lambda: datetime.now(timezone.utc))

    # ---- inputs ----------------------------------------------------------------------
    def calendar(self) -> list[dict[str, Any]]:
        cache = self.folder / "calendar.json"
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
            if time.time() - cached["fetched_at"] < CALENDAR_TTL_SECONDS:
                return cached["events"]
        except (OSError, ValueError, KeyError):
            cached = None
        this_week, next_week = CALENDAR_URLS
        try:
            events = [row for row in self.fetch(this_week) or [] if isinstance(row, dict)]
        except Exception:  # noqa: BLE001 - rate limited or down: keep the last good calendar
            return cached["events"] if cached else []
        try:  # published only late in the week; a 404 before that is normal
            events.extend(row for row in self.fetch(next_week) or [] if isinstance(row, dict))
        except Exception:  # noqa: BLE001
            pass
        self.folder.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"fetched_at": time.time(), "events": events}, ensure_ascii=False), encoding="utf-8")
        return events

    def candidates(self) -> tuple[list[Candidate], list[Candidate]]:
        now = self.now()
        events: list[Candidate] = []
        seen: set[str] = set()
        for row in self.calendar():
            if row.get("impact") != "High":
                continue
            try:
                at = datetime.fromisoformat(str(row["date"])).astimezone(BJT)
            except (KeyError, ValueError):
                continue
            if not (now - PAST_GRACE <= at <= now + timedelta(days=7)):
                continue
            country, title = str(row.get("country") or ""), str(row.get("title") or "")
            key = f"{country}:{title}:{at:%Y%m%d%H%M}"
            if key in seen:
                continue
            seen.add(key)
            base = next((w for pattern, w, _ in EVENT_WEIGHT if re.search(pattern, title, re.I)), 30)
            detail = " · ".join(part for part in (f"预期 {row['forecast']}" if row.get("forecast") else "", f"前值 {row['previous']}" if row.get("previous") else "") if part)
            events.append(Candidate("event:" + hashlib.sha1(key.encode()).hexdigest()[:10], "event", f"{COUNTRY.get(country, country)} {title}",
                                    at.isoformat(), detail, base * COUNTRY_WEIGHT.get(country, 0.3)))
        breaking: list[Candidate] = []
        try:
            feed = self.fetch(f"{self.intel_url}{INTEL_REALTIME}")
        except Exception:  # noqa: BLE001 - calendar alone still makes a list
            feed = {}
        titles: set[str] = set()
        for row in (feed or {}).get("items") or []:
            triage = row.get("triage") if isinstance(row.get("triage"), dict) else {}
            if row.get("source") == "reddit" or (triage.get("bucket") or row.get("bucket")) != "high_impact":
                continue
            title = re.sub(r"^(财联社)?\d{1,2}月\d{1,2}日电[，,]\s*", "", str(row.get("title") or "")).strip()
            if not title or title[:18] in titles:
                continue
            titles.add(title[:18])
            stamp = str(row.get("collected_at") or row.get("published_at") or "")
            try:
                at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                at = (at if at.tzinfo else at.replace(tzinfo=timezone.utc)).astimezone(BJT)
            except ValueError:
                continue
            breaking.append(Candidate(f"news:{row.get('id')}", "breaking", title, at.isoformat(), str(row.get("source") or ""), 20))
        events.sort(key=lambda c: c.at)
        breaking.sort(key=lambda c: c.at, reverse=True)
        return events[:25], breaking[:25]

    # ---- ranking ---------------------------------------------------------------------
    def rank(self, events: list[Candidate], breaking: list[Candidate]) -> dict[str, Any]:
        pool = {c.ref: c for c in events + breaking}
        if not pool:
            return {"provider": "none", "items": []}
        prompt = PROMPT.format(now=self.now().astimezone(BJT).strftime("%Y-%m-%d %H:%M"), markets="、".join(MARKETS),
                               events=json.dumps([c.__dict__ for c in events], ensure_ascii=False),
                               breaking=json.dumps([c.__dict__ for c in breaking], ensure_ascii=False))
        items = self._parse(self.complete(prompt), pool)
        if len(items) >= min(3, len(pool)):
            return {"provider": "codex", "items": items[:3]}
        return {"provider": "rules", "items": self._rules(events, breaking)}

    def _parse(self, text: str, pool: dict[str, Candidate]) -> list[dict[str, Any]]:
        match = re.search(r"\{.*\}", text or "", flags=re.S)
        if not match:
            return []
        try:
            rows = json.loads(match.group(0)).get("items") or []
        except (ValueError, AttributeError):
            return []
        items, used = [], set()
        for row in rows:
            ref = str((row or {}).get("ref") or "")
            source = pool.get(ref)
            if not source or ref in used:
                continue  # the model may only choose among the inputs
            used.add(ref)
            markets = [m for m in (row.get("markets") or []) if m in MARKETS][:5]
            items.append(self._item(source, title=str(row.get("title") or "").strip()[:40] or source.title, markets=markets,
                                    why=str(row.get("why") or "").strip()[:120], volatility="高" if row.get("volatility") == "高" else "中"))
        return items

    def _rules(self, events: list[Candidate], breaking: list[Candidate]) -> list[dict[str, Any]]:
        ranked = sorted(events, key=lambda c: -c.weight)
        chosen: list[Candidate] = []
        for candidate in ranked:  # one release is one thing: FOMC rate, statement, projections and press conference share a slot
            if all(not _same_release(candidate, c) for c in chosen):
                chosen.append(candidate)
            if len(chosen) == 3:
                break
        if len(chosen) < 3:
            chosen.extend(breaking[: 3 - len(chosen)])
        return [self._item(c, title=self._rule_title(c), markets=[], why="", volatility="高" if c.weight >= 60 else "中") for c in chosen]

    @staticmethod
    def _rule_title(candidate: Candidate) -> str:
        if candidate.kind != "event":
            return candidate.title[:40]
        country, raw = candidate.title.split(" ", 1) if " " in candidate.title else ("", candidate.title)
        name = next((label for pattern, _, label in EVENT_WEIGHT if re.search(pattern, raw, re.I)), raw)
        return name if name.startswith(("美联储", "欧洲央行", "日本央行")) else f"{country} {name}"

    @staticmethod
    def _item(source: Candidate, *, title: str, markets: list[str], why: str, volatility: str) -> dict[str, Any]:
        return {"ref": source.ref, "kind": source.kind, "title": title, "source_title": source.title, "at": source.at,
                "detail": source.detail, "markets": markets, "why": why, "volatility": volatility}

    # ---- refresh + storage -------------------------------------------------------------
    def current(self) -> dict[str, Any]:
        try:
            return json.loads((self.folder / "current.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def refresh(self, *, force: bool = False) -> dict[str, Any]:
        """Full re-rank when forced, on the first run after 08:30 Beijing time, or when the list is empty or an
        event on it is long past; otherwise only when a new
        high-impact flash appeared, at most once an hour. Returns the list with `replaced` refs that are new."""
        previous = self.current()
        events, breaking = self.candidates()
        now = self.now()
        generated = _parse_time(previous.get("generated_at"))
        seen = set(previous.get("breaking_seen") or [])
        new_flashes = [c.ref for c in breaking if c.ref not in seen]
        stale = not generated or now - generated > timedelta(hours=24) or any(
            (_parse_time(item.get("at")) or now) < now - PAST_GRACE for item in previous.get("items") or [] if item.get("kind") == "event")
        morning = now.astimezone(BJT).replace(hour=8, minute=30, second=0, microsecond=0)
        missed_morning = now >= morning and (not generated or generated < morning)
        due = force or not previous.get("items") or stale or missed_morning or (new_flashes and (not generated or (now - generated).total_seconds() >= BREAKING_REPICK_SECONDS))
        if not due:
            return previous
        ranked = self.rank(events, breaking)
        old_refs = {item["ref"] for item in previous.get("items") or []}
        payload = {
            "generated_at": now.astimezone(BJT).isoformat(timespec="seconds"),
            "provider": ranked["provider"],
            "items": ranked["items"],
            "replaced": [item["ref"] for item in ranked["items"] if item["ref"] not in old_refs and previous.get("items")],
            "breaking_seen": sorted(c.ref for c in breaking),
            "calendar_events": len(events),
            "breaking_candidates": len(breaking),
        }
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = self.folder / "current.json.tmp"
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.folder / "current.json")
        with (self.folder / "history.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({key: payload[key] for key in ("generated_at", "provider", "items", "replaced")}, ensure_ascii=False) + "\n")
        return payload


WEEKDAY = "一二三四五六日"


def when_label(item: dict[str, Any], now: datetime) -> str:
    """周三 02:00 · 还有 1 天 11 小时 / 已公布 / 突发 13:09 — Beijing time."""
    at = _parse_time(item.get("at"))
    if at is None:
        return ""
    at = at.astimezone(BJT)
    if item.get("kind") == "breaking":
        return f"突发 · {at:%m-%d %H:%M}" if at.date() != now.astimezone(BJT).date() else f"突发 · {at:%H:%M}"
    stamp = f"周{WEEKDAY[at.weekday()]} {at:%H:%M}"
    delta = at - now
    if delta.total_seconds() <= 0:
        return f"{stamp} · 已公布"
    hours = int(delta.total_seconds() // 3600)
    left = f"还有 {hours // 24} 天 {hours % 24} 小时" if hours >= 24 else f"还有 {hours} 小时 {int(delta.total_seconds() // 60) % 60} 分" if hours else f"还有 {int(delta.total_seconds() // 60)} 分钟"
    return f"{stamp} · {left}"


def _same_release(a: Candidate, b: Candidate) -> bool:
    if a.kind != "event" or b.kind != "event" or a.title.split(" ")[0] != b.title.split(" ")[0]:
        return False
    return abs(datetime.fromisoformat(a.at) - datetime.fromisoformat(b.at)) <= timedelta(minutes=90)


def _parse_time(value: Any) -> datetime | None:
    try:
        at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=BJT)


PROMPT = """你是宏观交易员的助手。现在是北京时间 {now}。
从下面两类候选里，选出接下来对【整个金融市场】冲击最大的三件事，按冲击从大到小排序。不要针对任何单一资产，要看全市场：股、债、汇、商品、加密一起考虑。

候选一：本周经济日历（时间已是北京时间，detail 里有预期值和前值）
{events}

候选二：过去 24 小时的重要突发新闻
{breaking}

规则：
- 只能从候选里选，用候选的 ref 字段引用；同一时间发布的同一组数据（例如 FOMC 利率、声明、点阵图）只算一件，选最核心的那条。
- title：不超过 18 个字的中文事件名。
- markets：会被波及的市场，只能从这些里选，最多 5 个：{markets}。
- why：一句中文，不超过 60 个字，说清楚市场在押注什么、结果偏离时会怎样。不给买卖建议。
- volatility：只能是 "高" 或 "中"。
只输出 JSON：{{"items":[{{"ref":"...","title":"...","markets":["..."],"why":"...","volatility":"高"}}]}}"""
