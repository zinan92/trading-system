from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config, load_risk_rules
from services.journal_store import load_json, write_json


class RiskMonitor:
    def __init__(self, output_root: Path | None = None) -> None:
        config = load_pipeline_config()
        self.output_root = output_root or ROOT / config.get("output_root", "outputs")
        self.rules = load_risk_rules().get("default", {})

    def run(self, run_date: str) -> dict:
        performance = self._latest(self.output_root / "performance" / f"{run_date}.json") or self._latest(self.output_root / "performance" / "current.json")
        equity_curve = self._latest(self.output_root / "equity_curve" / "current.json")
        data_source = self._latest(self.output_root / "data_source_preflight" / "current.json")
        guardrails = self._latest(self.output_root / "strategy_guardrails" / "current.json")
        paper_blocks = load_json(self.output_root / "paper_execution_blocks" / f"{run_date}.json")
        checks = [
            self._data_source_check(data_source),
            self._daily_loss_check(equity_curve),
            self._open_r_check(performance),
            self._open_exposure_check(performance),
            self._strategy_guardrail_check(guardrails),
            self._paper_execution_block_check(paper_blocks, data_source),
        ]
        blocks = [item for item in checks if item["status"] == "block"]
        warnings = [item for item in checks if item["status"] == "warn"]
        auto_approval_warnings = self._auto_approval_blocking_warnings(warnings)
        payload = {
            "run_date": run_date,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": "block" if blocks else ("warn" if warnings else "pass"),
            "kill_switch_active": bool(blocks),
            "allow_paper_auto_approve": not blocks and not auto_approval_warnings,
            "allow_manual_review": True,
            "checks": checks,
            "summary": {
                "blocks": len(blocks),
                "warnings": len(warnings),
                "auto_approval_blocks": len(blocks) + len(auto_approval_warnings),
                "passed": sum(1 for item in checks if item["status"] == "pass"),
                "block_reasons": [item["summary"] for item in blocks],
                "warning_reasons": [item["summary"] for item in warnings],
                "auto_approval_block_reasons": [item["summary"] for item in blocks + auto_approval_warnings],
            },
            "limits": {
                "daily_loss_stop_pct": float(self.rules.get("daily_loss_stop_pct", 1.25)),
                "max_open_unrealized_r_before_block": -1.0,
                "max_open_trades_before_block": 3,
            },
            "source_artifacts": {
                "performance": str(self.output_root / "performance" / f"{run_date}.json"),
                "equity_curve": str(self.output_root / "equity_curve" / "current.json"),
                "data_source_preflight": str(self.output_root / "data_source_preflight" / "current.json"),
                "strategy_guardrails": str(self.output_root / "strategy_guardrails" / "current.json"),
                "paper_execution_blocks": str(self.output_root / "paper_execution_blocks" / f"{run_date}.json"),
            },
        }
        write_json(self.output_root / "risk_monitor" / "current.json", [payload])
        write_json(self.output_root / "risk_monitor" / f"{run_date}.json", [payload])
        self._write_markdown(run_date, payload)
        return payload

    def _auto_approval_blocking_warnings(self, warnings: list[dict]) -> list[dict]:
        auto_blocking_names = {
            "daily_loss",
            "open_unrealized_r",
            "open_exposure",
            "strategy_guardrails",
            "paper_execution_blocks",
        }
        return [item for item in warnings if item.get("name") in auto_blocking_names]

    def _data_source_check(self, data_source: dict) -> dict:
        if not data_source:
            return self._check("data_source", "block", "Data source preflight is missing; block paper auto-approval.", {})
        if not data_source.get("ready_for_paper"):
            return self._check("data_source", "block", data_source.get("message") or "Data source does not allow paper trading.", data_source)
        if not data_source.get("ready_for_live"):
            return self._check("data_source", "warn", "Paper data is usable, but live is blocked because official broker feed is missing.", {"latest_provider": data_source.get("latest_provider"), "ready_for_live": data_source.get("ready_for_live")})
        return self._check("data_source", "pass", "Official broker feed is live-ready.", {"latest_provider": data_source.get("latest_provider")})

    def _daily_loss_check(self, equity_curve: dict) -> dict:
        cap = float(self.rules.get("daily_loss_stop_pct", 1.25))
        if "daily_pnl_pct" not in equity_curve:
            evidence = {
                "daily_loss_stop_pct": cap,
                "current_drawdown_pct": equity_curve.get("current_drawdown_pct"),
                "reason": "daily_pnl_pct missing; lifetime drawdown is not used as a daily kill-switch",
            }
            return self._check("daily_loss", "warn", "Daily PnL is unavailable; keep manual review active.", evidence)
        daily_pnl_pct = float(equity_curve.get("daily_pnl_pct", 0) or 0)
        evidence = {
            "daily_pnl": equity_curve.get("daily_pnl", 0),
            "daily_pnl_pct": daily_pnl_pct,
            "daily_loss_stop_pct": cap,
            "day_start_equity": equity_curve.get("day_start_equity"),
            "current_equity": equity_curve.get("current_equity"),
            "current_drawdown_pct": equity_curve.get("current_drawdown_pct"),
            "max_drawdown_pct": equity_curve.get("max_drawdown_pct"),
        }
        if daily_pnl_pct <= -cap:
            return self._check("daily_loss", "block", f"Daily PnL {daily_pnl_pct:.2f}% breached daily loss stop {cap:.2f}%.", evidence)
        if daily_pnl_pct <= -(cap * 0.7):
            return self._check("daily_loss", "warn", f"Daily PnL {daily_pnl_pct:.2f}% is close to daily loss stop {cap:.2f}%.", evidence)
        return self._check("daily_loss", "pass", "Daily PnL is within limit.", evidence)

    def _open_r_check(self, performance: dict) -> dict:
        summary = performance.get("summary", {}) if performance else {}
        open_r = float(summary.get("open_unrealized_r", 0) or 0)
        if open_r <= -1.0:
            return self._check("open_unrealized_r", "block", f"Open paper exposure is {open_r:.2f}R; block new paper auto-approval.", summary)
        if open_r <= -0.5:
            return self._check("open_unrealized_r", "warn", f"Open paper exposure is {open_r:.2f}R; manual review required before adding exposure.", summary)
        return self._check("open_unrealized_r", "pass", "Open unrealized R is within monitor limit.", {"open_unrealized_r": open_r})

    def _open_exposure_check(self, performance: dict) -> dict:
        summary = performance.get("summary", {}) if performance else {}
        open_count = int(summary.get("open_trade_count", 0) or 0)
        if open_count >= 3:
            return self._check("open_exposure", "block", f"{open_count} open paper trades; block additional auto-approved exposure.", summary)
        if open_count >= 2:
            return self._check("open_exposure", "warn", f"{open_count} open paper trades; avoid adding exposure without review.", summary)
        return self._check("open_exposure", "pass", "Open exposure count is within monitor limit.", {"open_trade_count": open_count})

    def _strategy_guardrail_check(self, guardrails: dict) -> dict:
        if not guardrails:
            return self._check("strategy_guardrails", "warn", "Strategy guardrails are missing; keep manual review active.", {})
        if guardrails.get("status") == "block" or guardrails.get("allow_new_paper_order") is False:
            return self._check("strategy_guardrails", "block", "Strategy guardrails block new paper exposure.", guardrails)
        if guardrails.get("status") == "warn":
            return self._check("strategy_guardrails", "warn", "Strategy guardrails require caution before new paper exposure.", guardrails.get("summary", {}))
        return self._check("strategy_guardrails", "pass", "Strategy guardrails pass.", guardrails.get("summary", {}))

    def _paper_execution_block_check(self, paper_blocks: list[dict], data_source: dict) -> dict:
        if not paper_blocks:
            return self._check("paper_execution_blocks", "pass", "No paper execution blocks recorded today.", {"count": 0})
        if data_source.get("ready_for_paper"):
            return self._check("paper_execution_blocks", "warn", "Historical paper execution blocks exist, but current data source allows paper trading.", {"count": len(paper_blocks), "latest": paper_blocks[-1]})
        return self._check("paper_execution_blocks", "block", "Paper execution blocks exist and current data source is not ready.", {"count": len(paper_blocks), "latest": paper_blocks[-1]})

    def _check(self, name: str, status: str, summary: str, evidence: dict) -> dict:
        return {"name": name, "status": status, "summary": summary, "evidence": evidence}

    def _latest(self, path: Path) -> dict:
        rows = load_json(path)
        return rows[-1] if rows else {}

    def _write_markdown(self, run_date: str, payload: dict) -> None:
        lines = [
            f"# Risk Monitor - {run_date}",
            "",
            f"- Status: {payload['status']}",
            f"- Kill switch active: {payload['kill_switch_active']}",
            f"- Allow paper auto-approve: {payload['allow_paper_auto_approve']}",
            f"- Blocks: {payload['summary']['blocks']}",
            f"- Warnings: {payload['summary']['warnings']}",
            f"- Auto-approval blocks: {payload['summary']['auto_approval_blocks']}",
            "",
            "## Checks",
        ]
        for check in payload["checks"]:
            lines.append(f"- {check['status']}: {check['name']} - {check['summary']}")
        path = self.output_root / "risk_monitor" / f"{run_date}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
