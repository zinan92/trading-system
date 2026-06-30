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
from services.journal_store import JournalStore, load_json, write_json
from services.live_env import apply_live_env, live_env_value_present
from services.paper_equity_curve import PaperEquityCurve
from services.paper_performance import PaperPerformanceAnalyzer
from services.paper_reconciliation import PaperReconciliation
from services.risk_monitor import RiskMonitor
from services.backend_maturity_audit import BackendMaturityAudit
from services.strategy_leaderboard import StrategyLeaderboard
from services.strategy_frequency_governance import StrategyFrequencyGovernance
from services.strategy_registry import StrategyRegistry
from services.strategy_guardrails import StrategyGuardrails
from services.strategy_daily_review import StrategyDailyReview
from services.trade_quality import DailyTradeSampler
from services.strategy_book import StrategyBook
from services.edge_judgment import EdgeJudgment
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
        demo = config.get("demo_trading", {}) or {}
        if demo.get("enabled") is True and str(demo.get("active_strategy_id", "")) == strategy.strategy_id:
            from services.binance_demo_broker_adapter import BinanceDemoBrokerAdapter

            profile_name = str(demo.get("broker_profile", config.get("broker", {}).get("provider", "binance_usdm")))
            broker_config = (config.get("broker_profiles", {}) or {}).get(profile_name) or config.get("broker", {})
            return BinanceDemoBrokerAdapter(scoped, broker_config, demo)

        # A `live` strategy routes execution through the live broker (its own
        # scoped namespace); everyone else returns None -> the default paper path.
        # The live adapter's gates (live_trading_enabled / dry_run / activation)
        # still decide whether a real order is actually sent.
        if not getattr(strategy, "live", False):
            return None
        from services.broker_adapter import LiveBrokerAdapter

        return LiveBrokerAdapter(scoped, bool(config.get("live_trading_enabled", False)), config.get("broker", {}))

    def _active_demo_broker_config(self, strategy) -> tuple[dict, dict] | None:
        config = load_pipeline_config()
        demo = config.get("demo_trading", {}) or {}
        if demo.get("enabled") is True and str(demo.get("active_strategy_id", "")) == strategy.strategy_id:
            profile_name = str(demo.get("broker_profile", config.get("broker", {}).get("provider", "binance_usdm")))
            broker_config = (config.get("broker_profiles", {}) or {}).get(profile_name) or config.get("broker", {})
            merged = {
                **broker_config,
                "provider": "binance_usdm",
                "environment": "demo",
                "base_url": "https://demo-fapi.binance.com",
                "dry_run": False,
                "request_dir": str(demo.get("request_dir", broker_config.get("request_dir", "demo_order_requests"))),
                "protective_failure_action": str(demo.get("protective_failure_action", "reduce_only_close")),
                "instrument_map": {"GOLD": "XAUUSDT", "XAUUSD": "XAUUSDT", **broker_config.get("instrument_map", {})},
            }
            return merged, demo
        return None

    def _execution_profile_for(self, strategy) -> dict:
        config = load_pipeline_config()
        demo = config.get("demo_trading", {}) or {}
        if demo.get("enabled") is True and str(demo.get("active_strategy_id", "")) == strategy.strategy_id:
            from services.binance_demo_broker_adapter import DEMO_BASE_URL, DEMO_SYMBOL

            profile_name = str(demo.get("broker_profile", config.get("broker", {}).get("provider", "binance_usdm")))
            broker_config = (config.get("broker_profiles", {}) or {}).get(profile_name) or config.get("broker", {})
            apply_live_env()
            key_env = str(broker_config.get("api_key_env", "BINANCE_API_KEY"))
            secret_env = str(broker_config.get("api_secret_env", "BINANCE_API_SECRET"))
            credentials_present = bool(live_env_value_present(key_env) and live_env_value_present(secret_env))
            return {
                "adapter": "binance_demo",
                "mode": "binance_futures_demo",
                "strategy_id": strategy.strategy_id,
                "armed": bool(credentials_present),
                "credentials_present": credentials_present,
                "credential_env_names": [key_env, secret_env],
                "endpoint": DEMO_BASE_URL,
                "symbol": DEMO_SYMBOL,
                "live_endpoint_allowed": False,
                "max_order_quantity": float(demo.get("max_order_quantity", 0.002)),
                "require_flat_before_entry": bool(demo.get("require_flat_before_entry", True)),
                "protective_failure_action": str(demo.get("protective_failure_action", "reduce_only_close")),
                "request_dir": str(demo.get("request_dir", "demo_order_requests")),
                "note": "Automatic execution uses Binance Futures Demo only; no live Binance endpoint is allowed by this adapter.",
            }
        if getattr(strategy, "live", False):
            broker = config.get("broker", {})
            return {
                "adapter": "live",
                "mode": "live_adapter_gated",
                "strategy_id": strategy.strategy_id,
                "armed": bool(config.get("live_trading_enabled", False) and not broker.get("dry_run", True)),
                "provider": broker.get("provider", ""),
                "dry_run": bool(broker.get("dry_run", True)),
                "note": "Live adapter remains behind live readiness and approval gates.",
            }
        return {
            "adapter": "paper",
            "mode": "paper_sim",
            "strategy_id": strategy.strategy_id,
            "armed": False,
            "note": "Paper simulator only; no broker network order is submitted.",
        }

    def _demo_reconciliation_for(self, strategy, scoped: Path, run_date: str) -> dict | None:
        active = self._active_demo_broker_config(strategy)
        if active is None:
            return None
        broker_config, _demo = active
        from services.live_reconciliation import LiveBrokerReconciliation

        return LiveBrokerReconciliation(scoped, broker_config).run(run_date)

    def _demo_reconciliation_block_reason(self, report: dict) -> str:
        if report.get("suspected_naked_position"):
            return f"Binance demo suspected naked position: {report.get('escalation_action') or report.get('reason_code')}"
        if report.get("confirmation_status") == "cannot_confirm":
            return f"Binance demo reconciliation cannot confirm venue state: {report.get('error')}"
        if report.get("error"):
            return f"Binance demo reconciliation failed: {report['error']}"
        reasons = sorted({str(item.get("reason", "reconciliation drift")) for item in report.get("drifts", [])})
        suffix = "; ".join(reasons) if reasons else "unknown drift"
        return f"Binance demo reconciliation drift: {suffix}"

    def _reconciliation_status(self, report: dict | None) -> str:
        if report is None:
            return ""
        if report.get("confirmation_status") == "cannot_confirm":
            return "cannot_confirm"
        if report.get("suspected_naked_position"):
            return "naked_position_suspected"
        if report.get("reconciled"):
            return "pass"
        if report.get("error"):
            return "error"
        return "drift"

    def run(self, run_date: str, paper_auto_approve: bool = False) -> dict:
        classification_audit = self.registry.classification_audit()
        runnable = self.registry.enabled_for_runner()
        skipped = [
            {
                "strategy_id": item.get("strategy_id"),
                "status": "skipped",
                "reason": "classification_incomplete",
                "classification_audit": item,
            }
            for item in classification_audit.get("strategies", [])
            if item.get("status") != "pass" and self.registry.get(str(item.get("strategy_id") or "")) and self.registry.get(str(item.get("strategy_id") or "")).enabled
        ]
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
            order_recovery = self._recover_demo_order_intents(strategy, scoped, run_date)
            demo_reconciliation = self._demo_reconciliation_for(strategy, scoped, run_date)
            pending = load_json(scoped / "journal_pending" / f"{run_date}.json")
            recovered_ticket_ids = list(order_recovery.get("recovered_ticket_ids", []) or [])
            executed_ticket = recovered_ticket_ids[0] if recovered_ticket_ids else None
            execution_error = ""
            if paper_auto_approve and pending:
                ticket_id = pending[0].get("ticket_id")
                if recovered_ticket_ids:
                    execution_error = "recovered order intent this cycle; skipped new auto approval to avoid stacking exposure"
                elif order_recovery.get("blocks_new_orders"):
                    execution_error = str(order_recovery.get("block_reason") or "demo order recovery blocks new execution")
                elif ticket_id in set(recovered_ticket_ids):
                    executed_ticket = ticket_id
                elif demo_reconciliation and not demo_reconciliation.get("reconciled", False):
                    execution_error = self._demo_reconciliation_block_reason(demo_reconciliation)
                else:
                    try:
                        broker_adapter = self._broker_adapter_for(strategy, scoped)
                        JournalStore(scoped).record_decision(
                            run_date=run_date,
                            ticket_id=ticket_id,
                            decision="executed_paper",
                            notes=f"multi-strategy auto execution ({strategy.strategy_id})",
                            broker_adapter=broker_adapter,
                        )
                        executed_ticket = ticket_id
                    except (ValueError, OSError, KeyError, RuntimeError) as exc:
                        # RuntimeError covers the live adapter's own gates (e.g.
                        # live_trading_enabled is false): a live-flagged strategy
                        # records the block and keeps running paper-side, never a
                        # hard fleet error.
                        execution_error = str(exc)
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
            result = {
                "strategy_id": strategy.strategy_id,
                "symbol": strategy.symbol,
                "status": "ok",
                "output_root": str(scoped),
                "starting_equity": strategy.starting_equity,
                "execution_profile": execution_profile,
                "pending_tickets": len(pending),
                "executed_ticket": executed_ticket,
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
            }
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

    def _finalize_cycle_audit(self, audit_sink: JsonCycleAuditSink, audit_context: dict, *, result: dict, error: str = "") -> str:
        if not audit_context:
            return ""
        try:
            audit_sink.finalize(audit_context, result=result, error=error)
            return ""
        except Exception as exc:  # noqa: BLE001 - keep audit failures observable without altering trading behavior.
            return f"{type(exc).__name__}: {exc}"

    def _recover_demo_order_intents(self, strategy, scoped: Path, run_date: str) -> dict:
        if self._active_demo_broker_config(strategy) is None:
            return {"status": "not_applicable", "recovered_count": 0, "recovered_ticket_ids": []}
        from services.order_lifecycle import WATCHDOG_STATES, OrderLifecycleStore

        store = OrderLifecycleStore(scoped)
        watchdog_blockers = store.watchdog_tick(run_date)
        rows = load_json(scoped / "order_lifecycle" / f"{run_date}.json")
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
            }
            self._write_order_recovery(scoped, run_date, report)
            return report
        if not intents:
            report = {
                "run_date": run_date,
                "strategy_id": strategy.strategy_id,
                "status": "clear",
                "recovered_count": 0,
                "recovered_ticket_ids": [],
                "blocks_new_orders": False,
                "block_reason": "",
                "watchdog_blockers": watchdog_blockers,
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
            }
            self._write_order_recovery(scoped, run_date, report)
            return report
        recovered_ticket_ids: list[str] = []
        recovered_orders: list[dict] = []
        errors: list[dict] = []
        blocks_new_orders = False
        block_reason = ""
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
            "escalation_action": "halt_new_orders_until_order_state_reconciled" if blocks_new_orders else "",
        }
        self._write_order_recovery(scoped, run_date, report)
        return report

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
