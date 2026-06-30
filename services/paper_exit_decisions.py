from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from services.journal_store import load_json, write_json
from services.paper_executor import PaperExecutor
from services.paper_exit_monitor import PaperExitMonitor


EXIT_DECISIONS = {"approve_exit", "reject_exit", "hold"}


class PaperExitDecisionQueue:
    def __init__(self, output_root: Path) -> None:
        self.output_root = output_root

    def build(self, run_date: str) -> dict:
        monitor = self._latest_monitor(run_date)
        if not monitor:
            monitor = PaperExitMonitor(self.output_root).run(run_date)
        decisions_path = self.output_root / "paper_exit_decisions" / f"{run_date}.decisions.json"
        decisions = load_json(decisions_path)
        if not decisions_path.exists():
            write_json(decisions_path, decisions)
        performance = self._latest_performance(run_date)
        open_metrics = {
            item.get("trade_id"): item
            for item in performance.get("open_trades", [])
            if item.get("trade_id")
        }
        performance_summary = performance.get("summary") or {}
        decided_trade_ids = {
            item.get("trade_id")
            for item in decisions
            if item.get("decision") in {"approve_exit", "reject_exit"}
        }
        queue = [
            self._queue_item(run_date, item, open_metrics.get(item.get("trade_id"), {}), performance_summary)
            for item in monitor.get("monitors", [])
            if item.get("trade_id") not in decided_trade_ids
        ]
        queue = sorted(queue, key=lambda item: (item["priority_rank"], item["distance_to_stop_pct"]))
        summary = {
            "open_items": len(queue),
            "high_priority": sum(1 for item in queue if item["priority"] == "high"),
            "medium_priority": sum(1 for item in queue if item["priority"] == "medium"),
            "needs_approval": sum(1 for item in queue if item["required_user_action"] == "approve_exit"),
            "recorded_decisions": len(decisions),
        }
        payload = {
            "run_date": run_date,
            "generated_at": self._now(),
            "status": "alert" if summary["high_priority"] else ("review" if queue else "empty"),
            "paper_only": True,
            "auto_close": False,
            "summary": summary,
            "queue": queue,
            "decisions": decisions[-20:],
            "source_artifacts": [
                f"outputs/paper_exit_monitor/{run_date}.json",
                f"outputs/performance/{run_date}.json",
                "outputs/paper_trades/current.json",
                f"outputs/paper_exit_decisions/{run_date}.decisions.json",
            ],
        }
        write_json(self.output_root / "paper_exit_decisions" / f"{run_date}.json", [payload])
        write_json(self.output_root / "paper_exit_decisions" / "current.json", [payload])
        self._write_markdown(run_date, payload)
        return payload

    def record_decision(
        self,
        run_date: str,
        trade_id: str,
        decision: str,
        notes: str = "",
        exit_price: float | None = None,
        exit_reason: str | None = None,
    ) -> dict:
        if decision not in EXIT_DECISIONS:
            raise ValueError(f"decision must be one of {sorted(EXIT_DECISIONS)}")
        queue = self.build(run_date)
        item = next((row for row in queue.get("queue", []) if row.get("trade_id") == trade_id), None)
        if not item:
            raise ValueError(f"trade_id not found in exit decision queue: {trade_id}")
        decision_id = self._decision_id(run_date, trade_id, decision)
        closed_trade = None
        if decision == "approve_exit":
            resolved_reason = exit_reason or item.get("exit_reason") or "manual_exit"
            closed_trade = PaperExecutor(self.output_root).close_trade_manual(
                run_date=run_date,
                trade_id=trade_id,
                exit_price=exit_price or item.get("suggested_exit_price"),
                exit_reason=resolved_reason,
                notes=notes,
                decision_id=decision_id,
            )
        record = {
            "decision_id": decision_id,
            "run_date": run_date,
            "decided_at": self._now(),
            "trade_id": trade_id,
            "ticket_id": item.get("ticket_id", ""),
            "symbol": item.get("symbol", "GOLD"),
            "side": item.get("side", ""),
            "decision": decision,
            "notes": notes,
            "requested_exit_price": exit_price,
            "suggested_exit_price": item.get("suggested_exit_price"),
            "exit_reason": exit_reason or item.get("exit_reason", ""),
            "closed_trade": closed_trade,
            "source_queue_item": item,
        }
        path = self.output_root / "paper_exit_decisions" / f"{run_date}.decisions.json"
        rows = [row for row in load_json(path) if row.get("decision_id") != decision_id]
        rows.append(record)
        write_json(path, rows)
        self.build(run_date)
        return record

    def _queue_item(self, run_date: str, item: dict, performance_item: dict | None = None, performance_summary: dict | None = None) -> dict:
        performance_item = performance_item or {}
        performance_summary = performance_summary or {}
        exit_type = "manual_review"
        required_action = "hold"
        priority = "low"
        suggested_price = float(item.get("latest_price", 0) or 0)
        exit_reason = "manual_exit"
        unrealized_r = self._float(performance_item.get("unrealized_r"), 0.0)
        portfolio_open_r = self._float(performance_summary.get("open_unrealized_r"), 0.0)
        if item.get("stop_touched"):
            exit_type = "stop_touched"
            required_action = "approve_exit"
            priority = "high"
            suggested_price = float(item.get("stop_loss", suggested_price) or suggested_price)
            exit_reason = "manual_stop_review_exit"
        elif item.get("target_touched"):
            exit_type = "target_touched"
            required_action = "approve_exit"
            priority = "high"
            suggested_price = float(item.get("target", suggested_price) or suggested_price)
            exit_reason = "manual_target_review_exit"
        elif portfolio_open_r <= -1.0 and unrealized_r <= -0.5:
            exit_type = "portfolio_risk_reduction"
            required_action = "approve_exit"
            priority = "high"
            exit_reason = "manual_risk_reduction_exit"
        elif unrealized_r <= -0.75:
            exit_type = "trade_drawdown_review"
            required_action = "approve_exit"
            priority = "high"
            exit_reason = "manual_drawdown_exit"
        elif unrealized_r <= -0.5:
            exit_type = "trade_drawdown_review"
            required_action = "approve_exit"
            priority = "medium"
            exit_reason = "manual_drawdown_exit"
        elif float(item.get("distance_to_stop_pct", 999) or 999) <= 0.25:
            exit_type = "near_stop_review"
            required_action = "approve_exit"
            priority = "medium"
            exit_reason = "manual_near_stop_exit"
        elif abs(float(item.get("unrealized_pnl", 0) or 0)) > 0:
            exit_type = "routine_hold_review"
            required_action = "hold"
            priority = "low"
        return {
            "decision_item_id": self._decision_item_id(run_date, str(item.get("trade_id", "")), exit_type),
            "run_date": run_date,
            "trade_id": item.get("trade_id", ""),
            "ticket_id": item.get("ticket_id", ""),
            "symbol": item.get("symbol", "GOLD"),
            "side": item.get("side", ""),
            "exit_type": exit_type,
            "priority": priority,
            "priority_rank": {"high": 0, "medium": 1, "low": 2}.get(priority, 3),
            "required_user_action": required_action,
            "latest_price": item.get("latest_price"),
            "suggested_exit_price": round(suggested_price, 4) if suggested_price else None,
            "exit_reason": exit_reason,
            "stop_loss": item.get("stop_loss"),
            "target": item.get("target"),
            "distance_to_stop_pct": item.get("distance_to_stop_pct", 999999),
            "distance_to_target_pct": item.get("distance_to_target_pct", 999999),
            "unrealized_pnl": item.get("unrealized_pnl", 0),
            "unrealized_r": unrealized_r,
            "portfolio_open_unrealized_r": portfolio_open_r,
            "latest_timestamp": item.get("latest_timestamp", ""),
            "source": "paper_exit_monitor",
        }

    def _latest_monitor(self, run_date: str) -> dict:
        dated = load_json(self.output_root / "paper_exit_monitor" / f"{run_date}.json")
        if dated:
            return dated[-1]
        current = load_json(self.output_root / "paper_exit_monitor" / "current.json")
        return current[-1] if current else {}

    def _latest_performance(self, run_date: str) -> dict:
        dated = load_json(self.output_root / "performance" / f"{run_date}.json")
        if dated:
            return dated[-1]
        current = load_json(self.output_root / "performance" / "current.json")
        return current[-1] if current else {}

    def _float(self, value: object, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _write_markdown(self, run_date: str, payload: dict) -> None:
        lines = [
            f"# Paper Exit Decisions - {run_date}",
            "",
            f"- Status: {payload['status']}",
            f"- Open items: {payload['summary']['open_items']}",
            f"- High priority: {payload['summary']['high_priority']}",
            f"- Paper only: {payload['paper_only']}",
            f"- Auto close: {payload['auto_close']}",
            "",
            "## Queue",
        ]
        if not payload["queue"]:
            lines.append("- No open exit decision items.")
        for item in payload["queue"]:
            lines.append(
                f"- {item['priority']} {item['trade_id']}: {item['exit_type']} "
                f"action={item['required_user_action']} latest={item.get('latest_price')} "
                f"suggested={item.get('suggested_exit_price')} stop_distance={item.get('distance_to_stop_pct')}%"
            )
        lines.extend(["", "## Recorded Decisions"])
        if not payload["decisions"]:
            lines.append("- No recorded exit decisions.")
        for item in payload["decisions"][-10:]:
            lines.append(f"- {item.get('trade_id')}: {item.get('decision')} reason={item.get('exit_reason') or 'n/a'} notes={item.get('notes') or 'n/a'}")
        path = self.output_root / "paper_exit_decisions" / f"{run_date}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _decision_item_id(self, run_date: str, trade_id: str, exit_type: str) -> str:
        return "exit_item_" + hashlib.sha256(f"{run_date}:{trade_id}:{exit_type}".encode("utf-8")).hexdigest()[:10]

    def _decision_id(self, run_date: str, trade_id: str, decision: str) -> str:
        return "exit_decision_" + hashlib.sha256(f"{run_date}:{trade_id}:{decision}".encode("utf-8")).hexdigest()[:10]

    def _now(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
