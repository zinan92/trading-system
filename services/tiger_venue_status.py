from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json


class TigerVenueStatus:
    """Read model for Tiger venue state in the dual-track console.

    This service reads local artifacts only. It never opens a Tiger SDK client
    and never submits, cancels, modifies, or closes orders.
    """

    def __init__(self, output_root: Path | None = None, checked_at: str | datetime | None = None) -> None:
        config = load_pipeline_config()
        self.output_root = Path(output_root) if output_root else ROOT / config.get("output_root", "outputs")
        self.checked_at = self._parse_time(checked_at) if checked_at is not None else None

    def snapshot(self) -> dict[str, Any]:
        reconciliation = self._artifact("tiger_reconciliation/current.json")
        order_sync = self._artifact("tiger_order_sync/current.json")
        kill_switch = self._artifact("tiger_kill_switch/current.json")
        contract_status = self._artifact("tiger_contracts/current.json")
        account_sync = self._artifact("tiger_account_sync/current.json")
        paper_order_drill = self._artifact("tiger_paper_order_drill/current.json")
        paper_order_readiness = self._artifact("tiger_paper_order_readiness/current.json")
        paper_order_refresh_runbook = self._artifact("tiger_paper_order_readiness/refresh_runbook_current.json")
        paper_order_approval = self._artifact("tiger_paper_order_approval/current.json")
        realtime_validation = self._artifact("tiger_realtime_validation/current.json")
        price_feed_readiness = self._artifact("tiger_price_feed_readiness/current.json")
        price_feed_acceptance = self._artifact("tiger_price_feed_acceptance/current.json")
        data_source_preflight = self._artifact("data_source_preflight/MGCmain_1m/current.json")
        status = self._overall_status(
            reconciliation["payload"],
            order_sync["payload"],
            kill_switch["payload"],
            contract_status["payload"],
            account_sync["payload"],
            paper_order_drill["payload"],
            paper_order_readiness["payload"],
            paper_order_approval["payload"],
            realtime_validation["payload"],
        )
        recent_fills = self._recent_fills(order_sync["payload"])
        open_orders = self._open_orders(order_sync["payload"])
        network_modification_observed = bool(
            kill_switch["payload"].get("network_order_created")
            or kill_switch["payload"].get("network_cancel_created")
        )
        paper_order_readiness_summary = self._paper_order_readiness_summary(paper_order_readiness)
        can_enter_attended_paper_order = (
            status == "ready"
            and paper_order_readiness_summary.get("ready_for_attended_paper_order") is True
            and paper_order_readiness_summary.get("can_submit_without_explicit_operator_authorization") is False
        )
        return {
            "provider": "tiger_openapi",
            "mode": "paper",
            "status": status,
            "headline": self._headline(
                status,
                reconciliation["payload"],
                order_sync["payload"],
                kill_switch["payload"],
                contract_status["payload"],
                account_sync["payload"],
                paper_order_drill["payload"],
                paper_order_readiness["payload"],
                paper_order_approval["payload"],
                realtime_validation["payload"],
            ),
            "can_open_new_orders": reconciliation["payload"].get("can_open_new_orders") is True,
            "can_enter_attended_paper_order": can_enter_attended_paper_order,
            "reconciliation": {
                "artifact": reconciliation["path"],
                "available": reconciliation["available"],
                "error": reconciliation["error"],
                "confirmation_status": str(reconciliation["payload"].get("confirmation_status") or "missing"),
                "system_state": str(reconciliation["payload"].get("system_state") or ""),
                "reason_code": str(reconciliation["payload"].get("reason_code") or ""),
                "drift_count": int(reconciliation["payload"].get("drift_count") or 0),
                "position_count": len(reconciliation["payload"].get("exchange_positions") or []),
                "open_order_count": len(reconciliation["payload"].get("exchange_open_orders") or []),
                "checked_at": str(reconciliation["payload"].get("checked_at") or ""),
            },
            "order_sync": {
                "artifact": order_sync["path"],
                "available": order_sync["available"],
                "error": order_sync["error"] or str(order_sync["payload"].get("error") or ""),
                "sync_status": str(order_sync["payload"].get("sync_status") or "missing"),
                "open_order_count": int(order_sync["payload"].get("open_order_count") or 0),
                "filled_order_count": int(order_sync["payload"].get("filled_order_count") or 0),
                "checked_at": str(order_sync["payload"].get("checked_at") or ""),
                "query": order_sync["payload"].get("query", {}) if isinstance(order_sync["payload"].get("query"), dict) else {},
            },
            "kill_switch": {
                "artifact": kill_switch["path"],
                "available": kill_switch["available"],
                "error": kill_switch["error"],
                "status": str(kill_switch["payload"].get("status") or "missing"),
                "network_order_created": bool(kill_switch["payload"].get("network_order_created", False)),
                "network_cancel_created": bool(kill_switch["payload"].get("network_cancel_created", False)),
                "network_actions": kill_switch["payload"].get("network_actions", {}) if isinstance(kill_switch["payload"].get("network_actions"), dict) else {},
                "checked_at": str(kill_switch["payload"].get("checked_at") or ""),
            },
            "contract": self._contract_summary(contract_status),
            "account_sync": self._account_sync_summary(account_sync),
            "paper_order_drill": self._paper_order_drill_summary(paper_order_drill),
            "paper_order_readiness": paper_order_readiness_summary,
            "paper_order_refresh_runbook": self._paper_order_refresh_runbook_summary(paper_order_refresh_runbook, paper_order_readiness_summary),
            "paper_order_approval": self._paper_order_approval_summary(paper_order_approval),
            "realtime_validation": self._realtime_validation_summary(realtime_validation),
            "price_feed_readiness": self._price_feed_readiness_summary(price_feed_readiness),
            "price_feed_acceptance": self._price_feed_acceptance_summary(price_feed_acceptance),
            "data_source_preflight": self._data_source_preflight_summary(data_source_preflight),
            "safety": {
                "dashboard_read_only": True,
                "network_modification_observed": network_modification_observed,
                "frontend_order_controls": "dualtrack_paper_only",
                "control_endpoint_registered": False,
            },
            "open_orders": open_orders,
            "recent_fills": recent_fills,
            "checked_at": self._now(),
        }

    def _artifact(self, relative: str) -> dict[str, Any]:
        path = self.output_root / relative
        try:
            rows = load_json(path)
        except Exception as exc:
            return {"path": str(path), "available": False, "payload": {}, "error": f"{type(exc).__name__}: {exc}"}
        if not isinstance(rows, list) or not rows:
            return {"path": str(path), "available": False, "payload": {}, "error": "artifact missing or empty"}
        payload = rows[-1] if isinstance(rows[-1], dict) else {}
        return {"path": str(path), "available": bool(payload), "payload": payload, "error": "" if payload else "artifact payload is not an object"}

    def _overall_status(self, reconciliation: dict, order_sync: dict, kill_switch: dict, contract_status: dict, account_sync: dict, paper_order_drill: dict, paper_order_readiness: dict, paper_order_approval: dict, realtime_validation: dict) -> str:
        resolution = contract_status.get("resolution", {}) if isinstance(contract_status.get("resolution"), dict) else {}
        if resolution and resolution.get("ready") is not True:
            return "blocked"
        recon_status = str(reconciliation.get("confirmation_status") or "")
        if recon_status in {"cannot_confirm", "confirmed_drift"} or reconciliation.get("suspected_naked_position"):
            return "blocked"
        if recon_status != "confirmed_flat":
            return "unknown"
        if kill_switch.get("network_order_created") or kill_switch.get("network_cancel_created"):
            return "manual_review"
        if str(order_sync.get("sync_status") or "") not in {"synced"}:
            return "degraded"
        if account_sync and not self._account_sync_ready(account_sync):
            return "degraded"
        if paper_order_drill and str(paper_order_drill.get("status") or "") != "pass":
            return "degraded"
        if paper_order_readiness and str(paper_order_readiness.get("status") or "") != "ready_for_attended_paper_order":
            return "degraded"
        if paper_order_approval:
            if paper_order_approval.get("submit_requested") or paper_order_approval.get("real_tiger_network_call_attempted"):
                return "manual_review"
            if str(paper_order_approval.get("status") or "") != "ready_for_operator_approval":
                return "degraded"
        if realtime_validation and self._realtime_validation_needs_review(realtime_validation):
            return "degraded"
        return "ready"

    def _headline(self, status: str, reconciliation: dict, order_sync: dict, kill_switch: dict, contract_status: dict, account_sync: dict, paper_order_drill: dict, paper_order_readiness: dict, paper_order_approval: dict, realtime_validation: dict) -> str:
        if status == "ready":
            if paper_order_approval and str(paper_order_approval.get("status") or "") == "ready_for_operator_approval":
                return "Tiger paper venue is flat; attended paper approval runbook is ready."
            return "Tiger paper venue is flat and order/fill sync is current."
        if status == "blocked":
            resolution = contract_status.get("resolution", {}) if isinstance(contract_status.get("resolution"), dict) else {}
            if resolution and resolution.get("ready") is not True:
                return str(resolution.get("block_reason") or resolution.get("status") or "Tiger contract resolution blocks new orders")
            return str(reconciliation.get("reason_code") or reconciliation.get("confirmation_status") or "Tiger reconciliation blocks new orders")
        if status == "manual_review":
            if paper_order_approval and (
                paper_order_approval.get("submit_requested") or paper_order_approval.get("real_tiger_network_call_attempted")
            ):
                return "Tiger paper approval artifact indicates submit/network activity; verify account state before proceeding."
            return "Tiger kill-switch network action was observed; verify account state before clearing HALT."
        if status == "degraded":
            if account_sync and not self._account_sync_ready(account_sync):
                return str(account_sync.get("error") or "Tiger account/balance sync is not current")
            if paper_order_drill and str(paper_order_drill.get("status") or "") != "pass":
                return "Tiger paper order drill is not passing."
            if paper_order_readiness and str(paper_order_readiness.get("status") or "") != "ready_for_attended_paper_order":
                return "Tiger attended paper order readiness is blocked."
            if paper_order_approval and str(paper_order_approval.get("status") or "") != "ready_for_operator_approval":
                return "Tiger attended paper order approval runbook is blocked."
            if realtime_validation and self._realtime_validation_needs_review(realtime_validation):
                return str(realtime_validation.get("message") or "Tiger realtime market-data validation needs review.")
            return str(order_sync.get("error") or "Tiger order/fill sync is not current")
        if not reconciliation:
            return "Tiger reconciliation artifact is missing."
        return "Tiger venue state is unknown."

    def _open_orders(self, order_sync: dict) -> list[dict[str, Any]]:
        return [self._order_summary(row) for row in (order_sync.get("exchange_open_orders") or []) if isinstance(row, dict)][:12]

    def _recent_fills(self, order_sync: dict) -> list[dict[str, Any]]:
        rows = [row for row in (order_sync.get("exchange_filled_orders") or []) if isinstance(row, dict)]
        return [self._order_summary(row) for row in rows[-12:]]

    def _order_summary(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "symbol": str(row.get("symbol") or ""),
            "order_id": str(row.get("order_id") or ""),
            "side": str(row.get("side") or ""),
            "type": str(row.get("type") or ""),
            "status": str(row.get("status") or ""),
            "quantity": row.get("quantity"),
            "filled_quantity": row.get("filled_quantity"),
            "average_fill_price": row.get("average_fill_price"),
            "filled_at": str(row.get("filled_at") or ""),
            "source": str(row.get("source") or ""),
        }

    def _contract_summary(self, contract_status: dict[str, Any]) -> dict[str, Any]:
        payload = contract_status["payload"]
        resolution = payload.get("resolution", {}) if isinstance(payload.get("resolution"), dict) else {}
        return {
            "artifact": contract_status["path"],
            "available": contract_status["available"],
            "error": contract_status["error"],
            "requested_symbol": str(resolution.get("requested_symbol") or ""),
            "execution_symbol": str(resolution.get("execution_symbol") or ""),
            "status": str(resolution.get("status") or "missing"),
            "ready": resolution.get("ready") is True,
            "block_reason": str(resolution.get("block_reason") or ""),
            "days_to_contract_month": resolution.get("days_to_contract_month"),
            "checked_at": str(payload.get("checked_at") or ""),
        }

    def _account_sync_summary(self, account_sync: dict[str, Any]) -> dict[str, Any]:
        payload = account_sync["payload"]
        balance = payload.get("exchange_balance", {}) if isinstance(payload.get("exchange_balance"), dict) else {}
        accounting = payload.get("exchange_accounting", {}) if isinstance(payload.get("exchange_accounting"), dict) else {}
        observation = payload.get("account_observation", {}) if isinstance(payload.get("account_observation"), dict) else {}
        return {
            "artifact": account_sync["path"],
            "available": account_sync["available"],
            "error": account_sync["error"] or str(payload.get("error") or ""),
            "sync_status": str(payload.get("sync_status") or "missing"),
            "account_observed": observation.get("account_observed") is True,
            "balance_present": balance.get("balance_present") is True,
            "accounting_observed": observation.get("accounting_observed") is True,
            "segment_key": str(observation.get("segment_key") or ""),
            "asset": str(balance.get("asset") or ""),
            "balance": balance.get("balance"),
            "available_for_trade": balance.get("available"),
            "net_realized_pnl_estimate": accounting.get("net_realized_pnl_estimate"),
            "unrealized_pnl_estimate": accounting.get("unrealized_pnl_estimate"),
            "checked_at": str(payload.get("checked_at") or ""),
        }

    def _account_sync_ready(self, account_sync: dict[str, Any]) -> bool:
        if str(account_sync.get("sync_status") or "") != "synced":
            return False
        observation = account_sync.get("account_observation", {}) if isinstance(account_sync.get("account_observation"), dict) else {}
        balance = account_sync.get("exchange_balance", {}) if isinstance(account_sync.get("exchange_balance"), dict) else {}
        accounting = account_sync.get("exchange_accounting", {}) if isinstance(account_sync.get("exchange_accounting"), dict) else {}
        return bool(
            observation.get("account_observed") is True
            and balance.get("balance_present") is True
            and accounting.get("net_realized_pnl_estimate") is not None
        )

    def _paper_order_drill_summary(self, paper_order_drill: dict[str, Any]) -> dict[str, Any]:
        payload = paper_order_drill["payload"]
        scenarios = [row for row in (payload.get("scenarios") or []) if isinstance(row, dict)]
        return {
            "artifact": paper_order_drill["path"],
            "available": paper_order_drill["available"],
            "error": paper_order_drill["error"],
            "status": str(payload.get("status") or "missing"),
            "run_id": str(payload.get("run_id") or ""),
            "mode": str(payload.get("mode") or ""),
            "real_tiger_network_call_attempted": bool(payload.get("real_tiger_network_call_attempted", False)),
            "scenario_count": len(scenarios),
            "passing_scenarios": sum(1 for row in scenarios if row.get("status") == "pass"),
            "checked_at": str(payload.get("finished_at") or ""),
        }

    def _paper_order_readiness_summary(self, paper_order_readiness: dict[str, Any]) -> dict[str, Any]:
        payload = paper_order_readiness["payload"]
        checks = [row for row in (payload.get("checks") or []) if isinstance(row, dict)]
        blockers = [row for row in (payload.get("blockers") or []) if isinstance(row, dict)]
        stale_blockers = [
            row
            for row in blockers
            if isinstance(row.get("evidence"), dict) and row["evidence"].get("current_for_run_date") is False
        ]
        return {
            "artifact": paper_order_readiness["path"],
            "available": paper_order_readiness["available"],
            "error": paper_order_readiness["error"],
            "status": str(payload.get("status") or "missing"),
            "ready_for_attended_paper_order": payload.get("ready_for_attended_paper_order") is True,
            "can_submit_without_explicit_operator_authorization": payload.get("can_submit_without_explicit_operator_authorization") is True,
            "real_tiger_network_call_attempted": bool(payload.get("real_tiger_network_call_attempted", False)),
            "check_count": len(checks),
            "blocker_count": len(blockers),
            "blocker_names": [str(row.get("name") or "") for row in blockers if row.get("name")],
            "stale_evidence_count": len(stale_blockers),
            "operator_next_action": self._paper_order_readiness_operator_next_action(payload, blockers, stale_blockers),
            "checked_at": str(payload.get("checked_at") or ""),
        }

    def _paper_order_readiness_operator_next_action(
        self,
        payload: dict[str, Any],
        blockers: list[dict[str, Any]],
        stale_blockers: list[dict[str, Any]],
    ) -> dict[str, Any]:
        status = str(payload.get("status") or "missing")
        steps = self._paper_order_readiness_refresh_steps(payload)
        opens_trade_client = any(step["opens_trade_client"] for step in steps)
        if status == "ready_for_attended_paper_order":
            action_status = "ready_for_operator_review"
            summary = "Paper-order evidence is current; explicit operator authorization is still required."
        elif stale_blockers:
            action_status = "refresh_stale_evidence"
            summary = f"Refresh {len(stale_blockers)} stale Tiger paper-order evidence artifacts before any attended canary."
        elif blockers:
            action_status = "resolve_blockers"
            summary = f"Resolve {len(blockers)} Tiger paper-order readiness blockers before any attended canary."
        else:
            action_status = status
            summary = f"Tiger paper-order readiness status is {status}."
        return {
            "status": action_status,
            "summary": summary,
            "refresh_step_count": len(steps),
            "stale_evidence_count": len(stale_blockers),
            "blocker_names": [str(row.get("name") or "") for row in blockers if row.get("name")],
            "opens_trade_client_read_only": opens_trade_client,
            "submits_orders": False,
            "writes_runtime_config": False,
            "refresh_steps": steps,
        }

    def _paper_order_readiness_refresh_steps(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        steps = []
        for command in (payload.get("next_commands") or []):
            command_text = str(command)
            if not command_text:
                continue
            steps.append(self._paper_order_readiness_refresh_step(command_text))
        return steps[:8]

    def _paper_order_readiness_refresh_step(self, command: str) -> dict[str, Any]:
        if "tiger_contract_status" in command:
            name = "refresh_contract_status"
            label = "refresh contract"
            opens_trade_client = False
        elif "tiger_openapi_reconciliation" in command:
            name = "refresh_reconciliation"
            label = "refresh reconciliation"
            opens_trade_client = True
        elif "tiger_openapi_account_sync" in command:
            name = "refresh_account_sync"
            label = "refresh account"
            opens_trade_client = True
        elif "tiger_openapi_order_sync" in command:
            name = "refresh_order_sync"
            label = "refresh orders/fills"
            opens_trade_client = True
        elif "tiger_openapi_paper_order_drill" in command:
            name = "refresh_local_drill"
            label = "run local drill"
            opens_trade_client = False
        elif "tiger_openapi_paper_order_readiness" in command:
            name = "refresh_readiness"
            label = "rerun readiness"
            opens_trade_client = False
        else:
            name = "refresh_evidence"
            label = "refresh evidence"
            opens_trade_client = True
        return {
            "name": name,
            "label": label,
            "opens_trade_client": opens_trade_client,
            "opens_trade_client_mode": "read_only" if opens_trade_client else "none",
            "submits_orders": False,
            "writes_runtime_config": False,
        }

    def _paper_order_refresh_runbook_summary(
        self,
        paper_order_refresh_runbook: dict[str, Any],
        current_readiness: dict[str, Any],
    ) -> dict[str, Any]:
        payload = paper_order_refresh_runbook["payload"]
        commands = [row for row in (payload.get("command_sequence") or []) if isinstance(row, dict)]
        generation = payload.get("generation_safety", {}) if isinstance(payload.get("generation_safety"), dict) else {}
        sequence_safety = payload.get("command_sequence_safety", {}) if isinstance(payload.get("command_sequence_safety"), dict) else {}
        source_checked_at = str(payload.get("readiness_checked_at") or "")
        source_status = str(payload.get("readiness_status") or "")
        source_blockers = int(payload.get("blocker_count") or 0)
        source_stale = int(payload.get("stale_evidence_count") or 0)
        source_names = [str(name) for name in (payload.get("blocker_names") or []) if name]
        current_names = [str(name) for name in (current_readiness.get("blocker_names") or []) if name]
        matches_current_readiness = bool(
            payload
            and source_checked_at
            and source_checked_at == str(current_readiness.get("checked_at") or "")
            and source_status == str(current_readiness.get("status") or "")
            and source_blockers == int(current_readiness.get("blocker_count") or 0)
            and source_stale == int(current_readiness.get("stale_evidence_count") or 0)
            and source_names == current_names
        )
        return {
            "artifact": paper_order_refresh_runbook["path"],
            "available": paper_order_refresh_runbook["available"],
            "error": paper_order_refresh_runbook["error"],
            "status": str(payload.get("status") or "missing"),
            "runbook_id": str(payload.get("runbook_id") or ""),
            "run_date": str(payload.get("run_date") or ""),
            "readiness_status": str(payload.get("readiness_status") or ""),
            "source_readiness_checked_at": source_checked_at,
            "current_readiness_checked_at": str(current_readiness.get("checked_at") or ""),
            "matches_current_readiness": matches_current_readiness,
            "blocker_count": int(payload.get("blocker_count") or 0),
            "stale_evidence_count": int(payload.get("stale_evidence_count") or 0),
            "current_blocker_count": int(current_readiness.get("blocker_count") or 0),
            "current_stale_evidence_count": int(current_readiness.get("stale_evidence_count") or 0),
            "command_count": len(commands),
            "opens_trade_client_read_only": sequence_safety.get("opens_trade_client_read_only") is True,
            "submits_orders": sequence_safety.get("submits_orders") is True,
            "writes_runtime_config": sequence_safety.get("writes_runtime_config") is True,
            "generation_opens_trade_client": generation.get("opens_trade_client") is True,
            "generation_submits_orders": generation.get("submits_orders") is True,
            "generation_writes_runtime_config": generation.get("writes_runtime_config") is True,
            "checked_at": str(payload.get("checked_at") or ""),
        }

    def _paper_order_approval_summary(self, paper_order_approval: dict[str, Any]) -> dict[str, Any]:
        payload = paper_order_approval["payload"]
        checks = [row for row in (payload.get("checks") or []) if isinstance(row, dict)]
        blockers = [row for row in (payload.get("blockers") or []) if isinstance(row, dict)]
        canary = payload.get("canary_check", {}) if isinstance(payload.get("canary_check"), dict) else {}
        return {
            "artifact": paper_order_approval["path"],
            "available": paper_order_approval["available"],
            "error": paper_order_approval["error"],
            "status": str(payload.get("status") or "missing"),
            "ticket_id": str(payload.get("ticket_id") or ""),
            "operator": str(payload.get("operator") or ""),
            "submit_requested": bool(payload.get("submit_requested", False)),
            "can_submit_without_explicit_operator_authorization": payload.get("can_submit_without_explicit_operator_authorization") is True,
            "real_tiger_network_call_attempted": bool(payload.get("real_tiger_network_call_attempted", False)),
            "use_attended_canary_risk_limits": payload.get("use_attended_canary_risk_limits") is True,
            "check_count": len(checks),
            "blocker_count": len(blockers),
            "canary_status": str(canary.get("status") or ""),
            "candidate_notional": canary.get("candidate_notional"),
            "candidate_stop_loss": canary.get("candidate_stop_loss"),
            "candidate_stop_loss_pct_of_equity": canary.get("candidate_stop_loss_pct_of_equity"),
            "runbook_available": bool(payload.get("submit_command")) and not bool(payload.get("submit_requested")),
            "checked_at": str(payload.get("checked_at") or ""),
        }

    def _realtime_validation_summary(self, realtime_validation: dict[str, Any]) -> dict[str, Any]:
        payload = realtime_validation["payload"]
        preflight = payload.get("preflight", {}) if isinstance(payload.get("preflight"), dict) else {}
        checks = preflight.get("checks", {}) if isinstance(preflight.get("checks"), dict) else {}
        permission = checks.get("quote_permission", {}) if isinstance(checks.get("quote_permission"), dict) else {}
        next_window = payload.get("next_trading_window", {}) if isinstance(payload.get("next_trading_window"), dict) else {}
        active_session = payload.get("active_session", {}) if isinstance(payload.get("active_session"), dict) else {}
        polls = payload.get("polls", {}) if isinstance(payload.get("polls"), dict) else {}
        market_hours_gate = payload.get("market_hours_gate", {}) if isinstance(payload.get("market_hours_gate"), dict) else {}
        gate_next_window = market_hours_gate.get("next_trading_window", {}) if isinstance(market_hours_gate.get("next_trading_window"), dict) else {}
        return {
            "artifact": realtime_validation["path"],
            "available": realtime_validation["available"],
            "error": realtime_validation["error"],
            "status": str(payload.get("status") or "missing"),
            "message": str(payload.get("message") or ""),
            "contract": str(payload.get("contract") or ""),
            "output_symbol": str(payload.get("output_symbol") or ""),
            "checked_at": str(payload.get("checked_at") or ""),
            "poll_seconds": payload.get("poll_seconds"),
            "max_lag_seconds": payload.get("max_lag_seconds"),
            "bar_advanced": payload.get("bar_advanced"),
            "fresh": payload.get("fresh"),
            "latest_bar_age_seconds": payload.get("latest_bar_age_seconds"),
            "next_trading_window": {
                "start": str(next_window.get("start") or ""),
                "end": str(next_window.get("end") or ""),
                "trading_date": str(next_window.get("trading_date") or ""),
            },
            "active_session": {
                "trading_date": str(active_session.get("trading_date") or ""),
                "timezone": str(active_session.get("timezone") or ""),
            },
            "polls": {
                "first_timestamp": str((polls.get("first") or {}).get("timestamp") or "") if isinstance(polls.get("first"), dict) else "",
                "second_timestamp": str((polls.get("second") or {}).get("timestamp") or "") if isinstance(polls.get("second"), dict) else "",
            },
            "quote_permission": {
                "names": [str(name) for name in (permission.get("names") or [])],
                "has_futures_realtime": permission.get("has_futures_realtime") is True,
            },
            "market_hours_gate": {
                "required": market_hours_gate.get("required") is True,
                "ready_for_price_feed_promotion": market_hours_gate.get("ready_for_price_feed_promotion") is True,
                "market_hours_observed": market_hours_gate.get("market_hours_observed") is True,
                "exit_code": market_hours_gate.get("exit_code"),
                "operator_action": str(market_hours_gate.get("operator_action") or ""),
                "next_trading_window": {
                    "start": str(gate_next_window.get("start") or ""),
                    "end": str(gate_next_window.get("end") or ""),
                    "trading_date": str(gate_next_window.get("trading_date") or ""),
                },
            },
            "safety": {
                "read_only": (payload.get("safety") or {}).get("read_only") is True if isinstance(payload.get("safety"), dict) else False,
                "writes_market_db": (payload.get("safety") or {}).get("writes_market_db") is True if isinstance(payload.get("safety"), dict) else False,
                "opens_trade_client": (payload.get("safety") or {}).get("opens_trade_client") is True if isinstance(payload.get("safety"), dict) else False,
                "submits_orders": (payload.get("safety") or {}).get("submits_orders") is True if isinstance(payload.get("safety"), dict) else False,
            },
        }

    def _realtime_validation_needs_review(self, realtime_validation: dict[str, Any]) -> bool:
        return str(realtime_validation.get("status") or "") in {"fail", "warn"}

    def _price_feed_readiness_summary(self, price_feed_readiness: dict[str, Any]) -> dict[str, Any]:
        payload = price_feed_readiness["payload"]
        summary = payload.get("summary", {}) if isinstance(payload.get("summary"), dict) else {}
        gate = summary.get("market_hours_gate", {}) if isinstance(summary.get("market_hours_gate"), dict) else {}
        blockers = [row for row in (payload.get("blockers") or []) if isinstance(row, dict)]
        safety = payload.get("safety", {}) if isinstance(payload.get("safety"), dict) else {}
        return {
            "artifact": price_feed_readiness["path"],
            "available": price_feed_readiness["available"],
            "error": price_feed_readiness["error"],
            "status": str(payload.get("status") or "missing"),
            "ready_for_price_feed": payload.get("ready_for_price_feed") is True,
            "can_enable_broker_orders_from_this_gate": payload.get("can_enable_broker_orders_from_this_gate") is True,
            "blocker_count": len(blockers),
            "contract": str(payload.get("contract") or ""),
            "feed_status": str(summary.get("feed_status") or ""),
            "imported_rows": summary.get("imported_rows"),
            "latest_timestamp": str(summary.get("latest_timestamp") or ""),
            "realtime_status": str(summary.get("realtime_status") or ""),
            "market_hours_gate": {
                "exit_code": gate.get("exit_code"),
                "operator_action": str(gate.get("operator_action") or ""),
                "ready_for_price_feed_promotion": gate.get("ready_for_price_feed_promotion") is True,
            },
            "safety": {
                "artifact_only": safety.get("artifact_only") is True,
                "opens_tiger_sdk_clients": safety.get("opens_tiger_sdk_clients") is True,
                "writes_market_db": safety.get("writes_market_db") is True,
                "submits_orders": safety.get("submits_orders") is True,
            },
            "checked_at": str(payload.get("checked_at") or ""),
        }

    def _price_feed_acceptance_summary(self, price_feed_acceptance: dict[str, Any]) -> dict[str, Any]:
        payload = price_feed_acceptance["payload"]
        steps = payload.get("steps", {}) if isinstance(payload.get("steps"), dict) else {}
        realtime = steps.get("realtime_validation", {}) if isinstance(steps.get("realtime_validation"), dict) else {}
        gate = realtime.get("market_hours_gate", {}) if isinstance(realtime.get("market_hours_gate"), dict) else {}
        next_window = gate.get("next_trading_window", {}) if isinstance(gate.get("next_trading_window"), dict) else {}
        readiness = steps.get("price_feed_readiness", {}) if isinstance(steps.get("price_feed_readiness"), dict) else {}
        catalog = steps.get("connector_catalog", {}) if isinstance(steps.get("connector_catalog"), dict) else {}
        blockers = [row for row in (payload.get("blockers") or []) if isinstance(row, dict)]
        safety = payload.get("safety", {}) if isinstance(payload.get("safety"), dict) else {}
        operator_state = self._acceptance_operator_next_action(payload, next_window)
        return {
            "artifact": price_feed_acceptance["path"],
            "available": price_feed_acceptance["available"],
            "error": price_feed_acceptance["error"],
            "status": str(payload.get("status") or "missing"),
            "exit_code": payload.get("exit_code"),
            "ready_for_price_feed": payload.get("ready_for_price_feed") is True,
            "can_enable_broker_orders_from_this_gate": payload.get("can_enable_broker_orders_from_this_gate") is True,
            "contract": str(payload.get("contract") or ""),
            "blocker_count": len(blockers),
            "realtime_status": str(realtime.get("status") or ""),
            "operator_action": str(gate.get("operator_action") or ""),
            "next_trading_window": {
                "start": str(next_window.get("start") or ""),
                "end": str(next_window.get("end") or ""),
                "trading_date": str(next_window.get("trading_date") or ""),
            },
            "readiness_status": str(readiness.get("status") or ""),
            "catalog_price_feed_status": str(catalog.get("tiger_price_feed_status") or ""),
            "catalog_broker_order_status": str(catalog.get("tiger_broker_order_status") or ""),
            "operator_status": operator_state["status"],
            "operator_summary": operator_state["summary"],
            "next_command": operator_state["next_command"],
            "safety": {
                "read_only": safety.get("read_only") is True,
                "opens_quote_client": safety.get("opens_quote_client") is True,
                "opens_trade_client": safety.get("opens_trade_client") is True,
                "submits_orders": safety.get("submits_orders") is True,
                "writes_market_db": safety.get("writes_market_db") is True,
                "credential_values_exposed": safety.get("credential_values_exposed") is True,
            },
            "checked_at": str(payload.get("checked_at") or ""),
        }

    def _acceptance_operator_next_action(self, payload: dict[str, Any], next_window: dict[str, Any]) -> dict[str, str]:
        action = payload.get("operator_next_action", {}) if isinstance(payload.get("operator_next_action"), dict) else {}
        status = str(action.get("status") or "")
        if status:
            return {
                "status": status,
                "summary": str(action.get("summary") or ""),
                "next_command": str(action.get("next_command") or self._acceptance_next_command(payload)),
            }
        fallback = self._price_feed_acceptance_operator_state(payload, next_window)
        return {
            "status": fallback["status"],
            "summary": fallback["summary"],
            "next_command": self._acceptance_next_command(payload),
        }

    def _price_feed_acceptance_operator_state(self, payload: dict[str, Any], next_window: dict[str, Any]) -> dict[str, str]:
        status = str(payload.get("status") or "missing")
        if status == "accepted":
            return {"status": "accepted", "summary": "Tiger price feed accepted."}
        if status == "blocked":
            return {"status": "blocked", "summary": "Tiger price-feed acceptance is blocked; inspect blockers before retrying."}
        if status != "pending_market_open":
            return {"status": status, "summary": f"Tiger price-feed acceptance status is {status}."}

        now = (self.checked_at or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
        start = self._parse_time(str(next_window.get("start") or ""))
        end = self._parse_time(str(next_window.get("end") or ""))
        if start and now < start:
            return {"status": "waiting_market_open", "summary": f"Wait until {start.isoformat()} to rerun Tiger price-feed acceptance."}
        if start and (end is None or now <= end):
            return {"status": "rerun_acceptance_now", "summary": "COMEX window is open; rerun Tiger price-feed acceptance now."}
        if end and now > end:
            return {"status": "window_expired", "summary": "The recorded COMEX validation window has expired; rerun acceptance to compute the next window."}
        return {"status": "pending_market_open", "summary": "Tiger price-feed acceptance is waiting for a COMEX market-hours validation window."}

    def _acceptance_next_command(self, payload: dict[str, Any]) -> str:
        commands = [str(item) for item in (payload.get("next_commands") or []) if item]
        if commands:
            return commands[0]
        contract = str(payload.get("contract") or "MGCmain")
        run_date = str(payload.get("run_date") or "<date>")
        return f"python3 -m pipelines.tiger_price_feed_acceptance --date {run_date} --contract {contract} --poll-seconds 75 --json"

    def _data_source_preflight_summary(self, data_source_preflight: dict[str, Any]) -> dict[str, Any]:
        payload = data_source_preflight["payload"]
        gate = payload.get("execution_venue_readiness_gate", {}) if isinstance(payload.get("execution_venue_readiness_gate"), dict) else {}
        evidence = gate.get("evidence", {}) if isinstance(gate.get("evidence"), dict) else {}
        acceptance = evidence.get("acceptance", {}) if isinstance(evidence.get("acceptance"), dict) else {}
        next_window = acceptance.get("next_trading_window", {}) if isinstance(acceptance.get("next_trading_window"), dict) else {}
        return {
            "artifact": data_source_preflight["path"],
            "available": data_source_preflight["available"],
            "error": data_source_preflight["error"],
            "status": str(payload.get("status") or "missing"),
            "symbol": str(payload.get("symbol") or ""),
            "timeframe": str(payload.get("timeframe") or ""),
            "source_key": str(payload.get("source_key") or ""),
            "ready_for_paper": payload.get("ready_for_paper") is True,
            "ready_for_live": payload.get("ready_for_live") is True,
            "live_data_mode": str(payload.get("live_data_mode") or ""),
            "latest_provider": str(payload.get("latest_provider") or ""),
            "latest_timestamp": str(payload.get("latest_timestamp") or ""),
            "latest_price": payload.get("latest_price"),
            "latest_record_age_minutes": payload.get("latest_record_age_minutes"),
            "execution_venue_rows": payload.get("execution_venue_rows"),
            "message": str(payload.get("message") or ""),
            "gate": {
                "required": gate.get("required") is True,
                "status": str(gate.get("status") or "missing"),
                "allows_live": gate.get("allows_live") is True,
                "summary": str(gate.get("summary") or ""),
                "next_trading_window": {
                    "start": str(next_window.get("start") or ""),
                    "end": str(next_window.get("end") or ""),
                    "trading_date": str(next_window.get("trading_date") or ""),
                },
            },
            "can_enable_broker_orders_from_this_gate": False,
            "checked_at": str(payload.get("checked_at") or ""),
        }

    def _now(self) -> str:
        return (self.checked_at or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0).isoformat()

    def _parse_time(self, value: str | datetime | None) -> datetime | None:
        if value is None or value == "":
            return None
        if isinstance(value, datetime):
            parsed = value
        else:
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError:
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).replace(microsecond=0)
