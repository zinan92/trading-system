from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from services.completion_audit import CompletionAudit
from services.config_loader import ROOT, load_pipeline_config
from services.bot_supervisor import BotSupervisor
from services.bot_checkpoint import BotCheckpoint
from services.data_source_preflight import DataSourcePreflight
from services.data_source_lineage import DataSourceLineage
from services.data_trust_report import DataTrustReport
from services.data_archive_manifest import DataArchiveManifest
from services.data_integrity_check import DataIntegrityCheck
from services.health_check import HealthCheck
from services.journal_store import load_json, write_json
from services.live_readiness import LiveReadiness
from services.live_switch_plan import LiveSwitchPlan
from services.live_cutover_package import LiveCutoverPackage
from services.live_dry_run_drill import LiveDryRunDrill
from services.live_broker_preflight_report import LiveBrokerPreflightReport
from services.live_submission_safety import LiveSubmissionSafetySmoke
from services.mock_runtime import MockTradingRuntime
from services.mock_trading_uat import MockTradingUAT
from services.operation_runbook import OperationRunbook
from services.official_feed_receipt import OfficialFeedReceipt
from services.official_feed_onboarding import OfficialFeedOnboarding
from services.paper_auto_approval_gate import PaperAutoApprovalGate
from services.paper_executor import PaperExecutor
from services.paper_exit_decisions import PaperExitDecisionQueue
from services.paper_exit_monitor import PaperExitMonitor
from services.paper_performance import PaperPerformanceAnalyzer
from services.paper_equity_curve import PaperEquityCurve
from services.paper_reconciliation import PaperReconciliation
from services.paper_risk_action_plan import PaperRiskActionPlan
from services.paper_trade_attribution import PaperTradeAttributor
from services.reporting import ReportBuilder
from services.risk_monitor import RiskMonitor
from services.runner_status import RunnerStatusStore
from services.strategy_improvement_plan import StrategyImprovementPlan
from services.strategy_guardrails import StrategyGuardrails
from services.system_doctor import SystemDoctor


