"""Trade frequency diagnostics, closed-loop audit, and performance confidence."""

from __future__ import annotations

from pathlib import Path

from services.execution_accounting import (
    executed_record_ids,
    execution_record_status,
)
from services.journal_store import load_json


class FrequencyMixin:
    def _daily_execution_for_date(self, namespace: Path, run_date: str) -> dict:
        signals = load_json(namespace / "signals" / f"{run_date}.json")
        return {
            "run_date": run_date,
            "status": "inactive",
            "effective_today": False,
            "min_daily_executed_trades": 1,
            "executed_trade_count": self._executed_trade_count_for_namespace(namespace, run_date),
            "signal_count": len(signals),
            "directional_signal_count": sum(1 for item in signals if item.get("direction") in {"long", "short"}),
            "ticket_count": len(load_json(namespace / "trade_tickets" / f"{run_date}.json")),
        }

    def _strategy_frequency_diagnostics(
        self,
        namespace: Path,
        run_date: str,
        daily_execution: dict,
        demo_blocker: dict,
    ) -> dict:
        signals = load_json(namespace / "signals" / f"{run_date}.json")
        tickets = load_json(namespace / "trade_tickets" / f"{run_date}.json")
        pending = load_json(namespace / "journal_pending" / f"{run_date}.json")
        risk_blocks = load_json(namespace / "risk_blocks" / f"{run_date}.json")
        paper_orders = load_json(namespace / "paper_orders" / f"{run_date}.json")
        demo_orders = load_json(namespace / "demo_order_requests" / f"{run_date}.json")
        live_requests = load_json(namespace / "live_order_requests" / f"{run_date}.json")
        executed_ids: set[str] = set()
        executed_ids.update(executed_record_ids(paper_orders, legacy_count_missing_status=True))
        executed_ids.update(executed_record_ids(demo_orders))
        executed_ids.update(executed_record_ids(live_requests))
        directional = sum(1 for item in signals if isinstance(item, dict) and item.get("direction") in {"long", "short"})
        quality_pass = 0
        quality_fail = 0
        quality_reasons: list[str] = []
        for ticket in tickets:
            if not isinstance(ticket, dict):
                continue
            quality = ticket.get("trade_quality") if isinstance(ticket.get("trade_quality"), dict) else {}
            if not quality:
                continue
            if quality.get("passes") is True:
                quality_pass += 1
            elif quality.get("passes") is False:
                quality_fail += 1
                quality_reasons.extend(str(item) for item in quality.get("reasons", []) if item)
        min_daily = int(daily_execution.get("min_daily_executed_trades") or 1)
        executed_count = int(daily_execution.get("executed_trade_count") or 0)
        if executed_ids:
            executed_count = max(executed_count, len(executed_ids))
        stage = "missing_artifacts"
        reason = "no strategy artifacts found for today"
        if demo_blocker.get("blocked"):
            stage = "execution_blocker"
            reason = demo_blocker.get("reason") or demo_blocker.get("status") or "demo execution blocked"
        elif executed_count >= min_daily:
            stage = "effective"
            reason = f"executed {executed_count}/{min_daily} trades today"
        elif executed_count > 0:
            stage = "low_volume"
            reason = f"executed {executed_count}/{min_daily} trades today"
        elif pending:
            stage = "pending_review"
            reason = f"{len(pending)} ticket(s) waiting for manual review"
        elif quality_fail and not quality_pass:
            stage = "quality_failed"
            reason = "; ".join(quality_reasons[:2]) or "ticket failed trade quality gate"
        elif risk_blocks:
            stage = "risk_blocked"
            reason = self._risk_block_reason(risk_blocks[-1])
        elif directional > 0 and not tickets:
            stage = "candidate_without_ticket"
            reason = "directional signal did not become a trade ticket"
        elif signals:
            stage = "no_signal"
            reason = "latest strategy output is watch/no-trade"
        attribution = self._frequency_attribution(
            stage=stage,
            reason=reason,
            executed_count=executed_count,
            signals=signals,
            directional=directional,
            tickets=tickets,
            pending=pending,
            risk_blocks=risk_blocks,
            quality_fail=quality_fail,
            quality_reasons=quality_reasons,
            demo_blocker=demo_blocker,
            paper_orders=paper_orders,
            demo_orders=demo_orders,
            live_requests=live_requests,
        )
        return {
            "strategy_id": namespace.name,
            "stage": stage,
            "stage_label": self._frequency_stage_label(stage),
            "reason": reason,
            "primary_reason": attribution["primary_reason"],
            "limiting_reason": attribution["limiting_reason"],
            "attribution": attribution,
            "needs_attention": stage not in {"effective"},
            "run_date": run_date,
            "min_daily_trades": min_daily,
            "executed_trade_count": executed_count,
            "signal_count": len(signals),
            "directional_signal_count": directional,
            "ticket_count": len(tickets),
            "pending_review_count": len(pending),
            "risk_block_count": len(risk_blocks),
            "quality_pass_count": quality_pass,
            "quality_fail_count": quality_fail,
            "paper_order_count": len(paper_orders),
            "demo_order_count": len(demo_orders),
            "live_request_count": len(live_requests),
            "blocked_order_count": self._blocked_execution_count([*paper_orders, *demo_orders, *live_requests]),
        }

    def _frequency_board(self, strategy_rows: list[dict]) -> dict:
        counts: dict[str, int] = {}
        bottlenecks = []
        for row in strategy_rows:
            diagnostic = row.get("frequency_diagnostics", {}) if isinstance(row, dict) else {}
            stage = diagnostic.get("stage", "unknown")
            counts[stage] = counts.get(stage, 0) + 1
            if diagnostic.get("needs_attention"):
                bottlenecks.append({
                    "strategy_id": row.get("strategy_id"),
                    "stage": stage,
                    "stage_label": diagnostic.get("stage_label", ""),
                    "reason": diagnostic.get("reason", ""),
                    "limiting_reason": diagnostic.get("limiting_reason", ""),
                    "limiting_reason_label": diagnostic.get("attribution", {}).get("limiting_reason_label", ""),
                    "executed_trade_count": diagnostic.get("executed_trade_count", 0),
                    "min_daily_trades": diagnostic.get("min_daily_trades", 1),
                })
        return {
            "stage_counts": counts,
            "bottlenecks": bottlenecks,
            "effective_strategy_count": counts.get("effective", 0),
            "needs_attention_count": len(bottlenecks),
        }

    def _frequency_stage_label(self, stage: str) -> str:
        labels = {
            "effective": "达标",
            "low_volume": "成交不足",
            "pending_review": "待人工审批",
            "quality_failed": "质量门失败",
            "risk_blocked": "风控拦截",
            "candidate_without_ticket": "有信号无出票",
            "no_signal": "无交易信号",
            "execution_blocker": "执行阻塞",
            "missing_artifacts": "缺少产物",
        }
        return labels.get(stage, stage or "unknown")

    def _risk_block_reason(self, item: dict) -> str:
        if not isinstance(item, dict):
            return "risk block"
        portfolio = item.get("portfolio_risk") if isinstance(item.get("portfolio_risk"), dict) else {}
        return str(portfolio.get("block_reason") or item.get("reason") or "risk block")

    def _frequency_attribution(
        self,
        *,
        stage: str,
        reason: str,
        executed_count: int,
        signals: list[dict],
        directional: int,
        tickets: list[dict],
        pending: list[dict],
        risk_blocks: list[dict],
        quality_fail: int,
        quality_reasons: list[str],
        demo_blocker: dict,
        paper_orders: list[dict],
        demo_orders: list[dict],
        live_requests: list[dict],
    ) -> dict:
        counts: dict[str, int] = {}
        evidence: list[dict] = []

        def add(reason_key: str, count: int = 1, detail: str = "") -> None:
            if count <= 0:
                return
            counts[reason_key] = counts.get(reason_key, 0) + count
            if len(evidence) < 8:
                evidence.append({
                    "reason": reason_key,
                    "reason_label": self._frequency_reason_label(reason_key),
                    "detail": detail,
                })

        if executed_count > 0:
            add("executed", executed_count, f"{executed_count} executed trades")
        if demo_blocker.get("blocked"):
            add("execution_blocker", 1, str(demo_blocker.get("reason") or demo_blocker.get("status") or "demo execution blocked"))
        if quality_fail:
            add("quality_gate_failed", quality_fail, "; ".join(quality_reasons[:2]) or "trade quality gate failed")
        if risk_blocks:
            add("risk_budget_used", len(risk_blocks), self._risk_block_reason(risk_blocks[-1]))
        if pending:
            add("pending_review", len(pending), "ticket waiting for manual review")
        blocked_orders = self._blocked_execution_count([*paper_orders, *demo_orders, *live_requests])
        if blocked_orders:
            add("execution_blocker", blocked_orders, "blocked/rejected execution request")
        non_executed_tickets = max(0, len(tickets) - executed_count - quality_fail)
        if non_executed_tickets and stage in {"ticket_no_execution", "low_volume"}:
            add("ticket_no_execution", non_executed_tickets, "ticket created but not executed")
        if directional > 0 and not tickets and not risk_blocks and not demo_blocker.get("blocked"):
            add("candidate_without_ticket", directional, "directional signal did not become a ticket")
        if signals and directional <= 0:
            add("market_no_signal", len(signals), "strategy output watch/no-trade")
        if not counts:
            add("missing_artifacts", 1, reason or "no strategy artifact found")

        primary = self._frequency_primary_reason(stage, counts)
        limiting = self._frequency_limiting_reason(counts, primary)
        return {
            "primary_reason": primary,
            "primary_reason_label": self._frequency_reason_label(primary),
            "limiting_reason": limiting,
            "limiting_reason_label": self._frequency_reason_label(limiting),
            "reason_counts": counts,
            "evidence": evidence,
            "frequency_is_diagnostic_not_sla": True,
        }

    def _frequency_primary_reason(self, stage: str, counts: dict[str, int]) -> str:
        if stage == "effective":
            return "executed"
        if stage == "execution_blocker":
            return "execution_blocker"
        if stage == "pending_review":
            return "pending_review"
        if stage == "quality_failed":
            return "quality_gate_failed"
        if stage == "risk_blocked":
            return "risk_budget_used"
        if stage == "candidate_without_ticket":
            return self._frequency_top_reason({
                key: value
                for key, value in counts.items()
                if key in {"data_blocked", "risk_budget_used", "quality_gate_failed", "signal_threshold_blocked", "candidate_without_ticket", "market_no_signal"}
            }) or "candidate_without_ticket"
        if stage == "no_signal":
            return "market_no_signal"
        if stage == "missing_artifacts":
            return "missing_artifacts"
        return self._frequency_top_reason(counts) or stage

    def _frequency_limiting_reason(self, counts: dict[str, int], primary: str) -> str:
        non_success = {key: value for key, value in counts.items() if key != "executed" and value > 0}
        return self._frequency_top_reason(non_success) or primary

    def _frequency_top_reason(self, counts: dict[str, int]) -> str:
        priority = [
            "execution_blocker",
            "data_blocked",
            "risk_budget_used",
            "quality_gate_failed",
            "pending_review",
            "ticket_no_execution",
            "signal_threshold_blocked",
            "candidate_without_ticket",
            "market_no_signal",
            "missing_artifacts",
        ]
        best = ""
        for key in priority:
            value = int(counts.get(key) or 0)
            if value > 0:
                return key
        return best

    def _frequency_reason_label(self, reason: str) -> str:
        labels = {
            "executed": "已成交",
            "execution_blocker": "执行阻塞",
            "data_blocked": "数据阻塞",
            "risk_budget_used": "风险预算/风控拦截",
            "quality_gate_failed": "质量门失败",
            "pending_review": "待人工审批",
            "ticket_no_execution": "有票未执行",
            "signal_threshold_blocked": "信号强度/置信度不足",
            "candidate_without_ticket": "有方向信号未出票",
            "market_no_signal": "市场无合格信号",
            "missing_artifacts": "缺少产物",
        }
        return labels.get(reason, reason or "unknown")

    def _blocked_execution_count(self, rows: list[dict]) -> int:
        blocked = 0
        for item in rows:
            status = execution_record_status(item)
            if status in {"blocked", "rejected", "failed", "skipped", "pending", "protective_order_missing"}:
                blocked += 1
        return blocked

    def _strategy_closed_loop_audit(self, namespace: Path, run_date: str, executed_trade_count: int) -> dict:
        signals_path = namespace / "signals" / f"{run_date}.json"
        tickets_path = namespace / "trade_tickets" / f"{run_date}.json"
        decisions_path = namespace / "journal_decisions" / f"{run_date}.json"
        orders_path = namespace / "paper_orders" / f"{run_date}.json"
        open_trades_path = namespace / "paper_trades" / "current.json"
        closed_trades_path = namespace / "paper_trades" / "closed" / f"{run_date}.json"
        risk_blocks_path = namespace / "risk_blocks" / f"{run_date}.json"
        exit_decisions_path = namespace / "paper_exit_decisions" / "current.json"

        signals = load_json(signals_path)
        tickets = load_json(tickets_path)
        decisions = load_json(decisions_path)
        orders = load_json(orders_path)
        open_trades = load_json(open_trades_path)
        closed_trades = load_json(closed_trades_path)
        risk_blocks = load_json(risk_blocks_path)
        exit_decisions = load_json(exit_decisions_path)
        raw_open_trades = open_trades if isinstance(open_trades, list) else []
        raw_closed_trades = closed_trades if isinstance(closed_trades, list) else []
        trace = self._trade_trace_artifacts(
            artifact_root=namespace,
            run_date=run_date,
            trades=[*raw_open_trades, *raw_closed_trades],
            signals=signals if isinstance(signals, list) else [],
            tickets=tickets if isinstance(tickets, list) else [],
            decisions=decisions if isinstance(decisions, list) else [],
            risk_blocks=risk_blocks if isinstance(risk_blocks, list) else [],
            orders=orders if isinstance(orders, list) else [],
        )
        trace_signals = trace["signals"]
        trace_tickets = trace["tickets"]
        trace_decisions = trace["decisions"]
        trace_risk_blocks = trace["risk_blocks"]
        trace_orders = trace["orders"]
        review_events = self._review_events(trace_signals, trace_decisions, trace_risk_blocks)
        trades = self._decorated_trades(
            raw_open_trades,
            raw_closed_trades,
            trace_signals,
            trace_tickets,
            trace_decisions,
            trace_risk_blocks,
            trace_orders,
            exit_decisions if isinstance(exit_decisions, dict) else {},
            {},
            review_events,
        )
        gaps = self._explainability_gaps(
            trades,
            trace_signals,
            trace_tickets,
            trace_orders,
            trace_decisions,
        )
        evidence_provenance = self._trade_evidence_provenance(trades)
        warn_count = sum(1 for item in gaps if item.get("severity") == "warn")
        info_count = sum(1 for item in gaps if item.get("severity") != "warn")
        gap_groups = self._gap_groups(gaps, group_by="type")
        root_cause_groups = self._gap_groups(gaps, group_by="root_cause")
        artifact_counts = {
            "signals": len(trace_signals),
            "tickets": len(trace_tickets),
            "decisions": len(trace_decisions),
            "orders": len(trace_orders),
            "open_trades": len(raw_open_trades),
            "closed_trades": len(raw_closed_trades),
            "risk_blocks": len(trace_risk_blocks),
            "exit_decisions": len(exit_decisions.get("queue", [])) if isinstance(exit_decisions, dict) else 0,
            "review_events": len(review_events),
        }
        has_trade_sample = executed_trade_count >= 1
        evidence = [
            artifact_counts["signals"] > 0,
            artifact_counts["tickets"] > 0,
            artifact_counts["decisions"] > 0,
            artifact_counts["orders"] > 0,
            (artifact_counts["open_trades"] + artifact_counts["closed_trades"]) > 0,
            artifact_counts["risk_blocks"] > 0 or any(
                isinstance(item, dict) and isinstance(item.get("risk_snapshot"), dict)
                for item in trace_decisions
            ),
            artifact_counts["review_events"] > 0,
        ]
        coverage_score = None if not has_trade_sample else round((sum(1 for item in evidence if item) / len(evidence)) * 100, 1)
        if not has_trade_sample:
            status = "waiting_for_samples"
        elif warn_count:
            status = "explainability_gap"
        elif coverage_score is not None and coverage_score < 100:
            status = "partial_closed_loop"
        else:
            status = "closed_loop_ok"

        gap_types = sorted({str(item.get("type")) for item in gaps if item.get("type")})
        return {
            "status": status,
            "executed_trade_count": executed_trade_count,
            "coverage_score": coverage_score,
            "gap_count": len(gaps),
            "warn_count": warn_count,
            "info_count": info_count,
            "gap_types": gap_types,
            "gap_groups": gap_groups,
            "root_cause_groups": root_cause_groups,
            "root_cause_count": len(root_cause_groups),
            "dominant_gap_group": root_cause_groups[0] if root_cause_groups else {},
            "sample_source": "executed_trade_count",
            "counts_watch_no_trade_as_sample": False,
            "artifact_counts": artifact_counts,
            "evidence_provenance": evidence_provenance,
            "next_action": self._closed_loop_next_action(status, gap_types),
            "source_artifacts": {
                "signals": self._artifact_ref(signals_path, False),
                "tickets": self._artifact_ref(tickets_path, False),
                "decisions": self._artifact_ref(decisions_path, False),
                "orders": self._artifact_ref(orders_path, False),
                "open_trades": self._artifact_ref(open_trades_path, False),
                "closed_trades": self._artifact_ref(closed_trades_path, False),
                "risk_blocks": self._artifact_ref(risk_blocks_path, False),
                "exit_decisions": self._artifact_ref(exit_decisions_path, False),
            },
        }

    def _closed_loop_next_action(self, status: str, gap_types: list[str]) -> str:
        if status == "waiting_for_samples":
            return "wait_for_real_trade_sample"
        if "filled_order_without_trade" in gap_types or "missing_order_artifact" in gap_types:
            return "reconcile_order_trade_records"
        if "missing_ticket_artifact" in gap_types:
            return "repair_ticket_artifacts"
        if "missing_signal_artifact" in gap_types:
            return "repair_signal_artifacts"
        if "missing_gate_context" in gap_types:
            return "attach_risk_gate_snapshot"
        if status == "partial_closed_loop":
            return "complete_closed_loop_artifacts"
        return "ready_for_review"

    def _performance_confidence(
        self,
        *,
        today_trade_count: int,
        trade_count_7d: int,
        closed_trade_count: int,
        open_trade_count: int,
        return_pct: object,
        win_rate: object,
        closed_loop_status: str,
    ) -> dict:
        min_closed_trades = 5
        today_count = self._safe_int(today_trade_count)
        seven_day_count = self._safe_int(trade_count_7d)
        closed_count = self._safe_int(closed_trade_count)
        open_count = self._safe_int(open_trade_count)
        uses_unrealized = open_count > 0
        if today_count < 1 and seven_day_count < 1 and closed_count < 1 and open_count < 1:
            status = "waiting_for_samples"
            tone = "warn"
            label = "waiting for samples"
            reason = "No executed trade samples are available yet."
        elif closed_count < 1 and open_count > 0:
            status = "open_pnl_only"
            tone = "warn"
            label = "open PnL only"
            reason = "Ranking may be driven by unrealized PnL because no trades are closed yet."
        elif closed_count < 1:
            status = "no_closed_trades"
            tone = "warn"
            label = "no closed trades"
            reason = "Performance has no realized trade outcome yet."
        elif closed_count < min_closed_trades:
            status = "low_closed_sample"
            tone = "warn"
            label = "low confidence"
            reason = f"Only {closed_count} closed trades; minimum confidence threshold is {min_closed_trades}."
        elif today_count < 1:
            status = "inactive_today"
            tone = "warn"
            label = "inactive today"
            reason = "No executed trade sample today; current performance is historical."
        elif closed_loop_status and closed_loop_status not in {"ok", "closed_loop_ok"}:
            status = "audit_risk"
            tone = "warn"
            label = "audit risk"
            reason = "Closed-loop evidence is incomplete for at least one trade."
        else:
            status = "realized_evidence"
            tone = "ok"
            label = "realized evidence"
            reason = "Closed trades clear the minimum sample threshold."
        return {
            "status": status,
            "tone": tone,
            "label": label,
            "reason": reason,
            "today_trade_count": today_count,
            "trade_count_7d": seven_day_count,
            "closed_trade_count": closed_count,
            "open_trade_count": open_count,
            "min_closed_trades": min_closed_trades,
            "uses_unrealized_pnl": uses_unrealized,
            "return_pct": return_pct,
            "win_rate": win_rate,
        }
