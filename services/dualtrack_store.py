from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.config_loader import ROOT, load_pipeline_config
from services.dualtrack_clock import cycle_window_from_id, parse_utc
from services.dualtrack_config import dualtrack_config
from services.journal_store import load_json, write_json
from services.market_view import MarketViewStore

PLAN_DIRECTIONS = {"long", "short", "flat"}
PLAN_AUTHORS = {"human", "ai"}
PLAN_STATUSES = {"draft", "locked", "fallback_active", "absent"}
INVALIDATION_SIDES = {"below", "above"}
INVALIDATION_SIDE_ALIASES = {"跌破": "below", "升破": "above"}
INVALIDATION_CONFIRMS = {"close_1m", "touch"}
INVALIDATION_CONFIRM_ALIASES = {"1m收盘": "close_1m", "1m 收盘": "close_1m", "触及": "touch"}
_NUMBER_PATTERN = re.compile(r"[-+]?\d+(?:,\d{3})*(?:\.\d+)?")


class DualTrackPlanStore:
    def __init__(self, output_root: Path | None = None, *, config: dict[str, Any] | None = None) -> None:
        pipeline_config = load_pipeline_config()
        self.output_root = Path(output_root) if output_root else ROOT / pipeline_config.get("output_root", "outputs")
        self.root = self.output_root / "dualtrack"
        self.config = config or dualtrack_config()

    def load_plan(self, cycle_id: str, author: str) -> dict[str, Any] | None:
        if author not in PLAN_AUTHORS:
            raise ValueError("author must be human or ai")
        rows = load_json(self._plan_path(cycle_id, author))
        return rows[-1] if rows else None

    def save_human_plan(self, payload: dict[str, Any], *, now: str | datetime | None = None, lock: bool = True) -> dict[str, Any]:
        cycle_id = str(payload.get("cycle_id") or "")
        existing = self.load_plan(cycle_id, "human") if cycle_id else None
        if existing and existing.get("status") == "locked":
            self.audit(cycle_id, "locked_mutation_rejected", {"author": "human"})
            raise ValueError("human plan is locked and immutable")
        status = "locked" if lock else "draft"
        plan = validate_plan(payload, author="human", status=status, now=now)
        write_json(self._plan_path(plan["cycle_id"], "human"), [plan])
        self.audit(plan["cycle_id"], "human_plan_locked" if lock else "human_plan_drafted", {"author": "human"})
        return plan

    def save_ai_plan(self, plan: dict[str, Any], *, now: str | datetime | None = None) -> dict[str, Any]:
        normalized = validate_plan(plan, author="ai", status=plan.get("status") or "fallback_active", now=now)
        write_json(self._plan_path(normalized["cycle_id"], "ai"), [normalized])
        self.audit(normalized["cycle_id"], "ai_plan_ingested", {"author": "ai"})
        return normalized

    def ensure_ai_plan(
        self,
        cycle_id: str,
        *,
        cycle_open: float,
        prev_cycle_range: float,
        now: str | datetime | None = None,
    ) -> dict[str, Any] | None:
        existing = self.load_plan(cycle_id, "ai")
        if existing:
            return existing
        run_date = cycle_id.split("_", 1)[0]
        try:
            view = MarketViewStore(self.output_root).latest(run_date)
        except Exception as exc:  # noqa: BLE001 - unreadable AI source must fail closed.
            self.audit(cycle_id, "ai_plan_ingest_failed", {"reason": str(exc)})
            return None
        if not view:
            self.audit(cycle_id, "ai_plan_absent", {"reason": "market_view_missing"})
            return None
        plan = self._plan_from_market_view(
            cycle_id,
            view,
            author="ai",
            status="fallback_active",
            cycle_open=float(cycle_open),
            prev_cycle_range=float(prev_cycle_range),
            now=now,
        )
        if plan is None:
            self.audit(cycle_id, "ai_plan_absent", {"reason": "market_view_not_directional"})
            return None
        return self.save_ai_plan(plan, now=now)

    def ensure_human_plan_from_market_view(
        self,
        cycle_id: str,
        *,
        cycle_open: float,
        prev_cycle_range: float,
        now: str | datetime | None = None,
    ) -> dict[str, Any] | None:
        existing = self.load_plan(cycle_id, "human")
        if existing and existing.get("status") == "locked":
            return existing
        window = self._window(cycle_id)
        status = "locked" if parse_utc(now) <= window.lock_deadline else "draft"
        run_date = cycle_id.split("_", 1)[0]
        try:
            view = MarketViewStore(self.output_root).latest(run_date)
        except Exception as exc:  # noqa: BLE001 - unreadable operator source must fail closed.
            self.audit(cycle_id, "human_plan_import_failed", {"reason": str(exc), "source": "obsidian"})
            return None
        if not view:
            self.audit(cycle_id, "human_plan_absent", {"reason": "market_view_missing", "source": "obsidian"})
            return None
        plan = self._plan_from_market_view(
            cycle_id,
            view,
            author="human",
            status=status,
            cycle_open=float(cycle_open),
            prev_cycle_range=float(prev_cycle_range),
            now=now,
        )
        if plan is None:
            self.audit(cycle_id, "human_plan_absent", {"reason": "market_view_not_directional", "source": "obsidian"})
            return None
        normalized = validate_plan(plan, author="human", status=status, now=now)
        write_json(self._plan_path(cycle_id, "human"), [normalized])
        self.audit(
            cycle_id,
            "human_plan_imported_from_market_view",
            {"author": "human", "source": "obsidian", "status": status},
        )
        return normalized

    def reveal_allowed(self, cycle_id: str, *, as_of: str | datetime | None = None) -> bool:
        human = self.load_plan(cycle_id, "human")
        if human and human.get("status") == "locked":
            return True
        window = self._window(cycle_id)
        return parse_utc(as_of) >= window.lock_deadline

    def plan_response(self, cycle_id: str, *, as_of: str | datetime | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "cycle_id": cycle_id,
            "human_plan": self.load_plan(cycle_id, "human"),
            "ai_plan_revealed": False,
        }
        if self.reveal_allowed(cycle_id, as_of=as_of):
            payload["ai_plan"] = self.load_plan(cycle_id, "ai")
            payload["ai_plan_revealed"] = True
        return payload

    def effective_plan(self, cycle_id: str, *, as_of: str | datetime | None = None) -> dict[str, Any] | None:
        window = self._window(cycle_id)
        human = self.load_plan(cycle_id, "human")
        if human and human.get("status") == "locked":
            locked_at = parse_utc(human.get("locked_at"))
            if locked_at <= window.lock_deadline:
                return {**human, "effective_author": "human"}
        ai_plan = self.load_plan(cycle_id, "ai")
        if ai_plan:
            return {**ai_plan, "effective_author": "ai", "status": "fallback_active"}
        return None

    def audit(self, cycle_id: str, event: str, detail: dict[str, Any]) -> None:
        path = self.root / "audit" / f"{cycle_id}.json"
        rows = load_json(path)
        rows.append({"ts": _now().isoformat(), "cycle_id": cycle_id, "event": event, "detail": detail})
        write_json(path, rows)

    def _plan_from_market_view(
        self,
        cycle_id: str,
        view: dict[str, Any],
        *,
        author: str,
        status: str,
        cycle_open: float,
        prev_cycle_range: float,
        now: str | datetime | None,
    ) -> dict[str, Any] | None:
        direction = _direction_from_market_view(view)
        if direction is None:
            return None
        grid = self.config["grid"]
        half_width = float(grid["range_k"]) * float(prev_cycle_range)
        low = cycle_open - half_width
        high = cycle_open + half_width
        key_levels = _float_list(view.get("key_levels")) or [cycle_open]
        invalidation = _invalidation_from_market_view(view, low=low, high=high, direction=direction)
        score = view.get("direction_score")
        confidence = None if score is None else max(1, min(10, round(float(score) / 10)))
        if direction == "long":
            plan_range = {"low": _matching_invalidation_price(invalidation, "below") or low, "high": None}
        elif direction == "short":
            plan_range = {"low": None, "high": _matching_invalidation_price(invalidation, "above") or high}
        else:
            plan_range = {"low": None, "high": None}
        return {
            "cycle_id": cycle_id,
            "author": author,
            "direction": direction,
            "range": plan_range,
            "key_levels": key_levels,
            "invalidation": invalidation,
            "confidence": confidence,
            "locked_at": parse_utc(now).isoformat() if status != "draft" else None,
            "source": "obsidian",
            "status": status,
        }

    def _plan_path(self, cycle_id: str, author: str) -> Path:
        return self.root / "plans" / f"{cycle_id}_{author}.json"

    def _window(self, cycle_id: str):
        deadline_min = int(self.config.get("plan_lock_deadline_min_before_cycle", 0))
        return cycle_window_from_id(cycle_id, lock_deadline_min_before_cycle=deadline_min)


