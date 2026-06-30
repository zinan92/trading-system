from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json
from services.live_activation import LiveActivationGate
from services.live_approval import LiveApprovalStore
from services.live_cutover_package import LiveCutoverPackage
from services.live_readiness import LiveReadiness
from services.live_submission_safety import LiveSubmissionSafetySmoke
from services.live_switch_plan import LiveSwitchPlan
from services.official_market_data_gate import is_official_broker_ohlc_ready


class LiveDryRunDrill:
    def __init__(self, output_root: Path | None = None, market_db: Path | None = None) -> None:
        config = load_pipeline_config()
        self.config = config
        self.output_root = output_root or ROOT / config.get("output_root", "outputs")
        self.market_db = market_db or ROOT / config.get("local_market_db", "data/market_data.db")

    def run(self, run_date: str, refresh_dependencies: bool = True) -> dict:
        if refresh_dependencies:
            readiness = LiveReadiness(self.output_root, self.market_db).run(run_date)
            switch_plan = LiveSwitchPlan(self.output_root).run(run_date)
            activation = LiveActivationGate(self.output_root).run(run_date)
            approval = LiveApprovalStore(self.output_root).status(run_date)
            submission_safety = LiveSubmissionSafetySmoke(self.output_root).run(run_date)
            cutover = LiveCutoverPackage(self.output_root, self.market_db).run(run_date)
        else:
            readiness = self._latest("live_readiness", run_date)
            switch_plan = self._latest("live_switch_plan", run_date)
            activation = self._latest("live_activation", run_date)
            approval = self._latest("live_approvals", run_date)
            submission_safety = self._latest("live_submission_safety", run_date)
            cutover = self._latest("live_cutover", run_date)

        broker = self._latest("broker_preflight", run_date)
        data_source = self._latest("data_source_preflight", run_date)
        data_lineage = self._latest("data_source_lineage", run_date)
        risk_monitor = self._latest("risk_monitor", run_date)
        operation_runbook = self._latest("operation_runbooks", run_date)
        phases = self._phases(readiness, activation, approval, submission_safety, broker, data_source, data_lineage, risk_monitor, operation_runbook)
        blockers = [item for item in phases if item["status"] != "pass"]
        real_money_ready = self._real_money_ready(readiness, activation, approval, submission_safety, blockers)
        dry_run_ready = bool(activation.get("dry_run_ready")) and self._submission_safety_ok(submission_safety)
        status = "real_money_ready" if real_money_ready else ("dry_run_ready" if dry_run_ready else "blocked")
        payload = {
            "run_date": run_date,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": status,
            "dry_run_ready": dry_run_ready,
            "real_money_ready": real_money_ready,
            "safe_to_submit_live_order": real_money_ready,
            "network_call_attempted": bool(submission_safety.get("network_call_attempted")),
            "submission_safety_status": submission_safety.get("status", ""),
            "blocked_by_activation_gate": bool(submission_safety.get("blocked_by_activation_gate")),
            "data_truth_level": data_lineage.get("truth_level", "unknown"),
            "latest_price": data_source.get("latest_price"),
            "latest_provider": data_source.get("latest_provider", ""),
            "official_rows": data_source.get("official_rows", 0),
            "broker_provider": broker.get("provider") or self.config.get("broker", {}).get("provider", ""),
            "execution_mode": self.config.get("execution_mode", "paper"),
            "broker_dry_run": broker.get("dry_run", True),
            "live_trading_enabled": bool(self.config.get("live_trading_enabled", False)),
            "phases": phases,
            "blockers": blockers,
            "readiness_status": readiness.get("status", ""),
            "activation_status": activation.get("status", ""),
            "cutover_status": cutover.get("status", ""),
            "approval_status": approval.get("status", ""),
            "required_external_inputs": self._required_external_inputs(cutover),
            "next_commands": self._next_commands(run_date),
            "evidence_paths": self._evidence_paths(run_date),
            "safety_invariants": [
                "safe_to_submit_live_order must remain false unless real_money_ready is true.",
                "Public gold-api.com data is allowed for paper trading only.",
                "The live submission safety smoke must not attempt a broker network call while activation is blocked.",
                "Human approval is a dated local artifact, not a chat message.",
            ],
        }
        write_json(self.output_root / "live_dry_run_drill" / "current.json", [payload])
        write_json(self.output_root / "live_dry_run_drill" / f"{run_date}.json", [payload])
        self._write_markdown(run_date, payload)
        return payload

    def _phases(
        self,
        readiness: dict,
        activation: dict,
        approval: dict,
        submission_safety: dict,
        broker: dict,
        data_source: dict,
        data_lineage: dict,
        risk_monitor: dict,
        operation_runbook: dict,
    ) -> list[dict]:
        readiness_checks = {item.get("name"): item for item in readiness.get("checks", [])}
        activation_checks = {item.get("name"): item for item in activation.get("real_money_checks", [])}
        dry_checks = {item.get("name"): item for item in activation.get("checks", [])}
        data_source_gate = {**data_source, "truth_level": data_lineage.get("truth_level", data_source.get("truth_level", ""))}
        phases = [
            self._phase(
                "official_data",
                "pass" if is_official_broker_ohlc_ready(data_source_gate) else "fail",
                "Official GOLD/XAUUSD 5m broker data is required for live.",
                {
                    "truth_level": data_lineage.get("truth_level", "unknown"),
                    "official_rows": data_source.get("official_rows", 0),
                    "latest_provider": data_source.get("latest_provider", ""),
                    "readiness_check": readiness_checks.get("official_market_data", {}),
                },
            ),
            self._phase(
                "live_env",
                self._check_status(readiness_checks.get("live_env") or activation_checks.get("live_env") or dry_checks.get("live_env")),
                "Broker secrets and local live env must be present without exposing values.",
                readiness_checks.get("live_env") or activation_checks.get("live_env") or dry_checks.get("live_env") or {},
            ),
            self._phase(
                "broker_provider",
                self._check_status(readiness_checks.get("broker_provider") or activation_checks.get("broker_provider_configured") or dry_checks.get("broker_provider_configured")),
                "Supported broker provider must be configured and preflight-ready.",
                {
                    "provider": broker.get("provider") or self.config.get("broker", {}).get("provider", ""),
                    "broker_ready": broker.get("ready"),
                    "broker_dry_run": broker.get("dry_run"),
                    "check": readiness_checks.get("broker_provider") or activation_checks.get("broker_provider_configured") or dry_checks.get("broker_provider_configured") or {},
                },
            ),
            self._phase(
                "broker_feedback",
                self._check_status(readiness_checks.get("broker_feedback")),
                "Broker feed/receipt loop must have successful evidence before live.",
                readiness_checks.get("broker_feedback") or {},
            ),
            self._phase(
                "risk_and_runner",
                self._risk_runner_status(readiness_checks, risk_monitor, operation_runbook),
                "Runner, risk monitor, and operation runbook must allow trading under the configured boundaries.",
                {
                    "runner": readiness_checks.get("runner", {}),
                    "risk_rules": readiness_checks.get("risk_rules", {}),
                    "risk_monitor_status": risk_monitor.get("status", ""),
                    "kill_switch_active": risk_monitor.get("kill_switch_active"),
                    "operation_status": operation_runbook.get("status", ""),
                },
            ),
            self._phase(
                "journal_review",
                self._check_status(readiness_checks.get("journal_review") or activation_checks.get("journal_review") or dry_checks.get("journal_review")),
                "Daily report, journal, review notes, and learning artifacts must exist.",
                readiness_checks.get("journal_review") or activation_checks.get("journal_review") or dry_checks.get("journal_review") or {},
            ),
            self._phase(
                "submission_safety",
                "pass" if self._submission_safety_ok(submission_safety) else "fail",
                "Safety smoke must prove activation blocks live submission and no network call was attempted.",
                submission_safety,
            ),
            self._phase(
                "human_approval",
                "pass" if approval.get("approved") is True else "fail",
                "A dated human approval artifact is required before any real-money order.",
                approval,
            ),
        ]
        return phases

    def _risk_runner_status(self, checks: dict, risk_monitor: dict, operation_runbook: dict) -> str:
        runner_ok = self._check_status(checks.get("runner")) == "pass"
        rules_ok = self._check_status(checks.get("risk_rules")) == "pass"
        risk_ok = risk_monitor.get("status") in {"pass", "warn", "block", ""}
        kill_switch_ok = not bool(risk_monitor.get("kill_switch_active"))
        operation_ok = operation_runbook.get("status") in {"paper_auto_ready", "paper_manual_only", "live_ready", "blocked", ""}
        return "pass" if runner_ok and rules_ok and risk_ok and kill_switch_ok and operation_ok else "fail"

    def _real_money_ready(self, readiness: dict, activation: dict, approval: dict, submission_safety: dict, blockers: list[dict]) -> bool:
        return (
            readiness.get("live_ready") is True
            and activation.get("real_money_ready") is True
            and approval.get("approved") is True
            and self._submission_safety_ok(submission_safety)
            and not blockers
        )

    def _submission_safety_ok(self, submission_safety: dict) -> bool:
        return (
            submission_safety.get("status") == "pass"
            and submission_safety.get("blocked_by_activation_gate") is True
            and not bool(submission_safety.get("network_call_attempted"))
        )

    def _check_status(self, check: dict | None) -> str:
        return "pass" if (check or {}).get("status") == "pass" else "fail"

    def _phase(self, name: str, status: str, summary: str, evidence: dict) -> dict:
        return {"name": name, "status": status, "summary": summary, "evidence": evidence}

    def _required_external_inputs(self, cutover: dict) -> list[dict]:
        return cutover.get("required_external_inputs") or [
            {"name": "official_gold_5m_feed", "required_for": "live", "proof": "official_rows>0 and live_data_mode=official_broker"},
            {"name": "broker_credentials", "required_for": "live", "secret": True, "proof": "live_env.status=pass"},
            {"name": "human_approval", "required_for": "live", "proof": "live_approvals/YYYY-MM-DD.approved.json"},
        ]

    def _next_commands(self, run_date: str) -> list[str]:
        return [
            f"python3 -m pipelines.live_dry_run_drill --date {run_date} --json",
            f"python3 -m pipelines.import_official_feed --date {run_date}",
            f"python3 -m pipelines.live_readiness --date {run_date} --json",
            f"python3 -m pipelines.live_activation --date {run_date}",
            f"python3 -m pipelines.live_approval --date {run_date} --action request --notes \"review dashboard, journal, risk, broker state\"",
        ]

    def _evidence_paths(self, run_date: str) -> dict:
        return {
            "live_dry_run_drill": str(self.output_root / "live_dry_run_drill" / f"{run_date}.json"),
            "live_readiness": str(self.output_root / "live_readiness" / f"{run_date}.json"),
            "live_activation": str(self.output_root / "live_activation" / f"{run_date}.json"),
            "live_submission_safety": str(self.output_root / "live_submission_safety" / f"{run_date}.json"),
            "live_cutover": str(self.output_root / "live_cutover" / f"{run_date}.json"),
            "live_approvals": str(self.output_root / "live_approvals" / f"{run_date}.json"),
            "data_source_preflight": str(self.output_root / "data_source_preflight" / f"{run_date}.json"),
            "data_source_lineage": str(self.output_root / "data_source_lineage" / f"{run_date}.json"),
        }

    def _write_markdown(self, run_date: str, payload: dict) -> None:
        lines = [
            f"# Live Dry-Run Drill - {run_date}",
            "",
            f"- Status: {payload['status']}",
            f"- Dry-run ready: {payload['dry_run_ready']}",
            f"- Real-money ready: {payload['real_money_ready']}",
            f"- Safe to submit live order: {payload['safe_to_submit_live_order']}",
            f"- Network call attempted: {payload['network_call_attempted']}",
            f"- Latest price: {payload.get('latest_price', 'n/a')} from {payload.get('latest_provider') or 'n/a'}",
            f"- Data truth: {payload.get('data_truth_level', 'unknown')} / official_rows={payload.get('official_rows', 0)}",
            "",
            "## Phases",
        ]
        for item in payload["phases"]:
            lines.append(f"- {item['status']}: {item['name']} - {item['summary']}")
        lines.extend(["", "## Blockers"])
        if payload["blockers"]:
            for item in payload["blockers"]:
                lines.append(f"- {item['name']}: {item['summary']}")
        else:
            lines.append("- none")
        lines.extend(["", "## Next Commands"])
        lines.extend([f"- `{command}`" for command in payload["next_commands"]])
        path = self.output_root / "live_dry_run_drill" / f"{run_date}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _latest(self, folder: str, run_date: str) -> dict:
        rows = load_json(self.output_root / folder / f"{run_date}.json")
        if not rows:
            rows = load_json(self.output_root / folder / "current.json")
        return rows[-1] if rows else {}


def run_live_dry_run_drill(run_date: str, output_root: Path | None = None, market_db: Path | None = None, refresh_dependencies: bool = True) -> dict:
    return LiveDryRunDrill(output_root, market_db).run(run_date, refresh_dependencies=refresh_dependencies)
