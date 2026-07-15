"""Dashboard boards: performance board, strategy detail, review loop, and dashboard health."""

from __future__ import annotations

from datetime import datetime, timezone

from services.journal_store import load_json
from services.edge_judgment import EdgeJudgment
from services.strategy_book import StrategyBook
from services.trade_record_card import TradeRecordCardBuilder


class BoardsMixin:
    def _performance_board(
        self,
        run_date: str,
        leaderboard: dict,
        strategy_summary: dict,
        bars: list[dict],
        strategy_config: dict,
        trading_plan: dict,
        health: dict,
        data_source_preflight: dict,
        ohlc_quality: dict,
        market_data_gate: dict,
    ) -> dict:
        rows = leaderboard.get("strategies", []) if isinstance(leaderboard, dict) else []
        summary_rows = strategy_summary.get("strategies", []) if isinstance(strategy_summary, dict) else []
        summary_by_id = {
            str(item.get("strategy_id")): item
            for item in summary_rows
            if isinstance(item, dict) and item.get("strategy_id")
        }
        known_ids: list[str] = []
        for row in rows:
            sid = row.get("strategy_id") if isinstance(row, dict) else ""
            if sid and sid not in known_ids:
                known_ids.append(sid)
        strategies_dir = self.output_root / "strategies"
        if strategies_dir.exists():
            for namespace in sorted(p for p in strategies_dir.iterdir() if p.is_dir()):
                if namespace.name not in known_ids:
                    known_ids.append(namespace.name)
        for sid in summary_by_id:
            if sid not in known_ids:
                known_ids.append(sid)

        by_id = {row.get("strategy_id"): row for row in rows if isinstance(row, dict)}
        strategy_rows = []
        for sid in known_ids:
            source = dict(by_id.get(sid, {}))
            config = strategy_config.get(sid, {}) if isinstance(strategy_config, dict) else {}
            summary = summary_by_id.get(sid, {})
            namespace = self._strategy_namespace(sid)
            daily_execution = source.get("daily_execution") or self._daily_execution_for_date(namespace, run_date)
            demo_blocker = self._demo_blocker(namespace, run_date)
            frequency_diagnostics = self._strategy_frequency_diagnostics(
                namespace,
                run_date,
                daily_execution,
                demo_blocker,
            )
            today_count = int(frequency_diagnostics.get("executed_trade_count") or 0)
            daily_execution = {**daily_execution, "executed_trade_count": today_count}
            trade_count_7d = self._executed_trade_count_window(sid, run_date, 7)
            inactive_days = self._consecutive_inactive_days(sid, run_date, 7)
            equity_points = source.get("equity_points") or []
            has_nav = bool(equity_points)
            sample_status = "active" if today_count >= 1 else "inactive_low_sample"
            if inactive_days >= 3:
                sample_status = "low_frequency_failure"
            closed_loop_audit = self._strategy_closed_loop_audit(namespace, run_date, today_count)
            closed_trade_count = self._safe_int(source.get("closed_trades"))
            open_trade_count = self._safe_int(source.get("open_trades"))
            strategy_book = StrategyBook(
                namespace,
                strategy_id=sid,
                strategy_config=config,
                starting_equity=float(config.get("starting_equity", source.get("starting_equity") or summary.get("starting_equity") or 10_000)),
            ).build(run_date, persist=False)
            edge_judgment = EdgeJudgment(namespace, strategy_id=sid).build(run_date, persist=False)
            realized_evidence = self._strategy_realized_evidence(
                namespace=namespace,
                source=source,
                closed_trade_count=closed_trade_count,
                open_trade_count=open_trade_count,
            )
            nav_quality = self._nav_quality(
                equity_points,
                bars,
                closed_trade_count=closed_trade_count,
                open_trade_count=open_trade_count,
            )
            strategy_rows.append({
                "strategy_id": sid,
                "rank": source.get("rank"),
                "engine": source.get("engine") or config.get("engine", "unknown"),
                "timeframe": source.get("timeframe") or config.get("timeframe", "5m"),
                "classification": source.get("classification") or config.get("classification", {}),
                "execution_profile": summary.get("execution_profile", {}),
                "status": summary.get("status", "waiting_for_samples" if not has_nav else "ok"),
                "today_trade_count": today_count,
                "trade_count_7d": trade_count_7d,
                "inactive_days": inactive_days,
                "sample_status": sample_status,
                "is_low_sample": today_count < 1,
                "low_sample_reason": "executed_trade_count < 1" if today_count < 1 else "",
                "daily_execution": daily_execution,
                "starting_equity": source.get("starting_equity") or summary.get("starting_equity"),
                "current_equity": source.get("current_equity") or summary.get("current_equity"),
                "return_pct": source.get("return_pct"),
                "gold_return_pct": source.get("gold_return_pct"),
                "vs_gold_pct": source.get("vs_gold_pct"),
                "max_drawdown_pct": source.get("max_drawdown_pct") if source.get("max_drawdown_pct") is not None else summary.get("current_drawdown_pct"),
                "win_rate": source.get("win_rate"),
                "profit_factor": source.get("profit_factor"),
                "net_pnl": source.get("net_pnl"),
                "closed_trades": closed_trade_count,
                "open_trades": open_trade_count,
                "realized_evidence": realized_evidence,
                "performance_confidence": self._performance_confidence(
                    today_trade_count=today_count,
                    trade_count_7d=trade_count_7d,
                    closed_trade_count=closed_trade_count,
                    open_trade_count=open_trade_count,
                    return_pct=source.get("return_pct"),
                    win_rate=source.get("win_rate"),
                    closed_loop_status=closed_loop_audit.get("status", ""),
                ),
                "position": source.get("position") or {"status": "flat", "summary": "flat"},
                "demo_blocker": demo_blocker,
                "frequency_diagnostics": frequency_diagnostics,
                "closed_loop_audit": closed_loop_audit,
                "evidence_provenance": closed_loop_audit.get("evidence_provenance", {}),
                "strategy_book": strategy_book,
                "edge_judgment": edge_judgment,
                "nav_points": equity_points,
                "nav_quality": nav_quality,
                "has_nav": has_nav,
                "freshness": self._artifact_freshness(namespace / "equity_curve" / "current.json"),
            })
        strategy_rows.sort(key=lambda r: (r.get("return_pct") is None, -(r.get("return_pct") or 0.0), r["strategy_id"]))
        for idx, row in enumerate(strategy_rows, 1):
            row["rank"] = row.get("rank") or idx
        top_five = [row["strategy_id"] for row in strategy_rows if row.get("has_nav")][:5]
        today_executed = sum(int(row.get("today_trade_count") or 0) for row in strategy_rows)
        low_sample = [row["strategy_id"] for row in strategy_rows if row.get("is_low_sample")]
        frequency_board = self._frequency_board(strategy_rows)
        explainability_gap_count = sum(int(row.get("closed_loop_audit", {}).get("gap_count") or 0) for row in strategy_rows)
        explainability_gap_groups = self._board_gap_groups(strategy_rows)
        explainability_gap_strategy_ids = [
            row["strategy_id"]
            for row in strategy_rows
            if int(row.get("closed_loop_audit", {}).get("warn_count") or 0) > 0
        ]
        evidence_provenance_summary = self._board_evidence_provenance(strategy_rows)
        closed_loop_issue_strategy_ids = [
            row["strategy_id"]
            for row in strategy_rows
            if row.get("today_trade_count", 0) >= 1
            and row.get("closed_loop_audit", {}).get("status") != "closed_loop_ok"
        ]
        active_strategy_id = trading_plan.get("active_strategy_id") or trading_plan.get("strategy_runtime", {}).get("active_strategy_id", "")
        active_demo_blocker = next((row.get("demo_blocker") for row in strategy_rows if row.get("strategy_id") == active_strategy_id), {})
        active_demo_blocked = bool(active_demo_blocker and active_demo_blocker.get("blocked"))
        return {
            "run_date": run_date,
            "generated_at": leaderboard.get("generated_at", "") if isinstance(leaderboard, dict) else "",
            "strategy_count": len(strategy_rows),
            "default_visible_strategy_ids": top_five,
            "today_executed_trade_count": today_executed,
            "today_has_real_trade": today_executed > 0,
            "low_sample_strategy_ids": low_sample,
            "frequency_board": frequency_board,
            "explainability_gap_count": explainability_gap_count,
            "explainability_gap_root_cause_count": len(explainability_gap_groups),
            "explainability_gap_groups": explainability_gap_groups,
            "explainability_gap_strategy_ids": explainability_gap_strategy_ids,
            "evidence_provenance": evidence_provenance_summary,
            "closed_loop_issue_strategy_ids": closed_loop_issue_strategy_ids,
            "active_strategy_id": active_strategy_id,
            "can_trade_today": bool(trading_plan.get("allowed_to_trade")) and not active_demo_blocked,
            "active_demo_blocker": active_demo_blocker or {},
            "trade_decision": trading_plan.get("decision") or trading_plan.get("status", ""),
            "data_mode": data_source_preflight.get("live_data_mode") or data_source_preflight.get("status", ""),
            "ohlc_quality": ohlc_quality,
            "market_data_gate": market_data_gate,
            "health_status": health.get("status", "unknown") if isinstance(health, dict) else "unknown",
            "gold_nav": self._gold_nav_from_bars(
                self._gold_nav_bars_for_strategy_window(
                    run_date=run_date,
                    bars=bars,
                    strategy_rows=strategy_rows,
                )
            ),
            "nav_quality_summary": self._nav_quality_summary(strategy_rows, bars),
            "realized_evidence_summary": self._realized_evidence_summary(strategy_rows),
            "strategies": strategy_rows,
            "leaderboard": strategy_rows,
        }

    def _strategy_detail(
        self,
        *,
        run_date: str,
        strategy_id: str,
        timeframe: str,
        bars: list[dict],
        signals: list[dict],
        tickets: list[dict],
        orders: list[dict],
        decisions: list[dict],
        open_trades: list[dict],
        closed_trades: list[dict],
        all_closed_trades: list[dict],
        paper_performance: dict,
        equity_curve: dict,
        risk_blocks: list[dict],
        risk_monitor: dict,
        live_reconciliation: dict,
        paper_exit_monitor: dict,
        paper_exit_decisions: dict,
        paper_reconciliation: dict,
        strategy_config: dict,
        ohlc_quality: dict,
        nav_curve_intraday: dict,
    ) -> dict:
        sid = strategy_id or (self.output_root.name if self.output_root.parent.name == "strategies" else "")
        config = strategy_config.get(sid, {}) if sid and isinstance(strategy_config, dict) else {}
        summary = paper_performance.get("summary", {}) if isinstance(paper_performance, dict) else {}
        open_rows = open_trades if isinstance(open_trades, list) else []
        closed_rows = all_closed_trades if all_closed_trades else closed_trades
        order_rows = orders if isinstance(orders, list) else []
        trace = self._trade_trace_artifacts(
            artifact_root=self.output_root,
            run_date=run_date,
            trades=[*open_rows, *closed_rows],
            signals=signals,
            tickets=tickets,
            decisions=decisions,
            risk_blocks=risk_blocks,
            orders=order_rows,
        )
        trace_signals = trace["signals"]
        trace_tickets = trace["tickets"]
        trace_decisions = trace["decisions"]
        trace_risk_blocks = trace["risk_blocks"]
        trace_orders = trace["orders"]
        review_events = self._review_events(trace_signals, trace_decisions, trace_risk_blocks)
        trades = self._decorated_trades(
            open_rows,
            closed_rows,
            trace_signals,
            trace_tickets,
            trace_decisions,
            trace_risk_blocks,
            trace_orders,
            paper_exit_decisions,
            risk_monitor,
            review_events,
            strategy_id=sid,
            strategy_config=config,
            run_date=run_date,
            latest_price=float(bars[-1].get("close")) if bars and bars[-1].get("close") is not None else None,
        )
        open_decorated = self._decorated_trades(
            open_rows,
            [],
            trace_signals,
            trace_tickets,
            trace_decisions,
            trace_risk_blocks,
            trace_orders,
            paper_exit_decisions,
            risk_monitor,
            review_events,
            strategy_id=sid,
            strategy_config=config,
            run_date=run_date,
            latest_price=float(bars[-1].get("close")) if bars and bars[-1].get("close") is not None else None,
        )
        closed_decorated = self._decorated_trades(
            [],
            closed_rows,
            trace_signals,
            trace_tickets,
            trace_decisions,
            trace_risk_blocks,
            trace_orders,
            paper_exit_decisions,
            risk_monitor,
            review_events,
            strategy_id=sid,
            strategy_config=config,
            run_date=run_date,
            latest_price=float(bars[-1].get("close")) if bars and bars[-1].get("close") is not None else None,
        )
        replay_bars, replay_ohlc = self._replay_bars_for_trades(
            strategy_id=sid,
            requested_timeframe=timeframe,
            artifact_bars=bars,
            trades=trades,
        )
        explainability_gaps = self._explainability_gaps(trades, trace_signals, trace_tickets, trace_orders, trace_decisions)
        explainability_warn_count = sum(1 for item in explainability_gaps if item.get("severity") == "warn")
        today_execution = self._daily_execution_for_date(self.output_root, run_date)
        today_trade_count = self._safe_int(today_execution.get("executed_trade_count"))
        trade_count_7d = self._executed_trade_count_window(sid, run_date, 7) if sid else today_trade_count
        open_trade_count = self._safe_int(summary.get("open_trade_count", len(open_rows)))
        closed_today_count = self._safe_int(summary.get("closed_today_count", len(closed_trades)))
        closed_all_count = self._safe_int(summary.get("closed_all_count", len(closed_rows)))
        nav_points = equity_curve.get("points", []) if isinstance(equity_curve, dict) else []
        strategy_book = StrategyBook(
            self.output_root,
            strategy_id=sid,
            strategy_config=config,
            starting_equity=float(config.get("starting_equity", 10_000)),
        ).build(run_date, persist=False)
        edge_judgment = EdgeJudgment(self.output_root, strategy_id=sid).build(run_date, persist=False)
        record_cards = [trade.get("record_card", {}) for trade in trades if isinstance(trade.get("record_card"), dict)]
        record_audit = TradeRecordCardBuilder(
            run_date=run_date,
            strategy_id=sid,
            strategy_config=config,
        ).summarize(record_cards)
        decision_snapshots = self._strategy_decision_snapshots(run_date, sid)
        latest_decision_snapshot = self._latest_decision_snapshot(decision_snapshots)
        latest_go_decision_snapshot = self._latest_decision_snapshot(decision_snapshots, final_decision="go")
        return {
            "strategy_id": sid,
            "timeframe": timeframe,
            "classification": config.get("classification", {}),
            "summary": {
                "open_trade_count": open_trade_count,
                "closed_today_count": closed_today_count,
                "closed_all_count": closed_all_count,
                "unrealized_pnl": summary.get("unrealized_pnl", 0),
                "realized_pnl_today": summary.get("realized_pnl_today", 0),
                "realized_pnl_all": summary.get("realized_pnl_all", 0),
                "net_pnl_marked": summary.get("net_pnl_marked", 0),
                "win_rate": summary.get("win_rate", 0),
                "max_drawdown_pct": equity_curve.get("max_drawdown_pct", 0) if isinstance(equity_curve, dict) else 0,
            },
            "bars": replay_bars,
            "replay_ohlc": replay_ohlc,
            "ohlc_quality": ohlc_quality,
            "nav_points": nav_points,
            "nav_quality": self._nav_quality(
                nav_points,
                replay_bars,
                closed_trade_count=closed_all_count,
                open_trade_count=open_trade_count,
            ),
            "nav_curve_intraday": nav_curve_intraday if isinstance(nav_curve_intraday, dict) else {},
            "gold_nav": self._gold_nav_from_bars(replay_bars),
            "orders": trace_orders,
            "open_orders": [item for item in trace_orders if item.get("status") in {"pending", "open", "submitted"}],
            "open_trades": open_decorated,
            "closed_trades": closed_decorated,
            "trades": trades,
            "trade_record_cards": record_cards,
            "trade_record_audit": record_audit,
            "strategy_book": strategy_book,
            "edge_judgment": edge_judgment,
            "trade_lifecycle": [
                {
                    "trade_id": trade.get("trade_id", ""),
                    "ticket_id": trade.get("ticket_id", ""),
                    "signal_id": trade.get("signal_id", ""),
                    "status": trade.get("status", ""),
                    "stages": trade.get("lifecycle", []),
                }
                for trade in trades
            ],
            "explainability_status": "ok" if not explainability_warn_count else "warn",
            "explainability_gaps": explainability_gaps,
            "explainability_gap_groups": self._gap_groups(explainability_gaps, group_by="type"),
            "explainability_root_cause_groups": self._gap_groups(explainability_gaps, group_by="root_cause"),
            "performance_confidence": self._performance_confidence(
                today_trade_count=today_trade_count,
                trade_count_7d=trade_count_7d,
                closed_trade_count=closed_all_count,
                open_trade_count=open_trade_count,
                return_pct=None,
                win_rate=summary.get("win_rate", 0),
                closed_loop_status="ok" if not explainability_warn_count else "explainability_gap",
            ),
            "unrealized_pnl": summary.get("unrealized_pnl", 0),
            "entry_reason": self._latest_entry_reason(trace_signals, trace_tickets, trace_decisions),
            "exit_reason": self._latest_exit_reason(trades, paper_exit_decisions),
            "strategy_signal": signals[-1] if signals else (trace_signals[-1] if trace_signals else {}),
            "latest_decision_snapshot": latest_decision_snapshot,
            "latest_go_decision_snapshot": latest_go_decision_snapshot,
            "decision_snapshot_summary": {
                "count": len(decision_snapshots),
                "go_count": sum(1 for item in decision_snapshots if item.get("final_decision") == "go"),
                "no_go_count": sum(1 for item in decision_snapshots if item.get("final_decision") == "no_go"),
                "latest_timestamp": latest_decision_snapshot.get("bar_timestamp") or latest_decision_snapshot.get("generated_at") or "",
                "latest_go_timestamp": latest_go_decision_snapshot.get("bar_timestamp") or latest_go_decision_snapshot.get("generated_at") or "",
            },
            "risk_block": trace_risk_blocks[-1] if trace_risk_blocks else {},
            "risk_monitor": risk_monitor,
            "live_reconciliation": live_reconciliation,
            "paper_exit_monitor": paper_exit_monitor,
            "paper_exit_decisions": paper_exit_decisions,
            "paper_reconciliation": paper_reconciliation,
            "review_events": review_events,
            "source_contract": self._source_contracts(run_date, sid, timeframe)["strategy_detail"],
        }

    def _strategy_decision_snapshots(self, run_date: str, strategy_id: str) -> list[dict]:
        if not strategy_id:
            return []
        paths = [
            self.output_root / "strategies" / strategy_id / "decision_snapshots" / f"{run_date}.json",
            self.output_root / "decision_snapshots" / f"{run_date}.json",
        ]
        rows: list[dict] = []
        for path in paths:
            if not path.exists():
                continue
            try:
                raw = load_json(path)
            except (OSError, ValueError):
                continue
            if isinstance(raw, list):
                rows = [item for item in raw if isinstance(item, dict)]
            elif isinstance(raw, dict):
                rows = [raw]
            if rows:
                break
        return sorted(
            rows,
            key=lambda item: self._parse_ts(item.get("bar_timestamp") or item.get("generated_at")) or datetime.min.replace(tzinfo=timezone.utc),
        )

    def _latest_decision_snapshot(self, rows: list[dict], final_decision: str | None = None) -> dict:
        candidates = [item for item in rows if not final_decision or item.get("final_decision") == final_decision]
        if not candidates:
            return {}
        item = candidates[-1]
        return {
            "strategy_id": item.get("strategy_id", ""),
            "bar_timestamp": item.get("bar_timestamp", ""),
            "generated_at": item.get("generated_at", ""),
            "final_decision": item.get("final_decision", ""),
            "signal": item.get("signal", {}),
            "execution_plan": item.get("execution_plan", {}),
            "no_go_reason": item.get("no_go_reason", ""),
        }

    def _review_loop_board(
        self,
        trading_plan: dict,
        evening_review: dict,
        strategy_experiments: dict,
        strategy_improvement_plan: dict,
        performance_board: dict,
    ) -> dict:
        plan_signal = trading_plan.get("current_signal", {}) if isinstance(trading_plan, dict) else {}
        position_map = trading_plan.get("position_map", {}) if isinstance(trading_plan, dict) else {}
        nearest_level = plan_signal.get("position_context", {}).get("nearest_level", {}) if isinstance(plan_signal, dict) else {}
        strategy_events = []
        for row in performance_board.get("strategies", []):
            daily = row.get("daily_execution", {})
            strategy_events.append({
                "strategy_id": row.get("strategy_id"),
                "classification": row.get("classification", {}),
                "signal_count": daily.get("signal_count", 0),
                "directional_signal_count": daily.get("directional_signal_count", 0),
                "ticket_count": daily.get("ticket_count", 0),
                "executed_trade_count": daily.get("executed_trade_count", 0),
                "sample_status": row.get("sample_status"),
                "risk_status": row.get("status", ""),
                "frequency_stage": row.get("frequency_diagnostics", {}).get("stage", ""),
                "frequency_stage_label": row.get("frequency_diagnostics", {}).get("stage_label", ""),
                "frequency_reason": row.get("frequency_diagnostics", {}).get("reason", ""),
            })
        hypotheses = []
        for key in ("review_hypotheses", "shadow_hypotheses"):
            items = strategy_experiments.get(key, []) if isinstance(strategy_experiments, dict) else []
            hypotheses.extend(items if isinstance(items, list) else [])
        next_steps = strategy_improvement_plan.get("next_steps", []) if isinstance(strategy_improvement_plan, dict) else []
        return {
            "morning_plan": {
                "run_date": trading_plan.get("run_date", ""),
                "generated_at": trading_plan.get("generated_at", ""),
                "active_strategy_id": trading_plan.get("active_strategy_id", ""),
                "decision": trading_plan.get("decision") or trading_plan.get("status", ""),
                "allowed_to_trade": bool(trading_plan.get("allowed_to_trade")),
                "blockers": trading_plan.get("blockers", []),
                "signal": plan_signal,
                "nearest_level": nearest_level,
                "position_map_status": position_map.get("status", ""),
                "key_frames": position_map.get("frames", {}),
            },
            "intraday_execution": {
                "today_has_real_trade": performance_board.get("today_has_real_trade", False),
                "today_executed_trade_count": performance_board.get("today_executed_trade_count", 0),
                "events": strategy_events,
            },
            "evening_review": {
                "run_date": evening_review.get("run_date", ""),
                "generated_at": evening_review.get("generated_at", ""),
                "status": evening_review.get("status", ""),
                "active_strategy_id": evening_review.get("active_strategy_id", ""),
                "summary": evening_review.get("summary", evening_review.get("review_summary", "")),
                "no_trade_review": evening_review.get("no_trade_review", {}),
                "previous_plan": evening_review.get("previous_plan", {}),
            },
            "hypothesis": {
                "items": hypotheses,
                "next_steps": next_steps if isinstance(next_steps, list) else [],
                "best_candidate": strategy_experiments.get("best_candidate", {}) if isinstance(strategy_experiments, dict) else {},
                "promotion_blockers": strategy_experiments.get("blockers", []) if isinstance(strategy_experiments, dict) else [],
            },
        }

    def _dashboard_health(
        self,
        run_date: str,
        performance_board: dict,
        leaderboard: dict,
        health: dict,
        data_source_preflight: dict,
        bars: list[dict],
        ohlc_quality: dict,
        market_data_gate: dict,
    ) -> dict:
        strategies = performance_board.get("strategies", [])
        nav_missing = [row["strategy_id"] for row in strategies if not row.get("has_nav")]
        low_sample = [row["strategy_id"] for row in strategies if row.get("is_low_sample")]
        explainability_gap_strategy_ids = performance_board.get("explainability_gap_strategy_ids", [])
        closed_loop_issue_strategy_ids = performance_board.get("closed_loop_issue_strategy_ids", [])
        evidence_provenance = performance_board.get("evidence_provenance", {})
        repaired_strategy_ids = evidence_provenance.get("repaired_strategy_ids", [])
        generated_at = leaderboard.get("generated_at", "") if isinstance(leaderboard, dict) else ""
        checks = [
            {"name": "api", "status": "ok", "message": "dashboard snapshot generated"},
            {
                "name": "schema_contract",
                "status": "ok",
                "message": f"{self.SCHEMA_VERSION} stable sections exposed",
                "schema_version": self.SCHEMA_VERSION,
                "stable_sections": list(self.STABLE_SECTIONS),
            },
            {
                "name": "strategy_leaderboard",
                "status": "ok" if leaderboard.get("run_date") == run_date and strategies else "warn",
                "message": f"{len(strategies)} strategies loaded",
                "generated_at": generated_at,
            },
            {
                "name": "strategy_nav",
                "status": "ok" if not nav_missing else "warn",
                "message": "all strategies have NAV" if not nav_missing else f"{len(nav_missing)} strategies missing NAV",
                "strategy_ids": nav_missing,
            },
            {
                "name": "strategy_samples",
                "status": "ok" if not low_sample else "warn",
                "message": "all strategies have executed samples today" if not low_sample else f"{len(low_sample)} strategies waiting for samples",
                "strategy_ids": low_sample,
            },
            {
                "name": "strategy_explainability",
                "status": "ok" if not explainability_gap_strategy_ids else "warn",
                "message": (
                    "all executed strategy samples have complete explanation artifacts"
                    if not explainability_gap_strategy_ids
                    else f"{len(explainability_gap_strategy_ids)} strategies have explanation gaps"
                ),
                "strategy_ids": explainability_gap_strategy_ids,
                "gap_count": performance_board.get("explainability_gap_count", 0),
                "root_cause_count": performance_board.get("explainability_gap_root_cause_count", 0),
                "gap_groups": performance_board.get("explainability_gap_groups", []),
            },
            {
                "name": "evidence_provenance",
                "status": "warn" if repaired_strategy_ids else "ok",
                "message": (
                    "all traceable trade evidence comes from original artifacts"
                    if not repaired_strategy_ids
                    else f"{len(repaired_strategy_ids)} strategies include repaired trade evidence"
                ),
                "strategy_ids": repaired_strategy_ids,
                "repaired_trade_count": evidence_provenance.get("repaired_trade_count", 0),
                "original_trace_trade_count": evidence_provenance.get("original_trace_trade_count", 0),
                "missing_trace_trade_count": evidence_provenance.get("missing_trace_trade_count", 0),
            },
            {
                "name": "closed_loop_coverage",
                "status": "ok" if not closed_loop_issue_strategy_ids else "warn",
                "message": (
                    "all strategies with samples have closed-loop coverage"
                    if not closed_loop_issue_strategy_ids
                    else f"{len(closed_loop_issue_strategy_ids)} strategies with samples need audit follow-up"
                ),
                "strategy_ids": closed_loop_issue_strategy_ids,
            },
            {
                "name": "active_demo_blocker",
                "status": "warn" if performance_board.get("active_demo_blocker", {}).get("blocked") else "ok",
                "message": (
                    performance_board.get("active_demo_blocker", {}).get("reason")
                    if performance_board.get("active_demo_blocker", {}).get("blocked")
                    else "active demo strategy has no reconciliation blocker"
                ),
                "strategy_id": performance_board.get("active_strategy_id", ""),
            },
            {
                "name": "data_freshness",
                "status": "ok" if bars and data_source_preflight.get("ready_for_paper", False) else "warn",
                "message": data_source_preflight.get("message", "market data loaded" if bars else "no market bars"),
                "latest_timestamp": (bars[-1].get("timestamp") if bars else ""),
                "latest_record_age_minutes": data_source_preflight.get("latest_record_age_minutes"),
            },
            {
                "name": "ohlc_quality",
                "status": "ok" if ohlc_quality.get("promotion_ready") else "warn",
                "message": ohlc_quality.get("action") or ohlc_quality.get("trust_label", "OHLC quality unknown"),
                "provider": ohlc_quality.get("provider", ""),
                "truth_level": ohlc_quality.get("truth_level", ""),
                "wide_bar_count": ohlc_quality.get("wide_bar_count", 0),
                "official_rows": ohlc_quality.get("official_rows", 0),
                "stale_artifacts": ohlc_quality.get("stale_artifacts", []),
            },
            {
                "name": "market_data_gate",
                "status": "ok" if market_data_gate.get("promotion_ready") else "warn",
                "message": market_data_gate.get("trader_action") or market_data_gate.get("trader_summary", "Market data gate unknown"),
                "mode": market_data_gate.get("mode", ""),
                "official_rows": market_data_gate.get("official_rows", 0),
                "blockers": market_data_gate.get("blockers", []),
            },
        ]
        if health.get("checks"):
            failing = [item for item in health.get("checks", []) if item.get("status") in {"fail", "error", "block"}]
            attention = [
                item for item in health.get("checks", [])
                if item.get("status") != "ok"
                and item.get("name") in {
                    "alert_delivery",
                    "active_demo_reconciliation",
                    "paper_reconciliation",
                    "daily_review",
                    "vitals_tp_sl_coverage",
                    "vitals_runner_liveness",
                    "vitals_data_feed",
                }
            ]
            checks.append({
                "name": "pipeline_health",
                "status": "ok" if not failing else "warn",
                "message": f"{len(failing)} blocking health checks" if failing else "pipeline health has no hard failures",
            })
            checks.append({
                "name": "health_attention",
                "status": "ok" if not attention else "warn",
                "message": "no user-facing health warnings" if not attention else f"{len(attention)} health warning(s) need attention",
                "items": [
                    {
                        "name": item.get("name", ""),
                        "status": item.get("status", ""),
                        "message": item.get("message", ""),
                    }
                    for item in attention
                ],
            })
        status = "ok" if all(item["status"] == "ok" for item in checks) else "warn"
        return {
            "run_date": run_date,
            "status": status,
            "checks": checks,
            "nav_missing_strategy_ids": nav_missing,
            "low_sample_strategy_ids": low_sample,
            "explainability_gap_strategy_ids": explainability_gap_strategy_ids,
            "repaired_evidence_strategy_ids": repaired_strategy_ids,
            "closed_loop_issue_strategy_ids": closed_loop_issue_strategy_ids,
            "health_attention": next((item.get("items", []) for item in checks if item.get("name") == "health_attention"), []),
        }
