from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from schemas.market_data import Bar
from services.config_loader import ROOT, load_pipeline_config
from services.dualtrack_clock import cycle_window_from_id, parse_utc
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

    def close_cycle(self, cycle_id: str, bars: Iterable[Bar], *, force: bool = False) -> dict[str, Any]:
        existing_attribution = load_json(self.root / "attribution" / f"{cycle_id}.json")
        if existing_attribution and not force:
            return existing_attribution[-1]
        rows = tuple(bars)
        if not rows:
            raise ValueError("bars are required")
        open_price = float(rows[0].open)
        close_price = float(rows[-1].close)
        realized_direction = _direction(open_price, close_price)
        machine_all_fills = load_json(self._fills_path(cycle_id, "machine"))
        machine_fills = _live_execution_fills(machine_all_fills)
        recovery_replay_fills = _recovery_replay_fills(machine_all_fills)
        human_fills = load_json(self._fills_path(cycle_id, "human"))
        machine_trades = self._trade_rows(cycle_id, "machine", machine_fills, force_rebuild=True)
        recovery_replay_trades = _trades_from_fills(recovery_replay_fills, track="machine")
        write_json(self._recovery_replay_trades_path(cycle_id), recovery_replay_trades)
        human_trades = self._trade_rows(cycle_id, "human", human_fills)
        machine_trades = apply_unrealized(machine_trades, close_price, mark_fresh=math.isfinite(close_price))
        human_trades = apply_unrealized(human_trades, close_price, mark_fresh=math.isfinite(close_price))
        if machine_trades:
            write_json(self._trades_path(cycle_id, "machine"), machine_trades)
        if human_trades:
            write_json(self._trades_path(cycle_id, "human"), human_trades)
        opportunities = census_opportunities(
            rows,
            reversal_bp=float(self.config["census"]["reversal_bp"]),
            min_run_pct=float(self.config["census"]["min_run_pct"]),
        )
        machine_captured = captured_count(opportunities, machine_fills)
        human_captured = captured_count(opportunities, human_fills)
        plan_grades = self._grade_plans(cycle_id, realized_direction)
        machine_review = self._machine_review(
            cycle_id,
            rows,
            machine_fills,
            machine_trades,
            recovery_replay_fills,
            recovery_replay_trades,
            realized_direction=realized_direction,
        )
        human_review = self._human_review(
            cycle_id,
            rows,
            human_fills,
            human_trades,
            realized_direction=realized_direction,
        )
        write_json(self.root / "reviews" / f"{cycle_id}_machine.json", [machine_review])
        write_json(self.root / "reviews" / f"{cycle_id}_human.json", [human_review])
        existing_cycle = _existing_cycle(cycle_id, self.root)
        scoreboard = self._update_scoreboard(cycle_id, plan_grades)
        cycle = {
            "cycle_id": cycle_id,
            "kind": cycle_id.rsplit("_", 1)[-1],
            "start": rows[0].timestamp,
            "end": rows[-1].timestamp,
            "open_price": open_price,
            "close_price": close_price,
            "realized_direction": realized_direction,
            "effective_plan_author": existing_cycle.get("effective_plan_author", ""),
            "machine_stood_down": bool(existing_cycle.get("machine_stood_down", False)),
            "opportunity_count": len(opportunities),
            "machine_captured": machine_captured,
            "human_captured": human_captured,
            "machine_realized_pnl": _pnl(machine_fills),
            "machine_recovery_replay_pnl": _pnl(recovery_replay_fills),
            "machine_recovery_replay_fill_count": len(recovery_replay_fills),
            "human_realized_pnl": _pnl(human_fills),
            "pnl_delta_machine_minus_human": round(_pnl(machine_fills) - _pnl(human_fills), 8),
            "machine_trade_count": len(machine_trades),
            "human_trade_count": len(human_trades),
            "plan_grades": plan_grades,
        }
        _preserve_machine_state(cycle, existing_cycle)
        write_json(self.root / "cycles" / f"{cycle_id}.json", [cycle])
        daily = self._write_daily_ledger(cycle_id, machine_fills, human_fills, recovery_replay_fills)
        weekly = self._write_weekly_ledger(daily["date"])
        attribution = self._attribution(
            cycle,
            machine_fills,
            human_fills,
            machine_trades,
            human_trades,
            opportunities,
            scoreboard,
            daily,
            weekly,
            machine_review,
            human_review,
            recovery_replay_fills,
            recovery_replay_trades,
        )
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
        weekly_rows = []
        weekly_dir = self.root / "ledger" / "weekly"
        if weekly_dir.exists():
            for path in sorted(weekly_dir.glob("*.json")):
                rows = load_json(path)
                if rows:
                    weekly_rows.append(rows[-1])
        return {
            "daily": daily_rows,
            "weekly": weekly_rows[-1] if weekly_rows else {},
            "weekly_history": weekly_rows,
            "recent_reviews": self._recent_reviews(),
        }

    def rebuild_ledgers(self) -> dict[str, Any]:
        """Rebuild recorded PnL ledgers from durable fill evidence."""
        cycle_ids: set[str] = set()
        fills_dir = self.root / "fills"
        if fills_dir.exists():
            for path in fills_dir.glob("*.json"):
                stem = path.stem
                if stem.endswith("_machine") or stem.endswith("_human"):
                    cycle_ids.add(stem.rsplit("_", 1)[0])
        packages_dir = self.root / "strategy_cycle_packages"
        if packages_dir.exists():
            cycle_ids.update(path.stem for path in packages_dir.glob("*.json"))
        daily_dir = self.root / "ledger" / "daily"
        if daily_dir.exists():
            for path in daily_dir.glob("*.json"):
                rows = load_json(path)
                if rows:
                    cycle_ids.update((rows[-1].get("cycles") or {}).keys())
        dates: set[str] = set()
        for cycle_id in sorted(cycle_ids):
            date = _cycle_date(cycle_id)
            if date is None:
                self._record_invalid_ledger_date(
                    str(cycle_id),
                    source="rebuild_ledgers.cycle_id",
                )
                continue
            machine_all = load_json(self._fills_path(cycle_id, "machine"))
            human_fills = load_json(self._fills_path(cycle_id, "human"))
            production_projection = self._production_cycle_projection(cycle_id)
            self._write_daily_ledger(
                cycle_id,
                _live_execution_fills(machine_all),
                human_fills,
                _recovery_replay_fills(machine_all),
                production_projection=production_projection,
            )
            dates.add(date)
        for date in sorted(dates):
            self._write_weekly_ledger(date)
        return self.ledger_payload()

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
            eligible = author != "human" or _is_clean_human_plan(cycle_id, plan, self.config)
            graded = eligible and direction in {"long", "short"} and realized_direction in {"long", "short"}
            hit = bool(graded and direction == realized_direction)
            grades[author] = {
                "cycle_id": cycle_id,
                "author": author,
                "direction": direction,
                "graded": graded,
                "hit": hit,
                "eligible": eligible,
            }
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

    def _write_daily_ledger(
        self,
        cycle_id: str,
        machine_fills: list[dict[str, Any]],
        human_fills: list[dict[str, Any]],
        recovery_replay_fills: list[dict[str, Any]],
        *,
        production_projection: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        date = cycle_id.split("_", 1)[0]
        path = self.root / "ledger" / "daily" / f"{date}.json"
        existing = load_json(path)
        row = existing[-1] if existing else {"date": date, "cycles": {}}
        machine = (
            round(float(production_projection["realized_pnl"]), 8)
            if production_projection is not None
            else _pnl(machine_fills)
        )
        machine_trade_count = (
            int(production_projection["trade_count"])
            if production_projection is not None
            else _closed_trade_count(machine_fills)
        )
        machine_fill_count = (
            int(production_projection["fill_count"])
            if production_projection is not None
            else len(machine_fills)
        )
        machine_source = (
            str(production_projection["source"])
            if production_projection is not None
            else "dualtrack_live_execution_fills"
        )
        human = _pnl(human_fills)
        recovery = _pnl(recovery_replay_fills)
        machine_recorded = round(machine + recovery, 8)
        row["cycles"][cycle_id] = {
            "machine": machine,
            "machine_recorded": machine_recorded,
            "machine_trade_count": machine_trade_count,
            "machine_fill_count": machine_fill_count,
            "machine_source": machine_source,
            "human": human,
            "delta_machine_minus_human": round(machine_recorded - human, 8),
            "total": round(machine_recorded + human, 8),
            "recovery_replay": {
                "machine": recovery,
                "fill_count": len(recovery_replay_fills),
                "eligible_for_paper_pnl": False,
                "eligible_for_recorded_pnl": True,
            },
        }
        machine_live = round(sum(float(item.get("machine") or 0.0) for item in row["cycles"].values()), 8)
        machine_recovery = round(
            sum(float((item.get("recovery_replay") or {}).get("machine") or 0.0) for item in row["cycles"].values()),
            8,
        )
        machine_total = round(machine_live + machine_recovery, 8)
        machine_trade_total = sum(
            int(item.get("machine_trade_count") or 0)
            for item in row["cycles"].values()
        )
        machine_fill_total = sum(
            int(item.get("machine_fill_count") or 0)
            for item in row["cycles"].values()
        )
        human_total = round(sum(float(item.get("human") or 0.0) for item in row["cycles"].values()), 8)
        row["tracks"] = {
            "machine": {
                "realized_pnl": machine_total,
                "live_observed_realized_pnl": machine_live,
                "recovery_replay_realized_pnl": machine_recovery,
                "trade_count": machine_trade_total,
                "fill_count": machine_fill_total,
            },
            "human": {"realized_pnl": human_total},
        }
        row["delta_machine_minus_human"] = round(row["tracks"]["machine"]["realized_pnl"] - row["tracks"]["human"]["realized_pnl"], 8)
        row["total_pnl"] = round(row["tracks"]["machine"]["realized_pnl"] + row["tracks"]["human"]["realized_pnl"], 8)
        write_json(path, [row])
        return row

    def _production_cycle_projection(self, cycle_id: str) -> dict[str, Any] | None:
        """Project immutable terminal execution truth into the derived ledger.

        Legacy simulation fills and production Paper execution use different
        stores.  A terminal package is the only immutable record that binds the
        production plan, fills, closed positions, reconciliation and PnL.  It
        may override legacy fill-derived values only after its complete hash
        chain verifies and the package is terminal.
        """

        path = self.root / "strategy_cycle_packages" / f"{cycle_id}.json"
        if not path.exists():
            return None
        # Local import avoids the execution-adapter compatibility cycle:
        # strategy_cycle_package -> execution composition -> legacy adapter ->
        # dualtrack_scoring.
        from services.strategy_cycle_package import load_latest_verified_cycle_package

        try:
            package = load_latest_verified_cycle_package(path)
        except ValueError as exc:
            raise ValueError(
                f"verified production cycle package required for ledger projection: {cycle_id}"
            ) from exc
        if str(package.get("status") or "") != "closed":
            return None
        execution = package.get("execution")
        if not isinstance(execution, dict):
            return None
        reconciliation = execution.get("reconciliation")
        if not isinstance(reconciliation, dict):
            return None
        if str(reconciliation.get("status") or "").lower() != "ok":
            return None
        if reconciliation.get("issues"):
            return None
        pnl = execution.get("pnl")
        if not isinstance(pnl, dict) or _optional_number(pnl.get("realized")) is None:
            return None
        positions = [
            row
            for row in execution.get("positions") or []
            if isinstance(row, dict)
        ]
        closed_positions = [
            row
            for row in positions
            if str(row.get("status") or "").lower() == "closed"
        ]
        fills = [
            row
            for row in execution.get("fills") or []
            if isinstance(row, dict)
        ]
        return {
            "realized_pnl": round(float(pnl["realized"]), 8),
            "trade_count": len(closed_positions),
            "fill_count": len(fills),
            "source": "verified_strategy_cycle_package.execution",
            "package_hash": str(package.get("package_hash") or ""),
        }

    def _write_weekly_ledger(self, date: str) -> dict[str, Any]:
        week = _iso_week(date)
        if week is None:
            self._record_invalid_ledger_date(date, source="write_weekly_ledger.date")
            return {
                "status": "skipped_invalid_date",
                "date": date,
                "reason": "non_iso_date",
            }
        daily_rows = []
        daily_dir = self.root / "ledger" / "daily"
        if daily_dir.exists():
            for path in sorted(daily_dir.glob("*.json")):
                rows = load_json(path)
                if not rows:
                    continue
                row = rows[-1]
                row_date = str(row.get("date") or "")
                row_week = _iso_week(row_date)
                if row_week is None:
                    self._record_invalid_ledger_date(
                        row_date,
                        source=f"write_weekly_ledger.daily:{path.name}",
                    )
                    continue
                if row_week == week:
                    daily_rows.append(row)
        machine = round(sum(row["tracks"]["machine"]["realized_pnl"] for row in daily_rows), 8)
        machine_live = round(sum(row["tracks"]["machine"].get("live_observed_realized_pnl", row["tracks"]["machine"]["realized_pnl"]) for row in daily_rows), 8)
        machine_recovery = round(sum(row["tracks"]["machine"].get("recovery_replay_realized_pnl", 0.0) for row in daily_rows), 8)
        machine_trades = sum(
            int(row["tracks"]["machine"].get("trade_count") or 0)
            for row in daily_rows
        )
        machine_fills = sum(
            int(row["tracks"]["machine"].get("fill_count") or 0)
            for row in daily_rows
        )
        human = round(sum(row["tracks"]["human"]["realized_pnl"] for row in daily_rows), 8)
        payload = {
            "week": week,
            "days": daily_rows,
            "tracks": {
                "machine": {
                    "realized_pnl": machine,
                    "live_observed_realized_pnl": machine_live,
                    "recovery_replay_realized_pnl": machine_recovery,
                    "trade_count": machine_trades,
                    "fill_count": machine_fills,
                },
                "human": {"realized_pnl": human},
            },
            "delta_machine_minus_human": round(machine - human, 8),
            "total_pnl": round(machine + human, 8),
            "target_usd": list(self.config["weekly_target_usd"]),
            "recovery_replay": {
                "machine_realized_pnl": machine_recovery,
                "eligible_for_paper_pnl": False,
                "eligible_for_recorded_pnl": True,
            },
        }
        write_json(self.root / "ledger" / "weekly" / f"{week}.json", [payload])
        return payload

    def _record_invalid_ledger_date(self, value: str, *, source: str) -> None:
        """Keep malformed legacy rows visible without letting them kill a tick."""

        path = self.root / "ledger" / "diagnostics" / "invalid_dates.json"
        rows = load_json(path)
        key = (str(value), str(source))
        if any(
            (str(row.get("value") or ""), str(row.get("source") or "")) == key
            for row in rows
            if isinstance(row, dict)
        ):
            return
        rows.append({
            "value": str(value),
            "source": str(source),
            "reason": "non_iso_date",
            "recorded_at": _now(),
        })
        write_json(path, rows)

    def _attribution(
        self,
        cycle: dict[str, Any],
        machine_fills: list[dict[str, Any]],
        human_fills: list[dict[str, Any]],
        machine_trades: list[dict[str, Any]],
        human_trades: list[dict[str, Any]],
        opportunities: list[dict[str, Any]],
        scoreboard: dict[str, Any],
        daily: dict[str, Any],
        weekly: dict[str, Any],
        machine_review: dict[str, Any],
        human_review: dict[str, Any],
        recovery_replay_fills: list[dict[str, Any]],
        recovery_replay_trades: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "cycle_id": cycle["cycle_id"],
            "status": "closed",
            "cycle": cycle,
            "tracks": {
                "machine": _track_stats(machine_fills, machine_trades, captured=cycle["machine_captured"], opportunity_count=len(opportunities)),
                "human": _track_stats(human_fills, human_trades, captured=cycle["human_captured"], opportunity_count=len(opportunities)),
            },
            "fills": {
                "machine": machine_fills,
                "human": human_fills,
            },
            "trades": {
                "machine": machine_trades,
                "human": human_trades,
            },
            "opportunities": opportunities,
            "plan_grades": cycle["plan_grades"],
            "machine_review": machine_review,
            "human_review": human_review,
            "scoreboard": scoreboard,
            "ledger": {"daily": daily, "weekly": weekly},
            "recovery_replay": {
                "status": "present" if recovery_replay_fills else "none",
                "eligible_for_paper_pnl": False,
                "fill_count": len(recovery_replay_fills),
                "trade_count": len(recovery_replay_trades),
                "realized_pnl": _pnl(recovery_replay_fills),
                "fills": recovery_replay_fills,
                "trades": recovery_replay_trades,
            },
        }

    def _machine_review(
        self,
        cycle_id: str,
        bars: tuple[Bar, ...],
        fills: list[dict[str, Any]],
        trades: list[dict[str, Any]],
        recovery_replay_fills: list[dict[str, Any]],
        recovery_replay_trades: list[dict[str, Any]],
        *,
        realized_direction: str,
    ) -> dict[str, Any]:
        plan = self.store.machine_plan(cycle_id) or {}
        decision = str(plan.get("direction") or "absent")
        active_bars = bars
        if plan.get("execution_start") not in (None, ""):
            execution_start = parse_utc(plan["execution_start"])
            active_bars = tuple(bar for bar in bars if parse_utc(bar.timestamp) >= execution_start) or bars
        actual_low = min(float(bar.low) for bar in active_bars)
        actual_high = max(float(bar.high) for bar in active_bars)
        market_review = _market_review(active_bars, self.config)
        realized_regime = str(market_review["realized_regime"])
        predicted = plan.get("range") if isinstance(plan.get("range"), dict) else {}
        predicted_low = _optional_number(predicted.get("low"))
        predicted_high = _optional_number(predicted.get("high"))
        low_breach = predicted_low is not None and actual_low < predicted_low
        high_breach = predicted_high is not None and actual_high > predicted_high
        if low_breach and high_breach:
            range_result = "both_sides_breached"
        elif low_breach:
            range_result = "low_breached"
        elif high_breach:
            range_result = "high_breached"
        elif predicted_low is not None and predicted_high is not None:
            range_result = "inside_range"
        else:
            range_result = "range_missing"
        recorded_fills = [*fills, *recovery_replay_fills]
        recorded_trades = [*trades, *recovery_replay_trades]
        key_levels = []
        for value in plan.get("key_levels") or []:
            price = _optional_number(value)
            if price is None:
                continue
            key_levels.append({
                "price": price,
                "touched": any(float(bar.low) <= price <= float(bar.high) for bar in active_bars),
            })
        grid_rows = []
        for index, order in enumerate(plan.get("grid_orders") or []):
            entry = float(order["entry"])
            side = str(order.get("side") or decision)
            stop_loss = _plan_stop(plan, side)
            take_profit = float(order["take_profit"])
            risk_width = abs(entry - stop_loss) if stop_loss is not None else None
            reward_width = abs(take_profit - entry)
            r_multiple = reward_width / risk_width if risk_width not in (None, 0.0) else None
            touched = any(float(bar.low) <= entry <= float(bar.high) for bar in active_bars)
            matching_trades = [row for row in recorded_trades if str(row.get("rung")) == str(index)]
            events = [str(exit_fill.get("event") or "") for trade in matching_trades for exit_fill in trade.get("exit_fills") or []]
            outcome = "target" if "target" in events else "stop" if "stop" in events else "open" if matching_trades else "unfilled"
            grid_rows.append({
                "rung": index,
                "side": side,
                "entry": entry,
                "take_profit": take_profit,
                "stop_loss": stop_loss,
                "risk_width": round(risk_width, 8) if risk_width is not None else None,
                "reward_width": round(reward_width, 8),
                "r_multiple": round(r_multiple, 4) if r_multiple is not None else None,
                "touched": touched,
                "filled": any(fill.get("event") == "entry" and str(fill.get("rung", -1)) == str(index) for fill in recorded_fills),
                "outcome": outcome,
            })
        if decision in {"neutral", "flat"} and not grid_rows:
            no_trade_reason = "neutral_decision"
        elif not fills and grid_rows and not any(row["touched"] for row in grid_rows):
            no_trade_reason = "planned_levels_not_touched"
        elif not fills and recovery_replay_fills:
            no_trade_reason = "recovery_replay_only"
        elif not fills:
            no_trade_reason = "no_valid_fill"
        else:
            no_trade_reason = ""
        direction_graded = decision in {"long", "short", "neutral", "flat"}
        direction_hit = decision == realized_regime if direction_graded else False
        direction_review = _direction_review(decision, realized_regime, direction_graded, direction_hit)
        range_review = _range_review(
            predicted_low,
            predicted_high,
            actual_low,
            actual_high,
            range_result,
        )
        evidence_issues = []
        if not plan:
            evidence_issues.append("machine_plan_missing")
        if plan.get("planning_error"):
            evidence_issues.append("machine_planning_error")
        evidence = {
            "status": "complete" if not evidence_issues else "insufficient",
            "issues": evidence_issues,
            "bar_count": len(active_bars),
            "plan_present": bool(plan),
            "fills_present": bool(recorded_fills),
            "no_trade_is_valid_outcome": True,
        }
        if no_trade_reason == "neutral_decision":
            summary = f"本周期选择中立，未下单；实际区间 {actual_low:.1f}-{actual_high:.1f}，复盘已完成。"
        elif no_trade_reason == "recovery_replay_only":
            summary = (
                f"本周期只有恢复回放成交 {len(recovery_replay_fills)} 笔，"
                f"已记录收益 {_pnl(recovery_replay_fills):+.2f} 美元；来源标记为故障恢复回放。"
            )
        elif no_trade_reason:
            summary = f"本周期方向为 {decision}，但没有有效成交（{no_trade_reason}）；复盘已完成。"
        else:
            summary = f"本周期方向为 {decision}，共 {len(fills)} 笔成交，已实现 {_pnl(fills):+.2f} 美元。"
        filled_levels = sum(1 for row in grid_rows if row["filled"])
        valid_rs = [float(row["r_multiple"]) for row in grid_rows if row.get("r_multiple") is not None]
        target_count = sum(1 for row in grid_rows if row["outcome"] == "target")
        stop_count = sum(1 for row in grid_rows if row["outcome"] == "stop")
        signal_text = "关键位到价直接成交，不等待额外技术信号" if grid_rows else "本周期没有可执行网格"
        tpsl_review = {
            "order_count": len(grid_rows),
            "valid_geometry_count": len(valid_rs),
            "average_r": round(sum(valid_rs) / len(valid_rs), 4) if valid_rs else None,
            "min_r": min(valid_rs) if valid_rs else None,
            "max_r": max(valid_rs) if valid_rs else None,
            "target_count": target_count,
            "stop_count": stop_count,
            "summary": (
                f"平均 R {sum(valid_rs) / len(valid_rs):.2f}；止盈 {target_count} 笔，止损 {stop_count} 笔"
                if valid_rs else "未形成可评分的止盈止损"
            ),
        }
        next_iteration = self._machine_next_iteration(
            cycle_id,
            plan=plan,
            evidence=evidence,
            direction_review=direction_review,
            range_review=range_review,
            tpsl_review=tpsl_review,
            trade_count=len(recorded_trades),
        )
        return {
            "schema_version": "dualtrack-machine-review-v3",
            "cycle_id": cycle_id,
            "completed": True,
            "decision": decision,
            "decision_mode": plan.get("decision_mode", ""),
            "realized_direction": realized_direction,
            "realized_regime": realized_regime,
            "direction_graded": direction_graded,
            "direction_hit": direction_hit,
            "evidence": evidence,
            "market_review": market_review,
            "direction_review": direction_review,
            "predicted_range": {"low": predicted_low, "high": predicted_high},
            "actual_range": {"low": actual_low, "high": actual_high},
            "range_result": range_result,
            "range_review": range_review,
            "key_level_review": {
                "planned_count": len(key_levels),
                "touched_count": sum(1 for row in key_levels if row["touched"]),
                "levels": key_levels,
                "summary": f"{sum(1 for row in key_levels if row['touched'])}/{len(key_levels)} 个关键位被触及" if key_levels else "未记录关键位",
            },
            "signal_review": {
                "status": "explicit" if grid_rows else "not_applicable",
                "logic": "price_touch" if grid_rows else "none",
                "summary": signal_text,
            },
            "tpsl_review": tpsl_review,
            "grid_orders": grid_rows,
            "fill_count": len(fills),
            "trade_count": len(trades),
            "realized_pnl": _pnl(fills),
            "recorded_fill_count": len(recorded_fills),
            "recorded_trade_count": len(recorded_trades),
            "recorded_realized_pnl": round(_pnl(fills) + _pnl(recovery_replay_fills), 8),
            "execution_evidence_status": "recovery_replay_only" if recovery_replay_fills and not fills else "live_observed",
            "recovery_replay_fill_count": len(recovery_replay_fills),
            "recovery_replay_trade_count": len(recovery_replay_trades),
            "recovery_replay_realized_pnl": _pnl(recovery_replay_fills),
            "execution_review": {
                "status": "recorded" if recorded_fills else "no_fill",
                "fill_count": len(recorded_fills),
                "trade_count": len(recorded_trades),
                "realized_pnl": round(_pnl(fills) + _pnl(recovery_replay_fills), 8),
                "summary": "成交与收益证据已入台账" if recorded_fills else "本周期无成交；这本身不等于策略失败",
            },
            "next_iteration": next_iteration,
            "no_trade_reason": no_trade_reason,
            "planning_error": plan.get("planning_error", ""),
            "summary": summary,
        }

    def _human_review(
        self,
        cycle_id: str,
        bars: tuple[Bar, ...],
        fills: list[dict[str, Any]],
        trades: list[dict[str, Any]],
        *,
        realized_direction: str,
    ) -> dict[str, Any]:
        plan = self.store.load_plan(cycle_id, "human") or {}
        decision = str(plan.get("direction") or "absent")
        key_levels = []
        for value in plan.get("key_levels") or []:
            price = _optional_number(value)
            if price is None:
                continue
            key_levels.append({
                "price": price,
                "touched": any(float(bar.low) <= price <= float(bar.high) for bar in bars),
            })
        trade_geometry = []
        for trade in trades:
            entry = _optional_number(trade.get("entry_price"))
            stop = _optional_number(trade.get("sl"))
            target = _optional_number(trade.get("tp"))
            risk_width = abs(entry - stop) if entry is not None and stop is not None else None
            reward_width = abs(target - entry) if entry is not None and target is not None else None
            r_multiple = reward_width / risk_width if risk_width not in (None, 0.0) and reward_width is not None else None
            events = [str(row.get("event") or "") for row in trade.get("exit_fills") or []]
            trade_geometry.append({
                "trade_id": trade.get("trade_id", ""),
                "risk_width": round(risk_width, 8) if risk_width is not None else None,
                "reward_width": round(reward_width, 8) if reward_width is not None else None,
                "r_multiple": round(r_multiple, 4) if r_multiple is not None else None,
                "outcome": "target" if "target" in events else "stop" if "stop" in events else str(trade.get("status") or "open"),
            })
        valid_rs = [float(row["r_multiple"]) for row in trade_geometry if row.get("r_multiple") is not None]
        signal = str(plan.get("entry_signal") or plan.get("signal") or "").strip()
        market_review = _market_review(bars, self.config)
        realized_regime = str(market_review["realized_regime"])
        direction_graded = decision in {"long", "short", "neutral", "flat"}
        direction_hit = decision == realized_regime if direction_graded else False
        direction_review = _direction_review(decision, realized_regime, direction_graded, direction_hit)
        actual_low = min(float(bar.low) for bar in bars)
        actual_high = max(float(bar.high) for bar in bars)
        predicted = plan.get("range") if isinstance(plan.get("range"), dict) else {}
        predicted_low = _optional_number(predicted.get("low"))
        predicted_high = _optional_number(predicted.get("high"))
        if predicted_low is None or predicted_high is None:
            range_result = "range_missing"
        else:
            low_breach = actual_low < predicted_low
            high_breach = actual_high > predicted_high
            range_result = "both_sides_breached" if low_breach and high_breach else "low_breached" if low_breach else "high_breached" if high_breach else "inside_range"
        range_review = _range_review(predicted_low, predicted_high, actual_low, actual_high, range_result)
        evidence_issues = [] if plan else ["human_plan_missing"]
        evidence = {
            "status": "complete" if not evidence_issues else "insufficient",
            "issues": evidence_issues,
            "bar_count": len(bars),
            "plan_present": bool(plan),
            "fills_present": bool(fills),
            "no_trade_is_valid_outcome": True,
        }
        tpsl_review = {
            "trade_count": len(trade_geometry),
            "valid_geometry_count": len(valid_rs),
            "average_r": round(sum(valid_rs) / len(valid_rs), 4) if valid_rs else None,
            "target_count": sum(1 for row in trade_geometry if row["outcome"] == "target"),
            "stop_count": sum(1 for row in trade_geometry if row["outcome"] == "stop"),
            "trades": trade_geometry,
            "summary": f"平均 R {sum(valid_rs) / len(valid_rs):.2f}" if valid_rs else "没有完整 TP/SL 宽度可评分",
        }
        signal_review = {
            "status": "explicit" if signal else "missing_contract",
            "logic": signal,
            "summary": signal or "本周期未记录入场信号逻辑，无法评分",
        }
        next_iteration = _human_next_iteration(
            cycle_id,
            self.config,
            evidence=evidence,
            direction_review=direction_review,
            range_review=range_review,
            signal_review=signal_review,
            tpsl_review=tpsl_review,
        )
        return {
            "schema_version": "dualtrack-human-review-v3",
            "cycle_id": cycle_id,
            "completed": True,
            "decision": decision,
            "realized_direction": realized_direction,
            "realized_regime": realized_regime,
            "direction_graded": direction_graded,
            "direction_hit": direction_hit,
            "evidence": evidence,
            "market_review": market_review,
            "direction_review": direction_review,
            "predicted_range": {"low": predicted_low, "high": predicted_high},
            "actual_range": {"low": actual_low, "high": actual_high},
            "range_result": range_result,
            "range_review": range_review,
            "key_level_review": {
                "planned_count": len(key_levels),
                "touched_count": sum(1 for row in key_levels if row["touched"]),
                "levels": key_levels,
                "summary": f"{sum(1 for row in key_levels if row['touched'])}/{len(key_levels)} 个关键位被触及" if key_levels else "未记录关键位",
            },
            "signal_review": signal_review,
            "tpsl_review": tpsl_review,
            "fill_count": len(fills),
            "trade_count": len(trades),
            "realized_pnl": _pnl(fills),
            "execution_review": {
                "status": "recorded" if fills else "no_fill",
                "fill_count": len(fills),
                "trade_count": len(trades),
                "realized_pnl": _pnl(fills),
                "summary": "成交与收益证据已入台账" if fills else "本周期无成交；这本身不等于判断失败",
            },
            "next_iteration": next_iteration,
            "summary": f"人工轨 {len(trades)} 笔交易，已实现 {_pnl(fills):+.2f} 美元。",
        }

    def _machine_next_iteration(
        self,
        cycle_id: str,
        *,
        plan: dict[str, Any],
        evidence: dict[str, Any],
        direction_review: dict[str, Any],
        range_review: dict[str, Any],
        tpsl_review: dict[str, Any],
        trade_count: int,
    ) -> dict[str, Any]:
        settings = _review_settings(self.config)
        active_change = plan.get("review_change") if isinstance(plan.get("review_change"), dict) else {}
        if active_change:
            observed_cycles, observed_trades = self._review_change_progress(
                str(active_change.get("change_id") or ""),
                current_cycle_id=cycle_id,
                current_trade_count=trade_count,
            )
            gate = _promotion_gate(settings, observed_cycles=observed_cycles, observed_trades=observed_trades)
            return {
                "status": "ready_for_operator_review" if gate["ready_for_operator_review"] else "collecting",
                "mode": "paper_challenger",
                "auto_apply": False,
                "max_changes": 1,
                "change_id": active_change.get("change_id", ""),
                "dimension": active_change.get("dimension", ""),
                "keep": "除这一项外，其余规则保持不变",
                "change": active_change.get("summary", ""),
                "expected_metric": active_change.get("expected_metric", ""),
                "validation_rule": _validation_rule(settings),
                "promotion_gate": gate,
            }
        gate = _promotion_gate(settings)
        base = {
            "mode": "paper_challenger",
            "auto_apply": False,
            "max_changes": 1,
            "validation_rule": _validation_rule(settings),
            "promotion_gate": gate,
        }
        if evidence.get("status") != "complete":
            return base | {
                "status": "blocked",
                "change_id": "",
                "dimension": "",
                "keep": "证据不足，不改策略",
                "change": "先补齐计划与行情证据",
                "expected_metric": "evidence_completeness",
            }
        if range_review.get("verdict") == "adjust":
            return base | {
                "status": "proposed",
                "change_id": f"{cycle_id}:range:01",
                "dimension": "range",
                "keep": "保留方向、网格触价逻辑和 TP/SL 规则",
                "change": "只测试更稳健的 12 小时区间宽度与边界",
                "expected_metric": "range_breach_rate",
            }
        if direction_review.get("verdict") == "adjust":
            return base | {
                "status": "proposed",
                "change_id": f"{cycle_id}:direction:01",
                "dimension": "direction",
                "keep": "保留区间、网格与 TP/SL 规则",
                "change": "只测试方向/中立判定阈值",
                "expected_metric": "direction_hit_rate",
            }
        average_r = _optional_number(tpsl_review.get("average_r"))
        if average_r is not None and average_r < 1.0:
            return base | {
                "status": "proposed",
                "change_id": f"{cycle_id}:tpsl:01",
                "dimension": "tpsl",
                "keep": "保留方向、区间和网格规则",
                "change": "只测试不低于 1R 的止盈止损几何",
                "expected_metric": "realized_r_multiple",
            }
        return base | {
            "status": "keep",
            "change_id": "",
            "dimension": "",
            "keep": "当前证据不支持改参数，下一周期保持原规则",
            "change": "不改",
            "expected_metric": "stability",
        }

    def _review_change_progress(
        self,
        change_id: str,
        *,
        current_cycle_id: str,
        current_trade_count: int,
    ) -> tuple[int, int]:
        observed_cycles = 1
        observed_trades = int(current_trade_count)
        reviews_dir = self.root / "reviews"
        if not change_id or not reviews_dir.exists():
            return observed_cycles, observed_trades
        for path in reviews_dir.glob("*_machine.json"):
            cycle_id = path.name.removesuffix("_machine.json")
            if cycle_id == current_cycle_id:
                continue
            plan = self.store.machine_plan(cycle_id) or {}
            change = plan.get("review_change") if isinstance(plan.get("review_change"), dict) else {}
            if change.get("change_id") != change_id:
                continue
            rows = load_json(path)
            if not rows:
                continue
            review = rows[-1]
            observed_cycles += 1
            observed_trades += int(review.get("recorded_trade_count", review.get("trade_count", 0)) or 0)
        return observed_cycles, observed_trades

    def _recent_reviews(self, *, limit: int = 8) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        attribution_dir = self.root / "attribution"
        if not attribution_dir.exists():
            return rows
        for path in sorted(attribution_dir.glob("*.json"), reverse=True):
            payloads = load_json(path)
            if not payloads:
                continue
            payload = payloads[-1]
            cycle_id = str(payload.get("cycle_id") or path.stem)
            machine_review = _historical_review_dimensions(
                payload.get("machine_review") or {},
                self.store.machine_plan(cycle_id) or {},
                payload,
                track="machine",
            )
            human_review = _historical_review_dimensions(
                payload.get("human_review") or {},
                self.store.load_plan(cycle_id, "human") or {},
                payload,
                track="human",
            )
            rows.append({
                "cycle_id": cycle_id,
                "tracks": payload.get("tracks") or {},
                "plan_grades": payload.get("plan_grades") or {},
                "machine_review": machine_review,
                "human_review": human_review,
                "recovery_replay": payload.get("recovery_replay") or {},
            })
            if len(rows) >= limit:
                break
        return rows

    def _fills_path(self, cycle_id: str, track: str) -> Path:
        return self.root / "fills" / f"{cycle_id}_{track}.json"

    def _trades_path(self, cycle_id: str, track: str) -> Path:
        return self.root / "trades" / f"{cycle_id}_{track}.json"

    def _recovery_replay_trades_path(self, cycle_id: str) -> Path:
        return self.root / "recovery_replay" / "trades" / f"{cycle_id}_machine.json"

    def _trade_rows(
        self,
        cycle_id: str,
        track: str,
        fills: list[dict[str, Any]],
        *,
        force_rebuild: bool = False,
    ) -> list[dict[str, Any]]:
        existing = load_json(self._trades_path(cycle_id, track))
        if existing and not force_rebuild:
            return existing
        trades = _trades_from_fills(fills, track=track)
        write_json(self._trades_path(cycle_id, track), trades)
        return trades


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


def _track_stats(fills: list[dict[str, Any]], trades: list[dict[str, Any]], *, captured: int, opportunity_count: int) -> dict[str, Any]:
    pnl = _pnl(fills)
    exits = [trade for trade in trades if trade.get("status") == "closed"]
    wins = [trade for trade in exits if float(trade.get("realized_pnl", 0.0)) > 0]
    return {
        "fill_count": len(fills),
        "trade_count": len(trades),
        "closed_trade_count": len(exits),
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


def _is_clean_human_plan(cycle_id: str, plan: dict[str, Any] | None, config: dict[str, Any]) -> bool:
    if not plan or plan.get("status") != "locked" or not plan.get("locked_at"):
        return False
    deadline_min = int(config.get("plan_lock_deadline_min_before_cycle", 0))
    window = cycle_window_from_id(cycle_id, lock_deadline_min_before_cycle=deadline_min)
    return parse_utc(plan.get("locked_at")) <= window.lock_deadline


def _existing_cycle(cycle_id: str, root: Path) -> dict[str, Any]:
    rows = load_json(root / "cycles" / f"{cycle_id}.json")
    return rows[-1] if rows else {}


def _preserve_machine_state(cycle: dict[str, Any], existing: dict[str, Any]) -> None:
    for key in ("layers", "trend_gate_armed", "trend_gate_frozen_at", "trend_gate_source", "stop_hit", "rearms", "range_observation"):
        if key in existing:
            cycle[key] = existing[key]


def _direction(open_price: float, close_price: float) -> str:
    if close_price > open_price:
        return "long"
    if close_price < open_price:
        return "short"
    return "flat"


def _review_settings(config: dict[str, Any]) -> dict[str, Any]:
    configured = config.get("review") if isinstance(config.get("review"), dict) else {}
    return {
        "directional_efficiency_neutral_threshold": float(
            configured.get("directional_efficiency_neutral_threshold", 0.35)
        ),
        "max_changes_per_cycle": 1,
        "minimum_promotion_cycles": int(configured.get("minimum_promotion_cycles", 10)),
        "minimum_promotion_trades": int(configured.get("minimum_promotion_trades", 30)),
        "preferred_promotion_trades": int(configured.get("preferred_promotion_trades", 100)),
        "auto_promote": False,
    }


def _market_review(bars: tuple[Bar, ...], config: dict[str, Any]) -> dict[str, Any]:
    open_price = float(bars[0].open)
    close_price = float(bars[-1].close)
    high = max(float(bar.high) for bar in bars)
    low = min(float(bar.low) for bar in bars)
    width = high - low
    net_move = close_price - open_price
    efficiency = abs(net_move) / width if width > 0 else 0.0
    threshold = _review_settings(config)["directional_efficiency_neutral_threshold"]
    if efficiency < threshold or net_move == 0:
        regime = "neutral"
    else:
        regime = "long" if net_move > 0 else "short"
    return {
        "open": open_price,
        "close": close_price,
        "high": high,
        "low": low,
        "net_move": round(net_move, 8),
        "actual_range_width": round(width, 8),
        "directional_efficiency": round(efficiency, 4),
        "neutral_threshold": threshold,
        "realized_regime": regime,
        "summary": (
            f"实际为{'中立震荡' if regime == 'neutral' else '单边上涨' if regime == 'long' else '单边下跌'}；"
            f"净移动占区间 {efficiency:.0%}"
        ),
    }


def _direction_review(
    decision: str,
    realized_regime: str,
    graded: bool,
    hit: bool,
) -> dict[str, Any]:
    if not graded:
        return {
            "verdict": "insufficient_evidence",
            "decision": decision,
            "realized_regime": realized_regime,
            "summary": "没有可评分的方向计划",
        }
    return {
        "verdict": "keep" if hit else "adjust",
        "decision": decision,
        "realized_regime": realized_regime,
        "hit": hit,
        "summary": (
            f"计划 {decision}，实际 {realized_regime}；判断一致"
            if hit else f"计划 {decision}，实际 {realized_regime}；需要复核方向判定"
        ),
    }


def _range_review(
    predicted_low: float | None,
    predicted_high: float | None,
    actual_low: float,
    actual_high: float,
    result: str,
) -> dict[str, Any]:
    predicted_width = (
        predicted_high - predicted_low
        if predicted_low is not None and predicted_high is not None
        else None
    )
    actual_width = actual_high - actual_low
    if predicted_width is None or predicted_width <= 0:
        verdict = "insufficient_evidence"
        summary = "计划未记录完整区间，无法评分"
    elif result == "inside_range":
        verdict = "keep"
        summary = f"实际 {actual_low:.1f}-{actual_high:.1f} 位于计划区间内"
    else:
        verdict = "adjust"
        summary = f"实际 {actual_low:.1f}-{actual_high:.1f} 突破计划边界（{result}）"
    return {
        "verdict": verdict,
        "result": result,
        "predicted_low": predicted_low,
        "predicted_high": predicted_high,
        "predicted_width": round(predicted_width, 8) if predicted_width is not None else None,
        "actual_low": actual_low,
        "actual_high": actual_high,
        "actual_width": round(actual_width, 8),
        "low_breached": result in {"low_breached", "both_sides_breached"},
        "high_breached": result in {"high_breached", "both_sides_breached"},
        "summary": summary,
    }


def _validation_rule(settings: dict[str, Any]) -> str:
    return (
        f"至少 {settings['minimum_promotion_cycles']} 个完整周期且 "
        f"{settings['minimum_promotion_trades']} 笔交易；{settings['preferred_promotion_trades']} 笔更稳健"
    )


def _promotion_gate(
    settings: dict[str, Any],
    *,
    observed_cycles: int = 0,
    observed_trades: int = 0,
) -> dict[str, Any]:
    ready = (
        observed_cycles >= settings["minimum_promotion_cycles"]
        and observed_trades >= settings["minimum_promotion_trades"]
    )
    return {
        "minimum_cycles": settings["minimum_promotion_cycles"],
        "minimum_trades": settings["minimum_promotion_trades"],
        "preferred_trades": settings["preferred_promotion_trades"],
        "observed_cycles": observed_cycles,
        "observed_trades": observed_trades,
        "ready_for_operator_review": ready,
        "auto_promote": False,
    }


def _human_next_iteration(
    cycle_id: str,
    config: dict[str, Any],
    *,
    evidence: dict[str, Any],
    direction_review: dict[str, Any],
    range_review: dict[str, Any],
    signal_review: dict[str, Any],
    tpsl_review: dict[str, Any],
) -> dict[str, Any]:
    settings = _review_settings(config)
    base = {
        "mode": "human_advisory",
        "auto_apply": False,
        "max_changes": 1,
        "validation_rule": "只给人工参考，不自动修改或锁定下一张作战单",
        "promotion_gate": _promotion_gate(settings),
    }
    if evidence.get("status") != "complete":
        return base | {
            "status": "blocked",
            "change_id": "",
            "dimension": "",
            "keep": "证据不足，不建议改计划",
            "change": "先补齐人工作战单证据",
            "expected_metric": "evidence_completeness",
        }
    if range_review.get("verdict") == "adjust":
        return base | {
            "status": "proposed",
            "change_id": f"{cycle_id}:human:range:01",
            "dimension": "range",
            "keep": "保留人工方向与入场自主权",
            "change": "建议下一轮单独复核区间宽度与边界",
            "expected_metric": "range_breach_rate",
        }
    if signal_review.get("status") == "missing_contract":
        return base | {
            "status": "proposed",
            "change_id": f"{cycle_id}:human:signal:01",
            "dimension": "signal",
            "keep": "保留方向、区间和风险参数",
            "change": "建议下一轮明确写出入场信号逻辑",
            "expected_metric": "signal_evidence_completeness",
        }
    if direction_review.get("verdict") == "adjust":
        return base | {
            "status": "proposed",
            "change_id": f"{cycle_id}:human:direction:01",
            "dimension": "direction",
            "keep": "保留关键位、信号和 TP/SL 规则",
            "change": "建议下一轮单独复核方向/中立判断",
            "expected_metric": "direction_hit_rate",
        }
    if not int(tpsl_review.get("valid_geometry_count") or 0):
        return base | {
            "status": "proposed",
            "change_id": f"{cycle_id}:human:tpsl:01",
            "dimension": "tpsl",
            "keep": "保留方向、区间和信号逻辑",
            "change": "建议下一轮补齐可评分的 TP/SL 宽度",
            "expected_metric": "tpsl_evidence_completeness",
        }
    return base | {
        "status": "keep",
        "change_id": "",
        "dimension": "",
        "keep": "当前证据不支持改动，下一轮保持原计划方法",
        "change": "不改",
        "expected_metric": "stability",
    }


def _pnl(fills: list[dict[str, Any]]) -> float:
    return round(sum(float(fill.get("realized_pnl", 0.0)) for fill in fills), 8)


def _closed_trade_count(fills: list[dict[str, Any]]) -> int:
    return sum(
        1
        for trade in _trades_from_fills(fills, track="machine")
        if str(trade.get("status") or "").lower() == "closed"
    )


def _live_execution_fills(fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [fill for fill in fills if fill.get("execution_origin") != "recovery_replay"]


def _recovery_replay_fills(fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [fill for fill in fills if fill.get("execution_origin") == "recovery_replay"]


def _optional_number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _plan_stop(plan: dict[str, Any], side: str) -> float | None:
    wanted = "below" if side == "long" else "above"
    for row in plan.get("invalidation") or []:
        if str(row.get("side") or "") == wanted:
            return _optional_number(row.get("price"))
    return None


def _historical_review_dimensions(
    review: dict[str, Any],
    plan: dict[str, Any],
    attribution: dict[str, Any],
    *,
    track: str,
) -> dict[str, Any]:
    """Add explicit dimensions to legacy reviews without inventing outcomes."""
    normalized = dict(review)
    normalized.setdefault("cycle_id", attribution.get("cycle_id", ""))
    normalized.setdefault("decision", plan.get("direction", "absent"))
    actual_range = normalized.get("actual_range") if isinstance(normalized.get("actual_range"), dict) else {}
    if not actual_range:
        machine_review = attribution.get("machine_review") or {}
        actual_range = machine_review.get("actual_range") if isinstance(machine_review.get("actual_range"), dict) else {}
    actual_low = _optional_number(actual_range.get("low"))
    actual_high = _optional_number(actual_range.get("high"))
    if "market_review" not in normalized:
        realized_regime = str(normalized.get("realized_regime") or normalized.get("realized_direction") or "unknown")
        normalized["market_review"] = {
            "low": actual_low,
            "high": actual_high,
            "actual_range_width": (
                round(actual_high - actual_low, 8)
                if actual_low is not None and actual_high is not None
                else None
            ),
            "realized_regime": realized_regime,
            "summary": "旧版复盘未记录完整开收盘路径，不能重算行情形态",
        }
    if "direction_review" not in normalized:
        graded = bool(normalized.get("direction_graded"))
        hit = bool(normalized.get("direction_hit"))
        normalized["direction_review"] = {
            "verdict": "keep" if graded and hit else "adjust" if graded else "insufficient_evidence",
            "decision": normalized.get("decision", "absent"),
            "realized_regime": normalized["market_review"].get("realized_regime", "unknown"),
            "hit": hit if graded else None,
            "summary": (
                "旧版方向判卷为正确" if graded and hit
                else "旧版方向判卷为偏差" if graded
                else "旧版复盘没有可评分方向"
            ),
        }
    if "range_review" not in normalized:
        predicted = normalized.get("predicted_range") if isinstance(normalized.get("predicted_range"), dict) else plan.get("range") or {}
        predicted_low = _optional_number(predicted.get("low"))
        predicted_high = _optional_number(predicted.get("high"))
        if actual_low is not None and actual_high is not None:
            result = str(normalized.get("range_result") or "")
            if not result and predicted_low is not None and predicted_high is not None:
                low_breach = actual_low < predicted_low
                high_breach = actual_high > predicted_high
                result = "both_sides_breached" if low_breach and high_breach else "low_breached" if low_breach else "high_breached" if high_breach else "inside_range"
            normalized["range_review"] = _range_review(
                predicted_low,
                predicted_high,
                actual_low,
                actual_high,
                result or "range_missing",
            )
        else:
            normalized["range_review"] = {
                "verdict": "insufficient_evidence",
                "summary": "旧版复盘缺少实际高低点，无法评分区间",
            }
    if "key_level_review" not in normalized:
        levels = []
        for value in plan.get("key_levels") or []:
            price = _optional_number(value)
            if price is None:
                continue
            touched = None if actual_low is None or actual_high is None else actual_low <= price <= actual_high
            levels.append({"price": price, "touched": touched})
        touched_count = sum(1 for row in levels if row["touched"] is True)
        if not levels:
            key_summary = "未记录关键位"
        elif actual_low is None or actual_high is None:
            key_summary = f"记录了 {len(levels)} 个关键位；历史触达证据不足"
        else:
            key_summary = f"{touched_count}/{len(levels)} 个关键位被触及"
        normalized["key_level_review"] = {
            "planned_count": len(levels),
            "touched_count": touched_count,
            "levels": levels,
            "summary": key_summary,
        }
    if "signal_review" not in normalized:
        signal = str(plan.get("entry_signal") or plan.get("signal") or "").strip()
        if track == "machine" and plan.get("grid_orders"):
            signal_review = {
                "status": "explicit",
                "logic": "price_touch",
                "summary": "关键位到价直接成交，不等待额外技术信号",
            }
        else:
            signal_review = {
                "status": "explicit" if signal else "missing_contract",
                "logic": signal,
                "summary": signal or "本周期未记录入场信号逻辑，无法评分",
            }
        normalized["signal_review"] = signal_review
    if "tpsl_review" not in normalized:
        geometry: list[dict[str, Any]] = []
        if track == "machine":
            for index, order in enumerate(plan.get("grid_orders") or []):
                side = str(order.get("side") or plan.get("direction") or "")
                entry = _optional_number(order.get("entry"))
                target = _optional_number(order.get("take_profit"))
                stop = _plan_stop(plan, side)
                risk = abs(entry - stop) if entry is not None and stop is not None else None
                reward = abs(target - entry) if entry is not None and target is not None else None
                geometry.append({
                    "rung": index,
                    "risk_width": risk,
                    "reward_width": reward,
                    "r_multiple": reward / risk if risk not in (None, 0.0) and reward is not None else None,
                })
        else:
            for trade in (attribution.get("trades") or {}).get("human") or []:
                entry = _optional_number(trade.get("entry_price"))
                target = _optional_number(trade.get("tp"))
                stop = _optional_number(trade.get("sl"))
                risk = abs(entry - stop) if entry is not None and stop is not None else None
                reward = abs(target - entry) if entry is not None and target is not None else None
                geometry.append({
                    "trade_id": trade.get("trade_id", ""),
                    "risk_width": risk,
                    "reward_width": reward,
                    "r_multiple": reward / risk if risk not in (None, 0.0) and reward is not None else None,
                })
        values = [float(row["r_multiple"]) for row in geometry if row.get("r_multiple") is not None]
        normalized["tpsl_review"] = {
            "valid_geometry_count": len(values),
            "average_r": round(sum(values) / len(values), 4) if values else None,
            "summary": f"平均 R {sum(values) / len(values):.2f}" if values else "没有完整 TP/SL 宽度可评分",
        }
    track_stats = (attribution.get("tracks") or {}).get(track) or {}
    normalized.setdefault("trade_count", track_stats.get("trade_count", 0))
    normalized.setdefault("realized_pnl", track_stats.get("realized_pnl", 0.0))
    if track == "machine":
        recovery = attribution.get("recovery_replay") or {}
        normalized.setdefault("recorded_trade_count", int(normalized.get("trade_count") or 0) + int(recovery.get("trade_count") or 0))
        normalized.setdefault("recorded_realized_pnl", round(float(normalized.get("realized_pnl") or 0.0) + float(recovery.get("realized_pnl") or 0.0), 8))
        if recovery.get("fill_count") and "不计入实时 paper 收益" in str(normalized.get("summary") or ""):
            normalized["summary"] = (
                f"故障恢复回放 {int(recovery.get('trade_count') or 0)} 笔，"
                f"已记录收益 {float(recovery.get('realized_pnl') or 0.0):+.2f} 美元；"
                "计入台账，不混入实时成交绩效。"
            )
    normalized.setdefault("evidence", {
        "status": "insufficient",
        "issues": ["legacy_review_contract"],
        "no_trade_is_valid_outcome": True,
    })
    normalized.setdefault("next_iteration", {
        "status": "legacy",
        "mode": "paper_challenger" if track == "machine" else "human_advisory",
        "auto_apply": False,
        "max_changes": 1,
        "change_id": "",
        "dimension": "",
        "keep": "旧版记录只用于查看，不据此自动改变下一轮",
        "change": "等待新版结构化复盘",
        "expected_metric": "evidence_completeness",
        "validation_rule": "旧版证据不足，不能形成升级结论",
        "promotion_gate": {
            "minimum_cycles": 10,
            "minimum_trades": 30,
            "preferred_trades": 100,
            "observed_cycles": 0,
            "observed_trades": 0,
            "ready_for_operator_review": False,
            "auto_promote": False,
        },
    })
    normalized.setdefault("summary", "历史周期已完成；旧版复盘未记录完整结论。")
    return normalized


def filter_invalid_machine_fills(fills: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Remove machine fills that violate basic execution geometry.

    Machine fills are simulated and can be recomputed. If an entry is already
    on the wrong side of its protective stop, treating it as a real trade makes
    the UI and PnL lie. Drop that entry and the paired immediate exit.
    """
    kept: list[dict[str, Any]] = []
    invalid_entries: dict[tuple[str, str, str], int] = {}
    reasons: list[dict[str, Any]] = []

    for fill in fills:
        event = str(fill.get("event") or "")
        if event == "entry":
            reason = _invalid_machine_entry_reason(fill)
            if reason:
                exit_key = _machine_exit_pair_key(fill)
                invalid_entries[exit_key] = invalid_entries.get(exit_key, 0) + 1
                reasons.append({
                    "fill_id": fill.get("fill_id", ""),
                    "reason": reason,
                    "price": fill.get("price"),
                    "sl": fill.get("sl"),
                    "tp": fill.get("tp"),
                })
                continue
            kept.append(fill)
            continue

        pair_key = _machine_exit_key(fill)
        if pair_key in invalid_entries and invalid_entries[pair_key] > 0:
            invalid_entries[pair_key] -= 1
            reasons.append({
                "fill_id": fill.get("fill_id", ""),
                "reason": "paired_exit_for_invalid_entry",
                "price": fill.get("price"),
                "sl": fill.get("sl"),
                "tp": fill.get("tp"),
            })
            continue
        kept.append(fill)

    return kept, {
        "invalid_machine_fill_count": len(fills) - len(kept),
        "invalid_machine_fill_reasons": reasons,
    }


def _invalid_machine_entry_reason(fill: dict[str, Any]) -> str:
    side = "long" if str(fill.get("side") or "").lower() == "buy" else "short"
    price = _finite_float(fill.get("price"))
    sl = _finite_float(fill.get("sl"))
    tp = _finite_float(fill.get("tp"))
    if price is None:
        return "missing_entry_price"
    if side == "long":
        if sl is not None and sl >= price:
            return "long_stop_not_below_entry"
        if tp is not None and tp <= price:
            return "long_target_not_above_entry"
    else:
        if sl is not None and sl <= price:
            return "short_stop_not_above_entry"
        if tp is not None and tp >= price:
            return "short_target_not_below_entry"
    return ""


def _machine_exit_pair_key(fill: dict[str, Any]) -> tuple[str, str, str]:
    exit_side = "sell" if str(fill.get("side") or "").lower() == "buy" else "buy"
    return (_fill_position_id(fill), str(fill.get("rung", "")), exit_side)


def _machine_exit_key(fill: dict[str, Any]) -> tuple[str, str, str]:
    return (_fill_position_id(fill), str(fill.get("rung", "")), str(fill.get("side") or "").lower())


def _finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _fill_units(fill: dict[str, Any]) -> float:
    try:
        if fill.get("pnl_units") not in (None, ""):
            units = float(fill.get("pnl_units") or 0.0)
            if math.isfinite(units) and units > 0:
                return units
        if fill.get("units") not in (None, ""):
            units = float(fill.get("units") or 0.0)
            if math.isfinite(units) and units > 0:
                return units
        price = float(fill.get("price") or 0.0)
        notional = float(fill.get("notional") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(price) or not math.isfinite(notional) or price <= 0 or notional <= 0:
        return 0.0
    return notional / price


def _fill_position_id(fill: dict[str, Any]) -> str:
    return str(fill.get("position_id") or fill.get("layer") or "")


def _trade_matches_exit(trade: dict[str, Any], fill: dict[str, Any]) -> bool:
    if trade.get("status") == "closed":
        return False
    exit_side = str(fill.get("side") or "").lower()
    wanted_trade_side = "long" if exit_side == "sell" else "short" if exit_side == "buy" else ""
    if wanted_trade_side and str(trade.get("side") or "").lower() != wanted_trade_side:
        return False
    fill_position = _fill_position_id(fill)
    if fill_position and str(trade.get("position_id") or "") != fill_position:
        return False
    fill_rung = fill.get("rung")
    if fill_rung not in (None, ""):
        trade_rung = trade.get("rung")
        if trade_rung in (None, "") or str(trade_rung) != str(fill_rung):
            return False
    remaining = trade.get("remaining_units", trade.get("units", 0.0))
    try:
        return float(remaining or 0.0) > 1e-9
    except (TypeError, ValueError):
        return False


def _fallback_exit_matches(trades: dict[str, dict[str, Any]], fill: dict[str, Any]) -> list[dict[str, Any]]:
    matches = [trade for trade in trades.values() if _trade_matches_exit(trade, fill)]
    matches.sort(key=lambda trade: str(trade.get("entry_ts") or ""), reverse=True)
    if str(fill.get("event") or "") == "flatten":
        return matches
    return matches[:1]


def apply_unrealized(trades: list[dict[str, Any]], mark_price: Any, *, mark_fresh: bool = True) -> list[dict[str, Any]]:
    try:
        mark = float(mark_price)
    except (TypeError, ValueError):
        mark = math.nan
    usable_mark = bool(mark_fresh) and math.isfinite(mark)
    rows: list[dict[str, Any]] = []
    for trade in trades:
        row = dict(trade)
        if row.get("status") != "open":
            rows.append(row)
            continue
        if not usable_mark:
            row["unrealized_pnl"] = None
            rows.append(row)
            continue
        try:
            entry = float(row.get("entry_price"))
            units = float(row.get("remaining_units", row.get("units", 0.0)) or 0.0)
        except (TypeError, ValueError):
            row["unrealized_pnl"] = None
            rows.append(row)
            continue
        if not math.isfinite(entry) or not math.isfinite(units):
            row["unrealized_pnl"] = None
            rows.append(row)
            continue
        direction = -1.0 if str(row.get("side")).lower() == "short" else 1.0
        row["unrealized_pnl"] = round((mark - entry) * units * direction, 8)
        rows.append(row)
    return rows


def _trades_from_fills(fills: list[dict[str, Any]], *, track: str) -> list[dict[str, Any]]:
    trades: dict[str, dict[str, Any]] = {}
    for fill in fills:
        event = str(fill.get("event") or "")
        if event == "entry":
            trade_id = str(fill.get("trade_id") or fill.get("fill_id") or "")
            units = _fill_units(fill)
            trades[trade_id] = {
                "trade_id": trade_id,
                "track": track,
                "position_id": _fill_position_id(fill),
                "layer": fill.get("layer", ""),
                "rung": fill.get("rung", ""),
                "side": "long" if fill.get("side") == "buy" else "short",
                "entry_fill_id": fill.get("fill_id", ""),
                "entry_ts": fill.get("ts", ""),
                "entry_price": fill.get("price"),
                "sl": fill.get("sl"),
                "tp": fill.get("tp"),
                "units": units,
                "remaining_units": fill.get("remaining_units", units),
                "entry_cost": fill.get("cost", 0.0),
                "exit_fills": [],
                "gross_pnl": 0.0,
                "realized_pnl": round(float(fill.get("realized_pnl", 0.0) or 0.0), 8),
                "status": fill.get("position_status", "open"),
                "execution_origin": fill.get("execution_origin", "legacy_unclassified"),
            }
            continue
        if event not in {"exit", "stop", "target", "flatten"}:
            continue
        matches = fill.get("matched_entries") or []
        if not matches:
            matches = []
            fallback_trades = _fallback_exit_matches(trades, fill)
            fallback_units = _fill_units(fill)
            for fallback_trade in fallback_trades:
                remaining = float(fallback_trade.get("remaining_units", fallback_trade.get("units", 0.0)) or 0.0)
                # Historical machine grid fills recorded entry notional on the
                # paired target/stop. Recomputing quantity at the exit price
                # leaves a phantom residual whenever the price differs. For a
                # rung-level machine protective exit without an explicit
                # quantity, the only safe reconstruction is to close the
                # entire matched remaining lot. Raw fills remain untouched.
                if (
                    track == "machine"
                    and event in {"stop", "target"}
                    and not _fill_has_explicit_units(fill)
                ):
                    units = remaining
                else:
                    units = fallback_units if fallback_units > 0 else remaining
                matches.append({
                    "trade_id": str(fallback_trade.get("trade_id") or ""),
                    "units": min(units, remaining) if remaining > 0 else units,
                    "gross_pnl": fill.get("gross_pnl", 0.0),
                    "realized_pnl": fill.get("realized_pnl", 0.0),
                })
        for match in matches:
            trade_id = str(match.get("trade_id") or fill.get("trade_id") or "")
            trade = trades.get(trade_id)
            if not trade:
                continue
            current_remaining = float(trade.get("remaining_units", trade.get("units", 0.0)) or 0.0)
            closed_units = float(match.get("units", 0.0) or 0.0)
            trade["exit_fills"].append({
                "fill_id": fill.get("fill_id", ""),
                "event": event,
                "ts": fill.get("ts", ""),
                "price": fill.get("price"),
                "units": match.get("units"),
                "realized_pnl": match.get("realized_pnl", fill.get("realized_pnl", 0.0)),
                "execution_origin": fill.get("execution_origin", "legacy_unclassified"),
            })
            trade["gross_pnl"] = round(float(trade.get("gross_pnl", 0.0)) + float(match.get("gross_pnl", fill.get("gross_pnl", 0.0)) or 0.0), 8)
            trade["realized_pnl"] = round(float(trade.get("realized_pnl", 0.0)) + float(match.get("realized_pnl", fill.get("realized_pnl", 0.0)) or 0.0), 8)
            trade["remaining_units"] = max(0.0, round(current_remaining - closed_units, 10))
            trade["exit_ts"] = fill.get("ts", "")
            trade["exit_price"] = fill.get("price")
            trade["status"] = "closed" if float(trade.get("remaining_units", 0.0) or 0.0) <= 1e-9 else "open"
    return list(trades.values())


def _fill_has_explicit_units(fill: dict[str, Any]) -> bool:
    return any(fill.get(key) not in (None, "") for key in ("pnl_units", "units", "quantity", "contracts"))


def _iso_week(date: str) -> str | None:
    try:
        parsed = datetime.fromisoformat(str(date))
    except (TypeError, ValueError):
        return None
    iso = parsed.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _cycle_date(cycle_id: str) -> str | None:
    date = str(cycle_id).split("_", 1)[0]
    return date if _iso_week(date) is not None else None


def _now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