def validate_plan(
    payload: dict[str, Any],
    *,
    author: str,
    status: str,
    now: str | datetime | None = None,
) -> dict[str, Any]:
    if author not in PLAN_AUTHORS:
        raise ValueError("author must be human or ai")
    if status not in PLAN_STATUSES:
        raise ValueError("invalid plan status")
    cycle_id = str(payload.get("cycle_id") or "")
    if not cycle_id:
        raise ValueError("cycle_id is required")
    direction = str(payload.get("direction") or "").lower()
    if direction not in PLAN_DIRECTIONS:
        raise ValueError("direction must be long, short, or flat")
    invalidation = [_normalize_invalidation(row) for row in (payload.get("invalidation") or [])]
    range_payload = payload.get("range") if isinstance(payload.get("range"), dict) else {}
    key_levels = _float_list(payload.get("key_levels"))
    low, high = _normalize_range(direction, range_payload, invalidation)
    if direction in {"long", "short"} and not key_levels:
        raise ValueError("key_levels must contain at least one price")
    confidence = payload.get("confidence")
    if confidence is not None:
        confidence = int(confidence)
        if confidence < 1 or confidence > 10:
            raise ValueError("confidence must be 1..10 or null")
    locked_at = payload.get("locked_at") or (parse_utc(now).isoformat() if status != "draft" else None)
    normalized = {
        "cycle_id": cycle_id,
        "author": author,
        "direction": direction,
        "range": {"low": low, "high": high},
        "key_levels": key_levels,
        "invalidation": invalidation,
        "confidence": confidence,
        "locked_at": locked_at,
        "source": str(payload.get("source") or ("console" if author == "human" else "obsidian")),
        "status": status,
    }
    bracket = _normalize_bracket(payload, direction=direction)
    if bracket is not None:
        normalized["bracket"] = bracket
    return normalized


