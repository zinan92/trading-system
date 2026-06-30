from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json


class LiveCutoverPackage:
    def __init__(self, output_root: Path | None = None, market_db: Path | None = None) -> None:
        config = load_pipeline_config()
        self.config = config
        self.output_root = output_root or ROOT / config.get("output_root", "outputs")
        self.market_db = market_db or ROOT / config.get("local_market_db", "data/market_data.db")

    def run(self, run_date: str) -> dict:
        readiness = self._latest("live_readiness", run_date)
        activation = self._latest("live_activation", run_date)
        switch_plan = self._latest("live_switch_plan", run_date)
        blockers = self._blockers(readiness, activation, switch_plan)
        status = self._status(readiness, activation, blockers)
        payload = {
            "run_date": run_date,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": status,
            "real_money_ready": bool(activation.get("real_money_ready")),
            "dry_run_ready": bool(activation.get("dry_run_ready")),
            "live_ready": bool(readiness.get("live_ready")),
            "execution_mode": self.config.get("execution_mode", "paper"),
            "live_trading_enabled": bool(self.config.get("live_trading_enabled", False)),
            "broker_provider": self.config.get("broker", {}).get("provider", ""),
            "blockers": blockers,
            "required_external_inputs": self._required_external_inputs(),
            "preflight_commands": self._preflight_commands(run_date),
            "cutover_sequence": self._cutover_sequence(run_date),
            "rollback_plan": self._rollback_plan(run_date),
            "evidence_paths": self._evidence_paths(run_date),
            "safety_invariants": [
                "Never place real-money orders unless live_activation.real_money_ready is true.",
                "Keep broker.dry_run=true until live_readiness passes and a dated approval artifact exists.",
                "Do not use public gold-api.com data for real-money execution.",
                "Every live order must have a broker receipt or explicit broker feedback artifact.",
            ],
        }
        write_json(self.output_root / "live_cutover" / "current.json", [payload])
        write_json(self.output_root / "live_cutover" / f"{run_date}.json", [payload])
        self._write_markdown(run_date, payload)
        return payload

    def _blockers(self, readiness: dict, activation: dict, switch_plan: dict) -> list[dict]:
        blockers: list[dict] = []
        if not readiness:
            blockers.append({"source": "live_readiness", "name": "artifact", "status": "missing", "summary": "Run live_readiness before building the cutover package."})
        if not activation:
            blockers.append({"source": "live_activation", "name": "artifact", "status": "missing", "summary": "Run live_activation before building the cutover package."})
        if not switch_plan:
            blockers.append({"source": "live_switch_plan", "name": "artifact", "status": "missing", "summary": "Run live_switch_plan before building the cutover package."})
        for check in readiness.get("checks", []):
            if check.get("status") != "pass":
                blockers.append({"source": "live_readiness", "name": check.get("name"), "status": check.get("status"), "summary": check.get("summary")})
        for check in activation.get("real_money_checks", []):
            if check.get("status") != "pass":
                blockers.append({"source": "live_activation", "name": check.get("name"), "status": check.get("status"), "summary": check.get("summary")})
        for step in switch_plan.get("steps", []):
            if step.get("status") != "done":
                blockers.append({"source": "live_switch_plan", "name": step.get("name"), "status": step.get("status"), "summary": step.get("action")})
        deduped: list[dict] = []
        seen = set()
        for item in blockers:
            key = (item.get("source"), item.get("name"))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped

    def _status(self, readiness: dict, activation: dict, blockers: list[dict]) -> str:
        if activation.get("real_money_ready") and readiness.get("live_ready") and not blockers:
            return "real_money_ready"
        if activation.get("dry_run_ready"):
            return "dry_run_ready"
        return "blocked"

    def _required_external_inputs(self) -> list[dict]:
        broker = self.config.get("broker", {})
        oanda = self.config.get("oanda_feed", {})
        return [
            {
                "name": "official_gold_5m_feed",
                "required_for": "live",
                "accepted_sources": ["OANDA XAU_USD M5", "MT5 broker CSV XAUUSD 5m"],
                "proof": "official_rows>0 and data_source_preflight.live_data_mode=official_broker",
            },
            {
                "name": str(oanda.get("token_env", "OANDA_API_TOKEN")),
                "required_for": "oanda_rest",
                "secret": True,
                "proof": "live_env.status=pass and oanda_account.status=pass",
            },
            {
                "name": str(oanda.get("account_id_env", "OANDA_ACCOUNT_ID")),
                "required_for": "oanda_rest",
                "secret": True,
                "proof": "oanda_account.account_ready=true",
            },
            {
                "name": "broker_provider",
                "required_for": "live",
                "accepted_values": ["oanda_rest", "mt5_file_bridge"],
                "current_value": broker.get("provider", "manual_gateway"),
                "proof": "broker_preflight.ready=true",
            },
        ]

    def _preflight_commands(self, run_date: str) -> list[str]:
        return [
            f"python3 -m pipelines.import_official_feed --date {run_date}",
            f"python3 -m pipelines.oanda_account --date {run_date}",
            f"python3 -m pipelines.broker_preflight --date {run_date}",
            f"python3 -m pipelines.live_readiness --date {run_date} --json",
            f"python3 -m pipelines.live_activation --date {run_date}",
            f"python3 -m pipelines.live_cutover --date {run_date}",
        ]

    def _cutover_sequence(self, run_date: str) -> list[dict]:
        return [
            {"order": 1, "action": "Import official XAUUSD 5m data and confirm live-ready data source.", "command": f"python3 -m pipelines.import_official_feed --date {run_date}"},
            {"order": 2, "action": "Validate broker credentials and account/instrument access without exposing secrets.", "command": f"python3 -m pipelines.oanda_account --date {run_date}"},
            {"order": 3, "action": "Run live readiness and keep execution protected until every check passes.", "command": f"python3 -m pipelines.live_readiness --date {run_date} --json"},
            {"order": 4, "action": "Review Dashboard, Trading Journal, risk state, open paper positions, and live switch plan.", "command": "open http://127.0.0.1:8765/dashboard.html"},
            {"order": 5, "action": "Create dated human approval only after dry-run readiness is proven.", "command": f"python3 -m pipelines.live_approval --date {run_date} --request"},
            {"order": 6, "action": "Disable broker dry_run and switch execution_mode to live only after explicit approval.", "command": "edit configs/pipeline.yaml locally"},
            {"order": 7, "action": "Run one live smoke-sized order only if live_activation.real_money_ready=true.", "command": f"python3 -m pipelines.live_activation --date {run_date}"},
        ]

    def _rollback_plan(self, run_date: str) -> list[dict]:
        return [
            {"order": 1, "action": "Set execution_mode back to paper and live_trading_enabled=false.", "verification": f"python3 -m pipelines.live_readiness --date {run_date}"},
            {"order": 2, "action": "Set broker.dry_run=true or broker.provider=manual_gateway.", "verification": f"python3 -m pipelines.broker_preflight --date {run_date}"},
            {"order": 3, "action": "Archive broker receipts and live order requests before further trading.", "verification": "python3 -m pipelines.broker_receipts"},
            {"order": 4, "action": "Record rollback reason in the daily Trading Journal.", "verification": f"python3 -m pipelines.daily_review --date {run_date}"},
        ]

    def _evidence_paths(self, run_date: str) -> dict:
        return {
            "live_readiness": str(self.output_root / "live_readiness" / f"{run_date}.json"),
            "live_activation": str(self.output_root / "live_activation" / f"{run_date}.json"),
            "live_switch_plan": str(self.output_root / "live_switch_plan" / f"{run_date}.json"),
            "live_env": str(self.output_root / "live_env" / f"{run_date}.json"),
            "oanda_account": str(self.output_root / "oanda_account" / f"{run_date}.json"),
            "broker_preflight": str(self.output_root / "broker_preflight" / "current.json"),
            "data_source_preflight": str(self.output_root / "data_source_preflight" / f"{run_date}.json"),
            "dashboard": str(ROOT / "dashboard.html"),
        }

    def _write_markdown(self, run_date: str, payload: dict) -> None:
        lines = [
            f"# Live Cutover Package - {run_date}",
            "",
            f"- Status: {payload['status']}",
            f"- Live ready: {payload['live_ready']}",
            f"- Dry-run ready: {payload['dry_run_ready']}",
            f"- Real-money ready: {payload['real_money_ready']}",
            f"- Broker provider: {payload['broker_provider'] or 'not configured'}",
            "",
            "## Blockers",
        ]
        if payload["blockers"]:
            for item in payload["blockers"]:
                lines.append(f"- {item['source']}/{item['name']}: {item['summary']}")
        else:
            lines.append("- none")
        lines.extend(["", "## Required External Inputs"])
        for item in payload["required_external_inputs"]:
            label = item["name"]
            if item.get("secret"):
                label = f"{label} (secret, value not written)"
            lines.append(f"- {label}: proof `{item['proof']}`")
        lines.extend(["", "## Cutover Sequence"])
        for item in payload["cutover_sequence"]:
            lines.append(f"{item['order']}. {item['action']} `{item['command']}`")
        lines.extend(["", "## Rollback Plan"])
        for item in payload["rollback_plan"]:
            lines.append(f"{item['order']}. {item['action']} Verify: `{item['verification']}`")
        path = self.output_root / "live_cutover" / f"{run_date}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _latest(self, folder: str, run_date: str) -> dict:
        rows = load_json(self.output_root / folder / f"{run_date}.json")
        if not rows:
            rows = load_json(self.output_root / folder / "current.json")
        return rows[-1] if rows else {}


def run_live_cutover_package(run_date: str, output_root: Path | None = None, market_db: Path | None = None) -> dict:
    return LiveCutoverPackage(output_root, market_db).run(run_date)
