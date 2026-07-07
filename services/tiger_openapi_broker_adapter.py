from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from schemas.market_data import PaperOrder
from services.broker_adapter import BrokerOrderRequest, LiveBrokerAdapter
from services.journal_store import write_json
from services.live_env import apply_live_env
from services.live_money_guardrails import LiveMoneyGuardrails
from services.order_lifecycle import OrderLifecycleStore
from services.tiger_contracts import TigerContractResolver
from services.tiger_openapi_account_sync import TigerOpenApiAccountSync


class TigerOpenApiPaperBrokerAdapter(LiveBrokerAdapter):
    """Tiger OpenAPI paper adapter, disabled unless explicitly armed.

    This adapter only supports Tiger paper/simulated trading. The repo defaults
    keep Tiger in dry-run mode; tests inject a fake TradeClient so no real
    account order is required to validate order construction and invariants.
    """

    name = "tiger_openapi_paper"

    def __init__(
        self,
        output_root: Path,
        broker_config: dict,
        *,
        trade_client: Any | None = None,
        sdk: Any | None = None,
    ) -> None:
        merged = {
            **broker_config,
            "provider": "tiger_openapi",
            "environment": str(broker_config.get("environment", "paper")),
            "request_dir": str(broker_config.get("request_dir", "tiger_order_requests")),
        }
        super().__init__(output_root, live_trading_enabled=True, broker_config=merged)
        self._trade_client = trade_client
        self._sdk = sdk

    def preflight(self) -> dict:
        readiness = self._tiger_preflight()
        network_mode = str(self.broker_config.get("network_order_submission", "not_implemented_fail_closed"))
        confirm_paper = bool(self.broker_config.get("confirm_tiger_paper_orders", False))
        paper_env = str(self.broker_config.get("environment", "paper")).lower() == "paper"
        if self.dry_run:
            return readiness

        ready = bool(
            readiness.get("live_trading_enabled")
            and readiness.get("props_path_exists")
            and readiness.get("props_path_owner_only")
            and paper_env
            and network_mode == "paper_tradeclient"
            and confirm_paper
        )
        block_reason = ""
        if not readiness.get("live_trading_enabled"):
            block_reason = "live_trading_enabled is false"
        elif not readiness.get("props_path_exists"):
            block_reason = f"missing Tiger OpenAPI config path env: {readiness.get('props_path_env')}"
        elif not readiness.get("props_path_owner_only"):
            block_reason = "Tiger OpenAPI config file must be owner-only (chmod 600)"
        elif not paper_env:
            block_reason = "Tiger network order submission is only implemented for paper environment"
        elif network_mode != "paper_tradeclient":
            block_reason = "Tiger OpenAPI network order submission is not enabled; fail-closed"
        elif not confirm_paper:
            block_reason = "confirm_tiger_paper_orders must be true before Tiger paper network submission"
        else:
            block_reason = "Tiger OpenAPI paper TradeClient ready"
        return {
            **readiness,
            "ready": ready,
            "block_reason": block_reason,
            "environment": str(self.broker_config.get("environment", "paper")),
            "network_order_submission": network_mode,
            "confirm_tiger_paper_orders": confirm_paper,
            "paper_tradeclient_ready": ready,
        }

    def submit_order(self, request: BrokerOrderRequest) -> PaperOrder:
        readiness = self.preflight()
        if self.dry_run:
            return self._submit_tiger_order(request, readiness)
        if not readiness.get("ready"):
            self._record_blocked_tiger_request(request, readiness, "blocked")
            raise RuntimeError(f"Tiger paper broker preflight failed: {readiness.get('block_reason')}")
        contract = TigerContractResolver(self.broker_config).resolve(str(request.ticket.get("asset") or ""), as_of=f"{request.run_date}T00:00:00+00:00")
        readiness = {**readiness, "tiger_contract": contract.to_dict()}
        if not contract.ready:
            self._record_blocked_tiger_request(request, readiness, "blocked")
            raise RuntimeError(f"Tiger contract resolution blocks new order: {contract.block_reason}")
        protection_check = self._protective_order_precheck(request.ticket)
        readiness = {**readiness, "protective_order_precheck": protection_check}
        if not protection_check.get("ready"):
            self._record_blocked_tiger_request(request, readiness, "blocked")
            raise RuntimeError(protection_check.get("block_reason") or "Tiger protective order precheck failed")
        client = self._client()
        if bool(self.broker_config.get("require_reconciliation_before_entry", True)):
            from services.tiger_openapi_reconciliation import TigerOpenApiPaperReconciliation

            reconciliation = TigerOpenApiPaperReconciliation(self.output_root, self.broker_config, trade_client=client).run(request.run_date)
            readiness = {**readiness, "tiger_reconciliation": self._reconciliation_summary(reconciliation), "tiger_reconciliation_raw": reconciliation}
            if reconciliation.get("can_open_new_orders") is not True:
                self._record_blocked_tiger_request(request, readiness, "blocked")
                raise RuntimeError(f"Tiger reconciliation blocks new order: {reconciliation.get('reason_code') or reconciliation.get('confirmation_status')}")
            if bool(self.broker_config.get("require_live_money_guardrails_before_entry", True)):
                account_sync = TigerOpenApiAccountSync(self.output_root, self.broker_config, trade_client=client)
                account_report = account_sync.run(request.run_date)
                merged_reconciliation = account_sync.merge_into_reconciliation(reconciliation, account_report)
                readiness = {
                    **readiness,
                    "tiger_account_sync": self._account_sync_summary(account_report),
                    "tiger_reconciliation_raw": merged_reconciliation,
                }
            account_check = self._account_precheck_from_reconciliation(reconciliation)
        else:
            account_check = self._account_precheck(client, request.ticket)
        readiness = {**readiness, "account_precheck": account_check}
        if not account_check.get("ready"):
            self._record_blocked_tiger_request(request, readiness, "blocked")
            raise RuntimeError(account_check.get("block_reason") or "Tiger account precheck failed")
        guardrails = self._live_money_guardrails_for_tiger_order(request, readiness, contract.execution_symbol)
        readiness = {**readiness, "live_money_guardrails": guardrails}
        if guardrails and guardrails.get("allows_new_order") is False:
            self._record_blocked_tiger_request(request, readiness, "blocked")
            blocker = guardrails.get("primary_blocker", {}) if isinstance(guardrails.get("primary_blocker"), dict) else {}
            raise RuntimeError(f"Tiger live money guardrails block new order: {blocker.get('message') or guardrails.get('status')}")
        return self._submit_tradeclient_order(request, readiness, client)

    def _submit_tradeclient_order(self, request: BrokerOrderRequest, readiness: dict, client: Any) -> PaperOrder:
        ticket = request.ticket
        contract = readiness.get("tiger_contract", {}) if isinstance(readiness.get("tiger_contract"), dict) else {}
        if contract.get("execution_symbol"):
            ticket = {**ticket, "asset": contract["execution_symbol"]}
        requested_price = float(request.latest_price or self._entry_midpoint(ticket["entry_zone"]))
        quantity = self._tiger_contract_quantity(float(request.actual_size or self._quantity(ticket, requested_price)))
        order_id = self._order_id(ticket["ticket_id"], request.run_date, requested_price)
        now = self._now()
        sdk_order, payload = self._build_tradeclient_order(ticket, order_id, requested_price, quantity, client)
        lifecycle_store = OrderLifecycleStore(self.output_root)
        lifecycle_store.write_intent(
            request.run_date,
            order_id=order_id,
            ticket_id=str(ticket.get("ticket_id", "")),
            idempotency_key=order_id[:36],
            requested_quantity=quantity,
            requested_price=requested_price,
            source="tiger_openapi:paper",
            metadata={"ticket": dict(ticket), "request": payload},
        )
        lifecycle_store.transition(request.run_date, order_id, "submitting", reason="tiger_paper_submit_started")

        responses: dict[str, Any] = {"preview": {}, "place_order": {}, "protective_status": payload["protective_status"]}
        try:
            if bool(self.broker_config.get("preview_before_place", True)):
                responses["preview"] = self._safe_sdk_response(client.preview_order(sdk_order))
            broker_order_id = client.place_order(sdk_order)
            responses["place_order"] = {
                "order_id": broker_order_id,
                "sdk_order_id": getattr(sdk_order, "id", None),
                "sub_ids": getattr(sdk_order, "sub_ids", None),
                "orders": self._safe_sdk_response(getattr(sdk_order, "orders", None)),
            }
        except Exception as exc:
            lifecycle_store.transition(
                request.run_date,
                order_id,
                "rejected",
                reason="tiger_paper_submit_error",
                metadata={"error_type": type(exc).__name__, "message": str(exc)},
            )
            receipt = PaperOrder(
                order_id=order_id,
                ticket_id=ticket["ticket_id"],
                status="rejected",
                requested_price=round(requested_price, 4),
                fill_price=None,
                quantity=quantity,
                filled_at="",
                rejection_reason=f"{type(exc).__name__}: {exc}",
            )
            self._record_live_request(request, order_id, now, ticket, payload, readiness, receipt, broker_response=responses)
            raise

        payload["network_order_created"] = True
        if contract:
            payload["requested_symbol"] = request.ticket.get("asset")
            payload["tiger_contract_resolution"] = contract
        lifecycle_store.transition(
            request.run_date,
            order_id,
            "accepted",
            reason="tiger_paper_order_accepted",
            metadata=responses["place_order"],
        )
        receipt = PaperOrder(
            order_id=order_id,
            ticket_id=ticket["ticket_id"],
            status="submitted_to_tiger_paper",
            requested_price=round(requested_price, 4),
            fill_price=None,
            quantity=quantity,
            filled_at="",
            rejection_reason=str(responses["place_order"].get("order_id") or "submitted to Tiger OpenAPI paper"),
        )
        self._record_live_request(request, order_id, now, ticket, payload, readiness, receipt, broker_response=responses)
        return receipt

    def _build_tradeclient_order(
        self,
        ticket: dict,
        order_id: str,
        requested_price: float,
        quantity: int,
        client: Any,
    ) -> tuple[Any, dict]:
        payload = self._tiger_order_payload(ticket, order_id, requested_price, quantity)
        symbol = payload["symbol"]
        contract_spec = self._contract_spec(symbol)
        if self._is_continuous_contract(symbol):
            raise RuntimeError("Tiger network orders require a dated tradable contract, not a continuous contract")
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
        entry_type = self._entry_order_type(ticket)
        side = payload["side"]
        time_in_force = payload["time_in_force"]
        legs = self._protective_legs(ticket, sdk, time_in_force)
        if legs and entry_type != "LIMIT":
            raise RuntimeError("Tiger attached protective orders require a LIMIT entry order")

        account = getattr(client, "_account", None) or str(self.broker_config.get("account", ""))
        if not account:
            raise RuntimeError("Tiger TradeClient account is missing")
        if entry_type == "MARKET":
            order = sdk.market_order(account, contract, side, quantity, time_in_force=time_in_force)
        elif entry_type == "STOP":
            trigger = float(ticket.get("aux_price") or ticket.get("stop_price") or requested_price)
            order = sdk.stop_order(account, contract, side, quantity, aux_price=trigger, time_in_force=time_in_force)
        elif entry_type == "STOP_LIMIT":
            trigger = float(ticket.get("aux_price") or ticket.get("stop_price") or requested_price)
            order = sdk.stop_limit_order(
                account,
                contract,
                side,
                quantity,
                limit_price=requested_price,
                aux_price=trigger,
                time_in_force=time_in_force,
            )
        elif legs:
            order = sdk.limit_order_with_legs(
                account,
                contract,
                side,
                quantity,
                requested_price,
                order_legs=legs,
                time_in_force=time_in_force,
            )
        else:
            order = sdk.limit_order(account, contract, side, quantity, requested_price, time_in_force=time_in_force)
        payload.update(
            {
                "network_order_created": False,
                "entry_order_type": entry_type,
                "contract_spec": contract_spec,
                "protective_status": "attached_in_parent_order" if legs else "not_requested",
                "protective_leg_count": len(legs),
                "preview_before_place": bool(self.broker_config.get("preview_before_place", True)),
            }
        )
        return order, payload

    def _protective_legs(self, ticket: dict, sdk: Any, time_in_force: str) -> list[Any]:
        legs = []
        leg_tif = str(self.broker_config.get("protective_time_in_force", time_in_force or "DAY")).upper()
        targets = list(ticket.get("targets") or [])
        if targets:
            legs.append(sdk.order_leg("PROFIT", price=float(targets[0]), time_in_force=leg_tif))
        if ticket.get("stop_loss") is not None:
            legs.append(sdk.order_leg("LOSS", price=float(ticket["stop_loss"]), time_in_force=leg_tif))
        if len(legs) > 2:
            raise RuntimeError("Tiger attached order supports at most two protective legs")
        return legs

    def _protective_order_precheck(self, ticket: dict) -> dict:
        entry_type = self._entry_order_type(ticket)
        targets = list(ticket.get("targets") or [])
        has_target = bool(targets and targets[0] is not None)
        has_stop = ticket.get("stop_loss") is not None
        required = bool(self.broker_config.get("require_attached_protection_before_entry", True))
        result = {
            "ready": True,
            "block_reason": "",
            "required": required,
            "same_path": "parent_order_attached_legs",
            "entry_order_type": entry_type,
            "has_stop_loss": has_stop,
            "has_take_profit": has_target,
            "expected_leg_count": int(has_target) + int(has_stop),
        }
        if not required:
            return result
        if entry_type != "LIMIT":
            return {
                **result,
                "ready": False,
                "block_reason": "Tiger network entries require LIMIT orders so TP/SL can be attached in the parent order",
            }
        if not has_stop or not has_target:
            missing = []
            if not has_stop:
                missing.append("stop_loss")
            if not has_target:
                missing.append("targets[0]")
            return {
                **result,
                "ready": False,
                "block_reason": f"Tiger network entries require attached stop_loss and take-profit before entry; missing {', '.join(missing)}",
            }
        return result

    def _account_precheck(self, client: Any, ticket: dict) -> dict:
        symbol = str(ticket.get("asset") or "")
        checks = {
            "position_check": "skipped",
            "open_order_check": "skipped",
            "positions": [],
            "open_orders": [],
        }
        if bool(self.broker_config.get("require_flat_before_entry", True)):
            positions = self._safe_list_call(client, "get_positions", sec_type="FUT", symbol=self._root_symbol(symbol))
            checks["position_check"] = "checked"
            checks["positions"] = [self._safe_sdk_response(item) for item in positions]
            nonflat = [item for item in positions if abs(self._position_quantity(item)) > 1e-12]
            if nonflat:
                return {**checks, "ready": False, "block_reason": "Tiger account is not flat for requested futures symbol"}
        if bool(self.broker_config.get("require_no_open_orders_before_entry", True)):
            open_orders = self._safe_list_call(client, "get_open_orders", sec_type="FUT", symbol=self._root_symbol(symbol))
            checks["open_order_check"] = "checked"
            checks["open_orders"] = [self._safe_sdk_response(item) for item in open_orders]
            if open_orders:
                return {**checks, "ready": False, "block_reason": "Tiger account has open orders for requested futures symbol"}
        return {**checks, "ready": True, "block_reason": ""}

    def _account_precheck_from_reconciliation(self, reconciliation: dict) -> dict:
        return {
            "ready": reconciliation.get("can_open_new_orders") is True,
            "block_reason": "" if reconciliation.get("can_open_new_orders") is True else str(reconciliation.get("reason_code") or reconciliation.get("confirmation_status") or "Tiger reconciliation blocks new orders"),
            "position_check": "reconciliation",
            "open_order_check": "reconciliation",
            "positions": reconciliation.get("exchange_positions", []),
            "open_orders": reconciliation.get("exchange_open_orders", []),
        }

    def _live_money_guardrails_for_tiger_order(self, request: BrokerOrderRequest, readiness: dict, execution_symbol: str) -> dict:
        if not bool(self.broker_config.get("require_live_money_guardrails_before_entry", True)):
            return {"status": "SKIPPED", "allows_new_order": True, "reason": "require_live_money_guardrails_before_entry=false"}
        ticket = request.ticket
        requested_price = float(request.latest_price or self._entry_midpoint(ticket["entry_zone"]))
        quantity = self._tiger_contract_quantity(float(request.actual_size or self._quantity(ticket, requested_price)))
        side = "BUY" if self._is_buy_action(str(ticket.get("action", ""))) else "SELL"
        return LiveMoneyGuardrails(self.output_root, broker_config=self.broker_config).evaluate_order(
            request.run_date,
            ticket={**ticket, "asset": execution_symbol},
            symbol=execution_symbol,
            side=side,
            requested_price=requested_price,
            quantity=quantity,
            source="tiger_openapi:paper",
            reconciliation=readiness.get("tiger_reconciliation_raw", {}) if isinstance(readiness.get("tiger_reconciliation_raw"), dict) else {},
        )

    def _reconciliation_summary(self, reconciliation: dict) -> dict:
        return {
            "confirmation_status": reconciliation.get("confirmation_status", ""),
            "system_state": reconciliation.get("system_state", ""),
            "reason_code": reconciliation.get("reason_code", ""),
            "blocks_new_orders": reconciliation.get("blocks_new_orders"),
            "can_open_new_orders": reconciliation.get("can_open_new_orders"),
            "suspected_naked_position": reconciliation.get("suspected_naked_position"),
            "drift_count": reconciliation.get("drift_count"),
            "checked_at": reconciliation.get("checked_at", ""),
            "artifact": str(self.output_root / "tiger_reconciliation" / "current.json"),
        }

    def _account_sync_summary(self, account_report: dict) -> dict:
        balance = account_report.get("exchange_balance", {}) if isinstance(account_report.get("exchange_balance"), dict) else {}
        accounting = account_report.get("exchange_accounting", {}) if isinstance(account_report.get("exchange_accounting"), dict) else {}
        observation = account_report.get("account_observation", {}) if isinstance(account_report.get("account_observation"), dict) else {}
        return {
            "sync_status": str(account_report.get("sync_status") or "missing"),
            "error": str(account_report.get("error") or ""),
            "account_observed": observation.get("account_observed") is True,
            "balance_present": balance.get("balance_present") is True,
            "accounting_observed": observation.get("accounting_observed") is True,
            "segment_key": str(observation.get("segment_key") or ""),
            "balance": balance.get("balance"),
            "available": balance.get("available"),
            "net_realized_pnl_estimate": accounting.get("net_realized_pnl_estimate"),
            "checked_at": str(account_report.get("checked_at") or ""),
            "artifact": str(self.output_root / "tiger_account_sync" / "current.json"),
        }

    def _safe_list_call(self, client: Any, method_name: str, **kwargs) -> list[Any]:
        method = getattr(client, method_name, None)
        if method is None:
            raise RuntimeError(f"Tiger TradeClient missing {method_name}")
        payload = method(**kwargs)
        if payload is None:
            return []
        if isinstance(payload, list):
            return payload
        if isinstance(payload, tuple):
            return list(payload)
        return [payload]

    def _position_quantity(self, item: Any) -> float:
        data = self._safe_sdk_response(item)
        for key in ["quantity", "position", "position_qty", "qty", "available_quantity"]:
            try:
                return float(data.get(key) or 0)
            except (TypeError, ValueError, AttributeError):
                continue
        return 0.0

    def _record_blocked_tiger_request(self, request: BrokerOrderRequest, readiness: dict, status: str) -> None:
        ticket = request.ticket
        requested_price = float(request.latest_price or self._entry_midpoint(ticket["entry_zone"]))
        try:
            quantity = self._tiger_contract_quantity(float(request.actual_size or self._quantity(ticket, requested_price)))
            payload = self._tiger_order_payload(ticket, self._order_id(ticket["ticket_id"], request.run_date, requested_price), requested_price, quantity)
        except Exception as exc:
            payload = {"network_order_created": False, "payload_error": f"{type(exc).__name__}: {exc}"}
            quantity = 0
        order_id = self._order_id(ticket["ticket_id"], request.run_date, requested_price)
        receipt = PaperOrder(
            order_id=order_id,
            ticket_id=ticket["ticket_id"],
            status=status,
            requested_price=round(requested_price, 4),
            fill_price=None,
            quantity=quantity,
            filled_at="",
            rejection_reason=str(
                (readiness.get("account_precheck") or {}).get("block_reason")
                or (readiness.get("live_money_guardrails") or {}).get("primary_blocker", {}).get("message")
                or (readiness.get("protective_order_precheck") or {}).get("block_reason")
                or (readiness.get("tiger_contract") or {}).get("block_reason")
                or (readiness.get("tiger_reconciliation") or {}).get("reason_code")
                or (readiness.get("tiger_reconciliation") or {}).get("confirmation_status")
                or readiness.get("block_reason")
                or "Tiger order blocked"
            ),
        )
        self._record_live_request(request, order_id, self._now(), ticket, payload, readiness, receipt, broker_response={})

    def _client(self) -> Any:
        if self._trade_client is not None:
            return self._trade_client
        try:
            from tigeropen.tiger_open_config import TigerOpenClientConfig
            from tigeropen.trade.trade_client import TradeClient
        except ImportError as exc:
            raise RuntimeError("tigeropen is not installed; install it locally to use Tiger OpenAPI trading") from exc
        props_path = self._props_path()
        if not props_path:
            raise RuntimeError("Tiger OpenAPI config path is required")
        return TradeClient(TigerOpenClientConfig(props_path=props_path))

    def _sdk_tools(self) -> Any:
        if self._sdk is not None:
            return self._sdk
        try:
            from tigeropen.common.util import contract_utils, order_utils
        except ImportError as exc:
            raise RuntimeError("tigeropen is not installed; cannot build Tiger order objects") from exc
        return _TigerOpenApiSdk(contract_utils, order_utils)

    def _props_path(self) -> str:
        if self.broker_config.get("props_path"):
            return str(self.broker_config["props_path"])
        apply_live_env()
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

    def _entry_order_type(self, ticket: dict) -> str:
        raw = str(ticket.get("order_type", "limit")).strip().lower().replace("-", "_")
        if raw in {"market", "mkt"}:
            return "MARKET"
        if raw in {"stop", "stp", "buy_stop", "sell_stop"}:
            return "STOP"
        if raw in {"stop_limit", "stp_lmt"}:
            return "STOP_LIMIT"
        return "LIMIT"

    def _root_symbol(self, symbol: str) -> str:
        configured = (self.broker_config.get("contract_specs", {}) or {}).get(symbol, {})
        if configured.get("symbol"):
            return str(configured["symbol"])
        match = re.match(r"([A-Za-z]+)", symbol)
        return match.group(1) if match else symbol

    def _contract_month(self, symbol: str) -> str:
        match = re.search(r"(\d{4})$", symbol)
        if not match:
            return ""
        yy = match.group(1)[:2]
        mm = match.group(1)[2:]
        return f"20{yy}{mm}"

    def _is_continuous_contract(self, symbol: str) -> bool:
        return symbol.lower().endswith("main")

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

    def _now(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class _TigerOpenApiSdk:
    def __init__(self, contract_utils: Any, order_utils: Any) -> None:
        self.contract_utils = contract_utils
        self.order_utils = order_utils

    def future_contract(self, *args, **kwargs):
        return self.contract_utils.future_contract(*args, **kwargs)

    def limit_order(self, *args, **kwargs):
        return self.order_utils.limit_order(*args, **kwargs)

    def market_order(self, *args, **kwargs):
        return self.order_utils.market_order(*args, **kwargs)

    def stop_order(self, *args, **kwargs):
        return self.order_utils.stop_order(*args, **kwargs)

    def stop_limit_order(self, *args, **kwargs):
        return self.order_utils.stop_limit_order(*args, **kwargs)

    def limit_order_with_legs(self, *args, **kwargs):
        return self.order_utils.limit_order_with_legs(*args, **kwargs)

    def order_leg(self, *args, **kwargs):
        return self.order_utils.order_leg(*args, **kwargs)
