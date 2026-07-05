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

    def __init__(self, output_root: Path | None = None) -> None:
        config = load_pipeline_config()
        self.output_root = Path(output_root) if output_root else ROOT / config.get("output_root", "outputs")

    def snapshot(self) -> dict[str, Any]:
        reconciliation = self._artifact("tiger_reconciliation/current.json")
        order_sync = self._artifact("tiger_order_sync/current.json")
        kill_switch = self._artifact("tiger_kill_switch/current.json")
        contract_status = self._artifact("tiger_contracts/current.json")
        account_sync = self._artifact("tiger_account_sync/current.json")
        paper_order_drill = self._artifact("tiger_paper_order_drill/current.json")
        paper_order_readiness = self._artifact("tiger_paper_order_readiness/current.json")
        status = self._overall_status(
            reconciliation["payload"],
            order_sync["payload"],
            kill_switch["payload"],
            contract_status["payload"],
            account_sync["payload"],
            paper_order_drill["payload"],
            paper_order_readiness["payload"],
        )
        recent_fills = self._recent_fills(order_sync["payload"])
        open_orders = self._open_orders(order_sync["payload"])
        network_modification_observed = bool(
            kill_switch["payload"].get("network_order_created")
            or kill_switch["payload"].get("network_cancel_created")
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
            ),
            "can_open_new_orders": reconciliation["payload"].get("can_open_new_orders") is True,
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
            "paper_order_readiness": self._paper_order_readiness_summary(paper_order_readiness),
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

    def _overall_status(self, reconciliation: dict, order_sync: dict, kill_switch: dict, contract_status: dict, account_sync: dict, paper_order_drill: dict, paper_order_readiness: dict) -> str:
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
        return "ready"

    def _headline(self, status: str, reconciliation: dict, order_sync: dict, kill_switch: dict, contract_status: dict, account_sync: dict, paper_order_drill: dict, paper_order_readiness: dict) -> str:
        if status == "ready":
            return "Tiger paper venue is flat and order/fill sync is current."
        if status == "blocked":
            resolution = contract_status.get("resolution", {}) if isinstance(contract_status.get("resolution"), dict) else {}
            if resolution and resolution.get("ready") is not True:
                return str(resolution.get("block_reason") or resolution.get("status") or "Tiger contract resolution blocks new orders")
            return str(reconciliation.get("reason_code") or reconciliation.get("confirmation_status") or "Tiger reconciliation blocks new orders")
        if status == "manual_review":
            return "Tiger kill-switch network action was observed; verify account state before clearing HALT."
        if status == "degraded":
            if account_sync and not self._account_sync_ready(account_sync):
                return str(account_sync.get("error") or "Tiger account/balance sync is not current")
            if paper_order_drill and str(paper_order_drill.get("status") or "") != "pass":
                return "Tiger paper order drill is not passing."
            if paper_order_readiness and str(paper_order_readiness.get("status") or "") != "ready_for_attended_paper_order":
                return "Tiger attended paper order readiness is blocked."
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
            "checked_at": str(payload.get("checked_at") or ""),
        }

    def _now(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
