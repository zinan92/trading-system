from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json
from services.risk_monitor import RiskMonitor
from services.strategy_guardrails import StrategyGuardrails


class PaperAutoApprovalGate:
    def __init__(self, output_root: Path | None = None) -> None:
        config = load_pipeline_config()
        self.output_root = output_root or ROOT / config.get("output_root", "outputs")

    def evaluate(self, run_date: str, auto_requested: bool) -> dict:
        pending = load_json(self.output_root / "journal_pending" / f"{run_date}.json")
        selected = pending[0] if pending else {}
        guardrail_allowed, guardrails = StrategyGuardrails(self.output_root).allows_new_paper_order(run_date)
        risk_monitor = RiskMonitor(self.output_root).run(run_date)
        reasons: list[str] = []
        status = "allow"
        if not auto_requested:
            status = "skipped"
            reasons.append("paper auto-approve was not requested")
        if not pending:
            status = "skipped" if status != "block" else status
            reasons.append("no pending ticket")
        if risk_monitor.get("kill_switch_active"):
            status = "block"
            reasons.extend((risk_monitor.get("summary") or {}).get("block_reasons", []))
        if not risk_monitor.get("allow_paper_auto_approve", False):
            status = "block"
            reasons.extend((risk_monitor.get("summary") or {}).get("auto_approval_block_reasons", []))
        if not guardrail_allowed:
            status = "block"
            reasons.extend((guardrails.get("summary") or {}).get("block_reasons", []))
        if status == "allow" and not selected:
            status = "skipped"
        payload = {
            "run_date": run_date,
            "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": status,
            "auto_requested": auto_requested,
            "allow_auto_approve": status == "allow",
            "selected_ticket_id": selected.get("ticket_id", ""),
            "pending_count": len(pending),
            "reasons": self._dedupe(reasons),
            "risk_monitor": {
                "status": risk_monitor.get("status"),
                "kill_switch_active": risk_monitor.get("kill_switch_active"),
                "allow_paper_auto_approve": risk_monitor.get("allow_paper_auto_approve"),
                "summary": risk_monitor.get("summary", {}),
            },
            "strategy_guardrails": {
                "status": guardrails.get("status"),
                "allow_new_paper_order": guardrails.get("allow_new_paper_order"),
                "summary": guardrails.get("summary", {}),
            },
            "source_artifacts": {
                "journal_pending": str(self.output_root / "journal_pending" / f"{run_date}.json"),
                "risk_monitor": str(self.output_root / "risk_monitor" / f"{run_date}.json"),
                "strategy_guardrails": str(self.output_root / "strategy_guardrails" / f"{run_date}.json"),
            },
        }
        write_json(self.output_root / "paper_auto_approval_gate" / f"{run_date}.json", [payload])
        write_json(self.output_root / "paper_auto_approval_gate" / "current.json", [payload])
        return payload

    def _dedupe(self, values: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            if not value or value in seen:
                continue
            seen.add(value)
            result.append(value)
        return result
