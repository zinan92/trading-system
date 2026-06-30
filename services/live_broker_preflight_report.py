from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from services.broker_adapter import broker_preflight
from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json
from services.live_env import LiveEnvStatus
from services.oanda_account_preflight import OandaAccountPreflight


class LiveBrokerPreflightReport:
    def __init__(self, output_root: Path | None = None) -> None:
        config = load_pipeline_config()
        self.config = config
        self.output_root = output_root or ROOT / config.get("output_root", "outputs")

    def run(self, run_date: str) -> dict:
        live_env = LiveEnvStatus(self.output_root).run(run_date)
        oanda_account = OandaAccountPreflight(output_root=self.output_root).run(run_date)
        broker = broker_preflight(self.output_root)
        activation = self._latest("live_activation", run_date)
        readiness = self._latest("live_readiness", run_date)
        safety = self._latest("live_submission_safety", run_date)
        checks = [
            self._live_env_check(live_env),
            self._broker_provider_check(broker),
            self._broker_credential_check(broker),
            self._oanda_account_check(broker, oanda_account),
            self._dry_run_check(broker),
            self._activation_check(activation),
            self._submission_safety_check(safety),
        ]
        payload = {
            "run_date": run_date,
            "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": self._rollup(checks),
            "provider": broker.get("provider", self.config.get("broker", {}).get("provider", "manual_gateway")),
            "execution_mode": self.config.get("execution_mode", "paper"),
            "live_trading_enabled": bool(self.config.get("live_trading_enabled", False)),
            "dry_run": broker.get("dry_run", True),
            "real_submit_blocked": activation.get("real_money_ready") is not True or safety.get("network_call_attempted") is not False,
            "checks": checks,
            "summary": {
                "passed": sum(1 for item in checks if item["status"] == "pass"),
                "warned": sum(1 for item in checks if item["status"] == "warn"),
                "failed": sum(1 for item in checks if item["status"] == "fail"),
                "missing_env": live_env.get("missing_keys", []) or broker.get("missing_env", []),
                "env_file_exists": live_env.get("env_file_exists", False),
                "secret_hygiene": (live_env.get("secret_hygiene") or {}).get("status", "unknown"),
                "oanda_account_ready": oanda_account.get("account_ready", False),
                "oanda_instrument_ready": oanda_account.get("instrument_ready", False),
                "activation_status": activation.get("status", "unknown"),
                "readiness_status": readiness.get("status", "unknown"),
                "network_call_attempted": safety.get("network_call_attempted"),
            },
            "source_artifacts": {
                "live_env": str(self.output_root / "live_env" / f"{run_date}.json"),
                "oanda_account": str(self.output_root / "oanda_account" / f"{run_date}.json"),
                "broker_preflight": str(self.output_root / "broker_preflight" / "current.json"),
                "live_activation": str(self.output_root / "live_activation" / f"{run_date}.json"),
                "live_readiness": str(self.output_root / "live_readiness" / f"{run_date}.json"),
                "live_submission_safety": str(self.output_root / "live_submission_safety" / f"{run_date}.json"),
            },
        }
        write_json(self.output_root / "live_broker_preflight" / f"{run_date}.json", [payload])
        write_json(self.output_root / "live_broker_preflight" / "current.json", [payload])
        self._write_markdown(run_date, payload)
        return payload

    def _live_env_check(self, live_env: dict) -> dict:
        if live_env.get("status") == "pass":
            return self._check("live_env", "pass", "Required broker env keys are present and values are not exposed.", live_env)
        return self._check("live_env", "fail", "Required broker env keys are missing or secret hygiene has warnings.", live_env)

    def _broker_provider_check(self, broker: dict) -> dict:
        provider = str(broker.get("provider", ""))
        if provider in {"binance_usdm", "oanda_rest", "mt5_file_bridge"}:
            return self._check("broker_provider", "pass", f"Broker provider is {provider}.", broker)
        return self._check("broker_provider", "fail", "Broker provider is not configured for live execution.", broker)

    def _broker_credential_check(self, broker: dict) -> dict:
        missing = broker.get("missing_env") or []
        if not missing and broker.get("ready"):
            return self._check("broker_credentials", "pass", "Broker credential preflight is ready.", broker)
        return self._check("broker_credentials", "fail", "Broker credentials or bridge paths are not ready.", broker)

    def _oanda_account_check(self, broker: dict, oanda_account: dict) -> dict:
        provider = str(broker.get("provider", ""))
        if provider != "oanda_rest":
            return self._check("oanda_account", "pass", "OANDA account is not required for the active broker provider.", {"provider": provider, "oanda_account": oanda_account})
        if oanda_account.get("account_ready") and oanda_account.get("instrument_ready"):
            return self._check("oanda_account", "pass", "OANDA account and XAU_USD instrument are reachable.", oanda_account)
        return self._check("oanda_account", "fail", "OANDA account or XAU_USD instrument is not reachable.", oanda_account)

    def _dry_run_check(self, broker: dict) -> dict:
        if broker.get("dry_run") is True:
            return self._check("dry_run", "warn", "Broker dry_run remains enabled; real submission is blocked.", broker)
        return self._check("dry_run", "pass", "Broker dry_run is disabled; real submission still requires activation gate.", broker)

    def _activation_check(self, activation: dict) -> dict:
        if activation.get("real_money_ready") is True:
            return self._check("activation_gate", "pass", "Live activation gate is real_money_ready.", activation)
        return self._check("activation_gate", "fail", "Live activation gate blocks real-money submission.", activation)

    def _submission_safety_check(self, safety: dict) -> dict:
        if safety.get("status") == "pass" and safety.get("blocked_by_activation_gate") is True and safety.get("network_call_attempted") is False:
            return self._check("submission_safety", "pass", "Safety smoke proves real submit is blocked before any network call.", safety)
        return self._check("submission_safety", "fail", "Live submission safety smoke is missing or unsafe.", safety)

    def _latest(self, folder: str, run_date: str) -> dict:
        dated = load_json(self.output_root / folder / f"{run_date}.json")
        current = load_json(self.output_root / folder / "current.json")
        if dated:
            return dated[-1]
        return current[-1] if current else {}

    def _rollup(self, checks: list[dict]) -> str:
        states = {item["status"] for item in checks}
        if "fail" in states:
            return "blocked"
        if "warn" in states:
            return "dry_run_only"
        return "ready"

    def _check(self, name: str, status: str, summary: str, evidence: dict) -> dict:
        return {"name": name, "status": status, "summary": summary, "evidence": evidence}

    def _write_markdown(self, run_date: str, payload: dict) -> None:
        lines = [
            f"# Live Broker Preflight - {run_date}",
            "",
            f"- Status: {payload['status']}",
            f"- Provider: {payload['provider']}",
            f"- Execution mode: {payload['execution_mode']}",
            f"- Live trading enabled: {payload['live_trading_enabled']}",
            f"- Dry run: {payload['dry_run']}",
            f"- Real submit blocked: {payload['real_submit_blocked']}",
            "",
            "## Checks",
        ]
        for item in payload["checks"]:
            lines.append(f"- {item['status']}: {item['name']} - {item['summary']}")
        path = self.output_root / "live_broker_preflight" / f"{run_date}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_live_broker_preflight_report(run_date: str, output_root: Path | None = None) -> dict:
    return LiveBrokerPreflightReport(output_root).run(run_date)
