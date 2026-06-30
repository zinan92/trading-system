from __future__ import annotations

from pathlib import Path

from services.config_loader import load_risk_rules
from services.journal_store import load_json


class PortfolioRiskState:
    def __init__(self, output_root: Path) -> None:
        self.output_root = output_root
        self.rules = load_risk_rules()

    def summary(self, run_date: str, candidate_loss_pct: float = 0.0) -> dict:
        cap = float(self.rules.get("default", {}).get("daily_loss_stop_pct", 1.25))
        used = self._daily_used_loss_pct(run_date)
        open_trades = load_json(self.output_root / "paper_trades" / "current.json")
        projected = used + float(candidate_loss_pct)
        return {
            "daily_loss_stop_pct": cap,
            "used_loss_pct": round(used, 4),
            "candidate_loss_pct": round(float(candidate_loss_pct), 4),
            "projected_loss_pct": round(projected, 4),
            "open_trades": len([item for item in open_trades if item.get("status") == "open"]),
            "allows_candidate": projected <= cap,
            "block_reason": "" if projected <= cap else f"daily risk cap exceeded: used {used:.2f}% + candidate {float(candidate_loss_pct):.2f}% > cap {cap:.2f}%",
        }

    def should_allow_ticket(self, run_date: str, ticket: dict) -> tuple[bool, dict]:
        risk = self.summary(run_date, float(ticket.get("max_loss_pct", 0)))
        return bool(risk["allows_candidate"]), risk

    def _daily_used_loss_pct(self, run_date: str) -> float:
        decisions = load_json(self.output_root / "journal_decisions" / f"{run_date}.json")
        used = 0.0
        for item in decisions:
            if item.get("decision_status") != "executed_paper" or not item.get("paper_order"):
                continue
            used += float(item.get("risk_snapshot", {}).get("max_loss_pct", 0))
        return used
