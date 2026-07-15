"""Facade assembling DashboardState from services.dashboard mixins; snapshot and shared helpers."""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config, load_risk_rules, load_strategy_config
from services.journal_store import load_json
from services.market_data_access import market_data_repository
from services.paper_executor import PaperExecutor
from services.runner_status import RunnerStatusStore
from services.system_vitals import SystemVitals
from services.dashboard.market_view import MarketViewMixin
from services.dashboard.nav_quality import NavQualityMixin
from services.dashboard.replay import ReplayMixin
from services.dashboard.boards import BoardsMixin
from services.dashboard.frequency import FrequencyMixin
from services.dashboard.provenance import ProvenanceMixin
from services.dashboard.trade_lifecycle import TradeLifecycleMixin
from services.dashboard.vitals import VitalsMixin


class DashboardState(
    MarketViewMixin,
    NavQualityMixin,
    ReplayMixin,
    BoardsMixin,
    FrequencyMixin,
    ProvenanceMixin,
    TradeLifecycleMixin,
    VitalsMixin,
):
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

    @staticmethod
    def _format_epoch(value: float) -> str:
        return datetime.fromtimestamp(value, tz=timezone.utc).replace(microsecond=0).isoformat()

    @staticmethod
    def _safe_float(value: object) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _safe_int(self, value: object) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def _artifact_freshness(self, path: Path) -> dict:
        if not path.exists():
            return {"exists": False, "modified_at": ""}
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).replace(microsecond=0)
        return {"exists": True, "modified_at": modified.isoformat()}

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

    def _load_mapping(self, path: Path) -> dict:
        if not path.exists():
            return {}
        import json

        return json.loads(path.read_text(encoding="utf-8"))

    def _read_text(self, path: Path) -> str:
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")
