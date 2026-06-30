from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json


class NoTradeDiagnostics:
    def __init__(self, output_root: Path | None = None) -> None:
        config = load_pipeline_config()
        self.output_root = output_root or ROOT / config.get("output_root", "outputs")

    def run(self, run_date: str) -> dict:
        runner = self._latest_mapping(self.output_root / "runner_status" / "current.json")
        gate = self._latest_mapping(self.output_root / "paper_auto_approval_gate" / "current.json")
        risk = self._latest_mapping(self.output_root / "risk_monitor" / "current.json")
        strategies = [self._strategy_status(path, run_date) for path in sorted((self.output_root / "strategies").glob("gold_*"))]
        global_status = self._namespace_status(self.output_root, "global", run_date)
        payload = {
            "run_date": run_date,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "runner": {
                "state": runner.get("state", ""),
                "updated_at": runner.get("updated_at", ""),
                "next_run_at": runner.get("next_run_at", ""),
                "interval_seconds": runner.get("interval_seconds"),
            },
            "global_auto_gate": {
                "status": gate.get("status", runner.get("paper_auto_gate_status", "")),
                "allow_auto_approve": gate.get("allow_auto_approve", runner.get("paper_auto_gate_allow")),
                "reasons": gate.get("reasons", runner.get("paper_auto_gate_reasons", [])),
            },
            "risk": {
                "status": risk.get("status", runner.get("risk_monitor_status", "")),
                "kill_switch_active": risk.get("kill_switch_active", runner.get("risk_kill_switch_active")),
                "summary": risk.get("summary", {}),
            },
            "global": global_status,
            "strategies": strategies,
        }
        write_json(self.output_root / "diagnostics" / "no_trade_current.json", [payload])
        write_json(self.output_root / "diagnostics" / f"no_trade_{run_date}.json", [payload])
        return payload

    def _strategy_status(self, root: Path, run_date: str) -> dict:
        return self._namespace_status(root, root.name, run_date)

    def _namespace_status(self, root: Path, name: str, run_date: str) -> dict:
        signals = load_json(root / "signals" / f"{run_date}.json")
        tickets = load_json(root / "trade_tickets" / f"{run_date}.json")
        pending = load_json(root / "journal_pending" / f"{run_date}.json")
        decisions = load_json(root / "journal_decisions" / f"{run_date}.json")
        orders = load_json(root / "paper_orders" / f"{run_date}.json")
        trades = load_json(root / "paper_trades" / "current.json")
        reconciliation = self._latest_mapping(root / "paper_reconciliation" / f"{run_date}.json")
        signal = signals[-1] if signals else {}
        open_trades = [item for item in trades if item.get("status", "open") == "open"]
        reasons = self._reasons(signal, tickets, pending, orders, reconciliation, open_trades)
        return {
            "namespace": name,
            "signal_status": signal.get("status", ""),
            "signal_direction": signal.get("direction", ""),
            "signal_strength": signal.get("strength", 0),
            "signal_id": signal.get("signal_id", ""),
            "tickets_today": len(tickets),
            "pending_today": len(pending),
            "decisions_today": len(decisions),
            "orders_today": len(orders),
            "filled_orders_today": sum(1 for item in orders if item.get("status") == "filled"),
            "open_trades": len(open_trades),
            "reconciliation_status": reconciliation.get("status", ""),
            "reasons": reasons,
        }

    def _reasons(
        self,
        signal: dict,
        tickets: list[dict],
        pending: list[dict],
        orders: list[dict],
        reconciliation: dict,
        open_trades: list[dict],
    ) -> list[str]:
        reasons: list[str] = []
        if not signal:
            reasons.append("no signal artifact")
        elif signal.get("status") == "no_signal":
            reasons.append("strategy emitted no_signal")
        elif not tickets:
            strength = int(signal.get("strength", 0) or 0)
            reasons.append(f"signal did not become trade ticket; strength={strength}")
        if tickets and not pending and not orders:
            reasons.append("ticket exists but no pending journal/order remains today")
        if pending:
            reasons.append("ticket is waiting in pending journal")
        if orders:
            filled = sum(1 for item in orders if item.get("status") == "filled")
            reasons.append(f"{filled}/{len(orders)} paper order(s) filled today")
        if open_trades:
            sides = sorted({str(item.get("side", "")) for item in open_trades if item.get("side")})
            reasons.append(f"{len(open_trades)} open paper trade(s); sides={','.join(sides)}")
        if reconciliation and reconciliation.get("status") != "pass":
            reasons.append(f"paper reconciliation {reconciliation.get('status')}")
        return reasons

    def _latest_mapping(self, path: Path) -> dict:
        rows = load_json(path)
        if isinstance(rows, list):
            return rows[-1] if rows else {}
        return rows if isinstance(rows, dict) else {}


def run_no_trade_diagnostics(run_date: str, output_root: Path | None = None) -> dict:
    return NoTradeDiagnostics(output_root=output_root).run(run_date)
