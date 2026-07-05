from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

from schemas.market_data import Bar
from services.config_loader import ROOT, load_pipeline_config
from services.dualtrack_clock import cycle_window, cycle_window_from_id, parse_utc
from services.dualtrack_config import dualtrack_config
from services.dualtrack_machine import DualTrackMachineRunner
from services.dualtrack_scoring import DualTrackScorer
from services.dualtrack_store import DualTrackPlanStore
from services.journal_store import load_json, write_json
from services.market_store import MarketStore


class DualTrackCycleRunner:
    """Schedule-facing driver for the dual-track paper engine.

    The runner is intentionally read-only toward venues. It reads local market
    bars and market-view artifacts, then writes only dualtrack paper artifacts.
    """

    def __init__(
        self,
        *,
        output_root: Path | None = None,
        market_db: Path | None = None,
        config: dict[str, Any] | None = None,
        symbol: str = "GOLD",
        timeframe: str = "1m",
    ) -> None:
        pipeline_config = load_pipeline_config()
        self.output_root = Path(output_root) if output_root else ROOT / pipeline_config.get("output_root", "outputs")
        env_market_db = os.getenv("TRADING_ORCHESTRATOR_MARKET_DB")
        self.market_db = Path(market_db or env_market_db or ROOT / pipeline_config.get("local_market_db", "data/market_data.db"))
        self.config = config or dualtrack_config()
        self.symbol = symbol
        self.timeframe = timeframe
        self.store = DualTrackPlanStore(self.output_root, config=self.config)
        self.machine = DualTrackMachineRunner(self.output_root, config=self.config)
        self.scorer = DualTrackScorer(self.output_root, config=self.config)
        self.market = MarketStore(self.market_db)

    def pre_cycle(self, cycle_id: str, *, as_of: str | datetime | None = None) -> dict[str, Any]:
        bars = self._cycle_bars(cycle_id, as_of=as_of)
        if not bars:
            self.store.audit(cycle_id, "cycle_runner_pre_cycle_skipped", {"reason": "cycle_bars_missing"})
            return {"event": "pre_cycle", "cycle_id": cycle_id, "status": "skipped", "reason": "cycle_bars_missing"}
        prev_range = self.previous_cycle_range(cycle_id)
        plan = self.store.ensure_ai_plan(
            cycle_id,
            cycle_open=float(bars[0].open),
            prev_cycle_range=prev_range,
            now=as_of or cycle_window_from_id(cycle_id).start,
        )
        return {
            "event": "pre_cycle",
            "cycle_id": cycle_id,
            "status": "ai_plan_ready" if plan else "fail_closed_no_ai_plan",
            "ai_plan_present": plan is not None,
            "prev_range": prev_range,
            "bar_count": len(bars),
        }

    def intraday_tick(self, cycle_id: str | None = None, *, as_of: str | datetime | None = None) -> dict[str, Any]:
        window = cycle_window(as_of) if cycle_id is None else cycle_window_from_id(cycle_id)
        bars = self._cycle_bars(window.cycle_id, as_of=as_of)
        if not bars:
            self.store.audit(window.cycle_id, "cycle_runner_intraday_skipped", {"reason": "cycle_bars_missing"})
            return {"event": "intraday", "cycle_id": window.cycle_id, "status": "skipped", "reason": "cycle_bars_missing"}
        prev_range = self.previous_cycle_range(window.cycle_id)
        state = self.machine.run_effective_plan(window.cycle_id, bars, prev_range=prev_range, as_of=as_of)
        self._write_runner_state(window.cycle_id, "intraday", {"bar_count": len(bars), "prev_range": prev_range})
        return {"event": "intraday", "cycle_id": window.cycle_id, "status": "ran", "bar_count": len(bars), "state": state}

    def close_cycle(self, cycle_id: str, *, as_of: str | datetime | None = None) -> dict[str, Any]:
        existing = load_json(self.output_root / "dualtrack" / "attribution" / f"{cycle_id}.json")
        if existing:
            return {"event": "close", "cycle_id": cycle_id, "status": "already_closed", "attribution": existing[-1]}
        bars = self._cycle_bars(cycle_id, as_of=cycle_window_from_id(cycle_id).end)
        if not bars:
            self.store.audit(cycle_id, "cycle_runner_close_skipped", {"reason": "cycle_bars_missing"})
            return {"event": "close", "cycle_id": cycle_id, "status": "skipped", "reason": "cycle_bars_missing"}
        prev_range = self.previous_cycle_range(cycle_id)
        self.machine.run_effective_plan(cycle_id, bars, prev_range=prev_range, as_of=as_of or cycle_window_from_id(cycle_id).end)
        attribution = self.scorer.close_cycle(cycle_id, bars)
        self._write_runner_state(cycle_id, "close", {"bar_count": len(bars), "prev_range": prev_range})
        return {"event": "close", "cycle_id": cycle_id, "status": "closed", "attribution": attribution}

    def fast_forward_day(self, date: str) -> dict[str, Any]:
        results = []
        for kind in ("DAY", "NIGHT"):
            cycle_id = f"{date}_{kind}"
            window = cycle_window_from_id(cycle_id)
            results.append(self.pre_cycle(cycle_id, as_of=window.start))
            results.append(self.intraday_tick(cycle_id, as_of=window.end))
            results.append(self.close_cycle(cycle_id, as_of=window.end))
        return {"event": "fast_forward_day", "date": date, "results": results}

    def auto(self, *, as_of: str | datetime | None = None) -> dict[str, Any]:
        now = parse_utc(as_of)
        current = cycle_window(now)
        previous = cycle_window(current.start - timedelta(minutes=1))
        results = [self.close_cycle(previous.cycle_id, as_of=now)]
        results.append(self.pre_cycle(current.cycle_id, as_of=now))
        results.append(self.intraday_tick(current.cycle_id, as_of=now))
        return {"event": "auto", "as_of": now.isoformat(), "results": results}

    def previous_cycle_range(self, cycle_id: str) -> float:
        window = cycle_window_from_id(cycle_id)
        start = window.start - timedelta(hours=12)
        end = window.start - timedelta(seconds=1)
        bars = self.market.load_bars_between(self.symbol, self.timeframe, start.isoformat(), end.isoformat())
        if not bars:
            return 0.0
        return round(max(float(bar.high) for bar in bars) - min(float(bar.low) for bar in bars), 8)

    def _cycle_bars(self, cycle_id: str, *, as_of: str | datetime | None = None) -> list[Bar]:
        window = cycle_window_from_id(cycle_id)
        end = parse_utc(as_of) if as_of is not None else window.end
        if end >= window.end:
            end = window.end - timedelta(seconds=1)
        if end < window.start:
            return []
        return self.market.load_bars_between(self.symbol, self.timeframe, window.start.isoformat(), end.isoformat())

    def _write_runner_state(self, cycle_id: str, event: str, detail: dict[str, Any]) -> None:
        path = self.output_root / "dualtrack" / "runner" / f"{cycle_id}.json"
        rows = load_json(path)
        rows.append({"ts": parse_utc(None).isoformat(), "cycle_id": cycle_id, "event": event, "detail": detail})
        write_json(path, rows)


