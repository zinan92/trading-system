from __future__ import annotations

import os
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config, load_strategy_config
from services.journal_store import load_json
from services.market_store import MarketStore
from services.paper_executor import PaperExecutor
from services.paper_equity_curve import PaperEquityCurve
from services.paper_exit_decisions import PaperExitDecisionQueue
from services.paper_exit_monitor import PaperExitMonitor
from services.paper_performance import PaperPerformanceAnalyzer
from services.paper_risk_action_plan import PaperRiskActionPlan
from services.strategy_reviewer import StrategyReviewer
from services.strategy_learning_actions import StrategyLearningActions
from services.strategy_experiment_queue import StrategyExperimentQueue
from services.strategy_improvement_plan import StrategyImprovementPlan
from services.strategy_promotion_gate import StrategyPromotionGate
from services.trading_journal import TradingJournalBuilder


class ReportBuilder:
    def __init__(self, output_root: Path | None = None) -> None:
        pipeline_config = load_pipeline_config()
        env_output_root = os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT")
        self.output_root = output_root or Path(env_output_root or str(ROOT / pipeline_config.get("output_root", "outputs")))

    def build_daily_report(self, run_date: str) -> Path:
        signals = load_json(self.output_root / "signals" / f"{run_date}.json")
        tickets = load_json(self.output_root / "trade_tickets" / f"{run_date}.json")
        pending = load_json(self.output_root / "journal_pending" / f"{run_date}.json")
        decisions = load_json(self.output_root / "journal_decisions" / f"{run_date}.json")
        paper_orders = load_json(self.output_root / "paper_orders" / f"{run_date}.json")
        risk_blocks = load_json(self.output_root / "risk_blocks" / f"{run_date}.json")
        data_quality = self._load_mapping(self.output_root / "data_quality" / f"{run_date}.json")
        data_source_rows = load_json(self.output_root / "data_source_preflight" / f"{run_date}.json")
        data_source = data_source_rows[-1] if data_source_rows else {}
        data_lineage_rows = load_json(self.output_root / "data_source_lineage" / f"{run_date}.json")
        data_lineage = data_lineage_rows[-1] if data_lineage_rows else {}
        official_feed_rows = load_json(self.output_root / "official_feed_receipts" / f"{run_date}.json")
        official_feed = official_feed_rows[-1] if official_feed_rows else {}
        data_integrity_rows = load_json(self.output_root / "data_integrity" / f"{run_date}.json")
        data_integrity = data_integrity_rows[-1] if data_integrity_rows else {}
        live_dry_run_rows = load_json(self.output_root / "live_dry_run_drill" / f"{run_date}.json")
        live_dry_run = live_dry_run_rows[-1] if live_dry_run_rows else {}
        live_submission_safety_rows = load_json(self.output_root / "live_submission_safety" / f"{run_date}.json")
        live_submission_safety = live_submission_safety_rows[-1] if live_submission_safety_rows else {}
        operation_runbook_rows = load_json(self.output_root / "operation_runbooks" / f"{run_date}.json")
        operation_runbook = operation_runbook_rows[-1] if operation_runbook_rows else {}
        paper_auto_gate_rows = load_json(self.output_root / "paper_auto_approval_gate" / f"{run_date}.json")
        paper_auto_gate = paper_auto_gate_rows[-1] if paper_auto_gate_rows else {}
        bot_supervisor_rows = load_json(self.output_root / "bot_supervisor" / f"{run_date}.json")
        bot_supervisor = bot_supervisor_rows[-1] if bot_supervisor_rows else {}
        broker_preflight = load_json(self.output_root / "broker_preflight" / "current.json")
        live_order_requests = load_json(self.output_root / "live_order_requests" / f"{run_date}.json")
        executor = PaperExecutor(self.output_root)
        closed_trades = executor.evaluate_exits(run_date)
        executor.mark_to_market(run_date)
        paper_positions = self._load_positions()
        open_trades = load_json(self.output_root / "paper_trades" / "current.json")
        closed_trades = load_json(self.output_root / "paper_trades" / "closed" / f"{run_date}.json") or closed_trades
        performance = PaperPerformanceAnalyzer(self.output_root).build(run_date)
        paper_exit_monitor = PaperExitMonitor(self.output_root).run(run_date)
        paper_exit_decisions = PaperExitDecisionQueue(self.output_root).build(run_date)
        equity_curve = PaperEquityCurve(self.output_root).build(run_date, performance)
        clean_manifest = load_json(self.output_root / "clean_bars" / run_date / "manifest.json")
        backtests = load_json(self.output_root / "backtests" / f"{run_date}.json")
        strategy_config = load_strategy_config()
        market_coverage = self._market_coverage()
        strategy_review = StrategyReviewer(self.output_root).build(run_date)
        learning_actions = StrategyLearningActions(self.output_root).build(run_date)
        strategy_experiments = StrategyExperimentQueue(self.output_root).build(run_date)
        strategy_promotion = StrategyPromotionGate(self.output_root).run(run_date)
        improvement_plan = StrategyImprovementPlan(self.output_root).build(run_date)
        paper_risk_action_plan = PaperRiskActionPlan(self.output_root).build(run_date)
        strategy_snapshot = strategy_review.get("strategy_snapshot", {})
        learning_ledger = load_json(self.output_root / "learning_ledger" / "current.json")
        cumulative_learning = learning_ledger[-1] if learning_ledger else {}
        strategy_proposals = load_json(self.output_root / "strategy_change_proposals" / "current.json")
        strategy_proposal = strategy_proposals[-1] if strategy_proposals else {}
        review_notes = self._build_review_notes(
            signals,
            tickets,
            pending,
            decisions,
            paper_orders,
            clean_manifest,
            open_trades,
            closed_trades,
            risk_blocks,
            market_coverage,
            backtests,
            strategy_config,
            data_quality,
            performance,
            data_source,
            data_lineage,
            official_feed,
            operation_runbook,
            paper_exit_monitor,
            paper_exit_decisions,
            paper_auto_gate,
            bot_supervisor,
            live_submission_safety,
            live_dry_run,
        )

        decision_counts = {
            "executed": sum(1 for item in decisions if item["decision_status"] == "executed"),
            "executed_paper": sum(1 for item in decisions if item["decision_status"] == "executed_paper"),
            "skipped": sum(1 for item in decisions if item["decision_status"] == "skipped"),
            "rejected": sum(1 for item in decisions if item["decision_status"] == "rejected"),
            "pending": len(pending),
        }
        avg_strength = sum(item["strength"] for item in signals) / len(signals) if signals else 0
        avg_confidence = sum(item["confidence"] for item in signals) / len(signals) if signals else 0

        lines = [
            f"# Trading OS Daily Report - {run_date}",
            "",
            "## Summary",
            "",
            f"- Signals generated: {len(signals)}",
            f"- Trade tickets generated: {len(tickets)}",
            f"- Paper orders: {len(paper_orders)}",
            f"- Average signal strength: {avg_strength:.1f}",
            f"- Average signal confidence: {avg_confidence:.1f}",
            f"- Executed: {decision_counts['executed']}",
            f"- Executed paper: {decision_counts['executed_paper']}",
            f"- Skipped: {decision_counts['skipped']}",
            f"- Rejected: {decision_counts['rejected']}",
            f"- Still pending: {decision_counts['pending']}",
            "",
            "## Decisions",
            "",
        ]

        if not decisions:
            lines.append("No manual decisions recorded yet.")
        for item in decisions:
            lines.extend(
                [
                    f"### {item['asset']} - {item['decision_status']}",
                    "",
                    f"- Ticket: {item['ticket_id']}",
                    f"- Signal: {item['signal_id']}",
                    f"- Notes: {item.get('notes') or 'n/a'}",
                    f"- Actual entry: {item.get('actual_entry') or 'n/a'}",
                    f"- Actual size: {item.get('actual_size') or 'n/a'}",
                    f"- Paper order: {(item.get('paper_order') or {}).get('order_id', 'n/a')}",
                    f"- Broker order: {(item.get('broker_order') or {}).get('order_id', 'n/a')}",
                    "",
                ]
            )

        lines.extend(["## Paper Orders", ""])
        if not paper_orders:
            lines.append("No paper orders recorded yet.")
        for item in paper_orders:
            lines.extend(
                [
                    f"- {item['ticket_id']}: {item['status']} @ {item.get('fill_price') or item['requested_price']} qty {item['quantity']} cost {item.get('total_cost', 0)}",
                ]
            )

        lines.extend(["", "## Paper Positions", ""])
        if not paper_positions:
            lines.append("No paper positions.")
        for symbol, item in paper_positions.items():
            net = f"net {item.get('net_side', item.get('side', 'n/a'))} {item.get('net_quantity', item.get('quantity', 'n/a'))}"
            legs = self._position_legs_text(item)
            lines.extend(
                [
                    f"- {symbol}: {item.get('side', 'n/a')} gross {item.get('quantity', 'n/a')} @ {item.get('avg_price', 'n/a')} | {net} | last {item.get('last_price', 'n/a')} | unrealized {item.get('unrealized_pnl', 0)} | risk used {item.get('risk_used_pct', 'n/a')}%",
                    f"  - Legs: {legs}",
                ]
            )

        lines.extend(["", "## Paper Trades", ""])
        if not open_trades and not closed_trades:
            lines.append("No paper trade lifecycle records.")
        for item in open_trades:
            regime = item.get("signal_regime", "unknown")
            strength = item.get("signal_strength", 0)
            lines.append(f"- OPEN {item['symbol']} {item['side']} {item['quantity']} @ {item['entry_price']} stop {item['stop_loss']} target {item['target']} | regime {regime} strength {strength}")
        for item in closed_trades:
            regime = item.get("signal_regime", "unknown")
            lines.append(f"- CLOSED {item['symbol']} {item['exit_reason']} @ {item['exit_price']} PnL {item['realized_pnl']} | regime {regime}")

        lines.extend(["", "## Paper Performance", ""])
        perf = performance.get("summary", {})
        lines.append(f"- Net marked PnL: {perf.get('net_pnl_marked', 0)}")
        lines.append(f"- Unrealized PnL: {perf.get('unrealized_pnl', 0)}")
        lines.append(f"- Realized PnL today/all: {perf.get('realized_pnl_today', 0)} / {perf.get('realized_pnl_all', 0)}")
        lines.append(f"- Execution costs open/closed: {perf.get('open_costs', 0)} / {perf.get('closed_costs', 0)}")
        lines.append(f"- Total execution costs: {perf.get('total_execution_costs', 0)}")
        lines.append(f"- Open risk amount: {perf.get('open_risk_amount', 0)}")
        lines.append(f"- Open unrealized R: {perf.get('open_unrealized_r', 0)}")
        lines.append(f"- Closed win rate: {perf.get('win_rate', 0)}")
        lines.append(f"- Expectancy R: {perf.get('expectancy_r', 0)}")
        lines.append(f"- Open by regime: {perf.get('open_by_signal_regime', {})}")
        lines.append(f"- Closed by regime: {perf.get('closed_by_signal_regime', {})}")

        lines.extend(["", "## Exit Monitor", ""])
        lines.extend(self._paper_exit_monitor_lines(paper_exit_monitor))

        lines.extend(["", "## Exit Decisions", ""])
        lines.extend(self._paper_exit_decision_lines(paper_exit_decisions))

        lines.extend(["", "## Paper Risk Actions", ""])
        lines.extend(self._paper_risk_action_lines(paper_risk_action_plan))

        lines.extend(["", "## Paper Equity Curve", ""])
        lines.append(f"- Current equity: {equity_curve.get('current_equity', 0)}")
        lines.append(f"- Current drawdown: {equity_curve.get('current_drawdown_pct', 0)}%")
        lines.append(f"- Max drawdown: {equity_curve.get('max_drawdown_pct', 0)}%")
        lines.append(f"- Curve points: {equity_curve.get('point_count', 0)}")

        lines.extend(["", "## Risk Blocks", ""])
        if not risk_blocks:
            lines.append("No portfolio risk blocks.")
        for item in risk_blocks:
            lines.append(f"- {item.get('asset', 'n/a')}: {item.get('reason', 'n/a')}")

        lines.extend(["", "## Blocked Candidates", ""])
        blocked_candidate_lines = self._blocked_candidate_lines(risk_blocks, signals, backtests)
        if blocked_candidate_lines:
            lines.extend(blocked_candidate_lines)
        else:
            lines.append("No blocked trade candidates.")

        lines.extend(["", "## Data Quality Gate", ""])
        gold_quality = data_quality.get("GOLD", {})
        if not gold_quality:
            lines.append("No data quality gate output.")
        else:
            lines.append(f"- Allows trading: {gold_quality.get('allows_trading')}")
            lines.append(f"- Missing ratio: {gold_quality.get('missing_ratio')}")
            lines.append(f"- Synthetic ratio: {gold_quality.get('synthetic_ratio')}")
            lines.append(f"- Reasons: {'; '.join(gold_quality.get('reasons', [])) or 'n/a'}")

        lines.extend(["", "## Market Data Truth", ""])
        if not data_source and not data_lineage and not official_feed and not operation_runbook:
            lines.append("No market data truth artifacts.")
        else:
            price_sanity = data_source.get("price_sanity") or {}
            permissions = operation_runbook.get("permissions") or {}
            lines.append(f"- Latest price: {data_source.get('latest_price', 'n/a')} from {data_source.get('latest_provider', 'n/a')}")
            lines.append(f"- Data truth: {data_lineage.get('truth_level', 'n/a')}")
            lines.append(f"- Price sanity: {price_sanity.get('passes', 'n/a')} range {price_sanity.get('min_price', 'n/a')}-{price_sanity.get('max_price', 'n/a')}")
            lines.append(f"- Paper ready: {data_source.get('ready_for_paper', 'n/a')}")
            lines.append(f"- Live ready: {data_source.get('ready_for_live', 'n/a')}")
            lines.append(f"- Official feed receipt: {official_feed.get('status', 'n/a')} official_rows={official_feed.get('official_rows', 'n/a')}")
            broker_quality = (official_feed.get("broker_csv") or {}).get("quality_summary") or {}
            broker_sanity = (official_feed.get("broker_csv") or {}).get("price_sanity") or {}
            if broker_quality:
                lines.append(
                    f"- Broker CSV quality: pass={broker_quality.get('pass', 0)} warn={broker_quality.get('warn', 0)} "
                    f"fail={broker_quality.get('fail', 0)} non_5m={broker_quality.get('non_5m_intervals', 0)} "
                    f"range={broker_quality.get('min_close', 'n/a')}-{broker_quality.get('max_close', 'n/a')}"
                )
            if broker_sanity:
                lines.append(f"- Broker CSV sanity gate: enabled={broker_sanity.get('enabled')} range {broker_sanity.get('min_price', 'n/a')}-{broker_sanity.get('max_price', 'n/a')}")
            lines.append(f"- Operation gate: {operation_runbook.get('status', 'n/a')} paper_manual={permissions.get('paper_manual_review', 'n/a')} paper_auto={permissions.get('paper_auto_approve', 'n/a')} live={permissions.get('live_trading', 'n/a')}")
            if data_integrity:
                lines.append(f"- Data integrity: {data_integrity.get('status', 'n/a')} pass={data_integrity.get('summary', {}).get('passed', 0)} fail={data_integrity.get('summary', {}).get('failed', 0)}")

        lines.extend(["", "## Live Submission Safety", ""])
        if live_submission_safety:
            lines.append(f"- Status: {live_submission_safety.get('status', 'n/a')}")
            lines.append(f"- Blocked by activation gate: {live_submission_safety.get('blocked_by_activation_gate', 'n/a')}")
            lines.append(f"- Network call attempted: {live_submission_safety.get('network_call_attempted', 'n/a')}")
            lines.append(f"- Provider: {live_submission_safety.get('provider', 'n/a')}")
            lines.append(f"- Error: {live_submission_safety.get('error') or 'n/a'}")
        else:
            lines.append("No live submission safety receipt generated yet.")

        lines.extend(["", "## Live Dry-Run Drill", ""])
        if live_dry_run:
            lines.append(f"- Status: {live_dry_run.get('status', 'n/a')}")
            lines.append(f"- Dry-run ready: {live_dry_run.get('dry_run_ready', 'n/a')}")
            lines.append(f"- Real-money ready: {live_dry_run.get('real_money_ready', 'n/a')}")
            lines.append(f"- Safe to submit live order: {live_dry_run.get('safe_to_submit_live_order', 'n/a')}")
            lines.append(f"- Network call attempted: {live_dry_run.get('network_call_attempted', 'n/a')}")
            lines.append(f"- Data truth: {live_dry_run.get('data_truth_level', 'n/a')} official_rows={live_dry_run.get('official_rows', 'n/a')}")
            for item in live_dry_run.get("blockers", [])[:5]:
                lines.append(f"- Blocker: {item.get('name', 'n/a')} - {item.get('summary', 'n/a')}")
        else:
            lines.append("No live dry-run drill generated yet.")

        lines.extend(["", "## Bot Supervisor", ""])
        lines.extend(self._bot_supervisor_lines(bot_supervisor))

        lines.extend(["", "## Paper Auto Approval Gate", ""])
        lines.extend(self._paper_auto_gate_lines(paper_auto_gate))

        lines.extend(["", "## Broker Readiness", ""])
        latest_preflight = broker_preflight[-1] if broker_preflight else {}
        if not latest_preflight:
            lines.append("No broker preflight recorded.")
        else:
            lines.append(f"- Mode: {latest_preflight.get('mode', 'n/a')}")
            lines.append(f"- Provider: {latest_preflight.get('provider', 'n/a')}")
            lines.append(f"- Ready: {latest_preflight.get('ready')}")
            lines.append(f"- Dry run: {latest_preflight.get('dry_run')}")
            lines.append(f"- Reason: {latest_preflight.get('block_reason') or 'n/a'}")
        if live_order_requests:
            lines.append(f"- Live request artifacts: {len(live_order_requests)}")
        else:
            lines.append("- Live request artifacts: 0")

        lines.extend(["", "## Data Coverage", ""])
        if not market_coverage:
            lines.append("No local market coverage available.")
        for item in market_coverage:
            if item["symbol"] == "GOLD" and item["timeframe"] == "5m":
                lines.append(f"- {item['symbol']} {item['timeframe']} {item['provider']}: {item['rows']} rows through {item['last_timestamp']}")

        strategy = strategy_config.get("gold_5m_v1", {})
        signal_config = strategy.get("signal", {})
        backtest_config = strategy.get("backtest", {})
        lines.extend(["", "## Strategy Config", ""])
        lines.append(f"- Strategy: gold_5m_v1")
        lines.append(f"- Config hash: {strategy_snapshot.get('config_hash', 'n/a')}")
        lines.append(f"- MA windows: short {signal_config.get('ma_short_bars')} / long {signal_config.get('ma_long_bars')}")
        lines.append(f"- Long threshold: strength {signal_config.get('long_strength_min')} / confidence {signal_config.get('long_confidence_min')}")
        lines.append(f"- Backtest stop/target: {backtest_config.get('stop_pct')} / {backtest_config.get('target_pct')}")
        lines.append(f"- Max hold bars: {backtest_config.get('max_hold_bars')} x 5m")

        lines.extend(["", "## Strategy Review Notes", ""])
        lines.extend(review_notes)

        lines.extend(["", "## Learning Ledger", ""])
        lines.append(f"- Summary: {strategy_review.get('summary', 'n/a')}")
        if cumulative_learning:
            lines.append(f"- Cumulative review days: {cumulative_learning.get('review_days')}")
            lines.append(f"- Strategy config hash: {cumulative_learning.get('strategy_config_hash', 'n/a')}")
            lines.append(f"- Cumulative paper orders: {cumulative_learning.get('paper_order_count')}")
            lines.append(f"- Cumulative closed PnL: {cumulative_learning.get('closed_realized_pnl')}")
            lines.append(f"- Learning state: {cumulative_learning.get('learning_state')}")
        if strategy_proposal:
            lines.append(f"- Strategy change proposal: {strategy_proposal.get('status')}")
            lines.append(f"- Proposal reason: {strategy_proposal.get('reason')}")
            for item in strategy_proposal.get("proposed_changes", []):
                lines.append(f"- Proposed change: {item.get('field')} -> {item.get('change')}")
        lines.append(f"- Learning actions: {learning_actions.get('status', 'n/a')} / {learning_actions.get('summary', {}).get('action_count', 0)} open")
        for item in learning_actions.get("actions", [])[:5]:
            lines.append(f"- Learning action: [{item.get('priority')}] {item.get('summary')}")
        lines.append(f"- Strategy experiments: {strategy_experiments.get('status', 'n/a')} / {len(strategy_experiments.get('experiments', []))} candidate(s)")
        best_experiment = strategy_experiments.get("best_candidate") or {}
        if best_experiment:
            lines.append(
                f"- Best shadow experiment: {best_experiment.get('variant_id')} score={best_experiment.get('score')} "
                f"PF={best_experiment.get('profit_factor')} avgR={best_experiment.get('avg_r')}"
            )
        for item in strategy_experiments.get("blockers", [])[:4]:
            lines.append(f"- Experiment blocker: {item.get('name')} - {item.get('summary')}")
        lines.append(f"- Strategy improvement plan: {improvement_plan.get('status', 'n/a')} / {len(improvement_plan.get('next_steps', []))} next step(s)")
        for item in improvement_plan.get("next_steps", [])[:4]:
            lines.append(f"- Improvement step: [{item.get('priority')}] {item.get('step_id')} - {item.get('summary')}")
        lines.append(f"- Strategy promotion gate: {strategy_promotion.get('status', 'n/a')} / allowed={strategy_promotion.get('promotion_allowed', 'n/a')}")
        for item in strategy_promotion.get("blockers", [])[:4]:
            lines.append(f"- Promotion blocker: {item.get('name')} - {item.get('summary')}")
        for item in strategy_review.get("observations", []):
            lines.append(f"- Observation: {item}")
        for item in strategy_review.get("suggestions", []):
            lines.append(f"- Suggestion: {item}")

        lines.extend(["", "## Pending", ""])
        if not pending:
            lines.append("No pending manual decisions.")
        for item in pending:
            lines.extend(
                [
                    f"- {item['asset']}: {item['ticket_id']}",
                ]
            )

        report_path = self.output_root / "reports" / f"{run_date}.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        review_path = self.output_root / "review_notes" / f"{run_date}.md"
        review_path.parent.mkdir(parents=True, exist_ok=True)
        review_path.write_text("\n".join([f"# Strategy Review Notes - {run_date}", ""] + review_notes) + "\n", encoding="utf-8")
        TradingJournalBuilder(self.output_root).build(run_date)
        return report_path

    def _load_positions(self) -> dict:
        path = self.output_root / "paper_positions" / "current.json"
        return self._load_mapping(path)

    def _position_legs_text(self, item: dict) -> str:
        legs = item.get("legs") or {}
        parts = []
        for side, label in (("long", "L"), ("short", "S")):
            leg = legs.get(side) or {}
            quantity = float(leg.get("quantity", 0) or 0)
            if quantity:
                parts.append(f"{label} {quantity:.4f} @ {float(leg.get('avg_price', 0) or 0):.2f}")
        return " · ".join(parts) if parts else "n/a"

    def _load_mapping(self, path: Path) -> dict:
        if not path.exists():
            return {}
        import json

        return json.loads(path.read_text(encoding="utf-8"))

    def _build_review_notes(
        self,
        signals: list[dict],
        tickets: list[dict],
        pending: list[dict],
        decisions: list[dict],
        paper_orders: list[dict],
        clean_manifest: list[dict],
        open_trades: list[dict] | None = None,
        closed_trades: list[dict] | None = None,
        risk_blocks: list[dict] | None = None,
        market_coverage: list[dict] | None = None,
        backtests: list[dict] | None = None,
        strategy_config: dict | None = None,
        data_quality: dict | None = None,
        performance: dict | None = None,
        data_source: dict | None = None,
        data_lineage: dict | None = None,
        official_feed: dict | None = None,
        operation_runbook: dict | None = None,
        paper_exit_monitor: dict | None = None,
        paper_exit_decisions: dict | None = None,
        paper_auto_gate: dict | None = None,
        bot_supervisor: dict | None = None,
        live_submission_safety: dict | None = None,
        live_dry_run: dict | None = None,
    ) -> list[str]:
        open_trades = open_trades or []
        closed_trades = closed_trades or []
        risk_blocks = risk_blocks or []
        market_coverage = market_coverage or []
        backtests = backtests or []
        strategy_config = strategy_config or {}
        data_quality = data_quality or {}
        performance = performance or {}
        data_source = data_source or {}
        data_lineage = data_lineage or {}
        official_feed = official_feed or {}
        operation_runbook = operation_runbook or {}
        paper_exit_monitor = paper_exit_monitor or {}
        paper_exit_decisions = paper_exit_decisions or {}
        paper_auto_gate = paper_auto_gate or {}
        bot_supervisor = bot_supervisor or {}
        live_submission_safety = live_submission_safety or {}
        live_dry_run = live_dry_run or {}
        gold_signal = next((item for item in signals if item.get("asset") == "GOLD"), signals[0] if signals else {})
        gold_backtest = next((item for item in backtests if item.get("signal_id") == gold_signal.get("signal_id")), backtests[0] if backtests else {})
        gold_ticket = next((item for item in tickets if item.get("asset") == "GOLD"), None)
        gold_manifest = next((item for item in clean_manifest if item.get("symbol") == "GOLD" and item.get("timeframe") == "5m"), {})
        latest_order = next((item for item in reversed(paper_orders) if str(item.get("ticket_id", "")).startswith("ticket_gold")), None)
        data_quality_text = (
            f"{gold_manifest.get('clean_rows', 0)} clean 5m bars, "
            f"{gold_manifest.get('missing_bars', 0)} missing gaps, "
            f"{gold_manifest.get('spike_flags', 0)} spikes"
        )
        notes = [
            f"- Strategy: {gold_signal.get('regime', 'no_signal')} / {gold_signal.get('direction', 'watch')} / strength {gold_signal.get('strength', 0)}.",
            f"- Data check: {data_quality_text}.",
            f"- Thesis: {gold_signal.get('thesis', 'No gold thesis generated.')}",
            f"- Invalid if: {gold_signal.get('invalid_if') or 'n/a'}",
        ]
        strategy = strategy_config.get("gold_5m_v1", {})
        if strategy:
            signal_config = strategy.get("signal", {})
            backtest_config = strategy.get("backtest", {})
            notes.append(
                f"- Strategy config: MA {signal_config.get('ma_short_bars')}/{signal_config.get('ma_long_bars')}, "
                f"long threshold {signal_config.get('long_strength_min')}/{signal_config.get('long_confidence_min')}, "
                f"stop/target {backtest_config.get('stop_pct')}/{backtest_config.get('target_pct')}, "
                f"hold {backtest_config.get('max_hold_bars')} bars."
            )
        if gold_backtest:
            notes.append(
                f"- Backtest: {gold_backtest.get('verdict')} on {gold_backtest.get('sample_size')} local 5m sample(s), "
                f"win_rate {gold_backtest.get('win_rate')}, avg_R {gold_backtest.get('avg_r')}, "
                f"profit_factor {gold_backtest.get('profit_factor')}."
            )
        gold_quality = data_quality.get("GOLD", {})
        if gold_quality:
            notes.append(
                f"- Data quality gate: allows_trading={gold_quality.get('allows_trading')}, "
                f"missing_ratio={gold_quality.get('missing_ratio')}, synthetic_ratio={gold_quality.get('synthetic_ratio')}."
            )
        if data_source or data_lineage or official_feed:
            price_sanity = data_source.get("price_sanity") or {}
            permissions = operation_runbook.get("permissions") or {}
            notes.append(
                f"- Data truth: {data_lineage.get('truth_level', 'unknown')} with latest "
                f"{data_source.get('latest_price', 'n/a')} from {data_source.get('latest_provider', 'n/a')}; "
                f"price_sanity={price_sanity.get('passes', 'n/a')}."
            )
            notes.append(
                f"- Official feed: status={official_feed.get('status', 'n/a')}, "
                f"official_rows={official_feed.get('official_rows', 'n/a')}, live_ready={official_feed.get('ready_for_live', 'n/a')}."
            )
            if operation_runbook:
                notes.append(
                    f"- Trading gate: {operation_runbook.get('status')} "
                    f"paper_manual={permissions.get('paper_manual_review')} "
                    f"paper_auto={permissions.get('paper_auto_approve')} "
                    f"live={permissions.get('live_trading')}."
                )
        if gold_ticket:
            notes.append(
                f"- Execution plan: {gold_ticket.get('order_type', 'limit')} {gold_ticket.get('entry_zone')} "
                f"stop {gold_ticket.get('stop_loss')} targets {gold_ticket.get('targets')} paper_only={gold_ticket.get('paper_only')}."
            )
        else:
            notes.append("- Execution plan: no approved candidate passed risk checks.")
        if latest_order:
            notes.append(f"- Paper result: {latest_order.get('status')} @ {latest_order.get('fill_price') or latest_order.get('requested_price')}.")
        elif pending:
            notes.append(f"- Paper result: {len(pending)} ticket(s) still pending manual decision.")
        elif decisions:
            notes.append(f"- Paper result: latest manual decision is {decisions[-1].get('decision_status')}.")
        else:
            notes.append("- Paper result: no manual decision recorded yet.")
        position = self._load_positions().get("GOLD", {})
        if position:
            notes.append(
                f"- Position review: {position.get('side')} {position.get('quantity')} @ {position.get('avg_price')}, "
                f"last {position.get('last_price', 'n/a')}, unrealized PnL {position.get('unrealized_pnl', 0)}."
            )
        realized = sum(float(item.get("realized_pnl", 0)) for item in closed_trades)
        notes.append(f"- Trade lifecycle: {len(open_trades)} open trade(s), {len(closed_trades)} closed today, realized PnL {realized:.4f}.")
        perf = performance.get("summary", {})
        if perf:
            notes.append(
                f"- Paper performance: net marked {perf.get('net_pnl_marked')}, "
                f"open R {perf.get('open_unrealized_r')}, expectancy R {perf.get('expectancy_r')}, "
                f"win_rate {perf.get('win_rate')}."
            )
        exit_summary = paper_exit_monitor.get("summary") or {}
        if exit_summary:
            notes.append(
                f"- Exit monitor: status={paper_exit_monitor.get('status')}, "
                f"open={exit_summary.get('open_trades')}, stop_touched={exit_summary.get('stop_touched')}, "
                f"target_touched={exit_summary.get('target_touched')}, "
                f"nearest_stop={exit_summary.get('nearest_stop_distance_pct')}%."
            )
        exit_decision_summary = paper_exit_decisions.get("summary") or {}
        if exit_decision_summary:
            notes.append(
                f"- Exit decision queue: status={paper_exit_decisions.get('status')}, "
                f"open={exit_decision_summary.get('open_items')}, high_priority={exit_decision_summary.get('high_priority')}, "
                f"recorded={exit_decision_summary.get('recorded_decisions')}."
            )
        supervisor_summary = bot_supervisor.get("summary") or {}
        if supervisor_summary:
            notes.append(
                f"- Bot supervisor: status={bot_supervisor.get('status')}, "
                f"mock_running={bot_supervisor.get('mock_bot_running')}, "
                f"runner={supervisor_summary.get('runner_state')}, "
                f"data_age={supervisor_summary.get('data_age_minutes')}m."
            )
        if live_dry_run:
            notes.append(
                f"- Live drill: status={live_dry_run.get('status')}, "
                f"safe_to_submit_live_order={live_dry_run.get('safe_to_submit_live_order')}, "
                f"data_truth={live_dry_run.get('data_truth_level')}, "
                f"blockers={len(live_dry_run.get('blockers', []))}."
            )
        if live_submission_safety:
            notes.append(
                f"- Live submission safety: status={live_submission_safety.get('status')}, "
                f"blocked_by_activation_gate={live_submission_safety.get('blocked_by_activation_gate')}, "
                f"network_call_attempted={live_submission_safety.get('network_call_attempted')}."
            )
        if paper_auto_gate:
            notes.append(
                f"- Paper auto gate: status={paper_auto_gate.get('status')}, "
                f"allow={paper_auto_gate.get('allow_auto_approve')}, "
                f"pending={paper_auto_gate.get('pending_count')}, "
                f"ticket={paper_auto_gate.get('selected_ticket_id') or 'n/a'}."
            )
        if risk_blocks:
            notes.append(f"- Risk block: {risk_blocks[-1].get('reason')}")
            notes.extend(self._blocked_candidate_lines(risk_blocks[-1:], [gold_signal] if gold_signal else signals, [gold_backtest] if gold_backtest else backtests))
        non_seed_rows = sum(
            item["rows"]
            for item in market_coverage
            if item["symbol"] == "GOLD" and item["timeframe"] == "5m" and item["provider"] != "local_synthetic_seed"
        )
        notes.append(f"- Data coverage: GOLD 5m has {non_seed_rows} non-seed local row(s); import broker/exported 5m CSV before relying on deeper backtests.")
        notes.append("- Next review: compare next 5m snapshot with entry zone, event score, and stop distance before approving another paper order.")
        return notes

    def _paper_auto_gate_lines(self, gate: dict) -> list[str]:
        if not gate:
            return ["No paper auto approval gate receipt generated yet."]
        lines = [
            f"- Status: {gate.get('status', 'unknown')}",
            f"- Auto requested: {gate.get('auto_requested')}",
            f"- Allow auto approve: {gate.get('allow_auto_approve')}",
            f"- Pending tickets: {gate.get('pending_count', 0)}",
            f"- Selected ticket: {gate.get('selected_ticket_id') or 'n/a'}",
        ]
        for reason in gate.get("reasons", []):
            lines.append(f"- Reason: {reason}")
        return lines

    def _bot_supervisor_lines(self, supervisor: dict) -> list[str]:
        if not supervisor:
            return ["No bot supervisor receipt generated yet."]
        summary = supervisor.get("summary") or {}
        lines = [
            f"- Status: {supervisor.get('status', 'unknown')}",
            f"- Mock bot running: {supervisor.get('mock_bot_running')}",
            f"- Live trading allowed: {supervisor.get('live_trading_allowed')}",
            f"- Runner state: {summary.get('runner_state', 'n/a')}",
            f"- Latest price: {summary.get('latest_price', 'n/a')} from {summary.get('latest_provider', 'n/a')}",
            f"- Data age: {summary.get('data_age_minutes', 'n/a')} minutes",
            f"- Operation status: {summary.get('operation_status', 'n/a')}",
        ]
        for item in supervisor.get("checks", []):
            lines.append(f"- {item.get('name')}: {item.get('status')} - {item.get('summary')}")
        return lines

    def _paper_exit_monitor_lines(self, monitor: dict) -> list[str]:
        if not monitor:
            return ["No paper exit monitor generated yet."]
        summary = monitor.get("summary") or {}
        lines = [
            f"- Status: {monitor.get('status', 'unknown')}",
            f"- Open trades: {summary.get('open_trades', 0)}",
            f"- Stop touched: {summary.get('stop_touched', 0)}",
            f"- Target touched: {summary.get('target_touched', 0)}",
            f"- Nearest stop distance: {summary.get('nearest_stop_distance_pct', 'n/a')}%",
        ]
        for item in (monitor.get("monitors") or [])[:5]:
            lines.append(
                f"- {item.get('trade_id', 'n/a')}: {item.get('side', 'n/a')} latest {item.get('latest_price', 'n/a')} "
                f"stop {item.get('stop_loss', 'n/a')} ({item.get('distance_to_stop_pct', 'n/a')}%) "
                f"target {item.get('target', 'n/a')} ({item.get('distance_to_target_pct', 'n/a')}%)"
            )
        return lines

    def _paper_exit_decision_lines(self, decisions: dict) -> list[str]:
        if not decisions:
            return ["No paper exit decision queue generated yet."]
        summary = decisions.get("summary") or {}
        lines = [
            f"- Status: {decisions.get('status', 'unknown')}",
            f"- Open items: {summary.get('open_items', 0)}",
            f"- High priority: {summary.get('high_priority', 0)}",
            f"- Recorded decisions: {summary.get('recorded_decisions', 0)}",
            f"- Auto close: {decisions.get('auto_close', False)}",
        ]
        for item in (decisions.get("queue") or [])[:5]:
            lines.append(
                f"- {item.get('trade_id', 'n/a')}: {item.get('priority', 'n/a')} {item.get('exit_type', 'n/a')} "
                f"action={item.get('required_user_action', 'n/a')} suggested={item.get('suggested_exit_price', 'n/a')} "
                f"latest={item.get('latest_price', 'n/a')}"
            )
        for item in (decisions.get("decisions") or [])[-5:]:
            lines.append(f"- Decision {item.get('trade_id', 'n/a')}: {item.get('decision', 'n/a')} notes={item.get('notes') or 'n/a'}")
        return lines

    def _paper_risk_action_lines(self, plan: dict) -> list[str]:
        if not plan:
            return ["No paper risk action plan generated yet."]
        summary = plan.get("summary") or {}
        lines = [
            f"- Status: {plan.get('status', 'unknown')}",
            f"- Actions: {summary.get('action_count', 0)}",
            f"- High priority: {summary.get('high_priority', 0)}",
            f"- Exit actions: {summary.get('exit_actions', 0)}",
            f"- Auto execute: {plan.get('auto_execute', False)}",
        ]
        for item in (plan.get("actions") or [])[:6]:
            lines.append(f"- [{item.get('priority')}] {item.get('summary')} | `{item.get('command')}`")
        return lines

    def _blocked_candidate_lines(self, risk_blocks: list[dict], signals: list[dict], backtests: list[dict]) -> list[str]:
        lines: list[str] = []
        for block in risk_blocks:
            signal_id = block.get("signal_id", "")
            signal = next((item for item in signals if item.get("signal_id") == signal_id), {})
            backtest = next((item for item in backtests if item.get("signal_id") == signal_id), {})
            risk = block.get("portfolio_risk") or {}
            if risk:
                risk_text = (
                    f"used {risk.get('used_loss_pct', 'n/a')}%, "
                    f"candidate {risk.get('candidate_loss_pct', 'n/a')}%, "
                    f"projected {risk.get('projected_loss_pct', 'n/a')}%, "
                    f"cap {risk.get('daily_loss_stop_pct', 'n/a')}%, "
                    f"open_trades {risk.get('open_trades', 'n/a')}"
                )
            else:
                risk_text = "risk snapshot n/a"
            lines.append(
                f"- {block.get('ticket_id') or 'candidate'}: "
                f"{signal.get('direction', 'n/a')} / {signal.get('regime', 'n/a')} / strength {signal.get('strength', 'n/a')}; "
                f"backtest {backtest.get('verdict', 'n/a')} PF {backtest.get('profit_factor', 'n/a')}; "
                f"blocked_by=\"{block.get('reason', 'n/a')}\"; {risk_text}."
            )
        return lines

    def _market_coverage(self) -> list[dict]:
        pipeline_config = load_pipeline_config()
        db_path = Path(os.getenv("TRADING_ORCHESTRATOR_MARKET_DB", str(ROOT / pipeline_config.get("local_market_db", "data/market_data.db"))))
        if not db_path.exists():
            return []
        return MarketStore(db_path).coverage()
