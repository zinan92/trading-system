from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

from schemas.market_data import PaperOrder
from services.broker_port import (
    BrokerCancelRequest,
    BrokerOrderRequest,
    BrokerPortDescriptor,
    BrokerProtectiveRecoveryRequest,
    BrokerCapability,
    broker_port_descriptor,
    execution_capabilities_for,
    require_broker_capability,
)
from services.journal_store import load_json, write_json
from services.live_env import apply_live_env, live_env_value_present
from services.order_lifecycle import IllegalOrderTransition, OrderLifecycleStore
from services.paper_executor import PaperExecutor
from services.risk_policy_composition import build_live_money_risk_adapter
from services.risk_port import canonical_live_risk_allows_exposure

class BinanceUsdmBrokerAdapter:
    """Binance USD-M execution, lifecycle, and protection adapter."""

    name = "live"

    def __init__(self, output_root: Path, live_trading_enabled: bool, broker_config: dict | None = None, opener=None) -> None:
        self.output_root = output_root
        self.live_trading_enabled = live_trading_enabled
        self.broker_config = broker_config or {}
        self.provider = str(self.broker_config.get("provider", "binance_usdm"))
        self.dry_run = bool(self.broker_config.get("dry_run", True))
        self.opener = opener or urllib.request.urlopen
        self._binance_transport_instance = None
        self._capabilities = execution_capabilities_for(provider=self.provider, adapter_name=self.name)

    @property
    def capabilities(self):
        return self._capabilities

    @property
    def descriptor(self) -> BrokerPortDescriptor:
        return broker_port_descriptor(self)

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
        if not self.dry_run:
            reconciliation = self._live_reconciliation_check(request.run_date)
            readiness = {
                **readiness,
                "live_reconciliation": reconciliation.get("report", {}),
            }
            if not reconciliation["ready"]:
                raise RuntimeError(reconciliation["block_reason"])
        return self._submit_binance_order(request, readiness)

    def _live_reconciliation_check(self, run_date: str) -> dict:
        """Refresh exchange reconciliation inline before any real order POST.

        Money guardrails demand a fresh same-UTC-day snapshot; without this
        refresh the first mainnet order would always fail
        BLOCKED_MONEY_GUARDRAIL_UNKNOWN on a stale artifact — and worse, a
        seeded-but-wrong snapshot could let an order POST into an unseen venue
        position. Mirrors the demo/testnet adapters' inline checks.
        """
        from services.live_reconciliation import LiveBrokerReconciliation

        report = LiveBrokerReconciliation(self.output_root, self.broker_config, opener=self.opener).run(run_date)
        if report.get("suspected_naked_position"):
            return {
                "ready": False,
                "block_reason": f"live reconciliation suspected naked position: {report.get('escalation_action') or report.get('reason_code')}",
                "report": report,
            }
        if report.get("confirmation_status") == "cannot_confirm":
            return {
                "ready": False,
                "block_reason": f"live reconciliation cannot confirm venue state: {report.get('error')}",
                "report": report,
            }
        if report.get("error"):
            return {"ready": False, "block_reason": f"live reconciliation failed: {report['error']}", "report": report}
        if report.get("drift_count", 0):
            reasons = sorted({str(item.get("reason", "reconciliation drift")) for item in report.get("drifts", [])})
            return {
                "ready": False,
                "block_reason": "live reconciliation drift: " + "; ".join(reasons),
                "report": report,
            }
        return {"ready": True, "block_reason": "", "report": report}

    def preflight(self) -> dict:
        return self._binance_preflight()

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
        guardrails = self._live_money_guardrails_for_order(request, readiness, symbol, str(payloads["entry"].get("side") or ""), requested_price, quantity)
        if guardrails and not canonical_live_risk_allows_exposure(guardrails):
            enriched_readiness = {**readiness, "live_money_guardrails": guardrails}
            receipt = PaperOrder(
                order_id=order_id,
                ticket_id=ticket["ticket_id"],
                status="blocked",
                requested_price=round(requested_price, 4),
                fill_price=None,
                quantity=quantity,
                filled_at="",
                rejection_reason=guardrails.get("primary_blocker", {}).get("message") or "live money guardrails block new order",
            )
            self._record_live_request(
                request,
                order_id,
                now,
                ticket,
                {"blocked_entry": self._safe_order_payload(payloads["entry"])},
                enriched_readiness,
                receipt,
                broker_response={"live_money_guardrails": guardrails},
            )
            raise RuntimeError(f"live money guardrails block order: {receipt.rejection_reason}")
        if guardrails:
            readiness = {**readiness, "live_money_guardrails": guardrails}
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
            classification = self._classify_binance_entry_submit_error(exc)
            error = classification["error"]
            responses["entry_error"] = error
            if classification["status"] == "ambiguous":
                self._transition_lifecycle(
                    lifecycle_store,
                    request.run_date,
                    order_id,
                    "submitting",
                    reason="binance_entry_submit_ambiguous",
                    metadata=error,
                )
                receipt = PaperOrder(
                    order_id=order_id,
                    ticket_id=ticket["ticket_id"],
                    status="submitted_to_binance",
                    requested_price=round(requested_price, 4),
                    fill_price=None,
                    quantity=quantity,
                    filled_at="",
                    rejection_reason=f"Binance entry submit ambiguous; recover by idempotency key before retrying: {error['message']}",
                )
                self._record_live_request(request, order_id, now, ticket, payloads, readiness, receipt, broker_response=responses)
                return receipt
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
                    protective_response = self._post_and_classify_binance_protective_order(protective)
                    if protective_response.get("error"):
                        responses["protective_errors"].append(protective_response["error"])
                    else:
                        responses["protective_orders"].append(protective_response["response"])
                except (OSError, TimeoutError, RuntimeError, urllib.error.HTTPError, json.JSONDecodeError, ValueError) as exc:
                    responses["protective_errors"].append(
                        {
                            "payload": self._safe_order_payload(protective),
                            **self._binance_exception_error(exc),
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
                    source=self._protective_blocker_source(readiness),
                    action="reduce_only_close_or_halt_until_position_reconciled",
                    details={"filled_quantity": filled_quantity, "protective_errors": responses["protective_errors"]},
                )
            # Mirror the REAL fill into local accounting so the system tracks its
            # own live position (exchange-managed exits). Best-effort, but the
            # outcome is recorded in the artifact and reconciliation backstops any
            # miss (exchange-has / local-missing -> drift -> alert).
            responses["local_mirror"] = self._mirror_live_fill(
                request.run_date,
                ticket,
                fill_price or requested_price,
                filled_quantity,
                order_id,
                protection_verified=responses["protective_status"] == "pass",
            )
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

    def _live_money_guardrails_for_order(
        self,
        request: BrokerOrderRequest,
        readiness: dict,
        symbol: str,
        side: str,
        requested_price: float,
        quantity: float,
    ) -> dict:
        mode = str(readiness.get("mode") or self.broker_config.get("environment") or "live").lower()
        if self.provider != "binance_usdm" or mode not in {"live", "testnet"}:
            return {}
        return build_live_money_risk_adapter(self.output_root, broker_config=self.broker_config).evaluate_order(
            request.run_date,
            ticket=request.ticket,
            symbol=symbol,
            side=side,
            requested_price=requested_price,
            quantity=quantity,
            source=f"{self.provider}:{mode}",
            reconciliation=readiness.get("live_reconciliation", {}) if isinstance(readiness.get("live_reconciliation"), dict) else {},
        )

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
            if key not in {"signature", "timestamp", "recvWindow"} and not str(key).startswith("_")
        }

    def _protective_blocker_source(self, readiness: dict) -> str:
        if readiness.get("demo_trading") is True:
            return "binance_demo_protective_orders"
        if readiness.get("testnet_trading") is True:
            return "binance_usdm_testnet_protective_orders"
        return "binance_usdm_live_protective_orders"

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
            payload = self._binance_signed_get(
                self._binance_transport().endpoints.order,
                {"symbol": symbol, "origClientOrderId": client_order_id},
            )
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

    def _classify_binance_entry_submit_error(self, exc: Exception) -> dict:
        payload: dict = {}
        http_status: int | None = None
        if isinstance(exc, urllib.error.HTTPError):
            http_status = exc.code
            payload = self._http_error_payload(exc)
        error = {
            "error_type": type(exc).__name__,
            "message": self._binance_error_message(payload) if payload else str(exc),
            "http_status": http_status,
            "binance_error": payload,
        }
        if isinstance(exc, urllib.error.HTTPError):
            if self._is_binance_ambiguous_submit_error(exc.code, payload):
                return {"status": "ambiguous", "error": error}
            return {"status": "rejected", "error": error}
        if isinstance(exc, RuntimeError) and "missing Binance environment variables" in str(exc):
            return {"status": "rejected", "error": error}
        return {"status": "ambiguous", "error": error}

    def _is_binance_ambiguous_submit_error(self, status: int, payload: dict) -> bool:
        if status >= 500:
            return True
        message = self._binance_error_message(payload).lower()
        return any(
            marker in message
            for marker in (
                "execution status unknown",
                "unknown error",
                "service unavailable",
                "request timeout",
                "internal error",
            )
        )

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

    def _binance_exception_error(self, exc: Exception) -> dict:
        """Classify a failed Binance request for diagnostics.

        ``urllib`` raises ``HTTPError`` before its response body is read, so a
        bare ``str(exc)`` collapses every rejection reason into the same
        generic "HTTP Error 400: Bad Request" and the real Binance
        ``{"code", "msg"}`` body is lost. Read it here instead.
        """
        if isinstance(exc, urllib.error.HTTPError):
            payload = self._http_error_payload(exc)
            return {
                "error_type": type(exc).__name__,
                "message": self._binance_error_message(payload) if payload else str(exc),
                "http_status": exc.code,
                "binance_error": payload,
            }
        return {"error_type": type(exc).__name__, "message": str(exc)}

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

    def _mirror_live_fill(
        self,
        run_date: str,
        ticket: dict,
        fill_price: float,
        quantity: float,
        order_id: str,
        *,
        protection_verified: bool = False,
    ) -> dict:
        try:
            PaperExecutor(self.output_root).record_external_fill(
                run_date,
                ticket,
                fill_price=fill_price,
                quantity=quantity,
                order_id=order_id,
                exchange_managed=True,
                protection_verified=protection_verified,
            )
            return {"mirrored": True}
        except (OSError, KeyError, ValueError, TypeError) as exc:
            return {"mirrored": False, "error": f"{type(exc).__name__}: {exc}"}

    def recover_missing_protective_orders(
        self,
        run_date: str,
        lifecycle_record: dict,
        exchange_position: dict | None = None,
        *,
        source: str = "order_recovery",
    ) -> dict:
        """Recovery-only path for a real exchange fill that exists without a
        resting reduceOnly stop. It never submits a new entry order."""

        order_id = str(lifecycle_record.get("order_id") or "")
        ticket = self._ticket_from_lifecycle_record(lifecycle_record)
        symbol = self._recovery_exchange_symbol(ticket, lifecycle_record, exchange_position)
        quantity = self._recovery_position_quantity(lifecycle_record, exchange_position)
        entry_price = self._recovery_entry_price(lifecycle_record, exchange_position)
        result = {
            "action": "attach_missing_protective_orders",
            "run_date": run_date,
            "order_id": order_id,
            "ticket_id": str(lifecycle_record.get("ticket_id") or ticket.get("ticket_id") or ""),
            "exchange_symbol": symbol,
            "quantity": round(quantity, 12),
            "status": "blocked",
            "protective_status": "not_attempted",
            "network_order_created": False,
            "request": {"protective_orders": []},
            "broker_response": {"protective_orders": [], "protective_errors": []},
            "local_mirror": {},
            "blocker": {},
        }
        store = OrderLifecycleStore(self.output_root)
        if not order_id or not ticket or quantity <= 0:
            reason = "cannot recover missing protective orders without durable order_id, ticket, and non-zero exchange quantity"
            result["block_reason"] = reason
            result["blocker"] = store.record_blocker(
                run_date,
                order_id,
                reason=reason,
                source=source,
                action="halt_new_orders_until_naked_position_resolved",
                details={"lifecycle_record": lifecycle_record, "exchange_position": exchange_position or {}},
            )
            return result

        existing_check = self._existing_resting_protective_coverage(symbol, self._exchange_position_amount(exchange_position or {}))
        result["existing_protective_orders"] = existing_check.get("matching_protective_orders", [])
        if existing_check.get("covered"):
            lifecycle = self._transition_lifecycle(
                store,
                run_date,
                order_id,
                "protective_attached",
                reason="missing_protective_orders_already_resting_on_exchange",
                protective_quantity=quantity,
                metadata={"source": source, "existing_protective_orders": existing_check.get("matching_protective_orders", [])},
            )
            if not self._local_position_matches_exchange(ticket, exchange_position):
                result["local_mirror"] = self._mirror_live_fill(
                    run_date,
                    ticket,
                    entry_price,
                    quantity,
                    order_id,
                    protection_verified=True,
                )
            result.update({"status": "recovered", "protective_status": "already_resting", "lifecycle_state": lifecycle.get("state", "")})
            self._record_protective_recovery_request(run_date, ticket, result)
            return result
        if existing_check.get("error"):
            reason = f"cannot verify existing protective orders before recovery: {existing_check['error']}"
            result["block_reason"] = reason
            result["blocker"] = store.record_blocker(
                run_date,
                order_id,
                reason=reason,
                source=source,
                action="halt_new_orders_until_naked_position_resolved",
                details={"exchange_position": exchange_position or {}, "existing_check": existing_check},
            )
            self._record_protective_recovery_request(run_date, ticket, result)
            return result

        filters = self._binance_symbol_status(symbol).get("filters", {})
        protective_payloads = self._binance_protective_payloads(
            ticket,
            order_id,
            symbol,
            quantity,
            filters,
            exit_side=self._exit_side_for_exchange_position(exchange_position),
        )
        result["request"] = {"protective_orders": [self._safe_order_payload(item) for item in protective_payloads]}
        if not protective_payloads:
            reason = "cannot recover missing protective orders because ticket has no stop_loss or target"
            result["block_reason"] = reason
            result["blocker"] = store.record_blocker(
                run_date,
                order_id,
                reason=reason,
                source=source,
                action="reduce_only_close_or_halt_until_position_reconciled",
                details={"ticket_id": ticket.get("ticket_id", ""), "exchange_position": exchange_position or {}},
            )
            self._record_protective_recovery_request(run_date, ticket, result)
            return result

        responses = {"protective_orders": [], "protective_errors": []}
        for protective in protective_payloads:
            try:
                protective_response = self._post_and_classify_binance_protective_order(protective)
                if protective_response.get("error"):
                    responses["protective_errors"].append(protective_response["error"])
                else:
                    responses["protective_orders"].append(protective_response["response"])
            except (OSError, TimeoutError, RuntimeError, urllib.error.HTTPError, json.JSONDecodeError, ValueError) as exc:
                responses["protective_errors"].append(
                    {
                        "payload": self._safe_order_payload(protective),
                        **self._binance_exception_error(exc),
                    }
                )
        protective_status = self._protective_status({"protective_orders": protective_payloads}, responses)
        result["protective_status"] = protective_status
        result["broker_response"] = {
            "protective_orders": responses["protective_orders"],
            "protective_errors": responses["protective_errors"],
            "protective_status": protective_status,
        }
        result["network_order_created"] = bool(responses["protective_orders"])

        if protective_status == "pass":
            lifecycle = self._transition_lifecycle(
                store,
                run_date,
                order_id,
                "protective_attached",
                reason="missing_protective_orders_recovered",
                protective_quantity=quantity,
                metadata={"source": source, "protective_status": protective_status},
            )
            if not self._local_position_matches_exchange(ticket, exchange_position):
                result["local_mirror"] = self._mirror_live_fill(
                    run_date,
                    ticket,
                    entry_price,
                    quantity,
                    order_id,
                    protection_verified=True,
                )
            result.update({"status": "recovered", "lifecycle_state": lifecycle.get("state", "")})
            self._record_protective_recovery_request(run_date, ticket, result)
            return result

        self._transition_lifecycle(
            store,
            run_date,
            order_id,
            "protective_failed",
            reason="missing_protective_recovery_failed",
            protective_quantity=float(len(responses.get("protective_orders", [])) > 0) * quantity,
            metadata={"source": source, "protective_status": protective_status, "protective_errors": responses["protective_errors"]},
        )
        reason = f"protective_recovery_status={protective_status}"
        result["block_reason"] = reason
        result["blocker"] = store.record_blocker(
            run_date,
            order_id,
            reason=reason,
            source=source,
            action="reduce_only_close_or_halt_until_position_reconciled",
            details={"filled_quantity": quantity, "protective_errors": responses["protective_errors"]},
        )
        self._record_protective_recovery_request(run_date, ticket, result)
        return result

    def _ticket_from_lifecycle_record(self, lifecycle_record: dict) -> dict:
        metadata = lifecycle_record.get("metadata") if isinstance(lifecycle_record.get("metadata"), dict) else {}
        ticket = metadata.get("ticket") if isinstance(metadata.get("ticket"), dict) else {}
        return ticket if isinstance(ticket, dict) else {}

    def _recovery_exchange_symbol(self, ticket: dict, lifecycle_record: dict, exchange_position: dict | None) -> str:
        metadata = lifecycle_record.get("metadata") if isinstance(lifecycle_record.get("metadata"), dict) else {}
        symbol = (
            (exchange_position or {}).get("symbol")
            or (exchange_position or {}).get("exchange_symbol")
            or metadata.get("symbol")
            or self._binance_symbol(str(ticket.get("asset") or "GOLD"))
        )
        return str(symbol).upper()

    def _recovery_position_quantity(self, lifecycle_record: dict, exchange_position: dict | None) -> float:
        amount = self._exchange_position_amount(exchange_position or {})
        if amount:
            return abs(amount)
        for key in ("filled_quantity", "requested_quantity"):
            try:
                value = abs(float(lifecycle_record.get(key, 0) or 0))
            except (TypeError, ValueError):
                value = 0.0
            if value:
                return value
        return 0.0

    def _recovery_entry_price(self, lifecycle_record: dict, exchange_position: dict | None) -> float:
        for value in ((exchange_position or {}).get("entry_price"), (exchange_position or {}).get("entryPrice"), lifecycle_record.get("requested_price")):
            try:
                parsed = float(value or 0)
            except (TypeError, ValueError):
                parsed = 0.0
            if parsed:
                return parsed
        return 0.0

    def _exchange_position_amount(self, exchange_position: dict) -> float:
        for key in ("position_amt", "positionAmt", "exchange_qty"):
            try:
                value = float(exchange_position.get(key, 0) or 0)
            except (TypeError, ValueError):
                value = 0.0
            if value:
                return value
        return 0.0

    def _local_position_matches_exchange(self, ticket: dict, exchange_position: dict | None) -> bool:
        if not exchange_position:
            return False
        asset = str(ticket.get("asset") or "GOLD")
        positions = load_json(self.output_root / "paper_positions" / "current.json")
        if not isinstance(positions, dict):
            return False
        local = positions.get(asset)
        if not isinstance(local, dict):
            return False
        if local.get("net_quantity") is not None:
            local_signed = float(local.get("net_quantity", 0) or 0)
        else:
            side = str(local.get("side", "long"))
            local_signed = float(local.get("quantity", 0) or 0) * (1 if side == "long" else -1)
        return abs(local_signed - self._exchange_position_amount(exchange_position)) <= 1e-8

    def _record_protective_recovery_request(self, run_date: str, ticket: dict, result: dict) -> None:
        request_dir = str(self.broker_config.get("request_dir", "live_order_requests"))
        path = self.output_root / request_dir / f"{run_date}.json"
        rows = load_json(path)
        rows.append(
            {
                "order_id": result.get("order_id", ""),
                "requested_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "provider": self.provider,
                "run_date": run_date,
                "ticket": ticket,
                "recovery_action": result.get("action", ""),
                "request": result.get("request", {}),
                "broker_response": result.get("broker_response", {}),
                "receipt": {
                    "order_id": result.get("order_id", ""),
                    "ticket_id": result.get("ticket_id", ""),
                    "status": result.get("status", ""),
                    "quantity": result.get("quantity"),
                },
            }
        )
        write_json(path, rows)

    def _post_and_classify_binance_protective_order(self, payload: dict) -> dict:
        response = self._post_binance_protective_order(payload)
        error = self._binance_protective_response_error(response, payload)
        if error:
            return {"response": response, "error": error}
        return {"response": response, "error": {}}

    def _binance_protective_response_error(self, response: dict, payload: dict) -> dict:
        safe_payload = self._safe_order_payload(payload)
        if not isinstance(response, dict):
            return {
                "payload": safe_payload,
                "error_type": "InvalidProtectiveResponse",
                "message": "protective order response is not a JSON object",
                "response": response,
            }
        if response.get("code") not in {None, "", 0, "0"}:
            return {
                "payload": safe_payload,
                "error_type": "BinanceProtectiveBodyError",
                "message": self._binance_error_message(response),
                "binance_error": response,
            }
        status = str(response.get("status") or response.get("algoStatus") or "").upper()
        if status != "NEW":
            return {
                "payload": safe_payload,
                "error_type": "ProtectiveOrderNotResting",
                "message": f"protective order response status is {status or 'missing'}, expected NEW",
                "response": self._safe_order_payload(response),
            }
        expected_client_id = str(payload.get("newClientOrderId") or payload.get("clientAlgoId") or "")
        response_client_id = str(response.get("clientOrderId") or response.get("clientAlgoId") or "")
        response_order_id = str(response.get("orderId") or response.get("algoId") or "")
        if not response_client_id or not response_order_id:
            return {
                "payload": safe_payload,
                "error_type": "ProtectiveOrderNotExchangeVerified",
                "message": "protective order response lacks exchange order id or client order id",
                "response": self._safe_order_payload(response),
            }
        if expected_client_id and response_client_id != expected_client_id:
            return {
                "payload": safe_payload,
                "error_type": "ProtectiveOrderMismatch",
                "message": f"protective order client id is {response_client_id}, expected {expected_client_id}",
                "response": self._safe_order_payload(response),
            }
        mismatch = self._binance_protective_response_mismatch(response, payload)
        if mismatch:
            return mismatch
        return {}

    def _binance_protective_response_mismatch(self, response: dict, payload: dict) -> dict:
        safe_payload = self._safe_order_payload(payload)
        safe_response = self._safe_order_payload(response)
        expected_symbol = str(payload.get("symbol") or "").upper()
        response_symbol = str(response.get("symbol") or "").upper()
        if response_symbol != expected_symbol:
            return {
                "payload": safe_payload,
                "error_type": "ProtectiveOrderMismatch",
                "message": f"protective order symbol is {response_symbol or 'missing'}, expected {expected_symbol}",
                "response": safe_response,
            }
        expected_side = str(payload.get("side") or "").upper()
        response_side = str(response.get("side") or "").upper()
        if response_side != expected_side:
            return {
                "payload": safe_payload,
                "error_type": "ProtectiveOrderMismatch",
                "message": f"protective order side is {response_side or 'missing'}, expected {expected_side}",
                "response": safe_response,
            }
        expected_type = str(payload.get("type") or "").upper()
        response_type = str(response.get("type") or response.get("orderType") or "").upper()
        if response_type != expected_type:
            return {
                "payload": safe_payload,
                "error_type": "ProtectiveOrderMismatch",
                "message": f"protective order type is {response_type or 'missing'}, expected {expected_type}",
                "response": safe_response,
            }
        expected_qty = self._float_or_none(payload.get("quantity"))
        response_qty = self._float_or_none(response.get("origQty", response.get("quantity")))
        if expected_qty is None or response_qty is None or response_qty + 1e-12 < expected_qty:
            return {
                "payload": safe_payload,
                "error_type": "ProtectiveOrderQuantityMismatch",
                "message": f"protective order quantity is {response_qty if response_qty is not None else 'missing'}, expected at least {expected_qty}",
                "response": safe_response,
            }
        if not self._truthy(response.get("reduceOnly")) and not self._truthy(response.get("closePosition")):
            return {
                "payload": safe_payload,
                "error_type": "ProtectiveOrderNotReduceOnly",
                "message": "protective order response is not reduce-only or close-position",
                "response": safe_response,
            }
        return {}

    def _float_or_none(self, value) -> float | None:
        try:
            if value is None or value == "":
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    def _truthy(self, value) -> bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "y"}

    def _existing_resting_protective_coverage(self, symbol: str, position_amt: float) -> dict:
        if not position_amt:
            return {"covered": False, "matching_protective_orders": []}
        try:
            open_orders = self._binance_open_orders_for_symbol(symbol)
        except (OSError, TimeoutError, RuntimeError, urllib.error.HTTPError, json.JSONDecodeError, ValueError, KeyError) as exc:
            detail = self._binance_exception_error(exc)
            return {"covered": False, "matching_protective_orders": [], "error": f"{detail['error_type']}: {detail['message']}"}
        exit_side = "SELL" if position_amt > 0 else "BUY"
        matching = [
            order
            for order in open_orders
            if self._open_order_covers_position(order, symbol=symbol, exit_side=exit_side)
        ]
        covered_qty = 0.0
        covers_full_position = False
        for order in matching:
            if order.get("close_position"):
                covers_full_position = True
                covered_qty = max(covered_qty, abs(position_amt))
            else:
                covered_qty += float(order.get("orig_qty", 0) or 0)
        return {
            "covered": covers_full_position or covered_qty + 1e-8 >= abs(position_amt),
            "covered_qty": round(covered_qty, 12),
            "matching_protective_orders": matching,
        }

    def _binance_open_orders_for_symbol(self, symbol: str) -> list[dict]:
        transport = self._binance_transport()
        payload = self._binance_signed_get(transport.endpoints.open_orders, {"symbol": symbol})
        orders = self._normalize_binance_open_order_payload(payload, source="open_orders")
        if self._binance_uses_algo_protective_orders():
            algo_payload = self._binance_signed_get(transport.endpoints.open_algo_orders, {"symbol": symbol})
            orders.extend(self._normalize_binance_open_order_payload(algo_payload, source="open_algo_orders"))
        return orders

    def _normalize_binance_open_order_payload(self, payload, *, source: str) -> list[dict]:
        if isinstance(payload, dict) and payload.get("ok") is True:
            payload = payload.get("body", [])
        rows = payload if isinstance(payload, list) else [payload] if isinstance(payload, dict) and payload else []
        normalized = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            normalized.append(
                {
                    "symbol": str(row.get("symbol", "")),
                    "order_id": str(row.get("orderId", row.get("algoId", ""))),
                    "client_order_id": str(row.get("clientOrderId", row.get("clientAlgoId", ""))),
                    "type": str(row.get("type", row.get("orderType", ""))),
                    "side": str(row.get("side", "")),
                    "orig_qty": float(row.get("origQty", row.get("quantity", 0)) or 0),
                    "reduce_only": str(row.get("reduceOnly", row.get("reduce_only", ""))).lower() == "true",
                    "close_position": str(row.get("closePosition", row.get("close_position", ""))).lower() == "true",
                    "status": str(row.get("status", row.get("algoStatus", ""))),
                    "source": source,
                }
            )
        return normalized

    def _open_order_covers_position(self, order: dict, *, symbol: str, exit_side: str) -> bool:
        if str(order.get("symbol", "")).upper() != str(symbol).upper():
            return False
        if str(order.get("side", "")).upper() != exit_side:
            return False
        if not (order.get("reduce_only") or order.get("close_position")):
            return False
        if str(order.get("status") or "").upper() not in {"", "NEW"}:
            return False
        return str(order.get("type") or "").upper() in {"STOP", "STOP_MARKET"}

    def _exit_side_for_exchange_position(self, exchange_position: dict | None) -> str | None:
        amount = self._exchange_position_amount(exchange_position or {})
        if amount > 0:
            return "SELL"
        if amount < 0:
            return "BUY"
        return None

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

    def _post_binance_order(self, payload: dict) -> dict:
        return self._binance_signed_request("POST", self._binance_transport().endpoints.order, payload)

    def _post_binance_protective_order(self, payload: dict) -> dict:
        endpoint = str(payload.get("_endpoint") or self._binance_transport().endpoints.order)
        body = {key: value for key, value in payload.items() if not str(key).startswith("_")}
        return self._binance_signed_request("POST", endpoint, body)

    def _delete_binance_open_orders(self, symbol: str) -> dict:
        return self._binance_signed_request(
            "DELETE",
            self._binance_transport().endpoints.all_open_orders,
            {"symbol": symbol},
        )

    def _delete_binance_algo_open_orders(self, symbol: str) -> dict:
        return self._binance_signed_request(
            "DELETE",
            self._binance_transport().endpoints.algo_open_orders,
            {"symbol": symbol},
        )

    def cancel_binance_order(self, symbol: str, *, orig_client_order_id: str = "", order_id: str = "") -> dict:
        require_broker_capability(self, BrokerCapability.CANCEL_ORDER)
        params = {"symbol": symbol}
        if orig_client_order_id:
            params["origClientOrderId"] = orig_client_order_id
        elif order_id:
            params["orderId"] = order_id
        else:
            raise ValueError("orig_client_order_id or order_id is required to cancel a Binance order")
        return self._binance_signed_request("DELETE", self._binance_transport().endpoints.order, params)

    def cancel_order(self, request: BrokerCancelRequest) -> dict:
        require_broker_capability(self, BrokerCapability.CANCEL_ORDER)
        return self.cancel_binance_order(
            self._binance_symbol(request.asset),
            orig_client_order_id=request.client_order_id,
            order_id=request.broker_order_id,
        )

    def recover_protective_orders(self, request: BrokerProtectiveRecoveryRequest) -> dict:
        require_broker_capability(self, BrokerCapability.PROTECTIVE_RECOVERY)
        return self.recover_missing_protective_orders(
            request.run_date,
            request.lifecycle_record,
            request.exchange_position,
            source=request.source,
        )

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
            result.update({"status": "failed", **self._binance_exception_error(exc)})
        return result

    def _binance_signed_request(self, method: str, endpoint: str, params: dict) -> dict:
        return self._binance_transport().signed_request(method, endpoint, params)

    def _binance_signed_get(self, endpoint: str, params: dict) -> dict:
        return self._binance_transport().signed_get(endpoint, params)

    def _binance_position_risk(self, symbol: str) -> dict:
        return self._binance_signed_get(
            self._binance_transport().endpoints.position_risk,
            {"symbol": symbol},
        )

    def _binance_transport(self):
        transport = self._binance_transport_instance
        if (
            transport is None
            or transport.broker_config is not self.broker_config
            or transport.opener is not self.opener
        ):
            from services.venues.binance_usdm_transport import BinanceUsdmTransport

            transport = BinanceUsdmTransport(
                broker_config=self.broker_config,
                opener=self.opener,
            )
            self._binance_transport_instance = transport
        return transport

    def _binance_base_url(self) -> str:
        return self._binance_transport().base_url()

    def _binance_symbol(self, asset: str) -> str:
        return self._binance_transport().symbol(asset)

    def _binance_symbol_status(self, symbol: str) -> dict:
        return self._binance_transport().symbol_status(symbol)

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
        raw_type = str(ticket.get("order_type", "market")).lower()
        order_type = "MARKET" if raw_type == "market" else "LIMIT"
        entry = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "quantity": self._format_decimal(quantity),
            "newClientOrderId": order_id[:36],
        }
        if order_type == "MARKET":
            entry["newOrderRespType"] = "RESULT"
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
        *,
        exit_side: str | None = None,
    ) -> list[dict]:
        is_buy = self._is_buy_action(str(ticket.get("action", "")))
        resolved_exit_side = str(exit_side or ("SELL" if is_buy else "BUY")).upper()
        protective = []
        if quantity <= 0:
            return protective
        use_algo_orders = self._binance_uses_algo_protective_orders()
        if ticket.get("stop_loss") is not None:
            protective.append(self._binance_protective_payload(order_id, symbol, resolved_exit_side, "STOP_MARKET", float(ticket["stop_loss"]), quantity, filters, "sl", use_algo_orders))
        targets = ticket.get("targets") or []
        if targets:
            protective.append(self._binance_protective_payload(order_id, symbol, resolved_exit_side, "TAKE_PROFIT_MARKET", float(targets[0]), quantity, filters, "tp", use_algo_orders))
        return protective

    def _binance_uses_algo_protective_orders(self) -> bool:
        return self._binance_transport().uses_algo_protective_orders()

    def _binance_protective_payload(
        self,
        order_id: str,
        symbol: str,
        side: str,
        order_type: str,
        trigger_price: float,
        quantity: float,
        filters: dict,
        suffix: str,
        use_algo_orders: bool,
    ) -> dict:
        if use_algo_orders:
            return {
                "_endpoint": self._binance_transport().protective_order_endpoint(use_algo_orders=True),
                "symbol": symbol,
                "side": side,
                "algoType": "CONDITIONAL",
                "type": order_type,
                "quantity": self._format_decimal(quantity),
                "reduceOnly": "true",
                "triggerPrice": self._format_binance_price(trigger_price, filters),
                "clientAlgoId": f"{order_id[:27]}_{suffix}",
            }
        return {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "quantity": self._format_decimal(quantity),
            "reduceOnly": "true",
            "stopPrice": self._format_binance_price(trigger_price, filters),
            "newClientOrderId": f"{order_id[:28]}_{suffix}",
        }

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

    def _is_buy_action(self, action: str) -> bool:
        return action.lower() in {"buy", "prepare_buy", "long", "open_long"}

    def _format_decimal(self, value: float) -> str:
        return f"{value:.6f}".rstrip("0").rstrip(".")

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