def _normalize_range(direction: str, range_payload: dict[str, Any], invalidation: list[dict[str, Any]]) -> tuple[float | None, float | None]:
    if direction == "flat":
        return None, None
    if not invalidation:
        raise ValueError("directional plans require at least one structured invalidation condition")
    low = _optional_float(range_payload.get("low"), "range.low")
    high = _optional_float(range_payload.get("high"), "range.high")
    if low is not None and high is not None and low >= high:
        raise ValueError("range.low must be below range.high")
    if direction == "long":
        floor = _matching_invalidation_price(invalidation, "below")
        if floor is None:
            raise ValueError("long plans require a below invalidation floor")
        if low is not None and not _same_price(low, floor):
            raise ValueError("range.low must match the below invalidation floor")
        return floor, high
    ceiling = _matching_invalidation_price(invalidation, "above")
    if ceiling is None:
        raise ValueError("short plans require an above invalidation ceiling")
    if high is not None and not _same_price(high, ceiling):
        raise ValueError("range.high must match the above invalidation ceiling")
    return low, ceiling


def _matching_invalidation_price(invalidation: list[dict[str, Any]], side: str) -> float | None:
    for row in invalidation:
        if row.get("side") == side:
            return float(row["price"])
    return None


def _same_price(left: float, right: float) -> bool:
    return abs(float(left) - float(right)) < 1e-8


