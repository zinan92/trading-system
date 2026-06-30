from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipelines.dashboard_server import build_system_status_contract
from services.dashboard_state import DashboardState
from services.journal_store import load_json, write_json


SCHEMA_VERSION = "cycle-audit-v1"


class CycleAuditSink:
    def begin(self, *, run_date: str, strategy_id: str, timeframe: str) -> dict:
        raise NotImplementedError

    def finalize(self, context: dict, *, result: dict | None = None, error: str = "") -> dict:
        raise NotImplementedError


class JsonCycleAuditSink(CycleAuditSink):
    def __init__(self, output_root: Path, *, base_output_root: Path | None = None) -> None:
        self.output_root = Path(output_root)
        self.base_output_root = Path(base_output_root) if base_output_root is not None else self.output_root

    def begin(self, *, run_date: str, strategy_id: str, timeframe: str) -> dict:
        now = _utcnow()
        cycle_id = self._pending_cycle_id(run_date, strategy_id, timeframe)
        record = {
            "schema_version": SCHEMA_VERSION,
            "run_date": run_date,
            "strategy_id": strategy_id,
            "timeframe": timeframe,
            "cycle_id": cycle_id,
            "cycle_timestamp": "",
            "recorded_at": now,
            "status": "in_progress",
            "phase": "cycle_started",
            "audit_gaps": [],
            "data": {"status": "unknown", "reason": "cycle started before data artifacts were written"},
            "strategy_evaluation": {"status": "not_run", "strategies": []},
            "no_ticket_reasons": [],
            "execution": {"status": "not_run", "why_not_executed": []},
            "risk": {"status": "not_run"},
            "orders": {"entry_generated": False, "tp_sl_generated": False, "tp_sl_covered": False, "order_lifecycle_refs": []},
            "system_state": {"status": "unknown"},
            "artifact_refs": {},
        }
        self._upsert(record)
        return {
            "run_date": run_date,
            "strategy_id": strategy_id,
            "timeframe": timeframe,
            "pending_cycle_id": cycle_id,
            "began_at": now,
        }

    def finalize(self, context: dict, *, result: dict | None = None, error: str = "") -> dict:
        run_date = str(context.get("run_date") or "")
        strategy_id = str(context.get("strategy_id") or "")
        timeframe = str(context.get("timeframe") or "")
        began_at = str(context.get("began_at") or _utcnow())
        pending_cycle_id = str(context.get("pending_cycle_id") or self._pending_cycle_id(run_date, strategy_id, timeframe))
        record = self._build_record(
            run_date=run_date,
            strategy_id=strategy_id,
            timeframe=timeframe,
            began_at=began_at,
            result=result or {},
            error=error,
        )
        self._upsert(record, replace_cycle_id=pending_cycle_id)
        return record

    def _build_record(
        self,
        *,
        run_date: str,
        strategy_id: str,
        timeframe: str,
        began_at: str,
        result: dict,
        error: str,
    ) -> dict:
        gaps: list[dict] = []
        artifact_refs = self._artifact_refs(run_date, gaps)
        data = self._data_snapshot(run_date, gaps)
        signals = self._rows("signals", run_date)
        tickets = self._rows("trade_tickets", run_date)
        pending = self._rows("journal_pending", run_date)
        decisions = self._rows("journal_decisions", run_date)
        decision_snapshots = self._rows("decision_snapshots", run_date)
        risk_blocks = self._rows("risk_blocks", run_date)
        risk_monitor = self._latest("risk_monitor", "current.json")
        paper_orders = self._rows("paper_orders", run_date)
        demo_requests = self._rows("demo_order_requests", run_date)
        paper_trades = self._rows("paper_trades", "current.json")
        lifecycle = self._rows("order_lifecycle", run_date)
        order_recovery = self._latest("order_recovery", "current.json")
        live_reconciliation = self._latest("live_reconciliation", "current.json")
        paper_execution_blocks = self._rows("paper_execution_blocks", run_date)
        cycle_timestamp = self._cycle_timestamp(decision_snapshots, data, began_at)
        cycle_id = self._cycle_id(run_date, strategy_id, timeframe, cycle_timestamp)
        no_ticket_reasons = self._no_ticket_reasons(decision_snapshots, risk_blocks, signals)
        system_state = self._system_state_snapshot(
            run_date=run_date,
            strategy_id=strategy_id,
            paper_trades=paper_trades,
            decisions=decisions,
            live_reconciliation=live_reconciliation,
            gaps=gaps,
        )
        execution = self._execution_snapshot(
            result=result,
            tickets=tickets,
            pending=pending,
            decisions=decisions,
            order_recovery=order_recovery,
            paper_execution_blocks=paper_execution_blocks,
            trade_permission=system_state.get("trade_permission", {}),
        )
        orders = self._orders_snapshot(
            tickets=tickets,
            decisions=decisions,
            paper_orders=paper_orders,
            demo_requests=demo_requests,
            lifecycle=lifecycle,
            paper_trades=paper_trades,
        )
        status = "error" if error else "completed"
        record = {
            "schema_version": SCHEMA_VERSION,
            "run_date": run_date,
            "strategy_id": strategy_id,
            "timeframe": timeframe,
            "cycle_id": cycle_id,
            "cycle_timestamp": cycle_timestamp,
            "recorded_at": _utcnow(),
            "began_at": began_at,
            "finalized_at": _utcnow(),
            "status": status,
            "phase": "error" if error else "cycle_finalized",
            "error": error,
            "data": data,
            "strategy_evaluation": self._strategy_evaluation(run_date, strategy_id, signals, decision_snapshots, tickets, gaps),
            "no_ticket_reasons": no_ticket_reasons,
            "execution": execution,
            "risk": self._risk_snapshot(risk_monitor, risk_blocks, gaps),
            "orders": orders,
            "system_state": system_state,
            "artifact_refs": artifact_refs,
            "paper_execution_blocks": self._paper_blocks_snapshot(paper_execution_blocks),
            "reconciliation": self._reconciliation_snapshot(live_reconciliation),
            "protective": self._protective_snapshot(lifecycle, demo_requests, paper_trades),
            "audit_gaps": gaps,
            "result_ref": {
                "status": result.get("status", ""),
                "executed_ticket": result.get("executed_ticket"),
                "execution_error": result.get("execution_error", ""),
                "order_recovery_status": result.get("order_recovery_status", ""),
            },
        }
        if not decision_snapshots and not signals:
            record["audit_gaps"].append(
                {
                    "stage": "strategy_evaluation",
                    "reason_code": "strategy_artifacts_missing",
                    "message": "signals and decision snapshots are both missing",
                }
            )
        record["audit_gaps"] = _dedupe_gaps(record["audit_gaps"])
        return record

    def _data_snapshot(self, run_date: str, gaps: list[dict]) -> dict:
        path = self.output_root / "data_source_preflight" / "current.json"
        payload = _latest_json(path)
        if not payload:
            gaps.append(self._gap("data", "data_source_preflight_missing", path))
            return {
                "status": "audit_gap",
                "freshness": "unknown",
                "reason_code": "data_source_preflight_missing",
                "artifact": str(path),
            }
        ready_for_paper = bool(payload.get("ready_for_paper"))
        latest_fresh = payload.get("latest_record_is_fresh")
        is_fresh = ready_for_paper and latest_fresh is not False and payload.get("status") in {"pass", "warn"}
        freshness = "fresh" if is_fresh else "stale"
        reason_code = "" if is_fresh else "data_stale"
        return {
            "status": payload.get("status", ""),
            "freshness": freshness,
            "reason_code": reason_code,
            "message": payload.get("message", ""),
            "ready_for_paper": ready_for_paper,
            "ready_for_live": bool(payload.get("ready_for_live")),
            "latest_timestamp": payload.get("latest_timestamp", ""),
            "latest_provider": payload.get("latest_provider", ""),
            "latest_record_age_minutes": payload.get("latest_record_age_minutes"),
            "max_public_quote_age_minutes": payload.get("max_public_quote_age_minutes"),
            "latest_record_is_fresh": latest_fresh,
            "basis": {
                "data_quality_allows_trading": payload.get("data_quality_allows_trading"),
                "data_quality_reasons": payload.get("data_quality_reasons", []),
                "price_sanity": payload.get("price_sanity", {}),
            },
            "artifact": str(path),
        }

    def _strategy_evaluation(
        self,
        run_date: str,
        strategy_id: str,
        signals: list[dict],
        decision_snapshots: list[dict],
        tickets: list[dict],
        gaps: list[dict],
    ) -> dict:
        signal_rows = [
            {
                "signal_id": item.get("signal_id", ""),
                "status": item.get("status", ""),
                "direction": item.get("direction", ""),
                "strength": item.get("strength"),
                "confidence": item.get("confidence"),
                "regime": item.get("regime", ""),
            }
            for item in signals
            if isinstance(item, dict)
        ]
        snapshot_rows = [
            {
                "signal_id": item.get("signal_id", ""),
                "final_decision": item.get("final_decision", ""),
                "no_go_reason": item.get("no_go_reason", ""),
                "bar_timestamp": item.get("bar_timestamp", ""),
            }
            for item in decision_snapshots
            if isinstance(item, dict)
        ]
        if not signals:
            gaps.append(self._gap("strategy_evaluation", "signals_missing", self.output_root / "signals" / f"{run_date}.json"))
        return {
            "status": "evaluated" if signals or decision_snapshots else "audit_gap",
            "strategies": [
                {
                    "strategy_id": strategy_id,
                    "signals": signal_rows,
                    "decision_snapshots": snapshot_rows,
                    "signal_count": len(signal_rows),
                    "directional_signal_count": sum(1 for row in signal_rows if row.get("direction") in {"long", "short"}),
                    "ticket_count": len(tickets),
                }
            ],
        }

    def _no_ticket_reasons(self, decision_snapshots: list[dict], risk_blocks: list[dict], signals: list[dict]) -> list[dict]:
        rows: list[dict] = []
        for snapshot in decision_snapshots:
            if not isinstance(snapshot, dict) or snapshot.get("final_decision") != "no_go":
                continue
            reason = str(snapshot.get("no_go_reason") or "")
            source = "decision_snapshots.no_go_reason"
            code = self._reason_code_from_snapshot(snapshot)
            rows.append(
                {
                    "signal_id": snapshot.get("signal_id", ""),
                    "reason_code": code,
                    "reason": reason,
                    "source": source,
                    "risk_block": snapshot.get("risk_block", {}),
                    "direction_bias": snapshot.get("direction_bias", {}),
                    "position_gate": snapshot.get("position_gate", {}),
                }
            )
        if not rows and signals and not any(item.get("direction") in {"long", "short"} for item in signals if isinstance(item, dict)):
            rows.append(
                {
                    "signal_id": signals[0].get("signal_id", ""),
                    "reason_code": "no_signal",
                    "reason": "signal is not directional",
                    "source": "signals.direction",
                }
            )
        if not rows:
            for block in risk_blocks:
                if isinstance(block, dict):
                    rows.append(
                        {
                            "signal_id": block.get("signal_id", ""),
                            "ticket_id": block.get("ticket_id", ""),
                            "reason_code": str(block.get("reason_code") or self._reason_code_from_risk_block(block)),
                            "reason": block.get("reason", ""),
                            "source": "risk_blocks.reason",
                            "risk_block": block,
                        }
                    )
        return rows

    def _execution_snapshot(
        self,
        *,
        result: dict,
        tickets: list[dict],
        pending: list[dict],
        decisions: list[dict],
        order_recovery: dict,
        paper_execution_blocks: list[dict],
        trade_permission: dict,
    ) -> dict:
        executed = [item for item in decisions if item.get("decision_status") in {"executed", "executed_paper"}]
        why_not: list[dict] = []
        if not executed and trade_permission and trade_permission.get("allows_new_order_if_signal") is False:
            blocker = trade_permission.get("primary_blocker", {}) if isinstance(trade_permission.get("primary_blocker"), dict) else {}
            why_not.append(
                {
                    "reason_code": blocker.get("code") or trade_permission.get("status") or "trade_permission_block",
                    "status": trade_permission.get("status", ""),
                    "reason": blocker.get("message") or trade_permission.get("headline") or "canonical trade permission blocks new orders",
                    "source": "trade_permission.primary_blocker",
                }
            )
            return self._execution_payload(tickets, pending, executed, why_not)
        execution_error = str(result.get("execution_error") or "")
        if execution_error:
            why_not.append({"reason_code": "execution_blocked", "reason": execution_error, "source": "multi_strategy_runner.execution_error"})
        if order_recovery.get("blocks_new_orders"):
            why_not.append(
                {
                    "reason_code": "order_recovery_blocked",
                    "reason": order_recovery.get("block_reason", ""),
                    "source": "order_recovery.current",
                }
            )
        for block in paper_execution_blocks:
            if isinstance(block, dict):
                why_not.append(
                    {
                        "reason_code": str(block.get("reason_code") or block.get("operation") or "paper_execution_block"),
                        "reason": block.get("reason") or block.get("message") or block.get("block_reason") or "",
                        "source": "paper_execution_blocks",
                    }
                )
        if tickets and pending and not executed and not why_not:
            why_not.append(
                {
                    "reason_code": "awaiting_execution_decision",
                    "reason": "ticket exists but no executed journal decision is recorded",
                    "source": "journal_pending",
                }
            )
        return self._execution_payload(tickets, pending, executed, why_not)

    def _execution_payload(self, tickets: list[dict], pending: list[dict], executed: list[dict], why_not: list[dict]) -> dict:
        return {
            "status": "executed" if executed else "not_executed" if tickets or pending or why_not else "not_applicable",
            "ticket_count": len(tickets),
            "pending_count": len(pending),
            "executed_count": len(executed),
            "executed_ticket_ids": [item.get("ticket_id") for item in executed],
            "why_not_executed": why_not,
        }

    def _risk_snapshot(self, risk_monitor: dict, risk_blocks: list[dict], gaps: list[dict]) -> dict:
        path = self.output_root / "risk_monitor" / "current.json"
        if not risk_monitor:
            gaps.append(self._gap("risk", "risk_monitor_missing", path))
            return {"status": "audit_gap", "artifact": str(path), "risk_blocks": risk_blocks}
        status = str(risk_monitor.get("status") or "")
        return {
            "status": status,
            "result": "pass" if status == "pass" else "fail" if status == "block" else "warn",
            "kill_switch_active": risk_monitor.get("kill_switch_active"),
            "allow_paper_auto_approve": risk_monitor.get("allow_paper_auto_approve"),
            "reason_codes": [item.get("name", "") for item in risk_monitor.get("checks", []) if item.get("status") in {"block", "warn"}],
            "block_reasons": (risk_monitor.get("summary") or {}).get("block_reasons", []),
            "warning_reasons": (risk_monitor.get("summary") or {}).get("warning_reasons", []),
            "risk_blocks": [
                {
                    "ticket_id": item.get("ticket_id", ""),
                    "signal_id": item.get("signal_id", ""),
                    "reason_code": item.get("reason_code") or self._reason_code_from_risk_block(item),
                    "reason": item.get("reason", ""),
                }
                for item in risk_blocks
                if isinstance(item, dict)
            ],
            "artifact": str(path),
        }

    def _orders_snapshot(
        self,
        *,
        tickets: list[dict],
        decisions: list[dict],
        paper_orders: list[dict],
        demo_requests: list[dict],
        lifecycle: list[dict],
        paper_trades: list[dict],
    ) -> dict:
        lifecycle_refs = [
            {
                "order_id": item.get("order_id", ""),
                "ticket_id": item.get("ticket_id", ""),
                "state": item.get("state", ""),
                "blocked": bool(item.get("blocked")),
                "artifact": str(self.output_root / "order_lifecycle" / f"{item.get('run_date', '') or ''}.json"),
            }
            for item in lifecycle
            if isinstance(item, dict)
        ]
        tp_sl_generated = any(item.get("stop_loss") not in {None, "", 0} or item.get("targets") for item in tickets if isinstance(item, dict))
        tp_sl_covered = any(
            item.get("state") == "protective_attached" for item in lifecycle if isinstance(item, dict)
        ) or any(
            (item.get("stop_loss") not in {None, "", 0} or item.get("target") not in {None, "", 0} or item.get("exchange_managed"))
            for item in paper_trades
            if isinstance(item, dict)
        )
        entry_generated = bool(tickets or paper_orders or demo_requests or lifecycle_refs or any(item.get("decision_status") in {"executed", "executed_paper"} for item in decisions))
        return {
            "entry_generated": entry_generated,
            "ticket_ids": [item.get("ticket_id", "") for item in tickets if isinstance(item, dict)],
            "paper_order_ids": [item.get("order_id", "") for item in paper_orders if isinstance(item, dict)],
            "demo_request_count": len(demo_requests),
            "tp_sl_generated": tp_sl_generated,
            "tp_sl_covered": tp_sl_covered,
            "order_lifecycle_refs": lifecycle_refs,
            "order_lifecycle_artifact": str(self.output_root / "order_lifecycle"),
        }

    def _system_state_snapshot(
        self,
        *,
        run_date: str,
        strategy_id: str,
        paper_trades: list[dict],
        decisions: list[dict],
        live_reconciliation: dict,
        gaps: list[dict],
    ) -> dict:
        system_vitals_path = self.base_output_root / "system_vitals" / "current.json"
        system_vitals = _latest_json(system_vitals_path)
        if not system_vitals:
            gaps.append(self._gap("system_state", "system_vitals_missing", system_vitals_path))
        demo_blocker = DashboardState(self.base_output_root)._demo_blocker(self.output_root, run_date)
        today_count = sum(1 for item in decisions if item.get("decision_status") in {"executed", "executed_paper"})
        payload = {
            "run_date": run_date,
            "strategy_id": strategy_id,
            "system_vitals": system_vitals,
            "performance_board": {
                "active_demo_blocker": demo_blocker,
                "strategies": [
                    {
                        "strategy_id": strategy_id,
                        "open_trades": len([item for item in paper_trades if item.get("status", "open") == "open"]),
                        "today_trade_count": today_count,
                    }
                ],
            },
            "strategy_detail": {
                "open_trades": [item for item in paper_trades if item.get("status", "open") == "open"],
            },
        }
        contract = build_system_status_contract(payload, strategy_id=strategy_id)
        return {
            "trade_permission": contract.get("trade_permission", {}),
            "health": contract.get("health", {}),
            "source": "dashboard_server.build_system_status_contract",
            "live_reconciliation": {
                "confirmation_status": live_reconciliation.get("confirmation_status", ""),
                "system_state": live_reconciliation.get("system_state", ""),
                "reason_code": live_reconciliation.get("reason_code", ""),
                "can_open_new_orders": live_reconciliation.get("can_open_new_orders"),
            },
        }

    def _artifact_refs(self, run_date: str, gaps: list[dict]) -> dict:
        refs: dict[str, dict] = {}
        required = {
            "data_source_preflight": self.output_root / "data_source_preflight" / "current.json",
            "signals": self.output_root / "signals" / f"{run_date}.json",
            "decision_snapshots": self.output_root / "decision_snapshots" / f"{run_date}.json",
            "risk_blocks": self.output_root / "risk_blocks" / f"{run_date}.json",
            "risk_monitor": self.output_root / "risk_monitor" / "current.json",
            "paper_reconciliation": self.output_root / "paper_reconciliation" / "current.json",
            "paper_execution_blocks": self.output_root / "paper_execution_blocks" / f"{run_date}.json",
        }
        optional = {
            "live_reconciliation": self.output_root / "live_reconciliation" / "current.json",
            "order_lifecycle": self.output_root / "order_lifecycle" / f"{run_date}.json",
            "order_recovery": self.output_root / "order_recovery" / "current.json",
            "demo_order_requests": self.output_root / "demo_order_requests" / f"{run_date}.json",
        }
        for name, path in required.items():
            exists = path.exists()
            refs[name] = {"path": str(path), "exists": exists}
            if not exists:
                gaps.append(self._gap(name, f"{name}_missing", path))
        for name, path in optional.items():
            refs[name] = {"path": str(path), "exists": path.exists()}
        return refs

    def _paper_blocks_snapshot(self, rows: list[dict]) -> dict:
        return {
            "count": len(rows),
            "blocks": [
                {
                    "operation": item.get("operation", ""),
                    "reason_code": item.get("reason_code") or item.get("operation") or "paper_execution_block",
                    "reason": item.get("reason") or item.get("message") or item.get("block_reason") or "",
                }
                for item in rows
                if isinstance(item, dict)
            ],
            "artifact": str(self.output_root / "paper_execution_blocks"),
        }

    def _reconciliation_snapshot(self, report: dict) -> dict:
        if not report:
            return {"status": "audit_gap", "artifact": str(self.output_root / "live_reconciliation" / "current.json")}
        return {
            "confirmation_status": report.get("confirmation_status", ""),
            "system_state": report.get("system_state", ""),
            "reason_code": report.get("reason_code", ""),
            "reconciled": report.get("reconciled"),
            "drift_count": report.get("drift_count"),
            "can_open_new_orders": report.get("can_open_new_orders"),
            "artifact": str(self.output_root / "live_reconciliation" / "current.json"),
        }

    def _protective_snapshot(self, lifecycle: list[dict], demo_requests: list[dict], paper_trades: list[dict]) -> dict:
        lifecycle_states = [str(item.get("state") or "") for item in lifecycle if isinstance(item, dict)]
        failed = any(state == "protective_failed" for state in lifecycle_states)
        failed = failed or any(
            str(((item.get("broker_response") or {}).get("protective_status") or "")).lower() in {"failed", "partial"}
            for item in demo_requests
            if isinstance(item, dict)
        )
        covered = any(state == "protective_attached" for state in lifecycle_states) or any(
            item.get("stop_loss") not in {None, "", 0} or item.get("target") not in {None, "", 0} or item.get("exchange_managed")
            for item in paper_trades
            if isinstance(item, dict)
        )
        return {
            "status": "failed" if failed else "covered" if covered else "not_observed",
            "reason_code": "protective_failed" if failed else "",
            "lifecycle_states": lifecycle_states,
            "artifact": str(self.output_root / "order_lifecycle"),
        }

    def _cycle_timestamp(self, decision_snapshots: list[dict], data: dict, began_at: str) -> str:
        for item in reversed(decision_snapshots):
            if isinstance(item, dict) and item.get("bar_timestamp"):
                return str(item["bar_timestamp"])
        if data.get("latest_timestamp"):
            return str(data["latest_timestamp"])
        return began_at

    def _cycle_id(self, run_date: str, strategy_id: str, timeframe: str, cycle_timestamp: str) -> str:
        return "|".join([run_date, strategy_id, timeframe, cycle_timestamp or "unknown"])

    def _pending_cycle_id(self, run_date: str, strategy_id: str, timeframe: str) -> str:
        return "|".join([run_date, strategy_id, timeframe, "in_progress"])

    def _upsert(self, record: dict, *, replace_cycle_id: str = "") -> None:
        path = self.output_root / "cycle_audit" / f"{record['run_date']}.json"
        cycle_id = str(record.get("cycle_id") or "")
        rows = []
        for item in load_json(path):
            if not isinstance(item, dict):
                continue
            if item.get("cycle_id") in {cycle_id, replace_cycle_id}:
                continue
            rows.append(item)
        existing_final = next((item for item in load_json(path) if isinstance(item, dict) and item.get("cycle_id") == cycle_id and item.get("status") != "in_progress"), None)
        rows.append(existing_final or record)
        write_json(path, rows)
        write_json(self.output_root / "cycle_audit" / "current.json", [existing_final or record])

    def _latest(self, folder: str, filename: str) -> dict:
        return _latest_json(self.output_root / folder / filename)

    def _rows(self, folder: str, run_date_or_file: str) -> list[dict]:
        filename = run_date_or_file if run_date_or_file.endswith(".json") else f"{run_date_or_file}.json"
        return _list_json(self.output_root / folder / filename)

    def _gap(self, stage: str, reason_code: str, path: Path) -> dict:
        return {
            "stage": stage,
            "reason_code": reason_code,
            "message": f"expected artifact is missing: {path}",
            "artifact": str(path),
        }

    def _reason_code_from_snapshot(self, snapshot: dict) -> str:
        risk_block = snapshot.get("risk_block", {}) if isinstance(snapshot.get("risk_block"), dict) else {}
        if risk_block.get("reason_code"):
            return str(risk_block["reason_code"])
        direction_bias = snapshot.get("direction_bias", {}) if isinstance(snapshot.get("direction_bias"), dict) else {}
        if direction_bias.get("action") == "block":
            return "direction_bias"
        position_gate = snapshot.get("position_gate", {}) if isinstance(snapshot.get("position_gate"), dict) else {}
        if position_gate.get("raw_direction") in {"long", "short"} and position_gate.get("allow_candidate") is False:
            return "position_gate"
        if risk_block:
            return self._reason_code_from_risk_block(risk_block)
        signal = snapshot.get("signal", {}) if isinstance(snapshot.get("signal"), dict) else {}
        if signal.get("direction") not in {"long", "short"}:
            return "no_signal"
        return "no_ticket"

    def _reason_code_from_risk_block(self, block: dict) -> str:
        if block.get("data_quality"):
            return "data_quality"
        if block.get("direction_bias"):
            return "direction_bias"
        if block.get("position_gate"):
            return "position_gate"
        if block.get("portfolio_risk"):
            return "portfolio_risk"
        if block.get("strategy_guardrails"):
            return "strategy_guardrails"
        return "risk_block"


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _load_json_any(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _latest_json(path: Path) -> dict:
    data = _load_json_any(path)
    if isinstance(data, list):
        return data[-1] if data and isinstance(data[-1], dict) else {}
    return data if isinstance(data, dict) else {}


def _list_json(path: Path) -> list[dict]:
    data = _load_json_any(path)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def _dedupe_gaps(gaps: list[dict]) -> list[dict]:
    seen: set[tuple[str, str, str]] = set()
    rows: list[dict] = []
    for gap in gaps:
        if not isinstance(gap, dict):
            continue
        key = (str(gap.get("stage", "")), str(gap.get("reason_code", "")), str(gap.get("artifact", "")))
        if key in seen:
            continue
        seen.add(key)
        rows.append(gap)
    return rows
