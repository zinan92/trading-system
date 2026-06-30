from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json
from services.live_env import LiveEnvStatus
from services.live_approval import LiveApprovalStore
from services.official_market_data_gate import official_broker_ohlc_status


class LiveActivationGate:
    def __init__(self, output_root: Path | None = None) -> None:
        config = load_pipeline_config()
        self.config = config
        self.output_root = output_root or Path(os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(ROOT / config.get("output_root", "outputs"))))

    def run(self, run_date: str) -> dict:
        live_env = LiveEnvStatus(self.output_root).run(run_date)
        data_source = self._latest("data_source_preflight", run_date)
        live_readiness = self._latest("live_readiness", run_date)
        approval = LiveApprovalStore(self.output_root).status(run_date)
        schedule = (load_json(self.output_root / "schedules" / "status_current.json") or [{}])[-1]
        mock = (load_json(self.output_root / "mock_runtime" / "current.json") or [{}])[-1]
        broker = (load_json(self.output_root / "broker_preflight" / "current.json") or [{}])[-1]
        supported_providers = {"oanda_rest", "mt5_file_bridge", "binance_usdm"}
        official_data_ready, official_data_summary = official_broker_ohlc_status(data_source)
        checks = [
            self._check_bool("official_5m_data", official_data_ready, official_data_summary, data_source),
            self._check_bool("live_env", live_env.get("status") == "pass", "Required broker env keys are present.", live_env),
            self._check_bool("schedule_active", schedule.get("status") == "active", "runner, trading plan, evening review, daily review, strategies, and dashboard are installed and loaded.", schedule),
            self._check_bool("mock_runtime", mock.get("mock_ready") and mock.get("mock_running"), "Mock trading loop is healthy and fresh.", mock),
            self._check_bool("journal_review", self._journal_artifacts_exist(run_date), "Daily journal/review artifacts exist.", {"run_date": run_date}),
            self._check_bool("broker_provider_configured", str(self.config.get("broker", {}).get("provider", "")) in supported_providers, "Supported live broker provider is configured.", {"provider": self.config.get("broker", {}).get("provider", ""), "supported": sorted(supported_providers)}),
        ]
        dry_run_ready = all(item["status"] == "pass" for item in checks)
        real_money_checks = [
            *checks,
            self._check_bool("execution_live", str(self.config.get("execution_mode", "paper")).lower() == "live" and bool(self.config.get("live_trading_enabled")), "execution_mode=live and live_trading_enabled=true.", {"execution_mode": self.config.get("execution_mode"), "live_trading_enabled": self.config.get("live_trading_enabled")}),
            self._check_bool("broker_not_dry_run", bool(broker.get("ready")) and broker.get("dry_run") is False, "Broker preflight is ready and dry_run=false.", broker),
            self._check_bool("live_readiness", live_readiness.get("live_ready") is True, "All live readiness checks pass.", live_readiness),
            self._check_bool("human_approval", approval.get("approved") is True, "Human approval artifact exists for this run date.", approval),
        ]
        real_money_ready = all(item["status"] == "pass" for item in real_money_checks)
        status = "real_money_ready" if real_money_ready else ("dry_run_ready" if dry_run_ready else "blocked")
        payload = {
            "run_date": run_date,
            "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": status,
            "dry_run_ready": dry_run_ready,
            "real_money_ready": real_money_ready,
            "checks": checks,
            "real_money_checks": real_money_checks,
            "next_actions": self._next_actions(checks, real_money_checks),
            "approval_path": str(self._approval_path(run_date)),
            "approval": approval,
        }
        write_json(self.output_root / "live_activation" / "current.json", [payload])
        write_json(self.output_root / "live_activation" / f"{run_date}.json", [payload])
        return payload

    def _latest(self, name: str, run_date: str) -> dict:
        rows = load_json(self.output_root / name / f"{run_date}.json")
        if not rows:
            rows = load_json(self.output_root / name / "current.json")
        return rows[-1] if rows else {}

    def _journal_artifacts_exist(self, run_date: str) -> bool:
        required = [
            self.output_root / "journals" / f"{run_date}.md",
            self.output_root / "review_notes" / f"{run_date}.md",
            self.output_root / "reports" / f"{run_date}.md",
            self.output_root / "strategy_reviews" / f"{run_date}.json",
            self.output_root / "learning_ledger" / f"{run_date}.json",
            self.output_root / "strategy_change_proposals" / f"{run_date}.json",
        ]
        return all(path.exists() for path in required)

    def _approval_path(self, run_date: str) -> Path:
        return self.output_root / "live_approvals" / f"{run_date}.approved.json"

    def _check_bool(self, name: str, passed: bool, summary: str, evidence: dict) -> dict:
        return {"name": name, "status": "pass" if passed else "fail", "summary": summary, "evidence": evidence}

    def _next_actions(self, dry_checks: list[dict], real_checks: list[dict]) -> list[str]:
        actions = []
        by_name = {item["name"]: item for item in [*dry_checks, *real_checks] if item["status"] != "pass"}
        if "official_5m_data" in by_name:
            actions.append("Connect execution-grade XAUUSD/XAUUSDT 5m bars, such as Binance USDM, until the market-data gate passes.")
        if "live_env" in by_name:
            actions.append("Copy configs/live.env.template to configs/live.env and fill the active broker keys locally.")
        if "broker_provider_configured" in by_name:
            actions.append("Set broker.provider to binance_usdm, oanda_rest, or mt5_file_bridge only after dry-run readiness is satisfied.")
        if "execution_live" in by_name:
            actions.append("Keep execution_mode=paper until dry_run_ready is true and you intentionally switch to live.")
        if "broker_not_dry_run" in by_name:
            actions.append("Keep broker.dry_run=true until live_readiness passes and a dated approval artifact exists.")
        if "human_approval" in by_name:
            actions.append("Create a dated approval artifact only after reviewing Dashboard, journal, risk, and broker state.")
        return actions or ["Live activation gate is clear."]