def _normalize_invalidation(row: Any) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ValueError("invalidation entries must be objects")
    side = str(row.get("side") or "").strip()
    side = INVALIDATION_SIDE_ALIASES.get(side, side)
    confirm = str(row.get("confirm") or "").strip()
    confirm = INVALIDATION_CONFIRM_ALIASES.get(confirm, confirm)
    if side not in INVALIDATION_SIDES:
        raise ValueError("invalidation.side must be below or above")
    if confirm not in INVALIDATION_CONFIRMS:
        raise ValueError("invalidation.confirm must be close_1m or touch")
    return {"side": side, "price": _required_float(row.get("price"), "invalidation.price"), "confirm": confirm}


def _normalize_bracket(payload: dict[str, Any], *, direction: str) -> dict[str, Any] | None:
    bracket = payload.get("bracket") if isinstance(payload.get("bracket"), dict) else {}
    entry = _price_from(bracket.get("entry", payload.get("entry")))
    take_profit = _price_from(_first_present(bracket, payload, ["take_profit", "tp", "target"]))
    stop_loss = _price_from(_first_present(bracket, payload, ["stop_loss", "sl", "stop"]))
    if entry is None and take_profit is None and stop_loss is None:
        return None
    if direction not in {"long", "short"}:
        raise ValueError("bracket plans require long or short direction")
    if entry is None or take_profit is None or stop_loss is None:
        raise ValueError("bracket plans require entry, take_profit, and stop_loss")
    if direction == "long" and not (stop_loss < entry < take_profit):
        raise ValueError("long bracket requires stop_loss < entry < take_profit")
    if direction == "short" and not (take_profit < entry < stop_loss):
        raise ValueError("short bracket requires take_profit < entry < stop_loss")
    normalized: dict[str, Any] = {
        "mode": "bracket",
        "entry": entry,
        "take_profit": take_profit,
        "stop_loss": stop_loss,
        "same_bar_priority": str(bracket.get("same_bar_priority") or payload.get("same_bar_priority") or "stop"),
    }
    for key in ("notional", "contracts"):
        value = _first_present(bracket, payload, [key])
        if value not in (None, ""):
            normalized[key] = _required_float(value, f"bracket.{key}")
    return normalized


def _price_from(value: Any) -> float | None:
    if isinstance(value, dict):
        value = value.get("price")
    return _optional_float(value, "bracket price")


def _first_present(primary: dict[str, Any], secondary: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        if primary.get(key) not in (None, ""):
            return primary.get(key)
        if secondary.get(key) not in (None, ""):
            return secondary.get(key)
    return None


def _direction_from_market_view(view: dict[str, Any]) -> str | None:
    bias = str(view.get("direction_bias") or "").lower()
    if bias in {"strong_long", "long_bias", "long", "bullish"}:
        return "long"
    if bias in {"strong_short", "short_bias", "short", "bearish"}:
        return "short"
    if bias in {"neutral", "flat", "watch"}:
        return "flat"
    score = view.get("direction_score")
    if score is None:
        return None
    value = float(score)
    if value >= 61:
        return "long"
    if value <= 39:
        return "short"
    return "flat"


def _invalidation_from_market_view(view: dict[str, Any], *, low: float, high: float, direction: str) -> list[dict[str, Any]]:
    expiry = view.get("expiry") if isinstance(view.get("expiry"), dict) else {}
    if direction == "long":
        return [{"side": "below", "price": float(expiry.get("expire_below") or low), "confirm": "touch"}]
    if direction == "short":
        return [{"side": "above", "price": float(expiry.get("expire_above") or high), "confirm": "touch"}]
    return [
        {"side": "below", "price": float(expiry.get("expire_below") or low), "confirm": "touch"},
        {"side": "above", "price": float(expiry.get("expire_above") or high), "confirm": "touch"},
    ]


def _float_list(value: Any) -> list[float]:
    rows = []
    for item in value or []:
        match = _NUMBER_PATTERN.search(str(item))
        if match:
            rows.append(float(match.group(0).replace(",", "")))
    return rows


def _required_float(value: Any, field: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a number") from exc


def _optional_float(value: Any, field: str) -> float | None:
    if value is None or value == "":
        return None
    return _required_float(value, field)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)
