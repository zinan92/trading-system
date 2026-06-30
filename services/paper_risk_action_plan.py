from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from services.journal_store import load_json, write_json


class PaperRiskActionPlan:
    def __init__(self, output_root: Path) -> None:
        self.output_root = output_root

    def build(self, run_date: str) -> dict:
        risk = self._latest("risk_monitor", run_date)
        exits = self._latest("paper_exit_decisions", run_date)
        performance = self._latest("performance", run_date)
        actions = self._actions(run_date, risk, exits, performance)
        summary = {
            "action_count": len(actions),
            "high_priority": sum(1 for item in actions if item["priority"] == "high"),
            "exit_actions": sum(1 for item in actions if item["type"] == "paper_exit"),
            "risk_status": risk.get("status", "unknown"),
            "exit_status": exits.get("status", "unknown"),
            "open_unrealized_r": (performance.get("summary") or {}).get("open_unrealized_r", 0),
        }
        payload = {
            "run_date": run_date,
            "generated_at": self._now(),
            "status": "action_required" if summary["high_priority"] else ("review" if actions else "empty"),
            "paper_only": True,
            "auto_execute": False,
            "summary": summary,
            "actions": actions,
            "source_artifacts": {
                "risk_monitor": str(self.output_root / "risk_monitor" / f"{run_date}.json"),
                "paper_exit_decisions": str(self.output_root / "paper_exit_decisions" / f"{run_date}.json"),
                "performance": str(self.output_root / "performance" / f"{run_date}.json"),
            },
        }
        write_json(self.output_root / "paper_risk_action_plan" / f"{run_date}.json", [payload])
        write_json(self.output_root / "paper_risk_action_plan" / "current.json", [payload])
        self._write_markdown(run_date, payload)
        return payload

    def _actions(self, run_date: str, risk: dict, exits: dict, performance: dict) -> list[dict]:
        actions: list[dict] = []
        for item in exits.get("queue", []):
            if item.get("required_user_action") != "approve_exit":
                continue
            priority = "high" if item.get("priority") == "high" else "medium"
            actions.append(
                {
                    "action_id": f"approve_exit_{item.get('trade_id', 'unknown')}",
                    "priority": priority,
                    "type": "paper_exit",
                    "summary": f"Review and approve paper exit for {item.get('trade_id')} due to {item.get('exit_type')}.",
                    "command": (
                        f"python3 -m pipelines.paper_exit_decision --date {run_date} "
                        f"--trade-id {item.get('trade_id')} --decision approve_exit "
                        f"--notes \"{item.get('exit_type')} after risk review\""
                    ),
                    "evidence": {
                        "trade_id": item.get("trade_id"),
                        "exit_type": item.get("exit_type"),
                        "suggested_exit_price": item.get("suggested_exit_price"),
                        "unrealized_r": item.get("unrealized_r"),
                        "portfolio_open_unrealized_r": item.get("portfolio_open_unrealized_r"),
                    },
                    "status": "open",
                }
            )
        risk_summary = risk.get("summary") or {}
        if risk.get("kill_switch_active") and not actions:
            actions.append(
                {
                    "action_id": "risk_review",
                    "priority": "high",
                    "type": "manual_review",
                    "summary": "Risk kill switch is active; review risk monitor before any new paper exposure.",
                    "command": f"python3 -m pipelines.risk_monitor --date {run_date}",
                    "evidence": {"block_reasons": risk_summary.get("block_reasons", [])},
                    "status": "open",
                }
            )
        open_count = int((performance.get("summary") or {}).get("open_trade_count", 0) or 0)
        if open_count and not any(item["type"] == "paper_exit" for item in actions):
            actions.append(
                {
                    "action_id": "open_exposure_review",
                    "priority": "medium",
                    "type": "manual_review",
                    "summary": f"Review {open_count} open paper trade(s) and decide hold/reject/exit.",
                    "command": f"python3 -m pipelines.paper_exit_decisions --date {run_date}",
                    "evidence": performance.get("summary", {}),
                    "status": "open",
                }
            )
        return actions

    def _latest(self, folder: str, run_date: str) -> dict:
        dated = load_json(self.output_root / folder / f"{run_date}.json")
        if dated:
            return dated[-1]
        current = load_json(self.output_root / folder / "current.json")
        return current[-1] if current else {}

    def _write_markdown(self, run_date: str, payload: dict) -> None:
        lines = [
            f"# Paper Risk Action Plan - {run_date}",
            "",
            f"- Status: {payload['status']}",
            f"- Paper only: {payload['paper_only']}",
            f"- Auto execute: {payload['auto_execute']}",
            f"- Actions: {payload['summary']['action_count']}",
            f"- High priority: {payload['summary']['high_priority']}",
            f"- Open unrealized R: {payload['summary']['open_unrealized_r']}",
            "",
            "## Actions",
        ]
        if not payload["actions"]:
            lines.append("- No paper risk action is currently required.")
        for item in payload["actions"]:
            lines.append(f"- [{item['priority']}] {item['action_id']}: {item['summary']} | `{item['command']}`")
        path = self.output_root / "paper_risk_action_plan" / f"{run_date}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _now(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
