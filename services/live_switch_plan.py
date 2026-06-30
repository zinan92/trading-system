from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json
from services.official_market_data_gate import is_official_broker_ohlc_ready


class LiveSwitchPlan:
    def __init__(self, output_root: Path | None = None) -> None:
        config = load_pipeline_config()
        self.output_root = output_root or Path(os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(ROOT / config.get("output_root", "outputs"))))

    def run(self, run_date: str) -> dict:
        data_source = self._latest("data_source_preflight", run_date)
        live_env = self._latest("live_env", run_date)
        oanda_account = self._latest("oanda_account", run_date)
        broker = self._latest("broker_preflight", None)
        live_readiness = self._latest("live_readiness", run_date)
        activation = self._latest("live_activation", run_date)
        schedule = self._latest("schedules", "status_current", dated=False)
        steps = self._steps(data_source, live_env, oanda_account, broker, live_readiness, activation, schedule)
        payload = {
            "run_date": run_date,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": "ready" if all(step["status"] == "done" for step in steps) else "blocked",
            "dry_run_ready": bool(activation.get("dry_run_ready")),
            "real_money_ready": bool(activation.get("real_money_ready")),
            "steps": steps,
            "commands": self._commands(run_date),
            "safety_order": [
                "Keep execution_mode=paper until execution-grade GOLD/XAUUSD 5m data is live-ready.",
                "Use broker dry_run first; do not disable dry_run before live_readiness passes.",
                "Require a dated human approval artifact before real-money execution.",
            ],
            "artifacts": {
                "data_source_preflight": str(self.output_root / "data_source_preflight" / "current.json"),
                "live_env": str(self.output_root / "live_env" / "current.json"),
                "oanda_account": str(self.output_root / "oanda_account" / "current.json"),
                "live_readiness": str(self.output_root / "live_readiness" / "current.json"),
                "live_activation": str(self.output_root / "live_activation" / "current.json"),
            },
        }
        write_json(self.output_root / "live_switch_plan" / "current.json", [payload])
        write_json(self.output_root / "live_switch_plan" / f"{run_date}.json", [payload])
        self._write_markdown(run_date, payload)
        return payload

    def _steps(self, data_source: dict, live_env: dict, oanda_account: dict, broker: dict, live_readiness: dict, activation: dict, schedule: dict) -> list[dict]:
        provider = str(broker.get("provider") or "")
        oanda_required = provider == "oanda_rest"
        return [
            self._step("schedule", schedule.get("status") == "active", "launchd runner, trading plan, evening review, daily review, strategies, and dashboard are active.", schedule),
            self._step("official_5m_data", is_official_broker_ohlc_ready(data_source), "Connect execution-grade Binance USDM or broker GOLD 5m bars until OHLC is live-ready.", data_source),
            self._step("live_env", live_env.get("status") == "pass", "Create configs/live.env with the active broker keys.", live_env),
            self._step("oanda_account", (not oanda_required) or (oanda_account.get("status") == "pass" and oanda_account.get("instrument_ready") is True), "Confirm OANDA account and XAU_USD instrument are reachable only when OANDA is the active broker.", {"provider": provider, "oanda_account": oanda_account}),
            self._step("broker_provider", broker.get("provider") in {"binance_usdm", "oanda_rest", "mt5_file_bridge"} and broker.get("ready") is True, "Configure a supported live broker provider and make broker_preflight pass.", broker),
            self._step("live_readiness", live_readiness.get("live_ready") is True, "Make all live_readiness checks pass before disabling broker dry_run.", live_readiness),
            self._step("human_approval", activation.get("approval", {}).get("approved") is True or activation.get("real_money_ready") is True, "Create dated human approval only after dry-run readiness is proven.", activation),
        ]

    def _step(self, name: str, done: bool, action: str, evidence: dict) -> dict:
        return {
            "name": name,
            "status": "done" if done else "blocked",
            "action": action,
            "summary": evidence.get("message") or evidence.get("status") or evidence.get("summary") or "",
            "evidence": evidence,
        }

    def _commands(self, run_date: str) -> list[str]:
        return [
            f"python3 -m pipelines.live_readiness --date {run_date} --json",
            f"python3 -m pipelines.live_activation --date {run_date}",
            f"python3 -m pipelines.live_approval --date {run_date} --request",
        ]

    def _write_markdown(self, run_date: str, payload: dict) -> None:
        lines = [
            f"# Live Switch Plan - {run_date}",
            "",
            f"- Status: {payload['status']}",
            f"- Dry-run ready: {payload['dry_run_ready']}",
            f"- Real-money ready: {payload['real_money_ready']}",
            "",
            "## Steps",
        ]
        for step in payload["steps"]:
            lines.append(f"- [{step['status']}] {step['name']}: {step['action']}")
        lines.extend(["", "## Commands"])
        for command in payload["commands"]:
            lines.append(f"- `{command}`")
        lines.extend(["", "## Safety Order"])
        for item in payload["safety_order"]:
            lines.append(f"- {item}")
        path = self.output_root / "live_switch_plan" / f"{run_date}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _latest(self, folder: str, key: str | None, dated: bool = True) -> dict:
        if not dated:
            rows = load_json(self.output_root / folder / f"{key}.json")
            return rows[-1] if rows else {}
        if key:
            rows = load_json(self.output_root / folder / f"{key}.json")
            if rows:
                return rows[-1]
        rows = load_json(self.output_root / folder / "current.json")
        return rows[-1] if rows else {}


def run_live_switch_plan(run_date: str, output_root: Path | None = None) -> dict:
    return LiveSwitchPlan(output_root).run(run_date)
