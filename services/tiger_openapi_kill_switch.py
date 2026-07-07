from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.broker_adapter import resolve_broker_config
from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json
from services.live_money_guardrails import LiveHaltStore
from services.tiger_openapi_reconciliation import TigerOpenApiPaperReconciliation


class TigerOpenApiPaperKillSwitch:
    """Tiger paper kill-switch planning surface.

    The default checked-in path is dry-run/read-only. Network cancel/close is
    implemented for paper testing, but it only runs when both config and the
    runtime confirmation explicitly opt in.
    """

    def __init__(
        self,
        output_root: Path | None = None,
        broker_config: dict | None = None,
        trade_client: Any | None = None,
        sdk: Any | None = None,
    ) -> None:
        config = load_pipeline_config()
        self.output_root = output_root or ROOT / config.get("output_root", "outputs")
        self.broker_config = broker_config if broker_config is not None else self._default_tiger_broker_config(config)
        self.trade_client = trade_client
        self.sdk = sdk
        self.halt_store = LiveHaltStore(self.output_root)

    def run(self, run_date: str, *, confirm_tiger_kill: bool = False, reason: str = "operator_tiger_paper_kill_switch") -> dict:
        reconciliation = TigerOpenApiPaperReconciliation(self.output_root, self.broker_config, trade_client=self.trade_client).run(run_date)
        report = {
            "run_date": run_date,
            "checked_at": self._now(),
            "provider": "tiger_openapi",
            "mode": "paper",
            "confirm_tiger_kill": confirm_tiger_kill,
            "network_actions": self._network_actions_ready(),
            "halt": self.halt_store.current(),
            "network_order_created": False,
            "network_cancel_created": False,
            "status": "dry_run",
            "reconciliation_before": reconciliation,
            "intended_cancel_orders": self._intended_cancel_orders(reconciliation),
            "intended_close_positions": self._intended_close_positions(reconciliation),
            "confirmed_flat": reconciliation.get("confirmation_status") == "confirmed_flat",
            "block_reason": "",
        }
        if not confirm_tiger_kill:
            report["block_reason"] = "dry-run only; pass confirm_tiger_kill to activate HALT after manual Tiger UI/API review"
            self._write_report(run_date, report)
            return report

        report["halt"] = self.halt_store.activate(
            run_date,
            reason=reason,
            source="tiger_openapi_paper_kill_switch",
            details={
                "mode": "paper",
                "network_order_created": False,
                "network_cancel_created": False,
                "reconciliation_status": reconciliation.get("confirmation_status"),
            },
        )
        if not report["network_actions"].get("ready"):
            report["status"] = "halt_active_manual_action_required"
            report["block_reason"] = f"{report['network_actions'].get('block_reason')}; HALT is active and manual flatten/cancel is required"
            self._write_report(run_date, report)
            return report

        network_result = self._execute_network_actions(reconciliation)
        report.update(network_result)
        errors = [item for item in report.get("cancel_responses", []) + report.get("close_responses", []) if item.get("status") == "error"]
        report["status"] = "halt_active_network_actions_sent" if not errors else "halt_active_network_action_error"
        report["block_reason"] = "" if not errors else "One or more Tiger cancel/close actions failed; verify account state in Tiger before clearing HALT"
        self._write_report(run_date, report)
        return report

    def clear_halt(self, run_date: str, *, confirm_clear: bool = False, reason: str = "operator_cleared_tiger_paper_halt") -> dict:
        if not confirm_clear:
            result = {
                "run_date": run_date,
                "checked_at": self._now(),
                "provider": "tiger_openapi",
                "mode": "paper",
                "status": "dry_run",
                "halt": self.halt_store.current(),
                "block_reason": "dry-run only; pass confirm_clear to clear persistent HALT",
            }
            self._write_report(run_date, result)
            return result
        record = self.halt_store.clear(run_date, reason=reason, source="tiger_openapi_paper_kill_switch", operator="manual")
        result = {"run_date": run_date, "checked_at": self._now(), "provider": "tiger_openapi", "mode": "paper", "status": "halt_cleared", "halt": record}
        self._write_report(run_date, result)
        return result

    def _intended_cancel_orders(self, reconciliation: dict) -> list[dict]:
        return [
            {
                "symbol": order.get("symbol", ""),
                "order_id": order.get("order_id", ""),
                "type": order.get("type", ""),
                "side": order.get("side", ""),
                "status": order.get("status", ""),
                "network_cancel_created": False,
            }
            for order in reconciliation.get("exchange_open_orders", [])
        ]

    def _intended_close_positions(self, reconciliation: dict) -> list[dict]:
        positions = []
        for position in reconciliation.get("exchange_positions", []):
            amount = float(position.get("position_amt") or 0.0)
            if not amount:
                continue
            positions.append(
                {
                    "symbol": position.get("symbol", ""),
                    "position_amt": amount,
                    "side": "SELL" if amount > 0 else "BUY",
                    "quantity": abs(amount),
                    "order_type": "MARKET",
                    "network_order_created": False,
                }
            )
        return positions

    def _network_actions_ready(self) -> dict:
        enabled = bool(self.broker_config.get("enable_tiger_kill_switch_network_actions", False))
        dry_run = bool(self.broker_config.get("dry_run", True))
        paper_env = str(self.broker_config.get("environment", "paper")).lower() == "paper"
        network_mode = str(self.broker_config.get("network_order_submission", "not_implemented_fail_closed"))
        confirm_paper = bool(self.broker_config.get("confirm_tiger_paper_orders", False))
        ready = bool(enabled and not dry_run and paper_env and network_mode == "paper_tradeclient" and confirm_paper)
        if not enabled:
            block_reason = "Tiger kill-switch network actions are disabled by config"
        elif dry_run:
            block_reason = "Tiger broker profile is dry_run=true"
        elif not paper_env:
            block_reason = "Tiger kill-switch network actions are only implemented for paper environment"
        elif network_mode != "paper_tradeclient":
            block_reason = "Tiger paper TradeClient network mode is not enabled"
        elif not confirm_paper:
            block_reason = "confirm_tiger_paper_orders must be true before Tiger kill-switch network actions"
        else:
            block_reason = "Tiger paper kill-switch network actions ready"
        return {
            "enabled": enabled,
            "ready": ready,
            "block_reason": block_reason,
            "dry_run": dry_run,
            "environment": str(self.broker_config.get("environment", "paper")),
            "network_order_submission": network_mode,
            "confirm_tiger_paper_orders": confirm_paper,
        }

    def _execute_network_actions(self, reconciliation: dict) -> dict:
        client = self._client()
        cancel_responses: list[dict] = []
        close_responses: list[dict] = []
        intended_cancel_orders = self._intended_cancel_orders(reconciliation)
        intended_close_positions = self._intended_close_positions(reconciliation)

        for action in intended_cancel_orders:
            response = self._cancel_order(client, action)
            action.update({"network_cancel_created": response.get("status") == "sent", "network_response": response})
            cancel_responses.append(response)

        for action in intended_close_positions:
            response = self._close_position(client, action)
            action.update({"network_order_created": response.get("status") == "sent", "network_response": response})
            close_responses.append(response)

        return {
            "network_cancel_created": any(item.get("network_cancel_created") for item in intended_cancel_orders),
            "network_order_created": any(item.get("network_order_created") for item in intended_close_positions),
            "intended_cancel_orders": intended_cancel_orders,
            "intended_close_positions": intended_close_positions,
            "cancel_responses": cancel_responses,
            "close_responses": close_responses,
        }

    def _cancel_order(self, client: Any, action: dict) -> dict:
        order_id = str(action.get("order_id") or "")
        if not order_id:
            return {"status": "error", "order_id": "", "error": "missing Tiger order_id"}
        try:
            response = client.cancel_order(id=self._order_id_value(order_id))
            return {"status": "sent", "order_id": order_id, "response": self._safe_sdk_response(response)}
        except Exception as exc:
            return {"status": "error", "order_id": order_id, "error": f"{type(exc).__name__}: {exc}"}

    def _close_position(self, client: Any, action: dict) -> dict:
        try:
            order = self._build_market_close_order(client, action)
            response = client.place_order(order)
            return {
                "status": "sent",
                "symbol": action.get("symbol", ""),
                "side": action.get("side", ""),
                "quantity": action.get("quantity"),
                "response": self._safe_sdk_response(response),
                "order": self._safe_sdk_response(order),
            }
        except Exception as exc:
            return {"status": "error", "symbol": action.get("symbol", ""), "error": f"{type(exc).__name__}: {exc}"}

    def _build_market_close_order(self, client: Any, action: dict) -> Any:
        account = getattr(client, "_account", None) or str(self.broker_config.get("account", ""))
        if not account:
            raise RuntimeError("Tiger TradeClient account is missing")
        symbol = str(action.get("symbol") or "")
        quantity = self._whole_quantity(float(action.get("quantity") or 0.0))
        side = str(action.get("side") or "").upper()
        if side not in {"BUY", "SELL"}:
            raise RuntimeError(f"Unsupported Tiger close side: {side}")
        contract_spec = self._contract_spec(symbol)
        sdk = self._sdk_tools()
        contract = sdk.future_contract(
            contract_spec["symbol"],
            contract_spec.get("currency", "USD"),
            expiry=contract_spec.get("expiry"),
            exchange=contract_spec.get("exchange", self.broker_config.get("exchange", "COMEX")),
            contract_month=contract_spec.get("contract_month"),
            multiplier=contract_spec.get("multiplier"),
            local_symbol=contract_spec.get("local_symbol", symbol),
        )
        time_in_force = str(self.broker_config.get("kill_switch_time_in_force", "DAY")).upper()
        return sdk.market_order(account, contract, side, quantity, time_in_force=time_in_force)

    def _client(self) -> Any:
        if self.trade_client is not None:
            return self.trade_client
        try:
            from tigeropen.tiger_open_config import TigerOpenClientConfig
            from tigeropen.trade.trade_client import TradeClient
        except ImportError as exc:
            raise RuntimeError("tigeropen is not installed; cannot execute Tiger kill-switch actions") from exc
        props_path = self._props_path()
        if not props_path:
            raise RuntimeError("Tiger OpenAPI config path is required")
        return TradeClient(TigerOpenClientConfig(props_path=props_path))

    def _sdk_tools(self) -> Any:
        if self.sdk is not None:
            return self.sdk
        try:
            from tigeropen.common.util import contract_utils, order_utils
        except ImportError as exc:
            raise RuntimeError("tigeropen is not installed; cannot build Tiger kill-switch close orders") from exc
        return _TigerOpenApiKillSwitchSdk(contract_utils, order_utils)

    def _props_path(self) -> str:
        if self.broker_config.get("props_path"):
            return str(self.broker_config["props_path"])
        from services.live_env import apply_live_env

        apply_live_env()
        import os

        return os.getenv(str(self.broker_config.get("props_path_env", "TIGER_OPENAPI_CONFIG_PATH")), "")

    def _contract_spec(self, symbol: str) -> dict:
        configured = (self.broker_config.get("contract_specs", {}) or {}).get(symbol, {})
        if configured:
            return {**configured, "local_symbol": configured.get("local_symbol", symbol)}
        root = self._root_symbol(symbol)
        month = self._contract_month(symbol)
        if not month:
            raise RuntimeError(f"Tiger contract spec is missing for {symbol}")
        return {
            "symbol": root,
            "currency": "USD",
            "exchange": str(self.broker_config.get("exchange", "COMEX")),
            "contract_month": month,
            "multiplier": self.broker_config.get("contract_multiplier"),
            "local_symbol": symbol,
        }

    def _root_symbol(self, symbol: str) -> str:
        prefix = ""
        for char in str(symbol):
            if char.isalpha() or (char.isdigit() and not prefix):
                prefix += char
            else:
                break
        return prefix or str(symbol)

    def _contract_month(self, symbol: str) -> str:
        digits = ""
        for char in reversed(str(symbol)):
            if char.isdigit():
                digits = char + digits
            elif digits:
                break
        if len(digits) < 4:
            return ""
        yy = digits[-4:-2]
        mm = digits[-2:]
        return f"20{yy}{mm}"

    def _whole_quantity(self, quantity: float) -> int:
        rounded = round(quantity)
        if rounded <= 0 or abs(quantity - rounded) > 1e-9:
            raise RuntimeError(f"Tiger futures kill-switch close quantity must be a positive whole contract: {quantity}")
        return int(rounded)

    def _order_id_value(self, order_id: str) -> int | str:
        return int(order_id) if order_id.isdigit() else order_id

    def _safe_sdk_response(self, item: Any) -> Any:
        if item is None:
            return {}
        if isinstance(item, (str, int, float, bool)):
            return item
        if isinstance(item, dict):
            return item
        if isinstance(item, list):
            return [self._safe_sdk_response(value) for value in item]
        if hasattr(item, "_asdict"):
            return dict(item._asdict())
        if hasattr(item, "__dict__"):
            return {key: value for key, value in vars(item).items() if not key.startswith("_")}
        return str(item)

    def _default_tiger_broker_config(self, config: dict) -> dict:
        resolved = resolve_broker_config(config)
        if str(resolved.get("provider") or "") == "tiger_openapi":
            return resolved
        profile = (config.get("broker_profiles", {}) or {}).get("tiger_openapi_paper")
        if isinstance(profile, dict) and profile:
            return dict(profile)
        return resolved

    def _write_report(self, run_date: str, report: dict) -> None:
        write_json(self.output_root / "tiger_kill_switch" / "current.json", [report])
        path = self.output_root / "tiger_kill_switch" / f"{run_date}.json"
        rows = load_json(path)
        rows.append(report)
        write_json(path, rows)

    def _now(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def run_tiger_openapi_paper_kill_switch(
    run_date: str,
    *,
    confirm_tiger_kill: bool = False,
    clear_halt: bool = False,
    confirm_clear: bool = False,
    output_root: Path | None = None,
) -> dict:
    service = TigerOpenApiPaperKillSwitch(output_root)
    if clear_halt:
        return service.clear_halt(run_date, confirm_clear=confirm_clear)
    return service.run(run_date, confirm_tiger_kill=confirm_tiger_kill)


class _TigerOpenApiKillSwitchSdk:
    def __init__(self, contract_utils: Any, order_utils: Any) -> None:
        self.contract_utils = contract_utils
        self.order_utils = order_utils

    def future_contract(self, *args, **kwargs):
        return self.contract_utils.future_contract(*args, **kwargs)

    def market_order(self, *args, **kwargs):
        return self.order_utils.market_order(*args, **kwargs)
