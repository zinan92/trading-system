from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json


class StrategyPromotionGate:
    def __init__(self, output_root: Path | None = None) -> None:
        config = load_pipeline_config()
        self.output_root = output_root or ROOT / config.get("output_root", "outputs")

    def run(self, run_date: str) -> dict:
        experiments = self._latest("strategy_experiments", run_date)
        ledger = self._latest("learning_ledger", run_date)
        data_lineage = self._latest("data_source_lineage", run_date)
        risk_monitor = self._latest("risk_monitor", run_date)
        guardrails = self._latest("strategy_guardrails", run_date)
        live_dry_run = self._latest("live_dry_run_drill", run_date)
        baseline = experiments.get("baseline") or {}
        candidate = experiments.get("best_candidate") or {}
        blockers = self._blockers(experiments, ledger, data_lineage, risk_monitor, guardrails, baseline, candidate)
        status = "requestable" if not blockers else "blocked"
        payload = {
            "run_date": run_date,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": status,
            "promotion_allowed": status == "requestable",
            "auto_apply": False,
            "paper_only": True,
            "live_config_change_allowed": False,
            "strategy_id": experiments.get("strategy_id", "gold_5m_v1"),
            "baseline": baseline,
            "candidate": candidate,
            "score_delta": self._score_delta(baseline, candidate),
            "closed_trade_count": ledger.get("closed_trade_count", experiments.get("closed_trade_count", 0)),
            "official_rows": self._official_rows(data_lineage),
            "execution_venue_rows": self._execution_venue_rows(data_lineage),
            "execution_grade_rows": self._execution_grade_rows(data_lineage),
            "data_truth_level": data_lineage.get("truth_level", experiments.get("data_truth_level", "unknown")),
            "risk_status": risk_monitor.get("status", ""),
            "guardrail_status": guardrails.get("status", ""),
            "live_dry_run_status": live_dry_run.get("status", ""),
            "blockers": blockers,
            "required_manual_steps": self._manual_steps(run_date, candidate),
            "source_artifacts": {
                "strategy_experiments": str(self.output_root / "strategy_experiments" / f"{run_date}.json"),
                "learning_ledger": str(self.output_root / "learning_ledger" / f"{run_date}.json"),
                "data_source_lineage": str(self.output_root / "data_source_lineage" / f"{run_date}.json"),
                "risk_monitor": str(self.output_root / "risk_monitor" / f"{run_date}.json"),
                "strategy_guardrails": str(self.output_root / "strategy_guardrails" / f"{run_date}.json"),
            },
        }
        write_json(self.output_root / "strategy_promotion_gate" / f"{run_date}.json", [payload])
        write_json(self.output_root / "strategy_promotion_gate" / "current.json", [payload])
        self._write_markdown(run_date, payload)
        return payload

    def _blockers(self, experiments: dict, ledger: dict, data_lineage: dict, risk_monitor: dict, guardrails: dict, baseline: dict, candidate: dict) -> list[dict]:
        blockers: list[dict] = []
        if not experiments:
            blockers.append({"name": "missing_experiments", "summary": "Strategy experiment queue has not run.", "evidence": {}})
            return blockers
        for item in experiments.get("blockers", []):
            blockers.append({"name": f"experiment_{item.get('name', 'blocker')}", "summary": item.get("summary", "Experiment blocker is open."), "evidence": item.get("evidence", {})})
        if experiments.get("status") != "experiment_ready":
            blockers.append({"name": "experiment_status", "summary": "Strategy experiments are not marked experiment_ready.", "evidence": {"status": experiments.get("status")}})
        if int(ledger.get("closed_trade_count", experiments.get("closed_trade_count", 0)) or 0) < 20:
            blockers.append({"name": "closed_trade_sample", "summary": "Need at least 20 closed paper trades before requesting parameter promotion.", "evidence": {"closed_trade_count": ledger.get("closed_trade_count", experiments.get("closed_trade_count", 0))}})
        execution_grade_rows = self._execution_grade_rows(data_lineage)
        if execution_grade_rows <= 0:
            blockers.append({"name": "execution_grade_data", "summary": "Need execution-grade GOLD 5m rows before requesting parameter promotion.", "evidence": {"execution_grade_rows": execution_grade_rows, "truth_level": data_lineage.get("truth_level")}})
        if not candidate:
            blockers.append({"name": "candidate", "summary": "No best experiment candidate exists.", "evidence": {}})
        else:
            if candidate.get("verdict") not in {"supportive", "mixed"}:
                blockers.append({"name": "candidate_verdict", "summary": "Best candidate must be supportive or mixed before promotion request.", "evidence": {"verdict": candidate.get("verdict")}})
            if self._score_delta(baseline, candidate) <= 0:
                blockers.append({"name": "candidate_score", "summary": "Best candidate does not outperform baseline score.", "evidence": {"baseline_score": baseline.get("score"), "candidate_score": candidate.get("score")}})
        if risk_monitor.get("kill_switch_active"):
            blockers.append({"name": "risk_kill_switch", "summary": "Risk kill switch is active.", "evidence": risk_monitor})
        if guardrails and not guardrails.get("allow_new_paper_order", False):
            blockers.append({"name": "strategy_guardrails", "summary": "Strategy guardrails do not allow new paper exposure.", "evidence": guardrails.get("summary", {})})
        return blockers

    def _manual_steps(self, run_date: str, candidate: dict) -> list[str]:
        return [
            f"Review outputs/strategy_experiments/{run_date}.json and compare candidate `{candidate.get('variant_id', 'n/a')}` against baseline.",
            "Create a small paper-only branch/config change; do not edit live config directly.",
            f"Run python3 -m pipelines.daily_review --date {run_date} after any paper-only experiment config change.",
            "Keep real-money execution blocked until live_readiness, live_activation, and human approval all pass.",
        ]

    def _official_rows(self, data_lineage: dict) -> int:
        return int(((data_lineage.get("provider_groups") or {}).get("official") or {}).get("rows", 0) or 0)

    def _execution_venue_rows(self, data_lineage: dict) -> int:
        return int(((data_lineage.get("provider_groups") or {}).get("execution_venue") or {}).get("rows", 0) or 0)

    def _execution_grade_rows(self, data_lineage: dict) -> int:
        return self._official_rows(data_lineage) + self._execution_venue_rows(data_lineage)

    def _score_delta(self, baseline: dict, candidate: dict) -> float:
        return round(float(candidate.get("score", 0) or 0) - float(baseline.get("score", 0) or 0), 4)

    def _latest(self, folder: str, run_date: str) -> dict:
        rows = load_json(self.output_root / folder / f"{run_date}.json")
        if not rows:
            rows = load_json(self.output_root / folder / "current.json")
        return rows[-1] if rows else {}

    def _write_markdown(self, run_date: str, payload: dict) -> None:
        lines = [
            f"# Strategy Promotion Gate - {run_date}",
            "",
            f"- Status: {payload['status']}",
            f"- Promotion allowed: {payload['promotion_allowed']}",
            f"- Auto apply: {payload['auto_apply']}",
            f"- Paper only: {payload['paper_only']}",
            f"- Candidate: {payload['candidate'].get('variant_id', 'n/a')}",
            f"- Score delta: {payload['score_delta']}",
            "",
            "## Blockers",
        ]
        if payload["blockers"]:
            for item in payload["blockers"]:
                lines.append(f"- {item['name']}: {item['summary']}")
        else:
            lines.append("- none")
        lines.extend(["", "## Manual Steps"])
        lines.extend(f"- {item}" for item in payload["required_manual_steps"])
        path = self.output_root / "strategy_promotion_gate" / f"{run_date}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_strategy_promotion_gate(run_date: str, output_root: Path | None = None) -> dict:
    return StrategyPromotionGate(output_root).run(run_date)
