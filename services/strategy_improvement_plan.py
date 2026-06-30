from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json


class StrategyImprovementPlan:
    def __init__(self, output_root: Path | None = None) -> None:
        config = load_pipeline_config()
        self.output_root = output_root or ROOT / config.get("output_root", "outputs")

    def build(self, run_date: str) -> dict:
        actions = self._latest("strategy_learning_actions", run_date)
        experiments = self._latest("strategy_experiments", run_date)
        promotion = self._latest("strategy_promotion_gate", run_date)
        guardrails = self._latest("strategy_guardrails", run_date)
        performance = self._latest("performance", run_date)
        data_lineage = self._latest("data_source_lineage", run_date)
        review = self._latest("strategy_reviews", run_date)
        next_steps = self._next_steps(actions, experiments, promotion, guardrails, performance, data_lineage, review)
        status = self._status(next_steps, promotion)
        payload = {
            "run_date": run_date,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": status,
            "strategy_id": experiments.get("strategy_id") or "gold_5m_v1",
            "paper_only": True,
            "auto_apply": False,
            "summary": {
                "next_step_count": len(next_steps),
                "high_priority": sum(1 for item in next_steps if item["priority"] == "high"),
                "manual_review_required": any(item["requires_manual_review"] for item in next_steps),
                "promotion_allowed": bool(promotion.get("promotion_allowed", False)),
                "allow_new_paper_order": bool(guardrails.get("allow_new_paper_order", False)),
                "data_truth_level": data_lineage.get("truth_level", "unknown"),
                "official_rows": int(((data_lineage.get("provider_groups") or {}).get("official") or {}).get("rows", 0) or 0),
                "open_unrealized_r": (performance.get("summary") or {}).get("open_unrealized_r", 0),
                "best_experiment": (experiments.get("best_candidate") or {}).get("variant_id", ""),
            },
            "next_steps": next_steps,
            "source_artifacts": {
                "strategy_learning_actions": str(self.output_root / "strategy_learning_actions" / f"{run_date}.json"),
                "strategy_experiments": str(self.output_root / "strategy_experiments" / f"{run_date}.json"),
                "strategy_promotion_gate": str(self.output_root / "strategy_promotion_gate" / f"{run_date}.json"),
                "strategy_guardrails": str(self.output_root / "strategy_guardrails" / f"{run_date}.json"),
                "performance": str(self.output_root / "performance" / f"{run_date}.json"),
                "data_source_lineage": str(self.output_root / "data_source_lineage" / f"{run_date}.json"),
            },
        }
        write_json(self.output_root / "strategy_improvement_plan" / f"{run_date}.json", [payload])
        write_json(self.output_root / "strategy_improvement_plan" / "current.json", [payload])
        self._write_markdown(run_date, payload)
        return payload

    def _next_steps(self, actions: dict, experiments: dict, promotion: dict, guardrails: dict, performance: dict, data_lineage: dict, review: dict) -> list[dict]:
        steps: list[dict] = []
        for item in (actions.get("actions") or [])[:5]:
            steps.append(
                self._step(
                    item.get("action_id", "learning_action"),
                    item.get("priority", "medium"),
                    item.get("type", "learning"),
                    item.get("summary", "Review learning action."),
                    item.get("command", ""),
                    item.get("type") in {"manual_review", "risk"} or item.get("priority") == "high",
                    item.get("evidence", {}),
                )
            )
        if experiments:
            best = experiments.get("best_candidate") or {}
            if best:
                steps.append(
                    self._step(
                        "shadow_best_candidate",
                        "medium",
                        "experiment",
                        f"Track paper-only shadow variant `{best.get('variant_id')}` against baseline before changing config.",
                        f"python3 -m pipelines.strategy_experiments --date {experiments.get('run_date')}",
                        True,
                        {
                            "variant_id": best.get("variant_id"),
                            "score": best.get("score"),
                            "profit_factor": best.get("profit_factor"),
                            "avg_r": best.get("avg_r"),
                            "baseline_profit_factor": (experiments.get("baseline") or {}).get("profit_factor"),
                        },
                    )
                )
            for blocker in (experiments.get("blockers") or [])[:3]:
                steps.append(
                    self._step(
                        f"experiment_blocker_{blocker.get('name', 'unknown')}",
                        "high" if blocker.get("name") in {"official_data", "closed_trade_sample"} else "medium",
                        "blocker",
                        blocker.get("summary", "Resolve experiment blocker."),
                        "python3 -m pipelines.daily_review --date " + str(experiments.get("run_date", "")),
                        True,
                        blocker.get("evidence", {}),
                    )
                )
        if promotion:
            if promotion.get("promotion_allowed"):
                steps.append(
                    self._step(
                        "promotion_request",
                        "medium",
                        "manual_review",
                        "Promotion gate is requestable; prepare a manual paper-only parameter change request.",
                        "python3 -m pipelines.strategy_promotion_gate --date " + str(promotion.get("run_date", "")),
                        True,
                        {"candidate": promotion.get("candidate", {})},
                    )
                )
            else:
                steps.append(
                    self._step(
                        "hold_parameters",
                        "medium",
                        "guardrail",
                        "Keep current strategy parameters; promotion gate is blocked.",
                        "python3 -m pipelines.strategy_promotion_gate --date " + str(promotion.get("run_date", "")),
                        False,
                        {"blockers": [item.get("name") for item in promotion.get("blockers", [])]},
                    )
                )
        if guardrails and not guardrails.get("allow_new_paper_order", False):
            steps.append(
                self._step(
                    "pause_new_paper_orders",
                    "high",
                    "risk",
                    "Do not add new paper exposure until strategy guardrails clear.",
                    "python3 -m pipelines.daily_review --date " + str(guardrails.get("run_date", "")),
                    True,
                    guardrails.get("summary", {}),
                )
            )
        if not steps:
            steps.append(
                self._step(
                    "collect_more_evidence",
                    "medium",
                    "learning",
                    "Continue mock trading and collect more closed paper trades before changing strategy parameters.",
                    "python3 -m pipelines.runner --iterations 1",
                    False,
                    {"review_summary": review.get("summary", ""), "truth_level": data_lineage.get("truth_level")},
                )
            )
        return steps

    def _status(self, steps: list[dict], promotion: dict) -> str:
        if any(item["priority"] == "high" for item in steps):
            return "action_required"
        if promotion.get("promotion_allowed"):
            return "promotion_review"
        return "collecting_evidence"

    def _step(self, step_id: str, priority: str, step_type: str, summary: str, command: str, requires_manual_review: bool, evidence: dict) -> dict:
        return {
            "step_id": step_id,
            "priority": priority,
            "type": step_type,
            "summary": summary,
            "command": command,
            "requires_manual_review": requires_manual_review,
            "status": "open",
            "evidence": evidence,
        }

    def _latest(self, folder: str, run_date: str) -> dict:
        dated = load_json(self.output_root / folder / f"{run_date}.json")
        current = load_json(self.output_root / folder / "current.json")
        if dated:
            return dated[-1]
        return current[-1] if current else {}

    def _write_markdown(self, run_date: str, payload: dict) -> None:
        lines = [
            f"# Strategy Improvement Plan - {run_date}",
            "",
            f"- Status: {payload['status']}",
            f"- Strategy: {payload['strategy_id']}",
            f"- Paper only: {payload['paper_only']}",
            f"- Auto apply: {payload['auto_apply']}",
            f"- Data truth: {payload['summary']['data_truth_level']} / official rows {payload['summary']['official_rows']}",
            f"- Best experiment: {payload['summary']['best_experiment'] or 'n/a'}",
            "",
            "## Next Steps",
        ]
        for item in payload["next_steps"]:
            lines.append(f"- [{item['priority']}] {item['step_id']}: {item['summary']} | `{item['command']}`")
        path = self.output_root / "strategy_improvement_plan" / f"{run_date}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_strategy_improvement_plan(run_date: str, output_root: Path | None = None) -> dict:
    return StrategyImprovementPlan(output_root).build(run_date)
