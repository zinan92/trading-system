"""Multi-strategy runner — the infra that lets many strategies run in parallel,
each with a fully isolated paper account.

Isolation boundary: every per-strategy artifact (signals, tickets, trades,
positions, equity, performance, reconciliation) is written under
`outputs/strategies/<strategy_id>/` by passing that scoped `output_root` to the
existing (already output_root-parameterized) services. The market DB is the only
shared sink — all strategies read the same bars/quotes.

This is a NEW path that runs ALONGSIDE the legacy global cycle. It does not
touch the live gold_5m_v1 cycle, the dashboard, or health/alerting (those still
read the global root). Pointing the dashboard at per-strategy namespaces is
Phase 4 (the leaderboard).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from pipelines.daily import run_daily_pipeline
from services.config_loader import ROOT, load_pipeline_config, load_risk_rules
from services.data_source_preflight import DataSourcePreflight
from services.decision_trace import DecisionTrace
from services.journal_store import JournalStore, load_json, write_json
from services.broker_adapter import PaperBrokerAdapter
from services.broker_read_model import (
    broker_reconciliation_block_reason,
    broker_reconciliation_status,
    project_broker_read_model,
)
from services.order_lifecycle import OrderLifecycleStore
from services.pending_auto_resolver import resolve_pending_cycle, sweep_stale_pending
from services.pending_entry_guard import evaluate_limit_entry_status, load_entry_candles
from services.paper_equity_curve import PaperEquityCurve
from services.paper_performance import PaperPerformanceAnalyzer
from services.paper_reconciliation import PaperReconciliation
from services.broker_port import (
    BrokerCancelRequest,
    BrokerCapability,
    BrokerProtectiveRecoveryRequest,
    UnsupportedBrokerCapability,
    require_broker_capability,
)
from services.risk_monitor import RiskMonitor
from services.backend_maturity_audit import BackendMaturityAudit
from services.strategy_leaderboard import StrategyLeaderboard
from services.strategy_frequency_governance import StrategyFrequencyGovernance
from services.strategy_registry import StrategyRegistry
from services.strategy_guardrails import StrategyGuardrails
from services.strategy_daily_review import StrategyDailyReview
from services.trade_quality import DailyTradeSampler
from services.trade_ticket_notifier import TradeTicketNotifier
from services.strategy_book import StrategyBook
from services.edge_judgment import EdgeJudgment
from services.strategy_objective import StrategyObjective
from services.cycle_audit import JsonCycleAuditSink


class MultiStrategyRunner:
    def __init__(self, output_root: Path | None = None, registry: StrategyRegistry | None = None) -> None:
        config = load_pipeline_config()
        self.base_output_root = (
            Path(output_root)
            if output_root is not None
            else Path(os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(ROOT / config.get("output_root", "outputs"))))
        )
        self.registry = registry or StrategyRegistry()

    def strategy_root(self, strategy_id: str) -> Path:
        return self.base_output_root / "strategies" / strategy_id

    def _broker_adapter_for(self, strategy, scoped: Path):
        config = load_pipeline_config()
        active = self._active_demo_broker_config(strategy, config=config)
        if active is not None:
            broker_config, demo = active
            from services.broker_composition import BrokerBuildContext, build_demo_broker_execution_port

            return build_demo_broker_execution_port(
                BrokerBuildContext(
                    output_root=scoped,
                    execution_mode="live",
                    live_trading_enabled=True,
                    broker_config=broker_config,
                    auxiliary_config=demo,
                )
            )

        # A `live` strategy routes execution through the live broker (its own
        # scoped namespace); everyone else returns None -> the default paper path.
        # The live adapter's gates (live_trading_enabled / dry_run / activation)
        # still decide whether a real order is actually sent.
        if not getattr(strategy, "live", False):
            return None
        from services.broker_composition import (
            build_configured_live_broker_execution_port,
            resolve_broker_profile_config,
        )

        return build_configured_live_broker_execution_port(
            scoped,
            bool(config.get("live_trading_enabled", False)),
            resolve_broker_profile_config(config),
        )

    def _active_demo_broker_config(self, strategy, *, config: dict | None = None) -> tuple[dict, dict] | None:
        config = config or load_pipeline_config()
        from services.broker_composition import resolve_active_demo_broker_config

        return resolve_active_demo_broker_config(
            config,
            strategy_id=strategy.strategy_id,
        )

    def _execution_profile_for(self, strategy) -> dict:
        config = load_pipeline_config()
        scoped = self.strategy_root(strategy.strategy_id)
        adapter = self._broker_adapter_for(strategy, scoped)
        selection_status = "resolved"
        if adapter is None:
            demo = config.get("demo_trading", {}) if isinstance(config.get("demo_trading"), dict) else {}
            if demo.get("enabled") is True and str(demo.get("active_strategy_id") or "") == strategy.strategy_id:
                selection_status = "configured_profile_unresolved"
            adapter = PaperBrokerAdapter(scoped)
        profile = project_broker_read_model(
            adapter,
            strategy_id=strategy.strategy_id,
            profile=str(getattr(adapter, "broker_config", {}).get("profile") or ""),
            asset=str(getattr(strategy, "symbol", "") or ""),
        )
        return {
            **profile,
            "selection_status": selection_status,
            "note": "Execution diagnostics are projected from local Broker Port configuration; venue preflight remains command-side.",
        }

    def _demo_reconciliation_for(self, strategy, scoped: Path, run_date: str) -> dict | None:
        active = self._active_demo_broker_config(strategy)
        if active is None:
            return None
        broker_config, demo = active
        from services.broker_composition import (
            BrokerBuildContext,
            build_broker_reconciliation_port,
        )

        execution = self._broker_adapter_for(strategy, scoped)
        reconciliation = build_broker_reconciliation_port(
            BrokerBuildContext(
                output_root=scoped,
                execution_mode="live",
                live_trading_enabled=True,
                broker_config=broker_config,
                auxiliary_config=demo,
            ),
            execution_port=execution,
        )
        return reconciliation.run(run_date)

    def _demo_reconciliation_block_reason(self, report: dict) -> str:
        return broker_reconciliation_block_reason(report)

    def _reconciliation_status(self, report: dict | None) -> str:
        return broker_reconciliation_status(report)

    def run(self, run_date: str, paper_auto_approve: bool = False) -> dict:
        classification_audit = self.registry.classification_audit()
        analysis_plugin_audit = self.registry.analysis_plugin_audit()
        runnable = self.registry.enabled_for_runner()
        runnable_ids = {strategy.strategy_id for strategy in runnable}
        classification_by_id = {
            str(item.get("strategy_id") or ""): item
            for item in classification_audit.get("strategies", [])
        }
        plugin_by_id = {
            str(item.get("strategy_id") or ""): item
            for item in analysis_plugin_audit.get("strategies", [])
        }
        skipped = []
        for strategy in self.registry.enabled():
            if strategy.strategy_id in runnable_ids:
                continue
            classification = classification_by_id.get(strategy.strategy_id, {})
            plugin = plugin_by_id.get(strategy.strategy_id, {})
            reasons = []
            if classification.get("status") != "pass":
                reasons.append("classification_incomplete")
            if plugin.get("status") != "pass":
                reasons.append("analysis_plugin_unavailable")
            skipped.append(
                {
                    "strategy_id": strategy.strategy_id,
                    "status": "skipped",
                    "reason": "+".join(reasons) or "strategy_not_runnable",
                    "classification_audit": classification,
                    "analysis_plugin_audit": plugin,
                }
            )
        results = [self._run_one(run_date, strategy, paper_auto_approve) for strategy in runnable] + skipped
        leaderboard = StrategyLeaderboard(self.base_output_root).build(run_date)
        config = load_pipeline_config()
        active_strategy_id = str((config.get("demo_trading", {}) or {}).get("active_strategy_id", ""))
        samples = DailyTradeSampler(self.base_output_root).build(run_date, active_strategy_id=active_strategy_id)
        frequency = StrategyFrequencyGovernance(self.base_output_root).build(
            run_date,
            daily_samples=samples,
            leaderboard=leaderboard,
        )
        strategy_daily_review = StrategyDailyReview(self.base_output_root).build(
            run_date,
            leaderboard=leaderboard,
            frequency=frequency,
        )
        backend_maturity = self._refresh_backend_maturity(run_date)
        payload = {
            "run_date": run_date,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "strategy_count": len(results),
            "classification_audit": classification_audit,
            "analysis_plugin_audit": analysis_plugin_audit,
            "reconciled": sum(1 for r in results if r.get("reconciliation_status") == "pass"),
            "errors": sum(1 for r in results if r.get("status") == "error"),
            "strategies": results,
            "leaderboard": leaderboard.get("strategies", []),
            "daily_trade_samples": {
                "status": samples.get("status"),
                "summary": samples.get("summary", {}),
            },
            "strategy_frequency": {
                "status": frequency.get("status"),
                "summary": frequency.get("summary", {}),
                "next_actions": frequency.get("next_actions", []),
            },
            "strategy_daily_review": {
                "strategy_count": strategy_daily_review.get("strategy_count", 0),
                "status_counts": strategy_daily_review.get("status_counts", {}),
            },
            "backend_maturity": {
                "status": backend_maturity.get("status"),
                "summary": backend_maturity.get("summary", {}),
                "generated_at": backend_maturity.get("generated_at", ""),
            },
        }
        write_json(self.base_output_root / "strategies" / "summary_current.json", [payload])
        write_json(self.base_output_root / "strategies" / f"summary_{run_date}.json", [payload])
        return payload

    def _refresh_backend_maturity(self, run_date: str) -> dict:
        try:
            return BackendMaturityAudit(self.base_output_root).run(run_date)
        except Exception as exc:  # noqa: BLE001 - the trading run should surface, not die on an audit refresh.
            return {
                "status": "error",
                "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "summary": {"error": f"{type(exc).__name__}: {exc}"},
            }

    def _run_one(self, run_date: str, strategy, paper_auto_approve: bool) -> dict:
        scoped = self.strategy_root(strategy.strategy_id)
        audit_sink = JsonCycleAuditSink(scoped, base_output_root=self.base_output_root)
        audit_context: dict = {}
        cycle_audit_error = ""
        try:
            audit_context = audit_sink.begin(run_date=run_date, strategy_id=strategy.strategy_id, timeframe=strategy.timeframe)
        except Exception as exc:  # noqa: BLE001 - audit must surface but not change execution behavior.
            cycle_audit_error = f"{type(exc).__name__}: {exc}"
        try:
            execution_profile = self._execution_profile_for(strategy)
            run_daily_pipeline(run_date, strategy=strategy, output_root=scoped)
            # Per-strategy data-source readiness on the strategy's OWN timeframe
            # (chan → GOLD 1m). The paper-execution gate reads this scoped
            # artifact; without it, an auto-approved ticket can never fill. The
            # global cycle still writes its own GOLD 5m preflight independently.
            DataSourcePreflight(output_root=scoped, symbol=strategy.symbol, timeframe=strategy.timeframe).run(run_date)
            ticket_notification = TradeTicketNotifier(self.base_output_root).notify_namespace(run_date, strategy.strategy_id, scoped)
            demo_reconciliation = self._demo_reconciliation_for(strategy, scoped, run_date)
            order_recovery = self._recover_demo_order_intents(strategy, scoped, run_date, reconciliation=demo_reconciliation)
            if order_recovery.get("reconciliation_refresh_required"):
                demo_reconciliation = self._demo_reconciliation_for(strategy, scoped, run_date)
            pending = load_json(scoped / "journal_pending" / f"{run_date}.json")
            recovered_ticket_ids = list(order_recovery.get("recovered_ticket_ids", []) or [])
            executed_ticket = recovered_ticket_ids[0] if recovered_ticket_ids else None
            execution_error = ""
            auto_resolution: dict = {"executed": [], "rejected": [], "skipped": [], "errors": [], "decisions": []}
            if paper_auto_approve and pending:
                # Decide whether a NEW auto-execution is allowed this cycle; the shared
                # resolver then terminates EVERY pending ticket (execute the primary or
                # auto-reject with the reason) so none is stranded or silently dropped.
                block_reason = ""
                if recovered_ticket_ids:
                    block_reason = "recovered order intent this cycle; skipped new auto approval to avoid stacking exposure"
                elif order_recovery.get("blocks_new_orders"):
                    block_reason = str(order_recovery.get("block_reason") or "demo order recovery blocks new execution")
                elif demo_reconciliation and not demo_reconciliation.get("reconciled", False):
                    block_reason = self._demo_reconciliation_block_reason(demo_reconciliation)
                gate_allows = not block_reason
                broker_adapter = self._broker_adapter_for(strategy, scoped) if gate_allows else None
                auto_resolution = resolve_pending_cycle(
                    run_date,
                    pending,
                    auto_approve=paper_auto_approve,
                    gate_allows=gate_allows,
                    gate_reasons=[block_reason] if block_reason else [],
                    store=JournalStore(scoped),
                    broker_adapter=broker_adapter,
                )
                if auto_resolution["executed"]:
                    executed_ticket = auto_resolution["executed"][0]
                if auto_resolution["errors"]:
                    execution_error = auto_resolution["errors"][0]["error"]
                elif block_reason:
                    execution_error = block_reason
            stale_sweep = sweep_stale_pending(run_date, store=JournalStore(scoped)) if paper_auto_approve else {"closed": []}
            decision_trace = self._build_decision_trace(scoped, run_date, strategy.strategy_id)
            performance = PaperPerformanceAnalyzer(scoped).build(run_date)
            equity = PaperEquityCurve(scoped, starting_equity=strategy.starting_equity).build(run_date, performance)
            StrategyGuardrails(scoped).run(run_date)
            risk_monitor = RiskMonitor(scoped).run(run_date)
            reconciliation = PaperReconciliation(scoped).run(run_date)
            strategy_book = StrategyBook(
                scoped,
                strategy_id=strategy.strategy_id,
                strategy_config=strategy.params,
                starting_equity=strategy.starting_equity,
            ).build(run_date)
            edge_judgment = EdgeJudgment(scoped, strategy_id=strategy.strategy_id).build(run_date)
            strategy_objective = StrategyObjective(scoped, strategy_id=strategy.strategy_id).build(
                run_date, edge_judgment=edge_judgment
            )
            result = {
                "strategy_id": strategy.strategy_id,
                "symbol": strategy.symbol,
                "status": "ok",
                "output_root": str(scoped),
                "starting_equity": strategy.starting_equity,
                "execution_profile": execution_profile,
                "pending_tickets": len(pending),
                "ticket_notification_status": ticket_notification.get("status", ""),
                "ticket_notifications_sent": ticket_notification.get("sent", 0),
                "executed_ticket": executed_ticket,
                "auto_resolution": auto_resolution,
                "stale_pending_sweep": stale_sweep,
                "decision_trace_status": decision_trace.get("status", ""),
                "decision_trace_count": decision_trace.get("record_count", 0),
                "decision_trace_artifact": decision_trace.get("artifact", ""),
                "execution_error": execution_error,
                "order_recovery_status": order_recovery.get("status", ""),
                "order_recovery_count": order_recovery.get("recovered_count", 0),
                "order_recovery_block_reason": order_recovery.get("block_reason", ""),
                "risk_monitor_status": risk_monitor.get("status"),
                "risk_kill_switch_active": risk_monitor.get("kill_switch_active"),
                "risk_allow_paper_auto_approve": risk_monitor.get("allow_paper_auto_approve"),
                "demo_reconciliation_status": self._reconciliation_status(demo_reconciliation),
                "demo_reconciliation_drift_count": (demo_reconciliation or {}).get("drift_count"),
                "demo_reconciliation_error": (demo_reconciliation or {}).get("error", ""),
                "current_equity": equity.get("current_equity"),
                "current_drawdown_pct": equity.get("current_drawdown_pct"),
                "reconciliation_status": reconciliation.get("status"),
                "strategy_book_status": strategy_book.get("audit", {}).get("status"),
                "edge_judgment_label": edge_judgment.get("label"),
                "strategy_objective_score": strategy_objective.get("objective_score"),
                "winner_gate_passed": strategy_objective.get("winner_gate", {}).get("passed"),
            }
            if decision_trace.get("error"):
                result["decision_trace_error"] = decision_trace["error"]
            cycle_audit_error = cycle_audit_error or self._finalize_cycle_audit(audit_sink, audit_context, result=result)
            if cycle_audit_error:
                result["cycle_audit_error"] = cycle_audit_error
            return result
        except Exception as exc:  # one strategy must not kill the fleet — record, don't swallow
            result = {
                "strategy_id": strategy.strategy_id,
                "status": "error",
                "output_root": str(scoped),
                "error": f"{type(exc).__name__}: {exc}",
            }
            cycle_audit_error = cycle_audit_error or self._finalize_cycle_audit(audit_sink, audit_context, result=result, error=result["error"])
            if cycle_audit_error:
                result["cycle_audit_error"] = cycle_audit_error
            return result

    def _build_decision_trace(self, output_root: Path, run_date: str, strategy_id: str) -> dict:
        try:
            rows = DecisionTrace(output_root).build(run_date, strategy_id)
            return {
                "status": "pass",
                "record_count": len(rows),
                "artifact": str(output_root / "decision_traces" / f"{run_date}.json"),
            }
        except Exception as exc:  # noqa: BLE001 - trace is observability and must not alter execution.
            return {
                "status": "error",
                "record_count": 0,
                "artifact": str(output_root / "decision_traces" / f"{run_date}.json"),
                "error": f"{type(exc).__name__}: {exc}",
            }

    def _finalize_cycle_audit(self, audit_sink: JsonCycleAuditSink, audit_context: dict, *, result: dict, error: str = "") -> str:
        if not audit_context:
            return ""
        try:
            audit_sink.finalize(audit_context, result=result, error=error)
            return ""
        except Exception as exc:  # noqa: BLE001 - keep audit failures observable without altering trading behavior.
            return f"{type(exc).__name__}: {exc}"

    def _recover_demo_order_intents(self, strategy, scoped: Path, run_date: str, reconciliation: dict | None = None) -> dict:
        if self._active_demo_broker_config(strategy) is None:
            return {"status": "not_applicable", "recovered_count": 0, "recovered_ticket_ids": []}
        from services.order_lifecycle import WATCHDOG_STATES, OrderLifecycleStore

        store = OrderLifecycleStore(scoped)
        watchdog_blockers = store.watchdog_tick(run_date)
        rows = load_json(scoped / "order_lifecycle" / f"{run_date}.json")
        expired_entry_orders = self._expire_demo_entry_orders(strategy, scoped, run_date, rows)
        if expired_entry_orders.get("expired_ticket_ids") or expired_entry_orders.get("errors"):
            rows = load_json(scoped / "order_lifecycle" / f"{run_date}.json")
        if expired_entry_orders.get("blocks_new_orders"):
            report = {
                "run_date": run_date,
                "strategy_id": strategy.strategy_id,
                "status": "blocked",
                "recovered_count": len(expired_entry_orders.get("expired_ticket_ids", []) or []),
                "recovered_ticket_ids": expired_entry_orders.get("expired_ticket_ids", []),
                "recovered_orders": expired_entry_orders.get("orders", []),
                "blocks_new_orders": True,
                "block_reason": expired_entry_orders.get("block_reason", ""),
                "errors": expired_entry_orders.get("errors", []),
                "watchdog_blockers": watchdog_blockers,
                "expired_entry_orders": expired_entry_orders,
                "escalation_action": "halt_new_orders_until_expired_entry_order_is_cancelled_or_reconciled",
            }
            self._write_order_recovery(scoped, run_date, report)
            return report
        blocked_unrecoverable = [
            item for item in rows
            if isinstance(item, dict)
            and item.get("blocked")
            and str(item.get("state") or "") in WATCHDOG_STATES
            and str(item.get("state") or "") != "submitting"
        ]
        intents = [
            item for item in rows
            if isinstance(item, dict) and item.get("state") == "submitting"
        ]
        protective_recovery = self._recover_unprotected_demo_positions(strategy, scoped, run_date, rows, reconciliation or {})
        if blocked_unrecoverable:
            reason = str((blocked_unrecoverable[0].get("blocker") or {}).get("reason") or "blocked order lifecycle intent requires reconciliation")
            report = {
                "run_date": run_date,
                "strategy_id": strategy.strategy_id,
                "status": "blocked",
                "recovered_count": 0,
                "recovered_ticket_ids": [],
                "blocks_new_orders": True,
                "block_reason": reason,
                "watchdog_blockers": watchdog_blockers,
                "blocked_orders": blocked_unrecoverable,
                "escalation_action": "halt_new_orders_until_order_state_reconciled",
                "protective_recovery": protective_recovery,
                "expired_entry_orders": expired_entry_orders,
                "reconciliation_refresh_required": protective_recovery.get("refresh_required", False),
            }
            self._write_order_recovery(scoped, run_date, report)
            return report
        if not intents:
            if protective_recovery.get("status") != "clear":
                report = {
                    "run_date": run_date,
                    "strategy_id": strategy.strategy_id,
                    "status": protective_recovery.get("status", "blocked"),
                    "recovered_count": len(protective_recovery.get("recovered_ticket_ids", []) or []),
                    "recovered_ticket_ids": protective_recovery.get("recovered_ticket_ids", []),
                    "recovered_orders": protective_recovery.get("recovered_orders", []),
                    "blocks_new_orders": protective_recovery.get("blocks_new_orders", True),
                    "block_reason": protective_recovery.get("block_reason", ""),
                    "errors": protective_recovery.get("errors", []),
                    "watchdog_blockers": watchdog_blockers,
                    "protective_recovery": protective_recovery,
                    "expired_entry_orders": expired_entry_orders,
                    "reconciliation_refresh_required": protective_recovery.get("refresh_required", False),
                    "escalation_action": "halt_new_orders_until_naked_position_resolved" if protective_recovery.get("blocks_new_orders") else "",
                }
                self._write_order_recovery(scoped, run_date, report)
                return report
            recovered_ticket_ids = list(expired_entry_orders.get("expired_ticket_ids", []) or [])
            recovered_orders = list(expired_entry_orders.get("orders", []) or [])
            report = {
                "run_date": run_date,
                "strategy_id": strategy.strategy_id,
                "status": "recovered" if recovered_orders else "clear",
                "recovered_count": len(recovered_ticket_ids),
                "recovered_ticket_ids": recovered_ticket_ids,
                "recovered_orders": recovered_orders,
                "blocks_new_orders": False,
                "block_reason": "",
                "watchdog_blockers": watchdog_blockers,
                "protective_recovery": protective_recovery,
                "expired_entry_orders": expired_entry_orders,
            }
            self._write_order_recovery(scoped, run_date, report)
            return report

        from services.broker_adapter import BrokerOrderRequest

        adapter = self._broker_adapter_for(strategy, scoped)
        if adapter is None:
            report = {
                "run_date": run_date,
                "strategy_id": strategy.strategy_id,
                "status": "blocked",
                "recovered_count": 0,
                "recovered_ticket_ids": [],
                "blocks_new_orders": True,
                "block_reason": "demo order recovery has no broker adapter",
                "watchdog_blockers": watchdog_blockers,
                "expired_entry_orders": expired_entry_orders,
            }
            self._write_order_recovery(scoped, run_date, report)
            return report
        recovered_ticket_ids: list[str] = []
        recovered_orders: list[dict] = []
        errors: list[dict] = []
        blocks_new_orders = bool(protective_recovery.get("blocks_new_orders"))
        block_reason = str(protective_recovery.get("block_reason") or "")
        recovered_ticket_ids.extend(protective_recovery.get("recovered_ticket_ids", []) or [])
        recovered_orders.extend(protective_recovery.get("recovered_orders", []) or [])
        errors.extend(protective_recovery.get("errors", []) or [])
        recovered_ticket_ids.extend(expired_entry_orders.get("expired_ticket_ids", []) or [])
        recovered_orders.extend(expired_entry_orders.get("orders", []) or [])
        errors.extend(expired_entry_orders.get("errors", []) or [])
        for intent in intents:
            ticket_id = str(intent.get("ticket_id") or "")
            ticket = self._ticket_for_recovery(scoped, run_date, ticket_id)
            if not ticket:
                blocks_new_orders = True
                reason = f"cannot recover submitting order without trade ticket: {ticket_id}"
                block_reason = block_reason or reason
                errors.append({"order_id": intent.get("order_id"), "ticket_id": ticket_id, "error": reason})
                continue
            try:
                receipt = adapter.submit_order(
                    BrokerOrderRequest(
                        run_date=run_date,
                        ticket=ticket,
                        latest_price=float(intent.get("requested_price") or 0) or None,
                        actual_size=float(intent.get("requested_quantity") or 0) or None,
                    )
                )
            except (OSError, TimeoutError, RuntimeError, ValueError, KeyError) as exc:
                blocks_new_orders = True
                reason = f"{type(exc).__name__}: {exc}"
                block_reason = block_reason or reason
                errors.append({"order_id": intent.get("order_id"), "ticket_id": ticket_id, "error": reason})
                continue
            current = load_json(scoped / "order_lifecycle" / f"{run_date}.json")
            current_state = next((item.get("state") for item in current if isinstance(item, dict) and item.get("order_id") == intent.get("order_id")), "")
            recovered_orders.append({"order_id": receipt.order_id, "ticket_id": ticket_id, "status": receipt.status, "state": current_state})
            if current_state == "submitting":
                blocks_new_orders = True
                reason = "submitting order recovery remains ambiguous; no duplicate order submitted"
                block_reason = block_reason or reason
                errors.append({"order_id": receipt.order_id, "ticket_id": ticket_id, "error": reason})
                continue
            self._record_recovered_journal_decision(scoped, run_date, ticket, receipt.to_dict())
            recovered_ticket_ids.append(ticket_id)

        status = "blocked" if blocks_new_orders else "recovered" if recovered_orders else "clear"
        report = {
            "run_date": run_date,
            "strategy_id": strategy.strategy_id,
            "status": status,
            "recovered_count": len(recovered_ticket_ids),
            "recovered_ticket_ids": recovered_ticket_ids,
            "recovered_orders": recovered_orders,
            "blocks_new_orders": blocks_new_orders,
            "block_reason": block_reason,
            "errors": errors,
            "watchdog_blockers": watchdog_blockers,
            "protective_recovery": protective_recovery,
            "expired_entry_orders": expired_entry_orders,
            "reconciliation_refresh_required": protective_recovery.get("refresh_required", False),
            "escalation_action": "halt_new_orders_until_order_state_reconciled" if blocks_new_orders else "",
        }
        self._write_order_recovery(scoped, run_date, report)
        return report

    def _expire_demo_entry_orders(self, strategy, scoped: Path, run_date: str, lifecycle_rows: list[dict]) -> dict:
        store = OrderLifecycleStore(scoped)
        expired_ticket_ids: list[str] = []
        orders: list[dict] = []
        errors: list[dict] = []
        adapter = None
        block_reason = ""
        for lifecycle in lifecycle_rows:
            if not isinstance(lifecycle, dict) or str(lifecycle.get("state") or "") != "accepted":
                continue
            ticket_id = str(lifecycle.get("ticket_id") or "")
            ticket = self._ticket_for_recovery(scoped, run_date, ticket_id)
            if str(ticket.get("order_type", "")).lower() != "limit":
                continue
            status = evaluate_limit_entry_status(ticket, load_entry_candles(scoped, run_date, ticket))
            if status.get("status") != "expired":
                continue
            if adapter is None:
                adapter = self._broker_adapter_for(strategy, scoped)
            if adapter is None:
                reason = "expired demo limit order requires a broker cancel capability"
                block_reason = block_reason or reason
                errors.append({"order_id": lifecycle.get("order_id"), "ticket_id": ticket_id, "error": reason, "expiry": status})
                continue
            try:
                require_broker_capability(adapter, BrokerCapability.CANCEL_ORDER)
                metadata = lifecycle.get("metadata") if isinstance(lifecycle.get("metadata"), dict) else {}
                client_order_id = str(metadata.get("client_order_id") or lifecycle.get("idempotency_key") or "")
                broker_order_id = str(metadata.get("broker_order_id") or "")
                cancel_response = adapter.cancel_order(
                    BrokerCancelRequest(
                        run_date=run_date,
                        asset=str(ticket.get("asset") or "GOLD"),
                        client_order_id=client_order_id,
                        broker_order_id=broker_order_id,
                    )
                )
                store.transition(
                    run_date,
                    str(lifecycle.get("order_id") or ""),
                    "expired",
                    reason="limit_entry_ttl_expired_cancelled",
                    metadata={"expiry": status, "cancel_response": cancel_response},
                )
                paper_order = {
                    "order_id": str(lifecycle.get("order_id") or ""),
                    "ticket_id": ticket_id,
                    "status": "expired",
                    "requested_price": lifecycle.get("requested_price"),
                    "fill_price": None,
                    "quantity": lifecycle.get("requested_quantity"),
                    "filled_at": "",
                    "rejection_reason": status.get("reason") or "limit entry expired",
                    "cancel_response": cancel_response,
                }
                self._record_recovered_journal_decision(scoped, run_date, ticket, paper_order)
                expired_ticket_ids.append(ticket_id)
                orders.append({"order_id": paper_order["order_id"], "ticket_id": ticket_id, "status": "expired", "state": "expired"})
            except (OSError, TimeoutError, RuntimeError, ValueError, KeyError, UnsupportedBrokerCapability) as exc:
                reason = f"{type(exc).__name__}: {exc}"
                block_reason = block_reason or reason
                errors.append({"order_id": lifecycle.get("order_id"), "ticket_id": ticket_id, "error": reason, "expiry": status})
        return {
            "status": "blocked" if errors else "expired" if expired_ticket_ids else "clear",
            "expired_count": len(expired_ticket_ids),
            "expired_ticket_ids": expired_ticket_ids,
            "orders": orders,
            "errors": errors,
            "blocks_new_orders": bool(errors),
            "block_reason": block_reason,
        }

    def _recover_unprotected_demo_positions(self, strategy, scoped: Path, run_date: str, lifecycle_rows: list[dict], reconciliation: dict) -> dict:
        if not reconciliation or not reconciliation.get("suspected_naked_position"):
            return {"status": "clear", "blocks_new_orders": False, "recovered_ticket_ids": [], "recovered_orders": [], "errors": [], "actions": [], "refresh_required": False}
        risks = [
            item for item in reconciliation.get("naked_position_risks", [])
            if isinstance(item, dict) and item.get("missing_protective_order")
        ]
        if not risks:
            return {"status": "clear", "blocks_new_orders": False, "recovered_ticket_ids": [], "recovered_orders": [], "errors": [], "actions": [], "refresh_required": False}
        adapter = self._broker_adapter_for(strategy, scoped)
        try:
            if adapter is None:
                raise UnsupportedBrokerCapability("broker adapter is unavailable")
            require_broker_capability(adapter, BrokerCapability.PROTECTIVE_RECOVERY)
        except UnsupportedBrokerCapability as exc:
            reason = "demo naked position recovery has no broker adapter capable of attaching protective orders"
            return {
                "status": "blocked",
                "blocks_new_orders": True,
                "block_reason": reason,
                "recovered_ticket_ids": [],
                "recovered_orders": [],
                "errors": [{"error": reason, "detail": str(exc)}],
                "actions": [],
                "refresh_required": False,
            }
        candidates = [
            item for item in lifecycle_rows
            if isinstance(item, dict) and str(item.get("state") or "") in {"filled", "protective_failed", "protective_attached"}
        ]
        actions: list[dict] = []
        errors: list[dict] = []
        recovered_ticket_ids: list[str] = []
        recovered_orders: list[dict] = []
        blocks_new_orders = False
        block_reason = ""
        for risk in risks:
            exchange_symbol = str(risk.get("exchange_symbol") or "").upper()
            lifecycle = self._lifecycle_for_exchange_symbol(candidates, exchange_symbol)
            if not lifecycle:
                blocks_new_orders = True
                reason = f"cannot recover naked {exchange_symbol} position without filled/protective_failed lifecycle record"
                block_reason = block_reason or reason
                errors.append({"exchange_symbol": exchange_symbol, "error": reason})
                continue
            try:
                action = adapter.recover_protective_orders(
                    BrokerProtectiveRecoveryRequest(
                        run_date=run_date,
                        lifecycle_record=lifecycle,
                        exchange_position=self._exchange_position_for_risk(reconciliation, risk),
                        source="order_recovery_missing_protective",
                    )
                )
            except (OSError, TimeoutError, RuntimeError, ValueError, KeyError) as exc:
                blocks_new_orders = True
                reason = f"{type(exc).__name__}: {exc}"
                block_reason = block_reason or reason
                errors.append({"order_id": lifecycle.get("order_id"), "exchange_symbol": exchange_symbol, "error": reason})
                continue
            actions.append(action)
            recovered_orders.append(
                {
                    "order_id": lifecycle.get("order_id", ""),
                    "ticket_id": lifecycle.get("ticket_id", ""),
                    "status": action.get("status", ""),
                    "state": OrderLifecycleStore(scoped).current(run_date, str(lifecycle.get("order_id") or "")).get("state", ""),
                    "recovery_action": action.get("action", ""),
                }
            )
            if action.get("status") == "recovered":
                recovered_ticket_ids.append(str(lifecycle.get("ticket_id") or ""))
            else:
                blocks_new_orders = True
                reason = action.get("block_reason") or action.get("protective_status") or "missing protective recovery did not complete"
                block_reason = block_reason or str(reason)
                errors.append({"order_id": lifecycle.get("order_id"), "exchange_symbol": exchange_symbol, "error": str(reason)})
        status = "blocked" if blocks_new_orders else "recovered" if actions else "clear"
        return {
            "status": status,
            "blocks_new_orders": blocks_new_orders,
            "block_reason": block_reason,
            "recovered_ticket_ids": recovered_ticket_ids,
            "recovered_orders": recovered_orders,
            "errors": errors,
            "actions": actions,
            "refresh_required": bool(actions),
        }

    def _lifecycle_for_exchange_symbol(self, rows: list[dict], exchange_symbol: str) -> dict:
        if not exchange_symbol:
            return rows[0] if rows else {}
        for row in rows:
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            ticket = metadata.get("ticket") if isinstance(metadata.get("ticket"), dict) else {}
            row_symbol = str(metadata.get("symbol") or "").upper()
            if row_symbol == exchange_symbol:
                return row
            asset = str(ticket.get("asset") or "GOLD")
            configured = str((load_pipeline_config().get("broker", {}) or {}).get("instrument_map", {}).get(asset, "")).upper()
            if configured == exchange_symbol:
                return row
        return rows[0] if len(rows) == 1 else {}

    def _exchange_position_for_risk(self, reconciliation: dict, risk: dict) -> dict:
        exchange_symbol = str(risk.get("exchange_symbol") or "").upper()
        for position in reconciliation.get("exchange_positions", []):
            if isinstance(position, dict) and str(position.get("symbol") or "").upper() == exchange_symbol:
                return position
        protection = risk.get("position_protection") if isinstance(risk.get("position_protection"), dict) else {}
        if protection:
            return {
                "symbol": exchange_symbol,
                "position_amt": protection.get("position_amt", risk.get("exchange_qty", 0)),
                "entry_price": risk.get("entry_price", 0),
            }
        return {"symbol": exchange_symbol, "position_amt": risk.get("exchange_qty", 0), "entry_price": risk.get("entry_price", 0)}

    def _ticket_for_recovery(self, scoped: Path, run_date: str, ticket_id: str) -> dict:
        ticket = next((item for item in load_json(scoped / "trade_tickets" / f"{run_date}.json") if item.get("ticket_id") == ticket_id), {})
        if ticket:
            return ticket
        lifecycle = next(
            (
                item for item in load_json(scoped / "order_lifecycle" / f"{run_date}.json")
                if isinstance(item, dict) and item.get("ticket_id") == ticket_id
            ),
            {},
        )
        snapshot = (lifecycle.get("metadata") or {}).get("ticket") if isinstance(lifecycle, dict) else {}
        return snapshot if isinstance(snapshot, dict) and snapshot.get("ticket_id") == ticket_id else {}

    def _record_recovered_journal_decision(self, scoped: Path, run_date: str, ticket: dict, paper_order: dict) -> None:
        ticket_id = str(ticket.get("ticket_id") or "")
        pending_path = scoped / "journal_pending" / f"{run_date}.json"
        pending_rows = load_json(pending_path)
        pending_item = next((item for item in pending_rows if item.get("ticket_id") == ticket_id), {"ticket_id": ticket_id, "signal_id": ticket.get("signal_id", ""), "asset": ticket.get("asset", "GOLD")})
        decisions_path = scoped / "journal_decisions" / f"{run_date}.json"
        decisions = [item for item in load_json(decisions_path) if item.get("ticket_id") != ticket_id]
        status = str(paper_order.get("status") or "")
        decision = "rejected" if status in {"rejected", "cancelled", "expired"} else "executed_paper"
        decisions.append(
            {
                **pending_item,
                "decision_status": decision,
                "decided_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "actual_entry": paper_order.get("fill_price") or paper_order.get("requested_price"),
                "actual_size": paper_order.get("quantity"),
                "paper_order": paper_order,
                "broker_order": None,
                "risk_snapshot": {
                    "max_loss_pct": float(ticket.get("max_loss_pct", 0) or 0),
                    "daily_loss_stop_pct": float(load_risk_rules().get("default", {}).get("daily_loss_stop_pct", 1.25)),
                },
                "notes": "recovered from durable order intent after runner restart",
            }
        )
        write_json(decisions_path, decisions)
        write_json(pending_path, [item for item in pending_rows if item.get("ticket_id") != ticket_id])

    def _write_order_recovery(self, scoped: Path, run_date: str, report: dict) -> None:
        write_json(scoped / "order_recovery" / f"{run_date}.json", [report])
        write_json(scoped / "order_recovery" / "current.json", [report])


def run_all_strategies(run_date: str, output_root: Path | None = None, paper_auto_approve: bool = False) -> dict:
    return MultiStrategyRunner(output_root=output_root).run(run_date, paper_auto_approve=paper_auto_approve)
