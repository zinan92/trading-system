from __future__ import annotations

import json
import os
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json


class TradingJournalBuilder:
    def __init__(self, output_root: Path | None = None) -> None:
        config = load_pipeline_config()
        env_output_root = os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT")
        self.output_root = output_root or Path(env_output_root or str(ROOT / config.get("output_root", "outputs")))

    def build(self, run_date: str) -> Path:
        signals = load_json(self.output_root / "signals" / f"{run_date}.json")
        tickets = load_json(self.output_root / "trade_tickets" / f"{run_date}.json")
        pending = load_json(self.output_root / "journal_pending" / f"{run_date}.json")
        decisions = load_json(self.output_root / "journal_decisions" / f"{run_date}.json")
        orders = load_json(self.output_root / "paper_orders" / f"{run_date}.json")
        open_trades = load_json(self.output_root / "paper_trades" / "current.json")
        closed_trades = load_json(self.output_root / "paper_trades" / "closed" / f"{run_date}.json")
        risk_blocks = load_json(self.output_root / "risk_blocks" / f"{run_date}.json")
        backtests = load_json(self.output_root / "backtests" / f"{run_date}.json")
        performance_rows = load_json(self.output_root / "performance" / f"{run_date}.json")
        performance = performance_rows[-1] if performance_rows else {}
        paper_exit_monitor_rows = load_json(self.output_root / "paper_exit_monitor" / f"{run_date}.json")
        paper_exit_monitor = paper_exit_monitor_rows[-1] if paper_exit_monitor_rows else {}
        paper_exit_decision_rows = load_json(self.output_root / "paper_exit_decisions" / f"{run_date}.json")
        paper_exit_decisions = paper_exit_decision_rows[-1] if paper_exit_decision_rows else {}
        equity_rows = load_json(self.output_root / "equity_curve" / f"{run_date}.json")
        equity_curve = equity_rows[-1] if equity_rows else {}
        data_quality = self._load_mapping(self.output_root / "data_quality" / f"{run_date}.json")
        data_source = self._latest_row(self.output_root / "data_source_preflight" / f"{run_date}.json")
        data_lineage = self._latest_row(self.output_root / "data_source_lineage" / f"{run_date}.json")
        data_trust = self._latest_row(self.output_root / "data_trust" / f"{run_date}.json")
        data_integrity = self._latest_row(self.output_root / "data_integrity" / f"{run_date}.json")
        official_feed = self._latest_row(self.output_root / "official_feed_receipts" / f"{run_date}.json")
        broker_quality = (official_feed.get("broker_csv") or {}).get("quality_summary") or {}
        broker_sanity = (official_feed.get("broker_csv") or {}).get("price_sanity") or {}
        operation_runbook = self._latest_row(self.output_root / "operation_runbooks" / f"{run_date}.json")
        learning_actions = self._latest_row(self.output_root / "strategy_learning_actions" / f"{run_date}.json")
        strategy_experiments = self._latest_row(self.output_root / "strategy_experiments" / f"{run_date}.json")
        strategy_improvement_plan = self._latest_row(self.output_root / "strategy_improvement_plan" / f"{run_date}.json")
        strategy_promotion = self._latest_row(self.output_root / "strategy_promotion_gate" / f"{run_date}.json")
        paper_risk_action_plan = self._latest_row(self.output_root / "paper_risk_action_plan" / f"{run_date}.json")
        paper_auto_gate = self._latest_row(self.output_root / "paper_auto_approval_gate" / f"{run_date}.json")
        bot_supervisor = self._latest_row(self.output_root / "bot_supervisor" / f"{run_date}.json")
        bot_checkpoint = self._latest_row(self.output_root / "bot_checkpoints" / f"{run_date}.json")
        live_submission_safety = self._latest_row(self.output_root / "live_submission_safety" / f"{run_date}.json")
        live_broker_preflight = self._latest_row(self.output_root / "live_broker_preflight" / f"{run_date}.json")
        live_dry_run = self._latest_row(self.output_root / "live_dry_run_drill" / f"{run_date}.json")
        positions = self._load_mapping(self.output_root / "paper_positions" / "current.json")
        review_text = self._read_text(self.output_root / "review_notes" / f"{run_date}.md")
        latest_signal = next((item for item in signals if item.get("asset") == "GOLD"), signals[0] if signals else {})
        latest_backtest = next((item for item in backtests if item.get("asset") == "GOLD"), backtests[0] if backtests else {})
        gold_quality = data_quality.get("GOLD", {})
        price_sanity = data_source.get("price_sanity") or {}
        permissions = operation_runbook.get("permissions") or {}
        realized_pnl = sum(float(item.get("realized_pnl", 0)) for item in closed_trades)

        lines = [
            f"# Trading Journal - {run_date}",
            "",
            "## Session Snapshot",
            "",
            f"- Signal: {latest_signal.get('direction', 'n/a')} / {latest_signal.get('regime', 'n/a')} / strength {latest_signal.get('strength', 'n/a')}",
            f"- Thesis: {latest_signal.get('thesis', 'n/a')}",
            f"- Invalid if: {latest_signal.get('invalid_if', 'n/a')}",
            f"- Backtest: {latest_backtest.get('verdict', 'n/a')} / samples {latest_backtest.get('sample_size', 0)} / PF {latest_backtest.get('profit_factor', 'n/a')}",
            f"- Data quality: allows_trading={gold_quality.get('allows_trading', 'n/a')} missing_ratio={gold_quality.get('missing_ratio', 'n/a')} synthetic_ratio={gold_quality.get('synthetic_ratio', 'n/a')}",
            f"- Data truth: {data_lineage.get('truth_level', 'n/a')} / latest {data_source.get('latest_price', 'n/a')} from {data_source.get('latest_provider', 'n/a')} / price_sanity={price_sanity.get('passes', 'n/a')}",
            f"- Data trust: {data_trust.get('status', 'n/a')} / mode={data_trust.get('display_mode', 'n/a')} / latest_is_mock={(data_trust.get('summary') or {}).get('latest_is_mock', 'n/a')}",
            f"- Data integrity: {data_integrity.get('status', 'n/a')} / pass={data_integrity.get('summary', {}).get('passed', 'n/a')} fail={data_integrity.get('summary', {}).get('failed', 'n/a')}",
            f"- Official feed: status={official_feed.get('status', 'n/a')} official_rows={official_feed.get('official_rows', 'n/a')} live_ready={official_feed.get('ready_for_live', 'n/a')}",
            f"- Broker CSV quality: pass={broker_quality.get('pass', 'n/a')} warn={broker_quality.get('warn', 'n/a')} fail={broker_quality.get('fail', 'n/a')} non_5m={broker_quality.get('non_5m_intervals', 'n/a')}",
            f"- Broker CSV sanity gate: enabled={broker_sanity.get('enabled', 'n/a')} range={broker_sanity.get('min_price', 'n/a')}-{broker_sanity.get('max_price', 'n/a')}",
            f"- Trading gate: {operation_runbook.get('status', 'n/a')} paper_manual={permissions.get('paper_manual_review', 'n/a')} paper_auto={permissions.get('paper_auto_approve', 'n/a')} live={permissions.get('live_trading', 'n/a')}",
            f"- Paper auto gate: {paper_auto_gate.get('status', 'n/a')} / allow={paper_auto_gate.get('allow_auto_approve', 'n/a')}",
            f"- Strategy experiments: {strategy_experiments.get('status', 'n/a')} / candidates={len(strategy_experiments.get('experiments', []))}",
            f"- Strategy improvement plan: {strategy_improvement_plan.get('status', 'n/a')} / steps={len(strategy_improvement_plan.get('next_steps', []))}",
            f"- Strategy promotion: {strategy_promotion.get('status', 'n/a')} / allowed={strategy_promotion.get('promotion_allowed', 'n/a')}",
            f"- Bot supervisor: {bot_supervisor.get('status', 'n/a')} / mock_running={bot_supervisor.get('mock_bot_running', 'n/a')}",
            f"- Bot checkpoint: {bot_checkpoint.get('status', 'n/a')} / resume_actions={len(bot_checkpoint.get('resume_actions', []))}",
            f"- Live submission safety: {live_submission_safety.get('status', 'n/a')} / blocked_by_activation_gate={live_submission_safety.get('blocked_by_activation_gate', 'n/a')} / network_call_attempted={live_submission_safety.get('network_call_attempted', 'n/a')}",
            f"- Live broker preflight: {live_broker_preflight.get('status', 'n/a')} / provider={live_broker_preflight.get('provider', 'n/a')} / real_submit_blocked={live_broker_preflight.get('real_submit_blocked', 'n/a')}",
            f"- Live drill: {live_dry_run.get('status', 'n/a')} / safe_to_submit_live_order={live_dry_run.get('safe_to_submit_live_order', 'n/a')}",
            "",
            "## Decisions",
            "",
        ]
        if not decisions:
            lines.append("No manual decisions recorded.")
        for item in decisions:
            paper_order = item.get("paper_order") or {}
            lines.extend(
                [
                    f"### {item.get('ticket_id', 'n/a')}",
                    "",
                    f"- Status: {item.get('decision_status', 'n/a')}",
                    f"- Asset: {item.get('asset', 'n/a')}",
                    f"- Decided at: {item.get('decided_at', 'n/a')}",
                    f"- Notes: {item.get('notes') or 'n/a'}",
                    f"- Actual entry: {item.get('actual_entry') or 'n/a'}",
                    f"- Actual size: {item.get('actual_size') or 'n/a'}",
                    f"- Paper order: {paper_order.get('order_id', 'n/a')} / {paper_order.get('status', 'n/a')} / fill {paper_order.get('fill_price') or paper_order.get('requested_price', 'n/a')} / cost {paper_order.get('total_cost', 0)}",
                    "",
                ]
            )

        lines.extend(["## Pending Review", ""])
        if not pending:
            lines.append("No pending manual decisions.")
        for item in pending:
            lines.append(f"- {item.get('ticket_id', 'n/a')}: {item.get('required_user_action', 'review')}")

        lines.extend(["", "## Paper Orders", ""])
        if not orders:
            lines.append("No paper orders.")
        for item in orders:
            lines.append(f"- {item.get('order_id', 'n/a')}: {item.get('status', 'n/a')} qty {item.get('quantity', 'n/a')} @ {item.get('fill_price') or item.get('requested_price', 'n/a')} cost {item.get('total_cost', 0)}")

        lines.extend(["", "## Positions And Trades", ""])
        if not positions:
            lines.append("No current paper positions.")
        for symbol, item in positions.items():
            net = f"net {item.get('net_side', item.get('side', 'n/a'))} {item.get('net_quantity', item.get('quantity', 'n/a'))}"
            lines.append(f"- {symbol}: {item.get('side', 'n/a')} gross {item.get('quantity', 'n/a')} @ {item.get('avg_price', 'n/a')} | {net} | unrealized {item.get('unrealized_pnl', 0)}")
            legs = self._position_legs_text(item)
            if legs != "n/a":
                lines.append(f"  - Legs: {legs}")
        lines.append(f"- Open trades: {len(open_trades)}")
        lines.append(f"- Closed today: {len(closed_trades)}")
        lines.append(f"- Realized PnL today: {realized_pnl:.4f}")

        perf = performance.get("summary", {})
        lines.extend(["", "## Performance Review", ""])
        if perf:
            lines.append(f"- Net marked PnL: {perf.get('net_pnl_marked', 0)}")
            lines.append(f"- Unrealized PnL: {perf.get('unrealized_pnl', 0)}")
            lines.append(f"- Open risk amount: {perf.get('open_risk_amount', 0)}")
            lines.append(f"- Execution costs open/closed: {perf.get('open_costs', 0)} / {perf.get('closed_costs', 0)}")
            lines.append(f"- Total execution costs: {perf.get('total_execution_costs', 0)}")
            lines.append(f"- Open unrealized R: {perf.get('open_unrealized_r', 0)}")
            lines.append(f"- Expectancy R: {perf.get('expectancy_r', 0)}")
            lines.append(f"- Win rate: {perf.get('win_rate', 0)}")
        else:
            lines.append("No paper performance artifact generated yet.")

        lines.extend(["", "## Exit Monitor", ""])
        exit_summary = paper_exit_monitor.get("summary") or {}
        if exit_summary:
            lines.append(f"- Status: {paper_exit_monitor.get('status', 'n/a')}")
            lines.append(f"- Open trades: {exit_summary.get('open_trades', 0)}")
            lines.append(f"- Stop touched: {exit_summary.get('stop_touched', 0)}")
            lines.append(f"- Target touched: {exit_summary.get('target_touched', 0)}")
            lines.append(f"- Nearest stop distance: {exit_summary.get('nearest_stop_distance_pct', 'n/a')}%")
            for item in (paper_exit_monitor.get("monitors") or [])[:5]:
                lines.append(
                    f"- {item.get('trade_id', 'n/a')}: {item.get('side', 'n/a')} latest {item.get('latest_price', 'n/a')} "
                    f"stop_distance {item.get('distance_to_stop_pct', 'n/a')}% target_distance {item.get('distance_to_target_pct', 'n/a')}%"
                )
        else:
            lines.append("No paper exit monitor generated yet.")

        lines.extend(["", "## Exit Decisions", ""])
        exit_decision_summary = paper_exit_decisions.get("summary") or {}
        if exit_decision_summary:
            lines.append(f"- Status: {paper_exit_decisions.get('status', 'n/a')}")
            lines.append(f"- Open items: {exit_decision_summary.get('open_items', 0)}")
            lines.append(f"- High priority: {exit_decision_summary.get('high_priority', 0)}")
            lines.append(f"- Recorded decisions: {exit_decision_summary.get('recorded_decisions', 0)}")
            for item in (paper_exit_decisions.get("queue") or [])[:5]:
                lines.append(
                    f"- {item.get('trade_id', 'n/a')}: {item.get('priority', 'n/a')} {item.get('exit_type', 'n/a')} "
                    f"action={item.get('required_user_action', 'n/a')} suggested={item.get('suggested_exit_price', 'n/a')}"
                )
            for item in (paper_exit_decisions.get("decisions") or [])[-5:]:
                lines.append(f"- Decision {item.get('trade_id', 'n/a')}: {item.get('decision', 'n/a')} notes={item.get('notes') or 'n/a'}")
        else:
            lines.append("No paper exit decision queue generated yet.")

        lines.extend(["", "## Paper Risk Actions", ""])
        risk_action_summary = paper_risk_action_plan.get("summary") or {}
        if risk_action_summary:
            lines.append(f"- Status: {paper_risk_action_plan.get('status', 'n/a')}")
            lines.append(f"- Actions: {risk_action_summary.get('action_count', 0)}")
            lines.append(f"- High priority: {risk_action_summary.get('high_priority', 0)}")
            lines.append(f"- Exit actions: {risk_action_summary.get('exit_actions', 0)}")
            for item in (paper_risk_action_plan.get("actions") or [])[:6]:
                lines.append(f"- [{item.get('priority')}] {item.get('summary')} | {item.get('command')}")
        else:
            lines.append("No paper risk action plan generated yet.")

        lines.extend(["", "## Bot Supervisor", ""])
        supervisor_summary = bot_supervisor.get("summary") or {}
        if supervisor_summary:
            lines.append(f"- Status: {bot_supervisor.get('status', 'n/a')}")
            lines.append(f"- Mock bot running: {bot_supervisor.get('mock_bot_running', 'n/a')}")
            lines.append(f"- Runner state: {supervisor_summary.get('runner_state', 'n/a')}")
            lines.append(f"- Latest price: {supervisor_summary.get('latest_price', 'n/a')} from {supervisor_summary.get('latest_provider', 'n/a')}")
            lines.append(f"- Data age: {supervisor_summary.get('data_age_minutes', 'n/a')} minutes")
            lines.append(f"- Operation status: {supervisor_summary.get('operation_status', 'n/a')}")
        else:
            lines.append("No bot supervisor receipt generated yet.")

        lines.extend(["", "## Bot Checkpoint", ""])
        if bot_checkpoint:
            summary = bot_checkpoint.get("summary") or {}
            lines.append(f"- Status: {bot_checkpoint.get('status', 'n/a')}")
            lines.append(f"- Mock recoverable: {bot_checkpoint.get('mock_recoverable', 'n/a')}")
            lines.append(f"- Live recoverable: {bot_checkpoint.get('live_recoverable', 'n/a')}")
            lines.append(f"- Runner state: {summary.get('runner_state', 'n/a')}")
            lines.append(f"- Open trades: {summary.get('open_trades', 0)}")
            lines.append(f"- Pending decisions: {summary.get('pending_decisions', 0)}")
            for item in bot_checkpoint.get("resume_actions", [])[:6]:
                lines.append(f"- [{item.get('priority')}] {item.get('action_id')}: {item.get('summary')} | {item.get('command')}")
        else:
            lines.append("No bot checkpoint generated yet.")

        lines.extend(["", "## Live Submission Safety", ""])
        if live_submission_safety:
            lines.append(f"- Status: {live_submission_safety.get('status', 'n/a')}")
            lines.append(f"- Blocked by activation gate: {live_submission_safety.get('blocked_by_activation_gate', 'n/a')}")
            lines.append(f"- Network call attempted: {live_submission_safety.get('network_call_attempted', 'n/a')}")
            lines.append(f"- Provider: {live_submission_safety.get('provider', 'n/a')}")
            lines.append(f"- Dry run: {live_submission_safety.get('dry_run', 'n/a')}")
            lines.append(f"- Live trading enabled in smoke: {live_submission_safety.get('live_trading_enabled', 'n/a')}")
            lines.append(f"- Error: {live_submission_safety.get('error') or 'n/a'}")
        else:
            lines.append("No live submission safety receipt generated yet.")

        lines.extend(["", "## Live Broker Preflight", ""])
        if live_broker_preflight:
            summary = live_broker_preflight.get("summary") or {}
            lines.append(f"- Status: {live_broker_preflight.get('status', 'n/a')}")
            lines.append(f"- Provider: {live_broker_preflight.get('provider', 'n/a')}")
            lines.append(f"- Execution mode: {live_broker_preflight.get('execution_mode', 'n/a')}")
            lines.append(f"- Dry run: {live_broker_preflight.get('dry_run', 'n/a')}")
            lines.append(f"- Real submit blocked: {live_broker_preflight.get('real_submit_blocked', 'n/a')}")
            lines.append(f"- Missing env: {', '.join(summary.get('missing_env', [])) or 'none'}")
            for item in live_broker_preflight.get("checks", [])[:7]:
                lines.append(f"- {item.get('status')}: {item.get('name')} - {item.get('summary')}")
        else:
            lines.append("No live broker preflight report generated yet.")

        lines.extend(["", "## Live Dry-Run Drill", ""])
        if live_dry_run:
            lines.append(f"- Status: {live_dry_run.get('status', 'n/a')}")
            lines.append(f"- Dry-run ready: {live_dry_run.get('dry_run_ready', 'n/a')}")
            lines.append(f"- Real-money ready: {live_dry_run.get('real_money_ready', 'n/a')}")
            lines.append(f"- Safe to submit live order: {live_dry_run.get('safe_to_submit_live_order', 'n/a')}")
            lines.append(f"- Network call attempted: {live_dry_run.get('network_call_attempted', 'n/a')}")
            lines.append(f"- Data truth: {live_dry_run.get('data_truth_level', 'n/a')} / official_rows={live_dry_run.get('official_rows', 'n/a')}")
            for item in live_dry_run.get("blockers", [])[:6]:
                lines.append(f"- Blocker: {item.get('name', 'n/a')} - {item.get('summary', 'n/a')}")
        else:
            lines.append("No live dry-run drill generated yet.")

        lines.extend(["", "## Data Trust", ""])
        if data_trust:
            summary = data_trust.get("summary") or {}
            lines.append(f"- Status: {data_trust.get('status', 'n/a')}")
            lines.append(f"- Display mode: {data_trust.get('display_mode', 'n/a')}")
            lines.append(f"- Latest price: {summary.get('latest_price', 'n/a')} from {summary.get('latest_provider', 'n/a')}")
            lines.append(f"- Latest is mock: {summary.get('latest_is_mock', 'n/a')}")
            lines.append(f"- Official rows: {summary.get('official_rows', 'n/a')}")
            for item in data_trust.get("checks", [])[:6]:
                lines.append(f"- {item.get('status')}: {item.get('name')} - {item.get('summary')}")
        else:
            lines.append("No data trust report generated yet.")

        lines.extend(["", "## Strategy Learning Actions", ""])
        if learning_actions:
            summary = learning_actions.get("summary") or {}
            lines.append(f"- Status: {learning_actions.get('status', 'n/a')}")
            lines.append(f"- Actions: {summary.get('action_count', 0)}")
            lines.append(f"- High priority: {summary.get('high_priority', 0)}")
            for item in learning_actions.get("actions", [])[:6]:
                lines.append(f"- [{item.get('priority')}] {item.get('summary')} | {item.get('command')}")
        else:
            lines.append("No strategy learning actions generated yet.")

        lines.extend(["", "## Strategy Experiments", ""])
        if strategy_experiments:
            best = strategy_experiments.get("best_candidate") or {}
            lines.append(f"- Status: {strategy_experiments.get('status', 'n/a')}")
            lines.append(f"- Paper only: {strategy_experiments.get('paper_only', 'n/a')}")
            lines.append(f"- Auto apply: {strategy_experiments.get('auto_apply', 'n/a')}")
            lines.append(f"- Sample bars: {strategy_experiments.get('sample_bars', 0)}")
            lines.append(f"- Best candidate: {best.get('variant_id', 'n/a')} score={best.get('score', 'n/a')} PF={best.get('profit_factor', 'n/a')} avgR={best.get('avg_r', 'n/a')}")
            for item in strategy_experiments.get("blockers", [])[:6]:
                lines.append(f"- Blocker: {item.get('name', 'n/a')} - {item.get('summary', 'n/a')}")
            for item in strategy_experiments.get("experiments", [])[:4]:
                lines.append(f"- Candidate: {item.get('variant_id')} verdict={item.get('verdict')} score={item.get('score')} params={item.get('parameters')}")
        else:
            lines.append("No strategy experiment queue generated yet.")

        lines.extend(["", "## Strategy Improvement Plan", ""])
        if strategy_improvement_plan:
            summary = strategy_improvement_plan.get("summary") or {}
            lines.append(f"- Status: {strategy_improvement_plan.get('status', 'n/a')}")
            lines.append(f"- Paper only: {strategy_improvement_plan.get('paper_only', 'n/a')}")
            lines.append(f"- Auto apply: {strategy_improvement_plan.get('auto_apply', 'n/a')}")
            lines.append(f"- High priority: {summary.get('high_priority', 0)}")
            lines.append(f"- Best experiment: {summary.get('best_experiment') or 'n/a'}")
            for item in strategy_improvement_plan.get("next_steps", [])[:6]:
                lines.append(f"- [{item.get('priority')}] {item.get('step_id')}: {item.get('summary')} | {item.get('command')}")
        else:
            lines.append("No strategy improvement plan generated yet.")

        lines.extend(["", "## Strategy Promotion Gate", ""])
        if strategy_promotion:
            candidate = strategy_promotion.get("candidate") or {}
            lines.append(f"- Status: {strategy_promotion.get('status', 'n/a')}")
            lines.append(f"- Promotion allowed: {strategy_promotion.get('promotion_allowed', 'n/a')}")
            lines.append(f"- Auto apply: {strategy_promotion.get('auto_apply', 'n/a')}")
            lines.append(f"- Paper only: {strategy_promotion.get('paper_only', 'n/a')}")
            lines.append(f"- Candidate: {candidate.get('variant_id', 'n/a')} score_delta={strategy_promotion.get('score_delta', 'n/a')}")
            for item in strategy_promotion.get("blockers", [])[:6]:
                lines.append(f"- Blocker: {item.get('name', 'n/a')} - {item.get('summary', 'n/a')}")
        else:
            lines.append("No strategy promotion gate generated yet.")

        lines.extend(["", "## Paper Auto Approval Gate", ""])
        if paper_auto_gate:
            lines.append(f"- Status: {paper_auto_gate.get('status', 'n/a')}")
            lines.append(f"- Auto requested: {paper_auto_gate.get('auto_requested', 'n/a')}")
            lines.append(f"- Allow auto approve: {paper_auto_gate.get('allow_auto_approve', 'n/a')}")
            lines.append(f"- Pending tickets: {paper_auto_gate.get('pending_count', 0)}")
            lines.append(f"- Selected ticket: {paper_auto_gate.get('selected_ticket_id') or 'n/a'}")
            for reason in paper_auto_gate.get("reasons", []):
                lines.append(f"- Reason: {reason}")
        else:
            lines.append("No paper auto approval gate receipt generated yet.")

        lines.extend(["", "## Equity Curve", ""])
        if equity_curve:
            lines.append(f"- Current equity: {equity_curve.get('current_equity', 0)}")
            lines.append(f"- Current drawdown: {equity_curve.get('current_drawdown_pct', 0)}%")
            lines.append(f"- Max drawdown: {equity_curve.get('max_drawdown_pct', 0)}%")
            lines.append(f"- Curve points: {equity_curve.get('point_count', 0)}")
        else:
            lines.append("No paper equity curve artifact generated yet.")

        lines.extend(["", "## Risk Notes", ""])
        if not risk_blocks:
            lines.append("No risk blocks.")
        for item in risk_blocks:
            lines.append(f"- {item.get('asset', 'n/a')}: {item.get('reason', 'n/a')}")

        lines.extend(["", "## Blocked Candidates", ""])
        if not risk_blocks:
            lines.append("No blocked trade candidates.")
        for item in risk_blocks:
            signal_id = item.get("signal_id", "")
            signal = next((candidate for candidate in signals if candidate.get("signal_id") == signal_id), {})
            backtest = next((candidate for candidate in backtests if candidate.get("signal_id") == signal_id), {})
            risk = item.get("portfolio_risk") or {}
            lines.append(
                f"- {item.get('ticket_id') or 'candidate'}: "
                f"{signal.get('direction', 'n/a')} / {signal.get('regime', 'n/a')} / strength {signal.get('strength', 'n/a')}; "
                f"backtest {backtest.get('verdict', 'n/a')} PF {backtest.get('profit_factor', 'n/a')}; "
                f"used {risk.get('used_loss_pct', 'n/a')}%, candidate {risk.get('candidate_loss_pct', 'n/a')}%, "
                f"projected {risk.get('projected_loss_pct', 'n/a')}%, cap {risk.get('daily_loss_stop_pct', 'n/a')}%; "
                f"blocked_by={item.get('reason', 'n/a')}."
            )

        lines.extend(["", "## Review Notes", ""])
        if review_text:
            review_lines = [line for line in review_text.splitlines() if line.strip()]
            lines.extend(review_lines[:40])
        else:
            lines.append("No strategy review notes generated yet.")

        path = self.output_root / "journals" / f"{run_date}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def _read_text(self, path: Path) -> str:
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    def _load_mapping(self, path: Path) -> dict:
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _position_legs_text(self, item: dict) -> str:
        legs = item.get("legs") or {}
        parts = []
        for side, label in (("long", "L"), ("short", "S")):
            leg = legs.get(side) or {}
            quantity = float(leg.get("quantity", 0) or 0)
            if quantity:
                parts.append(f"{label} {quantity:.4f} @ {float(leg.get('avg_price', 0) or 0):.2f}")
        return " · ".join(parts) if parts else "n/a"

    def _latest_row(self, path: Path) -> dict:
        rows = load_json(path)
        return rows[-1] if rows else {}
