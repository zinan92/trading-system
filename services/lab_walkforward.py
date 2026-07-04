"""Walk-forward windows and holdout quarantine for Strategy Lab."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from schemas.market_data import Bar
from services.lab_registry import LabRegistry


@dataclass(frozen=True)
class WalkForwardConfig:
    train_days: int = 60
    validate_days: int = 14
    step_days: int = 14
    holdout_days: int = 28


class HoldoutQuarantine:
    def __init__(self, bars: list[Bar], config: WalkForwardConfig | None = None) -> None:
        self.bars = sorted(bars, key=lambda item: item.timestamp)
        self.config = config or WalkForwardConfig()
        self.holdout_start = self._compute_holdout_start()

    def research_bars(self) -> list[Bar]:
        return [bar for bar in self.bars if _parse_ts(bar.timestamp) < self.holdout_start]

    def public_bars_between(self, start: datetime, end: datetime) -> list[Bar]:
        if end >= self.holdout_start:
            raise ValueError("public lab API cannot read holdout bars")
        return [bar for bar in self.research_bars() if start <= _parse_ts(bar.timestamp) <= end]

    def consume_holdout(self, registry: LabRegistry, exp_id: str) -> list[Bar]:
        holdout = [bar for bar in self.bars if _parse_ts(bar.timestamp) >= self.holdout_start]
        registry.record_holdout_consumption(exp_id, {
            "start": holdout[0].timestamp if holdout else self.holdout_start.isoformat(),
            "end": holdout[-1].timestamp if holdout else "",
            "bars": len(holdout),
        })
        return holdout

    def _compute_holdout_start(self) -> datetime:
        if not self.bars:
            return datetime.now(timezone.utc)
        last = _parse_ts(self.bars[-1].timestamp)
        return last - timedelta(days=max(28, self.config.holdout_days))


def build_windows(bars: list[Bar], config: WalkForwardConfig | None = None) -> list[dict]:
    cfg = config or WalkForwardConfig()
    quarantine = HoldoutQuarantine(bars, cfg)
    research = quarantine.research_bars()
    if not research:
        return []
    first = _parse_ts(research[0].timestamp)
    last = _parse_ts(research[-1].timestamp)
    windows: list[dict] = []
    cursor = first
    while True:
        train_start = cursor
        train_end = train_start + timedelta(days=cfg.train_days)
        validate_start = train_end
        validate_end = validate_start + timedelta(days=cfg.validate_days)
        if validate_end > last:
            break
        windows.append({
            "train_start": train_start.isoformat(),
            "train_end": train_end.isoformat(),
            "validate_start": validate_start.isoformat(),
            "validate_end": validate_end.isoformat(),
        })
        cursor += timedelta(days=cfg.step_days)
    return windows


def run_walkforward(
    bars: list[Bar],
    evaluate_window: Callable[[list[Bar], dict], dict],
    config: WalkForwardConfig | None = None,
) -> dict:
    cfg = config or WalkForwardConfig()
    quarantine = HoldoutQuarantine(bars, cfg)
    results = []
    for window in build_windows(bars, cfg):
        validate_bars = quarantine.public_bars_between(_parse_ts(window["validate_start"]), _parse_ts(window["validate_end"]))
        result = evaluate_window(validate_bars, window)
        results.append({"window": window, "result": result})
    statuses = [item["result"].get("status") for item in results]
    return {
        "status": "valid" if results and all(status == "valid" for status in statuses) else "invalid",
        "window_count": len(results),
        "holdout_start": quarantine.holdout_start.isoformat(),
        "windows": results,
    }


def load_gold_1m_bars(market_db: Path, start: str | None = None, end: str | None = None) -> list[Bar]:
    from services.market_store import MarketStore

    store = MarketStore(market_db)
    if start and end:
        return store.load_bars_between("GOLD", "1m", start, end)
    return store.load_bars("GOLD", "1m", 2_000_000)


def _parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0)
