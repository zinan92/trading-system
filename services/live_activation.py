from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json
from services.live_env import LiveEnvStatus
from services.live_approval import LiveApprovalStore
from services.official_market_data_gate import official_broker_ohlc_status
from services.schedule_profiles import is_focus_profile
from services.live_activation_gate import LiveActivationGate as SourceBoundLiveActivationGate


class LiveActivationGate(SourceBoundLiveActivationGate):
    """Compatibility runner plus the source-bound attended activation protocol.

    ``run`` preserves the repository's legacy read-only readiness artifact for
    existing callers.  The inherited ``preflight``/``prepare_activation``/
    ``confirm`` methods are the only source-bound Hyperliquid Live activation
    protocol; neither path enables network writes.
    """

    def __init__(self, output_root: Path | None = None, *, park_user_id: str = "") -> None:
        config = load_pipeline_config()
        self.config = config
        self.output_root = output_root or Path(os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(ROOT / config.get("output_root", "outputs"))))
        super().__init__(self.output_root, park_user_id=park_user_id, repo_root=ROOT)

    def run(self, run_date: str) -> dict:
        live_env = LiveEnvStatus(self.output_root).run(run_date)
        live_readiness = self._latest("live_readiness", run_date)
        approval = LiveApprovalStore(self.output_root).status(run_date)
        schedule = (load_json(self.output_root / "schedules" / "status_current.json") or [{}])[-1]
        mock = (load_json(self.output_root / "mock_runtime" / "current.json") or [{}])[-1]
        broker = (load_json(self.output_root / "broker_preflight" / "current.json") or [{}])[-1]
        provider = self._configured_provider(broker)
        data_source = self._market_data_preflight(self._market_data_identity(provider), run_date)
        supported_providers = {"oanda_rest", "mt5_file_bridge", "binance_usdm", "tiger_openapi"}
        official_data_ready, official_data_summary = official_broker_ohlc_status(data_source)
        checks = [
            self._check_bool("official_5m_data", official_data_ready, official_data_summary, data_source),
            self._check_bool("live_env", live_env.get("status") == "pass", "Required broker env keys are present.", live_env),
            self._check_bool("schedule_active", schedule.get("status") == "active", self._schedule_summary(schedule), schedule),
            self._check_bool("mock_runtime", mock.get("mock_ready") and mock.get("mock_running"), "Mock trading loop is healthy and fresh.", mock),
            self._check_bool("journal_review", self._journal_artifacts_exist(run_date), "Daily journal/review artifacts exist.", {"run_date": run_date}),
            self._check_bool("broker_provider_configured", provider in supported_providers, "Supported live broker provider is configured.", {"provider": provider, "supported": sorted(supported_providers)}),
        ]
        dry_run_ready = all(item["status"] == "pass" for item in checks)
        real_money_checks = [
            *checks,
            self._check_bool("execution_live", str(self.config.get("execution_mode", "paper")).lower() == "live" and bool(self.config.get("live_trading_enabled")), "execution_mode=live and live_trading_enabled=true.", {"execution_mode": self.config.get("execution_mode"), "live_trading_enabled": self.config.get("live_trading_enabled")}),
            self._check_bool("broker_not_dry_run", bool(broker.get("ready")) and broker.get("dry_run") is False, "Broker preflight is ready and dry_run=false.", broker),
            self._check_bool("live_readiness", live_readiness.get("live_ready") is True, "All live readiness checks pass.", live_readiness),
            self._check_bool("human_approval", approval.get("approved") is True, "Human approval artifact exists for this run date.", approval),
            self._source_bound_canary_check(),
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

    def _source_bound_canary_check(self) -> dict:
        """Make the source-bound attended protocol the only Live authority.

        The legacy ``run`` artifact is still emitted for old read models, but
        it can never become ``real_money_ready`` from timer/provider checks.
        A future attended canary must append a source-bound ``canary_passed``
        receipt to the inherited journal before this check can pass.
        """

        rows = self.rows()
        confirmed = next((row for row in reversed(rows) if row.get("event") == "activation_confirmed"), None)
        canary = next(
            (
                row
                for row in reversed(rows)
                if row.get("event") == "canary_passed"
                and confirmed
                and row.get("activation_digest") == confirmed.get("activation_digest")
                and row.get("execution_authorized") is True
                and row.get("live_writes_enabled") is True
            ),
            None,
        )
        passed = bool(confirmed and canary)
        return self._check_bool(
            "source_bound_attended_canary",
            passed,
            "Only a source-bound Telegram activation followed by an attended canary may authorize Live writes.",
            {
                "activation_digest": confirmed.get("activation_digest") if confirmed else None,
                "canary_event": canary.get("event") if canary else None,
                "live_writes_enabled": bool(canary and canary.get("live_writes_enabled") is True),
            },
        )

    def _latest(self, name: str, run_date: str) -> dict:
        rows = load_json(self.output_root / name / f"{run_date}.json")
        if not rows:
            rows = load_json(self.output_root / name / "current.json")
        return rows[-1] if rows else {}

    def _configured_provider(self, broker: dict) -> str:
        if broker.get("provider"):
            return str(broker.get("provider"))
        broker_config = dict(self.config.get("broker", {}) or {})
        profile_name = str(self.config.get("broker_profile") or broker_config.get("broker_profile") or broker_config.get("profile") or "").strip()
        if profile_name:
            profile = dict((self.config.get("broker_profiles", {}) or {}).get(profile_name, {}) or {})
            if profile.get("provider"):
                return str(profile.get("provider"))
        return str(broker_config.get("provider", ""))

    def _market_data_identity(self, provider: str) -> dict:
        if provider == "tiger_openapi":
            feed = self.config.get("tiger_futures_feed", {}) or {}
            symbol = str(feed.get("output_symbol") or feed.get("contract") or "MGCmain")
            timeframe = str(feed.get("timeframe") or feed.get("period") or "1m")
            return {"provider": provider, "symbol": symbol, "timeframe": timeframe, "source_key": f"{symbol}_{timeframe}"}
        return {"provider": provider, "symbol": "GOLD", "timeframe": "5m", "source_key": "GOLD_5m"}

    def _market_data_preflight(self, identity: dict, run_date: str) -> dict:
        source_key = str(identity.get("source_key") or "GOLD_5m")
        if source_key != "GOLD_5m":
            for path in [
                self.output_root / "data_source_preflight" / source_key / f"{run_date}.json",
                self.output_root / "data_source_preflight" / source_key / "current.json",
            ]:
                rows = load_json(path)
                if rows:
                    return rows[-1]
        return self._latest("data_source_preflight", run_date)

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

    def _schedule_summary(self, schedule: dict) -> str:
        profile = str(schedule.get("profile") or "")
        if is_focus_profile(profile):
            return "dualtrack focus schedule is active: dashboard, dualtrack cycle/live tick, GOLD 1m feed, and deadman ping are installed and loaded."
        return "full schedule is active: runner, trading plan, evening review, daily review, strategies, dashboard, dualtrack cycle/live tick, and deadman ping are installed and loaded."

    def _next_actions(self, dry_checks: list[dict], real_checks: list[dict]) -> list[str]:
        actions = []
        by_name = {item["name"]: item for item in [*dry_checks, *real_checks] if item["status"] != "pass"}
        if "official_5m_data" in by_name:
            actions.append("Connect execution-grade broker or execution-venue OHLC bars for the active provider until the market-data gate passes.")
        if "live_env" in by_name:
            actions.append("Copy configs/live.env.template to configs/live.env and fill the active broker keys locally.")
        if "broker_provider_configured" in by_name:
            actions.append("Set broker.provider to binance_usdm, oanda_rest, mt5_file_bridge, or tiger_openapi only after dry-run readiness is satisfied.")
        if "execution_live" in by_name:
            actions.append("Keep execution_mode=paper until dry_run_ready is true and you intentionally switch to live.")
        if "broker_not_dry_run" in by_name:
            actions.append("Keep broker.dry_run=true until live_readiness passes and a dated approval artifact exists.")
        if "human_approval" in by_name:
            actions.append("Create a dated approval artifact only after reviewing Dashboard, journal, risk, and broker state.")
        return actions or ["Live activation gate is clear."]
