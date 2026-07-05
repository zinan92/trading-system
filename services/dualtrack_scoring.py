from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from schemas.market_data import Bar
from services.config_loader import ROOT, load_pipeline_config
from services.dualtrack_config import dualtrack_config
from services.dualtrack_store import DualTrackPlanStore
from services.journal_store import load_json, write_json


class DualTrackScorer:
    def __init__(self, output_root: Path | None = None, *, config: dict[str, Any] | None = None) -> None:
        pipeline_config = load_pipeline_config()
        self.output_root = Path(output_root) if output_root else ROOT / pipeline_config.get("output_root", "outputs")
        self.root = self.output_root / "dualtrack"
        self.config = config or dualtrack_config()
        self.store = DualTrackPlanStore(self.output_root, config=self.config)

    def close_cycle(self, cycle_id: str, bars: Iterable[Bar]) -> dict[str, Any]:
        existing_attribution = load_json(self.root / "attribution" / f"{cycle_id}.json")
        if existing_attribution:
            return existing_attribution[-1]
        rows = tuple(bars)
        if not rows:
            raise ValueError("bars are required")
        open_price = float(rows[0].open)
        close_price = float(rows[-1].close)
        realized_direction = _direction(open_price, close_price)
        machine_fills = load_json(self._fills_path(cycle_id, "machine"))
        human_fills = load_json(self._fills_path(cycle_id, "human"))
        opportunities = census_opportunities(
            rows,
            reversal_bp=float(self.config["census"]["reversal_bp"]),
            min_run_pct=float(self.config["census"]["min_run_pct"]),
        )
        machine_captured = captured_count(opportunities, machine_fills)
        human_captured = captured_count(opportunities, human_fills)
        plan_grades = self._grade_plans(cycle_id, realized_direction)
        scoreboard = self._update_scoreboard(cycle_id, plan_grades)
        cycle = {
            "cycle_id": cycle_id,
            "kind": cycle_id.rsplit("_", 1)[-1],
            "start": rows[0].timestamp,
            "end": rows[-1].timestamp,
            "open_price": open_price,
            "close_price": close_price,
            "realized_direction": realized_direction,
            "effective_plan_author": _existing_cycle(cycle_id, self.root).get("effective_plan_author", ""),
            "machine_stood_down": bool(_existing_cycle(cycle_id, self.root).get("machine_stood_down", False)),
            "opportunity_count": len(opportunities),
            "machine_captured": machine_captured,
            "human_captured": human_captured,
            "machine_realized_pnl": _pnl(machine_fills),
            "human_realized_pnl": _pnl(human_fills),
            "plan_grades": plan_grades,
        }
        write_json(self.root / "cycles" / f"{cycle_id}.json", [cycle])
        daily = self._write_daily_ledger(cycle_id, machine_fills, human_fills)
        weekly = self._write_weekly_ledger(daily["date"])
        attribution = self._attribution(cycle, machine_fills, human_fills, opportunities, scoreboard, daily, weekly)
        write_json(self.root / "attribution" / f"{cycle_id}.json", [attribution])
        return attribution

    def attribution_payload(self, cycle_id: str) -> dict[str, Any]:
        rows = load_json(self.root / "attribution" / f"{cycle_id}.json")
        if not rows:
            raise ValueError("cycle attribution is only available after close")
        return rows[-1]

    def ledger_payload(self, *, week: str | None = None) -> dict[str, Any]:
        if week:
            weekly = load_json(self.root / "ledger" / "weekly" / f"{week}.json")
            return weekly[-1] if weekly else {}
        daily_rows = []
        daily_dir = self.root / "ledger" / "daily"
        if daily_dir.exists():
            for path in sorted(daily_dir.glob("*.json")):
                rows = load_json(path)
                if rows:
                    daily_rows.append(rows[-1])
        return {"daily": daily_rows}

    def record_verdict(self, cycle_id: str, note: str) -> dict[str, Any]:
        payload = {"cycle_id": cycle_id, "note": str(note).strip(), "recorded_at": _now()}
        if not payload["note"]:
            raise ValueError("note is required")
        write_json(self.root / "verdicts" / f"{cycle_id}.json", [payload])
        return payload

    def _grade_plans(self, cycle_id: str, realized_direction: str) -> dict[str, Any]:
        grades = {}
        for author in ("human", "ai"):
            plan = self.store.load_plan(cycle_id, author)
            direction = str((plan or {}).get("direction") or "absent")
            graded = direction in {"long", "short"} and realized_direction in {"long", "short"}
            hit = bool(graded and direction == realized_direction)
            grades[author] = {"cycle_id": cycle_id, "author": author, "direction": direction, "graded": graded, "hit": hit}
        return grades

    def _update_scoreboard(self, cycle_id: str, grades: dict[str, Any]) -> dict[str, Any]:
        existing_rows = load_json(self.root / "scoreboard.json")
        board = existing_rows[-1] if existing_rows else {"history": {"human": [], "ai": []}}
        history = board.setdefault("history", {"human": [], "ai": []})
        window = int(self.config["scoreboard"]["window_cycles"])
        for author, grade in grades.items():
            rows = [row for row in history.get(author, []) if row.get("cycle_id") != cycle_id]
            rows.append({"cycle_id": cycle_id, "graded": bool(grade["graded"]), "hit": bool(grade["hit"])})
            history[author] = rows
            board[author] = _score_rows(rows, window)
        human_rate = board.get("human", {}).get("rolling_30", {}).get("hit_rate", 0.0)
        threshold = float(self.config["scoreboard"]["gate_threshold"])
        board["trend_leg_gate"] = {"threshold": threshold, "armed": human_rate >= threshold, "basis": board.get("human", {}).get("rolling_30", {})}
        write_json(self.root / "scoreboard.json", [board])
        return board

    def _write_daily_ledger(self, cycle_id: str, machine_fills: list[dict[str, Any]], human_fills: list[dict[str, Any]]) -> dict[str, Any]:
        date = cycle_id.split("_", 1)[0]
        path = self.root / "ledger" / "daily" / f"{date}.json"
        existing = load_json(path)
        row = existing[-1] if existing else {"date": date, "cycles": {}}
        machine = _pnl(machine_fills)
        human = _pnl(human_fills)
        row["cycles"][cycle_id] = {"machine": machine, "human": human, "total": round(machine + human, 8)}
        row["tracks"] = {
            "machine": {"realized_pnl": round(sum(item["machine"] for item in row["cycles"].values()), 8)},
            "human": {"realized_pnl": round(sum(item["human"] for item in row["cycles"].values()), 8)},
        }
        row["total_pnl"] = round(row["tracks"]["machine"]["realized_pnl"] + row["tracks"]["human"]["realized_pnl"], 8)
        write_json(path, [row])
        return row

    def _write_weekly_ledger(self, date: str) -> dict[str, Any]:
        parsed = datetime.fromisoformat(date)
        iso = parsed.isocalendar()
        week = f"{iso.year}-W{iso.week:02d}"
        daily_rows = []
        daily_dir = self.root / "ledger" / "daily"
        if daily_dir.exists():
            for path in sorted(daily_dir.glob("*.json")):
                rows = load_json(path)
                if rows and _iso_week(rows[-1]["date"]) == week:
                    daily_rows.append(rows[-1])
        machine = round(sum(row["tracks"]["machine"]["realized_pnl"] for row in daily_rows), 8)
        human = round(sum(row["tracks"]["human"]["realized_pnl"] for row in daily_rows), 8)
        payload = {
            "week": week,
            "days": daily_rows,
            "tracks": {"machine": {"realized_pnl": machine}, "human": {"realized_pnl": human}},
            "total_pnl": round(machine + human, 8),
            "target_usd": list(self.config["weekly_target_usd"]),
        }
        write_json(self.root / "ledger" / "weekly" / f"{week}.json", [payload])
        return payload

    def _attribution(
        self,
        cycle: dict[str, Any],
        machine_fills: list[dict[str, Any]],
        human_fills: list[dict[str, Any]],
        opportunities: list[dict[str, Any]],
        scoreboard: dict[str, Any],
        daily: dict[str, Any],
        weekly: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "cycle_id": cycle["cycle_id"],
            "status": "closed",
            "cycle": cycle,
            "tracks": {
                "machine": _track_stats(machine_fills, captured=cycle["machine_captured"], opportunity_count=len(opportunities)),
                "human": _track_stats(human_fills, captured=cycle["human_captured"], opportunity_count=len(opportunities)),
            },
            "opportunities": opportunities,
            "plan_grades": cycle["plan_grades"],
            "scoreboard": scoreboard,
            "ledger": {"daily": daily, "weekly": weekly},
        }

    def _fills_path(self, cycle_id: str, track: str) -> Path:
        return self.root / "fills" / f"{cycle_id}_{track}.json"


