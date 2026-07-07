from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from services.journal_store import write_json
from services.bias_ledger import BiasLedger
from services.market_view import MarketViewStore


BEIJING = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class MarketViewDraft:
    run_date: str
    score: int
    summary: str
    raw_text: str
    key_levels: list[str]
    timeframes: list[str]
    trade_plan: str
    valid_for_hours: float | None = None
    expires_at: str = ""
    expires_if_price_moves_pct: float | None = None
    expiry_target_price: float | None = None
    expire_above: float | None = None
    expire_below: float | None = None
    confidence: str = "heuristic"

    def to_record_kwargs(self) -> dict:
        return {
            "run_date": self.run_date,
            "score": self.score,
            "summary": self.summary,
            "raw_text": self.raw_text,
            "key_levels": self.key_levels,
            "timeframes": self.timeframes,
            "trade_plan": self.trade_plan,
            "valid_for_hours": self.valid_for_hours,
            "expires_at": self.expires_at,
            "expires_if_price_moves_pct": self.expires_if_price_moves_pct,
            "expiry_target_price": self.expiry_target_price,
            "expire_above": self.expire_above,
            "expire_below": self.expire_below,
        }


class MarketViewIntake:
    """Convert Park's oral market view into the existing MarketViewStore contract.

    This parser is intentionally conservative. It extracts obvious structured
    fields and keeps the full raw text in the artifact so a human can override
    any ambiguous interpretation.
    """

    def __init__(
        self,
        output_root: Path,
        obsidian_root: Path | None = None,
        market_db: Path | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.store = MarketViewStore(output_root, obsidian_root=obsidian_root)
        self.bias_ledger = BiasLedger(self.output_root, market_db)

    def draft(self, run_date: str, raw_text: str) -> MarketViewDraft:
        text = _normalize(raw_text)
        score = _extract_score(text)
        return MarketViewDraft(
            run_date=run_date,
            score=score,
            summary=_extract_summary(text),
            raw_text=raw_text.strip(),
            key_levels=_extract_key_levels(text),
            timeframes=_extract_timeframes(text),
            trade_plan=_extract_trade_plan(text),
            valid_for_hours=_extract_valid_for_hours(text),
            expires_at=_extract_expires_at(run_date, text),
            expires_if_price_moves_pct=_extract_move_pct(text),
            expiry_target_price=_extract_target_price(text, score),
            expire_above=_extract_bound(text, "above"),
            expire_below=_extract_bound(text, "below"),
        )

    def record(
        self,
        run_date: str,
        raw_text: str,
        *,
        write_obsidian: bool = False,
        as_of: str | datetime | None = None,
    ) -> dict:
        self.bias_ledger.guard_before_record(as_of=as_of)
        draft = self.draft(run_date, raw_text)
        payload = self.store.record(**draft.to_record_kwargs(), write_obsidian=write_obsidian)
        ledger_entry = self.bias_ledger.append_open_view(payload)
        payload["bias_ledger"] = {
            "view_id": ledger_entry["view_id"],
            "status": ledger_entry["status"],
            "pending_reason": ledger_entry["pending_reason"],
            "price_at_issue": ledger_entry["price_at_issue"],
            "expires_at": ledger_entry["expires_at"],
        }
        payload["intake"] = {
            "parser": "market_view_intake_v1",
            "confidence": draft.confidence,
            "extracted": {
                "score": draft.score,
                "timeframes": draft.timeframes,
                "key_level_count": len(draft.key_levels),
                "expiry_target_price": draft.expiry_target_price,
                "valid_for_hours": draft.valid_for_hours,
                "expires_at": draft.expires_at,
                "expires_if_price_moves_pct": draft.expires_if_price_moves_pct,
                "expire_above": draft.expire_above,
                "expire_below": draft.expire_below,
            },
        }
        write_json(self.output_root / "market_views" / f"{draft.run_date}.json", [payload])
        write_json(self.output_root / "market_views" / "current.json", [payload])
        return payload


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _extract_score(text: str) -> int:
    explicit = re.search(r"(?:方向|大方向|方向分|分数|score)[^\d]{0,8}(\d{1,3})\s*(?:分|/100)?", text, re.I)
    if explicit:
        return _clamp_score(explicit.group(1))
    ten_point = re.search(r"([一二三四五六七八九十]{1,3}|\d{1,2})\s*分(?:的)?(做空|空|看空|强空|做多|多|看多|强多)", text)
    if ten_point:
        value = _chinese_or_int(ten_point.group(1))
        direction = ten_point.group(2)
        if value is not None and value <= 10:
            if value == 10 and direction in {"做空", "空", "看空", "强空"}:
                return 10
            if value == 10 and direction in {"做多", "多", "看多", "强多"}:
                return 90
            return _clamp_score(value * 10)
    lowered = text.lower()
    if any(token in lowered for token in ["强烈做空", "强空", "只做空", "strong short"]):
        return 10
    if any(token in lowered for token in ["偏空", "做空优先", "bearish"]):
        return 35
    if any(token in lowered for token in ["强烈做多", "强多", "只做多", "strong long"]):
        return 90
    if any(token in lowered for token in ["偏多", "做多优先", "bullish"]):
        return 65
    return 50


def _extract_summary(text: str) -> str:
    pieces = _sentences(text)
    useful = [item for item in pieces if len(item) >= 8]
    return " ".join(useful[:3]).strip() or text[:180].strip() or "未提供摘要"


def _extract_timeframes(text: str) -> list[str]:
    candidates = [
        ("3D", [r"\b3d\b", r"三日线", r"3\s*日线"]),
        ("1D", [r"\b1d\b", r"日线"]),
        ("4H", [r"\b4h\b", r"4\s*小时", r"四小时"]),
        ("15m", [r"\b15m\b", r"15\s*分钟", r"十五分钟"]),
        ("5m", [r"\b5m\b", r"5\s*分钟", r"五分钟"]),
        ("1m", [r"\b1m\b", r"1\s*分钟", r"一分钟"]),
    ]
    found: list[str] = []
    for label, patterns in candidates:
        if any(re.search(pattern, text, re.I) for pattern in patterns):
            found.append(label)
    return found or ["1D"]


def _extract_key_levels(text: str) -> list[str]:
    levels: list[str] = []
    keywords = ["目标位", "目标价", "目标", "平台", "支撑", "压力", "前低", "前高", "上破", "突破", "站上", "下破", "跌破", "失守"]
    for match in re.finditer(r"(?<![\d.])([1-6]\d{3}(?:\.\d+)?)(?![\d.])", text):
        start = max(0, match.start() - 18)
        end = min(len(text), match.end() + 18)
        context = text[start:end].strip(" ，。；;：:")
        keyword = _grouped_level_keyword(text, match.end()) or _nearest_keyword(text, match.start(), match.end(), keywords, start, end)
        label = f"{match.group(1)} {keyword}".strip() if keyword else context or f"price {match.group(1)}"
        if label not in levels:
            levels.append(label)
        if len(levels) >= 12:
            break
    return levels


def _grouped_level_keyword(text: str, price_end: int) -> str:
    match = re.match(r"\s*/\s*[1-6]\d{3}(?:\.\d+)?\s*(前低|前高|支撑|压力|平台)", text[price_end : price_end + 24])
    return match.group(1) if match else ""


def _nearest_keyword(text: str, price_start: int, price_end: int, keywords: list[str], start: int, end: int) -> str:
    best_keyword = ""
    best_distance: int | None = None
    window = text[start:end]
    for keyword in keywords:
        for match in re.finditer(re.escape(keyword), window):
            absolute = start + match.start()
            distance = min(abs(absolute - price_start), abs(absolute - price_end))
            if best_distance is None or distance < best_distance:
                best_distance = distance
                best_keyword = keyword
    return best_keyword


def _extract_trade_plan(text: str) -> str:
    sentences = _sentences(text)
    keywords = ["计划", "只做", "入场", "止损", "止盈", "tp", "sl", "ema50", "信号", "反弹", "回踩"]
    selected = [item for item in sentences if any(keyword in item.lower() for keyword in keywords)]
    return " ".join(selected[:4]).strip() or "按口述方向和关键位置等待小级别信号。"


def _extract_valid_for_hours(text: str) -> float | None:
    match = re.search(r"(?:有效|valid)[^\d]{0,8}(\d+(?:\.\d+)?)\s*(?:小时|h|hours?)", text, re.I)
    return float(match.group(1)) if match else None


def _extract_expires_at(run_date: str, text: str) -> str:
    match = re.search(r"(?:有效到|到|expires?\s*at)\s*(\d{1,2})[:：点](\d{2})?", text, re.I)
    if not match:
        return ""
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    local = datetime.fromisoformat(run_date).replace(hour=hour, minute=minute, tzinfo=BEIJING)
    return local.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _extract_move_pct(text: str) -> float | None:
    match = re.search(r"(?:偏离|移动|波动|move)[^\d%]{0,8}(\d+(?:\.\d+)?)\s*%", text, re.I)
    return float(match.group(1)) if match else None


def _extract_target_price(text: str, score: int) -> float | None:
    patterns = [
        r"(?:目标位|目标价|目标|target)[^\d]{0,8}([1-6]\d{3}(?:\.\d+)?)",
        r"(?:打到|跌到|涨到|看到|看向)[^\d]{0,8}([1-6]\d{3}(?:\.\d+)?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return float(match.group(1))
    if score <= 30:
        supports = _prices_after_keywords(text, ["支撑", "前低"])
        return min(supports) if supports else None
    if score >= 70:
        resistances = _prices_after_keywords(text, ["压力", "平台", "前高"])
        return max(resistances) if resistances else None
    return None


def _extract_bound(text: str, side: str) -> float | None:
    if side == "above":
        pattern = r"(?:上破|突破|站上)[^\d]{0,8}([1-6]\d{3}(?:\.\d+)?)"
    else:
        pattern = r"(?:下破|跌破|失守)[^\d]{0,8}([1-6]\d{3}(?:\.\d+)?)"
    match = re.search(pattern, text)
    return float(match.group(1)) if match else None


def _prices_after_keywords(text: str, keywords: list[str]) -> list[float]:
    values: list[float] = []
    for keyword in keywords:
        for match in re.finditer(rf"{keyword}[^\d]{{0,12}}([1-6]\d{{3}}(?:\.\d+)?)", text):
            values.append(float(match.group(1)))
    return values


def _sentences(text: str) -> list[str]:
    return [item.strip() for item in re.split(r"[。！？!?]\s*", text) if item.strip()]


def _clamp_score(value: object) -> int:
    return max(0, min(100, int(round(float(value)))))


def _chinese_or_int(value: str) -> int | None:
    if value.isdigit():
        return int(value)
    table = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    if value == "十":
        return 10
    if value.endswith("十"):
        return table.get(value[0], 0) * 10
    if "十" in value:
        left, right = value.split("十", 1)
        return (table.get(left, 1) * 10) + table.get(right, 0)
    return table.get(value)
