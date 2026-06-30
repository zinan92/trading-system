from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Protocol

from schemas.market_data import PaperOrder
from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json
from services.live_env import apply_live_env, live_env_value_present
from services.order_lifecycle import IllegalOrderTransition, OrderLifecycleStore
from services.paper_executor import PaperExecutor


@dataclass(frozen=True)
class BrokerOrderRequest:
    run_date: str
    ticket: dict
    latest_price: float | None = None
    actual_size: float | None = None


class BrokerAdapter(Protocol):
    name: str

    def submit_order(self, request: BrokerOrderRequest) -> PaperOrder:
        ...


class PaperBrokerAdapter:
    name = "paper"

    def __init__(self, output_root: Path) -> None:
        self.executor = PaperExecutor(output_root)

    def submit_order(self, request: BrokerOrderRequest) -> PaperOrder:
        return self.executor.execute_ticket(
            run_date=request.run_date,
            ticket=request.ticket,
            latest_price=request.latest_price,
            actual_size=request.actual_size,
        )


class LiveBrokerAdapter:
    name = "live"

    def __init__(self, output_root: Path, live_trading_enabled: bool, broker_config: dict | None = None, opener=None) -> None:
        self.output_root = output_root
        self.live_trading_enabled = live_trading_enabled
        self.broker_config = broker_config or {}
        self.provider = str(self.broker_config.get("provider", "manual_gateway"))
        self.dry_run = bool(self.broker_config.get("dry_run", True))
        self.opener = opener or urllib.request.urlopen

    def submit_order(self, request: BrokerOrderRequest) -> PaperOrder:
        if not self.live_trading_enabled:
            raise RuntimeError("live trading is disabled; set live_trading_enabled only after wiring a real broker adapter")
        readiness = self.preflight()
        if not readiness["ready"] and not self.dry_run:
            raise RuntimeError(f"live broker preflight failed: {readiness['block_reason']}")
        if not self.dry_run:
            activation = self._live_activation(request.run_date)
            if activation.get("real_money_ready") is not True:
                raise RuntimeError("live activation gate is not real_money_ready; real broker submission is blocked")
        if self.provider == "mt5_file_bridge":
            return self._record_mt5_file_bridge_request(request, readiness)
        if self.provider == "oanda_rest":
            return self._submit_oanda_order(request, readiness)
        if self.provider == "binance_usdm":
            return self._submit_binance_order(request, readiness)
        if not self.dry_run:
            raise NotImplementedError(f"live broker provider {self.provider} is not wired for real order submission yet")
        return self._record_dry_run_request(request, readiness)

    def preflight(self) -> dict:
        env = apply_live_env()
        if self.provider == "mt5_file_bridge":
            return self._mt5_file_bridge_preflight()
        if self.provider == "oanda_rest":
            return self._oanda_preflight()
        if self.provider == "binance_usdm":
            return self._binance_preflight()
        api_key_env = str(self.broker_config.get("api_key_env", "BROKER_API_KEY"))
        account_id_env = str(self.broker_config.get("account_id_env", "BROKER_ACCOUNT_ID"))
        missing = [name for name in [api_key_env, account_id_env] if not live_env_value_present(name)]
        allowed_symbols = list(self.broker_config.get("allowed_symbols", []))
        ready = bool(self.live_trading_enabled and (self.dry_run or not missing))
        block_reason = ""
        if not self.live_trading_enabled:
            block_reason = "live_trading_enabled is false"
        elif missing and not self.dry_run:
            block_reason = f"missing broker environment variables: {', '.join(missing)}"
        elif self.dry_run:
            block_reason = "dry_run enabled; no real broker order will be sent"
        return {
            "provider": self.provider,
            "mode": "live",
            "dry_run": self.dry_run,
            "live_trading_enabled": self.live_trading_enabled,
            "ready": ready,
            "block_reason": block_reason,
            "api_key_env": api_key_env,
            "account_id_env": account_id_env,
            "missing_env": missing,
            "env_file": env["path"],
            "env_file_exists": env["exists"],
            "allowed_symbols": allowed_symbols,
            "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        }

    def _oanda_preflight(self) -> dict:
        env = apply_live_env()
        token_env = str(self.broker_config.get("api_key_env") or self.broker_config.get("token_env") or "OANDA_API_TOKEN")
        account_id_env = str(self.broker_config.get("account_id_env", "OANDA_ACCOUNT_ID"))
        missing = [name for name in [token_env, account_id_env] if not live_env_value_present(name)]
        ready = bool(self.live_trading_enabled and (self.dry_run or not missing))
        block_reason = ""
        if not self.live_trading_enabled:
            block_reason = "live_trading_enabled is false"
        elif missing and not self.dry_run:
            block_reason = f"missing OANDA environment variables: {', '.join(missing)}"
        elif self.dry_run:
            block_reason = "OANDA dry_run enabled; request artifact only"
        else:
            block_reason = "OANDA REST broker ready; real orders can be submitted"
        return {
            "provider": self.provider,
            "mode": "live",
            "dry_run": self.dry_run,
            "live_trading_enabled": self.live_trading_enabled,
            "ready": ready,
            "block_reason": block_reason,
            "api_key_env": token_env,
            "account_id_env": account_id_env,
            "missing_env": missing,
            "env_file": env["path"],
            "env_file_exists": env["exists"],
            "allowed_symbols": list(self.broker_config.get("allowed_symbols", [])),
            "base_url": self._oanda_base_url(),
            "account_id_present": live_env_value_present(account_id_env),
            "instrument": self._oanda_instrument("GOLD"),
            "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        }

    def _mt5_file_bridge_preflight(self) -> dict:
        outbox_dir = self._mt5_outbox_dir()
        inbox_dir = self._mt5_inbox_dir()
        docs = {}
        missing = []
        writable = False
        inbox_writable = False
        try:
            outbox_dir.mkdir(parents=True, exist_ok=True)
            probe = outbox_dir / ".write_test"
            probe.write_text("ok\n", encoding="utf-8")
            probe.unlink(missing_ok=True)
            writable = True
        except OSError as exc:
            missing.append(f"outbox_not_writable:{exc}")
        try:
            inbox_dir.mkdir(parents=True, exist_ok=True)
            probe = inbox_dir / ".write_test"
            probe.write_text("ok\n", encoding="utf-8")
            probe.unlink(missing_ok=True)
            inbox_writable = True
        except OSError as exc:
            missing.append(f"inbox_not_writable:{exc}")
        if writable or inbox_writable:
            docs = self._ensure_mt5_bridge_docs(outbox_dir, inbox_dir)
        ready = bool(self.live_trading_enabled and writable and inbox_writable)
        block_reason = ""
        if not self.live_trading_enabled:
            block_reason = "live_trading_enabled is false"
        elif not writable:
            block_reason = "MT5 outbox directory is not writable"
        elif not inbox_writable:
            block_reason = "MT5 inbox directory is not writable"
        elif self.dry_run:
            block_reason = "MT5 file bridge dry_run enabled; request artifact only"
        else:
            block_reason = "MT5 file bridge outbox ready; external MT5 EA must consume files"
        return {
            "provider": self.provider,
            "mode": "live",
            "dry_run": self.dry_run,
            "live_trading_enabled": self.live_trading_enabled,
            "ready": ready,
            "block_reason": block_reason,
            "missing_env": [],
            "allowed_symbols": list(self.broker_config.get("allowed_symbols", [])),
            "outbox_dir": str(outbox_dir),
            "inbox_dir": str(inbox_dir),
            "outbox_writable": writable,
            "inbox_writable": inbox_writable,
            **docs,
            "errors": missing,
            "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        }

    def _record_dry_run_request(self, request: BrokerOrderRequest, readiness: dict) -> PaperOrder:
        ticket = request.ticket
        requested_price = float(request.latest_price or self._entry_midpoint(ticket["entry_zone"]))
        quantity = float(request.actual_size or self._quantity(ticket, requested_price))
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        order_id = self._order_id(ticket["ticket_id"], request.run_date, requested_price)
        receipt = PaperOrder(
            order_id=order_id,
            ticket_id=ticket["ticket_id"],
            status="dry_run",
            requested_price=round(requested_price, 4),
            fill_price=None,
            quantity=round(quantity, 6),
            filled_at="",
            rejection_reason="live dry-run request recorded locally; no broker order sent",
        )
        payload = {
            "order_id": order_id,
            "requested_at": now,
            "provider": self.provider,
            "run_date": request.run_date,
            "ticket": ticket,
            "request": {
                "symbol": ticket.get("asset"),
                "action": ticket.get("action"),
                "order_type": ticket.get("order_type", "limit"),
                "time_in_force": ticket.get("time_in_force", "day"),
                "requested_price": receipt.requested_price,
                "quantity": receipt.quantity,
                "stop_loss": ticket.get("stop_loss"),
                "targets": ticket.get("targets", []),
            },
            "readiness": readiness,
            "receipt": receipt.to_dict(),
        }
        request_dir = str(self.broker_config.get("request_dir", "live_order_requests"))
        path = self.output_root / request_dir / f"{request.run_date}.json"
        rows = [item for item in load_json(path) if item.get("order_id") != order_id]
        rows.append(payload)
        write_json(path, rows)
        return receipt

    def _record_mt5_file_bridge_request(self, request: BrokerOrderRequest, readiness: dict) -> PaperOrder:
        ticket = request.ticket
        requested_price = float(request.latest_price or self._entry_midpoint(ticket["entry_zone"]))
        quantity = float(request.actual_size or self._quantity(ticket, requested_price))
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        order_id = self._order_id(ticket["ticket_id"], request.run_date, requested_price)
        status = "bridge_dry_run" if self.dry_run else "submitted_to_bridge"
        rejection = "MT5 bridge dry-run request recorded locally; no EA should execute it" if self.dry_run else "submitted to MT5 file bridge outbox; execution depends on external EA"
        receipt = PaperOrder(
            order_id=order_id,
            ticket_id=ticket["ticket_id"],
            status=status,
            requested_price=round(requested_price, 4),
            fill_price=None,
            quantity=round(quantity, 6),
            filled_at="",
            rejection_reason=rejection,
        )
        bridge_payload = {
            "order_id": order_id,
            "created_at": now,
            "dry_run": self.dry_run,
            "symbol": ticket.get("asset"),
            "action": ticket.get("action"),
            "order_type": ticket.get("order_type", "limit"),
            "time_in_force": ticket.get("time_in_force", "day"),
            "requested_price": receipt.requested_price,
            "quantity": receipt.quantity,
            "stop_loss": ticket.get("stop_loss"),
            "targets": ticket.get("targets", []),
            "source_ticket_id": ticket.get("ticket_id"),
            "manual_execution_required": ticket.get("manual_execution_required", True),
        }
        outbox_file = self._mt5_outbox_dir() / f"{order_id}.json"
        outbox_file.write_text(json.dumps(bridge_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        payload = {
            "order_id": order_id,
            "requested_at": now,
            "provider": self.provider,
            "run_date": request.run_date,
            "ticket": ticket,
            "request": bridge_payload,
            "readiness": readiness,
            "outbox_file": str(outbox_file),
            "receipt": receipt.to_dict(),
        }
        request_dir = str(self.broker_config.get("request_dir", "live_order_requests"))
        path = self.output_root / request_dir / f"{request.run_date}.json"
        rows = [item for item in load_json(path) if item.get("order_id") != order_id]
        rows.append(payload)
        write_json(path, rows)
        return receipt

    def _submit_oanda_order(self, request: BrokerOrderRequest, readiness: dict) -> PaperOrder:
        ticket = request.ticket
        requested_price = float(request.latest_price or self._entry_midpoint(ticket["entry_zone"]))
        quantity = float(request.actual_size or self._quantity(ticket, requested_price))
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        order_id = self._order_id(ticket["ticket_id"], request.run_date, requested_price)
        oanda_payload = self._oanda_order_payload(ticket, order_id, requested_price, quantity)
        if self.dry_run:
            receipt = PaperOrder(
                order_id=order_id,
                ticket_id=ticket["ticket_id"],
                status="oanda_dry_run",
                requested_price=round(requested_price, 4),
                fill_price=None,
                quantity=round(quantity, 6),
                filled_at="",
                rejection_reason="OANDA dry-run request recorded locally; no broker order sent",
            )
            self._record_live_request(request, order_id, now, ticket, oanda_payload, readiness, receipt, broker_response={})
            return receipt
        broker_response = self._post_oanda_order(oanda_payload)
        fill = broker_response.get("orderFillTransaction") or {}
        create = broker_response.get("orderCreateTransaction") or {}
        fill_price = float(fill["price"]) if fill.get("price") else None
        status = "filled" if fill_price is not None else "submitted_to_oanda"
        receipt = PaperOrder(
            order_id=order_id,
            ticket_id=ticket["ticket_id"],
            status=status,
            requested_price=round(requested_price, 4),
            fill_price=fill_price,
            quantity=round(quantity, 6),
            filled_at=fill.get("time", ""),
            rejection_reason=str(create.get("id") or broker_response.get("lastTransactionID") or "submitted to OANDA REST"),
        )
        self._record_live_request(request, order_id, now, ticket, oanda_payload, readiness, receipt, broker_response=broker_response)
        return receipt

    def _binance_preflight(self) -> dict:
        env = apply_live_env()
        api_key_env = str(self.broker_config.get("api_key_env", "BINANCE_API_KEY"))
        secret_env = str(self.broker_config.get("api_secret_env", "BINANCE_API_SECRET"))
        missing = [name for name in [api_key_env, secret_env] if not live_env_value_present(name)]
        symbol = self._binance_symbol("GOLD")
        exchange_status = self._binance_symbol_status(symbol)
        ready = bool(self.live_trading_enabled and exchange_status.get("status") == "TRADING" and (self.dry_run or not missing))
        if not self.live_trading_enabled:
            block_reason = "live_trading_enabled is false"
        elif exchange_status.get("status") != "TRADING":
            block_reason = f"Binance symbol {symbol} is not trading"
        elif missing and not self.dry_run:
            block_reason = f"missing Binance environment variables: {', '.join(missing)}"
        elif self.dry_run:
            block_reason = "Binance USDM dry_run enabled; request artifact only"
        else:
            block_reason = "Binance USDM broker ready; real futures orders can be submitted"
        return {
            "provider": self.provider,
            "mode": "live",
            "dry_run": self.dry_run,
            "live_trading_enabled": self.live_trading_enabled,
            "ready": ready,
            "block_reason": block_reason,
            "api_key_env": api_key_env,
            "api_secret_env": secret_env,
            "missing_env": missing,
            "env_file": env["path"],
            "env_file_exists": env["exists"],
            "base_url": self._binance_base_url(),
            "symbol": symbol,
            "instrument": symbol,
            "allowed_symbols": list(self.broker_config.get("allowed_symbols", [])),
            "exchange_status": exchange_status,
            "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        }

    def _submit_binance_order(self, request: BrokerOrderRequest, readiness: dict) -> PaperOrder:
        ticket = request.ticket
        requested_price = float(request.latest_price or self._entry_midpoint(ticket["entry_zone"]))
        raw_quantity = float(request.actual_size or self._quantity(ticket, requested_price))
        symbol = self._binance_symbol(str(ticket.get("asset", "GOLD")))
        filters = readiness.get("exchange_status", {}).get("filters", {})
        quantity = self._round_step(raw_quantity, str(filters.get("step_size", "0.001")))
        if quantity <= 0:
            raise RuntimeError("Binance order quantity is zero after applying exchange step size")
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        order_id = self._order_id(ticket["ticket_id"], request.run_date, requested_price)
        payloads = self._binance_order_payloads(ticket, order_id, symbol, requested_price, quantity, filters)
        if self.dry_run:
            receipt = PaperOrder(
                order_id=order_id,
                ticket_id=ticket["ticket_id"],
                status="binance_dry_run",
                requested_price=round(requested_price, 4),
                fill_price=None,
                quantity=quantity,
                filled_at="",
                rejection_reason="Binance USDM dry-run request recorded locally; no broker order sent",
            )
            self._record_live_request(request, order_id, now, ticket, payloads, readiness, receipt, broker_response={})
            return receipt
        lifecycle_store = OrderLifecycleStore(self.output_root)
        lifecycle, intent_created = lifecycle_store.write_intent(
            request.run_date,
            order_id=order_id,
            ticket_id=str(ticket.get("ticket_id", "")),
            idempotency_key=str(payloads["entry"].get("newClientOrderId", order_id[:36])),
            requested_quantity=quantity,
            requested_price=requested_price,
            source=f"{self.provider}:{readiness.get('mode', 'live')}",
            metadata={
                "symbol": symbol,
                "provider": self.provider,
                "mode": readiness.get("mode", "live"),
                "ticket": dict(ticket),
            },
        )
        if lifecycle.get("state") == "entry":
            lifecycle = self._transition_lifecycle(lifecycle_store, request.run_date, order_id, "submitting", reason="binance_submit_started")

        responses = {"entry": {}, "protective_orders": [], "protective_errors": []}
        recovered_entry = {}
        if not intent_created and lifecycle.get("state") == "submitting":
            recovery = self._recover_binance_entry(symbol, str(payloads["entry"].get("newClientOrderId", order_id[:36])))
            responses["entry_recovery"] = {key: value for key, value in recovery.items() if key != "entry"}
            if recovery.get("status") == "found":
                recovered_entry = recovery.get("entry", {}) if isinstance(recovery.get("entry"), dict) else {}
                responses["entry_recovered_from_idempotency_key"] = True
            elif recovery.get("status") == "ambiguous":
                self._transition_lifecycle(
                    lifecycle_store,
                    request.run_date,
                    order_id,
                    "submitting",
                    reason="binance_entry_recovery_ambiguous",
                    metadata={"entry_recovery": responses["entry_recovery"]},
                )
                receipt = PaperOrder(
                    order_id=order_id,
                    ticket_id=ticket["ticket_id"],
                    status="submitted_to_binance",
                    requested_price=round(requested_price, 4),
                    fill_price=None,
                    quantity=quantity,
                    filled_at="",
                    rejection_reason="Binance entry recovery ambiguous; no duplicate order submitted",
                )
                self._record_live_request(request, order_id, now, ticket, payloads, readiness, receipt, broker_response=responses)
                return receipt
        try:
            responses["entry"] = recovered_entry or self._post_binance_order(payloads["entry"])
        except (OSError, TimeoutError, RuntimeError, urllib.error.HTTPError, json.JSONDecodeError, ValueError) as exc:
            error = {"error_type": type(exc).__name__, "message": str(exc)}
            responses["entry_error"] = error
            self._transition_lifecycle(
                lifecycle_store,
                request.run_date,
                order_id,
                "rejected",
                reason="binance_entry_rejected",
                metadata=error,
            )
            receipt = PaperOrder(
                order_id=order_id,
                ticket_id=ticket["ticket_id"],
                status="rejected",
                requested_price=round(requested_price, 4),
                fill_price=None,
                quantity=quantity,
                filled_at="",
                rejection_reason=f"{error['error_type']}: {error['message']}",
            )
            self._record_live_request(request, order_id, now, ticket, payloads, readiness, receipt, broker_response=responses)
            return receipt

        terminal_state = self._binance_entry_terminal_state(responses["entry"])
        if terminal_state:
            self._transition_lifecycle(
                lifecycle_store,
                request.run_date,
                order_id,
                terminal_state,
                reason=f"binance_entry_{terminal_state}",
                metadata={"broker_response": self._safe_order_payload(responses["entry"])},
            )
            receipt = PaperOrder(
                order_id=order_id,
                ticket_id=ticket["ticket_id"],
                status=terminal_state,
                requested_price=round(requested_price, 4),
                fill_price=None,
                quantity=quantity,
                filled_at="",
                rejection_reason=str(responses["entry"].get("status") or responses["entry"].get("msg") or terminal_state),
            )
            self._record_live_request(request, order_id, now, ticket, payloads, readiness, receipt, broker_response=responses)
            return receipt

        lifecycle = self._transition_lifecycle(
            lifecycle_store,
            request.run_date,
            order_id,
            "accepted",
            reason="binance_entry_accepted",
            metadata={"broker_order_id": responses["entry"].get("orderId"), "client_order_id": responses["entry"].get("clientOrderId")},
        )
        executed_reported = self._binance_executed_quantity(responses["entry"])
        fill_price = self._binance_fill_price(responses["entry"])
        if executed_reported is None and fill_price is not None:
            executed_quantity = quantity
        else:
            executed_quantity = executed_reported or 0.0
            if executed_quantity <= 0:
                fill_price = None
        if fill_price is None and executed_quantity > 0:
            fill_price = requested_price
            responses["fill_price_source"] = "requested_price_fallback_after_executed_qty"
        filled_quantity = executed_quantity if executed_quantity > 0 else 0.0
        status = "submitted_to_binance"
        if filled_quantity > 0:
            lifecycle_state = "partially_filled" if filled_quantity + 1e-12 < quantity else "filled"
            lifecycle = self._transition_lifecycle(
                lifecycle_store,
                request.run_date,
                order_id,
                lifecycle_state,
                reason="binance_entry_fill_reported",
                filled_quantity=filled_quantity,
                metadata={"requested_quantity": quantity, "filled_quantity": filled_quantity},
            )
            payloads["protective_orders"] = self._binance_protective_payloads(ticket, order_id, symbol, filled_quantity, filters)
            for protective in payloads.get("protective_orders", []):
                try:
                    responses["protective_orders"].append(self._post_binance_order(protective))
                except (OSError, TimeoutError, RuntimeError, urllib.error.HTTPError, json.JSONDecodeError, ValueError) as exc:
                    responses["protective_errors"].append(
                        {
                            "payload": self._safe_order_payload(protective),
                            "error_type": type(exc).__name__,
                            "message": str(exc),
                        }
                    )
            responses["protective_status"] = self._protective_status(payloads, responses)
            status = "filled"
            if responses["protective_status"] == "pass":
                self._transition_lifecycle(
                    lifecycle_store,
                    request.run_date,
                    order_id,
                    "protective_attached",
                    reason="binance_protective_orders_attached",
                    protective_quantity=filled_quantity,
                    metadata={"protective_status": responses["protective_status"]},
                )
            elif responses["protective_status"] in {"failed", "partial"}:
                status = "protective_order_missing"
                self._transition_lifecycle(
                    lifecycle_store,
                    request.run_date,
                    order_id,
                    "protective_failed",
                    reason="binance_protective_order_failed",
                    protective_quantity=float(len(responses.get("protective_orders", [])) > 0) * filled_quantity,
                    metadata={"protective_status": responses["protective_status"], "protective_errors": responses["protective_errors"]},
                )
                lifecycle_store.record_blocker(
                    request.run_date,
                    order_id,
                    reason=f"protective_status={responses['protective_status']}",
                    source="binance_demo_protective_orders",
                    action="reduce_only_close_or_halt_until_position_reconciled",
                    details={"filled_quantity": filled_quantity, "protective_errors": responses["protective_errors"]},
                )
            # Mirror the REAL fill into local accounting so the system tracks its
            # own live position (exchange-managed exits). Best-effort, but the
            # outcome is recorded in the artifact and reconciliation backstops any
            # miss (exchange-has / local-missing -> drift -> alert).
            responses["local_mirror"] = self._mirror_live_fill(request.run_date, ticket, fill_price or requested_price, filled_quantity, order_id)
            if status == "protective_order_missing" and self._auto_close_on_protective_failure(readiness):
                responses["emergency_close"] = self._reduce_only_close_after_protective_failure(
                    request.run_date,
                    ticket,
                    payloads,
                    symbol,
                    filled_quantity,
                    fill_price or requested_price,
                    order_id,
                )
                if responses["emergency_close"].get("status") == "closed":
                    self._transition_lifecycle(
                        lifecycle_store,
                        request.run_date,
                        order_id,
                        "closed",
                        reason="protective_failure_emergency_close",
                        metadata=responses["emergency_close"],
                    )
                    status = "protective_order_missing_closed"
        else:
            responses["protective_status"] = "not_requested_until_fill"
        receipt = PaperOrder(
            order_id=order_id,
            ticket_id=ticket["ticket_id"],
            status=status,
            requested_price=round(requested_price, 4),
            fill_price=fill_price,
            quantity=round(filled_quantity if filled_quantity > 0 else quantity, 6),
            filled_at=now if filled_quantity > 0 else "",
            rejection_reason=str(responses["entry"].get("orderId") or responses["entry"].get("clientOrderId") or "submitted to Binance USDM"),
        )
        self._record_live_request(request, order_id, now, ticket, payloads, readiness, receipt, broker_response=responses)
        return receipt

    def _protective_status(self, payloads: dict, responses: dict) -> str:
        expected = len(payloads.get("protective_orders", []) or [])
        if expected == 0:
            return "not_requested"
        posted = len(responses.get("protective_orders", []) or [])
        errors = len(responses.get("protective_errors", []) or [])
        if errors == 0 and posted == expected:
            return "pass"
        if posted == 0:
            return "failed"
        return "partial"

    def _safe_order_payload(self, payload: dict) -> dict:
        return {
            key: value
            for key, value in payload.items()
            if key not in {"signature", "timestamp", "recvWindow"}
        }

    def _transition_lifecycle(
        self,
        store: OrderLifecycleStore,
        run_date: str,
        order_id: str,
        state: str,
        *,
        reason: str,
        metadata: dict | None = None,
        filled_quantity: float | None = None,
        protective_quantity: float | None = None,
    ) -> dict:
        try:
            return store.transition(
                run_date,
                order_id,
                state,
                reason=reason,
                metadata=metadata,
                filled_quantity=filled_quantity,
                protective_quantity=protective_quantity,
            )
        except IllegalOrderTransition:
            return store.current(run_date, order_id)

    def _recover_binance_entry(self, symbol: str, client_order_id: str) -> dict:
        try:
            payload = self._binance_signed_get("/fapi/v1/order", {"symbol": symbol, "origClientOrderId": client_order_id})
        except urllib.error.HTTPError as exc:
            error = self._http_error_payload(exc)
            return self._classify_binance_recovery_payload({"ok": False, "status": exc.code, "error": error})
        except (OSError, TimeoutError, RuntimeError, json.JSONDecodeError, ValueError) as exc:
            return {"status": "ambiguous", "reason": f"{type(exc).__name__}: {exc}", "entry": {}}
        return self._classify_binance_recovery_payload(payload)

    def _classify_binance_recovery_payload(self, payload) -> dict:
        if not isinstance(payload, dict):
            return {"status": "ambiguous", "reason": "unexpected recovery payload", "entry": {}}
        body = payload.get("body") if payload.get("ok") is True and isinstance(payload.get("body"), dict) else payload
        error = payload.get("error") if isinstance(payload.get("error"), dict) else body
        if self._is_binance_not_found(error):
            return {"status": "not_found", "reason": self._binance_error_message(error), "entry": {}}
        if payload.get("ok") is False:
            return {"status": "ambiguous", "reason": self._binance_error_message(error), "entry": {}}
        if isinstance(body, dict) and self._is_binance_order_payload(body):
            return {"status": "found", "reason": "", "entry": body}
        return {"status": "ambiguous", "reason": "recovery payload did not contain a Binance order", "entry": {}}

    def _http_error_payload(self, exc: urllib.error.HTTPError) -> dict:
        try:
            text = exc.read().decode("utf-8", errors="replace")
        except (OSError, ValueError):
            text = ""
        try:
            payload = json.loads(text) if text else {}
        except json.JSONDecodeError:
            payload = {"raw": text[:500]}
        return payload if isinstance(payload, dict) else {"raw": text[:500]}

    def _is_binance_not_found(self, payload) -> bool:
        if not isinstance(payload, dict):
            return False
        code = payload.get("code")
        try:
            if int(code) == -2013:
                return True
        except (TypeError, ValueError):
            pass
        msg = str(payload.get("msg") or payload.get("message") or payload.get("raw") or "").lower()
        return "order does not exist" in msg

    def _binance_error_message(self, payload) -> str:
        if not isinstance(payload, dict):
            return "unknown recovery error"
        return str(payload.get("msg") or payload.get("message") or payload.get("raw") or payload)

    def _is_binance_order_payload(self, payload: dict) -> bool:
        if self._is_binance_not_found(payload):
            return False
        return any(payload.get(key) not in {None, ""} for key in ("orderId", "clientOrderId", "status", "executedQty", "origQty"))

    def _binance_entry_terminal_state(self, response: dict) -> str:
        status = str(response.get("status") or "").upper()
        if status == "REJECTED":
            return "rejected"
        if status in {"CANCELED", "CANCELLED"}:
            return "cancelled"
        if status in {"EXPIRED", "EXPIRED_IN_MATCH"}:
            return "expired"
        return ""

    def _mirror_live_fill(self, run_date: str, ticket: dict, fill_price: float, quantity: float, order_id: str) -> dict:
        try:
            PaperExecutor(self.output_root).record_external_fill(
                run_date, ticket, fill_price=fill_price, quantity=quantity, order_id=order_id, exchange_managed=True,
            )
            return {"mirrored": True}
        except (OSError, KeyError, ValueError, TypeError) as exc:
            return {"mirrored": False, "error": f"{type(exc).__name__}: {exc}"}

    def _record_live_request(
        self,
        request: BrokerOrderRequest,
        order_id: str,
        requested_at: str,
        ticket: dict,
        order_payload: dict,
        readiness: dict,
        receipt: PaperOrder,
        broker_response: dict,
    ) -> None:
        request_dir = str(self.broker_config.get("request_dir", "live_order_requests"))
        path = self.output_root / request_dir / f"{request.run_date}.json"
        rows = [item for item in load_json(path) if item.get("order_id") != order_id]
        rows.append(
            {
                "order_id": order_id,
                "requested_at": requested_at,
                "provider": self.provider,
                "run_date": request.run_date,
                "ticket": ticket,
                "request": order_payload,
                "readiness": readiness,
                "broker_response": broker_response,
                "receipt": receipt.to_dict(),
            }
        )
        write_json(path, rows)

    def _oanda_order_payload(self, ticket: dict, order_id: str, requested_price: float, quantity: float) -> dict:
        raw_type = str(ticket.get("order_type", "limit")).lower()
        order_type = "MARKET" if raw_type == "market" else "LIMIT"
        units = quantity if self._is_buy_action(str(ticket.get("action", ""))) else -quantity
        order = {
            "type": order_type,
            "instrument": self._oanda_instrument(str(ticket.get("asset", "GOLD"))),
            "units": self._format_decimal(units),
            "timeInForce": self._oanda_time_in_force(order_type, str(ticket.get("time_in_force", ""))),
            "positionFill": str(self.broker_config.get("position_fill", "DEFAULT")),
            "clientExtensions": {
                "id": order_id[:128],
                "tag": "trading_orchestrator",
                "comment": str(ticket.get("ticket_id", ""))[:128],
            },
        }
        if order_type == "LIMIT":
            order["price"] = self._format_price(requested_price)
        if ticket.get("stop_loss") is not None:
            order["stopLossOnFill"] = {"price": self._format_price(float(ticket["stop_loss"]))}
        targets = ticket.get("targets") or []
        if targets:
            order["takeProfitOnFill"] = {"price": self._format_price(float(targets[0]))}
        return {"order": order}

    def _post_oanda_order(self, payload: dict) -> dict:
        apply_live_env()
        token_env = str(self.broker_config.get("api_key_env") or self.broker_config.get("token_env") or "OANDA_API_TOKEN")
        account_id_env = str(self.broker_config.get("account_id_env", "OANDA_ACCOUNT_ID"))
        token = os.getenv(token_env)
        account_id = os.getenv(account_id_env)
        if not live_env_value_present(token_env) or not live_env_value_present(account_id_env):
            raise RuntimeError(f"missing OANDA environment variables: {token_env}, {account_id_env}")
        url = f"{self._oanda_base_url()}/v3/accounts/{urllib.parse.quote(account_id, safe='')}/orders"
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept-Datetime-Format": "RFC3339",
                "Content-Type": "application/json",
                "User-Agent": "TradingOrchestrator/1.0",
            },
        )
        with self.opener(request, timeout=int(self.broker_config.get("timeout_seconds", 10))) as response:
            return json.loads(response.read().decode("utf-8"))

    def _post_binance_order(self, payload: dict) -> dict:
        return self._binance_signed_request("POST", "/fapi/v1/order", payload)

    def _delete_binance_open_orders(self, symbol: str) -> dict:
        return self._binance_signed_request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})

    def _auto_close_on_protective_failure(self, readiness: dict) -> bool:
        if readiness.get("demo_trading") is not True:
            return False
        return str(self.broker_config.get("protective_failure_action", "reduce_only_close")) == "reduce_only_close"

    def _reduce_only_close_after_protective_failure(
        self,
        run_date: str,
        ticket: dict,
        payloads: dict,
        symbol: str,
        quantity: float,
        fallback_price: float,
        order_id: str,
    ) -> dict:
        entry_side = str(payloads.get("entry", {}).get("side", "BUY")).upper()
        close_payload = {
            "symbol": symbol,
            "side": "SELL" if entry_side == "BUY" else "BUY",
            "type": "MARKET",
            "quantity": self._format_decimal(quantity),
            "reduceOnly": "true",
            "newClientOrderId": f"{order_id[:20]}_protect_close"[:36],
        }
        result = {
            "action": "reduce_only_close",
            "status": "attempted",
            "request": self._safe_order_payload(close_payload),
            "response": {},
            "local_mirror": {},
        }
        try:
            response = self._post_binance_order(close_payload)
            close_price = self._binance_fill_price(response) or fallback_price
            close_qty = self._binance_executed_quantity(response) or quantity
            local = PaperExecutor(self.output_root).record_external_close(
                run_date,
                order_id=order_id,
                exit_price=close_price,
                quantity=close_qty,
                exit_reason="protective_order_missing_emergency_close",
                close_order_id=str(response.get("orderId") or response.get("clientOrderId") or ""),
            )
            result.update(
                {
                    "status": "closed" if local.get("closed") else "submitted",
                    "response": response,
                    "fill_price": close_price,
                    "quantity": close_qty,
                    "local_mirror": local,
                }
            )
        except (OSError, TimeoutError, RuntimeError, urllib.error.HTTPError, json.JSONDecodeError, ValueError) as exc:
            result.update({"status": "failed", "error_type": type(exc).__name__, "message": str(exc)})
        return result

    def _binance_signed_request(self, method: str, endpoint: str, params: dict) -> dict:
        apply_live_env()
        api_key_env = str(self.broker_config.get("api_key_env", "BINANCE_API_KEY"))
        secret_env = str(self.broker_config.get("api_secret_env", "BINANCE_API_SECRET"))
        api_key = os.getenv(api_key_env)
        api_secret = os.getenv(secret_env)
        if not live_env_value_present(api_key_env) or not live_env_value_present(secret_env):
            raise RuntimeError(f"missing Binance environment variables: {api_key_env}, {secret_env}")
        signed = {**params, "timestamp": int(time.time() * 1000), "recvWindow": int(self.broker_config.get("recv_window_ms", 5000))}
        query = urllib.parse.urlencode(signed)
        signature = hmac.new(str(api_secret).encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
        body = f"{query}&signature={signature}".encode("utf-8")
        request = urllib.request.Request(
            f"{self._binance_base_url()}{endpoint}",
            data=body,
            method=method,
            headers={
                "X-MBX-APIKEY": str(api_key),
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "TradingOrchestrator/1.0",
            },
        )
        with self.opener(request, timeout=int(self.broker_config.get("timeout_seconds", 10))) as response:
            return json.loads(response.read().decode("utf-8"))

    def _binance_signed_get(self, endpoint: str, params: dict) -> dict:
        apply_live_env()
        api_key_env = str(self.broker_config.get("api_key_env", "BINANCE_API_KEY"))
        secret_env = str(self.broker_config.get("api_secret_env", "BINANCE_API_SECRET"))
        api_key = os.getenv(api_key_env)
        api_secret = os.getenv(secret_env)
        if not live_env_value_present(api_key_env) or not live_env_value_present(secret_env):
            raise RuntimeError(f"missing Binance environment variables: {api_key_env}, {secret_env}")
        signed = {**params, "timestamp": int(time.time() * 1000), "recvWindow": int(self.broker_config.get("recv_window_ms", 5000))}
        query = urllib.parse.urlencode(signed)
        signature = hmac.new(str(api_secret).encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
        request = urllib.request.Request(
            f"{self._binance_base_url()}{endpoint}?{query}&signature={signature}",
            method="GET",
            headers={"X-MBX-APIKEY": str(api_key), "User-Agent": "TradingOrchestrator/1.0"},
        )
        with self.opener(request, timeout=int(self.broker_config.get("timeout_seconds", 10))) as response:
            return json.loads(response.read().decode("utf-8"))

    def _oanda_base_url(self) -> str:
        if self.broker_config.get("base_url"):
            return str(self.broker_config["base_url"]).rstrip("/")
        environment = str(self.broker_config.get("environment", "practice")).lower()
        return "https://api-fxtrade.oanda.com" if environment == "live" else "https://api-fxpractice.oanda.com"

    def _binance_base_url(self) -> str:
        if self.broker_config.get("base_url"):
            return str(self.broker_config["base_url"]).rstrip("/")
        environment = str(self.broker_config.get("environment", "live")).lower()
        return "https://testnet.binancefuture.com" if environment == "testnet" else "https://fapi.binance.com"

    def _oanda_instrument(self, asset: str) -> str:
        mapping = self.broker_config.get("instrument_map", {"GOLD": "XAU_USD", "XAUUSD": "XAU_USD"})
        return str(mapping.get(asset, asset))

    def _binance_symbol(self, asset: str) -> str:
        mapping = self.broker_config.get("instrument_map", {"GOLD": "XAUUSDT", "XAUUSD": "XAUUSDT"})
        return str(mapping.get(asset, asset)).upper()

    def _binance_symbol_status(self, symbol: str) -> dict:
        try:
            params = urllib.parse.urlencode({"symbol": symbol})
            request = urllib.request.Request(
                f"{self._binance_base_url()}/fapi/v1/exchangeInfo?{params}",
                headers={"User-Agent": "TradingOrchestrator/1.0"},
            )
            with self.opener(request, timeout=int(self.broker_config.get("timeout_seconds", 10))) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, TimeoutError, json.JSONDecodeError, KeyError, ValueError):
            return {"symbol": symbol, "status": "UNKNOWN", "filters": {}, "truth_level": "exchange_info_unavailable"}
        symbols = payload.get("symbols") or []
        item = next((row for row in symbols if row.get("symbol") == symbol), symbols[0] if symbols else {})
        filters = {row.get("filterType"): row for row in item.get("filters", []) if row.get("filterType")}
        lot = filters.get("LOT_SIZE", {})
        market_lot = filters.get("MARKET_LOT_SIZE", {})
        price_filter = filters.get("PRICE_FILTER", {})
        min_notional = filters.get("MIN_NOTIONAL", {})
        return {
            "symbol": item.get("symbol", symbol),
            "status": item.get("status", "UNKNOWN"),
            "contract_type": item.get("contractType", ""),
            "underlying_type": item.get("underlyingType", ""),
            "margin_asset": item.get("marginAsset", ""),
            "filters": {
                "tick_size": price_filter.get("tickSize", "0.01"),
                "step_size": market_lot.get("stepSize") or lot.get("stepSize", "0.001"),
                "min_qty": market_lot.get("minQty") or lot.get("minQty", "0.001"),
                "min_notional": min_notional.get("notional", "5"),
            },
            "truth_level": "official_binance_exchange_info",
        }

    def _binance_order_payloads(
        self,
        ticket: dict,
        order_id: str,
        symbol: str,
        requested_price: float,
        quantity: float,
        filters: dict,
    ) -> dict:
        is_buy = self._is_buy_action(str(ticket.get("action", "")))
        side = "BUY" if is_buy else "SELL"
        exit_side = "SELL" if is_buy else "BUY"
        raw_type = str(ticket.get("order_type", "market")).lower()
        order_type = "MARKET" if raw_type == "market" else "LIMIT"
        entry = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "quantity": self._format_decimal(quantity),
            "newClientOrderId": order_id[:36],
        }
        if order_type == "LIMIT":
            entry["timeInForce"] = "GTC" if str(ticket.get("time_in_force", "")).lower() != "ioc" else "IOC"
            entry["price"] = self._format_binance_price(requested_price, filters)
        protective = self._binance_protective_payloads(ticket, order_id, symbol, quantity, filters)
        return {"entry": entry, "protective_orders": protective}

    def _binance_protective_payloads(
        self,
        ticket: dict,
        order_id: str,
        symbol: str,
        quantity: float,
        filters: dict,
    ) -> list[dict]:
        is_buy = self._is_buy_action(str(ticket.get("action", "")))
        exit_side = "SELL" if is_buy else "BUY"
        protective = []
        if quantity <= 0:
            return protective
        if ticket.get("stop_loss") is not None:
            protective.append(
                {
                    "symbol": symbol,
                    "side": exit_side,
                    "type": "STOP_MARKET",
                    "quantity": self._format_decimal(quantity),
                    "reduceOnly": "true",
                    "stopPrice": self._format_binance_price(float(ticket["stop_loss"]), filters),
                    "newClientOrderId": f"{order_id[:28]}_sl",
                }
            )
        targets = ticket.get("targets") or []
        if targets:
            protective.append(
                {
                    "symbol": symbol,
                    "side": exit_side,
                    "type": "TAKE_PROFIT_MARKET",
                    "quantity": self._format_decimal(quantity),
                    "reduceOnly": "true",
                    "stopPrice": self._format_binance_price(float(targets[0]), filters),
                    "newClientOrderId": f"{order_id[:28]}_tp",
                }
            )
        return protective

    def _format_binance_price(self, value: float, filters: dict) -> str:
        return self._format_decimal(self._round_step(value, str(filters.get("tick_size", "0.01"))))

    def _round_step(self, value: float, step: str) -> float:
        step_decimal = Decimal(str(step))
        if step_decimal == 0:
            return float(value)
        rounded = (Decimal(str(value)) / step_decimal).to_integral_value(rounding=ROUND_DOWN) * step_decimal
        return float(rounded)

    def _binance_fill_price(self, response: dict) -> float | None:
        for key in ["avgPrice", "price"]:
            value = response.get(key)
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                parsed = 0.0
            if parsed:
                return parsed
        fills = response.get("fills") or []
        if fills:
            try:
                return float(fills[0].get("price"))
            except (TypeError, ValueError):
                return None
        return None

    def _binance_executed_quantity(self, response: dict) -> float | None:
        try:
            value = float(response.get("executedQty", 0) or 0)
        except (TypeError, ValueError):
            value = 0.0
        return value or None

    def _oanda_time_in_force(self, order_type: str, raw: str) -> str:
        value = raw.strip().upper()
        if order_type == "MARKET":
            return value if value in {"FOK", "IOC"} else "FOK"
        mapping = {"DAY": "GFD", "GTC": "GTC", "GFD": "GFD", "GTD": "GTD"}
        return mapping.get(value, "GTC")

    def _is_buy_action(self, action: str) -> bool:
        return action.lower() in {"buy", "prepare_buy", "long", "open_long"}

    def _format_decimal(self, value: float) -> str:
        return f"{value:.6f}".rstrip("0").rstrip(".")

    def _format_price(self, value: float) -> str:
        return f"{value:.3f}".rstrip("0").rstrip(".")

    def _mt5_outbox_dir(self) -> Path:
        raw = Path(str(self.broker_config.get("outbox_dir", "data/broker_outbox/mt5")))
        return raw if raw.is_absolute() else ROOT / raw

    def _mt5_inbox_dir(self) -> Path:
        raw = Path(str(self.broker_config.get("inbox_dir", "data/broker_inbox/mt5")))
        return raw if raw.is_absolute() else ROOT / raw

    def _ensure_mt5_bridge_docs(self, outbox_dir: Path, inbox_dir: Path) -> dict:
        request_template = outbox_dir / "ORDER_REQUEST.json.template"
        receipt_template = inbox_dir / "ORDER_RECEIPT.json.template"
        outbox_readme = outbox_dir / "README_MT5_FILE_BRIDGE.md"
        inbox_readme = inbox_dir / "README_MT5_FILE_BRIDGE.md"
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        request_example = {
            "order_id": "live_dryrun_example",
            "created_at": now,
            "dry_run": True,
            "symbol": "GOLD",
            "action": "prepare_buy",
            "order_type": "limit",
            "time_in_force": "day",
            "requested_price": 4571.3,
            "quantity": 0.25,
            "stop_loss": 4480.0,
            "targets": [4750.0],
            "source_ticket_id": "ticket_gold_YYYYMMDD_example",
            "manual_execution_required": True,
        }
        receipt_example = {
            "order_id": "live_dryrun_example",
            "broker_order_id": "mt5-ticket-id",
            "status": "filled",
            "fill_price": 4571.3,
            "filled_quantity": 0.25,
            "timestamp": now,
            "message": "executed by external MT5 EA or manual bridge",
        }
        request_template.write_text(json.dumps(request_example, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        receipt_template.write_text(json.dumps(receipt_example, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        outbox_readme.write_text(
            "\n".join(
                [
                    "# MT5 File Bridge Outbox",
                    "",
                    "The trading bot writes one JSON order request per order into this directory.",
                    "An external MT5 Expert Advisor, script, or manual bridge may consume these files.",
                    "",
                    "Contract:",
                    "- Treat `dry_run: true` as a non-executable test artifact.",
                    "- Execute only when live mode is intentionally enabled and `dry_run: false`.",
                    "- Preserve `order_id`; it is the correlation key for broker receipts.",
                    "- Do not modify request files in place after consuming them.",
                    "",
                    "Order request fields are shown in `ORDER_REQUEST.json.template`.",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        inbox_readme.write_text(
            "\n".join(
                [
                    "# MT5 File Bridge Inbox",
                    "",
                    "Write broker execution receipts here as JSON files matching `*.json`.",
                    "The bot imports receipts with `python3 -m pipelines.broker_receipts --date YYYY-MM-DD`.",
                    "",
                    "Required receipt fields:",
                    "- `order_id` or `client_order_id`",
                    "- `status` such as `filled`, `rejected`, `partial`, or `pending`",
                    "",
                    "Recommended receipt fields:",
                    "- `broker_order_id` or `ticket`",
                    "- `fill_price`",
                    "- `filled_quantity` or `quantity`",
                    "- `timestamp` or `filled_at`",
                    "- `message` or `reason`",
                    "",
                    "Receipt examples are shown in `ORDER_RECEIPT.json.template`.",
                    "Templates use `.json.template` so they are ignored by the `*.json` importer.",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return {
            "outbox_readme": str(outbox_readme),
            "inbox_readme": str(inbox_readme),
            "request_template": str(request_template),
            "receipt_template": str(receipt_template),
        }

    def _entry_midpoint(self, entry_zone: str) -> float:
        low, high = [float(part) for part in entry_zone.split("-", 1)]
        return (low + high) / 2

    def _quantity(self, ticket: dict, price: float) -> float:
        account_equity = float(self.broker_config.get("dry_run_account_equity", 100_000))
        notional = account_equity * (float(ticket.get("position_size_pct", 0)) / 100)
        return notional / price if price else 0.0

    def _order_id(self, ticket_id: str, run_date: str, requested_price: float) -> str:
        raw = f"{ticket_id}:{run_date}:{self.provider}:entry".encode("utf-8")
        return f"live_dryrun_{hashlib.sha256(raw).hexdigest()[:10]}"

    def _live_activation(self, run_date: str) -> dict:
        rows = load_json(self.output_root / "live_activation" / f"{run_date}.json")
        if not rows:
            rows = load_json(self.output_root / "live_activation" / "current.json")
        return rows[-1] if rows else {}


def broker_preflight(output_root: Path | None = None) -> dict:
    config = load_pipeline_config()
    root = output_root or Path(os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(ROOT / config.get("output_root", "outputs"))))
    mode = str(config.get("execution_mode", "paper")).lower()
    broker_config = config.get("broker", {})
    if mode == "paper":
        result = {
            "provider": broker_config.get("provider", "manual_gateway"),
            "mode": "paper",
            "dry_run": True,
            "live_trading_enabled": bool(config.get("live_trading_enabled", False)),
            "ready": True,
            "block_reason": "paper mode active; live broker is not used",
            "allowed_symbols": broker_config.get("allowed_symbols", []),
            "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        }
    else:
        result = LiveBrokerAdapter(root, bool(config.get("live_trading_enabled", False)), broker_config).preflight()
    write_json(root / "broker_preflight" / "current.json", [result])
    return result


def build_broker_adapter(output_root: Path) -> BrokerAdapter:
    config = load_pipeline_config()
    mode = str(config.get("execution_mode", "paper")).lower()
    if mode == "paper":
        return PaperBrokerAdapter(output_root)
    if mode == "live":
        return LiveBrokerAdapter(output_root, bool(config.get("live_trading_enabled", False)), config.get("broker", {}))
    raise ValueError(f"unknown execution_mode: {mode}")
