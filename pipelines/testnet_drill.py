from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from services.binance_usdm_testnet_broker_adapter import (
    TESTNET_BASE_URL,
    TESTNET_SYMBOL,
    BinanceUsdmTestnetBrokerAdapter,
)
from services.binance_usdm_testnet_canary import BinanceUsdmTestnetCanary
from services.binance_usdm_testnet_kill_switch import BinanceUsdmTestnetKillSwitch
from services.broker_adapter import BrokerOrderRequest
from services.config_loader import ROOT, load_pipeline_config
from services.cycle_audit import JsonCycleAuditSink
from services.deadman_ping import ExternalDeadmanPing
from services.journal_store import load_json, write_json
from services.live_money_guardrails import LiveMoneyGuardrails
from services.live_reconciliation import LiveBrokerReconciliation
from services.order_lifecycle import OrderLifecycleStore
from services.paper_executor import PaperExecutor
from services.run_date import utc_run_date


DEFAULT_SCENARIOS = (
    "green_loop",
    "crash_recovery",
    "kill_switch",
    "always_on_deadman",
    "money_guardrails",
    "naked_window",
)


class RecordingOpener:
    def __init__(self, delegate=None) -> None:
        self.delegate = delegate or urllib.request.urlopen
        self.calls: list[dict] = []

    def __call__(self, request, timeout):  # noqa: ANN001 - urllib accepts Request-like objects.
        method = request.get_method() if hasattr(request, "get_method") else str(getattr(request, "method", "GET") or "GET")
        parsed = urllib.parse.urlparse(request.full_url)
        self.calls.append(
            {
                "method": method,
                "base_url": f"{parsed.scheme}://{parsed.netloc}",
                "path": parsed.path,
            }
        )
        return self.delegate(request, timeout=timeout)

    def post_count(self, *paths: str) -> int:
        wanted = set(paths)
        return sum(1 for call in self.calls if call.get("method") == "POST" and call.get("path") in wanted)

    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for call in self.calls:
            key = f"{call.get('method')} {call.get('path')}"
            counts[key] = counts.get(key, 0) + 1
        return {"count": len(self.calls), "by_endpoint": dict(sorted(counts.items()))}


class RetryingOpener:
    def __init__(self, delegate=None, *, attempts: int = 3, delay_seconds: float = 0.75) -> None:
        self.delegate = delegate or urllib.request.urlopen
        self.attempts = attempts
        self.delay_seconds = delay_seconds

    def __call__(self, request, timeout):  # noqa: ANN001
        method = request.get_method() if hasattr(request, "get_method") else str(getattr(request, "method", "GET") or "GET")
        max_attempts = self.attempts if method in {"GET", "DELETE"} else 1
        last_exc: Exception | None = None
        for attempt in range(max_attempts):
            try:
                return self.delegate(request, timeout=timeout)
            except (OSError, TimeoutError, urllib.error.URLError) as exc:
                last_exc = exc
                if attempt + 1 >= max_attempts:
                    break
                time.sleep(self.delay_seconds * (attempt + 1))
        if last_exc is not None:
            raise last_exc
        return self.delegate(request, timeout=timeout)


