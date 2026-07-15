from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config, load_risk_rules, load_strategy_config
from services.execution_accounting import (
    executed_record_ids,
    execution_record_status,
)
from services.journal_store import load_json
from services.market_data_access import market_data_repository, uses_independent_datafeed
from services.market_view import MarketViewStore, infer_market_view_reference_price, market_view_target_expiry_bounds
from services.official_market_data_gate import execution_venue_rows, is_official_broker_ohlc_ready
from services.paper_executor import PaperExecutor
from services.portfolio_risk import PortfolioRiskState
from services.runner_status import RunnerStatusStore
from services.edge_judgment import EdgeJudgment
from services.strategy_book import StrategyBook
from services.system_vitals import SystemVitals
from services.trade_record_card import TradeRecordCardBuilder


class DashboardState:
    SCHEMA_VERSION = "dashboard-v2.1"
    STABLE_SECTIONS = ("performance_board", "strategy_detail", "review_loop", "dashboard_health")
    _DATE_TOKEN_PATTERN = re.compile(r"(20\d{2})(\d{2})(\d{2})")
    _REPLAY_OHLC_MAX_BARS = 9_000
    _REPLAY_OHLC_PAD_SECONDS = 6 * 60 * 60

    def __init__(self, output_root: Path | None = None, market_db: Path | None = None) -> None:
        config = load_pipeline_config()
        self.config = config
        env_output_root = os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT")
        env_market_db = os.getenv("TRADING_ORCHESTRATOR_MARKET_DB")
        self.output_root = output_root or Path(env_output_root or str(ROOT / config.get("output_root", "outputs")))
        db_value = market_db or Path(env_market_db or str(ROOT / config.get("local_market_db", "data/market_data.db")))
        self.market_db = db_value

    def snapshot(self, run_date: str) -> dict:
        strategy_id = self._strategy_id_from_output_root()
        strategy_config = load_strategy_config()
        executor = PaperExecutor(self.output_root)
        executor.evaluate_exits(run_date)
        executor.mark_to_market(run_date)
        bars, bar_timeframe = self._load_primary_bars(run_date, strategy_id, strategy_config)
        latest = bars[-1] if bars else {}
        latest_quote = market_data_repository(self.market_db).load_latest_quote("GOLD")
        decisions = load_json(self.output_root / "journal_decisions" / f"{run_date}.json")
        pending = load_json(self.output_root / "journal_pending" / f"{run_date}.json")
        orders = load_json(self.output_root / "paper_orders" / f"{run_date}.json")
        positions = self._load_mapping(self.output_root / "paper_positions" / "current.json")
        signals = load_json(self.output_root / "signals" / f"{run_date}.json")
        backtests = load_json(self.output_root / "backtests" / f"{run_date}.json")
        tickets = load_json(self.output_root / "trade_tickets" / f"{run_date}.json")
        manifest = load_json(self.output_root / "clean_bars" / run_date / "manifest.json")
        review = self._read_text(self.output_root / "review_notes" / f"{run_date}.md")
        journal = self._read_text(self.output_root / "journals" / f"{run_date}.md")
        collector_runs = load_json(self.output_root / "collector_runs" / f"{run_date}.json")
        open_trades = load_json(self.output_root / "paper_trades" / "current.json")
        closed_trades = load_json(self.output_root / "paper_trades" / "closed" / f"{run_date}.json")
        all_closed_trades = self._load_all_closed_trades()
        paper_performance_rows = load_json(self.output_root / "performance" / "current.json")
        equity_curve_rows = load_json(self.output_root / "equity_curve" / "current.json")
        paper_reconciliation_rows = load_json(self.output_root / "paper_reconciliation" / "current.json")
        paper_attribution_rows = load_json(self.output_root / "paper_trade_attribution" / "current.json")
        paper_exit_monitor_rows = load_json(self.output_root / "paper_exit_monitor" / "current.json")
        paper_exit_decision_rows = load_json(self.output_root / "paper_exit_decisions" / "current.json")
        daily_review_rows = load_json(self.output_root / "daily_review_runs" / "current.json")
        operation_runbook_rows = load_json(self.output_root / "operation_runbooks" / "current.json")
        paper_execution_blocks = load_json(self.output_root / "paper_execution_blocks" / f"{run_date}.json")
        risk_blocks = load_json(self.output_root / "risk_blocks" / f"{run_date}.json")
        data_quality = self._load_mapping(self.output_root / "data_quality" / f"{run_date}.json")
        data_gap_rows = load_json(self.output_root / "data_gaps" / "current.json")
        data_gap_repair_rows = load_json(self.output_root / "data_gap_repair_requests" / "current.json")
        data_archive_rows = load_json(self.output_root / "data_archive" / "current.json")
        data_integrity_rows = load_json(self.output_root / "data_integrity" / "current.json")
        broker_preflight_rows = load_json(self.output_root / "broker_preflight" / "current.json")
        data_source_preflight_rows = load_json(self.output_root / "data_source_preflight" / "current.json")
        data_source_lineage_rows = load_json(self.output_root / "data_source_lineage" / "current.json")
        data_trust_rows = load_json(self.output_root / "data_trust" / "current.json")
        broker_feed_doctor_rows = load_json(self.output_root / "broker_feed_doctor" / "current.json")
        broker_feed_rows = load_json(self.output_root / "broker_feed_imports" / "current.json")
        broker_feed_smoke_rows = load_json(self.output_root / "broker_feed_smoke" / "current.json")
        official_feed_receipt_rows = load_json(self.output_root / "official_feed_receipts" / "current.json")
        official_feed_onboarding_rows = load_json(self.output_root / "official_feed_onboarding" / "current.json")
        oanda_feed_rows = load_json(self.output_root / "oanda_feed" / "current.json")
        oanda_account_rows = load_json(self.output_root / "oanda_account" / "current.json")
        binance_feed_rows = load_json(self.output_root / "binance_usdm_feed" / "current.json")
        data_health_rows = load_json(self.output_root / "data_health" / "current.json")
        live_order_requests = load_json(self.output_root / "live_order_requests" / f"{run_date}.json")
        broker_receipts = load_json(self.output_root / "broker_receipts" / "current.json")
        broker_receipt_summary = load_json(self.output_root / "broker_receipts" / "summary_current.json")
        mt5_smoke_rows = load_json(self.output_root / "mt5_bridge_smoke" / "current.json")
        strategy_reviews = load_json(self.output_root / "strategy_reviews" / f"{run_date}.json")
        strategy_snapshot_rows = load_json(self.output_root / "strategy_snapshots" / "current.json")
        learning_ledger_rows = load_json(self.output_root / "learning_ledger" / "current.json")
        strategy_proposal_rows = load_json(self.output_root / "strategy_change_proposals" / "current.json")
        strategy_learning_actions_rows = load_json(self.output_root / "strategy_learning_actions" / "current.json")
        strategy_experiment_rows = load_json(self.output_root / "strategy_experiments" / "current.json")
        strategy_improvement_plan_rows = load_json(self.output_root / "strategy_improvement_plan" / "current.json")
        strategy_promotion_rows = load_json(self.output_root / "strategy_promotion_gate" / "current.json")
        strategy_guardrails_rows = load_json(self.output_root / "strategy_guardrails" / "current.json")
        risk_monitor_rows = load_json(self.output_root / "risk_monitor" / "current.json")
        live_money_guardrails_rows = load_json(self.output_root / "live_money_guardrails" / "current.json")
        paper_risk_action_plan_rows = load_json(self.output_root / "paper_risk_action_plan" / "current.json")
        paper_auto_gate_rows = load_json(self.output_root / "paper_auto_approval_gate" / "current.json")
        health_rows = load_json(self.output_root / "health" / "current.json")
        alerts_rows = load_json(self.output_root / "alerts" / "current.json")
        leaderboard_rows = load_json(self.output_root / "strategy_leaderboard" / "current.json")
        strategy_frequency_rows = load_json(self.output_root / "strategy_frequency" / "current.json")
        strategy_daily_review_rows = load_json(self.output_root / "strategy_daily_reviews" / "current.json")
        daily_trade_sample_rows = load_json(self.output_root / "daily_trade_samples" / "current.json")
        trade_review_rows = load_json(self.output_root / "trade_reviews" / "current.json")
        backend_maturity_rows = load_json(self.output_root / "backend_maturity" / "current.json")
        strategy_summary_rows = load_json(self.output_root / "strategies" / "summary_current.json")
        audit_rows = load_json(self.output_root / "audits" / "current.json")
        mock_runtime_rows = load_json(self.output_root / "mock_runtime" / "current.json")
        mock_uat_rows = load_json(self.output_root / "mock_uat" / "current.json")
        bot_supervisor_rows = load_json(self.output_root / "bot_supervisor" / "current.json")
        bot_checkpoint_rows = load_json(self.output_root / "bot_checkpoints" / "current.json")
        live_readiness_rows = load_json(self.output_root / "live_readiness" / "current.json")
        live_env_rows = load_json(self.output_root / "live_env" / "current.json")
        live_activation_rows = load_json(self.output_root / "live_activation" / "current.json")
        live_approval_rows = load_json(self.output_root / "live_approvals" / "current.json")
        live_submission_safety_rows = load_json(self.output_root / "live_submission_safety" / "current.json")
        live_reconciliation_rows = load_json(self.output_root / "live_reconciliation" / "current.json")
        legacy_live_reconciliation = live_reconciliation_rows[-1] if live_reconciliation_rows else {}
        live_broker_preflight_rows = load_json(self.output_root / "live_broker_preflight" / "current.json")
        live_switch_plan_rows = load_json(self.output_root / "live_switch_plan" / "current.json")
        live_cutover_rows = load_json(self.output_root / "live_cutover" / "current.json")
        live_dry_run_drill_rows = load_json(self.output_root / "live_dry_run_drill" / "current.json")
        trading_plan_rows = load_json(self.output_root / "trading_plans" / "current.json")
        evening_review_rows = load_json(self.output_root / "evening_reviews" / "current.json")
        doctor_rows = load_json(self.output_root / "doctor" / "current.json")
        schedule_rows = load_json(self.output_root / "schedules" / "current.json")
        schedule_status_rows = load_json(self.output_root / "schedules" / "status_current.json")
        schedule_install_plan_rows = load_json(self.output_root / "schedules" / "install_plan_current.json")
        schedule_install_rows = load_json(self.output_root / "schedules" / "install_current.json")
        schedule_rollback_plan_rows = load_json(self.output_root / "schedules" / "rollback_plan_current.json")
        schedule_rollback_rows = load_json(self.output_root / "schedules" / "rollback_current.json")
        schedule_post_install_verify_rows = load_json(self.output_root / "schedules" / "post_install_verify_current.json")
        schedule_takeover_package_rows = load_json(self.output_root / "schedules" / "takeover_package_current.json")
        schedule_takeover_package_check_rows = load_json(self.output_root / "schedules" / "takeover_package_check_current.json")
        risk = self._risk_summary(run_date, tickets, risk_blocks)
        runner = RunnerStatusStore(self.output_root).current()
        data_source_preflight = data_source_preflight_rows[-1] if data_source_preflight_rows else {}
        market_db = self._market_db_summary()
        data_provenance = self._data_provenance_summary(data_source_preflight, market_db, latest, latest_quote)
        market_view_status = self._market_view_status(run_date, latest, latest_quote)
        data_trust = data_trust_rows[-1] if data_trust_rows else {}
        official_feed_receipt = official_feed_receipt_rows[-1] if official_feed_receipt_rows else {}
        oanda_feed = oanda_feed_rows[-1] if oanda_feed_rows else {}
        broker_feed_doctor = broker_feed_doctor_rows[-1] if broker_feed_doctor_rows else {}
        ohlc_quality = self._ohlc_quality(
            run_date,
            bars,
            data_trust,
            official_feed_receipt,
            data_source_preflight,
            market_db,
            oanda_feed,
            broker_feed_doctor,
        )
        broker_feed = broker_feed_rows[-1] if broker_feed_rows else {}
        market_data_gate = self._market_data_gate(
            run_date,
            ohlc_quality,
            official_feed_receipt,
            data_source_preflight,
            data_provenance,
            oanda_feed,
            broker_feed_doctor,
            broker_feed,
        )
        paper_account = self.config.get(
            "paper_account",
            {
                "starting_equity": 10000,
                "max_allowed_leverage": 5,
                "base_currency": "USD",
                "default_ticker": "XAU",
            },
        )
        paper_performance = paper_performance_rows[-1] if paper_performance_rows else {}
        equity_curve = equity_curve_rows[-1] if equity_curve_rows else {}
        leaderboard = leaderboard_rows[-1] if leaderboard_rows else {}
        strategy_summary = strategy_summary_rows[-1] if strategy_summary_rows else {}
        health = health_rows[-1] if health_rows else {}
        trading_plan = trading_plan_rows[-1] if trading_plan_rows else {}
        evening_review = evening_review_rows[-1] if evening_review_rows else {}
        live_reconciliation = self._dashboard_live_reconciliation(trading_plan, legacy_live_reconciliation)
        performance_board = self._performance_board(
            run_date,
            leaderboard,
            strategy_summary,
            bars,
            strategy_config,
            trading_plan,
            health,
            data_source_preflight,
            ohlc_quality,
            market_data_gate,
        )
        intraday_nav_curve = self._intraday_nav_curve(
            bars,
            paper_performance,
            open_trades,
            paper_account,
            equity_curve,
            all_closed_trades,
        )
        strategy_detail = self._strategy_detail(
            run_date=run_date,
            strategy_id=strategy_id,
            timeframe=bar_timeframe,
            bars=bars,
            signals=signals,
            tickets=tickets,
            orders=orders,
            decisions=decisions,
            open_trades=open_trades,
            closed_trades=closed_trades,
            all_closed_trades=all_closed_trades,
            paper_performance=paper_performance,
            equity_curve=equity_curve,
            risk_blocks=risk_blocks,
            risk_monitor=risk_monitor_rows[-1] if risk_monitor_rows else {},
            live_reconciliation=live_reconciliation,
            paper_exit_monitor=paper_exit_monitor_rows[-1] if paper_exit_monitor_rows else {},
            paper_exit_decisions=paper_exit_decision_rows[-1] if paper_exit_decision_rows else {},
            paper_reconciliation=paper_reconciliation_rows[-1] if paper_reconciliation_rows else {},
            strategy_config=strategy_config,
            ohlc_quality=ohlc_quality,
            nav_curve_intraday=intraday_nav_curve,
        )
        review_loop = self._review_loop_board(
            trading_plan,
            evening_review,
            strategy_experiment_rows[-1] if strategy_experiment_rows else {},
            strategy_improvement_plan_rows[-1] if strategy_improvement_plan_rows else {},
            performance_board,
        )
        dashboard_health = self._dashboard_health(
            run_date,
            performance_board,
            leaderboard,
            health,
            data_source_preflight,
            bars,
            ohlc_quality,
            market_data_gate,
        )
        contract = self._schema_contract(run_date, strategy_id, bar_timeframe)
        source_contracts = self._source_contracts(run_date, strategy_id, bar_timeframe)
        return {
            "contract": contract,
            "run_date": run_date,
            "strategy_id": strategy_id,
            "bar_timeframe": bar_timeframe,
            "latest": latest,
            "latest_quote": latest_quote,
            "bars": bars,
            "manifest": manifest,
            "signals": signals,
            "backtests": backtests,
            "tickets": tickets,
            "orders": orders,
            "positions": positions,
            "decisions": decisions,
            "pending": pending,
            "review": review,
            "journal": journal,
            "collector_runs": collector_runs,
            "open_trades": open_trades,
            "closed_trades": closed_trades,
            "paper_performance": paper_performance,
            "equity_curve": equity_curve,
            "nav_curve_intraday": intraday_nav_curve,
            "paper_reconciliation": paper_reconciliation_rows[-1] if paper_reconciliation_rows else {},
            "paper_trade_attribution": paper_attribution_rows[-1] if paper_attribution_rows else {},
            "paper_exit_monitor": paper_exit_monitor_rows[-1] if paper_exit_monitor_rows else {},
            "paper_exit_decisions": paper_exit_decision_rows[-1] if paper_exit_decision_rows else {},
            "daily_review": daily_review_rows[-1] if daily_review_rows else {},
            "operation_runbook": operation_runbook_rows[-1] if operation_runbook_rows else {},
            "paper_execution_blocks": paper_execution_blocks,
            "risk_blocks": risk_blocks,
            "data_quality": data_quality,
            "data_gaps": data_gap_rows[-1] if data_gap_rows else {},
            "data_gap_repair": data_gap_repair_rows[-1] if data_gap_repair_rows else {},
            "data_archive": data_archive_rows[-1] if data_archive_rows else {},
            "data_integrity": data_integrity_rows[-1] if data_integrity_rows else {},
            "broker_preflight": broker_preflight_rows[-1] if broker_preflight_rows else {},
            "data_source_preflight": data_source_preflight,
            "data_source_lineage": data_source_lineage_rows[-1] if data_source_lineage_rows else {},
            "data_trust": data_trust,
            "broker_feed_doctor": broker_feed_doctor,
            "broker_feed": broker_feed,
            "broker_feed_smoke": broker_feed_smoke_rows[-1] if broker_feed_smoke_rows else {},
            "official_feed_receipt": official_feed_receipt,
            "official_feed_onboarding": official_feed_onboarding_rows[-1] if official_feed_onboarding_rows else {},
            "oanda_feed": oanda_feed,
            "oanda_account": oanda_account_rows[-1] if oanda_account_rows else {},
            "binance_usdm_feed": binance_feed_rows[-1] if binance_feed_rows else {},
            "data_health": data_health_rows[-1] if data_health_rows else {},
            "live_order_requests": live_order_requests,
            "broker_receipts": broker_receipts,
            "broker_receipt_summary": broker_receipt_summary[-1] if broker_receipt_summary else {},
            "mt5_bridge_smoke": mt5_smoke_rows[-1] if mt5_smoke_rows else {},
            "strategy_review": strategy_reviews[-1] if strategy_reviews else {},
            "strategy_snapshot": strategy_snapshot_rows[-1] if strategy_snapshot_rows else {},
            "learning_ledger": learning_ledger_rows[-1] if learning_ledger_rows else {},
            "strategy_change_proposal": strategy_proposal_rows[-1] if strategy_proposal_rows else {},
            "strategy_learning_actions": strategy_learning_actions_rows[-1] if strategy_learning_actions_rows else {},
            "strategy_experiments": strategy_experiment_rows[-1] if strategy_experiment_rows else {},
            "strategy_improvement_plan": strategy_improvement_plan_rows[-1] if strategy_improvement_plan_rows else {},
            "strategy_promotion_gate": strategy_promotion_rows[-1] if strategy_promotion_rows else {},
            "strategy_guardrails": strategy_guardrails_rows[-1] if strategy_guardrails_rows else {},
            "risk_monitor": risk_monitor_rows[-1] if risk_monitor_rows else {},
            "live_money_guardrails": live_money_guardrails_rows[-1] if live_money_guardrails_rows else {},
            "paper_risk_action_plan": paper_risk_action_plan_rows[-1] if paper_risk_action_plan_rows else {},
            "paper_auto_approval_gate": paper_auto_gate_rows[-1] if paper_auto_gate_rows else {},
            "health": health,
            "system_vitals": SystemVitals(self.output_root, self.market_db).run(run_date, persist=False),
            "alerts": alerts_rows[-1] if alerts_rows else {},
            "strategy_leaderboard": leaderboard,
            "strategy_frequency": strategy_frequency_rows[-1] if strategy_frequency_rows else {},
            "strategy_daily_reviews": strategy_daily_review_rows[-1] if strategy_daily_review_rows else {},
            "daily_trade_samples": daily_trade_sample_rows[-1] if daily_trade_sample_rows else {},
            "trade_reviews": trade_review_rows[-1] if trade_review_rows else {},
            "backend_maturity": backend_maturity_rows[-1] if backend_maturity_rows else {},
            "strategy_summary": strategy_summary,
            "audit": audit_rows[-1] if audit_rows else {},
            "mock_runtime": mock_runtime_rows[-1] if mock_runtime_rows else {},
            "mock_uat": mock_uat_rows[-1] if mock_uat_rows else {},
            "bot_supervisor": bot_supervisor_rows[-1] if bot_supervisor_rows else {},
            "bot_checkpoint": bot_checkpoint_rows[-1] if bot_checkpoint_rows else {},
            "live_readiness": live_readiness_rows[-1] if live_readiness_rows else {},
            "live_env": live_env_rows[-1] if live_env_rows else {},
            "live_activation": live_activation_rows[-1] if live_activation_rows else {},
            "live_approval": live_approval_rows[-1] if live_approval_rows else {},
            "live_submission_safety": live_submission_safety_rows[-1] if live_submission_safety_rows else {},
            "live_reconciliation": live_reconciliation,
            "legacy_live_reconciliation": self._decorate_live_reconciliation(
                legacy_live_reconciliation,
                truth_scope="legacy_global",
                strategy_id="",
                path=self.output_root / "live_reconciliation" / "current.json",
            ) if legacy_live_reconciliation else {},
            "live_broker_preflight": live_broker_preflight_rows[-1] if live_broker_preflight_rows else {},
            "live_switch_plan": live_switch_plan_rows[-1] if live_switch_plan_rows else {},
            "live_cutover": live_cutover_rows[-1] if live_cutover_rows else {},
            "live_dry_run_drill": live_dry_run_drill_rows[-1] if live_dry_run_drill_rows else {},
            "trading_plan": trading_plan,
            "evening_review": evening_review,
            "doctor": doctor_rows[-1] if doctor_rows else {},
            "schedule": schedule_rows[-1] if schedule_rows else {},
            "schedule_status": schedule_status_rows[-1] if schedule_status_rows else {},
            "schedule_install_plan": schedule_install_plan_rows[-1] if schedule_install_plan_rows else {},
            "schedule_install": schedule_install_rows[-1] if schedule_install_rows else {},
            "schedule_rollback_plan": schedule_rollback_plan_rows[-1] if schedule_rollback_plan_rows else {},
            "schedule_rollback": schedule_rollback_rows[-1] if schedule_rollback_rows else {},
            "schedule_post_install_verify": schedule_post_install_verify_rows[-1] if schedule_post_install_verify_rows else {},
            "schedule_takeover_package": schedule_takeover_package_rows[-1] if schedule_takeover_package_rows else {},
            "schedule_takeover_package_check": schedule_takeover_package_check_rows[-1] if schedule_takeover_package_check_rows else {},
            "performance": self._performance_summary(open_trades, closed_trades),
            "risk": risk,
            "runner": runner,
            "paper_account": paper_account,
            "market_db": market_db,
            "data_provenance": data_provenance,
            "market_view_status": market_view_status,
            "ohlc_quality": ohlc_quality,
            "market_data_gate": market_data_gate,
            "performance_board": performance_board,
            "strategy_detail": strategy_detail,
            "review_loop": review_loop,
            "dashboard_health": dashboard_health,
            "source_contracts": source_contracts,
            "strategy_config": strategy_config,
            "risk_rules": load_risk_rules(),
        }

    def _schema_contract(self, run_date: str, strategy_id: str, timeframe: str) -> dict:
        generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        return {
            "schema_version": self.SCHEMA_VERSION,
            "generated_at": generated_at,
            "run_date": run_date,
            "strategy_id": strategy_id,
            "timeframe": timeframe,
            "stable_sections": list(self.STABLE_SECTIONS),
            "sample_count_contract": {
                "source_of_truth": "daily_execution.executed_trade_count",
                "true_sample_fields": [
                    "performance_board.today_executed_trade_count",
                    "performance_board.strategies[].today_trade_count",
                    "performance_board.strategies[].trade_count_7d",
                ],
                "excludes": ["watch", "no-trade", "no_signal", "risk_block", "blocked", "skipped", "pending"],
                "inactive_rule": "executed_trade_count < 1 => inactive / low sample",
                "low_frequency_rule": "consecutive inactive days >= 3 => low_frequency_failure",
            },
            "runtime_boundaries": [
                "local JSON artifacts",
                "GET /api/dashboard",
                "GET /api/dashboard?view=trader",
                "GET /api/dashboard?strategy=<id>",
                "no Freqtrade runtime",
                "no QuantConnect API",
                "no live/demo gate mutation",
            ],
        }

    def _source_contracts(self, run_date: str, strategy_id: str, timeframe: str) -> dict:
        detail_root = self._detail_namespace(strategy_id)
        return {
            "performance_board": {
                "section": "performance_board",
                "required_sources": [
                    self._artifact_ref(self.output_root / "strategy_leaderboard" / "current.json", True),
                    self._artifact_ref(self.output_root / "strategies" / "summary_current.json", False),
                    self._artifact_ref(self.output_root / "data_source_preflight" / "current.json", False),
                    self._artifact_ref(self.output_root / "trading_plans" / "current.json", False),
                ],
                "per_strategy_sources": [
                    "outputs/strategies/<strategy_id>/equity_curve/current.json",
                    f"outputs/strategies/<strategy_id>/paper_orders/{run_date}.json",
                    f"outputs/strategies/<strategy_id>/demo_order_requests/{run_date}.json",
                    f"outputs/strategies/<strategy_id>/live_order_requests/{run_date}.json",
                    f"outputs/strategies/<strategy_id>/journal_decisions/{run_date}.json",
                ],
                "freshness_fields": ["generated_at", "freshness.modified_at"],
            },
            "strategy_detail": {
                "section": "strategy_detail",
                "strategy_id": strategy_id,
                "timeframe": timeframe,
                "required_sources": [
                    self._artifact_ref(detail_root / "clean_bars" / run_date / f"GOLD_{timeframe}.json", True),
                    self._artifact_ref(detail_root / "signals" / f"{run_date}.json", False),
                    self._artifact_ref(detail_root / "trade_tickets" / f"{run_date}.json", False),
                    self._artifact_ref(detail_root / "journal_decisions" / f"{run_date}.json", False),
                    self._artifact_ref(detail_root / "paper_orders" / f"{run_date}.json", False),
                    self._artifact_ref(detail_root / "paper_trades" / "current.json", False),
                    self._artifact_ref(detail_root / "paper_trades" / "closed" / f"{run_date}.json", False),
                    self._artifact_ref(detail_root / "paper_exit_decisions" / "current.json", False),
                    self._artifact_ref(detail_root / "risk_blocks" / f"{run_date}.json", False),
                    self._artifact_ref(detail_root / "live_reconciliation" / "current.json", False),
                ],
            },
            "review_loop": {
                "section": "review_loop",
                "required_sources": [
                    self._artifact_ref(self.output_root / "trading_plans" / "current.json", False),
                    self._artifact_ref(self.output_root / "evening_reviews" / "current.json", False),
                    self._artifact_ref(self.output_root / "strategy_experiments" / "current.json", False),
                    self._artifact_ref(self.output_root / "strategy_improvement_plan" / "current.json", False),
                ],
            },
            "dashboard_health": {
                "section": "dashboard_health",
                "required_sources": [
                    self._artifact_ref(self.output_root / "health" / "current.json", False),
                    self._artifact_ref(self.output_root / "data_source_preflight" / "current.json", False),
                    self._artifact_ref(self.output_root / "data_trust" / "current.json", False),
                    self._artifact_ref(self.output_root / "official_feed_receipts" / "current.json", False),
                    self._artifact_ref(self.output_root / "strategy_leaderboard" / "current.json", True),
                ],
            },
        }

    def _detail_namespace(self, strategy_id: str) -> Path:
        if self.output_root.parent.name == "strategies":
            return self.output_root
        if strategy_id:
            return self.output_root / "strategies" / strategy_id
        return self.output_root

    def _strategy_namespace(self, strategy_id: str) -> Path:
        if self.output_root.parent.name == "strategies" and self.output_root.name == strategy_id:
            return self.output_root
        return self.output_root / "strategies" / strategy_id

    def _artifact_ref(self, path: Path, required: bool = False) -> dict:
        return {
            "path": self._display_path(path),
            "required": required,
            **self._artifact_freshness(path),
        }

    def _display_path(self, path: Path) -> str:
        try:
            return str(path.relative_to(ROOT))
        except ValueError:
            return str(path)

    def _strategy_id_from_output_root(self) -> str:
        if self.output_root.parent.name == "strategies":
            return self.output_root.name
        return ""

    def _global_output_root(self) -> Path:
        if self.output_root.parent.name == "strategies":
            return self.output_root.parent.parent
        return self.output_root

    def _market_view_status(self, run_date: str, latest: dict, latest_quote: dict) -> dict:
        root = self._global_output_root()
        rows = load_json(root / "market_views" / f"{run_date}.json")
        if not rows:
            rows = load_json(root / "market_views" / "current.json")
        view = rows[-1] if rows else {}
        checked_at = self._market_view_checked_at(latest, latest_quote)
        current_price = self._market_view_price(latest, latest_quote)
        if not view:
            return {
                "status": "missing",
                "expired": True,
                "filter_effect": "neutral_no_filter",
                "operator_message": "没有可用口述方向；系统不会按人工方向过滤多空信号。",
                "checked_at": checked_at.isoformat(),
                "current_price": current_price,
            }
        expiry = view.get("expiry") or {}
        generated_at = self._parse_dt(view.get("generated_at")) or checked_at
        valid_hours = self._safe_float(expiry.get("valid_for_hours"))
        if valid_hours is None:
            valid_hours = MarketViewStore.DEFAULT_VALID_FOR_HOURS
        expires_at = self._parse_dt(expiry.get("expires_at"))
        if expires_at is None:
            expires_at = generated_at.replace(microsecond=0) + timedelta(hours=valid_hours)
        reference_price = infer_market_view_reference_price(view, self.market_db)
        move_pct = self._safe_float(expiry.get("expires_if_price_moves_pct"))
        if move_pct is None:
            move_pct = MarketViewStore.DEFAULT_PRICE_MOVE_EXPIRY_PCT
        expire_above, expire_below, target_rule = market_view_target_expiry_bounds(view)
        payload = {
            "run_date": str(view.get("run_date") or run_date),
            "source": str(view.get("source") or ""),
            "generated_at": generated_at.isoformat(),
            "checked_at": checked_at.isoformat(),
            "direction_score": view.get("direction_score"),
            "direction_bias": view.get("direction_bias", ""),
            "stance": view.get("stance", ""),
            "summary": view.get("summary", ""),
            "trade_plan": view.get("trade_plan", ""),
            "key_levels": view.get("key_levels", []),
            "timeframes": view.get("timeframes", []),
            "current_price": current_price,
            "reference_price": reference_price,
            "expires_at": expires_at.isoformat(),
            "valid_for_hours": valid_hours,
            "expires_if_price_moves_pct": move_pct,
            "target_price": self._safe_float(expiry.get("target_price")),
            "target_rule": target_rule,
            "expire_above": expire_above,
            "expire_below": expire_below,
            "price_expiry_ready": bool(reference_price and move_pct),
            "status": "active",
            "expired": False,
            "reason": "market view active",
            "filter_effect": "active_direction_filter_enabled",
            "operator_message": "口述方向仍有效；强多/强空会过滤反向信号，偏多/偏空会降权反向信号。",
        }
        expired_reason = ""
        if checked_at > expires_at:
            expired_reason = f"time expired at {expires_at.isoformat()}"
        elif current_price is not None:
            if expire_above is not None and current_price >= expire_above:
                expired_reason = f"price reached expire_above {expire_above}"
            elif expire_below is not None and current_price <= expire_below:
                expired_reason = f"price reached expire_below {expire_below}"
            elif reference_price and move_pct:
                actual_move = abs((current_price - reference_price) / reference_price) * 100
                if actual_move >= move_pct:
                    expired_reason = f"price moved {actual_move:.2f}% from reference {reference_price}"
        if expired_reason:
            payload.update({
                "status": "expired",
                "expired": True,
                "reason": expired_reason,
                "filter_effect": "expired_direction_filter_disabled",
                "operator_message": "口述方向已失效；系统不再按这条观点过滤多空信号，只把它保留为历史判断。",
            })
        return payload

    def _market_view_checked_at(self, latest: dict, latest_quote: dict) -> datetime:
        for value in (latest_quote.get("timestamp"), latest.get("timestamp")):
            parsed = self._parse_dt(value)
            if parsed is not None:
                return parsed.replace(microsecond=0)
        return datetime.now(timezone.utc).replace(microsecond=0)

    def _market_view_price(self, latest: dict, latest_quote: dict) -> float | None:
        for row in (latest_quote, latest):
            for key in ("close", "price"):
                value = self._safe_float(row.get(key))
                if value is not None:
                    return value
        return None

    def _dashboard_live_reconciliation(self, trading_plan: dict, legacy: dict) -> dict:
        scoped_strategy_id = self._strategy_id_from_output_root()
        if scoped_strategy_id:
            return self._decorate_live_reconciliation(
                legacy,
                truth_scope="strategy_scoped",
                strategy_id=scoped_strategy_id,
                path=self.output_root / "live_reconciliation" / "current.json",
            )

        active_strategy_id = (
            trading_plan.get("active_strategy_id")
            or (trading_plan.get("strategy_runtime") or {}).get("active_strategy_id")
            or (self.config.get("demo_trading") or {}).get("active_strategy_id")
            or ""
        )
        if active_strategy_id:
            path = self.output_root / "strategies" / str(active_strategy_id) / "live_reconciliation" / "current.json"
            rows = load_json(path)
            active = rows[-1] if rows and isinstance(rows[-1], dict) else {}
            if active:
                return self._decorate_live_reconciliation(
                    active,
                    truth_scope="active_demo_strategy",
                    strategy_id=str(active_strategy_id),
                    path=path,
                )

        return self._decorate_live_reconciliation(
            legacy,
            truth_scope="legacy_global",
            strategy_id="",
            path=self.output_root / "live_reconciliation" / "current.json",
        )

    def _decorate_live_reconciliation(self, reconciliation: dict, truth_scope: str, strategy_id: str, path: Path) -> dict:
        if not isinstance(reconciliation, dict) or not reconciliation:
            return {}
        return {
            **reconciliation,
            "truth_scope": truth_scope,
            "strategy_id": strategy_id,
            "artifact": str(path),
        }

    def _load_primary_bars(self, run_date: str, strategy_id: str, strategy_config: dict) -> tuple[list[dict], str]:
        configured = "5m"
        if strategy_id:
            configured = str((strategy_config.get(strategy_id, {}) or {}).get("timeframe", "5m"))
        candidates = [configured, "5m", "1m", "15m", "1h", "4h", "1d"]
        seen: set[str] = set()
        for timeframe in candidates:
            if timeframe in seen:
                continue
            seen.add(timeframe)
            rows = load_json(self.output_root / "clean_bars" / run_date / f"GOLD_{timeframe}.json")
            if rows:
                return rows, timeframe
        return [], configured

    def _gold_nav_bars_for_strategy_window(
        self,
        *,
        run_date: str,
        bars: list[dict],
        strategy_rows: list[dict],
    ) -> list[dict]:
        start_ts = None
        for row in strategy_rows:
            for point in row.get("nav_points") or []:
                ts = self._parse_ts(point.get("timestamp") if isinstance(point, dict) else None)
                if ts is not None and (start_ts is None or ts < start_ts):
                    start_ts = ts
        if start_ts is None:
            return bars

        merged: dict[str, dict] = {}
        clean_root = self.output_root / "clean_bars"
        if clean_root.exists():
            for path in sorted(clean_root.glob("*/GOLD_5m.json")):
                if path.parent.name > run_date:
                    continue
                rows = load_json(path)
                if not isinstance(rows, list):
                    continue
                for item in rows:
                    if not isinstance(item, dict):
                        continue
                    timestamp = item.get("timestamp")
                    if not timestamp:
                        continue
                    ts = self._parse_ts(timestamp)
                    if ts is None or ts < start_ts:
                        continue
                    merged[str(timestamp)] = item

        if not merged:
            return bars
        return sorted(merged.values(), key=lambda item: self._parse_ts(item.get("timestamp")) or 0)

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

    def _gold_nav_from_bars(self, bars: list[dict]) -> dict:
        points = []
        first_close = None
        for item in bars:
            if not isinstance(item, dict) or not item.get("timestamp") or item.get("close") is None:
                continue
            close = float(item.get("close") or 0)
            if close <= 0:
                continue
            if first_close is None:
                first_close = close
            points.append({
                "timestamp": item["timestamp"],
                "close": round(close, 4),
                "nav": round((close / first_close) * 100, 4) if first_close else 100.0,
            })
        current = points[-1] if points else {}
        return {
            "baseline": "gold_buy_and_hold",
            "point_count": len(points),
            "start_close": round(first_close, 4) if first_close is not None else None,
            "current_close": current.get("close"),
            "return_pct": round(current.get("nav", 100) - 100, 4) if points else None,
            "points": points,
        }

    def _nav_quality(
        self,
        points: list,
        bars: list[dict],
        *,
        closed_trade_count: int = 0,
        open_trade_count: int = 0,
    ) -> dict:
        nav_points = self._clean_nav_quality_points(points)
        gold_times = sorted(
            ts
            for ts in (self._parse_ts(item.get("timestamp")) for item in bars if isinstance(item, dict))
            if ts is not None
        )
        overlap_points = nav_points
        clipped_to_gold_window = False
        if gold_times and nav_points:
            start = gold_times[0]
            end = gold_times[-1]
            overlap_points = [item for item in nav_points if start <= item["ts"] <= end]
            clipped_to_gold_window = len(overlap_points) != len(nav_points)
        gaps = [
            nav_points[index]["ts"] - nav_points[index - 1]["ts"]
            for index in range(1, len(nav_points))
            if nav_points[index]["ts"] > nav_points[index - 1]["ts"]
        ]
        median_gap_minutes = None
        cadence = "missing"
        if len(nav_points) == 1:
            cadence = "single_checkpoint"
        elif gaps:
            sorted_gaps = sorted(gaps)
            median_gap_minutes = round(sorted_gaps[len(sorted_gaps) // 2] / 60, 2)
            if median_gap_minutes >= 18 * 60:
                cadence = "daily_checkpoint"
            elif median_gap_minutes >= 45:
                cadence = "sparse_intraday_checkpoint"
            else:
                cadence = "intraday_checkpoint"

        overlap_count = len(overlap_points)
        point_count = len(nav_points)
        can_compare = overlap_count >= 2
        trend_ready = overlap_count >= 4
        if point_count == 0:
            status = "missing_nav"
            tone = "warn"
            confidence = "missing"
            reason = "missing_equity_curve"
        elif not can_compare:
            status = "not_comparable"
            tone = "warn"
            confidence = "low"
            reason = "needs_two_overlap_checkpoints"
        elif not trend_ready:
            status = "low_confidence"
            tone = "warn"
            confidence = "low"
            reason = "fewer_than_four_overlap_checkpoints"
        elif open_trade_count > 0 and closed_trade_count < 1:
            status = "open_pnl_driven"
            tone = "warn"
            confidence = "medium"
            reason = "ranking_depends_on_unrealized_pnl"
        else:
            status = "comparable"
            tone = "ok"
            confidence = "medium" if cadence != "intraday_checkpoint" else "high"
            reason = "same_window_edge_ready"

        return {
            "source": "paper_equity_curve_checkpoints",
            "is_intraday_curve": cadence == "intraday_checkpoint",
            "cadence": cadence,
            "median_gap_minutes": median_gap_minutes,
            "point_count": point_count,
            "overlap_point_count": overlap_count,
            "gold_point_count": len(gold_times),
            "clipped_to_gold_window": clipped_to_gold_window,
            "can_compare": can_compare,
            "trend_ready": trend_ready,
            "recommended_view": "edge_checkpoints",
            "status": status,
            "tone": tone,
            "confidence": confidence,
            "reason": reason,
            "window_start": self._format_epoch(overlap_points[0]["ts"]) if overlap_points else "",
            "window_end": self._format_epoch(overlap_points[-1]["ts"]) if overlap_points else "",
        }

    def _nav_quality_summary(self, strategy_rows: list[dict], bars: list[dict]) -> dict:
        qualities = [row.get("nav_quality", {}) for row in strategy_rows if isinstance(row, dict)]
        gold_times = sorted(
            ts
            for ts in (self._parse_ts(item.get("timestamp")) for item in bars if isinstance(item, dict))
            if ts is not None
        )
        gold_gap_minutes = None
        if len(gold_times) >= 2:
            gaps = [gold_times[index] - gold_times[index - 1] for index in range(1, len(gold_times)) if gold_times[index] > gold_times[index - 1]]
            if gaps:
                sorted_gaps = sorted(gaps)
                gold_gap_minutes = round(sorted_gaps[len(sorted_gaps) // 2] / 60, 2)
        with_nav = [item for item in qualities if item.get("point_count", 0) > 0]
        comparable = [item for item in qualities if item.get("trend_ready")]
        low_confidence = [item for item in qualities if item.get("point_count", 0) > 0 and not item.get("trend_ready")]
        open_pnl_driven = [item for item in qualities if item.get("status") == "open_pnl_driven"]
        return {
            "source": "paper_equity_curve_checkpoints",
            "strategy_count": len(strategy_rows),
            "with_nav_strategy_count": len(with_nav),
            "comparable_strategy_count": len(comparable),
            "low_confidence_strategy_count": len(low_confidence),
            "missing_nav_strategy_count": max(0, len(strategy_rows) - len(with_nav)),
            "open_pnl_driven_strategy_count": len(open_pnl_driven),
            "gold_point_count": len(gold_times),
            "gold_median_gap_minutes": gold_gap_minutes,
            "is_intraday_nav_available": any(item.get("is_intraday_curve") for item in qualities),
            "default_read": "same_window_edge_checkpoints",
            "trader_warning": "strategy_nav_points_are_equity_checkpoints_not_minute_curve",
        }

    def _clean_nav_quality_points(self, points: list) -> list[dict]:
        clean = []
        for item in points or []:
            if not isinstance(item, dict):
                continue
            ts = self._parse_ts(item.get("timestamp"))
            equity = self._safe_float(item.get("equity"))
            if ts is None or equity is None:
                continue
            clean.append({"ts": ts, "timestamp": item.get("timestamp"), "equity": equity})
        return sorted(clean, key=lambda item: item["ts"])

    @staticmethod
    def _format_epoch(value: float) -> str:
        return datetime.fromtimestamp(value, tz=timezone.utc).replace(microsecond=0).isoformat()

    def _replay_bars_for_trades(
        self,
        *,
        strategy_id: str,
        requested_timeframe: str,
        artifact_bars: list[dict],
        trades: list[dict],
    ) -> tuple[list[dict], dict]:
        fallback = self._replay_ohlc_meta(
            strategy_id=strategy_id,
            requested_timeframe=requested_timeframe,
            selected_timeframe=requested_timeframe,
            source="strategy_artifact",
            bars=artifact_bars,
            reason="market_db_unavailable",
            trade_count=len(trades or []),
        )
        if not trades or (not uses_independent_datafeed(self.market_db) and not self.market_db.exists()):
            return artifact_bars, fallback
        bounds = self._replay_time_bounds(trades, artifact_bars)
        if not bounds:
            return artifact_bars, {**fallback, "reason": "no_trade_time_bounds"}
        start_epoch, end_epoch = bounds
        if end_epoch <= start_epoch:
            return artifact_bars, {**fallback, "reason": "invalid_trade_time_bounds"}

        selected_timeframe = self._select_replay_timeframe(requested_timeframe, start_epoch, end_epoch)
        store = market_data_repository(self.market_db)
        rows = store.load_bars_between(
            "GOLD",
            selected_timeframe,
            self._format_epoch(start_epoch),
            self._format_epoch(end_epoch),
        )
        if not rows and selected_timeframe != requested_timeframe:
            rows = store.load_bars_between(
                "GOLD",
                requested_timeframe,
                self._format_epoch(start_epoch),
                self._format_epoch(end_epoch),
            )
            if rows:
                selected_timeframe = requested_timeframe
        if not rows:
            return artifact_bars, {**fallback, "reason": "market_db_window_empty"}
        replay_bars = [bar.to_dict() for bar in rows]
        return replay_bars, self._replay_ohlc_meta(
            strategy_id=strategy_id,
            requested_timeframe=requested_timeframe,
            selected_timeframe=selected_timeframe,
            source="market_data_db",
            bars=replay_bars,
            reason="covers_trade_replay_window",
            trade_count=len(trades or []),
            start_epoch=start_epoch,
            end_epoch=end_epoch,
        )

    def _replay_time_bounds(self, trades: list[dict], artifact_bars: list[dict]) -> tuple[float, float] | None:
        times: list[float] = []
        for trade in trades or []:
            for key in ("opened_at", "closed_at"):
                parsed = self._parse_ts(trade.get(key))
                if parsed is not None:
                    times.append(parsed)
        if not times:
            return None
        latest_bar_ts = None
        for bar in artifact_bars or []:
            parsed = self._parse_ts(bar.get("timestamp") if isinstance(bar, dict) else None)
            if parsed is not None:
                latest_bar_ts = parsed if latest_bar_ts is None else max(latest_bar_ts, parsed)
        open_trades = [
            trade
            for trade in trades or []
            if trade.get("opened_at") and not trade.get("closed_at") and str(trade.get("status", "")).lower() != "closed"
        ]
        if open_trades and latest_bar_ts is not None:
            times.append(latest_bar_ts)
        start = max(0.0, min(times) - self._REPLAY_OHLC_PAD_SECONDS)
        end = max(times) + self._REPLAY_OHLC_PAD_SECONDS
        if latest_bar_ts is not None:
            end = min(end, latest_bar_ts)
        return start, end

    def _select_replay_timeframe(self, requested_timeframe: str, start_epoch: float, end_epoch: float) -> str:
        requested = requested_timeframe or "5m"
        requested_seconds = self._timeframe_seconds(requested)
        estimated = (end_epoch - start_epoch) / requested_seconds if requested_seconds else self._REPLAY_OHLC_MAX_BARS + 1
        if estimated <= self._REPLAY_OHLC_MAX_BARS:
            return requested
        if requested != "5m":
            five_min_estimated = (end_epoch - start_epoch) / self._timeframe_seconds("5m")
            if five_min_estimated <= max(self._REPLAY_OHLC_MAX_BARS, 12_000):
                return "5m"
        return requested

    @staticmethod
    def _timeframe_seconds(timeframe: str) -> int:
        units = {"m": 60, "h": 3600, "d": 86400}
        text = str(timeframe or "5m").strip().lower()
        match = re.match(r"^(\d+)([mhd])$", text)
        if not match:
            return 300
        return max(1, int(match.group(1))) * units[match.group(2)]

    def _replay_ohlc_meta(
        self,
        *,
        strategy_id: str,
        requested_timeframe: str,
        selected_timeframe: str,
        source: str,
        bars: list[dict],
        reason: str,
        trade_count: int,
        start_epoch: float | None = None,
        end_epoch: float | None = None,
    ) -> dict:
        first = bars[0].get("timestamp") if bars and isinstance(bars[0], dict) else ""
        last = bars[-1].get("timestamp") if bars and isinstance(bars[-1], dict) else ""
        providers = sorted({
            str(item.get("provider"))
            for item in bars or []
            if isinstance(item, dict) and item.get("provider")
        })
        return {
            "strategy_id": strategy_id,
            "source": source,
            "reason": reason,
            "requested_timeframe": requested_timeframe,
            "timeframe": selected_timeframe,
            "bar_count": len(bars or []),
            "first_timestamp": first,
            "last_timestamp": last,
            "window_start": self._format_epoch(start_epoch) if start_epoch is not None else first,
            "window_end": self._format_epoch(end_epoch) if end_epoch is not None else last,
            "trade_count": trade_count,
            "providers": providers,
            "max_bar_budget": self._REPLAY_OHLC_MAX_BARS,
            "is_complete_replay_window": source == "market_data_db" and bool(bars),
        }

    def _ohlc_quality(
        self,
        run_date: str,
        bars: list[dict],
        data_trust: dict,
        official_feed_receipt: dict,
        data_source_preflight: dict,
        market_db: dict,
        oanda_feed: dict,
        broker_feed_doctor: dict,
    ) -> dict:
        source_config = self.config.get("market_data_sources", {}).get("gold_5m", {})
        official_providers = set(source_config.get("official_broker_providers", ["broker_csv", "mt5_csv", "ibkr", "oanda"]))
        public_providers = set(source_config.get("public_providers", ["gold-api.com", "yahoo_chart:GC=F"]))
        execution_venue_providers = set(source_config.get("execution_venue_providers", ["binance_usdm"]))
        if not bars:
            return {
                "status": "warn",
                "tone": "warn",
                "provider": "",
                "truth_level": "missing",
                "trust_label": "No OHLC / 无K线",
                "action": "Load GOLD OHLC bars before replay or review. / 先加载黄金K线再回放或复盘。",
                "replay_ready": False,
                "promotion_ready": False,
                "bar_count": 0,
                "wide_bar_count": 0,
                "official_rows": self._safe_int(official_feed_receipt.get("official_rows") or data_source_preflight.get("official_rows")),
                "stale_artifacts": self._stale_quality_artifacts(run_date, data_trust, official_feed_receipt),
            }

        latest = bars[-1] if isinstance(bars[-1], dict) else {}
        provider_counts: dict[str, int] = {}
        flags: set[str] = set()
        wide_bars = []
        for item in bars:
            if not isinstance(item, dict):
                continue
            provider = str(item.get("provider") or "unknown")
            provider_counts[provider] = provider_counts.get(provider, 0) + 1
            for flag in item.get("quality_flags") or []:
                flags.add(str(flag))
            high = self._safe_float(item.get("high"))
            low = self._safe_float(item.get("low"))
            close = self._safe_float(item.get("close"))
            if high is not None and low is not None and close and close > 0 and (high - low) / close > 0.01:
                wide_bars.append({
                    "timestamp": item.get("timestamp", ""),
                    "range_pct": round(((high - low) / close) * 100, 4),
                    "open": item.get("open"),
                    "high": item.get("high"),
                    "low": item.get("low"),
                    "close": item.get("close"),
                })
        provider = str(latest.get("provider") or (max(provider_counts, key=provider_counts.get) if provider_counts else "unknown"))
        official_rows = self._safe_int(
            official_feed_receipt.get("official_rows")
            or data_source_preflight.get("official_rows")
            or market_db.get("gold_5m_official_rows")
        )
        execution_venue_rows = self._safe_int(market_db.get("gold_5m_imported_rows")) - self._safe_int(market_db.get("gold_5m_official_rows"))
        execution_venue_source = provider in execution_venue_providers or official_feed_receipt.get("truth_level") == "execution_venue"
        proxy = not execution_venue_source and (provider in public_providers or "public_proxy_feed" in flags)
        if official_rows > 0 and provider in official_providers:
            truth_level = "official_broker"
        elif execution_venue_source:
            truth_level = "execution_venue"
        elif proxy:
            truth_level = "public_proxy"
        elif provider:
            truth_level = "unknown"
        else:
            truth_level = "missing"
        stale_artifacts = self._stale_quality_artifacts(run_date, data_trust, official_feed_receipt)
        replay_ready = bool(bars)
        execution_grade_ready = truth_level in {"official_broker", "execution_venue"}
        promotion_ready = execution_grade_ready and not stale_artifacts
        issues = []
        if proxy:
            issues.append("proxy feed")
        if not execution_grade_ready:
            issues.append("no execution-grade rows")
        if stale_artifacts:
            issues.append("stale quality artifacts")
        if promotion_ready:
            trust_label = "Tradable OHLC / 可交易K线"
            action = "OK for replay, review, and strategy checks. / 可用于回放、复盘和策略检查。"
        elif replay_ready:
            if execution_grade_ready:
                reason_zh = []
                reason_en = []
                if stale_artifacts:
                    reason_zh.append("质量检查过期")
                    reason_en.append("stale quality checks")
                trust_label = f"Execution venue feed; {' + '.join(reason_en) or 'review checks'} / 可执行场所行情；{' + '.join(reason_zh) or '检查未完成'}"
                action = "Binance USDM/execution venue feed is connected; review the listed candle issues before changing exposure or strategy status. / Binance USDM/可执行场所行情已接入；调整仓位或策略状态前，先处理列出的K线问题。"
            else:
                trust_label = "Replay only / 仅用于回放"
                action = "Connect an execution-grade feed before trading decisions. / 交易决策前需接入可执行行情源。"
        else:
            trust_label = "No OHLC / 无K线"
            action = "Load GOLD OHLC bars before replay or review. / 先加载黄金K线再回放或复盘。"
        return {
            "status": "ok" if promotion_ready else "warn",
            "tone": "ok" if promotion_ready else "warn",
            "provider": provider,
            "provider_counts": provider_counts,
            "truth_level": truth_level,
            "trust_label": trust_label,
            "action": action,
            "replay_ready": replay_ready,
            "promotion_ready": promotion_ready,
            "bar_count": len(bars),
            "latest_timestamp": latest.get("timestamp", ""),
            "latest_close": latest.get("close"),
            "quality_flags": sorted(flags),
            "proxy_feed": proxy,
            "wide_bar_count": len(wide_bars),
            "wide_bar_threshold_pct": 1.0,
            "wide_bar_examples": wide_bars[:5],
            "official_rows": official_rows,
            "execution_venue_rows": max(0, execution_venue_rows),
            "execution_grade_ready": execution_grade_ready,
            "oanda_ready": bool(oanda_feed.get("ready")),
            "oanda_missing_env": oanda_feed.get("missing_env", []),
            "broker_csv_valid_files": broker_feed_doctor.get("valid_file_count", 0),
            "stale_artifacts": stale_artifacts,
            "issues": issues,
        }

    @staticmethod
    def _safe_float(value: object) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _stale_quality_artifacts(run_date: str, data_trust: dict, official_feed_receipt: dict) -> list[dict]:
        stale = []
        for name, payload in (("data_trust", data_trust), ("official_feed_receipt", official_feed_receipt)):
            if not isinstance(payload, dict) or not payload:
                continue
            artifact_date = payload.get("run_date")
            if artifact_date and artifact_date != run_date:
                stale.append({"name": name, "run_date": artifact_date, "expected_run_date": run_date})
        return stale

    def _market_data_gate(
        self,
        run_date: str,
        ohlc_quality: dict,
        official_feed_receipt: dict,
        data_source_preflight: dict,
        data_provenance: dict,
        oanda_feed: dict,
        broker_feed_doctor: dict,
        broker_feed: dict,
    ) -> dict:
        official_rows = self._safe_int(ohlc_quality.get("official_rows") or official_feed_receipt.get("official_rows") or data_source_preflight.get("official_rows"))
        execution_venue_rows = self._safe_int(
            ohlc_quality.get("execution_venue_rows")
            or data_source_preflight.get("execution_venue_rows")
            or ((data_provenance.get("provider_groups") or {}).get("execution_venue") or {}).get("rows")
        )
        wide_bar_count = self._safe_int(ohlc_quality.get("wide_bar_count"))
        broker_valid_files = self._safe_int(broker_feed_doctor.get("valid_file_count"))
        broker_imported_rows = self._safe_int(broker_feed.get("imported_rows") or official_feed_receipt.get("broker_csv", {}).get("imported_rows"))
        oanda_missing = oanda_feed.get("missing_env") or official_feed_receipt.get("oanda_feed", {}).get("missing_env") or []
        stale_artifacts = ohlc_quality.get("stale_artifacts") or []
        execution_grade_ready = bool(ohlc_quality.get("execution_grade_ready")) or official_rows > 0 or execution_venue_rows > 0
        promotion_ready = bool(ohlc_quality.get("promotion_ready"))
        replay_ready = bool(ohlc_quality.get("replay_ready"))
        blockers = []
        if not execution_grade_ready:
            blockers.append("no_execution_grade_ohlc")
        if stale_artifacts:
            blockers.append("stale_quality_artifacts")
        if promotion_ready:
            mode = "promotion_ready"
            trader_label = "可交易行情 / Tradable feed"
            trader_summary = "Gold OHLC is from an execution-grade venue and clean enough for strategy review. / 黄金K线来自可执行行情源，质量足够用于策略复核。"
            trader_action = "Use Trade Replay and closed-trade evidence for strategy review. / 可结合交易回放和平仓样本做策略复核。"
        elif replay_ready:
            mode = "quality_review" if execution_grade_ready else "replay_only"
            if execution_grade_ready:
                reason_zh = []
                reason_en = []
                if stale_artifacts:
                    reason_zh.append("质量检查过期")
                    reason_en.append("stale quality checks")
                trader_label = f"{' + '.join(reason_zh) or '行情检查未完成'} / {' + '.join(reason_en) or 'market-data checks incomplete'}"
                trader_summary = f"Binance USDM/execution venue feed is connected; entries are paused because {' and '.join(reason_en) or 'quality checks are incomplete'}. / Binance USDM/可执行场所行情已接入；暂停新增是因为{'，'.join(reason_zh) or '行情检查未完成'}。"
                trader_action = "Refresh stale quality checks before changing exposure or strategy status. / 调整仓位或策略状态前，先刷新过期质量检查。"
            else:
                trader_label = "仅可回放 / Replay only"
                trader_summary = "Gold OHLC is useful for visual replay, but not execution-grade. / 当前黄金K线可用于视觉回放，但不是可执行行情源。"
                trader_action = "Connect Binance USDM or another execution-grade GOLD feed before trading decisions. / 交易决策前需接入 Binance USDM 或其他可执行黄金行情源。"
        else:
            mode = "missing"
            trader_label = "无K线 / No OHLC"
            trader_summary = "Gold OHLC is missing for this replay. / 当前回放缺少黄金K线。"
            trader_action = "Refresh market data before reading strategy entries and exits. / 先刷新行情再解读策略进出场。"
        ops_actions = list(official_feed_receipt.get("next_actions") or [])
        if not ops_actions:
            if oanda_missing:
                ops_actions.append("Fill OANDA_API_TOKEN and OANDA_ACCOUNT_ID in configs/live.env, then run import_official_feed.")
            if broker_valid_files <= 0:
                ops_actions.append("Put a valid XAUUSD 5m MT5/broker CSV into data/broker_feeds/gold_5m, then run import_official_feed.")
            if not execution_grade_ready:
                ops_actions.append("Connect Binance USDM or another execution-grade GOLD feed before trading decisions.")
        return {
            "run_date": run_date,
            "status": "ok" if promotion_ready else "warn",
            "mode": mode,
            "trader_label": trader_label,
            "trader_summary": trader_summary,
            "trader_action": trader_action,
            "promotion_ready": promotion_ready,
            "replay_ready": replay_ready,
            "provider": ohlc_quality.get("provider", ""),
            "truth_level": ohlc_quality.get("truth_level", ""),
            "official_rows": official_rows,
            "execution_venue_rows": execution_venue_rows,
            "execution_grade_ready": execution_grade_ready,
            "bar_count": self._safe_int(ohlc_quality.get("bar_count")),
            "wide_bar_count": wide_bar_count,
            "wide_bar_threshold_pct": ohlc_quality.get("wide_bar_threshold_pct", 1.0),
            "proxy_feed": bool(ohlc_quality.get("proxy_feed")),
            "quality_flags": ohlc_quality.get("quality_flags", []),
            "stale_artifacts": stale_artifacts,
            "blockers": blockers,
            "oanda": {
                "status": oanda_feed.get("status") or official_feed_receipt.get("oanda_feed", {}).get("status"),
                "ready": bool(oanda_feed.get("ready") or official_feed_receipt.get("oanda_feed", {}).get("ready")),
                "missing_env": oanda_missing,
                "imported_rows": self._safe_int(oanda_feed.get("imported_rows") or official_feed_receipt.get("oanda_feed", {}).get("imported_rows")),
                "instrument": oanda_feed.get("instrument") or official_feed_receipt.get("oanda_feed", {}).get("instrument", "XAU_USD"),
            },
            "broker_csv": {
                "doctor_status": broker_feed_doctor.get("status") or official_feed_receipt.get("broker_csv", {}).get("doctor_status"),
                "input_dir": broker_feed_doctor.get("input_dir") or broker_feed.get("input_dir") or official_feed_receipt.get("broker_csv", {}).get("input_dir", ""),
                "valid_file_count": broker_valid_files,
                "file_count": self._safe_int(broker_feed_doctor.get("file_count") or official_feed_receipt.get("broker_csv", {}).get("file_count")),
                "row_count": self._safe_int(broker_feed_doctor.get("row_count") or official_feed_receipt.get("broker_csv", {}).get("row_count")),
                "imported_rows": broker_imported_rows,
                "latest_timestamp": broker_feed_doctor.get("latest_timestamp") or official_feed_receipt.get("broker_csv", {}).get("latest_timestamp", ""),
            },
            "data_source": {
                "ready_for_paper": bool(data_source_preflight.get("ready_for_paper")),
                "ready_for_live": bool(data_source_preflight.get("ready_for_live")),
                "provenance_mode": data_provenance.get("mode", ""),
                "allows_live": bool(data_provenance.get("allows_live")),
                "latest_provider": data_source_preflight.get("latest_provider") or data_provenance.get("latest_provider", ""),
                "latest_timestamp": data_source_preflight.get("latest_timestamp", ""),
            },
            "ops_actions": ops_actions,
            "commands": [
                "python3 -m pipelines.broker_feed_doctor --date " + run_date,
                "python3 -m pipelines.import_official_feed --date " + run_date,
                "python3 -m pipelines.data_trust --date " + run_date,
            ],
        }

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

    def _strategy_realized_evidence(
        self,
        *,
        namespace: Path,
        source: dict,
        closed_trade_count: int,
        open_trade_count: int,
    ) -> dict:
        min_closed_trades = 5
        closed_trades = self._load_closed_trades_from_namespace(namespace)
        closed_count = len(closed_trades) if closed_trades else self._safe_int(closed_trade_count)
        open_count = self._safe_int(open_trade_count)
        realized_values = [self._safe_float(item.get("realized_pnl")) for item in closed_trades]
        realized_values = [value for value in realized_values if value is not None]
        realized_pnl = round(sum(realized_values), 4) if realized_values else (0.0 if closed_count == 0 else None)
        wins = sum(1 for value in realized_values if value > 0)
        losses = sum(1 for value in realized_values if value < 0)
        breakeven = max(0, closed_count - wins - losses)
        win_rate = round(wins / closed_count, 4) if closed_count else 0.0
        avg_pnl = round(realized_pnl / closed_count, 4) if realized_pnl is not None and closed_count else None
        gross_profit = sum(value for value in realized_values if value > 0)
        gross_loss = abs(sum(value for value in realized_values if value < 0))
        profit_factor = round(gross_profit / gross_loss, 4) if gross_loss else None
        net_marked = self._safe_float(source.get("net_pnl"))
        open_marked_pnl = None
        if net_marked is not None and realized_pnl is not None:
            open_marked_pnl = round(net_marked - realized_pnl, 4)
        elif open_count > 0 and net_marked is not None:
            open_marked_pnl = round(net_marked, 4)

        if closed_count < 1 and open_count > 0:
            status = "open_pnl_only"
            tone = "warn"
            label = "open PnL only"
            action = "wait_for_close"
            reason = "Open trades exist, but no closed outcome has validated the strategy yet."
        elif closed_count < 1:
            status = "waiting_for_closed"
            tone = "warn"
            label = "waiting for closed trades"
            action = "collect_samples"
            reason = "No closed trade evidence is available yet."
        elif closed_count < min_closed_trades:
            if realized_pnl is not None and realized_pnl > 0:
                status = "thin_realized_profit"
                tone = "warn"
                label = "thin realized profit"
                action = "collect_more_closed"
                reason = f"Closed PnL is positive, but only {closed_count}/{min_closed_trades} closed trades are available."
            else:
                status = "thin_realized_loss"
                tone = "bad"
                label = "thin realized loss"
                action = "review_or_pause"
                reason = f"Closed PnL is negative with only {closed_count}/{min_closed_trades} closed trades."
        elif realized_pnl is not None and realized_pnl > 0:
            status = "realized_profitable"
            tone = "ok"
            label = "realized profitable"
            action = "review_for_promotion"
            reason = "Closed trade sample is above the minimum threshold and realized PnL is positive."
        elif realized_pnl is not None and realized_pnl < 0:
            status = "realized_losing"
            tone = "bad"
            label = "realized losing"
            action = "diagnose_or_pause"
            reason = "Closed trade sample is above the minimum threshold and realized PnL is negative."
        else:
            status = "realized_flat"
            tone = "warn"
            label = "realized flat"
            action = "collect_more_closed"
            reason = "Closed trades are available, but realized PnL is flat."

        return {
            "status": status,
            "tone": tone,
            "label": label,
            "reason": reason,
            "trader_action": action,
            "closed_trade_count": closed_count,
            "open_trade_count": open_count,
            "min_closed_trades": min_closed_trades,
            "wins": wins,
            "losses": losses,
            "breakeven": breakeven,
            "win_rate": win_rate,
            "realized_pnl": realized_pnl,
            "avg_realized_pnl": avg_pnl,
            "profit_factor": profit_factor,
            "net_marked_pnl": round(net_marked, 4) if net_marked is not None else None,
            "open_marked_pnl": open_marked_pnl,
            "uses_open_pnl": open_count > 0,
            "closed_trade_ids": [item.get("trade_id", "") for item in closed_trades[:8]],
        }

    def _realized_evidence_summary(self, strategy_rows: list[dict]) -> dict:
        rows = [row for row in strategy_rows if isinstance(row, dict)]
        evidences = [row.get("realized_evidence", {}) for row in rows]
        with_closed = [item for item in evidences if self._safe_int(item.get("closed_trade_count")) > 0]
        open_only = [item for item in evidences if item.get("status") == "open_pnl_only"]
        profitable = [item for item in evidences if item.get("status") == "realized_profitable"]
        losing = [item for item in evidences if item.get("status") == "realized_losing"]
        thin_profit = [item for item in evidences if item.get("status") == "thin_realized_profit"]
        thin_loss = [item for item in evidences if item.get("status") == "thin_realized_loss"]
        total_realized = sum(
            float(item.get("realized_pnl") or 0)
            for item in evidences
            if item.get("realized_pnl") is not None
        )
        total_open_marked = sum(
            float(item.get("open_marked_pnl") or 0)
            for item in evidences
            if item.get("open_marked_pnl") is not None
        )

        def _best_row(candidates: list[dict], reverse: bool) -> dict:
            keyed = []
            for row in rows:
                evidence = row.get("realized_evidence", {})
                pnl = self._safe_float(evidence.get("realized_pnl"))
                if evidence in candidates and pnl is not None:
                    keyed.append((pnl, row))
            if not keyed:
                return {}
            return sorted(keyed, key=lambda item: item[0], reverse=reverse)[0][1]

        best = _best_row(with_closed, True)
        worst = _best_row(with_closed, False)
        return {
            "strategy_count": len(rows),
            "with_closed_trade_strategy_count": len(with_closed),
            "open_pnl_only_strategy_count": len(open_only),
            "realized_profitable_strategy_count": len(profitable),
            "realized_losing_strategy_count": len(losing),
            "thin_realized_profit_strategy_count": len(thin_profit),
            "thin_realized_loss_strategy_count": len(thin_loss),
            "total_realized_pnl": round(total_realized, 4),
            "total_open_marked_pnl": round(total_open_marked, 4),
            "best_realized_strategy_id": best.get("strategy_id", "") if best else "",
            "best_realized_pnl": best.get("realized_evidence", {}).get("realized_pnl") if best else None,
            "worst_realized_strategy_id": worst.get("strategy_id", "") if worst else "",
            "worst_realized_pnl": worst.get("realized_evidence", {}).get("realized_pnl") if worst else None,
            "trader_warning": "separate_realized_pnl_from_open_marked_pnl",
        }

    def _trade_evidence_provenance(self, trades: list[dict]) -> dict:
        rows = [trade for trade in trades if isinstance(trade, dict)]
        repaired = []
        original = []
        missing = []
        for trade in rows:
            signal = trade.get("strategy_signal") if isinstance(trade.get("strategy_signal"), dict) else {}
            provenance = signal.get("artifact_provenance") if isinstance(signal.get("artifact_provenance"), dict) else {}
            if provenance.get("status") == "repaired_from_paper_trade":
                repaired.append(trade)
            elif signal:
                original.append(trade)
            else:
                missing.append(trade)
        return {
            "status": "has_repaired_trace" if repaired else ("missing_trace" if missing else "original_trace"),
            "trade_count": len(rows),
            "original_trace_trade_count": len(original),
            "repaired_trade_count": len(repaired),
            "missing_trace_trade_count": len(missing),
            "repaired_trade_ids": [item.get("trade_id", "") for item in repaired[:8]],
            "missing_trade_ids": [item.get("trade_id", "") for item in missing[:8]],
            "trader_note": (
                "repaired_trace_is_reviewable_but_not_original_signal_evidence"
                if repaired
                else ("missing_trace_blocks_promotion" if missing else "all_trace_evidence_original")
            ),
        }

    def _board_evidence_provenance(self, strategy_rows: list[dict]) -> dict:
        repaired_strategy_ids = []
        missing_strategy_ids = []
        original = 0
        repaired = 0
        missing = 0
        total = 0
        for row in strategy_rows:
            if not isinstance(row, dict):
                continue
            provenance = row.get("closed_loop_audit", {}).get("evidence_provenance", {})
            total += self._safe_int(provenance.get("trade_count"))
            original += self._safe_int(provenance.get("original_trace_trade_count"))
            repaired_count = self._safe_int(provenance.get("repaired_trade_count"))
            missing_count = self._safe_int(provenance.get("missing_trace_trade_count"))
            repaired += repaired_count
            missing += missing_count
            if repaired_count:
                repaired_strategy_ids.append(row.get("strategy_id", ""))
            if missing_count:
                missing_strategy_ids.append(row.get("strategy_id", ""))
        return {
            "trade_count": total,
            "original_trace_trade_count": original,
            "repaired_trade_count": repaired,
            "missing_trace_trade_count": missing,
            "repaired_strategy_ids": [item for item in repaired_strategy_ids if item],
            "missing_strategy_ids": [item for item in missing_strategy_ids if item],
            "trader_warning": "show_repaired_trace_separately_from_original_evidence",
        }

    @staticmethod
    def _load_closed_trades_from_namespace(namespace: Path) -> list[dict]:
        closed_dir = namespace / "paper_trades" / "closed"
        if not closed_dir.exists():
            return []
        out: list[dict] = []
        seen: set[str] = set()
        for path in sorted(closed_dir.glob("*.json")):
            rows = load_json(path)
            for trade in rows if isinstance(rows, list) else []:
                if not isinstance(trade, dict):
                    continue
                tid = str(trade.get("trade_id") or f"{path.name}:{len(out)}")
                if tid in seen:
                    continue
                seen.add(tid)
                out.append(trade)
        return out

    def _safe_int(self, value: object) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def _gap_groups(self, gaps: list[dict], group_by: str = "type") -> list[dict]:
        grouped: dict[str, dict] = {}
        for gap in gaps or []:
            if not isinstance(gap, dict):
                continue
            gap_type = str(gap.get("type") or "unknown_gap")
            root_cause = self._gap_root_cause(gap_type)
            key = root_cause if group_by == "root_cause" else gap_type
            item = grouped.setdefault(
                key,
                {
                    "key": key,
                    "type": gap_type if group_by == "type" else "",
                    "root_cause": root_cause,
                    "label": self._gap_group_label(key),
                    "message": gap.get("message", ""),
                    "severity": "info",
                    "count": 0,
                    "warn_count": 0,
                    "info_count": 0,
                    "gap_types": [],
                    "trade_ids": [],
                    "ticket_ids": [],
                    "signal_ids": [],
                },
            )
            item["count"] += 1
            severity = str(gap.get("severity") or "info")
            if severity == "warn":
                item["warn_count"] += 1
                item["severity"] = "warn"
            else:
                item["info_count"] += 1
            if gap_type not in item["gap_types"]:
                item["gap_types"].append(gap_type)
            for source_key, target_key in (
                ("trade_id", "trade_ids"),
                ("ticket_id", "ticket_ids"),
                ("signal_id", "signal_ids"),
            ):
                value = gap.get(source_key)
                if value and value not in item[target_key]:
                    item[target_key].append(value)

        groups = sorted(
            grouped.values(),
            key=lambda item: (item["warn_count"] == 0, -int(item["count"]), item["key"]),
        )
        for item in groups:
            item["next_action"] = self._gap_group_next_action(item["root_cause"], item["gap_types"])
            item["sample_trade_ids"] = item.pop("trade_ids")[:6]
            item["sample_ticket_ids"] = item.pop("ticket_ids")[:6]
            item["sample_signal_ids"] = item.pop("signal_ids")[:6]
        return groups

    def _board_gap_groups(self, strategy_rows: list[dict]) -> list[dict]:
        grouped: dict[str, dict] = {}
        for row in strategy_rows:
            sid = row.get("strategy_id", "")
            audit = row.get("closed_loop_audit", {}) if isinstance(row, dict) else {}
            root_groups = audit.get("root_cause_groups", []) if isinstance(audit, dict) else []
            for group in root_groups:
                if not isinstance(group, dict):
                    continue
                key = str(group.get("root_cause") or group.get("key") or "unknown_gap")
                item = grouped.setdefault(
                    key,
                    {
                        "key": key,
                        "root_cause": key,
                        "label": self._gap_group_label(key),
                        "severity": "info",
                        "count": 0,
                        "warn_count": 0,
                        "info_count": 0,
                        "gap_types": [],
                        "strategy_ids": [],
                    },
                )
                item["count"] += int(group.get("count") or 0)
                item["warn_count"] += int(group.get("warn_count") or 0)
                item["info_count"] += int(group.get("info_count") or 0)
                if item["warn_count"] > 0:
                    item["severity"] = "warn"
                if sid and sid not in item["strategy_ids"]:
                    item["strategy_ids"].append(sid)
                for gap_type in group.get("gap_types", []):
                    if gap_type and gap_type not in item["gap_types"]:
                        item["gap_types"].append(gap_type)
        groups = sorted(
            grouped.values(),
            key=lambda item: (item["warn_count"] == 0, -int(item["count"]), item["key"]),
        )
        for item in groups:
            item["strategy_count"] = len(item["strategy_ids"])
            item["next_action"] = self._gap_group_next_action(item["root_cause"], item["gap_types"])
        return groups

    def _gap_root_cause(self, gap_type: str) -> str:
        artifact_missing = {
            "missing_signal_artifact",
            "missing_ticket_artifact",
            "missing_order_artifact",
            "executed_decision_without_order_artifact",
        }
        if gap_type in artifact_missing:
            return "artifact_provenance_missing"
        if gap_type in {"filled_order_without_trade", "executed_decision_without_trade"}:
            return "order_trade_reconciliation"
        if gap_type == "missing_gate_context":
            return "risk_gate_snapshot_missing"
        if gap_type == "directional_signal_without_ticket":
            return "signal_ticket_intent_gap"
        if gap_type in {"missing_entry_reason", "missing_exit_reason"}:
            return "trade_reason_missing"
        return gap_type or "unknown_gap"

    def _gap_group_label(self, key: str) -> str:
        return {
            "artifact_provenance_missing": "artifact provenance missing",
            "order_trade_reconciliation": "order/trade reconciliation",
            "risk_gate_snapshot_missing": "risk gate snapshot missing",
            "signal_ticket_intent_gap": "signal without ticket",
            "trade_reason_missing": "trade reason missing",
        }.get(key, key.replace("_", " "))

    def _gap_group_next_action(self, root_cause: str, gap_types: list[str]) -> str:
        if root_cause == "artifact_provenance_missing":
            return "repair_same_day_signal_ticket_order_artifacts"
        if root_cause == "order_trade_reconciliation":
            return "reconcile_order_trade_records"
        if root_cause == "risk_gate_snapshot_missing":
            return "attach_risk_gate_snapshot"
        if root_cause == "signal_ticket_intent_gap":
            return "verify_no_trade_signal_handling"
        if root_cause == "trade_reason_missing":
            return "backfill_entry_exit_reasons"
        return self._closed_loop_next_action("explainability_gap", gap_types)

    def _executed_trade_count_window(self, strategy_id: str, run_date: str, days: int) -> int:
        namespace = self._strategy_namespace(strategy_id)
        try:
            end = datetime.fromisoformat(run_date).date()
        except ValueError:
            return 0
        total = 0
        for offset in range(days):
            day = end - timedelta(days=offset)
            total += self._executed_trade_count_for_namespace(namespace, day.isoformat())
        return total

    def _consecutive_inactive_days(self, strategy_id: str, run_date: str, max_days: int) -> int:
        namespace = self._strategy_namespace(strategy_id)
        try:
            end = datetime.fromisoformat(run_date).date()
        except ValueError:
            return 0
        count = 0
        for offset in range(max_days):
            day = end - timedelta(days=offset)
            if self._executed_trade_count_for_namespace(namespace, day.isoformat()) >= 1:
                break
            count += 1
        return count

    def _executed_trade_count_for_namespace(self, namespace: Path, run_date: str) -> int:
        if not namespace.exists():
            return 0
        execution_ids: set[str] = set()
        decisions = load_json(namespace / "journal_decisions" / f"{run_date}.json")
        for item in decisions:
            if item.get("decision_status") in {"executed", "executed_paper"}:
                ticket_id = str(item.get("ticket_id") or item.get("order_id") or "")
                if ticket_id:
                    execution_ids.add(ticket_id)
        for rel in ("paper_orders", "demo_order_requests", "live_order_requests"):
            rows = load_json(namespace / rel / f"{run_date}.json")
            legacy = rel == "paper_orders"
            execution_ids.update(executed_record_ids(rows, legacy_count_missing_status=legacy))
        return len(execution_ids)

    def _artifact_freshness(self, path: Path) -> dict:
        if not path.exists():
            return {"exists": False, "modified_at": ""}
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).replace(microsecond=0)
        return {"exists": True, "modified_at": modified.isoformat()}

    def _demo_blocker(self, namespace: Path, run_date: str) -> dict:
        reconciliation = self._latest_reconciliation(namespace)
        if reconciliation:
            if reconciliation.get("suspected_naked_position"):
                return {
                    "blocked": True,
                    "status": "suspected_naked_position",
                    "reason": "suspected naked position: " + str(reconciliation.get("escalation_action") or reconciliation.get("reason_code") or ""),
                    "system_state": reconciliation.get("system_state", "BLOCKED_NAKED_POSITION_SUSPECTED"),
                    "reason_code": reconciliation.get("reason_code", "naked_position_suspected"),
                    "artifact": str(namespace / "live_reconciliation" / "current.json"),
                    "reconciliation": reconciliation,
                }
            if reconciliation.get("confirmation_status") == "cannot_confirm":
                return {
                    "blocked": True,
                    "status": "reconciliation_unknown",
                    "reason": f"Binance demo reconciliation cannot confirm venue state: {reconciliation.get('error')}",
                    "system_state": reconciliation.get("system_state", "BLOCKED_RECONCILIATION_UNKNOWN"),
                    "reason_code": reconciliation.get("reason_code", "venue_state_unknown"),
                    "artifact": str(namespace / "live_reconciliation" / "current.json"),
                    "reconciliation": reconciliation,
                }
            if reconciliation.get("error"):
                return {
                    "blocked": True,
                    "status": "reconciliation_error",
                    "reason": f"Binance demo reconciliation failed: {reconciliation.get('error')}",
                    "system_state": reconciliation.get("system_state", "BLOCKED_RECONCILIATION_UNKNOWN"),
                    "reason_code": reconciliation.get("reason_code", "venue_state_unknown"),
                    "artifact": str(namespace / "live_reconciliation" / "current.json"),
                    "reconciliation": reconciliation,
                }
            if int(reconciliation.get("drift_count") or 0) > 0:
                reasons = sorted({str(item.get("reason", "reconciliation drift")) for item in reconciliation.get("drifts", [])})
                return {
                    "blocked": True,
                    "status": "orphan_demo_position",
                    "reason": "Binance demo reconciliation drift: " + ("; ".join(reasons) if reasons else "unknown drift"),
                    "system_state": reconciliation.get("system_state", "BLOCKED_RECONCILIATION_DRIFT"),
                    "reason_code": reconciliation.get("reason_code", "reconciliation_drift"),
                    "artifact": str(namespace / "live_reconciliation" / "current.json"),
                    "reconciliation": reconciliation,
                }

        request = self._latest_demo_request(namespace, run_date)
        if request:
            receipt = request.get("receipt", {}) if isinstance(request.get("receipt"), dict) else {}
            broker_response = request.get("broker_response", {}) if isinstance(request.get("broker_response"), dict) else {}
            protective_status = str(broker_response.get("protective_status") or "")
            if request.get("status") == "blocked":
                guard = request.get("guard", {}) if isinstance(request.get("guard"), dict) else {}
                return {
                    "blocked": True,
                    "status": "demo_order_blocked",
                    "reason": guard.get("block_reason") or request.get("block_reason") or "Binance demo order blocked",
                    "artifact": str(namespace / "demo_order_requests" / f"{run_date}.json"),
                    "request": request,
                }
            if protective_status in {"failed", "partial"} or receipt.get("status") == "protective_order_missing":
                emergency = broker_response.get("emergency_close", {}) if isinstance(broker_response.get("emergency_close"), dict) else {}
                local = emergency.get("local_mirror", {}) if isinstance(emergency.get("local_mirror"), dict) else {}
                if emergency.get("status") == "closed" and local.get("closed") is True:
                    return {"blocked": False, "status": "protective_failure_emergency_closed", "reason": ""}
                return {
                    "blocked": True,
                    "status": "protective_order_missing",
                    "reason": f"Binance demo protective order status is {protective_status or receipt.get('status')}",
                    "artifact": str(namespace / "demo_order_requests" / f"{run_date}.json"),
                    "request": request,
                }
        return {"blocked": False, "status": "clear", "reason": ""}

    def _latest_reconciliation(self, namespace: Path) -> dict:
        rows = load_json(namespace / "live_reconciliation" / "current.json")
        if not rows:
            return {}
        item = rows[-1]
        return item if isinstance(item, dict) else {}

    def _latest_demo_request(self, namespace: Path, run_date: str) -> dict:
        candidates = [
            namespace / "demo_order_requests" / f"{run_date}.json",
            namespace / "demo_order_requests" / "current.json",
        ]
        for path in candidates:
            rows = load_json(path)
            if rows:
                item = rows[-1]
                return item if isinstance(item, dict) else {}
        return {}

    def _trade_trace_artifacts(
        self,
        *,
        artifact_root: Path | None = None,
        run_date: str,
        trades: list[dict],
        signals: list[dict],
        tickets: list[dict],
        decisions: list[dict],
        risk_blocks: list[dict],
        orders: list[dict],
    ) -> dict:
        """Load same-day artifacts plus cross-day rows directly referenced by trades.

        Strategy detail often shows current open trades that were opened on prior
        UTC days. Auditing those trades against only `run_date` artifacts creates
        false missing_signal/missing_ticket gaps. Keep the run-date rows for
        same-day signal diagnostics, then pull only referenced historical rows.
        """
        trade_rows = [item for item in trades if isinstance(item, dict)]
        signal_ids = {item.get("signal_id") for item in trade_rows if item.get("signal_id")}
        ticket_ids = {item.get("ticket_id") for item in trade_rows if item.get("ticket_id")}
        order_ids = {item.get("order_id") for item in trade_rows if item.get("order_id")}
        root = artifact_root or self.output_root
        candidate_dates = {run_date}
        for trade in trade_rows:
            for key in ("opened_at", "closed_at"):
                date_value = self._date_from_timestamp(trade.get(key))
                if date_value:
                    candidate_dates.add(date_value)
            for key in ("signal_id", "ticket_id", "order_id"):
                candidate_dates.update(self._dates_from_identifier(trade.get(key)))

        def matching(path: Path, current: list[dict], predicate) -> list[dict]:
            rows = [item for item in current if isinstance(item, dict)]
            for date_value in sorted(candidate_dates):
                path_for_date = path / f"{date_value}.json"
                if not path_for_date.exists():
                    continue
                loaded = load_json(path_for_date)
                for item in loaded if isinstance(loaded, list) else []:
                    if isinstance(item, dict) and predicate(item, date_value):
                        rows.append(item)
            return self._dedupe_artifact_rows(rows)

        return {
            "signals": matching(
                root / "signals",
                signals if isinstance(signals, list) else [],
                lambda item, date_value: date_value == run_date or item.get("signal_id") in signal_ids,
            ),
            "tickets": matching(
                root / "trade_tickets",
                tickets if isinstance(tickets, list) else [],
                lambda item, date_value: date_value == run_date
                or item.get("ticket_id") in ticket_ids
                or item.get("signal_id") in signal_ids,
            ),
            "decisions": matching(
                root / "journal_decisions",
                decisions if isinstance(decisions, list) else [],
                lambda item, date_value: date_value == run_date
                or item.get("ticket_id") in ticket_ids
                or item.get("signal_id") in signal_ids,
            ),
            "risk_blocks": matching(
                root / "risk_blocks",
                risk_blocks if isinstance(risk_blocks, list) else [],
                lambda item, date_value: date_value == run_date
                or item.get("ticket_id") in ticket_ids
                or item.get("signal_id") in signal_ids,
            ),
            "orders": matching(
                root / "paper_orders",
                orders if isinstance(orders, list) else [],
                lambda item, date_value: date_value == run_date
                or item.get("order_id") in order_ids
                or item.get("ticket_id") in ticket_ids,
            ),
        }

    @classmethod
    def _dates_from_identifier(cls, value: object) -> set[str]:
        text = str(value or "")
        out: set[str] = set()
        for match in cls._DATE_TOKEN_PATTERN.finditer(text):
            year, month, day = match.groups()
            try:
                out.add(datetime(int(year), int(month), int(day), tzinfo=timezone.utc).date().isoformat())
            except ValueError:
                continue
        return out

    @staticmethod
    def _date_from_timestamp(value: object) -> str:
        if not value:
            return ""
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return ""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).date().isoformat()

    @staticmethod
    def _dedupe_artifact_rows(rows: list[dict]) -> list[dict]:
        out: list[dict] = []
        seen: set[tuple] = set()
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            key = (
                row.get("signal_id") or "",
                row.get("ticket_id") or "",
                row.get("order_id") or "",
                row.get("trade_id") or "",
                row.get("generated_at") or row.get("created_at") or row.get("decided_at") or row.get("filled_at") or "",
            )
            if not any(key):
                key = ("row", index, repr(sorted(row.items())))
            if key in seen:
                continue
            seen.add(key)
            out.append(row)
        return out

    def _decorated_trades(
        self,
        open_trades: list[dict],
        closed_trades: list[dict],
        signals: list[dict],
        tickets: list[dict],
        decisions: list[dict],
        risk_blocks: list[dict],
        orders: list[dict],
        paper_exit_decisions: dict,
        risk_monitor: dict,
        review_events: list[dict],
        strategy_id: str = "",
        strategy_config: dict | None = None,
        run_date: str = "",
        latest_price: float | None = None,
    ) -> list[dict]:
        signal_by_id = {item.get("signal_id"): item for item in signals if isinstance(item, dict)}
        ticket_by_id = {item.get("ticket_id"): item for item in tickets if isinstance(item, dict)}
        decision_by_ticket = {item.get("ticket_id"): item for item in decisions if isinstance(item, dict)}
        risk_by_ticket = {item.get("ticket_id"): item for item in risk_blocks if isinstance(item, dict)}
        order_by_id = {item.get("order_id"): item for item in orders if isinstance(item, dict)}
        order_by_ticket = {}
        for item in orders:
            if isinstance(item, dict) and item.get("ticket_id") and item.get("ticket_id") not in order_by_ticket:
                order_by_ticket[item.get("ticket_id")] = item
        exit_items = paper_exit_decisions.get("queue", []) if isinstance(paper_exit_decisions, dict) else []
        exit_by_trade = {item.get("trade_id"): item for item in exit_items if isinstance(item, dict)}
        out = []
        for trade in [*(open_trades or []), *(closed_trades or [])]:
            if not isinstance(trade, dict):
                continue
            ticket = ticket_by_id.get(trade.get("ticket_id"), {})
            signal = signal_by_id.get(trade.get("signal_id"), {})
            decision = decision_by_ticket.get(trade.get("ticket_id"), {})
            risk_block = risk_by_ticket.get(trade.get("ticket_id"), {})
            embedded_order = decision.get("paper_order") if isinstance(decision.get("paper_order"), dict) else {}
            order = order_by_id.get(trade.get("order_id"), {}) or order_by_ticket.get(trade.get("ticket_id"), {}) or embedded_order
            exit_item = exit_by_trade.get(trade.get("trade_id"), {})
            entry_reason = (
                ticket.get("rationale")
                or signal.get("thesis")
                or trade.get("signal_regime")
                or decision.get("notes")
                or ""
            )
            lifecycle = self._trade_lifecycle(
                trade=trade,
                signal=signal,
                ticket=ticket,
                decision=decision,
                risk_block=risk_block,
                order=order,
                exit_item=exit_item,
                risk_monitor=risk_monitor,
                review_events=review_events,
            )
            enriched = {
                **trade,
                "strategy_id": strategy_id or trade.get("strategy_id", ""),
                "entry_reason": entry_reason,
                "exit_reason": trade.get("exit_reason") or exit_item.get("exit_reason", ""),
                "strategy_signal": signal,
                "ticket": ticket,
                "risk_block": risk_block,
                "decision": decision,
                "order": order,
                "exit_decision": exit_item,
                "lifecycle": lifecycle,
            }
            enriched["record_card"] = TradeRecordCardBuilder(
                run_date=run_date or self._date_from_timestamp(trade.get("opened_at")) or "",
                strategy_id=strategy_id,
                strategy_config=strategy_config or {},
                latest_price=latest_price,
            ).build(enriched)
            out.append(enriched)
        return out

    def _trade_lifecycle(
        self,
        *,
        trade: dict,
        signal: dict,
        ticket: dict,
        decision: dict,
        risk_block: dict,
        order: dict,
        exit_item: dict,
        risk_monitor: dict,
        review_events: list[dict],
    ) -> list[dict]:
        ticket_id = str(trade.get("ticket_id") or "")
        signal_id = str(trade.get("signal_id") or "")
        related_review_events = [
            item
            for item in review_events
            if item.get("ticket_id") == ticket_id or item.get("signal_id") == signal_id
        ]
        risk_snapshot = decision.get("risk_snapshot", {}) if isinstance(decision.get("risk_snapshot"), dict) else {}
        if risk_block:
            gate_event = self._lifecycle_event(
                "gate",
                True,
                risk_block.get("status", "block"),
                risk_block.get("generated_at", ""),
                risk_block.get("reason", "risk gate blocked the candidate"),
                "risk_blocks",
            )
        elif risk_snapshot:
            gate_event = self._lifecycle_event(
                "gate",
                True,
                "passed",
                decision.get("decided_at") or decision.get("created_at", ""),
                "max_loss_pct={max_loss_pct} daily_loss_stop_pct={daily_loss_stop_pct}".format(
                    max_loss_pct=risk_snapshot.get("max_loss_pct", "unknown"),
                    daily_loss_stop_pct=risk_snapshot.get("daily_loss_stop_pct", "unknown"),
                ),
                "journal_decisions.risk_snapshot",
            )
        elif risk_monitor:
            gate_event = self._lifecycle_event(
                "gate",
                True,
                risk_monitor.get("status", "unknown"),
                risk_monitor.get("generated_at", ""),
                f"kill_switch={risk_monitor.get('kill_switch_active', 'unknown')}",
                "risk_monitor",
            )
        else:
            gate_event = self._lifecycle_event("gate", False, "missing", "", "no gate snapshot found", "")

        order_summary = "order artifact missing"
        if order:
            order_summary = "fill_price={fill_price} quantity={quantity}".format(
                fill_price=order.get("fill_price", order.get("requested_price", "unknown")),
                quantity=order.get("quantity", "unknown"),
            )
        position_summary = "{side} qty={quantity} entry={entry} TP={target} SL={stop}".format(
            side=trade.get("side", ""),
            quantity=trade.get("quantity", "unknown"),
            entry=trade.get("entry_price", "unknown"),
            target=trade.get("target", "unknown"),
            stop=trade.get("stop_loss", "unknown"),
        )
        closed = bool(trade.get("closed_at") or trade.get("status") == "closed")
        if closed:
            exit_event = self._lifecycle_event(
                "exit",
                True,
                "closed",
                trade.get("closed_at", ""),
                "{reason} pnl={pnl}".format(
                    reason=trade.get("exit_reason") or exit_item.get("exit_reason", "closed"),
                    pnl=trade.get("realized_pnl", "unknown"),
                ),
                "paper_trades.closed",
            )
        elif exit_item:
            exit_event = self._lifecycle_event(
                "exit",
                True,
                exit_item.get("required_user_action") or exit_item.get("exit_type", "review"),
                exit_item.get("latest_timestamp", ""),
                "{exit_type}: {reason}".format(
                    exit_type=exit_item.get("exit_type", "exit_review"),
                    reason=exit_item.get("exit_reason", ""),
                ),
                "paper_exit_decisions",
            )
        else:
            exit_event = self._lifecycle_event("exit", False, "open", "", "open trade; no exit decision artifact", "")

        review_summary = "; ".join(
            f"{item.get('type', 'event')}:{item.get('status', '')}" for item in related_review_events[:4]
        )
        return [
            self._lifecycle_event(
                "signal",
                bool(signal),
                signal.get("status") or ("present" if signal else "missing_artifact"),
                signal.get("generated_at", ""),
                signal.get("thesis") or trade.get("signal_regime") or "signal artifact missing",
                "signals",
            ),
            self._lifecycle_event(
                "ticket",
                bool(ticket),
                ticket.get("status") or ticket.get("verdict") or ("created" if ticket else "missing_artifact"),
                ticket.get("created_at") or ticket.get("generated_at", ""),
                ticket.get("rationale") or ticket.get("trigger") or "ticket artifact missing",
                "trade_tickets",
            ),
            gate_event,
            self._lifecycle_event(
                "order",
                bool(order),
                order.get("status", "missing_artifact") if order else "missing_artifact",
                order.get("filled_at") or order.get("submitted_at") or decision.get("decided_at", ""),
                order_summary,
                "paper_orders",
            ),
            self._lifecycle_event(
                "position",
                True,
                trade.get("status", "open"),
                trade.get("opened_at", ""),
                position_summary,
                "paper_trades",
            ),
            exit_event,
            self._lifecycle_event(
                "review",
                bool(related_review_events),
                f"{len(related_review_events)} events" if related_review_events else "missing",
                related_review_events[-1].get("timestamp", "") if related_review_events else "",
                review_summary or "no related review event",
                "review_events",
            ),
        ]

    def _lifecycle_event(self, stage: str, present: bool, status: str, timestamp: str, summary: str, source: str) -> dict:
        return {
            "stage": stage,
            "present": bool(present),
            "status": status or "",
            "timestamp": timestamp or "",
            "summary": summary or "",
            "source": source or "",
        }

    def _explainability_gaps(
        self,
        trades: list[dict],
        signals: list[dict],
        tickets: list[dict],
        orders: list[dict],
        decisions: list[dict],
    ) -> list[dict]:
        gaps = []
        signal_ids = {item.get("signal_id") for item in signals if isinstance(item, dict)}
        ticket_ids = {item.get("ticket_id") for item in tickets if isinstance(item, dict)}
        trade_order_ids = {item.get("order_id") for item in trades if isinstance(item, dict)}
        trade_ticket_ids = {item.get("ticket_id") for item in trades if isinstance(item, dict)}
        order_ids = {item.get("order_id") for item in orders if isinstance(item, dict)}
        order_ticket_ids = {item.get("ticket_id") for item in orders if isinstance(item, dict)}

        for trade in trades:
            if not isinstance(trade, dict):
                continue
            trade_id = trade.get("trade_id", "")
            if not trade.get("entry_reason"):
                gaps.append(self._gap("missing_entry_reason", "warn", "trade has no entry reason", trade_id, trade.get("ticket_id", ""), trade.get("signal_id", "")))
            if trade.get("signal_id") and trade.get("signal_id") not in signal_ids:
                gaps.append(self._gap("missing_signal_artifact", "warn", "trade references a signal not present in trade trace artifacts", trade_id, trade.get("ticket_id", ""), trade.get("signal_id", "")))
            if trade.get("ticket_id") and trade.get("ticket_id") not in ticket_ids:
                gaps.append(self._gap("missing_ticket_artifact", "warn", "trade references a ticket not present in trade trace artifacts", trade_id, trade.get("ticket_id", ""), trade.get("signal_id", "")))
            if (trade.get("order_id") or trade.get("ticket_id")) and not trade.get("order"):
                gaps.append(self._gap("missing_order_artifact", "warn", "trade has no matching paper order artifact", trade_id, trade.get("ticket_id", ""), trade.get("signal_id", "")))
            if (trade.get("closed_at") or trade.get("status") == "closed") and not trade.get("exit_reason"):
                gaps.append(self._gap("missing_exit_reason", "warn", "closed trade has no exit reason", trade_id, trade.get("ticket_id", ""), trade.get("signal_id", "")))
            gate = next((item for item in trade.get("lifecycle", []) if item.get("stage") == "gate"), {})
            if not gate.get("present"):
                gaps.append(self._gap("missing_gate_context", "info", "trade has no risk gate snapshot", trade_id, trade.get("ticket_id", ""), trade.get("signal_id", "")))

        for order in orders:
            if not isinstance(order, dict):
                continue
            status = str(order.get("status") or "").lower()
            if status not in {"filled", "executed", "submitted"}:
                continue
            if order.get("order_id") not in trade_order_ids and order.get("ticket_id") not in trade_ticket_ids:
                gaps.append(self._gap("filled_order_without_trade", "warn", "filled order has no matching trade record", "", order.get("ticket_id", ""), ""))

        for decision in decisions:
            if not isinstance(decision, dict) or decision.get("decision_status") not in {"executed", "executed_paper"}:
                continue
            embedded = decision.get("paper_order") if isinstance(decision.get("paper_order"), dict) else {}
            decision_order_id = embedded.get("order_id") or decision.get("order_id")
            if decision_order_id and decision_order_id not in order_ids:
                gaps.append(self._gap("executed_decision_without_order_artifact", "warn", "executed decision embeds an order missing from paper_orders", "", decision.get("ticket_id", ""), decision.get("signal_id", "")))
            if decision.get("ticket_id") and decision.get("ticket_id") not in order_ticket_ids and decision.get("ticket_id") not in trade_ticket_ids:
                gaps.append(self._gap("executed_decision_without_trade", "warn", "executed decision has no matching order or trade", "", decision.get("ticket_id", ""), decision.get("signal_id", "")))

        for signal in signals:
            if not isinstance(signal, dict):
                continue
            if signal.get("direction") in {"long", "short"} and signal.get("signal_id") not in {item.get("signal_id") for item in tickets if isinstance(item, dict)}:
                gaps.append(self._gap("directional_signal_without_ticket", "info", "directional signal did not produce a same-day ticket", "", "", signal.get("signal_id", "")))
        return gaps

    def _gap(self, gap_type: str, severity: str, message: str, trade_id: str, ticket_id: str, signal_id: str) -> dict:
        return {
            "type": gap_type,
            "severity": severity,
            "message": message,
            "trade_id": trade_id or "",
            "ticket_id": ticket_id or "",
            "signal_id": signal_id or "",
        }

    def _latest_entry_reason(self, signals: list[dict], tickets: list[dict], decisions: list[dict]) -> str:
        if tickets:
            return tickets[-1].get("rationale") or tickets[-1].get("trigger") or ""
        if signals:
            return signals[-1].get("thesis", "")
        if decisions:
            return decisions[-1].get("notes", "")
        return ""

    def _latest_exit_reason(self, trades: list[dict], paper_exit_decisions: dict) -> str:
        for trade in reversed(trades):
            if trade.get("exit_reason"):
                return str(trade["exit_reason"])
        queue = paper_exit_decisions.get("queue", []) if isinstance(paper_exit_decisions, dict) else []
        if queue:
            item = queue[0]
            return item.get("exit_type") or item.get("exit_reason", "")
        return ""

    def _review_events(self, signals: list[dict], decisions: list[dict], risk_blocks: list[dict]) -> list[dict]:
        events = []
        for signal in signals:
            if not isinstance(signal, dict):
                continue
            events.append({
                "type": "signal",
                "signal_id": signal.get("signal_id", ""),
                "ticket_id": "",
                "timestamp": signal.get("generated_at", ""),
                "status": signal.get("status", ""),
                "direction": signal.get("direction", ""),
                "summary": signal.get("thesis", ""),
                "counts_as_trade_sample": False,
            })
        for decision in decisions:
            if not isinstance(decision, dict):
                continue
            executed = decision.get("decision_status") in {"executed", "executed_paper"}
            events.append({
                "type": "decision",
                "signal_id": decision.get("signal_id", ""),
                "ticket_id": decision.get("ticket_id", ""),
                "timestamp": decision.get("decided_at") or decision.get("created_at", ""),
                "status": decision.get("decision_status", ""),
                "summary": decision.get("notes", ""),
                "counts_as_trade_sample": executed,
            })
        for block in risk_blocks:
            if not isinstance(block, dict):
                continue
            events.append({
                "type": "risk_block",
                "signal_id": block.get("signal_id", ""),
                "ticket_id": block.get("ticket_id", ""),
                "timestamp": block.get("generated_at", ""),
                "status": block.get("status", "block"),
                "summary": block.get("reason", ""),
                "counts_as_trade_sample": False,
            })
        return sorted(events, key=lambda item: item.get("timestamp") or "")

    def _risk_summary(self, run_date: str, tickets: list[dict], risk_blocks: list[dict]) -> dict:
        next_ticket = next((item for item in tickets if item.get("asset") == "GOLD"), {})
        requested = float(next_ticket.get("max_loss_pct", 0))
        summary = PortfolioRiskState(self.output_root).summary(run_date, requested)
        if risk_blocks and not next_ticket:
            summary = {**summary, **risk_blocks[-1].get("portfolio_risk", {}), "block_reason": risk_blocks[-1].get("reason", "")}
        return {
            "daily_loss_stop_pct": summary["daily_loss_stop_pct"],
            "used_loss_pct": summary["used_loss_pct"],
            "next_ticket_loss_pct": requested,
            "allows_next_paper_order": bool(next_ticket) and bool(summary["allows_candidate"]),
            "block_reason": summary.get("block_reason", ""),
            "blocked_ticket_id": risk_blocks[-1].get("ticket_id", "") if risk_blocks and not next_ticket else "",
            "blocked_signal_id": risk_blocks[-1].get("signal_id", "") if risk_blocks and not next_ticket else "",
            "candidate_loss_pct": summary.get("candidate_loss_pct", requested),
            "projected_loss_pct": summary.get("projected_loss_pct", summary.get("used_loss_pct")),
            "open_trades": summary.get("open_trades", 0),
        }

    @staticmethod
    def _parse_ts(value) -> float | None:
        """Parse a timestamp to epoch seconds, tolerating both '...Z' and
        '...+00:00' forms (the bars use +00:00, closed_at uses Z). String
        comparison across these two forms is wrong — always compare as epochs."""
        if not value:
            return None
        text = str(value).strip()
        if not text or text.startswith("mock-"):
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()

    def _parse_dt(self, value) -> datetime | None:
        epoch = self._parse_ts(value)
        if epoch is None:
            return None
        return datetime.fromtimestamp(epoch, tz=timezone.utc)

    def _load_all_closed_trades(self) -> list[dict]:
        """All closed paper trades across history (paper_trades/closed/*.json),
        deduped by trade_id. Needed so the NAV curve can mark trades that closed
        on earlier days, not just today's run_date."""
        closed_dir = self.output_root / "paper_trades" / "closed"
        if not closed_dir.exists():
            return []
        out: list[dict] = []
        seen: set = set()
        for path in sorted(closed_dir.glob("*.json")):
            rows = load_json(path)
            for trade in rows if isinstance(rows, list) else []:
                tid = trade.get("trade_id") or f"_{len(out)}"
                if tid in seen:
                    continue
                seen.add(tid)
                out.append(trade)
        return out

    def _intraday_nav_curve(
        self,
        bars: list[dict],
        paper_performance: dict,
        open_trades: list[dict],
        paper_account: dict,
        equity_curve: dict,
        closed_trades: list[dict] | None = None,
    ) -> dict:
        starting_equity = float(paper_account.get("starting_equity", 10000) or 10000)
        performance_trades = paper_performance.get("open_trades", []) if paper_performance else []
        open_source = performance_trades or open_trades

        def _is_gold_position(item: dict) -> bool:
            return (
                item.get("symbol", "GOLD") in {"GOLD", "XAU", "XAUUSD"}
                and item.get("entry_price") is not None
                and item.get("quantity") is not None
            )

        open_positions = [t for t in open_source if _is_gold_position(t) and t.get("status", "open") == "open"]
        closed_positions = [t for t in (closed_trades or []) if _is_gold_position(t) and t.get("closed_at")]

        clean_bars = [
            item for item in bars
            if item.get("timestamp") and item.get("close") is not None
        ]
        if not clean_bars:
            return {
                "status": "warn",
                "reason": "no_bars",
                "starting_equity": starting_equity,
                "current_equity": equity_curve.get("current_equity", starting_equity),
                "point_count": 0,
                "points": [],
            }

        points = []
        peak = starting_equity
        max_drawdown = 0.0
        # Mark every trade to market on every bar so the equity curve is a true
        # path (full bar history already capped upstream by chart_display_bars):
        #   - before a trade opens: no contribution
        #   - while open: (bar_close - entry) * qty * direction  → tracks price
        #   - after it closes: its recorded realized_pnl (exact; do NOT recompute
        #     from exit-entry, and do NOT re-subtract execution cost)
        # This is what makes a held position show as a curve, not a flat step.
        for bar in clean_bars:
            close = float(bar.get("close", 0) or 0)
            timestamp = str(bar.get("timestamp", ""))
            t = self._parse_ts(timestamp)
            unrealized = 0.0
            realized = 0.0
            active_count = 0
            for trade in open_positions:
                opened = self._parse_ts(trade.get("opened_at"))
                if opened is not None and t is not None and t < opened:
                    continue
                direction = 1 if trade.get("side", "long") == "long" else -1
                entry = float(trade.get("entry_price", 0) or 0)
                quantity = float(trade.get("quantity", 0) or 0)
                unrealized += (close - entry) * quantity * direction
                active_count += 1
            for trade in closed_positions:
                opened = self._parse_ts(trade.get("opened_at"))
                closed = self._parse_ts(trade.get("closed_at"))
                if opened is not None and t is not None and t < opened:
                    continue
                if closed is not None and t is not None and t >= closed:
                    realized += float(trade.get("realized_pnl", 0) or 0)
                else:
                    direction = 1 if trade.get("side", "long") == "long" else -1
                    entry = float(trade.get("entry_price", 0) or 0)
                    quantity = float(trade.get("quantity", 0) or 0)
                    unrealized += (close - entry) * quantity * direction
                    active_count += 1
            equity = starting_equity + realized + unrealized
            peak = max(peak, equity)
            drawdown = ((equity - peak) / peak) * 100 if peak else 0.0
            max_drawdown = min(max_drawdown, drawdown)
            points.append({
                "timestamp": timestamp,
                "close": round(close, 4),
                "equity": round(equity, 4),
                "unrealized_pnl": round(unrealized, 4),
                "realized_pnl": round(realized, 4),
                "active_trade_count": active_count,
                "drawdown_pct": round(drawdown, 4),
            })
        current = points[-1] if points else {}
        return {
            "status": "pass" if points else "warn",
            "source": "5m_mark_to_market",
            "starting_equity": starting_equity,
            "current_equity": current.get("equity", equity_curve.get("current_equity", starting_equity)),
            "current_drawdown_pct": current.get("drawdown_pct", equity_curve.get("current_drawdown_pct", 0)),
            "max_drawdown_pct": round(max_drawdown, 4),
            "point_count": len(points),
            "points": points,
        }

    def _market_db_summary(self) -> dict:
        independent = uses_independent_datafeed(self.market_db)
        if not independent and not self.market_db.exists():
            return {"path": str(self.market_db), "exists": False, "bars": []}
        coverage = market_data_repository(self.market_db).coverage()
        source_config = self.config.get("market_data_sources", {}).get("gold_5m", {})
        official_providers = set(source_config.get("official_broker_providers", ["broker_csv", "mt5_csv", "ibkr", "oanda"]))
        public_providers = set(source_config.get("public_providers", ["gold-api.com", "yahoo_chart:GC=F"]))
        execution_venue_providers = set(source_config.get("execution_venue_providers", []))
        gold_5m = [item for item in coverage if item["symbol"] == "GOLD" and item["timeframe"] == "5m"]
        return {
            "path": "datafeed" if independent else str(self.market_db),
            "exists": True,
            "backend": "datafeed" if independent else "legacy_test_store",
            "bars": coverage,
            "official_providers": sorted(official_providers),
            "execution_venue_providers": sorted(execution_venue_providers),
            "public_providers": sorted(public_providers),
            "gold_5m_total_rows": sum(item["rows"] for item in gold_5m),
            "gold_5m_imported_rows": sum(
                item["rows"]
                for item in gold_5m
                if item["provider"] != "local_synthetic_seed"
            ),
            "gold_5m_official_rows": sum(item["rows"] for item in gold_5m if item["provider"] in official_providers),
            "gold_5m_execution_venue_rows": sum(item["rows"] for item in gold_5m if item["provider"] in execution_venue_providers),
            "gold_5m_public_rows": sum(item["rows"] for item in gold_5m if item["provider"] in public_providers),
            "gold_5m_synthetic_rows": sum(item["rows"] for item in gold_5m if item["provider"] == "local_synthetic_seed"),
        }

    def _data_provenance_summary(self, preflight: dict, market_db: dict, latest: dict, latest_quote: dict) -> dict:
        latest_price = latest_quote.get("close") if latest_quote else latest.get("close")
        ready_for_live = bool(preflight.get("ready_for_live"))
        ready_for_paper = bool(preflight.get("ready_for_paper"))
        official_rows = int(preflight.get("official_rows") or market_db.get("gold_5m_official_rows") or 0)
        execution_rows = int(preflight.get("execution_venue_rows") or market_db.get("gold_5m_execution_venue_rows") or 0)
        public_rows = int(preflight.get("public_rows") or market_db.get("gold_5m_public_rows") or 0)
        synthetic_rows = int(market_db.get("gold_5m_synthetic_rows") or 0)
        latest_bar_provider = latest.get("provider", "")
        latest_quote_provider = latest_quote.get("provider", "") if latest_quote else ""
        gate_payload = {
            **preflight,
            "official_rows": official_rows,
            "execution_venue_rows": execution_rows,
            "official_broker_providers": preflight.get("official_broker_providers") or market_db.get("official_providers") or [],
        }
        official_live_ready = is_official_broker_ohlc_ready(gate_payload)
        execution_venue_ready = bool(
            ready_for_live
            and not official_live_ready
            and (
                preflight.get("live_data_mode") == "execution_venue"
                or execution_venue_rows(gate_payload) > 0
            )
        )
        display_record = latest if (official_live_ready or execution_venue_ready) else (latest_quote or latest)
        latest_provider = latest_bar_provider if (official_live_ready or execution_venue_ready) else (latest_quote_provider or latest_bar_provider)
        latest_flags = display_record.get("quality_flags", []) if display_record else []
        latest_truth = self._latest_truth_level(latest_provider, latest_flags, preflight, market_db)
        if official_live_ready:
            mode = "LIVE_OFFICIAL"
            label = "official broker OHLC"
            allows_live = True
        elif execution_venue_ready:
            mode = "EXECUTION_VENUE"
            label = "execution venue OHLC"
            allows_live = True
        elif ready_for_paper:
            mode = "PAPER_PUBLIC"
            label = "public snapshot / local paper feed"
            allows_live = False
        else:
            mode = "DATA_BLOCKED"
            label = "market data unavailable"
            allows_live = False
        return {
            "mode": mode,
            "label": label,
            "latest_price": latest_price,
            "latest_provider": latest_provider or preflight.get("latest_provider", ""),
            "latest_bar_provider": latest_bar_provider,
            "latest_quote_provider": latest_quote_provider,
            "latest_timestamp": display_record.get("timestamp", "") if display_record else "",
            "latest_quality_flags": latest_flags,
            "latest_truth_level": latest_truth,
            "latest_is_mock": latest_truth == "mock",
            "latest_is_public": latest_truth == "public",
            "latest_is_official": latest_truth == "official",
            "latest_is_execution_venue": latest_truth == "execution_venue",
            "allows_paper": ready_for_paper,
            "allows_live": allows_live,
            "allows_execution_venue": execution_venue_ready,
            "official_rows": official_rows,
            "execution_venue_rows": execution_rows,
            "public_rows": public_rows,
            "synthetic_rows": synthetic_rows,
            "market_db": market_db.get("path", ""),
            "message": preflight.get("message", ""),
        }

    def _latest_truth_level(self, provider: str, quality_flags: list, preflight: dict, market_db: dict) -> str:
        provider = provider or preflight.get("latest_provider", "")
        official = set(preflight.get("official_broker_providers") or market_db.get("official_providers") or [])
        execution_venue = set(preflight.get("execution_venue_providers") or market_db.get("execution_venue_providers") or [])
        public = set(preflight.get("public_providers") or market_db.get("public_providers") or [])
        flags = set(quality_flags or [])
        if provider in official:
            return "official"
        if provider in execution_venue or "execution_venue_feed" in flags:
            return "execution_venue"
        if provider in {"mock_kline", "local_synthetic_seed"} or "mock" in flags or "synthetic_seed" in flags:
            return "mock"
        if provider in public or "paper_only_market_data" in flags or "live_snapshot" in flags:
            return "public"
        if provider:
            return "unknown"
        return "missing"

    def _performance_summary(self, open_trades: list[dict], closed_trades: list[dict]) -> dict:
        realized = sum(float(item.get("realized_pnl", 0)) for item in closed_trades)
        wins = sum(1 for item in closed_trades if float(item.get("realized_pnl", 0)) > 0)
        losses = sum(1 for item in closed_trades if float(item.get("realized_pnl", 0)) < 0)
        total = len(closed_trades)
        return {
            "open_trades": len(open_trades),
            "closed_trades": total,
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / total, 4) if total else 0,
            "realized_pnl": round(realized, 4),
        }

    def _load_mapping(self, path: Path) -> dict:
        if not path.exists():
            return {}
        import json

        return json.loads(path.read_text(encoding="utf-8"))

    def _read_text(self, path: Path) -> str:
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")
