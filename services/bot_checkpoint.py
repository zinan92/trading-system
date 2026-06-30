from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json


class BotCheckpoint:
    def __init__(self, output_root: Path | None = None) -> None:
        config = load_pipeline_config()
        self.output_root = output_root or ROOT / config.get("output_root", "outputs")

    def build(self, run_date: str) -> dict:
        runner = self._load_mapping(self.output_root / "runner_status" / "current.json")
        supervisor = self._latest("bot_supervisor", run_date)
        data_source = self._latest("data_source_preflight", run_date)
        data_lineage = self._latest("data_source_lineage", run_date)
        risk_monitor = self._latest("risk_monitor", run_date)
        guardrails = self._latest("strategy_guardrails", run_date)
        improvement = self._latest("strategy_improvement_plan", run_date)
        performance = self._latest("performance", run_date)
        auto_gate = self._latest("paper_auto_approval_gate", run_date)
        pending = load_json(self.output_root / "journal_pending" / f"{run_date}.json")
        orders = load_json(self.output_root / "paper_orders" / f"{run_date}.json")
        open_trades = load_json(self.output_root / "paper_trades" / "current.json")
        journal_path = self.output_root / "journals" / f"{run_date}.md"
        review_path = self.output_root / "review_notes" / f"{run_date}.md"
        report_path = self.output_root / "reports" / f"{run_date}.md"
        resume = self._resume_actions(
            run_date,
            runner,
            supervisor,
            data_source,
            data_lineage,
            risk_monitor,
            guardrails,
            improvement,
            auto_gate,
            pending,
            open_trades,
        )
        status = self._status(resume, supervisor, data_source)
        payload = {
            "run_date": run_date,
            "checkpointed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": status,
            "mock_recoverable": status in {"ready", "attention_required"},
            "live_recoverable": False,
            "execution_mode": "paper",
            "summary": {
                "runner_state": runner.get("state", ""),
                "supervisor_status": supervisor.get("status", ""),
                "mock_bot_running": supervisor.get("mock_bot_running", False),
                "latest_price": data_source.get("latest_price"),
                "latest_provider": data_source.get("latest_provider", ""),
                "data_truth_level": data_lineage.get("truth_level", "unknown"),
                "official_rows": int(((data_lineage.get("provider_groups") or {}).get("official") or {}).get("rows", 0) or 0),
                "pending_decisions": len(pending),
                "paper_orders": len(orders),
                "open_trades": len(open_trades),
                "net_pnl_marked": (performance.get("summary") or {}).get("net_pnl_marked", 0),
                "open_unrealized_r": (performance.get("summary") or {}).get("open_unrealized_r", 0),
                "risk_status": risk_monitor.get("status", ""),
                "kill_switch_active": risk_monitor.get("kill_switch_active", False),
                "allow_new_paper_order": guardrails.get("allow_new_paper_order", False),
                "paper_auto_allow": auto_gate.get("allow_auto_approve", False),
                "improvement_status": improvement.get("status", ""),
                "improvement_steps": len(improvement.get("next_steps", [])),
            },
            "resume_actions": resume,
            "artifacts": {
                "runner_status": str(self.output_root / "runner_status" / "current.json"),
                "bot_supervisor": str(self.output_root / "bot_supervisor" / f"{run_date}.json"),
                "data_source_preflight": str(self.output_root / "data_source_preflight" / f"{run_date}.json"),
                "data_source_lineage": str(self.output_root / "data_source_lineage" / f"{run_date}.json"),
                "risk_monitor": str(self.output_root / "risk_monitor" / f"{run_date}.json"),
                "strategy_guardrails": str(self.output_root / "strategy_guardrails" / f"{run_date}.json"),
                "strategy_improvement_plan": str(self.output_root / "strategy_improvement_plan" / f"{run_date}.json"),
                "paper_auto_approval_gate": str(self.output_root / "paper_auto_approval_gate" / f"{run_date}.json"),
                "journal": str(journal_path),
                "review_notes": str(review_path),
                "report": str(report_path),
            },
            "artifact_status": {
                "journal": journal_path.exists(),
                "review_notes": review_path.exists(),
                "report": report_path.exists(),
            },
        }
        write_json(self.output_root / "bot_checkpoints" / f"{run_date}.json", [payload])
        write_json(self.output_root / "bot_checkpoints" / "current.json", [payload])
        self._write_markdown(run_date, payload)
        return payload

    def _resume_actions(
        self,
        run_date: str,
        runner: dict,
        supervisor: dict,
        data_source: dict,
        data_lineage: dict,
        risk_monitor: dict,
        guardrails: dict,
        improvement: dict,
        auto_gate: dict,
        pending: list[dict],
        open_trades: list[dict],
    ) -> list[dict]:
        actions: list[dict] = []
        if runner.get("state") not in {"ok", "running"}:
            actions.append(self._action("restart_runner", "high", "runtime", "Restart the 5m mock runner.", f"python3 -m pipelines.runner --date {run_date} --interval-seconds 300 --iterations 1"))
        if supervisor.get("status") in {"fail", ""}:
            actions.append(self._action("run_supervisor", "high", "runtime", "Rebuild supervisor status before approving more paper trades.", f"python3 -m pipelines.bot_supervisor --date {run_date}"))
        if not data_source.get("ready_for_paper", False):
            actions.append(self._action("refresh_market_data", "high", "data", "Refresh GOLD 5m data and preflight before any paper decision.", f"python3 -m pipelines.collect --date {run_date} --iterations 1"))
        official_rows = int(((data_lineage.get("provider_groups") or {}).get("official") or {}).get("rows", 0) or 0)
        if official_rows == 0:
            actions.append(self._action("import_official_feed", "medium", "data", "Import official XAUUSD 5m broker feed before live readiness or parameter promotion.", f"python3 -m pipelines.import_official_feed --date {run_date}"))
        if risk_monitor.get("kill_switch_active"):
            actions.append(self._action("respect_kill_switch", "high", "risk", "Risk kill switch is active; do not add paper exposure.", f"python3 -m pipelines.daily_review --date {run_date}"))
        if not guardrails.get("allow_new_paper_order", False):
            actions.append(self._action("review_guardrails", "high", "risk", "Strategy guardrails block new paper orders.", f"python3 -m pipelines.daily_review --date {run_date}"))
        if pending:
            actions.append(self._action("review_pending_tickets", "medium", "journal", f"Review {len(pending)} pending paper ticket(s).", f"python3 -m pipelines.review --date {run_date}"))
        if open_trades:
            actions.append(self._action("review_open_trades", "medium", "paper", f"Review {len(open_trades)} open paper trade(s) before adding exposure.", f"python3 -m pipelines.paper_exit_decisions --date {run_date}"))
        if improvement.get("status") == "action_required":
            actions.append(self._action("follow_improvement_plan", "high", "learning", "Follow the strategy improvement plan before changing parameters.", f"python3 -m pipelines.strategy_improvement_plan --date {run_date}"))
        if auto_gate and not auto_gate.get("allow_auto_approve", False):
            actions.append(self._action("manual_paper_only", "medium", "execution", "Paper auto-approval is blocked; manual review remains required.", f"python3 -m pipelines.paper_auto_approval_gate --date {run_date} --auto-requested"))
        if not actions:
            actions.append(self._action("continue_mock_loop", "low", "runtime", "Continue the 5m mock loop; no checkpoint blockers are open.", f"python3 -m pipelines.runner --date {run_date} --interval-seconds 300 --iterations 1"))
        return actions

    def _status(self, actions: list[dict], supervisor: dict, data_source: dict) -> str:
        if any(item["priority"] == "high" for item in actions):
            return "attention_required"
        if supervisor.get("mock_bot_running") and data_source.get("ready_for_paper"):
            return "ready"
        return "watch"

    def _action(self, action_id: str, priority: str, action_type: str, summary: str, command: str) -> dict:
        return {
            "action_id": action_id,
            "priority": priority,
            "type": action_type,
            "summary": summary,
            "command": command,
            "status": "open",
        }

    def _latest(self, folder: str, run_date: str) -> dict:
        dated = load_json(self.output_root / folder / f"{run_date}.json")
        current = load_json(self.output_root / folder / "current.json")
        if dated:
            return dated[-1]
        return current[-1] if current else {}

    def _load_mapping(self, path: Path) -> dict:
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_markdown(self, run_date: str, payload: dict) -> None:
        lines = [
            f"# Bot Checkpoint - {run_date}",
            "",
            f"- Status: {payload['status']}",
            f"- Mock recoverable: {payload['mock_recoverable']}",
            f"- Live recoverable: {payload['live_recoverable']}",
            f"- Latest price: {payload['summary']['latest_price']} from {payload['summary']['latest_provider']}",
            f"- Data truth: {payload['summary']['data_truth_level']} / official rows {payload['summary']['official_rows']}",
            f"- Open trades: {payload['summary']['open_trades']}",
            f"- Pending decisions: {payload['summary']['pending_decisions']}",
            "",
            "## Resume Actions",
        ]
        for item in payload["resume_actions"]:
            lines.append(f"- [{item['priority']}] {item['action_id']}: {item['summary']} | `{item['command']}`")
        path = self.output_root / "bot_checkpoints" / f"{run_date}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_bot_checkpoint(run_date: str, output_root: Path | None = None) -> dict:
    return BotCheckpoint(output_root).build(run_date)
