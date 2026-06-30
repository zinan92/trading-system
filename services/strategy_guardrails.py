from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json


class StrategyGuardrails:
    def __init__(self, output_root: Path | None = None) -> None:
        config = load_pipeline_config()
        self.output_root = output_root or ROOT / config.get("output_root", "outputs")

    def run(self, run_date: str) -> dict:
        performance = self._latest(self.output_root / "performance" / f"{run_date}.json") or self._latest(self.output_root / "performance" / "current.json")
        ledger = self._latest(self.output_root / "learning_ledger" / "current.json")
        proposal = self._latest(self.output_root / "strategy_change_proposals" / "current.json")
        data_source = self._latest(self.output_root / "data_source_preflight" / "current.json")
        open_trades = load_json(self.output_root / "paper_trades" / "current.json")
        risk_blocks = load_json(self.output_root / "risk_blocks" / f"{run_date}.json")
        checks = [
            self._data_source(data_source),
            self._open_exposure(performance, open_trades),
            self._drawdown_guard(performance),
            self._learning_guard(ledger, proposal),
            self._risk_block_guard(risk_blocks, ledger),
        ]
        hard_blocks = [item for item in checks if item["status"] == "block"]
        warnings = [item for item in checks if item["status"] == "warn"]
        payload = {
            "run_date": run_date,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": "block" if hard_blocks else ("warn" if warnings else "pass"),
            "allow_new_paper_order": not hard_blocks,
            "checks": checks,
            "summary": {
                "hard_blocks": len(hard_blocks),
                "warnings": len(warnings),
                "passed": sum(1 for item in checks if item["status"] == "pass"),
                "block_reasons": [item["summary"] for item in hard_blocks],
                "warning_reasons": [item["summary"] for item in warnings],
            },
            "source_artifacts": {
                "performance": str(self.output_root / "performance" / f"{run_date}.json"),
                "learning_ledger": str(self.output_root / "learning_ledger" / "current.json"),
                "strategy_change_proposal": str(self.output_root / "strategy_change_proposals" / "current.json"),
                "data_source_preflight": str(self.output_root / "data_source_preflight" / "current.json"),
                "paper_trades": str(self.output_root / "paper_trades" / "current.json"),
            },
        }
        write_json(self.output_root / "strategy_guardrails" / "current.json", [payload])
        write_json(self.output_root / "strategy_guardrails" / f"{run_date}.json", [payload])
        self._write_markdown(run_date, payload)
        return payload

    def allows_new_paper_order(self, run_date: str) -> tuple[bool, dict]:
        guardrails = self.run(run_date)
        return bool(guardrails.get("allow_new_paper_order")), guardrails

    def _data_source(self, data_source: dict) -> dict:
        if not data_source:
            return self._check("data_source", "warn", "Data source preflight is missing; rely on execution preflight before order submission.", {})
        if data_source.get("ready_for_paper"):
            return self._check("data_source", "pass", "Data source allows paper trading.", {"status": data_source.get("status"), "provider": data_source.get("latest_provider")})
        return self._check("data_source", "block", data_source.get("message") or "Data source does not allow paper trading.", data_source)

    def _open_exposure(self, performance: dict, open_trades: list[dict]) -> dict:
        summary = performance.get("summary", {}) if performance else {}
        open_count = int(summary.get("open_trade_count", len(open_trades)) or 0)
        if open_count >= 3:
            return self._check("open_exposure", "block", f"{open_count} open paper trades already exist; do not add exposure.", {"open_trade_count": open_count})
        if open_count >= 2:
            return self._check("open_exposure", "warn", f"{open_count} open paper trades exist; review exposure before adding.", {"open_trade_count": open_count})
        return self._check("open_exposure", "pass", "Open paper exposure is within guardrail.", {"open_trade_count": open_count})

    def _drawdown_guard(self, performance: dict) -> dict:
        summary = performance.get("summary", {}) if performance else {}
        open_r = float(summary.get("open_unrealized_r", 0) or 0)
        net_marked = float(summary.get("net_pnl_marked", 0) or 0)
        if open_r <= -1.0:
            return self._check("open_drawdown", "block", f"Open paper exposure is {open_r:.2f}R; block new exposure until reviewed.", {"open_unrealized_r": open_r, "net_pnl_marked": net_marked})
        if open_r <= -0.5:
            return self._check("open_drawdown", "warn", f"Open paper exposure is {open_r:.2f}R; avoid adding exposure without manual review.", {"open_unrealized_r": open_r, "net_pnl_marked": net_marked})
        return self._check("open_drawdown", "pass", "Open paper drawdown is within guardrail.", {"open_unrealized_r": open_r, "net_pnl_marked": net_marked})

    def _learning_guard(self, ledger: dict, proposal: dict) -> dict:
        proposal_status = str(proposal.get("status", ""))
        learning_state = str(ledger.get("learning_state", ""))
        closed_trades = int(ledger.get("closed_trade_count", 0) or 0)
        if proposal_status == "review_required" or learning_state == "review_losing_regimes_before_parameter_changes":
            return self._check("learning_state", "warn", "Learning ledger requires review before promotion or capital increase; paper sampling may continue.", {"proposal_status": proposal_status, "learning_state": learning_state, "closed_trade_count": closed_trades, "blocks_paper_sampling": False})
        if proposal_status == "hold_parameters" or closed_trades < 20:
            return self._check("learning_state", "warn", "Keep parameters unchanged while collecting at least 20 closed paper trades.", {"proposal_status": proposal_status, "learning_state": learning_state, "closed_trade_count": closed_trades, "blocks_paper_sampling": False})
        return self._check("learning_state", "pass", "Learning ledger does not block new paper exposure.", {"proposal_status": proposal_status, "learning_state": learning_state, "closed_trade_count": closed_trades})

    def _risk_block_guard(self, risk_blocks: list[dict], ledger: dict) -> dict:
        today_blocks = len(risk_blocks)
        hard_safety_blocks = [item for item in risk_blocks if self._is_hard_safety_block(item)]
        cumulative_blocks = int(ledger.get("risk_block_count", 0) or 0)
        review_days = max(1, int(ledger.get("review_days", 1) or 1))
        if hard_safety_blocks:
            return self._check(
                "risk_blocks",
                "block",
                f"{len(hard_safety_blocks)} hard safety block(s) occurred today; stop adding paper exposure.",
                {"today_blocks": today_blocks, "hard_safety_blocks": len(hard_safety_blocks), "latest": hard_safety_blocks[-1], "cumulative_blocks": cumulative_blocks},
            )
        if today_blocks >= 2:
            return self._check("risk_blocks", "warn", f"{today_blocks} non-safety candidate block(s) occurred today; keep collecting paper samples and inspect the funnel.", {"today_blocks": today_blocks, "hard_safety_blocks": 0, "cumulative_blocks": cumulative_blocks})
        if cumulative_blocks > max(3, review_days // 2):
            return self._check("risk_blocks", "warn", "Risk blocks are frequent in the learning ledger; keep size and thresholds unchanged.", {"today_blocks": today_blocks, "cumulative_blocks": cumulative_blocks, "review_days": review_days})
        return self._check("risk_blocks", "pass", "Risk block frequency is within guardrail.", {"today_blocks": today_blocks, "cumulative_blocks": cumulative_blocks, "review_days": review_days})

    def _is_hard_safety_block(self, block: dict) -> bool:
        if not isinstance(block, dict):
            return False
        reason = str(block.get("reason", "")).lower()
        if block.get("portfolio_risk"):
            return True
        if block.get("data_quality"):
            return True
        hard_terms = (
            "daily loss",
            "loss stop",
            "kill switch",
            "reconciliation",
            "drift",
            "orphan",
            "protective",
            "order missing",
            "broker",
            "exchange",
            "data quality",
            "data source",
        )
        return any(term in reason for term in hard_terms)

    def _check(self, name: str, status: str, summary: str, evidence: dict) -> dict:
        return {"name": name, "status": status, "summary": summary, "evidence": evidence}

    def _latest(self, path: Path) -> dict:
        rows = load_json(path)
        return rows[-1] if rows else {}

    def _write_markdown(self, run_date: str, payload: dict) -> None:
        lines = [
            f"# Strategy Guardrails - {run_date}",
            "",
            f"- Status: {payload['status']}",
            f"- Allow new paper order: {payload['allow_new_paper_order']}",
            f"- Hard blocks: {payload['summary']['hard_blocks']}",
            f"- Warnings: {payload['summary']['warnings']}",
            "",
            "## Checks",
        ]
        for check in payload["checks"]:
            lines.append(f"- {check['status']}: {check['name']} - {check['summary']}")
        path = self.output_root / "strategy_guardrails" / f"{run_date}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_strategy_guardrails(run_date: str, output_root: Path | None = None) -> dict:
    return StrategyGuardrails(output_root).run(run_date)