class TestnetDrill:
    __test__ = False

    def __init__(
        self,
        *,
        output_root: Path | None = None,
        runtime_root: Path | None = None,
        opener=None,
        run_id: str | None = None,
    ) -> None:
        config = load_pipeline_config()
        self.config = config
        self.output_root = output_root or ROOT / str(config.get("output_root", "outputs"))
        self.run_id = run_id or _run_id()
        self.runtime_root = runtime_root or self.output_root / "testnet_drill_runtime" / self.run_id
        self.evidence_dir = self.output_root / "testnet_drill"
        self.opener = opener if opener is not None else RetryingOpener()

    def run(
        self,
        run_date: str,
        *,
        execute_testnet: bool = False,
        quantity: float = 0.002,
        scenarios: list[str] | None = None,
    ) -> dict:
        selected = scenarios or list(DEFAULT_SCENARIOS)
        started = _utcnow()
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        scenario_results: list[dict] = []
        connectivity = self._connectivity(run_date, quantity=quantity)
        scenario_results.append(connectivity)
        if execute_testnet and connectivity["status"] != "pass":
            selected = []

        for name in selected:
            if not execute_testnet and name != "always_on_deadman":
                scenario_results.append(
                    _scenario(
                        name,
                        "skip",
                        "validation-only mode; real Binance USDM testnet order was not created",
                        {"requires_execute_testnet": True},
                    )
                )
                continue
            scenario_results.append(self._run_named_scenario(name, run_date, quantity=quantity, execute_testnet=execute_testnet))

        payload = {
            "schema_version": "binance-usdm-testnet-drill-v1",
            "run_id": self.run_id,
            "run_date": run_date,
            "started_at": started,
            "finished_at": _utcnow(),
            "status": self._rollup(scenario_results),
            "mode": "execute_testnet" if execute_testnet else "validation_only",
            "provider": "binance_usdm",
            "base_url": TESTNET_BASE_URL,
            "symbol": TESTNET_SYMBOL,
            "quantity": quantity,
            "runtime_root": str(self.runtime_root),
            "safety": {
                "mainnet_allowed": False,
                "allowed_base_url": TESTNET_BASE_URL,
                "execution_mode_defaults_unchanged": str(self.config.get("execution_mode", "")).lower() == "paper",
                "live_trading_enabled_default": bool(self.config.get("live_trading_enabled", False)),
                "broker_dry_run_default": bool((self.config.get("broker", {}) or {}).get("dry_run", True)),
            },
            "scenarios": scenario_results,
            "notes": [
                "All exchange actions are locked to Binance USDM TESTNET demo-fapi endpoint.",
                "Scenario runtime roots are isolated so HALT and daily-count artifacts do not mutate production outputs.",
            ],
        }
        self._write_payload(payload)
        return payload

    def _run_named_scenario(self, name: str, run_date: str, *, quantity: float, execute_testnet: bool) -> dict:
        try:
            if name == "green_loop":
                return self._green_loop(run_date, quantity=quantity)
            if name == "crash_recovery":
                return self._crash_recovery(run_date, quantity=quantity)
            if name == "kill_switch":
                return self._kill_switch(run_date, quantity=quantity)
            if name == "always_on_deadman":
                return self._always_on_deadman(run_date)
            if name == "money_guardrails":
                return self._money_guardrails(run_date, quantity=quantity)
            if name == "naked_window":
                return self._naked_window(run_date, quantity=quantity)
            return _scenario(name, "fail", f"unknown scenario: {name}", {})
        except Exception as exc:  # noqa: BLE001 - scenario artifact must capture failures.
            evidence = {"error_type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc(limit=8)}
            return _scenario(name, "fail", f"{name} failed: {type(exc).__name__}: {exc}", evidence)

    def _connectivity(self, run_date: str, *, quantity: float) -> dict:
        root = self._scenario_root("connectivity")
        try:
            canary = BinanceUsdmTestnetCanary(root, opener=self.opener)
            result = canary.run(run_date, execute_testnet=False, quantity=quantity)
            status = "pass" if result.get("status") == "ready_for_testnet_execution" else "fail"
            return _scenario(
                "connectivity",
                status,
                "testnet endpoint, credentials, and ticker are reachable in validation mode" if status == "pass" else "testnet validation failed",
                _compact_canary(result),
            )
        except Exception as exc:  # noqa: BLE001
            return _scenario(
                "connectivity",
                "fail",
                f"testnet connectivity failed: {type(exc).__name__}: {exc}",
                {"error_type": type(exc).__name__, "message": str(exc)},
            )

    def _green_loop(self, run_date: str, *, quantity: float) -> dict:
        root = self._scenario_root("green_loop")
        canary = BinanceUsdmTestnetCanary(root, opener=self.opener)
        result = canary.run(run_date, execute_testnet=True, quantity=quantity)
        checks = {
            "status_pass": result.get("status") == "pass",
            "network_order_created": bool(result.get("network_order_created")),
            "network_close_created": bool(result.get("network_close_created")),
            "reconciliation_open": (result.get("reconciliation_open") or {}).get("confirmation_status") == "confirmed_open",
            "reconciliation_flat": ((result.get("close_report") or {}).get("reconciliation_after") or {}).get("confirmation_status") == "confirmed_flat",
            "server_side_protective": self._check_status(result, "server_side_protective_orders") == "pass",
            "lifecycle_reconciled": (result.get("lifecycle") or {}).get("state") == "reconciled",
        }
        qty = self._green_loop_qty(result)
        checks["qty_consistent"] = qty["consistent"]
        status = "pass" if all(checks.values()) else "fail"
        return _scenario(
            "green_loop",
            status,
            "real testnet entry, server-side protection, close, and flat reconciliation completed" if status == "pass" else "green loop did not satisfy all venue-state assertions",
            {
                "checks": checks,
                "quantity_reconciliation": qty,
                "canary": _compact_canary(result),
                "accounting_open": (result.get("reconciliation_open") or {}).get("exchange_accounting", {}),
                "accounting_final": (((result.get("close_report") or {}).get("reconciliation_after") or {}).get("exchange_accounting", {})),
            },
        )

    def _crash_recovery(self, run_date: str, *, quantity: float) -> dict:
        root = self._scenario_root("crash_recovery")
        cmd = [
            sys.executable,
            "-m",
            "pipelines.testnet_drill",
            "--date",
            run_date,
            "--quantity",
            str(quantity),
            "--internal-open-and-die",
            str(root),
        ]
        child = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, check=False)
        marker = _latest(load_json(root / "testnet_drill_markers" / "crash_child.json"))
        adapter = self._adapter(root)
        reconciliation = LiveBrokerReconciliation(root, adapter.broker_config, opener=self.opener).run(run_date)
        order_id = str(marker.get("order_id") or "")
        request_rows = load_json(root / "testnet_order_requests" / f"{run_date}.json")
        entry_clients = sorted(
            {
                str(((row.get("request") or {}).get("entry") or {}).get("newClientOrderId") or "")
                for row in request_rows
                if isinstance(row, dict) and ((row.get("request") or {}).get("entry") or {}).get("newClientOrderId")
            }
        )
        close_report = adapter.close_testnet_position(run_date, order_id=order_id, confirm_close_testnet_position=True, exit_reason="testnet_drill_crash_recovery_cleanup")
        final = close_report.get("reconciliation_after", {})
        checks = {
            "child_exited_abruptly": child.returncode == 77,
            "child_created_one_order": bool(marker.get("network_order_created")),
            "restarted_reconciliation_open": reconciliation.get("confirmation_status") == "confirmed_open",
            "protective_resting_or_recovered": all(item.get("covered") for item in reconciliation.get("position_protection", []) or []),
            "no_duplicate_entry_intent": len(entry_clients) == 1,
            "cleanup_confirmed_flat": final.get("confirmation_status") == "confirmed_flat",
        }
        status = "pass" if all(checks.values()) else "fail"
        return _scenario(
            "crash_recovery",
            status,
            "subprocess died while holding a testnet position; restarted process rebuilt venue state without duplicate entry" if status == "pass" else "crash recovery drill failed one or more assertions",
            {
                "checks": checks,
                "child": {"returncode": child.returncode, "stderr_tail": child.stderr[-1000:]},
                "marker": marker,
                "entry_client_order_ids": entry_clients,
                "reconciliation_after_restart": _compact_reconciliation(reconciliation),
                "cleanup": _compact_close(close_report),
                "simulation_note": "The child process exits with os._exit(77) after the position is open and before normal cleanup.",
            },
        )

    def _kill_switch(self, run_date: str, *, quantity: float) -> dict:
        root = self._scenario_root("kill_switch")
        open_result = self._open_testnet_position(root, run_date, quantity=quantity, marker="kill_switch")
        kill = BinanceUsdmTestnetKillSwitch(root, opener=self.opener)
        kill_report = kill.run(run_date, confirm_testnet_kill=True, reason="testnet_drill_kill_switch")
        restarted_halt = BinanceUsdmTestnetKillSwitch(root, opener=self.opener).halt_store.current()
        blocked_after_halt = self._attempt_order_expecting_block(root, run_date, quantity=quantity, expected="operator HALT is active")
        cleared = kill.clear_halt(run_date, confirm_clear=True, reason="testnet_drill_clear_halt")
        post_clear_guardrail = LiveMoneyGuardrails(root, broker_config={"request_dir": "testnet_order_requests"}).evaluate_order(
            run_date,
            ticket=self._ticket(run_date, float(open_result.get("price") or 0), "kill_switch_post_clear"),
            symbol=TESTNET_SYMBOL,
            side="BUY",
            requested_price=float(open_result.get("price") or 0),
            quantity=quantity,
            source="binance_usdm:testnet",
            reconciliation=(kill_report.get("reconciliation_after") or {}),
        )
        checks = {
            "position_opened": open_result.get("status") == "filled",
            "kill_status_confirmed": kill_report.get("status") == "halted_flat_confirmed",
            "confirmed_flat": bool(kill_report.get("confirmed_flat")),
            "open_orders_cleared": not (kill_report.get("reconciliation_after") or {}).get("exchange_open_orders"),
            "halt_persisted_after_restart": restarted_halt.get("active") is True,
            "new_order_blocked_during_halt": blocked_after_halt.get("blocked") is True,
            "halt_cleared": (cleared.get("halt") or {}).get("active") is False,
            "post_clear_not_operator_halt": post_clear_guardrail.get("status") != "BLOCKED_OPERATOR_HALT",
        }
        status = "pass" if all(checks.values()) else "fail"
        return _scenario(
            "kill_switch",
            status,
            "testnet kill switch flattened, cancelled orders, persisted HALT, blocked new order, then cleared HALT" if status == "pass" else "kill switch drill failed one or more assertions",
            {
                "checks": checks,
                "open": open_result,
                "kill": _compact_kill(kill_report),
                "blocked_after_halt": blocked_after_halt,
                "cleared": cleared,
                "post_clear_guardrail_status": post_clear_guardrail.get("status"),
            },
        )

    def _always_on_deadman(self, run_date: str) -> dict:
        root = self._scenario_root("always_on_deadman")
        market_db = root / "market.sqlite"
        self._seed_market_db(market_db)
        stale = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(microsecond=0).isoformat()
        write_json(root / "runner_status" / "current.json", [{"state": "running", "updated_at": stale, "interval_seconds": 300}])
        write_json(root / "strategies" / "summary_current.json", [{"run_date": run_date, "generated_at": _utcnow(), "strategies": []}])
        write_json(root / "live_reconciliation" / "current.json", [{"run_date": run_date, "checked_at": stale, "confirmation_status": "confirmed_flat", "exchange_positions": []}])
        service = ExternalDeadmanPing(root, market_db, url="https://hc-ping.example/testnet-drill", opener=_NoNetworkOpener(), timeout_seconds=1)
        result = service.run(run_date, dry_run=True)
        always_on = result.get("always_on", {})
        ping = result.get("ping", {})
        checks = {
            "always_on_blocked": str(always_on.get("status") or "").startswith("BLOCKED_"),
            "dry_run_fail_branch": ping.get("status") == "dry_run_fail",
            "fail_url_selected": ping.get("target_kind") == "fail",
            "critical_when_reconciliation_stale": result.get("severity") == "critical",
            "position_unknown": (result.get("exposure") or {}).get("position_unknown") is True,
        }
        status = "pass" if all(checks.values()) else "fail"
        return _scenario(
            "always_on_deadman",
            status,
            "stale critical heartbeat routes the external dead-man ping to /fail and treats stale reconciliation as possible exposure" if status == "pass" else "dead-man drill failed one or more assertions",
            {"checks": checks, "deadman": result},
        )

    def _money_guardrails(self, run_date: str, *, quantity: float) -> dict:
        root = self._scenario_root("money_guardrails")
        adapter = self._adapter(root, testnet_config={"request_dir": "testnet_order_requests", "max_order_quantity": 0.01, "require_flat_before_entry": True})
        price = self._ticker_price(adapter)
        reconciliation = LiveBrokerReconciliation(root, adapter.broker_config, opener=self.opener).run(run_date)
        subcases = [
            self._guardrail_adapter_block(root, run_date, "single_notional", price=price, quantity=0.01, seed_daily_count=False),
            self._guardrail_adapter_block(root, run_date, "daily_trade_count", price=price, quantity=quantity, seed_daily_count=True),
            self._guardrail_daily_loss_injection(root, run_date, price=price, quantity=quantity),
        ]
        checks = {
            "single_notional_blocked_before_post": self._subcase_ok(subcases, "single_notional", "BLOCKED_SINGLE_ORDER_NOTIONAL_LIMIT"),
            "daily_trade_count_blocked_before_post": self._subcase_ok(subcases, "daily_trade_count", "BLOCKED_DAILY_TRADE_LIMIT"),
            "daily_loss_simulated_hard_block": self._subcase_ok(subcases, "daily_loss", "BLOCKED_DAILY_LOSS_LIMIT"),
            "canonical_audit_reason_present": all(bool(item.get("cycle_audit", {}).get("why_not_executed")) for item in subcases),
        }
        status = "pass" if all(checks.values()) else "fail"
        return _scenario(
            "money_guardrails",
            status,
            "testnet adapter refused guardrail-violating entries before any order POST; daily loss uses injected guardrail state because exchange loss cannot be deterministically forced" if status == "pass" else "money guardrail drill failed one or more assertions",
            {
                "checks": checks,
                "initial_reconciliation": _compact_reconciliation(reconciliation),
                "subcases": subcases,
                "simulation_gap": "The daily-loss subcase injects a fresh same-day reconciliation snapshot with loss above limit. Binance testnet does not provide a deterministic way to create an exact daily loss threshold without market risk.",
            },
        )

    def _naked_window(self, run_date: str, *, quantity: float) -> dict:
        root = self._scenario_root("naked_window")
        adapter = self._adapter(root)
        price = self._ticker_price(adapter)
        ticket = self._ticket(run_date, price, "naked_window")
        order_id = self._stable_order_id(run_date, "naked-window")
        payload = {
            "symbol": TESTNET_SYMBOL,
            "side": "BUY",
            "type": "MARKET",
            "quantity": adapter._format_decimal(quantity),
            "newOrderRespType": "RESULT",
            "newClientOrderId": order_id[:36],
        }
        guard = adapter._testnet_static_guard(adapter.preflight())
        if not guard.get("ready"):
            raise RuntimeError(guard.get("block_reason") or "testnet static guard failed")
        response = adapter._post_binance_order(payload)
        fill_price = adapter._binance_fill_price(response) or price
        filled_qty = adapter._binance_executed_quantity(response) or quantity
        store = OrderLifecycleStore(root)
        lifecycle, _ = store.write_intent(
            run_date,
            order_id=order_id,
            ticket_id=str(ticket["ticket_id"]),
            idempotency_key=order_id[:36],
            requested_quantity=filled_qty,
            requested_price=fill_price,
            source="testnet_drill:naked_window",
            metadata={"symbol": TESTNET_SYMBOL, "provider": "binance_usdm", "mode": "testnet", "ticket": ticket},
        )
        for state in ("submitting", "accepted", "filled"):
            lifecycle = store.transition(
                run_date,
                order_id,
                state,
                reason=f"testnet_drill_raw_entry_{state}",
                filled_quantity=filled_qty if state == "filled" else None,
            )
        PaperExecutor(root).record_external_fill(
            run_date,
            ticket,
            fill_price=fill_price,
            quantity=filled_qty,
            order_id=order_id,
            exchange_managed=True,
            protection_verified=False,
        )
        mirrored_trades_before_cleanup = load_json(root / "paper_trades" / "current.json")
        naked = LiveBrokerReconciliation(root, adapter.broker_config, opener=self.opener).run(run_date)
        recovery = adapter.recover_missing_protective_orders(run_date, lifecycle, self._position_for_symbol(naked, TESTNET_SYMBOL), source="testnet_drill_naked_window")
        after_recovery = LiveBrokerReconciliation(root, adapter.broker_config, opener=self.opener).run(run_date)
        close_report = adapter.close_testnet_position(run_date, order_id=order_id, confirm_close_testnet_position=True, exit_reason="testnet_drill_naked_window_cleanup")
        checks = {
            "raw_entry_created": bool(response.get("orderId") or response.get("clientOrderId")),
            "mirror_written": bool(mirrored_trades_before_cleanup),
            "reconcile_identified_naked": naked.get("suspected_naked_position") is True and naked.get("system_state") == "BLOCKED_NAKED_POSITION_SUSPECTED",
            "not_marked_healthy": not (naked.get("confirmation_status") == "confirmed_open" and naked.get("reconciled") is True),
            "recovery_action_attempted": recovery.get("status") in {"recovered", "blocked"},
            "recovery_attached_or_explicit_block": recovery.get("status") == "recovered" or bool(recovery.get("blocker") or recovery.get("block_reason")),
            "cleanup_confirmed_flat": (close_report.get("reconciliation_after") or {}).get("confirmation_status") == "confirmed_flat",
        }
        status = "pass" if all(checks.values()) else "fail"
        return _scenario(
            "naked_window",
            status,
            "raw filled testnet position with local mirror but no protective order was classified as naked and then recovered or explicitly blocked" if status == "pass" else "naked-window drill failed one or more assertions",
            {
                "checks": checks,
                "raw_entry_response": adapter._safe_order_payload(response),
                "naked_reconciliation": _compact_reconciliation(naked),
                "recovery": recovery,
                "after_recovery": _compact_reconciliation(after_recovery),
                "cleanup": _compact_close(close_report),
                "simulation_note": "This intentionally bypasses normal submit_order to model a crash after entry fill and before protective POST.",
            },
        )

    def _guardrail_adapter_block(
        self,
        root: Path,
        run_date: str,
        name: str,
        *,
        price: float,
        quantity: float,
        seed_daily_count: bool,
    ) -> dict:
        subroot = root / name
        if seed_daily_count:
            write_json(
                subroot / "testnet_order_requests" / f"{run_date}.json",
                [
                    {"request": {"entry": {"newClientOrderId": f"seed_{name}_1"}}, "receipt": {"status": "filled"}},
                    {"request": {"entry": {"newClientOrderId": f"seed_{name}_2"}}, "receipt": {"status": "filled"}},
                ],
            )
        recording = RecordingOpener(self.opener)
        adapter = self._adapter(
            subroot,
            opener=recording,
            testnet_config={"request_dir": "testnet_order_requests", "max_order_quantity": max(quantity, 0.01), "require_flat_before_entry": True},
        )
        ticket = self._ticket(run_date, price, f"money_guardrails_{name}")
        blocked = {}
        try:
            adapter.submit_order(BrokerOrderRequest(run_date, ticket, latest_price=price, actual_size=quantity))
            blocked = {"blocked": False, "error": ""}
        except RuntimeError as exc:
            blocked = {"blocked": True, "error": str(exc)}
        guardrail = _latest(load_json(subroot / "live_money_guardrails" / "current.json"))
        audit = self._audit_guardrail_block(subroot, run_date, guardrail, ticket)
        return {
            "name": name,
            "mode": "real_testnet_adapter_pre_post",
            "blocked": blocked,
            "guardrail_status": guardrail.get("status", ""),
            "primary_blocker": guardrail.get("primary_blocker", {}),
            "posts": recording.post_count("/fapi/v1/order", "/fapi/v1/algoOrder"),
            "opener": recording.summary(),
            "cycle_audit": audit,
        }

    def _guardrail_daily_loss_injection(self, root: Path, run_date: str, *, price: float, quantity: float) -> dict:
        subroot = root / "daily_loss"
        ticket = self._ticket(run_date, price, "money_guardrails_daily_loss")
        start_ms, end_ms = LiveMoneyGuardrails(subroot, broker_config={"request_dir": "testnet_order_requests"})._utc_day_window_ms(run_date)
        reconciliation = {
            "run_date": run_date,
            "checked_at": _utcnow(),
            "error": "",
            "confirmation_status": "confirmed_flat",
            "exchange_positions": [],
            "exchange_balance": {"asset": "USDT", "balance": 1000.0, "available": 1000.0, "balance_present": True},
            "account_observation": {
                "account_observed": True,
                "balance_present": True,
                "history_requested": True,
                "fills_observed": True,
                "income_observed": True,
                "fills_count": 1,
                "income_count": 0,
                "symbol": TESTNET_SYMBOL,
                "reason": "testnet drill injected daily-loss threshold state",
            },
            "exchange_accounting": {
                "net_realized_pnl_estimate": -20.0,
                "utc_trading_day": {"run_date": run_date, "start_time_ms": start_ms, "end_time_ms": end_ms},
            },
        }
        write_json(subroot / "live_reconciliation" / "current.json", [reconciliation])
        guardrail = LiveMoneyGuardrails(subroot, broker_config={"request_dir": "testnet_order_requests"}).evaluate_order(
            run_date,
            ticket=ticket,
            symbol=TESTNET_SYMBOL,
            side="BUY",
            requested_price=price,
            quantity=quantity,
            source="binance_usdm:testnet_drill_injected_daily_loss",
            reconciliation=reconciliation,
        )
        audit = self._audit_guardrail_block(subroot, run_date, guardrail, ticket)
        return {
            "name": "daily_loss",
            "mode": "simulated_guardrail_injection",
            "blocked": {"blocked": guardrail.get("allows_new_order") is False, "error": guardrail.get("primary_blocker", {}).get("message", "")},
            "guardrail_status": guardrail.get("status", ""),
            "primary_blocker": guardrail.get("primary_blocker", {}),
            "posts": 0,
            "cycle_audit": audit,
        }

    def _audit_guardrail_block(self, root: Path, run_date: str, guardrail: dict, ticket: dict) -> dict:
        self._seed_ready_system_vitals(root, run_date)
        now = _utcnow()
        write_json(root / "data_source_preflight" / "current.json", [{"status": "pass", "ready_for_paper": True, "ready_for_live": True, "latest_record_is_fresh": True, "latest_timestamp": now}])
        write_json(root / "signals" / f"{run_date}.json", [{"signal_id": ticket.get("signal_id", ""), "status": "go", "direction": "long", "strength": 1}])
        write_json(root / "trade_tickets" / f"{run_date}.json", [ticket])
        write_json(root / "journal_pending" / f"{run_date}.json", [{"ticket_id": ticket.get("ticket_id", ""), "signal_id": ticket.get("signal_id", "")}])
        write_json(root / "risk_monitor" / "current.json", [{"status": "pass", "checks": [], "summary": {"block_reasons": [], "warning_reasons": []}}])
        write_json(root / "paper_execution_blocks" / f"{run_date}.json", [])
        write_json(root / "live_money_guardrails" / "current.json", [guardrail])
        sink = JsonCycleAuditSink(root, base_output_root=root)
        ctx = sink.begin(run_date=run_date, strategy_id="gold_1m_chan", timeframe="1m")
        record = sink.finalize(ctx, result={"status": "blocked_by_guardrail"})
        why_not = record.get("execution", {}).get("why_not_executed", [])
        return {
            "status": record.get("status", ""),
            "trade_permission": record.get("system_state", {}).get("trade_permission", {}),
            "why_not_executed": why_not,
            "artifact": str(root / "cycle_audit" / f"{run_date}.json"),
        }

    def _open_testnet_position(self, root: Path, run_date: str, *, quantity: float, marker: str) -> dict:
        adapter = self._adapter(root)
        price = self._ticker_price(adapter)
        ticket = self._ticket(run_date, price, marker)
        order = adapter.submit_order(BrokerOrderRequest(run_date, ticket, latest_price=price, actual_size=quantity))
        receipt = order.to_dict()
        return {"status": receipt.get("status"), "order_id": receipt.get("order_id", ""), "ticket_id": receipt.get("ticket_id", ""), "price": price, "receipt": receipt}

    def _attempt_order_expecting_block(self, root: Path, run_date: str, *, quantity: float, expected: str) -> dict:
        adapter = self._adapter(root)
        price = self._ticker_price(adapter)
        ticket = self._ticket(run_date, price, "halt_block_probe")
        recording = RecordingOpener(self.opener)
        adapter.opener = recording
        try:
            adapter.submit_order(BrokerOrderRequest(run_date, ticket, latest_price=price, actual_size=quantity))
            return {"blocked": False, "error": "", "posts": recording.post_count("/fapi/v1/order", "/fapi/v1/algoOrder")}
        except RuntimeError as exc:
            return {
                "blocked": expected in str(exc),
                "error": str(exc),
                "posts": recording.post_count("/fapi/v1/order", "/fapi/v1/algoOrder"),
            }

    def _adapter(self, root: Path, *, opener=None, testnet_config: dict | None = None) -> BinanceUsdmTestnetBrokerAdapter:
        config = load_pipeline_config()
        profiles = config.get("broker_profiles", {}) or {}
        profile = profiles.get("binance_usdm_testnet") or {}
        broker = config.get("broker", {}) or {}
        merged_config = {**broker, **profile, "base_url": TESTNET_BASE_URL, "environment": "testnet", "dry_run": False}
        merged_testnet = {**(config.get("testnet_trading", {}) or {}), **(testnet_config or {})}
        return BinanceUsdmTestnetBrokerAdapter(root, merged_config, merged_testnet, opener=opener or self.opener)

    def _ticker_price(self, adapter: BinanceUsdmTestnetBrokerAdapter) -> float:
        params = urllib.parse.urlencode({"symbol": TESTNET_SYMBOL})
        request = urllib.request.Request(f"{adapter._binance_base_url()}/fapi/v1/ticker/price?{params}", headers={"User-Agent": "TradingOrchestrator/1.0"})
        with self.opener(request, timeout=int(adapter.broker_config.get("timeout_seconds", 10))) as response:
            payload = json.loads(response.read().decode("utf-8"))
        price = float(payload.get("price", 0) or 0)
        if price <= 0:
            raise RuntimeError("Binance USDM testnet ticker returned non-positive price")
        return price

    def _ticket(self, run_date: str, price: float, marker: str) -> dict:
        safe_price = float(price or 4000.0)
        nonce = self._stable_order_id(run_date, marker)[:12]
        return {
            "ticket_id": f"testnet_drill_{marker}_{run_date}_{nonce}",
            "signal_id": f"testnet_drill_signal_{marker}_{run_date}_{nonce}",
            "asset": "GOLD",
            "action": "prepare_buy",
            "entry_zone": f"{safe_price:.2f}-{safe_price:.2f}",
            "stop_loss": round(safe_price * 0.997, 2),
            "targets": [round(safe_price * 1.003, 2)],
            "position_size_pct": 0.01,
            "max_loss_pct": 0.01,
            "order_type": "market",
            "time_in_force": "day",
        }

    def _stable_order_id(self, run_date: str, marker: str) -> str:
        digest = hashlib.sha256(f"{self.run_id}:{run_date}:{marker}:{TESTNET_SYMBOL}".encode("utf-8")).hexdigest()[:18]
        return f"drill_{marker.replace('_', '')}_{digest}"[:36]

    def _scenario_root(self, name: str) -> Path:
        return self.runtime_root / name

    def _write_payload(self, payload: dict) -> None:
        write_json(self.evidence_dir / "current.json", [payload])
        write_json(self.evidence_dir / f"{self.run_id}.json", [payload])

    def _rollup(self, scenarios: list[dict]) -> str:
        states = {item.get("status") for item in scenarios}
        if "fail" in states:
            return "fail"
        if "skip" in states:
            return "ready_for_testnet_execution"
        if "simulated_pass" in states:
            return "pass_with_simulated_cases"
        return "pass"

    def _green_loop_qty(self, result: dict) -> dict:
        receipt = result.get("order_receipt") or {}
        open_positions = (result.get("reconciliation_open") or {}).get("exchange_positions") or []
        close = (result.get("close_report") or {}).get("close_response") or {}
        entry_qty = _float(receipt.get("quantity"))
        position_qty = abs(_float(open_positions[0].get("position_amt"))) if open_positions and isinstance(open_positions[0], dict) else 0.0
        close_qty = _float(close.get("executedQty") or close.get("origQty"))
        return {
            "entry_filled_qty": entry_qty,
            "position_qty": position_qty,
            "close_qty": close_qty,
            "consistent": _near(entry_qty, position_qty) and _near(entry_qty, close_qty),
        }

    def _check_status(self, canary_result: dict, name: str) -> str:
        for item in canary_result.get("checks", []) or []:
            if isinstance(item, dict) and item.get("name") == name:
                return str(item.get("status") or "")
        return ""

    def _position_for_symbol(self, reconciliation: dict, symbol: str) -> dict:
        for item in reconciliation.get("exchange_positions", []) or []:
            if isinstance(item, dict) and str(item.get("symbol") or "").upper() == symbol:
                return item
        return {}

    def _subcase_ok(self, subcases: list[dict], name: str, status: str) -> bool:
        item = next((row for row in subcases if row.get("name") == name), {})
        return item.get("guardrail_status") == status and item.get("posts") == 0 and bool(item.get("blocked", {}).get("blocked"))

    def _seed_market_db(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as con:
            con.execute("CREATE TABLE IF NOT EXISTS bars (symbol TEXT, timestamp TEXT)")
            con.execute("DELETE FROM bars")
            con.execute("INSERT INTO bars(symbol, timestamp) VALUES (?, ?)", ("GOLD", _utcnow()))

    def _seed_ready_system_vitals(self, root: Path, run_date: str) -> None:
        vitals = [
            {"name": "data_feed", "status": "up", "message": "testnet drill seeded fresh data", "detail": {}},
            {"name": "strategy_evaluation", "status": "up", "message": "testnet drill seeded strategy evaluation", "detail": {}},
            {"name": "runner_liveness", "status": "up", "message": "testnet drill seeded runner liveness", "detail": {}},
            {"name": "execution_blocker", "status": "up", "message": "no hard execution blocker detected", "detail": {}},
            {"name": "tp_sl_coverage", "status": "up", "message": "open positions have TP/SL protection", "detail": {}},
        ]
        write_json(
            root / "system_vitals" / "current.json",
            [
                {
                    "run_date": run_date,
                    "checked_at": _utcnow(),
                    "overall": "alive",
                    "alive": True,
                    "vitals": vitals,
                    "always_on": {"status": "READY", "blocks_new_orders": False, "degraded": False, "jobs": []},
                }
            ],
        )


class _NoNetworkOpener:
    def __call__(self, request, timeout):  # noqa: ANN001
        raise AssertionError("dry-run deadman drill must not call the network")


def _internal_open_and_die(root: Path, run_date: str, quantity: float) -> None:
    drill = TestnetDrill(runtime_root=root.parent, run_id=root.parent.name)
    result = drill._open_testnet_position(root, run_date, quantity=quantity, marker="crash_recovery")
    write_json(
        root / "testnet_drill_markers" / "crash_child.json",
        [
            {
                **result,
                "network_order_created": result.get("status") == "filled",
                "abrupt_exit_at": _utcnow(),
                "note": "process intentionally exits via os._exit before close cleanup",
            }
        ],
    )
    os._exit(77)


def _scenario(name: str, status: str, summary: str, evidence: dict) -> dict:
    return {
        "name": name,
        "status": status,
        "summary": summary,
        "checked_at": _utcnow(),
        "evidence": evidence,
    }


def _compact_canary(result: dict) -> dict:
    return {
        "status": result.get("status", ""),
        "mode": result.get("mode", ""),
        "base_url": result.get("base_url", ""),
        "symbol": result.get("symbol", ""),
        "network_order_created": bool(result.get("network_order_created")),
        "network_close_created": bool(result.get("network_close_created")),
        "checks": [
            {"name": item.get("name", ""), "status": item.get("status", ""), "summary": item.get("summary", "")}
            for item in result.get("checks", []) or []
            if isinstance(item, dict)
        ],
        "order_receipt": {
            "order_id": (result.get("order_receipt") or {}).get("order_id", ""),
            "status": (result.get("order_receipt") or {}).get("status", ""),
            "quantity": (result.get("order_receipt") or {}).get("quantity"),
            "fill_price": (result.get("order_receipt") or {}).get("fill_price"),
        },
        "reconciliation_open": _compact_reconciliation(result.get("reconciliation_open") or {}),
        "close_report": _compact_close(result.get("close_report") or {}),
        "lifecycle_state": (result.get("lifecycle") or {}).get("state", ""),
    }


def _compact_reconciliation(report: dict) -> dict:
    return {
        "confirmation_status": report.get("confirmation_status", ""),
        "system_state": report.get("system_state", ""),
        "reason_code": report.get("reason_code", ""),
        "reconciled": report.get("reconciled"),
        "drift_count": report.get("drift_count"),
        "suspected_naked_position": bool(report.get("suspected_naked_position")),
        "exchange_position_count": len(report.get("exchange_positions", []) or []),
        "exchange_positions": report.get("exchange_positions", []),
        "position_protection": report.get("position_protection", []),
        "exchange_open_orders": report.get("exchange_open_orders", []),
        "account_observation": report.get("account_observation", {}),
        "exchange_accounting": report.get("exchange_accounting", {}),
        "exchange_balance": report.get("exchange_balance", {}),
        "error": report.get("error", ""),
    }


def _compact_close(report: dict) -> dict:
    return {
        "status": report.get("status", ""),
        "network_order_created": bool(report.get("network_order_created")),
        "block_reason": report.get("block_reason", ""),
        "close_response": report.get("close_response", {}),
        "protective_cancel": report.get("protective_cancel", {}),
        "reconciliation_after": _compact_reconciliation(report.get("reconciliation_after", {}) or {}),
    }


def _compact_kill(report: dict) -> dict:
    return {
        "status": report.get("status", ""),
        "network_order_created": bool(report.get("network_order_created")),
        "confirmed_flat": bool(report.get("confirmed_flat")),
        "block_reason": report.get("block_reason", ""),
        "halt": report.get("halt", {}),
        "cancel_before": report.get("cancel_before", {}),
        "cancel_after": report.get("cancel_after", {}),
        "close_orders": report.get("close_orders", []),
        "reconciliation_after": _compact_reconciliation(report.get("reconciliation_after", {}) or {}),
    }


def _latest(rows: list[dict]) -> dict:
    return rows[-1] if rows and isinstance(rows[-1], dict) else {}


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _near(left: float, right: float, tolerance: float = 1e-8) -> bool:
    return abs(float(left or 0.0) - float(right or 0.0)) <= tolerance


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a repeatable Binance USDM TESTNET pre-mainnet drill.")
    parser.add_argument("--date", default=utc_run_date(), help="UTC trading date for drill artifacts.")
    parser.add_argument("--quantity", type=float, default=0.002, help="Minimal XAUUSDT testnet quantity.")
    parser.add_argument("--execute-testnet", action="store_true", help="Create real Binance USDM TESTNET orders. Mainnet remains forbidden.")
    parser.add_argument("--scenario", action="append", choices=DEFAULT_SCENARIOS, help="Run only this scenario. Can be provided multiple times.")
    parser.add_argument("--runtime-root", default="", help="Optional isolated runtime root.")
    parser.add_argument("--run-id", default="", help="Optional stable drill run id.")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--internal-open-and-die", default="", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.internal_open_and_die:
        _internal_open_and_die(Path(args.internal_open_and_die), args.date, args.quantity)
        return

    drill = TestnetDrill(
        runtime_root=Path(args.runtime_root) if args.runtime_root else None,
        run_id=args.run_id or None,
    )
    result = drill.run(args.date, execute_testnet=args.execute_testnet, quantity=args.quantity, scenarios=args.scenario)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    print(f"testnet_drill: {result['status']} run_id={result['run_id']} mode={result['mode']} symbol={result['symbol']}")
    print(f"artifact: {result['runtime_root']}")
    for item in result["scenarios"]:
        print(f"- {item['status']}: {item['name']} - {item['summary']}")


if __name__ == "__main__":
    main()