def build_parser() -> argparse.ArgumentParser:  # pragma: no cover - thin CLI wrapper
    parser = argparse.ArgumentParser(description="Run the dual-track cycle orchestrator.")
    parser.add_argument("--event", choices=("auto", "pre-cycle", "intraday", "close", "fast-forward-day"), default="auto")
    parser.add_argument("--cycle-id", default="")
    parser.add_argument("--date", default="")
    parser.add_argument("--as-of", default="")
    parser.add_argument("--market-db", default="")
    parser.add_argument("--output-root", default="")
    parser.add_argument("--symbol", default="GOLD")
    parser.add_argument("--timeframe", default="1m")
    return parser


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - thin CLI wrapper
    args = build_parser().parse_args(argv)
    runner = DualTrackCycleRunner(
        output_root=Path(args.output_root) if args.output_root else None,
        market_db=Path(args.market_db) if args.market_db else None,
        symbol=args.symbol,
        timeframe=args.timeframe,
    )
    as_of = args.as_of or None
    if args.event == "auto":
        payload = runner.auto(as_of=as_of)
    elif args.event == "fast-forward-day":
        if not args.date:
            raise SystemExit("--date is required for fast-forward-day")
        payload = runner.fast_forward_day(args.date)
    else:
        cycle_id = args.cycle_id or cycle_window(as_of).cycle_id
        if args.event == "pre-cycle":
            payload = runner.pre_cycle(cycle_id, as_of=as_of)
        elif args.event == "intraday":
            payload = runner.intraday_tick(cycle_id, as_of=as_of)
        else:
            payload = runner.close_cycle(cycle_id, as_of=as_of)
    print(payload)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
