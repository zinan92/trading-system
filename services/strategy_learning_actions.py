from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from services.journal_store import load_json, write_json


class StrategyLearningActions:
    def __init__(self, output_root: Path) -> None:
        self.output_root = output_root

    def build(self, run_date: str) -> dict:
        review = self._latest(self.output_root / "strategy_reviews" / f"{run_date}.json")
        ledger = self._latest(self.output_root / "learning_ledger" / f"{run_date}.json") or self._latest(self.output_root / "learning_ledger" / "current.json")
        proposal = self._latest(self.output_root / "strategy_change_proposals" / f"{run_date}.json") or self._latest(self.output_root / "strategy_change_proposals" / "current.json")
        performance = self._latest(self.output_root / "performance" / f"{run_date}.json")
        exit_monitor = self._latest(self.output_root / "paper_exit_monitor" / f"{run_date}.json")
        auto_gate = self._latest(self.output_root / "paper_auto_approval_gate" / f"{run_date}.json")
        data_lineage = self._latest(self.output_root / "data_source_lineage" / f"{run_date}.json")
        actions = self._actions(run_date, review, ledger, proposal, performance, exit_monitor, auto_gate, data_lineage)
        payload = {
            "run_date": run_date,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": self._status(actions, proposal),
            "summary": {
                "action_count": len(actions),
                "high_priority": sum(1 for item in actions if item["priority"] == "high"),
                "manual_review_required": any(item["type"] == "manual_review" for item in actions),
                "experiment_allowed": proposal.get("status") == "eligible_for_small_experiment",
                "learning_state": ledger.get("learning_state", "unknown"),
                "closed_trade_count": ledger.get("closed_trade_count", 0),
            },
            "actions": actions,
            "source_artifacts": {
                "strategy_review": str(self.output_root / "strategy_reviews" / f"{run_date}.json"),
                "learning_ledger": str(self.output_root / "learning_ledger" / f"{run_date}.json"),
                "strategy_change_proposal": str(self.output_root / "strategy_change_proposals" / f"{run_date}.json"),
                "performance": str(self.output_root / "performance" / f"{run_date}.json"),
                "paper_exit_monitor": str(self.output_root / "paper_exit_monitor" / f"{run_date}.json"),
                "paper_auto_approval_gate": str(self.output_root / "paper_auto_approval_gate" / f"{run_date}.json"),
            },
        }
        write_json(self.output_root / "strategy_learning_actions" / f"{run_date}.json", [payload])
        write_json(self.output_root / "strategy_learning_actions" / "current.json", [payload])
        self._write_markdown(run_date, payload)
        return payload

    def _actions(
        self,
        run_date: str,
        review: dict,
        ledger: dict,
        proposal: dict,
        performance: dict,
        exit_monitor: dict,
        auto_gate: dict,
        data_lineage: dict,
    ) -> list[dict]:
        actions: list[dict] = []
        official_rows = int(((data_lineage.get("provider_groups") or {}).get("official") or {}).get("rows", 0) or 0)
        if official_rows == 0:
            actions.append(self._action(
                "official_feed",
                "high",
                "data",
                "Import official XAUUSD 5m broker data before live trading or parameter changes.",
                "python3 -m pipelines.import_official_feed --date " + run_date,
                {"official_rows": official_rows, "truth_level": data_lineage.get("truth_level")},
            ))
        perf = performance.get("summary", {}) if performance else {}
        open_r = float(perf.get("open_unrealized_r", 0) or 0)
        open_count = int(perf.get("open_trade_count", 0) or 0)
        if open_count:
            actions.append(self._action(
                "open_exposure_review",
                "high" if open_r <= -0.5 else "medium",
                "manual_review",
                f"Review {open_count} open paper trade(s) before adding exposure.",
                "python3 -m pipelines.paper_reconciliation --date " + run_date,
                {"open_unrealized_r": open_r, "open_trade_count": open_count},
            ))
        exit_summary = exit_monitor.get("summary") or {}
        nearest_stop = exit_summary.get("nearest_stop_distance_pct")
        if nearest_stop is not None and float(nearest_stop) < 1.25:
            actions.append(self._action(
                "stop_distance_review",
                "high",
                "manual_review",
                "Nearest open paper stop is close; review invalidation before any new paper order.",
                "python3 -m pipelines.daily_review --date " + run_date,
                {"nearest_stop_distance_pct": nearest_stop},
            ))
        if auto_gate and not auto_gate.get("allow_auto_approve", False):
            actions.append(self._action(
                "auto_approval_block",
                "medium",
                "risk",
                "Keep paper auto-approval blocked until the gate reasons clear.",
                "python3 -m pipelines.paper_auto_approval_gate --date " + run_date + " --auto-requested",
                {"reasons": auto_gate.get("reasons", [])},
            ))
        closed = int(ledger.get("closed_trade_count", 0) or 0)
        if closed < 20:
            actions.append(self._action(
                "collect_trade_sample",
                "medium",
                "learning",
                f"Collect {20 - closed} more closed paper trade(s) before changing strategy parameters.",
                "python3 -m pipelines.runner --date " + run_date + " --iterations 1",
                {"closed_trade_count": closed, "target_closed_trades": 20},
            ))
        if proposal.get("status") == "eligible_for_small_experiment":
            actions.append(self._action(
                "shadow_experiment",
                "medium",
                "experiment",
                "Prepare a paper-only shadow backtest experiment from the approved proposal.",
                "python3 -m pipelines.review --date " + run_date,
                {"proposed_changes": proposal.get("proposed_changes", [])},
            ))
        for suggestion in (review.get("suggestions") or [])[:2]:
            actions.append(self._action(
                "review_suggestion",
                "low",
                "review",
                suggestion,
                "python3 -m pipelines.daily_review --date " + run_date,
                {"source": "strategy_review"},
            ))
        return actions

    def _status(self, actions: list[dict], proposal: dict) -> str:
        if any(item["priority"] == "high" for item in actions):
            return "review_required"
        if proposal.get("status") == "eligible_for_small_experiment":
            return "experiment_ready"
        return "collecting_evidence"

    def _action(self, action_id: str, priority: str, action_type: str, summary: str, command: str, evidence: dict) -> dict:
        return {
            "action_id": action_id,
            "priority": priority,
            "type": action_type,
            "summary": summary,
            "command": command,
            "evidence": evidence,
            "status": "open",
        }

    def _latest(self, path: Path) -> dict:
        rows = load_json(path)
        return rows[-1] if rows else {}

    def _write_markdown(self, run_date: str, payload: dict) -> None:
        lines = [
            f"# Strategy Learning Actions - {run_date}",
            "",
            f"- Status: {payload['status']}",
            f"- Actions: {payload['summary']['action_count']}",
            f"- High priority: {payload['summary']['high_priority']}",
            f"- Learning state: {payload['summary']['learning_state']}",
            "",
            "## Actions",
        ]
        for item in payload["actions"]:
            lines.append(f"- [{item['priority']}] {item['action_id']}: {item['summary']} | `{item['command']}`")
        path = self.output_root / "strategy_learning_actions" / f"{run_date}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