def census_opportunities(bars: Iterable[Bar], *, reversal_bp: float, min_run_pct: float) -> list[dict[str, Any]]:
    rows = tuple(bars)
    opportunities = []
    if len(rows) < 2:
        return opportunities
    run_start = rows[0]
    run_open = float(rows[0].open)
    last_close = run_open
    direction = ""
    peak = run_open
    trough = run_open
    for bar in rows[1:]:
        close = float(bar.close)
        move_pct = (close - run_open) / run_open * 100
        next_direction = "long" if move_pct > 0 else "short" if move_pct < 0 else direction
        peak = max(peak, close)
        trough = min(trough, close)
        reversal = abs((close - last_close) / last_close * 10_000) if last_close else 0
        if direction and next_direction != direction and reversal >= reversal_bp:
            _append_run(opportunities, run_start, bar, run_open, last_close, direction, min_run_pct)
            run_start = bar
            run_open = last_close
            peak = trough = close
        direction = next_direction or direction
        last_close = close
    if direction:
        _append_run(opportunities, run_start, rows[-1], run_open, last_close, direction, min_run_pct)
    return opportunities


def captured_count(opportunities: list[dict[str, Any]], fills: list[dict[str, Any]]) -> int:
    count = 0
    for opportunity in opportunities:
        start = opportunity["start"]
        end = opportunity["end"]
        wanted_side = "buy" if opportunity["direction"] == "long" else "sell"
        if any(start <= fill.get("ts", "") <= end and fill.get("side") == wanted_side for fill in fills):
            count += 1
    return count


