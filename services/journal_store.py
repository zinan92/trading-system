from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config, load_risk_rules


VALID_DECISIONS = {"executed", "executed_paper", "skipped", "rejected"}


def load_json(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, rows: list[dict]) -> None:
    """Atomically write ``rows`` to ``path`` as pretty-printed JSON.

    A dashboard process can be reading the same path concurrently. Writing
    to a sibling temp file and ``os.replace``-ing it makes the swap atomic
    on POSIX and Windows, so readers either see the previous full payload
    or the new full payload — never a half-written file.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(rows, indent=2, ensure_ascii=False) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


class JournalStore:
    def __init__(self, output_root: Path | None = None) -> None:
        pipeline_config = load_pipeline_config()
        self.output_root = output_root or ROOT / pipeline_config.get("output_root", "outputs")

    def record_decision(
        self,
        run_date: str,
        ticket_id: str,
        decision: str,
        notes: str = "",
        actual_entry: float | None = None,
        actual_size: float | None = None,
        broker_adapter=None,
    ) -> dict:
        if decision not in VALID_DECISIONS:
            raise ValueError(f"decision must be one of {sorted(VALID_DECISIONS)}")

        pending_path = self.output_root / "journal_pending" / f"{run_date}.json"
        pending_rows = load_json(pending_path)
        pending_item = next((item for item in pending_rows if item["ticket_id"] == ticket_id), None)
        if not pending_item:
            raise ValueError(f"ticket_id not found in pending journal: {ticket_id}")
        ticket = self._load_ticket(run_date, ticket_id)

        decisions_path = self.output_root / "journal_decisions" / f"{run_date}.json"
        decisions = [item for item in load_json(decisions_path) if item["ticket_id"] != ticket_id]
        decided_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        paper_order = None
        broker_order = None
        if decision == "executed_paper":
            from services.broker_adapter import BrokerOrderRequest, build_broker_adapter
            from services.paper_executor import PaperExecutor

            self._enforce_daily_risk_limit(ticket, decisions)
            self._enforce_execution_readiness(run_date, require_live=False)
            executor = PaperExecutor(self.output_root)
            latest_price = actual_entry or executor.latest_clean_close(run_date, ticket["asset"])
            # An injected adapter lets the multi-strategy runner route a single
            # `live` strategy through the live broker while the rest stay paper.
            # The live adapter's own gates (live_trading_enabled / dry_run /
            # activation) still apply — injection only selects WHICH strategy may.
            adapter = broker_adapter or build_broker_adapter(self.output_root)
            paper_order = adapter.submit_order(BrokerOrderRequest(
                run_date=run_date,
                ticket=ticket,
                latest_price=latest_price,
                actual_size=actual_size,
            )).to_dict()
        elif decision == "executed" and str(load_pipeline_config().get("execution_mode", "paper")).lower() == "live":
            from services.broker_adapter import BrokerOrderRequest, build_broker_adapter
            from services.paper_executor import PaperExecutor

            self._enforce_daily_risk_limit(ticket, decisions)
            self._enforce_execution_readiness(run_date, require_live=True)
            executor = PaperExecutor(self.output_root)
            latest_price = actual_entry or executor.latest_clean_close(run_date, ticket["asset"])
            adapter = build_broker_adapter(self.output_root)
            broker_order = adapter.submit_order(BrokerOrderRequest(
                run_date=run_date,
                ticket=ticket,
                latest_price=latest_price,
                actual_size=actual_size,
            )).to_dict()
        decision_record = {
            **pending_item,
            "decision_status": decision,
            "decided_at": decided_at,
            "actual_entry": actual_entry,
            "actual_size": actual_size,
            "paper_order": paper_order,
            "broker_order": broker_order,
            "risk_snapshot": {
                "max_loss_pct": float(ticket.get("max_loss_pct", 0)),
                "daily_loss_stop_pct": float(load_risk_rules().get("default", {}).get("daily_loss_stop_pct", 1.25)),
            },
            "notes": notes,
        }
        decisions.append(decision_record)
        write_json(decisions_path, decisions)

        remaining_pending = [item for item in pending_rows if item["ticket_id"] != ticket_id]
        write_json(pending_path, remaining_pending)
        return decision_record

    def _enforce_daily_risk_limit(self, ticket: dict, existing_decisions: list[dict]) -> None:
        rules = load_risk_rules()
        daily_cap = float(rules.get("default", {}).get("daily_loss_stop_pct", 1.25))
        used = 0.0
        for item in existing_decisions:
            if item.get("decision_status") != "executed_paper" or not item.get("paper_order"):
                continue
            if item.get("risk_snapshot"):
                used += float(item["risk_snapshot"].get("max_loss_pct", 0))
            else:
                existing_ticket = self._ticket_from_decision(item)
                used += float(existing_ticket.get("max_loss_pct", 0))
        requested = float(ticket.get("max_loss_pct", 0))
        if used + requested > daily_cap:
            raise ValueError(f"daily paper risk cap exceeded: used {used:.2f}% + requested {requested:.2f}% > cap {daily_cap:.2f}%")

    def _enforce_execution_readiness(self, run_date: str, require_live: bool) -> None:
        preflight = self._latest_preflight(run_date)
        if not preflight:
            raise ValueError("data source preflight missing; run daily/import_official_feed before execution")
        key = "ready_for_live" if require_live else "ready_for_paper"
        if not preflight.get(key, False):
            message = preflight.get("message") or f"data source preflight blocks {'live' if require_live else 'paper'} execution"
            raise ValueError(f"data source preflight blocks {'live' if require_live else 'paper'} execution: {message}")

    def _latest_preflight(self, run_date: str) -> dict:
        dated = load_json(self.output_root / "data_source_preflight" / f"{run_date}.json")
        if dated:
            return dated[-1]
        current = load_json(self.output_root / "data_source_preflight" / "current.json")
        return current[-1] if current else {}

    def _ticket_from_decision(self, item: dict) -> dict:
        ticket_id = item.get("ticket_id", "")
        try:
            run_date = ticket_id.split("_")[2]
            run_date = f"{run_date[:4]}-{run_date[4:6]}-{run_date[6:]}"
            return self._load_ticket(run_date, ticket_id)
        except (IndexError, ValueError):
            return {}

    def _load_ticket(self, run_date: str, ticket_id: str) -> dict:
        ticket_path = self.output_root / "trade_tickets" / f"{run_date}.json"
        ticket = next((item for item in load_json(ticket_path) if item["ticket_id"] == ticket_id), None)
        if not ticket:
            raise ValueError(f"ticket_id not found in trade tickets: {ticket_id}")
        return ticket
