from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from services.journal_store import load_json, write_json
from services.market_store import MarketStore


OBSIDIAN_DAILY_TRADE_ANALYSIS_DIR = Path("003_park原始输出") / "每日交易分析"


def direction_bias_from_score(score: int | float) -> dict:
    value = int(round(float(score)))
    if value < 0 or value > 100:
        raise ValueError("direction score must be between 0 and 100")
    if value <= 30:
        bias = "strong_short"
        allowed = ["short"]
        blocked = ["long"]
        stance = "只做空"
    elif value <= 39:
        bias = "short_bias"
        allowed = ["short", "long"]
        blocked = []
        stance = "偏空，做空优先，做多降权"
    elif value <= 60:
        bias = "neutral"
        allowed = ["long", "short"]
        blocked = []
        stance = "中性，多空都可做"
    elif value <= 69:
        bias = "long_bias"
        allowed = ["long", "short"]
        blocked = []
        stance = "偏多，做多优先，做空降权"
    else:
        bias = "strong_long"
        allowed = ["long"]
        blocked = ["short"]
        stance = "只做多"
    return {
        "score": value,
        "bias": bias,
        "allowed_directions": allowed,
        "blocked_directions": blocked,
        "stance": stance,
    }


class MarketViewStore:
    DEFAULT_VALID_FOR_HOURS = 12
    DEFAULT_PRICE_MOVE_EXPIRY_PCT = 1.0

    def __init__(self, output_root: Path, obsidian_root: Path | None = None) -> None:
        self.output_root = Path(output_root)
        self.obsidian_root = Path(obsidian_root) if obsidian_root else None

    def record(
        self,
        run_date: str,
        score: int | float,
        summary: str,
        raw_text: str = "",
        key_levels: Iterable[str] | None = None,
        timeframes: Iterable[str] | None = None,
        trade_plan: str = "",
        source: str = "Park口述",
        reference_price: float | None = None,
        valid_for_hours: float | None = None,
        expires_at: str = "",
        expires_if_price_moves_pct: float | None = None,
        expiry_target_price: float | None = None,
        expire_above: float | None = None,
        expire_below: float | None = None,
        write_obsidian: bool = False,
    ) -> dict:
        bias = direction_bias_from_score(score)
        generated_at = datetime.now(timezone.utc).replace(microsecond=0)
        valid_hours = self.DEFAULT_VALID_FOR_HOURS if valid_for_hours is None else float(valid_for_hours)
        expiry_ts = expires_at or (generated_at + timedelta(hours=valid_hours)).isoformat()
        move_expiry = (
            self.DEFAULT_PRICE_MOVE_EXPIRY_PCT
            if expires_if_price_moves_pct is None
            else float(expires_if_price_moves_pct)
        )
        target_price = float(expiry_target_price) if expiry_target_price is not None else None
        mapped_expire_above, mapped_expire_below, target_rule = market_view_target_expiry_bounds(
            {"direction_bias": bias["bias"], "expiry": {"target_price": target_price, "expire_above": expire_above, "expire_below": expire_below}}
        )
        payload = {
            "run_date": run_date,
            "generated_at": generated_at.isoformat(),
            "asset": "XAUUSD",
            "source": source,
            "direction_score": bias["score"],
            "direction_bias": bias["bias"],
            "stance": bias["stance"],
            "allowed_directions": bias["allowed_directions"],
            "blocked_directions": bias["blocked_directions"],
            "summary": summary.strip(),
            "key_levels": [str(item).strip() for item in (key_levels or []) if str(item).strip()],
            "timeframes": [str(item).strip() for item in (timeframes or []) if str(item).strip()],
            "trade_plan": trade_plan.strip(),
            "raw_text": raw_text.strip(),
            "reference_price": float(reference_price) if reference_price is not None else None,
            "expiry": {
                "status": "active",
                "valid_for_hours": valid_hours,
                "expires_at": expiry_ts,
                "expires_if_price_moves_pct": move_expiry,
                "target_price": target_price,
                "target_rule": target_rule,
                "expire_above": mapped_expire_above,
                "expire_below": mapped_expire_below,
                "policy": "time_or_price_condition_first",
                "reason": "",
            },
        }
        self._validate(payload)
        write_json(self.output_root / "market_views" / f"{run_date}.json", [payload])
        write_json(self.output_root / "market_views" / "current.json", [payload])
        md_path = self.output_root / "market_views" / f"{run_date}.md"
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(self._markdown(payload), encoding="utf-8")
        if write_obsidian:
            self.write_obsidian(payload)
        return payload

    def write_obsidian(self, payload: dict) -> Path:
        if not self.obsidian_root:
            raise ValueError("obsidian_root is required when write_obsidian=True")
        target_dir = self.obsidian_root / OBSIDIAN_DAILY_TRADE_ANALYSIS_DIR
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{payload['run_date']}.md"
        target.write_text(self._markdown(payload), encoding="utf-8")
        return target

    def latest(self, run_date: str) -> dict:
        dated = load_json(self.output_root / "market_views" / f"{run_date}.json")
        return dated[-1] if dated else {}

    def load_active(self, run_date: str, *, as_of: str | datetime | None = None) -> dict:
        payload = self.latest(run_date)
        if not payload:
            raise ValueError("market_view_missing_for_date")
        if str(payload.get("run_date") or "") != run_date:
            raise ValueError("market_view_date_mismatch")
        expiry = payload.get("expiry") if isinstance(payload.get("expiry"), dict) else {}
        if str(expiry.get("status") or "").lower() != "active":
            raise ValueError("market_view_expired")
        expires_at = str(expiry.get("expires_at") or "").strip()
        if not expires_at:
            raise ValueError("market_view_expiry_missing")
        try:
            expiry_time = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("market_view_expiry_invalid") from exc
        if expiry_time.tzinfo is None:
            expiry_time = expiry_time.replace(tzinfo=timezone.utc)
        now = _parse_datetime_utc(as_of)
        if now >= expiry_time.astimezone(timezone.utc):
            raise ValueError("market_view_expired")
        return payload

    def _validate(self, payload: dict) -> None:
        if payload["direction_score"] < 0 or payload["direction_score"] > 100:
            raise ValueError("direction_score must be 0-100")
        if not payload["summary"]:
            raise ValueError("summary is required")
        if len(payload["timeframes"]) < 1:
            raise ValueError("at least one timeframe is required")
        expiry = payload.get("expiry") or {}
        if expiry.get("valid_for_hours") is not None and float(expiry["valid_for_hours"]) <= 0:
            raise ValueError("valid_for_hours must be positive")
        if expiry.get("expires_if_price_moves_pct") is not None and float(expiry["expires_if_price_moves_pct"]) <= 0:
            raise ValueError("expires_if_price_moves_pct must be positive")

    def _markdown(self, payload: dict) -> str:
        key_levels = "\n".join(f"- {item}" for item in payload["key_levels"]) or "- n/a"
        timeframes = "\n".join(f"- {item}" for item in payload["timeframes"]) or "- n/a"
        raw = payload.get("raw_text") or "n/a"
        return (
            "---\n"
            f"title: {payload['run_date']} 黄金市场口述\n"
            f"created: {payload['run_date']}\n"
            f"asset: {payload['asset']}\n"
            f"source: {payload['source']}\n"
            f"direction_bias_score: {payload['direction_score']}\n"
            f"direction_bias: {payload['direction_bias']}\n"
            "---\n\n"
            f"# {payload['run_date']} 黄金市场口述\n\n"
            "## 方向\n\n"
            f"- 分数：{payload['direction_score']} / 100\n"
            f"- Bias：{payload['direction_bias']}\n"
            f"- 操作含义：{payload['stance']}\n\n"
            "## 过期条件\n\n"
            f"- 参考价格：{payload.get('reference_price') or 'n/a'}\n"
            f"- 时间过期：{payload.get('expiry', {}).get('expires_at') or 'n/a'}\n"
            f"- 价格波动过期：{payload.get('expiry', {}).get('expires_if_price_moves_pct') or 'n/a'}%\n"
            f"- 目标位过期：{payload.get('expiry', {}).get('target_price') or 'n/a'}"
            f"（{payload.get('expiry', {}).get('target_rule') or 'n/a'}）\n"
            f"- 上破失效：{payload.get('expiry', {}).get('expire_above') or 'n/a'}\n"
            f"- 下破失效：{payload.get('expiry', {}).get('expire_below') or 'n/a'}\n\n"
            "## 摘要\n\n"
            f"{payload['summary']}\n\n"
            "## 时间级别\n\n"
            f"{timeframes}\n\n"
            "## 关键价位\n\n"
            f"{key_levels}\n\n"
            "## 今日交易计划\n\n"
            f"{payload.get('trade_plan') or 'n/a'}\n\n"
            "## 原始口述\n\n"
            f"{raw}\n"
        )