def _append_run(
    opportunities: list[dict[str, Any]],
    start_bar: Bar,
    end_bar: Bar,
    open_price: float,
    close_price: float,
    direction: str,
    min_run_pct: float,
) -> None:
    move_pct = abs((close_price - open_price) / open_price * 100) if open_price else 0.0
    if move_pct >= min_run_pct:
        opportunities.append({
            "start": start_bar.timestamp,
            "end": end_bar.timestamp,
            "direction": direction,
            "move_pct": round(move_pct, 4),
        })


def _track_stats(fills: list[dict[str, Any]], *, captured: int, opportunity_count: int) -> dict[str, Any]:
    pnl = _pnl(fills)
    exits = [fill for fill in fills if float(fill.get("realized_pnl", 0.0)) != 0]
    wins = [fill for fill in exits if float(fill.get("realized_pnl", 0.0)) > 0]
    return {
        "fill_count": len(fills),
        "win_rate": round(len(wins) / len(exits), 4) if exits else 0.0,
        "realized_pnl": pnl,
        "captured": captured,
        "opportunity_count": opportunity_count,
        "discipline_violations": sum(1 for fill in fills if fill.get("out_of_plan")),
    }


def _score_rows(rows: list[dict[str, Any]], window: int) -> dict[str, Any]:
    graded_rows = [row for row in rows if row.get("graded")]
    rolling = graded_rows[-window:]
    hits = sum(1 for row in graded_rows if row.get("hit"))
    rolling_hits = sum(1 for row in rolling if row.get("hit"))
    return {
        "graded": len(graded_rows),
        "hits": hits,
        "rolling_30": {
            "graded": len(rolling),
            "hits": rolling_hits,
            "hit_rate": round(rolling_hits / len(rolling), 4) if rolling else 0.0,
        },
    }


def _existing_cycle(cycle_id: str, root: Path) -> dict[str, Any]:
    rows = load_json(root / "cycles" / f"{cycle_id}.json")
    return rows[-1] if rows else {}


def _direction(open_price: float, close_price: float) -> str:
    if close_price > open_price:
        return "long"
    if close_price < open_price:
        return "short"
    return "flat"


def _pnl(fills: list[dict[str, Any]]) -> float:
    return round(sum(float(fill.get("realized_pnl", 0.0)) for fill in fills), 8)


def _iso_week(date: str) -> str:
    parsed = datetime.fromisoformat(date)
    iso = parsed.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
