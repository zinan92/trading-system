from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from services.broker_adapter import broker_preflight
from services.broker_feed_doctor import BrokerFeedDoctor
from services.broker_receipts import BrokerReceiptImporter
from services.completion_audit import CompletionAudit
from services.config_loader import ROOT, load_pipeline_config
from services.data_gap_repair import DataGapRepairRequest
from services.data_source_preflight import DataSourcePreflight
from services.health_check import HealthCheck
from services.journal_store import load_json, write_json
from services.live_readiness import LiveReadiness
from services.live_switch_plan import LiveSwitchPlan
from services.live_cutover_package import LiveCutoverPackage
from services.mock_runtime import MockTradingRuntime
from services.paper_reconciliation import PaperReconciliation
from services.strategy_guardrails import StrategyGuardrails


class SystemDoctor:
    def __init__(self, output_root: Path | None = None) -> None:
        config = load_pipeline_config()
        self.config = config
        self.output_root = output_root or Path(os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(ROOT / config.get("output_root", "outputs"))))

    def run(self, run_date: str) -> dict:
        broker = broker_preflight(self.output_root)
        feed_doctor = BrokerFeedDoctor(self.output_root).run(run_date)
        receipts = BrokerReceiptImporter(self.output_root).import_pending(run_date)
        data_source = DataSourcePreflight(self.output_root).run(run_date)
        paper_reconciliation = PaperReconciliation(self.output_root).run(run_date)
        strategy_guardrails = StrategyGuardrails(self.output_root).run(run_date)
        health = HealthCheck(self.output_root).run(run_date)
        audit = CompletionAudit(self.output_root).run(run_date)
        mock_runtime = MockTradingRuntime(self.output_root).run(run_date)
        live_readiness = LiveReadiness(self.output_root).run(run_date)
        live_switch_plan = LiveSwitchPlan(self.output_root).run(run_date)
        live_cutover = LiveCutoverPackage(self.output_root).run(run_date)
        latest_runner = self._load_mapping(self.output_root / "runner_status" / "current.json")
        mt5_smoke = (load_json(self.output_root / "mt5_bridge_smoke" / "current.json") or [{}])[-1]
        feed_smoke = (load_json(self.output_root / "broker_feed_smoke" / "current.json") or [{}])[-1]
        oanda_feed = (load_json(self.output_root / "oanda_feed" / "current.json") or [{}])[-1]
        data_gaps = (load_json(self.output_root / "data_gaps" / "current.json") or [{}])[-1]
        repair_request = DataGapRepairRequest(self.output_root, self._feed_dir()).build(run_date)
        next_actions = self._next_actions(data_source, health, audit, latest_runner, feed_doctor, oanda_feed, live_readiness, mock_runtime)
        status = self._rollup(health.get("status"), audit.get("status"), live_readiness.get("status"), mock_runtime.get("status"))
        payload = {
            "run_date": run_date,
            "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": status,
            "summary": {
                "health": health.get("status"),
                "audit": audit.get("status"),
                "mock_runtime": mock_runtime.get("status"),
                "mock_ready": mock_runtime.get("mock_ready"),
                "mock_running": mock_runtime.get("mock_running"),
                "paper_reconciliation": paper_reconciliation.get("status"),
                "live_readiness": live_readiness.get("status"),
                "live_switch_plan": live_switch_plan.get("status"),
                "live_cutover": live_cutover.get("status"),
                "live_cutover_blockers": len(live_cutover.get("blockers", [])),
                "strategy_guardrails": strategy_guardrails.get("status"),
                "allow_new_paper_order": strategy_guardrails.get("allow_new_paper_order"),
                "live_execution_ready": live_readiness.get("live_ready"),
                "live_failed_checks": (live_readiness.get("summary") or {}).get("failed_checks", []),
                "broker_mode": broker.get("mode"),
                "broker_ready": broker.get("ready"),
                "broker_receipts": receipts.get("total_receipts"),
                "new_broker_receipts": receipts.get("new_receipts"),
                "broker_feed_doctor": feed_doctor.get("status"),
                "oanda_feed": oanda_feed.get("status", ""),
                "oanda_ready": oanda_feed.get("ready"),
                "oanda_imported_rows": oanda_feed.get("imported_rows"),
                "mt5_bridge_smoke": mt5_smoke.get("status", ""),
                "broker_feed_smoke": feed_smoke.get("status", ""),
                "data_gaps": data_gaps.get("status", ""),
                "data_gap_count": data_gaps.get("gap_count", 0),
                "data_gap_repair": repair_request.get("status", ""),
                "data_source": data_source.get("status"),
                "paper_ready": data_source.get("ready_for_paper"),
                "live_ready": data_source.get("ready_for_live"),
                "latest_price": data_source.get("latest_price"),
                "latest_provider": data_source.get("latest_provider"),
                "latest_quote_price": (data_source.get("latest_quote") or {}).get("close"),
                "latest_quote_provider": (data_source.get("latest_quote") or {}).get("provider"),
                "runner_state": latest_runner.get("state", ""),
            },
            "next_actions": next_actions,
            "artifacts": {
                "dashboard_url": "http://127.0.0.1:8765/dashboard-v4.html",
                "health": str(self.output_root / "health" / "current.json"),
                "audit": str(self.output_root / "audits" / "current.json"),
                "mock_runtime": str(self.output_root / "mock_runtime" / "current.json"),
                "paper_reconciliation": str(self.output_root / "paper_reconciliation" / "current.json"),
                "live_readiness": str(self.output_root / "live_readiness" / "current.json"),
                "live_switch_plan": str(self.output_root / "live_switch_plan" / "current.json"),
                "live_cutover": str(self.output_root / "live_cutover" / "current.json"),
                "data_source_preflight": str(self.output_root / "data_source_preflight" / "current.json"),
                "data_gaps": str(self.output_root / "data_gaps" / "current.json"),
                "data_gap_repair": str(self.output_root / "data_gap_repair_requests" / "current.json"),
                "broker_feed_doctor": str(self.output_root / "broker_feed_doctor" / "current.json"),
                "oanda_feed": str(self.output_root / "oanda_feed" / "current.json"),
                "broker_preflight": str(self.output_root / "broker_preflight" / "current.json"),
                "broker_receipts": str(self.output_root / "broker_receipts" / "current.json"),
                "mt5_bridge_smoke": str(self.output_root / "mt5_bridge_smoke" / "current.json"),
                "broker_feed_smoke": str(self.output_root / "broker_feed_smoke" / "current.json"),
                "runner_status": str(self.output_root / "runner_status" / "current.json"),
                "strategy_guardrails": str(self.output_root / "strategy_guardrails" / "current.json"),
            },
        }
        write_json(self.output_root / "doctor" / "current.json", [payload])
        write_json(self.output_root / "doctor" / f"{run_date}.json", [payload])
        return payload

    def _next_actions(
        self,
        data_source: dict,
        health: dict,
        audit: dict,
        runner: dict,
        feed_doctor: dict | None = None,
        oanda_feed: dict | None = None,
        live_readiness: dict | None = None,
        mock_runtime: dict | None = None,
    ) -> list[str]:
        actions: list[str] = []
        feed_doctor = feed_doctor or {}
        oanda_feed = oanda_feed or {}
        live_readiness = live_readiness or {}
        mock_runtime = mock_runtime or {}
        if not runner:
            actions.append("Start the 5m runner: python3 -m pipelines.runner --date <date> --paper-auto-approve --iterations 0 --interval-seconds 300")
        elif runner.get("state") not in {"ok", "running"}:
            actions.append("Inspect runner_status/current.json because the latest runner state is not ok.")
        if feed_doctor.get("status") == "fail":
            actions.append("Fix broker feed CSV validation errors before importing XAUUSD 5m data.")
        if not data_source.get("ready_for_live"):
            if oanda_feed.get("status") == "skipped":
                actions.append("Copy configs/live.env.template to configs/live.env, fill OANDA_API_TOKEN and OANDA_ACCOUNT_ID, then run python3 -m pipelines.import_official_feed --date <date> to import official XAU_USD M5 bars.")
            actions.append("Import official broker/MT5 XAUUSD 5m CSV via data/broker_feeds/gold_5m/ to unlock live-ready market data.")
        if health.get("status") == "error":
            actions.append("Fix System Health error checks before running new paper decisions.")
        failed = [item for item in audit.get("requirements", []) if item.get("status") == "fail"]
        for item in failed:
            actions.append(f"Resolve audit failure: {item.get('name')} - {item.get('summary')}")
        if live_readiness.get("status") == "fail":
            for item in live_readiness.get("next_actions", [])[:3]:
                if item not in actions:
                    actions.append(item)
        if mock_runtime.get("status") in {"fail", "warn"}:
            for item in mock_runtime.get("next_actions", [])[:2]:
                if item not in actions:
                    actions.append(item)
        if not actions:
            actions.append("System is ready for the configured execution mode.")
        return actions

    def _rollup(self, health_status: str | None, audit_status: str | None, live_status: str | None = None, mock_status: str | None = None) -> str:
        if health_status == "error" or audit_status == "fail":
            return "fail"
        if health_status == "warn" or audit_status == "warn" or live_status in {"fail", "warn"} or mock_status == "warn":
            return "warn"
        if mock_status == "fail":
            return "fail"
        return "pass"

    def _load_mapping(self, path: Path) -> dict:
        if not path.exists():
            return {}
        import json

        return json.loads(path.read_text(encoding="utf-8"))

    def _feed_dir(self) -> Path:
        raw = Path(self.config.get("broker_feed", {}).get("input_dir", "data/broker_feeds/gold_5m"))
        return raw if raw.is_absolute() else ROOT / raw
