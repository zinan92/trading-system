from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from services.journal_store import load_json, write_json


class PaperTradeAttributor:
    def __init__(self, output_root: Path) -> None:
        self.output_root = output_root

    def run(self, run_date: str) -> dict:
        decisions = load_json(self.output_root / "journal_decisions" / f"{run_date}.json")
        signals = load_json(self.output_root / "signals" / f"{run_date}.json")
        backtests = load_json(self.output_root / "backtests" / f"{run_date}.json")
        tickets = self._load_tickets(run_date)
        open_path = self.output_root / "paper_trades" / "current.json"
        open_trades = load_json(open_path)
        closed_path = self.output_root / "paper_trades" / "closed" / f"{run_date}.json"
        closed_trades = load_json(closed_path)

        updated_open, open_changes = self._attribute_rows(open_trades, decisions, tickets, signals, backtests)
        updated_closed, closed_changes = self._attribute_rows(closed_trades, decisions, tickets, signals, backtests)
        if open_changes:
            write_json(open_path, updated_open)
        if closed_changes or closed_path.exists():
            write_json(closed_path, updated_closed)

        payload = {
            "run_date": run_date,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": "pass",
            "summary": {
                "open_trades": len(open_trades),
                "closed_trades": len(closed_trades),
                "updated_open_trades": open_changes,
                "updated_closed_trades": closed_changes,
                "attributed_open_trades": sum(1 for item in updated_open if item.get("attribution_status") in {"ticket", "signal", "journal_only"}),
                "attributed_closed_trades": sum(1 for item in updated_closed if item.get("attribution_status") in {"ticket", "signal", "journal_only"}),
            },
            "source_artifacts": {
                "journal_decisions": str(self.output_root / "journal_decisions" / f"{run_date}.json"),
                "signals": str(self.output_root / "signals" / f"{run_date}.json"),
                "backtests": str(self.output_root / "backtests" / f"{run_date}.json"),
                "paper_trades": str(open_path),
            },
        }
        write_json(self.output_root / "paper_trade_attribution" / f"{run_date}.json", [payload])
        write_json(self.output_root / "paper_trade_attribution" / "current.json", [payload])
        return payload

    def _attribute_rows(
        self,
        rows: list[dict],
        decisions: list[dict],
        tickets: list[dict],
        signals: list[dict],
        backtests: list[dict],
    ) -> tuple[list[dict], int]:
        by_ticket = {item.get("ticket_id"): item for item in tickets if item.get("ticket_id")}
        decision_by_ticket = {item.get("ticket_id"): item for item in decisions if item.get("ticket_id")}
        signal_by_id = {item.get("signal_id"): item for item in signals if item.get("signal_id")}
        backtest_by_signal = {item.get("signal_id"): item for item in backtests if item.get("signal_id")}
        updated = []
        changes = 0
        for row in rows:
            new_row = dict(row)
            if self._has_attribution(new_row):
                updated.append(new_row)
                continue
            ticket = by_ticket.get(new_row.get("ticket_id"), {})
            decision = decision_by_ticket.get(new_row.get("ticket_id"), {})
            signal_id = ticket.get("signal_id") or decision.get("signal_id") or new_row.get("signal_id", "")
            signal = signal_by_id.get(signal_id, {})
            backtest = ticket.get("backtest") or backtest_by_signal.get(signal_id, {})
            source = "ticket" if ticket else ("signal" if signal else ("journal_only" if signal_id else "unresolved"))

            new_row["signal_id"] = signal_id
            new_row["signal_regime"] = ticket.get("signal_regime") or signal.get("regime") or ("unresolved_legacy" if signal_id else "unknown")
            new_row["signal_strength"] = int(ticket.get("signal_strength") or signal.get("strength") or 0)
            new_row["signal_confidence"] = int(ticket.get("signal_confidence") or signal.get("confidence") or 0)
            new_row["factor_scores"] = ticket.get("factor_scores") or signal.get("factor_scores") or {}
            new_row["backtest_verdict"] = backtest.get("verdict", "")
            new_row["source_artifacts"] = ticket.get("source_artifacts") or signal.get("source_artifacts") or []
            new_row["attribution_status"] = source
            new_row["attributed_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            flags = list(new_row.get("quality_flags", []))
            flag = f"attribution_{source}"
            if flag not in flags:
                flags.append(flag)
            new_row["quality_flags"] = flags
            updated.append(new_row)
            changes += 1
        return updated, changes

    def _has_attribution(self, row: dict) -> bool:
        return bool(row.get("signal_id") and row.get("signal_regime") and row.get("attribution_status"))

    def _load_tickets(self, run_date: str) -> list[dict]:
        rows = load_json(self.output_root / "trade_tickets" / f"{run_date}.json")
        if rows:
            return rows
        root = self.output_root / "trade_tickets"
        if not root.exists():
            return []
        all_rows = []
        for path in sorted(root.glob("*.json")):
            all_rows.extend(load_json(path))
        return all_rows