def infer_market_view_reference_price(
    market_view: dict,
    market_db: Path | None,
    *,
    symbol: str = "GOLD",
) -> float | None:
    explicit = _safe_float((market_view.get("expiry") or {}).get("reference_price") or market_view.get("reference_price"))
    if explicit is not None:
        return explicit
    generated_at = str(market_view.get("generated_at") or "")
    if not generated_at or not market_db:
        return None
    db_path = Path(market_db)
    if not db_path.exists():
        return None
    try:
        store = MarketStore(db_path)
        for timeframe in ("1m", "5m"):
            row = store.load_bar_at_or_before(symbol, timeframe, generated_at)
            value = _safe_float(row.get("close") if row else None)
            if value is not None:
                return value
    except Exception:
        return None
    return None


def market_view_target_expiry_bounds(market_view: dict) -> tuple[float | None, float | None, str]:
    expiry = market_view.get("expiry") or {}
    expire_above = _safe_float(expiry.get("expire_above"))
    expire_below = _safe_float(expiry.get("expire_below"))
    target_price = _safe_float(expiry.get("target_price") or expiry.get("expiry_target_price"))
    bias = str(market_view.get("direction_bias") or market_view.get("stance") or "").lower()
    target_rule = str(expiry.get("target_rule") or "")
    if target_price is None:
        return expire_above, expire_below, target_rule
    if bias in {"strong_short", "short_bias", "short", "bearish", "只做空"}:
        if expire_below is None:
            expire_below = target_price
        target_rule = target_rule or "strong_or_short_bias_expires_when_price_reaches_or_breaks_target_below"
    elif bias in {"strong_long", "long_bias", "long", "bullish", "只做多"}:
        if expire_above is None:
            expire_above = target_price
        target_rule = target_rule or "strong_or_long_bias_expires_when_price_reaches_or_breaks_target_above"
    else:
        target_rule = target_rule or "target_price_recorded_without_directional_expiry_mapping"
    return expire_above, expire_below, target_rule


def _safe_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_datetime_utc(value: str | datetime | None) -> datetime:
    if value is None:
        parsed = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