class DailyReviewRunner:
    def __init__(self, output_root: Path | None = None, market_db: Path | None = None) -> None:
        config = load_pipeline_config()
        self.output_root = output_root or Path(os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(ROOT / config.get("output_root", "outputs"))))
        self.market_db = market_db or Path(os.getenv("TRADING_ORCHESTRATOR_MARKET_DB", str(ROOT / config.get("local_market_db", "data/market_data.db"))))

    def run(self, run_date: str) -> dict:
        started_at = self._now()
        receipt = {
            "run_date": run_date,
            "status": "running",
            "started_at": started_at,
            "finished_at": "",
            "artifacts": {},
            "summary": {},
            "checks": [],
        }
        self._write(receipt, run_date)

        data_source = DataSourcePreflight(self.output_root, self.market_db).run(run_date)
        data_lineage = DataSourceLineage(self.output_root, self.market_db).run(run_date)
        data_trust = DataTrustReport(self.output_root, self.market_db).run(run_date)
        official_feed_receipt = OfficialFeedReceipt(self.output_root, self.market_db).refresh(run_date)
        official_feed_onboarding = OfficialFeedOnboarding(self.output_root).build(run_date)
        executor = PaperExecutor(self.output_root)
        executor.evaluate_exits(run_date)
        executor.mark_to_market(run_date)
        trade_attribution = PaperTradeAttributor(self.output_root).run(run_date)
        performance = PaperPerformanceAnalyzer(self.output_root).build(run_date)
        paper_exit_monitor = PaperExitMonitor(self.output_root).run(run_date)
        paper_exit_decisions = PaperExitDecisionQueue(self.output_root).build(run_date)
        equity_curve = PaperEquityCurve(self.output_root).build(run_date, performance)
        paper_reconciliation = PaperReconciliation(self.output_root).run(run_date)
        report_path = ReportBuilder(self.output_root).build_daily_report(run_date)
        strategy_guardrails = StrategyGuardrails(self.output_root).run(run_date)
        risk_monitor = RiskMonitor(self.output_root).run(run_date)
        paper_risk_action_plan = PaperRiskActionPlan(self.output_root).build(run_date)
        runner_status = RunnerStatusStore(self.output_root).current()
        paper_auto_gate = PaperAutoApprovalGate(self.output_root).evaluate(run_date, auto_requested=bool(runner_status.get("paper_auto_approve")))
        health = HealthCheck(self.output_root, self.market_db).run(run_date)
        audit = CompletionAudit(self.output_root, self.market_db).run(run_date)
        mock_runtime = MockTradingRuntime(self.output_root, self.market_db).run(run_date)
        mock_uat = MockTradingUAT(self.output_root).run(run_date)
        live_readiness = LiveReadiness(self.output_root, self.market_db).run(run_date)
        live_switch_plan = LiveSwitchPlan(self.output_root).run(run_date)
        live_cutover = LiveCutoverPackage(self.output_root, self.market_db).run(run_date)
        operation_runbook = OperationRunbook(self.output_root).run(run_date)
        live_submission_safety = LiveSubmissionSafetySmoke(self.output_root).run(run_date)
        live_dry_run_drill = LiveDryRunDrill(self.output_root, self.market_db).run(run_date)
        live_broker_preflight = LiveBrokerPreflightReport(self.output_root).run(run_date)
        bot_supervisor = BotSupervisor(self.output_root).run(run_date)
        report_path = ReportBuilder(self.output_root).build_daily_report(run_date)
        strategy_improvement_plan = StrategyImprovementPlan(self.output_root).build(run_date)
        bot_checkpoint = BotCheckpoint(self.output_root).build(run_date)
        report_path = ReportBuilder(self.output_root).build_daily_report(run_date)
        self._ensure_paper_account_artifacts(run_date)
        data_archive = DataArchiveManifest(self.output_root, self.market_db).run(run_date)
        data_integrity = DataIntegrityCheck(self.output_root, self.market_db).run(run_date)

        receipt = {
            "run_date": run_date,
            "status": self._status(health, audit, mock_runtime),
            "started_at": started_at,
            "finished_at": self._now(),
            "artifacts": {
                "report": str(report_path),
                "journal": str(self.output_root / "journals" / f"{run_date}.md"),
                "review_notes": str(self.output_root / "review_notes" / f"{run_date}.md"),
                "performance": str(self.output_root / "performance" / f"{run_date}.json"),
                "equity_curve": str(self.output_root / "equity_curve" / f"{run_date}.json"),
                "paper_reconciliation": str(self.output_root / "paper_reconciliation" / f"{run_date}.json"),
                "paper_trade_attribution": str(self.output_root / "paper_trade_attribution" / f"{run_date}.json"),
                "paper_exit_monitor": str(self.output_root / "paper_exit_monitor" / f"{run_date}.json"),
                "paper_exit_decisions": str(self.output_root / "paper_exit_decisions" / f"{run_date}.json"),
                "health": str(self.output_root / "health" / f"{run_date}.json"),
                "audit": str(self.output_root / "audits" / f"{run_date}.json"),
                "mock_runtime": str(self.output_root / "mock_runtime" / f"{run_date}.json"),
                "mock_uat": str(self.output_root / "mock_uat" / f"{run_date}.json"),
                "strategy_guardrails": str(self.output_root / "strategy_guardrails" / f"{run_date}.json"),
                "strategy_experiments": str(self.output_root / "strategy_experiments" / f"{run_date}.json"),
                "strategy_improvement_plan": str(self.output_root / "strategy_improvement_plan" / f"{run_date}.json"),
                "strategy_promotion_gate": str(self.output_root / "strategy_promotion_gate" / f"{run_date}.json"),
                "risk_monitor": str(self.output_root / "risk_monitor" / f"{run_date}.json"),
                "paper_risk_action_plan": str(self.output_root / "paper_risk_action_plan" / f"{run_date}.json"),
                "paper_auto_approval_gate": str(self.output_root / "paper_auto_approval_gate" / f"{run_date}.json"),
                "live_readiness": str(self.output_root / "live_readiness" / f"{run_date}.json"),
                "live_switch_plan": str(self.output_root / "live_switch_plan" / f"{run_date}.json"),
                "live_cutover": str(self.output_root / "live_cutover" / f"{run_date}.json"),
                "live_submission_safety": str(self.output_root / "live_submission_safety" / f"{run_date}.json"),
                "live_dry_run_drill": str(self.output_root / "live_dry_run_drill" / f"{run_date}.json"),
                "live_broker_preflight": str(self.output_root / "live_broker_preflight" / f"{run_date}.json"),
                "bot_supervisor": str(self.output_root / "bot_supervisor" / f"{run_date}.json"),
                "bot_checkpoint": str(self.output_root / "bot_checkpoints" / f"{run_date}.json"),
                "data_archive": str(self.output_root / "data_archive" / f"{run_date}.json"),
                "data_integrity": str(self.output_root / "data_integrity" / f"{run_date}.json"),
                "data_source_lineage": str(self.output_root / "data_source_lineage" / f"{run_date}.json"),
                "data_trust": str(self.output_root / "data_trust" / f"{run_date}.json"),
                "official_feed_receipt": str(self.output_root / "official_feed_receipts" / f"{run_date}.json"),
                "official_feed_onboarding": str(self.output_root / "official_feed_onboarding" / f"{run_date}.md"),
            },
            "summary": {
                "data_source": data_source.get("status"),
                "data_source_lineage": data_lineage.get("status"),
                "data_truth_level": data_lineage.get("truth_level"),
                "data_trust": data_trust.get("status"),
                "data_display_mode": data_trust.get("display_mode"),
                "official_feed_receipt": official_feed_receipt.get("status"),
                "official_feed_onboarding": official_feed_onboarding.get("status"),
                "official_rows": official_feed_receipt.get("official_rows"),
                "paper_reconciliation": paper_reconciliation.get("status"),
                "paper_trade_attribution": trade_attribution.get("status"),
                "paper_exit_monitor": paper_exit_monitor.get("status"),
                "paper_exit_decisions": paper_exit_decisions.get("status"),
                "paper_ready": data_source.get("ready_for_paper"),
                "live_ready": data_source.get("ready_for_live"),
                "health": health.get("status"),
                "audit": audit.get("status"),
                "mock_runtime": mock_runtime.get("status"),
                "mock_uat": mock_uat.get("status"),
                "strategy_guardrails": strategy_guardrails.get("status"),
                "strategy_experiments": ((load_json(self.output_root / "strategy_experiments" / f"{run_date}.json") or [{}])[-1]).get("status"),
                "strategy_improvement_plan": strategy_improvement_plan.get("status"),
                "strategy_promotion_gate": ((load_json(self.output_root / "strategy_promotion_gate" / f"{run_date}.json") or [{}])[-1]).get("status"),
                "risk_monitor": risk_monitor.get("status"),
                "paper_risk_action_plan": paper_risk_action_plan.get("status"),
                "paper_auto_approval_gate": paper_auto_gate.get("status"),
                "risk_kill_switch_active": risk_monitor.get("kill_switch_active"),
                "risk_allow_paper_auto_approve": risk_monitor.get("allow_paper_auto_approve"),
                "allow_new_paper_order": strategy_guardrails.get("allow_new_paper_order"),
                "mock_ready": mock_runtime.get("mock_ready"),
                "mock_running": mock_runtime.get("mock_running"),
                "live_readiness": live_readiness.get("status"),
                "live_switch_plan": live_switch_plan.get("status"),
                "live_cutover": live_cutover.get("status"),
                "live_submission_safety": live_submission_safety.get("status"),
                "live_submission_blocked_by_gate": live_submission_safety.get("blocked_by_activation_gate"),
                "live_submission_network_call_attempted": live_submission_safety.get("network_call_attempted"),
                "live_dry_run_drill": live_dry_run_drill.get("status"),
                "live_broker_preflight": live_broker_preflight.get("status"),
                "safe_to_submit_live_order": live_dry_run_drill.get("safe_to_submit_live_order"),
                "bot_supervisor": bot_supervisor.get("status"),
                "mock_bot_running": bot_supervisor.get("mock_bot_running"),
                "bot_checkpoint": bot_checkpoint.get("status"),
                "data_archive": data_archive.get("status"),
                "data_integrity": data_integrity.get("status"),
                "net_pnl_marked": (performance.get("summary") or {}).get("net_pnl_marked"),
                "open_unrealized_r": (performance.get("summary") or {}).get("open_unrealized_r"),
                "expectancy_r": (performance.get("summary") or {}).get("expectancy_r"),
                "current_equity": equity_curve.get("current_equity"),
                "current_drawdown_pct": equity_curve.get("current_drawdown_pct"),
                "max_drawdown_pct": equity_curve.get("max_drawdown_pct"),
            },
            "checks": [
                {"name": "report", "status": "pass" if report_path.exists() else "fail"},
                {"name": "journal", "status": "pass" if (self.output_root / "journals" / f"{run_date}.md").exists() else "fail"},
                {"name": "review_notes", "status": "pass" if (self.output_root / "review_notes" / f"{run_date}.md").exists() else "fail"},
                {"name": "performance", "status": "pass" if performance.get("summary") else "fail"},
                {"name": "equity_curve", "status": "pass" if equity_curve.get("status") == "pass" else "fail"},
                {"name": "paper_reconciliation", "status": "pass" if paper_reconciliation.get("status") == "pass" else "fail"},
                {"name": "paper_trade_attribution", "status": "pass" if trade_attribution.get("status") == "pass" else "fail"},
                {"name": "paper_exit_monitor", "status": "pass" if paper_exit_monitor.get("status") in {"pass", "alert", "empty"} else "fail"},
                {"name": "paper_exit_decisions", "status": "pass" if paper_exit_decisions.get("status") in {"review", "alert", "empty"} else "fail"},
                {"name": "mock_runtime", "status": "pass" if mock_runtime.get("mock_ready") else "fail"},
                {"name": "mock_uat", "status": "pass" if mock_uat.get("status") in {"pass", "warn"} else "fail"},
                {"name": "strategy_guardrails", "status": "pass" if strategy_guardrails.get("status") in {"pass", "warn", "block"} else "fail"},
                {"name": "strategy_experiments", "status": "pass" if ((load_json(self.output_root / "strategy_experiments" / f"{run_date}.json") or [{}])[-1]).get("status") in {"blocked", "collecting_evidence", "experiment_ready"} else "fail"},
                {"name": "strategy_improvement_plan", "status": "pass" if strategy_improvement_plan.get("status") in {"action_required", "promotion_review", "collecting_evidence"} else "fail"},
                {"name": "strategy_promotion_gate", "status": "pass" if ((load_json(self.output_root / "strategy_promotion_gate" / f"{run_date}.json") or [{}])[-1]).get("status") in {"blocked", "requestable"} else "fail"},
                {"name": "risk_monitor", "status": "pass" if risk_monitor.get("status") in {"pass", "warn", "block"} else "fail"},
                {"name": "paper_risk_action_plan", "status": "pass" if paper_risk_action_plan.get("status") in {"action_required", "review", "empty"} else "fail"},
                {"name": "paper_auto_approval_gate", "status": "pass" if paper_auto_gate.get("status") in {"allow", "block", "skipped"} else "fail"},
                {"name": "health", "status": "pass" if health.get("status") in {"ok", "warn"} else "fail"},
                {"name": "audit", "status": "pass" if audit.get("status") in {"pass", "warn"} else "fail"},
                {"name": "live_switch_plan", "status": "pass" if live_switch_plan.get("status") in {"ready", "blocked"} else "fail"},
                {"name": "live_cutover", "status": "pass" if live_cutover.get("status") in {"real_money_ready", "dry_run_ready", "blocked"} else "fail"},
                {"name": "live_submission_safety", "status": "pass" if live_submission_safety.get("status") == "pass" and live_submission_safety.get("network_call_attempted") is False else "fail"},
                {"name": "live_dry_run_drill", "status": "pass" if live_dry_run_drill.get("status") in {"real_money_ready", "dry_run_ready", "blocked"} else "fail"},
                {"name": "live_broker_preflight", "status": "pass" if live_broker_preflight.get("status") in {"ready", "dry_run_only", "blocked"} else "fail"},
                {"name": "bot_supervisor", "status": "pass" if bot_supervisor.get("status") in {"pass", "warn"} else "fail"},
                {"name": "bot_checkpoint", "status": "pass" if bot_checkpoint.get("status") in {"ready", "watch", "attention_required"} else "fail"},
                {"name": "data_archive", "status": "pass" if data_archive.get("status") in {"pass", "warn"} else "fail"},
                {"name": "data_integrity", "status": "pass" if data_integrity.get("status") in {"pass", "warn"} else "fail"},
                {"name": "data_source_lineage", "status": "pass" if data_lineage.get("status") in {"pass", "warn"} else "fail"},
                {"name": "data_trust", "status": "pass" if data_trust.get("status") in {"pass", "warn"} else "fail"},
                {"name": "official_feed_receipt", "status": "pass" if official_feed_receipt.get("status") in {"pass", "warn"} else "fail"},
                {"name": "official_feed_onboarding", "status": "pass" if official_feed_onboarding.get("status") in {"ready", "open"} else "fail"},
            ],
        }
        self._write(receipt, run_date)
        doctor = SystemDoctor(self.output_root).run(run_date)
        receipt["summary"]["doctor"] = doctor.get("status")
        receipt["artifacts"]["doctor"] = str(self.output_root / "doctor" / f"{run_date}.json")
        receipt["summary"]["operation_runbook"] = operation_runbook.get("status")
        receipt["artifacts"]["operation_runbook"] = str(self.output_root / "operation_runbooks" / f"{run_date}.json")
        self._write(receipt, run_date)
        return receipt

    def _status(self, health: dict, audit: dict, mock_runtime: dict) -> str:
        if health.get("status") == "error" or audit.get("status") == "fail" or not mock_runtime.get("mock_ready"):
            return "fail"
        if health.get("status") == "warn" or audit.get("status") == "warn" or mock_runtime.get("status") == "warn":
            return "warn"
        return "pass"

    def _write(self, receipt: dict, run_date: str) -> None:
        write_json(self.output_root / "daily_review_runs" / f"{run_date}.json", [receipt])
        write_json(self.output_root / "daily_review_runs" / "current.json", [receipt])

    def _ensure_paper_account_artifacts(self, run_date: str) -> None:
        for path in [
            self.output_root / "paper_orders" / f"{run_date}.json",
            self.output_root / "paper_trades" / "current.json",
            self.output_root / "paper_trades" / "closed" / f"{run_date}.json",
            self.output_root / "paper_exit_decisions" / f"{run_date}.decisions.json",
        ]:
            if not path.exists():
                write_json(path, [])
        positions_path = self.output_root / "paper_positions" / "current.json"
        if not positions_path.exists():
            positions_path.parent.mkdir(parents=True, exist_ok=True)
            positions_path.write_text(json.dumps({}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def _now(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
